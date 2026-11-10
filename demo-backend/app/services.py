"""Shared helpers: audit log, version history, scenario export/restore."""
from sqlalchemy.orm import Session

from app import models

MAX_SNAPSHOTS = 50


def scenario_dump(sc: models.Scenario) -> dict:
    return {
        "name": sc.name,
        "target_host": sc.target_host,
        "rules": [
            {"selector": r.selector, "type": r.type, "value": r.value, "host": r.host or "", "page": r.page or "",
             "disabled": bool(r.disabled), "position": r.position}
            for r in sorted(sc.rules, key=lambda r: r.position)
        ],
        "profiles": [{"name": p.name, "variables": dict(p.variables or {})} for p in sc.profiles],
    }


def audit(db: Session, user: models.User, action: str, detail: str = "", scenario_id: int | None = None):
    db.add(models.AuditLog(org_id=user.org_id, user_email=user.email, scenario_id=scenario_id,
                           action=action, detail=detail[:500]))


def touch(db: Session, user: models.User, sc: models.Scenario, action: str, detail: str = ""):
    """Every content change: bump the version (extensions re-sync), write the audit log, keep a snapshot."""
    sc.version += 1
    db.flush()
    db.expire(sc, ["rules", "profiles"])
    audit(db, user, action, detail, sc.id)
    db.add(models.ScenarioSnapshot(scenario_id=sc.id, version=sc.version, created_by=user.email,
                                   note=(action + (": " + detail if detail else ""))[:300],
                                   data=scenario_dump(sc)))
    db.flush()
    old = (db.query(models.ScenarioSnapshot.id).filter_by(scenario_id=sc.id)
           .order_by(models.ScenarioSnapshot.id.desc()).offset(MAX_SNAPSHOTS).all())
    if old:
        db.query(models.ScenarioSnapshot).filter(
            models.ScenarioSnapshot.id.in_([i for (i,) in old])).delete(synchronize_session=False)


def replace_content(db: Session, sc: models.Scenario, data: dict):
    """Replace a scenario's rules and profiles with those in a dump (restore / import / template)."""
    for r in list(sc.rules):
        db.delete(r)
    for p in list(sc.profiles):
        db.delete(p)
    db.flush()
    for i, r in enumerate(data.get("rules", [])):
        db.add(models.Rule(scenario_id=sc.id, selector=r["selector"], type=r.get("type", "text"),
                           value=r.get("value", ""), host=r.get("host", "") or "", page=r.get("page", "") or "",
                           disabled=bool(r.get("disabled", False)), position=r.get("position", i)))
    seen = set()
    for p in data.get("profiles", []):
        if p["name"] in seen:
            continue
        seen.add(p["name"])
        db.add(models.Profile(scenario_id=sc.id, name=p["name"],
                              variables={str(k): str(v) for k, v in (p.get("variables") or {}).items()}))
    db.flush()
    db.expire(sc, ["rules", "profiles"])


VALID_TYPES = {"text", "hide", "image", "html", "api"}


def clean_dump(data: dict) -> dict:
    """Validates imported / template data so bad input can't create broken rules."""
    if not isinstance(data, dict):
        raise ValueError("Expected an object")
    rules = []
    for r in data.get("rules") or []:
        if not isinstance(r, dict) or not str(r.get("selector", "")).strip():
            raise ValueError("Every rule needs a selector")
        t = r.get("type", "text")
        if t not in VALID_TYPES:
            raise ValueError(f"Unknown rule type: {t}")
        rules.append({"selector": str(r["selector"]), "type": t, "value": str(r.get("value", "")),
                      "host": str(r.get("host", "") or ""), "page": str(r.get("page", "") or ""), "disabled": bool(r.get("disabled", False))})
    profiles = []
    for p in data.get("profiles") or []:
        if not isinstance(p, dict) or not str(p.get("name", "")).strip():
            raise ValueError("Every profile needs a name")
        profiles.append({"name": str(p["name"]).strip()[:100],
                         "variables": {str(k): str(v) for k, v in (p.get("variables") or {}).items()}})
    return {"rules": rules, "profiles": profiles}
