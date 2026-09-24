#!/usr/bin/env python3
"""
Offline Stage 6 simulator.

Exercises every diff path Stage 6 knows about - no-op, caption edit,
comment delta, deleted post, brand-new post, exact-duplicate repost
(auto-merge), and a keyword-gated "SOLD" repost (ask) - in one run, against
your REAL current snapshot, without touching Instagram or the live account.

What's real vs. fake:
  - FAKE: the Graph API media-list fetch (this is the part that depends on
    unpredictable live vendor activity, so it's the part synthesized here).
  - REAL: pHash downloads, Gemini caption embeddings, and every line of the
    actual diff/matching/reporting logic in scripts/run_stage6.run_sync() -
    reused directly, not reimplemented, so there is nothing to drift.

Never writes a real changes file or the real snapshot files - output goes to
report/changes.simulated.json and persist_snapshot=False, so re-running this
is always safe and repeatable against the same baseline.

Usage:
    uv run scripts/simulate_stage6.py
    uv run scripts/simulate_stage6.py --vendor-handle oluwadunnioluajayi \\
        --golden eval/golden/vendor_gadgets_01.json
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.logging_config import configure_logging  # noqa: E402
from scripts.run_stage6 import (  # noqa: E402
    DEFAULT_GOLDEN_PATH,
    DEFAULT_VENDOR_HANDLE,
    run_sync,
    snapshot_dir_for,
)

SIMULATED_CHANGES_PATH = Path("report/changes.simulated.json")


def _blank_raw_post(post_id: str, caption: str, media_url: str) -> dict:
    """Minimal raw Graph API media shape - just enough for
    _normalize_fresh_post() to consume like a real /me/media row."""
    return {
        "id": post_id,
        "caption": caption,
        "media_type": "IMAGE",
        "media_url": media_url,
        "permalink": None,
        "timestamp": None,
        "comments_count": 0,
        "comments": {"data": []},
    }


def build_synthetic_raw_posts(snapshot: list[dict], vendor_handle: str = DEFAULT_VENDOR_HANDLE) -> list[dict]:
    """Clones every post in the real snapshot as-is, then mutates a handful
    of them to hit every diff path. Needs at least 6 posts in the snapshot
    to have distinct posts for each mutation - run_build_snapshot.py's 30
    are plenty.

    Clones carry the snapshot's own comment_count forward, so an untouched
    post is a genuine no-op. This used to set comments_count=0 on every clone,
    which made every post that really had comments report a spurious
    "content_changed": compute_content_hash() also hashed len(comments) back
    then, which a clone cannot reconstruct from a snapshot that only stores
    the count. That component is gone from the hash (see
    pipeline/stages/stage6_sync.py), so carrying the count forward is now
    enough, and the only changes reported are the intended ones.
    """
    entries = {e["post_id"]: e for e in snapshot}
    ids = list(entries)
    if len(ids) < 6:
        raise ValueError("Need at least 6 posts in the snapshot to simulate every path distinctly")

    raw = []
    for e in snapshot:
        clone = _blank_raw_post(e["post_id"], e.get("caption"), e.get("media_url"))
        clone["comments_count"] = e.get("comment_count") or 0
        raw.append(clone)
    by_id = {r["id"]: r for r in raw}

    # 1. No-op: pick a post that genuinely had zero comments in the real
    #    snapshot, so cloning it can't spuriously touch its content_hash
    #    (see the comments_count caveat above) - a real, clean no-op demo.
    no_op_candidates = [pid for pid in ids if entries[pid].get("comment_count", 0) == 0]
    ids = [no_op_candidates[0]] + [pid for pid in ids if pid != no_op_candidates[0]]

    # 2. Caption edit: price change appended to an existing caption.
    edit_id = ids[1]
    by_id[edit_id]["caption"] = (by_id[edit_id]["caption"] or "") + "\n\n[SIMULATED PRICE REDUCED]"

    # 3. Comment delta: bump the count and supply the new comments directly
    #    (sidesteps the real comments-edge unreliability documented in
    #    README.md - this exercises the cursor/delta-listing logic itself).
    delta_id = ids[2]
    by_id[delta_id]["comments_count"] = (entries[delta_id].get("comment_count") or 0) + 2
    by_id[delta_id]["comments"] = {
        "data": [
            {"id": "sim_c1", "username": "buyer_test", "text": "Still available?", "timestamp": "2026-09-05T00:00:00+0000"},
            {"id": "sim_c2", "username": vendor_handle, "text": "Yes, still available!", "timestamp": "2026-09-05T00:05:00+0000"},
        ]
    }

    # 4. Deleted: drop ids[3] from the fresh list entirely.
    deleted_id = ids[3]
    raw = [r for r in raw if r["id"] != deleted_id]

    # 5. Brand-new post: no image/caption overlap with anything in the catalog.
    raw.append(_blank_raw_post(
        "SIM_NEW_0001",
        "2020 Kia Sportage, low mileage, well maintained. DM for price.",
        "https://picsum.photos/seed/sim-new-0001/800/600",
    ))

    # 6. Exact-duplicate repost: reuses ids[4]'s real image (for a genuine
    #    pHash match) but a caption written fresh here rather than reused
    #    verbatim - some real catalog posts already contain "sold" from an
    #    earlier real merge, which would silently turn this into the same
    #    scenario as case 7 below. Should pHash-match at distance 0 and
    #    auto-merge with no vendor action (plain refresh/restock signal).
    repost_source = entries[ids[4]]
    raw.append(_blank_raw_post(
        "SIM_REPOST_EXACT_0001",
        "Still available! Reposting for visibility - message us for details.",
        repost_source.get("media_url"),
    ))

    # 7. SOLD repost: reuses ids[5]'s real image with a rewritten "SOLD!"
    #    caption - should still pHash-match (image unchanged) and
    #    auto-merge, but propose out_of_stock and require vendor action for
    #    that transition specifically (merge itself still self-applies).
    sold_source = entries[ids[5]]
    raw.append(_blank_raw_post(
        "SIM_REPOST_SOLD_0001",
        "SOLD! 🎉 Thank you to our client - " + (sold_source.get("caption") or ""),
        sold_source.get("media_url"),
    ))

    return raw


def main() -> None:
    configure_logging()
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor-handle", default=DEFAULT_VENDOR_HANDLE,
                         help="Instagram username whose real snapshot to simulate against "
                              "(default: ayodele.akinbohun)")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN_PATH,
                         help="Golden-set file, only used for matched_permalink display "
                              "(default: eval/golden/vendor_autos_01.json)")
    parser.add_argument("--changes", type=Path, default=SIMULATED_CHANGES_PATH,
                         help="Where to write the simulated changes JSON "
                              "(default: report/changes.simulated.json)")
    args = parser.parse_args()

    vendor_handle = args.vendor_handle
    snapshot_dir = snapshot_dir_for(vendor_handle)
    # The simulator fakes the Graph fetch but its embedding calls are real and
    # cost real quota, so they are logged like any other. The "simulated_"
    # prefix keeps them out of any run_id a published figure is derived from.
    vendor_id = args.golden.stem if args.golden.exists() else vendor_handle
    run_id = f"simulated_sync_{vendor_id}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"

    snapshot = json.loads((snapshot_dir / "latest.json").read_text(encoding="utf-8"))
    emb_matrix = np.load(snapshot_dir / "embeddings.npy")
    embedding_index = json.loads((snapshot_dir / "embedding_index.json").read_text(encoding="utf-8"))
    post_ids_by_row = [embedding_index[str(i)] for i in range(len(embedding_index))]
    golden_by_id = {}
    if args.golden.exists():
        golden_by_id = {p["post_id"]: p for p in json.loads(args.golden.read_text(encoding="utf-8"))}

    raw_posts = build_synthetic_raw_posts(snapshot, vendor_handle)

    print(f"=== SIMULATED sync for {vendor_handle} (no live API fetch, no snapshot writes) ===")
    run_sync(
        raw_posts,
        snapshot,
        emb_matrix,
        post_ids_by_row,
        golden_by_id,
        args.changes,
        persist_snapshot=False,
        vendor_handle=vendor_handle,
        snapshot_dir=snapshot_dir,
        run_id=run_id,
        vendor_id=vendor_id,
    )


if __name__ == "__main__":
    main()
