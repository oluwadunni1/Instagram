# Test content plan — vendor_gadgets_01

Authoring sheet for making the gadgets/electronics account exercise every pipeline path.
Derived from the coverage audit of the 2026-09-06 dump (30 posts).

**Sequence matters — do it in this order:**

1. Author everything in Priority 1-3 below (new posts + caption edits on existing posts).
2. `make ingest ACCOUNT=vendor_gadgets_01 TOKEN_ENV=IG_ACCESS_TOKEN_GADGETS` — fresh dump.
3. `make golden ACCOUNT=vendor_gadgets_01`, hand-label, hand-author comments.
4. `make harness` / `make stage5`.
5. `make snapshot` — freezes the Stage 6 baseline.
6. **Only then** do Priority 4 (reposts / caption edits / deletion) — those are only
   detectable as changes *against* an existing snapshot.
7. `make sync`.

**Do not bother posting real comments.** The Graph API returns zero comments for this app
(Advanced Access gate — see `FINDINGS.md` 2026-09-04), so any comment-dependent case has to
be hand-authored into the golden set regardless. Put every signal you want tested **in the
caption**, or accept that it only exists in the hand-authored golden data.

---

## Priority 1 — Stage 2 post-type classes (biggest gap)

Stage 2 classifies into 5 classes and is currently scored on ~1. Two posts each. The image
matters less than the caption here — a screenshot, a plain graphic, or a phone photo is fine.

### `announcement` (2 posts)

```
📢 PUBLIC HOLIDAY NOTICE

We will be CLOSED this Monday for the public holiday. Orders placed over the weekend will be
delivered Tuesday.

Thanks for your patience 🙏
LEKKI — PRIMO ELECTRONICS, SUITE A14, LEKKI PHASE 1, LAGOS
```

```
WE HAVE EXPANDED! 🎉

Our Lekki branch has moved to a bigger space — SUITE A14, Lekki Phase 1.
Same team, more stock, easier parking.

Come through 👋 //WhatsApp 08065405439
```

### `testimonial_repost` (2 posts)

```
Another happy customer ❤️ Thank you for trusting us with your upgrade 🙏

Your satisfaction is why we do this.
#customerreview #primoelectronics
```

```
Customer feedback 💬

"Got my iPhone 13 Pro yesterday, battery health exactly as described. Very legit seller, I go
come back." — thank you sir 🙏
```

### `meme_personal` (2 posts)

```
POV: your battery health hits 79% and the phone starts behaving 💀😂

Tag someone who needs an upgrade.
```

```
Monday mood at the shop 😂 Nobody warned us about month-end rush.
```

### `ad_creative` (2 posts)

Deliberately promotional with **no specific product or price** — this is what separates it
from `product_listing`.

```
END OF YEAR SALE IS LOADING 🎉🔥

Big discounts across phones, laptops, speakers and gaming.
Starts Friday. Follow so you don't miss it 👀
```

```
PRIMO ELECTRONICS 📱💻🎧

Lagos' trusted gadget plug. UK used • Open box • Brand new.
Nationwide delivery. //WhatsApp 08065405439
```

---

## Priority 2 — Stage 4 signals (6 of 7 uncovered)

One post each. Each caption must contain the evidence quote verbatim — Stage 4 requires a
direct textual quote and will not infer.

| Signal | Caption |
|---|---|
| `sold` | `SOLD OUT ❌`<br><br>`The iPhone 14 Pro Max 256GB has been sold. Thank you!`<br><br>`More units landing next week — stay tuned.` |
| `urgent` + `stock_count_known` | `⚠️ ONLY 2 UNITS LEFT`<br><br>`iPhone 15 Pro 256GB (Physical + eSIM) — ₦985,000`<br>`Battery Health: 96%`<br><br>`First come, first served.` |
| `price_negotiable` | `Premium UK used iPhone 13 128GB`<br><br>`₦420,000 — price is slightly negotiable for serious buyers.`<br><br>`LEKKI — PRIMO ELECTRONICS //WhatsApp 08065405439` |
| `finance_available` | `BUY NOW, PAY LATER 💳`<br><br>`MacBook Air M2 13inch 256GB — ₦780,000`<br><br>`We work with two partner banks. Pay 40% deposit and spread the balance over 3 months.` |
| `swap_deal` | `SWAP DEAL ACCEPTED 🔄`<br><br>`Bring your iPhone 12 Pro 128GB + ₦250,000 balance and go home with an iPhone 14 Pro 256GB.`<br><br>`Swap valuation done in store.` |
| `clearance` | `CLEARANCE SALE 🔥`<br><br>`Samsung S23 Ultra 256GB`<br>`Was ₦720,000 — NOW ₦595,000`<br><br>`Stock must go before month end.` |

