"""The watcher: rerun every active search, find new listings, judge them, email the good ones."""

from __future__ import annotations

import logging
from typing import Any

from . import judge, llm
from .config import Settings
from .ebay import EbayClient, summarize_item
from .mailer import Mailer, guess_reaction
from .store import Store, now_iso

log = logging.getLogger("hunter")


def _excluded(title: str, search: dict[str, Any]) -> bool:
    t = (title or "").lower()
    return any(w.lower() in t for w in search.get("exclude_keywords") or [])


def _quiet(_: str) -> None:
    pass


def find_new(ebay: EbayClient, search: dict[str, Any], seen: dict[str, str], limit: int,
             say=_quiet) -> list[dict[str, Any]]:
    """Run each query in the search; return unseen listings (newest first), deduped across queries."""
    found: dict[str, dict[str, Any]] = {}
    for q in search.get("queries") or [search.get("name", "")]:
        res = ebay.search(q, search.get("category_ids"), search.get("filters"), search.get("aspects"), limit=limit)
        say(f"  eBay: \"{q}\" returned {len(res.get('itemSummaries') or [])} listings")
        for raw in res.get("itemSummaries", []) or []:
            item = summarize_item(raw)
            iid = item["item_id"]
            if not iid or iid in seen or iid in found or _excluded(item["title"], search):
                continue
            found[iid] = item
    return list(found.values())


class Watcher:
    def __init__(self, settings: Settings, store: Store, *, ebay=None, claude=None, mailer: Mailer | None = None,
                 dry_run: bool = False, say=_quiet):
        self.say = say
        self.s = settings
        self.store = store
        self.ebay = ebay or EbayClient(settings)
        self.claude = claude or llm.client()
        self.mailer = mailer or Mailer(settings, dry_run=dry_run)

    def run(self, only: str | None = None) -> dict[str, Any]:
        seen = self.store.load_seen()
        profile = self.store.read_profile()
        feedback = self.store.feedback()
        report: dict[str, Any] = {"searches": {}, "sent": 0}
        for search in self.store.list_searches(active_only=True):
            slug = search["slug"]
            if only and slug != only:
                continue
            first_run = slug not in seen
            self.say(f"Search: {search.get('name', slug)}" + (" (first run)" if first_run else ""))
            seen.setdefault(slug, {})
            try:
                stats = self._run_one(search, seen[slug], profile, feedback, first_run)
            except Exception as e:  # one broken search shouldn't stop the others
                log.exception("search %s failed", slug)
                stats = {"error": str(e)}
            report["searches"][slug] = stats
            if "error" in stats:
                self.say(f"  Failed: {stats['error'][:200]}")
            else:
                self.say(f"  Done: {stats.get('new', 0)} new, {stats.get('reviewed', 0)} reviewed closely, "
                         f"{stats.get('sent', 0)} worth showing you")
            report["sent"] += stats.get("sent", 0)
            self.store.save_seen(seen)  # save as we go so a crash doesn't re-email
        return report

    def _run_one(self, search, seen_for_search, profile, feedback, first_run) -> dict[str, Any]:
        new = find_new(self.ebay, search, seen_for_search, self.s.results_per_query, say=self.say)
        stamp = now_iso()
        stats = {"new": len(new), "reviewed": 0, "sent": 0}
        self.say(f"  {len(new)} listings you haven't seen yet")
        if not new:
            return stats
        to_triage = new
        if first_run:
            # The backlog is old news: only weigh the newest 40, mark the rest seen.
            to_triage = sorted(new, key=lambda i: i.get("listed") or "", reverse=True)[:40]

        relevant_fb = [f for f in feedback if f.get("search") == search["slug"]] or feedback
        system = judge.system_for(profile, search, relevant_fb)

        # Stage 1: thumbnail triage over everything new (batched).
        shortlist: list[dict[str, Any]] = []
        for i in range(0, len(to_triage), 40):
            batch = to_triage[i:i + 40]
            self.say(f"  Quick look at listings {i + 1}-{i + len(batch)} of {len(to_triage)} (titles and main photos)...")
            decisions = judge.triage(self.claude, self.s.model, system, batch)
            for item in batch:
                d = decisions.get(item["item_id"])
                if d is None or d["look_closer"]:
                    shortlist.append(item)
        stats["shortlisted"] = len(shortlist)
        self.say(f"  {len(shortlist)} look promising")

        # Mark everything seen now; items beyond the deep-review cap stay unreviewed rather than looping forever.
        for item in new:
            seen_for_search[item["item_id"]] = stamp
        if first_run:
            # First run of a search: the backlog is old news. Review only the newest handful.
            shortlist = shortlist[: max(3, self.s.max_deep_reviews // 2)]
        shortlist = shortlist[: self.s.max_deep_reviews]

        # Stage 2: full listing + photos.
        for n, summary in enumerate(shortlist, 1):
            self.say(f"  Close look {n}/{len(shortlist)}: {(summary['title'] or '')[:60]} ({summary['price']})...")
            full = self.ebay.get_item(summary["item_id"])
            item = summarize_item(full) if full else summary
            item["item_id"] = summary["item_id"]
            verdict = judge.review(self.claude, self.s.model, system, item, max_photos=self.s.max_photos)
            stats["reviewed"] += 1
            record = {"at": now_iso(), "search": search["slug"], "item_id": item["item_id"], "title": item["title"],
                      "price": item["price"], "url": item["url"], "verdict": verdict["verdict"],
                      "headline": verdict["headline"]}
            if verdict["verdict"] != "pass":
                record["delivery"] = self.mailer.send(item, verdict, search)
                stats["sent"] += 1
                self.say(f"    {verdict['verdict'].replace('_', ' ')}: {verdict['headline']} [{record['delivery']}]")
            else:
                self.say(f"    pass: {verdict['headline']}")
            self.store.append_jsonl("reviews.jsonl", record)
        return stats


def ingest_replies(settings: Settings, store: Store, mailer: Mailer) -> int:
    """Pull email replies into the feedback log, matched to the listing they answer."""
    reviews = {r["item_id"]: r for r in store.read_jsonl("reviews.jsonl")}
    n = 0
    for reply in mailer.fetch_replies():
        r = reviews.get(reply["item_id"], {})
        store.add_feedback({"item_id": reply["item_id"], "search": r.get("search"), "title": r.get("title"),
                            "price": r.get("price"), "verdict": r.get("verdict"),
                            "reaction": guess_reaction(reply["note"]), "note": reply["note"], "via": "email"})
        n += 1
    return n


def learn(settings: Settings, store: Store, claude=None) -> bool:
    fb = store.feedback()
    if not fb:
        return False
    learned = judge.learn(claude or llm.client(), settings.model, store.read_profile(), fb)
    store.update_profile_section("Learned from feedback", learned)
    return True
