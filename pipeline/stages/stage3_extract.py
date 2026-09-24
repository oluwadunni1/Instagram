"""
Stage 3 - Product extraction, REAL LLM implementation with a Pass A/Pass B
vision-escalation cascade - same architecture as stage2_triage.py.

Pass A (cheap, text-only): caption + vendor's own comment replies -> a small
text model extracts the product array. Handles the majority of posts at
minimum cost.

Pass B (vision, escalation ONLY): triggered when any extracted product is
missing a name, has no price at all, or falls below the confidence bar -
catches vendors who write the product name/specs/price directly on the
photo instead of (or in addition to) the caption. Sends the post's hero
image to a vision model instead of guessing from incomplete text. Skipped
entirely when the post's own caption/comments say "DM for price" (or
"inbox/call for price") - that's Pass A correctly reporting a confirmed
absence, not an uncertain one, so vision has nothing left to find (see
DM_FOR_PRICE_RE).

If the LLM call fails for any reason (rate limit, network error, bad JSON/
schema after complete_structured()'s own retries are exhausted), this logs
a warning and falls back to the original zero-cost regex heuristic rather
than letting Stage 3 crash the eval run.
"""

import json
import logging
import re
from typing import Literal

import litellm
from pydantic import BaseModel

from pipeline.llm_client import complete_structured, log_token_usage, throttle
from pipeline.ocr import DEFAULT_MIN_CHARS, extract_text
from pipeline.stages.stage2_triage import _is_uninformative_caption
from pipeline.types import Post, ProductPrediction, vision_image_url

logger = logging.getLogger(__name__)

MODEL = "gemini/gemini-3.5-flash-lite"

ESCALATION_CONFIDENCE_THRESHOLD = 0.6

PASS_B_SUFFIX = (
    "\n\nWe couldn't find complete product details in the text. Please look "
    "closely at the provided image to see if the product name, specs, or "
    "price are written directly on the photo."
)

# ---------------------------------------------------------------------------
# The OCR price guard.
#
# Stage 2 only asks "is this a product?", so a garbled OCR read is a cheap
# error. Stage 3 reads the PRICE, which the brief calls the one unforgivable
# failure mode - eval/harness.py prints "MUST BE 100%" on missing-price recall
# for exactly this reason. pipeline/ocr.py's docstring records the three things
# its spike found, and two of them are attacks on the price specifically:
#
#   - "N500.000", a period used as the thousands separator. float() on that
#     gives 500, not 500000 - a 1000x error on a real listing.
#   - distractor numbers everywhere: an IMEI (352142373198245), model codes
#     (A3288S), storage sizes. "longest digit run is the price" returns an IMEI.
#
# So a price the model attributes to the image is only allowed to stand when
# the exact integer is recoverable from the raw OCR text by rules that REFUSE
# to guess. Ungrounded means voided and escalated to vision, which is the
# behaviour that existed before OCR - the guard can cost a call, never a price.
#
# Checked against the five committed caches in runs/vendor_gadgets_01/ocr/
# before this was wired: 5/5 grounded against their gold prices, and the IMEI
# produced no candidate at all, because it carries no currency marker.

# A money-shaped token: a currency marker, then digits separated by . , or
# SAME-LINE whitespace. \s would be wrong here - it crosses newlines, and
# "PRICENGN1,400,000" followed by a line reading "16Pk" then parses as the
# group sequence 1/400/000/16 and is refused. That was a real miss in the spike.
OCR_MONEY_RE = re.compile(
    r"(?:\u20a6|NGN|N|\$)\s*([0-9][0-9.,\t ]*[0-9]|[0-9])",
    re.IGNORECASE,
)

# Below this a "price" is a page number or a spec figure, not a naira asking price.
MIN_PLAUSIBLE_OCR_PRICE = 100


