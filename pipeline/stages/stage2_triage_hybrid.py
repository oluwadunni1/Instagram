"""
Stage 2 triage as a cross-provider cascade: Jev Pass A, Gemini on escalation.

    caption empty/emoji-only?  --yes-->  OCR the image (local, free)
             | no                          |
             |                             +-- text found --> Jev on the OCR text
             |                             |                    +-- unsure --> Gemini VISION
             |                             +-- nothing found --> Gemini VISION
             v
          Jev noul  --confidence < escalate_below-->  Gemini TEXT
             | else
             v
          Jev's answer

Three tiers, cheapest first, which is the brief's funnel applied to the
escalation path itself rather than only to the main line.

WHY THIS SHAPE. Two gated runs measured what each model can do alone on the
binary product-vs-not question:

                     autos          gadgets
    Jev alone      28/30 = 93%    33/41 = 80%   P=100% R=76%
    Gemini alone   30/30 = 100%   39/41 = 95%   P= 97% R=97%

Jev's gap is not comprehension. FIVE of its eight gadgets misses are posts
with no caption at all, where the golden set's own note reads "product and
price are written on the image". Jev is text-only and structurally cannot
answer those. On the 36 text-bearing gadgets posts it is 92% against Gemini's
94%, with ZERO false positives across all 71 posts in both vendors - it never
once called a non-listing a listing.

And unlike Gemini, Jev's confidence actually separates right from wrong:
+0.217 on autos and +0.113 on gadgets, against Gemini's +0.074. That is the
property a confidence-triggered cascade needs and the reason the existing
all-Gemini cascade's escalation trigger was never a reliable safety net.

TWO TRIGGERS, TWO TARGETS, deliberately different:

  - An empty caption goes to OCR first, then vision only if OCR reads nothing
    or Jev is still unsure. A spike over the five caption-less gadgets posts
    read the product name on 5/5 and the price on 5/5, and those five posts
    were 86% of the hybrid's gadgets bill. OCR is local and free, so a failed
    attempt costs latency and nothing else.
  - Low confidence WITH a caption goes to TEXT (flash-lite), not vision. The
    low-confidence cases observed are textual boundary calls - a post marked
    SOLD while still listing a price, an "available soon" - where looking at
    the photo adds nothing, and text is far cheaper than vision.

Emits the same binary vocabulary as stage2_triage_jev.py ("product_listing" /
"not_product"), so every downstream consumer keeps working unchanged - they
all test `!= "product_listing"` already.
"""

from __future__ import annotations

import logging

from pipeline.jev_client import DEFAULT_JEV_MODEL
from pipeline.ocr import DEFAULT_MIN_CHARS, extract_text
from pipeline.stages.stage2_triage import (
    MODEL as GEMINI_MODEL,
    TriageResult,
    _is_uninformative_caption,
    _pass_a,
    _pass_b,
)
from pipeline.stages.stage2_triage_jev import DEFAULT_THRESHOLD, triage_post_jev
from pipeline.types import Post, vision_image_url

logger = logging.getLogger(__name__)

# Confidence below which a Jev answer is sent to Gemini. Selected on the TUNE
# half (vendor_autos_01) only: both 0.80 and 0.85 reach 100% there, so the
# cheaper escalation rate wins. gadgets reaches 40/41 at 0.85, but that was
# observed with knowledge of the test half and is deliberately NOT what ships.
DEFAULT_ESCALATE_BELOW = 0.80

# Distinct token-log stage names so the two escalation paths can be costed
# separately, and so a hybrid run's Gemini calls are not confusable with a
# pure-Gemini run's.
TEXT_STAGE_NAME = "stage2_hybrid_gemini_text"
VISION_STAGE_NAME = "stage2_hybrid_gemini_vision"


def _to_binary(result: TriageResult) -> str:
    """Collapse the five-way Gemini vocabulary onto the binary one.

    Lossless for this stage's purpose: everything downstream tests
    `!= "product_listing"` anyway.
    """
    return "product_listing" if result.post_type == "product_listing" else "not_product"


