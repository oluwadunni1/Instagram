"""
Local OCR over post images, the free tier of the vision cascade.

WHY THIS EXISTS. Vision is 8.5% of calls across the whole token log but 22.6%
of all spend, and on the hybrid's gadgets run five vision calls were 86% of the
bill. Every one of those five posts is caption-less with the product and price
printed on the picture, which is exactly what classic OCR is for.

A spike over those five read the product name on 5/5 and the price on 5/5, so
this is wired on measured evidence rather than hope. Brief section 5 asked for
the comparison ("classic OCR first (PaddleOCR/Tesseract) with an LLM
interpreting the OCR text, vs. sending the image straight to a cheap vision
model") and it had never been run.

THREE THINGS THE SPIKE FOUND THAT SHAPE THIS MODULE:

  - OCR text carries character-level noise. "IPH0NE" and "C0RE3" both came
    back with a zero for the letter O. Anything consuming this text must
    tolerate that, which is why it goes to a model rather than a regex.
  - Distractor numbers are everywhere: an IMEI (352142373198245), model codes
    (A3288S), random digits. A "longest digit run is the price" heuristic
    would confidently return an IMEI.
  - One card read "N500.000", a period as the thousands separator. float()
    on that gives 500, not 500000 - a 1000x price error, and the brief calls a
    hallucinated price the one unforgivable failure mode.

So this module deliberately returns RAW TEXT and interprets nothing. The
interpretation belongs to a model downstream.

OPTIONAL DEPENDENCY. rapidocr-onnxruntime is heavy (~100MB of ONNX models) and
this POC must still run on a fresh clone without it. The engine is imported
lazily and every failure degrades to "no text", which callers treat as "escalate
to vision" - the behaviour that existed before OCR.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 20
IMAGE_DOWNLOAD_SLEEP_SECONDS = 0.3

# Below this many recognised characters, treat the read as failed and let the
# caller escalate to vision. A handful of stray glyphs off a busy photo is not
# a product card, and feeding that to a model wastes a call to answer nothing.
DEFAULT_MIN_CHARS = 12

_engine = None
_engine_lock = threading.Lock()
_engine_unavailable = False


def _get_engine():
    """The RapidOCR singleton, or None if the package is not installed.

    Loading the models costs seconds and megabytes, so it happens once per
    process and only when OCR is actually reached. A missing package is logged
    once and then remembered, because a harness run would otherwise log it per
    post.
    """
    global _engine, _engine_unavailable
    if _engine is not None or _engine_unavailable:
        return _engine
    with _engine_lock:
        if _engine is not None or _engine_unavailable:
            return _engine
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            _engine_unavailable = True
            logger.warning(
                "[ocr] rapidocr-onnxruntime is not installed - every OCR attempt will "
                "report no text and the caller will escalate to vision as before. "
                "Install it with `uv sync --extra ocr` to enable the cheap tier."
            )
            return None
        logger.info("[ocr] loading OCR models (first use downloads ~100MB)")
        _engine = RapidOCR()
    return _engine


def cache_path(account_label: str, post_id: str) -> Path:
    """Where one post's OCR text is cached.

    Keyed by post_id rather than image URL on purpose: CDN URLs carry rotating
    oh=/oe= params and expire in days, so a URL-keyed cache would miss on every
    refresh. The post id is stable forever.

    Cached under runs/, which is committed, so the text survives the CDN links
    that produced it. That matters more than it sounds: the README records that
    third parties cannot refresh expired links and therefore cannot reproduce
    any vision result. A committed OCR cache makes the caption-less posts
    reproducible from a clone for the first time.
    """
    return Path("runs") / account_label / "ocr" / f"{post_id}.txt"


def read_cached(account_label: str | None, post_id: str) -> str | None:
    if not account_label or not post_id:
        return None
    path = cache_path(account_label, post_id)
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[ocr] %s: unreadable cache: %s", post_id, exc)
        return None


def write_cache(account_label: str | None, post_id: str, text: str) -> None:
    if not account_label or not post_id:
        return
    path = cache_path(account_label, post_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        logger.warning("[ocr] %s: could not write cache: %s", post_id, exc)


def extract_text(
    image_url: str | None,
    post_id: str = "",
    account_label: str | None = None,
    min_chars: int = DEFAULT_MIN_CHARS,
    use_cache: bool = True,
) -> tuple[str | None, str]:
    """Recognised text from one image. Returns (text_or_None, reason).

    `text` is None whenever the caller should escalate to vision instead: no
    URL, engine missing, download failed, undecodable image, or too little text
    recognised to be worth a model call. `reason` always says which, so the
    predictions file can show why a post took the path it took.

    Pass image_url from pipeline/types.py::vision_image_url(), never
    post["media_url"] - a Reel's media_url is the .mp4 itself.

    Interprets nothing. See the module docstring for why raw text is the
    contract.
    """
    if not image_url:
        return None, "no image url"

    if use_cache:
        cached = read_cached(account_label, post_id)
        if cached is not None:
            if len(cached.strip()) < min_chars:
                return None, f"cached, too little text ({len(cached.strip())} chars)"
            return cached, "cache hit"

    engine = _get_engine()
    if engine is None:
        return None, "ocr engine not installed"

    try:
        resp = requests.get(image_url, timeout=IMAGE_DOWNLOAD_TIMEOUT_SECONDS)
        resp.raise_for_status()
        blob = resp.content
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        # 403 here is the documented CDN link-rot failure, not a bug.
        hint = " (expired CDN link - run scripts/refresh_media_urls.py)" if status == 403 else ""
        logger.warning("[ocr] %s: download failed HTTP %s%s", post_id, status, hint)
        return None, f"download HTTP {status}"
    except requests.RequestException as exc:
        logger.warning("[ocr] %s: download failed: %s", post_id, type(exc).__name__)
        return None, f"download {type(exc).__name__}"
    finally:
        time.sleep(IMAGE_DOWNLOAD_SLEEP_SECONDS)

    try:
        # Raw bytes, NOT BytesIO. RapidOCR accepts str/Path/bytes/ndarray/
        # PIL.Image and rejects a file object with LoadImageError - which the
        # spike never hit, because it passed a path.
        result, _elapsed = engine(blob)
        lines = [str(item[1]) for item in (result or [])]
    except Exception as exc:  # noqa: BLE001 - a bad image must not kill a run
        logger.warning("[ocr] %s: OCR failed: %s: %s", post_id, type(exc).__name__, exc)
        return None, f"ocr {type(exc).__name__}"

    text = "\n".join(lines).strip()
    if use_cache:
        # Cache even a short read: re-downloading to rediscover that an image
        # has no text is the most wasteful thing this module could do.
        write_cache(account_label, post_id, text)

    if len(text) < min_chars:
        return None, f"too little text ({len(text)} chars)"

    logger.debug("[ocr] %s: recognised %d chars across %d lines", post_id, len(text), len(lines))
    return text, f"ok ({len(text)} chars)"
