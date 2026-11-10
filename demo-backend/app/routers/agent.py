import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import agent as agent_mod
from app import models
from app.auth import get_current_user, require_role
from app.db import get_db
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


def _client():
    try:
        return agent_mod.get_client()
    except agent_mod.AgentUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


def _run(db, user, sc, kind, brief, fn):
    client = _client()
    run = models.AgentRun(org_id=user.org_id, scenario_id=sc.id, user_email=user.email, kind=kind, brief=brief[:4000])
    try:
        proposal, trace = fn(client)
    except HTTPException:
        raise
    except Exception as e:                      # model/network problems must not become a 500 with a stack trace
        run.status, run.proposal, run.trace = "failed", {"error": str(e)[:300]}, []
        db.add(run)
        db.commit()
        raise HTTPException(status_code=502, detail="The AI request failed: " + str(e)[:200])
    run.proposal, run.trace = proposal, trace
    db.add(run)
    audit(db, user, "agent." + kind, brief[:200], sc.id)
    db.commit()
    db.refresh(run)
    return {"run_id": run.id, "status": run.status, "proposal": proposal, "steps": len(trace), "trace": trace}


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
                lambda c: agent_mod.run_builder(c, body.brief, snap, existing, body.research, body.host or sc.target_host))


@router.post("/scenarios/{scenario_id}/repair")
def repair(scenario_id: int, body: RepairIn, db: Session = Depends(get_db), user=Depends(editor)):
    sc = get_org_scenario(db, user, scenario_id)
    rule = get_org_rule(db, user, body.rule_id)
    if rule.scenario_id != sc.id:
        raise HTTPException(status_code=404, detail="Rule not found")
    cands = [e.model_dump() for e in body.candidates]
    info = {"selector": rule.selector, "type": rule.type, "value": rule.value}

    def go(c):
        result, trace = agent_mod.run_repair(c, info, cands)
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


@router.post("/runs/{run_id}/apply")
def apply_run(run_id: int, db: Session = Depends(get_db), user=Depends(editor)):
    """The human approval step: only now does a proposal change the scenario."""
    run = db.query(models.AgentRun).filter_by(id=run_id, org_id=user.org_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "proposed":
        raise HTTPException(status_code=409, detail=f"Run is already {run.status}")
    sc = get_org_scenario(db, user, run.scenario_id)
    p = run.proposal
    added_rules = added_profiles = vars_set = 0
    if run.kind == "repair":
        rule = get_org_rule(db, user, p["rule_id"])
        if not p.get("selector"):
            raise HTTPException(status_code=422, detail="The assistant found no replacement selector")
        rule.selector = p["selector"]
        added_rules = 1
    else:
        if not p.get("complete"):
            raise HTTPException(status_code=422, detail="The proposal is incomplete and cannot be applied")
        have = {(r.selector, r.type) for r in sc.rules}
        for r in p["rules"]:
            if (r["selector"], r["type"]) in have:
                continue
            db.add(models.Rule(scenario_id=sc.id, selector=r["selector"], type=r["type"], value=r["value"],
                               position=len(sc.rules) + added_rules))
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
    return {"ok": True, "rules_added": added_rules, "profiles_added": added_profiles, "variables_set": vars_set, "version": sc.version}
