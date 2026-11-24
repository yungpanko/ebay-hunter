"""Search builder: a conversation that turns a loose idea into a researched, saved search."""

from __future__ import annotations

import json
from typing import Any, Callable

import yaml

from . import llm
from .config import Settings
from .ebay import EbayClient, EbayError, summarize_item
from .schema import SEARCH_SCHEMA, search_from_tool_input
from .store import Store

SYSTEM = """You are a sharp, friendly secondhand clothing shopping companion. You help one person turn a loose \
idea ("vintage jeans", "a good chore coat") into a refined, researched eBay search that a watcher will rerun \
every couple of hours. You talk like a knowledgeable friend who shops with them, not a form.

How to work:
- Meet them where they are. Never assume how much they know about this category. Their first message and the way \
they answer tell you whether they're an expert who wants a precise query fast, or someone who'd value the \
education. Match that. Experts get sharper questions; newcomers get a bit more context, still briefly.
- Keep it short. This is a chat, not an article. Ask ONE question per turn, in two or three sentences at most, \
like a text from a friend. Give the why in a single short clause (e.g. "Made in USA matters to you? Those \
pre-2003 501s are the better denim, and sellers often miss the tag."), and go deeper only if they ask. No \
headers, no long bullet lists, no measurement guides unless they want one. Short answer options are fine \
("pre-2003, 90s, or any era?").
- Never let it feel like a form. Once you have enough for a decent search (often after two or three answers), say so in a short line and let them choose: keep refining, or save now. For example: "I have enough to start watching. A couple more details would sharpen it, or I can save it now and fill the rest with sensible defaults." Offer that again every few turns, not after every question. If they want to go ahead, or answer vaguely, don't push: fill the gaps with sensible defaults, say what you assumed in one line, and save. They can always refine later with `hunter edit`.
- Research when it helps. Use web search to check facts, date ranges, identifying tags and typical prices rather \
than guessing, but share only the one or two findings that matter, in a sentence. If eBay tools are available, use them to find the right \
category, check the exact item-specific names, and preview what a query actually returns, then adjust.
- Fit means measurements, not tag sizes. Vintage sizing is unreliable. Ask for body measurements or, better, \
measurements of a garment that fits them well, plus how they like it to fit, then turn that into garment \
measurement ranges with sensible ease. Ask for one measurement at a time.
- Taste is open-ended. Colors, eras, moods, fades, wear, brands: capture it in their words, don't force categories.
- Leave room for gems. Sellers mislabel and under-describe; that's where deals are. Keep eBay filters loose enough \
to catch badly titled listings, add a broad query or two, and put the precise requirements in must-haves and \
knowledge so the judge (which reads photos) can catch what the title missed.
- Price: help them set a realistic range and say what a steal looks like, based on research.
- Anything about the person that will matter beyond this search (measurements, general taste, how they buy), save \
with update_profile so future searches start smarter.

When it's ready, sum the search up in a few short lines and ask if it looks right, then call save_search. Don't save before \
they say it looks right. After saving, confirm in one sentence. Don't say when watching starts or promise emails; the app prints the next steps itself."""


def _tools(has_ebay: bool) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 8},
        {
            "name": "save_search",
            "description": "Save the finished search so the watcher starts running it. Call only after the user "
                           "agrees the search looks right.",
            "strict": True,
            "input_schema": SEARCH_SCHEMA,
        },
        {
            "name": "update_profile",
            "description": "Replace one section of the user's long-lived profile (measurements, taste, buying "
                           "style). Write the full new section text, merging what's already there.",
            "strict": True,
            "input_schema": llm.obj({
                "section": {"type": "string", "enum": ["Measurements and fit", "Taste", "Buying style"]},
                "content": llm.STR,
            }),
        },
    ]
    if has_ebay:
        tools += [
            {
                "name": "ebay_category_suggestions",
                "description": "Find eBay category ids for a phrase like 'mens jeans'.",
                "input_schema": llm.obj({"query": llm.STR}),
            },
            {
                "name": "ebay_category_aspects",
                "description": "List the item-specific names and common values eBay uses in a category, so "
                               "aspect filters use exact spellings.",
                "input_schema": llm.obj({"category_id": llm.STR}),
            },
            {
                "name": "ebay_preview_search",
                "description": "Run a draft query against live eBay and see the total count and the newest titles "
                               "and prices. Use it to check a query isn't too narrow or too noisy.",
                "input_schema": SEARCH_PREVIEW_SCHEMA,
            },
        ]
    return tools


