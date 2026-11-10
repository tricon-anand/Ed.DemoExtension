"""Phase 4: the AI scenario builder.

An agent is a model that can call tools in a loop and react to their results. Here the tools are
"propose_rule", "propose_profile" and "finish". Each tool VALIDATES what the model proposes and
returns an error message when it is wrong, so the model corrects itself before a person ever sees it.
Nothing is saved to a scenario until a person approves the proposal (see routers/agent.py).
"""
import json
import os
import re

from app.formatting import FILTERS, FILTER_EXPR, PLAIN_EXPR
from app.services import VALID_TYPES

MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "10"))
RESERVED = {"_locale", "_currency"}


class AgentUnavailable(Exception):
    pass


DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "groq": "llama-3.3-70b-versatile"}


def provider() -> str:
    """AGENT_PROVIDER=anthropic|groq. If unset: Anthropic when its key exists, otherwise Groq when its key exists."""
    p = os.getenv("AGENT_PROVIDER", "").lower()
    if p in DEFAULT_MODELS:
        return p
    if os.getenv("ANTHROPIC_API_KEY") or not os.getenv("GROQ_API_KEY"):
        return "anthropic"
    return "groq"


def model_name() -> str:
    return os.getenv("AGENT_MODEL") or DEFAULT_MODELS[provider()]


def supports_research() -> bool:
    return provider() == "anthropic"          # web search is a built-in Anthropic tool


class OpenAICompatClient:
    """Lets the agent loop talk to any OpenAI-compatible chat API (Groq, and others) while the loop
    keeps using the Anthropic message shape: this class converts both directions."""

    def __init__(self, api_key, base_url, timeout=90, transport=None, sleep=None):
        import time
        self._sleep = sleep or time.sleep
        import httpx
        self.http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport,
                                 headers={"Authorization": "Bearer " + api_key})
        self.messages = self

    @staticmethod
    def _convert_messages(system, messages):
        out = [{"role": "system", "content": system}]
        for m in messages:
            c = m["content"]
            if isinstance(c, str):
                out.append({"role": m["role"], "content": c})
            elif m["role"] == "assistant":
                text = " ".join(getattr(b, "text", "") for b in c if getattr(b, "type", "") == "text").strip()
                calls = [{"id": b.id, "type": "function",
                          "function": {"name": b.name, "arguments": json.dumps(b.input or {})}}
                         for b in c if getattr(b, "type", "") == "tool_use"]
                msg = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
            else:
                for r in c:
                    out.append({"role": "tool", "tool_call_id": r["tool_use_id"], "content": r["content"]})
        return out

    @staticmethod
    def _tool_failed(r) -> bool:
        try:
            e = r.json()["error"]
        except Exception:
            return False
        return e.get("code") == "tool_use_failed" or "tool call" in str(e.get("message", "")).lower()

    def _post(self, body):
        """One request; waits and repeats on 429 (free plans have small per-minute limits)."""
        r = self.http.post("/chat/completions", json=body)
        for _ in range(3):
            if r.status_code != 429:
                break
            try:
                wait = float(r.headers.get("retry-after", "") or 8)
            except ValueError:
                wait = 8
            self._sleep(min(max(wait, 1), 30))
            r = self.http.post("/chat/completions", json=body)
        return r

    def create(self, model, max_tokens, system, tools, messages, **_):
        from types import SimpleNamespace as NS
        body = {
            "model": model, "max_tokens": max_tokens,
            "messages": self._convert_messages(system, messages),
            "tools": [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                        "parameters": t["input_schema"]}}
                      for t in tools if "input_schema" in t],      # Anthropic server tools (web search) are dropped
            "temperature": 0.3,
        }
        r = self._post(body)
        # The model sometimes writes a broken tool call and the provider refuses it (400 tool_use_failed).
        # Sampling is random, so trying again, more strictly each time, almost always works.
        for attempt in (1, 2):
            if r.status_code != 400 or not self._tool_failed(r):
                break
            body = dict(body, temperature=0.1 if attempt == 1 else 0, parallel_tool_calls=False)
            r = self._post(body)
        if r.status_code >= 400:
            try:
                detail = r.json()["error"]["message"]
            except Exception:
                detail = r.text[:200]
            raise RuntimeError(f"{r.status_code}: {detail}")
        msg = r.json()["choices"][0]["message"]
        blocks = []
        if msg.get("content"):
            blocks.append(NS(type="text", text=msg["content"]))
        for call in msg.get("tool_calls") or []:
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except ValueError:
                args = {}             # malformed arguments: the tool's validation will ask the model to retry
            blocks.append(NS(type="tool_use", id=call["id"], name=call["function"]["name"], input=args))
        return NS(content=blocks, stop_reason="tool_use" if msg.get("tool_calls") else "end_turn")


