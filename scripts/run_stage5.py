#!/usr/bin/env python3
"""
Stage 5 - Flags & Routing
Loads cached Stage 2/3/4 prediction JSONs and routes each post into a bucket.
Zero LLM calls. Run with: uv run scripts/run_stage5.py

Defaults reproduce the original vendor_autos_01 / Gemini Cascade run.
eval/harness.py writes predictions to report/<vendor_id>/, where vendor_id
is the golden file's stem - but the filename within that directory is keyed
by model label, so the model half still has to be named explicitly:

    uv run scripts/run_stage5.py \\
        --golden eval/golden/vendor_gadgets_01.json \\
        --stage2 report/vendor_gadgets_01/stage2_<model>_predictions.json \\
        --stage3 report/vendor_gadgets_01/stage3_<model>_predictions.json \\
        --stage4 report/vendor_gadgets_01/stage4_<model>_predictions.json \\
        --label "GPT-4o Mini" --append-findings
"""

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.stages.stage5_reconcile import route_post  # noqa: E402

DEFAULT_STAGE2_PATH = Path("report/vendor_autos_01/stage2_Gemini_Cascade_Google_AI_Studio_predictions.json")
DEFAULT_STAGE3_PATH = Path("report/vendor_autos_01/stage3_gemini-3.5-flash-lite_Google_AI_Studio_predictions.json")
DEFAULT_STAGE4_PATH = Path("report/vendor_autos_01/stage4_gemini_gemini-3.5-flash-lite_predictions.json")
DEFAULT_GOLDEN_PATH = Path("eval/golden/vendor_autos_01.json")
DEFAULT_LABEL = "Gemini Cascade"


def load_raw_results(path: Path) -> dict[str, object]:
    """Loads a report/stage*_predictions.json file into post_id -> raw_result."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    return {entry["post_id"]: entry["raw_result"] for entry in entries}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN_PATH,
                         help="Golden-set file to route (default: vendor_autos_01)")
    parser.add_argument("--stage2", type=Path, default=DEFAULT_STAGE2_PATH,
                         help="Stage 2 report/<vendor>/stage2_*_predictions.json to route")
    parser.add_argument("--stage3", type=Path, default=DEFAULT_STAGE3_PATH,
                         help="Stage 3 report/<vendor>/stage3_*_predictions.json to route")
    parser.add_argument("--stage4", type=Path, default=DEFAULT_STAGE4_PATH,
                         help="Stage 4 report/<vendor>/stage4_*_predictions.json to route")
    parser.add_argument("--label", default=DEFAULT_LABEL,
                         help="Label for the console header and FINDINGS.md section title")
    parser.add_argument("--append-findings", action="store_true",
                         help="Append a dated section to FINDINGS.md with this run's results "
                              "(off by default so exploratory runs during demo prep don't "
                              "spam the file - pass this once you have a result worth recording)")
    args = parser.parse_args()

    stage2_by_post = load_raw_results(args.stage2)
    stage3_by_post = load_raw_results(args.stage3)
    stage4_by_post = load_raw_results(args.stage4)
    golden_posts = json.loads(args.golden.read_text(encoding="utf-8"))

    routed = []
    for post in golden_posts:
        post_id = post["post_id"]
        stage2_result = stage2_by_post.get(post_id, {"post_type": "unknown"})
        stage3_result = stage3_by_post.get(post_id, [])
        stage4_result = stage4_by_post.get(post_id, [])
        routed.append(route_post(post, stage2_result, stage3_result, stage4_result))

    total = len(routed)
    bucket_counts = Counter(r["bucket"] for r in routed)
    auto_import = bucket_counts.get("auto_import", 0)
    needs_attention = bucket_counts.get("needs_attention", 0)
    auto_exclude = bucket_counts.get("auto_exclude", 0)

    flag_counts: Counter = Counter()
    for r in routed:
        flag_counts.update(r["flags"])

    lines = []
    lines.append(f"=== Stage 5 Routing Distribution ({args.label} outputs, {args.golden.stem}) ===")
    lines.append(f"  auto_import       : {auto_import}/{total} ({auto_import / total * 100:.0f}%)")
    lines.append(f"  needs_attention   : {needs_attention}/{total} ({needs_attention / total * 100:.0f}%)")
    lines.append(f"  auto_exclude      : {auto_exclude}/{total} ({auto_exclude / total * 100:.0f}%)")
    lines.append(f"  Attention rate    : {needs_attention}/{total} = {needs_attention / total * 100:.0f}%")
    lines.append("  POC target        : <= 10 attention items per 100 posts")
    lines.append("  Top flag reasons:")
    for flag, count in flag_counts.most_common():
        lines.append(f"    [{count}x] {flag}")
    lines.append("  Needs-attention posts:")
    for r in routed:
        if r["bucket"] == "needs_attention":
            lines.append(f"    {r['post_id']}: {'; '.join(r['flags'])}")
    lines.append("  Auto-exclude posts:")
    for post, r in zip(golden_posts, routed):
        if r["bucket"] == "auto_exclude":
            post_id = post["post_id"]
            stage2_result = stage2_by_post.get(post_id, {"post_type": "unknown"})
            lines.append(f"    {post_id}: {stage2_result.get('post_type')}")

    output = "\n".join(lines)
    print(output)

    if args.append_findings:
        findings_path = Path("FINDINGS.md")
        with findings_path.open("a", encoding="utf-8") as f:
            f.write(f"\n## Stage 5 Routing Distribution — {args.label} ({date.today().isoformat()})\n\n")
            f.write(f"Golden set: `{args.golden}` ({total} posts). Cached inputs: "
                    f"`{args.stage2.name}`, `{args.stage3.name}`, `{args.stage4.name}` - zero LLM "
                    "calls, pure deterministic routing over already-computed Stage 2/3/4 outputs.\n\n")
            f.write("| Bucket | Count | % |\n")
            f.write("|---|---|---|\n")
            f.write(f"| auto_import | {auto_import}/{total} | {auto_import / total * 100:.0f}% |\n")
            f.write(f"| needs_attention | {needs_attention}/{total} | {needs_attention / total * 100:.0f}% |\n")
            f.write(f"| auto_exclude | {auto_exclude}/{total} | {auto_exclude / total * 100:.0f}% |\n\n")
            f.write(f"Attention rate: {needs_attention}/{total} = {needs_attention / total * 100:.0f}% "
                    "(POC target: <= 10 attention items per 100 posts).\n\n")
            f.write("Top flag reasons:\n\n")
            for flag, count in flag_counts.most_common():
                f.write(f"- [{count}x] {flag}\n")
            f.write("\nNeeds-attention posts:\n\n")
            for r in routed:
                if r["bucket"] == "needs_attention":
                    f.write(f"- `{r['post_id']}`: {'; '.join(r['flags'])}\n")
            f.write("\nAuto-exclude posts:\n\n")
            for post, r in zip(golden_posts, routed):
                if r["bucket"] == "auto_exclude":
                    post_id = post["post_id"]
                    stage2_result = stage2_by_post.get(post_id, {"post_type": "unknown"})
                    f.write(f"- `{post_id}`: {stage2_result.get('post_type')}\n")


if __name__ == "__main__":
    main()
