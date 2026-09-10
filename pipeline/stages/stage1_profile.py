"""
Stage 1 - Account profiling (brief section 5).

ONE call per account, ever - not per post. Reads the raw dump's bio
+ a sample of recent captions + posting frequency, produces a small
profile that gets injected as context into every downstream stage
(Stage 2 triage, Stage 3 extraction, Stage 4 signals).

Caches its result to runs/<account>/profile.json so re-running the
pipeline doesn't silently re-trigger this call every time - this
stage should run once per account, not once per pipeline test.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from pipeline.config.experiment_schema import DEFAULT_EXPERIMENT_PATH, load_experiment_config
from pipeline.exceptions import MissingRawDumpError
from pipeline.llm_client import complete_structured
from pipeline.types import Post

logger = logging.getLogger(__name__)

SAMPLE_CAPTION_COUNT = 10


class AccountProfile(BaseModel):
    business_category: Literal[
        "fashion", "food", "gadgets", "beauty", "services", "automotive", "mixed"
    ]
    # NOTE: "automotive" is an extension beyond the brief's base category
    # list (fashion/food/gadgets/beauty/services/mixed) - added because
    # our actual test account needed it. Document in findings.
    seller_style: Literal["catalog_poster", "lifestyle_mixer"]
    language_mix: Literal["english", "pidgin", "yoruba", "mixed"]
    pricing_behavior: Literal["captions", "dm_for_price", "price_on_image", "mixed"]
    vendor_username: str | None = None
    # Instagram username of the account owner. Stored here so downstream
    # stages can tell the LLM whose comments carry vendor authority without
    # needing a per-comment flag in the raw data. None = unknown/not set yet.


SYSTEM_PROMPT = """You are analyzing an Instagram business account's posting \
patterns to build a profile that will guide how other systems process this \
vendor's posts. Base every judgment ONLY on the bio and captions provided - \
do not guess or assume anything not evidenced in the text.

Return ONLY a JSON object with exactly these fields:
- business_category: one of fashion, food, gadgets, beauty, services, automotive, mixed
- seller_style: "catalog_poster" (posts are primarily structured product listings) \
or "lifestyle_mixer" (mixes product posts with lifestyle/personal content)
- language_mix: "english", "pidgin", "yoruba", or "mixed" - based on the actual \
language used across captions
- pricing_behavior: "captions" (prices typically stated in caption text), \
"dm_for_price" (vendor typically asks buyers to DM for pricing), \
"price_on_image" (prices typically overlaid on photos, inferred if captions \
rarely have prices but posts are clearly product listings), or "mixed\""""


def build_profile_input(dump: dict) -> dict:
    """Assembles Stage 1's input from an already-ingested raw dump -
    no new API call needed, this data is already cached from ingest.py.

    Args:
        dump: A raw dump dict as written by ingest.py (has "profile" and
            "media" keys).

    Returns:
        dict with "biography", "sample_captions", and "posting_frequency" -
        exactly what generate_profile() needs to build its user prompt.
    """
    profile = dump.get("profile", {})
    media: list[Post] = dump.get("media", [])

    captions = [m.get("caption", "") for m in media[:SAMPLE_CAPTION_COUNT] if m.get("caption")]

    timestamps = sorted(m["timestamp"] for m in media if m.get("timestamp"))
    posting_frequency = "unknown"
    if len(timestamps) >= 2:
        # Rough density signal, not a precise interval - good enough for
        # a "how often does this vendor post" impression, not a schedule.
        posting_frequency = f"{len(timestamps)} posts spanning {timestamps[0]} to {timestamps[-1]}"

    return {
        "biography": profile.get("biography", ""),
        "sample_captions": captions,
        "posting_frequency": posting_frequency,
    }


