import os
import pytest
from app import models, security
import app.db as dbmod


def mk(client, h, name="S1", host="localhost"):
    r = client.post("/scenarios", json={"name": name, "target_host": host}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------- history, audit ----------
def test_history_and_restore(client, org_a):
    sid = mk(client, org_a)
    client.post(f"/scenarios/{sid}/rules", json={"selector": "#a", "type": "text", "value": "one"}, headers=org_a)
    rid = client.post(f"/scenarios/{sid}/rules", json={"selector": "#b", "type": "text", "value": "two"}, headers=org_a).json()["id"]
    hist = client.get(f"/scenarios/{sid}/history", headers=org_a).json()
    assert [h["rules"] for h in hist] == [2, 1, 0]
    client.delete(f"/rules/{rid}", headers=org_a)
    after_delete = client.get(f"/scenarios/{sid}/history", headers=org_a).json()
    assert after_delete[0]["rules"] == 1
    # restore the version that still had both rules
    snap = next(h for h in hist if h["rules"] == 2)
    r = client.post(f"/scenarios/{sid}/history/{snap['id']}/restore", headers=org_a)
    assert r.status_code == 200
    rules = client.get(f"/scenarios/{sid}/rules", headers=org_a).json()
    assert {x["selector"] for x in rules} == {"#a", "#b"}
    # restoring is itself a new version, so it can be undone
    assert client.get(f"/scenarios/{sid}/history", headers=org_a).json()[0]["note"].startswith("scenario.restore")


def test_audit_log_records_changes_and_isolated(client, org_a, org_b):
    sid = mk(client, org_a)
    client.post(f"/scenarios/{sid}/profiles", json={"name": "UK", "variables": {"x": "1"}}, headers=org_a)
    log = client.get("/audit", headers=org_a).json()
    assert {"scenario.create", "profile.create"} <= {a["action"] for a in log}
    assert all(a["user"] == "a@test.com" for a in log)
    assert client.get("/audit", headers=org_b).json() == []
    assert client.get(f"/scenarios/{sid}/history", headers=org_b).status_code == 404


def test_presenter_cannot_read_audit(client, org_a, make_user):
    p = make_user("p@test.com", "presenter")
    assert client.get("/audit", headers=p).status_code == 403
    assert client.get("/analytics", headers=p).status_code == 403


# ---------- hosts ----------
def test_rule_host_in_bundle(client, org_a):
    sid = mk(client, org_a, host="app.example.com")
    client.post(f"/scenarios/{sid}/rules", json={"selector": "#a", "value": "x"}, headers=org_a)
    client.post(f"/scenarios/{sid}/rules", json={"selector": "#b", "value": "y", "host": "*.other.com"}, headers=org_a)
    rules = client.get(f"/scenarios/{sid}/bundle", headers=org_a).json()["rules"]
    assert [r["host"] for r in rules] == ["app.example.com", "*.other.com"]


def test_old_database_gets_new_column(tmp_path):
    from sqlalchemy import create_engine, text
    eng = create_engine(f"sqlite:///{tmp_path/'old.db'}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE rules (id INTEGER PRIMARY KEY, scenario_id INTEGER, selector TEXT, type VARCHAR(20), value TEXT, disabled BOOLEAN, position INTEGER, created_at DATETIME, updated_at DATETIME)"))
        c.execute(text("INSERT INTO rules (id, scenario_id, selector, type, value, disabled, position) VALUES (1, 1, '#a', 'text', 'v', 0, 0)"))
    dbmod.migrate(eng)
    with eng.connect() as c:
        assert c.execute(text("SELECT host FROM rules")).scalar() == ""


# ---------- templates, import/export ----------
def test_template_creates_scenario_with_disabled_example_rules(client, org_a):
    keys = [t["key"] for t in client.get("/templates", headers=org_a).json()]
    assert {"education", "healthcare", "social", "sales"} <= set(keys)
    r = client.post("/scenarios/from-template", json={"key": "education", "target_host": "lms.test"}, headers=org_a)
    assert r.status_code == 201
    sid = r.json()["id"]
    assert len(client.get(f"/scenarios/{sid}/profiles", headers=org_a).json()) == 4
    rules = client.get(f"/scenarios/{sid}/rules", headers=org_a).json()
    assert rules and all(x["disabled"] for x in rules)
    assert client.post("/scenarios/from-template", json={"key": "nope"}, headers=org_a).status_code == 404


