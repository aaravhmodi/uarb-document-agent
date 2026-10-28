"""Builds the ZIP(s) and the reply (plain text and styled HTML) from fetched matters.

Replies are short: the matter in a few lines, then each document type's files, then only the
notes that apply, then one-click follow-ups.
"""
import secrets
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote

from .site import DOC_TYPES, URL, Doc, Matter

# Gmail rejects messages over 25 MB *after* base64, which adds a third.
ATTACH_LIMIT = 18 * 1024 * 1024
LINK_DAYS = 7
SIGN = "UARB Document Agent"
MAX_ACTIONS = 4

_SINGULAR = {"Exhibits": "Exhibit", "Key Documents": "Key Document", "Other Documents": "Other Document",
             "Transcripts": "Transcript", "Recordings": "Recording"}


@dataclass
class Result:
    """One (matter, document type) the user asked for."""
    matter: str
    doc_type: str
    limit: int
    info: Matter | None = None
    error: str | None = None
    skip: int = 0  # documents sent in earlier replies ("more")


@dataclass
class Package:
    zips: list[Path] = field(default_factory=list)  # one per email; the first goes with the reply
    attached: list[list[Doc]] = field(default_factory=list)  # the docs in each ZIP
    left_out: list[Doc] = field(default_factory=list)  # too big to email even alone
    link: str | None = None  # the full ZIP, when it doesn't all fit in one email
    link_bytes: int = 0

    @property
    def docs(self) -> list[Doc]:
        return [d for part in self.attached for d in part]

    @property
    def emailed(self) -> list[Doc]:
        """Docs that reach the user as an attachment: only the first ZIP once there is a link."""
        return (self.attached[0] if self.attached else []) if self.link else self.docs

    @property
    def all(self) -> list[Doc]:
        return self.docs + self.left_out


def package(results: list[Result], out_dir: Path, limit: int = ATTACH_LIMIT,
            links_dir: Path | None = None, base_url: str | None = None) -> Package:
    """Packs the files, in order, into as few ZIPs under `limit` as it can, one per email. If it
    doesn't all fit in one and `base_url` is set, also writes a full ZIP to download instead."""
    pkg = Package()
    used = limit + 1  # forces a new part on the first file
    for r in results:
        for d in (r.info.docs if r.info else []):
            if not d.files:
                continue
            d.folder = f"{r.matter} - {r.doc_type}" if len(results) > 1 else ""
            # Judge by raw size: PDFs barely compress, and it never overshoots the limit.
            size = _size(d)
            if size > limit:
                pkg.left_out.append(d)
                continue
            if used + size > limit:
                pkg.attached.append([])
                used = 0
            pkg.attached[-1].append(d)
            used += size
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = "_".join(dict.fromkeys(r.matter for r in results))
    for i, docs in enumerate(pkg.attached):
        part = f"-part{i + 1}of{len(pkg.attached)}" if len(pkg.attached) > 1 else ""
        pkg.zips.append(_zip(out_dir / f"{name}-{stamp}{part}.zip", docs))
    if base_url and links_dir and (len(pkg.attached) > 1 or pkg.left_out):
        token = secrets.token_urlsafe(18)  # the link is the only key, so make it unguessable
        full = _zip(links_dir / token / f"{name}-{stamp}.zip", pkg.all)
        pkg.link, pkg.link_bytes = f"{base_url.rstrip('/')}/{token}/{full.name}", full.stat().st_size
    return pkg


