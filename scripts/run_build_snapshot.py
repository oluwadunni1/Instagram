#!/usr/bin/env python3
"""
Stage 6 - Sync, Step 1: builds the baseline snapshot for a vendor from the
(already-refreshed) golden set, so scripts/run_stage6.py has a "before"
state to diff a fresh Instagram fetch against.

Usage:
    uv run scripts/run_build_snapshot.py
    uv run scripts/run_build_snapshot.py --vendor-handle gadget.hub.ng \\
        --golden eval/golden/vendor_gadgets_01.json

Defaults reproduce the original ayodele.akinbohun / vendor_autos_01 baseline.
--vendor-handle must match the actual Instagram username (used to detect the
vendor's own comment replies, e.g. "sold" - see compute_lifecycle_state()) -
it is unrelated to the local account_label folder name ingest.py lets you
pick freely.
"""

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.logging_config import configure_logging  # noqa: E402
from pipeline.media_fingerprint import compute_caption_embedding, compute_image_phash  # noqa: E402
from pipeline.stages.stage6_sync import compute_content_hash, compute_lifecycle_state  # noqa: E402
from pipeline.types import vision_image_url  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_VENDOR_HANDLE = "ayodele.akinbohun"
DEFAULT_GOLDEN_PATH = Path("eval/golden/vendor_autos_01.json")


def _comment_cursor(post: dict) -> str | None:
    comments = post.get("comments") or []
    return comments[-1]["id"] if comments else None


def build_snapshot_entry(post: dict, vendor_handle: str) -> tuple[dict, str | None, str | None, list[float], bool]:
    """Returns (snapshot_entry, phash_or_None, phash_fail_reason_or_None,
    embedding, embedding_ok)."""
    post_id = post["post_id"]
    content_hash = compute_content_hash(post)
    phash, phash_fail_reason = compute_image_phash(vision_image_url(post), post_id=post_id)
    embedding, embedding_ok = compute_caption_embedding(post.get("caption"), post_id=post_id)

    entry = {
        "post_id": post_id,
        "caption": post.get("caption"),
        "comment_count": post.get("comment_count"),
        "media_url": post.get("media_url"),
        "content_hash": content_hash,
        "image_phash": phash,
        "comment_cursor": _comment_cursor(post),
        "lifecycle_state": compute_lifecycle_state(post.get("post_type"), post.get("comments"), vendor_handle),
        "catalog_products": post.get("products", []),
        "post_type": post.get("post_type"),
        "carousel_classification": post.get("carousel_classification"),
    }
    return entry, phash, phash_fail_reason, embedding, embedding_ok


def main() -> None:
    configure_logging()

    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor-handle", default=DEFAULT_VENDOR_HANDLE,
                         help="Instagram username - used to detect the vendor's own comment "
                              "replies (default: ayodele.akinbohun)")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN_PATH,
                         help="Golden-set file to build the snapshot from "
                              "(default: eval/golden/vendor_autos_01.json)")
    args = parser.parse_args()

    vendor_handle = args.vendor_handle
    golden_path = args.golden
    snapshot_dir = Path("data/snapshots") / vendor_handle

    posts = json.loads(golden_path.read_text(encoding="utf-8"))
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    snapshot_entries = []
    embeddings = []
    embedding_index = {}
    phash_ok = 0
    phash_failures: list[tuple[str, str]] = []
    embed_ok = embed_fail = 0
    lifecycle_counts: Counter = Counter()

    for i, post in enumerate(posts):
        post_id = post["post_id"]
        logger.info("[%d/%d] %s", i + 1, len(posts), post_id)

        entry, phash, phash_fail_reason, embedding, embedding_ok_flag = build_snapshot_entry(post, vendor_handle)

        if phash is not None:
            phash_ok += 1
        else:
            phash_failures.append((post_id, phash_fail_reason or "unknown"))
        if embedding_ok_flag:
            embed_ok += 1
        else:
            embed_fail += 1
        lifecycle_counts[entry["lifecycle_state"]] += 1

        snapshot_entries.append(entry)
        embedding_index[str(i)] = post_id
        embeddings.append(embedding)

    latest_path = snapshot_dir / "latest.json"
    latest_path.write_text(json.dumps(snapshot_entries, indent=2, ensure_ascii=False), encoding="utf-8")

    embeddings_path = snapshot_dir / "embeddings.npy"
    np.save(embeddings_path, np.array(embeddings, dtype=np.float64))

    index_path = snapshot_dir / "embedding_index.json"
    index_path.write_text(json.dumps(embedding_index, indent=2), encoding="utf-8")

    logger.info("")
    logger.info("=== Stage 6 Snapshot Build - %s ===", vendor_handle)
    logger.info("Total posts processed : %d", len(posts))
    logger.info("pHash                 : %d succeeded / %d failed", phash_ok, len(phash_failures))
    logger.info("Embeddings            : %d succeeded / %d failed", embed_ok, embed_fail)
    logger.info("Lifecycle state distribution:")
    for state, count in lifecycle_counts.most_common():
        logger.info("  %-14s: %d", state, count)
    if phash_failures:
        logger.info("pHash failures (post_id: reason):")
        for post_id, reason in phash_failures:
            logger.info("  %s: %s", post_id, reason)
    logger.info("Written -> %s", latest_path)
    logger.info("Written -> %s", embeddings_path)
    logger.info("Written -> %s", index_path)


if __name__ == "__main__":
    main()
