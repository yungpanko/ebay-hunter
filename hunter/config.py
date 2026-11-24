"""Settings, all read from the environment so nothing about any one user is baked in."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default  # empty counts as unset


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("HUNTER_DATA_DIR", "data")))

    # Claude
    model: str = field(default_factory=lambda: _env("HUNTER_MODEL", "claude-opus-5-5"))

    # eBay (official Browse API, application token only: read-only public listings)
    ebay_client_id: str = field(default_factory=lambda: _env("EBAY_CLIENT_ID"))
    ebay_client_secret: str = field(default_factory=lambda: _env("EBAY_CLIENT_SECRET"))
    ebay_marketplace: str = field(default_factory=lambda: _env("EBAY_MARKETPLACE", "EBAY_US"))
    ebay_env: str = field(default_factory=lambda: _env("EBAY_ENV", "production"))  # or "sandbox"

    # Email (any SMTP/IMAP account; Gmail with an app password is the easy default)
    email_to: str = field(default_factory=lambda: _env("HUNTER_EMAIL_TO"))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD"))
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST", "smtp.gmail.com"))
    smtp_port: int = field(default_factory=lambda: int(_env("SMTP_PORT", "465")))
    imap_host: str = field(default_factory=lambda: _env("IMAP_HOST", "imap.gmail.com"))

    # Limits that keep cost predictable
    results_per_query: int = field(default_factory=lambda: int(_env("HUNTER_RESULTS_PER_QUERY", "50")))
    max_deep_reviews: int = field(default_factory=lambda: int(_env("HUNTER_MAX_DEEP_REVIEWS", "6")))
    max_photos: int = field(default_factory=lambda: int(_env("HUNTER_MAX_PHOTOS", "8")))

    @property
    def searches_dir(self) -> Path:
        return self.data_dir / "searches"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def profile_path(self) -> Path:
        return self.data_dir / "profile.md"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def has_ebay(self) -> bool:
        return bool(self.ebay_client_id and self.ebay_client_secret)

    @property
    def has_email(self) -> bool:
        return bool(self.email_to and self.smtp_user and self.smtp_password)
