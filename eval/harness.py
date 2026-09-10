"""
Eval harness.

Runs the pipeline (as defined by a pipeline/config/experiments/*.yaml
experiment config - swap models/functions there, never here) against every
golden-set file in eval/golden/, and prints per-stage scores + cost.

Every prompt/model change gets a score, not a vibe (brief section 8).

Stages are scored INDEPENDENTLY against gold labels, not chained - see
CLAUDE.md before interpreting any number this prints. What is shared with the
chained path (scripts/run_pipeline.py) is the input each stage sees: the
Stage 1 profile is threaded into Stages 2/3/4 here too, as of 2026-09-09. It
was not before, which is why figures recorded before that date are not
comparable to ones after it (see resolve_profile()).

Usage:
    uv run eval/harness.py
    uv run eval/harness.py --config pipeline/config/experiments/stage2_gemini_pro.yaml
    uv run eval/harness.py --golden eval/golden/vendor_autos_01.json
"""

import argparse
import functools
import importlib
import inspect
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
from pipeline.exceptions import MissingRawDumpError  # noqa: E402
from pipeline.logging_config import configure_logging  # noqa: E402
from pipeline.stages.stage1_profile import get_or_create_profile  # noqa: E402
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
        (bound_fn, model_name, cost_per_call_usd) - bound_fn takes the post
        and the Stage 1 profile positionally, since every other config key
        is already bound in as a kwarg default. Binding with
        functools.partial(fn, **kwargs) leaves both positional slots free,
        which is why threading the profile needs no change here.
    """
    entry = getattr(config, stage_key)
    module = importlib.import_module(entry.module)
    fn = getattr(module, entry.function)

    # Bind every config key except the bookkeeping ones as kwargs into
    # the function - lets a stage's real model config (text_model,
    # vision_model, confidence_threshold, etc.) reach the function without
    # the harness needing stage-specific code. "model" is ALWAYS label-only:
    # it carries a human-readable string used for log lines and the
    # predictions filename (e.g. "Gemini Cascade (Google AI Studio)"), which
    # is frequently not a valid litellm model string at all. A stage that
    # needs a real model must name its parameter something else
    # (text_model/vision_model/signals_model).
    reserved = {"module", "function", "cost_per_call_usd", "note", "model"}
    extra_kwargs = {k: v for k, v in entry.model_dump().items() if k not in reserved}

    # Tripwire: a stage function whose signature declares one of the reserved
    # names can never receive it, and fails SILENTLY - the run completes,
    # reports the config's label, and quietly uses the function's own default.
    # That is exactly how every stage4_*.yaml model comparison ran on the same
    # hardcoded model while reporting four different ones (FINDINGS.md
    # 2026-09-07). Fail loudly instead of producing plausible wrong numbers.
    shadowed = reserved.intersection(inspect.signature(fn).parameters)
    if shadowed:
        raise ValueError(
            f"{entry.module}.{entry.function} declares parameter(s) {sorted(shadowed)}, which "
            f"load_stage_fn() reserves and strips - the configured value would never reach the "
            f"function and it would silently run its own default. Rename the parameter (e.g. "
            f"'model' -> 'signals_model') and set the new key in the experiment YAML."
        )

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


def score_stage2(posts: list[Post], triage_fn: Callable[[Post, dict | None], dict], model_name: str,
                  cost_per_call: float, vendor_id: str, profile: dict | None = None) -> None:
    """Scores Stage 2 triage accuracy against every labeled post, with a
    breakdown of accuracy on escalated (Pass B/vision) vs non-escalated
    (Pass A/text-only) posts - the only way to see whether vision
    escalation is actually helping, hurting, or just adding cost, since
    the two passes are otherwise blended into one overall accuracy number.
    Writes per-post predictions to
    report/<vendor_id>/stage2_<model>_predictions.json.

    profile is Stage 1's AccountProfile as a dict, passed through to the
    stage exactly as scripts/run_pipeline.py does. Scoring on inputs the
    production path does not use produces numbers that describe neither -
    see main() for how it is resolved, and None when it cannot be.
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
            result = triage_fn(post, profile)
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

    # Scoped by vendor: prediction filenames carry only the model label, so
    # without this a second vendor's run would silently overwrite the first
    # vendor's cached predictions - which Stage 5 routes from.
    report_dir = Path("report") / vendor_id
    report_dir.mkdir(parents=True, exist_ok=True)
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


