"""The AI agent is tested with a scripted fake model: no API key, no network."""
from types import SimpleNamespace as NS

import pytest
from app import agent as agent_mod

SNAP = [
    {"selector": "[data-testid='revenue']", "tag": "span", "text": "$1,234"},
    {"selector": "[data-testid='user']", "tag": "span", "text": "Bob"},
]


def tool(_tool_name, **inp):
    return NS(type="tool_use", id="t" + _tool_name, name=_tool_name, input=inp)


class FakeClient:
    """Plays back scripted responses and records what the agent sent back after each tool call."""
    def __init__(self, script):
        self.script, self.seen = list(script), []
        self.messages = self

    def create(self, **kw):
        self.seen.append([m for m in kw["messages"]])
        content = self.script.pop(0)
        return NS(content=content, stop_reason="tool_use" if any(b.type == "tool_use" for b in content) else "end_turn")


def last_results(fake, n=-1):
    return [r["content"] for r in fake.seen[n][-1]["content"]]


def test_builder_self_corrects_after_validation_errors():
    fake = FakeClient([
        # attempt 1: invented selector + a profile that lacks a variable
        [tool("propose_rule", selector="#made-up", type="text", value="{{company}}"),
         tool("propose_rule", selector="[data-testid='revenue']", type="text", value="{{revenue | currency}}"),
         tool("propose_profile", name="India", variables={"company": "Tata", "_locale": "en_IN", "_currency": "INR"}),
         tool("finish", summary="done")],
        # attempt 2: fix everything
        [tool("propose_profile", name="India", variables={"company": "Tata", "revenue": "1980500", "_locale": "en_IN", "_currency": "INR"}),
         tool("finish", summary="India demo")],
    ])
    proposal, trace = agent_mod.run_builder(fake, "bank demo", SNAP)
    first = last_results(fake, 1)
    assert first[0].startswith("ERROR") and "snapshot" in first[0]       # invented selector rejected
    assert first[1].startswith("OK")
    assert first[3].startswith("ERROR") and "revenue" in first[3]        # finish refused: variable missing
    assert proposal["complete"] and proposal["summary"] == "India demo"
    assert [r["selector"] for r in proposal["rules"]] == ["[data-testid='revenue']"]
    assert len(trace) == 2


def test_builder_rejects_unknown_filter_and_bad_api_rule():
    b = agent_mod.Builder(SNAP)
    assert b.propose_rule("[data-testid='user']", "text", "{{x | shout}}").startswith("ERROR")
    assert b.propose_rule("*/api/x*", "api", "not json").startswith("ERROR")
    assert b.propose_rule("*/api/x*", "api", '[{"path":"a","value":"{{n}}"}]').startswith("OK")
    assert b.propose_rule("x", "script", "").startswith("ERROR")


def test_builder_stops_at_step_limit(monkeypatch):
    monkeypatch.setattr(agent_mod, "MAX_STEPS", 3)
    fake = FakeClient([[tool("propose_profile", name="p", variables={"a": "1"})]] * 5)
    _, trace = agent_mod.run_builder(fake, "x", SNAP)
    assert len(trace) == 3


@pytest.fixture()
def fake_agent(monkeypatch):
    def install(script):
        fake = FakeClient(script)
        monkeypatch.setattr(agent_mod, "get_client", lambda: fake)
        return fake
    return install


def mk(client, h):
    return client.post("/scenarios", json={"name": "S", "target_host": "x.test"}, headers=h).json()["id"]


GOOD = [[tool("propose_rule", selector="[data-testid='user']", type="text", value="{{user}}", reason="name"),
         tool("propose_rule", selector="[data-testid='revenue']", type="text", value="{{rev | currency}}", reason="number"),
         tool("propose_profile", name="UK", variables={"user": "Oliver", "rev": "1980", "_locale": "en_GB", "_currency": "GBP"}),
         tool("finish", summary="ok")]]


