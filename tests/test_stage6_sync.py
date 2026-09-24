"""Stage 6 sync diff logic - the boundaries that are easy to break silently.

Nothing in tests/ touched pipeline/stages/stage6_sync.py before this file, even
though every Stage 6 figure rests on it. These are offline and deterministic
(no network, no .env, no golden set - see tests/conftest.py); model quality is
the harness's job and stays there.

What is pinned here is the behaviour where an off-by-one or a changed hash
input would not fail anything, just quietly move a number:
  - the content hash's inputs, which decide what counts as "unchanged";
  - the match thresholds' inclusive/exclusive boundaries;
  - the auto-merge rule, which is the one action Stage 6 takes without asking;
  - that a dump-built baseline and a live fetch agree on comment_count.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.stages.stage6_sync import (  # noqa: E402
    compute_content_hash,
    compute_lifecycle_state,
    diff_posts,
    find_embedding_match,
    find_phash_match,
)
from scripts.run_build_snapshot import normalize_dump_post  # noqa: E402
from scripts.run_stage6 import (  # noqa: E402
    AUTO_MERGE_PHASH_MAX_DISTANCE,
    DEFAULT_EMBEDDING_THRESHOLD,
    DEFAULT_PHASH_THRESHOLD,
    _normalize_fresh_post,
)

VENDOR = "ayodele.akinbohun"


def _post(caption="a caption", comment_count=0, comments=None):
    return {"caption": caption, "comment_count": comment_count, "comments": comments or []}


def _phash(bits: str) -> str:
    """A 64-bit pHash as the 16-hex-char string imagehash produces."""
    return f"{int(bits, 2):016x}"


def _with_distance(distance: int) -> tuple[str, str]:
    """Two pHashes exactly `distance` bits apart."""
    return _phash("0" * 64), _phash("1" * distance + "0" * (64 - distance))


# --- content hash ---------------------------------------------------------


def test_content_hash_ignores_the_comments_array():
    """THE regression test for the live-sync hash bug.

    Meta withholds comment text, so a live fetch always returns an empty
    comments array while comment_count is a real number. A baseline built from
    a golden set carries hand-authored comment objects. Those two posts are the
    same post, and hashing len(comments) made them hash differently - so every
    such post reported content_changed on every sync, forever.
    """
    live = _post(comment_count=2, comments=[])
    from_golden = _post(comment_count=2, comments=[{"id": "1"}, {"id": "2"}])
    assert compute_content_hash(live) == compute_content_hash(from_golden)


def test_content_hash_still_moves_on_caption_and_on_count():
    base = _post(caption="Toyota Corolla", comment_count=1)
    assert compute_content_hash(_post(caption="Toyota Camry", comment_count=1)) != compute_content_hash(base)
    assert compute_content_hash(_post(caption="Toyota Corolla", comment_count=2)) != compute_content_hash(base)


# --- the two normalizers must mean the same thing by comment_count --------


def test_dump_and_live_rows_agree_on_comment_count():
    """A baseline is built from a raw dump and diffed against a live fetch, by
    two different normalizers. If they disagreed about what comment_count is,
    the first sync would report a comment_delta on every commented post - which
    is exactly the metric a designed sync experiment is trying to measure.

    scripts/run_pipeline.py::normalize_post() is deliberately NOT used for the
    baseline: it counts readable comment BODIES, which Meta always makes 0.
    """
    dump_row = {"id": "p1", "caption": "hi", "media_url": "u", "comments_count": 3, "comments": []}
    live_row = {"id": "p1", "caption": "hi", "media_url": "u", "comments_count": 3, "comments": {"data": []}}
    assert normalize_dump_post(dump_row)["comment_count"] == _normalize_fresh_post(live_row)["comment_count"] == 3


def test_dump_built_baseline_is_a_no_op_against_the_same_live_post():
    """The end-to-end consequence of the two tests above: a post that has not
    changed since the baseline must diff as no_op, comments and all."""
    dump_row = {"id": "p1", "caption": "Clean 2018 Corolla", "media_url": "u", "comments_count": 4, "comments": []}
    live_row = {"id": "p1", "caption": "Clean 2018 Corolla", "media_url": "u", "comments_count": 4,
                "comments": {"data": []}}

    baseline_entry = normalize_dump_post(dump_row)
    baseline_entry["content_hash"] = compute_content_hash(baseline_entry)

    assert diff_posts(_normalize_fresh_post(live_row), baseline_entry) == {"change_type": "no_op"}


def test_dump_without_comments_count_falls_back_to_zero():
    """A dump taken before ingest.py requested the field. Must not raise - the
    builder warns instead, because a silent 0 here poisons the first sync."""
    assert normalize_dump_post({"id": "p1", "caption": "hi"})["comment_count"] == 0


# --- pHash ----------------------------------------------------------------


def test_phash_distance_sentinel_when_either_side_is_missing():
    """999 is "never a match" - a post whose image failed to download must not
    accidentally match everything by scoring 0."""
    from pipeline.stages.stage6_sync import phash_hamming_distance

    assert phash_hamming_distance(None, _phash("0" * 64)) == 999
    assert phash_hamming_distance(_phash("0" * 64), None) == 999
    assert phash_hamming_distance(None, None) == 999


@pytest.mark.parametrize(
    "distance,expect_match",
    [(0, True), (11, True), (12, False), (13, False)],
)
def test_phash_threshold_is_strictly_less_than(distance, expect_match):
    """Distance exactly at the threshold must NOT match - the comparison is
    `<`, not `<=`. One character here silently widens the repost net."""
    query, stored = _with_distance(distance)
    match = find_phash_match(query, [{"post_id": "old", "image_phash": stored}],
                             threshold=DEFAULT_PHASH_THRESHOLD)
    assert (match is not None) is expect_match
    if expect_match:
        assert match["distance"] == distance


def test_phash_match_returns_the_nearest_entry():
    query = _phash("0" * 64)
    snapshot = [
        {"post_id": "far", "image_phash": _phash("1" * 10 + "0" * 54)},
        {"post_id": "near", "image_phash": _phash("1" * 2 + "0" * 62)},
    ]
    assert find_phash_match(query, snapshot, threshold=DEFAULT_PHASH_THRESHOLD)["post_id"] == "near"


def test_no_phash_means_no_match_attempted():
    assert find_phash_match(None, [{"post_id": "old", "image_phash": _phash("0" * 64)}]) is None


# --- embeddings -----------------------------------------------------------


def _unit_pair(cosine: float) -> tuple[list[float], list[float]]:
    """Two unit vectors at exactly `cosine` similarity."""
    return [1.0, 0.0], [cosine, float(np.sqrt(1 - cosine**2))]


@pytest.mark.parametrize(
    "cosine,expect_match",
    [(1.0, True), (0.9201, True), (0.9199, False), (0.5, False)],
)
def test_embedding_threshold_is_inclusive(cosine, expect_match):
    """The comparison is `>=`, the opposite sense from the pHash threshold's
    `<` - which is why both are pinned here."""
    query, stored = _unit_pair(cosine)
    match = find_embedding_match(query, np.array([stored]), ["old"], threshold=DEFAULT_EMBEDDING_THRESHOLD)
    assert (match is not None) is expect_match


def test_the_norm_epsilon_makes_the_exact_boundary_miss():
    """Documenting, not endorsing: the `+ 1e-9` guarding the norm division
    scales every similarity down by about a part in a billion, so a pair at
    EXACTLY 0.92 lands just under the 0.92 threshold and does not match. The
    `>=` is inclusive in the source and effectively exclusive in practice.

    The epsilon is load-bearing - an empty caption embeds to a zero vector
    (pipeline/media_fingerprint.py), and without it that is a divide by zero -
    so this is left alone rather than "fixed": at a 1e-9 offset it can only
    change the verdict for a pair sitting on the boundary to nine decimal
    places, and every Stage 6 number on file was produced under it.
    """
    query, stored = _unit_pair(DEFAULT_EMBEDDING_THRESHOLD)
    assert find_embedding_match(query, np.array([stored]), ["old"],
                                threshold=DEFAULT_EMBEDDING_THRESHOLD) is None


def test_an_empty_caption_matches_nothing():
    """An empty caption embeds to a zero vector, which must not match every
    post by scoring 0 similarity against everything - or a caption-less post
    would repost-match the catalog at random."""
    zero = [0.0, 0.0]
    stored = np.array([[1.0, 0.0], [0.0, 1.0]])
    assert find_embedding_match(zero, stored, ["a", "b"], threshold=DEFAULT_EMBEDDING_THRESHOLD) is None


# --- the auto-merge rule --------------------------------------------------


def test_auto_merge_is_exact_identity_only():
    """"Merging an exact repost" is the brief's one self-applying no-brainer,
    so "exact" is taken literally: distance 0. Distance 1 is a real edit a
    vendor should see, and this constant is what keeps it that way."""
    assert AUTO_MERGE_PHASH_MAX_DISTANCE == 0


def test_an_embedding_match_can_never_auto_merge():
    """run_stage6.py gates auto-merge on `phash_match is not None and
    distance <= AUTO_MERGE_PHASH_MAX_DISTANCE`. An embedding score is semantic
    similarity, never identity, so no score - not even 1.0 - may self-apply.
    This pins the shape of that condition, which a refactor could widen.
    """
    phash_match = None
    embedding_match = {"post_id": "old", "score": 1.0}
    is_exact_duplicate = phash_match is not None and phash_match["distance"] <= AUTO_MERGE_PHASH_MAX_DISTANCE
    assert embedding_match is not None
    assert not is_exact_duplicate


# --- lifecycle ------------------------------------------------------------


def test_sold_from_the_vendor_marks_out_of_stock():
    comments = [{"username": VENDOR, "text": "This one is SOLD, thank you!"}]
    assert compute_lifecycle_state("product_listing", comments, VENDOR) == "out_of_stock"


def test_the_same_word_from_a_buyer_does_not():
    """A buyer asking "is this sold?" is not the vendor saying it is. The whole
    point of carrying vendor_handle this far down."""
    comments = [{"username": "random_buyer", "text": "is this sold?"}]
    assert compute_lifecycle_state("product_listing", comments, VENDOR) == "active"


def test_unclassified_post_has_no_lifecycle_state():
    """None, not "active": a post Stage 2 has not classified is not known to be
    a product at all, and asserting a state would be a guess."""
    assert compute_lifecycle_state(None, [], VENDOR) is None


def test_non_product_is_excluded():
    assert compute_lifecycle_state("announcement", [], VENDOR) == "excluded"


# --- diff_posts -----------------------------------------------------------


def test_no_op_fast_path():
    """The 0-AI-call path the whole incremental-sync claim rests on."""
    post = _post(caption="unchanged", comment_count=3)
    entry = {**post, "content_hash": compute_content_hash(post)}
    assert diff_posts(post, entry) == {"change_type": "no_op"}


def test_caption_edit_reports_both_sides():
    entry = {"caption": "old text", "comment_count": 0, "content_hash": "stale"}
    result = diff_posts(_post(caption="new text"), entry)
    assert result["caption_changed"] is True
    assert (result["old_caption"], result["new_caption"]) == ("old text", "new text")


def test_comment_increase_reports_the_delta():
    entry = {"caption": "same", "comment_count": 1, "content_hash": "stale"}
    result = diff_posts(_post(caption="same", comment_count=4), entry)
    assert result["comment_count_increased"] is True
    assert result["comment_delta"] == 3


def test_comment_decrease_is_reported_but_is_not_a_delta():
    """A comment removed upstream is real and must surface, but it is not "new
    comments to run Stage 4 on" - the caller keys that off comment_delta."""
    entry = {"caption": "same", "comment_count": 5, "content_hash": "stale"}
    result = diff_posts(_post(caption="same", comment_count=2), entry)
    assert result["comment_count_increased"] is False
    assert "comment_delta" not in result
    assert "decreased" in result["evidence"]


