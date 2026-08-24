"""
Stage 2 — Post triage (DUMMY / heuristic stub).

This is not the real Stage 2. It's a zero-cost placeholder so the
eval harness has something to score before any model is wired up —
satisfies the Week 1 milestone ("eval harness scores a dummy
pipeline"). Swap this for a real cascade (cheap text model -> vision
escalation, per brief §5) once the harness loop is proven out.

Contract: every real stage implementation must match this same
function signature (post_dict, profile) -> result_dict, so swapping
dummy -> real is a config change (see pipeline/config/registry.json),
not a harness rewrite.
"""

PRODUCT_HINTS = ("₦", "$", "price", "swap possible", "negotiable")
ANNOUNCEMENT_HINTS = ("clearance", "0% down", "financing", "dm \"finance\"", "promo")


def triage_post(post: dict, profile: dict | None = None) -> dict:
    caption = (post.get("caption") or "").lower()

    if any(hint in caption for hint in ANNOUNCEMENT_HINTS):
        return {"post_type": "announcement", "confidence": 0.6}

    if any(hint in caption for hint in PRODUCT_HINTS):
        return {"post_type": "product_listing", "confidence": 0.6}

    # Genuinely unsure — real Stage 2 would escalate to vision here (§5 Pass B)
    return {"post_type": "product_listing", "confidence": 0.3}