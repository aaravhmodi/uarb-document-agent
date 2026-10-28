"""Terminal prototype: uarb "Can you give me Other Documents files from M12205?" """
import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from .agent import handle


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # the Windows console mangles curly quotes otherwise
    ap = argparse.ArgumentParser(description="Ask the UARB agent for documents, as you would by email.")
    ap.add_argument("request", nargs="+", help='e.g. "Other Documents from M12205", then "more M12205"')
    ap.add_argument("--out", type=Path, default=Path("downloads"))
    ap.add_argument("--headed", action="store_true", help="show the browser")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    history_file = a.out / "history.json"  # so "more" continues across runs, as it does by email
    history = json.loads(history_file.read_text()) if history_file.exists() else {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not a.headed)
        reply = handle(" ".join(a.request), browser, a.out, history=history)
        browser.close()
    history_file.write_text(json.dumps(history))
    if reply is None:
        print("(no reply: nothing to do, e.g. a thank-you)")
        return
    body, html, pkg = reply
    print("\n" + "=" * 70 + "\n" + body + "=" * 70)
    preview = a.out / "last-reply.html"
    preview.write_text(html, encoding="utf-8")
    print(f"HTML preview: {preview.resolve()}")
    if pkg.link:
        print(f"Link: {pkg.link}")
    for path in pkg.zips[:1] if pkg.link else pkg.zips:
        print(f"ZIP: {path.resolve()} ({path.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
