# Instagram Catalog Intelligence Pipeline

A research POC: turn a Nigerian vendor's Instagram feed into a structured product catalog plus
a short list of things that need the vendor's attention — using the cheapest model that can do
each job, instead of one expensive model call per post.

Input is a connected Instagram Professional account. Output is `catalog.json` and a three-way
routing split (`auto_import` / `needs_attention` / `auto_exclude`). The interesting part is
that most posts never touch a vision model or a frontier model, and that every claim in this
README comes from a run that passed an automated validity gate.

**Status:** complete. Research POC, sponsor-owned, published for reference. Not production
code and not licensed for reuse.

---

## What the brief asked for

This repo was built against a self-contained POC brief. The brief itself is not published, so
what it specified is summarised here — the codebase cites it as "brief section N" throughout.

**What the POC had to prove**

1. A cascade pipeline (cheap model first, expensive model only on escalation) can parse a real
   vendor account at high accuracy and very low cost per account.
2. Nigerian Instagram caption and comment conventions can be reliably extracted. This is the
   hard, differentiating part.
3. The "needs attention" flags are trustworthy enough that a vendor reviewing them feels *"it
   mostly worked, just confirm a few things"* — not *"I have to redo everything."*
4. We know, with data, which model wins each stage and what a full parse costs.

**Non-goals.** Not production code — a standalone repo, not the monorepo. No fine-tuning. No DM
automation, no storefront rendering, no rules engine. **No scraping** — official Meta API only,
which is a compliance line rather than a preference.

**Ingestion and compliance (Stage 0).** A Meta developer app with the *Instagram API with
Instagram Login* product, scope `instagram_business_basic`. The vendor account must be
Professional (Business/Creator); personal accounts have no API access. Live testing is legal
without App Review — development mode supports ~25 test users. Carousel children are always
cached at ingest even though the POC does not process them per-slide, because historical
re-pulls are often impossible. Raw pulls are cached to disk so the pipeline can be re-run
offline.

**Golden set first.** The brief's central discipline: hand-label a golden set *before* building
any pipeline stage, and build the eval harness *before* tuning prompts. Every prompt or model
change gets a score, not a vibe. Without this, everything "looks good" and nothing is known.

**The Nigerian conventions the golden set must cover** — described in the brief as the moat:

- prices written `25k`, `₦25,000`, `#25k` (naira typed as `#`), or bare `25,000`
- `DM for price`, `price in bio`, `send a DM`
- price overlaid on the image, not in the caption
- sizes as ranges (`40–45`, `S–XXL`); colours listed with emojis
- Pidgin/Yoruba-mixed captions and comments
- `sold` / `gone` / `it don finish` in comment replies
- WhatsApp numbers in bio/captions (capture, but treat as PII)
- carousel posts where each slide is a different item

**Hard requirement — models are config, not code.** Every stage reads its model from a config
file. Switching any stage to any model, open-source or API, must be a one-line config change
with zero code changes. The eval harness accepts a config, so comparing models is: swap config,
run harness, compare scores.

**Targets.** Triage precision ≥ 90%; price accuracy ≥ 85%; missing-price recall 100% (never
silently invent a price); stale/out-of-stock flag precision ≥ 75%; carousel classification
≥ 85%; full parse ≤ $0.15 per 100-post account (stretch ≤ $0.05) and ≤ 10 minutes; sync
change-classification precision ≥ 85%; sync on an unchanged account ≈ $0.

---

## Architecture — the funnel

```
Stage 0  INGEST     API pull -> raw JSON dump                        (no AI)
Stage 1  ACCOUNT    What kind of business is this?                   (1 call per account)
Stage 2  TRIAGE     Product post vs. not?                            (cheap text -> vision escalation)
Stage 3  EXTRACT    name, variants, price, quantity                  (product posts only)
Stage 4  SIGNALS    Stale? Out of stock? Inventory hints?            (regex prefilter -> LLM)
Stage 5  FLAGS      Confidence routing -> auto-import vs. attention  (no AI)
Stage 6  SYNC       Re-pull -> diff -> AI on deltas only             (no AI in the diff)
```

The design rule for every stage is *cheapest thing first, escalate on low confidence*. The
funnel is the cost strategy.

**The escalation triggers live in Python, not in prompt wording.** Changing escalation
behaviour means changing code:

