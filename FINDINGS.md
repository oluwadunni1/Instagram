# Findings

Durable record of experiment results for the Instagram Catalog Pipeline POC.
Experiment configs live in `pipeline/config/experiments/`; raw per-post
predictions and `report/token_log.csv` are regenerated on every harness run
and gitignored (see README) - this file is where results that need to
survive past the next run get written down.

## Stage 2 Triage — Cascade Model Comparison (2026-09-01)

Golden set: `eval/golden/vendor_autos_01.json` (30 labeled posts, all with
`post_type` gold labels). Configs: `stage2_llama_cascade.yaml`,
`stage2_qwen_cascade.yaml`, `stage2_gemini_cascade.yaml` - each swaps only
Stage 2's Pass A (text) / Pass B (vision) models, `confidence_threshold`
held at 0.7 across all three for a fair comparison.

| Config | Pass A (text) | Pass B (vision) | Accuracy | Escalation Rate | Total Tokens |
|---|---|---|---|---|---|
| `stage2_llama_cascade` | Llama 3.1 8B | Llama 4 Scout | 27/30 = 90.0% | 0/30 = 0% | 11,447 |
| `stage2_qwen_cascade` | Qwen 2.5 7B | Qwen3-VL 8B | 23/30 = 76.7% | 0/30 = 0% | 11,351 |
| `stage2_gemini_cascade` | Gemini 3.5 Flash-Lite | Gemini 3.5 Flash | 27/30 = 90.0% | 2/30 = 6.7% | 14,467 |

All three routed through OpenRouter (Llama, Qwen) or Google AI Studio
(Gemini) - the exact model strings originally requested
(`groq/llama3-8b-8192`, `groq/llama-3.2-11b-vision-preview`,
`openrouter/qwen/qwen-2.5-7b-instruct:free`,
`openrouter/qwen/qwen-2-vl-7b-instruct:free`, `gemini/gemini-1.5-flash-8b`,
`gemini/gemini-1.5-flash`) were all retired by the providers as of this
date; see each YAML's `note` field for the specific replacement and why.

### Why Llama and Gemini land on the same accuracy despite different escalation rates

The two 27/30 scores are a coincidence of arithmetic, not equivalent
behavior - Llama and Gemini get **zero of the same posts wrong** (fully
disjoint error sets, verified against the saved
`report/stage2_*_predictions.json` files for this run).

Gemini's escalation genuinely worked as designed on 2 posts
(`17975827368107390`, `18073956491710644`, gold=`product_listing`):
Llama's Pass A misread both as `announcement` at **0.8 confidence** -
wrongly confident, and above the 0.7 threshold, so Llama's cascade never
escalates on them. Gemini's Pass A must have scored below 0.7 on these
same two posts, triggering Pass B, which then got both right at 1.0
confidence.

But Gemini has its own separate blind spot: 3 *other* posts where its
Pass A was wrong at **0.95 confidence** (again above threshold, so no
escalation) - misread `announcement` → `product_listing` twice and
`announcement` → `ad_creative` once (golds: `announcement`,
`announcement`, `testimonial_repost`). Llama got all 3 of these right on
Pass A alone.

Net effect: escalation fixed 2 of Llama's failure modes for Gemini, but
Gemini introduced 3 new ones elsewhere - same overall count, different
mechanism. The real finding: **both models are confidently wrong on their
own errors** (0.8-0.95 confidence on wrong answers). A confidence-threshold
escalation trigger only catches uncertainty a model admits to - it's not a
reliable safety net against a model that's simply wrong and sure of it.

Qwen doesn't get even that partial benefit: worse accuracy (76.7%) *and*
the same 0% self-reported-uncertainty problem - its Pass A is both less
accurate and equally unable to recognize when it should escalate.

### Known gap surfaced during this run (fixed in the Stage 3 schema upgrade below)

Every `stage2_*.yaml` config's `stage3_extract` block points `module` at
the real `pipeline.stages.stage3_extract` (which makes live Gemini calls)
while labeling it `model: dummy-heuristic` - it only falls back to the
actual zero-cost regex heuristic when the Gemini call fails. So every
"Stage 3 cost: $0.0000" line logged during these runs was a live Gemini
call, not a dummy stub - harmless cost-wise (free tier), but the label is
still misleading (not fixed - would mean changing every experiment YAML's
`model` label). Token logging for these calls, however, is now wired up -
see below.

## Stage 3 Extraction — Output Schema Upgrade (2026-09-01)

`pipeline/stages/stage3_extract.py`'s Pydantic models and `SYSTEM_PROMPT`
were extended to match the full POC spec: `ProductItem` gained
`description`, `variants`, `images` (always `[]` - populated separately
from cached carousel slides), a `quantity_signal` object
(`kind`/`evidence`, same vocabulary as the golden-set relabel above), a
`negotiation_signal` string, and `price.confidence`. The `product_name`
field was also renamed to `name`, matching the golden set directly (no
more dual `product_name`/`name` key mirroring in `_to_predictions()`).
`eval/harness.py`'s scoring logic was **not** touched - it only ever reads
`predicted[0]["name"]` and `predicted[0]["price"]["value"/"source"]`,
everything else rides into `report/stage3_*_predictions.json` via
`raw_result` unscored.

Golden set: `eval/golden/vendor_autos_01.json`, default config
(`gemini/gemini-3.5-flash-lite`, same cascade architecture as Stage 2).

| Metric | Before schema upgrade | After schema upgrade |
|---|---|---|
| Price accuracy (price exists) | 19/19 = 100% | 19/19 = 100% |
| Name similarity (fuzzy, 25 products) | 91% | 91% |
| Missing-price recall | 6/6 = 100% | 6/6 = 100% |
| Escalation rate (Pass A → vision Pass B) | not logged | 9/25 = 36% |
| Total tokens (Pass A + Pass B) | not logged | 72,243 |

Scoring is byte-identical before/after, confirming the new fields are
additive and didn't disturb existing extraction quality. The last two rows
are new: Stage 3 had never been wired to `log_token_usage()` before this
task (the gap noted above) - `_pass_a` now passes `run_id`/`vendor_id`
through `complete_structured()`, and `_pass_b` (which bypasses
`complete_structured()` for its multimodal image message) now logs
directly, both mirroring `stage2_triage.py`'s existing pattern. This
required one additive line in `eval/harness.py`'s `main()` (passing
`run_id`/`vendor_id` into the existing generic `load_stage_fn(...,
"stage3_extract")` call, same as Stage 2 already had) - no scoring logic
changed.

Token breakdown: 25 posts have gold products, all take a Pass A call
(46,354 tokens); 9 of those escalate to Pass B (25,889 tokens) - a **36%
escalation rate**, notably higher than any Stage 2 cascade measured above.
Worth a closer look before assuming Stage 3's cascade is well-tuned: per
`_needs_escalation()`, Stage 3 escalates on a missing name, a missing
price, *or* confidence below 0.6 - a looser trigger than Stage 2's
confidence-only threshold, which may explain the gap. All 9 escalations
completed successfully this run (no rate-limit fallback triggered), though
an earlier run against the same config did hit one transient Gemini 429
mid-cascade and fell back to the zero-cost regex heuristic for one post -
expected graceful degradation, not an error, but a reminder that token
totals here can vary run-to-run whenever a fallback fires.

### Root cause of the 36% escalation rate

Re-ran Pass A in isolation for all 9 escalated posts and checked which of
`_needs_escalation()`'s three conditions actually fired. Confidence never
does - Pass A reported 0.9-1.0 confidence on every one of these, so that
trigger is dead weight on this dataset. Every escalation comes down to
`price.source == "none"`, which fires for two very different reasons the
code can't currently tell apart:

| Cause | Count | Outcome |
|---|---|---|
| Genuinely no price anywhere (DM-for-price, or nothing stated) | 6/9 | Pass A correctly nulled the price per the "never invent a price" rule. Escalating re-asks vision the same unanswerable question - Pass B re-confirmed "none" every time (100% harmlessly, no hallucination), but at 2x cost for zero information gain. |
| Price only visible as text overlaid on the photo | 2/9 | Pass A (text-only) genuinely can't see it. Pass B correctly recovered it (`17975827368107390`, `18073956491710644`) - escalation working exactly as designed. |
| Price stated plainly in the caption, but Pass A missed it | 1/9 | `18051759722796086` (Tesla Model 3) - caption states "Price range: $34,000 – $38,000," Pass A returned `none` anyway. Pass B then got it right (`source: "caption"`, matching gold) - but by re-reading the same caption text a second time, not by looking at the image. Two live theories: `PriceModel` has no way to represent a range (only a single `value`), and/or the vendor's own "This unit is officially SOLD!" comment confused Pass A into thinking no current price applies - the same caption-vs-comment conflation ruled out for `quantity_signal` during the golden-set relabel, showing up on the price field instead. |

**The actual finding**: `_needs_escalation()` treats "Pass A confidently
found no price" and "Pass A is unsure whether a price exists" as the same
signal. They aren't - the first is Pass A doing its job correctly, and
escalating on it is pure wasted cost (6/9 = 67% of all escalations this
run).

**Fixed (2026-09-01, after this analysis)**: `extract_product()` now skips
Pass B entirely when the post's own caption/comments match a new
`DM_FOR_PRICE_RE` pattern ("DM for price", "inbox/call for price", and
close variants like "send a DM/WhatsApp for the price") - a confirmed
absence, not an uncertain one, so vision has nothing left to find. This is
a code change, not a prompt change - the escalation decision runs in
Python against the model's structured output and has no way to react to
prompt wording, so a prompt-only fix wasn't mechanically possible. Verified
live (not via a full harness re-run) against the two clearest cases: the
confirmed-DM-for-price post `18156202255497621` now makes exactly one Pass
A call and no longer escalates; the genuine image-overlay case
`17975827368107390` still escalates and still correctly recovers the price
from the photo. Of the 9 posts in the table above, this fix targets the 5
whose caption/comments literally say "DM for price" (`18156202255497621`,
`17942404707299492`, `18128952712668902`, `18457973368136711`,
`18112015715068333`) - projected escalation rate drops from 9/25 (36%) to
4/25 (16%) on this golden set. The remaining 4 escalations are untouched by
this fix: 2 genuine image-overlay cases (working as designed), the Tesla
caption-miss, and `18029319446669897` ("Unknown Vehicle" flash sale, which
never says "DM for price" - it states no vehicle or price at all - so it's
a different, not-yet-fixed case of the same underlying problem).

## Stage 3 Extraction — Qwen Cascade vs. Gemini (2026-09-01)

`pipeline/stages/stage3_extract.py` had a single hardcoded `MODEL` constant
used for both Pass A and Pass B, with no config override at all - the same
gap flagged for Stage 3/4 since the very first config-system migration.
Added `text_model`/`vision_model` parameters to `extract_product()`
(defaulting to the existing `MODEL` constant, so `default.yaml` and every
other untouched config keep working unchanged), threaded through to
`_pass_a`/`_pass_b`, mirroring `stage2_triage.py`'s existing pattern.

