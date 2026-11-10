import os

os.environ["APP_SKIP_DOTENV"] = "1"          # a developer's real .env must never leak into tests
os.environ.setdefault("ALLOW_OPEN_REGISTRATION", "true")      # most tests create two organizations
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-with-more-than-32-characters")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import app.db as dbmod

# Use a throwaway in-memory database for every test run, never demo.db.
test_engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
dbmod.engine = test_engine
dbmod.SessionLocal.configure(bind=test_engine)

from app.main import app  # noqa: E402  (must come after the engine swap)
from app import models  # noqa: E402


@pytest.fixture()
def client():
    dbmod.Base.metadata.drop_all(bind=test_engine)
    dbmod.Base.metadata.create_all(bind=test_engine)
    from app import security
    security.reset()
    with TestClient(app) as c:
        yield c


def _register(client, org, email, password="password123"):
    r = client.post("/auth/register", json={"org_name": org, "email": email, "password": password})
    assert r.status_code == 201, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


@pytest.fixture()
def org_a(client):
    return _register(client, "Org A", "a@test.com")


@pytest.fixture()
def org_b(client):
    return _register(client, "Org B", "b@test.com")


@pytest.fixture()
def make_user(client):
    """Add a user with a given role to an existing organization."""
    from app.auth import hash_password

    def make(email, role, org_email="a@test.com", password="password123"):
        with dbmod.SessionLocal() as db:
            org_id = db.query(models.User).filter_by(email=org_email).one().org_id
            db.add(models.User(org_id=org_id, email=email, role=role, password_hash=hash_password(password)))
            db.commit()
        r = client.post("/auth/login", data={"username": email, "password": password})
        assert r.status_code == 200, r.text
        return {"Authorization": "Bearer " + r.json()["access_token"]}

    return make