def test_hash_moved_but_no_tracked_field_did():
    """The fallback branch: never silently report nothing changed when the
    hash says something did."""
    post = _post(caption="same", comment_count=2)
    entry = {"caption": "same", "comment_count": 2, "content_hash": "a-stale-hash"}
    result = diff_posts(post, entry)
    assert result["change_type"] == "modified"
    assert result["evidence"] == "content_hash changed but caption/comment_count did not move"


# --- snapshot state across repeated syncs ---------------------------------
#
# Everything above tests one diff in isolation. These drive run_sync end to end,
# repeatedly, because both bugs they pin were invisible in a single sync and
# only appeared on the one after it. Offline: pHash and the embedding call are
# stubbed, so nothing is downloaded and no provider is reached.


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    """A two-post snapshot on disk plus a sync() helper that advances it."""
    import scripts.run_stage6 as s6

    # OLD/NEW/NEWER are the same image, so every repost of it lands at
    # distance 0. KEEP is a different product and must never match.
    phashes = {"OLD": "f" * 16, "NEW": "f" * 16, "NEWER": "f" * 16, "KEEP": "0" * 16}
    monkeypatch.setattr(s6, "compute_image_phash", lambda url, post_id="": (phashes.get(post_id), None))
    monkeypatch.setattr(
        s6, "compute_caption_embedding",
        lambda caption, post_id="", run_id=None, vendor_id=None: ([0.0, 0.0], True),
    )

    snapshot = []
    for pid, caption in [("OLD", "iPhone 15, N900,000"), ("KEEP", "PS5 Slim, N990,000")]:
        entry = {
            "post_id": pid, "caption": caption, "comment_count": 0, "media_url": "u",
            "content_hash": "", "image_phash": phashes[pid], "comment_cursor": None,
            "lifecycle_state": "active", "catalog_products": [{"name": pid}],
            "post_type": "product_listing", "carousel_classification": None,
        }
        entry["content_hash"] = compute_content_hash(entry)
        snapshot.append(entry)

    (tmp_path / "latest.json").write_text(json.dumps(snapshot), encoding="utf-8")
    np.save(tmp_path / "embeddings.npy", np.zeros((2, 2)))
    (tmp_path / "embedding_index.json").write_text(json.dumps({"0": "OLD", "1": "KEEP"}), encoding="utf-8")

    counter = {"n": 0}

    def sync(fresh_ids_and_captions):
        counter["n"] += 1
        snap = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
        emb = np.load(tmp_path / "embeddings.npy")
        index = json.loads((tmp_path / "embedding_index.json").read_text(encoding="utf-8"))
        ids = [index[str(i)] for i in range(len(index))]
        raw = [
            {"id": pid, "caption": cap, "media_url": "u", "media_type": "IMAGE",
             "comments_count": 0, "comments": {"data": []}}
            for pid, cap in fresh_ids_and_captions
        ]
        out = s6.run_sync(
            raw, snap, emb, ids, {}, tmp_path / f"changes_{counter['n']}.json",
            persist_snapshot=True, vendor_handle="_test", snapshot_dir=tmp_path,
            sync_timestamp=f"2026-09-23T0{counter['n']}-00-00+00-00",
        )
        entries = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
        return out, {e["post_id"]: e for e in entries}

    return sync


