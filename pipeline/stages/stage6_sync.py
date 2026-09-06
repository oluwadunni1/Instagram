"""
Stage 6 - Sync diff logic. Pure Python, no LLM calls: compares a freshly
fetched post against its previously captured snapshot entry using content
hashes, perceptual image hashes, and caption embeddings computed elsewhere
(pipeline/media_fingerprint.py, scripts/run_build_snapshot.py).
"""

from __future__ import annotations

import hashlib

import numpy as np


def compute_content_hash(post: dict) -> str:
    """Stable hash of a post's non-URL content. CDN query params (oh=/oe=)
    rotate on every refresh even when nothing about the post itself
    changed, so the hash deliberately excludes media_url."""
    content = f"{post['caption']}|{post['comment_count']}|{len(post.get('comments', []))}"
    return hashlib.sha256(content.encode()).hexdigest()


def phash_hamming_distance(h1: str | None, h2: str | None) -> int:
    """Bit-difference between two pHash hex strings. 999 (never a match)
    if either side is missing."""
    if h1 is None or h2 is None:
        return 999
    return bin(int(h1, 16) ^ int(h2, 16)).count("1")


def find_phash_match(new_phash: str | None, snapshot: list[dict], threshold: int = 12) -> dict | None:
    """Nearest snapshot entry to new_phash by pHash Hamming distance, if
    under `threshold`, else None. The returned dict is the matched
    snapshot entry plus a "distance" key."""
    if not new_phash:
        return None

    best_entry = None
    best_distance = None
    for entry in snapshot:
        distance = phash_hamming_distance(new_phash, entry.get("image_phash"))
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_entry = entry

    if best_entry is not None and best_distance < threshold:
        return {**best_entry, "distance": best_distance}
    return None


def find_embedding_match(
    query_emb: list[float], emb_matrix: np.ndarray, post_ids: list[str], threshold: float = 0.92
) -> dict | None:
    """Nearest snapshot post to query_emb by cosine similarity of caption
    embeddings, if at/above `threshold`, else None."""
    q = np.array(query_emb)
    sims = emb_matrix @ q / (np.linalg.norm(emb_matrix, axis=1) * np.linalg.norm(q) + 1e-9)
    best_idx = int(np.argmax(sims))
    if sims[best_idx] >= threshold:
        return {"post_id": post_ids[best_idx], "score": float(sims[best_idx])}
    return None


def compute_lifecycle_state(post_type: str | None, comments: list[dict], vendor_handle: str) -> str | None:
    """Shared by run_build_snapshot.py (golden-set posts, post_type always
    known) and run_stage6.py (fresh/merged posts, where post_type is only
    known if the post already existed in a prior snapshot).

    Returns None for a post whose type is genuinely unclassified yet (a
    brand-new post Stage 2 hasn't run on) - asserting "active" for a post we
    don't even know is a product listing would be a guess, not a finding.
    """
    if post_type is None:
        return None
    if post_type != "product_listing":
        return "excluded"
    for comment in comments or []:
        if comment.get("username") == vendor_handle and "sold" in (comment.get("text") or "").lower():
            return "out_of_stock"
    return "active"


def diff_posts(fresh_post: dict, snapshot_entry: dict) -> dict:
    """Compares a post present in both the fresh fetch and the previous
    snapshot (same post_id). content_hash is the fast-path equality check;
    when it differs, reports which of caption/comment-count actually moved
    so the caller can decide what downstream work (Stage 3 re-run, Stage 4
    on the comment delta) is actually needed."""
    fresh_hash = compute_content_hash(fresh_post)
    if fresh_hash == snapshot_entry.get("content_hash"):
        return {"change_type": "no_op"}

    old_caption = snapshot_entry.get("caption")
    new_caption = fresh_post.get("caption")
    caption_changed = old_caption != new_caption

    old_count = snapshot_entry.get("comment_count") or 0
    new_count = fresh_post.get("comment_count") or 0
    comment_count_increased = new_count > old_count

    delta: dict = {
        "change_type": "modified",
        "caption_changed": caption_changed,
        "comment_count_increased": comment_count_increased,
    }
    evidence_parts = []
    if caption_changed:
        delta["old_caption"] = old_caption
        delta["new_caption"] = new_caption
        evidence_parts.append("caption text changed")
    if new_count != old_count:
        if comment_count_increased:
            delta["comment_delta"] = new_count - old_count
            evidence_parts.append(f"comment count {old_count} -> {new_count}")
        else:
            # A drop, not a delta worth surfacing as "new comments" - still
            # real and worth reporting (comment(s) removed/hidden upstream).
            evidence_parts.append(f"comment count decreased {old_count} -> {new_count}")
    if not evidence_parts:
        # content_hash differed but neither tracked field moved at all -
        # e.g. a comment was edited in place. Surface it rather than
        # silently reporting nothing changed.
        evidence_parts.append("content_hash changed but caption/comment_count did not move")
    delta["evidence"] = "; ".join(evidence_parts)
    return delta