def _normalise_ocr_price(token: str) -> int | None:
    """One money token as an integer, or None when it is ambiguous.

    A separator followed by exactly three digits is a thousands separator and
    is stripped, so "500.000" is 500000. A trailing group of one or two digits
    could be a decimal fraction OR a mangled read, and there is no safe way to
    tell them apart - so it is refused rather than guessed. That one rule is
    what stops the 1000x error, and it is why this returns None instead of a
    best effort.
    """
    groups = [g for g in re.split(r"[.,\t ]+", token.strip()) if g]
    if not groups:
        return None
    if len(groups) == 1:
        return int(groups[0])
    if any(len(g) != 3 for g in groups[1:]) or len(groups[0]) > 3:
        return None
    return int("".join(groups))


def _ocr_price_candidates(ocr_text: str | None) -> set[int]:
    """Every integer the raw OCR text could legitimately be asserting as a price."""
    out: set[int] = set()
    for match in OCR_MONEY_RE.finditer(ocr_text or ""):
        value = _normalise_ocr_price(match.group(1))
        if value is not None and value >= MIN_PLAUSIBLE_OCR_PRICE:
            out.add(value)
    return out


def _guard_ocr_prices(result: "ProductExtractionResult", ocr_text: str | None) -> bool:
    """Void any image-sourced price the OCR text does not actually support.

    Only touches products whose price.source is "image" - a price read from the
    caption or from a vendor comment was never OCR's to get wrong. Returns True
    when anything was voided, which means the post still needs escalation.
    """
    candidates = _ocr_price_candidates(ocr_text)
    voided = False
    for item in result.products:
        if item.price.source != "image" or item.price.value is None:
            continue
        if item.price.value in candidates:
            continue
        logger.warning(
            "[stage3_extract] voiding ungrounded OCR price %s (candidates: %s)",
            item.price.value,
            sorted(candidates) or "none",
        )
        item.price.value = None
        item.price.currency = None
        item.price.source = "none"
        item.price.confidence = 0.0
        voided = True
    return voided

NAIRA_RE = re.compile(r"₦\s?([\d,]+)")
USD_RE = re.compile(r"\$\s?([\d,]+)")

# Matches "DM for price", "send a DM/WhatsApp for the price", "inbox for
# price", "call for price", etc. - the same phrasing the CRITICAL section of
# SYSTEM_PROMPT tells the model always means price.source="none" (confirmed
# absent, not just unknown). Used both by _regex_fallback() and to skip a
# wasted vision escalation in extract_product() - if the vendor is on record
# saying "DM for price" in the post's own caption/comments, the price isn't
# sitting on the photo either; escalating just re-asks the same unanswerable
# question at 2x cost (see README.md's Stage 3 root-cause analysis).
#
# price(s|ing): the plural matters in practice - a multi-variant listing
# ("iPhone 12 64GB / 128GB / 256GB ... send us a DM for prices!") naturally
# pluralizes, and the singular-only pattern silently missed every such post.
DM_FOR_PRICE_RE = re.compile(
    r"\b(dm|inbox|call)\b[^.!?\n]{0,25}\bfor\b[^.!?\n]{0,10}\bpric(e|es|ing)\b",
    re.IGNORECASE,
)

# A multi-product catalog post needs room for one JSON object PER product, and
# complete_structured()'s own default is 1024 - enough for the 1-3 products a
# typical single-item listing carries, and silently not enough beyond that.
#
# Measured 2026-09-09: one extracted product serialises to ~94 tokens, so a
# 9-product post needs ~850 (marginal once the wrapper is counted) and the
# 15-product post in vendor_gadgets_01 needs ~1,400. Over the limit the model
# emits truncated JSON, schema validation fails, complete_structured() burns its
# 3 repair retries, and extract_product() falls back to _regex_fallback() - so
# the post is answered by a heuristic and the run reports a clean score.
#
# This is model-independent: the SAME three vendor_gadgets_01 posts fell back on
# Gemini (twice) and on GPT-4o Mini. vendor_autos_01 tops out at 3 products/post
# and never hit it, which is why it validated first time and hid the bug.
#
# Overridable per experiment via a `max_tokens:` key in the stage3_extract block
# of any pipeline/config/experiments/*.yaml - load_stage_fn() binds it like any
# other non-reserved config key.
DEFAULT_MAX_TOKENS = 4096