---

## Priority 3 — Stage 3 extraction edge cases

These target the "never invent a price" hard requirement (brief §11) and the distractor rules
already written into `stage3_extract.py`'s `SYSTEM_PROMPT`.

**DM-for-price (no number anywhere)** — exercises `DM_FOR_PRICE_RE`, the skip-escalation path,
and Stage 5's missing-price flag. Gold `price.source` must be `"none"`.
```
Brand new MacBook Air M3 13inch (2025)
8GB/256GB • Sealed in box

💰 PRICE: 📲 Send a DM / WhatsApp for the price! Serious buyers only.

LEKKI — PRIMO ELECTRONICS //WhatsApp 08065405439
```

**Price range** — `PriceModel` has a single `value`, so this is a known limitation worth
measuring. Golden set has `price.min`/`price.max` extension fields for it.
```
Samsung A-Series available in stock 📱

Price range: ₦180,000 – ₦260,000 depending on model and storage.
Walk in to see all options.
```

**Comparison-price distractor** — the ₦1,700,000 must NOT be extracted as this product's price.
```
Why pay ₦1,700,000 for the iPhone 17 Pro Max when this gives you 90% of it?

iPhone 16 Pro Max 256GB (Physical + eSIM)
Battery Health: 98%
₦1,150,000 only 🔥
```

**Financing-deposit distractor** — the ₦150,000 deposit must NOT become the price.
```
Take it home today with just ₦150,000 deposit!

PlayStation 5 Slim 1TB — full price ₦990,000
Balance spread over 2 months.
```

**Pidgin / mixed language** — feeds Stage 1's `language_mix` and reflects the real market.
```
This one na correct gadget 🔥

iPhone 13 Pro 256GB — ₦520,000
E dey work well well, no wahala. Battery health 91%.

Abeg come collect am before e finish.
```

### Editing the 9 caption-less posts

**Keep at least 4 without captions** — ideally the Reels. Empty captions force Stage 2 into
vision Pass B via `_is_uninformative_caption()`, which is exactly the path the `thumbnail_url`
fix made work, and it's currently your only coverage of it. Losing it would be a downgrade.

For the other ~5, add captions from Priority 2/3 above rather than writing generic ones — that
closes two gaps with one edit.

Also check whether any caption-less post has the **price written on the image**. If so, that's
your `price.source: "image"` case (Stage 3 Pass B recovering a price from the photo) — label it
as such and don't add a caption price.

---

## Priority 4 — Stage 6 sync (AFTER `make snapshot`)

These are only meaningful as diffs against a frozen baseline. Note the post IDs you touch.

| Case | Action | What it exercises |
|---|---|---|
| Exact repost | Re-upload the **identical image file** of an existing post, any caption | pHash distance 0 → `repost_merge`, `auto_merged: true`, no vendor prompt |
| SOLD repost | Re-upload an existing product's image **with a "SOLD" overlay** + rewritten caption containing "sold" | Keyword-gated loosened thresholds (pHash 16 / embedding 0.90) → `repost_match` proposing `out_of_stock` |
| Caption edit | Edit one existing post's price (e.g. ₦435,000 → ₦399,000) | `caption_edit`, `needs_stage3_rerun: true` |
| Deletion | Delete one post | `deleted` → `proposed_transition: archived`, never silent-deleted |

Re-run `make sync` after these and check `report/changes_gadgets.json`.

---

## Coverage checklist

- [ ] Stage 2: 2× announcement, 2× testimonial_repost, 2× meme_personal, 2× ad_creative
- [ ] Stage 4: sold, urgent, stock_count_known, price_negotiable, finance_available, swap_deal, clearance
- [ ] Stage 3: DM-for-price, price range, comparison distractor, deposit distractor, Pidgin
- [ ] ≥4 caption-less posts retained (prefer Reels) for forced vision escalation
- [ ] `price.source: "image"` case identified and labeled
- [ ] Stage 6: exact repost, SOLD repost, caption edit, deletion — all after `make snapshot`
