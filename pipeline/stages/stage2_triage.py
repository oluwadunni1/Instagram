"""
Stage 2 - Post triage (brief section 5), REAL cascade implementation.

Pass A (cheap, text-only): caption + Stage 1's account profile -> a
small text model classifies the post. Handles the majority of posts
at minimum cost.

Pass B (vision, escalation ONLY): triggered when Pass A's confidence
is below threshold, OR the caption is empty/emoji-only (no text
signal to work with at all). Sends the post's hero image to a vision
model instead. This should be the minority path - brief section 5
flags that if more than 40% of posts escalate, the TEXT PROMPT needs
work before reaching for a bigger model.

Every result reports whether it escalated, so the harness can compute
and print the escalation rate - the brief treats this as a cost
signal to watch, not just a debugging detail.
"""

import json
import re
from typing import Literal

import litellm
from pydantic import BaseModel

from pipeline.llm_client import complete_structured, log_token_usage, throttle
from pipeline.types import Post, vision_image_url

POST_TYPES = Literal[
    "product_listing", "announcement", "testimonial_repost", "meme_personal", "ad_creative"
]

# In-module fallback for triage_post()'s text_model/vision_model, mirroring
# stage3_extract.py's and stage4_signals.py's MODEL constants. Was previously
# the literal "openrouter/google/gemini-flash-1.5-8b" - a model that has since
# been retired and 404s. That was dormant only because every shipping config
# sets both text_model and vision_model explicitly; the first config to omit
# either, or any direct triage_post(post) call, would have 404'd on every
# Stage 2 call with no loud failure (2026-09-08 audit).
MODEL = "gemini/gemini-3.5-flash-lite"

# Caption is "non-informative" if, after stripping emoji/punctuation/whitespace,
# fewer than this many characters remain - triggers escalation regardless of
# Pass A's stated confidence, since there was barely any text to classify from.
MIN_INFORMATIVE_CAPTION_CHARS = 3

_WORD_CHARS_RE = re.compile(r"[^\w]", flags=re.UNICODE)


class TriageResult(BaseModel):
    post_type: POST_TYPES
    confidence: float
    escalated: bool = False  # set by OUR code after the call, not the model - default lets
                              # model_validate() succeed on the model's raw {post_type,
                              # confidence} response before we override this ourselves


# The post-type definitions, shared by both passes. Derived from
# eval/LABEL_CODEBOOK.md, which reconstructs the labeler's decision rule from
# the golden set's own notes. Before 2026-09-21 both prompts listed the five
# names and defined none of them, so the model was asked to reproduce a
# boundary nobody had written down. Adding these RE-BASELINES every Stage 2
# figure: numbers measured before and after are not comparable.
POST_TYPE_DEFINITIONS = """Decide in this order and take the first match:

1. product_listing - a specific item is being offered for sale right now. \
Pre-orders count when a real offer is attached. A stated price is NOT \
required; "DM for price" is still an offer.
2. testimonial_repost - a sale that already completed: a delivery, a \
handover, or a customer's own words reshared. The product is often named \
and visible but is no longer available.
3. announcement - the business is informing customers: opening hours, \
location, policy, availability, or the mechanics of a promotion. No \
specific item is offered. Clearance sales, financing offers and recurring \
discount days belong HERE. So do posts naming products that are not \
purchasable yet, such as unreleased models.
4. ad_creative - a produced engagement device built around products, such \
as a poll or a "pick one" question, whose purpose is interaction rather \
than an offer. RARE.
5. meme_personal - personal or lifestyle content in a conversational voice. \
Nothing sold, no business information conveyed.

Two rules that decide most hard cases: being promotional or enthusiastic \
does NOT by itself make a post an ad_creative, and a post that reads like a \
listing but offers nothing purchasable is an announcement."""


PASS_A_SYSTEM_PROMPT = f"""You are triaging an Instagram post for a vendor \
account, to decide whether it's a product listing or something else. Use \
the account profile for context (e.g. a "food" account's "portions left" \
language differs from a "fashion" account's "sizes 10-16").

{POST_TYPE_DEFINITIONS}

Return ONLY a JSON object with exactly these fields:
- post_type: one of product_listing, announcement, testimonial_repost, meme_personal, ad_creative
- confidence: a float 0.0-1.0 reflecting how sure you are, based ONLY on \
the caption text provided. If the caption is vague, short, or could \
plausibly be more than one category, give a LOW confidence rather than \
guessing - a downstream step will look at the image when confidence is low."""

PASS_B_SYSTEM_PROMPT = f"""You are triaging an Instagram post for a vendor \
account. The caption text was not enough to classify this post confidently, \
so you are being shown the actual photo instead. Use the account profile \
for context.

{POST_TYPE_DEFINITIONS}

Return ONLY a JSON object with exactly these fields:
- post_type: one of product_listing, announcement, testimonial_repost, meme_personal, ad_creative
- confidence: a float 0.0-1.0 reflecting how sure you are, now that you \
can see the image."""