SYSTEM_PROMPT = """You are extracting structured product-listing data from a single Instagram post for
a vendor account. The account may sell any kind of product; for an automotive vendor,
each distinct vehicle offered in the post is one product. Use the account profile for
context (e.g. category, vendor_username).

Return ONLY a JSON object with exactly one field:
- products: a JSON array, one element per distinct product actually being offered for
  sale in this post. If the post is a single-item listing, return a one-element array.
  If it lists several distinct items (e.g. a carousel comparing two cars, or a lineup
  of several trims), return one element per item. Do NOT include an item that is only
  mentioned as a comparison, a "why settle for X" put-down, or a headline hook, and is
  not actually being offered with its own price/description in this post.

If the post is clearly a product listing but the caption is too vague to identify a
specific product (e.g. "Make una dm for more info" with no product details at all),
you MUST still return a single product with name set to null and price.source
set to "none". Do NOT return an empty products array for a product-listing post.

For carousel/gallery posts, if the same item appears across multiple slides (e.g.
different angles or photos of one car), treat it as ONE product - do not duplicate it.
Only return multiple products when the post is genuinely offering different items for
sale (different make/model, different trim, or explicitly different price points).

Each element of `products` must have exactly these fields:
- name: string or null. Include year, make, model, and trim/spec EXACTLY as
  stated in the caption (e.g. "2016 Toyota Camry Sport"). Never invent a year, make,
  model, or trim that isn't stated.
- description: a concise, factual description of the product using specs from the
  caption verbatim (year, make, model, trim, condition, color). No editorializing -
  don't add marketing language or opinions that aren't in the source text.
- price: an object with:
  - value: integer or null - the raw numeric sale price for THIS product, no currency
    symbols or commas. If the price is spelled out in words (e.g. "one hundred and
    fifteen m (115m)"), convert it to the integer it represents (115000000).
  - currency: "NGN", "USD", etc, or null.
  - source: "caption" if the price appears in the post caption, "comments" if it only
    appears in the vendor's own comment replies (provided separately below), "image"
    if it's only visible as text/overlay written directly on the photo itself (not in
    the caption or comments), or "none" if there is no price for this product anywhere.
  - confidence: a float 0.0-1.0 for how confident you are that this is the correct
    asking price for this product.
- quantity_signal: an object describing any inventory/stock signal for this product.
  Check BOTH the caption AND the vendor's own comment replies (provided separately
  below) - a stock signal may appear in either place. Set kind to one of:
  - "explicit_count": a specific number of units is stated (e.g. "only 3 units left",
    "we have exactly 4 units in stock").
  - "low_stock": stock is described as limited or almost gone but no exact number is
    given (e.g. "almost sold out", "going fast", "moving fast").
  - "restock": the vendor is announcing new stock arriving (e.g. "pre-order now",
    "back in stock").
  - "none": no inventory signal at all.
  Always set evidence to the exact quoted text (from the caption or a vendor comment)
  that justifies the kind, or null if kind is "none".
- negotiation_signal: one of exactly these string values:
  - "negotiable": the vendor states or implies the price can move (e.g. "slightly
    negotiable", a vendor comment confirming there's room to negotiate).
  - "fixed": the vendor explicitly refuses negotiation (e.g. "price is firm",
    "non-negotiable", "no lowballers").
  - "unknown": no signal either way - this includes plain "DM for price" posts with no
    further negotiation language either way.
- variants: if the vendor explicitly lists distinct options for this product (e.g.
  "available in Red and White"), return them as a list of objects, one per variant
  type, e.g. [{"type": "color", "values": ["Red", "White"]}]. Otherwise return [].
- images: always return [] - the pipeline populates this separately from cached
  carousel slides, do not attempt to extract or invent image URLs yourself.
- extraction_confidence: a float 0.0-1.0 for how confident you are in this product's
  extracted fields overall.

CRITICAL - never invent a price (this is a hard requirement):
If the caption says anything like "DM for price", "inbox for price", "call for price",
"send a DM/WhatsApp for the price", or otherwise directs the buyer to ask privately
instead of stating a number, you MUST set price.value to null, price.currency to null,
and price.source to "none" for that product - even if some OTHER number appears
elsewhere in the post (e.g. a comparison item's price, or a different product's price).

Only extract a number as THIS product's sale price when it's actually presented as
that product's price. Common distractors to ignore, unless the post frames them as the
actual asking price:
- Financing deposit / minimum-deposit amounts.
- Bundle or fleet-discount totals for buying multiple units.
- A swap deal's required cash balance.
- Prices quoted for a different, unrelated product mentioned only as a comparison
  (e.g. "why pay 95m for a Prado when...").

ABOUT THE "IMAGE TEXT" BLOCK (only present on some posts):
That block is raw optical character recognition output from the photo. It is
LOWER TRUST than the caption. It carries character-level errors (a zero for the
letter O, as in "IPH0NE"), it is unordered, and it is full of numbers that are
not prices - IMEIs, model codes, storage sizes, spec figures.
- Use it to identify the product and its price only where the caption and the
  vendor's comments do not already answer that.
- A price taken only from that block must have price.source set to "image".
- Never treat a number as the price unless the image text actually presents it
  as one (next to "Price", "NGN", or a currency symbol). A long bare digit run
  is an IMEI or a serial number, never a price.
- Correct obvious character errors when reading the product NAME, but never
  "correct" a price figure - report the digits exactly as they appear.

Never fabricate a name, price, or spec detail that isn't stated or clearly
shown in the post."""


