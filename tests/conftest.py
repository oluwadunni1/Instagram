"""Shared pytest setup.

There is no broad test suite by design (CLAUDE.md) - the eval harness is the
verification mechanism for model quality. These tests cover only the things
the harness structurally cannot: credential handling, per-post error
isolation inside the harness itself, and deterministic Stage 5 routing. All
of them are offline; nothing here may make a network call or read .env.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The repo root for `pipeline.*`, and eval/ for `harness` - eval/ is not a
# package (no __init__.py) and harness.py is written to be run as a script.
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "eval"))


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Removes the courtesy sleeps from media_fingerprint's finally blocks so
    credential tests don't pay 0.3s each."""
    import pipeline.media_fingerprint as mf

    monkeypatch.setattr(mf.time, "sleep", lambda _seconds: None)
