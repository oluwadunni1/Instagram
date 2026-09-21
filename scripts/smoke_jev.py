#!/usr/bin/env python3
"""
Smoke test for TypeSafe's Jev (System One) against real golden captions.

PURPOSE: confirm the wire format before any pipeline code is written. Jev is
not an OpenAI-compatible endpoint - it takes {state, model, questions} at
POST /v1/systemone and returns typed decisions rather than text - so litellm
cannot route it, complete_structured() cannot call it, and every assumption
about the request/response shape is currently unverified documentation.

Seeing the raw JSON is the deliverable. Scoring is incidental: this prints
gold-vs-predicted only so the distributions mean something to read, NOT as a
measurement. A real Stage 2 number has to come from eval/harness.py through
scripts/verify_run.py like every other model.

Deliberately writes nothing. In particular it never appends to
report/token_log.csv: that log is the audit trail verify_run.py gates on, and
a probe is not a gated run - the same reason complete_structured() only logs
when run_id is set.

Usage:
    uv run scripts/smoke_jev.py
    uv run scripts/smoke_jev.py --limit 8
    uv run scripts/smoke_jev.py --golden eval/golden/vendor_gadgets_01.json --no-stratify
    uv run scripts/smoke_jev.py --model jev-1.13.0 --raw-all

On Windows, prefix with PYTHONIOENCODING=utf-8 if you widen the caption
preview - captions carry emoji that cp1252 cannot encode.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.logging_config import configure_logging  # noqa: E402
from pipeline.settings import (  # noqa: E402
    get_typesafe_api_key,
    redact_tokens,
    typesafe_auth_headers,
)

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
REQUEST_TIMEOUT_SECONDS = 30
QUESTION_ID = "post_type"

# $/1M input tokens. Output is free on Jev. Used only for the console estimate
# below - nothing here writes a cost anywhere, and estimated_cost_usd in the
# token log remains hardcoded 0.0 (see README.md).
INPUT_PRICE_PER_MTOK = 0.042

DEFAULT_GOLDEN_PATHS = [
    Path("eval/golden/vendor_autos_01.json"),
    Path("eval/golden/vendor_gadgets_01.json"),
]

# The five Stage 2 post types, in the order pipeline/stages/stage2_triage.py
# declares them. Rubrics are derived from eval/LABEL_CODEBOOK.md, which
# reconstructs the labeler's actual decision rule from the golden set's notes -
# keep the two in sync.
#
# The first version of these rubrics was written from intuition and said
# "promotional implies ad_creative", which is the INVERSE of the rule the gold
# labels follow. ad_creative then acted as a magnet and took 7 of 11
# predictions in the first probes, swallowing clearance sales, finance offers,
# delivery posts and a pre-order listing. Gemini, whose prompt had no rubrics
# at all, scored 8/11 on the same posts against Jev's 5/11.
POST_TYPE_CRITERIA = {
    "product_listing":
        "A specific item is being offered for sale right now. Pre-orders count when a real "
        "offer is attached. A stated price is NOT required - 'DM for price' is still an offer.",
    "announcement":
        "The business is informing customers: opening hours, location, policy, availability, or "
        "the mechanics of a promotion. No specific item is offered. Clearance sales, financing "
        "offers and recurring discount days belong HERE, not in ad_creative. Also covers posts "
        "that name products which are not purchasable yet, such as unreleased models.",
    "testimonial_repost":
        "A sale that already completed: a delivery, a handover, or a customer's own words "
        "reshared. The product is often named and visible but is no longer available.",
    "meme_personal":
        "Personal or lifestyle content in a conversational voice. Nothing is sold and no "
        "business information is conveyed.",
    "ad_creative":
        "A produced engagement device built around products, such as a poll or a 'pick one' "
        "question, whose purpose is interaction rather than an offer. RARE: if a post is "
        "promotional but is not a poll, it is almost certainly an announcement.",
}

INSTRUCTIONS = (
    "Classify this Instagram post from a Nigerian vendor's business account by what the caption "
    "is doing. Judge only from the caption text provided. Work in this order and take the first "
    "match: (1) is a specific product being offered for sale right now? (2) is the post about a "
    "sale that already happened? (3) is the business informing customers about something? "
    "(4) is it a poll or engagement device built around products? (5) otherwise it is personal "
    "or lifestyle content. Being promotional or enthusiastic does not by itself make a post an "
    "advert; a post that reads like a listing but offers nothing purchasable is an announcement."
)


def safe(text: str) -> str:
    """Console-safe text. Captions carry emoji, and a Windows cp1252 stdout
    raises UnicodeEncodeError on them mid-print - documented in CLAUDE.md as a
    trap that costs real debugging time. Degrade the character, never the run."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def load_posts(golden_paths: list[Path]) -> list[dict]:
    """Every post from every given golden file, tagged with its source stem."""
    posts: list[dict] = []
    for path in golden_paths:
        if not path.exists():
            print(f"  ! golden file not found, skipping: {path}")
            continue
        for post in json.loads(path.read_text(encoding="utf-8")):
            post["_vendor"] = path.stem
            posts.append(post)
    return posts