SEARCH_PREVIEW_SCHEMA = llm.obj({
    "query": llm.STR,
    "category_ids": llm.arr(llm.STR),
    "filters": SEARCH_SCHEMA["properties"]["filters"],
    "aspects": SEARCH_SCHEMA["properties"]["aspects"],
})


class Builder:
    def __init__(self, settings: Settings, store: Store, *, claude=None, ebay: EbayClient | None = None,
                 ask: Callable[[str], str] = input, say: Callable[[str], None] = print):
        self.s = settings
        self.store = store
        self.claude = claude or llm.client()
        self.ebay = ebay
        if self.ebay is None and settings.has_ebay:
            self.ebay = EbayClient(settings)
        self.ask = ask
        self.say = say
        self.saved_path = None

    def _system(self, existing: dict[str, Any] | None) -> list[dict[str, Any]]:
        extra = f"\n\nThe user's profile so far:\n\n{self.store.read_profile()}"
        if existing:
            extra += ("\n\nThey are revising this existing search. Keep its slug when you save:\n\n"
                      + yaml.safe_dump(existing, sort_keys=False))
        return [{"type": "text", "text": SYSTEM + extra}]

    # ---- tool execution ---------------------------------------------------
    def _run_tool(self, name: str, args: dict[str, Any], existing: dict[str, Any] | None) -> str:
        if name == "save_search":
            search = search_from_tool_input(args)
            if existing and existing.get("slug"):
                search["slug"] = existing["slug"]
                search["created"] = existing.get("created")
            self.saved_path = self.store.save_search(search)
            return f"Saved to {self.saved_path}. The watcher will pick it up on its next run."
        if name == "update_profile":
            self.store.update_profile_section(args["section"], args["content"])
            return "Profile updated."
        if not self.ebay:
            return "eBay tools are not configured."
        try:
            if name == "ebay_category_suggestions":
                return json.dumps(self.ebay.category_suggestions(args["query"]))
            if name == "ebay_category_aspects":
                aspects = self.ebay.category_aspects(args["category_id"])
                return json.dumps(aspects[:40])
            if name == "ebay_preview_search":
                aspects = {a["name"]: a["values"] for a in args.get("aspects", [])}
                res = self.ebay.search(args["query"], args.get("category_ids"), args.get("filters"), aspects,
                                       limit=20)
                items = [summarize_item(i) for i in res.get("itemSummaries", [])]
                return json.dumps({
                    "total": res.get("total", 0),
                    "newest": [{"title": i["title"], "price": i["price"], "condition": i["condition"]}
                               for i in items],
                })
        except EbayError as e:
            return f"eBay error: {e}"
        return f"Unknown tool {name}"

    # ---- conversation ------------------------------------------------------
    def chat(self, opening: str, existing: dict[str, Any] | None = None, max_turns: int = 60):
        messages: list[dict[str, Any]] = [{"role": "user", "content": opening}]
        system = self._system(existing)
        tools = _tools(self.ebay is not None)
        for _ in range(max_turns):
            resp = llm.create(
                self.claude,
                model=self.s.model,
                max_tokens=16000,
                system=system,
                tools=tools,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                cache_control={"type": "ephemeral"},
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})
            # Web search citations split one paragraph into several text blocks; join them back inline.
            said = "".join(b.text for b in resp.content if b.type == "text").strip()
            if said:
                self.say(said)
            if resp.stop_reason == "pause_turn":  # long server-side web search; let it continue
                continue
            if resp.stop_reason == "tool_use":
                results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        out = self._run_tool(block.name, block.input, existing)
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
                messages.append({"role": "user", "content": results})
                continue
            if resp.stop_reason == "refusal":
                self.say("(Claude declined to continue this conversation.)")
                return self.saved_path
            if self.saved_path:
                return self.saved_path
            reply = self.ask("\nyou> ").strip()
            if reply.lower() in {"", "quit", "exit", "q"}:
                return self.saved_path
            messages.append({"role": "user", "content": reply})
        return self.saved_path
