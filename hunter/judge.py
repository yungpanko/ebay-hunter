"""Judging listings: a cheap thumbnail triage over every new listing, then a careful photo-by-photo review
of the ones worth a closer look."""

from __future__ import annotations

import json
from typing import Any

import yaml

from . import llm
from .ebay import resize_image

PERSONA = """You are this person's secondhand shopping companion: a friend who knows their taste, fit and \
history, has done the research, and helps them buy with confidence. You're judging eBay listings found by one of \
their saved searches. Great pieces list every day, often underpriced because sellers miss details (a Made in USA \
tag only visible in a photo, a mislabeled size, an era the seller didn't recognise). Their attention is the \
scarce thing: surface what's worth it, skip what isn't, and say why in a sentence they can act on."""


def context_block(profile: str, search: dict[str, Any], feedback: list[dict[str, Any]]) -> str:
    search_view = {k: v for k, v in search.items() if k not in {"created", "updated", "active"}}
    fb_lines = []
    for f in feedback[-40:]:
        fb_lines.append(f"- [{f.get('reaction', 'note')}] {f.get('title', '')} ({f.get('price', '')}): "
                        f"{f.get('note', '')}".strip())
    return (
        f"## Their profile\n\n{profile.strip()}\n\n"
        f"## The saved search\n\n{yaml.safe_dump(search_view, sort_keys=False, allow_unicode=True)}\n"
        f"## Their recent reactions to things you've shown them (newest last)\n\n"
        + ("\n".join(fb_lines) if fb_lines else "(none yet)")
    )


def system_for(profile: str, search: dict[str, Any], feedback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Stable per search for a whole run, so it caches across every item in the run.
    return [{"type": "text", "text": PERSONA + "\n\n" + context_block(profile, search, feedback)}]


# ---- stage 1: triage --------------------------------------------------------

TRIAGE_SCHEMA = llm.obj({
    "items": llm.arr(llm.obj({
        "item_id": llm.STR,
        "look_closer": llm.BOOL,
        "reason": llm.STR,
    })),
})

TRIAGE_INSTRUCTIONS = """Below are new listings, each with its title, price, condition and main photo. Decide \
which deserve a closer look (all photos, item specifics, description). Be generous: titles and specifics are \
often wrong or missing, and an off-brief piece that fits their taste is welcome. Only skip what's clearly wrong: \
the wrong kind of item, clearly outside their size or budget, an obvious reproduction or fast fashion when they \
want vintage, or something their reactions say they don't like. Return one entry per listing."""


def triage(c, model: str, system, items: list[dict[str, Any]], thumb_size: int = 300) -> dict[str, dict]:
    if not items:
        return {}
    content: list[dict[str, Any]] = [{"type": "text", "text": TRIAGE_INSTRUCTIONS}]
    for it in items:
        content.append({"type": "text", "text": json.dumps({
            "item_id": it["item_id"], "title": it["title"], "price": it["price"],
            "shipping": it["shipping"], "condition": it["condition"],
            "buying_options": it["buying_options"],
        })})
        if it["images"]:
            content.append({"type": "image", "source": {"type": "url", "url": resize_image(it["images"][0], thumb_size)}})
    out = llm.structured(c, model=model, system=system, content=content, schema=TRIAGE_SCHEMA,
                         effort="low", max_tokens=8000)
    return {r["item_id"]: r for r in out["items"]}


# ---- stage 2: deep review ---------------------------------------------------

VERDICT_SCHEMA = llm.obj({
    "verdict": {"type": "string", "enum": ["strong_buy", "worth_a_look", "long_shot", "pass"]},
    "score": {"type": "integer", "description": "1-10: how worth their time this is, all things considered."},
    "headline": {**llm.STR, "description": "One short line (under 15 words), friend-to-friend, on why this is (or "
                 "isn't) worth their time."},
    "why": {**llm.arr(llm.STR), "description": "2-5 short reasons tied to their criteria, taste and fit."},
    "photo_findings": {**llm.arr(llm.STR), "description": "Details you saw in photos that the title or specifics "
                       "don't mention (tags, labels, construction, wear, flaws)."},
    "fit": {**llm.STR, "description": "How the stated or visible measurements compare to their ranges."},
    "price_take": {**llm.STR, "description": "Steal / fair / high, and why, including shipping."},
    "concerns": llm.arr(llm.STR),
    "missing_info": {**llm.arr(llm.STR), "description": "Facts you'd need before buying that the listing lacks."},
    "seller_question": {**llm.STR, "description": "A short, polite message to the seller asking for the missing "
                        "info, or an empty string if nothing is missing."},
    "off_brief_gem": {**llm.BOOL, "description": "True if it doesn't match the brief but is worth showing anyway."},
})

REVIEW_INSTRUCTIONS = """Review this listing closely for them. Read every photo: tags, care labels, country of \
origin, RN numbers, stitching, hardware, fabric, fades, flaws, and any measurement shown in a photo. Compare with \
the search's must-haves, knowledge notes, measurement ranges and price guidance, and with their reactions so far. \
Measurements beat tag sizes. If a key measurement or detail is missing, say so and draft a seller question.

Verdicts: "strong_buy" means act now, it fits and the price is right. "worth_a_look" means promising with a \
question or tradeoff. "long_shot" means it could be great but key facts are uncertain (missing measurements, \
unclear era), or it's an off-brief gem they might love. "pass" means not for them. The score (1-10) ranks finds \
within a roundup email: 9-10 drop everything, 7-8 strong, 5-6 maybe, below 5 pass. Be honest: a pass is a \
useful answer, and crying wolf costs their trust."""


def review(c, model: str, system, item: dict[str, Any], max_photos: int = 8, photo_size: int = 1200) -> dict[str, Any]:
    listing = {k: v for k, v in item.items() if k not in {"images", "price_value"}}
    content: list[dict[str, Any]] = [{"type": "text", "text": REVIEW_INSTRUCTIONS},
                                     {"type": "text", "text": "Listing:\n" + json.dumps(listing, indent=1)}]
    for i, url in enumerate(item["images"][:max_photos], 1):
        content.append({"type": "text", "text": f"Photo {i}:"})
        content.append({"type": "image", "source": {"type": "url", "url": resize_image(url, photo_size)}})
    return llm.structured(c, model=model, system=system, content=content, schema=VERDICT_SCHEMA, effort="medium")


# ---- learning ---------------------------------------------------------------

LEARN_SCHEMA = llm.obj({"learned": {**llm.STR, "description": "The full new 'Learned from feedback' section as "
                                    "Markdown bullets."}})


def learn(c, model: str, profile: str, feedback: list[dict[str, Any]]) -> str:
    """Distil their reactions into durable preferences in the profile."""
    lines = "\n".join(
        f"- [{f.get('reaction', 'note')}] {f.get('search', '')} | {f.get('title', '')} ({f.get('price', '')}): "
        f"{f.get('note', '')}" for f in feedback[-200:])
    content = [{"type": "text", "text": (
        "Here is their profile and every reaction they've given to listings you showed them. Rewrite the "
        "'Learned from feedback' section: concise bullets about taste, fit, price comfort and dealbreakers that "
        "the reactions show, grouped by category where it differs. Keep earlier lessons unless newer reactions "
        "contradict them. Don't repeat what other sections already say.\n\n"
        f"## Profile\n\n{profile}\n\n## Reactions\n\n{lines}")}]
    out = llm.structured(c, model=model, system=PERSONA, content=content, schema=LEARN_SCHEMA, effort="medium")
    return out["learned"]
