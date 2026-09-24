# Phase C worksheet — oluwadunnioluajayi, baseline 2026-09-22

Baseline: **41 posts**, snapshot run `snapshot_vendor_gadgets_01_20260922T205835Z`, dump pulled `2026-09-22T20:56:11.261708+00:00`.

Tick each action off **and write the time you did it**. This file is the ground truth —
scoring is a join on `post_id` against the sync's `changes.json`, so an action you did but
did not record counts against us as a false positive.

## Rules that will silently ruin the run

- **Delete, never archive.** Deletion is detected only as an absence from `/me/media`, so an
  archived post is indistinguishable from a deleted one.
- **Do not touch a post marked `leave untouched`.** They are the `no_op` denominator and the
  whole call-reduction claim rests on them.
- **Keep the original image files** for the repost rows in the second table.
- Anything that changes that you did not do (an organic comment, say) → write it in the
  Notes column as `INVESTIGATE`, do not score it.

## Part 1 — changes to existing posts

| # | action | what to change | post_id | type | caption now | done? | time |
|---|---|---|---|---|---|---|---|
| 1 | **EDIT — cosmetic only** | add a speaker emoji after the title line | [18103239884214338](https://www.instagram.com/p/Dc6mglhiHoH/) | IMAGE | JBL SUPER BASS SPEAKERS •Boombox 4……….N640,000 •Extreme 4……. | ☐ | |
| 2 | **EDIT — cosmetic only** | append the hashtags #jbl #lagos on a new line | [17944115412074853](https://www.instagram.com/reel/Dc6nD6ThLmU/) | REEL | PRICE LIST FOR JBL SPEAKERS Boombox 4……….N640,000 Xtreme 5…… | ☐ | |
| 3 | **EDIT — cosmetic only** | append the hashtag #starlink on a new line | [18094596503428757](https://www.instagram.com/p/Dc6n7RSiNGu/) | IMAGE | INTRODUCING THE LATEST STARLINK STANDARD X •5G super-fast sp | ☐ | |
| 4 | **EDIT — cosmetic only** | add a box emoji right after RESTOCKED | [18093257891291658](https://www.instagram.com/p/Dc6rt3sCIzy/) | IMAGE | RESTOCKED PREMIUM USED 🇬🇧✅ SE 2ND Gen 128GB…N157,000 SE 2ND  | ☐ | |
| 5 | **EDIT — cosmetic only** | add a fourth fire emoji - a one-character edit | [18130873003584392](https://www.instagram.com/p/Dc6sI3NCNJO/) | IMAGE | AWOOF DEAL ON 🔥🔥🔥 •OPEN BOX •iPhone 12PRO 128GB •Physical &  | ☐ | |
| 6 | **EDIT — change the price** | move the FIRST price meaningfully, not by 1 naira | [18051806288800495](https://www.instagram.com/p/Dc6VYBJCDUw/) | IMAGE | •SWEET DEAL 😋😋😘 •APPLE iPad 11TH Gen (2025) •Battery Health- | ☐ | |
| 7 | **EDIT — change the price** | move the FIRST price meaningfully, not by 1 naira | [18173692510448068](https://www.instagram.com/p/Dc6VscFiE3B/) | IMAGE | JUST IN BRAND NEW SONY PlayStation PS5 SLIM 1TB………N990,000 P | ☐ | |
| 8 | **EDIT — change the price** | move the FIRST price meaningfully, not by 1 naira | [18091786415437652](https://www.instagram.com/p/Dc6V_XniDZf/) | IMAGE | IPHONE 💥💥💥💥 NEW ACTIVE 17AIR…..256GB BLACK …..N1,080,000 OTH | ☐ | |
| 9 | **EDIT — change the price** | move the FIRST price meaningfully, not by 1 naira | [18027958613896333](https://www.instagram.com/p/Dc6XdSgCMhT/) | CAROUSEL_ALBUM | INTRODUCING THE LATEST FOLD •Galaxy Z-Fold 8 •8/256GB (Dual  | ☐ | |
| 10 | **EDIT — change the price** | move the FIRST price meaningfully, not by 1 naira | [18056670212554989](https://www.instagram.com/p/Dc6X2lXCCAm/) | IMAGE | ZENTALITY Super Fast Adapter..N7,000 POWER BANK •50,000mah.. | ☐ | |
| 11 | **DELETED — done 2026-09-23** | already deleted - nothing to do | [17983552128060866](https://www.instagram.com/reel/Dc-HDRFpYhM/) | REEL | Implementing these new rules ASAPPPPP💯 Here are some updates | ☐ | |
| 12 | **DELETED — done 2026-09-23** | already deleted - nothing to do | [18149430643539602](https://www.instagram.com/reel/Dc-_7PjBEZh/) | REEL | 📢 PUBLIC HOLIDAY NOTICE We will be CLOSED this Monday for th | ☐ | |

> **Rows 11-12 are already done** - both deleted on 2026-09-23. They were announcements (a
> stale public-holiday notice and an iPhone-18 rumour post), chosen over the script's original
> suggestions, which were live iPhone listings it had no way to value.

For **EDIT — change the price**, move the number meaningfully (not by ₦1); that edit should
reach Stage 3 and produce a real price change. For **EDIT — cosmetic only**, add or remove an
emoji or a hashtag and change nothing else — the question is whether a trivial edit costs a
full reprocess.

## Part 2 — posts to create

These cannot be assigned an id in advance. Record the id or permalink as you go.

| # | what to post | expected | source post_id | new post_id | done? | time |
|---|---|---|---|---|---|---|
| 13 | Product, price **in the caption** | `new_post` | — | | ☐ | |
| 14 | Product, **no caption**, price on the image | `new_post`, drives the Stage 3 OCR tier | — | | ☐ | |
| 15 | Product, price in caption, different category | `new_post` | — | | ☐ | |
| 16 | Announcement / non-product, no price | `new_post` → should route `auto_exclude` | — | | ☐ | |
| 17 | Re-upload the **exact same image file**, new caption | `repost_merge`, distance 0, auto-merges | `18127850242812986` iPhone 12 ProMax | | ☐ | |
| 18 | Re-upload the **exact same image file**, new caption | `repost_merge` | `18136320136714592` AWOOF iPhone 17 ProMax | | ☐ | |
| 19 | Re-upload **cropped ~5%** | `repost_match` pHash, must **not** auto-merge | `17986005777060517` AirPods Max | | ☐ | |
| 20 | Re-upload **with a filter applied** | `repost_match` pHash | `18351809149247806` the caption-less post | | ☐ | |
| 21 | **Different** image, caption copied and one word changed | `repost_match` embedding, cosine >= 0.92 | `17956459401229902` iPhone 11 | | ☐ | |
| 22 | Re-upload the image captioned `SOLD` | `repost_merge` + `out_of_stock`, vendor action still required | `18024559289696773` iPhone Ultra Case | | ☐ | |

> **Row 22 produces `repost_merge`, not `repost_match`.** An exact re-upload is pHash distance
> 0, which trips the auto-merge rule before the keyword band is ever consulted. It still proposes
> `out_of_stock` and still requires your confirmation, which is the branch worth testing: the
> merge self-applies, the stock transition does not. The keyword-widened 12-16 band cannot be hit
> deliberately by hand and this row never tested it.
>
> Note also that `18024559289696773` was relabeled `announcement` on 2026-09-23, so it carries no
> catalog products - the merge will inherit an empty catalog entry. Still a valid `repost_merge`
> label for scoring; just do not read the empty products as a defect.

> **You do not need the original image files.** Downloading a post's image from Instagram
> and re-uploading it still pHashes at **distance 0** - measured at JPEG quality 95/80/65 and
> after a resize to 1080px wide, on three of your own posts. Instagram's own re-encoding does
> not break the auto-merge.

### Why these sources

Every source is an untouched post, so nothing carries two labels. Rows 17-20 and 22 need an
`IMAGE` post because a carousel hashes only its cover and a Reel its thumbnail; row 21 reuses
a caption rather than an image, so a carousel is fine there.

Rows 17–22 are scored against the post you took the image or caption from, so the **source
post_id** column is not optional — without it there is nothing to check the match against.
Pick `IMAGE` posts as sources, not carousels or Reels: a carousel hashes its cover image only
and a Reel hashes its thumbnail, which makes an exact re-upload harder to reason about.

Use sources from Part 3 (untouched posts) where you can. Taking the image from a post you also
edited in Part 1 gives that post two labels at once and muddies both.

## Part 3 — the 29 untouched posts

Do not touch these. Listed so you can check nothing drifted.

| post_id | type | caption |
|---|---|---|
| [17983272389889086](https://www.instagram.com/p/Dc7OSaYCOGa/) | IMAGE | Premium Uk used iPhone XR📱🔥 iPhone XR 64GB – ₦205,000 iPhone |
| [18114879538989256](https://www.instagram.com/p/Dc7OzuuiCru/) | IMAGE | Premium Uk used iPhone 17 📱🔥 256GB Price: NGN1,020,000 LEKKI |
| [18018200018873070](https://www.instagram.com/p/Dc7PUU6CN0r/) | CAROUSEL_ALBUM | Uk used Dell Latitude 5300 2-in-1 💻🔥 Core i5 | 8th Gen | 8GB |
| [17868280992643734](https://www.instagram.com/p/Dc7Q85-CPlx/) | CAROUSEL_ALBUM | MacBook Pro M1 chip 13’inch 2020 Touch Bar 512GB/16GB NGN950 |
| [18351809149247806](https://www.instagram.com/p/Dc7RIwrCJl1/) | IMAGE | (no caption) |
| [18136320136714592](https://www.instagram.com/p/Dc7R2xViAp6/) | IMAGE | •AWOOF DEAL ON 🔥❤️ •OPEN BOX •iPhone 17PROMAX 256GB •Physica |
| [17953469082214541](https://www.instagram.com/p/Dc7SQ1HiOE2/) | CAROUSEL_ALBUM | Premium Uk used iPhone 14 📱🔥 iPhone 14 128GB (Physical + eSI |
| [17958045885195473](https://www.instagram.com/p/Dc7S1X7iFnN/) | CAROUSEL_ALBUM | 🥳Open box - Google pixel 9 pro 256GB – ₦790,000 LEKKI 📍 PRIM |
| [17919224106434401](https://www.instagram.com/p/Dc7TfxliHoG/) | IMAGE | No explanations. Just pick ONE. 👀👇 Which Samsung are you tak |
| [17956459401229902](https://www.instagram.com/p/Dc7UKPHCC-C/) | CAROUSEL_ALBUM | Premium UK used iPhone 11 📱🔥 iPhone 11 64GB – ₦225,000 iPhon |
| [18099765671288015](https://www.instagram.com/p/Dc7U52ZiNxD/) | CAROUSEL_ALBUM | UK-used MacBook Air M2 2022 🔥💻 256/8GB NGN950,000 LEKKI 📍 PR |
| [18190139290395806](https://www.instagram.com/p/Dc8r7BaCD2g/) | IMAGE | A new month, a new beginning… and hopefully, a new iPhone 18 |
| [17940067572092567](https://www.instagram.com/reel/Dc8s2LZzvUb/) | REEL | (no caption) |
| [18457546087186979](https://www.instagram.com/reel/Dc8zFm_ut5R/) | REEL | (no caption) |
| [18127850242812986](https://www.instagram.com/p/Dc8zsUmCFse/) | IMAGE | Premium quality Uk used iPhone 12 ProMax 🔥📱 * iPhone 12 Pro  |
| [18163097968466814](https://www.instagram.com/p/Dc80RLIiEJO/) | IMAGE | Phone 18 Ultra (The first Foldable iPhone) | iPhone 18ProMax |
| [18007098038770815](https://www.instagram.com/p/Dc806HrCHWg/) | IMAGE | 📢 ANNOUNCING GBANJO THURSDAYS! 📢 Get ready for massive price |
| [18085616054264180](https://www.instagram.com/reel/Dc82ZfJNAqF/) | REEL | (no caption) |
| [17875545636631606](https://www.instagram.com/reel/Dc829nLy-Mi/) | REEL | (no caption) |
| [17909013375530552](https://www.instagram.com/p/Dc83qT8CGTw/) | CAROUSEL_ALBUM | Premium Uk used iPhone 15 Plus 📱🔥 iPhone 15 Plus 128GB (Phys |
| [17966735571148449](https://www.instagram.com/p/Dc9lL96iPGY/) | CAROUSEL_ALBUM | Open box - Uk used iPhone 14 ProMax 🔥📱 iPhone 14 Pro Max 128 |
| [18117403781512663](https://www.instagram.com/reel/Dc-H54NP4f8/) | REEL | ❌❌SOLD❌❌🚨ONE LUCKY BUYER🚨 Uk Used iPhone 16 Plus ESIM UNLOCK |
| [17908533072539933](https://www.instagram.com/reel/Dc-Iim5N_OO/) | REEL | 🚨❌❌SOLD❌❌ONE LUCKY BUYER🚨 Uk Used iPhone 17 Pro Max ESIM UNL |
| [18047192480810719](https://www.instagram.com/reel/Dc-K0DYxyW6/) | REEL | Premium Uk used iPhone 12 📱🔥 iPhone 12 64GB iPhone 12 128GB  |
| [18024559289696773](https://www.instagram.com/p/Dc-3NB0CJMN/) | IMAGE | iPhone Ultra Protective Case available soon ( Phone not incl |
| [17868259755637335](https://www.instagram.com/p/Dc_AehEiPdp/) | IMAGE | WE HAVE EXPANDED! 🎉 Our Lekki branch has moved to a bigger s |
| [17994209417829051](https://www.instagram.com/reel/Dc_A8t5Nu60/) | REEL | Another happy customer ❤️ Thank you for trusting us with you |
| [17986005777060517](https://www.instagram.com/p/Dc_i92aiK35/) | IMAGE | 🚨ONE LUCKY BUYER🚨 Neatly Used AirPods Max 🔥 Price: NGN240,00 |
| [18115331426067887](https://www.instagram.com/reel/Dc_j37RMCf8/) | REEL | Take it home today with just ₦100,000 deposit! Brand new Sam |

## Part 4 - after you are done

Tell me and I run **two** syncs back to back.

Sync 1 scores everything above. Sync 2 changes nothing on Instagram and must come back
completely silent - zero changes, every post a no-op, zero embedding calls. That is the
test of the claim the whole stage rests on, and it costs you no posting effort.

Two defects found on 2026-09-23 would each have broken sync 2 before they were fixed: a
deleted post re-reported its deletion on every later sync, and an exact repost flip-flopped
with the post it merged into, forever. Both now report once and then go quiet.

One baseline post already carries a comment: `18457546087186979` (`comment_count = 1`). If that comment is removed before the
sync, expect a `content_changed` reporting a decrease — real, and not a defect.
