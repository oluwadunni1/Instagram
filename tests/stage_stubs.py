"""Importable stage stubs for the load_stage_fn tests.

load_stage_fn() resolves a stage by importlib module path + function name, so
the functions it binds have to live in a real importable module - hence this
file rather than locals defined inside a test.
"""

from __future__ import annotations


def good_stage(
    post: dict,
    profile: dict | None = None,
    text_model: str = "unset",
    threshold: float = 0.0,
) -> dict:
    """A correctly-named stage: its real model parameter is `text_model`.

    `profile` sits second, matching every real stage entrypoint - the harness
    and scripts/run_pipeline.py both pass the Stage 1 profile positionally
    there, and it is echoed back so a test can prove it arrived.
    """
    return {
        "post_id": post.get("post_id"),
        "profile": profile,
        "text_model": text_model,
        "threshold": threshold,
    }


def shadowed_model_stage(post: dict, profile: dict | None = None,
                         model: str = "hardcoded-default") -> dict:
    """The 2026-09-07 bug shape: a parameter named `model`, which
    load_stage_fn() reserves and strips, so it silently never arrives."""
    return {"post_id": post.get("post_id"), "model": model}


def shadowed_note_stage(post: dict, profile: dict | None = None, note: str = "") -> dict:
    """`note` is bookkeeping-only and equally reserved."""
    return {"post_id": post.get("post_id"), "note": note}
