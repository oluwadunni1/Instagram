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
    """Records which passes ran, so routing can be asserted directly.

    `ocr_text` controls what the OCR tier returns: None means it read nothing
    and the caller should fall through to vision.
    """
    seen: dict = {"jev": 0, "text": 0, "vision": 0, "ocr": 0,
                  "text_stage": None, "vision_stage": None, "ocr_text": None}

    def fake_ocr(image_url, post_id="", account_label=None, min_chars=12, use_cache=True):
        seen["ocr"] += 1
        text = seen.get("ocr_text")
        return (text, "ok") if text else (None, "too little text")

    monkeypatch.setattr(hybrid, "extract_text", fake_ocr)

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

def test_empty_caption_tries_ocr_before_vision(calls: dict) -> None:
    """OCR is local and free, so it must be attempted before anything is paid
    for. When OCR reads nothing, vision is still the fallback."""
    result = hybrid.triage_post_hybrid(post(caption=""))
    assert calls["ocr"] == 1, "OCR must be tried before vision"
    assert calls["vision"] == 1
    assert calls["jev"] == 0, "Jev cannot help when neither caption nor OCR has text"
    assert result["escalation_reason"] == "no_caption_vision"


def test_ocr_text_goes_to_jev_and_avoids_vision(calls: dict) -> None:
    """The whole point of the tier. OCR turns a caption-less post into a
    text-bearing one, which puts it back inside Jev's reach - Jev cannot see
    images, and that was its one structural limit as a Pass A."""
    calls["ocr_text"] = "iPhone 15 Pro Max 256GB Price:NGN730,000"
    calls["p_yes"] = 0.96
    result = hybrid.triage_post_hybrid(post(caption=""))
    assert calls["ocr"] == 1
    assert calls["jev"] == 1
    assert calls["vision"] == 0, "a confident Jev read of OCR text must not reach vision"
    assert result["post_type"] == "product_listing"
    assert result["escalation_reason"] == "ocr"


def test_ocr_read_but_jev_unsure_still_falls_back_to_vision(calls: dict) -> None:
    """OCR narrows the vision path, it does not replace it. A garbled read
    that leaves Jev uncertain must not silently become an answer."""
    calls["ocr_text"] = "sdkfj 88 ???"
    calls["p_yes"] = 0.55          # confidence 0.55, below the 0.80 default
    result = hybrid.triage_post_hybrid(post(caption=""))
    assert calls["ocr"] == 1
    assert calls["jev"] == 1
    assert calls["vision"] == 1, "an unsure Jev on OCR text must still escalate"
    assert result["escalation_reason"] == "no_caption_vision"


def test_use_ocr_false_restores_the_old_behaviour(calls: dict) -> None:
    """The flag exists so the OCR saving can be measured by difference, and so
    a clone without rapidocr installed behaves identically."""
    result = hybrid.triage_post_hybrid(post(caption=""), use_ocr=False)
    assert calls["ocr"] == 0, "use_ocr=False must not even attempt OCR"
    assert calls["vision"] == 1
    assert result["escalation_reason"] == "no_caption_vision"


def test_emoji_only_caption_counts_as_empty(calls: dict) -> None:
    result = hybrid.triage_post_hybrid(post(caption="🔥🔥🔥"))
    assert calls["ocr"] == 1
    assert calls["vision"] == 1
    assert result["escalation_reason"] == "no_caption_vision"


def test_a_caption_bearing_post_never_reaches_ocr(calls: dict) -> None:
    """OCR is only for posts with nothing to read. Running it on a post that
    already has a caption would spend latency to learn nothing."""
    calls["p_yes"] = 0.95
    hybrid.triage_post_hybrid(post())
    assert calls["ocr"] == 0


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
