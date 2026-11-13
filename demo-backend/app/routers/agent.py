import json
import os
import queue
import threading
import time

from copy import deepcopy

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import agent as agent_mod
from app import models
from app.auth import get_current_user, require_role
from app.db import get_db, SessionLocal
from app.routers.rules_profiles import get_org_rule
from app.routers.scenarios import get_org_scenario
from app.services import audit, touch

router = APIRouter(prefix="/agent", tags=["ai assistant"])
editor = require_role("admin", "editor")


class Element(BaseModel):
    selector: str = Field(max_length=500)
    tag: str = Field(default="", max_length=40)
    text: str = Field(default="", max_length=200)


class BuildIn(BaseModel):
    brief: str = Field(min_length=3, max_length=4000)
    snapshot: list[Element] = Field(default_factory=list, max_length=300)
    research: bool = False
    host: str = Field(default="", max_length=255)


class RepairIn(BaseModel):
    rule_id: int
    candidates: list[Element] = Field(min_length=1, max_length=300)


class ApplyIn(BaseModel):
    allow_incomplete: bool = False


def _client():
    try:
        return agent_mod.get_client()
    except agent_mod.AgentUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


def _run(db, user, sc, kind, brief, fn):
    """fn(client, recorder) -> (proposal, trace).

    The run row is created immediately with status 'running' and refreshed after every recorded
    call, so the calls monitor (at /monitor/) can follow a request live, token by token.
    """
    client = _client()
    run = models.AgentRun(org_id=user.org_id, scenario_id=sc.id, user_email=user.email, kind=kind,
                          brief=brief[:4000], status="running", calls=[])
    db.add(run)
    db.commit()
    rec = agent_mod.Recorder()

    def persist():
        """Snapshot the calls recorded so far into the run row; the monitor polls this.
        Deep-copied: SQLAlchemy skips the UPDATE when the assigned value still compares
        equal to the committed one (the recorder mutates its dicts in place)."""
        try:
            t = rec.totals()
            run.calls = deepcopy(rec.calls)
            run.tokens_in, run.tokens_out = t["input_tokens"], t["output_tokens"]
            run.calls_failed = t["failed_calls"]
            run.model = next((c.get("model") or c.get("name") for c in rec.calls if c["type"] == "llm"), "")
            db.commit()
        except Exception:
            db.rollback()                      # monitoring must never break the agent

    rec.on_change = persist
    try:
        proposal, trace = fn(client, rec)
    except HTTPException:
        persist()
        run.status = "failed"
        db.commit()
        raise
    except Exception as e:                      # model/network problems must not become a 500 with a stack trace
        persist()
        run.status, run.proposal = "failed", {"error": str(e)[:300]}
        db.commit()
        raise HTTPException(status_code=502, detail="The AI request failed: " + str(e)[:200])
    persist()
    run.status, run.proposal, run.trace = "proposed", proposal, trace
    audit(db, user, "agent." + kind, brief[:200], sc.id)
    db.commit()
    db.refresh(run)
    return {"run_id": run.id, "status": run.status, "proposal": proposal, "steps": len(trace),
            "trace": trace, "totals": rec.totals()}


@router.get("/status")
def status(user=Depends(get_current_user)):
    try:
        agent_mod.get_client()
        ok = True
    except agent_mod.AgentUnavailable:
        ok = False
    from app import dotenv
    key = os.getenv("GROQ_API_KEY" if agent_mod.provider() == "groq" else "ANTHROPIC_API_KEY") or ""
    return {"key_hint": ("..." + key[-4:]) if len(key) > 8 else "", "env_overrides_file": list(dotenv.SHADOWED),
            "available": ok, "provider": agent_mod.provider(), "model": agent_mod.model_name(), "research": agent_mod.supports_research()}


@router.post("/scenarios/{scenario_id}/build")
def build(scenario_id: int, body: BuildIn, db: Session = Depends(get_db), user=Depends(editor)):
    sc = get_org_scenario(db, user, scenario_id)
    snap = [e.model_dump() for e in body.snapshot]
    existing = [r.selector for r in sc.rules]
    return _run(db, user, sc, "build", body.brief,
                lambda c, rec: agent_mod.run_builder(c, body.brief, snap, existing, body.research,
                                                     body.host or sc.target_host, recorder=rec))


# ---------- streaming build: partial results reach the user while the agent is still working ----------

STREAM_IDLE = 15          # seconds between keep-alives while the model is thinking
STREAM_MAX_WAIT = 1200    # hard cap for one streamed run, so a stuck run cannot hang the client


def _sse(kind: str, payload: dict) -> str:
    return f"event: {kind}\ndata: {json.dumps(payload, default=str)}\n\n"


