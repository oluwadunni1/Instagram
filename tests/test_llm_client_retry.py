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


def test_a_402_inside_a_longer_message_is_still_caught() -> None:
    """The status arrives buried in a nested provider JSON blob, not as a
    clean attribute - litellm does not reliably surface it otherwise."""
    exc = RuntimeError(
        "litellm.APIError: APIError: OpenrouterException - "
        '{"error":{"message":"Insufficient credits","code":402,'
        '"metadata":{"limit_source":"openrouter_credits"}}}'
    )
    assert _is_permanent_provider_error(exc) is True
