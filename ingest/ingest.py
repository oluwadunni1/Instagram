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
    2. python ingest.py <account_label>

    account_label is a folder name YOU choose to identify this
    vendor account locally (e.g. "vendor_fashion_01"). It does not
    need to match their Instagram username — this keeps your local
    run folders organized by category/number instead of by handle,
    which is convenient once you have several test accounts and
    also avoids scattering vendor usernames across your filesystem
    and logs unnecessarily (comments already carry real usernames,
    per §11 — no need to add more surface area than the API itself
    requires).

Notes:
  - Uses graph.instagram.com per the "Instagram API with Instagram
    Login" product (not graph.facebook.com).
  - Respects the ~200 calls/user/hour business-use-case rate limit
    with basic 429 backoff.
  - Comments contain real people's handles/text — treated as
    sensitive. This script writes raw dumps to runs/, which must
    stay out of git (see .gitignore note at bottom of this file).
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()  # reads .env in the working directory into os.environ

GRAPH_BASE = "https://graph.instagram.com"
API_VERSION = "v23.0"  # bump as Meta ships new versions; check current in their docs
RATE_LIMIT_SLEEP_SECONDS = 2  # gentle default pause between paginated calls
MAX_RETRIES = 5

ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN")


def _get(url: str, params: dict) -> dict:
    """GET with basic 429/5xx backoff."""
    params = {**params, "access_token": ACCESS_TOKEN}
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = RATE_LIMIT_SLEEP_SECONDS * (2 ** (attempt - 1))
            print(f"  [backoff] {resp.status_code} on {url} — retry {attempt}/{MAX_RETRIES} in {wait}s")
            time.sleep(wait)
            continue
        # Non-retryable error — surface it immediately
        raise RuntimeError(f"Request failed ({resp.status_code}): {resp.text}")
    raise RuntimeError(f"Gave up after {MAX_RETRIES} retries: {url}")


def fetch_profile() -> dict:
    fields = "id,username,name,biography,account_type,media_count"
    return _get(f"{GRAPH_BASE}/{API_VERSION}/me", {"fields": fields})


def fetch_media_list() -> list[dict]:
    """Paginate through /me/media."""
    fields = "id,caption,timestamp,media_type,media_url,permalink,children{media_type,media_url}"
    url = f"{GRAPH_BASE}/{API_VERSION}/me/media"
    params = {"fields": fields, "limit": 50}
    all_media = []
    while url:
        data = _get(url, params)
        all_media.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        url = next_url
        params = {}  # next_url already carries all query params
        if url:
            time.sleep(RATE_LIMIT_SLEEP_SECONDS)
    return all_media


def fetch_comments(media_id: str) -> list[dict]:
    """Paginate through comments for a single media item."""
    fields = "id,text,username,timestamp,like_count"
    url = f"{GRAPH_BASE}/{API_VERSION}/{media_id}/comments"
    params = {"fields": fields, "limit": 50}
    all_comments = []
    while url:
        try:
            data = _get(url, params)
        except RuntimeError as e:
            # Comments can be disabled on some posts — don't kill the whole run
            print(f"  [warn] comments fetch failed for {media_id}: {e}")
            break
        all_comments.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        url = next_url
        params = {}
        if url:
            time.sleep(RATE_LIMIT_SLEEP_SECONDS)
    return all_comments


def ingest_account(account_label: str) -> Path:
    if not ACCESS_TOKEN:
        raise SystemExit("Set IG_ACCESS_TOKEN in your environment first.")

    run_dir = Path("runs") / account_label / "raw"
    run_dir.mkdir(parents=True, exist_ok=True)
    pulled_at = datetime.now(timezone.utc).isoformat()

    print(f"[1/3] Fetching profile for '{account_label}'...")
    profile = fetch_profile()

    print("[2/3] Fetching media list (paginated)...")
    media_list = fetch_media_list()
    print(f"  -> {len(media_list)} posts found")

    print("[3/3] Fetching comments per post (this is the slow part)...")
    for i, post in enumerate(media_list, start=1):
        post["comments"] = fetch_comments(post["id"])
        print(f"  -> [{i}/{len(media_list)}] {post['id']}: {len(post['comments'])} comments")
        time.sleep(RATE_LIMIT_SLEEP_SECONDS)

    dump = {
        "account_label": account_label,
        "pulled_at": pulled_at,
        "profile": profile,
        "media": media_list,
    }

    out_path = run_dir / f"dump_{pulled_at.replace(':', '-')}.json"
    out_path.write_text(json.dumps(dump, indent=2, ensure_ascii=False))
    print(f"\nDone. Raw dump cached at: {out_path}")
    print("Re-run the pipeline against this file offline — no need to re-hit the API.")
    return out_path


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python ingest.py <account_label>")
        sys.exit(1)
    ingest_account(sys.argv[1])