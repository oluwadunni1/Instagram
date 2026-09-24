# Label codebook

The conventions behind the golden set: the five `post_type` values, and how a post's products are
split into rows.

**Why this file exists.** Until now the five values were declared in two places and defined in
neither: the FIELD GUIDE in `eval/make_golden_skeleton.py` lists the names, and
`pipeline/stages/stage2_triage.py` lists them again in its enum and both system prompts. Nothing
said what any of them meant. A model was therefore asked to reproduce a boundary that was never
written down, and a human labeling a new vendor had nothing to be consistent against.

**How it was derived.** Not invented. Reconstructed from the per-post `notes` the labeler left
across the 71 labeled posts in `eval/golden/`, which record the reasoning case by case
("Clearance sale promotional post", "Delivery/Sold out post, no active product listed"). Where
the existing labels are inconsistent, this file says so rather than smoothing it over. The gold
labels are the authority; this document describes them.

---

## The decision procedure

Apply in order. The first match wins.

1. **Is a specific product being offered for sale, right now?** Yes, then `product_listing`.
2. **Is a completed sale itself the subject of the post** - a delivery, a handover, or a
   customer's own words? Yes, then `testimonial_repost`. Ask what the post is *about*, not
   whether a sale happened: a SOLD mark on a post whose subject is still an item, with its
   specs and its price, does not move it out of step 1.
3. **Is it the business informing customers** about hours, location, policy, availability, or
   the mechanics of a promotion? Yes, then `announcement`.
4. **Is it a produced engagement device built around products**, such as a poll or a "pick one"?
   Yes, then `ad_creative`.
5. **Otherwise**, personal or lifestyle content in a conversational voice: `meme_personal`.

### The rule that matters most

**"Promotional" does not mean `ad_creative`.** This is the single largest source of error. A
clearance sale, a financing offer and a weekly discount day are all promotional, and all three
are labeled `announcement`, because none of them offers a specific purchasable item. `ad_creative`
is a narrow residual category, not a bucket for marketing energy.

### The second rule that matters

**Listing-shaped is not the same as purchasable.** A post can name three models with specs and
still be an `announcement` if the items are not actually for sale yet.

---

## `product_listing`

**Test:** a specific item a buyer could ask to purchase today.

Includes pre-orders when a real offer is attached, and multi-item posts where each item is a
genuine offer. A price is not required: "DM for price" is still an offer.

**Deferred availability with no offer attached is an `announcement`, not a listing.** "Available
soon", "coming soon" and "expected September" all describe something a buyer cannot ask to
purchase today, which is what the test above asks. The dividing line is whether an offer is
attached, not whether the words sound promotional: "Pre-order yours today" is an offer and stays
a `product_listing`; "available soon" on its own, with no price and no call to action, is the
business telling customers what is coming - step 3.

A SOLD mark does **not** move a post out of this class, and that is a different question
entirely: a completed sale is a fact about an item that exists, whereas deferred availability
means the item is not purchasable yet. Both prompts stated these as one rule until 2026-09-23
and contradicted themselves, which is what `18024559289696773` exposed - see below.

| Example | Why |
|---|---|
| `18075852833375244` | "2024 Toyota Hilux Adventure" with condition, engine, trim. A single unambiguous offer. |
| `18042748280816067` | "Pre-order yours today" across three vehicles. Note: the labeler recorded that the Leopard 8 named in the hook is a **negative distractor** and must not be extracted as a product. |
| `18120996466885495` | A two-vehicle comparison, still a real offer of both. Boundary case: see below. |

---

## `announcement`

**Test:** the business is telling customers something. No specific item is being offered.

This is the largest non-product class and absorbs most promotional content.

| Example | Why |
|---|---|
| `17868259755637335` | Branch relocation. "No product offered." |
| `18149430643539602` | Public holiday closure notice. |
| `17894529186657159` | **Clearance sale.** Promotional, still `announcement`: no specific unit is listed. |
| `18331493713257979` | **Financing offer** with partner banks. Promotional, still `announcement`. |
| `17983552128060866` | Commentary on an unreleased iPhone. Nothing is for sale. |
| `18024559289696773` | **"iPhone Ultra Protective Case available soon ( Phone not included )".** Relabeled from `product_listing` on 2026-09-23. No price, no call to action - a heads-up, not an offer. Gold carries no products, matching `18163097968466814` below. See the note in README.md on what this cost. |
| `18163097968466814` | **The documented trap.** Names three iPhone 18 models and reads exactly like a listing, but they are unreleased ("Expected September 9"). Gold carries no products. |
| `17897690802571800` | "Happy new month to all our clients worldwide." Institutional voice, addressed to clients. |

