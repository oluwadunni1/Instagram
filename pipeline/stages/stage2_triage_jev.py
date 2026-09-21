"""
Stage 2 triage on Jev, as a BINARY decision: product listing, or not.

Why binary rather than the five-way enum stage2_triage.py uses: nothing
downstream consumes the other four categories. Every functional consumer tests
only `!= "product_listing"` - stage5_reconcile.py routes non-listings to
auto_exclude, stage6_sync.py marks them excluded, run_pipeline.py gates Stage 3
on it. The five-way value survives only as display text. The brief's own
success criterion is "Product-vs-not-product triage precision >= 90%", which
was binary all along.

Binary also makes Stage 2 measurable. The golden set splits 59 product /
12 not-product with both classes populated, where the five-way has three
classes at n <= 2 that cannot be scored at all.

CAVEAT that must travel with any comparison from this module: Jev chooses
between 2 options while the Gemini stage chooses between 5, so part of any Jev
advantage is the smaller option set rather than the model. This is not a
like-for-like result.

Returns the same `post_type` vocabulary the rest of the pipeline already
tests against, so nothing downstream needs to change: "product_listing", or
"not_product" for everything else.
"""

from __future__ import annotations

import logging

from pipeline.jev_client import DEFAULT_JEV_MODEL, decide, noul_probability
from pipeline.types import Post

logger = logging.getLogger(__name__)

QUESTION_ID = "is_product_listing"

# Default decision boundary. 0.5 is the neutral starting point, NOT a tuned
# value - sweeping it belongs in the calibration work, on the tune half only.
DEFAULT_THRESHOLD = 0.5

# The operative test from eval/LABEL_CODEBOOK.md, which reconstructs the rule
# the golden set's labels actually follow. Keep the two aligned: the codebook
# is the spec, this is one consumer of it.
#
# The two clauses after the question are the ones that decide the hard cases,
# and both were learned the expensive way. Writing "promotional implies advert"
# into a rubric made ad_creative a magnet that took 7 of 11 predictions in the
# first probes.
INSTRUCTIONS = (
    "Is a specific product being offered for sale in this post, right now? "
    "Answer yes only if a buyer could ask to purchase a specific item today. "
    "Pre-orders count when a real offer is attached, and a stated price is NOT "
    "required - 'DM for price' is still an offer. "
    "Answer no for posts that inform customers without offering a specific item "
    "(opening hours, location, a branch move, a clearance sale, a financing "
    "offer, a recurring discount day), for posts about a sale that already "
    "completed such as a delivery or handover, for polls and engagement posts, "
    "and for personal or lifestyle content. "
    "Two rules decide most hard cases: being promotional or enthusiastic does "
    "not by itself make a post an offer, and a post that reads like a listing "
    "but offers nothing purchasable yet - an unreleased model, for example - "
    "is not an offer."
)


def triage_post_jev(
    post: Post,
    profile: dict | None = None,
    jev_model: str = DEFAULT_JEV_MODEL,
    threshold: float = DEFAULT_THRESHOLD,
    run_id: str | None = None,
    vendor_id: str | None = None,
) -> dict:
    """Classifies one post as product_listing or not, via a single Jev noul.

    Args:
        post: Post dict with at least "caption".
        profile: Stage 1's AccountProfile as a dict. Passed into the state
            because the Gemini stage gets it too - withholding it here would
            repeat the failure that invalidated every figure before
            2026-09-09, where the harness and the chained path fed the same
            stage different inputs.
        jev_model: deliberately NOT named `model`. load_stage_fn() reserves and
            strips that key, so a parameter called `model` silently never
            receives its configured value - the bug that ran a four-model
            Stage 4 comparison on one model.
        threshold: P(yes) at or above this is product_listing.
        run_id: token usage is logged only when set.

    Returns:
        {"post_type": "product_listing" | "not_product",
         "confidence": float, "escalated": False, "p_product": float}

        `escalated` is always False: Jev has no vision path, so the honest
        comparison is against the Gemini stage's Pass A line rather than its
        overall figure.
    """
    caption = (post.get("caption") or "").strip()
    post_id = post.get("post_id") or post.get("id") or ""

    # An object state rather than a bare string, so the profile travels with
    # the caption the way it does in the Gemini prompt.
    state: dict = {"caption": caption}
    if profile:
        state["account_profile"] = profile

    body = decide(
        state=state,
        questions={
            QUESTION_ID: {"type": "noul", "instructions": INSTRUCTIONS},
        },
        jev_model=jev_model,
        stage_name="stage2_triage_jev",
        run_id=run_id,
        vendor_id=vendor_id,
        post_id=post_id,
    )

    p_product = noul_probability(body, QUESTION_ID)
    is_product = p_product >= threshold

    # A noul returns P(yes) and no separate confidence field, unlike choice and
    # score. Distance from the decision boundary is the honest analogue: 0.95
    # and 0.05 are both confident answers, 0.51 is not.
    confidence = max(p_product, 1.0 - p_product)

    logger.debug("[stage2_triage_jev] %s: p_product=%.3f -> %s (conf=%.3f)",
                  post_id, p_product, "product_listing" if is_product else "not_product",
                  confidence)

    return {
        "post_type": "product_listing" if is_product else "not_product",
        "confidence": confidence,
        "escalated": False,
        "p_product": p_product,
    }
