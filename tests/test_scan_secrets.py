"""Tests for the pre-commit secret scan.

A scanner nobody checks is worse than none - it produces the feeling of
coverage without the coverage. These pin both halves: it catches the
credential shapes this project actually uses, and it stays quiet on the
things that legitimately live in the repo (.env.example's empty keys, the
CDN cache params on a media_url, the fake key in tests/test_credentials.py).

Every literal below is a fabricated value of the right *shape*. Never put a
real credential in a test.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# scripts/ is not a package and scan_secrets.py is written to be run as a
# script, so load it by path rather than importing it.
_spec = importlib.util.spec_from_file_location(
    "scan_secrets", REPO_ROOT / "scripts" / "scan_secrets.py"
)
scan_secrets = importlib.util.module_from_spec(_spec)
sys.modules["scan_secrets"] = scan_secrets
_spec.loader.exec_module(scan_secrets)


def _write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str) -> list[str]:
    (tmp_path / "candidate.py").write_text(content, encoding="utf-8")
    monkeypatch.setattr(scan_secrets, "REPO_ROOT", tmp_path)
    return scan_secrets.scan_file("candidate.py")


@pytest.mark.parametrize("line", [
    'GEMINI_API_KEY = "AIza' + "B" * 35 + '"',
    'IG_ACCESS_TOKEN = "IGQW' + "x" * 30 + '"',
    'OPENROUTER_API_KEY = "sk-or-v1-' + "0" * 32 + '"',
    'R2_SECRET_ACCESS_KEY = "' + "a" * 40 + '"',
    'R2_ACCESS_KEY_ID = "AKIA' + "Q" * 16 + '"',
])
def test_credential_shapes_are_caught(line: str, tmp_path: Path, monkeypatch) -> None:
    assert _write(tmp_path, monkeypatch, line), f"missed: {line.split(' =')[0]}"


def test_findings_never_echo_the_matched_value(tmp_path: Path, monkeypatch) -> None:
    """A scanner that prints the secret it found has written it somewhere new
    - a build log, a CI artifact, a terminal scrollback."""
    secret = "AIza" + "B" * 35
    problems = _write(tmp_path, monkeypatch, f'GEMINI_API_KEY = "{secret}"')
    assert problems
    assert all(secret not in problem for problem in problems)


def test_allowlist_pragma_silences_a_line(tmp_path: Path, monkeypatch) -> None:
    line = 'FIXTURE = "AIza' + "B" * 35 + '"  # pragma: allowlist secret'
    assert _write(tmp_path, monkeypatch, line) == []


@pytest.mark.parametrize("line", [
    "GEMINI_API_KEY=",                                        # .env.example
    'url = "https://cdn.example.com/p.jpg?oh=abc&oe=def"',    # CDN cache params
    'headers = {"x-goog-api-key": api_key}',                  # a variable, not a literal
    'raise RuntimeError(f"failed: {redact_tokens(exc)}")',
])
def test_legitimate_lines_are_not_flagged(line: str, tmp_path: Path, monkeypatch) -> None:
    assert _write(tmp_path, monkeypatch, line) == []


def test_the_repos_own_files_are_clean(tmp_path: Path, monkeypatch) -> None:
    """.env.example and the credential tests are the two files most likely to
    trip the scanner; if either does, the hook becomes noise people bypass."""
    monkeypatch.setattr(scan_secrets, "REPO_ROOT", REPO_ROOT)
    for path in (".env.example", "tests/test_credentials.py", "pipeline/settings.py"):
        assert scan_secrets.scan_file(path) == [], f"{path} trips the scanner"


# --- path rules ------------------------------------------------------------

def test_env_file_is_rejected_outright() -> None:
    """.env reached a commit once already (03e5554, deleted in 17f9812);
    .gitignore alone did not prevent it."""
    assert scan_secrets.check_path_rules(".env")
    assert scan_secrets.check_path_rules(".dvc/config.local")


@pytest.mark.parametrize("path", [
    "eval/golden/vendor_autos_01.json",
    "runs/vendor_autos_01/raw/dump_x.json",
    "data/snapshots/ayodele.akinbohun/posts.json",
    "report/token_log.csv",
])
def test_dvc_tracked_data_is_rejected(path: str) -> None:
    """Real vendor and commenter data belongs in R2 behind a .dvc pointer."""
    assert scan_secrets.check_path_rules(path)


@pytest.mark.parametrize("path", ["runs.dvc", "report.dvc", "eval/.gitignore", "pipeline/settings.py"])
def test_pointers_and_source_are_allowed(path: str) -> None:
    assert scan_secrets.check_path_rules(path) == []
