# Instagram Catalog Intelligence Pipeline

A research POC that turns a Nigerian vendor's Instagram feed into a structured product catalog
plus a short list of items needing the vendor's attention, using the cheapest model that can do
each job rather than one expensive call per post.

Input is a connected Instagram Professional account. Output is `catalog.json` and a three-way
split: `auto_import`, `needs_attention`, `auto_exclude`.

## What the brief asked for

Prove that a cascade pipeline (cheap model first, expensive model only on escalation) can parse
a real vendor account accurately and cheaply; that Nigerian caption conventions (`25k`,
`₦25,000`, `#25k`, "DM for price", price-on-image, Pidgin/Yoruba mixing, "it don finish") can be
extracted reliably; that the attention flags are trustworthy enough to feel like "confirm a few
things" rather than "redo everything"; and which model wins each stage at what cost.

Two hard rules: **models are config, not code** (swapping one is a YAML edit), and **never
invent a price** (`source: "none"` plus a flag is always correct). No scraping; official Meta
API only.

## Architecture

```
Stage 0  INGEST     API pull -> raw JSON dump                        (no AI)
Stage 1  ACCOUNT    What kind of business is this?                   (1 call per account)
Stage 2  TRIAGE     Product post vs. not?                            (text -> vision escalation)
Stage 3  EXTRACT    name, variants, price, quantity                  (product posts only)
Stage 4  SIGNALS    Stale? Out of stock?                             (regex prefilter -> LLM)
Stage 5  FLAGS      Confidence routing -> import vs. attention       (no AI)
Stage 6  SYNC       Re-pull -> diff -> AI on deltas only             (no AI in the diff)
```

Escalation triggers live in Python, not in prompt wording:

| Stage | Escalates when |
|---|---|
| 2 | Pass A confidence < threshold, or caption empty/emoji-only |
| 3 | missing name, `price.source == "none"`, or low confidence, **unless** `DM_FOR_PRICE_RE` matches (a confirmed absence, not uncertainty) |
| 4 | never. A free regex prefilter gates the LLM call, so a post with no candidate keywords costs zero |
| 5, 6 | never. Deterministic Python over cached outputs, zero LLM calls |

`llm_client.py::complete_structured()` is the single call site for every LLM interaction: JSON
repair, retry on schema mismatch, backoff on 429/5xx, throttling, token logging. Vision Pass B
calls bypass it (multimodal message shapes) and log and throttle themselves.

## Results

Every figure comes from a run that passed `scripts/verify_run.py`. `report/token_log.csv` is the
audit trail. Two vendors: `vendor_autos_01` (30 posts), `vendor_gadgets_01` (41 posts).

