"""Thin client for eBay's official Browse and Taxonomy APIs (read-only, application token)."""

from __future__ import annotations

import base64
import re
import time
from typing import Any, Iterable

import requests

from .config import Settings

SCOPE = "https://api.ebay.com/oauth/api_scope"


class EbayError(RuntimeError):
    pass


def resize_image(url: str, size: int) -> str:
    """eBay image URLs carry their size as s-l<N>; swap it for the size we want."""
    return re.sub(r"s-l\d+", f"s-l{size}", url) if url else url


def _escape_aspect(value: str) -> str:
    # Braces, pipes and commas are syntax in aspect_filter values.
    return re.sub(r"([{}|,])", r"\\\1", value)


def build_filter(filters: dict[str, Any] | None) -> str | None:
    """Turn the search file's `filters` block into Browse API filter syntax."""
    if not filters:
        return None
    parts: list[str] = []
    lo, hi = filters.get("price_min"), filters.get("price_max")
    if lo is not None or hi is not None:
        parts.append(f"price:[{'' if lo is None else lo}..{'' if hi is None else hi}]")
        parts.append(f"priceCurrency:{filters.get('currency', 'USD')}")
    if filters.get("buying_options"):
        parts.append("buyingOptions:{" + "|".join(filters["buying_options"]) + "}")
    if filters.get("conditions"):
        parts.append("conditions:{" + "|".join(filters["conditions"]) + "}")
    if filters.get("condition_ids"):
        parts.append("conditionIds:{" + "|".join(str(c) for c in filters["condition_ids"]) + "}")
    if filters.get("item_location_country"):
        parts.append(f"itemLocationCountry:{filters['item_location_country']}")
    if filters.get("delivery_country"):
        parts.append(f"deliveryCountry:{filters['delivery_country']}")
    if filters.get("returns_accepted"):
        parts.append("returnsAccepted:true")
    for raw in filters.get("extra", []) or []:  # escape hatch for any other Browse filter
        parts.append(str(raw))
    return ",".join(parts) or None


def build_aspect_filter(category_id: str | None, aspects: dict[str, list[str]] | None) -> str | None:
    if not aspects or not category_id:
        return None  # eBay requires exactly one category for aspect filtering
    parts = [f"categoryId:{category_id}"]
    for name, values in aspects.items():
        if values:
            parts.append(f"{name}:{{" + "|".join(_escape_aspect(v) for v in values) + "}")
    return ",".join(parts)


class EbayClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        if not settings.has_ebay:
            raise EbayError("EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are not set.")
        self.s = settings
        self.http = session or requests.Session()
        sandbox = settings.ebay_env == "sandbox"
        self.base = "https://api.sandbox.ebay.com" if sandbox else "https://api.ebay.com"
        self._token: str | None = None
        self._token_exp = 0.0

    # ---- auth ----------------------------------------------------------
    def _auth_header(self) -> dict[str, str]:
        if not self._token or time.time() > self._token_exp - 60:
            creds = f"{self.s.ebay_client_id}:{self.s.ebay_client_secret}".encode()
            r = self.http.post(
                f"{self.base}/identity/v1/oauth2/token",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Authorization": "Basic " + base64.b64encode(creds).decode(),
                },
                data={"grant_type": "client_credentials", "scope": SCOPE},
                timeout=30,
            )
            if r.status_code != 200:
                raise EbayError(f"eBay token request failed ({r.status_code}): {r.text[:300]}")
            body = r.json()
            self._token = body["access_token"]
            self._token_exp = time.time() + int(body.get("expires_in", 7200))
        return {
            "Authorization": f"Bearer {self._token}",
            "X-EBAY-C-MARKETPLACE-ID": self.s.ebay_marketplace,
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(4):
            r = self.http.get(f"{self.base}{path}", headers=self._auth_header(), params=params, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 404:
                return {}
            if r.status_code != 200:
                raise EbayError(f"eBay GET {path} failed ({r.status_code}): {r.text[:300]}")
            return r.json()
        raise EbayError(f"eBay GET {path} kept failing (rate limited or down).")

    # ---- browse --------------------------------------------------------
    def search(
        self,
        q: str,
        category_ids: Iterable[str] | None = None,
        filters: dict[str, Any] | None = None,
        aspects: dict[str, list[str]] | None = None,
        limit: int = 50,
        sort: str = "newlyListed",
    ) -> dict[str, Any]:
        cats = [str(c) for c in (category_ids or []) if c]
        params: dict[str, Any] = {"q": q, "limit": min(limit, 200)}
        if sort:
            params["sort"] = sort
        if cats:
            params["category_ids"] = ",".join(cats)
        f = build_filter(filters)
        if f:
            params["filter"] = f
        af = build_aspect_filter(cats[0] if len(cats) == 1 else None, aspects)
        if af:
            params["aspect_filter"] = af
        return self._get("/buy/browse/v1/item_summary/search", params)

    def get_item(self, item_id: str) -> dict[str, Any]:
        return self._get(f"/buy/browse/v1/item/{requests.utils.quote(item_id, safe='')}")

    # ---- taxonomy (used by the search builder) -------------------------
    def _tree_id(self) -> str:
        if not hasattr(self, "_tree"):
            data = self._get(
                "/commerce/taxonomy/v1/get_default_category_tree_id",
                {"marketplace_id": self.s.ebay_marketplace},
            )
            self._tree = data["categoryTreeId"]
        return self._tree

    def category_suggestions(self, q: str) -> list[dict[str, str]]:
        data = self._get(
            f"/commerce/taxonomy/v1/category_tree/{self._tree_id()}/get_category_suggestions", {"q": q}
        )
        out = []
        for s in data.get("categorySuggestions", [])[:10]:
            path = " > ".join(a["categoryName"] for a in reversed(s.get("categoryTreeNodeAncestors", [])))
            out.append({"id": s["category"]["categoryId"], "name": s["category"]["categoryName"], "path": path})
        return out

    def category_aspects(self, category_id: str, max_values: int = 40) -> list[dict[str, Any]]:
        data = self._get(
            f"/commerce/taxonomy/v1/category_tree/{self._tree_id()}/get_item_aspects_for_category",
            {"category_id": category_id},
        )
        out = []
        for a in data.get("aspects", []):
            values = [v["localizedValue"] for v in a.get("aspectValues", [])[:max_values]]
            out.append({"name": a["localizedAspectName"], "values": values})
        return out


# ---- normalising listings for the judge and the email -------------------

def summarize_item(item: dict[str, Any]) -> dict[str, Any]:
    """A compact, model-friendly view of a search summary or full item."""
    price = item.get("price") or item.get("currentBidPrice") or {}
    ship = None
    for opt in item.get("shippingOptions", []) or []:
        cost = (opt.get("shippingCost") or {}).get("value")
        if cost is not None:
            ship = cost
            break
    images = []
    if item.get("image", {}).get("imageUrl"):
        images.append(item["image"]["imageUrl"])
    for extra in item.get("additionalImages", []) or item.get("thumbnailImages", []) or []:
        if extra.get("imageUrl") and extra["imageUrl"] not in images:
            images.append(extra["imageUrl"])
    return {
        "item_id": item.get("itemId"),
        "title": item.get("title"),
        "price": f"{price.get('value', '?')} {price.get('currency', '')}".strip(),
        "price_value": float(price["value"]) if price.get("value") else None,
        "shipping": ship,
        "buying_options": item.get("buyingOptions", []),
        "bid_count": item.get("bidCount"),
        "ends": item.get("itemEndDate"),
        "listed": item.get("itemCreationDate"),
        "condition": item.get("condition"),
        "condition_description": item.get("conditionDescription"),
        "url": item.get("itemWebUrl"),
        "images": images,
        "seller": (item.get("seller") or {}).get("username"),
        "seller_feedback": (item.get("seller") or {}).get("feedbackPercentage"),
        "location": (item.get("itemLocation") or {}).get("country"),
        "aspects": {a["name"]: a["value"] for a in item.get("localizedAspects", []) or [] if "name" in a},
        "description": _strip_html(item.get("description") or item.get("shortDescription") or ""),
    }


def _strip_html(html: str, limit: int = 4000) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:limit]
