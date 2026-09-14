# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A POC cascade AI pipeline that turns a vendor's Instagram feed into a structured product
catalog plus attention flags. The full spec is `poc/POC-BRIEF_INSTAGRAM_CATALOG_PIPELINE.pdf`;
inline comments across the codebase cite it as "brief section N".

Python 3.11+, managed with `uv`. Every command below assumes `uv run`.

## Commands

`make` (GNU Make) wraps the common invocations; a bare `make` prints the target list with
current variable defaults. Git Bash on Windows does not bundle `make` — if it is missing,
run the underlying `uv run ...` command shown by `make help`.

```bash
make ingest ACCOUNT=vendor_gadgets_01 TOKEN_ENV=IG_ACCESS_TOKEN_GADGETS
make golden ACCOUNT=vendor_gadgets_01          # labeling skeleton; refuses to overwrite
make update-golden ACCOUNT=vendor_gadgets_01   # append only newly-pulled posts
make harness GOLDEN=eval/golden/vendor_gadgets_01.json CONFIG=pipeline/config/experiments/default.yaml
make pipeline ACCOUNT=vendor_autos_01 [LIMIT=N] [INGEST=1] [LIVE=1] [FORCE_PROFILE=1]
make stage5  GOLDEN=... STAGE2=... STAGE3=... STAGE4=... LABEL="..." [APPEND=1]
make snapshot VENDOR=<ig_username> GOLDEN=...  # Stage 6 baseline
make sync     VENDOR=<ig_username> GOLDEN=... CHANGES=report/changes_x.json TOKEN_ENV=...
make simulate-sync                             # offline Stage 6, no API, no snapshot writes
make data-pull / data-push / data-status       # DVC <-> Cloudflare R2
make verify RUN_ID=<id> [EXPECT=<model>] [POSTS=N]  # gate a run before publishing its numbers
make test / check-secrets / install-hooks      # offline checks (see below)
```

**`make verify` gates a run's validity** (`scripts/verify_run.py`) against
`report/token_log.csv` — the only artifact that records what actually reached litellm rather
than what the config claimed. It fails on: zero rows logged (the run never reached a model),
any `fallback_used=True` row (part of the output is regex heuristic, not model output), a
model string outside `--expect-model`, or a call count outside a band derived from `--posts`.
Exits non-zero, so `make harness … && make verify RUN_ID=…` fails the pair. Bare
`make verify` lists the log's run_ids. **No number goes into `FINDINGS.md` or a stakeholder
artifact from a run that has not passed this gate.**

**`eval/harness.py` is the verification mechanism for anything model-related**: it scores each
stage against the hand-labeled golden set and writes per-post predictions. To verify a change
did not regress anything, re-run the harness (or `make stage5`, which is zero-LLM and runs off
cached predictions) and compare against the numbers recorded in `FINDINGS.md`. `make stage5`
with no overrides currently prints 18/30 auto_import, 9/30 needs_attention, 3/30 auto_exclude
for `vendor_autos_01` — this file previously recorded 17/10/3, which no longer reproduces and
predates the current working tree; treat the recorded figure as the thing to re-derive, not as
a target to hit.

**`tests/` is deliberately narrow** (`make test`, pytest, offline). It does not test model
quality — that is the harness's job and always will be. It covers only what the harness
structurally cannot: credential handling (`test_credentials.py`), the harness's own per-post
error isolation and `load_stage_fn()`'s reserved-parameter tripwire (`test_harness.py`),
deterministic Stage 5 routing (`test_stage5_reconcile.py`), the token-log reader every cost
figure and the run gate depend on (`test_token_log.py`, `test_verify_run.py`), and the
pre-commit secret scanner (`test_scan_secrets.py`). No test may make a network call, read
`.env`, or read a golden set.
Adding a test that needs a model response is a sign it belongs in the harness instead.

