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
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "eval"))

from make_golden_skeleton import latest_dump  # noqa: E402

from pipeline.logging_config import configure_logging  # noqa: E402
from pipeline.media_fingerprint import compute_caption_embedding, compute_image_phash  # noqa: E402
from pipeline.stages.stage6_sync import compute_content_hash, compute_lifecycle_state  # noqa: E402
from pipeline.types import vision_image_url  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_VENDOR_HANDLE = "ayodele.akinbohun"
DEFAULT_GOLDEN_PATH = Path("eval/golden/vendor_autos_01.json")


def normalize_dump_post(raw: dict) -> dict:
    """A raw ingest dump row -> the field names build_snapshot_entry() reads.

    Deliberately NOT scripts/run_pipeline.py::normalize_post(), which sets
    comment_count to len(comments) - the number of comment bodies actually
    readable, which Meta always makes 0. Here comment_count must mean the same
    thing it means to scripts/run_stage6.py::_normalize_fresh_post(): the
    Graph API's own comments_count. If these two ever disagree, the first sync
    against this baseline reports a comment_delta on every commented post and
    the change-classification numbers are worthless. tests/test_stage6_sync.py
    pins them to each other.

    A live-derived entry has no gold to draw on, so post_type/products/
    carousel_classification are left unset - build_snapshot_entry() then
    records None/[]/None, exactly as run_stage6.py::_advance_snapshot() does
    for a brand-new post.
    """
    return {
        "post_id": raw["id"],
        "caption": raw.get("caption") or "",
        "media_url": raw.get("media_url"),
        "thumbnail_url": raw.get("thumbnail_url"),
        "media_product_type": raw.get("media_product_type"),
        "media_type": raw.get("media_type"),
        "permalink": raw.get("permalink"),
        "timestamp": raw.get("timestamp"),
        "comment_count": raw.get("comments_count", 0),
        "comments": raw.get("comments") or [],
    }


def load_posts(dump_path: Path | None, golden_path: Path | None) -> tuple[list[dict], str]:
    """Returns (posts, source_description). Exactly one of the two paths is set."""
    if dump_path is not None:
        dump = json.loads(dump_path.read_text(encoding="utf-8"))
        media = dump["media"]
        if media and "comments_count" not in media[0]:
            logger.warning(
                "%s predates the comments_count field in ingest/ingest.py, so every entry in "
                "this baseline will record comment_count=0 and the first sync will report a "
                "spurious comment_delta on every post that has comments. Re-run `make ingest` "
                "and build from the new dump.",
                dump_path.name,
            )
        return [normalize_dump_post(raw) for raw in media], f"dump {dump_path}"
    return json.loads(golden_path.read_text(encoding="utf-8")), f"golden set {golden_path}"


def _comment_cursor(post: dict) -> str | None:
    comments = post.get("comments") or []
    return comments[-1]["id"] if comments else None


def build_snapshot_entry(
    post: dict,
    vendor_handle: str,
    run_id: str | None = None,
    vendor_id: str | None = None,
) -> tuple[dict, str | None, str | None, list[float], bool]:
    """Returns (snapshot_entry, phash_or_None, phash_fail_reason_or_None,
    embedding, embedding_ok). run_id/vendor_id are passed straight through to
    the embedding call so its cost lands in report/token_log.csv."""
    post_id = post["post_id"]
    content_hash = compute_content_hash(post)
    phash, phash_fail_reason = compute_image_phash(vision_image_url(post), post_id=post_id)
    embedding, embedding_ok = compute_caption_embedding(
        post.get("caption"), post_id=post_id, run_id=run_id, vendor_id=vendor_id
    )

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
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--dump", type=Path, default=None,
                         help="Raw ingest dump to build the baseline from - the path to use. "
                              "Mutually exclusive with --golden/--account")
    source.add_argument("--account", default=None,
                         help="Local account_label whose most recent raw dump to use, e.g. "
                              "vendor_gadgets_01. Shorthand for --dump <latest dump>")
    source.add_argument("--golden", type=Path, default=None,
                         help="Golden-set file to build the snapshot from. Legacy path: a golden "
                              "set's hand-authored comments and gold labels are not what a live "
                              "sync re-fetches (default when no source is given: "
                              "eval/golden/vendor_autos_01.json)")
    args = parser.parse_args()

    vendor_handle = args.vendor_handle
    dump_path = args.dump
    if args.account is not None:
        dump_path = latest_dump(args.account)
    golden_path = args.golden
    if dump_path is None and golden_path is None:
        golden_path = DEFAULT_GOLDEN_PATH
    snapshot_dir = Path("data/snapshots") / vendor_handle

    # vendor_id keys report/token_log.csv. It is the golden file's stem where
    # there is one and the account_label otherwise - both name the local
    # experiment folder, never the Instagram handle (see CLAUDE.md).
    vendor_id = args.account or (golden_path.stem if golden_path is not None else vendor_handle)
    run_id = f"snapshot_{vendor_id}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"

    posts, source_description = load_posts(dump_path, golden_path)
    logger.info("Building baseline for %s from %s (run_id=%s)", vendor_handle, source_description, run_id)
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

        entry, phash, phash_fail_reason, embedding, embedding_ok_flag = build_snapshot_entry(
            post, vendor_handle, run_id=run_id, vendor_id=vendor_id
        )

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
    logger.info("Source                : %s", source_description)
    logger.info("Run id                : %s", run_id)
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
