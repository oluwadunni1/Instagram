"""Run-validity gate tests.

scripts/verify_run.py is what makes a published number auditable, so a gate
that passes a bad run is worse than no gate at all - it launders exactly the
failures it exists to catch. Each test below is one of those failures, all of
which have actually happened on this project:

  - a run that never reached a model but printed clean scores anyway
    (README.md)
  - a throttled run whose output is partly regex heuristic
  - a "four model comparison" that ran one model (README.md)

Offline: no network, no .env, no golden set.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from pipeline import llm_client

REPO_ROOT = Path(__file__).resolve().parent.parent

# scripts/ is not a package - load by path, same as tests/test_run_pipeline.py.
_spec = importlib.util.spec_from_file_location(
    "verify_run", REPO_ROOT / "scripts" / "verify_run.py"
)
verify_run = importlib.util.module_from_spec(_spec)
sys.modules["verify_run"] = verify_run
_spec.loader.exec_module(verify_run)

HEADER = (
    "run_id,timestamp,vendor_id,post_id,stage,model,"
    "prompt_tokens,completion_tokens,total_tokens,escalated,fallback_used,estimated_cost_usd\n"
)


def _log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    path = tmp_path / "token_log.csv"
    path.write_text(HEADER + body, encoding="utf-8")
    monkeypatch.setattr(llm_client, "TOKEN_LOG_PATH", path)


def _rows(run_id: str, n: int, model: str = "gemini/lite", fallback: str = "False") -> str:
    return "".join(
        f"{run_id},t,v,p{i},stage2_triage_pass_a,{model},10,1,11,False,{fallback},0.0\n"
        for i in range(n)
    )


def test_zero_rows_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure the gate exists for: a run that printed scores without ever
    calling a model. A clean-looking 0% and a dead network are otherwise
    indistinguishable."""
    _log(tmp_path, monkeypatch, _rows("someone-else", 5))

    passed, lines = verify_run.verify("mine")

    assert passed is False
    assert "ZERO rows" in "\n".join(lines)


def test_a_clean_run_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _log(tmp_path, monkeypatch, _rows("mine", 30))

    passed, lines = verify_run.verify(
        "mine", expect_models=["gemini/lite"], posts=30
    )

    assert passed is True
    assert "FAIL" not in "\n".join(lines)


def test_a_single_fallback_row_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One degraded post means the run's output is a mixture of model and
    regex results. The scores still print; only this says not to trust them."""
    _log(tmp_path, monkeypatch, _rows("mine", 29) + _rows("mine", 1, fallback="True"))

    passed, lines = verify_run.verify("mine")

    assert passed is False
    assert "regex fallback" in "\n".join(lines)


def test_an_unexpected_model_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for README.md: four stage4_*.yaml
    comparisons reported four models while running one. The config's `model:`
    key is a display label and cannot detect this - only the log can."""
    _log(tmp_path, monkeypatch, _rows("mine", 30, model="gemini/lite"))

    passed, lines = verify_run.verify(
        "mine", expect_models=["openrouter/meta-llama/llama-3.1-8b-instruct"]
    )

    assert passed is False
    assert "UNEXPECTED: gemini/lite" in "\n".join(lines)


def test_a_configured_model_that_was_never_called_is_reported_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cascade whose vision Pass B never triggered legitimately never calls
    its vision_model. That is a fact worth printing, not a failure - failing it
    would push runs toward escalating for the sake of the gate."""
    _log(tmp_path, monkeypatch, _rows("mine", 30, model="gemini/lite"))

    passed, lines = verify_run.verify(
        "mine", expect_models=["gemini/lite", "gemini/flash"]
    )

    assert passed is True
    assert "never called: gemini/flash" in "\n".join(lines)


def test_call_count_far_below_the_post_count_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Half the posts silently skipped still produces a plausible-looking
    accuracy figure over the half that ran."""
    _log(tmp_path, monkeypatch, _rows("mine", 4))

    passed, lines = verify_run.verify("mine", posts=30)

    assert passed is False
    assert "call count in range" in "\n".join(lines)


