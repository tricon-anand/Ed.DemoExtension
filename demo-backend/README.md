# Demo Overlay: backend

FastAPI + SQLite. Run from this folder with the virtual environment active:

    pip install -r requirements.txt
    uvicorn app.main:app --port 8001 --reload

* Admin page: http://localhost:8001/admin/
* API docs:   http://localhost:8001/docs
* Tests:      `python -m pytest`  (in-memory database, no API key, no network)

Settings are environment variables: see `.env.example`.

## What the server does

| Area | Endpoints |
|---|---|
| Accounts | `/auth/register` (first organization only), `/auth/login` (rate limited), `/auth/invitations`, `/auth/accept-invite`, `/auth/users`, `/auth/me` |
| Scenarios | `/scenarios`, `/scenarios/{id}/rules`, `/profiles`, `/bundle`, `/preview`, `/export`, `/scenarios/import`, `/scenarios/from-template`, `/templates` |
| Safety net | `/scenarios/{id}/history` (+ restore), `/audit` |
| Insight | `/usage` (sent by the extension), `/analytics` |
| Calls monitor | `/agent/calls` (totals + request list), `/agent/calls/{id}` (full history) — dashboard at **/monitor/** |
| Personal | `/scenarios/{id}/my-profile` (your own default profile) |
| AI assistant | `/agent/status`, `/agent/scenarios/{id}/build` (one-shot) or `/build/stream` (partial results as server-sent events), `/repair`, `/agent/runs`, `/agent/runs/{id}/apply` |

Existing `demo.db` files are upgraded automatically at startup (new columns are added, nothing is deleted).

## Roles

admin: everything, including invitations and deleting scenarios. editor: change scenarios, use the AI assistant, see activity. presenter: use scenarios only.

## The AI assistant, in one paragraph

`app/agent.py` runs a loop: the model calls `propose_rule` / `propose_profile` / `finish`; every call is validated (selector must exist in the page scan, filters must be known, every variable must exist in every profile, api paths must be well formed, and JSON-looking values must actually parse) and errors go back to the model so it fixes itself. The result is stored as a *proposal*. Nothing changes in a scenario until a person presses Apply, and every apply is written to the audit log and the version history, so it can be undone with Restore. List briefs (students, deals, chart points) are handled with whole-list api rules: the profile holds the list as one JSON-array variable and one edit replaces the array wholesale, with a different length if needed.

`POST /agent/scenarios/{id}/build/stream` is the streaming version of `build`: the run starts in a worker thread and the response is a server-sent event stream. Every accepted rule and profile is sent the moment its validation passes (`rule` / `profile` events), plus `step`, `llm` and `tool` events in between (so validation errors and the model's self-corrections are visible live), and a final `done` event with the full proposal and token totals. If the run stops early (step limit, model error), the `done` event still carries everything that was built so far with `complete: false` — five rules that did get created are shown and can be used. Applying such a partial proposal needs `{"allow_incomplete": true}` on `/agent/runs/{id}/apply`; the response then lists `warnings` (e.g. a variable a rule uses that no profile defines — the extension leaves the page's own text for those). The extension popup consumes this stream and paints the rules as they arrive.

## The calls monitor

Every agent request is recorded with real runtime data — no mock data. A `Recorder` in `app/agent.py` captures each **LLM call** (model, exact input payload, output text and tool calls, input/output tokens, duration, HTTP retry attempts such as 429 rate limits) and each **tool call** (name, arguments, result, duration, and which LLM call triggered it), in execution order. Calls whose validation returns "ERROR: …" are marked as failed; the model's fix-and-retry is simply the next call. The dashboard at **http://localhost:8001/monitor/** (same login as the admin page, editors and admins) shows total requests, total tokens, failed calls, and per-request timelines you can expand to inspect inputs, outputs, retries and errors. Data comes from the `calls`, `tokens_in`, `tokens_out`, `calls_failed` and `model` columns on `agent_runs`; the run row is created with status `running` and updated after every call, so in-progress requests are visible live.

## Choosing the AI provider

Set one key in the terminal that starts uvicorn: `ANTHROPIC_API_KEY` (Claude) or `GROQ_API_KEY` (Groq). With both set, Anthropic is used unless `AGENT_PROVIDER=groq`. `AGENT_MODEL` overrides the model (defaults: `claude-sonnet-5-5`, `llama-3.3-70b-versatile`). "Research the prospect on the web" works with Anthropic only. Any other OpenAI-compatible service can be added by pointing `GROQ_BASE_URL` at it.

## Production checklist

1. `APP_ENV=production` and a real `SECRET_KEY` (the server refuses to start otherwise).
2. Serve over HTTPS (a reverse proxy such as Caddy or nginx) and set the extension's API URL to the https address.
3. Move from SQLite to PostgreSQL when more than a handful of people use it (change `DATABASE_URL` in `app/db.py`).
4. Back up the database. History and audit rows grow over time.
5. Login throttling is per server process. With several workers put the limit in the reverse proxy as well.
