"""
Incremental golden-set updater.

Use this instead of make_golden_skeleton.py once a golden file
already exists for an account and you've pulled fresh posts via
ingest.py. Diffs the latest raw dump against what's already in the
golden file by post_id, and appends unlabeled skeleton entries ONLY
for genuinely new posts. Every already-labeled post is left exactly
as-is - this never touches or reorders existing entries.

Usage:
    uv run eval/update_golden_skeleton.py vendor_autos_01

If eval/golden/vendor_autos_01.json doesn't exist yet, this just
behaves like make_golden_skeleton.py (nothing to diff against).
"""

import json
import sys
from pathlib import Path

from make_golden_skeleton import make_product_entry, collect_images, build_comments_list, latest_dump


def build_new_entry(post: dict, vendor_username: str | None) -> dict:
    is_carousel = "children" in post
    return {
        "post_id": post["id"],
        "media_type": post.get("media_type"),
        "caption": post.get("caption", ""),
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


def update_skeleton(account_label: str) -> None:
    dump_path = latest_dump(account_label)
    data = json.loads(dump_path.read_text(encoding="utf-8"))
    vendor_username = data.get("profile", {}).get("username")

    golden_path = Path("eval") / "golden" / f"{account_label}.json"
    existing = json.loads(golden_path.read_text(encoding="utf-8")) if golden_path.exists() else []
    existing_ids = {entry["post_id"] for entry in existing}

    new_entries = [
        build_new_entry(post, vendor_username) for post in data["media"]
        if post["id"] not in existing_ids
    ]

    if not new_entries:
        print(f"No new posts found for '{account_label}' - {len(existing)} already labeled, "
              f"{len(data['media'])} in latest pull. Nothing to do.")
        return

    merged = existing + new_entries
    golden_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Updated: {golden_path}")
    print(f"  {len(existing)} previously labeled posts - untouched")
    print(f"  {len(new_entries)} new posts appended, ready to label:")
    for entry in new_entries:
        print(f"    - {entry['post_id']}: {entry['permalink']}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: uv run eval/update_golden_skeleton.py <account_label>")
        sys.exit(1)
    update_skeleton(sys.argv[1])