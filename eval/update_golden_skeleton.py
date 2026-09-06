"""
Incremental golden-set updater.

Use this instead of make_golden_skeleton.py once a golden file
already exists for an account and you've pulled fresh posts via
ingest.py. Diffs the latest raw dump against what's already in the
golden file by post_id, and appends unlabeled skeleton entries ONLY
for genuinely new posts.

No hand-labeled field (post_type, products, notes, carousel_classification,
etc.) on an existing entry is ever touched or reordered. The one exception
is SYNCED_FIELDS - pure pipeline metadata (currently just media_url) that
is always refreshed to match the latest raw dump, even when an entry
already has a value. media_url specifically needs this: Instagram's CDN
URLs carry a short-lived expiry token, so an old golden entry's media_url
can go dead (403 on fetch) well before its hand-labeled content does -
"already has a value" isn't the same as "still works". A post no longer
present in the latest pull is left exactly as-is; there's nothing fresher
to sync it to.

Usage:
    uv run eval/update_golden_skeleton.py vendor_autos_01

If eval/golden/vendor_autos_01.json doesn't exist yet, this just
behaves like make_golden_skeleton.py (nothing to diff against).
"""

import json
import logging
import sys
from pathlib import Path

from make_golden_skeleton import build_comments_list, collect_images, latest_dump, make_product_entry

from pipeline.exceptions import MissingRawDumpError
from pipeline.logging_config import configure_logging
from pipeline.types import Post

logger = logging.getLogger(__name__)

# Pure pipeline metadata (never hand-labeled) that's always kept in sync
# with the latest raw dump, even overwriting an existing value - see the
# module docstring for why media_url specifically needs this. thumbnail_url
# is the same kind of CDN URL (video-post cover image) with the same
# rotating-expiry-token problem, so it gets the same treatment.
SYNCED_FIELDS = ("media_url", "thumbnail_url")


def build_new_entry(post: Post, vendor_username: str | None) -> dict:
    """One skeleton entry for a post that isn't in the golden file yet -
    same shape as make_golden_skeleton.py::build_skeleton()'s entries."""
    is_carousel = "children" in post
    return {
        "post_id": post["id"],
        "media_type": post.get("media_type"),
        "caption": post.get("caption", ""),
        "media_url": post.get("media_url"),
        "thumbnail_url": post.get("thumbnail_url"),
        "media_product_type": post.get("media_product_type"),
        "permalink": post.get("permalink"),
        "timestamp": post.get("timestamp"),
        "has_carousel_children": is_carousel,
        "comment_count": len(post.get("comments", [])),
        "comments": build_comments_list(post, vendor_username),
        "post_type": None,
        "carousel_classification": None if is_carousel else "not_carousel",
        "expected_flag": None,
        "notes": "",
        "products": [make_product_entry(images=collect_images(post))],
    }


def sync_known_fields(existing: list[dict], posts_by_id: dict[str, Post]) -> int:
    """Syncs SYNCED_FIELDS on existing entries to the latest raw dump's
    value - including overwriting a value that's already set, since these
    fields (currently just media_url) can go stale on their own. Returns
    the number of entries changed."""
    synced = 0
    for entry in existing:
        post = posts_by_id.get(entry["post_id"])
        if post is None:
            continue  # post no longer in the latest pull - nothing to sync from
        changed = False
        for field in SYNCED_FIELDS:
            fresh_value = post.get(field)
            if fresh_value and entry.get(field) != fresh_value:
                entry[field] = fresh_value
                changed = True
        if changed:
            synced += 1
    return synced


def update_skeleton(account_label: str) -> None:
    """Appends skeleton entries for posts new since the golden file was
    last built/updated, and syncs SYNCED_FIELDS on existing entries to the
    latest raw dump. Writes eval/golden/<account_label>.json in place.

    Raises:
        MissingRawDumpError: If no raw dump exists yet for account_label.
    """
    dump_path = latest_dump(account_label)
    data = json.loads(dump_path.read_text(encoding="utf-8"))
    vendor_username = data.get("profile", {}).get("username")
    posts_by_id: dict[str, Post] = {post["id"]: post for post in data["media"]}

    golden_path = Path("eval") / "golden" / f"{account_label}.json"
    existing = json.loads(golden_path.read_text(encoding="utf-8")) if golden_path.exists() else []
    existing_ids = {entry["post_id"] for entry in existing}

    synced_count = sync_known_fields(existing, posts_by_id)

    new_entries = [
        build_new_entry(post, vendor_username) for post in data["media"]
        if post["id"] not in existing_ids
    ]

    if not new_entries and not synced_count:
        logger.info("No new posts and nothing to sync for '%s' - %d already labeled, %d in latest pull. "
                     "Nothing to do.", account_label, len(existing), len(data["media"]))
        return

    merged = existing + new_entries
    golden_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info("Updated: %s", golden_path)
    if synced_count:
        logger.info("  %d existing entries synced (fresh %s)", synced_count, ", ".join(SYNCED_FIELDS))
    logger.info("  %d previously labeled posts - hand-labeled fields untouched", len(existing))
    logger.info("  %d new posts appended, ready to label:", len(new_entries))
    for entry in new_entries:
        logger.info("    - %s: %s", entry["post_id"], entry["permalink"])


if __name__ == "__main__":
    configure_logging()

    if len(sys.argv) != 2:
        logger.info("Usage: uv run eval/update_golden_skeleton.py <account_label>")
        sys.exit(1)
    try:
        update_skeleton(sys.argv[1])
    except MissingRawDumpError as exc:
        sys.exit(str(exc))
