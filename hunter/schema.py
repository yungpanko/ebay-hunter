"""The shape of a saved search, shared by the builder (which writes it) and the watcher (which runs it)."""

from __future__ import annotations

from typing import Any

from .llm import BOOL, NUM, STR, arr, nullable, obj

SEARCH_SCHEMA: dict[str, Any] = obj({
    "name": {**STR, "description": "Short human name, e.g. 'Made in USA Levi's 501s, 32-33 waist'."},
    "idea": {**STR, "description": "What the user first said they wanted, in their words."},
    "summary": {**STR, "description": "Two or three sentences: what we're hunting and why it matters to this user."},
    "queries": {**arr(STR), "description": "1-6 eBay keyword queries. Mix precise ones with looser ones that catch "
                "poorly titled listings (sellers who miss details are where deals hide)."},
    "category_ids": {**arr(STR), "description": "eBay category ids. Use exactly one if aspects are set."},
    "filters": obj({
        "price_min": nullable(NUM),
        "price_max": nullable(NUM),
        "currency": STR,
        "buying_options": arr({"type": "string", "enum": ["FIXED_PRICE", "AUCTION", "BEST_OFFER"]}),
        "conditions": arr({"type": "string", "enum": ["NEW", "USED", "UNSPECIFIED"]}),
        "item_location_country": nullable(STR),
        "delivery_country": nullable(STR),
        "returns_accepted": BOOL,
    }),
    "aspects": {**arr(obj({"name": STR, "values": arr(STR)})),
                "description": "eBay item specifics to filter on, using eBay's exact names and values. Keep this "
                "loose: sellers often leave specifics blank, so over-filtering hides gems."},
    "exclude_keywords": {**arr(STR), "description": "Words that rule a title out (e.g. 'reproduction', 'kids')."},
    "must_haves": arr(STR),
    "nice_to_haves": arr(STR),
    "dealbreakers": arr(STR),
    "measurements": {**arr(obj({
        "name": {**STR, "description": "e.g. 'waist (measured flat, doubled)', 'inseam', 'pit to pit'."},
        "min": nullable(NUM),
        "max": nullable(NUM),
        "unit": STR,
        "note": {**STR, "description": "How to measure / how much ease, and where this range came from."},
    })), "description": "Garment measurement ranges. Measurements, not tag sizes: vintage sizing is unreliable."},
    "fit_notes": STR,
    "price_guidance": {**STR, "description": "What counts as a steal, fair, or too much for this, and why."},
    "knowledge": {**arr(STR), "description": "Researched identifying details the judge should look for in photos "
                  "(tags, stitching, buttons, labels, date markers) and what they mean."},
    "gem_notes": {**STR, "description": "What kinds of off-brief finds the user would still want to see."},
})


def search_from_tool_input(data: dict[str, Any]) -> dict[str, Any]:
    """Tool input -> the YAML layout we store (aspects become a simple mapping)."""
    out = dict(data)
    out["aspects"] = {a["name"]: a["values"] for a in data.get("aspects", []) if a.get("values")}
    f = dict(out.get("filters") or {})
    out["filters"] = {k: v for k, v in f.items() if v not in (None, [], "", False)}
    return out
