"""Eval-harness tests.

The harness is this project's verification mechanism (there is no broad test
suite - CLAUDE.md), which means the harness itself is the one thing nothing
verifies. These cover the ways it can produce a wrong or missing number
without anyone noticing: a per-post failure taking the whole run down, a
configured value silently never reaching a stage, and the Stage 1 profile
never reaching one either - so the scored stages saw different inputs than
the production path gives them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import harness
from pipeline.config.experiment_schema import StageConfig
from pipeline.exceptions import MissingRawDumpError


# --- score_stage3 per-post error isolation ---------------------------------

def _listing(post_id: str, price_source: str = "caption", value: int | None = 1000) -> dict:
    return {
        "post_id": post_id,
        "post_type": "product_listing",
        "products": [{"name": "Thing", "price": {"value": value, "source": price_source}}],
    }


def _run_stage3(posts: list[dict], extract_fn, tmp_path: Path, monkeypatch,
                profile: dict | None = None) -> list[dict]:
    monkeypatch.chdir(tmp_path)
    harness.score_stage3(posts, extract_fn, "test-model", 0.0, "vendor_test", profile)
    written = tmp_path / "report" / "vendor_test" / "stage3_test-model_predictions.json"
    return json.loads(written.read_text(encoding="utf-8"))


def test_one_failing_post_does_not_abort_the_run(tmp_path: Path, monkeypatch, caplog) -> None:
    """score_stage2/score_stage4 have always isolated per-post failures;
    score_stage3 did not. It only looked safe because the one real Stage 3
    implementation swallows everything into _regex_fallback - an accident of
    that implementation, not a guarantee the harness enforced.
    """
    posts = [_listing("p1"), _listing("p2"), _listing("p3")]

    def extract_fn(post: dict, profile: dict | None = None) -> list[dict]:
        if post["post_id"] == "p2":
            raise RuntimeError("model gave up")
        return [{"name": "Thing", "price": {"value": 1000, "source": "caption"}}]

    with caplog.at_level(logging.ERROR):
        predictions = _run_stage3(posts, extract_fn, tmp_path, monkeypatch)

    # Every post is still scored and recorded - the run completed.
    assert [p["post_id"] for p in predictions] == ["p1", "p2", "p3"]
    assert predictions[1]["error"] == "model gave up"
    assert "[stage3 ERROR]" in caplog.text
    assert "p2" in caplog.text


def test_malformed_product_is_an_error_not_a_crash(tmp_path: Path, monkeypatch) -> None:
    """A stage3 returning a product with no "price" key is the same class of
    failure as one that raises, so the predicted[0]["price"] unpacking lives
    inside the same try."""
    posts = [_listing("p1")]

    predictions = _run_stage3(
        posts, lambda post, profile=None: [{"name": "Thing"}], tmp_path, monkeypatch
    )

    assert "error" in predictions[0]


def test_errored_post_never_counts_as_missing_price_recall(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """Brief section 11's "never invent a price" recall must stay honest.

    An errored post produced no evidence that the pipeline declined to invent
    a price, so it counts in the denominator but not as a hit - otherwise a
    run where every call failed would report 100% recall on the one metric
    the brief makes a hard requirement.
    """
    posts = [_listing("p1", price_source="none", value=None)]

    def always_fails(post: dict, profile: dict | None = None) -> list[dict]:
        raise RuntimeError("429 quota exceeded")

    with caplog.at_level(logging.INFO):
        _run_stage3(posts, always_fails, tmp_path, monkeypatch)

    assert "Missing-price recall: 0/1 = 0%" in caplog.text
    assert "MUST BE 100%" in caplog.text
    assert "Errors: 1 post(s)" in caplog.text


def test_hallucinated_price_still_fails_loudly(tmp_path: Path, monkeypatch, caplog) -> None:
    """The error path must not have softened the real check it sits next to."""
    posts = [_listing("p1", price_source="none", value=None)]

    def invents(post: dict, profile: dict | None = None) -> list[dict]:
        return [{"name": "Thing", "price": {"value": 5000, "source": "caption"}}]

    with caplog.at_level(logging.ERROR):
        _run_stage3(posts, invents, tmp_path, monkeypatch)

    assert "[HALLUCINATED PRICE]" in caplog.text


# --- load_stage_fn reserved-parameter tripwire -----------------------------

def _config(function: str, **extra) -> object:
    """A minimal ExperimentConfig stand-in - load_stage_fn only ever does
    getattr(config, stage_key), and resolve_profile() only reads
    stage1_profile (absent from every family_*.yaml, hence None here)."""
    entry = StageConfig(
        module="tests.stage_stubs",
        function=function,
        model="Display Label (Not A Model String)",
        cost_per_call_usd=0.0,
        **extra,
    )
    return type("Cfg", (), {"stage2_triage": entry, "stage1_profile": None})()


def test_configured_values_reach_the_stage_function() -> None:
    bound_fn, model_name, cost = harness.load_stage_fn(
        _config("good_stage", text_model="gemini/gemini-3.5-flash-lite", threshold=0.7),
        "stage2_triage",
    )

    result = bound_fn({"post_id": "p1"}, None)
    assert result["text_model"] == "gemini/gemini-3.5-flash-lite"
    assert result["threshold"] == 0.7
    # `model` is a display label for filenames and log lines, never a kwarg.
    assert model_name == "Display Label (Not A Model String)"
    assert cost == 0.0


def test_binding_kwargs_leaves_the_profile_slot_free() -> None:
    """load_stage_fn() binds config keys with functools.partial(fn, **kwargs),
    so both positional slots stay open. If it ever bound positionally instead,
    the profile would land in the wrong parameter - silently, since every
    stage's profile parameter is optional."""
    bound_fn, _, _ = harness.load_stage_fn(
        _config("good_stage", text_model="m"), "stage2_triage"
    )

    result = bound_fn({"post_id": "p1"}, {"vendor_username": "someone"})
    assert result["profile"] == {"vendor_username": "someone"}
    assert result["text_model"] == "m"


