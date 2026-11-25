import json

import pytest

from hunter import links
from hunter.chat import Companion
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


def test_pages_and_companion(setup):
    s, store = setup
    ebay = FakeEbay([listing(1, "Levis SilverTab Loose 34x32", price="38.00",
                         buyingOptions=["FIXED_PRICE", "BEST_OFFER"], itemId="v1|100001|0")])
    script = iter([
        # turn 1, on the find page: refresh the listing, then answer
        resp(text("Checking. "), tool_use("refresh_listing", {"roundup_id": ROUNDUP["id"], "n": 1}, "t1"),
             stop="tool_use"),
        resp(text("Still up with Best Offer on. Offer **$30**, walk away above $35."), stop="end_turn"),
        # turn 2: a reaction plus a change to the search
        resp(tool_use("record_feedback", {"roundup_id": ROUNDUP["id"], "n": 1, "reaction": "liked",
                                          "note": "these look good, love the fade"}, "t2"),
             tool_use("update_search", {"slug": "jeans", "field": "price_guidance",
                                        "value_json": "\"Up to $80 for great fades\""}, "t3"),
             tool_use("update_profile", {"section": "Taste", "content": "Heavy, even fades."}, "t4"),
             stop="tool_use"),
        resp(text("Noted, and I bumped your budget to $80 for great fades."), stop="end_turn"),
    ])
    claude = FakeClaude(lambda kw: next(script))
    app = create_app(s, store, Companion(s, store, claude=claude, ebay=ebay)).test_client()
    k = links.key_for("s3cret", ROUNDUP["id"])
    hk = links.home_key(s)

    assert app.get(f"/r/{ROUNDUP['id']}/1?k=wrong").status_code == 404
    assert app.get("/?k=wrong").status_code == 404
    home = app.get(f"/?k={hk}").get_data(as_text=True)
    assert "Faded loose jeans" in home and "Light SilverTab Loose in your range." in home  # finds without clicking
    page = app.get(f"/r/{ROUNDUP['id']}?k={k}").get_data(as_text=True)
    assert "/r/r20261125170000/2?k=" in page and "Your shopping companion" in page
    find = app.get(f"/r/{ROUNDUP['id']}/1?k={k}").get_data(as_text=True)
    assert "Could you measure the rise?" in find and "Strong buy · 9/10" in find and "Buy It Now, Best Offer" in find
    assert "Looking at: #1 · Levis SilverTab Loose 34x32" in find

    page_ctx = {"kind": "find", "roundup_id": ROUNDUP["id"], "n": 1}
    assert app.post("/chat?k=wrong", json={"message": "hi"}).status_code == 404
    r = app.post(f"/chat?k={hk}", json={"message": "What should I offer?", "page": page_ctx})
    assert r.status_code == 200 and "Offer **$30**" in r.get_json()["reply"] and not r.get_json()["changed"]
    sys_text = claude.calls[0]["system"]
    assert "Faded loose jeans" in sys_text and "Levis 569" in sys_text  # global context: searches and all finds
    assert claude.calls[0]["messages"][0]["content"].startswith("[Looking at: find #1 in roundup")

    r = app.post(f"/chat?k={hk}", json={"message": "these look good", "page": page_ctx})
    assert r.get_json()["changed"]
    assert store.feedback()[-1]["note"] == "these look good, love the fade"
    assert store.load_search("jeans")["price_guidance"] == "Up to $80 for great fades"
    assert "Heavy, even fades." in store.read_profile()
    # one conversation across pages: the home page shows it too
    home = app.get(f"/?k={hk}").get_data(as_text=True)
    assert "these look good" in home and "Looking at: Home" in home


def test_look_at_find_returns_photos(setup):
    s, store = setup
    c = Companion(s, store, claude=None, ebay=None)
    blocks = c._tool("look_at_find", {"roundup_id": ROUNDUP["id"], "n": 1})
    assert blocks[0]["type"] == "text" and blocks[1]["source"]["url"].endswith("s-l1200.jpg")
    assert "No such find" in c._tool("look_at_find", {"roundup_id": "nope", "n": 1})
