"""Turns a free-text email into what to do: which matters, which documents, and what kind of
email it is (a request, a question, a thank-you, a call for help).

Patterns do the reliable parts (matter numbers above all; the LLM never adds one). An LLM, when
configured, fills in what patterns can't: unusual phrasings, title filters, the kind of email.
"""
import json
import os
import re
from dataclasses import dataclass, field

from .site import DOC_TYPES

MAX_DOCS = 10

# Word patterns per tab. Checked in order, and "key"/"other" first so "documents" alone
# never matches both.
_SYNONYMS = {
    "Key Documents": r"key\s*doc(ument)?s?",
    "Other Documents": r"other\s*doc(ument)?s?",
    "Exhibits": r"exhibits?|evidence",
    "Transcripts": r"transcripts?",
    "Recordings": r"recordings?|audio|video|webcasts?",
}
_MATTER = re.compile(r"\bM[\s\-#]?(\d{5})\b", re.I)
_BAD_MATTER = re.compile(r"\bM[\s\-#]?\d{1,4}\b|\bM[\s\-#]?\d{6,}\b", re.I)
_COUNT = re.compile(r"\b(\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:of\s+the\s+)?(?:\w+\s+){0,2}"
                    r"(?:files|docs|documents|exhibits|transcripts|recordings)\b", re.I)
_WORDS = {w: i + 1 for i, w in enumerate("one two three four five six seven eight nine ten".split())}
_MORE = re.compile(r"\b(next|more|remaining|the rest|additional)\b", re.I)
# Document numbers: 5-6 digits (not a matter number) or exhibit style like H-4, H-4(C)-ii.
_DOC_NO = re.compile(r"(?<![M\d])\b(\d{5,6})\b|\b([A-Z]{1,3}-\d{1,3}(?:\([A-Za-z]\))?(?:-[ivx]+)?)(?![\w-])")
_THANKS = re.compile(r"^\W*(thanks|thank you|thx|ty|cheers|great|perfect|awesome|got it|ok(ay)?|appreciate it)\b", re.I)
_HELP = re.compile(r"\b(what can you do|how do(es)? (this|you|it) work|help|instructions)\b", re.I)


@dataclass
class Request:
    matters: list[str] = field(default_factory=list)
    doc_types: list[str] = field(default_factory=list)
    limit: int = MAX_DOCS
    bad_matters: list[str] = field(default_factory=list)
    more: bool = False  # "next 10", "the rest": continue after what was sent before
    numbers: list[str] = field(default_factory=list)  # specific documents asked for
    title_words: list[str] = field(default_factory=list)  # "only the board orders"
    kind: str = "request"  # request | question | thanks | help
    used_llm: bool = False


def parse(text: str, llm: bool = True) -> Request:
    req = Request()
    req.matters = list(dict.fromkeys(f"M{d}" for d in _MATTER.findall(text)))
    req.bad_matters = [m.group(0) for m in _BAD_MATTER.finditer(text)]
    rest = text
    for name, pat in _SYNONYMS.items():
        if re.search(rf"\b({pat})\b", rest, re.I):
            req.doc_types.append(name)
            rest = re.sub(rf"\b({pat})\b", " ", rest, flags=re.I)
    if re.search(r"\ball\s+(the\s+)?(document\s+)?types\b|\bevery\s+type\b", text, re.I):
        req.doc_types = list(DOC_TYPES)
    req.doc_types = [t for t in DOC_TYPES if t in req.doc_types]  # site order
    if c := _COUNT.search(text):
        n = c.group(1).lower()
        req.limit = max(1, min(MAX_DOCS, _WORDS.get(n) or int(n)))
    req.more = bool(_MORE.search(_MATTER.sub(" ", text)))
    req.numbers = list(dict.fromkeys(a or b for a, b in _DOC_NO.findall(_MATTER.sub(" ", text))))
    short = len(text.strip()) < 80
    if short and _THANKS.search(text) and not (req.doc_types or req.more or req.numbers or "?" in text):
        req.kind = "thanks"
    elif _HELP.search(text) and not req.doc_types:
        req.kind = "help"
    elif "?" in text and not (req.doc_types or req.numbers or req.more):
        req.kind = "question"
    if llm and os.getenv("OPENAI_API_KEY"):
        _llm_fill(text, req)
    return req


def _llm_fill(text: str, req: Request) -> None:
    """Fills gaps the patterns left. Never overrides a matter number or a document type found
    by pattern, and only narrows a request with title words when the email clearly asks to."""
    got = _ask_json(
        "A user emails an agent that fetches documents from the Nova Scotia UARB public database. "
        f"Each matter has these tabs: {', '.join(DOC_TYPES)}. Key Documents hold decisions, orders and the "
        "application; Exhibits hold evidence filed by parties; Other Documents hold correspondence and letters; "
        "Transcripts and Recordings are hearing records. Read the email and reply as JSON with: "
        '"kind": "request" (wants documents), "question" (asks something about a matter, maybe also wanting '
        'documents), "thanks" (only thanks or acknowledges, nothing to do), or "help" (asks what the agent can do); '
        '"doc_types": tab names it wants, exact names only, [] if none; '
        '"more": true if it wants the next/remaining documents after ones already sent; '
        '"title_words": words a document title must contain, ONLY if the email restricts to certain documents '
        'by name or topic (e.g. "only the board orders" -> ["order"]), else [].',
        text[:4000])
    if not got:
        return
    req.used_llm = True
    if not req.doc_types:
        req.doc_types = [t for t in DOC_TYPES if t in (got.get("doc_types") or [])]
    req.more = req.more or bool(got.get("more"))
    req.title_words = [w for w in (got.get("title_words") or []) if isinstance(w, str) and w.strip()][:5]
    if got.get("kind") in ("request", "question", "thanks", "help"):
        req.kind = got["kind"]
    if req.doc_types and req.kind == "thanks":  # it named documents: do the work
        req.kind = "request"


def answer_question(text: str, facts: list[dict]) -> str | None:
    """A short answer to a question in the email, from what the agent read on the site only.
    None when there is no question beyond asking for documents, or no LLM."""
    if not os.getenv("OPENAI_API_KEY") or not facts:
        return None
    got = _ask_json(
        "You answer emails about matters in the Nova Scotia UARB public documents database. Use ONLY the "
        "facts given; never guess. Dates are MM/DD/YYYY; 'Date Received' is the initial filing, 'Decision Date' "
        "or 'Date Final Submissions' the final one. 'documents_seen' lists only the rows the agent read, which "
        "can be fewer than the counts. If the facts don't answer the question, say so in one sentence and suggest "
        "which document type to request. You cannot read the contents of documents. If the email only asks for "
        'documents to be sent and asks nothing else, the answer is "". Write dates like "Oct 23, 2025". '
        "Plain text, 1-2 short sentences, no "
        'greeting or sign-off. Reply as JSON {"answer": "..."}.',
        f"Email:\n{text[:4000]}\n\nFacts: {json.dumps(facts)}")
    answer = (got or {}).get("answer", "")
    return answer.strip() if isinstance(answer, str) and answer.strip() else None


def _ask_json(system: str, user: str) -> dict | None:
    from openai import OpenAI

    try:
        out = OpenAI().chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            response_format={"type": "json_object"},
            # Callers bound the email; facts are at most a few hundred rows, so nothing is cut here.
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        got = json.loads(out.choices[0].message.content)
    except Exception:  # noqa: BLE001 - every LLM use has a pattern-only fallback
        return None
    return got if isinstance(got, dict) else None
