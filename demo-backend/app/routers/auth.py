import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app import models, security
from app.auth import hash_password, verify_password, create_token, get_current_user, require_role
from app.schemas import RegisterIn, TokenOut, UserOut
from app.services import audit

router = APIRouter(prefix="/auth", tags=["auth"])


def open_registration() -> bool:
    return os.getenv("ALLOW_OPEN_REGISTRATION", "false").lower() in ("1", "true", "yes")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@router.get("/options")
def options(db: Session = Depends(get_db)):
    """Lets the login page know whether to offer 'Create account'. Reveals nothing else."""
    return {"open_registration": open_registration() or db.query(models.User).first() is None}


@router.post("/register", response_model=TokenOut, status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    """Creates a new organization. Closed once the first one exists, unless ALLOW_OPEN_REGISTRATION=true.
    Everyone else joins through an invitation."""
    if not open_registration() and db.query(models.User).first() is not None:
        raise HTTPException(status_code=403, detail="Registration is closed. Ask an admin for an invitation.")
    email = body.email.strip().lower()
    if db.query(models.User).filter(models.User.email == email).first():
        raise HTTPException(status_code=409, detail="Email already registered")
    org = models.Organization(name=body.org_name.strip())
    user = models.User(
        org=org, email=email, password_hash=hash_password(body.password), role="admin"
    )
    db.add_all([org, user])
    db.commit()
    return TokenOut(access_token=create_token(user.id))


@router.post("/login", response_model=TokenOut)
def login(request: Request, form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # OAuth2 form calls the email field "username"
    email = form.username.strip().lower()
    key = f"{email}|{request.client.host if request.client else ''}"
    security.check_login(key)
    user = db.query(models.User).filter(models.User.email == email).first()
    if user is None or not verify_password(form.password, user.password_hash):
        security.record_failure(key)
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    security.clear(key)
    return TokenOut(access_token=create_token(user.id))


@router.get("/me", response_model=UserOut)
def me(user: models.User = Depends(get_current_user)):
    return user


# ---------- invitations ----------
class InviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = Field(default="presenter", pattern="^(admin|editor|presenter)$")


class AcceptIn(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=128)


@router.post("/invitations", status_code=201)
def invite(body: InviteIn, db: Session = Depends(get_db), user: models.User = Depends(require_role("admin"))):
    email = body.email.strip().lower()
    if db.query(models.User).filter(models.User.email == email).first():
        raise HTTPException(status_code=409, detail="That email already has an account")
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=7)
    db.add(models.Invitation(org_id=user.org_id, email=email, role=body.role,
                             token_hash=_hash_token(token), expires_at=expires))
    audit(db, user, "user.invite", f"{email} as {body.role}")
    db.commit()
    # The token is shown once and only its hash is stored. Send it to the person (email, chat).
    return {"email": email, "role": body.role, "token": token, "expires_at": expires.isoformat()}


@router.post("/accept-invite", response_model=TokenOut, status_code=201)
def accept_invite(body: AcceptIn, db: Session = Depends(get_db)):
    inv = db.query(models.Invitation).filter_by(token_hash=_hash_token(body.token), used=False).first()
    exp = inv.expires_at if inv else None
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if inv is None or exp < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Invitation is invalid or has expired")
    if db.query(models.User).filter(models.User.email == inv.email).first():
        raise HTTPException(status_code=409, detail="Email already registered")
    user = models.User(org_id=inv.org_id, email=inv.email, role=inv.role,
                       password_hash=hash_password(body.password))
    inv.used = True
    db.add(user)
    db.commit()
    return TokenOut(access_token=create_token(user.id))


@router.get("/users")
def users(db: Session = Depends(get_db), user: models.User = Depends(require_role("admin"))):
    rows = db.query(models.User).filter_by(org_id=user.org_id).order_by(models.User.email).all()
    return [{"id": u.id, "email": u.email, "role": u.role} for u in rows]
