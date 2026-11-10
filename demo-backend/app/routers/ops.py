"""History, audit log, usage analytics, templates, import/export, per-user default profile."""
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user, require_role
from app.db import get_db
from app.routers.scenarios import get_org_scenario
from app.services import audit, clean_dump, replace_content, scenario_dump, touch
from app.templates_lib import TEMPLATES, template_dump, template_list

router = APIRouter(tags=["history, audit, analytics, templates"])
editor = require_role("admin", "editor")


# ---------- version history ----------
@router.get("/scenarios/{scenario_id}/history")
def history(scenario_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    sc = get_org_scenario(db, user, scenario_id)
    rows = (db.query(models.ScenarioSnapshot).filter_by(scenario_id=sc.id)
            .order_by(models.ScenarioSnapshot.id.desc()).all())
    return [{"id": s.id, "version": s.version, "created_by": s.created_by, "note": s.note,
             "created_at": s.created_at.isoformat(), "rules": len(s.data.get("rules", [])),
             "profiles": len(s.data.get("profiles", []))} for s in rows]


@router.post("/scenarios/{scenario_id}/history/{snapshot_id}/restore")
def restore(scenario_id: int, snapshot_id: int, db: Session = Depends(get_db), user=Depends(editor)):
    sc = get_org_scenario(db, user, scenario_id)
    snap = db.query(models.ScenarioSnapshot).filter_by(id=snapshot_id, scenario_id=sc.id).first()
    if snap is None:
        raise HTTPException(status_code=404, detail="Version not found")
    replace_content(db, sc, snap.data)
    sc.name = snap.data.get("name", sc.name)
    sc.target_host = snap.data.get("target_host", sc.target_host)
    touch(db, user, sc, "scenario.restore", f"back to version {snap.version}")
    db.commit()
    return {"ok": True, "version": sc.version}


# ---------- audit log ----------
@router.get("/audit")
def audit_log(scenario_id: int | None = None, limit: int = 100, db: Session = Depends(get_db), user=Depends(editor)):
    q = db.query(models.AuditLog).filter_by(org_id=user.org_id)
    if scenario_id is not None:
        q = q.filter_by(scenario_id=scenario_id)
    rows = q.order_by(models.AuditLog.id.desc()).limit(max(1, min(limit, 500))).all()
    return [{"id": a.id, "user": a.user_email, "scenario_id": a.scenario_id, "action": a.action,
             "detail": a.detail, "at": a.created_at.isoformat()} for a in rows]


# ---------- usage analytics ----------
EVENTS = {"sync", "demo_on", "demo_off", "profile_switch", "page_view"}


class UsageIn(BaseModel):
    event: str = Field(max_length=40)
    scenario_id: int | None = None
    host: str = Field(default="", max_length=255)
    profile: str = Field(default="", max_length=100)


@router.post("/usage", status_code=204)
def track(body: UsageIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if body.event not in EVENTS:
        raise HTTPException(status_code=422, detail="Unknown event")
    if body.scenario_id is not None:
        get_org_scenario(db, user, body.scenario_id)       # org isolation also applies to analytics
    db.add(models.UsageEvent(org_id=user.org_id, user_email=user.email, scenario_id=body.scenario_id,
                             event=body.event, host=body.host, profile=body.profile))
    db.commit()


@router.get("/analytics")
def analytics(days: int = 30, db: Session = Depends(get_db), user=Depends(editor)):
    days = max(1, min(days, 365))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (db.query(models.UsageEvent).filter(models.UsageEvent.org_id == user.org_id,
                                               models.UsageEvent.created_at >= since).all())
    names = {s.id: s.name for s in db.query(models.Scenario).filter_by(org_id=user.org_id)}
    by_scenario = Counter(); by_user = Counter(); by_day = Counter(); by_event = Counter(); by_profile = Counter()
    for e in rows:
        by_event[e.event] += 1
        by_user[e.user_email] += 1
        by_day[e.created_at.date().isoformat()] += 1
        if e.event in ("demo_on", "page_view"):
            by_scenario[names.get(e.scenario_id, "(deleted)")] += 1
        if e.profile and e.event in ("profile_switch", "demo_on"):
            by_profile[e.profile] += 1
    top = lambda c: [{"name": k, "count": v} for k, v in c.most_common(10)]
    return {"days": days, "total": len(rows), "events": dict(by_event), "scenarios": top(by_scenario),
            "users": top(by_user), "profiles": top(by_profile),
            "per_day": [{"day": d, "count": by_day[d]} for d in sorted(by_day)]}


# ---------- templates ----------
class FromTemplateIn(BaseModel):
    key: str
    name: str = ""
    target_host: str = ""


@router.get("/templates")
def templates(user=Depends(get_current_user)):
    return template_list()


@router.post("/scenarios/from-template", status_code=201)
def from_template(body: FromTemplateIn, db: Session = Depends(get_db), user=Depends(editor)):
    if body.key not in TEMPLATES:
        raise HTTPException(status_code=404, detail="Template not found")
    data = template_dump(body.key)
    sc = models.Scenario(org_id=user.org_id, name=(body.name or data["name"]).strip(),
                         target_host=body.target_host.strip(), version=0)
    db.add(sc)
    db.flush()
    replace_content(db, sc, clean_dump(data))
    touch(db, user, sc, "scenario.from_template", body.key)
    db.commit()
    return {"id": sc.id, "name": sc.name, "version": sc.version}


# ---------- export / import ----------
@router.get("/scenarios/{scenario_id}/export")
def export_scenario(scenario_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return scenario_dump(get_org_scenario(db, user, scenario_id))


class ImportIn(BaseModel):
    name: str = ""
    data: dict


@router.post("/scenarios/import", status_code=201)
def import_scenario(body: ImportIn, db: Session = Depends(get_db), user=Depends(editor)):
    try:
        data = clean_dump(body.data)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    sc = models.Scenario(org_id=user.org_id, name=(body.name or body.data.get("name") or "Imported").strip(),
                         target_host=str(body.data.get("target_host", "")), version=0)
    db.add(sc)
    db.flush()
    replace_content(db, sc, data)
    touch(db, user, sc, "scenario.import", sc.name)
    db.commit()
    return {"id": sc.id, "name": sc.name, "version": sc.version}


# ---------- per-user default profile ----------
class MyProfileIn(BaseModel):
    profile: str = Field(default="", max_length=100)     # "" clears the preference


@router.get("/scenarios/{scenario_id}/my-profile")
def get_my_profile(scenario_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    sc = get_org_scenario(db, user, scenario_id)
    pref = db.query(models.UserPref).filter_by(user_id=user.id, scenario_id=sc.id).first()
    return {"profile": pref.profile_name if pref else ""}


@router.put("/scenarios/{scenario_id}/my-profile")
def set_my_profile(scenario_id: int, body: MyProfileIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    sc = get_org_scenario(db, user, scenario_id)
    if body.profile and body.profile not in [p.name for p in sc.profiles]:
        raise HTTPException(status_code=404, detail="Profile not found")
    pref = db.query(models.UserPref).filter_by(user_id=user.id, scenario_id=sc.id).first()
    if not body.profile:
        if pref:
            db.delete(pref)
    elif pref:
        pref.profile_name = body.profile
    else:
        db.add(models.UserPref(user_id=user.id, scenario_id=sc.id, profile_name=body.profile))
    db.commit()
    return {"profile": body.profile}
