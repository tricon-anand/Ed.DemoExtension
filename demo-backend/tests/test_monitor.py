"""The calls monitor: real runtime data recorded from the agent loop (no mock data anywhere)."""
import json as _json
from types import SimpleNamespace as NS

import httpx

from app import agent as agent_mod

SNAP = [
    {"selector": "[data-testid='revenue']", "tag": "span", "text": "$1,234"},
    {"selector": "[data-testid='user']", "tag": "span", "text": "Bob"},
]


def tool(_tool_name, **inp):
    return NS(type="tool_use", id="t" + _tool_name, name=_tool_name, input=inp)


class FakeClient:
    """Scripted responses carrying realistic usage/model metadata, like a real provider."""
    def __init__(self, script):
        self.script = list(script)
        self.messages = self

    def create(self, **kw):
        content = self.script.pop(0)
        return NS(content=content,
                  stop_reason="tool_use" if any(b.type == "tool_use" for b in content) else "end_turn",
                  usage=NS(input_tokens=100, output_tokens=20),
                  model="test-model")


def mk(client, h):
    return client.post("/scenarios", json={"name": "S", "target_host": "x.test"}, headers=h).json()["id"]


def test_calls_recorded_in_order_with_tokens(client, org_a, monkeypatch):
    fake = FakeClient([
        [tool("propose_rule", selector="#made-up", type="text", value="{{c}}"),   # validation failure
         tool("propose_profile", name="UK", variables={"c": "1", "revenue": "123"})],   # ok
        [tool("propose_rule", selector="[data-testid='user']", type="text", value="{{c}}"),
         tool("propose_rule", selector="[data-testid='revenue']", type="text", value="{{revenue}}"),
         tool("finish", summary="ok")],
    ])
    monkeypatch.setattr(agent_mod, "get_client", lambda: fake)
    sid = mk(client, org_a)
    r = client.post(f"/agent/scenarios/{sid}/build", json={"brief": "x y z", "snapshot": SNAP}, headers=org_a)
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    d = client.get(f"/agent/calls/{run_id}", headers=org_a).json()
    assert d["status"] == "proposed" and d["model"] == "test-model"
    assert d["tokens_in"] == 200 and d["tokens_out"] == 40                       # 2 LLM calls
    assert [c["type"] for c in d["calls"]] == ["llm", "tool", "tool", "llm", "tool", "tool", "tool"]
    llm1 = d["calls"][0]
    assert llm1["model"] == "test-model" and llm1["tokens"]["total"] == 120
    assert llm1["input"]["system"] and llm1["input"]["messages"][0]["role"] == "user"
    assert llm1["input"]["tools"] == ["propose_rule", "propose_profile", "finish"]
    assert llm1["output"]["tool_calls"][0]["name"] == "propose_rule"
    bad = d["calls"][1]
    assert bad["status"] == "error" and bad["llm_call"] == 1                     # linked to its LLM call
    assert bad["output"].startswith("ERROR") and "#made-up" in bad["input"]["selector"]
    assert d["calls"][2]["status"] == "ok"
    assert d["calls"][3]["i"] == 4 and d["calls"][3].get("llm_call") is None
    assert d["calls"][6]["name"] == "finish" and d["calls"][6]["status"] == "ok"

    ov = client.get("/agent/calls", headers=org_a).json()
    assert ov["totals"]["requests"] == 1 and ov["totals"]["total_tokens"] == 240
    assert ov["totals"]["input_tokens"] == 200 and ov["totals"]["failed_calls"] == 1
    row = ov["runs"][0]
    assert row["id"] == run_id and row["tokens_in"] == 200 and row["calls_failed"] == 1


def test_unknown_tool_call_is_recorded_as_failed(client, org_a, monkeypatch):
    fake = FakeClient([[tool("no_such_tool")], []])
    monkeypatch.setattr(agent_mod, "get_client", lambda: fake)
    sid = mk(client, org_a)
    r = client.post(f"/agent/scenarios/{sid}/build", json={"brief": "x y z"}, headers=org_a)
    assert r.status_code == 200
    d = client.get(f"/agent/calls/{r.json()['run_id']}", headers=org_a).json()
    unk = d["calls"][1]
    assert unk["name"] == "no_such_tool" and unk["status"] == "error"
    assert "unknown tool" in unk["output"]


