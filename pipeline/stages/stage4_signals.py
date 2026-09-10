"""
Stage 4 - Business signal detection, two-layer cost-control architecture.

Layer 1 (free, regex prefilter): scans caption + every comment for candidate
signal keywords. If nothing matches, returns [] immediately - zero LLM
calls on the majority of posts, which say nothing about stock, price
flexibility, financing, swaps, or sale status at all.

Layer 2 (LLM, only reached on a Layer 1 hit): reads the caption plus the
full comment thread, each comment labeled [BUYER] or [VENDOR] (matched
against profile's vendor_username, same duck-typed profile handling
stage3_extract.py's _vendor_comment_texts uses - a dict or the Pydantic
AccountProfile itself), plus the post's age in days. Returns only signals
with a direct textual quote backing them - no guessing.

Unlike stage2_triage.py/stage3_extract.py, there is no vision escalation
here - every signal this stage looks for lives in text (caption/comments),
not in the photo.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel

from pipeline.llm_client import complete_structured
from pipeline.types import Post

logger = logging.getLogger(__name__)

MODEL = "gemini/gemini-3.5-flash-lite"

# NOTE ON \b: both word boundaries are load-bearing - the leading one stops "sold"
# matching "resold", the trailing one stops it matching "soldier". The original bug
# was not the boundaries themselves but pairing them with STEM-PREFIXES: \bfinanc\b
# can never match "financing", \bnegotiat\b can never match "negotiable", \bbank\b
# misses "banks", \bslash\b misses "slashed". Those were dead alternatives that
# could not fire on any real word, and the signals they gate reached the LLM only by
# accident, via unrelated tokens like "still available" (FINDINGS.md 2026-09-07).
# The fix keeps both boundaries and adds \w* to the stems that need inflection -
# \bfinanc\w*\b matches "financing" because \w* consumes the suffix before the
# trailing boundary applies.
PREFILTER_RE = re.compile(
    r"\b("
    r"sold|gone|out of stock|sold out|"
    r"still available|still up|"
    r"units? left|in stock|units? (?:landed|available)|only \d+ units?|exactly \d+ units?|"
    r"restock\w*|back in stock|pre.?order\w*|"
    r"negotia\w*|flexib\w*|room to|serious buyer\w*|"
    r"financ\w*|bank\w*|instal?lment\w*|deposit\w*|spread the|pay later|part payment|"
    r"swap\w*|exchang\w*|"
    r"clearanc\w*|price drop\w*|slash\w*|"
    r"going fast|hurry\w*|limit\w*|last (?:unit|one|chance|few)|first come"
    r")\b",
    re.IGNORECASE,
)


class Signal(BaseModel):
    signal: Literal[
        "sold",
        "urgent",
        "price_negotiable",
        "finance_available",
        "swap_deal",
        "clearance",
        "stock_count_known",
    ]
    confidence: float
    evidence: str  # exact quote from caption or comment that triggered this


class SignalExtractionResult(BaseModel):
    signals: list[Signal]


SYSTEM_PROMPT = """You are analyzing a single Instagram post (caption + full comment thread) for a
vendor account, to detect business signals useful to a buyer or deal-hunting agent. These are
interpretations of context, not extracted product facts (that's Stage 3).

Comments are labeled [BUYER] or [VENDOR] so you know who said what - only the vendor's own
words carry authority for signals that require the vendor's confirmation (sold, finance
offered, swap accepted, stock count). A buyer's guess or question is never itself evidence
for a signal, even if it turns out to be right.

Return ONLY a JSON object with exactly one field:
- signals: a JSON array of objects, one per business signal you detect. If none of the
  allowed signals are clearly evidenced, return an empty array. Do NOT guess, and do NOT
  invent a signal that isn't in the allowed list below.

Each element of `signals` must have exactly these fields:
- signal: one of exactly these string values - sold, urgent, price_negotiable,
  finance_available, swap_deal, clearance, stock_count_known. Never return any other value.
- confidence: a float 0.0-1.0 for how confident you are this signal is actually present.
- evidence: the exact quoted text (from the caption or one comment) that justifies this
  signal. Never paraphrase - quote it verbatim. If you can't quote specific text backing a
  signal, don't return that signal.

Allowed signals and what counts as evidence:
- sold: the VENDOR explicitly confirms the unit is sold/gone/no longer available (e.g.
  "sold", "gone", "no longer available"). A buyer asking "is this sold?" is not evidence.
- urgent: genuine scarcity or time pressure - a specific limited quantity, "going fast",
  "last one", "first come first served". General sales enthusiasm ("amazing deal!") is not
  urgency.
- price_negotiable: the VENDOR states or implies the price can move ("room to negotiate",
  "flexible for serious buyers", "slightly negotiable"). A buyer asking to negotiate is not
  evidence on its own - the vendor must respond affirmatively, or the caption itself must
  say it.
