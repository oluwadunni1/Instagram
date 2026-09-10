# Validated Baseline — experiment record (opened 2026-09-09)

Companion to `FINDINGS.md`, not a replacement. `FINDINGS.md` remains the durable
record of everything up to 2026-09-08 and is still append-only; **nothing in it has
been edited or deleted.** This file starts a clean record for the re-baselining
effort, because the two are not comparable and mixing them would invite exactly the
apples-to-oranges quoting this work exists to end.

## Why a separate file

Every number in `FINDINGS.md` was measured **without the Stage 1 profile reaching
Stages 2/3/4** — the harness never passed it, while `scripts/run_pipeline.py` always
did (see the first entry below). Those figures were honestly measured on inputs the
production path does not use. They are not wrong; they are not comparable to
anything recorded here.

Two rules for this file:

1. **No number is recorded unless its run passed `scripts/verify_run.py`.** Every
   table names its `run_id`, auditable in `report/token_log.csv`.
2. A run that fails its gate is written up as a failure and discarded — never
   quietly repeated until it looks better.

## Status

| | vendor_autos_01 (30 posts) | vendor_gadgets_01 (41 posts) |
|---|---|---|
| family_gemini | ✅ validated | ✅ validated |
| family_llama | ✅ validated | ✅ validated (escalation caveated) |
| family_gpt4o_mini | ✅ validated | ❌ blocked: OpenRouter credit |
| family_qwen | ❌ failed gate x2 (model: invalid JSON) | ❌ blocked: OpenRouter credit |
| Stage 5 routing | ✅ Gemini, Llama, GPT-4o Mini | ✅ Gemini, Llama |
| Chained pipeline (Stages 1-5) | ✅ validated (18% attention) | ✅ validated (15% attention) |
| Stage 6 sync + incremental | ✅ validated (87% fewer calls) | not run — vendor 1 only by decision |

## Validated results ledger

**Every row below comes from a run that passed `scripts/verify_run.py`.** No row is
recorded without its `run_id`. Rows are per-stage harness scores (stages scored
independently against gold, not chained - see CLAUDE.md).

| vendor | family | run_id | S2 acc | S3 price | name sim | miss-price | S4 macro / **micro** | escalation | tokens |
|---|---|---|---|---|---|---|---|---|---|
| autos | **Gemini** | `family_gemini_20260909T104731Z` | **97%** (29/30) | **100%** (19/19) | **91%** | 6/6 | 87% / **88%** | 1/30 | **91,102** |
| autos | GPT-4o Mini | `family_gpt4o_mini_20260909T113412Z` | 93% (28/30) | 84% (16/19) | 87% | 6/6 | 77% / **76%** | 2/30 | 287,258 |
| autos | Llama | `family_llama_20260909T111801Z` | 87% (26/30) | 79% (15/19) | 87% | 6/6 | 48% / **51%** | 0/30 | 92,878 |
| gadgets | **Gemini** | `family_gemini_20260910T081202Z` | **93%** (38/41) | **100%** (31/31) | **81%** | 3/3 | 67% / **77%** | 5/41 | **115,281** |
| gadgets | Llama | `family_llama_20260909T115035Z` | 90% (37/41) | 87% (27/31) | 72% | 3/3 | 38% / **43%** | 5/41 †  | 136,193 |

† Llama's vendor-2 escalation rate is **inflated by the `max_tokens` bug**: 3 of
those 5 vision calls existed only to repair a truncated Pass A. True rate ~2/41.
Its accuracy figures stand; its escalation and cost figures do not.

### Comparability notes — read before putting these in one table

1. **Only the gadgets Gemini run has the `max_tokens` fix.** vendor_autos_01 tops
   out at 3 products/post so it never truncated - the three autos rows are
   unaffected. The gadgets Llama row predates the fix and carries the † caveat.
   A like-for-like gadgets comparison needs Llama re-run post-fix.
2. **Stage 4 denominators differ by vendor.** autos has 25 gold signals across 7
   types; gadgets has **5 across 4 types**. Any gadgets F1 moves in 20-point
   steps. Compare models within a vendor, never across.
3. **Use micro F1, not macro**, when comparing models - macro's denominator is
   gold ∪ predicted, so a model that hallucinates types is scored over more of
   them.
4. **Cost is tokens, not dollars.** `estimated_cost_usd` is hardcoded to 0.0.

### Stage 5 routing (zero LLM calls, all verified deterministic)

| vendor | family | auto_import | needs_attention | auto_exclude | attention rate |
|---|---|---|---|---|---|
| autos | **Gemini** | 18/30 | 7/30 | 5/30 | **23%** |
| autos | GPT-4o Mini | 16/30 | 11/30 | 3/30 | 37% |
| autos | Llama | 12/30 | 11/30 | 7/30 | 37% |
| gadgets | **Gemini** | 29/41 | 5/41 | 7/41 | **12%** |
| gadgets | Llama | 27/41 | 8/41 | 6/41 | 20% |

POC target is <=10%. Best achieved is 12% (Gemini, gadgets). The vendor matters
more than the model: gadgets is a catalog-poster account with prices in captions;
autos posts DM-for-price listings that are *correctly* routed to a human.

---|---|---|
| family_gemini (harness) | ✅ validated | pending — fresh quota |
| family_llama | ✅ validated | ✅ validated |
| family_gpt4o_mini | ✅ validated | ❌ failed gate twice (credit) |
| family_qwen | ❌ failed gate twice (model) | ❌ failed gate (credit) |
| Stage 5 routing | ✅ from validated predictions | pending |
| Chained pipeline | pending | pending |

Gemini free-tier usage on 2026-09-09: **152 calls** (75 discarded + 76 validated +
1 Stage 1). Observed daily ceiling is roughly 175 before sustained 429s.

---

## The harness never passed the Stage 1 profile — every prior number is on different inputs (2026-09-09)

**No models were run for this entry. It is a code-correctness finding that
invalidates the comparability of everything above it.**

`scripts/run_pipeline.py` has always passed the Stage 1 profile into Stages 2, 3
and 4 (`profile_dict`, at its three call sites). `eval/harness.py` never did — it
called `triage_fn(post)`, `extract_fn(post)`, `detect_fn(post)` with the post
alone. Both paths bind the same stage functions through the same
`load_stage_fn()`, and every one of those functions takes `profile` as its second
positional parameter defaulting to `None`, so the omission raised nothing and
logged nothing. The harness simply scored a different input than the pipeline
runs.

**Consequences, worst first:**

