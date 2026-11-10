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
| Personal | `/scenarios/{id}/my-profile` (your own default profile) |
| AI assistant | `/agent/status`, `/agent/scenarios/{id}/build`, `/repair`, `/agent/runs`, `/agent/runs/{id}/apply` |

Existing `demo.db` files are upgraded automatically at startup (new columns are added, nothing is deleted).

## Roles

admin: everything, including invitations and deleting scenarios. editor: change scenarios, use the AI assistant, see activity. presenter: use scenarios only.

## The AI assistant, in one paragraph

`app/agent.py` runs a loop: the model calls `propose_rule` / `propose_profile` / `finish`; every call is validated (selector must exist in the page scan, filters must be known, every variable must exist in every profile) and errors go back to the model so it fixes itself. The result is stored as a *proposal*. Nothing changes in a scenario until a person presses Apply, and every apply is written to the audit log and the version history, so it can be undone with Restore.

## Choosing the AI provider

Set one key in the terminal that starts uvicorn: `ANTHROPIC_API_KEY` (Claude) or `GROQ_API_KEY` (Groq). With both set, Anthropic is used unless `AGENT_PROVIDER=groq`. `AGENT_MODEL` overrides the model (defaults: `claude-sonnet-5-5`, `llama-3.3-70b-versatile`). "Research the prospect on the web" works with Anthropic only. Any other OpenAI-compatible service can be added by pointing `GROQ_BASE_URL` at it.

## Production checklist

1. `APP_ENV=production` and a real `SECRET_KEY` (the server refuses to start otherwise).
2. Serve over HTTPS (a reverse proxy such as Caddy or nginx) and set the extension's API URL to the https address.
3. Move from SQLite to PostgreSQL when more than a handful of people use it (change `DATABASE_URL` in `app/db.py`).
4. Back up the database. History and audit rows grow over time.
5. Login throttling is per server process. With several workers put the limit in the reverse proxy as well.
