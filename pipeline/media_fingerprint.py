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


def compute_caption_embedding(caption: str | None, post_id: str = "") -> tuple[list[float], bool]:
    """Gemini text-embedding-004 embedding for `caption`
    (task_type=SEMANTIC_SIMILARITY). Returns (embedding, True) on success;
    a zero vector on an empty caption (True - deliberate, not a failure) or
    on an API error (False - logged as a failure so callers can report it).
    """
    if not caption:
        return [0.0] * EMBEDDING_DIM, True

    try:
        resp = requests.post(
            GEMINI_EMBED_URL,
            params={"key": GEMINI_API_KEY},
            json={
                "content": {"parts": [{"text": caption}]},
                "taskType": "SEMANTIC_SIMILARITY",
                "outputDimensionality": EMBEDDING_DIM,
            },
            timeout=EMBEDDING_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        values = resp.json()["embedding"]["values"]
        return values, True
    except Exception as exc:  # noqa: BLE001 - any failure degrades to a zero vector
        logger.warning("[embedding] %s: Gemini embedding call failed: %s", post_id, exc)
        return [0.0] * EMBEDDING_DIM, False
    finally:
        time.sleep(EMBEDDING_SLEEP_SECONDS)
