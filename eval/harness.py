"""
Eval harness.

Runs the pipeline (as defined by pipeline/config/registry.json -
swap models/functions there, never here) against every golden-set
file in eval/golden/, and prints per-stage scores + cost.

Every prompt/model change gets a score, not a vibe (brief section 8).

Usage:
    uv run eval/harness.py
    uv run eval/harness.py --config pipeline/config/registry.json
    uv run eval/harness.py --golden eval/golden/vendor_autos_01.json
"""

import argparse
import importlib
import json
import sys
from pathlib import Path

# Make the repo root importable regardless of how/where this script is invoked from
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def load_registry(config_path: Path) -> dict:
    return json.loads(config_path.read_text(encoding="utf-8"))


def load_stage_fn(registry: dict, stage_key: str):
    entry = registry[stage_key]
    module = importlib.import_module(entry["module"])
    fn = getattr(module, entry["function"])
    return fn, entry.get("model", "unknown"), entry.get("cost_per_call_usd", 0.0)


def load_golden_posts(golden_dir: Path, single_file) -> list:
    files = [single_file] if single_file else sorted(golden_dir.glob("*.json"))
    posts = []
    for f in files:
        posts.extend(json.loads(f.read_text(encoding="utf-8")))
    return posts


def score_stage2(posts, triage_fn, model_name, cost_per_call) -> None:
    correct = 0
    total = 0
    total_cost = 0.0

    for post in posts:
        gold_type = post.get("post_type")
        if gold_type is None:
            continue  # unlabeled - skip rather than penalize
        result = triage_fn(post)
        total_cost += cost_per_call
        total += 1
        if result["post_type"] == gold_type:
            correct += 1
        else:
            print(f"  [stage2 miss] {post['post_id']}: predicted={result['post_type']!r} "
                  f"gold={gold_type!r} (conf={result.get('confidence')})")

    accuracy = correct / total if total else 0.0
    print(f"\nStage 2 (triage) - model: {model_name}")
    print(f"  Accuracy: {correct}/{total} = {accuracy:.0%}")
    print(f"  Cost: ${total_cost:.4f}")


def score_carousel(posts) -> None:
    """Compares gold carousel_classification against predicted product
    count as a rough proxy, until a real carousel classifier exists.
    Right now every dummy prediction is 1 product (the sanctioned
    'gallery' fallback per brief section 3), so this mostly reports
    how many true multi-product posts exist in your golden set - worth
    watching once a real classifier is wired in."""
    labeled = [p for p in posts if p.get("carousel_classification")]
    if not labeled:
        return
    multi_product_gold = sum(1 for p in labeled if p["carousel_classification"] == "multi_product")
    print(f"\nCarousel classification - {len(labeled)} labeled posts "
          f"({multi_product_gold} true multi_product in gold set)")
    print("  (dummy Stage 3 always returns 1 product - real classifier not wired in yet)")


def score_stage3(posts, extract_fn, model_name, cost_per_call) -> None:
    """Scores against the first product in gold's products[] list per
    post - matches the dummy stub's single-product-always behavior.
    Once a real multi-product-capable Stage 3 exists, extend this to
    align predicted[i] <-> gold[i] for every product, not just [0]."""
    price_correct = 0
    price_total = 0
    missing_price_recall_hits = 0
    missing_price_total = 0
    total_cost = 0.0

    for post in posts:
        if post.get("post_type") != "product_listing":
            continue
        gold_products = post.get("products") or []
        if not gold_products:
            continue
        gold_price = gold_products[0].get("price", {})
        gold_value = gold_price.get("value")
        gold_source = gold_price.get("source")

        predicted = extract_fn(post)
        total_cost += cost_per_call
        pred_price = predicted[0]["price"] if predicted else {"value": None, "source": "none"}

        if gold_source == "none":
            # This is the "never invent a price" check - brief section 11
            missing_price_total += 1
            if pred_price["source"] == "none":
                missing_price_recall_hits += 1
            else:
                print(f"  [HALLUCINATED PRICE] {post['post_id']}: pipeline invented "
                      f"{pred_price['value']} when gold says no price exists")
        elif gold_value is not None:
            price_total += 1
            if pred_price["value"] == gold_value:
                price_correct += 1
            else:
                print(f"  [stage3 price miss] {post['post_id']}: predicted={pred_price['value']!r} "
                      f"gold={gold_value!r}")

    price_acc = price_correct / price_total if price_total else 0.0
    missing_recall = missing_price_recall_hits / missing_price_total if missing_price_total else None

    print(f"\nStage 3 (extraction) - model: {model_name}")
    print(f"  Price accuracy (when price exists): {price_correct}/{price_total} = {price_acc:.0%}")
    if missing_recall is not None:
        flag = "OK" if missing_recall == 1.0 else "*** MUST BE 100% - brief section 11 hard requirement ***"
        print(f"  Missing-price recall: {missing_price_recall_hits}/{missing_price_total} = "
              f"{missing_recall:.0%}  {flag}")
    else:
        print("  Missing-price recall: n/a (no missing-price posts in this golden set)")
    print(f"  Cost: ${total_cost:.4f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("pipeline/config/registry.json"))
    parser.add_argument("--golden", type=Path, default=None,
                         help="Score a single golden file instead of all of eval/golden/")
    args = parser.parse_args()

    registry = load_registry(args.config)
    posts = load_golden_posts(Path("eval/golden"), args.golden)

    if not posts:
        raise SystemExit("No golden-set posts found. Label at least one account first (see brief section 8).")

    print(f"Scoring {len(posts)} golden-set posts...")

    triage_fn, triage_model, triage_cost = load_stage_fn(registry, "stage2_triage")
    score_stage2(posts, triage_fn, triage_model, triage_cost)

    score_carousel(posts)

    extract_fn, extract_model, extract_cost = load_stage_fn(registry, "stage3_extract")
    score_stage3(posts, extract_fn, extract_model, extract_cost)


if __name__ == "__main__":
    main()