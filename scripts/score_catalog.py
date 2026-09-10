#!/usr/bin/env python3
"""
Scores a chained catalog against a golden set - the measurement nothing else makes.

`eval/harness.py` scores each stage **independently against gold**: `score_stage3`
runs on posts whose *gold* `post_type == "product_listing"`, regardless of what
Stage 2 predicted. So every per-stage number in `FINDINGS.md` assumes perfect
upstream routing, and CLAUDE.md says so outright.

`scripts/run_pipeline.py` runs the stages *chained* on live predictions - Stage 3
only ever sees posts Stage 2 called listings - but reads no golden set at all, by
design, so it reports no accuracy.

This script closes the loop: it scores the chained catalog on the harness's own
denominators, so the two are directly comparable. The difference between them is
the **routing penalty** - what perfect-upstream-routing was worth all along.

The critical choice, and the one thing that would silently erase the finding if
it were done the other way: a gold product_listing that Stage 2 mis-routed is
scored as a **miss**, not excluded from the denominator. It extracted nothing,
so its name is wrong and its price is wrong. Excluding it would reproduce the
harness's own numbers and measure nothing.

Kept separate from run_pipeline.py on purpose - the runner's "reads no golden
set" property is its design claim, is asserted in its docstring, and is covered
by tests. This mirrors scripts/run_stage5.py, which likewise scores from cached
artifacts rather than recomputing them.

Usage:
    uv run scripts/score_catalog.py \\
        --catalog runs/vendor_gadgets_01/catalog.json \\
        --golden  eval/golden/vendor_gadgets_01.json

    uv run scripts/score_catalog.py --catalog ... --golden ... --json score.json

Read-only: writes nothing unless --json is passed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
# eval/ is not a package and harness.py runs as a script - same two-path setup
# tests/conftest.py and scripts/run_pipeline.py use.
sys.path.insert(0, str(REPO_ROOT / "eval"))

from harness import name_similarity  # noqa: E402


def score_catalog(catalog: dict, golden: list[dict]) -> dict:
    """Scores one chained catalog against a golden set.

    Every denominator here matches eval/harness.py's, so the result can be put
    beside a harness run without adjustment.
    """
    items = {item["post_id"]: item for item in catalog.get("items", [])}
    scored_posts = 0
    missing_from_catalog = 0

    # --- Stage 2 ----------------------------------------------------------
    s2_correct = 0
    s2_total = 0

    # --- Stage 3 (gold denominator) ---------------------------------------
    price_correct = 0
    price_total = 0
    missing_price_hits = 0
    missing_price_total = 0
    name_scores: list[float] = []
    routing_misses = 0          # gold listings the chained run never extracted from
    routing_missed_ids: list[str] = []

    # --- Stage 4 ----------------------------------------------------------
    tp: Counter = Counter()
    fp: Counter = Counter()
    fn: Counter = Counter()

    for post in golden:
        post_id = post.get("post_id")
        item = items.get(post_id)
        if item is None:
            missing_from_catalog += 1
            continue
        scored_posts += 1

        # Stage 2: same denominator as score_stage2 - every gold-labelled post.
        gold_type = post.get("post_type")
        if gold_type is not None:
            s2_total += 1
            if item.get("post_type") == gold_type:
                s2_correct += 1

        # Stage 3: same denominator as score_stage3 - posts whose GOLD type is
        # product_listing and which carry gold products. A post Stage 2 sent
        # elsewhere still counts; it simply extracted nothing.
        gold_products = post.get("products") or []
        if gold_type == "product_listing" and gold_products:
            gold_price = gold_products[0].get("price") or {}
            gold_value = gold_price.get("value")
            gold_source = gold_price.get("source")
            gold_name = gold_products[0].get("name")

            predicted = item.get("products") or []
            if not predicted:
                routing_misses += 1
                routing_missed_ids.append(post_id)
            pred_price = (predicted[0].get("price") or {}) if predicted else {}
            pred_name = predicted[0].get("name") if predicted else None

            if gold_name:
                # name_similarity() returns 0.0 for a None prediction, so a
                # mis-routed post scores zero rather than being skipped.
                name_scores.append(name_similarity(pred_name, gold_name))

            if gold_source == "none":
                # Brief section 11: never invent a price. A post that was never
                # extracted from cannot have invented one - it counts as a hit
                # here, exactly as the harness would score an empty prediction.
                # routing_misses is reported alongside so this stays visible.
                missing_price_total += 1
                if pred_price.get("source", "none") == "none":
                    missing_price_hits += 1
            elif gold_value is not None:
                price_total += 1
                if pred_price.get("value") == gold_value:
                    price_correct += 1

        # Stage 4: same denominator as score_stage4 - posts carrying
        # expected_signals. A signal predicted with zero gold instances is a
        # false positive and enters the macro denominator, matching the harness.
        if "expected_signals" in post:
            gold_signals = {s["signal"] for s in post["expected_signals"]}
            pred_signals = {s["signal"] for s in (item.get("signals") or [])}
            for signal in gold_signals | pred_signals:
                if signal in gold_signals and signal in pred_signals:
                    tp[signal] += 1
                elif signal in pred_signals:
                    fp[signal] += 1
                else:
                    fn[signal] += 1

    per_signal = {}
    f1_scores = []
    for signal in sorted(set(tp) | set(fp) | set(fn)):
        s_tp, s_fp, s_fn = tp[signal], fp[signal], fn[signal]
        precision = s_tp / (s_tp + s_fp) if (s_tp + s_fp) else 0.0
        recall = s_tp / (s_tp + s_fn) if (s_tp + s_fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        f1_scores.append(f1)
        per_signal[signal] = {
            "precision": precision, "recall": recall, "f1": f1,
            "tp": s_tp, "fp": s_fp, "fn": s_fn,
        }

    buckets = Counter(item["bucket"] for item in catalog.get("items", []))
    total_items = len(catalog.get("items", []))

    return {
        "account_label": catalog.get("account_label"),
        "run_id": catalog.get("run_id"),
        "config": (catalog.get("config") or {}).get("name"),
        "scored_posts": scored_posts,
        "golden_posts": len(golden),
        "missing_from_catalog": missing_from_catalog,
        "stage2": {
            "correct": s2_correct,
            "total": s2_total,
            "accuracy": s2_correct / s2_total if s2_total else 0.0,
        },
        "stage3": {
            "price_correct": price_correct,
            "price_total": price_total,
            "price_accuracy": price_correct / price_total if price_total else 0.0,
            "name_similarity": sum(name_scores) / len(name_scores) if name_scores else 0.0,
            "name_scored": len(name_scores),
            "missing_price_hits": missing_price_hits,
            "missing_price_total": missing_price_total,
            "missing_price_recall": (
                missing_price_hits / missing_price_total if missing_price_total else None
            ),
            "routing_misses": routing_misses,
            "routing_missed_ids": routing_missed_ids,
        },
        "stage4": {
            "per_signal": per_signal,
            "macro_f1": sum(f1_scores) / len(f1_scores) if f1_scores else 0.0,
            "signal_types": len(f1_scores),
        },
        "routing": {
            "auto_import": buckets.get("auto_import", 0),
            "needs_attention": buckets.get("needs_attention", 0),
            "auto_exclude": buckets.get("auto_exclude", 0),
            "total": total_items,
            "attention_rate": buckets.get("needs_attention", 0) / total_items if total_items else 0.0,
        },
    }


def format_report(score: dict) -> str:
    s2, s3, s4, r = score["stage2"], score["stage3"], score["stage4"], score["routing"]
    lines = [
        f"=== Chained score: {score['account_label']} "
        f"(config: {score['config']}, run: {score['run_id']}) ===",
        f"  Scored {score['scored_posts']} of {score['golden_posts']} golden posts"
        + (f"  ({score['missing_from_catalog']} not in catalog)"
           if score["missing_from_catalog"] else ""),
        "",
        "  These use eval/harness.py's own denominators, so they sit directly beside",
        "  a harness run. The difference is the routing penalty.",
        "",
        f"  Stage 2 accuracy        : {s2['correct']}/{s2['total']} = {s2['accuracy'] * 100:.0f}%",
        f"  Stage 3 price accuracy  : {s3['price_correct']}/{s3['price_total']} "
        f"= {s3['price_accuracy'] * 100:.0f}%",
        f"  Stage 3 name similarity : {s3['name_similarity'] * 100:.0f}% over "
        f"{s3['name_scored']} product(s)",
    ]
    if s3["missing_price_recall"] is not None:
        flag = "OK" if s3["missing_price_recall"] == 1.0 else "*** MUST BE 100% (brief section 11) ***"
        lines.append(
            f"  Missing-price recall    : {s3['missing_price_hits']}/{s3['missing_price_total']} "
            f"= {s3['missing_price_recall'] * 100:.0f}%  {flag}"
        )
    else:
        lines.append("  Missing-price recall    : n/a (no missing-price posts in gold)")

    lines.append("")
    lines.append(
        f"  ROUTING PENALTY         : {s3['routing_misses']} gold product_listing(s) never "
        "reached extraction"
    )
    if s3["routing_misses"]:
        lines.append(
            "    Stage 2 routed these elsewhere, so they scored zero on name and price above."
        )
        lines.append(
            "    The harness would have scored them anyway - that gap is what this measures."
        )
        for post_id in s3["routing_missed_ids"]:
            lines.append(f"      {post_id}")

    lines.append("")
    if s4["per_signal"]:
        lines.append("  Stage 4 signals:")
        for signal, m in s4["per_signal"].items():
            lines.append(
                f"    {signal:<20} P: {m['tp']}/{m['tp'] + m['fp']} = {m['precision'] * 100:3.0f}%"
                f"   R: {m['tp']}/{m['tp'] + m['fn']} = {m['recall'] * 100:3.0f}%"
                f"   F1: {m['f1'] * 100:3.0f}%"
            )
        lines.append(
            f"    Macro F1 across {s4['signal_types']} signal type(s): {s4['macro_f1'] * 100:.0f}%"
        )
    else:
        lines.append("  Stage 4 signals: nothing scored (no expected_signals in gold)")

    lines += [
        "",
        f"  Routing: auto_import {r['auto_import']}/{r['total']}"
        f"  needs_attention {r['needs_attention']}/{r['total']}"
        f"  auto_exclude {r['auto_exclude']}/{r['total']}",
        f"  Attention rate: {r['attention_rate'] * 100:.0f}%",
    ]
    return "\n".join(lines)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover - non-reconfigurable stream
        pass

    parser = argparse.ArgumentParser(
        description="Score a run_pipeline.py catalog against a golden set, on the "
                    "harness's own denominators."
    )
    parser.add_argument("--catalog", type=Path, required=True,
                        help="runs/<account>/catalog.json from scripts/run_pipeline.py")
    parser.add_argument("--golden", type=Path, required=True,
                        help="eval/golden/<account>.json")
    parser.add_argument("--json", type=Path, default=None,
                        help="Also write the machine-readable score here")
    args = parser.parse_args()

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    golden = json.loads(args.golden.read_text(encoding="utf-8"))

    score = score_catalog(catalog, golden)
    print(format_report(score))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(score, indent=2), encoding="utf-8")
        print(f"\n  Score written to {args.json}")


if __name__ == "__main__":
    main()