def test_unavailable_without_api_key(client, org_a, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = mk(client, org_a)
    assert client.get("/agent/status", headers=org_a).json()["available"] is False
    r = client.post(f"/agent/scenarios/{sid}/build", json={"brief": "bank demo", "snapshot": SNAP}, headers=org_a)
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_proposal_changes_nothing_until_applied(client, org_a, fake_agent):
    fake_agent(GOOD)
    sid = mk(client, org_a)
    r = client.post(f"/agent/scenarios/{sid}/build", json={"brief": "bank demo", "snapshot": SNAP}, headers=org_a)
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["proposal"]["complete"]
    assert client.get(f"/scenarios/{sid}/rules", headers=org_a).json() == []          # not applied yet
    ap = client.post(f"/agent/runs/{run['run_id']}/apply", headers=org_a)
    assert ap.json()["rules_added"] == 2 and ap.json()["profiles_added"] == 1
    assert ap.json()["profiles_touched"] == ["UK"]                                   # the run built for this profile
    assert len(client.get(f"/scenarios/{sid}/rules", headers=org_a).json()) == 2
    assert client.post(f"/agent/runs/{run['run_id']}/apply", headers=org_a).status_code == 409   # only once
    assert "agent.apply" in {a["action"] for a in client.get("/audit", headers=org_a).json()}


def test_ai_rules_apply_to_all_urls(client, org_a, fake_agent):
    fake_agent(GOOD)
    sid = mk(client, org_a)
    run = client.post(f"/agent/scenarios/{sid}/build", json={"brief": "x y z", "snapshot": SNAP}, headers=org_a).json()
    client.post(f"/agent/runs/{run['run_id']}/apply", headers=org_a)
    rules = client.get(f"/scenarios/{sid}/bundle", headers=org_a).json()["rules"]
    assert rules and all(r["host"] == "*" for r in rules)      # "*" = shown on every URL, not just the target host


def test_agent_isolation_and_roles(client, org_a, org_b, make_user, fake_agent):
    fake_agent(GOOD)
    sid = mk(client, org_a)
    assert client.post(f"/agent/scenarios/{sid}/build", json={"brief": "x y z"}, headers=org_b).status_code == 404
    pres = make_user("p@test.com", "presenter")
    assert client.post(f"/agent/scenarios/{sid}/build", json={"brief": "x y z"}, headers=pres).status_code == 403
    run_id = client.post(f"/agent/scenarios/{sid}/build", json={"brief": "x y z", "snapshot": SNAP}, headers=org_a).json()["run_id"]
    assert client.post(f"/agent/runs/{run_id}/apply", headers=org_b).status_code == 404


def test_incomplete_proposal_cannot_be_applied(client, org_a, fake_agent):
    fake_agent([[tool("propose_profile", name="UK", variables={"a": "1"})], []])
    sid = mk(client, org_a)
    run = client.post(f"/agent/scenarios/{sid}/build", json={"brief": "x y z", "snapshot": SNAP}, headers=org_a).json()
    assert run["proposal"]["complete"] is False
    assert client.post(f"/agent/runs/{run['run_id']}/apply", headers=org_a).status_code == 422


def test_model_failure_becomes_502_not_500(client, org_a, monkeypatch):
    class Boom:
        messages = None
        def create(self, **kw): raise RuntimeError("network down")
    b = Boom(); b.messages = b
    monkeypatch.setattr(agent_mod, "get_client", lambda: b)
    sid = mk(client, org_a)
    r = client.post(f"/agent/scenarios/{sid}/build", json={"brief": "x y z"}, headers=org_a)
    assert r.status_code == 502


def test_repair_picks_only_from_candidates(client, org_a, fake_agent):
    sid = mk(client, org_a)
    rid = client.post(f"/scenarios/{sid}/rules", json={"selector": "#old-revenue", "value": "x"}, headers=org_a).json()["id"]
    cands = [{"selector": "#new-revenue", "tag": "span", "text": "$5"}, {"selector": "#nav", "tag": "a", "text": "Home"}]
    fake = fake_agent([[tool("choose_selector", selector="#invented", reason="guess")],
                       [tool("choose_selector", selector="#new-revenue", reason="same figure")]])
    r = client.post(f"/agent/scenarios/{sid}/repair", json={"rule_id": rid, "candidates": cands}, headers=org_a)
    assert r.status_code == 200, r.text
    assert last_results(fake, 1)[0].startswith("ERROR")
    run = r.json()
    assert run["proposal"]["selector"] == "#new-revenue"
    assert client.get(f"/scenarios/{sid}/rules", headers=org_a).json()[0]["selector"] == "#old-revenue"
    client.post(f"/agent/runs/{run['run_id']}/apply", headers=org_a)
    assert client.get(f"/scenarios/{sid}/rules", headers=org_a).json()[0]["selector"] == "#new-revenue"


# ---------- Groq / OpenAI-compatible adapter (HTTP is mocked: no key, no network) ----------
import json as _json
import httpx


def _groq(handler):
    return agent_mod.OpenAICompatClient("k", "https://groq.test/openai/v1", transport=httpx.MockTransport(handler))


def test_groq_adapter_round_trip_and_conversion(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "groq")
    seen = []

    def handler(req):
        body = _json.loads(req.content)
        seen.append(body)
        assert req.headers["authorization"] == "Bearer k"
        if len(seen) == 1:
            msg = {"content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "propose_profile", "arguments": '{"name":"UK","variables":{"user":"Oliver","rev":"1980","_locale":"en_GB","_currency":"GBP"}}'}},
                {"id": "c2", "type": "function", "function": {"name": "propose_rule", "arguments": '{"selector":"[data-testid=\'user\']","type":"text","value":"{{user}}"}'}},
                {"id": "c4", "type": "function", "function": {"name": "propose_rule", "arguments": '{"selector":"[data-testid=\'revenue\']","type":"text","value":"{{rev | currency}}"}'}},
                {"id": "c3", "type": "function", "function": {"name": "finish", "arguments": '{"summary":"ok"}'}}]}
        else:
            msg = {"content": "done", "tool_calls": None}
        return httpx.Response(200, json={"choices": [{"message": msg}]})

    proposal, trace = agent_mod.run_builder(_groq(handler), "bank demo", SNAP, research=True)
    assert proposal["complete"] and proposal["profiles"][0]["name"] == "UK"
    first = seen[0]
    assert first["messages"][0]["role"] == "system"
    assert {t["function"]["name"] for t in first["tools"]} == {"propose_rule", "propose_profile", "finish"}   # no web_search
    assert "Research the prospect" not in first["messages"][1]["content"]                                   # research ignored on Groq
    assert trace[0]["tools"] == ["propose_profile", "propose_rule", "propose_rule", "finish"]
    # the second request carries the assistant tool calls and one 'tool' message per call
    msgs = seen[1]["messages"] if len(seen) > 1 else []
    assert not msgs or [m["role"] for m in msgs[-5:]] == ["assistant", "tool", "tool", "tool", "tool"]


