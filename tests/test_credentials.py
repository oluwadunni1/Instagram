"""Credential-handling tests.

Two live-token leaks have now reached the console in this project (the IG
token in a Graph URL, FINDINGS.md 2026-09-07; the Gemini key in an embedding
URL, FINDINGS.md 2026-09-08), both by the same mechanism: a credential in a
query string that requests then embedded in an exception it logged. These
tests pin the fixes so a third call site can't reintroduce it quietly.

The sentinel below is a fake value chosen to be obviously not a real key -
never put a real credential in a test.
"""

from __future__ import annotations

import logging

import pytest
import requests

import pipeline.media_fingerprint as mf
from pipeline.settings import (
    gemini_auth_headers,
    redact_tokens,
    strip_url_credentials,
)

SENTINEL = "AIzaFAKEtestkeyDONOTUSE0123456789"


# --- redact_tokens ---------------------------------------------------------

@pytest.mark.parametrize("text", [
    f"https://generativelanguage.googleapis.com/v1beta/models/x:embedContent?key={SENTINEL}",
    f"429 Client Error for url: https://example.com/v1?key={SENTINEL}&alt=json",
    f"https://graph.instagram.com/me/media?access_token={SENTINEL}&fields=id",
    f"Authorization: Bearer {SENTINEL}",
    f"{{'x-goog-api-key': '{SENTINEL}'}}",
    f"https://example.com/?api_key={SENTINEL}",
])
def test_redact_tokens_removes_every_credential_shape(text: str) -> None:
    assert SENTINEL not in redact_tokens(text)
    assert "<redacted>" in redact_tokens(text)


def test_redact_tokens_leaves_non_credentials_alone() -> None:
    # `key=` must match the query param, not the tail of a longer word, and
    # the CDN cache params on a media_url are not credentials.
    text = "https://cdn.example.com/p.jpg?oh=abc&oe=def&monkey=business"
    assert redact_tokens(text) == text


# --- strip_url_credentials -------------------------------------------------

def test_strip_url_credentials_removes_token_and_reports_it() -> None:
    url = f"https://graph.instagram.com/me/media?fields=id&access_token={SENTINEL}&after=XYZ"
    clean, had_credential = strip_url_credentials(url)
    assert had_credential is True
    assert SENTINEL not in clean
    # Everything else about the next-page URL must survive, or pagination
    # silently restarts from page 1 / drops requested fields.
    assert "fields=id" in clean
    assert "after=XYZ" in clean


def test_strip_url_credentials_is_a_noop_on_clean_urls() -> None:
    url = "https://graph.instagram.com/me/media?fields=id&after=XYZ"
    assert strip_url_credentials(url) == (url, False)


# --- gemini_auth_headers ---------------------------------------------------

def test_gemini_auth_headers_uses_header_and_tolerates_missing_key() -> None:
    assert gemini_auth_headers(SENTINEL) == {"x-goog-api-key": SENTINEL}
    # No key configured must not produce a header with a None value (requests
    # raises on that); let the API return its own "missing key" error.
    assert gemini_auth_headers(None) == {}


# --- compute_caption_embedding --------------------------------------------

def test_embedding_call_sends_key_in_header_never_in_url(
    monkeypatch: pytest.MonkeyPatch, no_sleep: None
) -> None:
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"embedding": {"values": [0.5] * mf.EMBEDDING_DIM}}

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setattr(mf, "GEMINI_API_KEY", SENTINEL)
    monkeypatch.setattr(mf.requests, "post", fake_post)

    values, ok = mf.compute_caption_embedding("a caption", post_id="p1")

    assert ok is True
    assert len(values) == mf.EMBEDDING_DIM
    assert SENTINEL not in captured["url"]
    assert captured["kwargs"].get("params") in (None, {})
    assert captured["kwargs"]["headers"] == {"x-goog-api-key": SENTINEL}


def test_embedding_failure_does_not_log_the_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, no_sleep: None
) -> None:
    """The regression test for FINDINGS.md 2026-09-08.

    A quota 429 is a demonstrated event on this project's Gemini key, and
    requests builds HTTPError's message from the full request URL. Even with
    header auth in place, the exception must be redacted before it is logged,
    because this branch logs at WARNING - visible at any LOG_LEVEL.
    """
    def fake_post(url: str, **kwargs: object):
        # Simulate the pre-fix shape: an error message carrying the key, of
        # the sort requests produces when a credential is in the URL.
        raise requests.HTTPError(
            f"429 Client Error: Too Many Requests for url: {mf.GEMINI_EMBED_URL}?key={SENTINEL}"
        )

    monkeypatch.setattr(mf, "GEMINI_API_KEY", SENTINEL)
    monkeypatch.setattr(mf.requests, "post", fake_post)

    with caplog.at_level(logging.WARNING):
        values, ok = mf.compute_caption_embedding("a caption", post_id="p1")

    assert ok is False
    assert values == [0.0] * mf.EMBEDDING_DIM  # degrades, does not crash
    assert caplog.text, "the failure must still be logged"
    assert SENTINEL not in caplog.text