def _is_uninformative_caption(caption: str) -> bool:
    """True if, after stripping emoji/punctuation/whitespace, the caption
    has too little text to classify from - regardless of what Pass A's
    confidence would say."""
    stripped = _WORD_CHARS_RE.sub("", caption or "")
    return len(stripped) < MIN_INFORMATIVE_CAPTION_CHARS


def _pass_a(
    post: Post,
    profile: dict | None,
    text_model: str,
    run_id: str | None,
    vendor_id: str | None,
    post_id: str,
    stage_name: str = "stage2_triage_pass_a",
) -> TriageResult:
    """Cheap text-only classification pass.

    stage_name defaults to this stage's own label and exists so another
    cascade can reuse this pass while keeping its rows distinguishable in
    report/token_log.csv - stage2_triage_hybrid.py calls it as Gemini's
    escalation target, and without a distinct label its cost could not be
    told apart from a pure-Gemini run.
    """
    caption = post.get("caption") or ""
    profile_context = f"Account profile: {profile}" if profile else "Account profile: unavailable"
    user_prompt = f"{profile_context}\n\nCaption:\n{caption}"

    result = complete_structured(
        model=text_model,
        system_prompt=PASS_A_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema=TriageResult,
        stage_name=stage_name,
        run_id=run_id,
        vendor_id=vendor_id,
        post_id=post_id,
        escalated=False,
    )
    result.escalated = False
    return result


def _pass_b(
    post: Post,
    profile: dict | None,
    vision_model: str,
    run_id: str | None,
    vendor_id: str | None,
    post_id: str,
    stage_name: str = "stage2_triage_pass_b",
) -> TriageResult:
    """Vision escalation. NOTE: complete_structured() currently only sends
    text messages - this constructs a multimodal message directly via
    litellm's OpenAI-compatible image_url format, since Pass B is the one
    call site in the pipeline that needs it. If more stages need vision
    later, this multimodal message-building belongs in llm_client.py
    instead of being duplicated per stage. Since this bypasses
    complete_structured(), it logs token usage itself instead of getting it
    for free."""
    image_url = vision_image_url(post)
    caption = post.get("caption") or "(no caption)"
    profile_context = f"Account profile: {profile}" if profile else "Account profile: unavailable"

    messages = [
        {"role": "system", "content": PASS_B_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"{profile_context}\n\nCaption: {caption}"},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        },
    ]

    # Bypasses complete_structured(), so it must throttle itself - this call
    # spends the same per-minute provider quota as every other one.
    throttle()
    response = litellm.completion(
        model=vision_model,
        messages=messages,
        temperature=0.0,
        response_format={"type": "json_object"},
        metadata={"run_name": stage_name, "tags": [stage_name]},
    )
    parsed = json.loads(response.choices[0].message.content)
    result = TriageResult.model_validate(parsed)
    result.escalated = True

    if run_id is not None:
        usage = getattr(response, "usage", None)
        log_token_usage(
            run_id=run_id,
            vendor_id=vendor_id or "unknown",
            post_id=post_id,
            stage=stage_name,
            model=vision_model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage, "total_tokens", None),
            escalated=True,
        )

    return result


def triage_post(
    post: Post,
    profile: dict | None = None,
    text_model: str = MODEL,
    vision_model: str = MODEL,
    confidence_threshold: float = 0.7,
    run_id: str | None = None,
    vendor_id: str | None = None,
) -> dict:
    """Classifies one post via the Pass A -> Pass B cascade.

    Pass B only runs when the caption is uninformative or Pass A's
    confidence is below confidence_threshold, AND the post has a
    media_url to escalate to.

    Args:
        post: Post dict with at least "caption"; "media_url" required for
            escalation to be possible at all.
        profile: Stage 1's AccountProfile (as a dict), or None.
        text_model: litellm model string for Pass A.
        vision_model: litellm model string for Pass B.
        confidence_threshold: Pass A confidence below this triggers escalation.
        run_id: Groups this call's report/token_log.csv row with the rest of
            one eval/harness.py run - token usage is only logged when this
            is set (see complete_structured() in pipeline/llm_client.py).
        vendor_id: Account label, for the token log's "vendor_id" column.

    Returns:
        dict matching TriageResult's fields (post_type, confidence, escalated).
    """
    caption = post.get("caption") or ""
    post_id = post.get("post_id") or post.get("id") or ""

    if _is_uninformative_caption(caption) and vision_image_url(post):
        result = _pass_b(post, profile, vision_model, run_id, vendor_id, post_id)
    else:
        result = _pass_a(post, profile, text_model, run_id, vendor_id, post_id)
        if result.confidence < confidence_threshold and vision_image_url(post):
            result = _pass_b(post, profile, vision_model, run_id, vendor_id, post_id)

    return result.model_dump()