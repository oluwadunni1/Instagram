"""
Golden-set skeleton generator.

Reads the latest raw dump for an account (from ingest.py) and emits
a labeling template - one entry per post, pre-filled with everything
we already know (caption, permalink, media type, comments) so you
only have to fill in the judgment fields by eye.

Usage:
    uv run eval/make_golden_skeleton.py vendor_autos_01

Output:
    eval/golden/vendor_autos_01.json

Labeling workflow:
    1. Run this script.
    2. Open the output file.
    3. For each post, open its "permalink" in a browser and fill in
       the fields marked null/empty - see FIELD GUIDE below.
    4. Save. This file becomes your ground truth for eval/harness.py.

SCHEMA SHAPE:
    Each post has post-level fields (post_type, carousel_classification)
    plus a "products" LIST - brief section 3 requires post->products to
    be 1:N from day one, since a single post (especially a carousel)
    can contain more than one distinct item. Most posts will have
    exactly one entry in "products"; true multi-product carousels have
    more than one, in slide order.

FIELD GUIDE:
    post_type               one of: product_listing | announcement |
                             testimonial_repost | meme_personal | ad_creative
    carousel_classification    one of: not_carousel | gallery | colorway |
                             multi_product. Only meaningful when the raw
                             dump's has_carousel_children was true.
                             "gallery" = one product, many angles (order
                             preserved, slide 1 = hero). "colorway" = one
                             product, each slide a variant (treated as
                             gallery for extraction in this POC).
                             "multi_product" = genuinely different items
                             per slide - this is the only case where
                             "products" should have more than one entry.
    expected_flag              three-way: null = should auto-import
                             (confident, no issue), "auto_exclude" =
                             confidently not a product post, or a short
                             reason string (e.g. "missing_price") = needs
                             human review. Grades Stage 5's routing
                             decision, not just extraction correctness.
    notes                      anything odd - Pidgin, price-on-image,
                             multi-product carousel, etc. This is your
                             moat data (brief section 8) - over-document
                             rather than under.

    Per entry in "products" (field names match the brief's Stage 3
    schema in section 3 exactly, so nothing needs renaming later):
      name                      string
      description               free-text summary in the vendor's own
                             framing - what they said, lightly cleaned up.
      variants                  list of {"type": "color|size|style",
                             "values": [...]} - buyer-selectable OPTIONS
                             only. Fixed facts about one specific unit
                             (mileage, condition) go in description or
                             attributes, not here.
      price                     {"value": number|null, "currency": str|null,
                             "source": "caption|image|comment|none"}.
                             source: "none" is a valid, important output -
                             never invent a price (brief section 11). Look
                             beyond the caption text (image overlay,
                             comments) before labeling "none" - but never
                             manufacture a number that isn't genuinely
                             present somewhere in what the vendor posted.
                             Do NOT hand-label a "confidence" sub-field -
                             that's the model's own self-assessment, it
                             doesn't exist until a real model runs.
      quantity_signal            {"kind": "explicit_count|low_stock|restock|
                             none", "evidence": "<quoted text, or null>"}.
                             Evidence-based (Stage 4), not a lifecycle
                             state - keep the quote, it matters later
                             (brief section 7: auto-applied changes must
                             carry their evidence).
      negotiation_signal          one of: negotiable | fixed | unknown. Only
                             mark fixed/negotiable when the caption states
                             it EXPLICITLY. Silence is "unknown", not an
                             inferred default - same "never invent" logic
                             as price.

      -- extensions beyond the brief's base schema (note these in your
         findings report as deliberate additions, not core spec) --
      category                  OPTIONAL - only fill if this vendor's
                             account genuinely mixes categories.
      attributes                OPEN key-value dict of structured facts -
                             category-agnostic on purpose (cars might have
                             mileage/transmission, fashion might have
                             material/fit - don't assume fixed keys).
      accepts_swap               true|false|null - captures "swap possible"
                             language distinct from negotiable/fixed.
      staleness_verdict          one of: active | stale_suspected |
                             out_of_stock | unknown.

    NOT hand-labeled: any "confidence"/"extraction_confidence" field
    (model self-assessment only, compared later per brief section 11's
    calibration check - not ground truth you supply).

    NOTE: "images" are pre-filled from the raw dump into each product's
    images[] automatically. For multi-product posts, you'll need to
    redistribute the images across the correct products by hand.
    There is NO separate "available_images" field at the post level.
"""

import json
import sys
from pathlib import Path