- **Stage 4's `[VENDOR]`/`[BUYER]` comment labelling has never fired in a scored
  run.** `_label_comments()` needs the profile to tell a vendor's own comment from
  a buyer's. Every Stage 4 P/R/F1 figure in this file was measured with that
  labelling switched off.
- Stages 2 and 3 lost the account-level context (`business_category`,
  `seller_style`, `pricing_behavior`) that their prompts are written to use.
- **Every harness number recorded before today is therefore not comparable to any
  number recorded after it.** This is not a claim that the old numbers were
  *wrong* — they were measured, honestly, on inputs the production path does not
  use.

Fixed in `eval/harness.py::resolve_profile()`, which loads the profile once per
run and threads it through all three scorers. The account label comes from
`--golden`'s stem (the existing convention, same as `vendor_id`), overridable with
a new `--account`. Two paths deliberately degrade to `profile=None` **with a
warning** rather than guessing: no `--golden` (which merges every vendor into one
run, so no single profile applies) and a missing raw dump.

Guarded by `tests/test_harness.py::test_profile_reaches_every_scored_stage`,
parametrised over all three scorers. The failure mode is silent — an optional
parameter that defaults to `None` — which is the same shape as the reserved-`model`
bug of 2026-09-07, and the reason it survived this long.

### `scripts/verify_run.py` — runs are now gated before their numbers are published

Nothing previously distinguished a real run from one that never reached a model.
The harness prints scores either way; `report/token_log.csv` is the only artifact
that knows the difference, because it records what actually reached litellm rather
than what the config said should. Three failures in this file are of that shape: a
run with zero logged rows reading as a clean 0% (2026-09-08), a throttled run whose
output is partly regex heuristic, and four "model comparisons" that ran one model
(2026-09-07).

`verify_run.py --run-id <id>` checks four things and exits non-zero on any:
rows logged at all; **zero `fallback_used=True` rows**; only `--expect-model`
strings called; call count within a band derived from `--posts`. `make verify
RUN_ID=…`; bare `--list` enumerates the log's run_ids.

`read_run_usage()` moved from `scripts/run_pipeline.py` to `pipeline/llm_client.py`,
next to its writer `log_token_usage()` and the shared `TOKEN_LOG_PATH`, so the gate
and the runner read the log through one function. Its tests moved to
`tests/test_token_log.py`; `tests/test_verify_run.py` covers the gate itself.

**First use found a real failure immediately.** Gating the most recent chained
`vendor_autos_01` run:

```
=== Verifying run pipeline_vendor_autos_01_20260908T225625Z ===
  [PASS] rows logged: 79 call(s), 87,574 tokens
  [FAIL] no regex fallback: 1 post(s) degraded to the zero-cost regex fallback
  [PASS] models match config: called gemini/gemini-3.5-flash, gemini/gemini-3.5-flash-lite
  [PASS] call count in range: 79 call(s), expected 30-120
NOT VALIDATED
```

One of 30 posts in that run carries heuristic output, not model output
(`stage3_extract_fallback`, 1 call, 0 tokens). The run's printed scores gave no
sign of it.

### Consequence for the planned re-baseline

The stakeholder figures are drawn from runs spanning 2026-09-01 to 09-08 and five
correctness fixes. The mixture is provable from file mtimes: for
`vendor_autos_01`, the Stage 2 predictions Stage 5 routes from are dated Sep 1
20:15, Stage 3 Sep 2 00:49, Stage 4 Sep 7 09:21 — which is why the recorded
17/10/3 routing split no longer reproduces (current tree: 18/9/3). One stage was
regenerated; two were not.

Every experiment therefore gets re-run on current code, both vendors, using the
four `family_*.yaml` configs (one harness invocation scores all three stages per
family — 8 runs, not 24), with `verify_run.py` gating each. A run that fails its
gate is discarded and repeated, not reported.

### First gated run failed, and found a gap in the gate itself (2026-09-09)

`family_gemini_20260909T103100Z`, vendor_autos_01, 75 calls. **Discarded, not
published.**

```
[FAIL] no regex fallback: 4 post(s) degraded to the zero-cost regex fallback
```

**Cause was expired CDN URLs, not throttling.** Two posts raised
`ImageFetchError: 403` from `fbcdn.net` on vision escalation — the `oh=`/`oe=`
params in the golden set's `media_url` fields had aged out. The same two posts are
the Stage 3 price misses (`predicted=None` against gold ₦32.6M and ₦123M). So
Stage 3 answered 4 posts with regex heuristic, making its 89% price accuracy and
100% missing-price recall a model/heuristic mixture. The printed scores gave no
sign of this; only the gate did.

Fixed by `scripts/refresh_media_urls.py` (Instagram Graph, not Gemini — costs
nothing against the free-tier budget): 30/30 `media_url` fields and 60 product
image URLs refreshed. Verified that **only URL fields changed** — every hand label
in the golden set is byte-identical, checked field-by-field against a backup.
`eval/golden/` is irreplaceable, so that check is not optional before running that
script.

**The gate had a blind spot, and this run walked straight through it.** All four
log-derived checks passed while two posts produced no result at all. An errored
post never reaches litellm, so it writes no token-log row — the log structurally
cannot see it. The harness records it as an `{"post_id": …, "error": …}` entry in
its per-post predictions JSON, which is the only durable artifact that knows.

Added check 5, `--predictions` (repeatable, one per stage file), counting entries
carrying an `error` key. It is deliberately not log-derived. Re-gating the failed
run now reports both problems and names the offending posts:

```
[FAIL] no errored posts: 2 errored of 72 scored post-entries
         17975827368107390: litellm.BadRequestError: ... Status code: 403 ...
         18073956491710644: litellm.BadRequestError: ... Status code: 403 ...
```

Writing the test for this found a second bug: an unreadable or missing
predictions file counted as "no errors found", so a run whose predictions never
landed would have PASSed. Unreadable files are now counted separately and are
equally fatal.

**Other observations from the discarded run, not published as results:**

- **Escalation rate 0/30.** Pass B fired only on the two posts that then 403'd, so
  `gemini-3.5-flash` (the configured `vision_model`) was never successfully called.
  The run was flash-lite text-only. The gate reports a configured-but-never-called
  model as information rather than failure — a cascade that legitimately never
  escalates should not be pushed into escalating for the gate's sake.
- **`pricing_behavior: captions` did not hurt missing-price recall.** All 6 of 6
  missing-price posts were among those that ran cleanly. The concern raised when
  the profile was regenerated was unfounded.