New config: `stage3_qwen_cascade.yaml` - Pass A on `openrouter/qwen/qwen3-14b`,
Pass B on `openrouter/qwen/qwen3-vl-8b-instruct`. Note:
`qwen/qwen-2.5-14b-instruct` doesn't exist on OpenRouter (no direct
2.5-series 14B) - substituted `qwen3-14b` (newer generation,
$0.00000012/$0.00000024 per prompt/completion token). Both models support
OpenRouter's stricter `structured_outputs` mode (real JSON-Schema-constrained
decoding) but that wasn't enabled here - kept on the same `response_format:
json_object` mode every other stage uses, per explicit request.

| Config | Pass A / Pass B | Price accuracy | Name similarity | Missing-price recall | Escalation rate | Total tokens |
|---|---|---|---|---|---|---|
| `default` (Gemini 3.5 Flash-Lite, both passes) | text+vision same model | 19/19 = 100% | 91% | 6/6 = 100% | 9/25 = 36% | 72,243 |
| `stage3_qwen_cascade` | Qwen3 14B / Qwen3-VL 8B | 17/19 = 89% | 91% | 6/6 = 100% | 8/25 = 32% | 76,040 |

Real OpenRouter spend for this run: ~$0.0129 (33 calls: 25 Pass A + 8 Pass
B), confirmed via OpenRouter's own `/api/v1/credits` usage delta - still on
a $0 credit balance, same no-hard-limit behavior noted for the Stage 2
experiments.

Escalation rate is close to Gemini's (32% vs 36%) and for 7 of the 8
escalated posts, the cause is identical to Gemini's - Pass A correctly
found no price anywhere and escalation just re-confirms that at 2x cost
(see root cause above). Name similarity and missing-price recall are
unchanged; price accuracy dropped 2 points, both misses new and specific
to Qwen:

**Qwen produces internally-inconsistent price objects that the escalation
trigger can't catch.** On `18042748280816067` (BYD Leopard 3, price stated
plainly as ₦40,000,000 in the caption) and `18051759722796086` (Tesla
Model 3, price stated as a caption range), Qwen returned
`price.source: "caption"` with `price.value: null` - claiming it found the
price in the caption while leaving the actual number empty. `_needs_escalation()`
only checks `price.source == "none"`, so a `source` of `"caption"` paired
with a null `value` sails through without triggering Pass B at all - a
distinct gap from the "no price anywhere" over-escalation problem found
against Gemini, and specific to Qwen's smaller/different model family
(Gemini never produced this particular inconsistency in either run). This
is exactly the kind of failure "strict JSON" mode (OpenRouter's
`structured_outputs`, confirmed supported by both Qwen models used here -
see above) would only partially help with: it enforces field *types*, not
cross-field invariants like "if source isn't `none`, value can't be
null" - that needs either a stronger prompt instruction or a Pydantic
`model_validator` on `PriceModel` to force a retry. Neither implemented
here; flagging as a finding.

The Tesla post is also a useful negative-comparison data point against the
earlier Gemini root-cause section: Gemini's Pass A missed this same
caption price entirely (triggering an escalation that then recovered it by
re-reading the caption in Pass B); Qwen's Pass A "found" it well enough to
correctly set `source: "caption"`, but failed at the same step Gemini's
Pass B succeeded at - filling in the actual number.

## Stage 4 Signals — Model Comparison (2026-09-02)

> **⚠️ INVALIDATED 2026-09-07 — do not cite the per-model numbers below.**
> All three runs in this section executed `gemini/gemini-3.5-flash-lite`,
> regardless of the model each config named. `detect_signals()`'s model
> parameter was called `model`, which `load_stage_fn()` reserves and strips,
> so the configured value never reached the function and it silently used its
> in-module default every time. Proven from `report/token_log.csv`, which
> records the model string at call time: all 14 calls in each of the
> `stage4_gemini` / `stage4_llama` / `stage4_qwen` runs logged
> `gemini/gemini-3.5-flash-lite`. The F1 differences below are run-to-run
> nondeterminism on one model, not model differences. The binding is fixed
> (see "Fixed: Stage 4's configured model never reached the code" below);
> this section is kept unedited as a record of what was originally reported.
> **Superseded by "Stage 4 Signals — Model Comparison, RE-RUN with working
> model binding (2026-09-07)"**, which reverses the ranking. Note also that
> this section's `stock_count_known` root-cause conclusion - "identical
> across all three models, therefore a prompt/labeling mismatch" - was an
> artifact of the same bug and does not hold.

New stage: `pipeline/stages/stage4_signals.py`, a two-layer cascade -
a free, zero-LLM regex prefilter (`PREFILTER_RE`) scans caption + every
comment for candidate keywords; only a hit escalates to an LLM call that
reads the caption, the full comment thread (each comment labeled `[BUYER]`
or `[VENDOR]`), and the post's age in days, returning signals with a
required verbatim evidence quote. `eval/harness.py` needed one line added
(passing `run_id`/`vendor_id` into the existing `load_stage_fn(...,
"stage4_signals")` call, same pattern Stage 2/3 already had) - no scoring
logic touched.

Three configs (`stage4_gemini.yaml`, `stage4_llama.yaml`,
`stage4_qwen.yaml`), each pairing the same Stage 4 model swap with
identical Stage 2/3 entries copied from `default.yaml` as directed. All
three originally-requested model strings were retired or nonexistent
(`gemini/gemini-1.5-flash-8b` - the whole 1.5 line is gone;
`groq/llama3-8b-8192` - Groq has retired all Llama chat models entirely;
`openrouter/qwen/qwen-2.5-7b-instruct:free` - free tier retired) - all
three findings repeat exactly what was already confirmed while building
the Stage 2/3 cascades, so no new API probing was needed, just the same
substitutions: `gemini-3.5-flash-lite`, `openrouter/meta-llama/llama-3.1-8b-instruct`,
`openrouter/qwen/qwen-2.5-7b-instruct` (paid slug).

Golden set: `eval/golden/vendor_autos_01.json`, 17 of 30 posts carry
`expected_signals` (the only ones `score_stage4()` scores). Prefilter
short-circuit rate is identical across all three configs, since it's the
same deterministic regex over the same posts: **3/17 (17.6%) skipped with
zero LLM calls**; the other 14 posts always reach the LLM regardless of
which model is configured.

| Signal | Gemini 3.5 Flash-Lite (P/R/F1) | Llama 3.1 8B (P/R/F1) | Qwen 2.5 7B (P/R/F1) |
|---|---|---|---|
| clearance | 33%/100%/50% | 33%/100%/50% | 33%/100%/50% |
| finance_available | 100%/67%/80% | 100%/67%/80% | 100%/67%/80% |
| price_negotiable | 100%/67%/80% | 100%/67%/80% | 67%/67%/67% |
| sold | 100%/100%/100% | 100%/100%/100% | 100%/50%/67% |
| stock_count_known | 33%/67%/44% | 33%/67%/44% | 33%/67%/44% |
| swap_deal | 100%/100%/100% | 100%/100%/100% | 100%/67%/80% |
| urgent | 100%/75%/86% | 100%/50%/67% | 100%/75%/86% |
| **Macro F1** | **77%** | **74%** | **68%** |
| Prefilter short-circuit | 3/17 = 17.6% | 3/17 = 17.6% | 3/17 = 17.6% |
| Total tokens (14 LLM calls) | 15,321 | 15,242 | 15,507 |

Gemini and Llama tie on 5 of 7 signals exactly and only diverge on
`urgent` (Llama misses 2 extra genuinely-urgent posts Gemini catches -
plain lower recall, no pattern beyond that). Qwen is the clear outlier:
weaker on `price_negotiable`, `sold`, and `swap_deal`, landing 6-9 points
below the other two on macro F1.

### Root cause: `stock_count_known`'s 33% precision is identical across all three models

Same false positives, same 4 posts, regardless of which model runs - this
is a prompt/gold-labeling mismatch, not a model quality problem. All 4
false positives are captions with an explicit unit count ("exactly 4 units
left", "Only 3 units available... First come, first served", "Only 1 unit
in stock!", "Over 40 cars available") - `SYSTEM_PROMPT` defines
`stock_count_known` as firing on a vendor-stated count "either in the
caption or in a vendor comment reply" (this is what the task spec
literally asked for), and every model faithfully applies that rule since a
caption is unambiguously the vendor's own words.

But the pre-existing gold `expected_signals` (authored before this task,
not part of the Stage 3 `quantity_signal` relabel) only tags
`stock_count_known` on 3 posts, and the pattern isn't "caption vs.
comment" - `18018786554879091` (Dodge RAM TRX, "Only 2 units landed", a
**caption**-stated count) IS gold-positive for `stock_count_known`
alongside `urgent`, while structurally near-identical captions
(`18075852833375244`'s "exactly 4 units left", `18129555988734304`'s
"Only 3 units available") are gold-tagged `urgent` only. The golden set's
own notes confirm this was a deliberate editorial choice per-post (e.g.
`18075852833375244`'s notes: "Caption's 'STOCK ALERT...' is genuine urgent
(scarcity) language" - framing the same phrase as urgency evidence, not a
separate stock-count signal), not a consistent rule the prompt could have
matched. Not fixed here - flagging as a finding, since fixing it means
either loosening the gold labels to accept both signals on these captions,
or adding a rule to `SYSTEM_PROMPT` distinguishing "count framed as
urgency" from "count as its own signal" that doesn't currently exist in
the task's spec.

### Known limitation carried over from Stage 2/3: vendor comment labeling never actually fires during these runs

`score_stage4()` calls `detect_fn(post)` with no `profile` argument -
identical to `score_stage2`/`score_stage3`'s calls. `detect_signals()`'s
`[BUYER]`/`[VENDOR]` comment labeling depends entirely on
`profile["vendor_username"]`, so every comment in these three runs was
labeled `[BUYER]`, including the vendor's own replies. Despite that, `sold`
and `swap_deal` still scored 100% precision on Gemini/Llama - the models
appear to infer vendor voice from content and tone (a reply like "No swaps
on this particular unit, sorry" reads as vendor-authored regardless of any
label) rather than actually relying on the role label this task asked for.
Worth a real test with profile threaded through before trusting that the
`[BUYER]`/`[VENDOR]` design is earning its keep - not fixed here, since
threading `profile` through `score_stage4()` (and `score_stage2`/
`score_stage3`, which share the exact same gap) is a harness change beyond
this task's one authorized line.

## Stage 5 Routing Distribution — Gemini Cascade (2026-09-02)

Golden set: `eval/golden/vendor_autos_01.json` (30 posts). Cached inputs: `stage2_Gemini_Cascade_Google_AI_Studio_predictions.json`, `stage3_gemini-3.5-flash-lite_Google_AI_Studio_predictions.json`, `stage4_gemini_gemini-3.5-flash-lite_predictions.json` - zero LLM calls, pure deterministic routing over already-computed Stage 2/3/4 outputs.

| Bucket | Count | % |
|---|---|---|
| auto_import | 17/30 | 57% |
| needs_attention | 10/30 | 33% |
| auto_exclude | 3/30 | 10% |

Attention rate: 10/30 = 33% (POC target: <= 10 attention items per 100 posts).

Top flag reasons:

- [6x] Missing price — set manually or mark as DM for price
- [2x] No products could be extracted from this post
- [2x] Item may already be sold — verify before importing
- [1x] Product name unknown — please verify

Needs-attention posts:

- `18156202255497621`: Missing price — set manually or mark as DM for price
- `17942404707299492`: Missing price — set manually or mark as DM for price
- `18173859304438540`: No products could be extracted from this post
- `18017164724876313`: Item may already be sold — verify before importing
- `18029319446669897`: Missing price — set manually or mark as DM for price; Product name unknown — please verify
- `18128952712668902`: Missing price — set manually or mark as DM for price
- `18457973368136711`: Missing price — set manually or mark as DM for price
- `18051759722796086`: Item may already be sold — verify before importing
- `18112015715068333`: Missing price — set manually or mark as DM for price
- `17894529186657159`: No products could be extracted from this post

Auto-exclude posts:

- `17897690802571800`: announcement
- `18114987011511783`: testimonial_repost
- `18331493713257979`: ad_creative

## Stage 6 Sync — POC Build & Findings (2026-09-04)

Built the POC's `sync <handle>` equivalent as two scripts plus a pure diff
module: `scripts/run_build_snapshot.py` (builds the "previous run" baseline
from the golden set), `scripts/run_stage6.py` (re-pulls fresh posts for
`ayodele.akinbohun` from the Graph API, diffs against the baseline, emits
`report/changes.json`), and `pipeline/stages/stage6_sync.py` (the
zero-LLM-call comparison functions both scripts share). Snapshot lives at
`data/snapshots/ayodele.akinbohun/` (`latest.json` + `embeddings.npy` +
`embedding_index.json`). Final run against live data: 40 fresh posts vs. 30
previous → 18 no-ops (zero AI calls), 4 new_post, 6 repost_match (4 pHash +
2 embedding), 2 caption_edit, 1 comment_delta, 10 content_changed, 0
deleted; 10 items required vendor action.

### Coverage against the 5 POC sync steps

| # | Spec requirement | Status |
|---|---|---|
| 1 | Anchor on media ID; new→full pipeline, gone→propose `archived`, never silent-delete | **Done.** `post_id` set-diff (`new_ids`/`deleted_ids`/`common_ids`); deleted posts get `proposed_transition: "archived"`, `requires_vendor_action: true` - no deletion ever happens automatically. |
| 2 | Content hash (caption + media URL + comment count); unchanged → hard no-op, zero AI calls | **Done, with one deliberate deviation.** `compute_content_hash()` hashes caption + comment_count + comment-list length - **not** media_url, on purpose: CDN URLs carry rotating `oh=`/`oe=` query params (confirmed while refreshing the golden set's URLs last session) that change on every fetch even when nothing about the post changed, which would make every post a false "changed" every sync. No-ops confirmed zero-AI-call in this run (18/30 common posts, no pHash/embedding/LLM calls made for any of them). |
| 3 | Caption changed → re-run Stage 3 on that post; price-crossing-negotiation-floor consistency check | **Partially done.** Caption edits are detected and flagged (`needs_stage3_rerun: true`, old/new caption included) but Stage 3 is **not actually invoked** - out of scope for this POC pass, by design (flag-only). No rules-engine price-floor check exists yet since nothing re-extracts the new price. |
| 4 | New comments only (per-post cursor) → Stage 4 on the delta; "sold"/"it don finish" → propose `out_of_stock` | **Blocked, partially done.** `comment_cursor` (last comment ID at snapshot time) and `_comments_after_cursor()` are implemented and correctly detect *that* a post's comment count moved. But listing *which* comments are new turned out to be unreliable - see blocker below - so the keyword-triggered `out_of_stock` proposal for comment deltas was not added; only `needs_stage4_rerun: true` is emitted, deferring signal detection entirely to a real Stage 4 call this POC doesn't make. |
| 5 | Repost dedup: embed + compare against catalog; high similarity → merge, never duplicate | **Done, including the auto-merge policy.** pHash cascade (checked first, cheap) falling back to caption-embedding cosine similarity. A bit-identical pHash (distance=0 - the literal reading of "exact repost") now emits `change_type: "repost_merge"`, `auto_merged: true`, `requires_vendor_action: false` and folds into the existing catalog entry; anything looser (including the keyword-gated 16/0.90 matches below) stays `repost_match`, always asking. An out-of-stock transition riding on an auto-merge still sets `requires_vendor_action: true` - the merge is the no-brainer, the transition is not, per the brief's own "out-of-stock transitions ... prompts the vendor" line. |

### Blocker: Graph API's `comments{}` edge doesn't reliably return what `comments_count` implies

This is the reason point 4 above is incomplete. Two independent pieces of
evidence from live data this run:

1. Post `18156202255497621`'s snapshot recorded `comment_count: 2` (from the
   golden set); a direct live re-fetch of that same post returned
   `comments_count: 0` **and** an empty `comments.data` array - the comments
   are simply gone from the API's perspective, not paginated elsewhere.
2. Post `18128205745669337` triggered a genuine `comment_delta` this sync
   (`delta_count: 2`, since `comments_count` rose), but the nested
   `comments{id,username,text,timestamp}` field expansion on the same
   `/media` call returned `new_comments: []` - zero actual comment objects
   despite the count increasing by 2.

Both point the same way: `comments_count` and the `comments` edge can
diverge on this API (likely comment visibility/moderation filtering, or the
edge simply not being populated in the same call that reports the count).
Since "sold"/"it don finish" detection needs the *actual comment text*, not
just a count delta, this can't be fixed by better cursor logic - it needs
either a separate dedicated call to the `/comments` edge per post (extra
API calls per common post, working against point 2's zero-AI-call promise)
or accepting that comment-delta detection will sometimes have no text to
show. Not resolved this session; `comment_delta` entries currently list
`new_comments` best-effort (empty when the edge doesn't cooperate) and
always defer to a real Stage 4 call rather than guessing from a count alone.

### Update (2026-09-04): root cause identified — Advanced Access gate, not API unreliability

The "unreliability" framing above turned out to be wrong. Full
investigation, triggered by needing to explain to a demo audience why
`eval/golden/vendor_autos_01.json`'s comments and
`scripts/simulate_stage6.py`'s `sim_c1`/`sim_c2` comment data are hand-typed
rather than pulled live (the golden set's comment `id` values -
`17858893269000006`, `...008`, `...010`, `...012`, `...014`, `...018` - are a
tidy same-prefix sequence incrementing by 2, not real Instagram comment IDs,
confirming they were fabricated, not fetched):

- Every raw dump ever pulled for this account (`dump_2026-08-24...`,
  `dump_2026-08-30...`, `dump_2026-09-04...`) has `"comments": []` on
  **every single post**, via `ingest.py`'s dedicated `/{media-id}/comments`
  edge - not just the two posts noted above, all of them, since day one.
- Tested directly against a post the vendor confirmed has real, visible
  comments on Instagram itself (post `18156202255497621`): the dedicated
  edge returns `{"data": []}` across 5 API versions (`v18.0` through
  `v26.0`) and 3 field combinations (full field list, id/text/username
  only, `text` alone) - ruling out API-version drift and field-selection as
  causes. A direct media-node fetch
  (`GET /{media-id}?fields=comments_count,comments{id,text,username}`)
  returned `comments_count: 0` too - not just the comment list, the *count*
  the API reports for its own media object is wrong, for a post with
  confirmed real comments.
- Checked the Meta App Dashboard: `instagram_business_manage_comments` is
  granted on the token (confirmed in the Permissions and Features list) but
  sits at **Standard Access**, and the app is in **Development mode**.

That combination is the actual cause. Meta's Graph API treats an app's own
admin/owner data (captions, media, profile - all `instagram_business_basic`,
Standard Access, works fine in dev mode, which is why every other field this
pipeline pulls has always been real) differently from data belonging to
*other* platform users. A comment's text/username belongs to whoever posted
it, not to the vendor account - that's third-party data, and Meta withholds
it (silently, as an empty/zeroed result rather than an error) until the
permission clears App Review and is granted **Advanced Access**, regardless
of what the token's scope list shows. No client-side code change - API
version, field syntax, dedicated edge vs. nested field expansion - can route
around this; it sits above all of that.

**Not fixed this session, and not fixable in code**: real comment retrieval
is blocked until `instagram_business_manage_comments` is submitted for App
Review and granted Advanced Access (Meta typically asks for a screencast of
the feature plus business verification; turnaround is days to weeks, not
something to expect before a near-term demo). Until then, the golden set's
hand-authored comments and `simulate_stage6.py`'s synthetic injection aren't
a workaround for a bug - they're the only available way to exercise
Stage 4/6's comment-dependent logic at all, and should be presented as
representative synthetic data standing in for live buyer engagement, not as
a bug to keep chasing.

### Finding: comment counts can *decrease*, and dropping the delta silently was a real bug

10 of the 30 common posts this run had a `content_hash` change where
neither caption nor comment count *increased* - the first implementation
of `run_stage6.py` didn't emit anything for these (not a no-op, not a
flagged change), silently losing them. Root cause matches the blocker
above: comment counts went *down* on these posts between the snapshot and
the live fetch (comments removed/hidden upstream, not a vendor edit).
Fixed by adding a `content_changed` change type so a hash change always
produces a visible entry (`requires_vendor_action: false`, since a comment
disappearing isn't itself vendor-actionable) - never silently drop a
detected change again.

### Finding: `text-embedding-004` (named in the original task spec) is retired

Confirmed via a live `GET v1beta/models` call against the project's own
Gemini key - only `gemini-embedding-001`/`-2`/`-2-preview` support
`embedContent` now. Same retirement pattern already seen with Stage 2/3/4's
chat models. Switched to `gemini-embedding-001`; its default output is
3072-dim, so `outputDimensionality: 768` is passed explicitly to match this
pipeline's 768-dim vectors (confirmed via a live probe of both).

### Finding: a blanket similarity-threshold loosening trades false negatives for false positives

Vendor reported 3 genuine reposts (heavily re-edited "SOLD" overlay images
+ rewritten captions) were missed at the default thresholds
(pHash 12 / embedding 0.92) - they sat at pHash distance 14 and embedding
score 0.91. Loosening both thresholds globally (16 / 0.85) caught all 3,
but also pulled in 4 unrelated new posts as false repost matches (embedding
scores 0.86-0.88, no lifecycle keyword anywhere in their captions) - car
listings share enough boilerplate (price line, location, phone number,
hashtags, even an identical "Hot deal🔥🔥" opening line across otherwise-
distinct posts) that 0.85-0.92 cosine similarity catches "same kind of
post," not "same post reposted."

**Fixed**: gated the loosened thresholds on caption content instead of
applying them globally. A caption containing "sold"/"delivered"/"handed
over" is independent evidence the post is about an existing item, not a
fresh listing - only those posts get searched under the wider thresholds
(pHash 16 / embedding 0.90); every other new post stays at the strict
default (12 / 0.92). Verified this recovers exactly the 3 intended matches
(all three now show the keyword in `evidence` and propose `out_of_stock`)
while the 4 false positives correctly fall back to `new_post`.

### Fixed: repost auto-merge and snapshot persistence

Two gaps flagged above (repost auto-merge, and the snapshot never advancing
after a sync) are now closed:

- **Auto-merge**: `AUTO_MERGE_PHASH_MAX_DISTANCE = 0` in `run_stage6.py`
  gates the merge decision - only a bit-identical pHash qualifies, never an
  embedding match (a similarity score, never identity). Verified live: of
  the vendor's 3 flagged reposts, the 2 exact pHash duplicates (distance=0)
  now auto-merge (`repost_merge`, `requires_vendor_action: false`) while
  the 1 embedding-matched repost correctly still asks.
- **Snapshot persistence**: `run_stage6.py` now rewrites
  `data/snapshots/<handle>/{latest.json,embeddings.npy,embedding_index.json}`
  at the end of every run (`_advance_snapshot()` in the same file), reusing
  whatever was already computed rather than recomputing everything (no-op
  posts carry forward untouched; a caption-only edit keeps its old pHash
  since an existing IG post's image can't change). Verified by running the
  sync twice back to back: first run - 18 no-ops/22 changes vs. a 30-post
  baseline; second run - 38 no-ops/0 changes vs. the just-advanced 38-post
  baseline, confirming the loop actually converges instead of re-reporting
  the same diffs forever.

### Finding: two simultaneously-live duplicate photos make the "canonical" post_id oscillate

Discovered while verifying the fix above, not something built or fixed
this pass. Both of this vendor's exact-pHash duplicate pairs turned out to
be two *separate posts that are both still live* on Instagram (confirmed
via caption content - each pair genuinely describes the same vehicle, e.g.
both `18079164065330268` and its new duplicate `18102993374591533` open
with "2019 Lexus RX 350 F-Sport"), not one post replacing another. Running
sync a *third* time reproduced both merges again, in the opposite
direction (the post_id dropped by the first merge reappeared as "new" and
got re-merged, evicting the post_id the first merge had just kept) - a
stable-count, no-data-loss oscillation (38 entries either way, and neither
direction has a lifecycle keyword so neither ever prompts the vendor), but
the catalog's "canonical" post_id for that item flips every sync rather
than settling.

Root cause: the snapshot schema keys a catalog entry by `post_id` 1:1, with
no separate product identity a `post_id` can point *to*. "Supersede the old
post_id with the new one" (this pass's choice) is right for the brief's
actual scenario - vendor deletes the old post after reposting - since the
old entry survives in the fetch's `deleted_ids` and the merge naturally
hands its identity to the still-live replacement. "Keep the old post_id
canonical instead" would fix this specific oscillation but breaks that
same scenario the other way: an archived-then-reposted item would stay
stuck on `lifecycle_state: "archived"` forever, since the live repost would
never get its own tracked entry. Both directions are correct for one real
scenario and wrong for the other - not a bug fixable by picking a
tiebreak, but a missing product-vs-post distinction. The brief's own note
that production would move to pgvector points at the right shape of fix
(a `product_id` catalog keyed separately from the `post_id`s that reference
it), which is beyond this POC's snapshot format. Not fixed this pass.

### Not built (explicitly out of this POC pass)

- No scheduling or webhook trigger - this is a single manual `sync` run,
  matching the brief's own note that Meta provides no media webhooks and
  comment webhooks weren't wired up either (see blocker above).
- No vendor-facing digest, no granular auto-apply permissions, no
  reversibility/audit log - all correctly out of scope per "POC scope: a
  `sync <handle>` command that re-pulls, diffs against the cached previous
  run, and emits `changes.json` - no-ops counted, deltas classified,
  proposed state transitions with evidence attached," which is exactly what
  was built.

## GPT-4o Mini — Full Cascade Comparison, Stages 2-5 (2026-09-05)

Adds a fourth model to the existing Stage 2/3/4 comparisons and a second
Stage 5 routing run: `openai/gpt-4o-mini` via OpenRouter (`openrouter/openai/gpt-4o-mini`,
$0.15 / $0.60 per 1M prompt/completion tokens - confirmed live against
OpenRouter's model page). New configs: `stage2_gpt4o_mini_cascade.yaml`,
`stage3_gpt4o_mini.yaml`, `stage4_gpt4o_mini.yaml` - same isolation
convention as the existing Llama/Qwen/Gemini cascades (only the stage under
test swaps models; the other two stay on a known-working pairing so the
harness run isn't dominated by unrelated errors). Golden set:
`eval/golden/vendor_autos_01.json` (30 posts), same as every prior run in
this file. `estimated_cost_usd` in `report/token_log.csv` is hardcoded to
0.0 (see `pipeline/llm_client.py`'s `log_token_usage()` - cost tracking
was never wired up since every model run before this one was free-tier),
so real cost below is computed manually from the log's raw prompt/completion
token counts at OpenRouter's published rate.

### Stage 2 comparison table

| Config | Pass A (text) | Pass B (vision) | Accuracy | Escalation Rate | Total Tokens |
|---|---|---|---|---|---|
| `stage2_llama_cascade` | Llama 3.1 8B | Llama 4 Scout | 27/30 = 90.0% | 0/30 = 0% | 11,447 |
| `stage2_qwen_cascade` | Qwen 2.5 7B | Qwen3-VL 8B | 23/30 = 76.7% | 0/30 = 0% | 11,351 |
| `stage2_gemini_cascade` | Gemini 3.5 Flash-Lite | Gemini 3.5 Flash | 27/30 = 90.0% | 2/30 = 6.7% | 14,467 |
| `stage2_gpt4o_mini_cascade` | GPT-4o Mini | GPT-4o Mini | **29/30 = 96.7%** | 2/30 = 6.7% | 61,904 |

### Stage 3 comparison table

| Config | Pass A / Pass B | Price accuracy | Name similarity | Missing-price recall | Escalation rate | Total tokens |
|---|---|---|---|---|---|---|
| `default` (Gemini 3.5 Flash-Lite, both passes) | text+vision same model | 19/19 = 100% | 91% | 6/6 = 100% | 9/25 = 36% | 72,243 |
| `stage3_qwen_cascade` | Qwen3 14B / Qwen3-VL 8B | 17/19 = 89% | 91% | 6/6 = 100% | 8/25 = 32% | 76,040 |
| `stage3_gpt4o_mini` | GPT-4o Mini / GPT-4o Mini | 16/19 = 84% | 91% | 6/6 = 100% | 6/25 = 24% | 205,453 |

### Stage 4 comparison table

> **⚠️ INVALIDATED 2026-09-07.** The GPT-4o Mini column here is not GPT-4o
> Mini — like the three columns it was compared against, it ran
> `gemini/gemini-3.5-flash-lite` (see the invalidation notice on the
> 2026-09-02 Stage 4 section). The "GPT-4o Mini wins Stage 4 on macro F1"
> conclusion drawn in the analysis below is therefore unsupported. The
> near-identical token totals across all four columns (15,242-15,507, under
> 2% spread, where Stage 2/3 varied 4-6x between genuinely different models)
> were the tell, and were mistakenly explained away at the time as Stage 4
> having no vision path. **Superseded by the 2026-09-07 re-run**, in which
> GPT-4o Mini actually scores 68% and places *second* to free-tier Gemini.

| Signal | Gemini F1 | Llama F1 | Qwen F1 | GPT-4o Mini F1 |
|---|---|---|---|---|
| clearance | 50% | 50% | 50% | 67% |
| finance_available | 80% | 80% | 80% | 80% |
| price_negotiable | 80% | 80% | 67% | 67% |
| sold | 100% | 100% | 67% | 100% |
| stock_count_known | 44% | 44% | 44% | 73% |
| swap_deal | 100% | 100% | 80% | 100% |
| urgent | 86% | 67% | 86% | 93% |
| **Macro F1** | **77%** | **74%** | **68%** | **83%** |
| Prefilter short-circuit | 3/17 = 17.6% | 3/17 = 17.6% | 3/17 = 17.6% | 3/17 = 17.6% |
| Total tokens (14 LLM calls) | 15,321 | 15,242 | 15,507 | 15,415 |

### Stage 5 routing comparison table

| Config | auto_import | needs_attention | auto_exclude |
|---|---|---|---|
| Gemini Cascade | 17/30 = 57% | 10/30 = 33% | 3/30 = 10% |
| GPT-4o Mini | 15/30 = 50% | 11/30 = 37% | 4/30 = 13% |

### Analysis

**Where GPT-4o Mini wins**: Stage 2 triage is its strongest result -
96.7% (29/30), the best of any model tested, missing only one post while
escalating at the same 6.7% rate as Gemini. Stage 4 signal detection is
its second win - 83% macro F1, beating Gemini's 77% (previous best) mainly
on `stock_count_known` (73% vs. 44% for all three prior models) and
`urgent` (93% vs. 86%/67%/86%). The `stock_count_known` result is worth
flagging specifically: FINDINGS.md's own root-cause section above showed
all three prior models landing on the *identical* 33% precision / same 4
false positives - a prompt/gold-labeling mismatch, not a model quality
gap. GPT-4o Mini breaking that pattern (67% precision, different error
profile) means it's reading the caption-vs-comment stock-count distinction
differently, not just "better" - worth a closer look before assuming it
fixed the underlying ambiguity rather than getting lucky on this golden
set.

**Where it falls short**: Stage 3 price extraction is the weak point -
84% (16/19), the worst of any model tested, below even Qwen's 89% and well
below Gemini's 100%. This isn't from under-escalating: its escalation rate
(24%) is the *lowest* of the three Stage 3 configs compared (Gemini 36%,
Qwen 32%), so GPT-4o Mini is reaching confident-but-wrong price
extractions on Pass A more often than genuinely flagging uncertainty -
the same "confidently wrong" failure mode the Stage 2 cascade comparison
found in Llama/Gemini, now showing up in Stage 3's price field instead.

**Cost note - vision is expensive for this model on OpenRouter**: Stage
2/3's total-token counts are 4-6x every other model's, entirely from the
handful of vision (Pass B) escalations: Stage 2's 2 escalations alone used
51,293 of its 61,904 total tokens (~25,600 prompt tokens per single-image
call), and Stage 3's 6 escalations dominate its 205,453-token total the
same way. Gemini/Llama/Qwen's vision passes never showed anything close to
this per-image token cost in prior runs. Text-only Pass A calls are cheap
and comparable across all four models (Stage 2 Pass A: 10,611 tokens for
30 gpt-4o-mini calls, in the same range as Llama's 11,447-token *total*).
Stage 4 has no vision path at all and lands right in line with the other
three models (15,415 tokens vs. 15,242-15,507).

**Stage 5 attention rate**: 37% (11/30), *above* Gemini's existing 33% and
moving further from the POC's <=10% target, not closer - directly
downstream of Stage 3's weaker price accuracy: 10 of the 11 needs-attention
flags are "Missing price," plus one low-extraction-confidence flag neither
Gemini's stack triggered. A stronger Stage 2/4 doesn't help the attention
rate if Stage 3 is the stage actually driving most flags on this golden
set.

**Real cost of this run** (computed from `report/token_log.csv`'s raw
prompt/completion token counts at OpenRouter's $0.15/$0.60 per 1M rate,
since `estimated_cost_usd` is hardcoded to 0.0 - see note above):

| Stage | Prompt tokens | Completion tokens | Real cost |
|---|---|---|---|
| Stage 2 (`stage2_gpt4o_mini_cascade`) | 61,251 | 653 | $0.0096 |
| Stage 3 (`stage3_gpt4o_mini`) | 199,376 | 6,077 | $0.0336 |
| Stage 4 (`stage4_gpt4o_mini`) | 14,085 | 1,330 | $0.0029 |
| **Total** | | | **$0.0460** |

For comparison, every prior model in this file ran on a free tier ($0.00
real spend) - GPT-4o Mini is the first paid model tested, and Stage 3's
vision escalations are responsible for the large majority of that $0.046
(73% of it), not the text passes or Stage 2/4.

## Fixed: vision escalation and pHash were broken for Reels/video posts (2026-09-05)

Prompted by prepping a second (gadgets/electronics) vendor account, which
will post Reels far more than the automotive vendor did. Instagram Graph
API's `media_url` field means different things by `media_type`: for
`IMAGE`/`CAROUSEL_ALBUM` it's a real image; for `VIDEO` (a Reel is
`media_type=VIDEO` with `media_product_type=REELS`, not its own
`media_type`) it's the **video file itself**. The API exposes
`thumbnail_url` - a static cover image - only for `VIDEO` media, and
neither `ingest/ingest.py` nor `scripts/run_stage6.py` requested it, nor
did anything downstream know to use it.

Affected every place that hands `media_url` to something expecting a
static image: `stage2_triage.py`/`stage3_extract.py`'s vision Pass B
(would send a video URL to the vision LLM), `media_fingerprint.py`'s
`compute_image_phash()` (would fail to decode, silently degrading Stage
6's repost-dedup to a no-op for every Reel), and
`make_golden_skeleton.py`'s `collect_images()` (would pre-fill a broken
video URL into the golden set's `products[].images`). None of this
surfaced yet only because the automotive golden set happens to contain
zero Reels.

**Fixed**: `ingest.py`/`run_stage6.py` now request `thumbnail_url` and
`media_product_type` (top-level and on carousel children). A new shared
helper, `pipeline/types.py::vision_image_url(post)`, returns
`thumbnail_url` when present else falls back to `media_url` - wired into
both stages' vision Pass B, both Stage 6 scripts' pHash calls, and
`make_golden_skeleton.py`/`update_golden_skeleton.py`'s image collection.
Golden-set entries now also carry `thumbnail_url`/`media_product_type` as
pre-filled (never hand-labeled) fields for visibility.
Not yet verified against a real Reel (the automotive account has none) -
first real check happens once the gadgets/electronics account has a Reel
ingested.

## Fixed: harness predictions are now scoped per vendor (2026-09-06)

Prompted by preparing to ingest a second vendor account
(gadgets/electronics). `eval/harness.py` wrote its per-post predictions to
`report/stage{2,3,4}_<MODEL>_predictions.json` - keyed by **model label
only, with no vendor in the path**. Running the harness for a second vendor
under the same config would therefore have silently overwritten the first
vendor's cached predictions, which is exactly what `scripts/run_stage5.py`
routes from. Nothing in the pipeline would have reported an error; vendor
1's Stage 5 inputs would just quietly become vendor 2's data.

**Fixed**: the three `score_stage*()` functions now take the `vendor_id`
`main()` already computes for the token log (the golden file's stem) and
write to `report/<vendor_id>/stage{N}_<model>_predictions.json`. The 14
existing vendor-1 prediction files were moved to `report/vendor_autos_01/`,
and `run_stage5.py`'s three `DEFAULT_STAGE*_PATH` constants plus the
`Makefile`'s `STAGE2/3/4` variables (now `report/$(ACCOUNT)/...`, so they
follow `ACCOUNT`) were repointed to match. **Historical note**: the
prediction filenames cited in the sections above (Stage 2 cascade
comparison, Stage 3 schema upgrade, Stage 5 routing) now live under
`report/vendor_autos_01/` rather than the `report/` root.

Two related footguns handled at the same time:
- `eval/harness.py` with no `--golden` globs *every* `eval/golden/*.json`,
  so a bare run now that a second golden set exists would merge both
  vendors into one accuracy number and tag the token log `all_vendors`.
  Not code-fixed (the glob is deliberate for the single-vendor case) -
  always pass `--golden`/`GOLDEN=` from here on.
- `scripts/refresh_media_urls.py` hardcoded vendor 1's golden path and
  `IG_ACCESS_TOKEN`; it now takes `--golden`/`--token-env` like the other
  scripts. It also requested only `id,media_url` from the API while its
  `_resolve_url()` claimed to fall back to `thumbnail_url` - a fallback
  that could never fire. It now requests `thumbnail_url` too, so Reel/video
  entries actually refresh instead of silently keeping a stale URL.

## Fixed: `DM_FOR_PRICE_RE` missed the plural "prices" (2026-09-07)

Surfaced immediately on the second vendor's real data. The pattern ended in
`\bprice\b`, so "Kindly send us a DM for **prices**!" did not match - the
trailing `s` defeats the word boundary. Both of `vendor_gadgets_01`'s
DM-for-price posts (`18047192480810719`, `17966735571148449`) use the
plural, and for a structural reason: they are multi-variant listings
("iPhone 12 64GB / 128GB / 256GB ... send us a DM for prices!"), and a
vendor listing several storage tiers naturally pluralizes. The singular-only
pattern was fine for vendor 1 purely by accident of phrasing.

Consequence had it shipped: both posts would escalate to vision Pass B on
every run - precisely the wasted-cost path the 2026-09-01 fix above was
written to prevent - while `FINDINGS.md` claimed the case was handled.

**Fixed**: pattern now ends `\bpric(e|es|ing)\b`, also covering "DM for
pricing". Verified three ways: the singular cases still match (no
regression), the plural/gerund now match, and "Send a DM to order" /
"Send us a Dm or visit our stores" still correctly do *not* match.
`vendor_autos_01` matches the same 5 posts before and after, so every number
recorded in the sections above is unaffected.

**Worth noting for future prompt/regex work**: this class of bug - a
matcher tuned against one vendor's phrasing silently failing on another's -
is exactly what a second vendor was supposed to surface, and it did so
before a single LLM call was made.

## Fixed: Stage 4's configured model never reached the code (2026-09-07)

Found by a read-only audit subagent. **Every Stage 4 model comparison ever
recorded in this file ran the same model.**

`eval/harness.py::load_stage_fn()` strips a fixed `reserved` set of keys -
`module`, `function`, `cost_per_call_usd`, `note`, `model` - before binding
the rest of the YAML entry as kwargs. `model` is reserved because it usually
holds a *display label*, not a model string (`"Gemini Cascade (Google AI
Studio)"`, `"dummy-heuristic"`), used for log lines and the predictions
filename. Stage 2 and 3 put their real model strings in `text_model` /
`vision_model`, so they were unaffected.

Stage 4 broke that convention: `detect_signals()`'s real parameter was
literally named `model`. So the configured value was stripped every time,
the function fell back to its in-module `MODEL =
"gemini/gemini-3.5-flash-lite"`, and the harness went on to report the
config's label - producing output that *looked* like four different models.

Proof, from `report/token_log.csv` (which records the model string passed to
litellm at call time, so it cannot be fooled by labels):

```
14 stage4_gemini     -> gemini/gemini-3.5-flash-lite
14 stage4_llama      -> gemini/gemini-3.5-flash-lite
14 stage4_qwen       -> gemini/gemini-3.5-flash-lite
14 stage4_gpt4o_mini -> gemini/gemini-3.5-flash-lite
```

**Invalidated**: the 2026-09-02 "Stage 4 Signals — Model Comparison" table
and the GPT-4o Mini Stage 4 column added 2026-09-05, both now carry
invalidation notices. The per-signal F1 spread across those columns is
run-to-run nondeterminism on a single model.

**The tell that was missed**: Stage 4's token totals across the four
"different models" were 15,321 / 15,242 / 15,507 / 15,415 - under 2% spread,
where Stage 2/3 varied 4-6x between genuinely different model families.
Different tokenizers do not agree to within 2%. This was noted at the time
and explained away as "Stage 4 has no vision path", which fit the numbers
without being the cause.

**Fixed**:
- `detect_signals()`'s parameter renamed `model` -> `signals_model`, and
  `signals_model:` added to `stage4_gemini/llama/qwen/gpt4o_mini.yaml`.
  `model:` stays in each file as the display label, so predictions filenames
  and the `Makefile`'s `STAGE4` default are unchanged.
- `stage4_gemini_flash_8b.yaml` deliberately sets no `signals_model` (it is a
  stale snapshot that falls back to the default); its note - which claimed
  `detect_signals()` "can't take a model kwarg" - was wrong even when written
  and has been corrected.
- **Tripwire added** in `load_stage_fn()`: it now raises if the target
  function's signature declares any reserved key, naming the offending
  parameter and telling the author to rename it. A config value that can
  never bind must fail loudly rather than silently produce plausible wrong
  numbers.
- The tripwire immediately caught a second violation:
  `stage1_profile.get_or_create_profile()` also declared `model`. Stage 1 was
  **not** actually broken - it resolves its own model from config via
  `_default_model_from_config()` and never goes through `load_stage_fn()` -
  but its parameter is renamed `profile_model` so the invariant holds
  uniformly: no stage function may declare a parameter named `model`; real
  model strings go in `text_model` / `vision_model` / `signals_model` /
  `profile_model`.

**Not yet done**: Stage 4 has not been re-run against real models. The four
Stage 4 rows will stay invalidated until someone re-runs the comparison -
which, unlike last time, will actually bill OpenRouter for the Llama, Qwen
and GPT-4o Mini configs.

## Fixed: live Instagram token leaked into logs and exception messages (2026-09-07)

Same audit. All three Graph API callers passed the token as an
`access_token` **query parameter**, which put a live credential into the URL
and therefore into several places that print it:

- **urllib3's DEBUG log line** - `"GET /path?query HTTP/1.1" 200` includes
  the full query string. `LOG_LEVEL=DEBUG` is a documented, supported flag,
  and nothing suppressed urllib3 the way `pipeline/llm_client.py` explicitly
  suppresses litellm. This was not hypothetical: a `LOG_LEVEL=DEBUG`
  ingest run earlier in the same session printed the account's live token to
  the console once per request, ~40 times.
- **`raise_for_status()`** - embeds the request URL in the exception
  message. `scripts/run_stage6.py::_fetch_all_media()` had no try/except at
  all, so any expired token, 429, or transient 5xx produced an **uncaught**
  traceback containing the token.
- **`requests.RequestException`** - same, and
  `scripts/refresh_media_urls.py` logged `str(exc)` directly at WARNING,
  which is always visible regardless of `LOG_LEVEL`.

The trigger conditions are ordinary operations, not edge cases: an expired
token, a rate limit, a network blip, or simply running with DEBUG.

**Fixed**:
- New `pipeline/settings.py::ig_auth_headers(token)` - the token now travels
  as an `Authorization: Bearer` header, so it never enters a URL. Verified
  live against `graph.instagram.com` (HTTP 200, and `access_token` absent
  from `request.url`) before switching the callers over.
- New `pipeline/settings.py::redact_tokens(text)` - strips `access_token=…`
  and `Bearer …` from any string. Defence in depth for the exception path,
  since a paginated `next` URL echoed back by the API can itself carry a
  token. Applied at every log/raise site in `ingest.py`, `run_stage6.py`,
  and `refresh_media_urls.py`.
- `_fetch_all_media()` now catches `HTTPError`/`RequestException` and
  re-raises a redacted `RuntimeError`, using `raise ... from None` - without
  suppressing the chain, the original unredacted exception still prints in
  the traceback.
- `pipeline/logging_config.py::_suppress_http_wire_logs()` raises the
  urllib3 loggers to INFO, mirroring the litellm suppression. Deliberately
  placed *before* `configure_logging()`'s early return, so a repeat call
  re-applies it. Raised to INFO rather than disabled, so genuine urllib3
  retry/connection warnings still surface.

**Verified**: a `LOG_LEVEL=DEBUG` profile fetch no longer contains the live
token anywhere in its output, and all three failure paths, exercised with a
canary token against a real 401, redact it - `ingest._get`,
`run_stage6._fetch_all_media`, and `refresh_media_urls.fetch_fresh_media`.

**Residual**: the tokens that were already printed to terminals and to
`report/run_output_*.txt` during earlier DEBUG runs are still real. Rotating
`IG_ACCESS_TOKEN` / `IG_ACCESS_TOKEN_GADGETS` in the Meta App Dashboard is
the only way to invalidate what already leaked; this fix stops new leaks
only.

## Fixed: two more `media_url`-vs-`thumbnail_url` sites missed by the Reels sweep (2026-09-07)

The 2026-09-05 fix threaded `vision_image_url()` through Stage 2/3's vision
passes, both pHash call sites, and `collect_images()` - but missed two
places, both found by the audit and a follow-up sweep for remaining
`media_url` consumers.

**1. `pipeline/stages/stage5_reconcile.py:40` - reviewer thumbnail.**
`route_post()` set `thumbnail_url = post.get("media_url")` and returned it in
every bucket. For a Reel that hands a human reviewer an `.mp4` in a field
named `thumbnail_url`, in the `needs_attention` queue that exists precisely
so a person can eyeball the item before importing. Fixed to
`vision_image_url(post)`. Verified: a real Reel from the gadgets dump now
routes a `.jpg` (its `media_url` ends `.mp4`), image posts are unchanged, and
`vendor_autos_01`'s routing is still 17/30 - 10/30 - 3/30.

**2. `scripts/refresh_media_urls.py` - actively corrupting the golden set.**
Worse than a display bug. `_resolve_url()` resolves `media_url or
thumbnail_url` - correct for refreshing the `media_url` *field*, wrong for
`products[].images[]`. For a Reel, `children` is empty, so the code
synthesised `children = [{"media_url": <the .mp4>}]` and wrote that into
`images[0]`, **overwriting the correct `.jpg` cover with the video file on
every refresh** - silently undoing the Reels fix inside `eval/golden/`, which
`CLAUDE.md` flags as irreplaceable. The script also never refreshed
`thumbnail_url` at all, despite now requesting it, so that field would go
stale on CDN expiry while `media_url` beside it stayed fresh.

Fixed: `images[]` now resolves via `vision_image_url()` (thumbnail-preferring)
while the `media_url` field keeps using `_resolve_url()`; `thumbnail_url` is
refreshed alongside `media_url`; and the synthesised single "child" carries
`thumbnail_url` through so the resolution can see it. `_resolve_url()`'s
docstring now states which of the two jobs it is and is not for. Verified on
a synthetic Reel (images[] stays `.jpg`, `media_url` correctly stays `.mp4`,
`thumbnail_url` refreshes) and a synthetic carousel (unchanged).

**Pattern worth noting**: three separate bugs this week
(`DM_FOR_PRICE_RE`'s plural, Stage 4's `model` binding, and these two) share
a shape - a fix or convention verified against one code path or one vendor,
assumed to hold everywhere. The sweep that found these was a plain grep for
the *remaining* consumers of the raw field, which is cheap and would have
caught both on 2026-09-05.

## Stage 4 Signals — Model Comparison, RE-RUN with working model binding (2026-09-07)

Supersedes both invalidated Stage 4 tables above. First Stage 4 comparison in
which the four configs actually ran four different models - verified per run
against `report/token_log.csv`'s recorded model string, not the config label.

Golden set: `eval/golden/vendor_autos_01.json`, 17 posts carrying
`expected_signals`. Prefilter short-circuit is unchanged and identical across
configs (deterministic regex): 3/17, so 14 posts reach the LLM.

| Signal | Gemini 3.5 Flash-Lite | Llama 3.1 8B | Qwen 2.5 7B | GPT-4o Mini |
|---|---|---|---|---|
| clearance | **67%** | 20% | 0% | 50% |
| finance_available | **80%** | 36% | 0% | **80%** |
| price_negotiable | **67%** | 31% | 0% | 44% |
| sold | 67% | 40% | 0% | **100%** |
| stock_count_known | **73%** | 37% | 33% | 50% |
| swap_deal | **100%** | 40% | 0% | 67% |
| urgent | **93%** | 74% | 40% | 86% |
| **Macro F1** | **78%** | **40%** | **10%** | **68%** |
| Errored posts | 0/17 | 1/17 | 0/17 | 0/17 |
| Real cost | $0.00 (free tier) | $0.00033 | $0.00062 | $0.00277 |

Total real spend for the whole comparison: **$0.0037**.

**The published conclusion is reversed.** The invalidated table claimed
GPT-4o Mini won Stage 4 at 83% macro F1. With models actually bound, **Gemini
3.5 Flash-Lite wins at 78%** - and it is the only free-tier option of the
four. GPT-4o Mini is second at 68%, costing ~8x Llama and ~4x Qwen per run to
place below a free model.

**Llama and Qwen fail in opposite directions, and both are unusable here.**
Llama 3.1 8B (40%) over-fires massively - 9 predicted `clearance` against 1
gold, 10 `price_negotiable` against 3, giving 11-29% precision on six of
seven signals with recall that is often fine. It is not failing to see the
signals; it is asserting them everywhere. Qwen 2.5 7B (10%) does the
opposite, emitting *zero* predictions for five of seven signal types - its
two non-zero scores are 100% precision at 20-25% recall. For a stage whose
output feeds Stage 5's routing, Llama's behaviour is the more dangerous:
false signals push posts into `needs_attention` (or worse, propose
`out_of_stock` via a spurious `sold`), whereas Qwen mostly just says nothing.

**A prior "finding" was also an artifact of the same bug.** The 2026-09-02
section concluded that `stock_count_known`'s 33% precision being *identical
across all three models* proved a prompt/gold-labeling mismatch rather than a
model-quality problem. It was identical because it was literally the same
model three times. With real models the spread is 73% / 37% / 33% / 50% - so
model choice clearly does matter for this signal, and that root-cause claim
should be treated as unproven rather than established. The underlying
caption-vs-comment labeling ambiguity described there may still be real; it
simply was not demonstrated by that evidence.

### Noise floor: what a re-run of the *same* model tells us

The Gemini re-run scored 78% against the previously recorded 77% - reassuring
in aggregate, but the per-signal numbers moved a lot for an identical model
on an identical golden set: `stock_count_known` 44% -> 73%, `sold` 100% ->
67%, `clearance` 50% -> 67%, `price_negotiable` 80% -> 67%.

With 1-3 gold instances per signal, one flipped post moves a per-signal F1 by
20-35 points. So: the Gemini-vs-GPT-4o-Mini gap (78 vs 68) is suggestive but
within an order of magnitude of the noise and should not be treated as
settled; the gaps to Llama (40) and Qwen (10) are far outside it and are
real. **Per-signal cells in this table should not be quoted as precise
measurements** - only the macro ordering is safe to rely on, and only for the
large gaps. Fixing this needs more gold instances per signal, not better
models (see `eval/TEST_CONTENT_PLAN.md`, which targets exactly this).

## Stage 1 Profiling — first run for vendor 2, and a bad default replaced (2026-09-07)

Prompted by a fair question while preparing to label vendor 2: which model
does Stage 1 actually use? Stage 1 is the first step run against every new
account in production, so the answer matters more than for any experiment
stage.

**Two things were wrong.** `default.yaml`'s `stage1_profile.model` was
`openrouter/stealth/ox-alpha` - an anonymized stealth test release, carrying
its own note warning that such models "may rotate/disappear without notice.
Confirm current availability ... before relying on this for a real run." And
**Stage 1 has never been evaluated**: `eval/harness.py` does not score it
(`stage1_profile` is optional in `ExperimentConfig` and `main()` never loads
it), and no Stage 1 result appears anywhere in this file. So the first step
of the production pipeline was an unevaluated model explicitly flagged as
unstable.

**Changed** `default.yaml` to `gemini/gemini-3.5-flash-lite`: free-tier,
already used by Stage 2/3/4, and the winner of the same-day Stage 4
comparison (78% macro F1, vs Llama 3.1 8B at 40% and Qwen 2.5 7B at 10% -
both of which are the alternatives offered by `stage1_llama3_8b.yaml` /
`stage1_qwen2_7b.yaml`, and both of which handled structured extraction on
this data poorly).

**First Stage 1 output for `vendor_gadgets_01`**, checked against
independently-known ground truth:

| Field | Model output | Correct? |
|---|---|---|
| business_category | `gadgets` | yes - iPhones, laptops, JBL, PS5 |
| seller_style | `catalog_poster` | yes - structured price-list posts |
| language_mix | `english` | **arguable** - 3/35 captions carry Nigerian slang ("awoof", "gbanjo"), so `mixed` is defensible |
| pricing_behavior | `mixed` | yes - most prices in captions, 2 DM-for-price, several on-image |
| vendor_username | `oluwadunnioluajayi` | yes |

4 of 5 clearly right, 1 judgment call. `language_mix` is the interesting one:
at 3/35 posts the slang is real but marginal, and the schema offers no
"predominantly english" option - a genuine ambiguity in the label vocabulary
rather than a model error.

**This is a reasoned default, not a measured one.** Nothing scores Stage 1,
so the choice rests on the model's performance at adjacent stages plus a
single qualitative check on one account. A proper Stage 1 evaluation would
need gold profiles per account, which do not exist. Flagging rather than
overstating.

Side effect: `runs/vendor_gadgets_01/` now contains `profile.json`, so both
vendors' run directories have the same shape. The earlier asymmetry was not a
structural inconsistency - Stage 1 had simply never been run for vendor 2.

## Audit round 2: three more silently-dormant model defaults (2026-09-08)

Second read-only audit, scoped to code changed since 2026-09-07. It re-verified
the three earlier fixes (Stage 4 `model` binding, the token-in-URL leak,
`stage5_reconcile`'s `media_url`) and found them sound. Three new issues, all
the same species as the ones already recorded: a value that is silently ignored
or silently wrong, with no loud failure.

**Fixed - `triage_post()`'s in-module defaults were the retired model.**
`pipeline/stages/stage2_triage.py` defaulted `text_model`/`vision_model` to
`openrouter/google/gemini-flash-1.5-8b`, which is retired and 404s - the very
model `default.yaml` was changed away from on 2026-09-07. Dormant only because
every shipping config sets both keys explicitly; the first config to omit
either, or any direct `triage_post(post)` call, would 404 on every Stage 2 call
without a loud failure. Stage 3 and Stage 4 both keep a current in-module
`MODEL` constant; Stage 2 had none and hardcoded a stale literal instead. Now
has `MODEL = "gemini/gemini-3.5-flash-lite"` with both defaults referencing it,
matching the other two stages. Also corrected `stage1_profile.py`'s CLI help,
which advertised the retired model as its copy-paste example.

**Fixed - one unredacted error log missed by the 2026-09-07 token-leak sweep.**
`scripts/refresh_media_urls.py:85` logged the parsed Graph API error object raw
(`data["error"]`) while the branch five lines above correctly used
`redact_tokens(resp.text)`. Not a live leak - Graph error payloads don't echo
the token back - but it broke the file's own stated rule, and the sweep that
fixed its sibling lines missed it. Now redacted; all four `logger.warning` error
paths in that file are consistent.

**NOT a bug - `gemini-3.6-flash` in `stage2_gemini_flash.yaml`.** The audit
flagged this as very likely a typo for `3.5-flash`, on the reasoning that the
string appears nowhere else in the repo while every other Gemini reference uses
`3.5-flash-lite`/`3.5-flash`/`3.1-pro-preview`. Checked against a live
`GET v1beta/models` call rather than accepting the inference:
`gemini-3.6-flash` **exists and supports generateContent** (as do `3.7-flash`
and `3.8-flash`). Config left unchanged. Worth recording as a case where the
"appears nowhere else, therefore wrong" heuristic - which correctly caught real
bugs elsewhere in this file - produced a false positive, and a one-call check
settled it.

**Open, not fixed - Stage 1 configs are dead.**
`pipeline/stages/stage1_profile.py::_default_model_from_config()` hardcodes
`load_experiment_config(DEFAULT_EXPERIMENT_PATH)`, so it reads `default.yaml`
regardless of which `--config` is in play. `stage1_llama3_8b.yaml` and
`stage1_qwen2_7b.yaml` therefore set a `stage1_profile.model` that **no code
path ever reads** - pointing at them to "compare Stage 1 models" silently runs
whatever `default.yaml` configures. Same failure shape as the Stage 4 binding
bug (config value ignored, run looks successful) but a different mechanism -
a hardcoded path rather than a stripped reserved key - so `load_stage_fn()`'s
tripwire cannot catch it, because Stage 1 never goes through `load_stage_fn()`
at all. This also sharpens the earlier note that "Stage 1 has never been
evaluated": through those two configs it could not have been. Needs a decision -
either thread the active config path into `_default_model_from_config()`, or
delete the two snapshots, since today they claim to do something they cannot.

## Vendor 2 (gadgets/electronics) — Full Stage 1-5 Model Comparison (2026-09-08)

First multi-model sweep against a **second vendor**, and the first chance to ask
whether any vendor-1 conclusion generalises. Golden set:
`eval/golden/vendor_gadgets_01.json` — 41 posts, 98 products, hand-labelled
2026-09-07 (36 posts from captions alone, 5 from vendor-supplied on-image text).

**Read the caveats at the bottom before quoting any number.** Two of the runs
had to be discarded and re-run, and the Stage 4 macro-F1 row is not computed
over a constant denominator.

### Stage 1 — Profiling

Not scored; `eval/harness.py` has no Stage 1 path. Run once on
`gemini/gemini-3.5-flash-lite`, output checked against independently-known
ground truth: 4 of 5 fields correct (`business_category`, `seller_style`,
`pricing_behavior`, `vendor_username`); `language_mix` arguable — 3/35 captions
carry Nigerian slang ("awoof", "gbanjo"), so `english` vs `mixed` is a coin-flip
the label vocabulary cannot express. No model comparison is possible here: see
the 2026-09-08 audit entry — `_default_model_from_config()` hardcodes
`default.yaml`, so `stage1_llama3_8b.yaml` / `stage1_qwen2_7b.yaml` set a model
nothing reads.

### Stage 2 — Triage

| Config | Pass A / Pass B | Accuracy | Pass A only | Pass B vision | Escalation | Tokens |
|---|---|---|---|---|---|---|
| `stage2_gemini_cascade` | Gemini 3.5 Flash-Lite / 3.5 Flash | **38/41 = 93%** | 33/35 = 94% | 5/6 = 83% | 6/41 = 15% | 20,316 |
| `stage2_llama_cascade` | Llama 3.1 8B / Llama 4 Scout | **38/41 = 93%** | 33/36 = 92% | 5/5 = 100% | 5/41 = 12% | 20,665 |
| `stage2_qwen_cascade` | Qwen 2.5 7B / Qwen3-VL 8B | 36/41 = 88% | 31/36 = 86% | 5/5 = 100% | 5/41 = 12% | 20,828 |
| `stage2_gpt4o_mini_cascade` | GPT-4o Mini (both passes) | **38/41 = 93%** | 33/36 = 92% | 5/5 = 100% | 5/41 = 12% | **184,082** |

**A three-way tie, which contradicts vendor 1.** On `vendor_autos_01` GPT-4o
Mini looked like a clear Stage 2 winner (97% vs 90%); here it merely ties
free-tier Gemini *and* Llama, which had trailed at 90%. A single-vendor ranking
was carrying more weight than it could bear.

**The token column is the real result.** GPT-4o Mini spent **9x** everyone
else's tokens for identical accuracy — its vision escalations tokenize images at
~25-30k each and this vendor has 5-6 of them. The same effect was recorded for
vendor 1, so it is now confirmed as a property of the model rather than of one
dataset. Every model escalated on essentially the same 5-6 posts (the
caption-less, image-priced ones) and vision Pass B scored 100% for three of four
models — the cascade doing exactly what it was designed to do.

### Stage 3 — Extraction

| Config | Pass A / Pass B | Price accuracy | Name sim. | Name sim. excl. nulls | Returned no name | Missing-price recall |
|---|---|---|---|---|---|---|
| `default` (Gemini 3.5 Flash-Lite) | text+vision same | **28/31 = 90%** | **74%** | 81% | 3/34 | 3/3 = 100% |
| `stage3_qwen_cascade` | Qwen3 14B / Qwen3-VL 8B | 26/31 = 84% | 64% | 77% | 6/34 | 3/3 = 100% |
| `stage3_gpt4o_mini` | GPT-4o Mini (both passes) | 19/31 = 61% | 44% | 72% | **13/34** | 3/3 = 100% |

**The headline name-similarity numbers are misleading; the last two columns are
the real story.** GPT-4o Mini's 44% is overwhelmingly a failure to *answer*, not
to name — it returned no product name on 13 of 34 products (38%). Excluding
nulls, all three sit in a 72-81% band, and most of that residual gap is a
labelling artifact: gold names carry condition prefixes ("Premium UK used iPhone
11 64GB") that models reasonably omit, costing ~35 points on an
otherwise-correct answer. The genuine differentiator is omission rate: 3 vs 6
vs 13.

All three hit 100% missing-price recall — none invented a price (brief section
11's hard requirement) on the vendor with 7 genuinely price-less products.

### Stage 4 — Signals

| Signal | Gemini 3.5 Flash-Lite | Llama 3.1 8B | Qwen 2.5 7B | GPT-4o Mini |
|---|---|---|---|---|
| clearance | **100%** | 40% | 0% | **100%** |
| finance_available | **100%** | 50% | 0% | **100%** |
| price_negotiable | **100%** | 40% | 0% | **100%** |
| sold | **100%** | **100%** | 0% | **100%** |
| stock_count_known | 0% (FP, no gold) | 0% (FP, no gold) | — | — |
| swap_deal | — | 0% (FP, no gold) | — | — |
| urgent | 0% (FP, no gold) | 0% (FP, no gold) | — | — |
| **Macro F1 (as reported)** | **67%** (over 6) | **33%** (over 7) | **0%** (over 4) | **100%** (over 4) |
| **Macro F1, gold-covered signals only** | **100%** (over 4) | 58% (over 4) | 0% (over 4) | **100%** (over 4) |
| Real cost | $0.00 (free tier) | ~$0.0003 | ~$0.001 | $0.0082 |

**The reported macro-F1 row is not like-for-like** — the denominator differs per
model, because `score_stage4()` includes any signal that was *predicted*, even
one with zero gold instances, and scores it 0%. Gemini's 67% and GPT-4o Mini's
100% both mean "100% on all four signals that have gold coverage"; the entire
difference is that Gemini also false-positived on `urgent` /
`stock_count_known` and GPT-4o Mini did not. The added row is the honest
comparison.

All four of Gemini's false positives trace to one recurring caption template,
`"ONE LUCKY BUYER"`, read as scarcity and as a count of one. On the two posts
that also say SOLD that is arguably incoherent (urgency about a sold item); on
the available one it is a defensible reading the gold rejected.

**Qwen's 0% is genuine, not a failed run**: all 8 calls succeeded against the
correct model with zero errors and 41 clean prediction entries — it simply
emitted no signals at all. That reproduces its vendor-1 behaviour exactly (10%
macro F1, zero predictions for five of seven signal types). Llama again fails
the opposite way — high recall, poor precision (1/4, 1/3, 1/4), 12 false
positives — reproducing its vendor-1 over-firing.

### Stage 5 — Routing

| Config | auto_import | needs_attention | auto_exclude | Attention rate |
|---|---|---|---|---|
| Gemini Cascade | 25/41 = 61% | 9/41 = 22% | 7/41 = 17% | **22%** |
| Qwen | 24/41 = 59% | 8/41 = 20% | 9/41 = 22% | **20%** |
| GPT-4o Mini | 16/41 = 39% | 17/41 = 41% | 8/41 = 20% | **41%** |
| *(vendor 1, Gemini, for reference)* | 17/30 = 57% | 10/30 = 33% | 3/30 = 10% | 33% |

Llama has no Stage 5 row: no `stage3_llama*` config exists, so there is no
same-family Stage 3 prediction to route from, and substituting another model's
Stage 3 would not be a Llama result.

**Stage 5 inverts the Stage 4 ranking, and that is the most useful finding
here.** GPT-4o Mini won Stage 4 outright yet produces the *worst* attention rate
(41%, nearly double Gemini's) — because Stage 5 routes on Stage 3's output, and
GPT-4o Mini's 13 missing product names become "Product name unknown" and
"Missing price" flags. Qwen's 20% looks best but is an artifact: detecting no
`sold` signals means never raising the sold flag, so it auto-imports items a
human should check. **No model reaches the POC's <=10% target on either
vendor**; the binding constraint is Stage 3 completeness and Stage 5's flag
rules, not model choice at Stage 4.

### Caveats — two runs discarded, and why

- **`stage4_gpt4o_mini`, first attempt: discarded.** All 8 Stage 4 calls died
  with `getaddrinfo failed` (DNS/network loss), exhausting 6 retries each, and
  the run reported 0% macro F1. Re-running with `--only-stage 4` gave **100%**.
  A run that reports a clean 0% because the machine lost its network is
  indistinguishable, in harness output, from a model that answers nothing — the
  only tell was zero rows in `report/token_log.csv`. **Check the token log
  before believing a 0%.**
- **Gemini free-tier quota exhaustion** (432 quota errors) wrecked Stage 2 in
  the later runs, which is why several show 38-41 errored posts at Stage 2.
  Stage 4 is scored independently so those Stage 4 numbers stand, but the Stage
  2 column comes only from the four dedicated `stage2_*` runs made before the
  quota ran out.
- **`--only-stage` was added to `eval/harness.py`** as a direct result: a
  Stage 4 comparison previously paid for Stage 2 and Stage 3 on every run
  (~75 wasted calls out of ~83 — and it was those Gemini calls that exhausted
  the quota). The GPT-4o Mini re-run took **31 seconds instead of ~8 minutes**.
- **Signal coverage remains thin**: only 4 of 7 signal types have any gold
  instance in this golden set (5 instances total); `urgent`, `swap_deal` and
  `stock_count_known` have none — so no model can be scored on them here, and
  every prediction against them is necessarily counted a false positive.
  `eval/TEST_CONTENT_PLAN.md` drafts the posts that would close this.

### Artifact caveat — GPT-4o Mini Stage 4 result is valid but not currently reproducible

The **100%** figure for GPT-4o Mini in the Stage 4 table above came from a real,
verified run: `stage4_gpt4o_mini_20260908T070314Z`, 8 calls recorded in
`report/token_log.csv`, 41 clean prediction entries, zero errors, 31 seconds
wall time with `--only-stage 4`. That run is the basis for both its Stage 4 row
and its Stage 5 row (41% attention rate), which was computed while those
predictions were on disk.

**The on-disk artifacts no longer match.** A queued batch job re-ran the same
config afterwards and overwrote both
`report/gadgets_runs/stage4_gpt4o_mini.txt` and
`report/vendor_gadgets_01/stage4_openrouter_openai_gpt-4o-mini_predictions.json`
with a failed run (8/41 errored, 0% macro F1). Two attempts to regenerate the
good state failed for two *different* reasons:

1. First failure: `litellm.APIError ... getaddrinfo failed` - transient DNS/
   network loss, 6 retries exhausted per call.
2. Second failure: **HTTP 402, OpenRouter credits exhausted** (16x `402`,
   account usage $0.197 with no remaining balance). A 5-token probe call still
   returns 200, but Stage 4's ~900-token requests are rejected on estimated
   cost.

**To regenerate**: add OpenRouter credit, then
`uv run eval/harness.py --golden eval/golden/vendor_gadgets_01.json --config
pipeline/config/experiments/stage4_gpt4o_mini.yaml --only-stage 4` (~31s, ~$0.008).
Until then the predictions JSON on disk is the errored version and must not be
fed to Stage 5.

**Process lesson, and the third instance this session**: a harness run that
loses its network or its credit reports a clean `0%` macro F1 that is
indistinguishable, in the printed output, from a model that genuinely answers
nothing. The only reliable tell is **zero rows in `report/token_log.csv` for
that run_id**. Qwen's 0% is real (8 calls logged, 41 clean entries, reproduced
across two independent runs); GPT-4o Mini's 0% was not. Check the token log
before believing any 0%, and never let a queued batch silently overwrite a
verified result - `--only-stage` exists partly to make targeted re-runs cheap
enough that this is avoidable.

## Cost analysis — token economics and production viability (2026-09-08)

All token counts below are measured from `report/token_log.csv` for
`vendor_gadgets_01` (41 posts), not estimated. Per-token prices were pulled
live from OpenRouter's `/api/v1/models` on 2026-09-08, not taken from the
config `note` fields (several of which are stale).

### Tokens per post

Full pipeline, all four stages, from the `default` config run plus the one-off
Stage 1 call amortised across the account:

| Stage | Calls | Prompt | Completion | Tokens/post | Share |
|---|---|---|---|---|---|
| Stage 1 (profile, amortised over 41) | 1 | 1,038 | 46 | 26 | 1% |
| Stage 2 (triage) | 42 | 17,647 | 2,850 | 500 | 18% |
| **Stage 3 (extraction)** | 37 | 69,673 | 12,789 | **2,011** | **74%** |
| Stage 4 (signals) | 8 | 7,574 | 490 | 197 | 7% |
| **Total** | 88 | 95,932 | 16,175 | **2,734** | |

**Stage 3 is three quarters of all token spend.** Stage 4 is the cheapest stage
despite having the longest prompt, because its regex prefilter lets only 8 of 41
posts reach the model - the two-layer design is doing exactly what it was built
for. Stage 1 is rounding error, since it runs once per account rather than per
post.

### Cost per post at PAID rates

Live OpenRouter pricing, $/1M tokens (prompt / completion):
Gemini 3.5 Flash-Lite `0.30 / 2.50` · GPT-4o Mini `0.15 / 0.60` ·
Qwen 2.5 7B `0.10 / 0.20` · Llama 3.1 8B `0.05 / 0.08`.

| Model | $/post | $/1,000 posts | $/100,000 posts |
|---|---|---|---|
| Gemini 3.5 Flash-Lite | $0.001688 | $1.69 | $168.82 |
| GPT-4o Mini | $0.000588 | $0.59 | $58.77 |
| Qwen 2.5 7B | $0.000313 | $0.31 | $31.29 |
| Llama 3.1 8B | $0.000149 | $0.15 | $14.86 |

**The free tier has been concealing a cost inversion.** Every `Cost: $0.0000`
line in this file is a Gemini free-tier artifact. At paid rates Gemini
3.5 Flash-Lite is the *most expensive* option measured here - its completion
tokens cost $2.50/M, more than 4x GPT-4o Mini's $0.60/M and 31x Llama's $0.08/M.
The model that wins on quality is 11x the price of the cheapest one. Any
production costing that carries the recorded `$0.0000` forward is wrong.

### The finding that actually decides viability: human review dominates

Attention rate drives reviewer time, which dwarfs inference. Assuming **45
seconds** of reviewer time per flagged item (see caveat):

| Model | LLM /1k posts | Flagged /1k | Human @$6/hr | Human @$25/hr | Human ÷ LLM |
|---|---|---|---|---|---|
| Gemini Cascade | $1.69 | 220 | $16.50 | $68.75 | **10x / 41x** |
| GPT-4o Mini | $0.59 | 410 | $30.75 | $128.12 | **52x / 217x** |
| Qwen | $0.31 | 200 | $15.00 | $62.50 | **48x / 202x** |

Break-even - the attention rate at which reviewer cost would merely *equal*
inference cost, at $6/hr:

| Model | Break-even attention rate | Actual |
|---|---|---|
| Gemini 3.5 Flash-Lite | 2.25% | 22% |
| GPT-4o Mini | 0.79% | 41% |
| Qwen 2.5 7B | 0.41% | 20% |

Every model is one to two orders of magnitude away from the point where token
price matters at all.

### Three consequences for a production/enterprise case

**1. Optimising model choice for token price optimises the wrong variable.**
Switching Gemini to Llama saves $1.54 per 1,000 posts. Cutting the attention
rate from 22% to the POC's 10% target saves ~$9 per 1,000 - roughly six times
more, and it is the stated goal anyway. Engineering effort belongs in Stage 3
completeness and Stage 5's flag rules, not in model shopping.

**2. GPT-4o Mini is the trap case.** It is ~3x cheaper per token than Gemini and
won Stage 4 outright, yet produces the highest *total* cost of the three - its
13 missing product names (of 34) become "Product name unknown" and "Missing
price" flags, pushing attention to 41%. Cheapest inference, most expensive
system. This is the clearest evidence in the project that per-stage benchmarks
do not compose into a system-level ranking.

**3. Unit economics are comfortable; the leverage is elsewhere.** At 30 new
posts/vendor/month, a vendor costs roughly **$0.55/month all-in** (Gemini,
$6/hr review), of which ~90% is labour. 10,000 vendors is on the order of
$5.5k/month. The model is affordable at enterprise scale - the question is
whether attention rate can be driven down, not whether inference is affordable.

### Caveats

- **45 seconds per reviewed item is an assumption, not a measurement**, and it
  is the single most load-bearing number here - every human-cost figure scales
  linearly with it. Worth timing against real `needs_attention` items before
  quoting any of this externally.
- Costs assume the vendor-2 token profile (2,734 tokens/post). A vendor with
  longer captions, more carousels, or more caption-less image-priced posts
  (which force vision escalation) will differ. GPT-4o Mini in particular is
  highly sensitive to escalation rate: its Stage 2 vision passes tokenize images
  at ~25-30k each, which is why its Stage 2 total was 184,082 tokens against
  everyone else's ~20,000 for identical accuracy.
- No Stage 6 sync cost is modelled. Sync is zero-LLM except caption embeddings
  on changed posts, so it is small, but it recurs per sync rather than per post.
- Llama has no attention-rate row: no `stage3_llama*` config exists, so it has
  no same-family Stage 5 result.

---

## Audit round 3: Gemini key leak, Stage 3 error isolation, duplicate Stage 5 flags (2026-09-08)

A read-only audit of the pipeline stages, config loading, token handling, and
the eval harness. Five findings acted on, one left open pending a live run.
No model calls were made; every number below comes from cached predictions.

### 1. The Gemini API key was passed in the URL and logged on failure (critical)

`pipeline/media_fingerprint.py::compute_caption_embedding()` authenticated with
`params={"key": GEMINI_API_KEY}` and logged the raw exception on failure:

```python
except Exception as exc:
    logger.warning("[embedding] %s: Gemini embedding call failed: %s", post_id, exc)
```

This is the same mechanism as the 2026-09-07 Instagram token leak, in a file
that sweep did not touch. `requests` builds `HTTPError`'s message from the full
request URL, query string included, so any non-200 printed the key. Three
aggravating details:

- The branch logs at **WARNING**, so it is visible at any `LOG_LEVEL` - not a
  `LOG_LEVEL=DEBUG`-only exposure.
- `GEMINI_API_KEY` is not an embeddings-scoped credential. It is the key
  litellm resolves for every `gemini/*` call in Stages 1-4, i.e. the pipeline's
  primary model credential.
- The failure is demonstrated, not hypothetical: this key has already been
  quota-exhausted mid-run (432 errors, 2026-09-08, same page).

`redact_tokens()` would not have helped either - it only matched
`access_token=` and `Bearer `, with no pattern for `key=`.

**Fixed.** The call now uses an `x-goog-api-key` header via a new
`settings.py::gemini_auth_headers()` (mirroring `ig_auth_headers()`), and the
except branch logs `redact_tokens(exc)`. `redact_tokens()` gained patterns for
`key=` / `api_key=` (with a leading word boundary, so `monkey=` is not a match)
and for an `x-goog-api-key` header echoed into an error string.

### 2. `score_stage3()` had no per-post error handling (medium)

`score_stage2()` and `score_stage4()` both wrap their per-post call in
`try/except` precisely so one bad post cannot end a run. `score_stage3()` did
not, around either `extract_fn(post)` or the `predicted[0]["price"]` unpacking.
This only looked safe because `extract_product()` swallows everything into
`_regex_fallback` - an accident of the one real implementation, not something
the harness enforced. Any alternate Stage 3 module, or a future narrowing of
that internal `except Exception`, would take down the whole run.

**Fixed.** Same pattern as its siblings. Two deliberate choices:

- The `predicted[0]["price"]` unpacking is inside the same `try`: a stage
  returning a product with no `price` key is the same class of failure as one
  that raises.
- An errored post counts in the missing-price denominator but never as a hit.
  It produced no evidence the pipeline declined to invent a price, so it must
  not prop up the brief-section-11 recall number. A run where every call failed
  now reports 0% recall and an explicit error count, not a silent 100%.

### 3. Stage 5 emitted one flag per offending product, not per reason (medium)

`route_post()` never deduplicated `flags`, so a post with N priceless products
appended `MISSING_PRICE_FLAG` N times. Bucket assignment was unaffected (only
emptiness is checked), but two outputs were wrong: `run_stage5.py`'s
`flag_counts` frequency table - whose `[Nx] reason` lines are quoted verbatim
into this file - and the per-post needs-attention line shown to a reviewer,
which repeated the same sentence N times.

Real case: post `17966735571148449` (vendor_gadgets_01) has 3 products, all
`price.source == "none"`.

**Fixed** (`flags = list(dict.fromkeys(flags))`). Measured on cached
predictions, buckets identical, counts corrected:

| vendor_gadgets_01, Gemini Cascade | before | after |
|---|---|---|
| auto_import / needs_attention / auto_exclude | 25 / 9 / 7 | 25 / 9 / 7 (unchanged) |
| `[Nx]` Missing price | **8x** | **6x** |
| all other flag counts | unchanged | unchanged |

vendor_autos_01 is byte-identical before and after - it happens to have no
multi-product post with a repeated flag, which is why this survived that
vendor's runs. **Any "Missing price" count quoted from a vendor_gadgets_01
Stage 5 run before today is inflated**; the routing distributions are fine.

### 4. `.env` is in git history, and `.env.example` never existed (housekeeping)

`.env` was committed in `03e5554` and deleted in `17f9812`. Both are ancestors
of `origin/main`, so `git show 03e5554:.env` still retrieves it from any clone.
The value that time was a 3-character placeholder (verified by length only, not
printed), so nothing needs rotating - but `.gitignore` demonstrably did not
prevent the staging, and deleting a file does not remove it from history.

Separately, seven experiment YAMLs pointed readers at a `.env.example` that was
neither tracked nor on disk.

**Fixed.** Added `.env.example` (key names, empty values) and
`scripts/scan_secrets.py`, installed as a pre-commit hook by
`make install-hooks` (`.pre-commit-config.yaml` is there too for anyone who
already uses that framework; it is not a dependency). The scan refuses:

- `.env` and `.dvc/config.local` by path, whatever their contents;
- anything under `eval/golden/`, `runs/`, `data/snapshots/`, `report/` except
  `.dvc` pointers and `.gitignore` files - real vendor and commenter data must
  reach R2, never a git object;
- credential shapes by content (Google `AIza...`, `IGQ`/`EAA`, `sk-or-v1-`,
  `sk-`, `gsk_`, `AKIA`, plus a generic `<credential-name> = <long literal>`).

It never prints the matched value, and a false positive is waived with a
`pragma: allowlist secret` comment on the line - visible in the diff, so a
waiver is reviewable. `make check-secrets` scans every tracked file; the repo
is currently clean across all 65.

### 5. Still open: does Graph echo `access_token` into `paging.next`?

Both pagination loops follow Graph's `paging.next` URL verbatim with
`params = {}`, on the assumption that the URL carries no credential of its own.
If Meta does echo an `access_token` param into it, the follow-up request puts
the token back in the query string despite the header auth fix. Local logging
is already defended (`redact_tokens()` on the error paths, urllib3 raised to
INFO), so this would be a wire-level issue only.

**Cannot be answered offline**: raw dumps persist only the merged media list,
not the paging envelope - confirmed by grepping every `dump_*.json` in `runs/`
for `paging`, `next`, and `access_token=` (zero hits in all five).

**Mitigated rather than left pending.** `settings.py::strip_url_credentials()`
now strips credential params from the next-page URL before it is followed, in
both `ingest.py::_next_page_url()` and `run_stage6.py::_fetch_all_media()`.
This is free and correct either way - the Authorization header still
authenticates the follow-up - and it logs a WARNING naming the fact, never the
value, when a credential was actually present. **The next authorized `make
ingest` or `make sync` answers the question**: a `[paging]` warning in the run
output means Graph does echo the token, and that result belongs in this file.

### Tests added (`tests/`, 47 passing)

There is still no broad test suite by design - `eval/harness.py` remains the
verification mechanism for model quality. `tests/` covers only what the harness
structurally cannot, and every test is offline (no network, no `.env`, no
golden-set reads):

- `test_credentials.py` - the embedding call sends the key in a header and
  never in the URL; a simulated 429 whose message carries a key is logged
  redacted; `redact_tokens()` covers every credential shape and leaves CDN
  cache params alone.
- `test_harness.py` - one failing post no longer aborts a Stage 3 run; an
  errored post never counts toward missing-price recall; the hallucinated-price
  check still fires. Plus a guard on `load_stage_fn()`'s reserved-parameter
  tripwire, which was previously only exercised on the day someone
  reintroduced the 2026-09-07 shadowing bug.
- `test_stage5_reconcile.py` - flag deduplication, and that dedup does not drop
  distinct reasons.
- `test_scan_secrets.py` - the scanner catches this project's credential
  shapes, honours the allowlist pragma, never echoes a matched value, and stays
  quiet on `.env.example` and its own fixtures.

Run with `make test`. `pytest` was added to the uv dev dependencies.

## KNOWN INACCURACIES — read before quoting any number from this file (2026-09-08)

Audit of this document's own reliability, written so nothing above gets lifted
into a stakeholder report without its caveat. Ordered by how much damage a
misquote would do.

### 1. Structural — the one caveat that must survive into any external report

**Every per-stage accuracy figure in this file assumes perfect upstream
routing.** `eval/harness.py` scores each stage against **gold labels**, not
against the previous stage's predictions, and there is no chained end-to-end
runner anywhere in the codebase. A reader who sees "Stage 2 93%, Stage 3 90%,
Stage 4 100%" will reasonably infer a system that works end-to-end at roughly
that level. **That has never been measured.** A real chained run would compound
Stage 2's errors into Stage 3 and Stage 5 and would be lower. Do not present
these as system accuracy.

### 2. Values that no longer reproduce

- **Vendor 1 Stage 5 routing.** Recorded throughout as 17/30 auto_import,
  10/30 needs_attention, 3/30 auto_exclude (**33% attention rate**). Re-run
  2026-09-08 produces **18/9/3 = 30%**. Working-tree changes since the original
  measurement shifted it. This propagates: the vendor-2 Stage 5 table cites 33%
  as its reference row, and the cost analysis derives human-review cost from
  attention rates.
- **`make stage5` is currently broken** (`Makefile:90: *** missing separator`),
  so the documented verification command does not run. Use
  `uv run scripts/run_stage5.py` directly until fixed.

### 3. Stale — measured before a later fix changed the mechanism

- **The 2026-09-07 Stage 4 re-run** (Gemini 78% / Llama 40% / Qwen 10% /
  GPT-4o Mini 68%) was measured **before** `PREFILTER_RE` was fixed the same
  day. That fix changed vendor 1's short-circuit rate from 17.6% to 6% and
  unblocked two gold-signal posts that previously could not reach the model at
  all. The recall half of those numbers is stale. The *ranking* is probably
  unaffected (all models share one prefilter) but has not been re-verified.

### 4. Valid but not reproducible

- **GPT-4o Mini, vendor 2: Stage 4 = 100% and Stage 5 = 41%.** Both come from a
  genuine verified run (`stage4_gpt4o_mini_20260908T070314Z`, 8 calls in
  `token_log.csv`, 41 clean entries). A queued batch later overwrote the
  artifacts with a failed run, and OpenRouter credit is now exhausted, so they
  cannot currently be regenerated. The numbers are real; the evidence on disk is
  not. See the artifact caveat above.

### 5. Already marked invalid above — do not quote at all

- The **2026-09-02 Stage 4 Signals comparison** (all three columns ran the same
  model).
- The **GPT-4o Mini Stage 4 column in the 2026-09-05 section** (same cause).
- That section's **`stock_count_known` root-cause conclusion** ("identical
  across all three models, therefore a prompt/labelling mismatch") — an artifact
  of the same bug.

### 6. Assumptions presented alongside measurements

- **45 seconds of reviewer time per flagged item** is invented, not measured.
  Every human-cost figure in the cost analysis scales linearly off it. It is the
  single most load-bearing number in the financial case and the easiest for a
  stakeholder to challenge.
- **Paid per-token pricing is applied to runs actually made on Gemini's free
  tier.** The comparison is arithmetically sound but no money was spent on the
  Gemini runs; do not present the cost table as observed spend.

### 7. Data-quality issues affecting the numbers

- **Suspected gold-labelling error, vendor 1 `18079164065330268`**: caption says
  "Financing available through our partner bank" while gold `expected_signals`
  is `[]`. If that is a mislabel, every model that correctly detects it is
  scored a false positive, depressing `finance_available` precision in every
  comparison that includes vendor 1. Never investigated.
- **Noise floor is large relative to the differences being reported.** Re-running
  the *same* model on the *same* golden set moved per-signal F1 by 20-35 points
  (`stock_count_known` 44% to 73%, `sold` 100% to 67%), because most signals have
  only 1-3 gold instances. Only large macro-level gaps are safe to quote; single
  per-signal cells are not.
- **Vendor 2 has zero gold instances for `urgent`, `swap_deal` and
  `stock_count_known`** (3 of 7 signals). No model can be scored on them there,
  and any prediction against them is counted a false positive by construction.
- **Vendor 2's Stage 2 table is clean**, but later runs in the same file show
  38-41 errored posts at Stage 2 from Gemini quota exhaustion. Those errors do
  not affect the Stage 2 column (which predates them) or Stage 4 (scored
  independently), but the raw logs will look alarming without this note.

### 8. What is safe to quote as-is

- **Token economics**: 2,734 tokens/post, Stage 3 at 74% of spend, Stage 4
  cheapest despite the longest prompt. Measured from `token_log.csv`, unaffected
  by everything above.
- **Cost per post at paid rates** — arithmetic over real token counts and live
  pricing (subject to the free-tier caveat in section 6).
- **Vendor 2 Stage 2 and Stage 3 comparison tables** — clean runs, verified
  against the token log.
- **The qualitative findings**, which are the strongest material here anyway:
  inference cost is 10-217x smaller than the human review it triggers; the
  cheapest-per-token model produced the most expensive system; and single-vendor
  model rankings did not survive contact with a second vendor.

### 9. Regenerating anything — current constraints

OpenRouter credit is exhausted and cannot be topped up before the demo, so
**nothing involving GPT-4o Mini, Llama 3.1 8B or Qwen 2.5 7B can be re-run**
until it is. Everything in `default.yaml` is Gemini end to end (Stages 1-4) and
remains runnable on the free tier, subject to a 15 req/min and a daily quota
that this project has already exhausted once in a day.

Zero-LLM paths are unaffected by both constraints and can be re-run freely:
`scripts/run_stage5.py`, `scripts/run_build_snapshot.py` (embeddings aside),
`scripts/simulate_stage6.py`, and `make test`.

## Chained pipeline vs. independent per-stage scoring (2026-09-09)

First measurement of what this file's own headline caveat is actually worth.
Every per-stage number above comes from `eval/harness.py`, which scores each
stage **independently against gold**: `score_stage3` runs on posts whose *gold*
`post_type == "product_listing"`, regardless of what Stage 2 predicted. So every
figure assumes perfect upstream routing, and nothing had ever measured the cost
of that assumption.

Two new pieces close the loop:

- **`scripts/run_pipeline.py`** — runs Stages 1-5 *chained* on live predictions
  and emits `runs/<account>/catalog.json`. It reads **no golden set at all**, so
  it works on any account the moment a raw dump exists. Stage 3/4 run only on
  posts Stage 2 *predicted* as listings; the Stage 1 profile is threaded into
  Stages 2/3/4, which the harness never does (the gap flagged 2026-09-02, so the
  `[VENDOR]`/`[BUYER]` comment labeling had never actually fired in a scored
  run). Stages resolve through `harness.load_stage_fn()`, so the YAML stays the
  single source of truth and the reserved-parameter tripwire still applies.
- **`scripts/score_catalog.py`** — scores that catalog against gold on the
  harness's own denominators, so the two sit side by side. Kept separate from the
  runner on purpose: "reads no golden set" is the runner's design claim and is
  covered by tests.

The load-bearing choice: **a gold `product_listing` that Stage 2 mis-routed is
scored as a miss, not excluded from the denominator.** Excluding it would
reproduce the harness's own numbers and measure nothing. `tests/test_score_catalog.py`
opens with a test named for exactly that failure.

### Result — `vendor_gadgets_01`, `default` config, 41 posts

| Metric | Harness (independent) | Chained | 
|---|---|---|
| Stage 2 accuracy | 38/41 = 93% | 38/41 = 93% |
| Stage 3 price accuracy | 28/31 = 90% | 29/31 = 94% |
| Name similarity | 74% | 73% |
| Missing-price recall | 3/3 = 100% | 3/3 = 100% |
| Attention rate | 22% | 17% |
| **Routing penalty** | — | **1 post** |

**The routing penalty is one post, and it is not a misclassification.** The single
gold listing that never reached extraction (`18351809149247806`) is the post whose
Stage 2 call died on a transient Gemini 503; per-post error isolation routed it to
`unknown` → `auto_exclude`. Stage 2 is accurate enough at the `product_listing`
boundary that chaining costs essentially nothing on this data.

**Do not read the other deltas as chaining effects.** Chained Stage 3 price
accuracy came out *higher* than independent (94% vs 90%), which routing cannot
cause — it is run-to-run nondeterminism, the same effect recorded in the
2026-09-07 noise-floor section. Two chained runs of this identical config on the
same day produced routing splits of 25/9/7 and 27/7/7 (attention 22% and 17%).
**Quote a 17-22% band, never a single figure**, and treat the chained-vs-
independent gap as "within noise, penalty ≈ 1 post" rather than as a measured
delta per metric.

### Fixed: Stage 3's regex fallback was invisible to the token log

Found by running the new chained path. When `extract_product()`'s LLM call fails
it falls back to `_regex_fallback()` — and logged **nothing**, so a run that
quietly degraded several posts to heuristic extraction was indistinguishable in
`report/token_log.csv` from a clean one. The `fallback_used` column has existed
since the log was created and **no caller had ever set it**.

Now writes a zero-token `stage3_extract_fallback` row with `fallback_used=True`,
and `run_pipeline.py` surfaces the count in its summary. Same species as the
Stage 4 binding bug: a measurement that silently reports success. In the first
41-post run 3 posts degraded this way (Gemini rate limits exhausting 3 retries);
all 3 carried the fallback's `extraction_confidence: 0.4` signature and were
routed to `needs_attention`, so a human would have seen them — the system
degraded correctly, it just could not say so.

### Family configs added — three of four UNRUN

Every existing `stage2_*`/`stage3_*`/`stage4_*` config swaps one stage and leaves
the others on Gemini (deliberately, so a stage-N comparison isn't polluted). None
of them is a same-family end-to-end run. Added
`pipeline/config/experiments/family_{gemini,llama,qwen,gpt4o_mini}.yaml`, each
holding one family across Stages 2/3/4. `family_llama.yaml` supplies the repo's
**first Llama Stage 3 entry**, closing the gap noted 2026-09-08 ("no `stage3_llama*`
config exists, so there is no same-family Stage 3 prediction to route from").

**Only `family_gemini` has run.** OpenRouter is at **credits 0 / usage $0.197**
(confirmed live, HTTP 200), so Llama, Qwen and GPT-4o Mini cannot make a single
call. The three unrun configs each carry a `note:` saying so, and **no number may
be published from them until each passes a 2-post smoke test with its own model
strings visible in `report/token_log.csv` for that run_id**. The repo already
carries two dead configs (`stage1_llama3_8b`, `stage1_qwen2_7b`) that no code path
reads; three more unverified ones would repeat the pattern that produced the
invalidated Stage 4 tables.

**Stage 1 is deliberately excluded from every `family_*.yaml`.**
`get_or_create_profile()` caches to `runs/<account>/profile.json`, so all four
families would silently share whichever profile was generated first — a per-family
Stage 1 number would be fiction.

### Not done: `vendor_autos_01` has no chained score

Attempted 2026-09-08. The run reached 32 of 40 posts and was killed at a 900s
timeout while in sustained Gemini free-tier 429 backoff (49 retry warnings), after
the day's two full 41-post gadgets runs had drained the quota. **No catalog was
written** — `run_pipeline.py` writes only at the end, so there is no partial
artifact to mistake for a result. Re-run on a fresh quota day. Until then the
chained-vs-independent finding above is single-vendor, which is exactly the
weakness the second vendor exists to catch — so it should not be generalised yet.

Practical note for anyone repeating this: **two full-account chained runs is
roughly the daily free-tier budget.** Schedule the autos run first and separately.

---

## This record continues in FINDINGS_BASELINE_2026-09.md (2026-09-09)

Re-baselining work from 2026-09-09 onward is recorded in
**`FINDINGS_BASELINE_2026-09.md`**, not here. Nothing above has been edited or
removed; this file stays the durable record through 2026-09-08.

**Before quoting any number above, read the first entry in that file.** The harness
never passed the Stage 1 profile into Stages 2/3/4 until 2026-09-09, while
`scripts/run_pipeline.py` always did. Every figure in this file was therefore
measured on different inputs than the production path uses — most acutely Stage 4,
whose `[VENDOR]`/`[BUYER]` comment labelling never fired in any scored run recorded
here. The numbers were honestly measured; they are simply not comparable to
anything in the new file, and the two must not be mixed in one table.

The new file also carries the rule that every number it records comes from a run
that passed `scripts/verify_run.py`, with its `run_id` named.
