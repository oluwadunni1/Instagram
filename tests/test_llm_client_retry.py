"""Retry-classification tests for pipeline/llm_client.py.

A permanent provider error that arrives wearing a transient error's type is
retried six times with exponential backoff (~2 minutes) before failing. That
turned three separate OpenRouter credit exhaustions on 2026-09-09 into
multi-minute mysteries instead of instant, legible failures - and on a 41-post
run it is over an hour of pure waiting for a result that cannot be published.

Offline: no network, no .env, no golden set.
"""

from __future__ import annotations

import pytest

from pipeline import llm_client
from pipeline.llm_client import _is_permanent_provider_error


@pytest.mark.parametrize("message", [
    'OpenrouterException - {"error":{"message":"Insufficient credits. This account never '
    'purchased credits. Make sure your key is on the correct account or org","code":402}}',
    'This request requires more credits, or fewer max_tokens.',
    'litellm.APIError: APIError: OpenrouterException - 402 payment required',
])
def test_credit_exhaustion_is_permanent(message: str) -> None:
    """All three shapes seen in the wild on 2026-09-09 - two distinct
    OpenRouter phrasings plus a bare status code."""
    assert _is_permanent_provider_error(RuntimeError(message)) is True


@pytest.mark.parametrize("message", [
    "429 Too Many Requests - rate limit exceeded, please retry",
    "503 Service Unavailable: provider overloaded",
    "Read timed out after 60s",
    "Connection refused",
    "500 Internal Server Error",
])
def test_genuinely_transient_errors_still_retry(message: str) -> None:
    """The backoff exists for a reason - free-tier 429s and provider 5xx are
    exactly what it is for, and must not be swept into the fast-fail path."""
    assert _is_permanent_provider_error(RuntimeError(message)) is False


# --- call throttling -------------------------------------------------------
# Gemini's free tier is 15 requests/minute/model. Unpaced, a 10-post run bursts
# past it, the retry loop absorbs the 429s, and Stage 3 quietly degrades to its
# regex heuristic while the run still prints clean scores (2026-09-11).

def _spy_clock(monkeypatch: pytest.MonkeyPatch, ticks: list[float]) -> list[float]:
    """Scripted monotonic clock; returns the list sleeps get recorded into.
    throttle() reads the clock twice per call - once to size the wait, once to
    record the start - so each call consumes two ticks."""
    slept: list[float] = []
    stream = iter(ticks)
    monkeypatch.setattr(llm_client.time, "monotonic", lambda: next(stream))
    monkeypatch.setattr(llm_client.time, "sleep", lambda seconds: slept.append(seconds))
    return slept


def test_a_burst_is_spaced_out_to_the_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_client, "MIN_CALL_INTERVAL_SECONDS", 4.0)
    monkeypatch.setattr(llm_client, "_last_call_started_at", 0.0)
    slept = _spy_clock(monkeypatch, [100.0, 100.0, 101.0, 101.0])

    llm_client.throttle()  # first call - nothing to wait behind
    llm_client.throttle()  # only 1s later, so it owes 3s

    assert slept == [3.0]


def test_a_slow_call_pays_no_further_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spacing is start-to-start, which is what a per-minute quota counts. A
    call that itself took longer than the interval has already paid it."""
    monkeypatch.setattr(llm_client, "MIN_CALL_INTERVAL_SECONDS", 4.0)
    monkeypatch.setattr(llm_client, "_last_call_started_at", 0.0)
    slept = _spy_clock(monkeypatch, [100.0, 100.0, 105.0, 105.0])

    llm_client.throttle()
    llm_client.throttle()

    assert slept == []


def test_zero_interval_disables_throttling_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    """The paid-tier escape hatch must not even read the clock."""
    monkeypatch.setattr(llm_client, "MIN_CALL_INTERVAL_SECONDS", 0.0)
    slept = _spy_clock(monkeypatch, [])  # any clock read would StopIteration

    llm_client.throttle()
    llm_client.throttle()

    assert slept == []


def test_a_402_inside_a_longer_message_is_still_caught() -> None:
    """The status arrives buried in a nested provider JSON blob, not as a
    clean attribute - litellm does not reliably surface it otherwise."""
    exc = RuntimeError(
        "litellm.APIError: APIError: OpenrouterException - "
        '{"error":{"message":"Insufficient credits","code":402,'
        '"metadata":{"limit_source":"openrouter_credits"}}}'
    )
    assert _is_permanent_provider_error(exc) is True
