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
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Type, TypeVar

import httpx
import litellm
from pydantic import BaseModel, ValidationError

# Suppress litellm's noisy "Provider List: https://..." startup banners and
# debug output - these add nothing to eval runs and drown real log lines.
litellm.suppress_debug_info = True
litellm.set_verbose = False

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_OBJ_RE = re.compile(r"(\{.*\})", re.DOTALL)


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

    estimated_cost_usd is hardcoded to 0.0 - every model we're running is
    currently on a free tier, so real cost tracking isn't wired up yet.
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
        "estimated_cost_usd": 0.0,
    }

    with TOKEN_LOG_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TOKEN_LOG_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


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