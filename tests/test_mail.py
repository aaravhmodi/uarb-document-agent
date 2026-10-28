from email.message import EmailMessage

from uarb_agent import mail


def msg(frm="Paula Prado <paula@senpilot.com>", to="itsaaravmodi+uarb@gmail.com", **headers):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, to, "Request"
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content("Hi Agent, Other Documents from M12205 please")
    return m


def test_answers_alias_and_matter_mail(monkeypatch):
    monkeypatch.setattr(mail, "ADDRESS", "itsaaravmodi@gmail.com")
    assert mail.should_answer(msg(), "hello") is None
    assert mail.should_answer(msg(to="itsaaravmodi@gmail.com"), "exhibits for M12205") is None


def test_skips_unrelated_and_automated(monkeypatch):
    monkeypatch.setattr(mail, "ADDRESS", "itsaaravmodi@gmail.com")
    assert mail.should_answer(msg(to="itsaaravmodi@gmail.com"), "re: internship chat") is not None
    assert mail.should_answer(msg(frm="itsaaravmodi@gmail.com"), "M12205") is not None
    assert mail.should_answer(msg(Auto_Submitted="auto-replied"), "M12205") is not None
    assert mail.should_answer(msg(frm="MAILER-DAEMON@google.com"), "M12205") is not None


def test_split_quote_and_name():
    new, quoted = mail.split_quote("Exhibits please\n\nOn Mon, Oct 26, 2026 at 6:00 PM Agent wrote:\n> M12205 is about")
    assert new.strip() == "Exhibits please" and "M12205" in quoted
    assert mail.first_name(msg()) == "Paula"
    assert mail.first_name(msg(frm="paula@senpilot.com")) == "there"


def test_body_text_reads_html_only_mail():
    m = EmailMessage()
    m.set_content("<p>Exhibits for <b>M12205</b></p>", subtype="html")
    assert "M12205" in mail.body_text(m)


def test_process_sends_text_and_html(monkeypatch, tmp_path):
    from uarb_agent.reply import Package
    sent = []
    monkeypatch.setattr(mail, "ADDRESS", "itsaaravmodi@gmail.com")
    monkeypatch.setattr(mail, "DATA", tmp_path)
    monkeypatch.setattr(mail, "handle", lambda *a, **k: ("text", "<p>html</p>", Package()))
    monkeypatch.setattr(mail, "send", lambda *a, **k: sent.append((a, k)))
    from collections import defaultdict
    mail.process(None, 1, msg(), defaultdict(int))
    assert sent and sent[0][0][1] == "text" and sent[0][1]["html"] == "<p>html</p>"


def test_process_apologises_when_handle_breaks(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(mail, "ADDRESS", "itsaaravmodi@gmail.com")
    monkeypatch.setattr(mail, "handle", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(mail, "send", lambda *a, **k: sent.append((a, k)))
    from collections import defaultdict
    mail.process(None, 1, msg(), defaultdict(int))
    assert "Sorry" in sent[0][0][1] and "Sorry" in sent[0][1]["html"]


def test_bad_matter_reply_needs_no_browser(tmp_path):
    from uarb_agent.agent import handle
    text, html, pkg = handle("exhibits for M1220", None, tmp_path, name="Paula")
    assert "M1220" in text and "Hi Paula" in html and not pkg.zips


def _reply(subject, body):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "Paula <p@senpilot.com>", "itsaaravmodi+uarb@gmail.com", subject
    m.set_content(body)
    return m


def test_reply_question_borrows_only_the_matter():
    quoted = "\n\nOn Mon, Oct 26, 2026 at 8:34 PM UARB Document Agent wrote:\n> I downloaded 7 Exhibits"
    text = mail.request_text(_reply("Re: Exhibits for M12205", "When was the final filing?" + quoted))
    assert "M12205" in text and "Exhibits" not in text


def test_reply_new_type_and_new_matter():
    text = mail.request_text(_reply("Re: Exhibits for M12205", "Now the key documents for M12383 please"))
    assert "M12383" in text and "M12205" not in text


def test_first_email_uses_subject():
    assert "Exhibits" in mail.request_text(_reply("Exhibits", "for M12205 please"))


def test_thanks_gets_no_reply(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(mail, "ADDRESS", "itsaaravmodi@gmail.com")
    monkeypatch.setattr(mail, "DATA", tmp_path)
    monkeypatch.setattr(mail, "handle", lambda *a, **k: None)
    monkeypatch.setattr(mail, "send", lambda *a, **k: sent.append(a))
    from collections import defaultdict
    mail.process(None, 1, msg(), defaultdict(int))
    assert sent == []
