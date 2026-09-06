"""
Centralized environment variable loading.

load_dotenv() happens exactly once, here - every other module that needs an
env var imports it from this module instead of calling load_dotenv() and
os.environ.get() itself.

Per-stage model/experiment config lives in
pipeline/config/experiment_schema.py (YAML + Pydantic), not here - see
load_experiment_config().
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def get_ig_access_token(var_name: str = "IG_ACCESS_TOKEN") -> str | None:
    """Looks up an Instagram access token by env var name.

    Lets a second (or third) connected account's token live in .env under
    its own name (e.g. IG_ACCESS_TOKEN_GADGETS) instead of overwriting
    IG_ACCESS_TOKEN every time you switch which vendor you're working with -
    ingest.py and scripts/run_stage6.py both take a --token-env flag that
    resolves through this function.
    """
    return os.environ.get(var_name)


IG_ACCESS_TOKEN = get_ig_access_token()
