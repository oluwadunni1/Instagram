"""
Shared console logging setup.

Call configure_logging() once from a script's `if __name__ == "__main__":`
block - never at import time in a library module - so importing a stage
module doesn't have side effects on logging for whatever process imported
it.

Format is deliberately plain: INFO lines render as just the message (so
normal progress/report output looks the same as the print()s it replaces),
while WARNING/ERROR lines get a visible level tag so problems stand out in
a wall of progress output. Verbosity is controlled by the LOG_LEVEL env var
(default INFO) - set it to DEBUG to see raw model responses logged by
pipeline.llm_client, or WARNING to see only problems.
"""

from __future__ import annotations

import logging
import os


class _PlainInfoFormatter(logging.Formatter):
    """%(message)s alone at INFO and below; "LEVEL  message" above that."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.levelno <= logging.INFO:
            return message
        return f"{record.levelname}  {message}"


def configure_logging(level: str | None = None) -> None:
    """Attach a console handler to the root logger. Safe to call more than
    once - subsequent calls just update the level rather than stacking
    handlers."""
    resolved_level = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()

    root = logging.getLogger()
    root.setLevel(resolved_level)

    # Before the early return below, so a second configure_logging() call
    # re-applies it rather than silently skipping it.
    _suppress_http_wire_logs()

    existing = next((h for h in root.handlers if isinstance(h, logging.StreamHandler)), None)
    if existing is not None:
        existing.setLevel(resolved_level)
        return

    handler = logging.StreamHandler()
    handler.setLevel(resolved_level)
    handler.setFormatter(_PlainInfoFormatter())
    root.addHandler(handler)


def _suppress_http_wire_logs() -> None:
    """Keep urllib3's per-request DEBUG line out of the log.

    urllib3 logs `"GET /path?query HTTP/1.1" 200` at DEBUG, i.e. the full
    query string. Historically that printed live Instagram access tokens to
    the console on any LOG_LEVEL=DEBUG run, because the token was passed as
    an `access_token` query param. Calls now use an Authorization header
    (see pipeline/settings.py::ig_auth_headers), so the URL no longer carries
    the token - this floor is the belt-and-braces half of that fix, and also
    stops DEBUG runs drowning real output in wire noise. Mirrors the existing
    litellm suppression in pipeline/llm_client.py.

    Raising the level to INFO (not disabling) keeps genuine urllib3 warnings,
    like retry/connection-pool messages, visible.
    """
    for noisy in ("urllib3", "urllib3.connectionpool", "requests.packages.urllib3"):
        logging.getLogger(noisy).setLevel(logging.INFO)