def generate_profile(dump: dict, model: str, account_label: str) -> AccountProfile:
    """Runs the single Stage 1 LLM call and attaches vendor_username.

    Args:
        dump: A raw dump dict as written by ingest.py.
        model: litellm model string to use for the profiling call.
        account_label: Used as the token log's vendor_id and to build a
            unique run_id for this call (see log_token_usage() in
            pipeline/llm_client.py).

    Returns:
        A validated AccountProfile.
    """
    profile_input = build_profile_input(dump)
    user_prompt = (
        f"Bio: {profile_input['biography']}\n\n"
        f"Posting frequency: {profile_input['posting_frequency']}\n\n"
        f"Sample captions:\n" + "\n---\n".join(profile_input["sample_captions"])
    )
    run_id = (
        f"stage1_{account_label}_{model.replace('/', '_').replace(':', '_')}"
        f"_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    )
    profile = complete_structured(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema=AccountProfile,
        stage_name="stage1_profile",
        run_id=run_id,
        vendor_id=account_label,
        post_id="",  # account-level call, not tied to one post
    )
    # vendor_username is plain account metadata from the dump - not content
    # the LLM can infer, so we read it here rather than asking the model for
    # it (which would require changing the prompt and the schema's required
    # fields, and the model could still get it wrong).
    profile.vendor_username = dump.get("profile", {}).get("username") or None
    return profile


def _default_model_from_config() -> str:
    """Falls back to pipeline/config/experiments/default.yaml's
    stage1_profile.model when no model is passed explicitly - this is what
    makes default.yaml the actual source of truth for Stage 1 too, not
    just Stage 2/3."""
    config = load_experiment_config(DEFAULT_EXPERIMENT_PATH)
    assert config.stage1_profile is not None, "default.yaml must configure stage1_profile"
    return config.stage1_profile.model


def get_or_create_profile(
    account_label: str, profile_model: str | None = None, force_refresh: bool = False
) -> AccountProfile:
    """Entry point for downstream stages. Loads the cached profile if
    it exists; only calls the model if it doesn't, or force_refresh=True.
    This is what makes Stage 1 genuinely 'once per account' in practice,
    not just in intent.

    profile_model=None (the default) reads
    pipeline/config/experiments/default.yaml's stage1_profile.model - pass a
    model string explicitly only when deliberately testing something OTHER
    than default.yaml's current configured choice (e.g. comparing two
    candidates).

    Named `profile_model`, NOT `model`, to satisfy the invariant enforced by
    eval/harness.py::load_stage_fn(): `model` is a reserved YAML key holding a
    display label, and a stage parameter of that name can never be bound from
    config. Stage 1 resolves its own model from config so it was never
    actually broken by this, unlike Stage 4 was - see FINDINGS.md 2026-09-07 -
    but the naming is now uniform across every stage.

    Raises:
        MissingRawDumpError: If no raw dump exists yet for account_label.
    """
    model = profile_model
    if model is None:
        model = _default_model_from_config()

    profile_path = Path("runs") / account_label / "profile.json"

    if profile_path.exists() and not force_refresh:
        cached = AccountProfile.model_validate(json.loads(profile_path.read_text(encoding="utf-8")))
        if cached.vendor_username is None:
            # Backfill vendor_username on cached profiles that predate this
            # field - avoids a wasted LLM call just to populate one metadata
            # field the model doesn't even produce.
            raw_dir = Path("runs") / account_label / "raw"
            dumps = sorted(raw_dir.glob("dump_*.json"))
            if dumps:
                dump = json.loads(dumps[-1].read_text(encoding="utf-8"))
                cached.vendor_username = dump.get("profile", {}).get("username") or None
                profile_path.write_text(cached.model_dump_json(indent=2), encoding="utf-8")
        return cached

    raw_dir = Path("runs") / account_label / "raw"
    dumps = sorted(raw_dir.glob("dump_*.json"))
    if not dumps:
        raise MissingRawDumpError(f"No raw dumps found in {raw_dir}. Run ingest.py first.")
    dump = json.loads(dumps[-1].read_text(encoding="utf-8"))

    profile = generate_profile(dump, model, account_label)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    return profile


if __name__ == "__main__":
    from pipeline.exceptions import PipelineError
    from pipeline.logging_config import configure_logging

    configure_logging()

    if len(sys.argv) not in (2, 3):
        logger.info("Usage: uv run pipeline/stages/stage1_profile.py <account_label> [model]")
        logger.info("  If [model] is omitted, uses pipeline/config/experiments/default.yaml's stage1_profile.model")
        logger.info("Example: uv run pipeline/stages/stage1_profile.py vendor_autos_01 gemini/gemini-3.5-flash-lite")
        sys.exit(1)
    account = sys.argv[1]
    cli_model = sys.argv[2] if len(sys.argv) == 3 else None
    try:
        result = get_or_create_profile(account, profile_model=cli_model, force_refresh=True)
    except PipelineError as exc:
        sys.exit(str(exc))
    print(result.model_dump_json(indent=2))