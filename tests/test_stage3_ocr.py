"""Offline tests for Stage 3's OCR tier and its price guard.

No network, no model, no golden set. The passes are replaced, so what is under
test is the routing and the guard - the two things the harness structurally
cannot see, because it only ever sees the final extraction.

The guard gets the most attention here on purpose. Stage 2's OCR tier can be
wrong cheaply; this one reads the price, which the brief calls the single
unforgivable failure mode, and eval/harness.py prints "MUST BE 100%" on
missing-price recall because of it.
"""

from __future__ import annotations

import pytest

from pipeline.stages import stage3_extract as s3
from pipeline.stages.stage3_extract import ProductExtractionResult


# --- the separator rules ---------------------------------------------------

@pytest.mark.parametrize(
    "token, expected",
    [
        # The case the whole guard exists for. pipeline/ocr.py's spike read a
        # real product card as "N500.000"; float() gives 500 and the listing is
        # off by 1000x. Three digits after the separator means thousands.
        ("500.000", 500_000),
        ("1,400,000", 1_400_000),
        ("910,000", 910_000),
        ("1 400 000", 1_400_000),
        ("500", 500),
        # Ambiguous: a trailing 1-2 digit group could be a decimal fraction or
        # a mangled read, and there is no safe way to tell. Refuse, never guess.
        ("1.5", None),
        ("2,5", None),
        ("1,400.50", None),
    ],
)
def test_separator_rules(token: str, expected: int | None) -> None:
    assert s3._normalise_ocr_price(token) == expected


def test_n500_000_is_never_five_hundred() -> None:
    """Stated separately from the table because it is the headline case: the
    wrong answer must be absent, not merely not-preferred."""
    candidates = s3._ocr_price_candidates("Off\nN500.000")
    assert 500_000 in candidates
    assert 500 not in candidates


def test_an_imei_is_not_a_price_candidate() -> None:
    """The second spike finding. A bare digit run carries no currency marker,
    so it never becomes a candidate and can never ground a price."""
    ocr = "IPHONE16256GB\n256GBA3288S\n352142373198245\nPrice:NGN910,000"
    assert s3._ocr_price_candidates(ocr) == {910_000}


def test_a_price_token_does_not_run_across_a_newline() -> None:
    """A real miss from the spike: with \\s in the token class, "NGN1,400,000"
    followed by a line reading "16Pk" parsed as the groups 1/400/000/16 and was
    refused, losing a price OCR had read correctly."""
    assert s3._ocr_price_candidates("PRICENGN1,400,000\n16Pk") == {1_400_000}


# --- the guard -------------------------------------------------------------

def result_with(price: dict) -> ProductExtractionResult:
    return ProductExtractionResult.model_validate(
        {"products": [{"name": "iPhone 15 128GB", "price": price, "extraction_confidence": 0.9}]}
    )


def test_guard_keeps_a_grounded_image_price() -> None:
    res = result_with({"value": 500_000, "currency": "NGN", "source": "image", "confidence": 0.9})
    assert s3._guard_ocr_prices(res, "IPH0NE 15 128GB\nN500.000") is False
    assert res.products[0].price.value == 500_000


def test_guard_voids_an_ungrounded_image_price_and_forces_escalation() -> None:
    """The 1000x error, caught. If the model had read "N500.000" as 500, that
    integer is not recoverable from the text under the separator rules, so it
    is voided rather than published."""
    res = result_with({"value": 500, "currency": "NGN", "source": "image", "confidence": 0.9})
    assert s3._guard_ocr_prices(res, "IPH0NE 15 128GB\nN500.000") is True
    price = res.products[0].price
    assert price.value is None
    assert price.source == "none"
    assert price.confidence == 0.0


def test_guard_voids_an_imei_read_as_a_price() -> None:
    res = result_with(
        {"value": 352142373198245, "currency": "NGN", "source": "image", "confidence": 0.8}
    )
    assert s3._guard_ocr_prices(res, "352142373198245\nPrice:NGN910,000") is True
    assert res.products[0].price.value is None


def test_guard_never_touches_a_caption_price() -> None:
    """A price the vendor typed was never OCR's to get wrong. The guard must
    not be able to damage the path that produces most of the project's prices."""
    res = result_with({"value": 7_500_000, "currency": "NGN", "source": "caption", "confidence": 0.95})
    assert s3._guard_ocr_prices(res, "unrelated image text") is False
    assert res.products[0].price.value == 7_500_000


# --- the cascade -----------------------------------------------------------

