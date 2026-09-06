"""
Stage 0 — Ingest (no AI)

Pulls, for one connected Instagram Professional (tester) account:
  - profile (bio, name, category)
  - media list (caption, timestamp, media type, permalink, media URL)
  - comments per post (paginated)
  - carousel children (all slide media IDs + image URLs), cached even
    though the POC doesn't process them yet — see brief §4

Caches everything to disk under runs/<account>/raw/ so the pipeline
can be re-run offline without re-hitting the API.

Usage:
    1. Copy .env.example to .env and fill in IG_ACCESS_TOKEN
    2. python ingest.py <account_label> [--token-env ENV_VAR_NAME]

    account_label is a folder name YOU choose to identify this
    vendor account locally (e.g. "vendor_fashion_01"). It does not
    need to match their Instagram username — this keeps your local
    run folders organized by category/number instead of by handle,
    which is convenient once you have several test accounts and
    also avoids scattering vendor usernames across your filesystem
    and logs unnecessarily (comments already carry real usernames,
    per §11 — no need to add more surface area than the API itself
    requires).

    --token-env lets a second (or third) connected account's token
    live in .env under its own name (e.g. IG_ACCESS_TOKEN_GADGETS)
    instead of overwriting IG_ACCESS_TOKEN every time you switch
    vendors - defaults to IG_ACCESS_TOKEN so existing single-account
    setups are unaffected.

Notes:
  - Uses graph.instagram.com per the "Instagram API with Instagram
    Login" product (not graph.facebook.com).
  - Respects the ~200 calls/user/hour business-use-case rate limit
    with basic 429 backoff.
  - Comments contain real people's handles/text — treated as
    sensitive. This script writes raw dumps to runs/, which must
    stay out of git (see .gitignore note at bottom of this file).
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from pipeline.exceptions import MissingCredentialsError
from pipeline.settings import get_ig_access_token

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.instagram.com"
API_VERSION = "v26.0"  # bump as Meta ships new versions; check current in their docs
RATE_LIMIT_SLEEP_SECONDS = 2  # gentle default pause between paginated calls
MAX_RETRIES = 5


def _get(url: str, params: dict, token: str) -> dict:
    """GET with basic 429/5xx backoff."""
    params = {**params, "access_token": token}
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = RATE_LIMIT_SLEEP_SECONDS * (2 ** (attempt - 1))
            logger.warning("[backoff] %s on %s - retry %d/%d in %ds", resp.status_code, url, attempt, MAX_RETRIES, wait)
            time.sleep(wait)
            continue
        # Non-retryable error — surface it immediately
        raise RuntimeError(f"Request failed ({resp.status_code}): {resp.text}")
    raise RuntimeError(f"Gave up after {MAX_RETRIES} retries: {url}")


def fetch_profile(token: str) -> dict:
    fields = "id,username,name,biography,account_type,media_count"
    return _get(f"{GRAPH_BASE}/{API_VERSION}/me", {"fields": fields}, token)


def fetch_media_list(token: str) -> list[dict]:
    """Paginate through /me/media."""
    # thumbnail_url/media_product_type: only populated for media_type=VIDEO
    # (Reels included) - media_url there is the video file, not an image.
    # See pipeline/types.py::vision_image_url() for which field downstream
    # vision calls/pHash actually use.
    fields = ("id,caption,timestamp,media_type,media_url,thumbnail_url,media_product_type,"
              "permalink,children{media_type,media_url,thumbnail_url}")
    url = f"{GRAPH_BASE}/{API_VERSION}/me/media"
    params = {"fields": fields, "limit": 50}
    all_media = []
    while url:
        data = _get(url, params, token)
        all_media.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        url = next_url
        params = {}  # next_url already carries all query params
        if url:
            time.sleep(RATE_LIMIT_SLEEP_SECONDS)
    return all_media


def fetch_comments(media_id: str, token: str, debug: bool = False) -> list[dict]:
    """Paginate through comments for a single media item."""
    fields = "id,text,username,timestamp,like_count"
    url = f"{GRAPH_BASE}/{API_VERSION}/{media_id}/comments"
    params = {"fields": fields, "limit": 50}
    all_comments = []
    first_call = True
    while url:
        try:
            data = _get(url, params, token)
        except RuntimeError as e:
            # Comments can be disabled on some posts — don't kill the whole run
            logger.warning("[warn] comments fetch failed for %s: %s", media_id, e)
            break
        if debug and first_call:
            # Logs the RAW response so you can see exactly what the API
            # returned — an empty {"data": []} means the request succeeded
            # but genuinely found nothing (often a scope issue), whereas an
            # "error" object here means something else is wrong entirely.
            logger.debug("[debug] raw response for %s: %s", media_id, json.dumps(data))
            first_call = False
        all_comments.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        url = next_url
        params = {}
        if url:
            time.sleep(RATE_LIMIT_SLEEP_SECONDS)
    return all_comments


def ingest_account(account_label: str, token_env: str = "IG_ACCESS_TOKEN") -> Path:
    """Pulls profile + media + comments for the connected IG account and
    caches the result to runs/<account_label>/raw/dump_<timestamp>.json.

    Args:
        account_label: Local folder name for this vendor's runs/ and
            eval/golden/ files - see the module docstring.
        token_env: .env variable name holding this account's access token.
            Defaults to IG_ACCESS_TOKEN; pass a different name (e.g.
            IG_ACCESS_TOKEN_GADGETS) to ingest a second connected account
            without overwriting the first one's token in .env.

    Raises:
        MissingCredentialsError: If the resolved token env var isn't set.
    """
    token = get_ig_access_token(token_env)
    if not token:
        raise MissingCredentialsError(f"Set {token_env} in your environment first.")

    run_dir = Path("runs") / account_label / "raw"
    run_dir.mkdir(parents=True, exist_ok=True)
    pulled_at = datetime.now(timezone.utc).isoformat()

    logger.info("[1/3] Fetching profile for '%s'...", account_label)
    profile = fetch_profile(token)

    logger.info("[2/3] Fetching media list (paginated)...")
    media_list = fetch_media_list(token)
    logger.info("  -> %d posts found", len(media_list))

    logger.info("[3/3] Fetching comments per post (this is the slow part)...")
    for i, post in enumerate(media_list, start=1):
        post["comments"] = fetch_comments(post["id"], token, debug=(i == 1))
        logger.info("  -> [%d/%d] %s: %d comments", i, len(media_list), post["id"], len(post["comments"]))
        time.sleep(RATE_LIMIT_SLEEP_SECONDS)

    dump = {
        "account_label": account_label,
        "pulled_at": pulled_at,
        "profile": profile,
        "media": media_list,
    }

    out_path = run_dir / f"dump_{pulled_at.replace(':', '-')}.json"
    out_path.write_text(json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Done. Raw dump cached at: %s", out_path)
    logger.info("Re-run the pipeline against this file offline — no need to re-hit the API.")
    return out_path


if __name__ == "__main__":
    from pipeline.exceptions import PipelineError
    from pipeline.logging_config import configure_logging

    configure_logging()

    parser = argparse.ArgumentParser()
    parser.add_argument("account_label", help="Local folder name for this vendor's runs/ and eval/golden/ files")
    parser.add_argument("--token-env", default="IG_ACCESS_TOKEN",
                         help="Env var holding this account's access token (default: IG_ACCESS_TOKEN) - "
                              "use a different name to ingest a second connected account without "
                              "overwriting the first one's token in .env")
    args = parser.parse_args()

    try:
        ingest_account(args.account_label, token_env=args.token_env)
    except PipelineError as exc:
        sys.exit(str(exc))