"""Chained-catalog scoring tests.

`scripts/score_catalog.py` exists to measure one thing: how much per-stage
accuracy depends on perfect upstream routing. Exactly one implementation
mistake would erase that finding without failing anything - excluding a
mis-routed post from the Stage 3 denominator instead of counting it as a miss.
Do that and the script reproduces the harness's own numbers and measures
nothing. The first test below is that guard.

Offline: no network, no .env, no golden set - fixtures are built inline.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# scripts/ is not a package and score_catalog.py runs as a script - same
# loader pattern as tests/test_scan_secrets.py and tests/test_run_pipeline.py.
_spec = importlib.util.spec_from_file_location(
    "score_catalog", REPO_ROOT / "scripts" / "score_catalog.py"
)
score_catalog = importlib.util.module_from_spec(_spec)
sys.modules["score_catalog"] = score_catalog
_spec.loader.exec_module(score_catalog)


def _gold(post_id: str, post_type: str = "product_listing", name: str = "iPhone 12",
          price_value: int | None = 100, price_source: str = "caption",
          signals: list[str] | None = None) -> dict:
    post: dict = {
        "post_id": post_id,
        "post_type": post_type,
        "products": [{"name": name, "price": {"value": price_value, "source": price_source}}],
    }
    if signals is not None:
        post["expected_signals"] = [{"signal": s} for s in signals]
    return post


def _item(post_id: str, post_type: str = "product_listing", bucket: str = "auto_import",
          products: list[dict] | None = None, signals: list[str] | None = None) -> dict:
    return {
        "post_id": post_id,
        "post_type": post_type,
        "bucket": bucket,
        "products": products if products is not None else [
            {"name": "iPhone 12", "price": {"value": 100, "source": "caption"}}
        ],
        "signals": [{"signal": s} for s in (signals or [])],
    }


def _catalog(items: list[dict]) -> dict:
    return {"account_label": "t", "run_id": "r", "config": {"name": "default"}, "items": items}


# --- the guard -------------------------------------------------------------

def test_misrouted_listing_counts_as_a_miss_not_an_exclusion() -> None:
    """THE test. A gold product_listing that Stage 2 sent elsewhere extracted
    nothing, so it must stay in the Stage 3 denominator and score zero. If it
    were excluded instead, chained and independent scores would be identical
    and the routing penalty would read as zero on every run."""
    golden = [_gold("a"), _gold("b")]
    # "b" was triaged as an announcement, so the chained run never extracted it.
    catalog = _catalog([
        _item("a"),
        _item("b", post_type="announcement", bucket="auto_exclude", products=[]),
    ])

    score = score_catalog.score_catalog(catalog, golden)

    assert score["stage3"]["price_total"] == 2, "mis-routed post must stay in the denominator"
    assert score["stage3"]["price_correct"] == 1
    assert score["stage3"]["routing_misses"] == 1
    assert score["stage3"]["routing_missed_ids"] == ["b"]
    # And its name scores zero rather than being skipped.
    assert score["stage3"]["name_scored"] == 2
    assert score["stage3"]["name_similarity"] == 0.5


def test_routing_penalty_is_zero_when_nothing_was_misrouted() -> None:
    golden = [_gold("a"), _gold("b")]
    catalog = _catalog([_item("a"), _item("b")])

    score = score_catalog.score_catalog(catalog, golden)

    assert score["stage3"]["routing_misses"] == 0
    assert score["stage3"]["price_accuracy"] == 1.0


# --- stage 2 ---------------------------------------------------------------

def test_stage2_scores_every_gold_labelled_post() -> None:
    golden = [_gold("a"), _gold("b", post_type="announcement")]
    catalog = _catalog([_item("a"), _item("b", post_type="product_listing", products=[])])

    score = score_catalog.score_catalog(catalog, golden)

    assert score["stage2"]["total"] == 2
    assert score["stage2"]["correct"] == 1


def test_posts_absent_from_the_catalog_are_reported_not_silently_dropped() -> None:
    golden = [_gold("a"), _gold("ghost")]
    catalog = _catalog([_item("a")])

    score = score_catalog.score_catalog(catalog, golden)

    assert score["missing_from_catalog"] == 1
    assert score["scored_posts"] == 1


# --- missing-price recall --------------------------------------------------

def test_missing_price_recall_holds_when_nothing_was_extracted() -> None:
    """Brief section 11 is "never invent a price". A post that was never
    extracted from cannot have invented one, so it is a hit - the same way the
    harness scores an empty prediction. routing_misses keeps it visible."""
    golden = [_gold("a", price_value=None, price_source="none")]
    catalog = _catalog([_item("a", products=[])])

    score = score_catalog.score_catalog(catalog, golden)

    assert score["stage3"]["missing_price_recall"] == 1.0
    assert score["stage3"]["routing_misses"] == 1


def test_hallucinated_price_breaks_missing_price_recall() -> None:
    golden = [_gold("a", price_value=None, price_source="none")]
    catalog = _catalog([
        _item("a", products=[{"name": "x", "price": {"value": 999, "source": "caption"}}])
    ])

    score = score_catalog.score_catalog(catalog, golden)

    assert score["stage3"]["missing_price_recall"] == 0.0


# --- stage 4 ---------------------------------------------------------------

def test_stage4_counts_a_signal_with_no_gold_instances_as_a_false_positive() -> None:
    """Matches score_stage4: a predicted signal with zero gold instances scores
    0% and enters the macro denominator. This is what made vendor 2's reported
    macro-F1 row not like-for-like across models, so the chained score has to
    reproduce it rather than quietly fix it."""
    golden = [_gold("a", signals=["sold"])]
    catalog = _catalog([_item("a", signals=["sold", "urgent"])])

    score = score_catalog.score_catalog(catalog, golden)

    assert score["stage4"]["per_signal"]["sold"]["f1"] == 1.0
    assert score["stage4"]["per_signal"]["urgent"]["f1"] == 0.0
    assert score["stage4"]["signal_types"] == 2
    assert score["stage4"]["macro_f1"] == 0.5


def test_stage4_skips_posts_with_no_expected_signals() -> None:
    golden = [_gold("a"), _gold("b", signals=["sold"])]
    catalog = _catalog([_item("a", signals=["urgent"]), _item("b", signals=["sold"])])

    score = score_catalog.score_catalog(catalog, golden)

    # "a" carries no expected_signals, so its predicted "urgent" is not scored.
    assert set(score["stage4"]["per_signal"]) == {"sold"}
    assert score["stage4"]["macro_f1"] == 1.0


# --- degenerate inputs -----------------------------------------------------

def test_empty_catalog_scores_zero_without_dividing_by_zero() -> None:
    score = score_catalog.score_catalog(_catalog([]), [_gold("a")])

    assert score["stage2"]["accuracy"] == 0.0
    assert score["stage3"]["price_accuracy"] == 0.0
    assert score["stage4"]["macro_f1"] == 0.0
    assert score["routing"]["attention_rate"] == 0.0
    assert score["missing_from_catalog"] == 1


def test_empty_golden_set_scores_zero_without_dividing_by_zero() -> None:
    score = score_catalog.score_catalog(_catalog([_item("a")]), [])

    assert score["scored_posts"] == 0
    assert score["stage3"]["missing_price_recall"] is None
    assert score["routing"]["total"] == 1


def test_routing_distribution_comes_from_the_whole_catalog() -> None:
    """Routing counts every catalog item, including posts with no gold label -
    the attention rate a reviewer actually faces is over the real feed."""
    catalog = _catalog([
        _item("a", bucket="auto_import"),
        _item("b", bucket="needs_attention"),
        _item("c", bucket="auto_exclude"),
        _item("d", bucket="needs_attention"),
    ])

    score = score_catalog.score_catalog(catalog, [_gold("a")])

    assert score["routing"]["needs_attention"] == 2
    assert score["routing"]["total"] == 4
    assert score["routing"]["attention_rate"] == 0.5
