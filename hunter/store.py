"""Plain-file storage: searches are YAML, history is JSON/JSONL, the profile is Markdown.

Everything lives under the data dir so it can be read, hand-edited and versioned in git.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import Settings

PROFILE_TEMPLATE = """# Shopper profile

Everything here is optional and grows over time. The search builder and the judge
both read this file; the `learn` step adds to "Learned from feedback".

## Measurements and fit
(Body measurements or measurements of garments that fit well, and how you like things to fit.)

## Taste
(Colors, eras, moods, brands, details you love or avoid. Free-form.)

## Buying style
(Budget comfort, how you feel about flaws, repairs, returns, auctions vs buy it now.)

## Learned from feedback
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > 40:
        slug = slug[:41].rsplit("-", 1)[0]
    return slug or "search"


class Store:
    def __init__(self, settings: Settings):
        self.s = settings
        self.s.searches_dir.mkdir(parents=True, exist_ok=True)
        self.s.state_dir.mkdir(parents=True, exist_ok=True)

    # ---- profile -------------------------------------------------------
    def read_profile(self) -> str:
        if not self.s.profile_path.exists():
            self.s.profile_path.write_text(PROFILE_TEMPLATE)
        return self.s.profile_path.read_text()

    def write_profile(self, text: str) -> None:
        self.s.profile_path.write_text(text.rstrip() + "\n")

    def update_profile_section(self, section: str, content: str) -> None:
        """Replace the body of a `## section` (creating it if needed)."""
        profile = self.read_profile()
        pattern = re.compile(rf"(^## {re.escape(section)}\s*\n)(.*?)(?=^## |\Z)", re.M | re.S)
        body = content.strip() + "\n\n"
        if pattern.search(profile):
            profile = pattern.sub(lambda m: m.group(1) + body, profile, count=1)
        else:
            profile = profile.rstrip() + f"\n\n## {section}\n" + body
        self.write_profile(profile)

    # ---- searches ------------------------------------------------------
    def search_path(self, slug: str) -> Path:
        return self.s.searches_dir / f"{slug}.yaml"

    def save_search(self, search: dict[str, Any]) -> Path:
        slug = search.get("slug") or slugify(search.get("name", "search"))
        search["slug"] = slug
        search.setdefault("created", now_iso())
        search["updated"] = now_iso()
        search.setdefault("active", True)
        path = self.search_path(slug)
        path.write_text(yaml.safe_dump(search, sort_keys=False, allow_unicode=True, width=100))
        return path

    def load_search(self, slug: str) -> dict[str, Any]:
        return yaml.safe_load(self.search_path(slug).read_text())

    def list_searches(self, active_only: bool = False) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.s.searches_dir.glob("*.yaml")):
            data = yaml.safe_load(path.read_text()) or {}
            data.setdefault("slug", path.stem)
            if active_only and not data.get("active", True):
                continue
            out.append(data)
        return out

    # ---- seen items (dedupe) --------------------------------------------
    def _seen_path(self) -> Path:
        return self.s.state_dir / "seen.json"

    def load_seen(self) -> dict[str, dict[str, str]]:
        p = self._seen_path()
        return json.loads(p.read_text()) if p.exists() else {}

    def save_seen(self, seen: dict[str, dict[str, str]]) -> None:
        # Keep the file bounded: newest 3000 ids per search.
        for slug, items in seen.items():
            if len(items) > 3000:
                keep = sorted(items.items(), key=lambda kv: kv[1])[-3000:]
                seen[slug] = dict(keep)
        self._seen_path().write_text(json.dumps(seen, indent=1, sort_keys=True))

    # ---- logs ----------------------------------------------------------
    def append_jsonl(self, name: str, record: dict[str, Any]) -> None:
        with (self.s.state_dir / name).open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_jsonl(self, name: str) -> list[dict[str, Any]]:
        p = self.s.state_dir / name
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]

    def surfaced(self) -> list[dict[str, Any]]:
        return self.read_jsonl("surfaced.jsonl")

    def feedback(self) -> list[dict[str, Any]]:
        return self.read_jsonl("feedback.jsonl")

    def add_feedback(self, record: dict[str, Any]) -> None:
        record.setdefault("at", now_iso())
        self.append_jsonl("feedback.jsonl", record)
