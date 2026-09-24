"""Token-log reader tests.

report/token_log.csv is the only artifact that records what actually reached
litellm, so it is what scripts/verify_run.py gates a run on and what
scripts/run_pipeline.py reports spend from. A bad read here reports a
confident token total for the wrong run, or hides a degraded one.

These tests were previously in tests/test_run_pipeline.py; read_run_usage()
moved next to its writer (log_token_usage) in pipeline/llm_client.py so the
runner and the gate share one definition of the log's shape.

Offline: no network, no .env, no golden set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import llm_client
from pipeline.llm_client import read_run_usage

HEADER = (
    "run_id,timestamp,vendor_id,post_id,stage,model,"
    "prompt_tokens,completion_tokens,total_tokens,escalated,fallback_used,estimated_cost_usd\n"
)


def _log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    path = tmp_path / "token_log.csv"
    path.write_text(HEADER + body, encoding="utf-8")
    monkeypatch.setattr(llm_client, "TOKEN_LOG_PATH", path)
    return path


def test_read_run_usage_ignores_other_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The log is append-only and shared across every run ever made, so a
    run_id filter that leaks would silently inflate the reported spend."""
    _log(
        tmp_path,
        monkeypatch,
        "mine,t,v,p1,stage2_triage_pass_a,gemini/x,10,2,12,False,False,0.0\n"
        "theirs,t,v,p2,stage2_triage_pass_a,gemini/x,900,90,990,False,False,0.0\n",
    )

    usage = read_run_usage("mine")

    assert usage["calls"] == 1
    assert usage["total_tokens"] == 12
    assert usage["models"] == ["gemini/x"]


def test_read_run_usage_aggregates_per_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _log(
        tmp_path,
        monkeypatch,
        "r,t,v,p1,stage2_triage_pass_a,gemini/lite,10,1,11,False,False,0.0\n"
        "r,t,v,p2,stage2_triage_pass_a,gemini/lite,20,2,22,False,False,0.0\n"
        "r,t,v,p2,stage2_triage_pass_b,gemini/flash,100,5,105,True,False,0.0\n",
    )

    usage = read_run_usage("r")

    assert usage["by_stage"]["stage2_triage_pass_a"]["calls"] == 2
    assert usage["by_stage"]["stage2_triage_pass_a"]["total_tokens"] == 33
    assert usage["by_stage"]["stage2_triage_pass_b"]["calls"] == 1
    assert usage["calls"] == 3
    assert usage["total_tokens"] == 138
    assert usage["models"] == ["gemini/flash", "gemini/lite"]


def test_read_run_usage_returns_zero_for_an_unlogged_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero rows is the documented tell that a run lost its network or its
    credit rather than genuinely producing nothing (README.md).
    It must read as zero, never crash, so the summary can say so out loud."""
    _log(tmp_path, monkeypatch, "")

    usage = read_run_usage("never-ran")

    assert usage["calls"] == 0
    assert usage["total_tokens"] == 0
    assert usage["by_stage"] == {}


def test_read_run_usage_survives_a_missing_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_client, "TOKEN_LOG_PATH", tmp_path / "absent.csv")

    assert read_run_usage("r")["calls"] == 0


def test_read_run_usage_tolerates_blank_token_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fallback row can land with empty token columns; it still counts as a
    call rather than taking the whole summary down."""
    _log(tmp_path, monkeypatch, "r,t,v,p1,stage3_extract_pass_a,gemini/lite,,,,False,True,0.0\n")

    usage = read_run_usage("r")

    assert usage["calls"] == 1
    assert usage["total_tokens"] == 0


def test_read_run_usage_counts_regex_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Stage 3 LLM failure falls back to the regex heuristic and logs a
    zero-token row flagged fallback_used. Before that row existed the
    degradation was invisible in the token log, so the summary reported a
    clean run over partly-heuristic output."""
    _log(
        tmp_path,
        monkeypatch,
        "r,t,v,p1,stage3_extract_pass_a,gemini/lite,50,5,55,False,False,0.0\n"
        "r,t,v,p2,stage3_extract_fallback,gemini/lite,0,0,0,False,True,0.0\n",
    )

    usage = read_run_usage("r")

    assert usage["fallbacks"] == 1
    assert usage["calls"] == 2
    assert usage["total_tokens"] == 55


# --- real cost -------------------------------------------------------------

def test_a_priced_model_gets_a_real_cost() -> None:
    """Jev's published rate: $0.042 per 1M input tokens, output free."""
    cost = llm_client.estimate_cost_usd("jev-1.13.0", 1_000_000, 500_000)
    assert cost == pytest.approx(0.042), "output must not be charged for Jev"


def test_an_unknown_model_is_priced_zero_and_warned_about(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never guess a rate. A guessed rate produces a number that looks like
    evidence and is not, which is the failure this column already had once."""
    monkeypatch.setattr(llm_client, "_warned_unpriced", set())
    with caplog.at_level("WARNING"):
        cost = llm_client.estimate_cost_usd("some/unpriced-model", 1000, 1000)
    assert cost == 0.0
    assert "some/unpriced-model" in caplog.text


def test_the_unknown_model_warning_fires_once_per_process(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness run would otherwise emit one warning per post and bury it."""
    monkeypatch.setattr(llm_client, "_warned_unpriced", set())
    with caplog.at_level("WARNING"):
        for _ in range(5):
            llm_client.estimate_cost_usd("some/unpriced-model", 10, 10)
    assert caplog.text.count("no rate on file") == 1


def test_read_run_usage_reports_how_much_of_the_cost_is_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The column's history is mixed: every row written before MODEL_RATES
    existed is 0.0 regardless of what it cost, and old rows are deliberately
    not recomputed. priced_calls is what lets a caller say a total understates
    instead of presenting it as the whole bill."""
    _log(
        tmp_path,
        monkeypatch,
        "r,t,v,p1,stage2_triage_jev,jev-1.13.0,1000,20,1020,False,False,0.000042\n"
        "r,t,v,p2,stage3_extract_pass_a,gemini/lite,2000,100,2100,False,False,0.0\n",
    )

    usage = read_run_usage("r")

    assert usage["calls"] == 2
    assert usage["priced_calls"] == 1, "the unpriced row must not count as priced"
    assert usage["cost_usd"] == pytest.approx(0.000042)
    assert usage["by_stage"]["stage2_triage_jev"]["cost_usd"] == pytest.approx(0.000042)
    assert usage["by_stage"]["stage3_extract_pass_a"]["cost_usd"] == 0.0


def test_a_run_predating_real_cost_reports_zero_priced_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every historical run in report/token_log.csv looks like this. It must
    read as 'no cost data', not as 'this run was free'."""
    _log(
        tmp_path,
        monkeypatch,
        "old,t,v,p1,stage3_extract_pass_a,gemini/lite,2000,100,2100,False,False,0.0\n"
        "old,t,v,p2,stage3_extract_pass_b,gemini/flash,3000,200,3200,True,False,0.0\n",
    )

    usage = read_run_usage("old")

    assert usage["calls"] == 2
    assert usage["cost_usd"] == 0.0
    assert usage["priced_calls"] == 0