# --- Stage 1 profile threading ---------------------------------------------

@pytest.mark.parametrize(
    "score_fn, extra_gold, capture_key",
    [
        ("score_stage2", {"post_type": "product_listing"}, "stage2"),
        ("score_stage3", {}, "stage3"),
        ("score_stage4", {"expected_signals": []}, "stage4"),
    ],
)
def test_profile_reaches_every_scored_stage(
    score_fn: str, extra_gold: dict, capture_key: str, tmp_path: Path, monkeypatch
) -> None:
    """Regression guard for the 2026-09-09 fix.

    Until then the harness called every stage with the post alone while
    scripts/run_pipeline.py passed the Stage 1 profile, so every published
    accuracy number was measured on inputs the production path never uses -
    and Stage 4's [VENDOR]/[BUYER] comment labelling never fired in a scored
    run. The failure mode is silent (the parameter is optional and defaults to
    None), which is the same shape as the reserved-`model` bug above: the run
    completes and reports plausible wrong numbers.
    """
    monkeypatch.chdir(tmp_path)
    profile = {"vendor_username": "ayodele.akinbohun", "business_category": "autos"}
    seen: list[dict | None] = []

    def stage_fn(post: dict, prof: dict | None = None):
        seen.append(prof)
        # Shaped to satisfy whichever scorer is calling: stage2 reads
        # ["post_type"], stage3 reads [0]["price"], stage4 reads {"signal"}.
        if score_fn == "score_stage2":
            return {"post_type": "product_listing", "confidence": 0.9}
        if score_fn == "score_stage3":
            return [{"name": "Thing", "price": {"value": 1000, "source": "caption"}}]
        return []

    posts = [{**_listing("p1"), **extra_gold}]
    getattr(harness, score_fn)(posts, stage_fn, "test-model", 0.0, "vendor_test", profile)

    assert seen == [profile], f"{score_fn} did not pass the profile through to the stage"


def test_no_account_resolves_to_no_profile_loudly(caplog) -> None:
    """The no---golden path merges every vendor in eval/golden/ into one run,
    so there is no single account whose profile applies. Guessing one would
    feed vendor A's profile to vendor B's posts; the warning is what stops the
    resulting number being read as a per-vendor one."""
    with caplog.at_level(logging.WARNING):
        assert harness.resolve_profile(None, _config("good_stage")) is None

    assert "not comparable" in caplog.text