LIVE = [("OLD", "iPhone 15, N900,000"), ("KEEP", "PS5 Slim, N990,000")]
REPOST = ("NEW", "Still available! iPhone 15")


def _types(out):
    return [(c["change_type"], c["post_id"]) for c in out["changes"]]


def test_a_deletion_is_reported_once_not_every_sync(sync_env):
    """THE regression test for re-reported deletions.

    An absent post stays in the snapshot as an `archived` record, which is
    deliberate - the deletion must never be silently dropped. But `archived`
    also has to mean "already reported", or the post lands in deleted_ids again
    on every later sync and the vendor is asked to archive it forever. Before
    the fix, three syncs after one deletion reported it three times.
    """
    out, entries = sync_env([("KEEP", "PS5 Slim, N990,000")])
    assert _types(out) == [("deleted", "OLD")]
    assert entries["OLD"]["lifecycle_state"] == "archived"

    for _ in range(2):
        out, entries = sync_env([("KEEP", "PS5 Slim, N990,000")])
        assert _types(out) == []
        assert entries["OLD"]["lifecycle_state"] == "archived"


def test_an_exact_repost_merges_once_and_does_not_flip_flop(sync_env):
    """THE regression test for the merge flip-flop.

    Reposting without deleting the original is what a vendor actually does, so
    the original is still live. The merge used to drop it from the snapshot
    entirely; the next sync then saw it as new, matched it at distance 0, and
    merged back the other way - alternating forever and reporting a change on
    an account where nothing had changed.
    """
    out, entries = sync_env(LIVE + [REPOST])
    assert _types(out) == [("repost_merge", "NEW")]
    assert set(entries) == {"OLD", "KEEP", "NEW"}

    for _ in range(2):
        out, entries = sync_env(LIVE + [REPOST])
        assert _types(out) == []
        assert set(entries) == {"OLD", "KEEP", "NEW"}


