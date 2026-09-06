"""
Pydantic schema + loader for experiment config YAML files.

Every experiment config in pipeline/config/experiments/*.yaml describes one
full pipeline run: which module/function implements each stage, and which
LiteLLM/OpenRouter model string that stage should call. This is what makes
the POC's hard requirement true - "every stage reads its model from a
config file... switching any stage to any model must be a one-line config
change with zero code changes" - a model swap is a YAML edit here, never a
code change in eval/harness.py or a pipeline/stages/*.py file.

load_experiment_config() is the single place that reads and validates these
files, replacing the duplicated json.loads(...) that used to live in both
eval/harness.py and stage1_profile.py before the JSON registry files were
migrated to this YAML + Pydantic system.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

DEFAULT_EXPERIMENT_PATH = Path("pipeline") / "config" / "experiments" / "default.yaml"


class StageConfig(BaseModel):
    """One stage's entry in an experiment config.

    extra="allow" because stages carry different extra keys beyond these
    (e.g. stage2_triage's text_model/vision_model/confidence_threshold)
    that get bound as kwargs into the stage function - see
    eval/harness.py::load_stage_fn(). "model" itself is just a LiteLLM
    model string (any provider LiteLLM/OpenRouter supports, e.g.
    "groq/llama-3.1-8b-instant" or "openrouter/qwen/qwen-2.5-7b-instruct:free")
    - not validated against a fixed list, since that list lives outside
    this repo's control.
    """

    model_config = ConfigDict(extra="allow")

    module: str
    function: str
    model: str = "unknown"
    cost_per_call_usd: float = 0.0
    note: str | None = None


class ExperimentConfig(BaseModel):
    """Validated shape of one experiment YAML file (pipeline/config/experiments/*.yaml).

    stage2_triage and stage3_extract are required - eval/harness.py always
    scores both. stage1_profile and stage4_signals are optional: Stage 1
    isn't scored by the harness at all (it's a once-per-account call made
    directly via pipeline/stages/stage1_profile.py), and Stage 4 is only
    scored when an experiment actually configures it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    stage1_profile: StageConfig | None = None
    stage2_triage: StageConfig
    stage3_extract: StageConfig
    stage4_signals: StageConfig | None = None


def load_experiment_config(path: Path | str = DEFAULT_EXPERIMENT_PATH) -> ExperimentConfig:
    """Loads and validates one experiment YAML file.

    A malformed entry (missing "module"/"function", a non-numeric cost, an
    unknown top-level key, etc.) fails here with a clear field-level
    Pydantic error instead of a bare KeyError/TypeError surfacing later
    inside load_stage_fn() or a stage function call.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return ExperimentConfig.model_validate(raw)