**`make install-hooks`** installs `scripts/scan_secrets.py` as a git pre-commit hook: it blocks
committing `.env`, `.dvc/config.local`, or anything under the two DVC-tracked data paths
(`data/snapshots/`, `report/`), and scans staged content for credential shapes. `.env` reached a
commit once already (`03e5554`) — `.gitignore` alone did not stop it. The value that time was an
empty placeholder, and the blob was purged from history before the repo was published, but the
staging is what the hook exists to prevent. Waive a false positive with a
`pragma: allowlist secret` comment on the line.

To score a single stage in isolation, point `--config` at an experiment YAML that swaps only
that stage; `--golden` restricts scoring to one vendor.

`LOG_LEVEL=DEBUG` surfaces raw model responses from `pipeline/llm_client.py`. On Windows,
prefix with `PYTHONIOENCODING=utf-8` when printing captions — they contain emoji that cp1252
cannot encode.

## Architecture

### The cascade is the core idea

Stages 2 and 3 both implement a two-pass cascade: **Pass A** is a cheap text-only call;
**Pass B** escalates to a vision model *only* when Pass A signals it should. The escalation
trigger differs per stage and lives in Python (not in the prompt), so changing escalation
behavior means changing code, not prompt wording:

- Stage 2 (`stage2_triage.py`): escalates when Pass A confidence < `confidence_threshold`, or
  the caption is empty/emoji-only (`_is_uninformative_caption`).
- Stage 3 (`stage3_extract.py`): escalates on missing name, `price.source == "none"`, or low
  confidence — *unless* the caption/comments match `DM_FOR_PRICE_RE`, which is a confirmed
  absence rather than uncertainty.
- Stage 4 (`stage4_signals.py`): no vision path. A free regex prefilter (`PREFILTER_RE`) gates
  the LLM call entirely; a post with no candidate keywords costs zero.
- Stages 5 and 6 make **zero LLM calls** — pure deterministic Python over cached outputs.

`pipeline/llm_client.py::complete_structured()` is the single call site for every LLM
interaction: JSON extraction/repair, retry-with-correction on schema mismatch, exponential
backoff on 429/5xx, and token logging. Vision Pass B calls bypass it (they need multimodal
message shapes) and therefore log their own token usage — mirror that pattern if you add one.

### Config-driven models — never hardcode a model in a stage

Which module/function/model each stage uses comes from `pipeline/config/experiments/*.yaml`,
validated by `pipeline/config/experiment_schema.py`. `eval/harness.py::load_stage_fn()` binds
every non-bookkeeping config key as a kwarg into the stage function, so a stage gains a
tunable simply by accepting a new keyword argument. **Swapping a model is a YAML edit, never a
code change** — this is a hard requirement from the brief. `default.yaml` is active; the
sibling files are experiment snapshots and are not kept in sync with it.

**Invariant: no stage function may declare a parameter named `model`.** `load_stage_fn()`
reserves and strips `module`, `function`, `cost_per_call_usd`, `note`, and `model` before
binding. `model:` in a YAML is a *display label* (`"Gemini Cascade (Google AI Studio)"`,
`"dummy-heuristic"`) used for log lines and the predictions filename — frequently not a valid
litellm string at all. Real model strings go in `text_model` / `vision_model` /
`signals_model` / `profile_model`. A parameter named `model` silently never receives its
configured value and falls back to the function's own default; that is exactly how every
Stage 4 model comparison ran on one model while reporting four (`FINDINGS.md` 2026-09-07).
`load_stage_fn()` now raises on this rather than binding silently.

### The harness scores stages independently, not as a chain

This is the most important thing to understand before interpreting any number in `FINDINGS.md`.
`main()` calls `score_stage2`, `score_stage3`, `score_stage4` in sequence, but nothing is piped
between them. Each filters and scores against **gold labels**: `score_stage3` runs on posts
whose *gold* `post_type == "product_listing"`, not on whatever Stage 2 predicted. So reported
per-stage accuracy assumes perfect upstream routing, and a real chained run would be lower.

