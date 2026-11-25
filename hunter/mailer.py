"""Email delivery over SMTP (one roundup per run), and reading the user's replies over IMAP as feedback."""

from __future__ import annotations

import email
import email.header
import html
import imaplib
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any

from . import links
from .config import Settings
from .ebay import resize_image

TAG_RE = re.compile(r"\[hunter:([^\]\s]+)\]")
TIERS = {  # order = how finds are ranked in a roundup
    "strong_buy": ("Strong buy", "#1a7f37"),
    "worth_a_look": ("Worth a look", "#9a6700"),
    "long_shot": ("Long shot", "#6e7781"),
}


def rank(find: dict[str, Any]) -> tuple:
    v = find["verdict"]
    return (list(TIERS).index(v["verdict"]) if v["verdict"] in TIERS else 9, -float(v.get("score") or 0))


def roundup_subject(finds: list[dict[str, Any]], roundup_id: str) -> str:
    counts = {}
    for f in finds:
        counts[f["verdict"]["verdict"]] = counts.get(f["verdict"]["verdict"], 0) + 1
    bits = [f"{counts[k]} {TIERS[k][0].lower()}" for k in TIERS if counts.get(k)]
    top = finds[0]["item"].get("title") or ""
    return f"{len(finds)} find{'s' if len(finds) != 1 else ''} ({', '.join(bits)}): {top[:45]}… [hunter:{roundup_id}]"


def _card_html(n: int, f: dict[str, Any], talk_url: str | None = None) -> str:
    e = html.escape
    item, v, search = f["item"], f["verdict"], f["search"]
    label, color = TIERS.get(v["verdict"], ("Find", "#6e7781"))
    if v.get("off_brief_gem"):
        label += " · off-brief gem"
    img = ""
    if item.get("images"):
        img = (f'<td style="width:110px;vertical-align:top;padding-right:12px"><a href="{e(item.get("url") or "")}">'
               f'<img src="{e(resize_image(item["images"][0], 300))}" width="110" alt="" '
               f'style="border-radius:6px;display:block"></a></td>')
    ship = item.get("shipping")
    ship_txt = f" + {ship} ship" if ship not in (None, "0.0", "0.00", 0) else ""
    points = (v.get("why") or [])[:2] + (v.get("photo_findings") or [])[:1]
    ask = ""
    if v.get("seller_question"):
        ask = f'<div style="color:#555;font-size:13px;margin-top:4px">Ask the seller: “{e(v["seller_question"])}”</div>'
    talk = ""
    if talk_url:
        talk = (f'<div style="margin-top:6px;font-size:14px"><a href="{e(talk_url)}">Full breakdown and talk it '
                f'through &rarr;</a></div>')
    return f"""<table role="presentation" style="width:100%;border-collapse:collapse;margin:0 0 18px">
<tr>{img}<td style="vertical-align:top">
<div style="font-size:12px;font-weight:600;color:{color};text-transform:uppercase;letter-spacing:.03em">
#{n} · {e(label)} · {e(str(v.get('score', '')))}/10</div>
<div style="font-size:16px;font-weight:600;margin:2px 0">{e(v.get('headline', ''))}</div>
<div style="font-size:14px"><a href="{e(item.get('url') or '')}">{e(item.get('title') or '')}</a>
· <b>{e(item.get('price', ''))}</b>{e(ship_txt)}</div>
<ul style="margin:6px 0 0;padding-left:18px;font-size:14px;color:#333">
{''.join(f'<li>{e(p)}</li>' for p in points if p)}</ul>
{ask}
{talk}
<div style="color:#888;font-size:12px;margin-top:4px">{e(search.get('name', ''))}</div>
</td></tr></table>"""


def render_roundup_html(finds: list[dict[str, Any]], link_for=None, home_url: str | None = None) -> str:
    home = (f'<p style="font-size:14px"><a href="{html.escape(home_url)}">Open your hunt and chat with your '
            f'companion &rarr;</a></p>') if home_url else ""
    cards = "".join(_card_html(n, f, link_for(n) if link_for else None) for n, f in enumerate(finds, 1))
    return f"""<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:620px;color:#222">
<p style="font-size:15px">Here's what turned up since the last check, best first.</p>
{home}
{cards}
<p style="color:#666;font-size:13px;margin-top:20px;border-top:1px solid #eee;padding-top:12px">
Reply with thoughts by number, like “1: love it, ordered” or “3: rise looks short”. I learn from every reply.</p>
</div>"""