class PriceModel(BaseModel):
    value: int | None = None
    currency: str | None = None
    source: Literal["caption", "image", "comments", "none"]
    confidence: float = 0.0


class QuantitySignalModel(BaseModel):
    kind: Literal["explicit_count", "low_stock", "restock", "none"]
    evidence: str | None = None


class ProductItem(BaseModel):
    name: str | None = None
    description: str | None = None
    images: list[str] = []
    variants: list[dict] = []
    price: PriceModel
    quantity_signal: QuantitySignalModel = QuantitySignalModel(kind="none")
    negotiation_signal: Literal["negotiable", "fixed", "unknown"] = "unknown"
    extraction_confidence: float = 0.0


class ProductExtractionResult(BaseModel):
    products: list[ProductItem]


def _vendor_comment_texts(post: Post, profile: dict | None) -> list[str]:
    """Same vendor-reply filtering the old regex stub used - profile might
    be a dict or the pydantic model itself, both accepted."""
    vendor_username = None
    if profile:
        vendor_username = profile.get("vendor_username") if isinstance(profile, dict) else getattr(profile, "vendor_username", None)

    if vendor_username and post.get("comments"):
        return [c.get("text", "") for c in post["comments"] if c.get("username") == vendor_username]
    return []


def _regex_fallback(post: Post, profile: dict | None = None) -> list[ProductPrediction]:
    """Original zero-cost regex heuristic - graceful-degradation path used
    when the real LLM call fails. Never attempts name extraction or
    multi-product carousels, same limits the dummy stub always had."""
    caption = post.get("caption") or ""
    vendor_comments = _vendor_comment_texts(post, profile)
    search_text = " ".join(vendor_comments) + " \n " + caption

    if DM_FOR_PRICE_RE.search(search_text):
        price_value, price_currency, price_source = None, None, "none"
    else:
        naira_match = NAIRA_RE.search(search_text)
        usd_match = USD_RE.search(search_text)

        if naira_match:
            price_value = int(naira_match.group(1).replace(",", ""))
            price_currency = "NGN"
            price_source = "comments" if NAIRA_RE.search(" ".join(vendor_comments)) else "caption"
        elif usd_match:
            price_value = int(usd_match.group(1).replace(",", ""))
            price_currency = "USD"
            price_source = "comments" if USD_RE.search(" ".join(vendor_comments)) else "caption"
        else:
            price_value, price_currency, price_source = None, None, "none"

    return [{
        "name": None,
        "description": None,
        "images": [],
        "variants": [],
        "price": {"value": price_value, "currency": price_currency, "source": price_source, "confidence": 0.0},
        "quantity_signal": {"kind": "none", "evidence": None},
        "negotiation_signal": "unknown",
        "extraction_confidence": 0.4,
    }]