---

## `testimonial_repost`

**Test:** a completed sale is the *subject* of the post, or someone else's words are being
reshared.

The product is usually visible and often named. It is still not a listing, because the post is
about the handover rather than about the item.

**This is a subject test, not a sale-status test.** A post that is shaped like a listing - the
item, its specs, its price - stays a `product_listing` even when it also carries a SOLD mark or
an available-soon date. There the sale status is an attribute of the listing, not what the post
is about. See the 2026-09-21 decision below.

| Example | Why |
|---|---|
| `18114987011511783` | "KEYS HANDED OVER." Delivery celebration. "No active product listed." |
| `18173859304438540` | "ANOTHER KEY HANDED OVER." Labeler adds "Delivery/Sold out post." |

**Most common error:** reading the celebratory tone as advertising and choosing `ad_creative`,
or reading the named vehicle as an offer and choosing `product_listing`. The handover is the
subject; the car is context.

---

## `meme_personal`

**Test:** personal or lifestyle content in a conversational voice. Nothing is being sold and no
business information is conveyed.

| Example | Why |
|---|---|
| `18190139290395806` | "A new month, a new beginning... and hopefully, a new iPhone 18!" A greeting with a playful product aside and an engagement question. |

**Boundary with `announcement`:** both of the greeting posts in the golden set are new-month
greetings, and they carry different labels. See the boundary section below.

---

## `ad_creative`

**Test:** a produced engagement device built around products. A poll, a "pick one", a campaign
unit whose purpose is interaction rather than an offer.

| Example | Why |
|---|---|
| `17919224106434401` | "No explanations. Just pick ONE. Which Samsung are you taking home?" Products appear only as poll options. |

**This is the rarest and most over-predicted class.** It is n=1 in the entire golden set, and the
one example is itself flagged ambiguous by the labeler. Choosing it should feel like a last
resort. If the post is promotional but not a poll, it is almost certainly `announcement`.

---

## Known boundary cases and the noise floor

**Six of 71 posts (8.5%)** carry an ambiguity or trap marker in their notes. No model should be
expected to score above that boundary, and a disagreement on one of these posts is weak evidence
of anything.

**Documented by the labeler:**

- `17919224106434401`, gold `ad_creative`: "AMBIGUOUS - engagement/poll post. No price, no
  specific offer. **meme_personal is defensible.**"
- `18007098038770815`, gold `announcement`: announces a recurring weekly discount day.
  "**ad_creative is defensible.** Explicit price-drop language makes clearance a genuine signal
  even though no product is listed."
- `18163097968466814`, gold `announcement`: flagged "TRAP" for reading listing-shaped while
  nothing is purchasable.

**Found while writing this file, not previously recorded:**

- `17897690802571800` (gold `announcement`) and `18190139290395806` (gold `meme_personal`) are
  **both new-month greetings with different labels.** The distinguishing feature appears to be
  voice: the first is institutional and addressed to "our clients worldwide"; the second is
  conversational, emoji-led, and closes on an engagement question. That is a thin distinction
  and it is applied here as observed, not endorsed. Treat disagreement between these two as
  inside the noise.

- `18120996466885495` (gold `product_listing`) is a "THIS OR THAT?" comparison of two SUVs,
  which is structurally close to the `ad_creative` poll above. Gold treats it as a listing,
  presumably because both vehicles are genuinely for sale with a stated budget. Jev split
  0.510 / 0.490 on exactly this post.

---

### Changed 2026-09-21: the SOLD-with-price clause

This file's stated position is that the gold labels are the authority and this document
describes them. **This one clause inverts that, deliberately, and says so here rather than
smoothing it over.**

As written before this date, step 2 asked whether a sale had happened. That filed a
SOLD-with-price listing and an "available soon" listing as `testimonial_repost`, which writes a
Stage 4 concern (*is this still available?*) into a Stage 2 category (*is this a product post?*).

What forced the change: all three remaining misses of the Jev+Gemini hybrid on
`vendor_gadgets_01` were posts of exactly this shape, and on one of them the escalation to
Gemini **agreed with Jev against the gold label**. Two models, given the same written
definitions, disagreeing with the label in the same direction is evidence about the definition.