### Run 1 VALIDATED — family_gemini, vendor_autos_01 (2026-09-09)

`run_id: family_gemini_20260909T104731Z` — the first fully gated run in this
project. All five checks pass: 76 calls / 91,102 tokens logged, **0 fallback
rows, 0 errored posts**, both configured models actually called, call count in
range. This is the first number in this file that is safe to publish as measured.

**Inputs**, stated because they are now part of the result: Stage 1 profile
`category=automotive style=catalog_poster pricing=captions
vendor=ayodele.akinbohun`, generated by `gemini-3.5-flash-lite` on 2026-09-09,
threaded into all three stages. Golden set URLs refreshed the same day.

| Stage | Metric | Result |
|---|---|---|
| 2 | Accuracy | 29/30 = **97%** |
| 2 | Pass A only (not escalated) | 28/29 = 97% |
| 2 | Pass B vision (escalated) | 1/1 = 100% |
| 2 | Escalation rate | 1/30 = 3% |
| 3 | Price accuracy (when price exists) | 19/19 = **100%** |
| 3 | Name similarity (fuzzy, free) | **91%** over 25 products |
| 3 | Missing-price recall (brief s11) | 6/6 = **100%** OK |
| 4 | Macro-averaged F1, 7 signal types | **87%** |

Stage 4 per-signal: `finance_available` / `sold` / `price_negotiable` 100% F1,
`urgent` 93%, `stock_count_known` 83%, `swap_deal` 80%, `clearance` 50%
(precision 1/3 — it over-fires on urgency language).

**Against the discarded run** (same config, stale URLs), the deltas are the cost
those two 403s were imposing: Stage 2 90% → 97%, Stage 3 price 89% → 100%, name
similarity 81% → 91%. Both previously-failing posts are now correct in both
stages, and Stage 3 Pass B ran on 4 posts (11,439 tokens) where it had fallen to
regex on those same 4.

**Stage 3's 19/19 exactly reproduces the pre-profile recorded figure.** That is
agreement, not a copied number: it means threading the profile did not move price
extraction on this vendor. Name similarity did move, so the profile is reaching
the prompt and having an effect — just not on prices.

**Escalation is low, and the report should say so.** 5 vision calls across 30
posts (1 in Stage 2, 4 in Stage 3). The cascade is genuinely exercised, but this
vendor's captions are informative enough that Pass B is rarely needed — which is
why the run is cheap. Describing `family_gemini` as a vision cascade without that
number attached would overstate what actually runs.

### Run 9 — Stage 5 routing from the validated run 1 predictions (2026-09-09)

Zero LLM calls; deterministic Python over run 1's cached predictions. **Verified
deterministic**: two consecutive invocations on the same prediction set produced
byte-identical output.

| Bucket | Result |
|---|---|
| auto_import | 18/30 (60%) |
| needs_attention | 7/30 (23%) |
| auto_exclude | 5/30 (17%) |

**This supersedes both previously recorded splits for vendor 1.** FINDINGS.md
recorded 17/10/3; the current tree reproduced 18/9/3. Neither was one run — the
Stage 2/3/4 predictions they routed from were dated Sep 1, Sep 2 and Sep 7
respectively, spanning five correctness fixes. **18/7/5 is the first split routed
from three prediction files produced by a single gated run.**

The movement against 18/9/3 is explained by run 1's better Stage 2: two posts that
previously routed to `needs_attention` now classify confidently into
`auto_exclude` (`ad_creative`, `announcement`). Attention traffic fell 9 → 7,
exclusions rose 3 → 5.

**The POC target is missed, and by a wide margin.** Brief target is ≤10 attention
items per 100 posts; this is 23 per 100. Five of the seven are "Missing price —
set manually or mark as DM for price". That is not an extraction failure — Stage 3
scored 100% price accuracy and 100% missing-price recall on this same run. The
pipeline is correctly identifying posts where **no price exists in the caption**,
and routing them to a human as designed. The target and the vendor's posting
behaviour are in tension; hitting ≤10% on this vendor would require either
auto-classifying DM-for-price posts as importable-without-price, or accepting
invented prices. Neither is acceptable under brief section 11.

This is a product finding to put to the stakeholder, not a number to tune away.

### OpenRouter credit confirmed live — the three paid families are unblocked (2026-09-09)

`family_llama.yaml`, `family_qwen.yaml` and `family_gpt4o_mini.yaml` each carried a
`note:` saying **UNRUN AS OF 2026-09-09**, because OpenRouter stood at credits 0 /
usage $0.197 and no call from those families could succeed. Those notes also
demanded a 2-post smoke test with the family's own model strings visible in
`report/token_log.csv` before any number from them is published.

Smoke test run as specified — `run_pipeline.py --config family_llama.yaml
--limit 2`:

```
TOTAL  7 calls  8,873 tokens  (4,436/post)
models actually called: openrouter/meta-llama/llama-3.1-8b-instruct,
                        openrouter/meta-llama/llama-4-scout
```

Both configured models appear, including `llama-4-scout` on the Stage 3 vision
Pass B, so the cascade's escalation path works on this family and not just its
text model. Credit is live. The blocking note on all three configs is satisfied
for `family_llama`; the other two are covered by their own gated runs below.

Cost signal worth carrying forward: **4,436 tokens/post** on this 2-post sample,
against ~1,199 tokens/post for the validated Gemini run (91,102 tokens / 76 calls
over 30 posts). Small sample, but the paid families are not merely
"Gemini-but-paid" — they are markedly more token-hungry per post, and
`estimated_cost_usd` in the token log is still hardcoded to 0.0, so real spend must
be computed by hand from `prompt_tokens`/`completion_tokens` against OpenRouter's
published rates.

### Runs 3–8 — the three paid families, vendor_autos_01 (2026-09-09)

Three harness runs, one per family, each scoring Stages 2/3/4. **Two validated,
one failed its gate twice and is not published as a result.**

| Family | run_id | Gate |
|---|---|---|
| Llama | `family_llama_20260909T111801Z` | ✅ VALIDATED |
| GPT-4o Mini | `family_gpt4o_mini_20260909T113412Z` | ✅ VALIDATED |
| Qwen | `family_qwen_20260909T112335Z`, retry `…T113929Z` | ❌ FAILED (both) |

#### Validated comparison, vendor_autos_01 (30 posts)