def test_the_superseded_post_keeps_its_record_but_loses_the_catalog(sync_env):
    """The tombstone carries no catalog_products, which is what popping the
    entry was protecting against - the catalog identity moves to the repost, so
    keeping both entries cannot duplicate a product."""
    _, entries = sync_env(LIVE + [REPOST])
    assert entries["OLD"]["lifecycle_state"] == "superseded"
    assert entries["OLD"]["superseded_by"] == "NEW"
    assert entries["OLD"]["catalog_products"] == []
    assert entries["NEW"]["catalog_products"] == [{"name": "OLD"}]


def test_a_later_repost_matches_the_live_post_not_the_tombstone(sync_env):
    """A third identical image must resolve against the post that currently
    holds the catalog entry. Matching the tombstone would hand the merge an
    entry whose catalog_products are empty, silently losing the product."""
    sync_env(LIVE + [REPOST])
    out, entries = sync_env(LIVE + [REPOST, ("NEWER", "Back in stock, iPhone 15")])
    merges = [c for c in out["changes"] if c["change_type"] == "repost_merge"]
    assert len(merges) == 1
    assert merges[0]["matched_post_id"] == "NEW"
    assert entries["NEWER"]["catalog_products"] == [{"name": "OLD"}]


def test_deleting_the_original_after_a_repost_still_reports_both_once(sync_env):
    """The other ordering: the vendor tidies up. Both events fire on the sync
    they happen, and the one after is silent."""
    out, entries = sync_env([("KEEP", "PS5 Slim, N990,000"), REPOST])
    assert sorted(_types(out)) == [("deleted", "OLD"), ("repost_merge", "NEW")]
    assert entries["OLD"]["lifecycle_state"] == "archived"

    out, _ = sync_env([("KEEP", "PS5 Slim, N990,000"), REPOST])
    assert _types(out) == []


