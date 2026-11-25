"""Server mode: the web page plus the hunt on a timer, with data kept in sync with GitHub.

Run with:  gunicorn -w 1 --threads 8 --timeout 180 -b 0.0.0.0:$PORT hunter.server:app
(one worker, so the hunt loop runs once).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from .chat import Companion
from .config import Settings
from .store import Store
from .sync import LOCK, GitSync
from .web import create_app

log = logging.getLogger("hunter")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def hunt_once(settings: Settings, store: Store, sync: GitSync) -> None:
    from .mailer import Mailer
    from .watch import Watcher, ingest_replies, learn
    import json

    with LOCK:
        if sync.enabled:
            sync.pull()
        try:
            ingest_replies(settings, store, Mailer(settings))
        except Exception:
            log.exception("inbox check failed")
        meta_path = settings.state_dir / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        count = len(store.feedback())
        if count > meta.get("learned_at_count", 0):
            try:
                learn(settings, store)
                meta["learned_at_count"] = count
                meta_path.write_text(json.dumps(meta, indent=1))
            except Exception:
                log.exception("learning failed")
        report = Watcher(settings, store, say=log.info).run()
        log.info("hunt done: %s", {k: v for k, v in report.items() if k != "searches"})
        if sync.enabled:
            sync.push("hunt: update state")


def hunt_loop(settings: Settings, store: Store, sync: GitSync) -> None:
    time.sleep(30)  # let the web server come up first
    while True:
        started = time.time()
        try:
            hunt_once(settings, store, sync)
        except Exception:
            log.exception("hunt failed")
        time.sleep(max(60.0, settings.hunt_every_hours * 3600 - (time.time() - started)))


def build():
    settings = Settings()
    workdir = Path(os.environ.get("HUNTER_WORKDIR", "/tmp/ebay-hunter-data"))
    sync = GitSync(settings, workdir)
    if sync.enabled:
        sync.ensure_clone()
        settings.data_dir = workdir / "data"
    store = Store(settings)

    class LockedCompanion(Companion):  # serialise chat writes with the hunt loop
        def reply(self, *a, **kw):
            with LOCK:
                return super().reply(*a, **kw)

    app = create_app(
        settings, store, LockedCompanion(settings, store),
        before_read=(lambda: sync.pull(wait=False)) if sync.enabled else (lambda: None),
        after_write=(lambda msg: sync.push(msg)) if sync.enabled else (lambda msg: None),
    )
    if settings.has_ebay and os.environ.get("HUNTER_SCHEDULER", "1") == "1":
        threading.Thread(target=hunt_loop, args=(settings, store, sync), daemon=True).start()
        log.info("hunting every %s hours", settings.hunt_every_hours)
    return app


app = build() if os.environ.get("HUNTER_NO_AUTOBUILD") != "1" else None
