"""One email in, one reply out. Shared by the terminal CLI and the email loop."""
import json
import os
import re
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Browser

from .reply import Package, Result, compose, compose_html, message, package, render_html, render_text
from .request import MAX_DOCS, answer_question, parse
from .site import DOC_TYPES, MatterNotFound, fetch

MAX_PAIRS = 5  # matter x type combinations per email; each one is a full browse

HELP = [("p", 'Email me a matter number and a document type, e.g. "Other Documents from M12205".'),
        ("list", "You can also:", [
            "Ask about a matter: status, outcome, filing dates, how many files",
            'Reply "more" for the next batch of the same documents',
            'Ask for specific documents by number: "send 99761" or "H-4"',
            'Narrow it down: "just 3 exhibits", "only the board orders"',
            "Ask for several matters or types in one email (up to 5 combinations)"]),
        ("p", f"Document types: {', '.join(DOC_TYPES)}. Up to {MAX_DOCS} files per reply.")]


def handle(text: str, browser: Browser, work_dir: Path, log=print, name: str = "there",
           history: dict | None = None, reply_to: str = "") -> tuple[str, str, Package] | None:
    """The reply as plain text and HTML, and the ZIPs to send; None when no reply is needed
    (a thank-you). `history` is this sender's past requests, for "more"; it is updated.
    `reply_to` is the agent's address, for the one-click follow-ups."""
    req = parse(text)
    history = {} if history is None else history
    if req.kind == "thanks":
        return None
    if not req.matters:
        if req.kind == "help" or not req.bad_matters and not req.doc_types:
            blocks = [("p", f"Hi {name},"), *HELP, ("p", "UARB Document Agent")]
            return render_text(blocks), render_html(blocks), Package()
        hint = (f" I saw {', '.join(req.bad_matters)}, but matter numbers are M and 5 digits, like M12205."
                if req.bad_matters else "")
        return *message(name, "I couldn't find a matter number in your email." + hint,
                        'Try: "Other Documents from M12205".'), Package()

    pairs = []
    for matter in req.matters:
        types = req.doc_types
        if not types and req.more and history.get(f"last:{matter}"):  # "more" continues the last type
            types = [history[f"last:{matter}"]]
        if not types and req.numbers:  # exhibits are numbered H-4 etc., other tabs plain digits
            types = (["Exhibits"] if any(re.match(r"[A-Za-z]", n) for n in req.numbers) else []) + \
                    (["Key Documents", "Other Documents"] if any(n.isdigit() for n in req.numbers) else [])
        pairs += [(matter, t) for t in types] or [(matter, None)]

    out = work_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    results = []
    for matter, doc_type in pairs[:MAX_PAIRS]:
        found = {d.number.lower() for r in results if r.matter == matter and r.info for d in r.info.picked}
        if req.numbers and not req.doc_types and {n.lower() for n in req.numbers} <= found:
            continue  # every number asked for is found; the same document can sit in two tabs
        skip =history.get(f"{matter}|{doc_type}", 0) if req.more and doc_type else 0
        r = Result(matter, doc_type or "", req.limit, skip=skip)
        for attempt in range(2):
            # A new context is a new WebDirect session. Reusing one resumes wherever the last
            # request left it (or an expired session), and the search box isn't there.
            ctx = browser.new_context(viewport={"width": 1400, "height": 1000}, accept_downloads=True)
            try:
                r.info = fetch(ctx.new_page(), matter, doc_type, out / f"{matter} - {doc_type}" if doc_type else out,
                               limit=req.limit, log=log, skip=skip, numbers=req.numbers, title_words=req.title_words)
                r.error = None
                break
            except MatterNotFound:
                r.error = "not found on the UARB site. Please check the number."
                break
            except Exception as e:  # noqa: BLE001 - one bad matter shouldn't sink the rest
                log(f"{matter} attempt {attempt + 1} failed: {str(e).splitlines()[0]}")
                r.error = "the UARB site didn't respond, even after a retry. Please try again in a few minutes."
            finally:
                ctx.close()
        if r.info and doc_type:
            history[f"{matter}|{doc_type}"] = skip + len(r.info.picked)
            history[f"last:{matter}"] = doc_type
        results.append(r)
    if req.numbers:  # searched several tabs for the numbers: keep the tabs they were in
        results = [r for r in results if not r.info or not r.doc_type or r.info.picked] or results

    # Full ZIPs too big for one email go in links/, which the mail loop serves publicly.
    pkg = package([r for r in results if r.doc_type], out, links_dir=work_dir.parent / "links",
                  base_url=os.environ.get("PUBLIC_URL") or _quick_tunnel_url())
    # A direct answer when the email asks something; "Can you send the exhibits?" is a request,
    # not a question. The rest of the reply says what was done, so there is no other opener.
    answer = answer_question(text, _facts(results)) if req.kind == "question" else None
    notes = []
    if any(not r.doc_type and r.info for r in results):
        notes.append(f"For files, reply with a document type: {', '.join(DOC_TYPES)}.")
    if len(pairs) > MAX_PAIRS:
        notes.append(f"I do up to {MAX_PAIRS} matter/type combinations per email; send the rest separately.")
    return compose(results, pkg, name, notes, answer), compose_html(results, pkg, name, notes, answer, reply_to), pkg


def _facts(results: list[Result]) -> list[dict]:
    """Everything the agent read from the site, for answering questions about it."""
    seen = {}
    for r in results:
        if not r.info:
            continue
        f = seen.setdefault(r.matter, {"matter": r.matter, "details": r.info.meta, "document_counts": r.info.counts,
                                       "documents_seen": {}})
        if r.doc_type:
            f["documents_seen"][r.doc_type] = [
                {"number": d.number, "title": d.title, "date": d.date, "security": d.security} for d in r.info.docs]
    return list(seen.values())


def _quick_tunnel_url() -> str | None:
    """The current address of a cloudflared quick tunnel, from its metrics port."""
    if not (metrics := os.environ.get("QUICK_TUNNEL")):
        return None
    try:
        with urllib.request.urlopen(f"{metrics}/quicktunnel", timeout=5) as r:
            host = json.load(r).get("hostname")
    except Exception:  # noqa: BLE001 - no tunnel: fall back to splitting across emails
        return None
    return f"https://{host}" if host else None