def test_a_missing_raw_dump_degrades_instead_of_crashing(monkeypatch, caplog) -> None:
    """An account with no raw dump yet has no profile to load. Scoring without
    one is worth more than not scoring at all, provided the log says so - the
    silent version of this is the bug being fixed."""
    def _raise(*args, **kwargs):
        raise MissingRawDumpError("no dump under runs/nope/raw/")

    monkeypatch.setattr(harness, "get_or_create_profile", _raise)

    with caplog.at_level(logging.WARNING):
        assert harness.resolve_profile("nope", _config("good_stage")) is None

    assert "make ingest ACCOUNT=nope" in caplog.text


@pytest.mark.parametrize("function", ["shadowed_model_stage", "shadowed_note_stage"])
def test_reserved_parameter_names_raise_instead_of_binding_silently(function: str) -> None:
    """Regression guard for FINDINGS.md 2026-09-07.

    A stage declaring a reserved name can never receive it: load_stage_fn()
    strips those keys, so the run completes, reports the config's label, and
    quietly uses the function's own default. That is how four stage4_*.yaml
    "model comparisons" all ran on one hardcoded model while reporting four.
    The tripwire that prevents it is otherwise only exercised by accident,
    on the day someone reintroduces the bug.
    """
    with pytest.raises(ValueError, match="reserves and strips"):
        harness.load_stage_fn(_config(function), "stage2_triage")


# --- Stage 4 micro vs macro F1 ---------------------------------------------

def _signal_post(post_id: str, gold: list[str]) -> dict:
    return {"post_id": post_id, "expected_signals": [{"signal": s} for s in gold]}


def _run_stage4(posts: list[dict], detect_fn, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    harness.score_stage4(posts, detect_fn, "test-model", 0.0, "vendor_test", None)


def test_micro_f1_is_reported_and_is_comparable_across_models(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """Regression guard for the 2026-09-09 macro-F1 artefact.

    all_signals comes from gold UNION predicted, so a model that hallucinates
    signal types is macro-averaged over MORE types - each one a 0%-F1 row. Two
    models scored on identical posts then get incomparable denominators, which
    is how GPT-4o Mini read as 100% over 4 types against Llama's 38% over 7.
    Micro pools every tp/fp/fn, so its denominator is fixed.

    Both models below catch the single gold signal; the second also invents
    three. Macro must diverge wildly, micro must stay honest.
    """
    posts = [_signal_post("p1", ["sold"])]

    with caplog.at_level(logging.INFO):
        _run_stage4(posts, lambda post, profile=None: [{"signal": "sold"}], tmp_path, monkeypatch)
    clean = caplog.text
    caplog.clear()

    with caplog.at_level(logging.INFO):
        _run_stage4(
            posts,
            lambda post, profile=None: [{"signal": s} for s in
                                        ("sold", "urgent", "clearance", "swap_deal")],
            tmp_path,
            monkeypatch,
        )
    noisy = caplog.text

    # The clean model: perfect on both averages.
    assert "Macro-averaged F1 across 1 signal type(s): 100%" in clean
    assert "F1=100%" in clean

    # The hallucinating model: macro collapses to 25% purely from the three
    # invented types, while micro reports the real 5-of-8 style picture.
    assert "Macro-averaged F1 across 4 signal type(s): 25%" in noisy
    assert "pooled tp=1 fp=3 fn=0" in noisy
    assert "3 signal type(s) predicted but never in gold" in noisy


def test_micro_f1_survives_a_model_that_predicts_nothing(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """A silent model must read as 0% recall, not divide-by-zero."""
    posts = [_signal_post("p1", ["sold"])]

    with caplog.at_level(logging.INFO):
        _run_stage4(posts, lambda post, profile=None: [], tmp_path, monkeypatch)

    assert "pooled tp=0 fp=0 fn=1" in caplog.text
    assert "R=0% F1=0%" in caplog.text
