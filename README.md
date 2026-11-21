# Owlet

**A private AI study assistant for Moodle that runs entirely on your own computer.**

Owlet logs into Moodle *with you* (in a normal Chrome window you can watch),
collects your courses, assignments, deadlines, and the teacher's files, and puts
everything into neat folders. Then you can chat with a local AI to explain an
assignment, plan your work, or draft a first version of the deliverables.

- **Private.** The AI runs on your machine through [Ollama](https://ollama.com/).
  No cloud AI, no API keys, no accounts, no data sent anywhere.
- **Transparent.** Chrome stays visible, so you see every page Owlet opens.
- **Read-only.** Owlet never submits, uploads, deletes, or changes anything on
  Moodle. You stay in control.
- **Never sees your password.** You log in to Moodle yourself, in Chrome.

> **Use it responsibly.** Owlet is a reading and drafting aid. Follow your
> institution's academic-integrity rules and check whether AI help is allowed in
> your course. Always review anything it writes.

---

## What it does

| | |
| --- | --- |
| **Syncs your Moodle** | After you log in, it reads every course and assignment: status, due date, instructions, and the teacher's attached files. |
| **Shows what's urgent** | Assignments get a badge: red for **overdue** or due within 24 hours, amber for due within 48 hours. |
| **Organises your files** | One folder per course, one per assignment, with the instructions and attachments already inside. |
| **Answers questions** | Ask "What should I do first?", "Explain this assignment", or "Summarise this course". |
| **Drafts assignments** | One click writes a first draft (Word, PDF, or code) plus a checklist of requirements. You review it and submit it yourself. |
| **Reads documents** | PDF, Word, PowerPoint (slides and notes), text, and code files. |

### What it will never do

- Submit an assignment or upload anything to Moodle
- Change grades, your profile, enrolment, or course content
- Bypass login, MFA, CAPTCHA, or any access control
- Run the code it generates, or run shell commands
- Talk to any online AI service
- Accept connections from other computers

---

## Getting started

### What you need

- **Windows 10 or 11**
- **Python 3.11 or newer**: [python.org/downloads](https://www.python.org/downloads/)
- **Google Chrome** (Microsoft Edge works too)
- **Ollama**: [ollama.com](https://ollama.com/)
- About **8 GB of free RAM** for the AI model (a GPU helps but isn't required)

### Install (one time)

Open PowerShell and run:

```powershell
# 1. Download Owlet
git clone https://github.com/sufiankhan-dev/Owlet.git
cd Owlet

# 2. Install the Python packages
python -m pip install -r requirements.txt

# 3. Download the AI model (about 5 GB)
ollama pull qwen3:8b
```

### Start Owlet

```powershell
python main.py
```

Then open **http://127.0.0.1:8765** in your browser.

### First use

1. In **Moodle connection**, enter your school's Moodle address
   (for example `https://moodle.yourschool.edu/`).
2. Click **Connect**. A Chrome window opens.
3. **Log in to Moodle in that Chrome window**, the same way you normally do.
4. Wait a moment. Owlet notices you're logged in and starts syncing your courses
   and assignments automatically.
5. Start chatting, or press **Draft** next to any assignment.

Chrome remembers your login, so next time you usually won't need to log in again.

> **Does your campus show a username/password popup *before* the Moodle page
> even loads?** That's network login (HTTP Basic auth), not Moodle. Open
> **Network login** in the connection panel and enter it there. It is kept in
> memory only and forgotten when you disconnect.

---

## How drafting works

1. Press **Draft** on an assignment, then **Draft with AI**.
2. Owlet reads the instructions and the teacher's files, then writes the
   deliverables into that assignment's folder:
   - essays and reports → `documents/` (Word) or `pdfs/`
   - programming work → `code/` (never run)
   - online-text answers → `code/online_text.md`
3. It also writes `explanations/REVIEW.md`: every requirement and the file that
   covers it, or **MISSING**.
4. When you've checked the files, press **Mark reviewed**. If anything changes
   afterwards, Owlet shows **Changed since review**.

The drafts come from a small local model. Treat them as a starting point, expect
to fix things, and **upload the final files to Moodle yourself.**

---

## Where your files go

Everything stays on your computer, inside the Owlet folder:

```
workspace/
└── Database Systems/                 ← one folder per course
    └── Normalization Exercise/       ← one folder per assignment
        ├── instructions/assignment.md   the assignment text
        ├── attachments/                 files from your teacher
        ├── extracted/                   text pulled from those files
        ├── explanations/REVIEW.md       the draft checklist
        ├── documents/  pdfs/  code/     drafts and generated files
        └── submission/
```

Owlet never silently overwrites a file. If it needs to replace one, it asks you
first, and saying no keeps the original.

**Start fresh:** type `/reset` in the chat. This clears synced data and
everything in `workspace/` and `generated/`, but keeps your Moodle address and
Chrome login.

---

## Common problems

| Problem | Fix |
| --- | --- |
| Ollama shows **offline** | Start the Ollama app (or run `ollama serve`), then check `ollama list`. |
| The model is missing | Run `ollama pull qwen3:8b`. |
| The first answer is very slow | Normal on a CPU: the model is loading. Later answers are faster. |
| Login isn't detected | Finish logging in, open your Moodle dashboard in Chrome, then click **Check login**. |
| "Profile already in use" | Close the other Chrome window Owlet opened, or the other copy of Owlet. |
| Chrome not found | Install Chrome, or set `BROWSER_CHANNEL=msedge` in `.env`. |
| "Address already in use" | Another program uses port 8765. Change `APP_PORT` in `.env`. |
| Courses or assignments look wrong | Your school's Moodle theme may be unusual. Ask Owlet to read the visible page, or open it yourself. |

**About speed:** on a CPU, `qwen3:8b` writes around 5 words per second, and the
first reply after starting can take 1–2 minutes. With a GPU it's much faster.
Asking for one step at a time helps.

---

## Known limitations

- Moodle sites look different. Owlet handles common Moodle 4.x pages and falls
  back to plain page text when a page is unusual.
- Scanned PDFs (images with no text) can't be read. There's no OCR.
- Old `.doc` and `.ppt` files aren't read; `.docx` and `.pptx` are.
- The AI makes mistakes. Always check its work against the real assignment.
- Built for one person on one computer, not as a shared web server.

---

## For developers

<details>
<summary><b>Configuration (<code>.env</code>)</b></summary>

Everything works without a `.env` file. To change settings, copy
`.env.example` to `.env`.

| Variable | Default | Notes |
| --- | --- | --- |
| `APP_HOST` | `127.0.0.1` | Must be loopback; anything else is refused |
| `APP_PORT` | `8765` | One process serves the API and the dashboard |
| `OLLAMA_HOST` | `http://localhost:11434` | Must be local |
| `OLLAMA_MODEL` | `qwen3:8b` | Other local models work; the app is tuned for qwen3 |
| `OLLAMA_NUM_CTX` | `16384` | Context window |
| `OLLAMA_TEMPERATURE` | `0.2` | Lower is more predictable |
| `OLLAMA_KEEP_ALIVE` | `30m` | Keeps the model loaded between turns |
| `OLLAMA_THINK` | `false` | Thinking mode roughly doubles CPU latency |
| `BROWSER_CHANNEL` | `chrome` | `chrome`, `msedge`, or `chromium` |
| `BROWSER_HEADLESS` | `false` | Must stay `false` for manual login and oversight |
| `BROWSER_TIMEOUT_MS` | `30000` | Page navigation timeout |
| `MOODLE_URL` | *(empty)* | Your Moodle site; or set it in the dashboard |
| `MAX_DOWNLOAD_MB` | `50` | Attachment size cap |
| `MAX_PAGE_CHARS` | `14000` | Page text sent to the model |
| `MAX_TOOL_RESULT_CHARS` | `8000` | Tool result cap |
| `APPROVAL_TIMEOUT_S` | `300` | No answer = denied |
| `MAX_AGENT_ITERATIONS` | `12` | Tool rounds per chat turn |
| `SYNC_ON_CONNECT` | `true` | Sync automatically once login is detected |
| `DUE_SOON_HOURS` | `48` | Amber badge threshold |
| `DUE_URGENT_HOURS` | `24` | Red badge threshold; must not exceed `DUE_SOON_HOURS` |
| `IGNORED_COURSES` | *(empty)* | Comma-separated course URLs to hide everywhere |

`IGNORED_COURSES` takes course page URLs such as
`https://moodle.example.edu/course/view.php?id=123`. A course is matched by host
and course id, so the same id on another Moodle is not hidden.

</details>

<details>
<summary><b>How it works</b></summary>

```
Visible Chrome  <--Playwright-->  BrowserSession  -->  Moodle parsers
      |                                                    |
      | manual login                                       v
      +--------------------> AppState sync  --------->  Tool registry
                                   |                       |
   FastAPI + SSE  <---- events/approvals ----  Agent loop (validated tool calls)
        |                                                  |
        v                                                  v
   Dashboard (127.0.0.1:8765)                    Ollama qwen3:8b (localhost)
        |
        +--> SQLite (courses, assignments, conversations, activity, approvals)
        +--> workspace/ (course material, generated files)
```

- **Stack:** Python, FastAPI, Playwright, Ollama, SQLite. The dashboard is plain
  HTML/CSS/JS in `app/ui/`, served by FastAPI. No Node, no build step.
- **Sync** (`app/sync.py`) is not an AI tool, so the model can't start a scrape.
  It reads `mod/assign/index.php` per course and Moodle's calendar web service
  for exact due times, all through Chrome's own session using in-page `fetch`.
  The visible tab is never navigated. Files are only ever created, so a repeat
  sync downloads nothing it already has. Your own submitted files are never
  fetched.
- **A chat turn** is a bounded loop: the model proposes tool calls, they are
  validated with Pydantic, run against the browser or workspace, and the results
  go back to the model. Overwrites pause for your approval. Answers stream over
  Server-Sent Events and are saved in SQLite. **Stop** works between every step.

</details>

<details>
<summary><b>The 14 AI tools</b></summary>

None of them can write to Moodle.

| Tool | Purpose |
| --- | --- |
| `get_current_page` | Current URL, title, login state |
| `get_page_content` | Visible text, headings, links, activities |
| `navigate_to_url` | Open an allowed read-only Moodle page |
| `list_courses` | Course overview |
| `open_course` | Open a course and read its activities |
| `list_assignments` | Pending assignments with due/status text |
| `open_assignment` | Read details and save them to the workspace |
| `download_attachment` | Save a visible file and extract its text |
| `read_pdf` | Extract PDF text with page limits |
| `read_document` | Read DOCX/PPTX/text/code from the workspace |
| `create_word_document` | Create a `.docx` in `documents/` |
| `create_pdf` | Render a PDF with headless Chrome |
| `create_code_file` | Write a code/text file (never run) |
| `save_assignment_workspace` | Save or refresh instructions and page text |

Navigation is limited to the Moodle host and blocks logout, admin, enrolment,
grading, profile editing, and submission paths.

</details>

<details>
<summary><b>Security model</b></summary>

- **Loopback only.** `APP_HOST` is validated at startup; Host and Origin headers
  are checked on every request.
- **CSRF guard.** Every POST/PUT/PATCH/DELETE needs the header `X-Owlet: 1`.
- **No Moodle password handling.** Login happens in Chrome; the app never sees it.
- **Network login is transient.** The optional HTTP Basic auth pair goes straight
  to Playwright, lives in memory, is cleared on disconnect, and is never logged
  or stored.
- **Read-only Moodle.** Dangerous paths are blocked even if the model asks.
- **Untrusted content.** Page and document text is treated as data, never as
  instructions, so "ignore your rules" text in a PDF does nothing.
- **Approval gate.** Overwrites wait for a click. Timeout or Stop = denied.
- **Path jail.** All writes stay under `workspace/` or `generated/`; traversal,
  absolute paths, and symlinks are rejected.
- **No execution.** Generated code is never run. No shell.
- **Strict CSP and redaction.** `script-src 'self'`, no framing; cookies,
  tokens, and URL query strings are stripped from logs and the UI.

Do not expose port 8765 through a tunnel, reverse proxy, or firewall rule.

</details>

<details>
<summary><b>Project layout</b></summary>

```
Owlet/
├── main.py              # entry point → http://127.0.0.1:8765
├── app/                 # FastAPI routes, state, sync, drafts, security, ui/
├── agent/               # Ollama client, tool-calling loop, prompts
├── browser/             # visible Chrome session, Moodle parsers
├── tools/               # the 14 tools and workspace helpers
├── database/            # SQLite repository
├── tests/               # unit, API, browser, and end-to-end tests
├── workspace/           # your course folders (gitignored)
└── generated/           # files made with no course open (gitignored)
```

Local data: `database/owlet.db`, `config/app_state.json` (Moodle URL only),
`browser/profile/` (Chrome login), `workspace/`, `generated/`, `logs/`.
Stop the app and delete these to wipe everything.

API routes for drafts: `GET /api/assignments/{id}/draft`,
`POST /api/assignments/{id}/draft`, `POST /api/assignments/{id}/review`.
`POST /api/reset` does the same as `/reset`.

</details>

<details>
<summary><b>Running the tests</b></summary>

```powershell
$env:PYTHONIOENCODING="utf-8"

# Fast suite (no browser, no model): 135 passed, 10 skipped
python -m pytest tests -q

# Also run the real-Chrome tests (opens a browser window)
$env:RUN_BROWSER_TESTS="1"; python -m pytest tests -q

# Real Ollama tests (needs qwen3:8b)
$env:RUN_OLLAMA_TESTS="1"; python -m pytest tests/test_ollama.py -q
```

The browser suite includes a full mock-Moodle workflow: sync, PDF download and
read, Word/PDF/code generation, and overwrite denial.

Contributors: please read [AGENTS.md](AGENTS.md) first. It lists the rules every
change must keep (loopback only, local model only, read-only Moodle, and so on).

</details>

---

## Contributing

Issues and pull requests are welcome. Before you start, read
[AGENTS.md](AGENTS.md), run `python -m pytest tests -q`, and keep the safety
rules intact: **local only, read-only Moodle, no passwords, nothing runs
automatically.**
