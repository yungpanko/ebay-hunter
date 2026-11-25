"""The web pages: home, roundups and finds, with the companion chat in a sidebar on every page."""

from __future__ import annotations

import html
import json
from typing import Any, Callable

from flask import Flask, abort, jsonify, request

from . import links
from .chat import Companion, FindNotFound
from .config import Settings
from .ebay import resize_image
from .mailer import TIERS
from .store import Store

e = html.escape
BUY = {"FIXED_PRICE": "Buy It Now", "BEST_OFFER": "Best Offer", "AUCTION": "Auction"}

CSS = """
:root{--bg:#faf9f7;--card:#fff;--ink:#1d1d1f;--muted:#6b6b70;--line:#e6e4e0;--accent:#1d1d1f;--accent-ink:#fff;
--me:#1d1d1f;--me-ink:#fff;--them:#f0eeea;--strong:#1a7f37;--look:#9a6700;--long:#6e7781;--side:#f3f1ed}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121214;--card:#1b1b1e;--ink:#ececee;
--muted:#9a9aa2;--line:#2c2c31;--accent:#ececee;--accent-ink:#121214;--me:#ececee;--me-ink:#121214;--them:#25252a;
--strong:#4ac26b;--look:#d4a72c;--long:#9198a1;--side:#18181b}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}a{color:inherit}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 400px;min-height:100vh}
main{padding:20px 24px 60px;max-width:760px;width:100%;margin:0 auto}
aside{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;background:var(--side);
border-left:1px solid var(--line)}
.side-head{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;
align-items:center}.side-head b{font-size:15px}.where{font-size:12px;color:var(--muted)}
#log{flex:1;overflow-y:auto;padding:8px 16px}
.msg{padding:9px 13px;border-radius:14px;margin:8px 0;max-width:92%;overflow-wrap:anywhere;font-size:15px}
.msg p{margin:.3em 0}.msg ul{margin:.3em 0;padding-left:18px}.me{background:var(--me);color:var(--me-ink);margin-left:auto}
.them{background:var(--them)}.ctx{font-size:11px;color:var(--muted);text-align:right;margin:10px 0 -4px}
.chips{display:flex;flex-wrap:wrap;gap:6px;padding:0 16px 8px}.chip{border:1px solid var(--line);
background:var(--card);color:var(--ink);border-radius:999px;padding:5px 11px;font-size:13px;cursor:pointer}
form.say{display:flex;gap:8px;padding:10px 16px 14px;border-top:1px solid var(--line)}
form.say textarea{flex:1;resize:none;border:1px solid var(--line);border-radius:10px;padding:9px;font:inherit;
background:var(--card);color:var(--ink);min-height:42px;max-height:140px}
.btn{display:inline-block;background:var(--accent);color:var(--accent-ink);padding:9px 15px;border-radius:8px;
text-decoration:none;font-weight:600;border:0;font-size:15px;cursor:pointer}
.tier{font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase}
.strong_buy{color:var(--strong)}.worth_a_look{color:var(--look)}.long_shot{color:var(--long)}
h1{font-size:24px;line-height:1.25;margin:4px 0 8px}h2{font-size:15px;text-transform:uppercase;letter-spacing:.04em;
color:var(--muted);margin:24px 0 6px}.muted{color:var(--muted);font-size:14px}
.photos{display:flex;gap:8px;overflow-x:auto;scroll-snap-type:x mandatory;margin:12px 0}
.photos img{height:280px;border-radius:10px;scroll-snap-align:start;flex:none;background:var(--line)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px 15px;margin:10px 0;display:block}
.card h3{margin:0 0 6px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.card ul{margin:0;padding-left:18px}.ask{border-left:3px solid var(--line);padding-left:10px;margin:8px 0}
.row{display:flex;gap:12px;align-items:flex-start;text-decoration:none}.row img{width:84px;border-radius:8px;flex:none}
.fab,.close{display:none}
@media (max-width:900px){.layout{display:block}main{padding:16px 16px 90px}
aside{position:fixed;inset:0;height:100dvh;z-index:10;transform:translateY(100%);transition:transform .2s;border:0}
body.open aside{transform:none}.fab{display:block;position:fixed;right:16px;bottom:16px;z-index:5;
box-shadow:0 4px 14px rgba(0,0,0,.2);border-radius:999px;padding:12px 18px}.close{display:block;background:none;
border:0;color:var(--ink);font-size:15px;cursor:pointer}}
"""


def _tier(review: dict[str, Any]) -> str:
    label = TIERS.get(review.get("verdict"), ("Find",))[0]
    if review.get("off_brief_gem"):
        label += " · off-brief gem"
    return (f'<div class="tier {e(review.get("verdict", ""))}">{e(label)} · {e(str(review.get("score", "")))}/10'
            "</div>")


def _section(title: str, items: list[str] | str | None) -> str:
    if not items:
        return ""
    body = f"<ul>{''.join(f'<li>{e(x)}</li>' for x in items if x)}</ul>" if isinstance(items, list) else \
        f"<p style='margin:0'>{e(items)}</p>"
    return f'<div class="card"><h3>{e(title)}</h3>{body}</div>'


