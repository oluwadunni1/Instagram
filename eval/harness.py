"""
Eval harness.

Runs the pipeline (as defined by a pipeline/config/experiments/*.yaml
experiment config - swap models/functions there, never here) against every
golden-set file in eval/golden/, and prints per-stage scores + cost.

Every prompt/model change gets a score, not a vibe (brief section 8).

Usage:
    uv run eval/harness.py
    uv run eval/harness.py --config pipeline/config/experiments/stage2_gemini_pro.yaml
    uv run eval/harness.py --golden eval/golden/vendor_autos_01.json
"""

import argparse
import functools
import importlib
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

# Make the repo root importable regardless of how/where this script is invoked from
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config.experiment_schema import (  # noqa: E402
    DEFAULT_EXPERIMENT_PATH,
    ExperimentConfig,
    load_experiment_config,
)
from pipeline.logging_config import configure_logging  # noqa: E402
from pipeline.types import Post  # noqa: E402

logger = logging.getLogger(__name__)


def load_stage_fn(
    config: ExperimentConfig, stage_key: str, **run_context: str
) -> tuple[Callable, str, float]:
    """Resolves config's stage_key entry (e.g. "stage2_triage") into a
    callable ready to score.

    Args:
        run_context: Extra kwargs bound in on top of the config's own
            (e.g. run_id/vendor_id for token-usage logging - see
            log_token_usage() in pipeline/llm_client.py). Only stages whose
            functions actually accept these should get them passed by the
            caller; a stage stub that doesn't make LLM calls (e.g.
            stage2_triage_dummy.py) still needs matching no-op parameters
            to accept and ignore them.

    Returns:
        (bound_fn, model_name, cost_per_call_usd) - bound_fn takes just the
        post (and, for stage2, an optional profile) since every other
        config key is already bound in as a kwarg default.
    """
    entry = getattr(config, stage_key)
    module = importlib.import_module(entry.module)
    fn = getattr(module, entry.function)

    # Bind every config key except the bookkeeping ones as kwargs into
    # the function - lets a stage's real model config (text_model,
    # vision_model, confidence_threshold, etc.) reach the function without
    # the harness needing stage-specific code. "model" stays label-only
    # for stages whose functions don't take it (e.g. still-dummy stubs),
    # so this never breaks a simpler stage that hasn't been wired to a
    # real model yet.
    reserved = {"module", "function", "cost_per_call_usd", "note", "model"}
    extra_kwargs = {k: v for k, v in entry.model_dump().items() if k not in reserved}
    extra_kwargs.update(run_context)
    bound_fn = functools.partial(fn, **extra_kwargs) if extra_kwargs else fn

    return bound_fn, entry.model, entry.cost_per_call_usd


def load_golden_posts(golden_dir: Path, single_file: Path | None) -> list[Post]:
    """Loads every post from every *.json file in golden_dir, or just
    single_file if given."""
    files = [single_file] if single_file else sorted(golden_dir.glob("*.json"))
    posts: list[Post] = []
    for f in files:
        posts.extend(json.loads(f.read_text(encoding="utf-8")))
    return posts