| | Gemini | GPT-4o Mini | Llama |
|---|---|---|---|
| Stage 2 accuracy | **29/30 = 97%** | 28/30 = 93% | 26/30 = 87% |
| Stage 3 price accuracy | **19/19 = 100%** | 16/19 = 84% | 15/19 = 79% |
| Stage 3 name similarity | **91%** | 87% | 87% |
| Missing-price recall (s11) | 6/6 = 100% | 6/6 = 100% | 6/6 = 100% |
| Stage 4 macro F1 | **87%** | 77% | 48% |
| Stage 2→B escalation | 1/30 | 2/30 | 0/30 |
| Total tokens | **91,102** | 287,258 | 92,878 |
| Tokens/post | **3,037** | 9,575 | 3,096 |

**Gemini wins every axis and is the cheapest — and it is the free-tier model.**
On this vendor the paid families bought nothing. That is the headline, and it holds
under gating: all three runs had 0 fallbacks, 0 errored posts, and only their
configured models in the log.

**GPT-4o Mini's cost is concentrated in vision, not volume.** 287k tokens for the
same 30 posts, 3.2× Gemini. Nearly all of it is Pass B: 6 Stage 3 vision calls
consumed **163,339 tokens** (~27k/call) and 2 Stage 2 vision calls another 51,379
(~26k/call). Gemini's 4 Stage 3 Pass B calls took 11,439 total (~2.9k/call) — a
~9× per-vision-call difference. Prompt tokens dominate everywhere (278,938 of
287,258 for GPT-4o Mini), so this is image-encoding cost, not verbosity.

**No USD figure is given here on purpose.** `estimated_cost_usd` in the token log
is hardcoded to 0.0 and every "Cost: $X" the harness prints is
`cost_per_call_usd` × call count, not spend. Real cost must be computed from the
prompt/completion counts above against each provider's current published rates.
The token counts are the durable measurement; the rates are not ours to assume.

#### Qwen failed the gate twice, identically — this is the finding

Both runs errored on **the same two posts**, at the same stage, with the same
message:

```
[FAIL] no errored posts: 2 errored of 72 scored post-entries
   17897690802571800: [stage2_triage_pass_a] Failed to get valid structured
                      output from openrouter/qwen/qwen-2.5-7b-instruct after 3
   18073956491710644: [stage2_triage_pass_a] Failed to get valid structured
                      output from openrouter/qwen/qwen-2.5-7b-instruct after 3
```

`complete_structured()`'s retry-with-correction exhausted all 3 attempts. This is
not a transient failure and repeating it further would be re-rolling for a better
number, so it stops here. **Qwen 2.5 7B cannot reliably emit schema-valid JSON on
this task: 2/30 = 7% hard failure.**

Both failing posts have **short captions** — `'Happy new month to all our clients
worldwide'` (44 chars) and `'Available in Lagos 📍'` (20 chars). Every other family
handled both. A short caption leaves the model less to anchor on, and Qwen appears
to degrade into unparseable output rather than a low-confidence answer.

Qwen's scored figures (Stage 2 83%, Stage 4 macro F1 **12%**) are recorded here as
context for the failure, **not as published results** — they were produced by a run
that did not pass its gate, and the 2 errored posts are charged as misses within
them.

#### Two things flagged as suspect, not concluded

- **Llama and Qwen both escalated 0/30 at Stage 2.** Llama nonetheless made 5
  Stage 3 Pass B calls, so the vision path works; Stage 2 simply never asked for
  it. Combined with Llama's 48% and Qwen's 12% Stage 4 F1, the pattern to rule out
  is **confidently-wrong Pass A output** — high confidence suppresses escalation,
  so a model that does not know what it does not know never escalates. Worth
  checking Pass A confidence distributions before treating the low escalation rate
  as efficiency.
- **Missing-price recall is 6/6 on all four families**, the brief's hard
  requirement. Four-for-four on a 6-post denominator is weak evidence. It holds;
  it should not be leaned on until vendor 2 adds its own missing-price posts.

### Vendor 2, paid families — one validated, one stopped by OpenRouter credit (2026-09-09)

| Family | run_id | Gate |
|---|---|---|
| Llama | `family_llama_20260909T115035Z` | ✅ VALIDATED (92 calls, 136,193 tokens) |
| GPT-4o Mini | `family_gpt4o_mini_20260909T115848Z` | ❌ FAILED — 4 fallback rows |

GPT-4o Mini's failure is **HTTP 402 from OpenRouter** — credit/in-flight budget
exhausted, not a model defect:

```
402 ... raises your in-flight budget ... given your current in-flight requests.
Failed to get valid structured output from openrouter/openai/gpt-4o-mini after 3 attempt(s)
```

Four Stage 3 posts fell to the regex heuristic. The run is discarded. Paid runs
stopped here rather than retrying into the same wall.

That run alone drew **416,093 tokens** (vs Llama's 136,193 for the identical 41
posts, a 3.1× gap that matches the 3.2× seen on vendor 1). GPT-4o Mini's vision
Pass B is the single most expensive thing measured in this baseline.

#### A methodology flaw in `score_stage4`'s macro F1 — do not compare it across models

The raw output invited a wrong conclusion:

```
gpt4o_mini gadgets: Macro-averaged F1 across 4 signal type(s): 100%
llama      gadgets: Macro-averaged F1 across 7 signal type(s):  38%
```

Same golden set, same posts. The denominators differ because `score_stage4`
derives its signal-type set from **gold ∪ predicted**. A model that hallucinates
signal types gets extra zero-F1 rows and a lower macro average; a model that
predicts only what exists is scored over fewer types. **The macro F1 is therefore
not comparable between two models that predicted different type sets.**

Pooling the per-post counts (micro-average) gives the honest comparison:

| | tp | fp | fn | micro P | micro R | micro F1 |
|---|---|---|---|---|---|---|
| GPT-4o Mini | 5 | 0 | 0 | 100% | 100% | **100%** |
| Llama | 5 | 13 | 0 | 28% | 100% | **43%** |

GPT-4o Mini genuinely did better — both caught all 5 gold signals, but Llama
emitted 13 false positives against 0. The *direction* survives; the *magnitude*
(100 vs 38) was an artefact. Micro F1 should be reported alongside macro F1, or
the type set fixed to the gold vocabulary.

**Separately, vendor 2's Stage 4 is close to unmeasurable.** 41 labelled posts
carry **5 gold signals total** across 4 types (`sold` 2, `clearance` 1,
`finance_available` 1, `price_negotiable` 1). Any F1 on that denominator moves in
20-point steps. GPT-4o Mini's "100%" means it got 5 of 5 — worth knowing, not
worth a percentage sign. Vendor 1 (30 posts, 7 types) is the only account where
Stage 4 currently has enough labels to compare models on.

