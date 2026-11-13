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
