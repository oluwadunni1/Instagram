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
CHANGES   ?= report/changes.json
LABEL     ?= Gemini Cascade
STAGE2    ?= report/stage2_Gemini_Cascade_Google_AI_Studio_predictions.json
STAGE3    ?= report/stage3_gemini-3.5-flash-lite_Google_AI_Studio_predictions.json
STAGE4    ?= report/stage4_gemini_gemini-3.5-flash-lite_predictions.json

.DEFAULT_GOAL := help
.PHONY: help ingest golden update-golden harness stage5 snapshot sync simulate-sync

help:
	@echo "Targets (each wraps an existing uv run script - see README for the full workflow):"
	@echo "  ingest          uv run ingest/ingest.py \$$(ACCOUNT) --token-env \$$(TOKEN_ENV)"
	@echo "  golden          uv run eval/make_golden_skeleton.py \$$(ACCOUNT)"
	@echo "  update-golden   uv run eval/update_golden_skeleton.py \$$(ACCOUNT)"
	@echo "  harness         uv run eval/harness.py --golden \$$(GOLDEN) --config \$$(CONFIG)"
	@echo "  stage5          uv run scripts/run_stage5.py --golden \$$(GOLDEN) --stage2 \$$(STAGE2) --stage3 \$$(STAGE3) --stage4 \$$(STAGE4) --label \"\$$(LABEL)\"  [APPEND=1 adds --append-findings]"
	@echo "  snapshot        uv run scripts/run_build_snapshot.py --vendor-handle \$$(VENDOR) --golden \$$(GOLDEN)"
	@echo "  sync            uv run scripts/run_stage6.py --vendor-handle \$$(VENDOR) --golden \$$(GOLDEN) --changes \$$(CHANGES) --token-env \$$(TOKEN_ENV)"
	@echo "  simulate-sync   uv run scripts/simulate_stage6.py"
	@echo ""
	@echo "Variables (override with VAR=value): ACCOUNT TOKEN_ENV VENDOR GOLDEN CONFIG CHANGES LABEL STAGE2 STAGE3 STAGE4 APPEND"
	@echo "Current defaults: ACCOUNT=$(ACCOUNT) TOKEN_ENV=$(TOKEN_ENV) VENDOR=$(VENDOR) GOLDEN=$(GOLDEN) CONFIG=$(CONFIG)"

ingest:
	uv run ingest/ingest.py $(ACCOUNT) --token-env $(TOKEN_ENV)

golden:
	uv run eval/make_golden_skeleton.py $(ACCOUNT)

update-golden:
	uv run eval/update_golden_skeleton.py $(ACCOUNT)

harness:
	uv run eval/harness.py --golden $(GOLDEN) --config $(CONFIG)

stage5:
	uv run scripts/run_stage5.py --golden $(GOLDEN) --stage2 $(STAGE2) --stage3 $(STAGE3) --stage4 $(STAGE4) --label "$(LABEL)" $(if $(APPEND),--append-findings,)

snapshot:
	uv run scripts/run_build_snapshot.py --vendor-handle $(VENDOR) --golden $(GOLDEN)

sync:
	uv run scripts/run_stage6.py --vendor-handle $(VENDOR) --golden $(GOLDEN) --changes $(CHANGES) --token-env $(TOKEN_ENV)

simulate-sync:
	uv run scripts/simulate_stage6.py