> **Stage 2 was re-baselined on 2026-09-21** and the triage row below is stale. The five post
> types had never been defined anywhere in the repo, so every model was reproducing a class
> boundary nobody had written down. `eval/LABEL_CODEBOOK.md` now defines them and both Stage 2
> prompts carry the definitions, which makes figures measured before that date incomparable.
> See [Stage 2, re-baselined](#stage-2-re-baselined) for what is currently gated.

| Target | Measured (Gemini cascade) | |
|---|---|---|
| Triage precision >= 90% | 97% autos (29/30), 93% gadgets (38/41) | met, **pre-codebook** |
| Price accuracy >= 85% | 100% (19/19, 31/31) | met |
| Missing-price recall 100% | 6/6, 3/3 | met |
| Stale/stock flag precision >= 75% | micro F1 88% autos, 77% gadgets | met |
| Escalation below 40% | 1/30, 5/41 | met |
| Attention <= 10 per 100 posts | 18% autos, 15% gadgets | **missed** |
| Full parse <= $0.15 / 100 posts | $0.17 Gemini at paid rates, $0.015 Llama | **marginal** |
| Sync on unchanged account ~ $0 | 18 no-ops, 0 AI calls | met |
| Carousel classification >= 85% | not scored | **not measured** |
| Wall-clock <= 10 min / 100 posts | never instrumented | **not measured** |

### Model comparison

Per-stage harness scores, each stage scored independently against gold labels:

| vendor | family | S2 acc | S3 price | name sim | miss price | S4 macro / micro | escalation | tokens |
|---|---|---|---|---|---|---|---|---|
| autos | **Gemini** | **97%** | **100%** | **91%** | 6/6 | 87% / **88%** | 1/30 | **91,102** |
| autos | GPT-4o Mini | 93% | 84% | 87% | 6/6 | 77% / **76%** | 2/30 | 287,258 |
| autos | Llama | 87% | 79% | 87% | 6/6 | 48% / **51%** | 0/30 | 92,878 |
| gadgets | **Gemini** | **93%** | **100%** | **81%** | 3/3 | 67% / **77%** | 5/41 | **115,281** |
| gadgets | Llama | 90% | 87% | 72% | 3/3 | 38% / **43%** | 5/41 † | 136,193 |

† Llama's gadgets escalation is inflated by a `max_tokens` bug: 3 of those 5 vision calls only
repaired a truncated Pass A. Accuracy stands, escalation and cost do not.

Gemini beat the GPT-4o Mini baseline on every scored axis for both vendors. Qwen failed its
gate twice on invalid JSON and has no recorded numbers.

In the first Stage 2 comparison, Llama and Gemini both scored 27/30 while getting zero of the
same posts wrong, both at 0.8 to 0.95 confidence on their own errors. A confidence threshold
only catches uncertainty a model admits to.

### Stage 2, re-baselined

Everything in this subsection is scored on **product vs not-product**, which is the brief's
actual target and what every downstream consumer reduces `post_type` to anyway: Stage 5, Stage 6
and `run_pipeline.py` all test only `!= "product_listing"`.

| | autos (tune) | gadgets (test) | precision | escalation |
|---|---|---|---|---|
| Jev alone (binary) | 28/30 = 93% | 33/41 = 80% | 100% | n/a, no vision |
| **Jev + Gemini hybrid** | **30/30 = 100%** | **38/41 = 93%** | **100%** | 20% / 15% |
| Gemini alone | 30/30 = 100% | see caveat | 97% | 1/30 |

Run ids: `stage2_jev_20260921T084428Z`, `stage2_jev_20260921T084716Z`,
`stage2_hybrid_20260921T091816Z`, `stage2_hybrid_20260921T092310Z`,
`family_gemini_20260921T081023Z`. All passed the gate.

**The hybrid** runs Jev as a text-only Pass A and escalates two ways: a caption-less post goes
straight to Gemini vision (skipping Jev entirely, since there is nothing to read), and a
low-confidence post with a caption goes to Gemini text. Jev alone loses mainly because five
gadgets posts have no caption at all and carry their price on the image, which a text-only model
cannot reach.

**Cost is where the funnel shows itself.** On gadgets the hybrid spent $0.0084 across 42 calls:

| | calls | cost | share |
|---|---|---|---|
| Jev | 36 | $0.000956 | 11% |
| Gemini vision | 5 | $0.007175 | **86%** |
| Gemini text | 1 | $0.000244 | 3% |

Five vision calls are 86% of the bill. The cheap first pass is close to free, so the lever worth
pulling is the escalation rate, not the Pass A model.

**Jev's confidence is better calibrated than Gemini's**, which is the property a
confidence-triggered cascade actually needs: separation between confidence-when-right and
confidence-when-wrong is +0.217 on autos and +0.113 on gadgets, against Gemini's +0.074.

**Caveat on Gemini alone for gadgets.** Not currently measurable. Two attempts on the new prompt
both failed the gate on provider 503s (36/41 and 34/41, with 1 and 3 errored posts). The 39/41
previously recorded is from the definition-free prompt and is not comparable.

**The codebook has a known defect, unfixed.** All three remaining hybrid misses on gadgets are
posts the codebook mis-specifies: two marked SOLD while still listing a price, and one
"available soon". The codebook says a completed sale is a `testimonial_repost`, which puts a
Stage 4 concern (is it still available) into a Stage 2 category (is this a product post). One of
those posts escalated to Gemini and **Gemini agreed with Jev**, so two independent models given
the same definitions disagree with the gold in the same direction. The autos labels and the
gadgets labels genuinely differ on this, and the codebook encoded the autos convention as
universal. Fixing it means editing the clause, not the models. It is unfixed because those posts
are in the test half.

## Cost

Measured from `report/token_log.csv` for `vendor_gadgets_01`.

| Stage | Calls | Tokens/post | Share |
|---|---|---|---|
| Stage 1 (amortised over 41 posts) | 1 | 26 | 1% |
| Stage 2 | 42 | 500 | 18% |
| **Stage 3** | 37 | **2,011** | **74%** |
| Stage 4 | 8 | 197 | 7% |
| **Total** | 88 | **2,734** | |

Stage 3 is three quarters of all token spend. Stage 4 is cheapest despite the longest prompt,
because its regex prefilter lets only 8 of 41 posts reach a model.

At live paid rates, the model that wins on quality is the most expensive one measured:

| Model | $/post | $/1,000 posts |
|---|---|---|
| Gemini 3.5 Flash-Lite | $0.001688 | $1.69 |
| GPT-4o Mini | $0.000588 | $0.59 |
| Qwen 2.5 7B | $0.000313 | $0.31 |
| Llama 3.1 8B | $0.000149 | $0.15 |

Gemini's completion tokens cost $2.50/M, over 4x GPT-4o Mini and 31x Llama. These runs were made
on the free tier, so this is arithmetic over real token counts and published rates, not observed
spend.

## Stage 6 sync

A re-sync finds most of a feed unchanged. The diff (content hash, then perceptual image hash,
then caption embedding) makes zero LLM calls.

A live sync of 40 posts against a 30-post baseline: 18 no-ops, 4 new, 2 caption edits, 1 comment
delta, 4 repost matches (2 pHash, 2 embedding), 2 exact duplicates auto-merged.

| | Full feed | Incremental |
|---|---|---|
| Posts processed | 40 | **6** |
| Model calls | 101 | **13** |
| Tokens | 116,201 | **14,288** |
| Reduction | | **87%** |

Excluding `content_changed` from reprocessing is the load-bearing choice: 10 of 23 changes were
that type (hash moved, caption and comment count did not, i.e. CDN churn). Including them would
reprocess 16 of 40 posts instead of 6. Three tests pin this.

The 0% attention rate across those 6 posts is a 6-post denominator, not a quality signal.

## Reproducing

Golden sets and raw dumps are committed, so a clone can score against the same hand labels.

```bash
uv sync
cp .env.example .env          # add one provider key, e.g. GEMINI_API_KEY
make harness GOLDEN=eval/golden/vendor_autos_01.json \
             CONFIG=pipeline/config/experiments/default.yaml
make verify  RUN_ID=<run_id the harness printed> POSTS=30
```

`make stage5` re-runs routing off cached predictions with zero LLM calls and needs no key.

**The image links are expired and you cannot refresh them.** `fbcdn.net` URLs carry short-lived
`oh=`/`oe=` params. `scripts/refresh_media_urls.py` needs a token belonging to the account that
owns the posts, since the Graph API only answers for its own connected account.

| Works offline | Needs live images |
|---|---|
| Stage 2 Pass A, Stage 3 text pass, Stage 4 | Stage 2/3 vision Pass B |
| Stage 5 routing | Stage 6 pHash, snapshot rebuild |
| Stage 6 content-hash and embedding paths | |

Escalated posts will fail on dead links. That is expected, not a bug.

## Commands

```bash
make ingest ACCOUNT=... TOKEN_ENV=...          # Stage 0 pull
make golden ACCOUNT=...                        # labeling skeleton
make harness GOLDEN=... CONFIG=...             # score stages against gold
make pipeline ACCOUNT=... [LIMIT=N] [LIVE=1]   # chained Stages 1-5
make stage5 GOLDEN=... STAGE2=... ...          # zero-LLM routing
make verify RUN_ID=... [EXPECT=...] [POSTS=N]  # gate a run before quoting it
make snapshot / sync / simulate-sync           # Stage 6
make test / check-secrets / install-hooks      # offline checks
make data-pull / data-push / data-status       # DVC <-> Cloudflare R2
```

`LLM_MIN_CALL_INTERVAL` (default 4.5s) spaces provider calls. Gemini's free tier caps at 15
requests/minute, not just a daily quota. Without pacing, a run bursts through it, the 429s are
absorbed by the retry loop, and Stage 3 drops to its regex fallback while still reporting clean
scores. Set to `0` on a paid tier.

`LOG_LEVEL=DEBUG` surfaces raw model responses. On Windows prefix `PYTHONIOENCODING=utf-8` when
printing captions.

## Layout

```
ingest/            Stage 0: Graph API pull, caching, retry
pipeline/stages/   Stages 1-6, one module each
pipeline/config/   experiment YAMLs + schema
pipeline/llm_client.py   single LLM call site
eval/harness.py    scores stages against the golden set
eval/golden/       hand-labeled golden sets   (git)
runs/              raw dumps, profiles, catalogs (git)
report/            predictions + token_log.csv  (DVC)
data/snapshots/    Stage 6 baselines            (DVC)
scripts/           drivers and checks
tests/             offline only
```

Three identifiers, easily confused:

- `account_label` (`vendor_autos_01`): a local folder name you choose. Need not match Instagram.
- `vendor_handle` (`ayodele.akinbohun`): the real Instagram username. Keys `data/snapshots/`.
- `vendor_id`: the golden file's stem. Scopes `report/<vendor_id>/` and the token log.

## Verification

- `eval/harness.py` scores stages against the golden set. It is the only check on model quality.
- `scripts/verify_run.py` gates a run against `report/token_log.csv`, the only record of what
  actually reached the provider. Fails on zero rows, any `fallback_used=True` row, a model
  outside `--expect-model`, an implausible call count, or any errored post. Exits non-zero.
- `tests/` is 116 offline tests covering what the harness cannot: credential handling, per-post
  error isolation, the reserved-parameter tripwire, Stage 5 routing, the token-log reader, the
  secret scanner. No test may make a network call, read `.env`, or read a golden set.

No number here comes from a run that has not passed the gate.

## What went wrong

Every one of these was silent. The pipeline kept producing plausible output throughout.

- **A configured model never bound.** `load_stage_fn()` strips a config key named `model`, and
  `detect_signals()` named its parameter `model`. A four-model Stage 4 comparison ran entirely
  on one model. Caught only from the token log. It now raises instead of binding silently.
- **The harness and the chained path fed stages different inputs.** The harness passed no
  Stage 1 profile while `run_pipeline.py` did, so published accuracy was measured on inputs the
  production path never uses. Silent because `profile` is optional everywhere.
- **Rate limiting became fake accuracy.** 429s absorbed by the retry loop pushed Stage 3 into
  its regex fallback, and the run reported clean scores over heuristic output. This is why
  `verify_run.py` exists.
- **Two credential leaks, same shape.** A token in a URL query string, embedded by `requests`
  into an exception that got logged. Now headers only, with `redact_tokens()` on every log path.
- **Reels broke vision.** A Reel's `media_url` is an `.mp4`. Everything now resolves through
  `vision_image_url()`, which prefers `thumbnail_url`.
- **`DM_FOR_PRICE_RE` missed a plural**, escalating confirmed price-absences to vision as if
  they were uncertain.
- **Stale CDN URLs biased results by path.** Only golden sets were refreshed, not the raw dumps
  the chained path reads.

Each was caught by an artifact recording what happened, not what was configured to happen.

## Caveats

- **Per-stage accuracy is not system accuracy.** Stages are scored against gold labels, not
  against the previous stage's output. A chained run is measurably lower.
- **Paid pricing is applied to free-tier runs.** A projection, not observed spend.
- **The noise floor is large.** Re-running the same model on the same golden set moved
  per-signal F1 by 20 to 35 points, since most signals have 1 to 3 gold instances. Only
  macro-level gaps are safe to quote. Gadgets has zero gold instances for 3 of 7 signals.
- **One suspected gold-labelling error was never investigated** (`18079164065330268`).
- **Single-vendor model rankings did not survive a second vendor.**

## Known limits

- **Comment text is withheld by Meta.** `comments_count` is real, the array is always empty.
  Stage 6 diffs the count, so a `comment_delta` is not evidence Stage 4 read anything.
- **`estimated_cost_usd` in the token log is hardcoded `0.0`.** Every printed cost is
  `cost_per_call_usd` x call count, not real spend.
- **Carousel per-slide extraction was never built.** Every carousel attaches all slides as one
  gallery, so carousel classification is unmeasured.
- **Nothing runs Stages 1 to 6 end to end on live predictions.** `run_pipeline.py` chains 1 to 5
  and is a driver, not an orchestrator.

Research POC, sponsor-owned, published for reference. Not licensed for reuse.
