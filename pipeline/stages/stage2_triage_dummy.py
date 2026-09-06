"""
Stage 2 - Post triage (DUMMY / heuristic stub) - PRESERVED for baseline
comparison purposes only.

This is the original zero-cost keyword-matching stub, kept separate from
the real cascade (stage2_triage.py) specifically so it can be re-run
against a growing golden set to produce a documented "before" baseline
whenever you want to measure how much a real model actually improves
things. Never wired into production - only referenced by
pipeline/config/experiments/dummy_baseline.yaml. stage3_extract.py's dummy
stub follows the same pattern, but currently has no real-model sibling yet.
"""

from pipeline.types import Post

PRODUCT_HINTS = ("₦", "$", "price", "swap possible", "negotiable")
ANNOUNCEMENT_HINTS = ("clearance", "0% down", "financing", "dm \"finance\"", "promo")


def triage_post(
    post: Post,
    profile: dict | None = None,
    run_id: str | None = None,
    vendor_id: str | None = None,
) -> dict:
    """Zero-cost keyword-match classification - see module docstring.

    run_id/vendor_id are accepted (and ignored) so eval/harness.py can bind
    them into every stage2_triage call uniformly - this stub makes no LLM
    calls, so there's nothing to log."""
    caption = (post.get("caption") or "").lower()

    if any(hint in caption for hint in ANNOUNCEMENT_HINTS):
        return {"post_type": "announcement", "confidence": 0.6}

    if any(hint in caption for hint in PRODUCT_HINTS):
        return {"post_type": "product_listing", "confidence": 0.6}

    return {"post_type": "product_listing", "confidence": 0.3}