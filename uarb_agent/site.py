"""Drives the UARB public documents database (a FileMaker WebDirect app) with Playwright.

WebDirect renders fields as absolutely positioned divs with no stable ids, so fields are
found by their placeholder or label text and values are matched to labels by position.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Error as PWError, Page, TimeoutError as PWTimeout

URL = "https://uarb.novascotia.ca/fmi/webd/UARB15"
DOC_TYPES = ["Exhibits", "Key Documents", "Other Documents", "Transcripts", "Recordings"]

# Every value box on the page, with its position, read in one round trip.
_BOXES = """() => [...document.querySelectorAll('.fm-textarea')].map(e => {
  const r = e.getBoundingClientRect();
  return {x: r.x, y: r.y, text: (e.querySelector('.text')?.innerText || '').trim()};
}).filter(b => b.text && b.y >= 0)"""

_LABELS = """() => [...document.querySelectorAll('.v-label')].map(e => {
  const r = e.getBoundingClientRect();
  return {x: r.x, y: r.y, text: (e.innerText || '').trim()};
}).filter(l => l.text)"""


class MatterNotFound(Exception):
    pass


@dataclass
class Doc:
    number: str
    title: str
    date: str
    ext: str
    security: str
    files: list[Path] = field(default_factory=list)
    folder: str = ""  # path inside the ZIP when one email covers several requests
    error: str | None = None

    @property
    def confidential(self) -> bool:
        # The Board serves only a one-page confidentiality notice for these.
        return self.security.lower().startswith("confidential")


@dataclass
class Matter:
    number: str
    meta: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    docs: list[Doc] = field(default_factory=list)  # every row read from the tab
    picked: list[Doc] = field(default_factory=list)  # the ones it went to download
    skipped: list[Doc] = field(default_factory=list)  # confidential ones it would otherwise have taken
    more_left: bool = False  # more public documents after `picked`


def open_matter(page: Page, matter: str) -> Matter:
    page.goto(URL, wait_until="networkidle", timeout=90_000)
    box = page.locator(".fm-textarea", has=page.locator(".placeholder", has_text="M01234"))
    # The div only becomes editable after WebDirect answers the click; typing earlier is lost
    # and an empty search silently opens M00001. So type, then check it landed.
    for _ in range(3):
        box.click()
        page.wait_for_timeout(700)
        page.keyboard.press("Control+A")
        page.keyboard.type(matter, delay=30)
        if matter in box.inner_text():
            break
    else:
        raise RuntimeError("could not type into the matter box")
    _click_nearest(page, page.locator("button", has_text="Search"), box.bounding_box()["y"])
    try:
        page.locator("button", has_text="Exhibits -").wait_for(timeout=30_000)
    except PWTimeout:
        raise MatterNotFound(matter) from None
    page.wait_for_timeout(1000)
    shown = page.locator("a, .fm-textarea .text").filter(has_text=re.compile(r"^M\d{5}")).first.inner_text().strip()
    if shown != matter:  # a bad number can land on another matter instead of an error
        raise MatterNotFound(matter)
    m = Matter(number=matter)
    for name in DOC_TYPES:
        label = page.locator("button", has_text=f"{name} -").first.inner_text()
        m.counts[name] = int(re.search(r"(\d+)\s*$", label.strip()).group(1))
    return m


def read_metadata(page: Page, m: Matter) -> None:
    """Header fields: each value belongs to the label column it sits under."""
    tabs_y = page.locator("button", has_text="Exhibits -").first.bounding_box()["y"]
    header = [lb for lb in page.evaluate(_LABELS) if 80 < lb["y"] < tabs_y
              and "Database" not in lb["text"] and "Found" not in lb["text"]]
    top = min(lb["y"] for lb in header)
    # The title is itself a label (bold title, grey description), sitting below the label row.
    title = [lb["text"] for lb in header if lb["y"] > top + 20]
    if title:
        m.meta["Title"], _, desc = title[0].partition("\n")
        if desc.strip():
            m.meta["Description"] = " ".join(desc.split())
    labels = sorted((lb for lb in header if lb["y"] <= top + 20), key=lambda lb: lb["x"])
    cols: dict[str, list[dict]] = {lb["text"]: [] for lb in labels}
    for b in page.evaluate(_BOXES):
        if not top < b["y"] < tabs_y:  # only between the label row and the tabs
            continue
        owner = [lb for lb in labels if lb["x"] <= b["x"] + 20]
        if owner:
            cols[owner[-1]["text"]].append(b)
    for label, boxes in cols.items():
        texts = [b["text"] for b in sorted(boxes, key=lambda b: b["y"])]
        names = [n.strip() for n in label.split("\n") if n.strip()]
        if names[0] == "Date Final":
            names = ["Date Final Submissions"]
        if len(names) == 2 and len(texts) == 2:  # stacked pair, e.g. "Matter No / Status"
            m.meta.update(zip(names, texts))
        elif texts:
            m.meta[" / ".join(names)] = "\n".join(texts)


def _read_rows(page: Page) -> list[Doc]:
    """The rows currently rendered: each GO GET IT button and the boxes on its two lines."""
    boxes = page.evaluate(_BOXES)
    docs = []
    for btn in _go_buttons(page).all():
        bb = btn.bounding_box()
        if not bb:
            continue
        row = [b for b in boxes if bb["y"] - 45 < b["y"] < bb["y"] + 15]
        if not row:
            continue
        top = min(b["y"] for b in row)
        upper = sorted((b for b in row if b["y"] < top + 15), key=lambda b: b["x"])
        lower = sorted((b for b in row if b["y"] >= top + 15), key=lambda b: b["x"])
        # Upper line: number, title, extension, date. Lower line: security.
        num = upper[0]["text"]
        date = next((b["text"] for b in upper if re.fullmatch(r"\d\d/\d\d/\d{4}", b["text"])), "")
        ext = next((b["text"] for b in upper if b["text"].startswith(".")), "")
        title = " ".join(b["text"] for b in upper[1:] if b["text"] not in (date, ext))
        sec = lower[0]["text"] if lower else ""
        docs.append(Doc(num, title, date, ext, sec))
    return docs


def list_docs(page: Page, doc_type: str, want: int) -> list[Doc]:
    """Opens a tab and reads rows, scrolling the list until `want` public docs or the end."""
    page.locator("button", has_text=f"{doc_type} -").first.click()
    # Every document tab has GO GET IT buttons; not all have a "Found Count" (Key Documents doesn't).
    _go_buttons(page).first.wait_for(timeout=30_000)
    page.wait_for_timeout(1500)
    seen: dict[str, Doc] = {}
    for _ in range(30):
        before = len(seen)
        for d in _read_rows(page):
            seen.setdefault(d.number, d)
        if sum(not d.confidential for d in seen.values()) >= want or len(seen) == before:
            break
        if not _scroll_grid(page, 600):
            break
    return list(seen.values())


def download(page: Page, doc: Doc, out_dir: Path, attempts: int = 2) -> None:
    """GO GET IT opens a 'Download Files' dialog with one button per file in the document.
    Big files sometimes drop mid-transfer, so each document gets a second try."""
    for attempt in range(attempts):
        doc.files, doc.error = [], None
        try:
            _download_once(page, doc, out_dir)
            return
        except PWError as e:
            doc.error = str(e).splitlines()[0][:200]
        finally:
            close = page.locator(".v-window").filter(has_text="ready for download").get_by_role("button", name="Close")
            if close.count():
                close.first.click()
                page.wait_for_timeout(500)


def _download_once(page: Page, doc: Doc, out_dir: Path) -> None:
    num_box = page.locator(".fm-textarea .text").filter(has_text=re.compile(rf"^\s*{re.escape(doc.number)}\s*$")).first
    _scroll_grid(page, None)
    while not num_box.count() and _scroll_grid(page, 500):
        pass
    num_box.scroll_into_view_if_needed()
    _click_nearest(page, _go_buttons(page), num_box.bounding_box()["y"] + 32)
    dialog = page.locator(".v-window").filter(has_text="ready for download")
    # The file buttons are filled in a moment after the dialog opens.
    dialog.locator(".fm-download-form [role=button]").first.wait_for(timeout=30_000)
    files = dialog.locator(".fm-download-form [role=button]").all()
    for i, btn in enumerate(files):
        with page.expect_download(timeout=180_000) as info:
            btn.click()
        dl = info.value
        part = f" ({i + 1})" if len(files) > 1 else ""
        path = out_dir / (_safe(f"{doc.number} - {doc.title}")[:120] + part + Path(dl.suggested_filename).suffix)
        dl.save_as(path)
        doc.files.append(path)


def fetch(page: Page, matter: str, doc_type: str | None, out_dir: Path, limit: int = 10, log=print,
          skip: int = 0, numbers: list[str] = (), title_words: list[str] = ()) -> Matter:
    """Opens the matter and downloads up to `limit` public documents of `doc_type`, after the
    first `skip`; or only the ones in `numbers`, or whose titles contain one of `title_words`."""
    log(f"Opening {matter}")
    m = open_matter(page, matter)
    log(f"Counts: {m.counts}")
    # Before any tab opens: a scrolled document grid leaves rows behind the header, where they
    # would be read as header values.
    read_metadata(page, m)
    if not doc_type or m.counts.get(doc_type, 0) == 0:  # metadata and counts only
        return m
    narrowed = bool(numbers or title_words)
    m.docs = list_docs(page, doc_type, 10**6 if narrowed else skip + limit)  # narrowed: read every row
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = {n.lower() for n in numbers}
    words = [w.lower() for w in title_words]
    pool = [d for d in m.docs if not d.confidential
            and (not narrowed or d.number.lower() in wanted or any(w in d.title.lower() for w in words))]
    picked = pool[skip:skip + limit]
    if narrowed:
        m.skipped = [d for d in m.docs if d.confidential and (d.number.lower() in wanted or any(w in d.title.lower() for w in words))]
    else:  # the confidential rows among those the window passed over
        last = m.docs.index(picked[-1]) if picked else len(m.docs) - 1
        first = m.docs.index(pool[skip]) if skip < len(pool) else last + 1
        m.skipped = [d for d in m.docs[first if skip else 0:last + 1] if d.confidential]
    m.picked = picked
    m.more_left = len(pool) > skip + len(picked) or (not narrowed and m.counts[doc_type] > len(m.docs))
    for i, doc in enumerate(picked):
        log(f"  [{i + 1}/{len(picked)}] {doc.number} {doc.title}")
        download(page, doc, out_dir)
    return m


def _scroll_grid(page: Page, by: int | None) -> bool:
    """The list is a virtualised Vaadin grid: only rows near the view exist, so scroll its
    scroller. `by=None` goes back to the top. Returns False when it could not move."""
    moved = page.evaluate("""by => {
      const s = document.querySelector('.v-grid-scroller-vertical');
      if (!s) return false;
      const before = s.scrollTop;
      s.scrollTop = by === null ? 0 : before + by;
      return s.scrollTop !== before;
    }""", by)
    page.wait_for_timeout(1200 if moved else 0)
    return moved


def _go_buttons(page: Page):
    return page.locator("button", has_text=re.compile("go get it", re.I))


def _click_nearest(page: Page, buttons, y: float) -> None:
    best = min(buttons.all(), key=lambda b: abs(b.bounding_box()["y"] - y))
    best.click()


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
