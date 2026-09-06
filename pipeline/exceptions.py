"""
Shared exception hierarchy for the pipeline.

Library functions (anything not directly behind an `if __name__ ==
"__main__":` guard) should raise these instead of `SystemExit`, so callers
other than a CLI script - e.g. a future harness path that calls
stage1_profile.get_or_create_profile() directly - can catch a real
exception instead of having the whole process torn down underneath them.

Each CLI entry point still gets the exact same user-facing behavior: catch
PipelineError in `__main__`, print the message, exit non-zero.
"""

from __future__ import annotations


class PipelineError(Exception):
    """Base class for expected, user-actionable pipeline failures."""


class MissingRawDumpError(PipelineError):
    """No raw dump found for an account - ingest.py needs to run first."""


class MissingCredentialsError(PipelineError):
    """A required credential (e.g. IG_ACCESS_TOKEN) is not set."""