def test_export_import_round_trip_and_validation(client, org_a, org_b):
    sid = mk(client, org_a)
    client.post(f"/scenarios/{sid}/rules", json={"selector": "#a", "type": "api", "value": "[]"}, headers=org_a)
    client.post(f"/scenarios/{sid}/profiles", json={"name": "India", "variables": {"_locale": "en_IN"}}, headers=org_a)
    dump = client.get(f"/scenarios/{sid}/export", headers=org_a).json()
    r = client.post("/scenarios/import", json={"name": "Copy", "data": dump}, headers=org_b)   # into another org
    assert r.status_code == 201
    new = r.json()["id"]
    assert client.get(f"/scenarios/{new}/profiles", headers=org_b).json()[0]["name"] == "India"
    bad = client.post("/scenarios/import", json={"data": {"rules": [{"selector": "#a", "type": "script"}]}}, headers=org_b)
    assert bad.status_code == 422


# ---------- analytics ----------
def test_usage_and_analytics(client, org_a, org_b):
    sid = mk(client, org_a)
    for ev in ("demo_on", "demo_on", "profile_switch"):
        assert client.post("/usage", json={"event": ev, "scenario_id": sid, "profile": "UK"}, headers=org_a).status_code == 204
    assert client.post("/usage", json={"event": "hack"}, headers=org_a).status_code == 422
    assert client.post("/usage", json={"event": "demo_on", "scenario_id": sid}, headers=org_b).status_code == 404
    a = client.get("/analytics", headers=org_a).json()
    assert a["total"] == 3 and a["events"]["demo_on"] == 2
    assert a["scenarios"][0] == {"name": "S1", "count": 2}
    assert client.get("/analytics", headers=org_b).json()["total"] == 0


# ---------- per-user default profile ----------
def test_user_default_profile(client, org_a, make_user):
    sid = mk(client, org_a)
    for n in ("US", "India"):
        client.post(f"/scenarios/{sid}/profiles", json={"name": n, "variables": {}}, headers=org_a)
    other = make_user("e@test.com", "editor")
    assert client.get(f"/scenarios/{sid}/bundle", headers=org_a).json()["activeProfile"] == "US"
    assert client.put(f"/scenarios/{sid}/my-profile", json={"profile": "India"}, headers=other).status_code == 200
    assert client.get(f"/scenarios/{sid}/bundle", headers=other).json()["activeProfile"] == "India"
    assert client.get(f"/scenarios/{sid}/bundle", headers=org_a).json()["activeProfile"] == "US"   # others unaffected
    assert client.put(f"/scenarios/{sid}/my-profile", json={"profile": "Mars"}, headers=other).status_code == 404
    client.put(f"/scenarios/{sid}/my-profile", json={"profile": ""}, headers=other)
    assert client.get(f"/scenarios/{sid}/bundle", headers=other).json()["activeProfile"] == "US"


# ---------- hardening ----------
def test_login_rate_limit(client, org_a):
    for _ in range(security.MAX_FAILS):
        assert client.post("/auth/login", data={"username": "a@test.com", "password": "wrong"}).status_code == 401
    r = client.post("/auth/login", data={"username": "a@test.com", "password": "password123"})
    assert r.status_code == 429          # even the right password is refused while throttled


