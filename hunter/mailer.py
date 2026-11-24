"""Email delivery over SMTP, and reading the user's replies over IMAP as feedback."""

from __future__ import annotations

import email
import html
import imaplib
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any

from .config import Settings
from .ebay import resize_image

TAG_RE = re.compile(r"\[hunter:([^\]\s]+)\]")
LABELS = {"strong_buy": "Strong buy", "worth_a_look": "Worth a look"}


def subject_for(item: dict[str, Any], verdict: dict[str, Any]) -> str:
    label = "Off-brief gem" if verdict.get("off_brief_gem") else LABELS.get(verdict["verdict"], "Found")
    title = (item.get("title") or "")[:70]
    return f"{label}: {title} · {item.get('price', '')} [hunter:{item['item_id']}]"


def _li(items: list[str]) -> str:
    return "".join(f"<li>{html.escape(x)}</li>" for x in items if x)


def render_html(item: dict[str, Any], verdict: dict[str, Any], search: dict[str, Any]) -> str:
    e = html.escape
    img = ""
    if item.get("images"):
        img = (f'<a href="{e(item["url"] or "")}"><img src="{e(resize_image(item["images"][0], 500))}" '
               f'alt="" style="max-width:100%;border-radius:8px;margin:8px 0"></a>')
    ship = f" + {item['shipping']} shipping" if item.get("shipping") not in (None, "0.0", "0.00", 0) else ""
    sections = []

    def section(title: str, body: str) -> None:
        if body:
            sections.append(f'<h3 style="margin:16px 0 4px;font-size:15px">{e(title)}</h3>{body}')

    section("Why", f"<ul>{_li(verdict.get('why', []))}</ul>" if verdict.get("why") else "")
    section("Spotted in the photos", f"<ul>{_li(verdict.get('photo_findings', []))}</ul>"
            if verdict.get("photo_findings") else "")
    section("Fit", f"<p>{e(verdict.get('fit', ''))}</p>" if verdict.get("fit") else "")
    section("Price", f"<p>{e(verdict.get('price_take', ''))}</p>" if verdict.get("price_take") else "")
    section("Watch out for", f"<ul>{_li(verdict.get('concerns', []))}</ul>" if verdict.get("concerns") else "")
    if verdict.get("missing_info"):
        q = verdict.get("seller_question", "")
        section("Missing from the listing",
                f"<ul>{_li(verdict['missing_info'])}</ul>"
                + (f'<p>Ask the seller:</p><blockquote style="border-left:3px solid #ccc;margin:0;padding-left:10px">'
                   f"{e(q)}</blockquote>" if q else ""))

    return f"""<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:600px;color:#222">
<p style="color:#666;font-size:13px;margin:0">From your search: {e(search.get('name', ''))}</p>
<h2 style="margin:6px 0">{e(verdict.get('headline', ''))}</h2>
<p style="margin:4px 0"><a href="{e(item.get('url') or '')}">{e(item.get('title') or '')}</a><br>
<b>{e(item.get('price', ''))}</b>{e(ship)} · {e(item.get('condition') or '')}
{' · ' + e(', '.join(item.get('buying_options') or [])) if item.get('buying_options') else ''}</p>
{img}
{''.join(sections)}
<p style="margin-top:20px"><a href="{e(item.get('url') or '')}" style="background:#222;color:#fff;padding:10px 16px;
border-radius:6px;text-decoration:none">Open on eBay</a></p>
<p style="color:#666;font-size:13px;margin-top:20px">Reply with what you think ("love it", "too short in the
rise", "bought it", "not my era") and I'll learn from it.</p>
</div>"""


def render_text(item: dict[str, Any], verdict: dict[str, Any], search: dict[str, Any]) -> str:
    lines = [verdict.get("headline", ""), "", f"{item.get('title')}  {item.get('price')}", item.get("url") or "", ""]
    for title, key in [("Why", "why"), ("Spotted in the photos", "photo_findings"), ("Watch out for", "concerns"),
                       ("Missing", "missing_info")]:
        if verdict.get(key):
            lines += [title + ":"] + [f"- {x}" for x in verdict[key]] + [""]
    for title, key in [("Fit", "fit"), ("Price", "price_take"), ("Ask the seller", "seller_question")]:
        if verdict.get(key):
            lines += [f"{title}: {verdict[key]}", ""]
    lines.append(f"From your search: {search.get('name', '')}. Reply with what you think and I'll learn from it.")
    return "\n".join(lines)


class Mailer:
    def __init__(self, settings: Settings, dry_run: bool = False):
        self.s = settings
        self.dry_run = dry_run or not settings.has_email

    def send(self, item: dict[str, Any], verdict: dict[str, Any], search: dict[str, Any]) -> str:
        msg = EmailMessage()
        msg["Subject"] = subject_for(item, verdict)
        msg["From"] = f"eBay Hunter <{self.s.smtp_user or 'hunter@localhost'}>"
        msg["To"] = self.s.email_to or "you@localhost"
        msg["Message-ID"] = make_msgid(domain="ebay-hunter")
        msg.set_content(render_text(item, verdict, search))
        msg.add_alternative(render_html(item, verdict, search), subtype="html")
        if self.dry_run:
            self.s.outbox_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9]+", "_", item["item_id"])
            path = self.s.outbox_dir / f"{safe}.eml"
            path.write_bytes(bytes(msg))
            (self.s.outbox_dir / f"{safe}.html").write_text(render_html(item, verdict, search))
            return f"dry-run: wrote {path}"
        with smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, context=ssl.create_default_context()) as smtp:
            smtp.login(self.s.smtp_user, self.s.smtp_password)
            smtp.send_message(msg)
        return f"sent to {self.s.email_to}"

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
                msg = email.message_from_bytes(parts[0][1])
                parsed = parse_reply(msg)
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
    return {"item_id": m.group(1), "note": note}


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