- finance_available: financing or a bank partnership mentioned BY THE VENDOR (e.g. "we work
  with partner banks", "financing available", "pay a deposit and spread the rest").
- swap_deal: a swap/exchange explicitly offered in the caption, or confirmed/accepted by the
  VENDOR in a comment. A buyer asking about a swap that the vendor then declines is NOT this
  signal.
- clearance: clearance-sale or price-slash language (e.g. "clearance", "price drop",
  "massive price drop", "was X now Y").
- stock_count_known: the VENDOR states or confirms a specific numeric count of units, either
  in the caption or in a vendor comment reply (e.g. "exactly 4 units", "only 2 landed", "we
  have just 1 unit in this exact spec"). A buyer's own guess at a count, even if never
  contradicted, is NOT evidence - only a vendor-stated number counts.

Use the post's age (given below the comments) as context - a scarcity or urgency claim from
weeks ago may no longer reflect current availability, but still report it if the text itself
clearly states it; you are reporting what the post/thread says, not verifying it's still true."""


def _vendor_username(profile: dict | None) -> str | None:
    """Same duck-typed profile handling stage3_extract.py's
    _vendor_comment_texts uses - profile might be a dict or the Pydantic
    AccountProfile itself."""
    if not profile:
        return None
    return profile.get("vendor_username") if isinstance(profile, dict) else getattr(profile, "vendor_username", None)


def _label_comments(post: Post, profile: dict | None) -> list[str]:
    """Every comment on the post, prefixed [BUYER] or [VENDOR] - unlike
    stage3_extract.py's vendor-only filtering, Stage 4 needs the full
    thread (e.g. stock_count_known depends on seeing a buyer's question
    next to the vendor's reply)."""
    vendor_username = _vendor_username(profile)
    labeled = []
    for comment in post.get("comments", []):
        role = "VENDOR" if vendor_username and comment.get("username") == vendor_username else "BUYER"
        labeled.append(f"[{role}] {comment.get('text', '')}")
    return labeled


def _post_age_days(post: Post) -> int | None:
    """Days between the post's timestamp and now, or None if the post has
    no parseable timestamp."""
    timestamp = post.get("timestamp")
    if not timestamp:
        return None
    try:
        posted_at = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - posted_at).days


def _build_user_prompt(post: Post, profile: dict | None) -> str:
    caption = post.get("caption") or ""
    comments_block = "\n".join(_label_comments(post, profile)) or "(no comments)"
    age_days = _post_age_days(post)
    age_line = f"{age_days} days ago" if age_days is not None else "unknown"
    return (
        f"Caption:\n{caption}\n\n"
        f"Comments (labeled by role):\n{comments_block}\n\n"
        f"Post age: {age_line}"
    )


def detect_signals(
    post: Post,
    profile: dict | None = None,
    signals_model: str = MODEL,
    run_id: str | None = None,
    vendor_id: str | None = None,
) -> list[dict]:
    """Detects business signals for one post via a regex prefilter -> LLM
    cascade.

    Args:
        post: Post dict with "caption" and optionally "comments"/"timestamp".
        profile: Stage 1's AccountProfile, as a dict or the pydantic model
            itself (both are accepted here, see _vendor_username). Used to
            label comments [VENDOR] vs [BUYER].
        signals_model: litellm model string for the LLM extraction call.
            Named `signals_model`, NOT `model`, deliberately: `model` is a
            reserved key in the experiment YAMLs (a human-readable label used
            for reporting and prediction filenames, e.g. "Gemini Cascade
            (Google AI Studio)") and is stripped by
            eval/harness.py::load_stage_fn() before kwargs are bound. A
            parameter called `model` therefore silently never receives its
            configured value - see FINDINGS.md 2026-09-07.
        run_id: Groups this call's report/token_log.csv row with the rest of
            one eval/harness.py run - token usage is only logged when this
            is set (see log_token_usage() in pipeline/llm_client.py).
        vendor_id: Account label, for the token log's "vendor_id" column.

    Returns:
        A list of dicts matching the golden set's expected_signals shape:
        [{"signal": "sold", "confidence": 0.95, "evidence": "..."}]. Returns
        [] immediately, with zero LLM calls, if the Layer 1 regex prefilter
        finds no candidate keywords anywhere in the caption or comments.
    """
    caption = post.get("caption") or ""
    comments_text = " ".join(comment.get("text", "") for comment in post.get("comments", []))
    search_text = f"{caption} {comments_text}"

    if not PREFILTER_RE.search(search_text):
        logger.debug(
            "[stage4_signals] %s: prefilter found no candidate keywords - short-circuiting, no LLM call",
            post.get("post_id") or post.get("id"),
        )
        return []

    post_id = post.get("post_id") or post.get("id") or ""
    user_prompt = _build_user_prompt(post, profile)

    result = complete_structured(
        model=signals_model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema=SignalExtractionResult,
        stage_name="stage4_signals",
        run_id=run_id,
        vendor_id=vendor_id,
        post_id=post_id,
    )

    return [signal.model_dump() for signal in result.signals]
