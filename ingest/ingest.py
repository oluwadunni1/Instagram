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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

from pipeline.exceptions import MissingCredentialsError
from pipeline.settings import (
    get_ig_access_token,
    ig_auth_headers,
    redact_tokens,
    strip_url_credentials,
)

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.instagram.com"
API_VERSION = "v26.0"  # bump as Meta ships new versions; check current in their docs
RATE_LIMIT_SLEEP_SECONDS = 2  # gentle default pause between paginated calls
MAX_RETRIES = 5

CAPTION_SNIPPET_CHARS = 52


def _caption_snippet(caption: str | None, width: int = CAPTION_SNIPPET_CHARS) -> str:
    """Single-line, length-capped caption for one console trace line.

    Captions are multi-line and can run for paragraphs, so the newlines have to
    collapse or one post takes over the whole screen.
    """
    text = " ".join((caption or "").split())
    if not text:
        return "(no caption)"
    return text if len(text) <= width else text[: width - 1] + "…"


def _media_label(post: dict) -> str:
    """Short display label for a post's media kind.

    A Reel is media_type=VIDEO with media_product_type=REELS rather than its own
    media_type, and the distinction is worth showing: Reels are the posts whose
    media_url is a video file, so they are the ones that depend on thumbnail_url
    for anything that needs a still image (see pipeline/types.py).
    """
    if post.get("media_product_type") == "REELS":
        return "REEL"
    media_type = post.get("media_type") or "?"
    return "CAROUSEL" if media_type == "CAROUSEL_ALBUM" else media_type


def log_ingest_summary(media_list: list[dict], out_path: Path) -> None:
    """Stage 0 summary block.

    Mirrors the per-stage summaries scripts/run_pipeline.py --live prints, so an
    onboarding run reads as one continuous story from raw pull to routed catalog.
    """
    total = len(media_list)
    kinds = Counter(_media_label(post) for post in media_list)
    with_caption = sum(1 for post in media_list if (post.get("caption") or "").strip())
    comments = sum(len(post.get("comments") or []) for post in media_list)
    stamps = sorted(post["timestamp"] for post in media_list if post.get("timestamp"))

    logger.info("")
    logger.info("=== Stage 0: ingest complete (zero AI calls) ===")
    logger.info("  Posts pulled       : %d", total)
    if stamps:
        logger.info("  Date range         : %s -> %s", stamps[0][:10], stamps[-1][:10])
    logger.info("  Media types        : %s",
                ", ".join(f"{kind} {count}" for kind, count in kinds.most_common()) or "none")
    logger.info("  Captions           : %d with text, %d blank", with_caption, total - with_caption)
    logger.info("  Comments retrieved : %d", comments)
    if total and comments == 0:
        # Every post, every account - see CLAUDE.md. Saying so here means the
        # zeros in the trace above are explained before anyone has to ask.
        logger.info("    Comment TEXT is withheld by Meta until instagram_business_manage_comments")
        logger.info("    clears App Review (the app is in Development mode). Counts are real, text")
        logger.info("    is not available - so Stage 4 reads captions only on this account.")
    logger.info("  Raw dump           : %s", out_path)
    logger.info("")


def _get(url: str, params: dict, token: str) -> dict:
    """GET with basic 429/5xx backoff.

    The token goes in an Authorization header, never the query string - a
    token in the URL leaks into urllib3's DEBUG log line and into requests'
    exception messages (see pipeline/settings.py::ig_auth_headers).
    """
    headers = ig_auth_headers(token)
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = RATE_LIMIT_SLEEP_SECONDS * (2 ** (attempt - 1))
            logger.warning("[backoff] %s on %s - retry %d/%d in %ds",
                            resp.status_code, redact_tokens(url), attempt, MAX_RETRIES, wait)
            time.sleep(wait)
            continue
        # Non-retryable error — surface it immediately. Both the URL and the
        # response body are redacted: a paginated `next` URL echoed back by
        # the API can itself carry the token.
        raise RuntimeError(f"Request failed ({resp.status_code}): {redact_tokens(resp.text)}")
    raise RuntimeError(f"Gave up after {MAX_RETRIES} retries: {redact_tokens(url)}")


def _next_page_url(data: dict, context: str) -> str | None:
    """The `paging.next` URL to follow, with any credential query param
    stripped.

    Pagination follows Graph's own URL verbatim, so if Meta echoes an
    `access_token=` param into it, the follow-up request carries the token
    in its query string even though the first call used a header. Stripping
    it costs nothing (the Authorization header still authenticates) and the
    log line below answers, on the next real run, whether Graph does this -
    an open question as of 2026-09-08, since raw dumps only persist the
    merged media list, not the paging envelope. Logs the fact, never the URL.
    """
    next_url = (data.get("paging") or {}).get("next")
    if not next_url:
        return None
    clean_url, had_credential = strip_url_credentials(next_url)
    if had_credential:
        logger.warning(
            "[paging] %s: Graph echoed a credential query param into paging.next; "
            "stripped it before following. Header auth still applies. "
            "Record this in FINDINGS.md - it resolves the 2026-09-08 open question.",
            context,
        )
    return clean_url


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
        url = _next_page_url(data, "me/media")
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
        url = _next_page_url(data, f"{media_id}/comments")
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
        logger.info(
            "  [%d/%d]  %s  %-8s  %-*s  %d comments",
            i, len(media_list), post["id"], _media_label(post),
            CAPTION_SNIPPET_CHARS, _caption_snippet(post.get("caption")),
            len(post["comments"]),
        )
        time.sleep(RATE_LIMIT_SLEEP_SECONDS)

    dump = {
        "account_label": account_label,
        "pulled_at": pulled_at,
        "profile": profile,
        "media": media_list,
    }

    out_path = run_dir / f"dump_{pulled_at.replace(':', '-')}.json"
    out_path.write_text(json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8")
    log_ingest_summary(media_list, out_path)
    logger.info("Re-run the pipeline against this file offline — no need to re-hit the API.")
    logger.info("Next: uv run scripts/run_pipeline.py --account %s --live", account_label)
    return out_path


if __name__ == "__main__":
    from pipeline.exceptions import PipelineError
    from pipeline.logging_config import configure_logging

    # Both streams, before anything logs: the per-post trace prints caption text,
    # and a caption with an emoji raises UnicodeEncodeError on a cp1252 Windows
    # console mid-run. configure_logging() writes to stderr, so stdout alone is
    # not enough here.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # pragma: no cover - non-reconfigurable stream
            pass

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