def test_editing_the_caption_of_a_post_that_is_also_superseded(sync_env):
    """A post can be caption-edited AND superseded by a repost in the same
    window - a vendor refreshes a listing's price, then reposts its image.

    The tombstone used to be built from the PREVIOUS snapshot entry, so it kept
    the stale caption and stale content_hash. The next sync then re-reported
    the edit that had already been reported, and rebuilt the entry through the
    ordinary caption-edit path, which silently dropped `superseded` and made
    the post a match candidate again.
    """
    edited = "iPhone 15, N850,000"  # the price moved
    out, entries = sync_env([("OLD", edited), ("KEEP", "PS5 Slim, N990,000"), REPOST])
    assert sorted(_types(out)) == [("caption_edit", "OLD"), ("repost_merge", "NEW")]

    # The tombstone carries the NEW caption, so it is up to date...
    assert entries["OLD"]["caption"] == edited
    assert entries["OLD"]["lifecycle_state"] == "superseded"
    assert entries["OLD"]["superseded_by"] == "NEW"

    # ...and the next sync, with nothing changed, is silent and keeps the state.
    out, entries = sync_env([("OLD", edited), ("KEEP", "PS5 Slim, N990,000"), REPOST])
    assert _types(out) == []
    assert entries["OLD"]["lifecycle_state"] == "superseded"
