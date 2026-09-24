#!/usr/bin/env python3
"""
Stage 1-5 end-to-end runner - the chained production path the harness is not.

`eval/harness.py` scores each stage independently against gold labels
(CLAUDE.md: "The harness scores stages independently, not as a chain"), which
is the right shape for measuring one stage but means no code path has ever run
the pipeline the way production would. This is that path:

    ingest (optional) -> Stage 1 -> Stage 2 -> Stage 3 -> Stage 4 -> Stage 5
                                         -> runs/<account>/catalog.json

Three things differ from a harness run, all deliberate:

1. **No golden set is read.** Nothing here needs, or can use, gold labels - so
   it runs against any account the moment its raw dump exists, including one
   that has never been hand-labeled. Accuracy is therefore not reported; this
   answers "what does the catalog look like", not "how good is it".
2. **Stage 3 and 4 run on Stage 2's PREDICTION**, not on gold `post_type`. A
   post Stage 2 calls `announcement` is routed straight to `auto_exclude` and
   never reaches extraction - which is also why per-stage harness accuracy
   (which assumes perfect upstream routing) is an upper bound on this.
3. **The Stage 1 profile is threaded into Stages 2/3/4.** This path always did;
   the harness did not until 2026-09-09, which is why every figure recorded
   before that date was measured on inputs this path never uses (see
   README.md). Both now pass it, so the two are comparable.

Every stage is resolved through `harness.load_stage_fn()`, so the experiment
YAML is the single source of truth for models exactly as it is for a scored
run, and the reserved-parameter tripwire applies identically.

Cost and escalation figures in the summary are read back from
`report/token_log.csv` filtered to this run's `run_id` - the log records the
model string passed to litellm at call time, so the summary cannot be fooled
by a config's display label (README.md).

Stages run batched - Stage 2 over every post, then Stage 3 over only the posts
Stage 2 called listings, and so on - rather than one post through all five. The
work and the call count are identical either way (Stages 3/4 are still gated on
Stage 2's prediction); batching just means each stage has a moment where it is
finished, which is what `--live` prints a summary at.

Usage:
    uv run scripts/run_pipeline.py --account vendor_gadgets_01
    uv run scripts/run_pipeline.py --account vendor_gadgets_01 --limit 8
    uv run scripts/run_pipeline.py --account vendor_new --ingest --token-env IG_ACCESS_TOKEN_GADGETS
    uv run scripts/run_pipeline.py --account vendor_autos_01 --changes report/changes.json
    uv run scripts/run_pipeline.py --account vendor_new_01 --live --limit 10

Writes runs/<account>/catalog.json, one JSON file per Stage 5 bucket under
runs/<account>/buckets/, and rows in report/token_log.csv. Never touches
eval/golden/ or data/snapshots/.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
# eval/ is not a package (no __init__.py) and harness.py is written to run as
# a script - same two-path setup tests/conftest.py uses.
sys.path.insert(0, str(REPO_ROOT / "eval"))

from harness import load_stage_fn  # noqa: E402
from make_golden_skeleton import build_comments_list, latest_dump  # noqa: E402

from pipeline.config.experiment_schema import (  # noqa: E402
    DEFAULT_EXPERIMENT_PATH,
    load_experiment_config,
)
from pipeline.llm_client import read_run_usage  # noqa: E402
from pipeline.logging_config import configure_logging  # noqa: E402
from pipeline.stages.stage1_profile import get_or_create_profile  # noqa: E402
from pipeline.stages.stage5_reconcile import route_post  # noqa: E402
from pipeline.types import Post, vision_image_url  # noqa: E402

logger = logging.getLogger(__name__)


def normalize_post(raw: dict, vendor_username: str | None) -> Post:
    """Raw Graph API media row -> the Post shape every stage expects.

    Mirrors eval/make_golden_skeleton.py's pre-filled fields exactly (raw
    dumps key by "id", stages read "post_id"), minus every hand-labeled
    field - there are no gold labels on this path.
    """
    return {
        "post_id": raw["id"],
        "media_type": raw.get("media_type"),
        "caption": raw.get("caption", ""),
        "media_url": raw.get("media_url"),
        "thumbnail_url": raw.get("thumbnail_url"),
        "media_product_type": raw.get("media_product_type"),
        "permalink": raw.get("permalink"),
        "timestamp": raw.get("timestamp"),
        "has_carousel_children": "children" in raw,
        "comment_count": len(raw.get("comments", [])),
        "comments": build_comments_list(raw, vendor_username),
    }


BUCKETS = ("auto_import", "needs_attention", "auto_exclude")

NAME_SNIPPET_CHARS = 32


def _banner(title: str) -> None:
    print("")
    print(f"=== {title} ".ljust(78, "="))


def _trace(index: int, total: int, post_id: str, detail: str) -> None:
    print(f"  [{index}/{total}] {post_id}  {detail}")


def _money(price: dict | None) -> str:
    """Human-readable price, distinguishing 'no price exists' from 'a price was
    claimed but no number came back' - the second is a model defect worth seeing
    on screen, the first is a correct and common answer (brief section 11)."""
    price = price or {}
    value = price.get("value")
    if not isinstance(value, int):
        source = price.get("source") or "none"
        return "no price" if source == "none" else f"price missing (source={source})"
    return f"{price.get('currency') or ''}{value:,}".strip()


def _triage_brief(result: dict) -> str:
    post_type = result.get("post_type") or "unknown"
    confidence = result.get("confidence")
    shown = f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "?"
    return f"{post_type:<18} conf={shown}" + ("  [vision]" if result.get("escalated") else "")


def _product_brief(products: list[dict]) -> str:
    if not products:
        return "no products extracted"
    first = products[0]
    name = (first.get("name") or "(unnamed)")[:NAME_SNIPPET_CHARS]
    extra = f"  (+{len(products) - 1} more)" if len(products) > 1 else ""
    return f"{len(products)} product(s)  {name} - {_money(first.get('price'))}{extra}"


def _stage_calls(run_id: str, stage: str) -> int:
    """Calls logged for one stage of this run, read back from
    report/token_log.csv rather than counted in-process - the same reason
    print_summary() reads usage from the log (README.md)."""
    return read_run_usage(run_id)["by_stage"].get(stage, {}).get("calls", 0)


def _print_stage1(profile: dict) -> None:
    _banner("Stage 1: account profile - one LLM call, cached per account")
    print(json.dumps(profile, indent=2, ensure_ascii=False))


def _print_stage2_summary(stage2_by_id: dict[str, dict]) -> None:
    total = len(stage2_by_id)
    kinds = Counter(result.get("post_type") or "unknown" for result in stage2_by_id.values())
    escalated = sum(1 for result in stage2_by_id.values() if result.get("escalated"))
    rate = escalated / total if total else 0.0

    print("")
    print(f"  Triaged {total} post(s):")
    for kind, count in kinds.most_common():
        print(f"    {kind:<20} {count:>3}")
    print(f"    {'vision escalations':<20} {escalated:>3}/{total} = {rate * 100:.0f}%")
    if rate > 0.4:
        print("    *** >40% - brief section 5 says fix the text prompt before reaching "
              "for a bigger model ***")


def _print_stage3_summary(stage3_by_id: dict[str, list[dict]], run_id: str) -> None:
    products = [product for result in stage3_by_id.values() for product in result]
    sources = Counter((product.get("price") or {}).get("source") or "none" for product in products)
    confidences = [
        product["extraction_confidence"] for product in products
        if isinstance(product.get("extraction_confidence"), (int, float))
    ]
    priced = sum(count for source, count in sources.items() if source != "none")

    print("")
    print(f"  Extracted {len(products)} product(s) from {len(stage3_by_id)} listing(s):")
    print(f"    {'with a price':<20} {priced:>3}/{len(products)}")
    for source, count in sources.most_common():
        print(f"      price.source={source:<12} {count:>3}")
    if confidences:
        print(f"    {'avg confidence':<20} {sum(confidences) / len(confidences):.2f}")
    print(f"    {'vision escalations':<20} {_stage_calls(run_id, 'stage3_extract_pass_b'):>3}")

    # No golden set on this path, so there is no accuracy to report - showing a
    # few real extractions is what makes the numbers above mean something.
    if products:
        print("    Sample:")
        for product in products[:3]:
            name = (product.get("name") or "(unnamed)")[:44]
            print(f"      {name} - {_money(product.get('price'))}")


def _print_stage4_summary(stage4_by_id: dict[str, list[dict]], run_id: str) -> None:
    signals = Counter(signal.get("signal") for result in stage4_by_id.values() for signal in result)
    with_signals = sum(1 for result in stage4_by_id.values() if result)
    calls = _stage_calls(run_id, "stage4_signals")
    considered = len(stage4_by_id)

    print("")
    print(f"  Signals on {with_signals}/{considered} listing(s):")
    for signal, count in signals.most_common():
        print(f"    {signal:<22} {count:>3}")
    if not signals:
        print("    (none detected)")
    print(f"    {'LLM calls made':<22} {calls:>3}/{considered}")
    # Only claim the prefilter did the saving when the stage demonstrably reached
    # a model at all - zero logged calls means something else is going on.
    if 0 < calls < considered:
        print(f"    {'prefilter skipped':<22} {considered - calls:>3}  at zero cost")


def _print_stage5_summary(items: list[dict]) -> None:
    """Per-post routing detail - which posts need a human, and why.

    print_summary() reports the same buckets as counts; this is the part a
    reviewer actually acts on, so it names posts rather than totalling them.
    """
    buckets = Counter(item["bucket"] for item in items)

    print("")
    for bucket in BUCKETS:
        count = buckets.get(bucket, 0)
        share = count / len(items) * 100 if items else 0.0
        print(f"    {bucket:<18} {count:>3}/{len(items)} ({share:.0f}%)")

    attention = [item for item in items if item["bucket"] == "needs_attention"]
    if attention:
        print("")
        print("  Needs attention - these go to a human, with the reason attached:")
        for item in attention:
            print(f"    {item['post_id']}: {'; '.join(item['flags'])}")

    excluded = [item for item in items if item["bucket"] == "auto_exclude"]
    if excluded:
        print("")
        print("  Auto-excluded - not product listings:")
        for item in excluded:
            print(f"    {item['post_id']}: {item.get('post_type')}")


def write_bucket_files(catalog: dict, catalog_path: Path) -> dict[str, Path]:
    """One JSON file per Stage 5 bucket, beside the catalog.

    catalog.json holds every post in one list, so inspecting just the posts that
    need a human means filtering it by hand. These are the same item objects,
    pre-split - each carries the `flags` that explain why it landed where it did.
    """
    bucket_dir = catalog_path.parent / "buckets"
    bucket_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    for bucket in BUCKETS:
        items = [item for item in catalog["items"] if item["bucket"] == bucket]
        path = bucket_dir / f"{bucket}.json"
        path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
        written[bucket] = path
    return written


def posts_needing_work(changes_path: Path) -> set[str]:
    """Post ids from a Stage 6 sync report that still need Stages 2-5 run on them.

    This is the Stage 6 -> pipeline handoff, and it is the whole economic point
    of sync: a routine re-sync finds that most of the feed is byte-identical to
    the snapshot, and those posts must cost nothing. Only three change types
    carry downstream work:

      new_post       - never seen; needs the full cascade
      caption_edit   - the text every stage reads has changed; re-extract
      comment_delta  - new comments; Stage 4 reads them for sold/stock signals

    Deliberately excluded: `content_changed` (the content hash moved but neither
    caption nor comment count did), `repost_match` and `repost_merge` (already
    resolved against an existing item), and every no-op. Re-running the cascade
    on those is exactly the spend Stage 6 exists to avoid.

    This used to explain `content_changed` as CDN churn. That was wrong, and it
    is worth recording because the wrong explanation is what kept the real cause
    hidden: `compute_content_hash()` has always excluded `media_url` precisely
    so rotating oh=/oe= params cannot move it. The actual cause was the hash's
    `len(comments)` component - a live fetch returns an empty comments array
    while the golden-derived baseline had hand-authored ones - and that
    component is gone. See README.md.
    """
    report = json.loads(changes_path.read_text(encoding="utf-8"))
    actionable = {"new_post", "caption_edit", "comment_delta"}
    return {
        entry["post_id"]
        for entry in report.get("changes", [])
        if entry.get("change_type") in actionable
    }


def run_pipeline(
    account_label: str,
    config_path: Path,
    limit: int | None = None,
    out_path: Path | None = None,
    post_ids: set[str] | None = None,
    live: bool = False,
    force_profile: bool = False,
) -> dict:
    """Runs Stages 1-5 chained over the latest raw dump and returns the catalog.

    post_ids restricts the run to those posts - the incremental path a Stage 6
    sync feeds. Applied before `limit`, so the two compose predictably.

    live prints a per-post trace and a summary as each stage finishes, for
    walking an audience through an onboarding run. It changes what is printed,
    never what is computed or called.

    force_profile re-runs Stage 1 even when runs/<account>/profile.json already
    exists - otherwise a rehearsed demo silently shows a cached profile and makes
    no call.
    """
    config = load_experiment_config(config_path)
    dump_path = latest_dump(account_label)
    dump = json.loads(dump_path.read_text(encoding="utf-8"))
    vendor_username = dump.get("profile", {}).get("username")

    posts = [normalize_post(raw, vendor_username) for raw in dump["media"]]
    total_in_dump = len(posts)
    if post_ids is not None:
        posts = [p for p in posts if p["post_id"] in post_ids]
        missing = post_ids - {p["post_id"] for p in posts}
        if missing:
            # Loud, not fatal: a post can be in the live feed the sync read but
            # absent from the dump on disk. Processing the rest beats refusing.
            logger.warning(
                "%d requested post(s) are not in %s and will be skipped: %s. "
                "Re-ingest to pick them up.",
                len(missing), dump_path.name, ", ".join(sorted(missing)),
            )
        logger.info("Incremental run: %d of %d post(s) in the dump need work (%.0f%% skipped)",
                     len(posts), total_in_dump,
                     100 * (1 - len(posts) / total_in_dump) if total_in_dump else 0)
    if limit is not None:
        posts = posts[:limit]

    run_id = f"pipeline_{account_label}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    vendor_id = account_label

    logger.info("Running Stages 1-5 over %d post(s) from %s", len(posts), dump_path.name)
    logger.info("  config=%s  run_id=%s", config.name, run_id)

    # --- Stage 1: once per account, cached on disk after the first run ------
    profile_model = config.stage1_profile.model if config.stage1_profile else None
    profile = get_or_create_profile(account_label, profile_model=profile_model,
                                    force_refresh=force_profile)
    profile_dict = profile.model_dump()
    logger.info("Stage 1 profile: category=%s style=%s pricing=%s vendor=%s",
                profile_dict.get("business_category"), profile_dict.get("seller_style"),
                profile_dict.get("pricing_behavior"), profile_dict.get("vendor_username"))
    if live:
        _print_stage1(profile_dict)

    triage_fn, triage_label, _ = load_stage_fn(
        config, "stage2_triage", run_id=run_id, vendor_id=vendor_id
    )
    extract_fn, extract_label, _ = load_stage_fn(
        config, "stage3_extract", run_id=run_id, vendor_id=vendor_id
    )
    signals_fn = signals_label = None
    if config.stage4_signals is not None:
        signals_fn, signals_label, _ = load_stage_fn(
            config, "stage4_signals", run_id=run_id, vendor_id=vendor_id
        )

    stage_reach = Counter()   # how many posts each stage was actually invoked on
    errors: list[dict] = []
    total_posts = len(posts)

    # --- Stage 2: every post ------------------------------------------------
    if live:
        _banner("Stage 2: triage - which posts are product listings?")
    stage2_by_id: dict[str, dict] = {}
    for index, post in enumerate(posts, start=1):
        post_id = post["post_id"]
        stage_reach["stage2"] += 1
        try:
            stage2 = triage_fn(post, profile_dict)
        except Exception as exc:
            # Mirrors the harness's per-post isolation: one bad post must not
            # take the run down. An un-triaged post is routed as unknown,
            # which Stage 5 sends to auto_exclude.
            logger.error("[stage2 ERROR] %s: %s: %s", post_id, type(exc).__name__, exc)
            errors.append({"post_id": post_id, "stage": "stage2", "error": str(exc)})
            stage2 = {"post_type": "unknown", "confidence": 0.0, "escalated": False}
        stage2_by_id[post_id] = stage2
        if live:
            _trace(index, total_posts, post_id, _triage_brief(stage2))
    if live:
        _print_stage2_summary(stage2_by_id)

    # --- Stages 3 + 4: only for predicted listings --------------------------
    # route_post() returns auto_exclude for anything that isn't a
    # product_listing before it reads either result, so calling them for a
    # non-listing would be spend with no effect on the outcome. This is the
    # chaining saving the harness cannot show.
    listings = [post for post in posts
                if stage2_by_id[post["post_id"]].get("post_type") == "product_listing"]
    total_listings = len(listings)

    if live:
        _banner(f"Stage 3: extraction - {total_listings} of {total_posts} post(s) qualify")
    stage3_by_id: dict[str, list[dict]] = {}
    for index, post in enumerate(listings, start=1):
        post_id = post["post_id"]
        stage_reach["stage3"] += 1
        stage3: list[dict] = []
        try:
            stage3 = extract_fn(post, profile_dict)
        except Exception as exc:
            logger.error("[stage3 ERROR] %s: %s: %s", post_id, type(exc).__name__, exc)
            errors.append({"post_id": post_id, "stage": "stage3", "error": str(exc)})
        stage3_by_id[post_id] = stage3
        if live:
            _trace(index, total_listings, post_id, _product_brief(stage3))
    if live:
        _print_stage3_summary(stage3_by_id, run_id)

    stage4_by_id: dict[str, list[dict]] = {}
    if signals_fn is not None:
        if live:
            _banner("Stage 4: signals - free regex prefilter gates every LLM call")
        for post in listings:
            post_id = post["post_id"]
            stage_reach["stage4"] += 1
            stage4: list[dict] = []
            try:
                stage4 = signals_fn(post, profile_dict)
            except Exception as exc:
                logger.error("[stage4 ERROR] %s: %s: %s", post_id, type(exc).__name__, exc)
                errors.append({"post_id": post_id, "stage": "stage4", "error": str(exc)})
            stage4_by_id[post_id] = stage4
            # Only posts that actually carry a signal are worth a line: a post
            # the prefilter skipped and a post the model cleared both return [],
            # and printing 30 identical empty lines buries the ones that matter.
            if live and stage4:
                print(f"  {post_id}  {', '.join(signal.get('signal', '?') for signal in stage4)}")
        if live:
            _print_stage4_summary(stage4_by_id, run_id)

    # --- Stage 5: deterministic, zero LLM -----------------------------------
    if live:
        _banner("Stage 5: routing - deterministic, zero LLM calls")
    items: list[dict] = []
    for post in posts:
        post_id = post["post_id"]
        stage2 = stage2_by_id[post_id]
        stage3 = stage3_by_id.get(post_id, [])
        stage4 = stage4_by_id.get(post_id, [])
        routed = route_post(post, stage2, stage3, stage4)

        items.append({
            "post_id": post_id,
            "permalink": post.get("permalink"),
            "timestamp": post.get("timestamp"),
            "media_type": post.get("media_type"),
            "media_product_type": post.get("media_product_type"),
            # vision_image_url(), never media_url - a Reel's media_url is the
            # .mp4 and this field is what a human reviewer is shown.
            "thumbnail_url": vision_image_url(post),
            "post_type": stage2.get("post_type"),
            "triage_confidence": stage2.get("confidence"),
            "triage_escalated": bool(stage2.get("escalated")),
            "bucket": routed["bucket"],
            "flags": routed["flags"],
            "products": stage3,
            "signals": stage4,
        })

    if live:
        _print_stage5_summary(items)

    buckets = Counter(item["bucket"] for item in items)
    flags = Counter(flag for item in items for flag in item["flags"])
    total = len(items)
    attention = buckets.get("needs_attention", 0)
    usage = read_run_usage(run_id)

    catalog = {
        "account_label": account_label,
        "vendor_username": vendor_username,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "source_dump": str(dump_path).replace("\\", "/"),
        "config": {
            "name": config.name,
            "path": str(config_path).replace("\\", "/"),
            "stage2": triage_label,
            "stage3": extract_label,
            "stage4": signals_label,
        },
        "profile": profile_dict,
        "summary": {
            "posts": total,
            "auto_import": buckets.get("auto_import", 0),
            "needs_attention": attention,
            "auto_exclude": buckets.get("auto_exclude", 0),
            "attention_rate": round(attention / total, 4) if total else 0.0,
            "products_extracted": sum(len(item["products"]) for item in items),
            "posts_reaching_stage2": stage_reach["stage2"],
            "posts_reaching_stage3": stage_reach["stage3"],
            "posts_reaching_stage4": stage_reach["stage4"],
            "stage2_escalations": sum(1 for item in items if item["triage_escalated"]),
            "flag_counts": dict(flags.most_common()),
            "errors": len(errors),
        },
        "usage": usage,
        "errors": errors,
        "items": items,
    }

    out_path = out_path or Path("runs") / account_label / "catalog.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
    bucket_paths = write_bucket_files(catalog, out_path)

    catalog["_out_path"] = str(out_path).replace("\\", "/")
    catalog["_bucket_paths"] = {
        bucket: str(path).replace("\\", "/") for bucket, path in bucket_paths.items()
    }
    return catalog


def print_summary(catalog: dict) -> None:
    s = catalog["summary"]
    usage = catalog["usage"]
    total = s["posts"] or 1

    def pct(n: int) -> str:
        return f"{n / total * 100:.0f}%"

    lines = [
        f"=== Catalog for {catalog['account_label']} "
        f"({catalog['vendor_username'] or 'unknown handle'}), config: {catalog['config']['name']} ===",
        f"  Posts processed   : {s['posts']}",
        f"  auto_import       : {s['auto_import']}/{s['posts']} ({pct(s['auto_import'])})",
        f"  needs_attention   : {s['needs_attention']}/{s['posts']} ({pct(s['needs_attention'])})",
        f"  auto_exclude      : {s['auto_exclude']}/{s['posts']} ({pct(s['auto_exclude'])})",
        f"  Attention rate    : {s['needs_attention']}/{s['posts']} = {pct(s['needs_attention'])}"
        "   (POC target: <= 10%)",
        f"  Products extracted: {s['products_extracted']}",
        "",
        "  Cascade reach (how many posts each stage was actually invoked on):",
        f"    Stage 2 triage    : {s['posts_reaching_stage2']}/{s['posts']} "
        f"({pct(s['posts_reaching_stage2'])})  - every post",
        f"    Stage 3 extract   : {s['posts_reaching_stage3']}/{s['posts']} "
        f"({pct(s['posts_reaching_stage3'])})  - predicted product_listing only",
        f"    Stage 4 signals   : {s['posts_reaching_stage4']}/{s['posts']} "
        f"({pct(s['posts_reaching_stage4'])})  - before its regex prefilter",
        f"    Stage 5 routing   : {s['posts']}/{s['posts']} (100%) - zero LLM calls",
        "",
        "  Measured LLM usage (read back from report/token_log.csv, not counted in-process):",
    ]
    if usage["calls"]:
        for stage, entry in sorted(usage["by_stage"].items()):
            lines.append(
                f"    {stage:<26} {entry['calls']:>3} calls  {entry['total_tokens']:>7,} tokens"
            )
        lines.append(
            f"    {'TOTAL':<26} {usage['calls']:>3} calls  {usage['total_tokens']:>7,} tokens"
            f"  ({usage['total_tokens'] / total:,.0f}/post)"
        )
        lines.append(f"    models actually called: {', '.join(usage['models'])}")
        if usage["fallbacks"]:
            lines.append(
                f"    *** {usage['fallbacks']} post(s) degraded to the zero-cost regex fallback "
                "after the LLM call failed."
            )
            lines.append(
                "        Extraction for those is heuristic, not model output - they carry "
                "confidence 0.4"
            )
            lines.append(
                "        and route to needs_attention, so a human sees them. Cost reads 0 tokens "
                "because no call succeeded."
            )
    else:
        lines.append("    *** ZERO rows logged for this run_id - no model was actually called. ***")
        lines.append("    A clean-looking run with no token rows means infrastructure failure,")
        lines.append("    not a model that answered nothing (README.md).")

    if s["flag_counts"]:
        lines.append("")
        lines.append("  Top flag reasons:")
        for flag, count in s["flag_counts"].items():
            lines.append(f"    [{count}x] {flag}")

    if s["errors"]:
        lines.append("")
        lines.append(f"  Errored stage calls: {s['errors']} (see catalog.json \"errors\")")

    lines.append("")
    lines.append(f"  Catalog written to {catalog['_out_path']}")

    bucket_paths = catalog.get("_bucket_paths") or {}
    if bucket_paths:
        lines.append("")
        lines.append("  Inspect a bucket directly (each item carries the flags explaining it):")
        for bucket in BUCKETS:
            path = bucket_paths.get(bucket)
            if path:
                lines.append(f"    {bucket:<16} {s.get(bucket, 0):>3} post(s)  ->  {path}")

    print("\n".join(lines))


def main() -> None:
    configure_logging()
    # Both streams: --live prints caption-derived product names on stdout while
    # configure_logging() writes errors to stderr, and an emoji reaching a cp1252
    # Windows console raises UnicodeEncodeError mid-run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # pragma: no cover - non-reconfigurable stream
            pass

    parser = argparse.ArgumentParser(
        description="Run Stages 1-5 chained on live predictions and emit a catalog JSON. "
                    "No golden set required."
    )
    parser.add_argument("--account", required=True,
                        help="Account label (the runs/<label>/ folder name)")
    parser.add_argument("--config", type=Path, default=DEFAULT_EXPERIMENT_PATH,
                        help="Experiment YAML - the single source of truth for models")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N posts (keeps a demo run cheap)")
    parser.add_argument("--changes", type=Path, default=None,
                        help="A Stage 6 sync report (report/changes*.json). Restricts this run "
                             "to the posts that sync says still need work - new posts, caption "
                             "edits and comment deltas - and skips everything sync resolved as "
                             "unchanged, CDN churn or an already-merged repost. This is the "
                             "incremental path; without it every post in the dump is processed.")
    parser.add_argument("--post-ids", default=None,
                        help="Comma-separated post ids to process, instead of --changes")
    parser.add_argument("--out", type=Path, default=None,
                        help="Catalog output path (default: runs/<account>/catalog.json)")
    parser.add_argument("--ingest", action="store_true",
                        help="LIVE: pull a fresh dump from the Graph API first. Off by default - "
                             "without it, the most recent existing dump is reused and this run "
                             "makes no Instagram API calls at all.")
    parser.add_argument("--token-env", default="IG_ACCESS_TOKEN",
                        help="Env var holding the IG token, used only with --ingest")
    parser.add_argument("--live", action="store_true",
                        help="Print a per-post trace and a summary as each stage finishes, "
                             "for walking through an onboarding run. Changes what is printed, "
                             "never what is computed or called.")
    parser.add_argument("--force-profile", action="store_true",
                        help="Re-run Stage 1 even if runs/<account>/profile.json exists. Without "
                             "it a repeated run reuses the cached profile and makes no Stage 1 "
                             "call - which is correct, but looks like nothing happened.")
    args = parser.parse_args()

    if args.ingest:
        from ingest.ingest import ingest_account

        logger.info("Pulling a fresh dump for %s (live Graph API)...", args.account)
        ingest_account(args.account, token_env=args.token_env)

    post_ids = None
    if args.changes:
        post_ids = posts_needing_work(args.changes)
        logger.info("Stage 6 handoff: %s lists %d post(s) needing Stages 2-5",
                     args.changes.name, len(post_ids))
    elif args.post_ids:
        post_ids = {pid.strip() for pid in args.post_ids.split(",") if pid.strip()}

    catalog = run_pipeline(args.account, args.config, limit=args.limit, out_path=args.out,
                            post_ids=post_ids, live=args.live,
                            force_profile=args.force_profile)
    if args.live:
        _banner("Run complete")
    print_summary(catalog)


if __name__ == "__main__":
    main()
