#!/usr/bin/env python3
"""
Run validity gate - makes "validated" mean something auditable rather than
"it printed numbers".

The harness and scripts/run_pipeline.py both print scores whether or not a
model was ever reached. A throttled run silently substitutes the zero-cost
regex fallback for model output; a run whose credentials or credit are dead
produces a clean-looking 0% that is indistinguishable from a model answering
nothing. In both cases report/token_log.csv is the only artifact that knows,
because it records what actually reached litellm rather than what the config
said should.

So: every run whose numbers are going to be published gets gated here first,
and a run that fails the gate is discarded and repeated, not reported.

    uv run scripts/verify_run.py --run-id family_gemini_20260909T101500Z \\
        --expect-model gemini/gemini-3.5-flash-lite \\
        --expect-model gemini/gemini-3.5-flash \\
        --posts 30

Exits 0 on PASS, 1 on FAIL, so it can gate a shell loop. --list shows the
run_ids present in the log, for when you no longer have the one printed by
the run.

Checks (each independently fatal):
  1. At least one row logged for the run_id.
  2. Zero fallback_used=True rows.
  3. Every model string called is in --expect-model, when that is given.
  4. Call count within --min-calls/--max-calls, or within a range derived
     from --posts.
  5. Zero errored posts across the --predictions files given.

Check 5 does not read the token log, and cannot: a post whose stage call
raised never reached litellm, so it logs no row at all. The harness records it
as an {"post_id": ..., "error": ...} entry in its per-post predictions JSON
instead, and scores it as a miss. Gating on the log alone therefore passes a
run with posts that silently produced nothing - which is how the first gated
run (family_gemini, 2026-09-09) came back with two 403'd posts and a PASS on
every log-derived check.
"""

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Imported as a module, not `from ... import TOKEN_LOG_PATH`, so that the path
# this reads and the path read_run_usage() reads are always the same object -
# one patch point for tests, and no way for the two to drift.
from pipeline import llm_client  # noqa: E402
from pipeline.llm_client import read_run_usage  # noqa: E402

# One call per post is the floor for any stage that ran at all; the cascade
# adds a vision Pass B on some posts and Stage 4's regex prefilter removes
# others, so the plausible band around a 3-stage run is wide. This only has
# to catch order-of-magnitude wrongness - half the posts silently skipped,
# or a stage looping - not to predict the exact count.
MIN_CALLS_PER_POST = 1.0
MAX_CALLS_PER_POST = 4.0


def list_run_ids() -> list[tuple[str, int]]:
    """Every run_id in the token log with its row count, oldest first."""
    if not llm_client.TOKEN_LOG_PATH.exists():
        return []
    counts: Counter = Counter()
    with llm_client.TOKEN_LOG_PATH.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            counts[row.get("run_id") or "unknown"] += 1
    return list(counts.items())


def count_errored_posts(paths: list[Path]) -> tuple[int, int, int, list[str]]:
    """(errored, total, unreadable, sample_messages) across the harness
    prediction files.

    An errored post never reached litellm, so it writes no token-log row and is
    invisible to every other check here. score_stage2/3/4 each record it as an
    entry carrying an "error" key, which is the only durable artifact that
    knows.

    A file that cannot be read is counted separately and is equally fatal.
    Treating it as "no errors found" would turn a run whose predictions never
    landed into a PASS - the exact laundering this gate exists to prevent.
    """
    errored = total = unreadable = 0
    samples: list[str] = []
    for path in paths:
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            unreadable += 1
            samples.append(f"{path.name}: unreadable ({type(exc).__name__})")
            continue

        # A chained run (scripts/run_pipeline.py) writes ONE catalog.json
        # instead of per-stage prediction lists, so --predictions had nothing
        # to point at and this check silently skipped - the weaker verification
        # on exactly the path that mirrors production. The catalog already
        # carries what is needed: an "errors" list of {post_id, stage, error}
        # and an "items" list. Accept either shape.
        if isinstance(entries, dict) and "items" in entries:
            catalog_errors = entries.get("errors") or []
            total += len(entries.get("items") or [])
            errored += len(catalog_errors)
            for err in catalog_errors:
                if len(samples) < 3:
                    msg = str(err.get("error", "")).splitlines()[0][:100]
                    samples.append(f"{err.get('post_id')} [{err.get('stage')}]: {msg}")
            continue
        total += len(entries)
        for entry in entries:
            if "error" in entry:
                errored += 1
                if len(samples) < 3:
                    msg = str(entry["error"]).splitlines()[0][:110] if str(entry["error"]).strip() else "(empty)"
                    samples.append(f"{entry.get('post_id')}: {msg}")
    return errored, total, unreadable, samples