def stratify(posts: list[dict], limit: int) -> list[dict]:
    """One post per distinct gold post_type first, then fill up to `limit`.

    Neither golden set alone covers the five-type enum: autos has no
    meme_personal or ad_creative, gadgets has no testimonial_repost. Taking the
    first N posts of either returns almost all product_listing and produces N
    near-identical distributions pinned near 1.0 - a weak read of a model whose
    entire selling point is the distribution. Round-robin by gold label so the
    rare classes (n=1 each) actually appear.
    """
    by_label: dict[str, list[dict]] = {}
    for post in posts:
        by_label.setdefault(post.get("post_type") or "unlabeled", []).append(post)

    picked: list[dict] = []
    # Rarest label first, so a small --limit still reaches the n=1 classes.
    labels = sorted(by_label, key=lambda k: len(by_label[k]))
    while len(picked) < limit and any(by_label[k] for k in labels):
        for label in labels:
            if not by_label[label]:
                continue
            picked.append(by_label[label].pop(0))
            if len(picked) >= limit:
                break
    return picked


def ask_jev(caption: str, model: str, headers: dict[str, str]) -> tuple[dict, float]:
    """One Jev call. Returns (parsed body, elapsed milliseconds)."""
    payload = {
        "model": model,
        "state": caption,
        "questions": {
            QUESTION_ID: {
                "type": "choice",
                "instructions": INSTRUCTIONS,
                "criteria": POST_TYPE_CRITERIA,
            }
        },
    }
    started = time.perf_counter()
    response = requests.post(
        TYPESAFE_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    return response.json(), elapsed_ms


def usage_tokens(body: dict) -> tuple[int, int]:
    """(input, output) tokens, tolerant of what the envelope actually calls
    them - the field names are unconfirmed until this script runs once."""
    usage = body.get("usage") or {}
    inp = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    return int(inp or 0), int(out or 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--golden", type=Path, action="append", dest="golden",
                        help="Golden file to draw captions from; repeatable. "
                             "Default: both vendor sets, so all five post types are reachable.")
    parser.add_argument("--limit", type=int, default=6,
                        help="How many posts to send (default 6).")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Jev model string (default {DEFAULT_MODEL}).")
    parser.add_argument("--offset", type=int, default=0,
                        help="Skip this many posts before sending. Selection is "
                             "deterministic, so a bare re-run re-sends the same captions - "
                             "use this to probe fresh ones.")
    parser.add_argument("--no-stratify", action="store_true",
                        help="Take posts in file order instead of one per gold label. "
                             "Expect almost all product_listing.")
    parser.add_argument("--raw-all", action="store_true",
                        help="Pretty-print every response body, not just the first.")
    args = parser.parse_args()

    configure_logging()

    key = get_typesafe_api_key()
    if not key:
        print("TYPESAFE_API_KEY is not set. Add it to .env (never to a shell "
              "command - it would land in your shell history and, in a Claude "
              "session, in the transcript).")
        return 1
    headers = typesafe_auth_headers(key)

    golden_paths = args.golden or DEFAULT_GOLDEN_PATHS
    posts = load_posts(golden_paths)
    if not posts:
        print("No golden posts loaded - nothing to send.")
        return 1

    if args.no_stratify:
        selected = posts[args.offset : args.offset + args.limit]
    else:
        selected = stratify(posts, args.limit + args.offset)[args.offset :]

    print("=" * 78)
    print("Jev smoke test - confirming the wire format, not measuring accuracy")
    print("=" * 78)
    print(f"  endpoint : POST {TYPESAFE_URL}")
    print(f"  model    : {args.model}")
    print(f"  posts    : {len(selected)} "
          f"({'file order' if args.no_stratify else 'stratified by gold label'})")
    print(f"  sources  : {', '.join(p.stem for p in golden_paths if p.exists())}")
    print()

    results: list[dict] = []
    latencies: list[float] = []
    total_in = total_out = 0
    envelope_confirmed = True

    for index, post in enumerate(selected, start=1):
        caption = (post.get("caption") or "").strip()
        gold = post.get("post_type") or "unlabeled"
        post_id = post.get("post_id") or post.get("id") or "?"

        if not caption:
            print(f"[{index}] {post_id}  (empty caption - skipped)")
            continue

        try:
            body, elapsed_ms = ask_jev(caption, args.model, headers)
        except Exception as exc:  # noqa: BLE001 - probe script, report and stop
            # redact_tokens(), never a bare exc. This call site sits outside
            # complete_structured() and logs its own errors, which is exactly
            # the shape both previous credential leaks in this project had.
            print(f"\n[{index}] {post_id} FAILED: {redact_tokens(exc)}")
            print("\nStopping - fix this before sending the rest.")
            return 1

        if index == 1 or args.raw_all:
            print("-" * 78)
            print(f"RAW RESPONSE (post {post_id}) - the point of this exercise:")
            print(json.dumps(body, indent=2, ensure_ascii=False))
            print("-" * 78)
            print()

        answer = (body.get("answers") or {}).get(QUESTION_ID) or {}
        choice = answer.get("choice")
        probabilities = answer.get("probabilities") or {}
        confidence = answer.get("confidence")

        if choice is None:
            envelope_confirmed = False
            print(f"[{index}] {post_id}  !! no 'answers.{QUESTION_ID}.choice' in the response.")
            print("    The envelope differs from what the plan assumed. That difference IS")
            print("    the finding - the integration gets written against the shape above.")
            print(json.dumps(body, indent=2, ensure_ascii=False))
            return 1

        in_tok, out_tok = usage_tokens(body)
        total_in += in_tok
        total_out += out_tok
        latencies.append(elapsed_ms)
        hit = choice == gold
        results.append({"hit": hit, "confidence": confidence})

        mark = "OK  " if hit else "MISS"
        conf_text = f"{confidence:.3f}" if isinstance(confidence, (int, float)) else str(confidence)
        print(f"[{index}] {mark} {post_id}  ({post.get('_vendor')})  {elapsed_ms:7.1f} ms")
        print(f"      gold={gold}   jev={choice}   confidence={conf_text}")
        print(f"      \"{safe(caption[:88])}...\"")
        if probabilities:
            ordered = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)
            spread = "  ".join(f"{k}={v:.3f}" for k, v in ordered)
            print(f"      {spread}")
            total_p = sum(probabilities.values())
            print(f"      options={len(probabilities)}  sum={total_p:.4f}")
        print()

    if not results:
        print("No posts were sent.")
        return 1

    # ---- summary -----------------------------------------------------------
    hits = sum(1 for r in results if r["hit"])
    confs = [r["confidence"] for r in results if isinstance(r["confidence"], (int, float))]
    hit_confs = [r["confidence"] for r in results
                 if r["hit"] and isinstance(r["confidence"], (int, float))]
    miss_confs = [r["confidence"] for r in results
                  if not r["hit"] and isinstance(r["confidence"], (int, float))]

    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"  envelope confirmed : {'yes' if envelope_confirmed else 'NO'}")
    print(f"  agreement w/ gold  : {hits}/{len(results)}  "
          f"(NOT a score - n is tiny and this is not a gated run)")
    if confs:
        print(f"  mean confidence    : {statistics.fmean(confs):.3f}")
    if hit_confs and miss_confs:
        print(f"    on hits          : {statistics.fmean(hit_confs):.3f}")
        print(f"    on misses        : {statistics.fmean(miss_confs):.3f}")
        print("    ^ the number the whole Jev question turns on: confidence should be")
        print("      LOWER on misses. Gemini's was not - it was wrong at 0.8-0.95.")
    elif miss_confs:
        print(f"    on misses        : {statistics.fmean(miss_confs):.3f}  (no hits to compare)")
    elif hit_confs:
        print(f"    on hits          : {statistics.fmean(hit_confs):.3f}  (no misses to compare)")

    print(f"  latency ms         : min {min(latencies):.0f}  "
          f"median {statistics.median(latencies):.0f}  max {max(latencies):.0f}")
    print("    ^ a probe, not a benchmark: includes connection setup, one machine,")
    print(f"      one network, n={len(latencies)}. Vendor claims 70-500 ms.")
    print(f"  tokens             : {total_in} in, {total_out} out")
    print(f"  est. cost          : ${total_in / 1_000_000 * INPUT_PRICE_PER_MTOK:.6f}  "
          f"(input only at ${INPUT_PRICE_PER_MTOK}/M; Jev output is free)")
    print()
    print("  Nothing was written. report/token_log.csv is untouched by design.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
