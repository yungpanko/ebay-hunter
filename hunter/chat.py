"""Talk a find through: sizing confidence, comparisons, market context and a buying plan.

One conversation per find, stored as plain JSON next to the rest of the state.
"""

from __future__ import annotations

import json
import re
import statistics
from typing import Any

import yaml

from . import llm
from .config import Settings
from .ebay import EbayClient, EbayError, resize_image, summarize_item
from .store import Store, now_iso

SYSTEM = """You are this person's secondhand shopping companion, talking through one eBay find you surfaced in \
their roundup. You know their profile, the saved search, your own review of this listing, and the other finds in \
the same roundup. Be the friend who has done the homework: direct, honest, and brief. Short paragraphs, a few \
bullets at most, no headers. Lead with the answer.

What you help with:
- Will it fit? Compare the listing's measurements (and anything readable in the photos) with their ranges and \
garments that fit. Say how confident you are and what would raise confidence; draft a seller question if needed.
- Is it real / what is it? Use what's visible in the photos and research identifying details when useful.
- How does it compare? Weigh it against the other finds in this roundup and against active listings.
- What's it worth? Use market_comparables for similar active listings, and web search for what such pieces \
usually sell for. Note that eBay's API has no sold prices; for those they can tick "Sold items" on eBay.
- How to buy it well. Refresh the listing first (price, bids, time left, whether Best Offer is on). Then give a \
concrete plan: for Best Offer, an opening offer and a walk-away number with the reasoning (sellers often accept \
roughly 70-85% of ask on slow listings; a polite note helps); for auctions, a true maximum including shipping and \
a plan to place it once, late, using eBay's automatic bidding instead of bidding up in small steps; for Buy It \
Now, whether to just buy or ask a question first.
- You never buy, bid or message sellers yourself. They decide and act; you advise.

When they react ("love it", "too short", "bought it", "not for me"), log it with record_feedback so future \
searches learn, and say so in a few words."""

TOOLS: list[dict[str, Any]] = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 4},
    {
        "name": "refresh_listing",
        "description": "Get the live state of this listing: current price or bid, bid count, time left, buying "
                       "options (including Best Offer), and availability.",
        "input_schema": llm.obj({}),
    },
    {
        "name": "market_comparables",
        "description": "Search active eBay listings similar to this one and get price stats plus examples.",
        "input_schema": llm.obj({"query": llm.STR, "max_price": llm.nullable(llm.NUM)}),
    },
    {
        "name": "record_feedback",
        "description": "Log their reaction to this find so the hunter learns their taste.",
        "strict": True,
        "input_schema": llm.obj({
            "reaction": {"type": "string", "enum": ["liked", "passed", "bought", "note"]},
            "note": llm.STR,
        }),
    },
]


class FindNotFound(LookupError):
    pass


def chat_key(roundup_id: str, n: int) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"{roundup_id}-{n}")


