#!/usr/bin/env python3
"""
Refreshes stale Instagram CDN URLs in the golden set.

CDN URLs (instagram.*.fna.fbcdn.net links) carry expiring `oh=`/`oe=` query
params. Every `post["media_url"]` and `post["products"][i]["images"][j]` in
the target golden set is re-pulled from the Instagram Graph API and the
golden set is overwritten in-place. No other fields are touched.

Usage:
    1. Set the account's token in .env (same token ingest.py uses).
    2. uv run scripts/refresh_media_urls.py
       uv run scripts/refresh_media_urls.py \\
           --golden eval/golden/vendor_gadgets_01.json \\
           --token-env IG_ACCESS_TOKEN_GADGETS

Defaults target vendor_autos_01 with IG_ACCESS_TOKEN. The token must belong
to the account that owns the posts in --golden, since the Graph API only
answers for the account its token is connected to.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.exceptions import MissingCredentialsError  # noqa: E402
from pipeline.logging_config import configure_logging  # noqa: E402
from pipeline.settings import get_ig_access_token, ig_auth_headers, redact_tokens  # noqa: E402
from pipeline.types import vision_image_url  # noqa: E402

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.instagram.com"
API_VERSION = "v21.0"
REQUEST_TIMEOUT_SECONDS = 10
RATE_LIMIT_SLEEP_SECONDS = 0.3  # basic tier: 200 calls/hour
DEFAULT_GOLDEN_PATH = Path("eval/golden/vendor_autos_01.json")


def fetch_fresh_media(post_id: str, has_carousel_children: bool, token: str) -> dict | None:
    """Fetches fresh media_url (+ children for carousel posts) for one post.

    Returns the API response dict, or None if the post is unreachable
    (deleted / 404 / any other API error) - caller skips that post and
    leaves its golden-set entry untouched.
    """
    # thumbnail_url is requested alongside media_url because _resolve_url()
    # falls back to it for VIDEO/Reel posts, where media_url is the video
    # file rather than an image - without asking for the field, that
    # fallback could never fire.
    fields = "id,media_url,thumbnail_url"
    if has_carousel_children:
        fields += ",children{id,media_url,thumbnail_url}"
    url = f"{GRAPH_BASE}/{API_VERSION}/{post_id}"
    params = {"fields": fields}

    try:
        resp = requests.get(url, params=params, headers=ig_auth_headers(token),
                             timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        # redact_tokens, not the bare exception: requests embeds the request
        # URL in connection/timeout errors, and this logs at WARNING - always
        # visible, not gated behind LOG_LEVEL.
        logger.warning("[skip] %s: request failed: %s", post_id, redact_tokens(exc))
        return None
    finally:
        time.sleep(RATE_LIMIT_SLEEP_SECONDS)

    if resp.status_code == 404:
        logger.warning("[skip] %s: 404 - post no longer exists on Instagram", post_id)
        return None
    if resp.status_code != 200:
        logger.warning("[skip] %s: API error (%d): %s", post_id, resp.status_code, redact_tokens(resp.text))
        return None

    data = resp.json()
    if "error" in data:
        logger.warning("[skip] %s: API error: %s", post_id, redact_tokens(data["error"]))
        return None
    return data


def _resolve_url(item: dict) -> str | None:
    """The item's own media_url, falling back to thumbnail_url.

    This is the right resolution for refreshing the `media_url` FIELD, which
    should keep holding whatever the API calls media_url (the .mp4 itself for
    a VIDEO/Reel). It is deliberately NOT the right resolution for
    products[].images[] - see update_post, which uses vision_image_url()
    there instead.
    """
    return item.get("media_url") or item.get("thumbnail_url")


def update_post(post: dict, api_data: dict) -> tuple[int, int]:
    """Mutates post's media_url, thumbnail_url and products[*].images[*]
    in-place from api_data. Returns (media_url_updated,
    product_image_urls_updated)."""
    post_id = post.get("post_id")
    media_updated = 0
    images_updated = 0

    fresh_media_url = _resolve_url(api_data)
    if fresh_media_url:
        post["media_url"] = fresh_media_url
        media_updated = 1
    else:
        logger.warning("[warn] %s: API response had neither media_url nor thumbnail_url - left unchanged", post_id)

    # thumbnail_url is a CDN URL with the same expiring oh=/oe= params as
    # media_url, so it goes stale the same way and must be refreshed too -
    # it only exists on VIDEO/Reel media, hence the guard.
    if api_data.get("thumbnail_url"):
        post["thumbnail_url"] = api_data["thumbnail_url"]

    children = api_data.get("children", {}).get("data", [])
    if not children and fresh_media_url:
        # Non-carousel posts have no children field at all - the post's own
        # single image IS the "child" every product's images[0] should map
        # to (golden-set non-carousel products duplicate the hero image
        # across their images arrays). Carry thumbnail_url through so the
        # vision_image_url() resolution below can see it.
        children = [{"media_url": fresh_media_url, "thumbnail_url": api_data.get("thumbnail_url")}]

    # vision_image_url(), NOT _resolve_url(): products[].images[] feeds Stage
    # 2/3's vision passes and the reviewer's thumbnail, so a Reel must resolve
    # to its .jpg cover, never the .mp4. Using _resolve_url here would
    # overwrite a correct thumbnail with the video file on every refresh -
    # silently corrupting the irreplaceable golden set.
    fresh_urls = [vision_image_url(child) for child in children]

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


def update_raw_media(raw: dict, api_data: dict) -> int:
    """Mutates ONE raw-dump media row's URLs in place. Returns media_url updates.

    Raw dumps are what scripts/run_pipeline.py reads, and they go stale exactly
    like golden sets do - the same expiring oh=/oe= CDN params. Refreshing only
    the golden set (which is all this script did until 2026-09-10) left the
    chained path failing vision on 403s that the harness path never saw, making
    chained results look systematically worse for reasons unrelated to any model.

    The shapes differ, which is why this is not update_post(): a dump keys posts
    by "id" (not "post_id") and carries no products[] at all. Its children ARE
    nested as {"data": [...]}, mirroring the raw Graph response it was saved
    from - the same shape api_data uses.
    """
    fresh = _resolve_url(api_data)
    updated = 0
    if fresh:
        raw["media_url"] = fresh
        updated = 1
    else:
        logger.warning("[warn] %s: API response had neither media_url nor thumbnail_url "
                        "- left unchanged", raw.get("id"))

    if api_data.get("thumbnail_url"):
        raw["thumbnail_url"] = api_data["thumbnail_url"]

    # Children are refreshed positionally, same as the golden path. A child the
    # API no longer returns keeps its stale URL rather than being dropped -
    # losing the row would change the post's shape, which is worse than a URL
    # that fails one vision call.
    api_children = api_data.get("children", {}).get("data", [])
    for j, child in enumerate((raw.get("children") or {}).get("data") or []):
        if j >= len(api_children):
            continue
        child_url = vision_image_url(api_children[j])
        if child_url:
            child["media_url"] = child_url
            if api_children[j].get("thumbnail_url"):
                child["thumbnail_url"] = api_children[j]["thumbnail_url"]

    return updated


def refresh_dump(dump_path: Path, token: str) -> None:
    """Refreshes every media row's CDN URLs in one raw dump, in place."""
    dump = json.loads(dump_path.read_text(encoding="utf-8"))
    media = dump.get("media") or []

    backup_path = dump_path.with_suffix(dump_path.suffix + ".bak")
    backup_path.write_text(json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Backed up current dump to %s", backup_path)

    updated = 0
    for i, raw in enumerate(media, start=1):
        post_id = raw.get("id")
        has_carousel = bool(raw.get("children"))
        logger.info("[%d/%d] Fetching %s (carousel=%s)...", i, len(media), post_id, has_carousel)
        api_data = fetch_fresh_media(post_id, has_carousel, token)
        if api_data is None:
            continue
        updated += update_raw_media(raw, api_data)

    dump_path.write_text(json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Updated %d/%d media rows in %s", updated, len(media), dump_path.name)


def main() -> None:
    configure_logging()

    parser = argparse.ArgumentParser()
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN_PATH,
                         help="Golden-set file whose CDN URLs to refresh "
                              "(default: eval/golden/vendor_autos_01.json)")
    parser.add_argument("--dump", type=Path, default=None,
                         help="Refresh a raw dump (runs/<account>/raw/dump_*.json) instead of a "
                              "golden set. Raw dumps are what scripts/run_pipeline.py reads, and "
                              "their CDN URLs expire the same way - refreshing only the golden set "
                              "leaves the chained path failing vision on 403s the harness never "
                              "sees (README.md).")
    parser.add_argument("--token-env", default="IG_ACCESS_TOKEN",
                         help="Env var holding the token for the account that owns these posts "
                              "(default: IG_ACCESS_TOKEN)")
    args = parser.parse_args()

    token = get_ig_access_token(args.token_env)
    if not token:
        raise MissingCredentialsError(f"Set {args.token_env} in your environment first.")

    if args.dump:
        refresh_dump(args.dump, token)
        return

    golden_path = args.golden
    posts = json.loads(golden_path.read_text(encoding="utf-8"))

    backup_path = golden_path.with_suffix(golden_path.suffix + ".bak")
    backup_path.write_text(json.dumps(posts, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Backed up current golden set to %s", backup_path)

    posts_updated = 0
    total_media_updated = 0
    total_images_updated = 0

    for i, post in enumerate(posts, start=1):
        post_id = post.get("post_id")
        has_carousel = bool(post.get("has_carousel_children"))
        logger.info("[%d/%d] Fetching %s (carousel=%s)...", i, len(posts), post_id, has_carousel)

        api_data = fetch_fresh_media(post_id, has_carousel, token)
        if api_data is None:
            continue

        media_updated, images_updated = update_post(post, api_data)
        if media_updated or images_updated:
            posts_updated += 1
        total_media_updated += media_updated
        total_images_updated += images_updated

    golden_path.write_text(json.dumps(posts, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info(
        "Updated %d/%d posts, %d media_url fields, %d product image URLs",
        posts_updated, len(posts), total_media_updated, total_images_updated,
    )


if __name__ == "__main__":
    try:
        main()
    except MissingCredentialsError as exc:
        sys.exit(str(exc))
