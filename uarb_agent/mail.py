"""The email agent: polls a Gmail inbox, runs each request through the browser, replies.

The inbox is shared with another agent, so it is opened read-only (nothing is marked, moved
or deleted), progress is kept by UID in a state file, and only mail sent to the +alias or
naming a valid matter number is answered.
"""
import email
import imaplib
import json
import os
import re
import smtplib
import time
from collections import defaultdict
from email.message import EmailMessage, Message
from email.utils import getaddresses, parseaddr
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import links
from .agent import handle
from .reply import LINK_DAYS, message
from .request import _MATTER

ADDRESS = os.environ.get("AGENT_EMAIL", "")
PASSWORD = os.environ.get("AGENT_EMAIL_PASSWORD", "")
ALIAS = os.environ.get("AGENT_ALIAS", "uarb")  # people write to user+uarb@gmail.com
DATA = Path(os.environ.get("DATA_DIR", "data"))
POLL = int(os.environ.get("POLL_SECONDS", "20"))
PER_SENDER_PER_DAY = 20
LINK_PORT = int(os.environ.get("LINK_PORT", "8080"))


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def alias_address() -> str:
    user, _, domain = ADDRESS.partition("@")
    return f"{user}+{ALIAS}@{domain}"


def body_text(msg: Message) -> str:
    """Plain text of the email, falling back to HTML with tags stripped."""
    plain = html = ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        if part.get_content_subtype() == "plain":
            plain = plain or text
        elif part.get_content_subtype() == "html":
            html = html or re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(style|script).*?</\1>", " ", text))
    return plain or html


def split_quote(text: str) -> tuple[str, str]:
    """New text, and the quoted thread under it ('On ... wrote:' or '>' lines)."""
    m = re.search(r"^\s*On .{5,200}wrote:\s*$|^\s*>", text, re.M | re.S)
    return (text[:m.start()], text[m.start():]) if m else (text, "")


def request_text(msg: Message) -> str:
    """What this email asks for. A reply in a thread is judged by its new text only: its subject
    and the quoted thread name earlier requests, so they lend just the matter number."""
    subject = msg.get("Subject", "")
    new, quoted = split_quote(body_text(msg))
    if not subject.lower().startswith("re:"):
        new = f"{subject}\n{new}"
    if not _MATTER.search(new):  # "what was the outcome?" in a thread about M12205
        new += "\n" + " ".join(dict.fromkeys(f"M{d}" for d in _MATTER.findall(f"{subject}\n{quoted}")))
    return new.strip()


def should_answer(msg: Message, text: str) -> str | None:
    """Why to skip this message, or None to answer it."""
    sender = parseaddr(msg.get("From", ""))[1].lower()
    if not sender or sender == ADDRESS.lower():
        return "from ourselves"
    if re.search(r"mailer-daemon|postmaster|no-?reply", sender):
        return "automated sender"
    if msg.get("Auto-Submitted", "no").lower() != "no" or msg.get("List-Id") \
            or msg.get("Precedence", "").lower() in ("bulk", "list", "junk"):
        return "auto-reply or list mail"
    to = " ".join(a for _, a in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []) + msg.get_all("Delivered-To", [])))
    if f"+{ALIAS}@" not in to.lower() and not _MATTER.search(text):
        return "not addressed to the agent and names no matter"
    return None


def first_name(msg: Message) -> str:
    name = parseaddr(msg.get("From", ""))[0].strip().strip('"')
    first = name.split()[0] if name else ""
    return first if first.isalpha() else "there"


def send(original: Message, body: str, attachment: Path | None, subject_suffix: str = "", html: str | None = None) -> None:
    out = EmailMessage()
    subject = original.get("Subject", "") or "Your UARB request"
    out["Subject"] = (subject if subject.lower().startswith("re:") else f"Re: {subject}") + subject_suffix
    out["From"] = f"UARB Document Agent <{ADDRESS}>"
    out["To"] = original.get("Reply-To") or original.get("From")
    out["Reply-To"] = alias_address()
    if original.get("Message-ID"):
        out["In-Reply-To"] = original["Message-ID"]
        out["References"] = (original.get("References", "") + " " + original["Message-ID"]).strip()
    out.set_content(body)
    if html:
        out.add_alternative(html, subtype="html")
    if attachment:
        out.add_attachment(attachment.read_bytes(), maintype="application", subtype="zip", filename=attachment.name)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=120) as s:
        s.login(ADDRESS, PASSWORD)
        s.send_message(out)


# Spam too: a first-time sender's short request often lands there.
FOLDERS = ["INBOX", "[Gmail]/Spam"]