def render_roundup_text(finds: list[dict[str, Any]], link_for=None) -> str:
    lines = ["Here's what turned up since the last check, best first.", ""]
    for n, f in enumerate(finds, 1):
        item, v = f["item"], f["verdict"]
        label = TIERS.get(v["verdict"], ("Find",))[0]
        lines += [f"#{n} {label} ({v.get('score', '')}/10): {v.get('headline', '')}",
                  f"   {item.get('title')}  {item.get('price')}", f"   {item.get('url')}"]
        for p in (v.get("why") or [])[:2]:
            lines.append(f"   - {p}")
        if v.get("seller_question"):
            lines.append(f"   Ask the seller: {v['seller_question']}")
        if link_for:
            lines.append(f"   Talk it through: {link_for(n)}")
        lines.append("")
    lines.append('Reply with thoughts by number, like "1: love it" or "3: rise looks short". I learn from every reply.')
    return "\n".join(lines)


class Mailer:
    def __init__(self, settings: Settings, dry_run: bool = False):
        self.s = settings
        self.dry_run = dry_run or not settings.has_email

    def send_roundup(self, finds: list[dict[str, Any]]) -> tuple[str, str, list[dict[str, Any]]]:
        """Send one email with every find. Returns (roundup id, delivery note, finds in the order shown)."""
        finds = sorted(finds, key=rank)
        roundup_id = "r" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        msg = EmailMessage()
        msg["Subject"] = roundup_subject(finds, roundup_id)
        msg["From"] = f"eBay Hunter <{self.s.smtp_user or 'hunter@localhost'}>"
        msg["To"] = self.s.email_to or "you@localhost"
        msg["Message-ID"] = make_msgid(domain="ebay-hunter")
        link_for = (lambda n: links.find_url(self.s, roundup_id, n)) if self.s.has_web else None
        msg.set_content(render_roundup_text(finds, link_for))
        home = links.home_url(self.s) if self.s.has_web else None
        msg.add_alternative(render_roundup_html(finds, link_for, home), subtype="html")
        if self.dry_run:
            self.s.outbox_dir.mkdir(parents=True, exist_ok=True)
            path = self.s.outbox_dir / f"{roundup_id}.eml"
            path.write_bytes(bytes(msg))
            (self.s.outbox_dir / f"{roundup_id}.html").write_text(render_roundup_html(finds, link_for, home))
            delivery = f"dry-run: wrote {self.s.outbox_dir / (roundup_id + '.html')}"
        else:
            with smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, context=ssl.create_default_context()) as smtp:
                smtp.login(self.s.smtp_user, self.s.smtp_password)
                smtp.send_message(msg)
            delivery = f"sent to {self.s.email_to}"
        return roundup_id, delivery, finds

    # ---- replies -> feedback ------------------------------------------------
    def fetch_replies(self) -> list[dict[str, str]]:
        """Unread replies to hunter emails. Marks them read."""
        if not self.s.has_email:
            return []
        out = []
        with imaplib.IMAP4_SSL(self.s.imap_host) as imap:
            imap.login(self.s.smtp_user, self.s.smtp_password)
            imap.select("INBOX")
            status, data = imap.search(None, '(UNSEEN SUBJECT "[hunter:")')
            if status != "OK":
                return []
            for num in data[0].split():
                status, parts = imap.fetch(num, "(RFC822)")
                if status != "OK":
                    continue
                parsed = parse_reply(email.message_from_bytes(parts[0][1]))
                if parsed:
                    out.append(parsed)
                imap.store(num, "+FLAGS", "\\Seen")
        return out


def parse_reply(msg: email.message.Message) -> dict[str, str] | None:
    subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
    m = TAG_RE.search(subject)
    if not m:
        return None
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                break
    else:
        body = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
    note = strip_quoted(body)
    if not note:
        return None
    return {"tag": m.group(1), "note": note}


def split_numbered(note: str) -> dict[int, str]:
    """'1: love it\\n#3 too short' -> {1: 'love it', 3: 'too short'}. Unnumbered text is returned under 0."""
    out: dict[int, str] = {}
    current = 0
    for line in note.splitlines():
        m = re.match(r"^\s*#?(\d{1,2})\s*[:.)\-]\s*(.*)$", line)
        if m:
            current = int(m.group(1))
            out[current] = m.group(2).strip()
        elif line.strip():
            out[current] = (out.get(current, "") + " " + line.strip()).strip()
    return out


def strip_quoted(body: str) -> str:
    keep = []
    for line in body.splitlines():
        if re.match(r"^\s*On .+wrote:\s*$", line) or line.startswith(">") or line.strip() == "--":
            break
        if re.match(r"^-+\s*Original Message\s*-+", line, re.I):
            break
        keep.append(line)
    return "\n".join(keep).strip()


def guess_reaction(note: str) -> str:
    n = note.lower()
    if re.search(r"\b(bought|purchased|grabbed|copped|ordered)\b", n):
        return "bought"
    if re.search(r"\b(love|perfect|great|yes|nice|want)\b", n) and not re.search(r"\b(not|don't|dont)\b", n):
        return "liked"
    if re.search(r"\b(pass|no|nah|not|too|wrong|don't|dont|hate|meh)\b", n):
        return "passed"
    return "note"