def test_checks_are_skipped_not_silently_passed_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --expect-model/--posts the gate still passes, but must say
    which checks it did not run - a PASS that hid its own gaps would be the
    same class of false confidence the gate is here to remove."""
    _log(tmp_path, monkeypatch, _rows("mine", 30))

    passed, lines = verify_run.verify("mine")
    text = "\n".join(lines)

    assert passed is True
    assert "[skip] models match config" in text
    assert "[skip] call count in range" in text


def test_list_run_ids_counts_rows_per_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _log(tmp_path, monkeypatch, _rows("a", 2) + _rows("b", 3))

    assert dict(verify_run.list_run_ids()) == {"a": 2, "b": 3}


def test_list_run_ids_survives_a_missing_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_client, "TOKEN_LOG_PATH", tmp_path / "absent.csv")

    assert verify_run.list_run_ids() == []


# --- errored posts (check 5) -----------------------------------------------
# Not log-derived: a post whose stage call raised never reached litellm, so it
# writes no token-log row and every other check here is blind to it. This is
# the gap the first gated run walked through - two 403'd posts, PASS on all
# four log-derived checks (README.md).

def _preds(tmp_path: Path, name: str, entries: list[dict]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def test_an_errored_post_fails_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real shape: an expired CDN URL 403s, the vision call raises, the
    post is scored as a miss and the token log never hears about it."""
    _log(tmp_path, monkeypatch, _rows("mine", 30))
    preds = _preds(tmp_path, "stage2.json", [
        {"post_id": "p1", "gold": "product_listing", "predicted": "product_listing"},
        {"post_id": "p2", "gold": "product_listing",
         "error": "litellm.BadRequestError: Unable to fetch image from URL. Status code: 403"},
    ])

    passed, lines = verify_run.verify("mine", predictions=[preds])
    text = "\n".join(lines)

    assert passed is False
    assert "1 errored of 2" in text
    assert "403" in text, "the gate must show why, not just that"


def test_clean_predictions_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _log(tmp_path, monkeypatch, _rows("mine", 30))
    preds = _preds(tmp_path, "stage2.json", [{"post_id": "p1", "predicted": "x"}])

    passed, lines = verify_run.verify("mine", predictions=[preds])

    assert passed is True
    assert "0 errored of 1" in "\n".join(lines)


def test_errors_are_counted_across_every_stage_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 3 hides its failures behind the regex fallback, so an error can
    surface in one stage's file and not another's. All three get counted."""
    _log(tmp_path, monkeypatch, _rows("mine", 30))
    s2 = _preds(tmp_path, "stage2.json", [{"post_id": "p1", "error": "boom"}])
    s4 = _preds(tmp_path, "stage4.json", [{"post_id": "p9", "error": "bang"}])

    passed, lines = verify_run.verify("mine", predictions=[s2, s4])

    assert passed is False
    assert "2 errored of 2" in "\n".join(lines)


def test_an_unreadable_predictions_file_fails_rather_than_passing_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing or truncated predictions file must not read as "no errors" -
    that would turn a broken run into a PASS, which is the whole failure mode
    this gate exists to prevent."""
    _log(tmp_path, monkeypatch, _rows("mine", 30))

    passed, lines = verify_run.verify("mine", predictions=[tmp_path / "absent.json"])

    assert passed is False
    assert "unreadable" in "\n".join(lines)


def test_the_check_is_skipped_not_passed_without_predictions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _log(tmp_path, monkeypatch, _rows("mine", 30))

    passed, lines = verify_run.verify("mine")

    assert passed is True
    assert "[skip] no errored posts" in "\n".join(lines)


# --- chained runs write a catalog, not prediction lists --------------------

def test_a_catalog_json_is_accepted_and_its_errors_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scripts/run_pipeline.py writes one catalog.json instead of per-stage
    prediction files, so --predictions had nothing to point at and check 5
    silently skipped - weaker verification on the path that mirrors production.
    The chained autos run of 2026-09-10 carried an errored stage call the gate
    could not see."""
    _log(tmp_path, monkeypatch, _rows("mine", 40))
    catalog = _preds(tmp_path, "catalog.json", [])  # placeholder, overwritten
    catalog.write_text(json.dumps({
        "run_id": "mine",
        "items": [{"post_id": f"p{i}"} for i in range(40)],
        "errors": [{"post_id": "p7", "stage": "stage2",
                    "error": "litellm.BadRequestError: Unable to fetch image from URL. 403"}],
    }), encoding="utf-8")

    passed, lines = verify_run.verify("mine", predictions=[catalog])
    text = "\n".join(lines)

    assert passed is False
    assert "1 errored of 40" in text
    assert "p7 [stage2]" in text, "the gate must name the post and the stage"
    assert "403" in text


def test_a_clean_catalog_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _log(tmp_path, monkeypatch, _rows("mine", 40))
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({
        "items": [{"post_id": f"p{i}"} for i in range(40)], "errors": [],
    }), encoding="utf-8")

    passed, lines = verify_run.verify("mine", predictions=[catalog])

    assert passed is True
    assert "0 errored of 40" in "\n".join(lines)


def test_prediction_lists_and_a_catalog_can_be_mixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both shapes in one invocation must total correctly - the dict branch
    must not swallow the list branch."""
    _log(tmp_path, monkeypatch, _rows("mine", 40))
    preds = _preds(tmp_path, "stage2.json", [{"post_id": "a", "error": "boom"}])
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({
        "items": [{"post_id": "b"}], "errors": [{"post_id": "b", "stage": "stage3", "error": "bang"}],
    }), encoding="utf-8")

    passed, lines = verify_run.verify("mine", predictions=[preds, catalog])

    assert passed is False
    assert "2 errored of 2" in "\n".join(lines)