def make_product_entry(images: list | None = None) -> dict:
    """One product skeleton, EXACT top-level field match to the brief's
    section 3 schema, with our extensions cleanly nested out of the way
    so a side-by-side comparison against the brief is unambiguous.

    images is pre-filled with ALL post images by default (correct for
    single-product posts). For multi-product posts, the labeller must
    duplicate this entry and redistribute images across products."""
    return {
        "name": None,
        "description": None,
        "images": images or [],
        "variants": [],
        "price": {
            "value": None,
            "currency": None,
            "source": None,
            "confidence": None,  # MODEL-ONLY - leave null, never hand-label
            # min/max: OPTIONAL extension - only fill when the caption states
            # a genuine range (e.g. "$34,000 - $38,000"). Leave both null for
            # a single fixed price; value alone is still your primary number.
            "min": None,
            "max": None,
        },
        "quantity_signal": {
            "kind": None,  # explicit_count|low_stock|restock|none, plus domain
                            # extensions when justified (e.g. "pre_order" for a
                            # vendor selling against future stock, not existing
                            # units) - document any such addition in notes.
            "evidence": None,
            "source": None,  # caption|comment|none - mirrors price.source, extension beyond brief's 2-key version
        },
        "negotiation_signal": None,  # negotiable|fixed|unknown - "unknown" exactly,
                                       # not "unspecified"/"n/a" - harness does literal
                                       # string comparison, synonyms will silently miss
        "extraction_confidence": None,  # MODEL-ONLY - leave null, never hand-label

        "extensions": {
            "category": None,          # only fill if account genuinely mixes categories
            "attributes": {},          # open key-value structured facts, category-agnostic
            "accepts_swap": None,      # true|false|null - Nigerian-market negotiation nuance
            "staleness": {
                "verdict": None,       # active|stale_suspected|out_of_stock|unknown
                "evidence": None,      # quoted comment/caption text, or null
                "source": None,        # caption|comment|none
            },
        },
    }


def collect_images(post: dict) -> list:
    """All images for this post, in order - ONE image for a normal post,
    N slides for a carousel. Pre-filled into the product's images[]."""
    images = []
    if post.get("media_url"):
        images.append(post["media_url"])
    for child in post.get("children", {}).get("data", []):
        if child.get("media_url"):
            images.append(child["media_url"])
    return images



def build_comments_list(post: dict, vendor_username: str | None) -> list:
    """Structured {username, text, is_vendor_reply} per comment - not just
    bare text. The vendor's OWN reply ("sold", "gone") is a much stronger
    staleness/stock signal than the same word from a random commenter
    (brief section 4/section 7), so this flag matters downstream, not
    just for display."""
    return [
        {
            "username": c.get("username"),
            "text": c.get("text"),
            "is_vendor_reply": bool(vendor_username) and c.get("username") == vendor_username,
        }
        for c in post.get("comments", [])
    ]


def latest_dump(account_label: str) -> Path:
    raw_dir = Path("runs") / account_label / "raw"
    dumps = sorted(raw_dir.glob("dump_*.json"))
    if not dumps:
        raise SystemExit(f"No raw dumps found in {raw_dir}. Run ingest.py first.")
    return dumps[-1]


def build_skeleton(account_label: str) -> None:
    dump_path = latest_dump(account_label)
    data = json.loads(dump_path.read_text(encoding="utf-8"))
    vendor_username = data.get("profile", {}).get("username")

    entries = []
    for post in data["media"]:
        is_carousel = "children" in post
        entries.append({
            # --- known, pre-filled from the raw dump ---
            "post_id": post["id"],
            "media_type": post.get("media_type"),
            "caption": post.get("caption", ""),
            "permalink": post.get("permalink"),
            "timestamp": post.get("timestamp"),
            "has_carousel_children": is_carousel,
            "comment_count": len(post.get("comments", [])),
            "comments": build_comments_list(post, vendor_username),

            # --- fields YOU fill in by hand ---
            "post_type": None,
            "carousel_classification": None if is_carousel else "not_carousel",
            "expected_flag": None,
            "notes": "",

            # One entry per distinct product on this post - could be 1
            # (most posts) or several (multi-product carousel, or a
            # single-image lineup post advertising more than one item).
            # For multi-product posts, duplicate the product entry and
            # redistribute images across products by hand.
            # Delete this list's single starter entry if the post isn't
            # a product at all (e.g. testimonial, promotional).
            "products": [make_product_entry(images=collect_images(post))],
        })

    out_dir = Path("eval") / "golden"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{account_label}.json"

    if out_path.exists():
        print(f"[warn] {out_path} already exists - not overwriting. "
              f"Delete it first if you want a fresh skeleton.")
        return

    out_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Skeleton written: {out_path} ({len(entries)} posts to label)")
    print("Open each permalink, fill in the null fields, save.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: uv run eval/make_golden_skeleton.py <account_label>")
        sys.exit(1)
    build_skeleton(sys.argv[1])