@router.post("/scenarios/{scenario_id}/build/stream")
def build_stream(scenario_id: int, body: BuildIn, db: Session = Depends(get_db), user=Depends(editor)):
    """Server-sent events version of `build`. The proposal streams out as it is built: every accepted
    rule and profile is sent the moment its validation passes, plus step/llm/tool events in between.
    If the run stops early (step limit, model error), everything proposed so far still arrives in the
    final `done` event with `complete: false`, so a half-built proposal is shown and can be applied."""
    sc = get_org_scenario(db, user, scenario_id)
    snap = [e.model_dump() for e in body.snapshot]
    existing = [r.selector for r in sc.rules]
    client = _client()                    # raises 503 before the stream starts when no API key is set

    run = models.AgentRun(org_id=user.org_id, scenario_id=sc.id, user_email=user.email, kind="build",
                          brief=body.brief[:4000], status="running", calls=[])
    db.add(run)
    db.commit()
    run_id = run.id

    rec = agent_mod.Recorder()
    q: queue.Queue = queue.Queue()

    def emit(kind, payload):
        q.put({"kind": kind, **payload})

    def work():
        """The agent loop in its own thread with its own DB session: a client that walks away
        mid-stream cannot take the run's final state down with the closed request session."""
        try:
            with SessionLocal() as wdb:
                wrun = wdb.get(models.AgentRun, run_id)

                def persist():            # same live persistence as the sync build (the /monitor/ view)
                    try:
                        t = rec.totals()
                        wrun.calls = deepcopy(rec.calls)
                        wrun.tokens_in, wrun.tokens_out = t["input_tokens"], t["output_tokens"]
                        wrun.calls_failed = t["failed_calls"]
                        wrun.model = next((c.get("model") or c.get("name") for c in rec.calls if c["type"] == "llm"), "")
                        wdb.commit()
                    except Exception:
                        wdb.rollback()    # monitoring must never break the agent

                rec.on_change = persist
                try:
                    proposal, trace = agent_mod.run_builder(
                        client, body.brief, snap, existing, body.research, body.host or sc.target_host,
                        recorder=rec, emit=emit)
                except Exception as e:    # model/network problems must not become a stack trace on the stream
                    persist()
                    wrun.status, wrun.proposal = "failed", {"error": str(e)[:300]}
                    wdb.commit()
                    q.put({"kind": "error", "run_id": run_id, "detail": "The AI request failed: " + str(e)[:200]})
                    return
                persist()
                wrun.status, wrun.proposal, wrun.trace = "proposed", proposal, trace
                audit(wdb, user, "agent.build", body.brief[:200], sc.id)
                wdb.commit()
                q.put({"kind": "done", "run_id": run_id, "proposal": proposal,
                       "steps": len(trace), "totals": rec.totals()})
        except Exception as e:            # even a setup failure must end the stream, never hang it
            q.put({"kind": "error", "run_id": run_id, "detail": str(e)[:300]})

    threading.Thread(target=work, daemon=True, name="agent-build-stream").start()

    def gen():
        yield _sse("start", {"run_id": run_id})
        deadline = time.monotonic() + STREAM_MAX_WAIT
        while True:
            try:
                ev = q.get(timeout=STREAM_IDLE)
            except queue.Empty:
                if time.monotonic() > deadline:
                    yield _sse("error", {"run_id": run_id, "detail": "The assistant took too long."})
                    return
                yield ": keep-alive\n\n"  # a comment line: keeps proxies from closing an idle stream
                continue
            if ev["kind"] in ("done", "error"):
                yield _sse(ev["kind"], ev)
                return
            yield _sse(ev["kind"], ev)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/scenarios/{scenario_id}/repair")
def repair(scenario_id: int, body: RepairIn, db: Session = Depends(get_db), user=Depends(editor)):
    sc = get_org_scenario(db, user, scenario_id)
    rule = get_org_rule(db, user, body.rule_id)
    if rule.scenario_id != sc.id:
        raise HTTPException(status_code=404, detail="Rule not found")
    cands = [e.model_dump() for e in body.candidates]
    info = {"selector": rule.selector, "type": rule.type, "value": rule.value}

    def go(c, rec):
        result, trace = agent_mod.run_repair(c, info, cands, recorder=rec)
        return {"rule_id": rule.id, "old_selector": rule.selector, **result}, trace

    return _run(db, user, sc, "repair", f"repair rule {rule.id}", go)


@router.get("/runs")
def runs(scenario_id: int | None = None, db: Session = Depends(get_db), user=Depends(editor)):
    q = db.query(models.AgentRun).filter_by(org_id=user.org_id)
    if scenario_id is not None:
        q = q.filter_by(scenario_id=scenario_id)
    return [{"id": r.id, "scenario_id": r.scenario_id, "kind": r.kind, "status": r.status, "brief": r.brief,
             "user": r.user_email, "created_at": r.created_at.isoformat(), "proposal": r.proposal}
            for r in q.order_by(models.AgentRun.id.desc()).limit(50)]


# ---------- calls monitor: every LLM and tool call, recorded from the real agent loop ----------

