from pathlib import Path

from uarb_agent.reply import Result, _counts, compose, compose_html, package
from uarb_agent.request import parse
from uarb_agent.site import Doc, Matter

META = {"Title": "Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,275,000",
        "Type": "Water", "Category": "Capital Expenditure Approvals", "Status": "Closed",
        "Date Received": "04/07/2025", "Date Final Submissions": "10/23/2025", "Outcome": "Directions given"}
COUNTS = {"Exhibits": 13, "Key Documents": 5, "Other Documents": 21, "Transcripts": 0, "Recordings": 0}


def test_counts_sentence():
    assert _counts(COUNTS) == "13 Exhibits, 5 Key Documents, 21 Other Documents, and no Transcripts or Recordings"
    assert _counts({"Exhibits": 1}) == "1 Exhibit, and no Key Documents, Other Documents, Transcripts or Recordings"


def _matter(tmp: Path, sizes: list[int]) -> Matter:
    docs = []
    for i, size in enumerate(sizes):
        f = tmp / f"{i}.pdf"
        f.write_bytes(bytes(size))
        docs.append(Doc(str(i), f"Doc {i}", "01/02/2025", ".pdf", "Public", files=[f]))
    secret = Doc("9(C)", "Secret", "01/02/2025", ".pdf", "Confidential")
    return Matter("M12205", META, COUNTS, docs + [secret], picked=docs, skipped=[secret], more_left=True)


def test_jot_notes_carry_the_brief(tmp_path):
    r = Result("M12205", "Other Documents", 10, _matter(tmp_path, [100, 200]))
    pkg = package([r], tmp_path)
    body = compose([r], pkg, name="Paula")
    assert body.startswith("Hi Paula,")
    assert "M12205: Halifax Regional Water Commission" in body
    assert "- Water / Capital Expenditure Approvals" in body
    assert "- Filed Apr 7, 2025, final filing Oct 23, 2025" in body
    assert "- Closed: Directions given" in body
    assert "- 39 files: 13 Exhibits, 5 Key Documents, 21 Other Documents, and no Transcripts or Recordings" in body
    assert "Other Documents: 2 of 21" in body and "- 0 Doc 0 (01/02/2025)" in body
    assert "Skipped 1 confidential" in body and '- "Send the next 10 Other Documents for M12205"' in body
    assert len(pkg.zips) == 1 and not pkg.left_out


def test_oversize_splits_across_emails(tmp_path):
    r = Result("M12205", "Exhibits", 10, _matter(tmp_path, [600, 300, 600, 5000]))
    pkg = package([r], tmp_path, limit=1000)  # compressible zeros, so judge by raw size
    assert [[d.number for d in p] for p in pkg.attached] == [["0", "1"], ["2"]]
    assert [d.number for d in pkg.left_out] == ["3"] and len(pkg.zips) == 2
    body = compose([r], pkg)
    assert "ZIP 1 of 2" in body and "in a following email" in body and "too big to email" in body


def test_two_types_one_summary(tmp_path):
    m = _matter(tmp_path, [100])
    rs = [Result("M12205", "Other Documents", 10, m), Result("M12205", "Transcripts", 10, m)]
    body = compose(rs, package(rs, tmp_path))
    assert body.count("M12205: Halifax") == 1 and "Transcripts: none in this matter." in body


def test_link_when_it_does_not_fit(tmp_path):
    r = Result("M12205", "Exhibits", 10, _matter(tmp_path, [600, 600, 5000]))
    pkg = package([r], tmp_path / "out", limit=1000, links_dir=tmp_path / "links", base_url="https://x.test/")
    assert pkg.link.startswith("https://x.test/") and pkg.link.endswith(".zip")
    assert [d.number for d in pkg.emailed] == ["0"] and len(pkg.all) == 3
    body, html = compose([r], pkg), compose_html([r], pkg)
    assert pkg.link in body and pkg.link in html and "Download all 3" in body
    assert "- 1 Doc 1 (01/02/2025) - in the link" in body
    assert "<script" not in html


def test_html_escapes_site_text(tmp_path):
    m = _matter(tmp_path, [10])
    m.meta = {**META, "Title": "<b>A & B</b>"}
    html = compose_html([Result("M12205", "Exhibits", 10, m)], package([], tmp_path))
    assert "&lt;b&gt;A &amp; B&lt;/b&gt;" in html


def test_answer_and_notes_reach_both_versions(tmp_path):
    r = Result("M12205", "", 10, Matter("M12205", META, COUNTS))
    pkg = package([r], tmp_path)
    for out in (compose([r], pkg, notes=["Pick a type."], answer="It closed on Oct 23."),
                compose_html([r], pkg, notes=["Pick a type."], answer="It closed on Oct 23.")):
        assert "Pick a type." in out and "It closed on Oct 23." in out


def test_more_from_offset(tmp_path):
    m = _matter(tmp_path, [10])
    m.more_left = False
    body = compose([Result("M12205", "Other Documents", 10, m, skip=10)], package([], tmp_path))
    assert "Other Documents: 1 of 21, from #11" in body and "Send the next" not in body


def test_follow_ups_are_one_click_requests(tmp_path):
    r = Result("M12205", "Transcripts", 10, Matter("M12205", META, COUNTS))
    html = compose_html([r], package([r], tmp_path), reply_to="agent+uarb@x.test")
    # No transcripts: offer the tabs that have files, each as an email the parser understands.
    assert 'href="mailto:agent+uarb@x.test?subject=Send%20the%20Exhibits%20for%20M12205"' in html
    assert "Key Documents (5)" in html and "Transcripts (" not in html and "Recordings (" not in html
    assert parse("Send the next 10 Exhibits for M12205", llm=False).more