def score_stage3(posts: list[Post], extract_fn: Callable[[Post, dict | None], list[dict]], model_name: str,
                  cost_per_call: float, vendor_id: str, profile: dict | None = None) -> None:
    """Scores against the first product in gold's products[] list per
    post - matches the dummy stub's single-product-always behavior.
    Once a real multi-product-capable Stage 3 exists, extend this to
    align predicted[i] <-> gold[i] for every product, not just [0].
    Writes per-post predictions to
    report/<vendor_id>/stage3_<model>_predictions.json.

    profile is Stage 1's AccountProfile as a dict - see score_stage2.
    """
    price_correct = 0
    price_total = 0
    missing_price_recall_hits = 0
    missing_price_total = 0
    error_count = 0
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

        try:
            predicted = extract_fn(post, profile)
            pred_price = predicted[0]["price"] if predicted else {"value": None, "source": "none"}
            pred_name = predicted[0].get("name") if predicted else None
        except Exception as exc:
            # A single post failure should not crash the whole eval run - log
            # it, charge it as a miss against whichever denominator this post
            # belongs to, move on. Mirrors score_stage2/score_stage4.
            # The extraction call and the predicted[0] unpacking are both
            # inside the try: a stage3 implementation that returns a product
            # without a "price" key is the same class of failure as one that
            # raises, and neither should take the run down.
            logger.error("[stage3 ERROR] %s: %s: %s", post["post_id"], type(exc).__name__, exc)
            error_count += 1
            total_cost += cost_per_call
            if gold_name:
                name_scores.append(0.0)
            if gold_source == "none":
                # Counted in the denominator but not as a hit: an errored post
                # is not evidence the pipeline refused to invent a price, so
                # it must not prop up the brief-section-11 recall number.
                missing_price_total += 1
            elif gold_value is not None:
                price_total += 1
            predictions.append({
                "post_id": post["post_id"],
                "gold_name": gold_name,
                "gold_price": gold_value,
                "error": str(exc),
            })
            continue
        total_cost += cost_per_call

        if gold_name:
            name_scores.append(name_similarity(pred_name, gold_name))

        if gold_source == "none":
            # This is the "never invent a price" check - brief section 11
            missing_price_total += 1
            if pred_price.get("source") == "none":
                missing_price_recall_hits += 1
            else:
                logger.error("[HALLUCINATED PRICE] %s: pipeline invented %s when gold says no price exists",
                             post["post_id"], pred_price.get("value"))
        elif gold_value is not None:
            price_total += 1
            if pred_price.get("value") == gold_value:
                price_correct += 1
            else:
                logger.warning("[stage3 price miss] %s: predicted=%r gold=%r",
                                post["post_id"], pred_price.get("value"), gold_value)

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
    if error_count:
        logger.info("  Errors: %d post(s) failed extraction and were charged as misses", error_count)
    logger.info("  Cost: $%.4f", total_cost)

    report_dir = Path("report") / vendor_id  # see score_stage2 for why
    report_dir.mkdir(parents=True, exist_ok=True)
    safe_model_name = model_name.replace("/", "_").replace(" ", "_").replace("(", "").replace(")", "")
    report_path = report_dir / f"stage3_{safe_model_name}_predictions.json"
    report_path.write_text(json.dumps(predictions, indent=2), encoding="utf-8")
    logger.info("  Saved detailed predictions to %s", report_path)