@router.get("/calls")
def calls_overview(db: Session = Depends(get_db), user=Depends(editor)):
    """Totals across all requests plus the request list for the dashboard at /monitor/."""
    requests, in_tok, out_tok, failed = (
        db.query(func.count(models.AgentRun.id),
                 func.coalesce(func.sum(models.AgentRun.tokens_in), 0),
                 func.coalesce(func.sum(models.AgentRun.tokens_out), 0),
                 func.coalesce(func.sum(models.AgentRun.calls_failed), 0))
        .filter(models.AgentRun.org_id == user.org_id).first())
    rows = (db.query(models.AgentRun, models.Scenario.name)
            .outerjoin(models.Scenario, models.Scenario.id == models.AgentRun.scenario_id)
            .filter(models.AgentRun.org_id == user.org_id)
            .order_by(models.AgentRun.id.desc()).limit(200))
    return {"totals": {"requests": requests, "input_tokens": in_tok, "output_tokens": out_tok,
                       "total_tokens": in_tok + out_tok, "failed_calls": failed},
            "runs": [{"id": r.id, "kind": r.kind, "status": r.status, "brief": r.brief,
                      "user": r.user_email, "scenario_id": r.scenario_id, "scenario": name,
                      "model": r.model, "tokens_in": r.tokens_in or 0, "tokens_out": r.tokens_out or 0,
                      "calls_failed": r.calls_failed or 0, "created_at": r.created_at.isoformat()}
                     for r, name in rows]}


@router.get("/calls/{run_id}")
def call_detail(run_id: int, db: Session = Depends(get_db), user=Depends(editor)):
    """The complete execution history of one request: every call, in order, with inputs and outputs."""
    r = db.query(models.AgentRun).filter_by(id=run_id, org_id=user.org_id).first()
    if r is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"id": r.id, "kind": r.kind, "status": r.status, "brief": r.brief, "user": r.user_email,
            "scenario_id": r.scenario_id,
            "scenario": db.query(models.Scenario.name).filter_by(id=r.scenario_id).scalar(),
            "model": r.model, "tokens_in": r.tokens_in or 0, "tokens_out": r.tokens_out or 0,
            "calls_failed": r.calls_failed or 0, "created_at": r.created_at.isoformat(),
            "calls": r.calls or [], "trace": r.trace or []}


@router.post("/runs/{run_id}/apply")
def apply_run(run_id: int, body: ApplyIn | None = None, db: Session = Depends(get_db), user=Depends(editor)):
    """The human approval step: only now does a proposal change the scenario.
    A partial proposal (the run stopped before `finish`, e.g. at the step limit) needs
    `{"allow_incomplete": true}`: it is applied as-is and the response carries warnings."""
    run = db.query(models.AgentRun).filter_by(id=run_id, org_id=user.org_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "proposed":
        raise HTTPException(status_code=409, detail=f"Run is already {run.status}")
    sc = get_org_scenario(db, user, run.scenario_id)
    p = run.proposal
    added_rules = added_profiles = vars_set = 0
    warnings: list[str] = []
    if run.kind == "repair":
        rule = get_org_rule(db, user, p["rule_id"])
        if not p.get("selector"):
            raise HTTPException(status_code=422, detail="The assistant found no replacement selector")
        rule.selector = p["selector"]
        added_rules = 1
    else:
        if not p.get("complete"):
            if not (p.get("rules") or p.get("profiles")):
                raise HTTPException(status_code=422, detail="The assistant proposed nothing that can be applied")
            if not (body and body.allow_incomplete):
                raise HTTPException(status_code=422, detail="The proposal is incomplete and cannot be applied")
            profiles = {pr["name"]: (pr.get("variables") or {}) for pr in p.get("profiles") or []}
            warnings = agent_mod.proposal_problems(p.get("rules") or [], profiles)
        have = {(r.selector, r.type) for r in sc.rules}
        for r in p["rules"]:
            if (r["selector"], r["type"]) in have:
                continue
            # host="*" = every URL. An empty host would fall back to the scenario's target host in
            # the bundle, so the rules would only show on the one app the scenario was built for.
            db.add(models.Rule(scenario_id=sc.id, selector=r["selector"], type=r["type"], value=r["value"],
                               host="*", position=len(sc.rules) + added_rules))
            added_rules += 1
        by_name = {x.name: x for x in sc.profiles}
        for pr in p["profiles"]:
            existing = by_name.get(pr["name"])
            if existing is not None:
                merged = dict(existing.variables or {})
                merged.update(pr["variables"])      # keep the other variables, set the proposed ones
                existing.variables = merged
                vars_set += len(pr["variables"])
                continue
            db.add(models.Profile(scenario_id=sc.id, name=pr["name"], variables=pr["variables"]))
            added_profiles += 1
    run.status = "applied"
    touch(db, user, sc, "agent.apply", f"run {run.id}: {added_rules} rules, {added_profiles} profiles, {vars_set} variables")
    db.commit()
    return {"ok": True, "rules_added": added_rules, "profiles_added": added_profiles, "variables_set": vars_set,
            "version": sc.version, "incomplete": run.kind != "repair" and not p.get("complete"),
            "warnings": warnings}
