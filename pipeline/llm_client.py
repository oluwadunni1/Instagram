"""
Thin LLM wrapper used by all pipeline stages.

complete_structured() is the single call-site for every LLM interaction in
the pipeline.  It:
  - Sends a system + user prompt via litellm (supports any provider/model string)
  - Expects the model to return a JSON object
  - Parses + validates that JSON into the caller-supplied Pydantic schema
  - Retries with a correction message if the model returns invalid JSON or
    a schema mismatch (brief section 3: "reject and retry on invalid JSON") -
    raises only after retries are exhausted, so a stage knows extraction
    genuinely failed rather than swallowing a bad response silently
  - Retries with exponential backoff on transient provider errors (429
    rate-limits and 5xx overloads) - free-tier models share pools that get
    temporarily overloaded; crashing the whole eval run on the first 429
    is the wrong behaviour for experiments
  - Logs prompt/completion token usage to report/token_log.csv (see
    log_token_usage()) whenever the caller passes a run_id - opt-in per
    call so a one-off script or REPL probe doesn't pollute the log

Logging goes to stdout so `uv run` output is easy to follow.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Type, TypeVar

import httpx
import litellm
from pydantic import BaseModel, ValidationError

import pipeline.settings  # noqa: F401 - import side effect: load_dotenv() before the env reads below

# Suppress litellm's noisy "Provider List: https://..." startup banners and
# debug output - these add nothing to eval runs and drown real log lines.
litellm.suppress_debug_info = True
litellm.set_verbose = False

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_OBJ_RE = re.compile(r"(\{.*\})", re.DOTALL)


# Gemini's free tier allows 15 requests/minute/model - one call every 4 seconds.
# Nothing here paced the calls, so a run burst straight through that ceiling: the
# transient-retry loop below absorbed the resulting 429s until Stage 3 exhausted
# its budget and dropped to _regex_fallback(), leaving the run reporting clean
# scores over partly-heuristic output. Observed 2026-09-11 on a 20-call, 10-post
# run, which lost one post that way.
#
# Spacing the calls costs roughly the wall-clock the backoff was burning anyway,
# without the failures. 4.5s rather than the bare 60/15 = 4.0s: exactly at the
# limit leaves no margin for however the provider bounds its window, and the
# difference over a whole run is a few seconds. Set LLM_MIN_CALL_INTERVAL=0 to
# disable on a paid tier.
DEFAULT_CALL_INTERVAL_SECONDS = 4.5


def _read_call_interval() -> float:
    """LLM_MIN_CALL_INTERVAL as a float, treating unset-or-blank as the default.

    The blank case is the one that matters: .env.example ships the key with an
    empty value, so a copied .env yields "" and a bare float("") would raise at
    import time and take the whole pipeline down. A malformed non-empty value
    still raises - a typo'd interval should be loud, not silently ignored.
    """
    raw = (os.environ.get("LLM_MIN_CALL_INTERVAL") or "").strip()
    if not raw:
        return DEFAULT_CALL_INTERVAL_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        raise ValueError(
            f"LLM_MIN_CALL_INTERVAL must be a number of seconds (got {raw!r}). "
            "Leave it blank for the default, or set 0 to disable throttling."
        ) from None


MIN_CALL_INTERVAL_SECONDS = _read_call_interval()

_last_call_started_at = 0.0
_throttle_lock = threading.Lock()


def throttle() -> None:
    """Blocks until MIN_CALL_INTERVAL_SECONDS have passed since the last call.

    Must be called before EVERY request that reaches a provider, including the
    vision Pass B calls in stage2_triage.py/stage3_extract.py that bypass
    complete_structured() - they spend the same per-minute quota, so throttling
    only this module's calls would still let a cascade trip the limit.

    Spacing is measured from one call's start to the next, which is what a
    requests-per-minute quota counts; a call that itself takes 3s therefore only
    waits 1s afterwards.
    """
    global _last_call_started_at
    if MIN_CALL_INTERVAL_SECONDS <= 0:
        return
    with _throttle_lock:
        wait = MIN_CALL_INTERVAL_SECONDS - (time.monotonic() - _last_call_started_at)
        if wait > 0:
            time.sleep(wait)
        _last_call_started_at = time.monotonic()


def _extract_json(text: str) -> dict:
    """Best-effort JSON extraction from model output.

    Models often wrap JSON in markdown fences or precede it with prose even
    when told to return ONLY JSON. Try the most-specific extractions first
    and fall back to broader ones before giving up.
    """
    # 1. Direct parse - most models that follow instructions land here
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Markdown code fence: ```json { ... } ```
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 3. Bare object anywhere in the response (catches "Sure! Here you go: {...}")
    m = _BARE_OBJ_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    raise json.JSONDecodeError(f"No valid JSON found in response", text, 0)


# Exceptions that indicate a transient provider problem - safe to retry with
# backoff. Distinguished from content errors (bad JSON / schema mismatch)
# which get a different retry path (re-prompt with correction message).
_TRANSIENT_EXCEPTIONS = (
    litellm.exceptions.RateLimitError,        # 429 - upstream rate limit
    litellm.exceptions.APIError,              # 5xx - provider overloaded / temp error
    litellm.exceptions.ServiceUnavailableError,  # 503 explicit
    litellm.exceptions.Timeout,               # network / gateway timeout
    httpx.ReadError,                          # server disconnect mid-stream
    httpx.ConnectError,                       # connection refused / DNS
    httpx.RemoteProtocolError,               # server sent bad HTTP
)

# Provider errors that arrive INSIDE a _TRANSIENT_EXCEPTIONS type but are
# permanent, so retrying only wastes wall-clock time. litellm raises
# OpenRouter's 402 as a generic APIError, which the tuple above classifies as
# transient - so an exhausted balance burned 6 retries with exponential
# backoff (2+4+8+16+32+64 = ~2 minutes) PER POST before failing. On a 41-post
# run that is over an hour to learn what one balance check answers instantly.
# Hit three times on 2026-09-09; see README.md.
#
# Matched on message text because the provider's HTTP status is not reliably
# surfaced on the exception - deliberately narrow, and it only ever converts a
# slow failure into a fast one with the same outcome.
_PERMANENT_ERROR_MARKERS = (
    "insufficient credits",
    "requires more credits",
    "never purchased credits",
    "402",
)


def _is_permanent_provider_error(exc: Exception) -> bool:
    """True for a provider error that no amount of waiting will fix."""
    text = str(exc).lower()
    return any(marker in text for marker in _PERMANENT_ERROR_MARKERS)


# ---------------------------------------------------------------------------
# Real cost, per row.
#
# estimated_cost_usd was hardcoded 0.0 from the start, on the reasoning that
# everything was running on a free tier. The consequence was that every "Cost:
# $X" this project printed was cost_per_call_usd from a YAML times a call
# count - a config's OPINION of what a call costs, not what the call cost - and
# every real figure in README.md had to be worked out by hand from token counts.
#
# Rates are $ per 1,000,000 tokens, (prompt, completion).
#
# A model that is not in this table is priced 0.0 and WARNED about, never
# guessed at. A silently-guessed rate is worse than no rate: it produces a
# number that looks like evidence and is not, which is the failure this whole
# column already had once.
#
# HISTORY IS MIXED, and every reader of the column has to know it: rows written
# before this landed are 0.0 regardless of what they actually cost. Old rows
# are deliberately NOT recomputed in place - the log is append-only evidence,
# and rewriting history destroys the one property that makes it the record.
# read_run_usage() therefore reports priced_calls alongside calls so a caller
# can say how much of a total is real.
MODEL_RATES: dict[str, tuple[float, float]] = {
    # Jev's published input rate, with free output. Source: scripts/smoke_jev.py,
    # which has been quoting it since the model was introduced.
    "jev-1.13.0": (0.042, 0.0),
    "jev-latest": (0.042, 0.0),
}

# Models seen in the log with no rate on file. Kept separate from MODEL_RATES so
# the gap is visible rather than implied by absence, and so filling one in is a
# single-line edit against a named source.
#
# UNPRICED, needs a rate from the provider's own page before any cost figure
# that includes these rows can be quoted:
#   gemini/gemini-3.5-flash-lite  - README records the COMPLETION rate as
#       $2.50/M, but the prompt rate has never been written down anywhere in
#       this repo, and the per-post figures in README.md were derived without
#       recording it. Half a rate cannot price a row.
#   gemini/gemini-3.5-flash       - same.
#   the GPT-4o Mini / Llama / Qwen strings from the model comparison.
#   gemini-embedding-001          - Stage 6's caption embeddings
#       (pipeline/media_fingerprint.py). Google prices embeddings per input
#       token on a separate sheet from the generative models, and embedContent
#       returns no usage block, so these rows carry 0 tokens: the row proves the
#       call happened and nothing more. A Stage 6 cost figure needs the call
#       COUNT against the published embedding rate, not this column.

_warned_unpriced: set[str] = set()


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Dollar cost of one call, or 0.0 when the model has no rate on file.

    Warns once per unknown model per process. Once, because a harness run would
    otherwise emit one warning per post and the signal would be lost in it.
    """
    rate = MODEL_RATES.get(model)
    if rate is None:
        if model not in _warned_unpriced:
            _warned_unpriced.add(model)
            logger.warning(
                "[token_log] no rate on file for model %r - its rows are priced 0.0 and "
                "any cost total including them understates. Add it to MODEL_RATES.",
                model,
            )
        return 0.0
    prompt_rate, completion_rate = rate
    return (prompt_tokens * prompt_rate + completion_tokens * completion_rate) / 1_000_000


