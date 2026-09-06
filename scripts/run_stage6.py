#!/usr/bin/env python3
"""
Stage 6 - Sync, Step 2: pulls fresh posts for the connected vendor from the
Instagram Graph API and diffs them against the Step 1 snapshot
(data/snapshots/<vendor-handle>/). Writes report/changes.json.

Usage:
    uv run scripts/run_stage6.py
    uv run scripts/run_stage6.py --vendor-handle gadget.hub.ng \\
        --golden eval/golden/vendor_gadgets_01.json \\
        --changes report/changes_gadgets.json

Defaults reproduce the original ayodele.akinbohun / vendor_autos_01 run.
--vendor-handle must match the actual Instagram username the connected
IG_ACCESS_TOKEN belongs to (there's no way to query an arbitrary handle
directly, only the account the token is connected to - see
ingest/ingest.py) and is used to detect the vendor's own comment replies
(e.g. "sold") - it is unrelated to the local account_label folder name
ingest.py lets you pick freely.
Run order: scripts/run_build_snapshot.py must have been run first for the
same --vendor-handle.
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.exceptions import MissingCredentialsError  # noqa: E402
from pipeline.logging_config import configure_logging  # noqa: E402
from pipeline.media_fingerprint import compute_caption_embedding, compute_image_phash  # noqa: E402
from pipeline.settings import get_ig_access_token  # noqa: E402
from pipeline.stages.stage6_sync import (  # noqa: E402
    compute_content_hash,
    compute_lifecycle_state,
    diff_posts,
    find_embedding_match,
    find_phash_match,
)
from pipeline.types import vision_image_url  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_VENDOR_HANDLE = "ayodele.akinbohun"
DEFAULT_GOLDEN_PATH = Path("eval/golden/vendor_autos_01.json")
DEFAULT_CHANGES_PATH = Path("report/changes.json")

# Reassigned from CLI args in main() via `global` before run_sync()/
# _advance_snapshot() run - both read these by module-global lookup rather
# than taking them as parameters, so this needs to happen before either is
# called, not just at import time. GOLDEN_PATH stays a plain alias (never
# reassigned) purely so scripts/simulate_stage6.py's existing
# `from scripts.run_stage6 import GOLDEN_PATH, ...` keeps working unchanged -
# that script always simulates against the default vendor, independent of
# whatever --vendor-handle this file's own main() is invoked with.
VENDOR_HANDLE = DEFAULT_VENDOR_HANDLE
SNAPSHOT_DIR = Path("data/snapshots") / VENDOR_HANDLE
GOLDEN_PATH = DEFAULT_GOLDEN_PATH

GRAPH_BASE = "https://graph.instagram.com"
API_VERSION = "v21.0"
MEDIA_FIELDS = (
    # thumbnail_url/media_product_type: only populated for media_type=VIDEO
    # (Reels included) - media_url there is the video file, not an image.
    # See pipeline/types.py::vision_image_url() for which field downstream
    # vision calls/pHash actually use.
    "id,caption,media_type,media_url,thumbnail_url,media_product_type,"
    "permalink,children{id,media_url,thumbnail_url},"
    "timestamp,comments_count,comments{id,username,text,timestamp}"
)
PAGE_SLEEP_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 15

# Default (strict) thresholds for an ordinary new post - loosening these
# globally caught the intended heavily-edited "SOLD" reposts but also pulled
# in unrelated posts that just share boilerplate listing language (price,
# location, contact info, hashtags), landing in the 0.85-0.92 cosine band
# with no lifecycle keyword anywhere in the caption. Gating the loosened
# thresholds on keyword presence catches the same reposts without that
# false-positive cost: an ordinary new post is still held to 12/0.92, and
# only a caption that says "sold"/"delivered"/"handed over" - independent
# evidence the vendor is talking about an existing item, not this post - is
# allowed to search the catalog under wider thresholds.
#
# These were tuned empirically against the automotive vendor's own
# false-positive patterns (car listings sharing boilerplate price/location/
# hashtag language). A different vertical's boilerplate may not collide the
# same way - worth a quick sanity check against a new vendor's false-positive
# rate before trusting these thresholds unchanged, though the keywords
# themselves ("sold"/"delivered"/"handed over") are generic enough to likely
# transfer as-is.
DEFAULT_PHASH_THRESHOLD = 12
DEFAULT_EMBEDDING_THRESHOLD = 0.92
KEYWORD_PHASH_THRESHOLD = 16
KEYWORD_EMBEDDING_THRESHOLD = 0.90
REPOST_KEYWORDS = ["sold", "delivered", "handed over"]

# "Merging an exact repost of an already-imported item" is the brief's one
# named no-brainer that self-applies without asking. Taken literally: only a
# bit-identical pHash counts as "exact" - an embedding match is a semantic
# *similarity* score, never identity, so it can never qualify here regardless
# of how high the score is. Everything above this distance (including the
# heavily-edited "SOLD" reposts this session added keyword-gated matching
# for) is a real edit a vendor should see, not a no-brainer merge.
AUTO_MERGE_PHASH_MAX_DISTANCE = 0


def _fetch_all_media(token: str) -> list[dict]:
    """Paginates /me/media for the connected account (the same "Instagram
    Login" pattern ingest.py uses - there is no way to query an arbitrary
    handle directly, only the account the token is connected to)."""
    url = f"{GRAPH_BASE}/{API_VERSION}/me/media"
    params = {"fields": MEDIA_FIELDS, "limit": 50, "access_token": token}
    all_media: list[dict] = []
    while url:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        all_media.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")
        params = {}  # next_url already carries all query params
        if url:
            time.sleep(PAGE_SLEEP_SECONDS)
    return all_media


def _normalize_fresh_post(raw: dict) -> dict:
    """Reshapes a raw Graph API media object into the same field names
    used by the golden-set-derived snapshot, so compute_content_hash()/
    diff_posts() can compare the two directly."""
    children = (raw.get("children") or {}).get("data", [])
    comments = (raw.get("comments") or {}).get("data", [])
    return {
        "post_id": raw["id"],
        "caption": raw.get("caption") or "",
        "media_url": raw.get("media_url"),
        "thumbnail_url": raw.get("thumbnail_url"),
        "permalink": raw.get("permalink"),
        "media_type": raw.get("media_type"),
        "timestamp": raw.get("timestamp"),
        "comment_count": raw.get("comments_count", 0),
        "comments": comments,
        "has_carousel_children": bool(children),
        "children": children,
    }


def _comments_after_cursor(comments: list[dict], cursor: str | None) -> list[dict]:
    """Comments strictly after the one with id == cursor - the per-post
    high-water mark recorded at snapshot time. No cursor means every
    current comment is new (first time this post has ever had one)."""
    if cursor is None:
        return comments
    for idx, comment in enumerate(comments):
        if comment.get("id") == cursor:
            return comments[idx + 1:]
    logger.warning(
        "comment_cursor %r not found in fresh comment list - returning full list as a best effort", cursor
    )
    return comments


def _matched_keyword(caption: str | None) -> str | None:
    caption_lower = (caption or "").lower()
    return next((kw for kw in REPOST_KEYWORDS if kw in caption_lower), None)


def _advance_snapshot(
    prev_index: dict,
    old_embedding_by_id: dict,
    fresh_by_id: dict,
    no_op_ids: list[str],
    changed_common_ids: list[str],
    new_own_ids: set,
    phash_by_new_id: dict,
    embedding_by_new_id: dict,
    merged_pairs: list[tuple[str, str]],
    deleted_ids: set,
) -> tuple[list[dict], list[list[float]]]:
    """Builds the snapshot that becomes the "previous run" for the *next*
    sync - without this, latest.json is never rewritten and every future
    sync re-diffs against the same stale baseline forever. Reuses whatever
    was already computed during this run (no-op posts keep their old
    pHash/embedding untouched; a caption-only edit keeps its old pHash since
    the image on an existing IG post can't change) rather than recomputing
    everything, in the same spirit as "diff before AI" for the sync itself.
    """
    entries: dict[str, dict] = {}
    embeddings: dict[str, list[float]] = {}

    # 1. No-ops: carry forward verbatim, zero recomputation.
    for pid in no_op_ids:
        entries[pid] = prev_index[pid]
        embeddings[pid] = old_embedding_by_id[pid]

    # 2. Same post_id, something changed (caption_edit / comment_delta /
    #    content_changed): media_url can't change on an existing IG post, so
    #    pHash is reused; embedding is only refreshed if the caption itself
    #    moved (mirrors the "caption changed -> re-run" cost model).
    for pid in changed_common_ids:
        old_entry = prev_index[pid]
        fresh = fresh_by_id[pid]
        if fresh.get("caption") != old_entry.get("caption"):
            embedding, _ = compute_caption_embedding(fresh.get("caption"), post_id=pid)
        else:
            embedding = old_embedding_by_id[pid]
        comments = fresh.get("comments") or []
        entries[pid] = {
            "post_id": pid,
            "caption": fresh.get("caption"),
            "comment_count": fresh.get("comment_count"),
            "media_url": fresh.get("media_url"),
            "content_hash": compute_content_hash(fresh),
            "image_phash": old_entry.get("image_phash"),
            "comment_cursor": comments[-1]["id"] if comments else old_entry.get("comment_cursor"),
            "lifecycle_state": compute_lifecycle_state(old_entry.get("post_type"), comments, VENDOR_HANDLE),
            "catalog_products": old_entry.get("catalog_products", []),
            "post_type": old_entry.get("post_type"),
            "carousel_classification": old_entry.get("carousel_classification"),
        }
        embeddings[pid] = embedding

    # 3. Deleted: never silently dropped from the record, marked archived -
    #    nothing else can be refreshed since the live post is gone.
    for pid in deleted_ids:
        archived_entry = dict(prev_index[pid])
        archived_entry["lifecycle_state"] = "archived"
        entries[pid] = archived_entry
        embeddings[pid] = old_embedding_by_id[pid]

    # 4. Genuinely-new posts and non-exact repost matches (not auto-merged):
    #    tracked as their own catalog entries going forward. post_type stays
    #    None - Stage 2 hasn't classified them in this sync pass.
    for pid in new_own_ids:
        fresh = fresh_by_id[pid]
        embedding = embedding_by_new_id.get(pid)
        if embedding is None:
            embedding, _ = compute_caption_embedding(fresh.get("caption"), post_id=pid)
        comments = fresh.get("comments") or []
        entries[pid] = {
            "post_id": pid,
            "caption": fresh.get("caption"),
            "comment_count": fresh.get("comment_count"),
            "media_url": fresh.get("media_url"),
            "content_hash": compute_content_hash(fresh),
            "image_phash": phash_by_new_id.get(pid),
            "comment_cursor": comments[-1]["id"] if comments else None,
            "lifecycle_state": None,
            "catalog_products": [],
            "post_type": None,
            "carousel_classification": None,
        }
        embeddings[pid] = embedding

    # 5. Auto-merged exact reposts: same catalog product, so the new post_id
    #    supersedes the old one entirely - never carry both (never duplicate).
    for new_pid, old_pid in merged_pairs:
        old_entry = prev_index[old_pid]
        fresh = fresh_by_id[new_pid]
        entries.pop(old_pid, None)
        embeddings.pop(old_pid, None)
        comments = fresh.get("comments") or []
        entries[new_pid] = {
            "post_id": new_pid,
            "caption": fresh.get("caption"),
            "comment_count": fresh.get("comment_count"),
            "media_url": fresh.get("media_url"),
            "content_hash": compute_content_hash(fresh),
            "image_phash": phash_by_new_id.get(new_pid),
            "comment_cursor": comments[-1]["id"] if comments else None,
            "lifecycle_state": compute_lifecycle_state(old_entry.get("post_type"), comments, VENDOR_HANDLE),
            "catalog_products": old_entry.get("catalog_products", []),
            "post_type": old_entry.get("post_type"),
            "carousel_classification": old_entry.get("carousel_classification"),
        }
        # Same product identity as the post it replaces - keep its caption
        # embedding rather than re-embedding (no new semantic content to add).
        embeddings[new_pid] = old_embedding_by_id[old_pid]

    ordered_ids = sorted(entries)
    return [entries[pid] for pid in ordered_ids], [embeddings[pid] for pid in ordered_ids]


def run_sync(
    raw_posts: list[dict],
    snapshot: list[dict],
    emb_matrix: np.ndarray,
    post_ids_by_row: list[str],
    golden_by_id: dict,
    changes_path: Path,
    persist_snapshot: bool = True,
) -> dict:
    """Everything after "we have a list of fresh posts, however obtained":
    diffs them against `snapshot`, writes `changes_path`, and (unless
    `persist_snapshot` is False) advances the on-disk snapshot. Factored out
    of main() so scripts/simulate_stage6.py can drive the exact same diff/
    matching/reporting logic with synthetic `raw_posts` instead of a live
    Graph API fetch - no duplicated logic to drift between the two.

    Returns the same dict written to `changes_path`.
    """
    prev_index = {entry["post_id"]: entry for entry in snapshot}
    prev_ids = set(prev_index)
    old_embedding_by_id = {pid: emb_matrix[i] for i, pid in enumerate(post_ids_by_row)}

    fresh_by_id = {p["post_id"]: p for p in (_normalize_fresh_post(r) for r in raw_posts)}
    fresh_ids = set(fresh_by_id)

    new_ids = fresh_ids - prev_ids
    deleted_ids = prev_ids - fresh_ids
    common_ids = fresh_ids & prev_ids

    changes: list[dict] = []
    no_op_ids: list[str] = []
    changed_common_ids: list[str] = []
    caption_edit_count = 0
    comment_delta_count = 0
    other_content_change_count = 0
    repost_phash_count = 0
    repost_embedding_count = 0
    repost_merge_count = 0

    for post_id in sorted(deleted_ids):
        changes.append({
            "change_type": "deleted",
            "post_id": post_id,
            "proposed_transition": "archived",
            "requires_vendor_action": True,
            "evidence": "Post present in previous snapshot but missing from the fresh Instagram fetch",
        })

    for post_id in sorted(common_ids):
        fresh = fresh_by_id[post_id]
        result = diff_posts(fresh, prev_index[post_id])

        if result["change_type"] == "no_op":
            no_op_ids.append(post_id)
            continue

        changed_common_ids.append(post_id)
        flagged = False

        if result.get("caption_changed"):
            old_caption = result.get("old_caption") or ""
            new_caption = result.get("new_caption") or ""
            changes.append({
                "change_type": "caption_edit",
                "post_id": post_id,
                "old_caption": result.get("old_caption"),
                "new_caption": result.get("new_caption"),
                "needs_stage3_rerun": True,
                "proposed_transition": "needs_attention",
                "evidence": f"Caption changed. Diff: {old_caption[:60]!r} -> {new_caption[:60]!r}",
            })
            caption_edit_count += 1
            flagged = True

        if result.get("comment_count_increased"):
            cursor = prev_index[post_id].get("comment_cursor")
            new_comments = _comments_after_cursor(fresh.get("comments", []), cursor)
            changes.append({
                "change_type": "comment_delta",
                "post_id": post_id,
                "delta_count": result["comment_delta"],
                "new_comments": [
                    {"id": c.get("id"), "username": c.get("username"), "text": c.get("text")}
                    for c in new_comments
                ],
                "needs_stage4_rerun": True,
                "proposed_transition": "needs_attention",
                "evidence": f"{result['comment_delta']} new comment(s) since cursor {cursor!r}",
            })
            comment_delta_count += 1
            flagged = True

        if not flagged:
            # content_hash changed but neither tracked field increased (e.g. a
            # comment was deleted, dropping comment_count) - still a real
            # change and must be surfaced, not silently folded into no_ops.
            changes.append({
                "change_type": "content_changed",
                "post_id": post_id,
                "proposed_transition": "needs_attention",
                "requires_vendor_action": False,
                "evidence": result.get("evidence", "content_hash changed but caption/comment_count did not increase"),
            })
            other_content_change_count += 1

    phash_by_new_id: dict[str, str | None] = {}
    embedding_by_new_id: dict[str, list] = {}
    merged_pairs: list[tuple[str, str]] = []

    for post_id in sorted(new_ids):
        fresh = fresh_by_id[post_id]
        caption = fresh.get("caption")
        inspect = {
            "caption": caption,
            "media_url": fresh.get("media_url"),
            "permalink": fresh.get("permalink"),
        }

        # A lifecycle keyword in the caption is independent evidence this
        # post is about an existing item ("sold"/"delivered"/"handed over"),
        # not a fresh listing - so for keyword-bearing captions only, search
        # the catalog under wider thresholds to survive a heavily re-edited
        # repost photo/caption. Everything else stays at the strict default.
        keyword = _matched_keyword(caption)
        phash_threshold = KEYWORD_PHASH_THRESHOLD if keyword else DEFAULT_PHASH_THRESHOLD
        embedding_threshold = KEYWORD_EMBEDDING_THRESHOLD if keyword else DEFAULT_EMBEDDING_THRESHOLD

        phash, _phash_fail_reason = compute_image_phash(vision_image_url(fresh), post_id=post_id)
        phash_by_new_id[post_id] = phash
        phash_match = find_phash_match(phash, snapshot, threshold=phash_threshold) if phash else None

        embedding_match = None
        if phash_match is None:
            embedding, _ = compute_caption_embedding(caption, post_id=post_id)
            embedding_by_new_id[post_id] = embedding
            embedding_match = find_embedding_match(
                embedding, emb_matrix, post_ids_by_row, threshold=embedding_threshold
            )

        if phash_match is None and embedding_match is None:
            changes.append({
                "change_type": "new_post",
                "post_id": post_id,
                "proposed_transition": "needs_import",
                "requires_vendor_action": True,
                "evidence": "New media ID, no pHash or embedding match in catalog",
                **inspect,
            })
            continue

        proposed_transition = "out_of_stock" if keyword else "needs_attention"
        keyword_note = (
            f" New post caption contains '{keyword}'."
            if keyword
            else " No lifecycle keyword found in new caption - flagged for vendor review."
        )
        matched_post_id = phash_match["post_id"] if phash_match is not None else embedding_match["post_id"]
        matched_golden = golden_by_id.get(matched_post_id, {})
        matched_inspect = {
            "matched_caption": prev_index.get(matched_post_id, {}).get("caption"),
            "matched_media_url": prev_index.get(matched_post_id, {}).get("media_url"),
            "matched_permalink": matched_golden.get("permalink"),
        }

        is_exact_duplicate = phash_match is not None and phash_match["distance"] <= AUTO_MERGE_PHASH_MAX_DISTANCE

        if is_exact_duplicate:
            # The one named no-brainer: merging an exact repost self-applies,
            # no vendor action needed for the merge itself. The *transition*
            # is a separate decision - an out-of-stock transition still asks
            # per policy ("out-of-stock transitions ... prompts the vendor"),
            # even when the repost identity behind it was auto-merged.
            merged_pairs.append((post_id, matched_post_id))
            transition_note = (
                " Out-of-stock transition still requires vendor confirmation per policy."
                if keyword
                else " Treated as a refresh/restock signal, no vendor action needed."
            )
            changes.append({
                "change_type": "repost_merge",
                "post_id": post_id,
                "matched_post_id": matched_post_id,
                "match_method": "phash",
                "hamming_distance": phash_match["distance"],
                "auto_merged": True,
                "proposed_transition": "out_of_stock" if keyword else "active",
                "requires_vendor_action": bool(keyword),
                "evidence": (
                    f"Exact image match (pHash distance={phash_match['distance']}) to catalog post "
                    f"{matched_post_id} - auto-merged as a repost, no new product created."
                    + keyword_note + transition_note
                ),
                **inspect,
                **matched_inspect,
            })
            repost_merge_count += 1
        elif phash_match is not None:
            changes.append({
                "change_type": "repost_match",
                "post_id": post_id,
                "matched_post_id": matched_post_id,
                "match_method": "phash",
                "hamming_distance": phash_match["distance"],
                "proposed_transition": proposed_transition,
                "requires_vendor_action": True,
                "evidence": (
                    f"Image is visually similar to catalog post "
                    f"(pHash distance={phash_match['distance']})." + keyword_note
                ),
                **inspect,
                **matched_inspect,
            })
            repost_phash_count += 1
        else:
            changes.append({
                "change_type": "repost_match",
                "post_id": post_id,
                "matched_post_id": matched_post_id,
                "match_method": "embedding",
                "similarity_score": round(embedding_match["score"], 4),
                "proposed_transition": proposed_transition,
                "requires_vendor_action": True,
                "evidence": (
                    f"Caption is semantically similar to catalog post "
                    f"(embedding score={embedding_match['score']:.2f})." + keyword_note
                ),
                **inspect,
                **matched_inspect,
            })
            repost_embedding_count += 1

    merged_new_ids = {new_pid for new_pid, _ in merged_pairs}
    new_own_ids = new_ids - merged_new_ids

    new_post_count = sum(1 for c in changes if c["change_type"] == "new_post")
    repost_match_count = repost_phash_count + repost_embedding_count
    vendor_action_required = sum(1 for c in changes if c.get("requires_vendor_action"))

    summary = {
        "total_fresh": len(fresh_ids),
        "total_prev": len(prev_ids),
        "no_ops": len(no_op_ids),
        "new_posts": new_post_count,
        "deleted_posts": len(deleted_ids),
        "caption_edits": caption_edit_count,
        "comment_deltas": comment_delta_count,
        "other_content_changes": other_content_change_count,
        "repost_matches": repost_match_count,
        "repost_merges": repost_merge_count,
    }

    if persist_snapshot:
        next_entries, next_embeddings = _advance_snapshot(
            prev_index=prev_index,
            old_embedding_by_id=old_embedding_by_id,
            fresh_by_id=fresh_by_id,
            no_op_ids=no_op_ids,
            changed_common_ids=changed_common_ids,
            new_own_ids=new_own_ids,
            phash_by_new_id=phash_by_new_id,
            embedding_by_new_id=embedding_by_new_id,
            merged_pairs=merged_pairs,
            deleted_ids=deleted_ids,
        )
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        (SNAPSHOT_DIR / "latest.json").write_text(json.dumps(next_entries, indent=2, ensure_ascii=False), encoding="utf-8")
        np.save(SNAPSHOT_DIR / "embeddings.npy", np.array(next_embeddings, dtype=np.float64))
        next_embedding_index = {str(i): entry["post_id"] for i, entry in enumerate(next_entries)}
        (SNAPSHOT_DIR / "embedding_index.json").write_text(json.dumps(next_embedding_index, indent=2), encoding="utf-8")

    output = {
        "sync_timestamp": datetime.now(timezone.utc).isoformat(),
        "vendor": VENDOR_HANDLE,
        "summary": summary,
        "changes": changes,
        "no_op_post_ids": sorted(no_op_ids),
    }

    changes_path.parent.mkdir(exist_ok=True)
    changes_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    bar = "─" * 33
    print(f"Sync complete — {VENDOR_HANDLE}")
    print(bar)
    print(f"Fresh posts:     {summary['total_fresh']}")
    print(f"Previous posts:  {summary['total_prev']}")
    print(bar)
    print(f"No-ops:          {summary['no_ops']}  (0 AI calls)")
    print(f"New posts:       {summary['new_posts']}  → needs import")
    print(f"Deleted:         {summary['deleted_posts']}")
    print(f"Caption edits:   {summary['caption_edits']}  → needs Stage 3 re-run")
    print(f"Comment deltas:  {summary['comment_deltas']}  → needs Stage 4 on delta")
    if other_content_change_count:
        print(f"Other changes:   {other_content_change_count}  → content_hash changed, no caption/comment increase")
    print(f"Repost matches:  {summary['repost_matches']}  ({repost_phash_count} pHash, {repost_embedding_count} embedding)")
    if repost_merge_count:
        print(f"Repost merges:   {repost_merge_count}  (exact duplicate, auto-applied)")
    print(bar)
    print(f"Vendor action required: {vendor_action_required} items")
    print(f"Written → {changes_path}")
    if persist_snapshot:
        print(f"Snapshot advanced → {SNAPSHOT_DIR / 'latest.json'}")
    else:
        print("Snapshot NOT written (persist_snapshot=False) - real baseline untouched")

    return output


def main() -> None:
    configure_logging()
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to cp1252, which can't render ─/—

    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor-handle", default=DEFAULT_VENDOR_HANDLE,
                         help="Instagram username the connected IG_ACCESS_TOKEN belongs to "
                              "(default: ayodele.akinbohun)")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN_PATH,
                         help="Golden-set file, only used for matched_permalink display "
                              "(default: eval/golden/vendor_autos_01.json)")
    parser.add_argument("--changes", type=Path, default=DEFAULT_CHANGES_PATH,
                         help="Where to write the sync's changes JSON (default: report/changes.json - "
                              "pass a distinct path per vendor if you want to keep multiple around)")
    parser.add_argument("--token-env", default="IG_ACCESS_TOKEN",
                         help="Env var holding this account's access token (default: IG_ACCESS_TOKEN) - "
                              "use a different name to sync a second connected account without "
                              "overwriting the first one's token in .env")
    args = parser.parse_args()

    global VENDOR_HANDLE, SNAPSHOT_DIR
    VENDOR_HANDLE = args.vendor_handle
    SNAPSHOT_DIR = Path("data/snapshots") / VENDOR_HANDLE

    token = get_ig_access_token(args.token_env)
    if not token:
        raise MissingCredentialsError(f"Set {args.token_env} in your environment first.")

    snapshot = json.loads((SNAPSHOT_DIR / "latest.json").read_text(encoding="utf-8"))
    emb_matrix = np.load(SNAPSHOT_DIR / "embeddings.npy")
    embedding_index = json.loads((SNAPSHOT_DIR / "embedding_index.json").read_text(encoding="utf-8"))
    post_ids_by_row = [embedding_index[str(i)] for i in range(len(embedding_index))]

    # Snapshot entries don't carry permalink (added after the snapshot schema
    # was fixed) - pull it from the golden set they were built from, purely
    # for the "matched_permalink" inspection field below (never used in
    # diffing/matching logic).
    golden_by_id = {}
    if args.golden.exists():
        golden_by_id = {p["post_id"]: p for p in json.loads(args.golden.read_text(encoding="utf-8"))}

    logger.info("Fetching fresh media list for %s...", VENDOR_HANDLE)
    raw_posts = _fetch_all_media(token)

    run_sync(raw_posts, snapshot, emb_matrix, post_ids_by_row, golden_by_id, args.changes, persist_snapshot=True)


if __name__ == "__main__":
    try:
        main()
    except MissingCredentialsError as exc:
        sys.exit(str(exc))