def triage_post_hybrid(
    post: Post,
    profile: dict | None = None,
    jev_model: str = DEFAULT_JEV_MODEL,
    text_model: str = GEMINI_MODEL,
    vision_model: str = GEMINI_MODEL,
    threshold: float = DEFAULT_THRESHOLD,
    escalate_below: float = DEFAULT_ESCALATE_BELOW,
    use_ocr: bool = True,
    ocr_min_chars: int = DEFAULT_MIN_CHARS,
    run_id: str | None = None,
    vendor_id: str | None = None,
) -> dict:
    """Classifies one post as product_listing or not, escalating when needed.

    Args:
        threshold: Jev's product/not decision boundary (P(yes) >= this is a
            listing). NOT the escalation trigger - see escalate_below. The two
            are deliberately named differently because conflating them would
            be silent.
        escalate_below: Jev confidence under which the post goes to Gemini.
        jev_model / text_model / vision_model: real model strings. None of
            them may be called `model` - load_stage_fn() reserves and strips
            that key and now raises rather than binding silently.

    Returns:
        {"post_type": "product_listing" | "not_product", "confidence": float,
         "escalated": bool, "escalation_reason": str | None, "p_product": float | None}
    """
    caption = (post.get("caption") or "").strip()
    post_id = post.get("post_id") or post.get("id") or ""
    image_url = vision_image_url(post)

    # --- trigger 1: nothing to read ---------------------------------------
    # Free and deterministic, so it runs before anything is spent. Skipping
    # the Jev call here is the point: a text model on an empty caption is a
    # guaranteed waste.
    if _is_uninformative_caption(caption) and image_url:
        # Try OCR before paying for vision. These posts carry the product and
        # price printed on the picture, which is what OCR is for, and a spike
        # over the five caption-less gadgets posts read the name on 5/5 and the
        # price on 5/5. OCR is local and free, so a failed attempt costs only
        # latency and we fall through to vision exactly as before.
        ocr_text = None
        if use_ocr:
            ocr_text, ocr_reason = extract_text(
                image_url, post_id=post_id, account_label=vendor_id, min_chars=ocr_min_chars,
            )
            logger.info("[stage2_hybrid] %s: no caption - OCR %s", post_id, ocr_reason)

        if ocr_text:
            # OCR turned a caption-less post into a text-bearing one, which
            # puts it back inside Jev's reach. This is the whole point: Jev
            # cannot see images, and that was its one structural limit as a
            # Pass A.
            ocr_post = {**post, "caption": ocr_text}
            jev_ocr = triage_post_jev(
                ocr_post, profile=profile, jev_model=jev_model, threshold=threshold,
                run_id=run_id, vendor_id=vendor_id,
            )
            if jev_ocr["confidence"] >= escalate_below:
                return {**jev_ocr, "escalated": True, "escalation_reason": "ocr"}
            # Jev is unsure even with the OCR text. Vision is still the
            # fallback - OCR narrows the vision path, it does not replace it.
            logger.info("[stage2_hybrid] %s: OCR read but Jev unsure (%.2f) - vision",
                         post_id, jev_ocr["confidence"])

        vision = _pass_b(post, profile, vision_model, run_id, vendor_id, post_id,
                          stage_name=VISION_STAGE_NAME)
        return {
            "post_type": _to_binary(vision),
            "confidence": vision.confidence,
            "escalated": True,
            "escalation_reason": "no_caption_vision",
            "p_product": None,
        }

    # An empty caption with no usable image has nothing to escalate TO, so
    # fall through to Jev rather than crash. It will almost certainly answer
    # "not_product", which is the right call on an empty post anyway.

    # --- Pass A: Jev ------------------------------------------------------
    jev = triage_post_jev(
        post, profile=profile, jev_model=jev_model, threshold=threshold,
        run_id=run_id, vendor_id=vendor_id,
    )

    if jev["confidence"] >= escalate_below:
        return {**jev, "escalation_reason": None}

    # --- trigger 2: Jev is unsure -----------------------------------------
    # Text, not vision: these are textual boundary calls, and text is cheaper.
    logger.info("[stage2_hybrid] %s: confidence %.2f < %.2f - escalating to Gemini text",
                 post_id, jev["confidence"], escalate_below)
    text = _pass_a(post, profile, text_model, run_id, vendor_id, post_id,
                    stage_name=TEXT_STAGE_NAME)
    return {
        "post_type": _to_binary(text),
        "confidence": text.confidence,
        "escalated": True,
        "escalation_reason": "low_confidence",
        "p_product": jev.get("p_product"),
    }
