"""
Shared image-pHash and caption-embedding helpers for Stage 6 sync.

Both scripts/run_build_snapshot.py (builds the baseline snapshot) and
scripts/run_stage6.py (matches fresh posts against it) need to compute
these identically, or find_phash_match()/find_embedding_match() in
pipeline/stages/stage6_sync.py end up comparing incompatible values -
kept in one place instead of duplicated per script.
"""

from __future__ import annotations

import logging
import os
import time
from io import BytesIO

import imagehash
import requests
from PIL import Image

import pipeline.settings  # noqa: F401 - import side effect: load_dotenv()
from pipeline.llm_client import log_token_usage, throttle
from pipeline.settings import gemini_auth_headers, redact_tokens

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# models/text-embedding-004 (the model named in the original spec) is retired -
# confirmed via GET v1beta/models for this key, which lists only
# gemini-embedding-001/-2/-2-preview as embedContent-capable. gemini-embedding-001
# is the direct successor. Its default output is 3072-dim, so
# outputDimensionality=768 is required below to match this pipeline's 768-dim
# vectors (see EMBEDDING_DIM) - confirmed via a live probe of both dimensions.
GEMINI_EMBED_MODEL = "gemini-embedding-001"
GEMINI_EMBED_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_EMBED_MODEL}:embedContent"
EMBEDDING_DIM = 768

IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 10
IMAGE_DOWNLOAD_SLEEP_SECONDS = 0.2
EMBEDDING_TIMEOUT_SECONDS = 15
EMBEDDING_SLEEP_SECONDS = 0.3


def compute_image_phash(media_url: str | None, post_id: str = "") -> tuple[str | None, str | None]:
    """Downloads media_url and returns (phash_hex, None) on success, or
    (None, reason) on any failure - a missing pHash must never crash a
    snapshot/sync run, just get logged and skipped."""
    if not media_url:
        reason = "no media_url"
        logger.warning("[phash] %s: %s", post_id, reason)
        return None, reason

    try:
        resp = requests.get(media_url, timeout=IMAGE_DOWNLOAD_TIMEOUT_SECONDS)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content))
        return str(imagehash.phash(img)), None
    except requests.Timeout:
        reason = "timeout"
        logger.warning("[phash] %s: download timed out", post_id)
        return None, reason
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        reason = f"HTTP {status}"
        logger.warning("[phash] %s: %s", post_id, reason)
        return None, reason
    except requests.RequestException as exc:
        reason = f"request error: {exc}"
        logger.warning("[phash] %s: %s", post_id, reason)
        return None, reason
    except Exception as exc:  # noqa: BLE001 - corrupt/undecodable image, etc.
        reason = f"undecodable image: {exc}"
        logger.warning("[phash] %s: %s", post_id, reason)
        return None, reason
    finally:
        time.sleep(IMAGE_DOWNLOAD_SLEEP_SECONDS)


def compute_caption_embedding(
    caption: str | None,
    post_id: str = "",
    run_id: str | None = None,
    vendor_id: str | None = None,
) -> tuple[list[float], bool]:
    """Gemini embedding for `caption` (task_type=SEMANTIC_SIMILARITY).
    Returns (embedding, True) on success; a zero vector on an empty caption
    (True - deliberate, not a failure) or on an API error (False - logged as a
    failure so callers can report it).

    Pass run_id/vendor_id to get the call into report/token_log.csv as a
    stage6_embedding row. Stage 6's provider calls were invisible before that:
    there were zero embedding rows in the log, so a sync's real cost had never
    been recorded and the "a sync on an unchanged account costs about $0" claim
    could not be checked against the log every other figure in this project is
    checked against.
    """
    if not caption:
        return [0.0] * EMBEDDING_DIM, True

    try:
        # Bypasses llm_client.complete_structured(), so it must throttle itself -
        # this spends the same Gemini per-minute quota as every other call, and a
        # snapshot build makes one of these per post back to back. Same reason the
        # vision Pass B calls throttle themselves (CLAUDE.md).
        throttle()
        # The key goes in an x-goog-api-key header, never `?key=` - requests
        # embeds the full request URL in HTTPError, and this call's failure
        # path logs at WARNING (visible at any LOG_LEVEL), so a quota 429
        # here used to print the live Gemini key. See README.md.
        resp = requests.post(
            GEMINI_EMBED_URL,
            headers=gemini_auth_headers(GEMINI_API_KEY),
            json={
                "content": {"parts": [{"text": caption}]},
                "taskType": "SEMANTIC_SIMILARITY",
                "outputDimensionality": EMBEDDING_DIM,
            },
            timeout=EMBEDDING_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        values = resp.json()["embedding"]["values"]
        _log_embedding_call(resp, post_id=post_id, run_id=run_id, vendor_id=vendor_id)
        return values, True
    except Exception as exc:  # noqa: BLE001 - any failure degrades to a zero vector
        # redact_tokens(exc), not the bare exception: defence in depth for
        # anything that still manages to carry a credential into an error.
        logger.warning("[embedding] %s: Gemini embedding call failed: %s", post_id, redact_tokens(exc))
        return [0.0] * EMBEDDING_DIM, False
    finally:
        time.sleep(EMBEDDING_SLEEP_SECONDS)


def _log_embedding_call(resp, *, post_id: str, run_id: str | None, vendor_id: str | None) -> None:
    """One report/token_log.csv row per successful embedding call.

    embedContent has no usage block in its response the way a chat completion
    does, so the token counts are best-effort and usually zero. That is fine -
    the row's EXISTENCE is the point. It is what makes a sync's call count, and
    therefore its cost, visible at all; gemini-embedding-001 has no rate on
    file either way (see MODEL_RATES in pipeline/llm_client.py).
    """
    if run_id is None:
        return
    usage = {}
    try:
        usage = resp.json().get("usageMetadata") or {}
    except Exception:  # noqa: BLE001 - a missing/odd usage block must never fail a snapshot build
        pass
    prompt_tokens = usage.get("promptTokenCount", 0) or 0
    log_token_usage(
        run_id=run_id,
        vendor_id=vendor_id or "unknown",
        post_id=post_id,
        stage="stage6_embedding",
        model=GEMINI_EMBED_MODEL,
        prompt_tokens=prompt_tokens,
        completion_tokens=0,
        total_tokens=usage.get("totalTokenCount", prompt_tokens) or prompt_tokens,
    )