TOKEN_LOG_PATH = Path("report") / "token_log.csv"
TOKEN_LOG_FIELDNAMES = [
    "run_id",
    "timestamp",
    "vendor_id",
    "post_id",
    "stage",
    "model",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "escalated",
    "fallback_used",
    "estimated_cost_usd",
]


def log_token_usage(
    *,
    run_id: str,
    vendor_id: str,
    post_id: str,
    stage: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int | None = None,
    escalated: bool = False,
    fallback_used: bool = False,
) -> None:
    """Append one row to report/token_log.csv, writing the header first if the file doesn't exist yet.

    estimated_cost_usd is computed from MODEL_RATES. A model with no rate on
    file is priced 0.0 and warned about - see the note above MODEL_RATES for
    why an unknown rate is never guessed, and why rows written before that
    landed are left at 0.0 rather than recomputed.
    """
    TOKEN_LOG_PATH.parent.mkdir(exist_ok=True)
    write_header = not TOKEN_LOG_PATH.exists()

    row = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "vendor_id": vendor_id,
        "post_id": post_id,
        "stage": stage,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens if total_tokens is not None else prompt_tokens + completion_tokens,
        "escalated": escalated,
        "fallback_used": fallback_used,
        "estimated_cost_usd": estimate_cost_usd(model, prompt_tokens, completion_tokens),
    }

    with TOKEN_LOG_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TOKEN_LOG_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def read_run_usage(run_id: str) -> dict:
    """Per-stage call counts and token totals for one run, read back from
    report/token_log.csv.

    Deliberately not accumulated in memory as we go: the log is what actually
    reached litellm, so a stage that silently fell back to a different model,
    or never called one at all, shows up here and cannot be papered over by
    the runner's own bookkeeping.

    Lives next to log_token_usage() (its writer) and TOKEN_LOG_PATH so the
    reader and writer of the log share one definition of its shape. Both
    scripts/run_pipeline.py and scripts/verify_run.py read through this.
    """
    empty = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
             "fallbacks": 0, "cost_usd": 0.0, "priced_calls": 0, "by_stage": {}, "models": []}
    if not TOKEN_LOG_PATH.exists():
        return empty

    by_stage: dict[str, dict] = {}
    models: Counter = Counter()
    totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
              "fallbacks": 0}
    # Tracked apart from the token totals because cost is only as trustworthy as
    # its coverage: rows predating MODEL_RATES, and rows for a model with no rate
    # on file, both carry 0.0. priced_calls < calls means the total understates,
    # and a caller quoting the figure has to say so.
    cost_usd = 0.0
    priced_calls = 0

    with TOKEN_LOG_PATH.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("run_id") != run_id:
                continue

            def _int(key: str) -> int:
                try:
                    return int(row.get(key) or 0)
                except ValueError:
                    return 0

            stage = row.get("stage") or "unknown"
            entry = by_stage.setdefault(
                stage,
                {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                 "total_tokens": 0, "cost_usd": 0.0},
            )
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                entry[key] += _int(key)
                totals[key] += _int(key)
            entry["calls"] += 1
            totals["calls"] += 1

            try:
                row_cost = float(row.get("estimated_cost_usd") or 0.0)
            except ValueError:
                row_cost = 0.0
            entry["cost_usd"] += row_cost
            cost_usd += row_cost
            if row_cost > 0.0:
                priced_calls += 1
            if (row.get("fallback_used") or "").strip().lower() == "true":
                totals["fallbacks"] += 1
            models[row.get("model") or "unknown"] += 1

    return {**totals, "cost_usd": cost_usd, "priced_calls": priced_calls,
            "by_stage": by_stage, "models": sorted(models)}


