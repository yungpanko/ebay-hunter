"""Command line: `python -m hunter <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import Settings
from .store import Store


def _say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _meta(store: Store) -> dict:
    p = store.s.state_dir / "meta.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _save_meta(store: Store, meta: dict) -> None:
    (store.s.state_dir / "meta.json").write_text(json.dumps(meta, indent=1))


def _after_save(path, s):
    if not path:
        print("\nNothing saved.")
        return
    print(f"\nSaved: {path}\n\nNext steps:")
    if s.has_ebay:
        print("  1. Try it once:  python -m hunter run --dry-run   (finds go to data/outbox/, no email)")
    else:
        print("  1. When your eBay keys arrive, export EBAY_CLIENT_ID and EBAY_CLIENT_SECRET,")
        print("     then try it once:  python -m hunter run --dry-run")
    print("  2. Save it for the schedule:  git add data && git commit -m 'new search' && git push")
    print("  3. Watching every 2 hours starts once the GitHub Actions secrets are set (see README, Setup step 4).")


def cmd_new(args, s, store):
    from .builder import Builder
    idea = " ".join(args.idea) or input("What are you looking for? ").strip()
    path = Builder(s, store, fresh=args.fresh).chat(idea)
    _after_save(path, s)


def cmd_edit(args, s, store):
    from .builder import Builder
    existing = store.load_search(args.slug)
    change = " ".join(args.change) or input("What would you like to change? ").strip()
    path = Builder(s, store).chat(change, existing=existing)
    _after_save(path, s)


def cmd_list(args, s, store):
    searches = store.list_searches()
    if not searches:
        print("No searches yet. Start one with: python -m hunter new vintage jeans")
    for sr in searches:
        state = "watching" if sr.get("active", True) else "paused"
        print(f"{sr['slug']:<40} {state:<9} {sr.get('name', '')}")


def cmd_toggle(active: bool):
    def run(args, s, store):
        sr = store.load_search(args.slug)
        sr["active"] = active
        store.save_search(sr)
        print(f"{args.slug}: {'watching' if active else 'paused'}")
    return run


def cmd_run(args, s, store):
    from .watch import Watcher
    report = Watcher(s, store, dry_run=args.dry_run, say=_say).run(only=args.search)
    print(json.dumps(report, indent=1))


def cmd_inbox(args, s, store):
    from .mailer import Mailer
    from .watch import ingest_replies
    print(f"{ingest_replies(s, store, Mailer(s))} new replies")


def cmd_learn(args, s, store):
    from .watch import learn
    print("profile updated" if learn(s, store) else "no feedback yet")
    meta = _meta(store)
    meta["learned_at_count"] = len(store.feedback())
    _save_meta(store, meta)


def cmd_feedback(args, s, store):
    reviews = {r["item_id"]: r for r in store.read_jsonl("reviews.jsonl")}
    r = reviews.get(args.item_id, {})
    store.add_feedback({"item_id": args.item_id, "search": r.get("search"), "title": r.get("title"),
                        "price": r.get("price"), "verdict": r.get("verdict"), "reaction": args.reaction,
                        "note": " ".join(args.note), "via": "cli"})
    print("noted")


def cmd_cron(args, s, store):
    """What the scheduler runs: read replies, learn if there's anything new, then hunt."""
    from .mailer import Mailer
    from .watch import Watcher, ingest_replies, learn
    try:
        print(f"{ingest_replies(s, store, Mailer(s))} new replies")
    except Exception as e:  # inbox trouble shouldn't stop the hunt
        print(f"inbox check failed: {e}", file=sys.stderr)
    meta = _meta(store)
    count = len(store.feedback())
    if count > meta.get("learned_at_count", 0):
        learn(s, store)
        meta["learned_at_count"] = count
        _save_meta(store, meta)
        print("profile updated from feedback")
    report = Watcher(s, store, dry_run=args.dry_run, say=_say).run()
    print(json.dumps(report, indent=1))


def main(argv=None):
    p = argparse.ArgumentParser(prog="hunter", description="Your secondhand shopping companion for eBay.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("new", help="build a new search from a loose idea, by conversation")
    a.add_argument("idea", nargs="*")
    a.add_argument("--fresh", action="store_true", help="start from scratch, ignoring your profile and other searches")
    a.set_defaults(fn=cmd_new)

    a = sub.add_parser("edit", help="refine an existing search by conversation")
    a.add_argument("slug")
    a.add_argument("change", nargs="*")
    a.set_defaults(fn=cmd_edit)

    sub.add_parser("list", help="list saved searches").set_defaults(fn=cmd_list)
    for name, active in [("pause", False), ("resume", True)]:
        a = sub.add_parser(name, help=f"{name} a search")
        a.add_argument("slug")
        a.set_defaults(fn=cmd_toggle(active))

    a = sub.add_parser("run", help="run searches once and email finds")
    a.add_argument("--search", help="only this search slug")
    a.add_argument("--dry-run", action="store_true", help="write emails to data/outbox instead of sending")
    a.set_defaults(fn=cmd_run)

    sub.add_parser("inbox", help="read email replies into feedback").set_defaults(fn=cmd_inbox)
    sub.add_parser("learn", help="distil feedback into the profile").set_defaults(fn=cmd_learn)

    a = sub.add_parser("feedback", help="record a reaction to a listing")
    a.add_argument("item_id")
    a.add_argument("reaction", choices=["liked", "passed", "bought", "note"])
    a.add_argument("note", nargs="*")
    a.set_defaults(fn=cmd_feedback)

    a = sub.add_parser("cron", help="scheduled run: inbox, learn, hunt")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(fn=cmd_cron)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    s = Settings()
    if args.cmd in {"run", "cron"} and not s.has_ebay:
        sys.exit("eBay keys aren't set yet. Export EBAY_CLIENT_ID and EBAY_CLIENT_SECRET, then try again.\n"
                 "You can still build searches now with: python -m hunter new <idea>")
    args.fn(args, s, Store(s))


if __name__ == "__main__":
    main()
