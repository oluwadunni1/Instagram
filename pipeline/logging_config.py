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

    existing = next((h for h in root.handlers if isinstance(h, logging.StreamHandler)), None)
    if existing is not None:
        existing.setLevel(resolved_level)
        return

    handler = logging.StreamHandler()
    handler.setLevel(resolved_level)
    handler.setFormatter(_PlainInfoFormatter())
    root.addHandler(handler)
