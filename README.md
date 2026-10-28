# UARB Document Agent

Hey Senpilot recruiters,

I didn't mention the agentic approach I took to this project in my video submission. I  used
claude code on the terminal with 5 agents working on different aspects of the submission that
I planned out myself with chatgpt as help. Once I had the first version done, I was able to make 
adjustments and a lot of bug fixes to ensure it was usable within the 3 hour limit. These are the 
steps I took to get the best project I could out of the 3 hours.

Try the agent: email **itsaaravmodi+uarb@gmail.com**, for example "Can you send me the exhibits
for M12205?". It replies with the documents, zipped, usually within a few minutes.


## How I worked on it

1. **I split the brief into the parts it listed.** Email in, website navigation, document
   counting, downloading, ZIP and reply. Each part had to work and be tested on its own before I
   moved on, so that if I ran out of time I would still have finished pieces rather than one
   half-built whole.

2. **I used the site by hand before writing any code.** I typed M12205 into the UARB database
   and clicked through it. It is a FileMaker WebDirect app: there are no links per matter, no
   normal form fields, and the page is made of positioned boxes. That ruled out simple scraping
   and decided the approach: Playwright driving a real browser, the way a person would.

3. **I built a terminal version first.** `uarb "exhibits for M12205"` in, a ZIP out. Email only
   came after that worked every time, because a bug is much easier to see in a terminal than in
   an inbox.

4. **I ran it on my own home server instead of the cloud.** I had already turned my PC into a
   server, so the agent runs there in Docker, on a virtual screen. I can watch its browser live
   from my laptop, which made debugging the site much faster: I could see exactly where it got
   stuck.

5. **I tested with real requests and turned every failure into a fix and a test.** The site
   ignored typing that arrived too early, a wrong number opened a different matter, sessions
   expired, big downloads dropped, and confidential documents came back as a one-page notice.
   Each one became a check in the code and a test (33 now).

6. **I solved the files that are too big for email.** Gmail rejects anything over 25 MB, and
   some filings are bigger than that on their own. The agent attaches what fits and sends a
   download link for the rest through a Cloudflare tunnel, so I never had to open a port on my
   home network. Links expire after 7 days.

7. **I used AI where it helps and rules where they are safer.** I wrote and reviewed the code
   with Claude Code to move quickly. Inside the agent, an LLM (gpt-4o-mini) only handles phrasing
   and answering questions; matter numbers are read by plain patterns, and the LLM is never
   allowed to add one. Without an API key, everything still works.

8. **Last, I made the reply something an analyst would want to read.** A short summary of the
   matter, a clean table of the documents sent, the confidential ones it skipped, and one-click
   buttons for the next batch or another document type.


## What it does

You email it a matter number and a document type. It searches the UARB database, reads the
matter's details and document counts, downloads up to 10 public documents, zips them, and
replies in the same thread.

It also handles follow-ups in the thread:

- "Send the rest" continues where the last batch stopped.
- "When was this first filed and is it still open?" gets a short answer from what it read on
  the page, and no files.
- "Just the board orders" filters by title.
- "Send document 99761" or "H-4" fetches specific documents.
- A typo like "M1220" gets a reply explaining the format instead of a wrong search.
- "Thanks!" gets no reply. "What can you do?" gets a short guide.


## How it works

    Gmail ──> read + parse ──> Chromium on the UARB site ──> ZIP + reply ──> Gmail
              (patterns,        (search, count, read          (under 18 MB;
               LLM for           rows, download)               a link for
               phrasing)                                       anything bigger)

| File | What it does |
|---|---|
| `uarb_agent/site.py` | Drives the UARB site: search, tab counts, matter details, document rows, downloads |
| `uarb_agent/request.py` | Turns the email into what to do: matters, types, count, "more", document numbers |
| `uarb_agent/reply.py` | Packs files into ZIPs under Gmail's limit and writes the reply (text and HTML) |
| `uarb_agent/agent.py` | One request in, one reply out, shared by the terminal and the email loop |
| `uarb_agent/mail.py` | The email loop |
| `uarb_agent/links.py` | Serves the big ZIPs behind expiring, unguessable links |
| `uarb_agent/cli.py` | The terminal version |

Things worth knowing about the site and the inbox:

- Every request gets a fresh browser session, because WebDirect sessions expire and a reused one
  resumes wherever the last request left off.
- After typing a matter number, the agent checks the text actually landed, and that the matter
  shown is the one asked for. Otherwise an empty search silently opens M00001.
- The document list only keeps about 12 rows on screen at a time, so the agent scrolls to reach
  the rest and scrolls back to click each one.
- Confidential documents download as a notice instead of the document, so the agent skips them
  and says which ones it skipped.
- The Gmail inbox is shared with another agent of mine, so this one opens it read-only, only
  answers mail sent to the +uarb address or naming a matter, and watches Spam too, where a first
  email from a new sender often lands.
- Every Gmail connection has a timeout, and progress is only saved after a successful check, so
  a dropped connection delays a reply but never loses an email.


## Run it

On your machine:

    uv sync
    uv run playwright install chromium
    uv run --env-file .env uarb "Can you give me Other Documents files from M12205?"
    uv run --env-file .env uarb --headed "exhibits for M12383"    # watch the browser
    uv run pytest

On a server: fill in `.env` from `.env.example`, then `docker compose up -d --build`. The agent's
browser can be watched at `http://<TAILSCALE_IP>:6082/vnc.html`.


## What I'd build next

This challenge is the data-collection side of the Regulatory Agent. The hard part, getting
reliably through the UARB site, is done. Next I would run the same browser code over every
matter on a schedule, store each matter's details, document rows and PDF text in Postgres, and
make it searchable, so the Regulatory Agent can find precedents and approval patterns across
thousands of filings instead of 10 documents per email.

Smaller things I'd fix:

- Give the agent its own Gmail account. The shared one is busy, and Gmail sometimes slows it down.
- Use a fixed Cloudflare address, so download links survive a restart.
- Run a few requests at once instead of one at a time.

Aarav Modi
