"""The web page behind each roundup: every find's full breakdown, and a chat to talk it through."""

from __future__ import annotations

import html
import json
from typing import Any, Callable

from flask import Flask, abort, jsonify, request

from . import links
from .chat import FindChat, FindNotFound
from .config import Settings
from .ebay import resize_image
from .mailer import TIERS

e = html.escape

CSS = """
:root{--bg:#faf9f7;--card:#fff;--ink:#1d1d1f;--muted:#6b6b70;--line:#e6e4e0;--accent:#1d1d1f;--accent-ink:#fff;
--me:#1d1d1f;--me-ink:#fff;--them:#f0eeea;--strong:#1a7f37;--look:#9a6700;--long:#6e7781}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121214;--card:#1b1b1e;--ink:#ececee;
--muted:#9a9aa2;--line:#2c2c31;--accent:#ececee;--accent-ink:#121214;--me:#ececee;--me-ink:#121214;--them:#25252a;
--strong:#4ac26b;--look:#d4a72c;--long:#9198a1}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:720px;margin:0 auto;padding:16px 16px 120px}a{color:inherit}
.tier{font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase}
.strong_buy{color:var(--strong)}.worth_a_look{color:var(--look)}.long_shot{color:var(--long)}
h1{font-size:22px;line-height:1.25;margin:4px 0 8px}.muted{color:var(--muted);font-size:14px}
.photos{display:flex;gap:8px;overflow-x:auto;scroll-snap-type:x mandatory;margin:12px -16px;padding:0 16px}
.photos img{height:260px;border-radius:10px;scroll-snap-align:start;flex:none;background:var(--line)}
.btn{display:inline-block;background:var(--accent);color:var(--accent-ink);padding:10px 16px;border-radius:8px;
text-decoration:none;font-weight:600;border:0;font-size:15px;cursor:pointer}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:12px 0}
.card h3{margin:0 0 6px;font-size:14px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.card ul{margin:0;padding-left:18px}.ask{border-left:3px solid var(--line);padding-left:10px;margin:8px 0}
.chat{margin-top:20px}.msg{padding:10px 14px;border-radius:14px;margin:8px 0;max-width:88%;overflow-wrap:anywhere}
.msg p{margin:.3em 0}.me{background:var(--me);color:var(--me-ink);margin-left:auto}.them{background:var(--them)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}.chip{border:1px solid var(--line);background:var(--card);
color:var(--ink);border-radius:999px;padding:6px 12px;font-size:14px;cursor:pointer}
form.say{position:fixed;left:0;right:0;bottom:0;background:var(--bg);border-top:1px solid var(--line);padding:10px 16px}
form.say div{max-width:720px;margin:0 auto;display:flex;gap:8px}
form.say textarea{flex:1;resize:none;border:1px solid var(--line);border-radius:10px;padding:10px;font:inherit;
background:var(--card);color:var(--ink);min-height:44px;max-height:140px}
.row{display:flex;gap:12px;align-items:flex-start;text-decoration:none}.row img{width:88px;border-radius:8px;flex:none}
"""

HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<meta name="robots" content="noindex"><style>{css}</style></head><body><main>"""


def _page(title: str, body: str, script: str = "") -> str:
    return HEAD.format(title=e(title), css=CSS) + body + "</main>" + script + "</body></html>"


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


def render_roundup(s: Settings, roundup: dict[str, Any]) -> str:
    rows = []
    for it in roundup["items"]:
        lst, rev = it.get("listing") or {}, it.get("review") or {}
        img = f'<img src="{e(resize_image(lst["images"][0], 300))}" alt="">' if lst.get("images") else ""
        rows.append(f'<a class="card row" href="{e(links.find_url(s, roundup["id"], it["n"]))}">{img}<div>'
                    f'{_tier(rev)}<div style="font-weight:600">{e(rev.get("headline", ""))}</div>'
                    f'<div class="muted">#{it["n"]} · {e(it["title"] or "")} · {e(it["price"] or "")}</div></div></a>')
    return _page("Your finds", f"<h1>Your finds</h1><p class='muted'>{e(roundup.get('at', ''))}</p>" + "".join(rows))


BUY = {"FIXED_PRICE": "Buy It Now", "BEST_OFFER": "Best Offer", "AUCTION": "Auction"}
QUICK = ["Will these fit me?", "How does this compare to the others?", "What's it really worth?",
         "I want it. What's the plan?", "Not for me"]


def render_find(s: Settings, roundup: dict[str, Any], find: dict[str, Any], history: list[dict]) -> str:
    lst, rev = find.get("listing") or {}, find.get("review") or {}
    photos = "".join(f'<a href="{e(resize_image(u, 1600))}"><img src="{e(resize_image(u, 800))}" alt="" '
                     f'loading="lazy"></a>' for u in lst.get("images") or [])
    ship = lst.get("shipping")
    ship_txt = f" + {ship} shipping" if ship not in (None, "0.0", "0.00", 0) else ""
    ask = ""
    if rev.get("missing_info") or rev.get("seller_question"):
        ask = '<div class="card"><h3>Missing from the listing</h3>'
        if rev.get("missing_info"):
            ask += "<ul>" + "".join(f"<li>{e(x)}</li>" for x in rev["missing_info"]) + "</ul>"
        if rev.get("seller_question"):
            ask += (f'<div class="ask" id="sq">{e(rev["seller_question"])}</div>'
                    '<button class="chip" onclick="navigator.clipboard.writeText(document.getElementById(\'sq\')'
                    '.innerText);this.innerText=\'Copied\'">Copy question</button>')
        ask += "</div>"
    back = links.roundup_url(s, roundup["id"])
    body = f"""<a class="muted" href="{e(back)}">&larr; All finds</a>
{_tier(rev)}<h1>{e(rev.get('headline', ''))}</h1>
<div class="muted">#{find['n']} · {e(lst.get('title') or '')}</div>
<div class="photos">{photos}</div>
<p style="margin:4px 0 12px"><b>{e(lst.get('price') or '')}</b>{e(ship_txt)} · {e(lst.get('condition') or '')}
{(' · ' + e(', '.join(BUY.get(o, o) for o in lst.get('buying_options') or []))) if lst.get('buying_options') else ''}</p>
<a class="btn" href="{e(lst.get('url') or '')}" target="_blank" rel="noopener">Open on eBay</a>
{_section('Why', rev.get('why'))}{_section('Spotted in the photos', rev.get('photo_findings'))}
{_section('Fit', rev.get('fit'))}{_section('Price', rev.get('price_take'))}
{_section('Watch out for', rev.get('concerns'))}{ask}
<div class="chat"><h3 class="muted" style="text-transform:uppercase;font-size:14px;letter-spacing:.04em">
Talk it through</h3><div id="log"></div>
<div class="chips">{''.join(f'<button class="chip" type="button">{e(q)}</button>' for q in QUICK)}</div></div>"""
    form = """<form class="say" id="f"><div><textarea id="t" rows="1" placeholder="Ask anything about this one"></textarea>
