# Restructure Stage 6 so a real sync can be measured

## Context

Stage 6 is the strongest result in the project and the least examined. It claims an 87% call
reduction on an incremental sync, and the brief's one hard target for it —
**change-classification precision ≥ 85%** — has never been produced, because producing it needs
a hand-labelled two-snapshot pair that does not exist.

The unlock is that **the labels do not have to be guessed.** If the vendor account is changed
deliberately — a caption edited, a post deleted, an image re-uploaded — then the ground truth is
known by construction, exactly and for free. That turns a labelling problem into a scheduling
problem.

Before that can happen, four structural defects have to go. Each one would silently corrupt the
measurement:

1. **The baseline is built from the golden set, not a live pull.** `run_build_snapshot.py:86`
   reads `eval/golden/<account>.json`. This is why the recorded sync "re-discovered" posts that
   were already in the dump — four of its "new" posts were new only relative to that rebuild.

2. **A content-hash mismatch fires spurious `content_changed` on every live sync.**
   `stage6_sync.py:19` hashes `caption | comment_count | len(comments)`. The golden-derived
   baseline has hand-authored comments, so the third component is non-zero for 11 autos posts. A
   live fetch always returns an **empty** comments array, so it is always 0. Every one of those
   posts hashes differently and reports as changed. `simulate_stage6.py:55-68` documents this
   for the simulator; nobody noticed it fires for real.

3. **A sync destroys its own "before".** `_advance_snapshot()` overwrites `latest.json`,
   `embeddings.npy` and `embedding_index.json` in place (`run_stage6.py:589-593`). Nothing is
   repeatable and no pair can be re-scored.

4. **Stage 6's provider calls are invisible and unpaced.** `pipeline/media_fingerprint.py` calls
   `gemini-embedding-001` but never calls `log_token_usage()` or `throttle()`. There are **zero**
   embedding rows in `report/token_log.csv`. So Stage 6's real cost has never been recorded, the
   "sync on an unchanged account costs ≈ $0" claim cannot be checked against the log the way
   every other claim in this project is, and a sync bursts through Gemini's 15 rpm ceiling —
   the exact thing `CLAUDE.md` says every new call site must respect.

**Intended outcome:** a gated, repeatable sync measurement on a live account with known labels,
and a precision figure against the ≥ 85% target stated with its denominator.

## Scope

- **Part 1** — the code changes that make a trustworthy measurement possible.
- **Part 2** — the designed experiment, including what to post on Instagram and when.
- **Part 3** — retire the contaminated autos snapshot.

**Out of scope, deliberately:** the missing `low_stock` and `stale` lifecycle states, and
sweeping the pHash / embedding / keyword thresholds. Both are real gaps; both are better done
*after* this experiment, because it is what produces the labelled pairs a sweep needs. **Stage 4
is parked** — signals cannot be honestly tested while every comment is injected.

---

## Part 1 — Code

### 1.1 Build the baseline from a live dump

`scripts/run_build_snapshot.py` — add `--dump PATH` (and `--account`) to build from
`runs/<account_label>/raw/dump_*.json`. **Reuse `latest_dump()` and `normalize_post()` from
`scripts/run_pipeline.py`** rather than writing a second reader. Keep `--golden` working so the
existing path is not broken, but make the dump path what the runbook uses.

A live-derived entry has no gold to draw on, so `post_type`, `catalog_products` and
`carousel_classification` are set the way `_advance_snapshot()` already sets them for new posts
(`run_stage6.py:272-276`) — `None`, `[]`, `None`. That is consistent, not a regression.

### 1.2 Fix the content hash — the load-bearing change

`pipeline/stages/stage6_sync.py:19`. Drop the `len(post.get("comments", []))` component. It is
redundant with `comment_count` when both come from the same source and actively wrong when they
do not, which is every live sync. Hash `caption | comment_count`.

**This invalidates every stored `content_hash`**, so both baselines must be rebuilt — which this
plan does anyway. Record that in the docstring so a future reader knows the hashes are not
comparable across this change.

### 1.3 Keep every snapshot version

