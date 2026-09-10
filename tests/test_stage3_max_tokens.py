"""Stage 3 output-budget tests.

A multi-product catalog post needs one JSON object PER product in the response.
complete_structured()'s default cap is 1024 tokens, which Stage 3 did not
override - so a post with enough products produced TRUNCATED JSON, failed schema
validation, exhausted the repair retries, and fell back to the regex heuristic.
The run then reported a clean score over partly-heuristic output.

It hid for a week because vendor_autos_01 tops out at 3 products/post. It shows
on vendor_gadgets_01 (up to 15), model-independently: the same three posts fell
back on Gemini twice and on GPT-4o Mini (FINDINGS_BASELINE_2026-09.md).

Offline: no network, no .env, no golden set.
"""

from __future__ import annotations

import inspect

from pipeline.llm_client import complete_structured
from pipeline.stages import stage3_extract


def test_stage3_budget_exceeds_the_generic_client_default() -> None:
    """The bug in one line: Stage 3 inheriting the generic 1024 default."""
    client_default = inspect.signature(complete_structured).parameters["max_tokens"].default

    assert stage3_extract.DEFAULT_MAX_TOKENS > client_default, (
        "Stage 3 must raise the output budget above complete_structured()'s generic "
        "default, or multi-product posts truncate and silently fall back to regex."
    )


def test_budget_covers_the_largest_observed_post() -> None:
    """vendor_gadgets_01 carries a 15-product post; one extracted product
    serialises to ~94 tokens (measured 2026-09-09). Keep headroom over that -
    gadgets names and descriptions run longer than the autos sample it was
    measured from."""
    largest_observed_products = 15
    tokens_per_product = 94

    assert stage3_extract.DEFAULT_MAX_TOKENS >= largest_observed_products * tokens_per_product * 2


def test_max_tokens_is_bindable_from_experiment_yaml() -> None:
    """load_stage_fn() binds every non-reserved config key as a kwarg, so a
    `max_tokens:` in a stage3_extract block must reach the function. It must NOT
    be one of the reserved names, or it would be stripped and silently ignored -
    the 2026-09-07 bug shape."""
    params = inspect.signature(stage3_extract.extract_product).parameters

    assert "max_tokens" in params
    assert params["max_tokens"].default == stage3_extract.DEFAULT_MAX_TOKENS
    assert "max_tokens" not in {"module", "function", "cost_per_call_usd", "note", "model"}


def test_pass_a_actually_forwards_the_budget(monkeypatch) -> None:
    """Threading it into the signature is useless if the call site drops it -
    a silent failure, since the default would still produce plausible output."""
    seen = {}

    def fake_complete_structured(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here - we only need the kwargs")

    monkeypatch.setattr(stage3_extract, "complete_structured", fake_complete_structured)

    try:
        stage3_extract._pass_a("prompt", "some/model", None, None, "p1", max_tokens=7777)
    except RuntimeError:
        pass

    assert seen.get("max_tokens") == 7777
