"""
TypedDicts for the raw dict shapes passed between stages, plus one small
shared helper (vision_image_url()) for reading them correctly.

The TypedDicts are annotation-only - nothing here changes at runtime, they
just give IDEs/type-checkers something better than a bare `dict` to check
stage2_triage.py, stage3_extract.py, ingest.py, and eval/harness.py against.

`total=False` throughout: every field is accessed defensively via .get()
in the actual pipeline code (a raw ingest dump post and a hand-labeled
golden-set entry have overlapping but not identical shapes - e.g. raw
posts key by "id", golden entries key by "post_id" - so no field here is
guaranteed present on every Post).
"""

from __future__ import annotations

from typing import TypedDict


class CommentEntry(TypedDict, total=False):
    id: str
    username: str | None
    text: str | None
    timestamp: str
    like_count: int
    is_vendor_reply: bool  # golden-set only, added by eval/make_golden_skeleton.py


class Post(TypedDict, total=False):
    id: str  # raw ingest dump key (ingest/ingest.py)
    post_id: str  # golden-set key (eval/make_golden_skeleton.py)
    caption: str
    media_url: str
    media_type: str
    # Only populated by the Graph API for media_type == "VIDEO" (Reels
    # included - a Reel is media_type=VIDEO with media_product_type=REELS,
    # not its own media_type). media_url for VIDEO is the video FILE itself,
    # not an image - see vision_image_url() below for the field to actually
    # use wherever a static image is needed.
    thumbnail_url: str
    media_product_type: str  # "FEED" | "REELS" | "STORY" | "AD" | "IGTV"
    permalink: str
    timestamp: str
    comments: list[CommentEntry]
    has_carousel_children: bool
    # Gold labels - only present on eval/golden/*.json entries, never on a
    # raw ingest dump post.
    post_type: str
    carousel_classification: str
    expected_flag: str | None
    notes: str
    products: list[dict]


class PriceInfo(TypedDict, total=False):
    value: int | None
    currency: str | None
    # NOTE: stage3_extract.py's dummy heuristic emits "comments" (plural)
    # here, while eval/make_golden_skeleton.py's FIELD GUIDE documents the
    # hand-labeled vocabulary as "comment" (singular). Harness scoring never
    # compares this value except for the "none" case, so it's currently
    # harmless - documented here rather than silently normalized.
    source: str
    confidence: float


class QuantitySignal(TypedDict, total=False):
    kind: str  # "explicit_count" | "low_stock" | "restock" | "none"
    evidence: str | None


class ProductPrediction(TypedDict, total=False):
    name: str | None
    description: str | None
    images: list[str]
    variants: list[dict]
    price: PriceInfo
    quantity_signal: QuantitySignal
    negotiation_signal: str  # "negotiable" | "fixed" | "unknown"
    extraction_confidence: float


def vision_image_url(post: Post) -> str | None:
    """The URL safe to hand to a vision model or image hasher for this
    post. thumbnail_url only exists on VIDEO-type media (Reels included) -
    media_url there is the video file itself, not an image. Preferring
    thumbnail_url when present, falling back to media_url, covers both
    cases without every call site needing to branch on media_type itself."""
    return post.get("thumbnail_url") or post.get("media_url")