def test_groq_adapter_survives_bad_arguments_and_reports_http_errors():
    def bad_args(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "propose_rule", "arguments": "{not json"}}]}}]})
    resp = _groq(bad_args).create(model="m", max_tokens=10, system="s", tools=[], messages=[{"role": "user", "content": "x"}])
    assert resp.stop_reason == "tool_use" and resp.content[0].input == {}

    def denied(req):
        return httpx.Response(401, json={"error": {"message": "Invalid API Key"}})
    with pytest.raises(RuntimeError, match="Invalid API Key"):
        _groq(denied).create(model="m", max_tokens=10, system="s", tools=[], messages=[{"role": "user", "content": "x"}])


def test_provider_selection(monkeypatch):
    for k in ("AGENT_PROVIDER", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "AGENT_MODEL"):
        monkeypatch.delenv(k, raising=False)
    assert agent_mod.provider() == "anthropic"
    monkeypatch.setenv("GROQ_API_KEY", "g")
    assert agent_mod.provider() == "groq" and agent_mod.model_name() == "llama-3.3-70b-versatile"
    assert isinstance(agent_mod.get_client(), agent_mod.OpenAICompatClient) and not agent_mod.supports_research()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")                       # both set: Anthropic wins unless told otherwise
    assert agent_mod.provider() == "anthropic"
    monkeypatch.setenv("AGENT_PROVIDER", "groq"); monkeypatch.setenv("AGENT_MODEL", "my-model")
    assert agent_mod.provider() == "groq" and agent_mod.model_name() == "my-model"


def test_groq_adapter_waits_and_retries_on_rate_limit():
    calls, waits = [], []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"message": "rate limit"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok", "tool_calls": None}}]})

    c = agent_mod.OpenAICompatClient("k", "https://groq.test/v1", transport=httpx.MockTransport(handler), sleep=waits.append)
    r = c.create(model="m", max_tokens=5, system="s", tools=[], messages=[{"role": "user", "content": "x"}])
    assert r.content[0].text == "ok" and len(calls) == 3 and waits == [2.0, 2.0]

    def always(req):
        return httpx.Response(429, json={"error": {"message": "rate limit reached"}})
    c2 = agent_mod.OpenAICompatClient("k", "https://groq.test/v1", transport=httpx.MockTransport(always), sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="rate limit reached"):
        c2.create(model="m", max_tokens=5, system="s", tools=[], messages=[{"role": "user", "content": "x"}])


