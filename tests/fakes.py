"""Stand-ins for eBay and Claude so the whole flow runs offline."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS
from typing import Any, Callable


def text(t: str):
    return NS(type="text", text=t)


def tool_use(name: str, args: dict, id_: str = "tu_1"):
    return NS(type="tool_use", name=name, input=args, id=id_)


def resp(*blocks, stop="end_turn"):
    return NS(content=list(blocks), stop_reason=stop)


class FakeClaude:
    """Feeds each call to `handler(kwargs)`, which returns a response; records every call."""

    def __init__(self, handler: Callable[[dict[str, Any]], Any]):
        self.calls: list[dict[str, Any]] = []
        self.handler = handler
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return self.handler(kw)


def json_resp(data: dict):
    return resp(text(json.dumps(data)))


def listing(n: int, title: str, price: str = "40.00", **extra) -> dict[str, Any]:
    base = {
        "itemId": f"v1|10000{n}|0",
        "title": title,
        "price": {"value": price, "currency": "USD"},
        "image": {"imageUrl": f"https://i.ebayimg.com/images/g/abc{n}/s-l225.jpg"},
        "itemWebUrl": f"https://www.ebay.com/itm/10000{n}",
        "condition": "Pre-owned",
        "buyingOptions": ["FIXED_PRICE"],
        "shippingOptions": [{"shippingCost": {"value": "8.00", "currency": "USD"}}],
        "itemCreationDate": "2026-11-24T18:00:00.000Z",
    }
    base.update(extra)
    return base


class FakeEbay:
    def __init__(self, pages: dict[str, list[dict]] | list[dict]):
        self.pages = pages
        self.searches: list[dict] = []
        self.item_calls: list[str] = []

    def search(self, q, category_ids=None, filters=None, aspects=None, limit=50, sort="newlyListed"):
        self.searches.append({"q": q, "category_ids": category_ids, "filters": filters, "aspects": aspects})
        items = self.pages.get(q, []) if isinstance(self.pages, dict) else self.pages
        return {"total": len(items), "itemSummaries": items}

    def get_item(self, item_id):
        self.item_calls.append(item_id)
        pool = [i for v in (self.pages.values() if isinstance(self.pages, dict) else [self.pages]) for i in v]
        for i in pool:
            if i["itemId"] == item_id:
                full = dict(i)
                full["additionalImages"] = [{"imageUrl": f"https://i.ebayimg.com/images/g/tag{item_id[-3]}/s-l500.jpg"}]
                full["localizedAspects"] = [{"name": "Brand", "value": "Levi's"}, {"name": "Size", "value": "33"}]
                full["description"] = "<p>Great jeans.<br>Waist 16 flat, inseam 31.</p>"
                return full
        return {}
