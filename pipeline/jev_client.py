"""
Call site for TypeSafe's Jev (System One), the sibling of llm_client.py.

Deliberately NOT part of llm_client.py. Jev is not an OpenAI-compatible
endpoint: it takes {state, model, questions} at POST /v1/systemone and returns
typed decisions with calibrated probabilities rather than text, so
complete_structured() cannot reach it and litellm cannot route it. The same
reasoning that makes complete_structured() the single call site for every text
LLM makes this the single call site for every Jev call - the moment Stage 4
also wants a noul, it should come through here rather than grow a second
copy of the retry and logging code.

Three things here exist because the smoke-test probes found them, not because
the docs mentioned them:

  - Jev returns 503 under load. One probe hit it and succeeded on a plain
    retry, so a harness run without backoff would die on a blip.
  - throttle() in llm_client.py is a single global pacer spacing EVERY
    provider call by 4.5s for Gemini's 15 requests/minute free-tier ceiling.
    Routing Jev through it would impose one provider's rate limit on another
    and erase the sub-second latency Jev is partly sold on. This module keeps
    its own interval.
  - "jev-latest" resolves server-side to a versioned model ("jev-1.13.0").
    The token log records the RESOLVED value, because the log's whole purpose
    is recording what actually reached the provider rather than what the
    config asked for - and because verify_run.py --expect-model is only
    meaningful against the real thing.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import requests

import pipeline.settings  # noqa: F401 - import side effect: load_dotenv()
from pipeline.llm_client import log_token_usage
from pipeline.settings import get_typesafe_api_key, redact_tokens, typesafe_auth_headers

logger = logging.getLogger(__name__)

JEV_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_JEV_MODEL = "jev-latest"
REQUEST_TIMEOUT_SECONDS = 30
MAX_TRANSIENT_RETRIES = 5

# Jev's published rate limits are not documented for this tier, and the probes
# ran ~11 calls back to back without a 429 (the one failure was a 503, which is
# load, not throttling). A small default spaces calls enough to be polite
# without pretending to know a ceiling. Set 0 to disable.
DEFAULT_CALL_INTERVAL_SECONDS = 0.25

# Status codes worth retrying: rate limiting and anything the server itself
# failed on. A 4xx that is not 429 means the request is wrong, and retrying an
# identical wrong request just wastes the budget.
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


def _read_call_interval() -> float:
    raw = os.environ.get("JEV_MIN_CALL_INTERVAL")
    if raw is None or raw.strip() == "":
        return DEFAULT_CALL_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[jev] JEV_MIN_CALL_INTERVAL=%r is not a number - using the %.2fs default",
            raw, DEFAULT_CALL_INTERVAL_SECONDS,
        )
        return DEFAULT_CALL_INTERVAL_SECONDS
    return max(0.0, value)


MIN_CALL_INTERVAL_SECONDS = _read_call_interval()

_last_call_started_at = 0.0
_throttle_lock = threading.Lock()


def throttle() -> None:
    """Space consecutive Jev calls by MIN_CALL_INTERVAL_SECONDS.

    Separate from llm_client.throttle() on purpose - see the module docstring.
    Two providers with different limits sharing one pacer means the slowest
    limit wins everywhere.
    """
    global _last_call_started_at
    if MIN_CALL_INTERVAL_SECONDS <= 0:
        return
    with _throttle_lock:
        wait = MIN_CALL_INTERVAL_SECONDS - (time.monotonic() - _last_call_started_at)
        if wait > 0:
            time.sleep(wait)
        _last_call_started_at = time.monotonic()


class JevError(RuntimeError):
    """A Jev call that could not be completed. Message is already redacted."""


def decide(
    *,
    state: object,
    questions: dict,
    jev_model: str = DEFAULT_JEV_MODEL,
    stage_name: str = "jev",
    run_id: str | None = None,
    vendor_id: str | None = None,
    post_id: str | None = None,
) -> dict:
    """One Jev System One call. Returns the parsed response body.

    Args:
        state: the content being judged. A string, or an object/array when the
            question needs more than the raw text (this pipeline passes the
            caption plus the Stage 1 account profile).
        questions: map of question id -> {type, instructions, criteria?}.
            Types are "noul" (yes/no, returns a probability), "choice" (one of
            up to 255 options) and "score" (ordered levels).
        jev_model: NOT `model`. eval/harness.py::load_stage_fn() reserves and
            strips a config key called `model`, so any stage reaching this
            function must name its parameter something else or the configured
            value silently never arrives.
        run_id: token usage is logged only when this is set, matching
            complete_structured() - a one-off probe should not pollute
            report/token_log.csv, which verify_run.py gates on.

    Raises:
        JevError: on a missing key, a permanent HTTP error, or transient
            failures that outlived the retry budget.
    """
    key = get_typesafe_api_key()
    if not key:
        raise JevError(
            "TYPESAFE_API_KEY is not set - add it to .env. "
            "pipeline/settings.py is the only place that loads it."
        )

    payload = {"model": jev_model, "state": state, "questions": questions}
    headers = typesafe_auth_headers(key)

    transient_attempts = 0
    while True:
        try:
            throttle()
            response = requests.post(
                JEV_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            transient_attempts += 1
            if transient_attempts > MAX_TRANSIENT_RETRIES:
                raise JevError(
                    f"[{stage_name}] Jev unreachable after {MAX_TRANSIENT_RETRIES} retries: "
                    f"{redact_tokens(exc)}"
                ) from None
            wait = 2 ** transient_attempts
            logger.warning("[jev] %s: network error, waiting %ds (retry %d/%d): %s",
                            stage_name, wait, transient_attempts, MAX_TRANSIENT_RETRIES,
                            redact_tokens(exc))
            time.sleep(wait)
            continue

        if response.status_code in _TRANSIENT_STATUS:
            transient_attempts += 1
            if transient_attempts > MAX_TRANSIENT_RETRIES:
                raise JevError(
                    f"[{stage_name}] Jev returned {response.status_code} after "
                    f"{MAX_TRANSIENT_RETRIES} retries - giving up rather than reporting "
                    f"a partial run as complete."
                )
            wait = 2 ** transient_attempts
            logger.warning("[jev] %s: HTTP %d, waiting %ds (retry %d/%d)",
                            stage_name, response.status_code, wait,
                            transient_attempts, MAX_TRANSIENT_RETRIES)
            time.sleep(wait)
            continue

        if not response.ok:
            # 4xx other than 429: the request itself is wrong. Retrying an
            # identical wrong request only burns budget. redact_tokens on the
            # body because an auth error can echo a header back.
            raise JevError(
                f"[{stage_name}] Jev returned HTTP {response.status_code}: "
                f"{redact_tokens(response.text)[:400]}"
            )

        break

    body = response.json()

    if run_id is not None:
        usage = body.get("usage") or {}
        # The model the SERVER says answered, not the alias we asked for:
        # "jev-latest" resolves to "jev-1.13.0", and the token log exists to
        # record what actually happened.
        resolved_model = body.get("model") or jev_model
        log_token_usage(
            run_id=run_id,
            vendor_id=vendor_id or "unknown",
            post_id=post_id or "",
            stage=stage_name,
            model=resolved_model,
            prompt_tokens=int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
        )

    return body


def noul_probability(body: dict, question_id: str) -> float:
    """P(yes) for one noul answer.

    A noul returns the probability directly and carries NO separate confidence
    field, unlike choice and score. Callers wanting a confidence should use the
    distance from the decision boundary - see stage2_triage_jev.py.
    """
    answer = (body.get("answers") or {}).get(question_id)
    if not isinstance(answer, dict) or "noul" not in answer:
        raise JevError(
            f"Jev response has no noul answer for question {question_id!r}. "
            f"Got keys: {sorted(answer) if isinstance(answer, dict) else type(answer).__name__}"
        )
    return float(answer["noul"])