def test_groq_sends_fewer_page_elements(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "groq")
    monkeypatch.delenv("AGENT_MAX_ELEMENTS", raising=False)
    big = [{"selector": f"#e{i}", "tag": "span", "text": str(i)} for i in range(200)]
    sent = []

    class Spy:
        messages = None
        def create(self, **kw):
            sent.append(kw["messages"][0]["content"])
            return NS(content=[], stop_reason="end_turn")
    spy = Spy(); spy.messages = spy
    agent_mod.run_builder(spy, "x y z", big)
    assert "#e59 " in sent[0] and "#e60 " not in sent[0]
    # a selector that was cut off is not accepted later either
    b = agent_mod.Builder(big[:60])
    assert b.propose_rule("#e100", "text", "x").startswith("ERROR")


# ---------- quality checks added after a real run ----------
DASH = [
    {"selector": "#district", "tag": "h1", "text": "Demo"},
    {"selector": "#enrol", "tag": "div", "text": "25"},
    {"selector": "#courses", "tag": "div", "text": "2"},
]


def test_number_element_must_get_only_a_placeholder():
    b = agent_mod.Builder(DASH)
    bad = b.propose_rule("#enrol", "text", "Enrollments: {{n | number}}")
    assert bad.startswith("ERROR") and "ONLY a placeholder" in bad
    assert b.propose_rule("#enrol", "text", "{{n | number}}").startswith("OK")
    assert b.propose_rule("#district", "text", "{{district}} ISD").startswith("OK")      # a name may have words around it


def test_big_number_without_filter_is_sent_back():
    b = agent_mod.Builder(DASH)
    b.propose_rule("#enrol", "text", "{{n}}")
    b.propose_rule("#district", "text", "{{d}}")
    b.propose_rule("#courses", "text", "{{c}}")
    b.propose_profile("Texas", {"n": "12000", "d": "Texas ISD", "c": "12"})
    msg = b.finish("x")
    assert msg.startswith("ERROR") and "| number" in msg and "'c'" not in msg      # 12 is fine without a filter


def test_uncovered_numbers_are_flagged_once_then_accepted():
    b = agent_mod.Builder(DASH)
    b.propose_rule("#district", "text", "{{d}}")
    b.propose_profile("Texas", {"d": "Texas ISD"})
    first = b.finish("x")
    assert first.startswith("NOT FINISHED YET") and "#enrol" in first and "#courses" in first
    assert not b.finished
    assert b.finish("x") == "OK: proposal complete" and b.finished         # a deliberate choice is respected


def test_prompt_asks_for_one_profile_and_no_label_prefixes():
    assert "exactly ONE profile" in agent_mod.SYSTEM and "ONLY the placeholder" in agent_mod.SYSTEM