### Qwen and GPT-4o Mini on vendor 2 — both lost to OpenRouter credit, not to the models (2026-09-09)

Both runs discarded. **Neither tells us anything about the models**, and the
figures below are recorded only so nobody mistakes them for results later.

| Run | run_id | Outcome |
|---|---|---|
| Qwen, vendor 2 | `family_qwen_20260909T122159Z` | ❌ 6 fallbacks, 5 errored posts |
| GPT-4o Mini, vendor 2 retry | `family_gpt4o_mini_20260909T123511Z` | ❌ killed mid-run: 13 fallbacks, 11 credit errors by call 76 |

Every error on both runs is the same:

```
litellm.APIError: OpenrouterException - {"error":{"message":"This request
requires more credits, or ...
```

A 2-post smoke test immediately beforehand passed, which is why these were
launched — 2 posts are cheap enough to clear a nearly-empty balance. A 41-post
run is not. **A passing smoke test proves the credential works, not that the
budget covers the run.** Size the smoke test to the balance, or check the balance
directly, before committing a full run.

The GPT-4o Mini retry was killed rather than left to finish: it had already
failed its gate by call 76 and was spending the remaining balance on output that
could not be published either way.

Ungated Qwen vendor-2 figures, **not results**: Stage 2 32/41 = 78% (5 errored),
Stage 3 price 26/31 = 84%, name similarity 62%, missing-price 3/3, Stage 4 macro
F1 **0%** — zero signals predicted across all 41 posts.

**That Stage 4 zero cannot be attributed yet.** Stage 4 made only 8 calls for 41
posts — its regex prefilter gates the LLM entirely, so ~33 posts never reached a
model, and the 8 that did returned nothing. That is a different failure shape from
vendor 1's unparseable-JSON errors, and the credit failure is confounded with it.
Re-run before drawing any conclusion.

#### Still open: is Qwen's failure specific to short captions?

Vendor 1 showed Qwen failing twice, identically, on two **short-caption** posts
(20 and 44 chars) with exhausted JSON-repair retries. Vendor 2's captions are more
structured (catalog-poster account), so a clean vendor-2 run is exactly the
contrast that would localise the failure to vague captions rather than to the task
in general. This run was corrupted by budget before it could answer that.
**Open question, not a closed finding.**

### OpenRouter's free tier no longer covers these models (2026-09-09)

The two vendor-2 reruns could not be attempted. The key in `.env` reaches
**nothing** — both the paid and the free path are closed:

| model | status | message |
|---|---|---|
| `qwen/qwen-2.5-7b-instruct` | 402 | "Insufficient credits. This account never purchased credits." |
| `openai/gpt-4o-mini` | 402 | same |
| `qwen/qwen-2.5-7b-instruct:free` | **404** | "This model is unavailable for free. The paid version is available now" |
| `meta-llama/llama-3.1-8b-instruct:free` | **404** | same |

These accounts are new OpenRouter accounts relying on a small free-trial
allowance rather than purchased credit. **That route is gone for these models** —
OpenRouter has retired the `:free` variants and now redirects to the paid slug.
This is not a key/org mismatch: it is the free tier itself no longer covering the
models this experiment uses.

Consequence for the baseline: the validated Llama runs (both vendors) and the
validated GPT-4o Mini run (vendor 1) got in on the previous account before it was
exhausted. **Completing vendor 2 for Qwen and GPT-4o Mini now requires purchased
credit** — roughly $0.07 for GPT-4o Mini's ~416k tokens at current rates, cents
for Qwen. Gemini is unaffected (separate free tier, own quota).

`total_credits`/`total_usage` both read `$0` on this account, so **the balance
endpoint cannot distinguish "never funded" from "free allowance intact"**. A
1-call probe against the actual model is the only reliable pre-flight check, and
it costs one call.

#### Fixed: a permanent 402 was being retried as if transient

`_TRANSIENT_EXCEPTIONS` includes `litellm.exceptions.APIError`, and litellm
raises OpenRouter's 402 as exactly that. So an exhausted balance consumed **6
retries with exponential backoff (2+4+8+16+32+64 ≈ 2 minutes) per post** before
failing. On a 41-post run that is over an hour of waiting to produce output that
cannot be published — and it is why the three credit failures today took so long
to diagnose rather than announcing themselves.

`complete_structured()` now fast-fails on a permanent provider error
(`_is_permanent_provider_error()`), matched narrowly on message text because
litellm does not reliably surface the HTTP status on the exception. It only ever
converts a slow failure into a fast one with the same outcome; genuine 429/5xx
still back off exactly as before, pinned by `tests/test_llm_client_retry.py`.

### What can and cannot be recovered from the pre-2026-09-09 runs

Asked directly: can the earlier runs in `FINDINGS.md` fill the gaps in this
baseline? **Partially auditable, but not validatable — they cannot fill the gaps.**

**Recoverable offline (done, zero LLM calls):**

*Errored posts*, counted from the per-post prediction JSONs still on disk. This
found real damage nobody had recorded:

| file | entries | errored |
|---|---|---|
| `vendor_gadgets_01/stage4_openrouter_openai_gpt-4o-mini` | 41 | **8** |
| `vendor_autos_01/stage2_cascade_see_text_model_vision_model` | 30 | **30** |
| `vendor_gadgets_01/stage2_cascade_see_text_model_vision_model` | 41 | 3 |
| `vendor_autos_01/stage4_openrouter_meta-llama_llama-3.1-8b` | 17 | 1 |
| `vendor_*/stage2_gemini-3.5-flash-lite_…` | 30 / 41 | 1 each |

The gadgets GPT-4o Mini Stage 4 run errored on **8 of 41 posts and scored 0 tp** —
its published figure describes a broken run.

*Micro-F1*, recomputed from the same files, correcting the macro denominator flaw.

**Not recoverable at any price:**

- **Fallback status.** The first `stage3_extract_fallback` row in the token log is
  `2026-09-08T22:25:40Z`. Every run before that is *uninstrumented*, so its `0
  fallbacks` means **"cannot tell"**, not "clean". `verify_run.py` cannot honestly
  gate them, and a gate that passes an unmeasurable run is worse than none.
- **The profile gap.** Old runs fed Stages 2/3/4 different inputs. No offline
  recomputation fixes that; only re-running does.

So old runs can be *audited* but never *validated*, and none may enter this file
as a result.

#### Correction: the macro-F1 flaw is real but narrower than first stated