The underlying inconsistency is the one recorded below: autos files a delivery/handover post as
`testimonial_repost`, gadgets files a SOLD-with-price listing as `product_listing`. Both are
defensible and no single rule satisfies both, so the codebook was always picking a side - it
previously picked the autos convention without recording that it was a choice. The subject test
picks the gadgets convention and keeps the autos handover posts, because in those the handover
genuinely *is* the subject.

**Caveat, and it is a real one: the posts that motivated this are in the TEST half.** The change
was made with knowledge of the test set, so any improvement it produces on `vendor_gadgets_01`
is not a clean held-out result and must not be quoted as one.

Changing this re-baselines every Stage 2 figure. The wording is carried in
`pipeline/stages/stage2_triage.py`'s `POST_TYPE_DEFINITIONS` and in `scripts/smoke_jev.py`'s
`criteria` rubrics; all three were changed together.

---

## Product granularity: one row per purchasable variant

**Test:** one product row per thing a buyer could ask for by name and be quoted a price for.

A listing offering an iPhone 12 in 64GB, 128GB and 256GB is **three rows**, not one row with a
storage variant - even when no price is given for any of them. What makes them separate is that
they are separately purchasable, not that they are separately priced.

| Example | Gold | Why |
|---|---|---|
| `18091786415437652` | 4 rows | iPhone 17 Air and 17 Pro Max, each in its own colour band, four distinct prices. |
| `18115331426067887` | 2 rows | Galaxy Tab S10 FE 128GB (N680,000) and 256GB (N900,000). |
| `18047192480810719` | 3 rows | iPhone 12 in 64/128/256GB, **every price null** ("DM for price"). Still three rows. |
| `18027958613896333` | 3 rows | Two Samsung S26 Ultra rows at different prices whose captions are otherwise identical. The caption never says what differs, and neither gold nor any model invents a distinguishing attribute - see the note below. |
| `17966735571148449` | 3 rows | iPhone 14 Pro Max 128/256/512GB, all prices null. |

**`variants` is for options that do NOT create a row.** A colour choice offered at one price for
one item ("available in Red and White, N450,000") is one row with a colour variant. The moment the
options carry their own prices, or are listed as separate things a buyer picks between, they are
rows.

**Why this is written down now.** It never was. The Stage 3 prompt said the opposite - "only
return multiple products when the post is genuinely offering different items for sale (different
make/model, different trim, or explicitly different price points)" - and told the model to put
options in a `variants` array. The model followed it exactly, so on four gadgets posts it returned
one row where gold had two, three, three and five. That looked like under-extraction in the
scores and was a spec nobody had recorded, the same failure as the `post_type` values above: a
boundary defined in a labeler's head and contradicted in a prompt. Recorded 2026-09-24.

**A known limit this exposes.** When two rows are genuinely indistinguishable from the caption -
the two S26 Ultras - nothing downstream flags it. `stage5_reconcile.route_post()` checks missing
price, unknown name and low confidence, so two rows with the same name at different prices route
`auto_import` and a buyer sees one of them at random. Extraction confidence is 1.0 and correctly
so: it measures fidelity to the caption, not whether the resulting catalog is coherent.

## Class balance, and what it means for any number quoted

Across both golden sets:

| class | n | share |
|---|---|---|
| product_listing | 59 | 83% |
| announcement | 8 | 11% |
| testimonial_repost | 2 | 3% |
| meme_personal | 1 | 1% |
| ad_creative | 1 | 1% |

Three of five classes have n <= 2. **Overall accuracy on this distribution is close to the
majority-class base rate** and says almost nothing about the four minority classes. Always report
per class with denominators, and treat any single minority cell as anecdote until n >= 10.

---

## Using this file

- **Labeling:** work the decision procedure in order and record reasoning in `notes`, including
  the label you rejected when it was close. Those notes are what made this file possible.
- **Prompts:** `pipeline/stages/stage2_triage.py` carries these definitions in both system
  prompts. Changing them re-baselines every Stage 2 number, so record the change and re-run
  rather than comparing across the boundary.
- **Jev criteria:** `scripts/smoke_jev.py` builds its `criteria` rubrics from this file. Jev
  requires a rubric per option, which the Gemini prompt historically did not have, so keep the
  two wordings aligned or the comparison stops being like for like.
- **Product granularity:** `pipeline/stages/stage3_extract.py` carries the one-row-per-variant
  rule in its system prompt. Changing it re-baselines every Stage 3 extraction number, so record
  the change and re-run both vendors rather than comparing across the boundary.