| Stage | Escalates when |
|---|---|
| 2 (`stage2_triage.py`) | Pass A confidence < `confidence_threshold`, or the caption is empty/emoji-only |
| 3 (`stage3_extract.py`) | missing name, `price.source == "none"`, or low confidence — **unless** the caption matches `DM_FOR_PRICE_RE`, which is a *confirmed absence* rather than uncertainty |
| 4 (`stage4_signals.py`) | never — no vision path. A free regex prefilter gates the LLM call entirely, so a post with no candidate keywords costs zero |
| 5, 6 | never — pure deterministic Python over cached outputs, zero LLM calls |

`pipeline/llm_client.py::complete_structured()` is the single call site for every LLM
interaction: JSON extraction and repair, retry-with-correction on schema mismatch, exponential
backoff on 429/5xx, throttling, and token logging. Vision Pass B calls bypass it because they
need multimodal message shapes, and therefore log and throttle themselves.

**Never invent a price.** `source: "none"` plus a flag is always the correct output. A
hallucinated price is the one unforgivable failure mode — it becomes real money at checkout.

---

## Results

Every figure below comes from a run that passed `scripts/verify_run.py` (see
[Verification](#how-anything-here-was-verified)). Run IDs are recorded in
`report/token_log.csv`, whose `run_id` column is the audit trail behind every table here.

Two vendors: `vendor_autos_01` (30 labeled posts) and `vendor_gadgets_01` (41 labeled posts).

### Target vs. measured — Gemini cascade

| Brief target | Measured | |
|---|---|---|
| Triage precision ≥ 90% | 97% autos (29/30), 93% gadgets (38/41) | met |
| Price accuracy ≥ 85% | 100% (19/19 autos, 31/31 gadgets) | met |
| Missing-price recall 100% | 6/6 autos, 3/3 gadgets | met |
| Stale/stock flag precision ≥ 75% | Stage 4 micro F1 88% autos, 77% gadgets | met |
| Escalation rate (brief: rework the prompt above 40%) | 1/30 autos, 5/41 gadgets | met |
| Attention items ≤ 10 per 100 posts | 18% autos, 15% gadgets | **missed** |
| Full parse ≤ $0.15 / 100 posts | $0.17 on Gemini at paid rates; $0.015 on Llama | **marginal** |
| Sync on unchanged account ≈ $0 | 18 no-ops, 0 AI calls | met |
| Carousel classification ≥ 85% | not scored — the brief's own complexity valve was taken | **not measured** |
| Wall-clock ≤ 10 min / 100 posts | never instrumented | **not measured** |

The two misses are real. The attention rate is roughly 1.5x the brief's ceiling, and the
headline cost target is met only by the *cheaper* model, not the one that wins on quality.

### Model comparison

Per-stage harness scores, stages scored independently against gold labels:

| vendor | family | S2 accuracy | S3 price | name similarity | missing price | S4 macro / micro | escalation | tokens |
|---|---|---|---|---|---|---|---|---|
| autos | **Gemini** | **97%** (29/30) | **100%** (19/19) | **91%** | 6/6 | 87% / **88%** | 1/30 | **91,102** |
| autos | GPT-4o Mini | 93% (28/30) | 84% (16/19) | 87% | 6/6 | 77% / **76%** | 2/30 | 287,258 |
| autos | Llama | 87% (26/30) | 79% (15/19) | 87% | 6/6 | 48% / **51%** | 0/30 | 92,878 |
| gadgets | **Gemini** | **93%** (38/41) | **100%** (31/31) | **81%** | 3/3 | 67% / **77%** | 5/41 | **115,281** |
| gadgets | Llama | 90% (37/41) | 87% (27/31) | 72% | 3/3 | 38% / **43%** | 5/41 † | 136,193 |

† Llama's gadgets escalation rate is inflated by a `max_tokens` bug — 3 of those 5 vision calls
existed only to repair a truncated Pass A. Its accuracy figures stand; its escalation and cost
figures do not.

Gemini beat the GPT-4o Mini baseline on every scored axis for both vendors. Qwen failed its
validity gate twice on invalid JSON and has no recorded numbers.

**A finding worth more than the scores.** In the first Stage 2 comparison, Llama and Gemini
both scored 27/30 while getting *zero of the same posts wrong*. Both models were confidently
wrong on their own errors — 0.8 to 0.95 confidence on wrong answers. A confidence-threshold
escalation trigger only catches uncertainty a model admits to. It is not a safety net against a
model that is simply wrong and sure of it.

### Cost

Measured from `report/token_log.csv` for `vendor_gadgets_01`, not estimated.

| Stage | Calls | Tokens/post | Share |
|---|---|---|---|
| Stage 1 (profile, amortised over 41 posts) | 1 | 26 | 1% |
| Stage 2 (triage) | 42 | 500 | 18% |
| **Stage 3 (extraction)** | 37 | **2,011** | **74%** |
| Stage 4 (signals) | 8 | 197 | 7% |
| **Total** | 88 | **2,734** | |

Stage 3 is three quarters of all token spend. Stage 4 is the cheapest stage *despite having the
longest prompt*, because its regex prefilter lets only 8 of 41 posts reach a model — the
two-layer design doing exactly what it was built for.

**The free tier was concealing a cost inversion.** At live paid rates, the model that wins on
quality is the most expensive one measured:

| Model | $/post | $/1,000 posts |
|---|---|---|
| Gemini 3.5 Flash-Lite | $0.001688 | $1.69 |
| GPT-4o Mini | $0.000588 | $0.59 |
| Qwen 2.5 7B | $0.000313 | $0.31 |
| Llama 3.1 8B | $0.000149 | $0.15 |

Gemini's completion tokens cost $2.50/M — more than 4x GPT-4o Mini's and 31x Llama's.

**But inference cost is not what decides viability.** Human review dominates it by 10–217x.
At 45 seconds of reviewer time per flagged item, Gemini's 220 flags per 1,000 posts cost
$16.50–$68.75 of human time against $1.69 of inference; GPT-4o Mini's 410 flags cost
$30.75–$128.12 against $0.59. **Attention rate, not model price, is the number that matters** —
which is also the target the POC missed.

### Stage 6 sync — the strongest cost result

A routine re-sync finds most of a feed unchanged and reprocesses only what moved. The diff
itself — content hash, then perceptual image hash, then caption embedding — makes **zero
language-model calls**.

A live sync of 40 fresh posts against a 30-post baseline: 18 no-ops, 4 new posts, 2 caption
edits, 1 comment delta, 4 repost matches (2 by pHash, 2 by caption embedding), 2 exact
duplicates auto-merged. Feeding that into the pipeline:

| | Full feed | Incremental |
|---|---|---|
| Posts processed | 40 | **6** |
| Model calls | 101 | **13** |
| Tokens | 116,201 | **14,288** |
| Reduction | — | **87%** |

A weekly re-sync of an established vendor costs an eighth of a full rebuild.

**Two things not to overclaim.** The 0% attention rate across those 6 posts is a 6-post
denominator, not a quality signal. And the 4 "new" posts are new *relative to the 30-post
golden baseline*, not newly posted — rebuilding from golden is what makes the run reproducible,
but it means the sync partly re-discovers posts already in the dump. A production sync against
a live-advanced snapshot would find fewer, and the saving would be larger, not smaller.

**Excluding `content_changed` from reprocessing is the load-bearing choice.** Ten of 23 changes
were that type — the content hash moved but caption and comment count did not, which is CDN
churn. Selecting them too would reprocess 16 of 40 posts instead of 6 and quietly reinstate
most of the spend Stage 6 exists to avoid. Three tests pin this.

---

## Reproducing the experiment

The golden sets and raw dumps are committed, so a clone can score against the same hand labels
this project's numbers came from.

```bash
uv sync
cp .env.example .env          # add one model provider key, e.g. GEMINI_API_KEY
make harness GOLDEN=eval/golden/vendor_autos_01.json \
             CONFIG=pipeline/config/experiments/default.yaml
make verify  RUN_ID=<the run_id the harness printed> POSTS=30
```

`make stage5` re-runs the routing off cached predictions and makes zero LLM calls, so it costs
nothing and needs no key at all.

### The image links are expired, and you cannot refresh them

CDN URLs on `fbcdn.net` carry short-lived `oh=`/`oe=` params. The links in the committed dumps
and golden sets have long since expired. `scripts/refresh_media_urls.py` exists to refresh them,
but it needs an access token belonging to **the account that owns the posts** — the Graph API
only answers for its own connected account. Nobody outside that vendor can refresh these links.

What this does and does not cost you:

| Still works offline | Needs live images |
|---|---|
| Stage 2 Pass A (text triage) | Stage 2 Pass B (vision escalation) |
| Stage 3 text pass | Stage 3 Pass B (price-on-image) |
| Stage 4 signals | Stage 6 pHash / snapshot rebuild |
| Stage 5 routing (zero LLM by design) | |
| Stage 6 content-hash and caption-embedding paths | |

Most of the measured results are reproducible. The vision escalation numbers are not — escalated
posts will fail on dead links, which is expected rather than a bug.

### Swapping a model

A YAML edit, never a code change. `pipeline/config/experiments/` holds the configs;
`default.yaml` is active and the siblings are experiment snapshots, not kept in sync with it.
Real model strings go in `text_model` / `vision_model` / `signals_model` / `profile_model`.

**A stage function may never declare a parameter named `model`.** `load_stage_fn()` reserves and
strips `module`, `function`, `cost_per_call_usd`, `note` and `model` before binding, so `model:`
in a YAML is a *display label* used for log lines and prediction filenames — frequently not a
valid model string at all. A parameter named `model` silently never receives its configured
value and falls back to the function's own default. That is exactly how an entire Stage 4 model
comparison ran on one model while reporting four. `load_stage_fn()` now raises rather than
binding silently.

---

## Commands

`make` wraps the underlying `uv run` invocations; a bare `make` prints the target list with
current defaults. Git Bash on Windows does not bundle `make` — if it is missing, run the
`uv run ...` command that `make help` prints.

```bash
make ingest ACCOUNT=vendor_gadgets_01 TOKEN_ENV=IG_ACCESS_TOKEN_GADGETS
make golden ACCOUNT=vendor_gadgets_01          # labeling skeleton; refuses to overwrite
make harness GOLDEN=... CONFIG=...             # score stages against the golden set
make pipeline ACCOUNT=vendor_autos_01 [LIMIT=N] [INGEST=1] [LIVE=1]
make stage5 GOLDEN=... STAGE2=... STAGE3=... STAGE4=...   # zero-LLM, off cached predictions
make verify RUN_ID=<id> [EXPECT=<model>] [POSTS=N]        # gate a run before quoting it
make snapshot VENDOR=<ig_username> GOLDEN=...             # Stage 6 baseline
make sync VENDOR=<ig_username> GOLDEN=... CHANGES=...     # Stage 6 diff
make simulate-sync                             # offline Stage 6, no API, no snapshot writes
make test / check-secrets / install-hooks      # offline checks
make data-pull / data-push / data-status       # DVC <-> Cloudflare R2
```

`LLM_MIN_CALL_INTERVAL` (default 4.5s) spaces every provider call. **Gemini's free tier caps at
15 requests/minute/model, not just ~400/day.** A 10-post run makes ~20 calls and bursts straight
through that ceiling if nothing paces it; the 429s are then absorbed by the retry loop until
Stage 3 exhausts its budget and drops to a regex fallback — so the run reports clean scores over
partly heuristic output. Set it to `0` on a paid tier.

`LOG_LEVEL=DEBUG` surfaces raw model responses. On Windows, prefix with `PYTHONIOENCODING=utf-8`
when printing captions — they contain emoji cp1252 cannot encode.

---

## Repo layout

```
ingest/            Stage 0 - Graph API pull, caching, retry/backoff
pipeline/
  stages/          Stages 1-6, one module each
  config/          experiment YAMLs + pydantic schema
  llm_client.py    the single LLM call site: JSON repair, retries, throttle, token log
eval/
  harness.py       scores stages against the golden set
  golden/          hand-labeled golden sets  (committed)
scripts/           drivers: run_pipeline, run_stage5/6, verify_run, scan_secrets, ...
tests/             offline only - no network, no .env, no golden-set reads
runs/              raw dumps, profiles, catalogs  (committed)
report/            predictions + token_log.csv   (DVC)
data/snapshots/    Stage 6 baselines              (DVC)
```

### Two identifiers, easily confused

- **`account_label`** (e.g. `vendor_autos_01`) — a local folder name you choose. Keys
  `runs/<account_label>/` and `eval/golden/<account_label>.json`. Need not match Instagram.
- **`vendor_handle`** (e.g. `ayodele.akinbohun`) — the real Instagram username. Used to detect
  the vendor's own comments and keys `data/snapshots/<vendor_handle>/`.
- **`vendor_id`** — the golden file's stem; scopes `report/<vendor_id>/` and the token log.

### What data is here, and why it was safe to commit

The brief said not to commit raw dumps, because dumps would carry commenter handles and text.
They do not. `instagram_business_manage_comments` sits at Standard Access while the Meta app is
in Development mode, so Meta withholds comment text — **every dump's `comments` array is
empty**. What remains is vendor captions, media URLs, permalinks and timestamps. The golden
sets' comments are hand-authored (their tidy `178588932690000xx` ID sequence gives them away)
with invented usernames.

So `eval/golden/` and `runs/` are committed on purpose, and the hygiene rule has nothing left to
bite on for them. `report/` (regenerated every run, and the bulk of the size) and
`data/snapshots/` (binary embeddings) stay DVC-tracked to Cloudflare R2, each with a committed
`.dvc` pointer so a commit pins the data it was produced against. `make data-pull` needs R2
credentials that are not published. `scripts/scan_secrets.py` enforces this split as a
pre-commit hook, and `tests/test_scan_secrets.py` pins it in both directions.

**`eval/golden/` is irreplaceable** — the hand labels cannot be regenerated.

---

## How anything here was verified

Three separate mechanisms, deliberately not overlapping.

**`eval/harness.py` is the verification mechanism for anything model-related.** It scores each
stage against the hand-labeled golden set and writes per-post predictions. Re-run it and compare
against the recorded numbers to check a change did not regress anything.

**`scripts/verify_run.py` gates a run's validity** against `report/token_log.csv` — the only
artifact recording what actually reached the provider rather than what the config claimed. It
fails on: zero rows logged (the run never reached a model), any `fallback_used=True` row (part
of the output is regex heuristic, not model output), a model string outside `--expect-model`, a
call count outside a band derived from `--posts`, or any errored post. It exits non-zero, so
`make harness … && make verify RUN_ID=…` fails the pair. **No number goes into
this README from a run that has not passed this gate.**

**`tests/` is deliberately narrow** — offline, 116 tests, and it does not test model quality;
that is the harness's job and always will be. It covers what the harness structurally cannot:
credential handling, the harness's own per-post error isolation and the reserved-parameter
tripwire, deterministic Stage 5 routing, the token-log reader every cost figure depends on, and
the secret scanner. No test may make a network call, read `.env`, or read a golden set. A test
that needs a model response belongs in the harness instead.

The experiment was recorded as it ran in an append-only findings log, including the entries
recording what was measured wrong and why. That log has been consolidated into this README —
[What went wrong](#what-went-wrong-and-what-it-cost) and
[Caveats on the numbers](#caveats-on-the-numbers) are what survived the consolidation. Code
comments citing a dated finding refer to those sections; `report/token_log.csv` remains the
primary evidence for every figure.

---

## What went wrong, and what it cost

The brief called this "throwaway code, keepable learnings." These are the learnings. Every one
of them was a silent failure — the pipeline kept producing plausible output the whole time.

**A stage's configured model silently never bound.** `load_stage_fn()` reserves and strips a
config key named `model`, and `detect_signals()` happened to name its parameter `model`. So the
configured value never reached the function and it used its in-module default every time. An
entire Stage 4 four-model comparison ran on one model while reporting four. Proven only from
`report/token_log.csv`, which records the model string at call time. `load_stage_fn()` now
raises instead of binding silently, and a test guards it. **Lesson: a config system that
silently ignores a key will eventually be believed.**

**The harness and the chained path fed stages different inputs.** For weeks the harness passed
no Stage 1 profile while `run_pipeline.py` did, so every published accuracy number was measured
on inputs the production path never uses — and Stage 4's `[VENDOR]`/`[BUYER]` comment labelling
never once fired in a scored run. Silent because every stage's `profile` parameter is optional
and defaults to `None`. Figures from before the fix are not comparable to ones after it, which
is why the experiment record was re-baselined from scratch.

**Rate limiting turned into fake accuracy.** Gemini's free tier caps at 15 requests/minute, not
just a daily quota. A run bursts through it, the 429s get absorbed by the transient-retry loop,
Stage 3 exhausts its budget and drops to its regex fallback — and the run reports clean scores
over partly heuristic output. This is the direct reason `scripts/verify_run.py` exists and why
it fails on any `fallback_used=True` row.

**Two credential leaks, same shape both times.** A token in a URL query string, which `requests`
then embedded in an exception message that something logged. Fixed by authenticating with
headers, routing every log through `redact_tokens()`, and passing any URL we did not construct
ourselves (a `paging.next` from Graph) through `strip_url_credentials()`.

**Reels broke vision silently.** A Reel is `media_type=VIDEO` with `media_product_type=REELS`,
and its `media_url` is an `.mp4`. Handing that to a vision model or `PIL.Image.open()` fails.
Everything now resolves through `vision_image_url()`, which prefers `thumbnail_url`.

**A regex missed a plural.** `DM_FOR_PRICE_RE` did not match "send us a DM for prices", so posts
with a confirmed price-absence were escalated to vision as if they were uncertain — paying twice
to re-confirm an unanswerable question.

**Stale CDN URLs biased results by path.** `refresh_media_urls.py` only refreshed the golden
sets, but the chained path reads the raw dumps, which nothing refreshed. The harness had fresh
image links and the production-shaped path did not, biasing chained results downward for reasons
unrelated to any model.

The through-line: **every one of these was caught by an artifact that records what actually
happened, not what was configured to happen.** `report/token_log.csv` caught the model binding
bug, the fallback contamination and the rate limiting. That is why no number here is quoted from
a run that has not passed the gate.

## Caveats on the numbers

**Per-stage accuracy is not system accuracy.** The single caveat that must survive into any
external quote. Seeing "Stage 2 97%, Stage 3 100%" invites the inference of a system that works
end-to-end at roughly that level. It does not follow, and a chained run is measurably lower.

**Reviewer time is assumed, not measured.** The 45 seconds per flagged item underpinning every
human-cost figure is invented. It is the most load-bearing number in the financial case and the
easiest to challenge.

**Paid pricing is applied to free-tier runs.** The cost table is arithmetic over real token
counts and live provider rates, but no money was actually spent on the Gemini runs. It is a
projection, not observed spend.

**The noise floor is large relative to the differences reported.** Re-running the *same* model
on the *same* golden set moved per-signal F1 by 20–35 points, because most signals have only 1–3
gold instances. Only macro-level gaps are safe to quote; single per-signal cells are not. The
gadgets vendor has zero gold instances for 3 of 7 signals, so no model can be scored on them
there at all.

**One suspected gold-labelling error was never investigated.** Post `18079164065330268`'s
caption says "Financing available through our partner bank" while its gold `expected_signals` is
empty. If that is a mislabel, every model that correctly detects it is scored a false positive.

**Single-vendor model rankings did not survive contact with a second vendor.** This is the
qualitative finding that most changed how the results were read.

## Known limits

**Comment text is withheld by Meta.** `comments_count` returns a real number but the comments
array is always empty. This is why Stage 6 can detect a `comment_delta` at all — it diffs the
count, not the text. The consequence is a half-signal: sync can tell you comments arrived, but
Stage 4 gets a count with nothing to read, so it cannot say whether they mean "sold" or "how
much?". Do not read a `comment_delta` as evidence Stage 4 acted on real comment content.

**The harness scores stages independently, not as a chain.** Each stage is scored against *gold*
labels, so `score_stage3` runs on posts whose gold `post_type` is `product_listing`, not on
whatever Stage 2 predicted. Reported per-stage accuracy therefore assumes perfect upstream
routing, and a real chained run is lower. Measured directly on the gadgets vendor: 12% attention
from cached per-stage predictions vs 15% chained — a 3-point gap that *is* error compounding.

**Figures recorded before 2026-09-09 are not comparable to ones after it.** Until then the
harness passed no Stage 1 profile while the chained path did, so every published accuracy number
was measured on inputs the chained path never uses. Fixed, and guarded by a test.

**`estimated_cost_usd` in `report/token_log.csv` is hardcoded `0.0`.** Every "Cost: $X" the
harness prints is `cost_per_call_usd` × call count, not real spend. Real cost must be computed
by hand from the log's token counts against provider rates — which is how the cost table above
was produced.

**Carousel per-slide extraction was never built.** The brief's sanctioned complexity valve was
taken: every carousel attaches all slides as one product's gallery. Carousel classification
accuracy is consequently unmeasured.

**Nothing runs Stages 1→6 end to end on live predictions.** `scripts/run_pipeline.py` chains
Stages 1→5 and is a driver, not a production orchestrator.