def score_stage4(posts: list[Post], detect_fn: Callable[[Post, dict | None], list[dict]], model_name: str,
                  cost_per_call: float, vendor_id: str, profile: dict | None = None) -> None:
    """Scores Stage 4 business-signal detection against `expected_signals`
    in the golden set.

    profile is Stage 1's AccountProfile as a dict (see score_stage2). Stage 4
    needs it more than the others do: without it _label_comments() cannot tell
    a vendor's own comment from a buyer's, so the [VENDOR]/[BUYER] labels in
    its prompt never fire.

    This is multi-label, not single-label like Stage 2's post_type: a post
    can have zero, one, or several gold signals, so precision/recall for a
    given signal name is accumulated across every labeled post's gold vs.
    predicted set for that name (a per-post exact-match check would wrongly
    zero out a post that got 2 of 3 signals right). Reports per-signal
    Precision/Recall/F1 plus a macro-average F1 across every signal type
    seen in either the gold or predicted sets - unseen signal types
    contribute no score, brief section 8: every prompt/model change gets a
    score, not a vibe. Writes per-post predictions to
    report/<vendor_id>/stage4_<model>_predictions.json.
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
            predicted = detect_fn(post, profile)
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
        logger.info("  Macro-averaged F1 across %d signal type(s): %.0f%%", len(all_signals),
                     macro_f1 * 100)

        # Micro-average: pool every tp/fp/fn, then compute one P/R/F1.
        #
        # Report BOTH, because macro F1 alone is not comparable between two
        # models. all_signals is derived from gold UNION predicted, so a model
        # that hallucinates signal types is scored over MORE types - each
        # hallucinated type adds a 0%-F1 row and drags its macro average down,
        # while a model that only predicts what exists is averaged over fewer.
        # On vendor_gadgets_01 (2026-09-09) that made GPT-4o Mini read as 100%
        # over 4 types against Llama's 38% over 7, on identical posts; pooled,
        # the honest gap was 100% vs 43% (Llama: 5 tp, 13 fp). Micro-averaging
        # has a fixed denominator - every gold and predicted signal, once - so
        # it compares across models. Macro still earns its place: it weights a
        # rare signal type equally with a common one, which micro does not.
        micro_tp, micro_fp, micro_fn = sum(tp.values()), sum(fp.values()), sum(fn.values())
        micro_p = micro_tp / (micro_tp + micro_fp) if (micro_tp + micro_fp) else 0.0
        micro_r = micro_tp / (micro_tp + micro_fn) if (micro_tp + micro_fn) else 0.0
        micro_f1 = 2 * micro_p * micro_r / (micro_p + micro_r) if (micro_p + micro_r) else 0.0
        logger.info("  Micro-averaged (pooled tp=%d fp=%d fn=%d): P=%.0f%% R=%.0f%% F1=%.0f%%"
                     "   <- use THIS to compare models",
                     micro_tp, micro_fp, micro_fn, micro_p * 100, micro_r * 100, micro_f1 * 100)
        if len(all_signals) > len(set(tp) | set(fn)):
            hallucinated = sorted(set(all_signals) - (set(tp) | set(fn)))
            logger.info("    (%d signal type(s) predicted but never in gold: %s - these depress "
                         "macro F1 only)", len(hallucinated), ", ".join(hallucinated))
    if error_count:
        logger.info("  Errored (no result - each gold signal counted as a miss above): %d/%d", error_count, total)
    logger.info("  Cost: $%.4f", total_cost)

    report_dir = Path("report") / vendor_id  # see score_stage2 for why
    report_dir.mkdir(parents=True, exist_ok=True)
    safe_model_name = model_name.replace("/", "_").replace(" ", "_").replace("(", "").replace(")", "")
    report_path = report_dir / f"stage4_{safe_model_name}_predictions.json"
    report_path.write_text(json.dumps(predictions, indent=2), encoding="utf-8")
    logger.info("  Saved detailed predictions to %s", report_path)


def resolve_profile(account_label: str | None, config: ExperimentConfig) -> dict | None:
    """Loads the Stage 1 profile the scored stages should see, or None.

    Until 2026-09-09 the harness passed no profile at all while
    scripts/run_pipeline.py passed one, so every published accuracy number was
    measured on different inputs than the production path uses. Stage 4 was
    worst affected: without a profile its [VENDOR]/[BUYER] comment labelling
    never fired during a scored run. Figures from before that date are not
    comparable to ones from after it.

    get_or_create_profile() caches to runs/<account_label>/profile.json, so
    this costs one model call per account, once, and nothing thereafter.

    Returns None (loudly) rather than guessing when there is no single account
    to resolve - the no---golden path merges every vendor in eval/golden/ into
    one run, and picking any one vendor's profile for another vendor's posts
    would quietly corrupt the result. Same for a missing raw dump: a run
    without a profile is worth strictly more than no run, provided the log
    says so.
    """
    if account_label is None:
        logger.warning(
            "No --golden/--account given, so there is no single account whose Stage 1 profile "
            "applies - scoring every vendor in eval/golden/ at once with profile=None. Stage 4's "
            "[VENDOR]/[BUYER] comment labelling will NOT fire, and these numbers are not "
            "comparable to a per-vendor run. Pass --golden (CLAUDE.md says to, now that more than "
            "one vendor exists)."
        )
        return None

    profile_model = config.stage1_profile.model if config.stage1_profile else None
    try:
        profile = get_or_create_profile(account_label, profile_model=profile_model).model_dump()
    except MissingRawDumpError as exc:
        logger.warning(
            "No Stage 1 profile for account %r (%s) - scoring with profile=None. Stages 2/3/4 will "
            "see different inputs than scripts/run_pipeline.py gives them, so do not compare these "
            "numbers to a chained run. Run `make ingest ACCOUNT=%s` first.",
            account_label, exc, account_label,
        )
        return None

    logger.info("Stage 1 profile (%s): category=%s style=%s pricing=%s vendor=%s",
                account_label, profile.get("business_category"), profile.get("seller_style"),
                profile.get("pricing_behavior"), profile.get("vendor_username"))
    return profile


def main() -> None:
    configure_logging()

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_EXPERIMENT_PATH)
    parser.add_argument("--golden", type=Path, default=None,
                         help="Score a single golden file instead of all of eval/golden/")
    parser.add_argument("--only-stage", choices=["2", "3", "4"], action="append", dest="only_stages",
                         help="Score ONLY the named stage(s); repeatable (e.g. --only-stage 3 --only-stage 4). "
                              "Default runs 2, 3 and 4. A stage-N comparison otherwise pays for the other two "
                              "stages on every run - for a Stage 4 sweep that is ~75 wasted calls out of ~83, "
                              "and it burns the Gemini free-tier quota that Stage 2/3 depend on. Stages are "
                              "scored independently against gold (see CLAUDE.md), so skipping one cannot change "
                              "another's result.")
    parser.add_argument("--account", default=None,
                         help="Account label (the runs/<label>/ folder name) whose Stage 1 profile "
                              "is threaded into Stages 2/3/4. Defaults to --golden's stem, which is "
                              "the convention everywhere else (see CLAUDE.md's two-identifiers "
                              "section) - pass this only for a golden file whose name differs from "
                              "its runs/ folder.")
    args = parser.parse_args()
    selected = set(args.only_stages) if args.only_stages else {"2", "3", "4"}

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

    profile = resolve_profile(args.account or (args.golden.stem if args.golden else None), config)

    if selected != {"2", "3", "4"}:
        logger.info("Scoring only stage(s): %s", ", ".join(sorted(selected)))

    if "2" in selected:
        triage_fn, triage_model, triage_cost = load_stage_fn(
            config, "stage2_triage", run_id=run_id, vendor_id=vendor_id
        )
        score_stage2(posts, triage_fn, triage_model, triage_cost, vendor_id, profile)

        score_carousel(posts)

    if "3" in selected:
        extract_fn, extract_model, extract_cost = load_stage_fn(
            config, "stage3_extract", run_id=run_id, vendor_id=vendor_id
        )
        score_stage3(posts, extract_fn, extract_model, extract_cost, vendor_id, profile)

    if "4" in selected:
        if config.stage4_signals is None:
            logger.warning("--only-stage 4 requested but %s has no stage4_signals block - nothing to score.",
                            args.config)
        else:
            signals_fn, signals_model, signals_cost = load_stage_fn(
                config, "stage4_signals", run_id=run_id, vendor_id=vendor_id
            )
            score_stage4(posts, signals_fn, signals_model, signals_cost, vendor_id, profile)


if __name__ == "__main__":
    main()