def new_messages(folder: str, state: dict) -> list[tuple[int, Message]]:
    """New mail in `folder` since last time. `state` is that folder's {uidvalidity, last_uid}."""
    imap = imaplib.IMAP4_SSL("imap.gmail.com", timeout=60)  # without it a dropped connection hangs the agent forever
    try:
        imap.login(ADDRESS, PASSWORD)
        imap.select(f'"{folder}"', readonly=True)  # EXAMINE: never changes flags
        status = imap.status(f'"{folder}"', "(UIDVALIDITY UIDNEXT)")[1][0]
        validity = int(re.search(rb"UIDVALIDITY (\d+)", status).group(1))
        nxt = int(re.search(rb"UIDNEXT (\d+)", status).group(1))
        if state.get("uidvalidity") != validity:  # first run, or Gmail renumbered: start from now
            state.update(uidvalidity=validity, last_uid=nxt - 1)
            log(f"{folder}: starting from UID {nxt - 1}; earlier mail is ignored")
            return []
        if nxt - 1 <= state["last_uid"]:
            return []
        _, data = imap.uid("search", None, f"UID {state['last_uid'] + 1}:*")
        out = []
        for uid in sorted(int(u) for u in data[0].split() if int(u) > state["last_uid"]):
            _, msg_data = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
            out.append((uid, email.message_from_bytes(msg_data[0][1])))
        return out
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


def save(state: dict) -> None:
    _save("state.json", state)


def _load(name: str) -> dict:
    path = DATA / name
    return json.loads(path.read_text()) if path.exists() else {}


def _save(name: str, value: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / name).write_text(json.dumps(value))


def process(browser, uid: int | str, msg: Message, sent_today: dict) -> None:
    text = f"{msg.get('Subject', '')}\n{body_text(msg)}"
    if why := should_answer(msg, text):
        log(f"UID {uid}: skipped ({why})")
        return
    sender = parseaddr(msg.get("From", ""))[1].lower()
    day = time.strftime("%Y-%m-%d")
    if sent_today[(sender, day)] >= PER_SENDER_PER_DAY:
        log(f"UID {uid}: skipped ({sender} is over {PER_SENDER_PER_DAY} requests today)")
        return
    sent_today[(sender, day)] += 1
    new = request_text(msg)
    log(f"UID {uid}: request from {sender}: {' '.join(new.split())[:200]}")
    histories = _load("history.json")
    try:
        reply = handle(new, browser, DATA / "downloads", log=log, name=first_name(msg), reply_to=alias_address(),
                       history=histories.setdefault(sender, {}))
        _save("history.json", histories)
    except Exception as e:  # noqa: BLE001 - always answer, even when something broke
        log(f"UID {uid}: failed: {e!r}")
        reply = (*message(first_name(msg), "Sorry, something went wrong while I was getting your documents "
                                           "from the UARB site. Please try again in a few minutes."), None)
    if reply is None:
        log(f"UID {uid}: no reply needed (a thank-you)")
        return
    body, html, pkg = reply
    send(msg, body, pkg.zips[0] if pkg and pkg.zips else None, html=html)
    # With a download link the full ZIP is a click away; without one, the other parts follow by email.
    for i, z in enumerate(pkg.zips[1:] if pkg and not pkg.link else [], start=2):
        send(msg, f"Part {i} of {len(pkg.zips)} of the documents you asked for.\n\nUARB Document Agent", z,
             f" (part {i} of {len(pkg.zips)})")
    log(f"UID {uid}: replied with {len(pkg.zips) if pkg else 0} ZIP(s)")


def main() -> None:
    if not ADDRESS or not PASSWORD:
        raise SystemExit("Set AGENT_EMAIL and AGENT_EMAIL_PASSWORD (a Gmail app password)")
    state_file = DATA / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    sent_today: dict = defaultdict(int)
    log(f"Watching {ADDRESS} (write to {alias_address()})")
    links.serve(DATA / "links", LINK_PORT)
    log(f"Serving download links on :{LINK_PORT} (public via {os.environ.get('PUBLIC_URL') or os.environ.get('QUICK_TUNNEL') or 'nothing: links off'})")
    with sync_playwright() as p:
        # Stays up for the life of the agent; each request opens its own fresh session in it.
        # Headed when there is a screen (the server's virtual one, viewable over noVNC).
        browser = p.chromium.launch(headless=not os.environ.get("DISPLAY"))
        while True:
            if not browser.is_connected():  # crashed: start a new one rather than fail every request
                browser = p.chromium.launch(headless=not os.environ.get("DISPLAY"))
            for folder in FOLDERS:
                try:
                    for uid, msg in new_messages(folder, state.setdefault(folder, {})):
                        process(browser, f"{folder}#{uid}", msg, sent_today)
                        state[folder]["last_uid"] = uid
                        save(state)
                    save(state)
                except Exception as e:  # noqa: BLE001 - a network blip must not stop the agent
                    log(f"Poll of {folder} failed: {e!r}")
            links.cleanup(DATA / "links", LINK_DAYS)
            links.cleanup(DATA / "downloads", 1)  # the raw files: ~100 MB a request
            time.sleep(POLL)


if __name__ == "__main__":
    main()