In `_advance_snapshot()`, before writing (`run_stage6.py:589-593`), copy the current trio to
`data/snapshots/<vendor_handle>/history/<sync_timestamp>/`. Small change, and it is what makes
the experiment re-scorable and a snapshot-to-snapshot diff possible later.

### 1.4 Log and throttle the embedding calls

`pipeline/media_fingerprint.py::compute_caption_embedding()` — call `throttle()` before the
request and `log_token_usage()` after, with `stage="stage6_embedding"`. Mirror the pattern the
vision Pass B calls use: they bypass `complete_structured()` and therefore do their own logging
and pacing. Token counts come from the embedding response where available; log the call with
zero tokens rather than not at all if the response carries none — **the row's existence is the
point**, since it is what makes a sync's cost visible and what lets the ≈ $0 claim be checked.

Also add `gemini-embedding-001` to the `MODEL_RATES` gap comment in `pipeline/llm_client.py`, as
another model with no rate on file.

### 1.5 Per-vendor changes path

`--changes` defaults to a fixed `report/changes.json` (`run_stage6.py:643`), which collides
across vendors — `CLAUDE.md` already flags it. Default to
`report/<vendor_handle>/changes_<sync_timestamp>.json`.

### 1.6 Give the simulator a CLI

`scripts/simulate_stage6.py` has no argparse and imports `VENDOR_HANDLE` / `SNAPSHOT_DIR` as
module globals from `run_stage6`, so it can only ever simulate the default vendor. Add
`--vendor-handle`, `--golden`, `--changes`. Cheap, and it is the only offline exercise Stage 6
has.

### 1.7 First tests for `stage6_sync` — `tests/test_stage6_sync.py`

Nothing in `tests/` touches any function in `stage6_sync.py` today. Offline, deterministic, no
network. Cover the behaviour that is easy to break and currently unguarded:

- `compute_content_hash`: identical caption + count hash equal **regardless of the comments
  array** — the regression test for 1.2;
- `phash_hamming_distance`: the `999` sentinel when either side is `None` (`:27`);
- `find_phash_match`: strict `<` threshold boundary — distance exactly 12 must **not** match;
- `find_embedding_match`: inclusive `>=` boundary — cosine exactly 0.92 **must** match;
- auto-merge only at distance 0 (`AUTO_MERGE_PHASH_MAX_DISTANCE`), and never from an embedding
  match (`run_stage6.py:489`);
- `compute_lifecycle_state`: `"sold"` from the vendor handle → `out_of_stock`; the same word
  from a buyer → `active`;
- `diff_posts`: the `no_op` fast path, and the `"content_hash changed but caption/comment_count
  did not move"` fallback branch.

### 1.8 Note the API version skew

`ingest/ingest.py` is on `v26.0`; `run_stage6.py:74` and `refresh_media_urls.py` are on `v21.0`.
Align Stage 6 to `v26.0` only after confirming `comments_count` and `children{}` still resolve —
if anything differs, leave it and record why.

---

## Part 2 — The designed experiment

Runs on **`vendor_gadgets_01` / `oluwadunnioluajayi`**, which has no snapshot yet, so the
baseline is clean from the first pull.

**Prerequisite: every `IG_ACCESS_TOKEN*` in `.env` is currently empty.** A live pull raises
`MissingCredentialsError` immediately. Refresh the token before Phase A. Tokens last ~60 days.

### Phase A — one post, before the baseline (you)

Post **D**, a disposable, clearly marked test post: an image with a caption like
`TEST POST — pipeline sync test, please ignore`. It exists only to be deleted in Phase C, so the
never-executed `deleted → archived` path costs nothing real.

### Phase B — take the baseline (me)

`refresh_media_urls.py --dump …` → `make ingest` → build the snapshot **from that dump**.
Record post count and timestamp. This is T0 and it is now archived under `history/`.

### Phase C — the changes (you)

Designed so every change type fires and each label is known. **Record the post id or permalink
and the time for each action** — that is the ground truth file.

