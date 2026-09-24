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
Stage 2  TRIAGE     Product post vs. not?                            (Jev -> OCR -> Gemini)
Stage 3  EXTRACT    name, variants, price, quantity                  (text -> +OCR -> vision)
Stage 4  SIGNALS    Stale? Out of stock?                             (regex prefilter -> LLM)
Stage 5  FLAGS      Confidence routing -> import vs. attention       (no AI)
Stage 6  SYNC       Re-pull -> diff -> AI on deltas only             (no generative calls)
```

Escalation triggers live in Python, not in prompt wording:

| Stage | Escalates when |
|---|---|
| 2 | Jev confidence < threshold -> Gemini text. Caption empty/emoji-only -> local OCR first; Jev re-runs on the OCR text, and only a still-unsure post reaches Gemini vision |
| 3 | missing name, `price.source == "none"`, or low confidence -> Tier 2 re-asks with the local OCR text; still unresolved -> vision. **Unless** `DM_FOR_PRICE_RE` matches (a confirmed absence, not uncertainty) |
| 4 | never. A free regex prefilter gates the LLM call, so a post with no candidate keywords costs zero |
| 5 | never. Deterministic Python over cached outputs, zero LLM calls |
| 6 | no generative calls ever. The diff itself is pure Python, but a new or caption-edited post costs one `gemini-embedding-001` call; an unchanged post costs nothing |

**OCR is the cascade's free tier.** `pipeline/ocr.py` runs locally, so a failed read costs only
latency. Both stages put it ahead of the vision model rather than in place of it: it narrows the
vision path, it does not replace it.

`llm_client.py::complete_structured()` is the single call site for every LLM interaction: JSON
repair, retry on schema mismatch, backoff on 429/5xx, throttling, token logging. Two kinds of
call bypass it and must therefore log and throttle themselves: the vision Pass B calls
(multimodal message shapes) and Stage 6's caption embeddings
(`pipeline/media_fingerprint.py`, a raw `embedContent` POST).

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
| Sync on unchanged account ~ $0 | 45 no-ops, 0 AI calls, 0 embedding rows | met, **measured 2026-09-24** |
| Change-classification precision >= 85% | 100%, n=23 changed posts | met, **first measurement** |
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
universal. Fixing it meant editing the clause, not the models.

**Changed 2026-09-21, and re-measured 2026-09-22.** `eval/LABEL_CODEBOOK.md`'s step 2 now tests
what a post is *about* rather than whether a sale happened, so a SOLD mark on a post whose
subject is still an item with its specs and price leaves it a `product_listing`. The wording is
carried in `pipeline/stages/stage2_triage.py`'s `POST_TYPE_DEFINITIONS` and
`scripts/smoke_jev.py`'s rubrics; all three changed together.

| `stage2_hybrid.yaml` | before | after | precision | escalation |
|---|---|---|---|---|
| autos (tune) | 30/30 = 100% | 30/30 = 100% | 100% | 7/30 = 23% |
| gadgets (test) | 38/41 = 93% | **39/41 = 95%** | 100% | 7/41 = 17% |

Run ids `stage2_hybrid_20260922T091356Z` and `stage2_hybrid_20260922T091520Z`, both VALIDATED,
and both still at **zero vision calls** - the Stage 2 OCR tier holds.

One of the three codebook misses is recovered and two remain; precision stays at 100% and autos
is unmoved, so the clause did not buy the gain by loosening something else. **The posts that
motivated the change are in the test half, so it was made with knowledge of the test set and
+1 on gadgets is not a clean held-out result.** Every Stage 2 figure recorded before 2026-09-22
predates the clause and is not comparable to one measured after it.

**Changed again on 2026-09-23, and this is the second time.** The 2026-09-22 clause said a post
is a `product_listing` even when it carries "a SOLD mark or an available-soon date". That second
half contradicted the rule directly below it - announcements cover "posts naming products that
are not purchasable yet" - and rule 2 is evaluated first, so the contradiction decided the
answer. The codebook itself was never ambiguous ("**Listing-shaped is not the same as
purchasable**"); only the two prompt copies were, and one gold label followed them.

`18024559289696773` ("iPhone Ultra Protective Case available soon ( Phone not included )") was
labeled `product_listing` with one product and is now `announcement` with none, matching
`18163097968466814`, the codebook's own documented trap for unreleased items. A sweep of both
golden sets for deferred-availability language found five posts and no other inconsistency: the
three that stay `product_listing` all carry a real offer ("Pre-order yours today", "has
arrived"), which is the dividing line now written into the codebook.

**What it costs, stated rather than buried.** The hybrid predicted `not_product` on that post at
0.9 confidence without escalating, so flipping the label turns a miss into a hit and gadgets
would read 40/41 rather than 39/41. It also drops Stage 3's scored set from 34 posts to 33 and
missing-price recall from 3/3 to 2/2. **No re-run has been made and no such figure is claimed
here.** More importantly: gadgets has now been adjusted twice in response to observed model
behaviour, so it is no longer a held-out set for Stage 2 in any useful sense, and its Stage 2
numbers should be read as "measured on a set we have twice corrected", not as generalisation.
The defensible part of this change is that a label contradicting its own codebook was fixed; the
accuracy gain that falls out of it is close to worthless as evidence.

### Stage 3, scored on every product - 2026-09-24

`score_stage3` compared `predicted[0]` against `gold[0]` and nothing else, so on
`vendor_gadgets_01` **31 of 91 priced gold products were ever checked - 34%**. 19 of its 34
product posts carry more than one product and one carries 15. It now scores every gold product,
aligned positionally, and reports the first-product metric beside it so figures recorded before
this date stay comparable.

Replayed through the corrected scorer from the cached `stage3_ocr` predictions - no model calls,
so this re-scores the exact output the original runs produced:

| | gadgets first-product | gadgets **all products** | autos first-product | autos **all products** |
|---|---|---|---|---|
| Price accuracy | 31/31 = 100% | **88/91 = 97%** | 19/19 = 100% | **24/24 = 100%** |
| Missing-price recall | 2/2 | **6/6** | 6/6 | **6/6** |
| Name similarity | 76% | 74% | 90% | 91% |
| Products returned / gold | | **90 / 97** | | 30 / 30 |
| Coverage of the legacy metric | 34% | | 79% | |

**Gadgets price accuracy is 97%, not 100%.** Three priced products were wrong, all of them by
never being returned at all:

| post | product | gold price |
|---|---|---|
| `18115331426067887` | Samsung Galaxy Tab S10 FE 256GB 5G | ₦900,000 |
| `18094596503428757` | Starlink Hook | ₦60,000 |
| `18094596503428757` | Starlink Hook & pipe | ₦120,000 |

**Under-extraction is the failure mode, and it was completely invisible.** Four of 19
multi-product gadgets posts returned fewer products than gold - 2→1, 3→1, 3→1, 5→3 - and every
one of them scored 100% before, because `products[0]` was right in all four. Seven gold products
were never returned. Autos shows none of this: 30 gold, 30 returned, which is why the vendor that
tops out at 3 products per post hid the problem, exactly as it hid the `max_tokens` truncation.

**Missing-price recall holds at 6/6 across all products** on both vendors, so nothing was
invented on the products the metric could not previously see. A gold product with no price that
the model never returned counts as a hit, not a hallucination - the brief's requirement is "never
invent a price", and under-extraction is counted separately rather than folded in.

Seven tests in `tests/test_harness.py` pin this, including that a hallucinated price on product 2
now fails loudly - it used to be silent.

### Stage 3, with the OCR tier

Stage 2's cascade drove its own vision spend to zero by putting local OCR ahead of the vision
model. That left Stage 3 holding every remaining vision call in the project - 214 of them, 67%
of all vision cost - while `pipeline/ocr.py` was already caching the text for exactly the posts
Stage 3 was paying to look at, and nothing read the cache.

Stage 3 scored alone (`--only-stage 3`). All four runs VALIDATED.

| vendor | config | calls | text | text+OCR | vision | tokens | price acc | missing-price recall | name sim |
|---|---|---|---|---|---|---|---|---|---|
| gadgets | `default.yaml` | 40 | 34 | 0 | **6** | 95,133 | 31/31 = 100% | 3/3 = 100% | 81% |
| gadgets | `stage3_ocr.yaml` | 35 | 29 | 5 | **1** | 80,888 | 31/31 = 100% | 3/3 = 100% | 76% |
| autos | `default.yaml` | 28 | 25 | 0 | **3** | 62,502 | 19/19 = 100% | 6/6 = 100% | 90% |
| autos | `stage3_ocr.yaml` | 31 | 25 | 3 | **3** | 68,729 | 19/19 = 100% | 6/6 = 100% | 90% |

Run ids: `default_20260921T233431Z`, `stage3_ocr_20260921T233746Z`,
`default_20260922T090452Z`, `stage3_ocr_20260922T090946Z`.

**The tier pays on one vendor and costs on the other, and the reason is the same fact both
times.** OCR only helps when the answer is printed on the picture. On gadgets it removed five of
six vision calls and 15% of tokens: those posts are caption-less with the product and price on
the image, which is what OCR is for. On autos it removed nothing and added three calls and 10%
more tokens - autos captions already carry the price, so Stage 3 escalates there for other
reasons, and the extra text pass answered nothing the caption had not. **Averaged across the two
it is roughly break-even on cost.** It is not a general saving; it is a saving on caption-less
product cards, and those have to exist in the feed for it to pay.

**Read the gadgets saving as image tokens, not a model downgrade.** Stage 2's OCR tier cut cost
79% because the vision call it displaced ran on `gemini-3.5-flash`. Here `default.yaml` already
points `vision_model` at `flash-lite`, so what OCR displaces is a same-model call carrying an
image. No dollar figure is quoted because no Gemini rate is on file (see Known limits).

**The price guard fired twice on autos, and that is the more interesting result.** On two posts
the model returned an image-sourced price - ₦32,600,000 and ₦123,000,000 - that the OCR text
contained no money-shaped token to support at all. The Pass A OCR call cannot see the image, so
an image-sourced price it cannot ground is an invention. Both were voided and escalated to
vision, and price accuracy stayed 19/19. Without the guard those two would have been published
as prices read off the photo. That is the failure the brief calls unforgivable, it appeared on
the FIRST vendor the tier was pointed at, and it appeared on the vendor where the tier otherwise
does nothing.

**Why a garbled read cannot become a wrong price.** OCR text reaches the model in its own
labelled `IMAGE TEXT` block, never as the caption - the substitution `stage2_triage_hybrid.py`
uses is safe for a product/not-product question and is not safe here. Any price marked
`price.source = "image"` must clear `_guard_ocr_prices`, which requires the exact integer to be
recoverable from the raw OCR text and refuses ambiguous separators: the spike's `N500.000` is
500000 or nothing, never 500. Checked against the five committed caches in
`runs/vendor_gadgets_01/ocr/` before it was wired: 5/5 grounded, and the IMEI in one of them
produces no candidate at all because it carries no currency marker.

**Name similarity: 76% against a 78-81% baseline band on gadgets, unchanged at 90% on autos.**
Two baseline gadgets runs measured 78% and 81%, two OCR runs both measured 76%. It is a free
fuzzy `SequenceMatcher` score, not a gate, and OCR text carries character noise by construction
(`IPH0NE`, `C0RE3`). Recorded rather than explained away.

**Not tried: gating the second tier on whether the OCR text contains a price at all.** All three
autos Tier 1 calls were spent on text that had no money token in it, and all three escalated to
vision anyway. Checking for a candidate before paying for the extra pass would have made autos
free instead of 10% more expensive. It is a change to the escalation rule, so it needs its own
gated run on both vendors.

**Two defects this comparison surfaced, both fixed:**

- The harness names its predictions file after the config's `model:` display label. Both configs
  started with the same label, so the first OCR run silently overwrote the baseline file it was
  meant to be compared against. `stage3_ocr.yaml` now carries a distinct label even though the
  litellm strings are identical.
- `verify_run.py`'s call-count band derives from `--posts`, but a `--only-stage 3` run touches
  only the posts whose *gold* type is `product_listing` - 34 of 41 on gadgets, 25 of 30 on
  autos. Passing the full post count failed both good runs on the floor. The band is right for a
  3-stage run and wrong for a single-stage one; pass the scored denominator until the gate
  learns about `--only-stage`.

**One earlier attempt is void and is recorded so nobody recovers it from the log.** Run
`stage3_ocr_20260921T234301Z` reported 68% price accuracy and 33% name similarity on autos. It
exhausted the Gemini free tier and logged 16 `fallback_used` rows - almost all of that output is
the regex heuristic, not the model. The companion `default_20260921T234042Z` carries one such
row. Both fail the gate. This is the documented 429-becomes-fake-accuracy failure, caught by the
mechanism built for it.

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

A re-sync finds most of a feed unchanged. The diff - content hash, then perceptual image hash,
then caption embedding - makes zero generative calls. An unchanged post costs nothing at all; a
new or caption-edited one costs a single `gemini-embedding-001` call, which the token log now
records (it did not until 2026-09-22, see below).

### The measured run - 2026-09-24

The first Stage 6 measurement against a **designed** change set: a live baseline was taken, the
vendor account was then changed deliberately, and the ground truth was written to
`eval/sync_labels/oluwadunnioluajayi_2026-09-24_truth.json` **before the sync ran**. Labels are
known by construction rather than guessed, which is what the brief's change-classification target
needs and what no earlier run had.

Baseline `snapshot_vendor_gadgets_01_20260922T205835Z` (41 posts, live-derived).
Syncs `sync_vendor_gadgets_01_20260924T181425Z` and `...T181627Z`.

| expected | predicted | n |
|---|---|---|
| `caption_edit` | `caption_edit` | 15 |
| `no_op` | `no_op` | 24 |
| `new_post` | `new_post` | 5 |
| `deleted` | `deleted` | 2 |
| `repost_merge` | `repost_merge` | 1 |

**47/47 per-post agreement. No disagreements, and nothing reported that the truth file did not
list.** Change-classification precision is therefore **100%, denominator 23** counting only the
changed posts, or 47 including the no-op denominator. Against the brief's >= 85% target: met, on
this change set.

**The reduction, re-derived on real data.** 20 of 45 posts need Stages 2-5 re-run
(`posts_needing_work()`): the 15 edits and the 5 new posts. The merge, the deletions and every
no-op cost nothing.

| | Full feed | Incremental |
|---|---|---|
| Posts processed | 45 | **20** |
| Reduction | | **56%** |

**This supersedes the 87% below.** That figure came from a golden-derived baseline which
re-discovered posts the feed already had, so it counted work it had invented. 56% is the first
figure measured against a live baseline with the corrected content hash.

**The second sync is the result that had never been demonstrated.** Run immediately after the
first with nothing changed on Instagram, it reported **zero changes, 45 no-ops, zero vendor
actions and zero `stage6_embedding` rows** - against 19 embedding rows for the sync before it.
That is the "a sync on an unchanged account costs about $0" claim, checkable in
`report/token_log.csv` for the first time rather than asserted.

It is also the proof that the three defects fixed on 2026-09-22/23 are dead on live data. Before
them this second sync would have re-reported both deletions, flip-flopped the merge, and
re-reported the caption edit on the superseded post.

**What this run does not establish.**

- **Only `caption_edit` (n=15) clears the codebook's n >= 10 bar.** `repost_merge` is n=1 and
  `deleted` is n=2 - anecdotes, per `eval/LABEL_CODEBOOK.md`'s own rule.
- **`caption_edit`, `no_op` and `deleted` are close to tautological.** Those labels were derived
  by exact string comparison and set arithmetic between the two dumps, and Stage 6 decides them
  the same way, so agreement was near-guaranteed. Deriving them that way is what kept the ground
  truth uncontaminated, but it means the informative results here are the single `repost_merge`
  and the silent second sync, not the 100%.
- **Four change types never fired.** `comment_delta` (comments cannot be added to this account),
  `repost_match` by pHash (no lightly-edited re-upload was posted - the nearest non-exact
  distance was 18), `repost_match` by embedding, and `out_of_stock` (no SOLD repost).
- **pHash only catches literal file reuse.** Of six new posts, one was a re-uploaded saved image
  and matched at distance 0; the other five measured 18-26 against every baseline post *and*
  every carousel child slide. A vendor who reposts a product with a fresh photo is invisible to
  the pHash path.

### The earlier golden-derived run - superseded

A live sync of 40 posts against a 30-post baseline: 18 no-ops, 4 new, 2 caption edits, 1 comment
delta, 4 repost matches (2 pHash, 2 embedding), 2 exact duplicates auto-merged.

| | Full feed | Incremental |
|---|---|---|
| Posts processed | 40 | **6** |
| Model calls | 101 | **13** |
| Tokens | 116,201 | **14,288** |
| Reduction | | **87%** |

Excluding `content_changed` from reprocessing is the load-bearing choice: 10 of 23 changes were
that type (hash moved, caption and comment count did not). Including them would reprocess 16 of
40 posts instead of 6. Three tests pin this.

The 0% attention rate across those 6 posts is a 6-post denominator, not a quality signal.

### What changed on 2026-09-22

Five structural defects, each of which would have silently corrupted a sync measurement:

| | was | now |
|---|---|---|
| Baseline source | built from the golden set, so a sync re-discovered posts the feed already had | `--dump`/`--account` builds it from a live raw dump; `--golden` kept for the original run |
| Content hash | `caption \| comment_count \| len(comments)` | `caption \| comment_count` |
| Comment counts | `comments_count` was never requested at ingest, so a dump-built baseline recorded 0 for every post | requested and threaded through; the builder warns loudly on a dump that predates it |
| Snapshot writes | `latest.json` overwritten in place, so no sync was repeatable | previous trio archived to `data/snapshots/<handle>/history/<sync_timestamp>/` first |
| Embedding calls | neither logged nor throttled; **zero** rows in the token log | `throttle()` + a `stage6_embedding` row per call |

Two more on 2026-09-23, both of the same shape - a state the sync leaves behind that makes the
*next* sync wrong, and both invisible in the single sync every test and every figure had ever
looked at:

| | was | now |
|---|---|---|
| A deleted post | kept as `archived`, but still absent from every later fetch, so it landed in `deleted_ids` again and re-reported **every sync forever**, each time with `requires_vendor_action` | `archived` means "already reported"; the record is carried forward silently |
| An exact repost | dropped the original from the snapshot while it was still live on Instagram, so the next sync saw it as new, matched it at distance 0 and merged back - **flip-flopping forever** | the original is kept as a `superseded` tombstone with no `catalog_products`, so it diffs as the no-op it is |

Both were found by running the same sync three times against a stubbed fetch, which nothing had
done before. A `superseded` or `archived` entry is also excluded from pHash and embedding
matching, so a later repost resolves against the live post rather than a tombstone.

**These two are why the "a sync on an unchanged account costs about $0" claim could not have
held in practice.** Any account that had ever had a deletion or an exact repost reported a
change on every subsequent sync, forever. Six tests in `tests/test_stage6_sync.py` now drive
`run_sync` repeatedly and pin that each event reports exactly once.

`report/changes.json` was a fixed path that collided across vendors and across runs; the default
is now `report/<vendor_handle>/changes_<sync_timestamp>.json`. `run_sync()` takes
`vendor_handle`/`snapshot_dir` as parameters instead of reading module globals, which is what
lets `scripts/simulate_stage6.py` simulate any vendor rather than only the default one.

**Those `content_changed` entries were not CDN churn, and the numbers above predate the fix.**
This section previously attributed them to rotating `oh=`/`oe=` params, which cannot be right -
`compute_content_hash()` has always excluded `media_url` for exactly that reason. The real cause
was the hash's third component, `len(comments)`: the baseline was built from a golden set whose
comments are hand-authored, while a live fetch always returns an empty array (Meta withholds the
text). Same post, different hash, on every sync forever. The component is gone as of
`pipeline/stages/stage6_sync.py`, which **invalidates every `content_hash` written before it** -
the stored snapshots have been rehashed in place and the pre-change copies kept under
`data/snapshots/<handle>/history/pre-hash-change/`. Offline proof, via `make simulate-sync`
against the same baseline before and after: `other_content_changes` 2 -> 0, `no_ops` 33 -> 35,
every other count identical, and the two posts that moved are precisely the two the baseline
records comments for. **The 87% reduction has since been re-derived** on a live baseline with the fixed hash and comes
out at 56% - see the measured run above.

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
| Stage 2 Pass A, Stage 3 text pass, Stage 4 | Stage 2/3 vision Pass B, Stage 2/3 OCR tiers |
| Stage 5 routing | Stage 6 pHash, snapshot build |
| Stage 6 content-hash diff | Stage 6 caption embeddings (key, not images) |

Escalated posts will fail on dead links. That is expected, not a bug.

## Commands

```bash
make ingest ACCOUNT=... TOKEN_ENV=...          # Stage 0 pull
make golden ACCOUNT=...                        # labeling skeleton
make harness GOLDEN=... CONFIG=...             # score stages against gold
make pipeline ACCOUNT=... [LIMIT=N] [LIVE=1]   # chained Stages 1-5
make stage5 GOLDEN=... STAGE2=... ...          # zero-LLM routing
make verify RUN_ID=... [EXPECT=...] [POSTS=N]  # gate a run before quoting it
make snapshot VENDOR=... ACCOUNT=...           # Stage 6 baseline, from the latest dump
make sync VENDOR=... GOLDEN=... TOKEN_ENV=...  # Stage 6 live sync
make simulate-sync VENDOR=... GOLDEN=...       # Stage 6 diff, no Graph fetch, no snapshot writes
make test / check-secrets / install-hooks      # offline checks
make data-pull / data-push / data-status       # DVC <-> Cloudflare R2
```

`LLM_MIN_CALL_INTERVAL` (default 4.5s) spaces provider calls. Gemini's free tier caps at 15
requests/minute, not just a daily quota. Without pacing, a run bursts through it, the 429s are
absorbed by the retry loop, and Stage 3 drops to its regex fallback while still reporting clean
scores. Set to `0` on a paid tier.

`make simulate-sync` fakes only the Graph API media list - the pHash downloads, the Gemini
embeddings and every line of the diff are real, and its embedding calls are logged under a
`simulated_sync_*` run id. It never writes a snapshot.

`make snapshot` builds from the most recent raw dump for `ACCOUNT`, which is what a live sync
re-fetches. `GOLDEN_SNAPSHOT=1` selects the legacy golden-set path instead. `make sync` writes to
`report/<VENDOR>/changes_<sync_timestamp>.json` unless `CHANGES=` overrides it, and archives the
snapshot it diffed against before advancing it.

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
data/snapshots/    Stage 6 baselines + history/ (DVC)
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
- `tests/` is 223 offline tests covering what the harness cannot: credential handling, per-post
  error isolation, the reserved-parameter tripwire, Stage 5 routing, the token-log reader, the
  secret scanner, and the Stage 6 diff. No test may make a network call, read `.env`, or read a
  golden set.
- `tests/test_stage6_sync.py` (34 tests) pins the boundaries where an off-by-one moves a number
  without failing anything: the content hash's inputs, the pHash threshold's strict `<` against
  the embedding threshold's inclusive `>=`, auto-merge at distance 0 only and never from an
  embedding match, and that a dump-built baseline agrees with a live fetch on `comment_count`.
  It also records that the `1e-9` epsilon guarding the cosine division makes that inclusive `>=`
  exclusive in practice at exactly 0.92 - documented rather than "fixed", since the epsilon is
  what keeps an empty caption's zero vector from dividing by zero.

  Six of those drive `run_sync` repeatedly rather than testing one diff, because the two defects
  fixed on 2026-09-23 were both invisible in a single sync and only appeared in the one after it.

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
- **The sync's content hash compared two sources that can never agree.** It included the length
  of the comments array, which Meta always returns empty on a live fetch and which the
  golden-derived baseline filled by hand. Every affected post reported `content_changed` on every
  sync. It stayed hidden because the symptom - a sync finding changes - looks exactly like a sync
  working, and because the README had already explained it away as CDN churn.
- **Stage 6 spent provider quota nothing recorded.** Its caption embeddings never wrote a token
  row or respected the rate limiter, so the one stage claiming to cost nothing was also the one
  stage whose cost no artifact could confirm or refute.

Each was caught by an artifact recording what happened, not what was configured to happen - or,
in the last two cases, by going to look for the artifact and finding it empty.

## Caveats

- **Per-stage accuracy is not system accuracy.** Stages are scored against gold labels, not
  against the previous stage's output. A chained run is measurably lower.
- **Paid pricing is applied to free-tier runs.** A projection, not observed spend.
- **The noise floor is large.** Re-running the same model on the same golden set moved
  per-signal F1 by 20 to 35 points, since most signals have 1 to 3 gold instances. Only
  macro-level gaps are safe to quote. Gadgets has zero gold instances for 3 of 7 signals.
- **One suspected gold-labelling error was never investigated** (`18079164065330268`).
- **Single-vendor model rankings did not survive a second vendor.**
- **Fixed 2026-09-24: only the first product per post used to be scored.** `score_stage3` now
  reports both metrics side by side - see the section below for what the corrected one says.
- **Every Stage 6 figure came off a golden-derived baseline, not a live pull.** The "before"
  state was built from `eval/golden/vendor_autos_01.json`, so a sync re-discovered posts the feed
  already had - four of the reported "new" posts were new only relative to that rebuild. The
  brief's change-classification precision target (>= 85%) has never been produced at all, because
  it needs a hand-labelled two-snapshot pair. `PLAN.md` designs that measurement; the code it
  depends on has landed, the measurement itself has not been run.

## Known limits

- **Comment text is withheld by Meta.** `comments_count` is real, the array is always empty.
  Stage 6 diffs the count, so a `comment_delta` is not evidence Stage 4 read anything.
- **`estimated_cost_usd` is computed from `MODEL_RATES` as of 2026-09-21, but only Jev
  has a rate on file.** Gemini's completion rate ($2.50/M) is recorded above; its prompt
  rate has never been written down anywhere in this repo, and half a rate cannot price a
  row. Unpriced models write `0.0` and log a warning naming the model. Rows written
  before this landed are `0.0` regardless of what they cost and are deliberately not
  recomputed, so `read_run_usage()` reports `priced_calls` beside `calls` - a total whose
  `priced_calls` is short of `calls` understates, and must be quoted that way. The harness's
  own printed "Cost:" line is still `cost_per_call_usd` x call count - a config constant, not
  spend - and every dollar figure in this file was computed by hand from token counts.
- **Stage 6's embedding calls are logged but cannot be priced.** `gemini-embedding-001` rows
  carry 0 tokens, because `embedContent` returns no usage block the way a chat completion does,
  and Google prices embeddings on a separate sheet. The row proves the call happened and nothing
  more, so a Stage 6 cost figure has to come from the call *count* against the published
  embedding rate. What the log does now settle is the shape: a sync on an unchanged account
  writes **zero** `stage6_embedding` rows, which is the first time the "about $0" claim has been
  checkable against the same evidence trail as every other figure here.
- **Carousel per-slide extraction was never built.** Every carousel attaches all slides as one
  gallery, so carousel classification is unmeasured.
- **Nothing runs Stages 1 to 6 end to end on live predictions.** `run_pipeline.py` chains 1 to 5
  and is a driver, not an orchestrator.
- **Two products with the same name at different prices route `auto_import`.** `route_post`
  flags missing price, unknown name and low confidence, and nothing checks for a duplicate name.
  A real gadgets caption lists `Samsung S26 Ultra 8/256GB` twice at N1,300,000 and N1,100,000;
  both gold and the model faithfully record two products with byte-identical attributes, since
  the caption itself never says what differs. Extraction confidence is 1.0 - correctly, because
  it measures fidelity to the caption, not whether the catalog that results is coherent.
- **A caption-less post can only ever be repost-matched by pHash.** An empty caption embeds to a
  zero vector (`media_fingerprint.py`), which matches nothing - good, in that 5 such posts
  cannot false-match each other, but it means the embedding half of Stage 6's cascade is
  structurally unavailable for them. Embedding the OCR text instead would close it and needs its
  own gated run.
- **On a multi-product carousel every slide image attaches to `products[0]`.** True of 19
  multi-product posts in the gadgets golden set, and 7 carousels are classified `multi_product`.
  The slide that would disambiguate two otherwise identical products is therefore attached to
  the wrong one, and per-slide extraction was never built.
- **Change-classification precision was measured on 2026-09-24 and is 100% on 23 changed posts**,
  but three of its five cells are `caption_edit`/`no_op`/`deleted`, which Stage 6 decides by the
  same string comparison the ground truth was derived from. Read the number with that in mind,
  and see the caveats under the measured run.

Research POC, sponsor-owned, published for reference. Not licensed for reuse.