def _zip(path: Path, docs: list[Doc]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for d in docs:
            for f in d.files:
                z.write(f, f"{d.folder}/{f.name}" if d.folder else f.name)
    return path


# ---------- the reply: blocks, then text and plain HTML from the same blocks ----------

def _blocks(results: list[Result], pkg: Package, name: str, notes: list[str], answer: str | None,
            reply_to: str = "") -> list[tuple]:
    """("p", text), ("matter", number, title, facts), ("docs", heading, rows, notes),
    ("link", label, url) and ("actions", [(label, request, mailto)]) blocks."""
    out: list[tuple] = [("p", f"Hi {name},")]
    if answer:
        out.append(("p", answer))
    seen = set()
    for r in results:
        if r.error:
            out.append(("p", f"{r.matter}: {r.error}"))
            continue
        if r.matter not in seen:
            seen.add(r.matter)
            out.append(("matter", r.matter, r.info.meta.get("Title", "untitled matter"), _facts(r.info)))
        if r.doc_type:
            out.append(_type_block(r, pkg))
    if pkg.link:
        out.append(("link", f"Download all {len(pkg.all)} ({_mb(pkg.link_bytes)}, link works {LINK_DAYS} days)",
                    pkg.link))
    elif len(pkg.zips) > 1:
        out.append(("p", f"Over Gmail's 25 MB limit, so this is ZIP 1 of {len(pkg.zips)}; "
                         "the rest follow in this thread."))
    out += [("p", n) for n in notes]
    if actions := _actions(results, reply_to):
        out.append(("actions", actions))
    out.append(("p", SIGN))
    return out


def _actions(results: list[Result], reply_to: str) -> list[tuple[str, str, str | None]]:
    """Follow-ups one click away: the next batch, then the matter's other tabs that have files.
    Each is a request the parser already understands, sent as a new email's subject."""
    several = len({r.matter for r in results}) > 1
    asked = {(r.matter, r.doc_type) for r in results}
    found: dict[str, str] = {}
    for r in results:
        if not r.info:
            continue
        tail = f" for {r.matter}"
        if r.doc_type and r.info.more_left and r.info.picked:
            found[f"Next {r.limit} {r.doc_type}" + (tail if several else "")] = f"Send the next {r.limit} {r.doc_type}{tail}"
        for t in DOC_TYPES:
            if r.info.counts.get(t) and (r.matter, t) not in asked:
                found[f"{t} ({r.info.counts[t]})" + (tail if several else "")] = f"Send the {t}{tail}"
    return [(label, req, f"mailto:{reply_to}?subject={quote(req)}" if reply_to else None)
            for label, req in list(found.items())[:MAX_ACTIONS]]


def _facts(m: Matter) -> list[str]:
    meta, out = m.meta, []
    if meta.get("Type") or meta.get("Category"):
        out.append(" / ".join(v for v in (meta.get("Type"), meta.get("Category")) if v))
    first = _date(meta.get("Date Received"))
    final = _date(meta.get("Date Final Submissions") or meta.get("Decision Date"))
    if first:
        out.append(f"Filed {first}" + (f", final filing {final}" if final else ""))
    if meta.get("Status"):
        out.append(meta["Status"] + (f": {meta['Outcome']}" if meta.get("Outcome") else ""))
    out.append(f"{sum(m.counts.values())} files: {_counts(m.counts)}")
    return out


def _type_block(r: Result, pkg: Package) -> tuple:
    m = r.info
    have = m.counts.get(r.doc_type, 0)
    if have == 0:
        return ("p", f"{r.doc_type}: none in this matter.")
    got = [d for d in m.picked if d.files]
    start = f", from #{r.skip + 1}" if r.skip else ""
    head = f"{r.doc_type}: {len(got)} of {have}{start}"
    if not m.picked:
        head += " (nothing more to send)" if r.skip else " (none matched)"
    elif any(not _where(d, pkg) for d in got):
        head += ", attached as a ZIP"
    rows = [(d.number, d.title, d.date, _where(d, pkg)) for d in got]
    extra = []
    if m.skipped:
        extra.append(f"Skipped {len(m.skipped)} confidential (the Board only releases a notice): "
                     + ", ".join(d.number for d in m.skipped))
    failed = [d for d in m.picked if not d.files]
    if failed:
        extra.append("Failed to download: " + ", ".join(d.number for d in failed))
    return ("docs", head, rows, extra)


def _where(d: Doc, pkg: Package) -> str:
    if d in (pkg.attached[0] if pkg.attached else []):  # in this email's ZIP
        return ""
    if pkg.link:
        return f" - in the link, {_mb(_size(d))}"
    if d in pkg.left_out:
        return f" - too big to email ({_mb(_size(d))}), get it on the UARB site"
    return " - in a following email"


def compose(results: list[Result], pkg: Package, name: str = "there", notes: list[str] = (),
            answer: str | None = None) -> str:
    return render_text(_blocks(results, pkg, name, notes, answer))


def compose_html(results: list[Result], pkg: Package, name: str = "there", notes: list[str] = (),
                 answer: str | None = None, reply_to: str = "") -> str:
    return render_html(_blocks(results, pkg, name, notes, answer, reply_to))


def render_text(blocks: list[tuple]) -> str:
    lines = []
    for kind, *rest in blocks:
        if kind == "p":
            lines += [rest[0], ""]
        elif kind == "list":
            lines += [rest[0], *(f"- {i}" for i in rest[1]), ""]
        elif kind == "matter":
            lines += [f"{rest[0]}: {rest[1]}", *(f"- {i}" for i in rest[2]), ""]
        elif kind == "docs":
            lines += [rest[0], *(f"- {n} {t} ({d}){w}" for n, t, d, w in rest[1]), *(f"- {e}" for e in rest[2]), ""]
        elif kind == "actions":
            lines += ["For more, reply with:", *(f'- "{req}"' for _, req, _ in rest[0]), ""]
        else:
            lines += [f"{rest[0]}: {rest[1]}", ""]
    return "\n".join(lines).rstrip() + "\n"


# EnerZen palette. Styles are inline: most mail clients drop <style> blocks.
_NAVY, _INK, _PERI, _SOFT, _LINE, _TINT = "#2a3786", "#252d60", "#5862a6", "#687ab8", "#e9ebf5", "#f3f4fa"
_FONT = "Futura,'Century Gothic','Trebuchet MS',Arial,sans-serif"
_EYEBROW = f"font-size:11px;font-weight:600;letter-spacing:.16em;text-transform:uppercase;color:{_PERI}"


def render_html(blocks: list[tuple]) -> str:
    """One card in the EnerZen style, built from tables and inline styles so Gmail and Outlook keep it."""
    parts = []
    for kind, *rest in blocks:
        if kind == "p":
            if rest[0] != SIGN:  # the header and footer carry the name
                parts.append(f'<p style="margin:0 0 14px;font-size:15px;line-height:1.55">{escape(rest[0])}</p>')
        elif kind == "list":
            parts.append(f'<p style="margin:0 0 6px;font-size:15px;font-weight:600">{escape(rest[0])}</p>'
                         '<ul style="margin:0 0 14px;padding-left:20px;font-size:14px;line-height:1.6">'
                         + "".join(f"<li>{escape(i)}</li>" for i in rest[1]) + "</ul>")
        elif kind == "matter":
            number, title, facts = rest
            parts.append(f'<div style="margin:4px 0 18px;padding:16px 18px;background:{_TINT};border:1px solid {_LINE};'
                         f'border-radius:12px"><div style="{_EYEBROW}">{escape(number)}</div>'
                         f'<div style="margin-top:4px;font-size:16px;font-weight:600;color:{_NAVY};line-height:1.35">'
                         f'{escape(title)}</div><div style="margin-top:8px;font-size:13px;color:{_PERI};line-height:1.6">'
                         + "<br>".join(escape(f) for f in facts) + "</div></div>")
        elif kind == "docs":
            head, rows, extra = rest
            cells = "".join(
                f'<tr><td style="padding:8px 12px 8px 0;border-top:1px solid {_LINE};font-family:Consolas,monospace;'
                f'font-size:12px;color:{_PERI};white-space:nowrap;vertical-align:top">{escape(n)}</td>'
                f'<td style="padding:8px 0;border-top:1px solid {_LINE};font-size:14px;vertical-align:top">{escape(t)}'
                + (f'<div style="font-size:12px;color:{_SOFT}">{escape(w.lstrip(" -"))}</div>' if w else "")
                + f'</td><td style="padding:8px 0 8px 12px;border-top:1px solid {_LINE};font-size:12px;color:{_SOFT};'
                f'white-space:nowrap;text-align:right;vertical-align:top">{escape(_date(d) or d)}</td></tr>'
                for n, t, d, w in rows)
            parts.append(f'<div style="margin:18px 0 6px;font-size:15px;font-weight:600;color:{_NAVY}">{escape(head)}</div>'
                         + (f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">'
                            f"{cells}</table>" if rows else "")
                         + "".join(f'<p style="margin:8px 0 0;font-size:12px;color:{_SOFT}">{escape(e)}</p>' for e in extra))
        elif kind == "actions":
            pills = "".join(
                f'<a href="{escape(href)}" style="display:inline-block;margin:0 8px 8px 0;padding:8px 14px;'
                f'border:1px solid #d7dcf0;border-radius:999px;color:{_NAVY};text-decoration:none;font-size:13px;'
                f'font-weight:600">{escape(label)}</a>' if href else
                f'<span style="display:inline-block;margin:0 8px 8px 0;font-size:13px">"{escape(req)}"</span>'
                for label, req, href in rest[0])
            parts.append(f'<div style="margin:22px 0 4px"><div style="{_EYEBROW};margin-bottom:10px">One click for more'
                         f"</div>{pills}</div>")
        else:
            parts.append(f'<p style="margin:18px 0"><a href="{escape(rest[1])}" style="display:inline-block;'
                         f'padding:12px 20px;background:{_NAVY};color:#ffffff;text-decoration:none;border-radius:10px;'
                         f'font-size:14px;font-weight:600">{escape(rest[0])}</a></p>')
    return ('<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f8f9fc">'
            '<table width="100%" cellpadding="0" cellspacing="0" style="background:#f8f9fc"><tr>'
            '<td align="center" style="padding:24px 12px">'
            f'<table width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;'
            f'border:1px solid {_LINE};border-radius:16px;font-family:{_FONT};color:{_INK}">'
            f'<tr><td style="padding:16px 24px;background:{_NAVY};border-radius:16px 16px 0 0;color:#ffffff;'
            f'font-size:12px;font-weight:600;letter-spacing:.18em;text-transform:uppercase">'
            f'{SIGN} <span style="color:#d0b455">&#9679;</span></td></tr>'
            f'<tr><td style="padding:24px">{"".join(parts)}</td></tr>'
            f'<tr><td style="padding:14px 24px;border-top:1px solid {_LINE};font-size:12px;color:{_SOFT}">'
            f'Public documents from the Nova Scotia Utility and Review Board &middot; '
            f'<a href="{URL}" style="color:{_PERI}">Open the UARB database</a></td></tr>'
            "</table></td></tr></table></body></html>")


def message(name: str, *paragraphs: str) -> tuple[str, str]:
    """A reply that is only words (help, errors), as text and HTML."""
    blocks = [("p", f"Hi {name},"), *(("p", p) for p in paragraphs), ("p", SIGN)]
    return render_text(blocks), render_html(blocks)


# ---------- small helpers ----------

def _counts(counts: dict[str, int]) -> str:
    have = [f"{n} {_noun(t, n)}" for t in DOC_TYPES if (n := counts.get(t, 0))]
    none = [t for t in DOC_TYPES if not counts.get(t, 0)]
    if none:
        have.append("no " + (", ".join(none[:-1]) + " or " + none[-1] if len(none) > 1 else none[0]))
    return ", ".join(have[:-1]) + ", and " + have[-1] if len(have) > 1 else have[0]


def _noun(doc_type: str, n: int) -> str:
    return doc_type if n != 1 else _SINGULAR[doc_type]


def _date(s: str | None) -> str | None:
    try:
        d = datetime.strptime((s or "").strip(), "%m/%d/%Y")
    except ValueError:
        return None
    return f"{d:%b} {d.day}, {d.year}"


def _size(d: Doc) -> int:
    return sum(f.stat().st_size for f in d.files)


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"