| # | action | expected `change_type` | why it's in the matrix |
|---|---|---|---|
| 1 | Edit a caption — change the **price** | `caption_edit` | should reach Stage 3 and produce a price change |
| 2 | Edit a caption — cosmetic only (emoji/hashtag) | `caption_edit` | does a trivial edit cost a full reprocess? |
| 3 | Comment once on a post, **from a different account** | `comment_delta` | the half-signal: count moves, text withheld |
| 4 | Comment twice on another post | `comment_delta` | a delta of 2 |
| 5 | **Delete post D** | `deleted` → `archived` | never executed in any run |
| 6 | New post: product, price **in the caption** | `new_post` | the ordinary path |
| 7 | New post: product, **no caption**, price on the image | `new_post` | drives the new Stage 3 OCR tier end to end |
| 8 | New post: announcement, no product | `new_post` | should route `auto_exclude` |
| 9 | Re-upload the **exact same image file** as an existing post, new caption | `repost_merge` | pHash distance 0 → auto-merge |
| 10 | Re-upload a **lightly edited** image (crop ~5%, or a filter) of an existing post | `repost_match` (phash) | distance 1–12: match but must **not** auto-merge |
| 11 | New post, **different image**, caption near-identical to an existing one | `repost_match` (embedding) | cosine ≥ 0.92 |
| 12 | Re-upload an existing product image captioned `SOLD` | `repost_match` + `out_of_stock` | the keyword-gated 16 / 0.90 thresholds |
| 13 | Leave everything else untouched | `no_op` | the denominator, and the 87% claim |

**Practical constraints, all of which will corrupt the run if missed:**

- Comments must come from an account **other than** `oluwadunnioluajayi`, or `_label_comments()`
  tags them `[VENDOR]` and the meaning inverts.
- **Do not archive or hide posts.** Deletion is detected only as an absence from `/me/media`
  (`run_stage6.py:350-352`), so archiving is indistinguishable from deleting and would poison the
  one label we are trying to establish.
- Give Instagram a few minutes after the last change before the sync.
- Items 9, 10 and 12 need the original image files, so keep them to hand.

Ground truth goes in `eval/sync_labels/<vendor_handle>_<date>.json`, shaped so scoring is a join
on `post_id` against `changes.json`.

### Phase D — sync and score (me)

`refresh_media_urls` → `make sync` → score with a new `scripts/score_sync.py`: per-change-type
precision and recall, a confusion matrix, and the headline precision against the ≥ 85% target
**stated with its denominator**. Also read the sync's cost out of `report/token_log.csv`, which
it will appear in for the first time (1.4).

---

## Part 3 — Retire the autos snapshot

`data/snapshots/ayodele.akinbohun/` is golden-derived and carries the old hash. Archive it to
`history/pre-live-rebaseline/`, then rebuild from a live dump with the fixed hash, so future
autos syncs are trustworthy. **No designed experiment there** — it is not an account to post to,
so it gets a clean baseline and nothing more.

---

## Verification

1. **Offline first** — `make test`. `tests/test_stage6_sync.py` and the existing
   `tests/test_run_pipeline.py` handoff tests must pass with no network.
2. **`make simulate-sync` before and after the hash change**, diffing
   `report/changes.simulated.json`. **The spurious `content_changed` entries should disappear and
   nothing else should move.** This is the cheapest possible proof that 1.2 fixed what it claims
   and broke nothing.
3. **Baseline sanity** — snapshot entry count equals the live dump's post count; `embeddings.npy`
   is `(n, 768)`; `embedding_index.json` has `n` rows.
4. **The scored experiment** — precision against ≥ 85%, per change type, with denominators. A
   single cell with n ≤ 2 is an anecdote and must be reported as one, per the codebook's own rule.
5. **Cost** — `report/token_log.csv` must now contain `stage6_embedding` rows. A sync on an
   unchanged account must produce **zero** of them, which finally makes the ≈ $0 claim checkable.
6. **The 87% reduction, re-derived** on real data rather than a golden-set rebuild, via
   `posts_needing_work()`.
7. Only then update `README.md`, and mark every figure with the run it came from.
