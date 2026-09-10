"""
Pre-commit secret scan.

Two things have already gone wrong in this repo that a commit-time check
catches for free:

  - `.env` was committed in 03e5554 and only deleted a commit later
    (17f9812). Deleting it does not remove it from history - anyone who has
    cloned the repo can still `git show 03e5554:.env`. The value that time
    was a placeholder, but .gitignore alone demonstrably did not prevent the
    staging.
  - Two live credentials have reached console logs (FINDINGS.md 2026-09-07,
    2026-09-08). Log leaks are transient; a committed one is permanent.

It also refuses to let the DVC-tracked data paths into git: eval/golden/,
runs/, data/snapshots/ and report/ hold real vendor and commenter data and
belong in R2 behind a .dvc pointer, never in a git object.

Usage:
    uv run scripts/scan_secrets.py            # staged changes (hook mode)
    uv run scripts/scan_secrets.py --all      # every tracked file
    make install-hooks                        # wire it into .git/hooks

Exit code 1 means "do not commit this". A false positive on a fixture or a
doc example is silenced by putting `pragma: allowlist secret` in a comment
on that line - deliberately visible in the diff, so waiving a hit is a
reviewable act rather than a quiet one.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

ALLOWLIST_PRAGMA = "pragma: allowlist secret"

# Paths that must never be committed at all, regardless of content.
FORBIDDEN_PATHS = (".env", ".dvc/config.local")

# DVC-tracked data: real vendor/commenter data, stored in Cloudflare R2 with
# a committed *.dvc pointer. The *.dvc and .gitignore files inside them are
# the parts that DO belong in git.
DATA_PREFIXES = ("eval/golden/", "runs/", "data/snapshots/", "report/")
DATA_ALLOWED_SUFFIXES = (".dvc", ".gitignore")

# Only text-ish files are worth scanning line by line.
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".pdf", ".mp4", ".ico", ".woff", ".woff2")

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Google / Gemini API key: AIza + 35 chars. GEMINI_API_KEY is this
    # pipeline's primary model credential (litellm resolves it for every
    # gemini/* call), not just the embeddings key.
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    # Instagram / Facebook long-lived user token.
    ("Instagram access token", re.compile(r"\b(?:IGQ|EAA)[0-9A-Za-z_\-]{20,}")),
    ("OpenRouter API key", re.compile(r"\bsk-or-v1-[0-9a-f]{32,}\b")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-)?[0-9A-Za-z_\-]{32,}\b")),
    ("Groq API key", re.compile(r"\bgsk_[0-9A-Za-z]{40,}\b")),
    ("AWS/R2 access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # Generic: a credential-ish name assigned a long opaque literal. No \b
    # before the name - GEMINI_API_KEY and R2_SECRET_ACCESS_KEY are prefixed,
    # and a leading word boundary would miss exactly the variables this repo
    # actually uses. The value class excludes { and $ so f-string
    # placeholders and shell/CI variable references don't trip it.
    ("credential assignment", re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|secret[_-]?access[_-]?key|client[_-]?secret|"
        r"auth[_-]?token|password)\b\s*[:=]\s*[\"']?([0-9A-Za-z_\-./+]{20,})[\"']?"
    )),
]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout


def staged_paths() -> list[str]:
    """Paths staged for commit, excluding deletions (nothing to scan)."""
    out = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    return [line for line in out.splitlines() if line.strip()]


def tracked_paths() -> list[str]:
    return [line for line in _git("ls-files").splitlines() if line.strip()]


def check_path_rules(path: str) -> list[str]:
    """Path-level violations: files that are wrong to commit at all."""
    problems = []
    normalized = path.replace("\\", "/")
    if normalized in FORBIDDEN_PATHS:
        problems.append(f"{path}: this file holds live credentials and must never be committed")
    for prefix in DATA_PREFIXES:
        if normalized.startswith(prefix) and not normalized.endswith(DATA_ALLOWED_SUFFIXES):
            problems.append(
                f"{path}: DVC-tracked data (real vendor/commenter data). "
                f"Commit the .dvc pointer and `make data-push` instead."
            )
    return problems


def scan_file(path: str) -> list[str]:
    """Content-level violations, reported as path:line: reason. Never prints
    the matched value - a scanner that echoes the secret it found has just
    written it somewhere new."""
    full = REPO_ROOT / path
    if full.suffix.lower() in SKIP_SUFFIXES or not full.is_file():
        return []
    try:
        text = full.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []  # binary or unreadable - nothing to scan

    problems = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if ALLOWLIST_PRAGMA in line:
            continue
        for label, pattern in PATTERNS:
            if pattern.search(line):
                problems.append(
                    f"{path}:{lineno}: looks like a {label}. If this is a fixture or an "
                    f"example, add a `{ALLOWLIST_PRAGMA}` comment on that line."
                )
                break
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--all", action="store_true",
                        help="scan every tracked file instead of just the staged ones")
    parser.add_argument("paths", nargs="*",
                        help="specific paths to scan (what the pre-commit framework passes)")
    args = parser.parse_args()

    if args.paths:
        paths = args.paths
    elif args.all:
        paths = tracked_paths()
    else:
        paths = staged_paths()

    problems: list[str] = []
    for path in paths:
        problems.extend(check_path_rules(path))
        problems.extend(scan_file(path))

    if problems:
        print("Secret scan FAILED - commit blocked:\n", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print(
            "\nIf a credential was already committed, rotating it is the fix - "
            "removing it in a later commit leaves it in history.",
            file=sys.stderr,
        )
        return 1

    print(f"Secret scan clean ({len(paths)} file(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