def score_stage2(posts: list[Post], triage_fn: Callable[[Post], dict], model_name: str, cost_per_call: float) -> None:
    """Scores Stage 2 triage accuracy against every labeled post, with a
    breakdown of accuracy on escalated (Pass B/vision) vs non-escalated
    (Pass A/text-only) posts - the only way to see whether vision
    escalation is actually helping, hurting, or just adding cost, since
    the two passes are otherwise blended into one overall accuracy number.
    Writes per-post predictions to report/stage2_<model>_predictions.json.
    """
    correct = 0
    total = 0
    error_count = 0
    escalated_count = 0
    escalated_correct = 0
    non_escalated_count = 0
    non_escalated_correct = 0
    total_cost = 0.0
    predictions = []

    for post in posts:
        gold_type = post.get("post_type")
        if gold_type is None:
            continue  # unlabeled - skip rather than penalize
        try:
            result = triage_fn(post)
        except Exception as exc:
            # A single post failure (model gives up, network dies, etc.) should
            # not crash the whole eval run - log it, count it as wrong, move on.
            # Counted in total/accuracy but NOT in the escalated/non-escalated
            # buckets below - it never produced a real result to attribute to
            # either pass, so lumping it into one would misrepresent that
            # pass's accuracy (see error_count in the summary instead).
            logger.error("[stage2 ERROR] %s: %s: %s", post["post_id"], type(exc).__name__, exc)
            total += 1
            error_count += 1
            total_cost += cost_per_call
            predictions.append({"post_id": post["post_id"], "gold": gold_type, "error": str(exc)})
            continue
        total_cost += cost_per_call
        total += 1
        is_escalated = bool(result.get("escalated"))
        is_correct = result["post_type"] == gold_type
        if is_escalated:
            escalated_count += 1
        else:
            non_escalated_count += 1
        if is_correct:
            correct += 1
            if is_escalated:
                escalated_correct += 1
            else:
                non_escalated_correct += 1
        else:
            logger.warning("[stage2 miss] %s: predicted=%r gold=%r (conf=%s, escalated=%s)",
                            post["post_id"], result["post_type"], gold_type,
                            result.get("confidence"), result.get("escalated"))

        predictions.append({
            "post_id": post["post_id"],
            "gold": gold_type,
            "predicted": result["post_type"],
            "raw_result": result
        })

    accuracy = correct / total if total else 0.0
    escalation_rate = escalated_count / total if total else 0.0
    non_escalated_accuracy = non_escalated_correct / non_escalated_count if non_escalated_count else None
    escalated_accuracy = escalated_correct / escalated_count if escalated_count else None

    logger.info("\nStage 2 (triage) - model: %s", model_name)
    logger.info("  Accuracy: %d/%d = %.0f%%", correct, total, accuracy * 100)
    if non_escalated_accuracy is not None:
        logger.info("    Pass A only (not escalated): %d/%d = %.0f%%",
                     non_escalated_correct, non_escalated_count, non_escalated_accuracy * 100)
    if escalated_accuracy is not None:
        logger.info("    Pass B vision (escalated):   %d/%d = %.0f%%",
                     escalated_correct, escalated_count, escalated_accuracy * 100)
    if error_count:
        logger.info("    Errored (no result - not counted in either pass above): %d/%d", error_count, total)
    escalation_flag = ("  *** >40% - brief section 5 says fix the text prompt before reaching for a bigger model ***"
                        if escalation_rate > 0.4 else "")
    logger.info("  Escalation rate (Pass A -> vision Pass B): %d/%d = %.0f%%%s",
                escalated_count, total, escalation_rate * 100, escalation_flag)
    logger.info("  Cost: $%.4f", total_cost)

    report_dir = Path("report")
    report_dir.mkdir(exist_ok=True)
    safe_model_name = model_name.replace("/", "_").replace(" ", "_").replace("(", "").replace(")", "")
    report_path = report_dir / f"stage2_{safe_model_name}_predictions.json"
    report_path.write_text(json.dumps(predictions, indent=2), encoding="utf-8")
    logger.info("  Saved detailed predictions to %s", report_path)


def score_carousel(posts: list[Post]) -> None:
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
    logger.info("\nCarousel classification - %d labeled posts (%d true multi_product in gold set)",
                len(labeled), multi_product_gold)
    logger.info("  (dummy Stage 3 always returns 1 product - real classifier not wired in yet)")


def name_similarity(predicted: str | None, gold: str | None) -> float:
    """Free, deterministic fuzzy match for free-text fields - no API
    call, no cost. Catches near-matches (spacing/capitalization/word
    order) that strict equality would wrongly flag as wrong. Only
    reach for an LLM-as-judge if this proves too crude on a spot-check -
    don't run model calls on every eval pass just to compare two
    strings when a free method usually gets you there."""
    if not predicted or not gold:
        return 0.0
    return SequenceMatcher(None, predicted.lower(), gold.lower()).ratio()