class FindChat:
    def __init__(self, settings: Settings, store: Store, *, claude=None, ebay: EbayClient | None = None):
        self.s = settings
        self.store = store
        self._claude = claude
        self._ebay = ebay

    @property
    def claude(self):
        if self._claude is None:
            self._claude = llm.client()
        return self._claude

    @property
    def ebay(self) -> EbayClient | None:
        if self._ebay is None and self.s.has_ebay:
            self._ebay = EbayClient(self.s)
        return self._ebay

    # ---- data -------------------------------------------------------------
    def roundup(self, roundup_id: str) -> dict[str, Any]:
        for r in self.store.read_jsonl("roundups.jsonl"):
            if r["id"] == roundup_id:
                return r
        raise FindNotFound(roundup_id)

    def find(self, roundup_id: str, n: int) -> dict[str, Any]:
        for it in self.roundup(roundup_id)["items"]:
            if it["n"] == n:
                return it
        raise FindNotFound(f"{roundup_id}/{n}")

    def _path(self, roundup_id: str, n: int):
        d = self.s.state_dir / "chats"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{chat_key(roundup_id, n)}.json"

    def history(self, roundup_id: str, n: int) -> list[dict[str, str]]:
        p = self._path(roundup_id, n)
        return json.loads(p.read_text()) if p.exists() else []

    def _save(self, roundup_id: str, n: int, history: list[dict[str, str]]) -> None:
        self._path(roundup_id, n).write_text(json.dumps(history, indent=1, ensure_ascii=False))

    # ---- context ----------------------------------------------------------
    def _context(self, roundup: dict[str, Any], find: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            search = self.store.load_search(find["search"])
        except FileNotFoundError:
            search = {"slug": find["search"]}
        search = {k: v for k, v in search.items() if k not in {"created", "updated", "active"}}
        listing = {k: v for k, v in (find.get("listing") or {}).items() if k not in {"images", "price_value"}}
        others = [{"n": o["n"], "title": o["title"], "price": o["price"], "verdict": o["verdict"],
                   "score": o.get("score"), "headline": (o.get("review") or {}).get("headline"),
                   "fit": (o.get("review") or {}).get("fit")}
                  for o in roundup["items"] if o["n"] != find["n"]]
        text = (
            f"## Their profile\n\n{self.store.read_profile()}\n\n"
            f"## The saved search\n\n{yaml.safe_dump(search, sort_keys=False, allow_unicode=True)}\n"
            f"## This find (#{find['n']} in the roundup)\n\n{json.dumps(listing, indent=1, ensure_ascii=False)}\n\n"
            f"## Your review of it\n\n{json.dumps(find.get('review') or {}, indent=1, ensure_ascii=False)}\n\n"
            f"## Other finds in the same roundup\n\n{json.dumps(others, indent=1, ensure_ascii=False)}\n\n"
            "## Its photos follow."
        )
        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for url in ((find.get("listing") or {}).get("images") or [])[: self.s.max_photos]:
            blocks.append({"type": "image", "source": {"type": "url", "url": resize_image(url, 1200)}})
        return blocks

    # ---- tools ------------------------------------------------------------
    def _tool(self, name: str, args: dict[str, Any], find: dict[str, Any]) -> str:
        if name == "record_feedback":
            self.store.add_feedback({"item_id": find["item_id"], "search": find["search"], "title": find["title"],
                                     "price": find["price"], "verdict": find["verdict"],
                                     "reaction": args["reaction"], "note": args["note"], "via": "web"})
            return "Logged."
        if not self.ebay:
            return "eBay isn't configured on this server."
        try:
            if name == "refresh_listing":
                raw = self.ebay.get_item(find["item_id"])
                if not raw:
                    return "The listing is gone (sold or ended)."
                live = summarize_item(raw)
                return json.dumps({
                    "price": live["price"], "shipping": live["shipping"], "buying_options": live["buying_options"],
                    "bid_count": live["bid_count"], "current_bid": (raw.get("currentBidPrice") or {}).get("value"),
                    "minimum_to_bid": (raw.get("minimumPriceToBid") or {}).get("value"), "ends": live["ends"],
                    "available": raw.get("estimatedAvailabilities", [{}])[0].get("estimatedAvailabilityStatus"),
                })
            if name == "market_comparables":
                filters = {"conditions": ["USED"]}
                if args.get("max_price"):
                    filters["price_max"] = args["max_price"]
                res = self.ebay.search(args["query"], filters=filters, limit=50, sort="")
                items = [summarize_item(i) for i in res.get("itemSummaries", []) or []]
                prices = [i["price_value"] for i in items if i["price_value"]]
                stats = {}
                if prices:
                    stats = {"count": len(prices), "min": min(prices), "median": statistics.median(prices),
                             "max": max(prices)}
                return json.dumps({"active_listings": res.get("total", 0), "price_stats": stats,
                                   "examples": [{"title": i["title"], "price": i["price"], "url": i["url"]}
                                                for i in items[:8]]})
        except EbayError as e:
            return f"eBay error: {e}"
        return f"Unknown tool {name}"

    # ---- conversation -----------------------------------------------------
    def reply(self, roundup_id: str, n: int, message: str, max_steps: int = 12) -> str:
        roundup = self.roundup(roundup_id)
        find = self.find(roundup_id, n)
        history = self.history(roundup_id, n)
        history.append({"role": "user", "text": message, "at": now_iso()})

        # Context and photos lead the first user turn and never change, so they cache across the conversation.
        messages: list[dict[str, Any]] = []
        for i, turn in enumerate(history):
            if i == 0:
                messages.append({"role": "user", "content": self._context(roundup, find)
                                 + [{"type": "text", "text": turn["text"]}]})
            else:
                messages.append({"role": turn["role"], "content": turn["text"]})

        answer_parts: list[str] = []
        for _ in range(max_steps):
            resp = llm.create(self.claude, model=self.s.model, max_tokens=16000, system=SYSTEM, tools=TOOLS,
                              thinking={"type": "adaptive"}, output_config={"effort": "medium"},
                              cache_control={"type": "ephemeral"}, messages=messages)
            messages.append({"role": "assistant", "content": resp.content})
            text = "".join(b.text for b in resp.content if b.type == "text")
            if resp.stop_reason == "pause_turn":
                answer_parts.append(text)
                continue
            if resp.stop_reason == "tool_use":
                answer_parts.append(text)
                results = [{"type": "tool_result", "tool_use_id": b.id, "content": self._tool(b.name, b.input, find)}
                           for b in resp.content if b.type == "tool_use"]
                messages.append({"role": "user", "content": results})
                continue
            if resp.stop_reason == "refusal":
                text = "Sorry, I can't help with that one."
            answer_parts.append(text)
            break
        answer = "".join(answer_parts).strip() or "Sorry, I lost my train of thought there. Try asking again?"
        history.append({"role": "assistant", "text": answer, "at": now_iso()})
        self._save(roundup_id, n, history)
        return answer
