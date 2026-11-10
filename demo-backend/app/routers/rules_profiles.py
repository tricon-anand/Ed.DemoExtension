from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app import models
from app.auth import get_current_user, require_role
from app.services import touch
from app.formatting import expand_filters, resolve_text
from app.schemas import RuleIn, RuleOut, ProfileIn, ProfileOut
from app.routers.scenarios import get_org_scenario

router = APIRouter(tags=["rules & profiles"])
editor = require_role("admin", "editor")


def get_org_rule(db: Session, user: models.User, rule_id: int) -> models.Rule:
    rule = (
        db.query(models.Rule)
        .join(models.Scenario)
        .filter(models.Rule.id == rule_id, models.Scenario.org_id == user.org_id)
        .first()
    )
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    return rule


def get_org_profile(db: Session, user: models.User, profile_id: int) -> models.Profile:
    prof = (
        db.query(models.Profile)
        .join(models.Scenario)
        .filter(models.Profile.id == profile_id, models.Scenario.org_id == user.org_id)
        .first()
    )
    if prof is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return prof


# ---------- rules ----------
@router.get("/scenarios/{scenario_id}/rules", response_model=list[RuleOut])
def list_rules(scenario_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return get_org_scenario(db, user, scenario_id).rules


@router.post("/scenarios/{scenario_id}/rules", response_model=RuleOut, status_code=201)
def create_rule(scenario_id: int, body: RuleIn, db: Session = Depends(get_db), user=Depends(editor)):
    sc = get_org_scenario(db, user, scenario_id)
    data = body.model_dump()
    if data["position"] is None:
        data["position"] = len(sc.rules)
    rule = models.Rule(scenario_id=sc.id, **data)
    db.add(rule)
    touch(db, user, sc, "rule.create", f"{body.type} {body.selector}"[:200])
    db.commit()
    db.refresh(rule)
    return rule


@router.put("/rules/{rule_id}", response_model=RuleOut)
def update_rule(rule_id: int, body: RuleIn, db: Session = Depends(get_db), user=Depends(editor)):
    rule = get_org_rule(db, user, rule_id)
    data = body.model_dump()
    if data["position"] is None:
        data["position"] = rule.position
    for key, val in data.items():
        setattr(rule, key, val)
    touch(db, user, rule.scenario, "rule.update", f"{body.type} {body.selector}"[:200])
    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int, db: Session = Depends(get_db), user=Depends(editor)):
    rule = get_org_rule(db, user, rule_id)
    sc, label = rule.scenario, f"{rule.type} {rule.selector}"[:200]
    db.delete(rule)
    touch(db, user, sc, "rule.delete", label)
    db.commit()


# ---------- profiles ----------
@router.get("/scenarios/{scenario_id}/profiles", response_model=list[ProfileOut])
def list_profiles(scenario_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return get_org_scenario(db, user, scenario_id).profiles


@router.post("/scenarios/{scenario_id}/profiles", response_model=ProfileOut, status_code=201)
def create_profile(scenario_id: int, body: ProfileIn, db: Session = Depends(get_db), user=Depends(editor)):
    sc = get_org_scenario(db, user, scenario_id)
    if any(p.name == body.name for p in sc.profiles):
        raise HTTPException(status_code=409, detail="Profile name already exists")
    prof = models.Profile(scenario_id=sc.id, name=body.name.strip(), variables=body.variables)
    db.add(prof)
    touch(db, user, sc, "profile.create", prof.name)
    db.commit()
    db.refresh(prof)
    return prof


@router.put("/profiles/{profile_id}", response_model=ProfileOut)
def update_profile(profile_id: int, body: ProfileIn, db: Session = Depends(get_db), user=Depends(editor)):
    prof = get_org_profile(db, user, profile_id)
    prof.name = body.name.strip()
    prof.variables = body.variables
    touch(db, user, prof.scenario, "profile.update", prof.name)
    db.commit()
    db.refresh(prof)
    return prof


@router.delete("/profiles/{profile_id}", status_code=204)
def delete_profile(profile_id: int, db: Session = Depends(get_db), user=Depends(editor)):
    prof = get_org_profile(db, user, profile_id)
    sc, name = prof.scenario, prof.name
    db.delete(prof)
    touch(db, user, sc, "profile.delete", name)
    db.commit()


# ---------- the bundle the extension downloads ----------
@router.get("/scenarios/{scenario_id}/bundle")
def bundle(
    scenario_id: int,
    request: Request,
    response: Response,
    profile: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    sc = get_org_scenario(db, user, scenario_id)
    names = [p.name for p in sc.profiles]
    if profile and profile not in names:
        raise HTTPException(status_code=404, detail="Profile not found")

    pref = (
        db.query(models.UserPref).filter_by(user_id=user.id, scenario_id=sc.id).first()
    )
    default = pref.profile_name if pref and pref.profile_name in names else (names[0] if names else "")
    active = profile or default

    etag = f'"{sc.id}-{sc.version}-{active}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag

    # Filters like {{revenue | currency}} are resolved here, per profile, so the
    # extension only ever sees plain {{name}} placeholders.
    rules, profiles, _ = expand_filters(
        [
            {
                "id": f"r{r.id}",
                "host": r.host or sc.target_host,
                "page": r.page or "",
                "selector": r.selector,
                "type": r.type,
                "value": r.value,
                "disabled": r.disabled,
            }
            for r in sc.rules
        ],
        {p.name: p.variables for p in sc.profiles},
    )

    # Same shape the extension already stores in chrome.storage.local
    return {
        "scenarioId": sc.id,
        "version": sc.version,
        "enabled": True,
        "activeProfile": active,
        "profiles": profiles,
        "rules": rules,
    }


# ---------- preview used by the admin page ----------
class PreviewIn(BaseModel):
    variables: dict[str, str] = Field(default_factory=dict)


@router.post("/scenarios/{scenario_id}/preview")
def preview(
    scenario_id: int,
    body: PreviewIn,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """What each saved rule would show for the given (possibly unsaved) variables."""
    sc = get_org_scenario(db, user, scenario_id)
    rules, profiles, origins = expand_filters(
        [{"id": r.id, "value": r.value} for r in sc.rules], {"preview": body.variables}
    )
    variables = profiles["preview"]
    return [{"id": r["id"], "result": resolve_text(r["value"], variables, origins)} for r in rules]