def _find_row(s: Settings, roundup_id: str, it: dict[str, Any]) -> str:
    lst, rev = it.get("listing") or {}, it.get("review") or {}
    img = f'<img src="{e(resize_image(lst["images"][0], 300))}" alt="">' if lst.get("images") else ""
    return (f'<a class="card row" href="{e(links.find_url(s, roundup_id, it["n"]))}">{img}<div>{_tier(rev)}'
            f'<div style="font-weight:600">{e(rev.get("headline", ""))}</div>'
            f'<div class="muted">#{it["n"]} · {e(it["title"] or "")} · {e(it["price"] or "")}</div></div></a>')


CHIPS = {
    "home": ["What's new?", "Which find is best for me right now?", "Tweak one of my searches"],
    "roundup": ["Which of these is best for me?", "Compare the top two", "Anything I should skip?"],
    "find": ["Will these fit me?", "What's it really worth?", "I want it. What's the plan?", "Not for me"],
}


def _page(s: Settings, title: str, body: str, page: dict[str, Any], where: str, history: list[dict]) -> str:
    hist = [{"role": h["role"], "text": h["text"], "page": h.get("label") or h.get("page")} for h in history[-60:]]
    chips = "".join(f'<button class="chip" type="button">{e(c)}</button>' for c in CHIPS[page["kind"]])
    chat_url = f"/chat?k={links.home_key(s)}"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{e(title)}</title><style>{CSS}</style></head><body><div class="layout"><main>{body}</main>
