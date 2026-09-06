#!/usr/bin/env python3
"""
Refreshes stale Instagram CDN URLs in the golden set.

CDN URLs (instagram.*.fna.fbcdn.net links) carry expiring `oh=`/`oe=` query
params. Every `post["media_url"]` and `post["products"][i]["images"][j]` in
eval/golden/vendor_autos_01.json is re-pulled from the Instagram Graph API
and the golden set is overwritten in-place. No other fields are touched.

Usage:
    1. Set IG_ACCESS_TOKEN in .env (same token ingest.py uses).
    2. uv run scripts/refresh_media_urls.py
"""

import json
import logging
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.exceptions import MissingCredentialsError  # noqa: E402
from pipeline.logging_config import configure_logging  # noqa: E402
from pipeline.settings import IG_ACCESS_TOKEN  # noqa: E402

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.instagram.com"
API_VERSION = "v21.0"
REQUEST_TIMEOUT_SECONDS = 10
RATE_LIMIT_SLEEP_SECONDS = 0.3  # basic tier: 200 calls/hour
GOLDEN_PATH = Path("eval/golden/vendor_autos_01.json")


def fetch_fresh_media(post_id: str, has_carousel_children: bool) -> dict | None:
    """Fetches fresh media_url (+ children for carousel posts) for one post.

    Returns the API response dict, or None if the post is unreachable
    (deleted / 404 / any other API error) - caller skips that post and
    leaves its golden-set entry untouched.
    """
    fields = "id,media_url"
    if has_carousel_children:
        fields += ",children{id,media_url}"
    url = f"{GRAPH_BASE}/{API_VERSION}/{post_id}"
    params = {"fields": fields, "access_token": IG_ACCESS_TOKEN}

    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        logger.warning("[skip] %s: request failed: %s", post_id, exc)
        return None
    finally:
        time.sleep(RATE_LIMIT_SLEEP_SECONDS)

    if resp.status_code == 404:
        logger.warning("[skip] %s: 404 - post no longer exists on Instagram", post_id)
        return None
    if resp.status_code != 200:
        logger.warning("[skip] %s: API error (%d): %s", post_id, resp.status_code, resp.text)
        return None

    data = resp.json()
    if "error" in data:
        logger.warning("[skip] %s: API error: %s", post_id, data["error"])
        return None
    return data


def _resolve_url(item: dict) -> str | None:
    """media_url, falling back to thumbnail_url (video items don't return
    media_url)."""
    return item.get("media_url") or item.get("thumbnail_url")


def update_post(post: dict, api_data: dict) -> tuple[int, int]:
    """Mutates post's media_url and products[*].images[*] in-place from
    api_data. Returns (media_url_updated, product_image_urls_updated)."""
    post_id = post.get("post_id")
    media_updated = 0
    images_updated = 0

    fresh_media_url = _resolve_url(api_data)
    if fresh_media_url:
        post["media_url"] = fresh_media_url
        media_updated = 1
    else:
        logger.warning("[warn] %s: API response had neither media_url nor thumbnail_url - left unchanged", post_id)

    children = api_data.get("children", {}).get("data", [])
    if not children and fresh_media_url:
        # Non-carousel posts have no children field at all - the post's own
        # single image IS the "child" every product's images[0] should map
        # to (golden-set non-carousel products duplicate the hero image
        # across their images arrays).
        children = [{"media_url": fresh_media_url}]

    fresh_urls = [_resolve_url(child) for child in children]

    for product in post.get("products") or []:
        images = product.get("images") or []
        for j in range(len(images)):
            if j < len(fresh_urls) and fresh_urls[j]:
                images[j] = fresh_urls[j]
                images_updated += 1
            elif j >= len(fresh_urls):
                logger.warning(
                    "[warn] %s: product %r images[%d] has no matching API child - left unchanged",
                    post_id, product.get("name"), j,
                )

    return media_updated, images_updated


def main() -> None:
    configure_logging()

    if not IG_ACCESS_TOKEN:
        raise MissingCredentialsError("Set IG_ACCESS_TOKEN in your environment first.")

    posts = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    backup_path = GOLDEN_PATH.with_suffix(GOLDEN_PATH.suffix + ".bak")
    backup_path.write_text(json.dumps(posts, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Backed up current golden set to %s", backup_path)

    posts_updated = 0
    total_media_updated = 0
    total_images_updated = 0

    for i, post in enumerate(posts, start=1):
        post_id = post.get("post_id")
        has_carousel = bool(post.get("has_carousel_children"))
        logger.info("[%d/%d] Fetching %s (carousel=%s)...", i, len(posts), post_id, has_carousel)

        api_data = fetch_fresh_media(post_id, has_carousel)
        if api_data is None:
            continue

        media_updated, images_updated = update_post(post, api_data)
        if media_updated or images_updated:
            posts_updated += 1
        total_media_updated += media_updated
        total_images_updated += images_updated

    GOLDEN_PATH.write_text(json.dumps(posts, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info(
        "Updated %d/%d posts, %d media_url fields, %d product image URLs",
        posts_updated, len(posts), total_media_updated, total_images_updated,
    )


if __name__ == "__main__":
    try:
        main()
    except MissingCredentialsError as exc:
        sys.exit(str(exc))
