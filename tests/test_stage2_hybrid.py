"""Offline tests for the Jev -> Gemini hybrid Stage 2 cascade.

No network. Both providers are mocked: Jev at the requests layer, Gemini by
replacing the two pass functions the hybrid reuses.

These cover the routing, which is the whole point of the module and the part
the harness cannot see. Whether the routing produces good answers is the
harness's job.
"""

from __future__ import annotations

import pytest

from pipeline import jev_client
from pipeline.stages import stage2_triage_hybrid as hybrid
from pipeline.stages.stage2_triage import TriageResult

from tests.test_stage2_jev import FakeResponse, jev_body  # reuse the Jev fakes


@pytest.fixture(autouse=True)
def _no_key_or_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jev_client, "get_typesafe_api_key",
                        lambda *a, **k: "ts-FAKEtestkeyDONOTUSE")  # pragma: allowlist secret
    monkeypatch.setattr(jev_client.time, "sleep", lambda _s: None)
    monkeypatch.setattr(jev_client, "throttle", lambda: None)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Records which passes ran, so routing can be asserted directly."""
    seen: dict = {"jev": 0, "text": 0, "vision": 0, "text_stage": None, "vision_stage": None}

    def fake_jev(*a: object, **k: object) -> FakeResponse:
        seen["jev"] += 1
        return FakeResponse(body=jev_body(seen.get("p_yes", 0.9)))

    def fake_text(post, profile, text_model, run_id, vendor_id, post_id, stage_name=""):
        seen["text"] += 1
        seen["text_stage"] = stage_name
        return TriageResult(post_type=seen.get("text_says", "announcement"), confidence=0.93)

    def fake_vision(post, profile, vision_model, run_id, vendor_id, post_id, stage_name=""):
        seen["vision"] += 1
        seen["vision_stage"] = stage_name
        return TriageResult(post_type=seen.get("vision_says", "product_listing"), confidence=0.97)

    monkeypatch.setattr(jev_client.requests, "post", fake_jev)
    monkeypatch.setattr(hybrid, "_pass_a", fake_text)
    monkeypatch.setattr(hybrid, "_pass_b", fake_vision)
    return seen


def post(caption: str = "2024 Toyota Hilux, brand new", image: bool = True) -> dict:
    p: dict = {"post_id": "p1", "caption": caption}
    if image:
        p["media_url"] = "https://cdn.example.com/p.jpg"
    return p


# --- trigger 1: no caption -------------------------------------------------

def test_empty_caption_goes_to_vision_and_skips_jev(calls: dict) -> None:
    """The point of running the free regex first: a text model on an empty
    caption is a guaranteed waste, so Jev must not be called at all."""
    result = hybrid.triage_post_hybrid(post(caption=""))
    assert calls["vision"] == 1
    assert calls["jev"] == 0, "Jev must not be called when there is no text to read"
    assert calls["text"] == 0
    assert result["escalated"] is True
    assert result["escalation_reason"] == "no_caption"


def test_emoji_only_caption_counts_as_empty(calls: dict) -> None:
    result = hybrid.triage_post_hybrid(post(caption="🔥🔥🔥"))
    assert calls["vision"] == 1
    assert calls["jev"] == 0
    assert result["escalation_reason"] == "no_caption"


def test_empty_caption_without_an_image_falls_through_to_jev(calls: dict) -> None:
    """Nothing to escalate to. Degrade to Jev rather than crash."""
    result = hybrid.triage_post_hybrid(post(caption="", image=False))
    assert calls["vision"] == 0
    assert calls["jev"] == 1
    assert result["escalated"] is False


# --- trigger 2: low confidence ---------------------------------------------

def test_low_confidence_escalates_to_text_not_vision(calls: dict) -> None:
    """The low-confidence cases observed are textual boundary calls - a post
    marked SOLD while still listing a price, an 'available soon'. Re-reading
    the photo adds nothing and vision costs more."""
    calls["p_yes"] = 0.70          # confidence 0.70, below the 0.80 default
    result = hybrid.triage_post_hybrid(post())
    assert calls["jev"] == 1
    assert calls["text"] == 1
    assert calls["vision"] == 0, "low confidence must NOT reach vision"
    assert result["escalated"] is True
    assert result["escalation_reason"] == "low_confidence"


def test_high_confidence_does_not_escalate(calls: dict) -> None:
    calls["p_yes"] = 0.97
    result = hybrid.triage_post_hybrid(post())
    assert calls["jev"] == 1
    assert calls["text"] == 0 and calls["vision"] == 0
    assert result["escalated"] is False
    assert result["escalation_reason"] is None
    assert result["post_type"] == "product_listing"


def test_confidence_exactly_at_the_threshold_does_not_escalate(calls: dict) -> None:
    calls["p_yes"] = 0.80   # confidence == escalate_below
    hybrid.triage_post_hybrid(post(), escalate_below=0.80)
    assert calls["text"] == 0


def test_escalate_below_is_configurable(calls: dict) -> None:
    calls["p_yes"] = 0.90
    hybrid.triage_post_hybrid(post(), escalate_below=0.95)
    assert calls["text"] == 1, "a higher bar must escalate a 0.90 answer"


# --- five-way to binary mapping --------------------------------------------

@pytest.mark.parametrize("gemini_says, expected", [
    ("product_listing", "product_listing"),
    ("announcement", "not_product"),
    ("testimonial_repost", "not_product"),
    ("meme_personal", "not_product"),
    ("ad_creative", "not_product"),
])
def test_gemini_five_way_collapses_onto_the_binary_vocabulary(
    calls: dict, gemini_says: str, expected: str
) -> None:
    """Downstream consumers all test `!= "product_listing"`, so the collapse is
    lossless for this stage's purpose."""
    calls["vision_says"] = gemini_says
    result = hybrid.triage_post_hybrid(post(caption=""))
    assert result["post_type"] == expected


# --- token-log attribution -------------------------------------------------

def test_escalations_carry_distinct_stage_names(calls: dict) -> None:
    """Without these, a hybrid run's Gemini calls are indistinguishable from a
    pure-Gemini run's in report/token_log.csv, and the two escalation paths
    cannot be costed separately."""
    hybrid.triage_post_hybrid(post(caption=""))
    assert calls["vision_stage"] == hybrid.VISION_STAGE_NAME

    calls["p_yes"] = 0.60
    hybrid.triage_post_hybrid(post())
    assert calls["text_stage"] == hybrid.TEXT_STAGE_NAME


def test_stage_declares_no_reserved_parameter() -> None:
    import inspect
    params = set(inspect.signature(hybrid.triage_post_hybrid).parameters)
    assert "model" not in params
    assert {"jev_model", "text_model", "vision_model"} <= params