<aside><div class="side-head"><div><b>Your shopping companion</b><div class="where">Looking at: {e(where)}</div></div>
<button class="close" type="button" onclick="document.body.classList.remove('open')">Close</button></div>
<div id="log"></div><div class="chips">{chips}</div>
<form class="say" id="f"><textarea id="t" rows="1" placeholder="Ask anything"></textarea>
<button class="btn" id="b">Send</button></form></aside></div>
<button class="btn fab" type="button" onclick="document.body.classList.add('open');document.getElementById('t').focus()">Ask</button>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js"></script>
<script>
const PAGE={json.dumps(page)},WHERE={json.dumps(where)},HIST={json.dumps(hist, ensure_ascii=False)},URL="{chat_url}";
const log=document.getElementById('log'),t=document.getElementById('t'),b=document.getElementById('b');
function md(x){{try{{return DOMPurify.sanitize(marked.parse(x))}}catch(_){{const d=document.createElement('div');
d.textContent=x;return d.innerHTML.replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>').replace(/\\n/g,'<br>')}}}}
let last=null;
function add(role,text,where){{if(role==='user'&&where&&where!==last){{const c=document.createElement('div');
c.className='ctx';c.textContent=where;log.appendChild(c);last=where}}
const d=document.createElement('div');d.className='msg '+(role==='user'?'me':'them');
if(role==='user')d.textContent=text;else d.innerHTML=md(text);log.appendChild(d);log.scrollTop=log.scrollHeight;return d}}
HIST.forEach(h=>add(h.role,h.text,h.page));
async function send(text){{if(!text.trim())return;add('user',text,WHERE);t.value='';b.disabled=true;
const w=add('assistant','Thinking…');
try{{const r=await fetch(URL,{{method:'POST',headers:{{'Content-Type':'application/json'}},
body:JSON.stringify({{message:text,page:PAGE}})}});const j=await r.json();w.innerHTML=md(j.reply||j.error||'Something went wrong.');
if(j.changed)w.insertAdjacentHTML('beforeend','<div class="where">Updated your files.</div>')}}
catch(_){{w.textContent='Could not reach the server. Try again?'}}b.disabled=false;log.scrollTop=log.scrollHeight}}
document.getElementById('f').addEventListener('submit',ev=>{{ev.preventDefault();send(t.value)}});
t.addEventListener('keydown',ev=>{{if(ev.key==='Enter'&&!ev.shiftKey){{ev.preventDefault();send(t.value)}}}});
document.querySelectorAll('.chips .chip').forEach(c=>c.addEventListener('click',()=>{{document.body.classList.add('open');send(c.innerText)}}));
</script></body></html>"""


def render_home(s: Settings, store: Store, companion: Companion) -> str:
    searches = []
    for sr in store.list_searches():
        state = "watching" if sr.get("active", True) else "paused"
        searches.append(f'<div class="card"><b>{e(sr.get("name", sr["slug"]))}</b> <span class="muted">· {state}</span>'
                        f'<div class="muted">{e(sr.get("summary", ""))}</div></div>')
    rows = []
    for r in reversed(companion.roundups()[-5:]):
        rows.append(f'<h2><a href="{e(links.roundup_url(s, r["id"]))}">Roundup · {e(r.get("at", "")[:16].replace("T", " "))}'
                    f"</a></h2>" + "".join(_find_row(s, r["id"], it) for it in r["items"]))
    body = ("<h1>Your hunt</h1><h2>Searches</h2>" + ("".join(searches) or "<p class='muted'>No searches yet.</p>")
            + ("".join(rows) or "<h2>Finds</h2><p class='muted'>No finds yet.</p>"))
    return _page(s, "Your hunt", body, {"kind": "home"}, companion.page_label(None), companion.history())


def render_roundup(s: Settings, companion: Companion, roundup: dict[str, Any]) -> str:
    body = (f'<a class="muted" href="{e(links.home_url(s))}">&larr; Home</a><h1>Your finds</h1>'
            f"<p class='muted'>{e(roundup.get('at', '')[:16].replace('T', ' '))}</p>"
            + "".join(_find_row(s, roundup["id"], it) for it in roundup["items"]))
    page = {"kind": "roundup", "roundup_id": roundup["id"]}
    return _page(s, "Your finds", body, page, companion.page_label(page), companion.history())


def render_find(s: Settings, companion: Companion, roundup: dict[str, Any], find: dict[str, Any]) -> str:
    lst, rev = find.get("listing") or {}, find.get("review") or {}
    photos = "".join(f'<a href="{e(resize_image(u, 1600))}"><img src="{e(resize_image(u, 800))}" alt="" '
                     f'loading="lazy"></a>' for u in lst.get("images") or [])
    ship = lst.get("shipping")
    ship_txt = f" + {ship} shipping" if ship not in (None, "0.0", "0.00", 0) else ""
    opts = ", ".join(BUY.get(o, o) for o in lst.get("buying_options") or [])
    ask = ""
    if rev.get("missing_info") or rev.get("seller_question"):
        ask = '<div class="card"><h3>Missing from the listing</h3>'
        if rev.get("missing_info"):
            ask += "<ul>" + "".join(f"<li>{e(x)}</li>" for x in rev["missing_info"]) + "</ul>"
        if rev.get("seller_question"):
            ask += (f'<div class="ask" id="sq">{e(rev["seller_question"])}</div><button class="chip" type="button" '
                    'onclick="navigator.clipboard.writeText(document.getElementById(\'sq\').innerText);'
                    'this.innerText=\'Copied\'">Copy question</button>')
        ask += "</div>"
    body = f"""<a class="muted" href="{e(links.roundup_url(s, roundup['id']))}">&larr; All finds</a>
{_tier(rev)}<h1>{e(rev.get('headline', ''))}</h1><div class="muted">#{find['n']} · {e(lst.get('title') or '')}</div>
<div class="photos">{photos}</div>
<p style="margin:4px 0 12px"><b>{e(lst.get('price') or '')}</b>{e(ship_txt)} · {e(lst.get('condition') or '')}
{(' · ' + e(opts)) if opts else ''}</p>
<a class="btn" href="{e(lst.get('url') or '')}" target="_blank" rel="noopener">Open on eBay</a>
{_section('Why', rev.get('why'))}{_section('Spotted in the photos', rev.get('photo_findings'))}
{_section('Fit', rev.get('fit'))}{_section('Price', rev.get('price_take'))}
{_section('Watch out for', rev.get('concerns'))}{ask}"""
    page = {"kind": "find", "roundup_id": roundup["id"], "n": find["n"]}
    return _page(s, lst.get("title") or "Find", body, page, companion.page_label(page), companion.history())


def create_app(settings: Settings, store: Store, companion: Companion, *,
               before_read: Callable[[], None] = lambda: None,
               after_write: Callable[[str], None] = lambda msg: None) -> Flask:
    app = Flask(__name__)

    def _check(name: str) -> None:
        if not links.valid(settings.web_secret, name, request.args.get("k", "")):
            abort(404)

    @app.get("/healthz")
    def health():
        return "ok"

    @app.get("/")
    def home():
        _check(links.HOME)
        before_read()
        return render_home(settings, store, companion)

    @app.get("/r/<roundup_id>")
    def roundup_page(roundup_id):
        _check(roundup_id)
        before_read()
        try:
            return render_roundup(settings, companion, companion.roundup(roundup_id))
        except FindNotFound:
            abort(404)

    @app.get("/r/<roundup_id>/<int:n>")
    def find_page(roundup_id, n):
        _check(roundup_id)
        before_read()
        try:
            return render_find(settings, companion, companion.roundup(roundup_id), companion.find(roundup_id, n))
        except FindNotFound:
            abort(404)

    @app.post("/chat")
    def chat():
        _check(links.HOME)
        body = request.get_json(silent=True) or {}
        message = (body.get("message") or "").strip()[:4000]
        if not message:
            return jsonify(error="Empty message"), 400
        try:
            reply = companion.reply(message, page=body.get("page"))
        except Exception as ex:  # keep the page usable; details go to the server log
            app.logger.exception("chat failed")
            return jsonify(error=f"Something went wrong: {type(ex).__name__}"), 500
        changed = bool(companion.history()[-1].get("changed_files"))
        after_write("chat")
        return jsonify(reply=reply, changed=changed)

    return app
