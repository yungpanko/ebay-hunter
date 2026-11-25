import json

import pytest

from hunter import links
from hunter.chat import FindChat
from hunter.config import Settings
from hunter.store import Store
from hunter.web import create_app

from .fakes import FakeClaude, FakeEbay, listing, resp, text, tool_use

ROUNDUP = {"id": "r20261125170000", "at": "2026-11-25T17:00:00+00:00", "delivery": "dry-run", "items": [
    {"n": 1, "item_id": "v1|100001|0", "search": "jeans", "title": "Levis SilverTab Loose 34x32", "price": "38.00 USD",
     "verdict": "strong_buy", "score": 9,
     "listing": {"item_id": "v1|100001|0", "title": "Levis SilverTab Loose 34x32", "price": "38.00 USD",
                 "shipping": "9.00", "url": "https://www.ebay.com/itm/100001", "condition": "Pre-owned",
                 "buying_options": ["FIXED_PRICE", "BEST_OFFER"],
                 "images": ["https://i.ebayimg.com/images/g/a/s-l500.jpg"]},
     "review": {"verdict": "strong_buy", "score": 9, "headline": "Light SilverTab Loose in your range.",
                "why": ["Waist 17 flat"], "photo_findings": [], "fit": "Good", "price_take": "Steal",
                "concerns": [], "missing_info": ["rise"], "seller_question": "Could you measure the rise?",
                "off_brief_gem": False}},
    {"n": 2, "item_id": "v1|100002|0", "search": "jeans", "title": "Levis 569", "price": "52.00 USD",
     "verdict": "long_shot", "score": 5, "listing": {"images": []}, "review": {"headline": "Maybe."}},
]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HUNTER_WEB_URL", "https://hunter.example")
    monkeypatch.setenv("HUNTER_WEB_SECRET", "s3cret")
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "x")
    s = Settings()
    store = Store(s)
    store.save_search({"slug": "jeans", "name": "Faded loose jeans"})
    store.append_jsonl("roundups.jsonl", ROUNDUP)
    return s, store


def test_links_are_signed(setup):
    s, _ = setup
    url = links.find_url(s, ROUNDUP["id"], 1)
    assert url.startswith("https://hunter.example/r/r20261125170000/1?k=")
    assert links.valid("s3cret", ROUNDUP["id"], url.split("k=")[1])
    assert not links.valid("s3cret", "r999", url.split("k=")[1])


def test_pages_and_chat(setup):
    s, store = setup
    ebay = FakeEbay([listing(1, "Levis SilverTab Loose 34x32", price="38.00",
                         buyingOptions=["FIXED_PRICE", "BEST_OFFER"], itemId="v1|100001|0")])
    script = iter([
        resp(text("Let me check it's still up. "), tool_use("refresh_listing", {}, "t1"), stop="tool_use"),
        resp(text("Still available at $38 with Best Offer on. Offer **$30**, walk away above $35."), stop="end_turn"),
        resp(tool_use("record_feedback", {"reaction": "bought", "note": "bought at 32"}, "t2"), stop="tool_use"),
        resp(text("Nice, logged it."), stop="end_turn"),
    ])
    claude = FakeClaude(lambda kw: next(script))
    app = create_app(s, FindChat(s, store, claude=claude, ebay=ebay)).test_client()
    k = links.key_for("s3cret", ROUNDUP["id"])

    assert app.get(f"/r/{ROUNDUP['id']}/1?k=wrong").status_code == 404
    page = app.get(f"/r/{ROUNDUP['id']}?k={k}").get_data(as_text=True)
    assert "Light SilverTab Loose in your range." in page and "/r/r20261125170000/2?k=" in page
    find = app.get(f"/r/{ROUNDUP['id']}/1?k={k}").get_data(as_text=True)
    assert "Could you measure the rise?" in find and "Strong buy · 9/10" in find and "s-l800.jpg" in find

    r = app.post(f"/r/{ROUNDUP['id']}/1/chat?k={k}", json={"message": "What should I offer?"})
    assert r.status_code == 200 and "Offer **$30**" in r.get_json()["reply"]
    first = claude.calls[0]["messages"][0]["content"]
    assert any(b["type"] == "image" for b in first) and "Faded loose jeans" in first[0]["text"]
    assert "Levis 569" in first[0]["text"]  # other finds are in context for comparisons
    assert json.loads(claude.calls[1]["messages"][2]["content"][0]["content"])["buying_options"] == \
        ["FIXED_PRICE", "BEST_OFFER"]

    app.post(f"/r/{ROUNDUP['id']}/1/chat?k={k}", json={"message": "I bought it for 32"})
    assert store.feedback()[-1]["reaction"] == "bought" and store.feedback()[-1]["via"] == "web"
    # second turn replays history: same first message (cacheable), then the earlier answer
    second = claude.calls[2]["messages"]
    assert second[0]["content"][-1]["text"] == "What should I offer?" and second[1]["role"] == "assistant"
    page = app.get(f"/r/{ROUNDUP['id']}/1?k={k}").get_data(as_text=True)
    assert "I bought it for 32" in page  # history renders on reload