def score_stage3(posts: list[Post], extract_fn: Callable[[Post], list[dict]], model_name: str, cost_per_call: float) -> None:
    """Scores against the first product in gold's products[] list per
    post - matches the dummy stub's single-product-always behavior.
    Once a real multi-product-capable Stage 3 exists, extend this to
    align predicted[i] <-> gold[i] for every product, not just [0].
    Writes per-post predictions to report/stage3_<model>_predictions.json.
    """
    price_correct = 0
    price_total = 0
    missing_price_recall_hits = 0
    missing_price_total = 0
    name_scores = []
    total_cost = 0.0
    predictions = []

    for post in posts:
        if post.get("post_type") != "product_listing":
            continue
        gold_products = post.get("products") or []
        if not gold_products:
            continue
        gold_price = gold_products[0].get("price", {})
        gold_value = gold_price.get("value")
        gold_source = gold_price.get("source")
        gold_name = gold_products[0].get("name")

        predicted = extract_fn(post)
        total_cost += cost_per_call
        pred_price = predicted[0]["price"] if predicted else {"value": None, "source": "none"}
        pred_name = predicted[0].get("name") if predicted else None

        if gold_name:
            name_scores.append(name_similarity(pred_name, gold_name))

        if gold_source == "none":
            # This is the "never invent a price" check - brief section 11
            missing_price_total += 1
            if pred_price["source"] == "none":
                missing_price_recall_hits += 1
            else:
                logger.error("[HALLUCINATED PRICE] %s: pipeline invented %s when gold says no price exists",
                             post["post_id"], pred_price["value"])
        elif gold_value is not None:
            price_total += 1
            if pred_price["value"] == gold_value:
                price_correct += 1
            else:
                logger.warning("[stage3 price miss] %s: predicted=%r gold=%r",
                                post["post_id"], pred_price["value"], gold_value)

        predictions.append({
            "post_id": post["post_id"],
            "gold_name": gold_name,
            "gold_price": gold_value,
            "predicted_name": pred_name,
            "predicted_price": pred_price.get("value"),
            "raw_result": predicted
        })

    price_acc = price_correct / price_total if price_total else 0.0
    missing_recall = missing_price_recall_hits / missing_price_total if missing_price_total else None

    logger.info("\nStage 3 (extraction) - model: %s", model_name)
    logger.info("  Price accuracy (when price exists): %d/%d = %.0f%%", price_correct, price_total, price_acc * 100)
    if name_scores:
        avg_name_sim = sum(name_scores) / len(name_scores)
        logger.info("  Name similarity (avg, fuzzy match, free/no-cost): %.0f%% over %d products",
                     avg_name_sim * 100, len(name_scores))
    if missing_recall is not None:
        flag = "OK" if missing_recall == 1.0 else "*** MUST BE 100% - brief section 11 hard requirement ***"
        logger.info("  Missing-price recall: %d/%d = %.0f%%  %s",
                     missing_price_recall_hits, missing_price_total, missing_recall * 100, flag)
    else:
        logger.info("  Missing-price recall: n/a (no missing-price posts in this golden set)")
    logger.info("  Cost: $%.4f", total_cost)

    report_dir = Path("report")
    report_dir.mkdir(exist_ok=True)
    safe_model_name = model_name.replace("/", "_").replace(" ", "_").replace("(", "").replace(")", "")
    report_path = report_dir / f"stage3_{safe_model_name}_predictions.json"
    report_path.write_text(json.dumps(predictions, indent=2), encoding="utf-8")
    logger.info("  Saved detailed predictions to %s", report_path)


