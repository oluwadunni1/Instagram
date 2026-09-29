"""Stage 0 request handling: network-error retry and the comments_count skip.

Offline - requests.get is replaced with a fake, and no token is read from .env.
"""

from __future__ import annotations

import pytest
import requests

import ingest.ingest as ingest


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self) -> dict:
        return self._payload


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest.time, "sleep", lambda _seconds: None)


def test_get_retries_a_network_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = [requests.ConnectionError("reset"), _Resp(200, {"ok": True})]

    def fake_get(*_args, **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(ingest.requests, "get", fake_get)
    assert ingest._get("https://graph.instagram.com/x", {}, "tok") == {"ok": True}


def test_get_raises_runtimeerror_once_network_retries_are_spent(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(*_args, **_kwargs):
        raise requests.Timeout("slow")

    monkeypatch.setattr(ingest.requests, "get", fake_get)
    # RuntimeError, not requests.Timeout: fetch_comments() isolates per-post
    # failures by catching RuntimeError, so a raw requests exception would
    # abort the whole ingest before the dump is written.
    with pytest.raises(RuntimeError):
        ingest._get("https://graph.instagram.com/x", {}, "tok")


def test_comments_request_skipped_only_on_an_explicit_zero_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    media = [
        {"id": "zero", "comments_count": 0},
        {"id": "some", "comments_count": 2},
        {"id": "missing"},  # no count in the response: must still fetch
    ]
    fetched: list[str] = []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ingest, "get_ig_access_token", lambda _name: "tok")
    monkeypatch.setattr(ingest, "fetch_profile", lambda _token: {"username": "v"})
    monkeypatch.setattr(ingest, "fetch_media_list", lambda _token: [dict(m) for m in media])

    def fake_fetch_comments(media_id, _token, debug=False):
        fetched.append(media_id)
        return []

    monkeypatch.setattr(ingest, "fetch_comments", fake_fetch_comments)

    ingest.ingest_account("acct")

    assert fetched == ["some", "missing"]