def complete_structured(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    schema: Type[T],
    stage_name: str = "unknown",
    temperature: float = 0.0,
    max_tokens: int = 1024,
    max_retries: int = 2,
    max_transient_retries: int = 6,
    run_id: str | None = None,
    vendor_id: str | None = None,
    post_id: str | None = None,
    escalated: bool = False,
    fallback_used: bool = False,
) -> T:
    """Call `model` and parse the response JSON into `schema`.

    Args:
        model:                 litellm model string, e.g. "openai/gpt-4o-mini"
                               or "openrouter/google/gemini-flash-1.5-8b".
        system_prompt:         System message text.
        user_prompt:           User message text.
        schema:                Pydantic BaseModel class to validate the response.
        stage_name:            Label used in log/error messages and the token log's
                               "stage" column.
        temperature:           Sampling temperature (default 0.0 for determinism).
        max_tokens:            Max tokens for the response.
        max_retries:           Extra attempts if the response is bad JSON or
                               fails schema validation. 0 = single-shot.
        max_transient_retries: Max retries for 429 / 5xx provider errors.
                               Backoff doubles each time: 2, 4, 8 ... seconds.
        run_id:                Groups every log_token_usage() row from one
                               experiment run together. Token usage is only
                               logged when this is set - omit it (the
                               default) for a one-off script or REPL probe
                               you don't want polluting report/token_log.csv.
        vendor_id:             Account label, for the token log's "vendor_id"
                               column. Only used when run_id is set.
        post_id:               Post id, for the token log's "post_id" column
                               - pass "" for an account-level call (e.g.
                               Stage 1) that isn't about one specific post.
                               Only used when run_id is set.
        escalated:             Whether this call is a cascade's vision/Pass B
                               escalation, for the token log's "escalated"
                               column. Only used when run_id is set.
        fallback_used:         Whether this call is a retry after a prior
                               fallback, for the token log's "fallback_used"
                               column. Only used when run_id is set.

    Returns:
        A validated instance of `schema`.

    Raises:
        RuntimeError: If every attempt (including retries) fails.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    last_content_error: Exception | None = None
    transient_attempts = 0

    for attempt in range(1, max_retries + 2):
        logger.info("[%s] Calling model: %s (attempt %d)", stage_name, model, attempt)

        # --- API call with transient-error retry loop ---
        response = None
        while True:
            try:
                throttle()
                response = litellm.completion(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                    metadata={"run_name": stage_name, "tags": [stage_name, f"attempt_{attempt}"]},
                )
                break  # success - exit transient retry loop
            except _TRANSIENT_EXCEPTIONS as exc:
                # Fail fast on a permanent error wearing a transient error's
                # type - an exhausted balance is not a blip, and backing off
                # from it just hides the cause behind minutes of waiting.
                if _is_permanent_provider_error(exc):
                    raise RuntimeError(
                        f"[{stage_name}] Permanent provider error on {model} - not retrying. "
                        f"Check the account balance for the key in use. Last: {exc}"
                    ) from exc
                transient_attempts += 1
                if transient_attempts > max_transient_retries:
                    raise RuntimeError(
                        f"[{stage_name}] Provider error on {model} after "
                        f"{max_transient_retries} transient retries. Last: {exc}"
                    ) from exc
                wait = 2 ** transient_attempts  # 2, 4, 8, 16, 32, 64s
                logger.warning(
                    "[%s] Transient provider error (%s) - waiting %ds (retry %d/%d)",
                    stage_name, type(exc).__name__, wait,
                    transient_attempts, max_transient_retries,
                )
                time.sleep(wait)
                # continue inner loop - don't advance content-attempt counter

        raw_text: str = response.choices[0].message.content or ""
        logger.debug("[%s] Raw response:\n%s", stage_name, raw_text)

        # --- Parse and validate JSON ---
        try:
            parsed = _extract_json(raw_text)
            result = schema.model_validate(parsed)
        except json.JSONDecodeError as exc:
            last_content_error = exc
            error_detail = f"non-JSON response: {raw_text!r}"
        except ValidationError as exc:
            last_content_error = exc
            error_detail = f"schema mismatch: {exc}"
        else:
            if run_id is not None:
                usage = getattr(response, "usage", None)
                log_token_usage(
                    run_id=run_id,
                    vendor_id=vendor_id or "unknown",
                    post_id=post_id or "",
                    stage=stage_name,
                    model=model,
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    total_tokens=getattr(usage, "total_tokens", None),
                    escalated=escalated,
                    fallback_used=fallback_used,
                )
            return result

        if attempt <= max_retries:
            logger.warning("[%s] Attempt %d failed (%s) - retrying", stage_name, attempt, error_detail)
            # Get the required field names directly from the schema so the
            # correction message is specific - a model that returned {} needs
            # to know exactly which fields it missed, not just "try again".
            required_fields = list(schema.model_fields.keys())
            messages.append({"role": "assistant", "content": raw_text})
            messages.append({
                "role": "user",
                "content": (
                    f"Your response was invalid: {error_detail}. "
                    f"You MUST return a JSON object with these exact fields: "
                    f"{required_fields}. "
                    f"Return ONLY the JSON object, no explanation, no markdown."
                ),
            })

    raise RuntimeError(
        f"[{stage_name}] Failed to get valid structured output from {model} after "
        f"{max_retries + 1} attempt(s). Last error: {last_content_error}"
    )