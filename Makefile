# Instagram Catalog Pipeline - common commands.
# Usage: make <target> [VAR=value ...]
#
# Every target is a thin wrapper around the existing `uv run ...` scripts -
# no behavior lives here, just fewer flags to retype. Defaults match the
# current vendor_autos_01 / Gemini Cascade setup, so a bare `make <target>`
# reproduces today's exact commands; override variables for a second vendor.
#
# Requires GNU Make. Git Bash on Windows doesn't bundle it by default - if
# `make --version` fails, install it (e.g. `choco install make`) or run
# these under WSL.

ACCOUNT   ?= vendor_autos_01
TOKEN_ENV ?= IG_ACCESS_TOKEN
VENDOR    ?= ayodele.akinbohun
GOLDEN    ?= eval/golden/$(ACCOUNT).json
CONFIG    ?= pipeline/config/experiments/default.yaml
# Empty by default: run_stage6.py then picks report/<VENDOR>/changes_<sync-timestamp>.json,
# which is per-vendor and per-run. The old fixed report/changes.json collided on both.
CHANGES   ?=
LABEL     ?= Gemini Cascade
# eval/harness.py writes predictions to report/<golden file stem>/, so these
# follow ACCOUNT automatically; only the model half needs overriding per run.
STAGE2    ?= report/$(ACCOUNT)/stage2_Gemini_Cascade_Google_AI_Studio_predictions.json
STAGE3    ?= report/$(ACCOUNT)/stage3_gemini-3.5-flash-lite_Google_AI_Studio_predictions.json
STAGE4    ?= report/$(ACCOUNT)/stage4_gemini_gemini-3.5-flash-lite_predictions.json

# Live-demo account. Separate from ACCOUNT/TOKEN_ENV so the demo targets below
# take no arguments at all - one less thing to mistype in front of an audience.
DEMO_ACCOUNT   ?= vendor_demo_01
DEMO_TOKEN_ENV ?= IG_ACCESS_TOKEN_DEMO

.DEFAULT_GOAL := help
.PHONY: help ingest golden update-golden harness pipeline demo-ingest demo-pipeline stage5 verify snapshot sync simulate-sync dvc-setup data-push data-pull data-status test check-secrets install-hooks

help:
	@echo "Targets (each wraps an existing uv run script - see README for the full workflow):"
	@echo "  ingest          uv run ingest/ingest.py \$$(ACCOUNT) --token-env \$$(TOKEN_ENV)"
	@echo "  golden          uv run eval/make_golden_skeleton.py \$$(ACCOUNT)"
	@echo "  update-golden   uv run eval/update_golden_skeleton.py \$$(ACCOUNT)"
	@echo "  harness         uv run eval/harness.py --golden \$$(GOLDEN) --config \$$(CONFIG)"
	@echo "  pipeline        uv run scripts/run_pipeline.py --account \$$(ACCOUNT) --config \$$(CONFIG)  [LIMIT=N caps posts; INGEST=1 pulls a fresh dump first; LIVE=1 traces each stage as it finishes; FORCE_PROFILE=1 re-runs Stage 1]"
	@echo "  stage5          uv run scripts/run_stage5.py --golden \$$(GOLDEN) --stage2 \$$(STAGE2) --stage3 \$$(STAGE3) --stage4 \$$(STAGE4) --label \"\$$(LABEL)\"  [APPEND=1 adds --append-findings]"
	@echo "  verify          uv run scripts/verify_run.py --run-id \$$(RUN_ID) [--expect-model \$$(EXPECT)] [--posts \$$(POSTS)] [--predictions \$$(PREDS)]   (RUN_ID= alone lists the log's run_ids)"
	@echo "  snapshot        uv run scripts/run_build_snapshot.py --vendor-handle \$$(VENDOR) --account \$$(ACCOUNT)"
	@echo "                  (baseline from the latest raw dump; pass GOLDEN= for the legacy golden-set path)"
	@echo "  sync            uv run scripts/run_stage6.py --vendor-handle \$$(VENDOR) --golden \$$(GOLDEN) --token-env \$$(TOKEN_ENV)"
	@echo "                  (changes default to report/\$$(VENDOR)/changes_<sync-timestamp>.json; CHANGES= overrides)"
	@echo "  simulate-sync   uv run scripts/simulate_stage6.py"
	@echo ""
	@echo "Live demo (no arguments - account and token are baked in):"
	@echo "  demo-ingest     Stage 0 for \$$(DEMO_ACCOUNT): pull the feed, per-post trace, summary"
	@echo "  demo-pipeline   Stages 1-5 for \$$(DEMO_ACCOUNT), --live --force-profile"
	@echo ""
	@echo "Checks (offline - these do NOT replace the harness, see CLAUDE.md):"
	@echo "  test            uv run pytest        (credential handling, harness error isolation, stage 5 routing)"
	@echo "  check-secrets   uv run scripts/scan_secrets.py --all"
	@echo "  install-hooks   install the secret scan as .git/hooks/pre-commit"
	@echo ""
	@echo "Data (DVC -> Cloudflare R2; tracks data/snapshots and report - golden sets and runs are in git):"
	@echo "  dvc-setup       one-time: read R2_* from .env, configure the remote"
	@echo "  data-push       upload local data to R2"
	@echo "  data-pull       download data from R2 (use on a fresh clone)"
	@echo "  data-status     show what differs between local, cache, and R2"
	@echo ""
	@echo "Variables (override with VAR=value): ACCOUNT TOKEN_ENV VENDOR GOLDEN GOLDEN_SNAPSHOT CONFIG CHANGES LABEL STAGE2 STAGE3 STAGE4 APPEND LIMIT INGEST LIVE FORCE_PROFILE RUN_ID EXPECT POSTS PREDS DEMO_ACCOUNT DEMO_TOKEN_ENV"
	@echo "Current defaults: ACCOUNT=$(ACCOUNT) TOKEN_ENV=$(TOKEN_ENV) VENDOR=$(VENDOR) GOLDEN=$(GOLDEN) CONFIG=$(CONFIG)"
	@echo "                  DEMO_ACCOUNT=$(DEMO_ACCOUNT) DEMO_TOKEN_ENV=$(DEMO_TOKEN_ENV)"

