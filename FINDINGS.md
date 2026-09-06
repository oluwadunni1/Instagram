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
