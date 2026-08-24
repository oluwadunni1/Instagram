"""
Stage 3 - Product extraction (DUMMY / heuristic stub).

Same purpose as stage2_triage.py - zero-cost placeholder to exercise
the harness's extraction scoring before a real model is wired in.
Naive regex-based price pull, nothing more.

Returns a LIST of product dicts, matching the golden-set schema's
post->products (1:N) shape. This dummy stub never attempts carousel
classification - it always returns exactly one product, which is
the brief's own sanctioned fallback for a finicky/unbuilt carousel
classifier (section 3: "every carousel attaches all slides as one
product's gallery"). Real Stage 3 must be able to return more than
one product for true multi-product carousels.
"""

import re

NAIRA_RE = re.compile(r"\u20a6\s?([\d,]+)")
USD_RE = re.compile(r"\$\s?([\d,]+)")


def extract_product(post: dict, profile: dict | None = None) -> list:
    caption = post.get("caption") or ""

    naira_match = NAIRA_RE.search(caption)
    usd_match = USD_RE.search(caption)

    if naira_match:
        price_value = int(naira_match.group(1).replace(",", ""))
        price_currency = "NGN"
        price_source = "caption"
    elif usd_match:
        price_value = int(usd_match.group(1).replace(",", ""))
        price_currency = "USD"
        price_source = "caption"
    else:
        # Never invent a price - brief section 11
        price_value, price_currency, price_source = None, None, "none"

    return [{
        "product_name": None,  # dummy stub doesn't attempt name extraction yet
        "price": {"value": price_value, "currency": price_currency, "source": price_source},
        "extraction_confidence": 0.4,
    }]