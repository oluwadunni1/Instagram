"""Stage 5 routing tests.

Stage 5 makes zero LLM calls, so unlike Stages 2-4 its behavior is fully
determined by its inputs - which makes it the one stage where a plain unit
test, not the eval harness, is the right verification tool.
"""

from __future__ import annotations

from pipeline.stages.stage5_reconcile import (
    MISSING_PRICE_FLAG,
    NO_PRODUCTS_FLAG,
    SOLD_FLAG,
    UNKNOWN_NAME_FLAG,
    route_post,
)

POST = {"post_id": "p1", "media_url": "https://cdn.example.com/p.jpg"}
LISTING = {"post_type": "product_listing"}


def _product(name: str | None = "Thing", price_source: str = "caption") -> dict:
    return {
        "name": name,
        "price": {"value": 1000 if price_source != "none" else None, "source": price_source},
        "extraction_confidence": 0.9,
    }


def test_flags_are_deduplicated_across_products() -> None:
    """One flag per distinct reason, not one per offending product.

    Before this fix a 3-product post with no prices emitted
    MISSING_PRICE_FLAG three times, which inflated run_stage5.py's "Top flag
    reasons: [Nx] ..." frequency table - the table whose lines get quoted
    verbatim into README.md - and repeated the same sentence three times on
    the reviewer's needs-attention line. Real occurrence: post
    17966735571148449 in vendor_gadgets_01 has 3 products, all price.source
    == "none".
    """
    products = [_product(price_source="none") for _ in range(3)]

    result = route_post(POST, LISTING, products, [])

    assert result["flags"] == [MISSING_PRICE_FLAG]
    assert result["bucket"] == "needs_attention"


def test_distinct_flags_all_survive_deduplication() -> None:
    """Dedup must collapse repeats without dropping different reasons."""
    products = [
        _product(name=None, price_source="none"),
        _product(name=None, price_source="none"),
    ]

    result = route_post(POST, LISTING, products, [])

    assert result["flags"] == [MISSING_PRICE_FLAG, UNKNOWN_NAME_FLAG]


def test_clean_products_route_to_auto_import() -> None:
    result = route_post(POST, LISTING, [_product(), _product(name="Other")], [])
    assert result["flags"] == []
    assert result["bucket"] == "auto_import"


def test_non_listing_is_excluded_before_any_flagging() -> None:
    result = route_post(POST, {"post_type": "lifestyle"}, [_product(price_source="none")], [])
    assert result["bucket"] == "auto_exclude"
    assert result["flags"] == []


def test_sold_signal_short_circuits_to_needs_attention() -> None:
    result = route_post(POST, LISTING, [_product()], [{"signal": "sold"}])
    assert result["bucket"] == "needs_attention"
    assert result["flags"] == [SOLD_FLAG]


def test_no_products_is_flagged_not_auto_imported() -> None:
    result = route_post(POST, LISTING, [], [])
    assert result["bucket"] == "needs_attention"
    assert result["flags"] == [NO_PRODUCTS_FLAG]


def test_thumbnail_url_is_preferred_for_reels() -> None:
    """media_url on a Reel is the .mp4; the reviewer needs the still."""
    reel = {
        "post_id": "p2",
        "media_type": "VIDEO",
        "media_url": "https://cdn.example.com/v.mp4",
        "thumbnail_url": "https://cdn.example.com/v.jpg",
    }
    result = route_post(reel, LISTING, [_product()], [])
    assert result["thumbnail_url"].endswith(".jpg")
