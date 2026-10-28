from uarb_agent.request import parse


def p(text):
    return parse(text, llm=False)


def test_example_from_brief():
    r = p("Hi Agent, Can you give me Other Documents files from M12205? Thanks!")
    assert r.matters == ["M12205"] and r.doc_types == ["Other Documents"] and r.limit == 10


def test_case_spacing_and_duplicates():
    assert p("exhibits for m 12205 and M-12205 and M12383").matters == ["M12205", "M12383"]


def test_key_vs_other_documents_do_not_bleed():
    assert p("key documents and other docs for M12205").doc_types == ["Key Documents", "Other Documents"]
    assert p("the key docs for M12205").doc_types == ["Key Documents"]


def test_synonyms_and_site_order():
    assert p("audio and the transcript, plus exhibits, M12205").doc_types == ["Exhibits", "Transcripts", "Recordings"]


def test_bad_matter_number():
    r = p("send exhibits for M1220")
    assert r.matters == [] and r.bad_matters == ["M1220"]


def test_count_is_capped_at_ten():
    assert p("just 3 exhibits from M12205").limit == 3
    assert p("five of the transcripts M12205").limit == 5
    assert p("50 exhibits M12205").limit == 10


def test_no_doc_type_without_llm():
    assert p("what is M12205 about?").doc_types == []


def test_all_types():
    assert len(p("all document types for M12205").doc_types) == 5


def test_follow_ups():
    assert p("next 10 please").more and p("can you send the rest?").more
    assert not p("Other Documents from M12205").more
    assert p("send 99761 and H-4(C)-ii from M12205").numbers == ["99761", "H-4(C)-ii"]
    assert p("exhibits for M12205").numbers == []


def test_kinds():
    assert p("Thanks!").kind == "thanks" and p("thank you so much").kind == "thanks"
    assert p("thanks, now send the exhibits").kind == "request"
    assert p("what can you do?").kind == "help"
    assert p("When was the final filing for M12205?").kind == "question"
    assert p("Can you give me Other Documents files from M12205?").kind == "request"


def test_polite_requests_are_not_questions():
    assert p("Actually can you also send me document 99761?").kind == "request"
    assert p("can you send the next ones for M12205?").kind == "request"