def test_registration_closes_and_invitations_work(client, monkeypatch):
    monkeypatch.setenv("ALLOW_OPEN_REGISTRATION", "false")
    body = {"org_name": "O", "email": "first@test.com", "password": "password123"}
    r = client.post("/auth/register", json=body)
    assert r.status_code == 201                                   # the very first organization is allowed
    admin = {"Authorization": "Bearer " + r.json()["access_token"]}
    assert client.post("/auth/register", json={**body, "email": "x@test.com"}).status_code == 403
    inv = client.post("/auth/invitations", json={"email": "new@test.com", "role": "presenter"}, headers=admin)
    assert inv.status_code == 201
    tok = inv.json()["token"]
    ok = client.post("/auth/accept-invite", json={"token": tok, "password": "password123"})
    assert ok.status_code == 201
    me = client.get("/auth/me", headers={"Authorization": "Bearer " + ok.json()["access_token"]}).json()
    assert me["role"] == "presenter" and me["org_id"] == client.get("/auth/me", headers=admin).json()["org_id"]
    assert client.post("/auth/accept-invite", json={"token": tok, "password": "password123"}).status_code == 400   # single use
    assert client.post("/auth/accept-invite", json={"token": "bogus", "password": "password123"}).status_code == 400
    pres = {"Authorization": "Bearer " + ok.json()["access_token"]}
    assert client.post("/auth/invitations", json={"email": "z@test.com"}, headers=pres).status_code == 403


def test_production_requires_strong_secret(monkeypatch):
    import importlib, app.auth as auth
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        importlib.reload(auth)
    monkeypatch.setenv("SECRET_KEY", "short")
    with pytest.raises(RuntimeError):
        importlib.reload(auth)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-with-more-than-32-characters")
    importlib.reload(auth)


# ---------- state data: page scope, save-as ----------
def test_rule_page_scope_round_trips(client, org_a):
    sid = mk(client, org_a)
    client.post(f"/scenarios/{sid}/rules", json={"selector": "#a", "value": "x"}, headers=org_a)
    r = client.post(f"/scenarios/{sid}/rules", json={"selector": "#b", "value": "y", "page": "/patients*"}, headers=org_a)
    assert r.json()["page"] == "/patients*"
    assert [x["page"] for x in client.get(f"/scenarios/{sid}/bundle", headers=org_a).json()["rules"]] == ["", "/patients*"]
    dump = client.get(f"/scenarios/{sid}/export", headers=org_a).json()
    new = client.post("/scenarios/import", json={"data": dump}, headers=org_a).json()["id"]
    assert [x["page"] for x in client.get(f"/scenarios/{new}/rules", headers=org_a).json()] == ["", "/patients*"]
    # editing a rule keeps its page scope when the client sends it back
    rid = r.json()["id"]
    client.put(f"/rules/{rid}", json={"selector": "#b", "value": "z", "page": "/patients*"}, headers=org_a)
    assert client.get(f"/scenarios/{sid}/rules", headers=org_a).json()[1]["page"] == "/patients*"


def test_state_profiles_are_independent_copies(client, org_a):
    sid = mk(client, org_a)
    tx = client.post(f"/scenarios/{sid}/profiles", json={"name": "Texas", "variables": {"patients": "8420", "_locale": "en_US"}}, headers=org_a).json()
    ca = client.post(f"/scenarios/{sid}/profiles", json={"name": "California", "variables": tx["variables"]}, headers=org_a).json()
    client.put(f"/profiles/{ca['id']}", json={"name": "California", "variables": {**tx["variables"], "patients": "12100"}}, headers=org_a)
    prof = {p["name"]: p["variables"] for p in client.get(f"/scenarios/{sid}/profiles", headers=org_a).json()}
    assert prof["Texas"]["patients"] == "8420" and prof["California"]["patients"] == "12100"
    assert client.post(f"/scenarios/{sid}/profiles", json={"name": "Texas", "variables": {}}, headers=org_a).status_code == 409


def test_login_page_options(client, monkeypatch):
    monkeypatch.setenv("ALLOW_OPEN_REGISTRATION", "false")
    assert client.get("/auth/options").json() == {"open_registration": True}        # nobody registered yet
    client.post("/auth/register", json={"org_name": "O", "email": "first@test.com", "password": "password123"})
    assert client.get("/auth/options").json() == {"open_registration": False}
    monkeypatch.setenv("ALLOW_OPEN_REGISTRATION", "true")
    assert client.get("/auth/options").json() == {"open_registration": True}