Earlier in this file I warned that every Stage 4 macro F1 in `FINDINGS.md` was
untrustworthy. Recomputing both averages for every Stage 4 prediction file on disk
shows that **overstated it.** Macro and micro track each other closely, and every
ranking survives:

| vendor | model | macro | micro |
|---|---|---|---|
| autos | Gemini (family, validated) | 87% | 88% |
| autos | GPT-4o Mini (family, validated) | 77% | 76% |
| autos | Llama (family, validated) | 48% | 51% |
| autos | Qwen (family, ungated) | 12% | 15% |
| autos | gemini-3.5-flash-lite (2026-09-07) | 78% | 82% |
| autos | llama-3.1-8b (2026-09-07) | 40% | 43% |
| autos | qwen-2.5-7b (2026-09-07) | 10% | 21% |

The old 2026-09-07 comparison (Gemini 78 / Llama 40 / Qwen 10) **reproduces
exactly** under macro, and micro moves it to 82 / 43 / 21 — same ordering, same
conclusion. The distortion is real only where the denominator is tiny: vendor 2's
5 gold signals gave GPT-4o Mini "100% over 4 types" against Llama's "38% over 7".
**Report both averages; distrust either when gold signals are in single digits.**

### Runs 9–16 — Stage 5 routing from all four validated prediction sets

Zero LLM calls, deterministic.

| vendor | family | auto_import | needs_attention | auto_exclude | attention rate |
|---|---|---|---|---|---|
| autos | **Gemini** | 18/30 | 7/30 | 5/30 | **23%** |
| autos | GPT-4o Mini | 16/30 | 11/30 | 3/30 | 37% |
| autos | Llama | 12/30 | 11/30 | 7/30 | 37% |
| gadgets | Llama | 27/41 | 8/41 | 6/41 | 20% |

**Gemini routes the least work to humans on vendor 1** — 7 attention items against
11 for both paid families — which is the same ordering as its Stage 2/3 accuracy.
A weaker upstream stage does not just score lower; it pushes ~60% more posts onto
a human. No family meets the ≤10% POC target on either vendor.

### Gemini vendor 2, attempt 1 — failed the gate on 503s, and disproved the quota ceiling (2026-09-09)

`family_gemini_20260909T142147Z`, 88 calls, 101,188 tokens. **Discarded.**

```
[FAIL] no regex fallback: 4 post(s) degraded to the zero-cost regex fallback
[FAIL] no errored posts: 1 errored of 116 scored post-entries
       18351809149247806: litellm.ServiceUnavailableError: GeminiException
```

**The cause of THIS run's failure is 503 Service Unavailable, not quota.** It was
launched with 152 Gemini calls already spent, against the "~175/day before
sustained 429s" figure observed on 2026-09-08, and made 88 more with zero 429s.

**RETRACTED — see the correction below.** On the strength of that I wrote here
that the daily ceiling "does not reproduce" and that the free tier is limited by
availability rather than a call budget. That was wrong, and it was wrong in the
direction that costs quota: a ceiling does exist. It was simply much higher than
the recorded figure. The first genuine `429 RESOURCE_EXHAUSTED` of the day
arrived at **~418 Gemini calls**, roughly 2.4x the 2026-09-08 observation. Plan
against ~400/day, and treat any single-figure ceiling in this repo's history as a
lower bound observed under different conditions, not a measured limit.

Ungated figures, **not results**: Stage 2 38/41 = 93% (escalation 4/41, Pass B
4/4 correct), missing-price 3/3. All 4 fallbacks are in Stage 3, so price accuracy
and name similarity are the contaminated fields; the Stage 2 numbers happen to be
clean but are not published from a failed run.

#### The micro-F1 fix earned its place on first live use

```
Macro-averaged F1 across 6 signal type(s): 67%
Micro-averaged (pooled tp=5 fp=3 fn=0): P=62% R=100% F1=77%   <- use THIS
  (2 signal type(s) predicted but never in gold: stock_count_known, urgent
   - these depress macro F1 only)
```

Exactly the distortion documented above: two hallucinated signal types pulled
macro 10 points below micro. Previously that gap was invisible and would have been
read as Gemini scoring worse on vendor 2 than it does.

### ROOT CAUSE: Stage 3 truncated multi-product posts and fell back to regex, silently (2026-09-09)

Three vendor_gadgets_01 posts fell back to the regex heuristic on **every** run —
Gemini twice and GPT-4o Mini once. Diffing the failing post IDs across attempts is
what exposed it; the counts alone (4, then 3) looked like flakiness.

| post | products | est. output tokens |
|---|---|---|
| `17944115412074853` | 9 | ~850 |
| `18103239884214338` | 9 | ~850 |
| `18056670212554989` | 15 | ~1,410 |

`complete_structured()` caps output at `max_tokens=1024` and **Stage 3 never
overrode it.** A multi-product catalog post needs one JSON object per product
(~94 tokens each, measured). Past the cap the model emits truncated JSON, schema
validation fails, the 3 repair retries burn, and `extract_product()` falls back to
`_regex_fallback()` — while the harness prints a clean score over heuristic output.

**Why it hid for a week:** `vendor_autos_01` tops out at **3 products/post** and
never trips it, which is why every vendor-1 run validated first time.
`vendor_gadgets_01` has 8 posts above 3 products, up to 15. The bug is
model-independent and fully deterministic — it is a property of the account's
posting style, and it would silently degrade production for any catalog vendor.

Fixed: `stage3_extract.DEFAULT_MAX_TOKENS = 4096`, threaded through
`extract_product()` → `_pass_a()`, overridable per experiment via a `max_tokens:`
key in a stage3_extract block (`load_stage_fn()` binds it like any non-reserved
key). Pinned by `tests/test_stage3_max_tokens.py`, including a test that catches
the call site dropping the value — which would fail silently.

**Effect, same config, same posts:**

| | attempt 1 | attempt 2 | with fix |
|---|---|---|---|
| Stage 3 fallbacks | 4 | 3 | **1** |
| Price accuracy | 27/31 = 87% | 28/31 = 90% | **30/31 = 97%** |
| Errored posts | 1 | 0 | **0** |
| Stage 3 Pass A tokens | 56,871 | 58,778 | 69,264 |

All three multi-product posts now extract correctly. **A 7-point price-accuracy
gain that was previously invisible** — the truncated posts were being answered by
regex and scored as if they were model output.

#### Correction: the daily Gemini ceiling exists, at ~418 calls