@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Records which passes ran, so the tier routing can be asserted directly."""
    seen: dict = {"ocr": 0, "pass_a": 0, "pass_a_ocr": 0, "vision": 0,
                  "ocr_text": None, "prompt_seen": None}

    def fake_ocr(image_url, post_id="", account_label=None, min_chars=12, use_cache=True):
        seen["ocr"] += 1
        text = seen.get("ocr_text")
        return (text, "ok") if text else (None, "too little text")

    def fake_pass_a(user_prompt, *a, **k):
        seen["pass_a"] += 1
        seen["prompt_seen"] = user_prompt
        return result_with(seen.get("price_a", {"source": "none", "confidence": 0.0}))

    def fake_pass_a_ocr(user_prompt, *a, **k):
        seen["pass_a_ocr"] += 1
        seen["prompt_seen"] = user_prompt
        return result_with(
            seen.get("price_ocr", {"value": 500_000, "currency": "NGN",
                                   "source": "image", "confidence": 0.9})
        )

    def fake_pass_b(post, user_prompt, *a, **k):
        seen["vision"] += 1
        return result_with({"value": 999, "currency": "NGN", "source": "image", "confidence": 0.9})

    monkeypatch.setattr(s3, "extract_text", fake_ocr)
    monkeypatch.setattr(s3, "_pass_a", fake_pass_a)
    monkeypatch.setattr(s3, "_pass_a_ocr", fake_pass_a_ocr)
    monkeypatch.setattr(s3, "_pass_b", fake_pass_b)
    return seen


def post(caption: str = "2024 Toyota Hilux, N7,500,000", image: bool = True) -> dict:
    p: dict = {"post_id": "p1", "caption": caption}
    if image:
        p["media_url"] = "https://cdn.example.com/p.jpg"
    return p


def test_empty_caption_reads_ocr_before_spending_a_pass(calls: dict) -> None:
    """Tier 0. With no caption, a plain Pass A is a call spent on an empty
    prompt that escalates anyway, so OCR goes first and rides along on one call."""
    calls["ocr_text"] = "IPH0NE 15 128GB\nN500.000"
    s3.extract_product(post(caption=""), use_ocr=True)
    assert calls["ocr"] == 1
    assert calls["pass_a"] == 0, "the empty-caption pass must not be spent"
    assert calls["pass_a_ocr"] == 1
    assert calls["vision"] == 0, "a grounded OCR price must not reach vision"


def test_ocr_text_arrives_in_its_own_block_not_as_the_caption(calls: dict) -> None:
    """The design decision that separates this from the Stage 2 wiring. The
    model has to be able to tell vendor-written text from recognised text."""
    calls["ocr_text"] = "IPH0NE 15 128GB\nN500.000"
    s3.extract_product(post(caption=""), use_ocr=True)
    prompt = calls["prompt_seen"]
    assert "IMAGE TEXT" in prompt
    assert prompt.index("Caption:") < prompt.index("IMAGE TEXT")


def test_captioned_post_tries_ocr_before_vision(calls: dict) -> None:
    """Tier 1. A caption does not mean the price is in it - Stage 3 escalates
    on a missing price, and that price is often printed on the photo."""
    calls["ocr_text"] = "IPH0NE 15 128GB\nN500.000"
    s3.extract_product(post(caption="Brand new, DM to order"), use_ocr=True)
    assert calls["pass_a"] == 1, "the captioned post still gets its normal text pass first"
    assert calls["ocr"] == 1
    assert calls["pass_a_ocr"] == 1
    assert calls["vision"] == 0


def test_unreadable_ocr_still_reaches_vision(calls: dict) -> None:
    """OCR narrows the vision path, it does not replace it. Every failure path
    in pipeline/ocr.py returns None and None must still mean escalate."""
    calls["ocr_text"] = None
    s3.extract_product(post(caption=""), use_ocr=True)
    assert calls["ocr"] == 1
    assert calls["vision"] == 1


def test_an_ungrounded_ocr_price_escalates_to_vision(calls: dict) -> None:
    """The guard's job inside the cascade: a price it voids must not simply
    disappear, it must send the post to the model that can actually look."""
    calls["ocr_text"] = "IPH0NE 15 128GB\nN500.000"
    calls["price_ocr"] = {"value": 500, "currency": "NGN", "source": "image", "confidence": 0.9}
    s3.extract_product(post(caption=""), use_ocr=True)
    assert calls["pass_a_ocr"] == 1
    assert calls["vision"] == 1, "a voided price must escalate, not vanish"


def test_use_ocr_false_restores_the_old_behaviour(calls: dict) -> None:
    """The flag is how the saving gets measured by difference, and how a clone
    without rapidocr installed keeps behaving exactly as the recorded runs did."""
    s3.extract_product(post(caption=""))          # default: use_ocr=False
    assert calls["ocr"] == 0, "the default must not even attempt OCR"
    assert calls["pass_a"] == 1
    assert calls["pass_a_ocr"] == 0
    assert calls["vision"] == 1


def test_dm_for_price_suppresses_ocr_as_well_as_vision(calls: dict) -> None:
    """A confirmed absence, not an uncertainty. If the vendor says to DM for
    the price, it is not on the photo either - OCR would download an image to
    re-answer a question already answered."""
    s3.extract_product(post(caption="Brand new Hilux, DM for price"), use_ocr=True)
    assert calls["ocr"] == 0
    assert calls["vision"] == 0


def test_no_image_means_no_ocr(calls: dict) -> None:
    s3.extract_product(post(caption="", image=False), use_ocr=True)
    assert calls["ocr"] == 0
    assert calls["vision"] == 0