def get_client():
    """Needs ANTHROPIC_API_KEY or GROQ_API_KEY. Tests replace this function with a fake."""
    if provider() == "groq":
        key = os.getenv("GROQ_API_KEY")
        if not key:
            raise AgentUnavailable("GROQ_API_KEY is not set on the server")
        return OpenAICompatClient(key, os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"))
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise AgentUnavailable("ANTHROPIC_API_KEY is not set on the server")
    import anthropic
    return anthropic.Anthropic()


SYSTEM = """You build sales-demo scenarios for a Chrome extension that replaces text on a live web page.
A scenario is a list of RULES and a list of PROFILES.
- A rule has a CSS selector, a type (text, hide, image, html, api) and a value. The value may use
  {{variable}} placeholders, and formatting filters: {{x | number}}, {{x | currency}}, {{x | percent}}, {{x | date}}.
- A profile is a named persona or country (for example "India") holding the variable values, plus
  _locale (en_US, en_IN, en_GB, de_DE) and _currency (USD, INR, GBP, EUR) whenever a filter is used.
- An "api" rule changes JSON that the page loads. Its selector is a URL pattern with * wildcards and its
  value is a JSON list like [{"path":"kpis.revenue","value":"{{revenue}}"}].
Rules you must follow:
1. Only use selectors from the page snapshot you are given. Never invent a selector. Prefer selectors that use an id or a
   data-/aria- attribute over long chains of :nth-of-type, when the snapshot offers both.
2. All people, patients, students and customers must be INVENTED. Never use real personal data.
3. Raw numbers go in the profile (enrollments = 12000) and the rule formats them ({{enrollments | number}},
   {{revenue | currency}}, {{rate | percent}}).
4. An element that shows only a number must get a value that is ONLY the placeholder, for example {{enrollments | number}}.
   Never add words such as "Enrollments:" to it: the label is a separate element and already on the page.
5. Cover every element in the snapshot that shows a metric, count, date, organisation name or person name that the brief
   implies. The page title and the organisation shown in headers usually need a rule too.
6. Make exactly ONE profile, named after the place or persona in the brief (for example "Texas"), unless the brief asks
   for several. Several profiles must differ in a believable way.
7. Every variable a rule uses must exist in EVERY profile.
8. TARGETED EDITS: when the brief asks to change one specific thing (for example "update enrollments from 23 to 25 in the
   first row", "rename the title"), propose ONLY the rule(s) for exactly that element, using the most specific selector
   from the snapshot, and a profile holding ONLY the new variable(s). Give the variable a name that is unique to that
   element (for example enrollments_row1) so no other element is affected. Never add rules for other rows, columns or
   elements, and never use a placeholder you did not define in the profile.
9. Use propose_rule and propose_profile, then call finish. If a tool returns an error, fix it and retry."""


TARGETED = re.compile(r"\b(update|change|replace|edit|fix|modify|rename)\b|\b(only|just)\b", re.I | re.S)
NUMERIC_TEXT = re.compile(r"^[\s$€£₹+\-]*\d[\d,.\s]*(%|[kKmMbB])?\s*$")


def _is_numeric(text: str) -> bool:
    return bool(NUMERIC_TEXT.match(text or ""))


def _placeholders(value: str):
    for m in FILTER_EXPR.finditer(value or ""):
        yield m.group(1), m.group(2)
    stripped = FILTER_EXPR.sub("", value or "")
    for m in PLAIN_EXPR.finditer(stripped):
        yield m.group(1), None


class Builder:
    """Holds the proposal while the agent works, and validates every tool call."""

    def __init__(self, snapshot, existing_selectors=()):
        self.snapshot = snapshot or []
        self.known = {e["selector"] for e in self.snapshot}
        self.existing = set(existing_selectors)
        self.rules: list[dict] = []
        self.profiles: dict[str, dict] = {}
        self.summary = ""
        self.finished = False
        self.targeted = False           # a narrow edit: no "cover everything" nudge
        self.warned = False             # the "uncovered elements" check is only a nudge, shown once
        self.text_of = {e["selector"]: e.get("text", "") for e in self.snapshot}

    # --- tools ---
    def propose_rule(self, selector="", type="text", value="", reason="", **_):
        selector = (selector or "").strip()
        if not selector:
            return "ERROR: selector is empty"
        if type not in VALID_TYPES:
            return f"ERROR: type must be one of {sorted(VALID_TYPES)}"
        if type == "api":
            try:
                edits = json.loads(value)
                assert isinstance(edits, list) and edits
                assert all(isinstance(e, dict) and isinstance(e.get("path"), str) and "value" in e for e in edits)
            except Exception:
                return 'ERROR: for api rules value must be a JSON list like [{"path":"a.b","value":"..."}]'
        elif self.snapshot and selector not in self.known:
            close = [s for s in sorted(self.known) if any(w and w in s for w in re.split(r"\W+", selector)[:3])][:5]
            return ("ERROR: this selector is not in the page snapshot. Use one of the snapshot selectors"
                    + (f", for example: {close}" if close else "."))
        shows = self.text_of.get(selector, "")
        if type == "text" and _is_numeric(shows) and re.sub(r"\{\{[^}]*\}\}", "", value).strip():
            return (f"ERROR: this element shows only the number '{shows}'. Its value must be ONLY a placeholder such as "
                    "{{name | number}}, with no extra words: the label is a separate element on the page.")
        for name, flt in _placeholders(value):
            if flt and flt not in FILTERS:
                return f"ERROR: unknown filter '{flt}'. Use one of {sorted(FILTERS)}"
        if any(r["selector"] == selector and r["type"] == type for r in self.rules):
            return "ERROR: a rule for this selector and type was already proposed"
        self.rules.append({"selector": selector, "type": type, "value": value, "reason": (reason or "")[:300],
                           "exists": selector in self.existing})
        return f"OK: rule {len(self.rules)} recorded"

    def propose_profile(self, name="", variables=None, **_):
        name = (name or "").strip()
        if not name or not isinstance(variables, dict) or not variables:
            return "ERROR: a profile needs a name and a non-empty variables object"
        bad = [k for k in variables if not re.fullmatch(r"[\w.-]+", str(k))]
        if bad:
            return f"ERROR: invalid variable names {bad}"
        self.profiles[name] = {str(k): str(v) for k, v in variables.items()}
        return f"OK: profile '{name}' recorded with {len(variables)} variables"

    def finish(self, summary="", **_):
        problems = self.problems()
        if problems:
            return "ERROR: not finished. " + " ".join(problems)
        if not self.warned and not self.targeted:
            covered = {r["selector"] for r in self.rules}
            missed = [f"{sel} ('{t[:30]}')" for sel, t in self.text_of.items() if sel not in covered and _is_numeric(t)][:8]
            if missed:
                self.warned = True
                return ("NOT FINISHED YET: these elements show numbers but have no rule: " + "; ".join(missed)
                        + ". Add rules for the ones the demo should change, then call finish again (it will be accepted).")
        self.summary = (summary or "")[:1000]
        self.finished = True
        return "OK: proposal complete"

    def problems(self):
        out = []
        if not self.rules:
            out.append("No rules were proposed.")
        if not self.profiles:
            out.append("No profiles were proposed.")
        used, filtered = set(), False
        for r in self.rules:
            if r["type"] == "api":
                try:
                    vals = [str(e.get("value", "")) for e in json.loads(r["value"])]
                except Exception:
                    vals = []
                text = " ".join(vals)
            else:
                text = r["value"]
            for n, f in _placeholders(text):
                used.add(n)
                filtered = filtered or bool(f)
        for pname, vars_ in self.profiles.items():
            for n in sorted(used):
                v = str(vars_.get(n, "")).replace(",", "")
                plain = any(n == nm and not f for r in self.rules for nm, f in _placeholders(r["value"]))
                if plain and v.isdigit() and int(v) >= 1000:
                    out.append(f"Variable '{n}' ({vars_[n]}) is a big number used without a filter: write {{{{{n} | number}}}} so it shows with separators.")
            missing = sorted(n for n in used if n not in vars_)
            if missing:
                out.append(f"Profile '{pname}' is missing variables {missing}.")
            if filtered and not self.targeted and not (RESERVED <= set(vars_)):   # a targeted edit merges into a profile that already has them
                out.append(f"Profile '{pname}' needs _locale and _currency because rules use formatting filters.")
        return out

    def proposal(self):
        return {"rules": self.rules, "profiles": [{"name": k, "variables": v} for k, v in self.profiles.items()],
                "summary": self.summary, "complete": self.finished}


TOOLS = [
    {"name": "propose_rule", "description": "Propose one replacement rule for the scenario.",
     "input_schema": {"type": "object", "properties": {
         "selector": {"type": "string"}, "type": {"type": "string", "enum": sorted(VALID_TYPES)},
         "value": {"type": "string"}, "reason": {"type": "string"}}, "required": ["selector", "type", "value"]}},
    {"name": "propose_profile", "description": "Propose a profile (persona/country) with all its variable values.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "variables": {"type": "object", "additionalProperties": {"type": "string"}}},
         "required": ["name", "variables"]}},
    {"name": "finish", "description": "Call once when rules and profiles are complete.",
     "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}},
]
WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}