ingest:
	uv run ingest/ingest.py $(ACCOUNT) --token-env $(TOKEN_ENV)

golden:
	uv run eval/make_golden_skeleton.py $(ACCOUNT)

update-golden:
	uv run eval/update_golden_skeleton.py $(ACCOUNT)

harness:
	uv run eval/harness.py --golden $(GOLDEN) --config $(CONFIG)

# Chained Stages 1-5 on live predictions, no golden set - emits
# runs/<ACCOUNT>/catalog.json. Reuses the newest existing raw dump unless
# INGEST=1, so a bare run makes no Instagram API calls.
pipeline:
	uv run scripts/run_pipeline.py --account $(ACCOUNT) --config $(CONFIG) $(if $(LIMIT),--limit $(LIMIT),) $(if $(INGEST),--ingest --token-env $(TOKEN_ENV),) $(if $(LIVE),--live,) $(if $(FORCE_PROFILE),--force-profile,)

# --- Live demo ------------------------------------------------------------
# Zero-argument wrappers for the onboarding walkthrough. Same scripts as
# `ingest` and `pipeline` above, with the demo account's arguments already
# filled in.
#
# --force-profile is deliberate: Stage 1 caches to runs/<account>/profile.json,
# so without it a rehearsed run shows the cached profile instantly and makes no
# call. The target should behave identically however many times it is run.

demo-ingest:
	uv run ingest/ingest.py $(DEMO_ACCOUNT) --token-env $(DEMO_TOKEN_ENV)

demo-pipeline:
	uv run scripts/run_pipeline.py --account $(DEMO_ACCOUNT) --live --force-profile

stage5:
	uv run scripts/run_stage5.py --golden $(GOLDEN) --stage2 $(STAGE2) --stage3 $(STAGE3) --stage4 $(STAGE4) --label "$(LABEL)" $(if $(APPEND),--append-findings,)

# Gate a run's validity before publishing its numbers: rows logged at all,
# zero regex fallbacks, only the configured models called, call count in a
# plausible band, and - from PREDS, not the log - zero errored posts. That
# last one cannot come from the log: an errored post never reached litellm, so
# it writes no row (README.md). Pass all three stage prediction
# files. Exits non-zero, so `make harness ... && make verify RUN_ID=...` fails
# the pair. With no RUN_ID, lists the run_ids present in the log.
verify:
	uv run scripts/verify_run.py $(if $(RUN_ID),--run-id $(RUN_ID),--list) $(foreach m,$(EXPECT),--expect-model $(m)) $(if $(POSTS),--posts $(POSTS),) $(foreach p,$(PREDS),--predictions $(p))

# Builds from the latest raw dump for ACCOUNT - the baseline is then the same
# live feed the sync re-fetches. GOLDEN_SNAPSHOT=1 uses the legacy golden-set
# path instead, which re-discovers posts a live sync already had.
snapshot:
	uv run scripts/run_build_snapshot.py --vendor-handle $(VENDOR) $(if $(GOLDEN_SNAPSHOT),--golden $(GOLDEN),--account $(ACCOUNT))

sync:
	uv run scripts/run_stage6.py --vendor-handle $(VENDOR) --golden $(GOLDEN) $(if $(CHANGES),--changes $(CHANGES),) --token-env $(TOKEN_ENV)

simulate-sync:
	uv run scripts/simulate_stage6.py --vendor-handle $(VENDOR) --golden $(GOLDEN)

# --- Offline checks -------------------------------------------------------
# eval/harness.py remains the verification mechanism for model quality; these
# cover only what it structurally cannot (see tests/conftest.py).

test:
	uv run pytest

check-secrets:
	uv run scripts/scan_secrets.py --all

install-hooks:
	@printf '#!/bin/sh\nexec uv run scripts/scan_secrets.py\n' > .git/hooks/pre-commit
	@chmod +x .git/hooks/pre-commit
	@echo "Installed .git/hooks/pre-commit -> scripts/scan_secrets.py"

# --- Data versioning (DVC -> Cloudflare R2) -------------------------------
# data/snapshots and report are DVC-tracked, not in git (see .gitignore); the
# golden sets and raw dumps are committed to git instead, so a clone can score
# without R2 access. Each DVC path has a committed *.dvc pointer, so
# `git checkout <commit> && make data-pull` restores the exact data that commit
# was produced against.
#
# dvc-setup is one-time (or after credentials rotate); it reads R2_ACCOUNT_ID,
# R2_BUCKET, R2_ACCESS_KEY_ID, and R2_SECRET_ACCESS_KEY from .env. After it
# runs, plain `uv run dvc push/pull` work too - these are just shorthand.

dvc-setup:
	uv run scripts/setup_dvc_remote.py

data-push:
	uv run dvc push

data-pull:
	uv run dvc pull

data-status:
	uv run dvc status --cloud