The one remaining fallback in the fixed run is a *different* post
(`17875545636631606`: empty caption, VIDEO, 1 product — no truncation involved).
Its error is `429 ... "status": "RESOURCE_EXHAUSTED"`, the first genuine quota
error of the day, at 418 Gemini calls. Its vision URL resolved correctly to the
`.jpg` thumbnail, so the Reels handling is sound.

`family_gemini_20260909T204649Z` is therefore **discarded**, one post short. Nothing
structural remains; a re-run on fresh quota should validate it.

### The truncation bug hid behind the cascade — and inflated Llama's escalation rate (2026-09-10)

Follow-up to the `max_tokens` root cause. Pass A and Pass B never shared an output
budget:

| | call path | max_tokens |
|---|---|---|
| Pass A | `complete_structured()` | **1024** (generic default, now 4096) |
| Pass B | `litellm.completion()` direct | **unset** — provider default |

`_pass_b()` bypasses `complete_structured()` (it needs multimodal message
shapes), and never passed a `max_tokens`. So vision was always uncapped while the
cheap text pass was capped — the opposite of what the cascade's cost logic
assumes.

That asymmetry produced two different outcomes from one bug:

- **Llama, vendor 2:** Pass A truncated at exactly 1024 completion tokens on all
  three multi-product posts. The resulting missing data triggered escalation, and
  the **uncapped Pass B recovered the full list** (1073, 1073, 1819 completion
  tokens) — predicting 9, 9 and 15 products against gold 9, 9 and 15. Correct
  answer, zero fallbacks, gate passed.
- **Gemini, vendor 2:** the same truncation produced unparseable JSON, exhausted
  the repair retries, and fell through to `_regex_fallback()`. Wrong answer.

**Consequence for a published figure.** Llama's validated vendor-2 run
(`family_llama_20260909T115035Z`) is *correct*, but its **12% escalation rate
(5/41) is inflated by the bug**: 3 of those 5 vision calls existed only to repair
a truncated Pass A. Its genuine escalation rate is ~2/41 ≈ 5%. Any cost or
"how often does the cascade need vision" claim drawn from that run overstates
Llama's vision usage by roughly 2.5x. The accuracy figures stand; the escalation
and cost figures do not.

This also means **zero fallbacks was never sufficient evidence of a clean Stage 3
run** on a multi-product account. A cascade that silently pays for a vision call
to repair a truncated text call looks identical, in the gate, to one that never
needed vision. Comparing completion tokens against the cap is what distinguishes
them — a Pass A row sitting at exactly `max_tokens` is the tell.

`_pass_b()` is deliberately left uncapped: unbounded output is the behaviour that
works, and capping it now would risk re-introducing the same failure on the vision
path. Pass A was the bug, not Pass B.

---

## Run 2 VALIDATED — family_gemini, vendor_gadgets_01 (2026-09-10)

`run_id: family_gemini_20260910T081202Z` — 89 calls, 115,281 tokens. All five
gate checks pass: 0 fallbacks, 0 errored posts, both configured models called.
First clean vendor-2 Gemini run, and the first to benefit from the `max_tokens`
fix.

Stage 1 profile: `category=gadgets style=catalog_poster pricing=mixed
vendor=oluwadunnioluajayi`, generated 2026-09-07 by `gemini-3.5-flash-lite` —
already the configured model, so unlike vendor 1 it needed no regeneration. Both
vendors now share the same Stage 1 model, which is what makes the cross-vendor
comparison valid.

| Stage | Metric | Result |
|---|---|---|
| 2 | Accuracy | 38/41 = **93%** |
| 2 | Pass A only (not escalated) | 33/36 = 92% |
| 2 | Pass B vision (escalated) | 5/5 = **100%** |
| 2 | Escalation rate | 5/41 = 12% |
| 3 | Price accuracy (when price exists) | 31/31 = **100%** |
| 3 | Name similarity (fuzzy, free) | **81%** over 34 products |
| 3 | Missing-price recall (brief s11) | 3/3 = **100%** OK |
| 4 | Macro F1 / **Micro F1** | 67% / **77%** (tp=5 fp=3 fn=0) |

**The `max_tokens` fix is worth 13 points of price accuracy on this vendor**, not
the 7 first measured: 87% (attempt 1) → 90% → 97% → **100%**. Every multi-product
post now extracts in full on the cheap text pass, with no vision repair.

Vision escalation is genuinely useful here and not merely repairing truncation:
5 escalations, **5/5 correct**, on an account whose captions are structured. Both
vendors now show the cascade earning its cost — vendor 1 at 1/30, vendor 2 at
5/41.

### Run 10 — Stage 5 routing, vendor_gadgets_01 (Gemini)

Zero LLM calls; verified deterministic (two consecutive runs byte-identical).

| Bucket | Result |
|---|---|
| auto_import | 29/41 (71%) |
| needs_attention | **5/41 (12%)** |
| auto_exclude | 7/41 (17%) |

Flag reasons: 2x missing price, 2x possibly-sold, 1x no products extracted.

**12% is the closest any run has come to the ≤10% POC target**, against vendor 1's
23%. The difference is the vendor, not the model: vendor 1's autos account posts
DM-for-price listings that are correctly routed to a human (Stage 3 scored 100%
price accuracy on both). A catalog-poster account with prices in captions is
simply more automatable. **The target is reachable for structured vendors and not
for DM-for-price vendors** — that is a product finding about vendor mix, not a
model-quality gap to tune away.

---

## Runs 17-18 VALIDATED — chained Stages 1-5, Gemini, both vendors (2026-09-10)

| vendor | run_id | posts | calls | tokens | gate |
|---|---|---|---|---|---|
| autos | `pipeline_vendor_autos_01_20260910T131337Z` | 40 | 101 | 116,201 | ✅ |
| gadgets | `pipeline_vendor_gadgets_01_20260910T132709Z` | 41 | 90 | 118,958 | ✅ |

| vendor | auto_import | needs_attention | auto_exclude | attention | products |
|---|---|---|---|---|---|
| autos | 26/40 (65%) | 7/40 | 7/40 | **18%** | 38 |
| gadgets | 29/41 (71%) | 6/41 | 6/41 | **15%** | 101 |

### What the chained run actually adds over Stage 5 on cached predictions

Fair challenge raised mid-run: if the per-stage predictions are already cached,
why re-send every post to Gemini instead of just routing what we have? Checked
rather than argued:

**autos — it adds nothing measurable.** All 25 posts Stage 2 *predicted* as
product listings already had a cached Stage 3 result, so piping live predictions
instead of gold labels changes no routing decision. **Error compounding is zero on
this vendor.** The chained 18% vs Stage 5's 23% is purely the denominator: the dump
carries 40 posts, the golden set 30, and the 10 extras can be routed but never
scored. 101 Gemini calls bought a `catalog.json` artifact and confirmed a figure
already derivable for free.

**gadgets — it adds the thing the chain exists for.** Golden set and dump are the
**same 41 posts, identical IDs**, so nothing is explained by denominators:

| | auto_import | attention | exclude | rate |
|---|---|---|---|---|
| Stage 5 from cached predictions (0 calls) | 29 | 5 | 7 | **12%** |
| Chained, live predictions (90 calls) | 29 | 6 | 6 | **15%** |

The 3-point gap **is error compounding** - one post Stage 2 misroutes, which
independent scoring structurally cannot see because `score_stage3` filters on
*gold* `post_type`, not on what Stage 2 predicted (CLAUDE.md). This is the first
direct measurement of it in the project.

**Consequence: the chain is a one-off deliverable per vendor, not a repeatable
measurement.** The plan's "run the Gemini chain twice per vendor and report the
range" is dropped. That instruction came from two runs disagreeing at 22% vs 17%
- both of which were *contaminated by fallbacks*. Now that runs are gated, the
disagreement is explained as fallback noise, not real variance, and re-running
would spend ~180 calls to re-derive Stage 5 output.

### Both fixes from the failed attempts are confirmed working

The first pair of chained runs failed the gate: autos on 5 x `ImageFetchError 403`,
gadgets on 3 x `RESOURCE_EXHAUSTED`.

**Stale raw dumps.** `refresh_media_urls.py` only ever refreshed `eval/golden/`,
but the chained path reads `runs/<account>/raw/dump_*.json`, which nothing
refreshed - the autos dump was 5 days old. So the harness path had fresh CDN URLs
and the production-shaped path did not, biasing chained results downward for
reasons unrelated to any model. Added `--dump`, refreshed both (40/40 and 41/41
rows). Result on autos: **4 fallbacks + 1 error -> 0 and 0**, attention 22% -> 18%.

**Gate blind spot on chained runs.** `verify_run.py --predictions` expected
per-stage prediction lists; a chained run writes one `catalog.json`, so check 5
reported `[skip]` and the autos run's errored stage call went unseen. The gate now
accepts a catalog (`items` + `errors`) alongside prediction lists. Both runs above
were verified through it - `0 errored of 40` and `0 errored of 41` are real checks,
not skips.

---

## Stage 6 sync, and the incremental path it feeds (2026-09-10)

The first end-to-end demonstration of the economic argument for Stage 6: a routine
re-sync finds most of a feed unchanged, and only what changed is reprocessed.

### Snapshot rebuild — encoders and hashers, zero LLM calls

Rebuilt `data/snapshots/ayodele.akinbohun/` from the golden set so the baseline is
reproducible and dated, resetting it to the 30 golden posts (the previous snapshot
had been advanced to 38 entries by earlier live syncs).

```
Total posts processed : 30
pHash                 : 30 succeeded / 0 failed
Embeddings            : 30 succeeded / 0 failed
Lifecycle: active 23 · excluded 5 · out_of_stock 2
```

**30/30 on both fingerprints, zero failures** — the golden-set URL refresh of
2026-09-09 is what made every image downloadable. Before that refresh this step would
have failed on expired CDN links, the same way Stage 2 vision did.

### Live sync — 40 fresh posts against the 30-post baseline

```
No-ops:          18  (0 AI calls)
New posts:        4  → needs import
Deleted:          0
Caption edits:    2  → needs Stage 3 re-run
Comment deltas:   1  → needs Stage 4 on delta
Other changes:   10  → content_hash moved, no caption/comment change
Repost matches:   4  (2 pHash, 2 embedding)
Repost merges:    2  (exact duplicate, auto-applied)
Vendor action required: 8 items
```

Report: `report/changes_autos_20260910.json`. **Zero language-model calls** — the whole
diff runs on content hashes, perceptual image hashes and caption embeddings.

Both repost-detection routes fired: 2 matches by pHash (near-identical image) and 2 by
caption embedding (reworded repost of the same item). Only exact duplicates auto-merge;
the looser matches are proposed and wait.

### The handoff — `run_pipeline.py --changes`

This did not exist. Stage 6 wrote a change report and nothing consumed it, so the
incremental saving was theoretical. Added `posts_needing_work()`, which selects the
post ids a sync says still need Stages 2-5:

| Change type | Selected | Why |
|---|---|---|
| `new_post` | ✅ | never seen; needs the full cascade |
| `caption_edit` | ✅ | the text every stage reads has changed |
| `comment_delta` | ✅ | Stage 4 reads comments for sold/stock signals |
| `content_changed` | ❌ | hash moved but caption and comment count did not — CDN churn |
| `repost_match` / `repost_merge` | ❌ | already resolved against an existing item |
| no-op | ❌ | byte-identical |

**Excluding `content_changed` is the load-bearing choice.** Ten of the 23 changes are
that type. Selecting them too would reprocess 16 of 40 posts instead of 6 and quietly
reinstate most of the spend Stage 6 exists to avoid. Pinned by three tests in
`tests/test_run_pipeline.py`, including the routine case where a sync finds nothing to
do and must therefore spend nothing.

### Result — VALIDATED

`run_id: pipeline_vendor_autos_01_20260910T160422Z`

```
Stage 6 handoff: changes_autos_20260910.json lists 6 post(s) needing Stages 2-5
Incremental run: 6 of 40 post(s) in the dump need work (85% skipped)
TOTAL  13 calls  14,288 tokens  (2,381/post)
```

Gate: 0 fallbacks, 0 errored posts, models match config.

| | Full feed | Incremental |
|---|---|---|
| Posts processed | 40 | **6** |
| Model calls | 101 | **13** |
| Tokens | 116,201 | **14,288** |
| Reduction | — | **87%** |

**This is the strongest cost result in the project**, and unlike the per-post token
figures it needs no currency conversion to be meaningful: a weekly re-sync of an
established vendor costs an eighth of a full rebuild, and the diff that decides so
costs nothing at all.

**Two things not to overclaim.** The 0% attention rate on those 6 posts is a 6-post
denominator, not a quality signal. And the 4 "new" posts are new *relative to the
30-post golden baseline*, not newly posted — rebuilding from golden is what makes the
run reproducible, but it means this sync partly re-discovers posts already in the dump.
A production sync against a live-advanced snapshot would find fewer, and the saving
would be larger, not smaller.