def _text_of(content):
    return " ".join(getattr(b, "text", "") for b in content if getattr(b, "type", "") == "text").strip()


def _loop(client, system, tools, first_message, handlers, done):
    """The agent loop: call the model, run the tools it asks for, give it the results, repeat."""
    messages = [{"role": "user", "content": first_message}]
    trace = []
    for step in range(MAX_STEPS):
        resp = client.messages.create(model=model_name(), max_tokens=4096, system=system, tools=tools, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
        trace.append({"step": step + 1, "stop": resp.stop_reason, "text": _text_of(resp.content)[:300],
                      "tools": [u.name for u in uses]})
        if resp.stop_reason == "pause_turn":          # a server-side tool (web search) is still running
            continue
        if not uses:
            break
        results = []
        for u in uses:
            fn = handlers.get(u.name)
            out = fn(**(u.input or {})) if fn else f"ERROR: unknown tool {u.name}"
            results.append({"type": "tool_result", "tool_use_id": u.id, "content": str(out)})
        messages.append({"role": "user", "content": results})
        if done():
            break
    return trace


def run_builder(client, brief, snapshot=None, existing_selectors=(), research=False, host=""):
    b = Builder(snapshot, existing_selectors)
    b.targeted = bool(TARGETED.search(brief or "")) and len(brief or "") < 400
    prompt = f"Brief from the presenter:\n{brief}\n\nTarget host: {host or '(any)'}\n"
    if b.targeted:
        prompt += "\nThis is a TARGETED edit: propose only the rule(s) and variable(s) needed for what was asked, nothing else.\n"
    if snapshot:
        # small free-plan token limits: send fewer elements to Groq (override with AGENT_MAX_ELEMENTS)
        limit = int(os.getenv("AGENT_MAX_ELEMENTS") or (60 if provider() == "groq" else 150))
        known = {e["selector"] for e in snapshot[:limit]}
        b.known = known
        prompt += "\nPage snapshot (selector | tag | current text):\n" + "\n".join(
            f"{e['selector']} | {e.get('tag', '')} | {e.get('text', '')[:80]}" for e in snapshot[:limit])
    else:
        prompt += "\nNo page snapshot was provided, so rules may only be api rules."
    research = research and supports_research()
    if research:
        prompt += "\n\nResearch the prospect with web search first (public, company-level facts only) and reflect them in the profiles."
    tools = TOOLS + ([WEB_SEARCH] if research else [])
    handlers = {"propose_rule": b.propose_rule, "propose_profile": b.propose_profile, "finish": b.finish}
    trace = _loop(client, SYSTEM, tools, prompt, handlers, lambda: b.finished)
    return b.proposal(), trace


# ---------- selector repair ----------
REPAIR_SYSTEM = """A CSS selector in a demo rule stopped matching because the web app changed.
Pick the replacement from the candidate list that shows the same kind of information as the old rule.
Call choose_selector with a selector copied exactly from the candidates, or with selector "" if none fits."""


def run_repair(client, rule, candidates):
    cands = {c["selector"] for c in candidates}
    picked = {}

    def choose(selector="", reason="", **_):
        if selector and selector not in cands:
            return "ERROR: choose a selector exactly as listed in the candidates, or empty if none fit"
        picked.update(selector=selector, reason=(reason or "")[:300])
        return "OK"

    tool = {"name": "choose_selector", "description": "Report the best replacement selector.",
            "input_schema": {"type": "object", "properties": {"selector": {"type": "string"}, "reason": {"type": "string"}},
                             "required": ["selector", "reason"]}}
    prompt = (f"Broken rule: selector={rule['selector']!r}, type={rule['type']}, value={rule['value']!r}\n\n"
              "Candidates (selector | tag | text):\n" + "\n".join(
                  f"{c['selector']} | {c.get('tag', '')} | {c.get('text', '')[:80]}" for c in candidates[:150]))
    trace = _loop(client, REPAIR_SYSTEM, [tool], prompt, {"choose_selector": choose}, lambda: bool(picked))
    return {"selector": picked.get("selector", ""), "reason": picked.get("reason", "")}, trace