def test_failed_llm_call_is_recorded_and_run_fails(client, org_a, monkeypatch):
    class Boom:
        messages = None

        def create(self, **kw):
            raise RuntimeError("network down")

    b = Boom()
    b.messages = b
    monkeypatch.setattr(agent_mod, "get_client", lambda: b)
    sid = mk(client, org_a)
    r = client.post(f"/agent/scenarios/{sid}/build", json={"brief": "x y z"}, headers=org_a)
    assert r.status_code == 502
    ov = client.get("/agent/calls", headers=org_a).json()
    assert ov["totals"]["requests"] == 1 and ov["totals"]["failed_calls"] == 1
    d = client.get(f"/agent/calls/{ov['runs'][0]['id']}", headers=org_a).json()
    assert d["status"] == "failed"
    assert d["calls"][0]["type"] == "llm" and d["calls"][0]["status"] == "error"
    assert "network down" in d["calls"][0]["error"]


def test_monitor_isolation_and_roles(client, org_a, org_b, make_user, monkeypatch):
    fake = FakeClient([[tool("propose_profile", name="UK", variables={"a": "1"})], []])
    monkeypatch.setattr(agent_mod, "get_client", lambda: fake)
    sid = mk(client, org_a)
    run_id = client.post(f"/agent/scenarios/{sid}/build",
                         json={"brief": "x y z", "snapshot": SNAP}, headers=org_a).json()["run_id"]
    assert client.get("/agent/calls", headers=org_b).json()["totals"]["requests"] == 0
    assert client.get(f"/agent/calls/{run_id}", headers=org_b).status_code == 404
    pres = make_user("p2@test.com", "presenter")
    assert client.get("/agent/calls", headers=pres).status_code == 403


def test_run_without_recorded_calls_returns_legacy_trace(client, org_a):
    """Runs recorded before call-level monitoring still answer with the compact step trace."""
    import app.db as dbmod
    from app import models
    with dbmod.SessionLocal() as db:
        org_id = db.query(models.User).filter_by(email="a@test.com").one().org_id
        run = models.AgentRun(org_id=org_id, scenario_id=0, kind="build", brief="old", status="proposed",
                              trace=[{"step": 1, "stop": "end_turn", "text": "hi", "tools": []}])
        db.add(run)
        db.commit()
        run_id = run.id
    d = client.get(f"/agent/calls/{run_id}", headers=org_a).json()
    assert d["calls"] == [] and d["trace"][0]["step"] == 1 and d["tokens_in"] == 0


def test_groq_client_records_usage_and_retries():
    """The OpenAI-compatible adapter normalizes tokens and reports its retry attempts."""
    calls = []

    def handler(req):
        calls.append(_json.loads(req.content))
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, json={"error": {"message": "rate limit"}})
        return httpx.Response(200, json={"model": "llama-3.3-70b-versatile",
                                         "choices": [{"message": {"content": "ok", "tool_calls": None}}],
                                         "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}})

    c = agent_mod.OpenAICompatClient("k", "https://groq.test/v1",
                                     transport=httpx.MockTransport(handler), sleep=lambda s: None)
    rec = agent_mod.Recorder()
    c.set_recorder(rec)
    call = rec.llm_start({"messages": []})
    resp = c.create(model="m", max_tokens=5, system="s", tools=[], messages=[{"role": "user", "content": "x"}])
    rec.llm_end(call, resp, 5.0)
    assert resp.usage.input_tokens == 11 and resp.usage.output_tokens == 7
    assert resp.model == "llama-3.3-70b-versatile"
    assert rec.calls[0]["tokens"] == {"input": 11, "output": 7, "total": 18}
    assert rec.calls[0]["model"] == "llama-3.3-70b-versatile"
    assert rec.calls[0]["attempts"] == [{"status": 429, "error": "rate limited, waiting 1s", "wait_ms": 1000}]
    assert rec.totals()["total_tokens"] == 18


def test_groq_client_records_broken_tool_call_retries():
    bodies = []

    def handler(req):
        bodies.append(_json.loads(req.content))
        if len(bodies) < 3:
            return httpx.Response(400, json={"error": {"message": "Failed to parse tool call arguments as JSON",
                                                       "code": "tool_use_failed"}})
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "ok", "tool_calls": None}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 1}})

    c = agent_mod.OpenAICompatClient("k", "https://groq.test/v1",
                                     transport=httpx.MockTransport(handler), sleep=lambda s: None)
    rec = agent_mod.Recorder()
    c.set_recorder(rec)
    call = rec.llm_start({"messages": []})
    resp = c.create(model="m", max_tokens=5, system="s", tools=[], messages=[{"role": "user", "content": "x"}])
    rec.llm_end(call, resp, 1.0)
    assert len(bodies) == 3 and rec.calls[0]["status"] == "ok"
    assert [a["status"] for a in rec.calls[0]["attempts"]] == [400, 400]
    assert "tool call" in rec.calls[0]["attempts"][0]["error"]
    assert rec.calls[0]["tokens"] == {"input": 5, "output": 1, "total": 6}