def _build_user_prompt(
    post: Post,
    profile: dict | None,
    vendor_comments: list[str],
    ocr_text: str | None = None,
) -> str:
    """The Pass A prompt, optionally carrying an OCR block.

    The OCR text goes in its OWN labelled block and never into the caption -
    that is the whole point of the shape. stage2_triage_hybrid.py can afford to
    substitute OCR text for the caption because its question is "is this a
    product?", where a garbled read is a cheap error. Here the model must be
    able to tell a caption the vendor wrote from text a recogniser guessed at,
    because one of them is trustworthy about a price and the other is not.
    """
    caption = post.get("caption") or ""
    profile_context = f"Account profile: {profile}" if profile else "Account profile: unavailable"
    comments_block = "\n".join(vendor_comments) if vendor_comments else "(none)"
    prompt = (
        f"{profile_context}\n\n"
        f"Caption:\n{caption}\n\n"
        f"Vendor's own comment replies (if any):\n{comments_block}"
    )
    if ocr_text:
        prompt += (
            "\n\nIMAGE TEXT (read off the photo by OCR - may contain character "
            "errors, unordered lines, and numbers that are not prices):\n"
            f"{ocr_text}"
        )
    return prompt


def _needs_escalation(result: ProductExtractionResult) -> bool:
    """True if any extracted product is missing a name, has no price at all
    (source == "none" - also covers "DM for price" captions, which we still
    want to double-check against the image), or fell below the confidence
    bar."""
    return any(
        item.name is None
        or item.price.source == "none"
        or item.extraction_confidence < ESCALATION_CONFIDENCE_THRESHOLD
        for item in result.products
    )


