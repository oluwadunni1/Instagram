"""
Stage 5 - Flags & Routing. Zero LLM calls: pure deterministic Python over
the already-computed Stage 2/3/4 outputs, routing each post's extracted
products into one of three buckets for the POC catalog import flow.
"""

from typing import Literal

from pipeline.types import Post

Bucket = Literal["auto_import", "needs_attention", "auto_exclude"]

MISSING_PRICE_FLAG = "Missing price — set manually or mark as DM for price"
UNKNOWN_NAME_FLAG = "Product name unknown — please verify"
LOW_CONFIDENCE_FLAG = "Low extraction confidence — please verify product details"
SOLD_FLAG = "Item may already be sold — verify before importing"
NO_PRODUCTS_FLAG = "No products could be extracted from this post"

LOW_CONFIDENCE_THRESHOLD = 0.65


def route_post(
    post: Post,
    stage2_result: dict,
    stage3_result: list[dict],
    stage4_result: list[dict],
) -> dict:
    """Routes a post's extracted products into one of three buckets.

    Priority order (first match wins, except the per-product flag pass
    which collects across every product before deciding auto_import vs.
    needs_attention):
      1. post_type isn't product_listing -> auto_exclude
      2. any stage4 "sold" signal -> needs_attention
      3. no products extracted -> needs_attention
      4. per-product checks (missing price, unknown name, low confidence)
         -> needs_attention if any flag fires, else auto_import
    """
    post_id = post.get("post_id") or post.get("id") or ""
    thumbnail_url = post.get("media_url")

    if stage2_result.get("post_type") != "product_listing":
        return {
            "post_id": post_id,
            "bucket": "auto_exclude",
            "flags": [],
            "thumbnail_url": thumbnail_url,
            "products": stage3_result,
            "signals": stage4_result,
        }

    if any(signal.get("signal") == "sold" for signal in stage4_result):
        return {
            "post_id": post_id,
            "bucket": "needs_attention",
            "flags": [SOLD_FLAG],
            "thumbnail_url": thumbnail_url,
            "products": stage3_result,
            "signals": stage4_result,
        }

    if not stage3_result:
        return {
            "post_id": post_id,
            "bucket": "needs_attention",
            "flags": [NO_PRODUCTS_FLAG],
            "thumbnail_url": thumbnail_url,
            "products": stage3_result,
            "signals": stage4_result,
        }

    flags: list[str] = []
    for product in stage3_result:
        price = product.get("price") or {}
        if price.get("source") == "none" and product.get("negotiation_signal") != "negotiable":
            flags.append(MISSING_PRICE_FLAG)
        if product.get("name") is None:
            flags.append(UNKNOWN_NAME_FLAG)
        extraction_confidence = product.get("extraction_confidence")
        if extraction_confidence is not None and extraction_confidence < LOW_CONFIDENCE_THRESHOLD:
            flags.append(LOW_CONFIDENCE_FLAG)

    bucket: Bucket = "needs_attention" if flags else "auto_import"

    return {
        "post_id": post_id,
        "bucket": bucket,
        "flags": flags,
        "thumbnail_url": thumbnail_url,
        "products": stage3_result,
        "signals": stage4_result,
    }
