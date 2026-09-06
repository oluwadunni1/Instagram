#!/usr/bin/env python3
"""
One-time setup for the Cloudflare R2 DVC remote.

Reads the four R2 values from .env (the same single secret store every other
credential in this project lives in - see pipeline/settings.py) and writes
them into DVC's config files, split by sensitivity:

  .dvc/config        bucket URL + endpoint. NOT secret, committed to git so
                     `dvc pull` works for anyone who has their own creds.
  .dvc/config.local  access key + secret. Gitignored by .dvc/.gitignore -
                     never committed.

After this runs once, plain `dvc push` / `dvc pull` work directly; you don't
need this script or the Makefile wrapper again unless credentials rotate.

Required in .env:
    R2_ACCOUNT_ID          Cloudflare account ID (the hex string in your
                           dashboard URL, and in the R2 endpoint hostname)
    R2_BUCKET              R2 bucket name, e.g. instagram-catalog-pipeline
    R2_ACCESS_KEY_ID       from an R2 API token with Object Read & Write
    R2_SECRET_ACCESS_KEY   shown once when the token is created

Usage:
    uv run scripts/setup_dvc_remote.py
    uv run scripts/setup_dvc_remote.py --remote-name r2 --path dvcstore
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline.settings  # noqa: F401,E402 - import side effect: load_dotenv()
from pipeline.exceptions import MissingCredentialsError  # noqa: E402
from pipeline.logging_config import configure_logging  # noqa: E402

logger = logging.getLogger(__name__)

REQUIRED_VARS = (
    "R2_ACCOUNT_ID",
    "R2_BUCKET",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)


def _dvc(*args: str) -> None:
    """Runs one `dvc` subcommand, surfacing its stderr on failure.

    Shells out rather than importing dvc.api because the remote-config
    commands are CLI-only - there's no supported Python equivalent for
    `dvc remote modify --local`.
    """
    cmd = ["uv", "run", "dvc", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            f"`{' '.join(args)}` failed:\n{result.stderr.strip() or result.stdout.strip()}"
        )


def read_r2_env() -> dict[str, str]:
    """Pulls the four R2 values out of the environment.

    Raises:
        MissingCredentialsError: If any are unset, naming all of the
            missing ones at once rather than failing on the first.
    """
    values = {name: os.environ.get(name, "").strip() for name in REQUIRED_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise MissingCredentialsError(
            "Missing from .env: "
            + ", ".join(missing)
            + "\n\nSee this script's docstring for where each value comes from in "
            "the Cloudflare dashboard."
        )
    return values


def configure_remote(values: dict[str, str], remote_name: str, path: str) -> None:
    """Writes the R2 remote into .dvc/config and .dvc/config.local.

    Args:
        values: The four R2_* values from read_r2_env().
        remote_name: DVC remote name. Set as the default (-d) remote, so
            bare `dvc push`/`dvc pull` use it with no -r flag.
        path: Key prefix inside the bucket. DVC writes a content-addressed
            store under here, not human-readable filenames.
    """
    bucket, account = values["R2_BUCKET"], values["R2_ACCOUNT_ID"]
    endpoint = f"https://{account}.r2.cloudflarestorage.com"

    # -f so re-running after a bucket rename or credential rotation
    # overwrites cleanly instead of erroring on an existing remote.
    _dvc("remote", "add", "-d", "-f", remote_name, f"s3://{bucket}/{path}")
    _dvc("remote", "modify", remote_name, "endpointurl", endpoint)
    # R2 has no regions, but boto3 requires one to sign the request -
    # "auto" is the value Cloudflare's own S3-compat docs specify.
    _dvc("remote", "modify", remote_name, "region", "auto")

    # --local -> .dvc/config.local, which .dvc/.gitignore already excludes.
    _dvc("remote", "modify", "--local", remote_name,
         "access_key_id", values["R2_ACCESS_KEY_ID"])
    _dvc("remote", "modify", "--local", remote_name,
         "secret_access_key", values["R2_SECRET_ACCESS_KEY"])

    logger.info("Configured DVC remote %r -> s3://%s/%s", remote_name, bucket, path)
    logger.info("  endpoint: %s", endpoint)
    logger.info("  credentials written to .dvc/config.local (gitignored)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--remote-name", default="r2", help="DVC remote name (default: r2)")
    parser.add_argument("--path", default="dvcstore", help="Key prefix inside the bucket (default: dvcstore)")
    args = parser.parse_args()

    configure_remote(read_r2_env(), args.remote_name, args.path)

    logger.info("")
    logger.info("Next: `make data-push` to upload, `make data-status` to verify.")


if __name__ == "__main__":
    configure_logging()
    try:
        main()
    except MissingCredentialsError as exc:
        sys.exit(str(exc))
