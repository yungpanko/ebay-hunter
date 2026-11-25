"""The companion: one continuous conversation that knows everything you're hunting for.

It sees your profile, every saved search, recent finds and your reactions, and which page you're looking at.
What you tell it flows back into the files (profile, searches, feedback), so context keeps accumulating.
"""

from __future__ import annotations

import json
import statistics
from typing import Any

import yaml

from . import llm
from .config import Settings
from .ebay import EbayClient, EbayError, resize_image, summarize_item
from .schema import SEARCH_SCHEMA
from .store import Store, now_iso

PERSONA = """You are the person's secondhand shopping companion: one ongoing conversation, like a friend who \
shops with them, knows their taste, fit and history, keeps watch on eBay for them, and has done the homework. \
Be direct, honest and brief: short paragraphs, a few bullets at most, no headers. Lead with the answer.

Each message says which page they're looking at. "This", "these" or "it" means what's on that page; on a \
roundup page it can mean the whole set. You can answer about any find without them opening it; use look_at_find \
to see a find's photos and full listing when the details matter.

What you help with:
- Will it fit? Compare listing measurements (and what's readable in photos) with their ranges and garments that \
fit. Say how confident you are and what would raise it; draft a seller question if needed.
- Comparing finds with each other and with active listings (market_comparables). eBay's API has no sold prices; \
use web search for typical sale prices, and tell them they can tick "Sold items" on eBay.
- Buying well. Refresh the listing first. For Best Offer: an opening offer and a walk-away number with the \
reasoning (sellers often accept roughly 70-85% of ask on slow listings). For auctions: a true max including \
shipping, placed once late via eBay's automatic bidding. You never buy, bid or message sellers; they act.
- Shaping the hunt. When they say what they want more or less of, or change their mind about fit, price, eras, \
brands or details, update the saved search (update_search) or their profile (update_profile) right away and say \
what you changed in a few words. New ideas for things to hunt can become a new search (create_search) once you \
have the essentials; ask one short question at a time if you need more.
- Learning. When they react to a find ("love it", "too short", "bought it", "not for me"), log it with \
record_feedback. Context is everything: capture the why, not just the verdict."""

SEARCH_FIELDS = [k for k in SEARCH_SCHEMA["properties"] if k not in {"idea"}]

TOOLS: list[dict[str, Any]] = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 4},
    {
        "name": "look_at_find",
        "description": "See one find in full: all photos, the listing details and your review of it.",
        "input_schema": llm.obj({"roundup_id": llm.STR, "n": {"type": "integer"}}),
    },
    {
        "name": "refresh_listing",
        "description": "Live state of a find's listing: price or bid, bid count, time left, Best Offer, availability.",
        "input_schema": llm.obj({"roundup_id": llm.STR, "n": {"type": "integer"}}),
    },
    {
        "name": "market_comparables",
        "description": "Search active eBay listings similar to something, with price stats and examples.",
        "input_schema": llm.obj({"query": llm.STR, "max_price": llm.nullable(llm.NUM)}),
    },
    {
        "name": "record_feedback",
        "description": "Log their reaction to a find so the hunter learns their taste.",
        "input_schema": llm.obj({
            "roundup_id": llm.STR, "n": {"type": "integer"},
            "reaction": {"type": "string", "enum": ["liked", "passed", "bought", "note"]},
            "note": llm.STR,
        }),
    },
    {
        "name": "update_profile",
        "description": "Replace one section of their long-lived profile. Write the full new section text, merging "
                       "what's already there.",
        "input_schema": llm.obj({
            "section": {"type": "string", "enum": ["Measurements and fit", "Taste", "Buying style",
                                                   "Category notes"]},
            "content": llm.STR,
        }),
    },
    {
        "name": "update_search",
        "description": "Change one field of a saved search. value_json is the field's complete new value as JSON "
                       "(e.g. a list of strings, a number, or for 'aspects' an object of name -> list of values).",
        "input_schema": llm.obj({
            "slug": llm.STR,
            "field": {"type": "string", "enum": SEARCH_FIELDS + ["active"]},
            "value_json": llm.STR,
        }),
    },
    {
        "name": "create_search",
        "description": "Save a brand-new search for the watcher. Fill every field; use sensible defaults for what "
                       "you don't know and say what you assumed.",
        "input_schema": SEARCH_SCHEMA,
    },
]


class FindNotFound(LookupError):
    pass