def verify(
    run_id: str,
    expect_models: list[str] | None = None,
    posts: int | None = None,
    min_calls: int | None = None,
    max_calls: int | None = None,
    predictions: list[Path] | None = None,
) -> tuple[bool, list[str]]:
    """Returns (passed, report_lines). Never raises on a failed check - a
    gate that crashes tells you less than one that explains itself."""
    usage = read_run_usage(run_id)
    lines: list[str] = []
    failures: list[str] = []

    def check(ok: bool, label: str, detail: str) -> None:
        lines.append(f"  [{'PASS' if ok else 'FAIL'}] {label}: {detail}")
        if not ok:
            failures.append(label)

    # --- 1. the run reached a model at all --------------------------------
    if usage["calls"] == 0:
        check(False, "rows logged", (
            f"ZERO rows in {llm_client.TOKEN_LOG_PATH} for run_id {run_id!r}. No model was called. "
            "A clean-looking run with no token rows is infrastructure failure, not a model "
            "that answered nothing (FINDINGS.md 2026-09-08). Nothing else can be checked."
        ))
        return False, lines
    check(True, "rows logged", f"{usage['calls']} call(s), {usage['total_tokens']:,} tokens")

    # --- 2. no silent degradation to the regex fallback -------------------
    check(usage["fallbacks"] == 0, "no regex fallback", (
        "0 fallback rows - every result is model output"
        if usage["fallbacks"] == 0 else
        f"{usage['fallbacks']} post(s) degraded to the zero-cost regex fallback after the LLM "
        "call failed. Those results are heuristic, not model output, so this run's numbers "
        "describe a mixture. Re-run on fresh quota."
    ))

    # --- 3. the models called are the models configured -------------------
    # The only check that cannot be fooled by a display label: every Stage 4
    # comparison published before 2026-09-07 reported four models while
    # running one, and the token log was the sole evidence.
    if expect_models:
        unexpected = [m for m in usage["models"] if m not in expect_models]
        missing = [m for m in expect_models if m not in usage["models"]]
        detail = f"called {', '.join(usage['models'])}"
        if unexpected:
            detail += f" - UNEXPECTED: {', '.join(unexpected)}"
        if missing:
            # Not fatal on its own: a cascade whose vision Pass B never
            # triggered legitimately never calls the vision model.
            detail += f" (configured but never called: {', '.join(missing)})"
        check(not unexpected, "models match config", detail)
    else:
        lines.append(f"  [skip] models match config: no --expect-model given "
                     f"(called {', '.join(usage['models'])})")

    # --- 4. call count in a plausible band --------------------------------
    if posts is not None and min_calls is None and max_calls is None:
        min_calls = int(posts * MIN_CALLS_PER_POST)
        max_calls = int(posts * MAX_CALLS_PER_POST)
    if min_calls is not None or max_calls is not None:
        lo = min_calls if min_calls is not None else 0
        hi = max_calls if max_calls is not None else sys.maxsize
        ok = lo <= usage["calls"] <= hi
        check(ok, "call count in range",
              f"{usage['calls']} call(s), expected {lo}-{hi if hi != sys.maxsize else 'inf'}")
    else:
        lines.append("  [skip] call count in range: no --posts/--min-calls/--max-calls given")

    # --- 5. no post silently produced nothing ------------------------------
    # Deliberately not log-derived: an errored post makes no call, so every
    # check above is blind to it.
    if predictions:
        errored, scored, unreadable, samples = count_errored_posts(predictions)
        detail = f"{errored} errored of {scored} scored post-entries"
        if unreadable:
            detail += f", {unreadable} predictions file(s) unreadable"
        for sample in samples:
            detail += "\n" + " " * 11 + sample
        check(errored == 0 and unreadable == 0, "no errored posts", detail)
    else:
        lines.append("  [skip] no errored posts: no --predictions given")

    lines.append("")
    lines.append("  Per stage:")
    for stage, entry in sorted(usage["by_stage"].items()):
        lines.append(f"    {stage:<26} {entry['calls']:>3} calls  {entry['total_tokens']:>7,} tokens")

    return not failures, lines


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gate one run's validity against report/token_log.csv before publishing "
                    "its numbers. Exits non-zero on failure."
    )
    parser.add_argument("--run-id", default=None,
                        help="The run_id printed by the harness or run_pipeline.py")
    parser.add_argument("--expect-model", action="append", dest="expect_models", default=None,
                        help="A model string the run is allowed to have called; repeatable. "
                             "Any other model string called is a failure. Give every model the "
                             "config names (text_model AND vision_model for a cascade).")
    parser.add_argument("--posts", type=int, default=None,
                        help=f"Post count, used to derive an expected call range "
                             f"({MIN_CALLS_PER_POST:g}-{MAX_CALLS_PER_POST:g} calls/post). "
                             "Overridden by --min-calls/--max-calls.")
    parser.add_argument("--predictions", type=Path, action="append", default=None,
                        help="A report/<vendor_id>/stage*_predictions.json written by this run; "
                             "repeatable. Any entry carrying an \"error\" key is a post that "
                             "produced no result - invisible to every log-derived check above, "
                             "because it never reached litellm. Pass all three stage files.")
    parser.add_argument("--min-calls", type=int, default=None)
    parser.add_argument("--max-calls", type=int, default=None)
    parser.add_argument("--list", action="store_true",
                        help="List the run_ids present in the token log and exit")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover - non-reconfigurable stream
        pass

    if args.list:
        runs = list_run_ids()
        if not runs:
            print(f"No rows in {llm_client.TOKEN_LOG_PATH}.")
            return
        print(f"{len(runs)} run_id(s) in {llm_client.TOKEN_LOG_PATH}:")
        for run_id, count in runs:
            print(f"  {count:>5} rows  {run_id}")
        return

    if not args.run_id:
        raise SystemExit("--run-id is required (or --list to see what is in the log)")

    passed, lines = verify(
        args.run_id,
        expect_models=args.expect_models,
        posts=args.posts,
        min_calls=args.min_calls,
        max_calls=args.max_calls,
        predictions=args.predictions,
    )

    print(f"=== Verifying run {args.run_id} ===")
    print("\n".join(lines))
    print()
    if passed:
        print("VALIDATED - this run's numbers are safe to publish.")
    else:
        print("NOT VALIDATED - discard this run and repeat it. Do not publish its numbers.")
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