`scripts/run_pipeline.py` is the chained path (Stages 1→5 on live predictions, no golden set,
emits `runs/<account>/catalog.json`). It is a driver, not a production orchestrator — nothing
runs 1→6 on live predictions, and stages execute only inside the harness or the
`scripts/run_*.py` drivers.

**Both paths must hand each stage the same inputs.** Until 2026-09-09 the harness passed no
Stage 1 profile while `run_pipeline.py` did, so every published accuracy number was measured
on inputs the chained path never uses — and Stage 4's `[VENDOR]`/`[BUYER]` comment labelling
never fired in a scored run. `eval/harness.py::resolve_profile()` now loads it once per run
(account label from `--golden`'s stem, or `--account`) and threads it into all three scorers.
The failure mode was silent, because every stage's `profile` parameter is optional and
defaults to `None`, so it is guarded by
`tests/test_harness.py::test_profile_reaches_every_scored_stage`. **Figures recorded before
2026-09-09 are not comparable to ones recorded after it** (`FINDINGS.md` 2026-09-09).

### Two different identifiers, easily confused

- `account_label` (e.g. `vendor_autos_01`) — a **local folder name you choose**. Keys
  `runs/<account_label>/`, `eval/golden/<account_label>.json`. Need not match Instagram.
- `vendor_handle` (e.g. `ayodele.akinbohun`) — the **real Instagram username**. Used to detect
  the vendor's own comments (`compute_lifecycle_state`, Stage 4's `[VENDOR]`/`[BUYER]` labels)
  and keys `data/snapshots/<vendor_handle>/`.
- `vendor_id` — the golden file's stem; scopes `report/<vendor_id>/` and the token log column.

### Where output goes (and what collides)

| Artifact | Path | Keyed by | Stored in |
|---|---|---|---|
| Raw dump | `runs/<account_label>/raw/dump_<ts>.json` | account_label | git |
| Stage 1 profile cache | `runs/<account_label>/profile.json` | account_label | git |
| Golden set | `eval/golden/<account_label>.json` | account_label | git |
| Chained catalog | `runs/<account_label>/catalog.json` | account_label | git |
| Stage 5 bucket split | `runs/<account_label>/buckets/{auto_import,needs_attention,auto_exclude}.json` | account_label | git |
| Harness predictions | `report/<vendor_id>/stage{2,3,4}_<model>_predictions.json` | vendor_id + model | DVC |
| Token log | `report/token_log.csv` | append-only, has `vendor_id` column | DVC |
| Stage 6 snapshot | `data/snapshots/<vendor_handle>/` | vendor_handle | DVC |
| Stage 6 changes | `report/changes.json` | fixed default — pass `--changes` per vendor | DVC |

`eval/harness.py` with **no** `--golden` globs every `eval/golden/*.json` and merges all
vendors into one accuracy number (tagging the token log `all_vendors`). Always pass
`--golden`/`GOLDEN=` now that more than one vendor exists.

## Traps that cost real debugging time

**Comments always come back empty from the Graph API.** Every post, every account. This is
not a bug and not fixable in code: `instagram_business_manage_comments` is at Standard Access
while the Meta app is in Development mode, and comment text belongs to third-party users, so
Meta withholds it until App Review grants Advanced Access.

**Updated 2026-09-10: `comments_count` now returns a real number; only the text is still
withheld.** Verified live on post `18128205745669337` — `comments_count: 2`, comments
array empty. This is why Stage 6 detects a `comment_delta` at all: it diffs
`comments_count`, not text. The consequence is a half-signal — sync can tell you
comments arrived on a post, but Stage 4 gets a count with nothing to read, so it cannot
say whether they mean "sold" or "how much?". Do not read a `comment_delta` as evidence
Stage 4 acted on real comment content.
Golden-set comments are therefore **hand-authored** (their IDs are a giveaway: a tidy
`1785889326900001x` sequence), and `simulate_stage6.py` injects synthetic ones. This is the
sanctioned workaround, not something to re-investigate — see `FINDINGS.md` 2026-09-04.