class Companion:
    def __init__(self, settings: Settings, store: Store, *, claude=None, ebay: EbayClient | None = None,
                 history_turns: int = 40):
        self.s = settings
        self.store = store
        self._claude = claude
        self._ebay = ebay
        self.history_turns = history_turns

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
    def roundups(self) -> list[dict[str, Any]]:
        return self.store.read_jsonl("roundups.jsonl")

    def roundup(self, roundup_id: str) -> dict[str, Any]:
        for r in self.roundups():
            if r["id"] == roundup_id:
                return r
        raise FindNotFound(roundup_id)

    def find(self, roundup_id: str, n: int) -> dict[str, Any]:
        for it in self.roundup(roundup_id)["items"]:
            if it["n"] == n:
                return it
        raise FindNotFound(f"{roundup_id}/{n}")

    @property
    def _path(self):
        return self.s.state_dir / "conversation.json"

    def history(self) -> list[dict[str, Any]]:
        return json.loads(self._path.read_text()) if self._path.exists() else []

    def _save(self, history: list[dict[str, Any]]) -> None:
        self._path.write_text(json.dumps(history, indent=1, ensure_ascii=False))

    # ---- context ----------------------------------------------------------
    def system(self) -> str:
        searches = []
        for sr in self.store.list_searches():
            view = {k: v for k, v in sr.items() if k not in {"created", "updated"}}
            searches.append(yaml.safe_dump(view, sort_keys=False, allow_unicode=True))
        finds = []
        for r in self.roundups()[-6:]:
            for it in r["items"]:
                rev = it.get("review") or {}
                finds.append({"roundup_id": r["id"], "n": it["n"], "found": r.get("at"), "search": it["search"],
                              "title": it["title"], "price": it["price"], "verdict": it["verdict"],
                              "score": it.get("score"), "headline": rev.get("headline"), "fit": rev.get("fit"),
                              "concerns": rev.get("concerns"), "url": (it.get("listing") or {}).get("url")})
        fb = [f"- [{f.get('reaction')}] {f.get('title', '')} ({f.get('price', '')}): {f.get('note', '')}"
              for f in self.store.feedback()[-60:]]
        return (
            f"{PERSONA}\n\n## Their profile\n\n{self.store.read_profile()}\n\n"
            f"## Saved searches\n\n" + ("\n---\n".join(searches) or "(none yet)") + "\n\n"
            f"## Recent finds (newest last)\n\n{json.dumps(finds, indent=1, ensure_ascii=False)}\n\n"
            f"## Their reactions so far\n\n" + ("\n".join(fb) or "(none yet)")
        )

    def describe_page(self, page: dict[str, Any] | None) -> str:
        page = page or {}
        try:
            if page.get("kind") == "find":
                it = self.find(page["roundup_id"], int(page["n"]))
                return (f"find #{it['n']} in roundup {page['roundup_id']}: {it['title']} ({it['price']}, "
                        f"{it['verdict']} {it.get('score')}/10)")
            if page.get("kind") == "roundup":
                r = self.roundup(page["roundup_id"])
                return f"roundup {r['id']} ({len(r['items'])} finds)"
        except (FindNotFound, KeyError, ValueError):
            pass
        return "the home page (searches and recent roundups)"

    def page_label(self, page: dict[str, Any] | None) -> str:
        """Short, human version of describe_page for the sidebar."""
        page = page or {}
        try:
            if page.get("kind") == "find":
                it = self.find(page["roundup_id"], int(page["n"]))
                return f"#{it['n']} · {it['title']}"
            if page.get("kind") == "roundup":
                r = self.roundup(page["roundup_id"])
                return f"Roundup · {r.get('at', '')[:16].replace('T', ' ')}"
        except (FindNotFound, KeyError, ValueError):
            pass
        return "Home"

    # ---- tools ------------------------------------------------------------
    def _tool(self, name: str, args: dict[str, Any]) -> Any:
        try:
            if name == "look_at_find":
                it = self.find(args["roundup_id"], int(args["n"]))
                lst = it.get("listing") or {}
                blocks: list[dict[str, Any]] = [{"type": "text", "text": json.dumps({
                    "listing": {k: v for k, v in lst.items() if k not in {"images", "price_value"}},
                    "review": it.get("review")}, indent=1, ensure_ascii=False)}]
                for url in (lst.get("images") or [])[: self.s.max_photos]:
                    blocks.append({"type": "image", "source": {"type": "url", "url": resize_image(url, 1200)}})
                return blocks
            if name == "record_feedback":
                it = self.find(args["roundup_id"], int(args["n"]))
                self.store.add_feedback({"item_id": it["item_id"], "search": it["search"], "title": it["title"],
                                         "price": it["price"], "verdict": it["verdict"],
                                         "reaction": args["reaction"], "note": args["note"], "via": "chat"})
                return "Logged."
            if name == "update_profile":
                self.store.update_profile_section(args["section"], args["content"])
                return "Profile updated."
            if name == "update_search":
                search = self.store.load_search(args["slug"])
                value = json.loads(args["value_json"])
                if args["field"] == "aspects" and isinstance(value, list):
                    value = {a["name"]: a["values"] for a in value}
                search[args["field"]] = value
                self.store.save_search(search)
                return f"Updated {args['field']} on {args['slug']}."
            if name == "create_search":
                from .schema import search_from_tool_input
                path = self.store.save_search(search_from_tool_input({"idea": "", **args}))
                return f"Saved as {path.stem}. The watcher picks it up on its next run."
        except FindNotFound as e:
            return f"No such find: {e}"
        except FileNotFoundError:
            return "No search with that slug."
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            return f"Couldn't apply that: {e}"
        if not self.ebay:
            return "eBay isn't configured here."
        try:
            if name == "refresh_listing":
                it = self.find(args["roundup_id"], int(args["n"]))
                raw = self.ebay.get_item(it["item_id"])
                if not raw:
                    return "The listing is gone (sold or ended)."
                live = summarize_item(raw)
                return json.dumps({
                    "price": live["price"], "shipping": live["shipping"], "buying_options": live["buying_options"],
                    "bid_count": live["bid_count"], "current_bid": (raw.get("currentBidPrice") or {}).get("value"),
                    "minimum_to_bid": (raw.get("minimumPriceToBid") or {}).get("value"), "ends": live["ends"],
                    "available": (raw.get("estimatedAvailabilities") or [{}])[0].get("estimatedAvailabilityStatus"),
                })
            if name == "market_comparables":
                filters: dict[str, Any] = {"conditions": ["USED"]}
                if args.get("max_price"):
                    filters["price_max"] = args["max_price"]
                res = self.ebay.search(args["query"], filters=filters, limit=50, sort="")
                items = [summarize_item(i) for i in res.get("itemSummaries", []) or []]
                prices = [i["price_value"] for i in items if i["price_value"]]
                stats = {"count": len(prices), "min": min(prices), "median": statistics.median(prices),
                         "max": max(prices)} if prices else {}
                return json.dumps({"active_listings": res.get("total", 0), "price_stats": stats,
                                   "examples": [{"title": i["title"], "price": i["price"], "url": i["url"]}
                                                for i in items[:8]]})
        except FindNotFound as e:
            return f"No such find: {e}"
        except EbayError as e:
            return f"eBay error: {e}"
        return f"Unknown tool {name}"

    # ---- conversation -----------------------------------------------------
    def reply(self, message: str, page: dict[str, Any] | None = None, max_steps: int = 12) -> str:
        history = self.history()
        where = self.describe_page(page)
        history.append({"role": "user", "text": message, "page": where, "label": self.page_label(page),
                        "at": now_iso()})

        # Durable memory lives in the files (profile, searches, feedback); the chat itself keeps recent turns.
        recent = history[-self.history_turns:]
        while recent and recent[0]["role"] != "user":
            recent = recent[1:]
        messages: list[dict[str, Any]] = []
        for turn in recent:
            if turn["role"] == "user":
                messages.append({"role": "user", "content": f"[Looking at: {turn.get('page', 'home')}]\n"
                                                            f"{turn['text']}"})
            else:
                messages.append({"role": "assistant", "content": turn["text"]})

        system = self.system()
        answer_parts: list[str] = []
        changed = False
        for _ in range(max_steps):
            resp = llm.create(self.claude, model=self.s.model, max_tokens=16000, system=system, tools=TOOLS,
                              thinking={"type": "adaptive"}, output_config={"effort": "medium"},
                              cache_control={"type": "ephemeral"}, messages=messages)
            messages.append({"role": "assistant", "content": resp.content})
            text = "".join(b.text for b in resp.content if b.type == "text")
            answer_parts.append(text)
            if resp.stop_reason == "pause_turn":
                continue
            if resp.stop_reason == "tool_use":
                results = []
                for b in resp.content:
                    if b.type == "tool_use":
                        changed |= b.name in {"record_feedback", "update_profile", "update_search", "create_search"}
                        results.append({"type": "tool_result", "tool_use_id": b.id,
                                        "content": self._tool(b.name, b.input)})
                messages.append({"role": "user", "content": results})
                continue
            if resp.stop_reason == "refusal":
                answer_parts.append("Sorry, I can't help with that one.")
            break
        answer = "".join(answer_parts).strip() or "Sorry, I lost my train of thought. Try asking again?"
        history.append({"role": "assistant", "text": answer, "at": now_iso(), "changed_files": changed})
        self._save(history)
        return answer