def score_stage4(posts: list[Post], detect_fn: Callable[[Post], list[dict]], model_name: str, cost_per_call: float) -> None:
    """Scores Stage 4 business-signal detection against `expected_signals`
    in the golden set.

    This is multi-label, not single-label like Stage 2's post_type: a post
    can have zero, one, or several gold signals, so precision/recall for a
    given signal name is accumulated across every labeled post's gold vs.
    predicted set for that name (a per-post exact-match check would wrongly
    zero out a post that got 2 of 3 signals right). Reports per-signal
    Precision/Recall/F1 plus a macro-average F1 across every signal type
    seen in either the gold or predicted sets - unseen signal types
    contribute no score, brief section 8: every prompt/model change gets a
    score, not a vibe. Writes per-post predictions to
    report/stage4_<model>_predictions.json.
    """
    tp: Counter = Counter()
    fp: Counter = Counter()
    fn: Counter = Counter()
    total = 0
    error_count = 0
    total_cost = 0.0
    predictions = []

    for post in posts:
        if "expected_signals" not in post:
            continue  # unlabeled - skip rather than penalize
        gold_signals = {s["signal"] for s in post["expected_signals"]}

        try:
            predicted = detect_fn(post)
        except Exception as exc:
            # A single post failure should not crash the whole eval run - log it,
            # charge every gold signal on this post as a miss, move on. Mirrors
            # score_stage2/score_stage3's per-post error handling.
            logger.error("[stage4 ERROR] %s: %s: %s", post["post_id"], type(exc).__name__, exc)
            total += 1
            error_count += 1
            total_cost += cost_per_call
            for signal in gold_signals:
                fn[signal] += 1
            predictions.append({"post_id": post["post_id"], "gold": sorted(gold_signals), "error": str(exc)})
            continue

        total_cost += cost_per_call
        total += 1
        pred_signals = {p["signal"] for p in predicted}

        for signal in gold_signals | pred_signals:
            if signal in gold_signals and signal in pred_signals:
                tp[signal] += 1
            elif signal in pred_signals:
                fp[signal] += 1
                logger.warning("[stage4 false positive] %s: predicted %r, not in gold %r",
                                post["post_id"], signal, sorted(gold_signals))
            else:
                fn[signal] += 1
                logger.warning("[stage4 miss] %s: gold has %r, not predicted (predicted=%r)",
                                post["post_id"], signal, sorted(pred_signals))

        predictions.append({
            "post_id": post["post_id"],
            "gold": sorted(gold_signals),
            "predicted": sorted(pred_signals),
            "raw_result": predicted,
        })

    logger.info("\nStage 4 (signal detection) - model: %s", model_name)
    all_signals = sorted(set(tp) | set(fp) | set(fn))
    if not all_signals:
        logger.info("  No labeled posts (expected_signals) found - nothing to score.")
    else:
        f1_scores = []
        for signal in all_signals:
            signal_tp, signal_fp, signal_fn = tp[signal], fp[signal], fn[signal]
            precision = signal_tp / (signal_tp + signal_fp) if (signal_tp + signal_fp) else 0.0
            recall = signal_tp / (signal_tp + signal_fn) if (signal_tp + signal_fn) else 0.0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
            f1_scores.append(f1)
            logger.info("  %-20s Precision: %d/%d = %3.0f%%   Recall: %d/%d = %3.0f%%   F1: %3.0f%%",
                         signal, signal_tp, signal_tp + signal_fp, precision * 100,
                         signal_tp, signal_tp + signal_fn, recall * 100, f1 * 100)
        macro_f1 = sum(f1_scores) / len(f1_scores)
        logger.info("  Macro-averaged F1 across %d signal type(s): %.0f%%", len(all_signals), macro_f1 * 100)
    if error_count:
        logger.info("  Errored (no result - each gold signal counted as a miss above): %d/%d", error_count, total)
    logger.info("  Cost: $%.4f", total_cost)

    report_dir = Path("report")
    report_dir.mkdir(exist_ok=True)
    safe_model_name = model_name.replace("/", "_").replace(" ", "_").replace("(", "").replace(")", "")
    report_path = report_dir / f"stage4_{safe_model_name}_predictions.json"
    report_path.write_text(json.dumps(predictions, indent=2), encoding="utf-8")
    logger.info("  Saved detailed predictions to %s", report_path)


def main() -> None:
    configure_logging()

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_EXPERIMENT_PATH)
    parser.add_argument("--golden", type=Path, default=None,
                         help="Score a single golden file instead of all of eval/golden/")
    args = parser.parse_args()

    config = load_experiment_config(args.config)
    posts = load_golden_posts(Path("eval/golden"), args.golden)

    if not posts:
        # main() is the real CLI entry point, so SystemExit here is correct
        # as-is - unlike the SystemExits removed elsewhere in the pipeline.
        raise SystemExit("No golden-set posts found. Label at least one account first (see brief section 8).")

    logger.info("Scoring %d golden-set posts against experiment %r...", len(posts), config.name)

    # Groups every report/token_log.csv row from this run together (see
    # log_token_usage() in pipeline/llm_client.py); vendor_id is the golden
    # file's stem when scoring one account, or "all_vendors" when scoring
    # everything in eval/golden/ at once.
    run_id = f"{config.name}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    vendor_id = args.golden.stem if args.golden else "all_vendors"

    triage_fn, triage_model, triage_cost = load_stage_fn(
        config, "stage2_triage", run_id=run_id, vendor_id=vendor_id
    )
    score_stage2(posts, triage_fn, triage_model, triage_cost)

    score_carousel(posts)

    extract_fn, extract_model, extract_cost = load_stage_fn(
        config, "stage3_extract", run_id=run_id, vendor_id=vendor_id
    )
    score_stage3(posts, extract_fn, extract_model, extract_cost)

    if config.stage4_signals is not None:
        signals_fn, signals_model, signals_cost = load_stage_fn(
            config, "stage4_signals", run_id=run_id, vendor_id=vendor_id
        )
        score_stage4(posts, signals_fn, signals_model, signals_cost)


if __name__ == "__main__":
    main()