def _pass_a(
    user_prompt: str,
    text_model: str,
    run_id: str | None,
    vendor_id: str | None,
    post_id: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> ProductExtractionResult:
    """Cheap text-only pass."""
    return complete_structured(
        model=text_model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema=ProductExtractionResult,
        stage_name="stage3_extract_pass_a",
        run_id=run_id,
        vendor_id=vendor_id,
        post_id=post_id,
        escalated=False,
        max_tokens=max_tokens,
    )


def _pass_a_ocr(
    user_prompt: str,
    text_model: str,
    run_id: str | None,
    vendor_id: str | None,
    post_id: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> ProductExtractionResult:
    """Text pass over a prompt that carries an OCR block.

    Same model and same schema as _pass_a - only the prompt differs. It gets its
    own token-log stage name so the tier's reach and cost are separable from the
    plain text pass, mirroring how stage2_triage_hybrid.py splits its Gemini
    text and vision stage names. Without that split the OCR tier is invisible in
    report/token_log.csv, which is the only artifact that records what actually
    reached a model.
    """
    return complete_structured(
        model=text_model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema=ProductExtractionResult,
        stage_name="stage3_extract_pass_a_ocr",
        run_id=run_id,
        vendor_id=vendor_id,
        post_id=post_id,
        escalated=True,
        max_tokens=max_tokens,
    )


def _pass_b(
    post: Post,
    user_prompt: str,
    vision_model: str,
    run_id: str | None,
    vendor_id: str | None,
    post_id: str,
) -> ProductExtractionResult:
    """Vision escalation. NOTE: complete_structured() currently only sends
    text messages - this constructs a multimodal message directly via
    litellm's OpenAI-compatible image_url format, mirroring
    stage2_triage.py's _pass_b (the one other call site that needs vision).
    Since this bypasses complete_structured(), it logs token usage itself
    instead of getting it for free."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_prompt + PASS_B_SUFFIX},
                {"type": "image_url", "image_url": {"url": vision_image_url(post)}},
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
        metadata={"run_name": "stage3_extract_pass_b", "tags": ["stage3_extract_pass_b"]},
    )
    parsed = json.loads(response.choices[0].message.content)
    result = ProductExtractionResult.model_validate(parsed)

    if run_id is not None:
        usage = getattr(response, "usage", None)
        log_token_usage(
            run_id=run_id,
            vendor_id=vendor_id or "unknown",
            post_id=post_id,
            stage="stage3_extract_pass_b",
            model=vision_model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage, "total_tokens", None),
            escalated=True,
        )

    return result


def _to_predictions(result: ProductExtractionResult) -> list[ProductPrediction]:
    return [
        {
            "name": item.name,
            "description": item.description,
            "images": item.images,
            "variants": item.variants,
            "price": item.price.model_dump(),
            "quantity_signal": item.quantity_signal.model_dump(),
            "negotiation_signal": item.negotiation_signal,
            "extraction_confidence": item.extraction_confidence,
        }
        for item in result.products
    ]


def extract_product(
    post: Post,
    profile: dict | None = None,
    text_model: str = MODEL,
    vision_model: str = MODEL,
    run_id: str | None = None,
    vendor_id: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    use_ocr: bool = False,
    ocr_min_chars: int = DEFAULT_MIN_CHARS,
) -> list[ProductPrediction]:
    """Extracts one or more products from a post via a three-tier cascade.

        caption uninformative + use_ocr  ->  OCR first, then ONE text pass
                                             carrying the IMAGE TEXT block
        caption present                  ->  Pass A (text)
                                             +-- unfinished --> OCR, text pass again
                                                 +-- still unfinished --> vision Pass B

    WHY OCR SITS HERE. Stage 2's cascade already drove its own vision spend to
    zero by inserting local OCR ahead of the vision model, which left this stage
    holding every remaining vision call in the project - 214 of them, 67% of all
    vision cost, while Stage 3 overall is 74% of all token spend. pipeline/ocr.py
    was already caching the text for exactly these posts and nothing read it.

    WHY IT IS NOT WIRED THE WAY STAGE 2 WIRES IT. Stage 2 substitutes OCR text
    for the caption, which it can afford because its question is only whether a
    post is a product. This stage reads the PRICE. OCR text therefore arrives in
    its own labelled block (see _build_user_prompt) and any price attributed to
    the image must clear _guard_ocr_prices before it is allowed to stand.

    Vision is NOT removed. Every failure path in pipeline/ocr.py returns None,
    and None still means escalate to vision, exactly as before OCR existed.

    Args:
        post: Post dict with "caption" and optionally "comments"/"media_url".
        profile: Stage 1's AccountProfile, as a dict or the pydantic model
            itself (both are accepted here, see _vendor_comment_texts).
        text_model: litellm model string for Pass A and for the OCR text pass.
        vision_model: litellm model string for Pass B.
        run_id: Groups this call's report/token_log.csv rows with the rest of
            one eval/harness.py run - token usage is only logged when set.
        vendor_id: Account label, for the token log's "vendor_id" column. Also
            the account the OCR cache is keyed under.
        use_ocr: Enables the OCR tier. Defaults to False IN CODE so every
            experiment YAML written before this tier existed reproduces its
            recorded run unchanged; stage3_ocr.yaml is what turns it on.
        ocr_min_chars: Below this many recognised characters the read counts as
            failed and the cascade escalates to vision as it did before.

    Returns:
        A list of ProductPrediction, one per distinct product the model found.
        Falls back to _regex_fallback() (always a single product) if any LLM
        call in the cascade fails for any reason.
    """
    vendor_comments = _vendor_comment_texts(post, profile)
    post_id = post.get("post_id") or post.get("id") or ""
    caption = post.get("caption") or ""
    image_url = vision_image_url(post)

    # "DM for price" (or "inbox/call for price") means the vendor has confirmed,
    # in their own words, that no price is stated anywhere - not that Pass A
    # merely failed to find one. The price is not sitting on the photo either in
    # that case, so neither OCR nor vision has anything left to find and both
    # would only re-ask an already-answered question.
    search_text = " ".join(vendor_comments) + " \n " + caption
    confirmed_no_price = bool(DM_FOR_PRICE_RE.search(search_text))
    ocr_allowed = use_ocr and bool(image_url) and not confirmed_no_price

    ocr_text: str | None = None
    ocr_attempted = False

    def _read_image_text() -> str | None:
        nonlocal ocr_attempted
        ocr_attempted = True
        text, reason = extract_text(
            image_url,
            post_id=post_id,
            account_label=vendor_id,
            min_chars=ocr_min_chars,
        )
        logger.info("[stage3_extract] %s: OCR %s", post_id, reason)
        return text

    try:
        # Tier 0. With no caption to read, Pass A would spend a call on an empty
        # prompt and escalate regardless, so OCR goes first and its text rides
        # along on that one call rather than costing an extra one.
        if ocr_allowed and _is_uninformative_caption(caption):
            ocr_text = _read_image_text()

        user_prompt = _build_user_prompt(post, profile, vendor_comments, ocr_text)
        if ocr_text:
            result = _pass_a_ocr(user_prompt, text_model, run_id, vendor_id, post_id, max_tokens)
        else:
            result = _pass_a(user_prompt, text_model, run_id, vendor_id, post_id, max_tokens)

        needs_escalation = _needs_escalation(result)
        if ocr_text and _guard_ocr_prices(result, ocr_text):
            needs_escalation = True

        # Tier 1. A captioned post Pass A could not finish. A price printed on
        # the photo is one of the three things _needs_escalation fires on, and
        # it is reachable without paying for vision.
        if needs_escalation and ocr_allowed and not ocr_attempted:
            ocr_text = _read_image_text()
            if ocr_text:
                logger.info("[stage3_extract] %s: retrying text pass with OCR", post_id)
                user_prompt = _build_user_prompt(post, profile, vendor_comments, ocr_text)
                result = _pass_a_ocr(
                    user_prompt, text_model, run_id, vendor_id, post_id, max_tokens
                )
                needs_escalation = _needs_escalation(result)
                if _guard_ocr_prices(result, ocr_text):
                    needs_escalation = True

        # Tier 2. Unchanged, and still the only paid escalation - now reached
        # only when the free tiers could not answer.
        if needs_escalation and image_url and not confirmed_no_price:
            logger.info("[stage3_extract] %s: escalating to vision Pass B", post_id)
            result = _pass_b(post, user_prompt, vision_model, run_id, vendor_id, post_id)
    except Exception as exc:
        logger.warning(
            "[stage3_extract] LLM extraction failed (%s: %s) - falling back to regex heuristic",
            type(exc).__name__, exc,
        )
        # Record the degradation in the token log. Without this the fallback is
        # invisible to every downstream measurement: no row is written, so a
        # run that quietly regex-extracted several posts is indistinguishable
        # in report/token_log.csv from one where those posts never existed.
        # Zero tokens because no call succeeded - fallback_used is the signal,
        # and this is the first caller to set it.
        if run_id and vendor_id:
            log_token_usage(
                run_id=run_id,
                vendor_id=vendor_id,
                post_id=post_id,
                stage="stage3_extract_fallback",
                model=text_model,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                fallback_used=True,
            )
        return _regex_fallback(post, profile)

    return _to_predictions(result)