**`media_url` is a video file for Reels.** A Reel is `media_type=VIDEO` with
`media_product_type=REELS`; its `media_url` ends in `.mp4`. Never hand it to a vision model or
`PIL.Image.open()`. Always resolve through `pipeline/types.py::vision_image_url()`, which
prefers `thumbnail_url` (a `.jpg`, present only on VIDEO media) and falls back to `media_url`.

**Gemini's free tier caps at 15 requests/minute/model, not just ~400/day.** A
10-post run makes ~20 calls and bursts straight through it if nothing paces them; the
429s are then absorbed by `complete_structured()`'s transient-retry loop until Stage 3
exhausts its budget and drops to `_regex_fallback()`, so the run reports clean scores
over partly-heuristic output and fails `make verify` on the fallback check.
`pipeline/llm_client.py::throttle()` spaces every provider call by
`LLM_MIN_CALL_INTERVAL` seconds (default 4.5; set 0 on a paid tier). **Any new call
site that reaches a provider must call it** — the vision Pass B calls bypass
`complete_structured()` and throttle themselves for exactly this reason.

**`estimated_cost_usd` in `report/token_log.csv` is hardcoded to 0.0.** Every "Cost: $X" the
harness prints is `cost_per_call_usd` × call count, not real spend. Real cost must be computed
by hand from the log's `prompt_tokens`/`completion_tokens` against the provider's rates.

**Configs labelled `model: dummy-heuristic` still make live LLM calls.** Every `stage2_*.yaml`
points `stage3_extract.module` at the real `pipeline.stages.stage3_extract`; the zero-cost
regex heuristic (`_regex_fallback`) only runs when the LLM call *fails*. Known and documented,
not fixed.

**Content hashing deliberately excludes `media_url`.** CDN URLs carry rotating `oh=`/`oe=`
params that change on every fetch, which would make every post falsely "changed" each sync.
`compute_content_hash()` hashes caption + comment count only.

## Conventions

- `pipeline/settings.py` is the only place `load_dotenv()` runs. Resolve tokens via
  `get_ig_access_token(var_name)` — multiple accounts keep separate `.env` entries (e.g.
  `IG_ACCESS_TOKEN_GADGETS`) selected with `--token-env`, rather than overwriting one variable.
- **A credential never goes in a URL.** Both leaks this project has had were a credential in a
  query string that `requests` then embedded in an exception someone logged (`FINDINGS.md`
  2026-09-07, 2026-09-08). Authenticate with `ig_auth_headers()` / `gemini_auth_headers()`,
  and log `redact_tokens(exc)`, never a bare `exc`, on any path that touches an API. A URL you
  did not construct yourself — a `paging.next` from Graph — goes through
  `strip_url_credentials()` before it is followed.
- Scripts in `scripts/` and `eval/` use `argparse` with defaults that reproduce the original
  `vendor_autos_01` run, so a bare invocation stays reproducible. Follow that pattern rather
  than hardcoding a vendor.
- `FINDINGS.md` is the durable experiment record: append dated sections, never rewrite history.
  `report/` is regenerated on every run and is not the record.
- **The data is split between git and DVC, and which is which is deliberate.**
  `eval/golden/` and `runs/` are **committed to git** so the experiment can be re-run from a
  clone. They carry no third-party data: Meta withholds comment text, so every raw dump's
  `comments` array is empty, and the golden sets' comments are hand-authored with invented
  usernames (the real handles in them are the vendor's own and the repo owner's).
  **`eval/golden/` is irreplaceable** — the hand labels cannot be regenerated.
  `data/snapshots/` and `report/` stay **DVC-tracked to Cloudflare R2** and gitignored, each
  with a committed `.dvc` pointer so a commit pins the data it was produced against — `report/`
  because it is regenerated every run and is the bulk of the size, `data/snapshots/` because it
  is binary embeddings. `scripts/scan_secrets.py` enforces exactly this split and
  `tests/test_scan_secrets.py` pins it in both directions.