<button class="btn" id="b">Send</button></div></form>"""
    script = f"""<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js"></script>
<script>
const hist={json.dumps([{"role": h["role"], "text": h["text"]} for h in history])};
const url=location.pathname+"/chat"+location.search;
const log=document.getElementById('log'),t=document.getElementById('t'),b=document.getElementById('b');
function md(x){{try{{return DOMPurify.sanitize(marked.parse(x))}}catch(_){{const d=document.createElement('div');
d.textContent=x;return d.innerHTML.replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>').replace(/\\n/g,'<br>')}}}}
function add(role,text){{const d=document.createElement('div');d.className='msg '+(role==='user'?'me':'them');
d.innerHTML=role==='user'?'':md(text);if(role==='user')d.textContent=text;log.appendChild(d);
d.scrollIntoView({{behavior:'smooth',block:'end'}});return d}}
hist.forEach(h=>add(h.role,h.text));
async function send(text){{if(!text.trim())return;add('user',text);t.value='';b.disabled=true;
const w=add('assistant','Thinking…');
try{{const r=await fetch(url,{{method:'POST',headers:{{'Content-Type':'application/json'}},
body:JSON.stringify({{message:text}})}});const j=await r.json();w.innerHTML=md(j.reply||j.error||'Something went wrong.')}}
catch(_){{w.textContent='Could not reach the server. Try again?'}}b.disabled=false;w.scrollIntoView({{block:'end'}})}}
document.getElementById('f').addEventListener('submit',ev=>{{ev.preventDefault();send(t.value)}});
t.addEventListener('keydown',ev=>{{if(ev.key==='Enter'&&!ev.shiftKey){{ev.preventDefault();send(t.value)}}}});
document.querySelectorAll('.chips .chip').forEach(c=>c.addEventListener('click',()=>send(c.innerText)));
</script>"""
    return _page(lst.get("title") or "Find", body + form, script)


def create_app(settings: Settings, chat: FindChat, *, before_read: Callable[[], None] = lambda: None,
               after_write: Callable[[str], None] = lambda msg: None) -> Flask:
    app = Flask(__name__)

    def _check(roundup_id: str) -> None:
        if not links.valid(settings.web_secret, roundup_id, request.args.get("k", "")):
            abort(404)

    @app.get("/healthz")
    def health():
        return "ok"

    @app.get("/r/<roundup_id>")
    def roundup_page(roundup_id):
        _check(roundup_id)
        before_read()
        try:
            return render_roundup(settings, chat.roundup(roundup_id))
        except FindNotFound:
            abort(404)

    @app.get("/r/<roundup_id>/<int:n>")
    def find_page(roundup_id, n):
        _check(roundup_id)
        before_read()
        try:
            return render_find(settings, chat.roundup(roundup_id), chat.find(roundup_id, n), chat.history(roundup_id, n))
        except FindNotFound:
            abort(404)

    @app.post("/r/<roundup_id>/<int:n>/chat")
    def find_chat(roundup_id, n):
        _check(roundup_id)
        message = ((request.get_json(silent=True) or {}).get("message") or "").strip()[:4000]
        if not message:
            return jsonify(error="Empty message"), 400
        try:
            reply = chat.reply(roundup_id, n, message)
        except FindNotFound:
            abort(404)
        except Exception as ex:  # keep the page usable; details go to the server log
            app.logger.exception("chat failed")
            return jsonify(error=f"Something went wrong: {type(ex).__name__}"), 500
        after_write(f"chat: {roundup_id} #{n}")
        return jsonify(reply=reply)

    return app