def test_groq_retries_a_broken_tool_call_more_strictly():
    bodies = []

    def handler(req):
        body = _json.loads(req.content)
        bodies.append(body)
        if len(bodies) < 3:
            return httpx.Response(400, json={"error": {"message": "Failed to parse tool call arguments as JSON",
                                                       "type": "invalid_request_error", "code": "tool_use_failed"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok", "tool_calls": None}}]})

    c = agent_mod.OpenAICompatClient("k", "https://groq.test/v1", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    r = c.create(model="m", max_tokens=5, system="s", tools=[], messages=[{"role": "user", "content": "x"}])
    assert r.content[0].text == "ok" and len(bodies) == 3
    assert "parallel_tool_calls" not in bodies[0] and bodies[0]["temperature"] == 0.3
    assert bodies[1]["temperature"] == 0.1 and bodies[2]["temperature"] == 0 and bodies[2]["parallel_tool_calls"] is False


def test_groq_gives_up_after_repeated_tool_failures_and_other_400s_are_not_retried():
    n = []

    def always_bad(req):
        n.append(1)
        return httpx.Response(400, json={"error": {"message": "Failed to parse tool call arguments as JSON", "code": "tool_use_failed"}})
    c = agent_mod.OpenAICompatClient("k", "https://groq.test/v1", transport=httpx.MockTransport(always_bad), sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="Failed to parse tool call"):
        c.create(model="m", max_tokens=5, system="s", tools=[], messages=[{"role": "user", "content": "x"}])
    assert len(n) == 3

    m = []

    def other(req):
        m.append(1)
        return httpx.Response(400, json={"error": {"message": "context length exceeded"}})
    c2 = agent_mod.OpenAICompatClient("k", "https://groq.test/v1", transport=httpx.MockTransport(other), sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="context length"):
        c2.create(model="m", max_tokens=5, system="s", tools=[], messages=[{"role": "user", "content": "x"}])
    assert len(m) == 1


def test_targeted_brief_skips_nudge_and_apply_merges_variables():
    from app import agent
    snap = [{"selector": "#a", "tag": "td", "text": "23"}, {"selector": "#b", "tag": "td", "text": "40"}]
    b = agent.Builder(snap, ())
    b.targeted = True
    b.propose_rule(selector="#a", type="text", value="{{enr_row1 | number}}")
    b.propose_profile(name="Texas", variables={"enr_row1": "25"})
    assert "OK" in b.finish(summary="x")          # no "#b uncovered" nudge
    assert agent.TARGETED.search("update enrollments data from 23 to 25 in first row")
    assert not agent.TARGETED.search("Edmentum admin dashboard for a Texas school district")


def test_apply_merges_variables_into_existing_profile(client, org_a, fake_agent):
    fake_agent(GOOD + [[tool("propose_rule", selector="[data-testid='user']", type="text", value="{{user_row1}}", reason="r"),
                        tool("propose_profile", name="UK", variables={"user_row1": "Zed"}),
                        tool("finish", summary="ok")]])
    sid = mk(client, org_a)
    r1 = client.post(f"/agent/scenarios/{sid}/build", json={"brief": "bank demo", "snapshot": SNAP}, headers=org_a).json()
    client.post(f"/agent/runs/{r1['run_id']}/apply", headers=org_a)
    r2 = client.post(f"/agent/scenarios/{sid}/build", json={"brief": "change user in first row", "snapshot": SNAP}, headers=org_a).json()
    ap = client.post(f"/agent/runs/{r2['run_id']}/apply", headers=org_a).json()
    assert ap["variables_set"] == 1
    assert ap["profiles_touched"] == ["UK"]              # a merged profile is also made active by the extension
    prof = [p for p in client.get(f"/scenarios/{sid}/profiles", headers=org_a).json() if p["name"] == "UK"][0]
    assert prof["variables"]["user_row1"] == "Zed" and prof["variables"]["rev"] == "1980"


# ---------- streaming build: partial results arrive while the agent is still working ----------

def read_stream(client, path, payload, headers):
    """Collects (event, data) pairs from a server-sent event response."""
    events = []
    with client.stream("POST", path, json=payload, headers=headers) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        kind = "message"
        for line in r.iter_lines():
            if line.startswith("event: "):
                kind = line[len("event: "):]
            elif line.startswith("data: "):
                events.append((kind, _json.loads(line[len("data: "):])))
    return events


def test_stream_shows_rules_as_they_are_validated(client, org_a, fake_agent):
    # two rules and a profile, then a duplicate that is rejected live, then the model stops
    # without ever calling finish: the partial proposal is still streamed and still applyable
    fake_agent([[tool("propose_rule", selector="[data-testid='user']", type="text", value="{{user}}", reason="name"),
                 tool("propose_rule", selector="[data-testid='revenue']", type="text", value="{{rev | currency}}", reason="n"),
                 tool("propose_profile", name="UK", variables={"user": "Oliver", "rev": "1980", "_locale": "en_GB", "_currency": "GBP"}),
                 tool("propose_rule", selector="[data-testid='user']", type="text", value="{{user}}")],
                []])
    sid = mk(client, org_a)
    events = read_stream(client, f"/agent/scenarios/{sid}/build/stream", {"brief": "x y z", "snapshot": SNAP}, org_a)
    kinds = [k for k, _ in events]
    assert kinds[0] == "start" and kinds[-1] == "done"
    assert kinds.count("rule") == 2 and "profile" in kinds and "step" in kinds and "llm" in kinds
    # the first rule arrives before its tool call is reported, i.e. truly mid-run
    assert kinds.index("rule") < kinds.index("done") - 1
    assert any(k == "tool" and d["ok"] is False and d["name"] == "propose_rule" for k, d in events)
    done = events[-1][1]
    p = done["proposal"]
    assert p["complete"] is False and len(p["rules"]) == 2 and len(p["profiles"]) == 1
    assert p["problems"] == []                       # the partial proposal is internally consistent
    # a partial proposal needs explicit consent to be applied
    assert client.post(f"/agent/runs/{done['run_id']}/apply", headers=org_a).status_code == 422
    ap = client.post(f"/agent/runs/{done['run_id']}/apply", json={"allow_incomplete": True}, headers=org_a)
    assert ap.status_code == 200, ap.text
    assert ap.json()["rules_added"] == 2 and ap.json()["incomplete"] is True and ap.json()["warnings"] == []
    assert len(client.get(f"/scenarios/{sid}/rules", headers=org_a).json()) == 2
    # (the apply above could only succeed because the worker thread had closed the run as "proposed")


def test_stream_partial_apply_carries_warnings(client, org_a, fake_agent):
    # the model stopped before finishing: the rule uses a variable the profile never got
    fake_agent([[tool("propose_rule", selector="[data-testid='revenue']", type="text", value="{{missing_var}}"),
                 tool("propose_profile", name="UK", variables={"other": "x"})], []])
    sid = mk(client, org_a)
    events = read_stream(client, f"/agent/scenarios/{sid}/build/stream", {"brief": "x y z", "snapshot": SNAP}, org_a)
    done = events[-1][1]
    assert done["proposal"]["complete"] is False
    assert any("missing" in p for p in done["proposal"]["problems"])
    ap = client.post(f"/agent/runs/{done['run_id']}/apply", json={"allow_incomplete": True}, headers=org_a)
    assert ap.status_code == 200, ap.text
    assert any("missing" in w for w in ap.json()["warnings"])


def test_stream_reports_model_failure_as_an_error_event(client, org_a, monkeypatch):
    class Boom:
        messages = None
        def create(self, **kw): raise RuntimeError("network down")
    b = Boom(); b.messages = b
    monkeypatch.setattr(agent_mod, "get_client", lambda: b)
    sid = mk(client, org_a)
    events = read_stream(client, f"/agent/scenarios/{sid}/build/stream", {"brief": "x y z"}, org_a)
    assert [k for k, _ in events][-1] == "error"
    assert "network down" in events[-1][1]["detail"]
    runs = client.get("/agent/runs", headers=org_a).json()
    assert runs[0]["status"] == "failed"             # the run is closed, never left "running"


def test_stream_needs_an_api_key_too(client, org_a, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = mk(client, org_a)
    r = client.post(f"/agent/scenarios/{sid}/build/stream", json={"brief": "x y z"}, headers=org_a)
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_proposal_problems_flags_missing_variables():
    rules = [{"type": "text", "value": "{{a}}"}]
    assert any("missing" in p for p in agent_mod.proposal_problems(rules, {"UK": {}}))
    assert agent_mod.proposal_problems(rules, {"UK": {"a": "1"}}) == []


# ---------- list scenarios: whole-list api rules, JSON variables and their validation ----------

STUDENTS_BRIEF = ("Replace the student list on the dashboard with 8 invented students. The list is served by "
                  "GET /api/students.json and every item looks like "
                  '{"id":"S-1001","name":"Ava Thompson","grade":"B","attendance":0.92}.')


def test_prompt_teaches_list_strategies():
    assert "LISTS AND TABLES" in agent_mod.SYSTEM and "{{students_json}}" in agent_mod.SYSTEM
    assert "students[0].grade" in agent_mod.SYSTEM and "students[*]." in agent_mod.SYSTEM
    assert "different length" in agent_mod.SYSTEM
    assert "DIFFER from the ones already on the page" in agent_mod.SYSTEM          # no near-duplicate names
    assert "*/api/students*" in agent_mod.SYSTEM and "_locale" in agent_mod.SYSTEM


def test_api_rule_paths_and_nested_json_are_validated():
    def rule(value):
        return agent_mod.Builder([]).propose_rule("*/api/students*", "api", value)
    assert rule('[{"path":"students","value":"[{\\"name\\":\\"Asha\\"}]"}]').startswith("OK")     # inline list, valid JSON
    assert rule('[{"path":"students[*].grade","value":"A"}]').startswith("OK")                  # wildcard path
    assert rule('[{"path":"students","value":"{{students_json}}"}]').startswith("OK")           # the whole-variable pattern
    assert rule('[{"path":"students","value":""}]').startswith("OK")                           # an empty value stays allowed
    bad_path = rule('[{"path":"students..name","value":"x"}]')
    assert bad_path.startswith("ERROR") and "students..name" in bad_path and "[*]" in bad_path
    bad_json = rule('[{"path":"students","value":"[{\'name\':\'Asha\'}]"}]')
    assert bad_json.startswith("ERROR") and "'students'" in bad_json and "JSON" in bad_json
    # updating a rule with a new value succeeds so the model can self-correct; identical value is rejected
    b = agent_mod.Builder([])
    assert b.propose_rule("*/api/students*", "api", '[{"path":"students","value":"1"}]').startswith("OK")
    assert b.propose_rule("*/api/students*", "api", '[{"path":"students","value":"1"}]').startswith("ERROR")
    assert b.propose_rule("*/api/students*", "api", '[{"path":"students","value":"2"}]') == "OK: rule 1 updated"


def test_profile_list_variables_must_parse():
    b = agent_mod.Builder([])
    assert b.propose_profile("UK", {"students_json": '[{"name":"Asha","grade":"A"}]'}).startswith("OK")
    bad = agent_mod.Builder([]).propose_profile("UK", {"students_json": "[{'name':'Asha'}]"})
    assert bad.startswith("ERROR") and "students_json" in bad and "JSON array" in bad
    native = agent_mod.Builder([]).propose_profile("UK", {"students_json": [{"name": "Asha"}]})
    assert native.startswith("ERROR") and "students_json" in native        # a real list must be sent as a string
    assert agent_mod.Builder([]).propose_profile("UK", {"note": "Hello {{user}}"}).startswith("OK")


def test_list_proposal_has_no_bogus_warnings_but_catches_missing_vars():
    rules = [{"type": "api", "value": '[{"path":"students","value":"{{students_json}}"},{"path":"customers","value":"{{customers}}"}]'},
             {"type": "text", "value": "{{students_count}}"}]
    profiles = {"Springfield High": {"students_json": '[{"name":"Asha"}]', "students_count": "8", "customers": "5420"}}
    assert agent_mod.proposal_problems(rules, profiles) == []
    assert any("missing" in p for p in agent_mod.proposal_problems(rules, {"Springfield High": {}}))


def test_builder_builds_a_whole_list_scenario():
    students = [{"id": "S-%04d" % i, "name": "Student %d" % i, "grade": "A", "attendance": round(0.9 + i / 100, 2)}
                for i in range(1, 9)]
    fake = FakeClient([
        [tool("propose_rule", selector="*/api/students*", type="api",
              value='[{"path":"students","value":"{{students_json}}"},{"path":"summary.total","value":"{{students_count}}"}]'),
         tool("propose_profile", name="Springfield High",
              variables={"students_json": _json.dumps(students), "students_count": "8"}),
         tool("finish", summary="students demo")],
    ])
    proposal, trace = agent_mod.run_builder(fake, STUDENTS_BRIEF, [])
    assert proposal["complete"] and proposal["problems"] == []
    rule = proposal["rules"][0]
    assert rule["type"] == "api" and rule["selector"] == "*/api/students*"
    edits = _json.loads(rule["value"])
    assert edits[0] == {"path": "students", "value": "{{students_json}}"}
    assert _json.loads(proposal["profiles"][0]["variables"]["students_json"]) == students
    assert len(trace) == 1


# ---------- captured API responses feed the build prompt (Increment 3) ----------

def capture(url, body, method="GET"):
    return {"url": url, "method": method, "body": _json.dumps(body)}


STUDENTS_CAP = capture("http://localhost:8000/api/students.json", {
    "term": "Fall 2025", "total": 8,
    "students": [{"id": "S-1001", "name": "Ava Thompson", "grade": "B", "attendance": 0.92},
                 {"id": "S-1002", "name": "Ben Ortega", "grade": "A", "attendance": 0.97},
                 {"id": "S-1003", "name": "Chen Wei", "grade": "C", "attendance": 0.81}]})
DASH_CAP = capture("http://localhost:8000/api/dashboard.json", {"kpis": {"revenue": 1234}, "monthly": [{"month": "Jan", "revenue": 100}]})


def test_prompt_teaches_captured_api_responses():
    assert "CAPTURED API RESPONSES" in agent_mod.SYSTEM
    assert "Without captures, only write paths the brief actually gives you" in agent_mod.SYSTEM


def test_captured_apis_are_summarized_in_the_prompt():
    sent = []

    class Spy:
        messages = None
        def create(self, **kw):
            sent.append(kw["messages"][0]["content"])
            return NS(content=[], stop_reason="end_turn")
    spy = Spy(); spy.messages = spy
    agent_mod.run_builder(spy, "texas district demo", [], api_caps=[STUDENTS_CAP, DASH_CAP])
    p = sent[0]
    assert "CAPTURED API RESPONSES" in p
    assert "*/api/students.json*" in p and "*/api/dashboard.json*" in p     # ready-to-copy selector patterns
    assert "…+2 more items" in p and "Ava Thompson" in p and "attendance" in p   # structure, not the whole body
    assert '"id":"S-1002"' not in p                                        # one sample item, not the whole list
    sent.clear()
    agent_mod.run_builder(spy, "texas district demo", [])
    assert "CAPTURED API RESPONSES" not in sent[0]                          # nothing captured: prompt unchanged


def test_api_paths_are_nudged_towards_captured_structure():
    def note_for(value, selector="*/api/students*"):
        # a fresh Builder per call: a second api rule for the same selector would be a duplicate
        return agent_mod.Builder([], (), None, [STUDENTS_CAP]).propose_rule(selector, "api", value)

    assert "NOTE" not in note_for('[{"path":"students[*].grade","value":"A"}]')       # a path the capture shows
    whole = note_for('[{"path":"students","value":"{{students_json}}"}]')
    assert whole.startswith("OK") and "NOTE" not in whole                  # wholesale replace of a shown key
    filled = note_for('[{"path":"summary.total","value":"{{n}}"}]')
    assert filled.startswith("OK") and "NOTE" not in filled               # a placeholder is not checkable yet
    scalar = note_for('[{"path":"summary.total","value":"5"}]')
    assert scalar.startswith("OK") and "summary.total" in scalar and "NOTE" in scalar       # nudge, not an error
    unmatched = note_for('[{"path":"x","value":"1"}]', "*/api/other*")
    assert unmatched.startswith("OK") and "no captured response matches" in unmatched
    # a nudged rule is still a valid rule: not flagged as a proposal problem
    assert agent_mod.proposal_problems([{"type": "api", "value": '[{"path":"summary.total","value":"5"}]'}],
                                       {"Texas": {"n": "1"}}) == []


def test_captured_paths_accept_new_keys_and_exact_indexes():
    def note_for(value):
        return agent_mod.Builder([], (), None, [STUDENTS_CAP]).propose_rule("*/api/students*", "api", value)

    # a missing key with a whole-list value is the "highlights" pattern: created on purpose, no nudge
    ok = note_for('[{"path":"highlights","value":"[\\"One district\\"]"}]')
    assert ok.startswith("OK") and "NOTE" not in ok
    ok2 = note_for('[{"path":"highlights","value":"{{highlights_json}}"}]')
    assert ok2.startswith("OK") and "NOTE" not in ok2                     # same, via a profile variable
    assert "NOTE" not in note_for('[{"path":"students[0].grade","value":"A"}]')
    assert "NOTE" not in note_for('[{"path":"term","value":"Spring"}]')
    nudged = note_for('[{"path":"students[9].grade","value":"A"}]')
    assert "NOTE" in nudged and "students[9].grade" in nudged              # beyond the captured 3 items
    assert note_for('[{"path":"GET students","value":"x"}]').startswith("ERROR")


def test_groq_sends_fewer_captured_endpoints(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "groq")
    monkeypatch.delenv("AGENT_MAX_ENDPOINTS", raising=False)
    caps = [capture(f"http://x.test/api/e{i}", {"k": i}) for i in range(20)]
    sent = []

    class Spy:
        messages = None
        def create(self, **kw):
            sent.append(kw["messages"][0]["content"])
            return NS(content=[], stop_reason="end_turn")
    spy = Spy(); spy.messages = spy
    agent_mod.run_builder(spy, "x y z", [], api_caps=caps)
    assert "e7" in sent[0] and "e8" not in sent[0]
    # validation still checks against every capture, even the ones cut from the prompt
    b = agent_mod.Builder([], (), None, caps)
    out = b.propose_rule("*/api/e12*", "api", '[{"path":"k","value":"1"}]')
    assert out.startswith("OK") and "NOTE" not in out


def test_build_accepts_captured_api_json_and_plumbs_it(client, org_a, fake_agent):
    fake = fake_agent(GOOD)
    sid = mk(client, org_a)
    r = client.post(f"/agent/scenarios/{sid}/build", json={
        "brief": "texas district demo", "snapshot": [], "api_json": [STUDENTS_CAP]}, headers=org_a)
    assert r.status_code == 200, r.text
    assert "*/api/students.json*" in fake.seen[0][0]["content"]            # the prompt the model received
    r = client.post(f"/agent/scenarios/{sid}/build/stream", json={
        "brief": "x y z", "api_json": [{"url": "x", "body": "not json"}]}, headers=org_a)
    assert r.status_code == 200                                           # a bad body is just not used for structure
