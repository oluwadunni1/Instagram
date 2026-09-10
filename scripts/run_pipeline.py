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
   FINDINGS_BASELINE_2026-09.md). Both now pass it, so the two are comparable.

Every stage is resolved through `harness.load_stage_fn()`, so the experiment
YAML is the single source of truth for models exactly as it is for a scored
run, and the reserved-parameter tripwire applies identically.

Cost and escalation figures in the summary are read back from
`report/token_log.csv` filtered to this run's `run_id` - the log records the
model string passed to litellm at call time, so the summary cannot be fooled
by a config's display label (FINDINGS.md 2026-09-07).

Usage:
    uv run scripts/run_pipeline.py --account vendor_gadgets_01
    uv run scripts/run_pipeline.py --account vendor_gadgets_01 --limit 8
    uv run scripts/run_pipeline.py --account vendor_new --ingest --token-env IG_ACCESS_TOKEN_GADGETS
    uv run scripts/run_pipeline.py --account vendor_autos_01 --changes report/changes.json

Writes only runs/<account>/catalog.json (and rows in report/token_log.csv).
Never touches eval/golden/ or data/snapshots/.
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
    caption nor comment count did - CDN churn, not new information),
    `repost_match` and `repost_merge` (already resolved against an existing
    item), and every no-op. Re-running the cascade on those is exactly the spend
    Stage 6 exists to avoid.
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
) -> dict:
    """Runs Stages 1-5 chained over the latest raw dump and returns the catalog.

    post_ids restricts the run to those posts - the incremental path a Stage 6
    sync feeds. Applied before `limit`, so the two compose predictably.
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
    profile = get_or_create_profile(account_label, profile_model=profile_model)
    profile_dict = profile.model_dump()
    logger.info("Stage 1 profile: category=%s style=%s pricing=%s vendor=%s",
                profile_dict.get("business_category"), profile_dict.get("seller_style"),
                profile_dict.get("pricing_behavior"), profile_dict.get("vendor_username"))

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

    items: list[dict] = []
    stage_reach = Counter()   # how many posts each stage was actually invoked on
    errors: list[dict] = []

    for post in posts:
        post_id = post["post_id"]

        # --- Stage 2 ------------------------------------------------------
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

        is_listing = stage2.get("post_type") == "product_listing"

        # --- Stage 3 + 4: only for predicted listings ----------------------
        # route_post() returns auto_exclude for anything that isn't a
        # product_listing before it reads either result, so calling them for
        # a non-listing would be spend with no effect on the outcome. This is
        # the chaining saving the harness cannot show.
        stage3: list[dict] = []
        stage4: list[dict] = []

        if is_listing:
            stage_reach["stage3"] += 1
            try:
                stage3 = extract_fn(post, profile_dict)
            except Exception as exc:
                logger.error("[stage3 ERROR] %s: %s: %s", post_id, type(exc).__name__, exc)
                errors.append({"post_id": post_id, "stage": "stage3", "error": str(exc)})

            if signals_fn is not None:
                stage_reach["stage4"] += 1
                try:
                    stage4 = signals_fn(post, profile_dict)
                except Exception as exc:
                    logger.error("[stage4 ERROR] %s: %s: %s", post_id, type(exc).__name__, exc)
                    errors.append({"post_id": post_id, "stage": "stage4", "error": str(exc)})

        # --- Stage 5: deterministic, zero LLM ------------------------------
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
    catalog["_out_path"] = str(out_path).replace("\\", "/")
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
        lines.append("    not a model that answered nothing (FINDINGS.md 2026-09-08).")

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
    print("\n".join(lines))


def main() -> None:
    configure_logging()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
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
                            post_ids=post_ids)
    print_summary(catalog)


if __name__ == "__main__":
    main()
