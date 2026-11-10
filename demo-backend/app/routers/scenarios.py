from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app import models
from app.auth import get_current_user, require_role
from app.services import audit, touch
from app.schemas import ScenarioIn, ScenarioOut

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


def get_org_scenario(db: Session, user: models.User, scenario_id: int) -> models.Scenario:
    """The ONLY way endpoints should load a scenario. Filters by the caller's org."""
    sc = (
        db.query(models.Scenario)
        .filter(models.Scenario.id == scenario_id, models.Scenario.org_id == user.org_id)
        .first()
    )
    if sc is None:
        # 404, not 403: don't reveal that the id exists in another org
        raise HTTPException(status_code=404, detail="Scenario not found")
    return sc


@router.get("", response_model=list[ScenarioOut])
def list_scenarios(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return (
        db.query(models.Scenario)
        .filter(models.Scenario.org_id == user.org_id)
        .order_by(models.Scenario.name)
        .all()
    )


@router.post("", response_model=ScenarioOut, status_code=201)
def create_scenario(
    body: ScenarioIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("admin", "editor")),
):
    sc = models.Scenario(org_id=user.org_id, name=body.name.strip(), target_host=body.target_host.strip())
    db.add(sc)
    sc.version = 0
    db.flush()
    touch(db, user, sc, "scenario.create", sc.name)      # makes it version 1 and writes the first snapshot
    db.commit()
    db.refresh(sc)
    return sc


@router.get("/{scenario_id}", response_model=ScenarioOut)
def get_scenario(
    scenario_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    return get_org_scenario(db, user, scenario_id)


@router.put("/{scenario_id}", response_model=ScenarioOut)
def update_scenario(
    scenario_id: int,
    body: ScenarioIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("admin", "editor")),
):
    sc = get_org_scenario(db, user, scenario_id)
    sc.name = body.name.strip()
    sc.target_host = body.target_host.strip()
    touch(db, user, sc, "scenario.update", sc.name)
    db.commit()
    db.refresh(sc)
    return sc


@router.delete("/{scenario_id}", status_code=204)
def delete_scenario(
    scenario_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("admin")),
):
    sc = get_org_scenario(db, user, scenario_id)
    audit(db, user, "scenario.delete", sc.name, sc.id)
    db.query(models.UserPref).filter_by(scenario_id=sc.id).delete()
    db.delete(sc)
    db.commit()
