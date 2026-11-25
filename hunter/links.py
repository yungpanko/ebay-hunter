"""Private links to the web page. Each roundup gets its own unguessable key, so a link only opens what it names."""

from __future__ import annotations

import hashlib
import hmac

from .config import Settings


def key_for(secret: str, roundup_id: str) -> str:
    return hmac.new(secret.encode(), roundup_id.encode(), hashlib.sha256).hexdigest()[:24]


def valid(secret: str, roundup_id: str, key: str) -> bool:
    return bool(secret) and hmac.compare_digest(key_for(secret, roundup_id), key or "")


def roundup_url(s: Settings, roundup_id: str) -> str:
    return f"{s.web_url}/r/{roundup_id}?k={key_for(s.web_secret, roundup_id)}"


def find_url(s: Settings, roundup_id: str, n: int) -> str:
    return f"{s.web_url}/r/{roundup_id}/{n}?k={key_for(s.web_secret, roundup_id)}"


HOME = "home"  # the key for the home page and the companion chat


def home_key(s: Settings) -> str:
    return key_for(s.web_secret, HOME)


def home_url(s: Settings) -> str:
    return f"{s.web_url}/?k={home_key(s)}"
