import email
import json
from pathlib import Path

import pytest
import yaml

from hunter.builder import Builder
from hunter.config import Settings
from hunter.ebay import EbayClient, build_aspect_filter, build_filter, resize_image, summarize_item
from hunter.mailer import Mailer, guess_reaction, parse_reply, strip_quoted, subject_for
from hunter.store import Store
from hunter.watch import Watcher, ingest_replies

from .fakes import FakeClaude, FakeEbay, json_resp, listing, resp, text, tool_use

SEARCH = {
    "name": "Made in USA Levi's 501s",
    "slug": "usa-501s",
    "idea": "vintage jeans",
    "summary": "USA-made 501s that fit.",
    "queries": ["levis 501 made in usa", "vintage levis jeans"],
    "category_ids": ["11483"],
    "filters": {"price_max": 120, "currency": "USD", "conditions": ["USED"]},
    "aspects": {"Brand": ["Levi's"]},
    "exclude_keywords": ["kids"],
    "must_haves": ["Made in USA"],
    "measurements": [{"name": "waist flat", "min": 15.5, "max": 16.5, "unit": "in", "note": ""}],
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ["SMTP_USER", "SMTP_PASSWORD", "HUNTER_EMAIL_TO", "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path / "data"))
    s = Settings()
    return s, Store(s)


# ---- eBay request building ----------------------------------------------------

def test_filters_and_aspects():
    f = build_filter({"price_min": 20, "price_max": 120, "currency": "USD", "buying_options": ["FIXED_PRICE", "AUCTION"],
                      "conditions": ["USED"], "item_location_country": "US"})
    assert f == ("price:[20..120],priceCurrency:USD,buyingOptions:{FIXED_PRICE|AUCTION},"
                 "conditions:{USED},itemLocationCountry:US")
    assert build_filter({"price_max": 50}) == "price:[..50],priceCurrency:USD"
    assert build_aspect_filter("11483", {"Brand": ["Levi's", "Lee"], "Country/Region of Manufacture": ["United States"]}) \
        == "categoryId:11483,Brand:{Levi's|Lee},Country/Region of Manufacture:{United States}"
    assert build_aspect_filter(None, {"Brand": ["Levi's"]}) is None
    assert resize_image("https://i.ebayimg.com/images/g/x/s-l225.jpg", 1200).endswith("s-l1200.jpg")


def test_ebay_client_auth_and_search(monkeypatch):
    class R:
        def __init__(self, code, body):
            self.status_code, self._b, self.text = code, body, json.dumps(body)

        def json(self):
            return self._b

    class Sess:
        def __init__(self):
            self.gets = []

        def post(self, url, headers, data, timeout):
            assert url.endswith("/identity/v1/oauth2/token")
            assert headers["Authorization"].startswith("Basic ")
            assert data["grant_type"] == "client_credentials"
            return R(200, {"access_token": "tok", "expires_in": 7200})

        def get(self, url, headers, params, timeout):
            self.gets.append((url, headers, params))
            return R(200, {"itemSummaries": []})

    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    sess = Sess()
    c = EbayClient(Settings(), session=sess)
    c.search("levis 501", ["11483"], {"price_max": 100}, {"Brand": ["Levi's"]})
    url, headers, params = sess.gets[0]
    assert url == "https://api.ebay.com/buy/browse/v1/item_summary/search"
    assert headers["Authorization"] == "Bearer tok" and headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
    assert params["sort"] == "newlyListed" and params["aspect_filter"] == "categoryId:11483,Brand:{Levi's}"


def test_summarize_item_strips_html():
    item = summarize_item({**listing(1, "Levis"), "description": "<div>Waist 16&nbsp;in<br>Made in USA</div>",
                           "localizedAspects": [{"name": "Brand", "value": "Levi's"}]})
    assert item["description"] == "Waist 16 in\nMade in USA"
    assert item["aspects"] == {"Brand": "Levi's"} and item["shipping"] == "8.00"


# ---- the watcher end to end ---------------------------------------------------

def _claude_for_watch():
    def handler(kw):
        schema = kw["output_config"]["format"]["schema"]
        assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["fallbacks"] == "default"
        content = kw["messages"][0]["content"]
        if "items" in schema["properties"]:  # triage
            ids = [json.loads(b["text"])["item_id"] for b in content if b["type"] == "text" and b["text"].startswith("{")]
            return json_resp({"items": [{"item_id": i, "look_closer": "100002" not in i, "reason": "r"} for i in ids]})
        assert any(b["type"] == "image" and "s-l1200" in b["source"]["url"] for b in content)
        return json_resp({
            "verdict": "strong_buy", "headline": "USA-made 501s in your size for $40.",
            "why": ["Made in USA tag in photo 2"], "photo_findings": ["Care tag reads Made in USA"],
            "fit": "16 flat matches your 15.5-16.5", "price_take": "Steal", "concerns": [],
            "missing_info": ["rise"], "seller_question": "Could you measure the front rise?", "off_brief_gem": False,
        })
    return FakeClaude(handler)


def test_watch_run_triage_review_email_and_dedupe(env):
    s, store = env
    store.save_search(dict(SEARCH))
    ebay = FakeEbay({
        "levis 501 made in usa": [listing(1, "Levis 501 USA 33x30"), listing(2, "Levis 550 relaxed")],
        "vintage levis jeans": [listing(1, "Levis 501 USA 33x30"), listing(3, "Old jeans blue"),
                                listing(4, "Levis kids jeans")],
    })
    claude = _claude_for_watch()
    w = Watcher(s, store, ebay=ebay, claude=claude, dry_run=True)
    report = w.run()
    stats = report["searches"]["usa-501s"]
    # 1 deduped across queries, 4 excluded by 'kids', 2 skipped by triage
    assert stats["new"] == 3 and stats["shortlisted"] == 2 and stats["reviewed"] == 2 and stats["sent"] == 2
    assert sorted(ebay.item_calls) == ["v1|100001|0", "v1|100003|0"]
    outbox = sorted(p.name for p in s.outbox_dir.glob("*.eml"))
    assert outbox == ["v1_100001_0.eml", "v1_100003_0.eml"]
    msg = email.message_from_bytes((s.outbox_dir / outbox[0]).read_bytes())
    assert msg["Subject"].startswith("Strong buy: Levis 501 USA 33x30") and "[hunter:v1|100001|0]" in msg["Subject"]
    html = (s.outbox_dir / "v1_100001_0.html").read_text()
    assert "Could you measure the front rise?" in html and "https://www.ebay.com/itm/100001" in html
    # The system prompt carries profile + search so it caches across items.
    assert "Made in USA Levi's 501s" in claude.calls[0]["system"][0]["text"]

    # Second run: nothing new, no more Claude calls.
    n_calls = len(claude.calls)
    report2 = w.run()
    assert report2["searches"]["usa-501s"]["new"] == 0 and len(claude.calls) == n_calls


def test_paused_search_is_skipped(env):
    s, store = env
    store.save_search({**SEARCH, "active": False})
    ebay = FakeEbay([listing(1, "x")])
    Watcher(s, store, ebay=ebay, claude=_claude_for_watch(), dry_run=True).run()
    assert ebay.searches == []


def test_first_run_caps_backlog(env, monkeypatch):
    monkeypatch.setenv("HUNTER_MAX_DEEP_REVIEWS", "6")
    s = Settings()
    store = Store(s)
    store.save_search({**SEARCH, "queries": ["q"]})
    ebay = FakeEbay({"q": [listing(i, f"Levis 501 #{i}") for i in range(10, 30)]})
    stats = Watcher(s, store, ebay=ebay, claude=_claude_for_watch(), dry_run=True).run()["searches"]["usa-501s"]
    assert stats["new"] == 20 and stats["reviewed"] == 3


# ---- the builder conversation ---------------------------------------------------

def test_builder_asks_then_saves(env):
    s, store = env
    saved_args = {
        "name": "Made in USA Levi's 501s", "idea": "vintage jeans", "summary": "s",
        "queries": ["levis 501 usa"], "category_ids": ["11483"],
        "filters": {"price_min": None, "price_max": 120, "currency": "USD", "buying_options": [],
                    "conditions": ["USED"], "item_location_country": None, "delivery_country": "US",
                    "returns_accepted": False},
        "aspects": [{"name": "Brand", "values": ["Levi's"]}, {"name": "Color", "values": []}],
        "exclude_keywords": [], "must_haves": ["Made in USA"], "nice_to_haves": [], "dealbreakers": [],
        "measurements": [], "fit_notes": "", "price_guidance": "", "knowledge": ["Big E pre-1971"], "gem_notes": "",
    }
    script = iter([
        resp(text("Is Made in USA important to you? Here's why..."), stop="end_turn"),
        resp(tool_use("update_profile", {"section": "Measurements and fit", "content": "Waist 32-33."}, "a"),
             tool_use("save_search", saved_args, "b"), stop="tool_use"),
        resp(text("Saved. I'll watch it."), stop="end_turn"),
    ])
    claude = FakeClaude(lambda kw: next(script))
    said = []
    b = Builder(s, store, claude=claude, ask=lambda _: "Yes, USA only, 32-33 waist", say=said.append)
    path = b.chat("vintage jeans")
    assert path and path.name == "made-in-usa-levi-s-501s.yaml"
    assert len(said) == 2
    data = yaml.safe_load(path.read_text())
    assert data["aspects"] == {"Brand": ["Levi's"]}
    assert data["filters"] == {"price_max": 120, "currency": "USD", "conditions": ["USED"], "delivery_country": "US"}
    assert "Waist 32-33." in store.read_profile()
    # tool results go back in one user message
    last_user = claude.calls[2]["messages"][-2]  # list is shared; [-1] is the final reply
    assert [r["tool_use_id"] for r in last_user["content"]] == ["a", "b"]
    assert said[0].startswith("Is Made in USA")
    tool_names = [t["name"] for t in claude.calls[0]["tools"]]
    assert tool_names == ["web_search", "save_search", "update_profile"]  # no eBay keys -> no eBay tools


# ---- replies become feedback -------------------------------------------------------

def test_reply_parsing():
    raw = (b"Subject: Re: Strong buy: Levis 501 =?utf-8?q?=C2=B7?= 40.00 USD [hunter:v1|100001|0]\r\n"
           b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
           b"Love these but the rise looks short\r\n\r\nOn Tue, Nov 24, 2026 at 3:00 PM eBay Hunter wrote:\r\n> stuff\r\n")
    r = parse_reply(email.message_from_bytes(raw))
    assert r == {"item_id": "v1|100001|0", "note": "Love these but the rise looks short"}
    assert strip_quoted("ok\n> quoted") == "ok"
    assert guess_reaction("bought it!") == "bought"
    assert guess_reaction("love it") == "liked"
    assert guess_reaction("too short for me") == "passed"


def test_ingest_replies(env):
    s, store = env
    store.append_jsonl("reviews.jsonl", {"item_id": "v1|1|0", "search": "usa-501s", "title": "Levis", "price": "40",
                                         "verdict": "strong_buy"})

    class M:
        def fetch_replies(self):
            return [{"item_id": "v1|1|0", "note": "bought it"}]

    assert ingest_replies(s, store, M()) == 1
    fb = store.feedback()[0]
    assert fb["reaction"] == "bought" and fb["search"] == "usa-501s"


def test_subject():
    assert subject_for({"title": "T", "price": "1 USD", "item_id": "v1|9|0"},
                       {"verdict": "worth_a_look", "off_brief_gem": True}) == "Off-brief gem: T · 1 USD [hunter:v1|9|0]"


def test_profile_sections(env):
    s, store = env
    store.update_profile_section("Taste", "Faded indigo, 70s-90s.")
    store.update_profile_section("Taste", "Faded indigo.")
    p = store.read_profile()
    assert p.count("Faded indigo") == 1 and "## Learned from feedback" in p


def test_cited_text_blocks_join_inline(env):
    s, store = env
    claude = FakeClaude(lambda kw: resp(text("Skip the 550, since "), text("it tapers"), text(", so try the 569.")))
    said = []
    Builder(s, store, claude=claude, ask=lambda _: "quit", say=said.append).chat("jeans")
    assert said == ["Skip the 550, since it tapers, so try the 569."]


def test_slug_is_short():
    from hunter.store import slugify
    s = slugify("Super faded loose straight-leg men's jeans (SilverTab Loose, 569)")
    assert len(s) <= 40 and not s.endswith("-")
