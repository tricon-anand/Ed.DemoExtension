def mk_scenario(client, h, name="Retail demo", host="localhost"):
    r = client.post("/scenarios", json={"name": name, "target_host": host}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def mk_rule(client, h, sid, selector, value, type="text"):
    r = client.post(f"/scenarios/{sid}/rules", json={"selector": selector, "type": type, "value": value}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def mk_profile(client, h, sid, name, variables):
    r = client.post(f"/scenarios/{sid}/profiles", json={"name": name, "variables": variables}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


UK = {"company": "Northbridge", "revenue": "1980500", "_locale": "en_GB", "_currency": "GBP"}
INDIA = {"company": "Bharat", "revenue": "84520000", "_locale": "en_IN", "_currency": "INR"}


# ---------- authentication ----------
def test_requests_without_a_token_are_rejected(client):
    assert client.get("/scenarios").status_code == 401
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_register_login_me(client):
    client.post("/auth/register", json={"org_name": "O", "email": "Me@Test.com", "password": "password123"})
    r = client.post("/auth/login", data={"username": "me@test.com", "password": "password123"})
    assert r.status_code == 200
    me = client.get("/auth/me", headers={"Authorization": "Bearer " + r.json()["access_token"]}).json()
    assert me["email"] == "me@test.com" and me["role"] == "admin"
    bad = client.post("/auth/login", data={"username": "me@test.com", "password": "wrong-password"})
    assert bad.status_code == 401


def test_duplicate_email_is_rejected(client, org_a):
    r = client.post("/auth/register", json={"org_name": "X", "email": "a@test.com", "password": "password123"})
    assert r.status_code == 409


# ---------- organization isolation ----------
def test_organizations_cannot_see_each_others_data(client, org_a, org_b):
    sid = mk_scenario(client, org_a)
    rid = mk_rule(client, org_a, sid, "#x", "v")
    pid = mk_profile(client, org_a, sid, "UK", UK)

    assert client.get("/scenarios", headers=org_b).json() == []
    paths = [
        ("get", f"/scenarios/{sid}"), ("get", f"/scenarios/{sid}/rules"), ("get", f"/scenarios/{sid}/profiles"),
        ("get", f"/scenarios/{sid}/bundle"),
    ]
    for method, path in paths:
        assert getattr(client, method)(path, headers=org_b).status_code == 404, path
    assert client.post(f"/scenarios/{sid}/rules", json={"selector": "#y"}, headers=org_b).status_code == 404
    assert client.post(f"/scenarios/{sid}/preview", json={"variables": {}}, headers=org_b).status_code == 404
    assert client.put(f"/rules/{rid}", json={"selector": "#y"}, headers=org_b).status_code == 404
    assert client.delete(f"/rules/{rid}", headers=org_b).status_code == 404
    assert client.put(f"/profiles/{pid}", json={"name": "Z", "variables": {}}, headers=org_b).status_code == 404
    assert client.delete(f"/profiles/{pid}", headers=org_b).status_code == 404
    assert client.delete(f"/scenarios/{sid}", headers=org_b).status_code == 404
    # and org A still has everything
    assert len(client.get(f"/scenarios/{sid}/rules", headers=org_a).json()) == 1


# ---------- roles ----------
def test_roles(client, org_a, make_user):
    presenter = make_user("p@test.com", "presenter")
    editor = make_user("e@test.com", "editor")
    sid = mk_scenario(client, org_a)

    assert client.get(f"/scenarios/{sid}", headers=presenter).status_code == 200      # read is allowed
    assert client.get(f"/scenarios/{sid}/bundle", headers=presenter).status_code == 200
    assert client.post("/scenarios", json={"name": "N"}, headers=presenter).status_code == 403
    assert client.post(f"/scenarios/{sid}/rules", json={"selector": "#a"}, headers=presenter).status_code == 403
    assert client.post("/scenarios", json={"name": "N"}, headers=editor).status_code == 201
    assert client.delete(f"/scenarios/{sid}", headers=editor).status_code == 403       # only admins delete
    assert client.delete(f"/scenarios/{sid}", headers=org_a).status_code == 204


# ---------- versions and the bundle ----------
def test_every_change_bumps_the_version(client, org_a):
    sid = mk_scenario(client, org_a)
    version = lambda: client.get(f"/scenarios/{sid}", headers=org_a).json()["version"]
    v = version()
    rid = mk_rule(client, org_a, sid, "#a", "1");                         assert version() > v; v = version()
    client.put(f"/rules/{rid}", json={"selector": "#a", "value": "2"}, headers=org_a);  assert version() > v; v = version()
    pid = mk_profile(client, org_a, sid, "UK", UK);                       assert version() > v; v = version()
    client.put(f"/profiles/{pid}", json={"name": "UK", "variables": {}}, headers=org_a); assert version() > v; v = version()
    client.delete(f"/profiles/{pid}", headers=org_a);                     assert version() > v; v = version()
    client.delete(f"/rules/{rid}", headers=org_a);                        assert version() > v


def test_bundle_has_the_shape_the_extension_expects(client, org_a):
    sid = mk_scenario(client, org_a, host="app.acme.com")
    mk_rule(client, org_a, sid, "#company-name", "{{company}}")
    mk_rule(client, org_a, sid, "#banner", "", type="hide")
    mk_profile(client, org_a, sid, "UK", UK)
    b = client.get(f"/scenarios/{sid}/bundle?profile=UK", headers=org_a).json()
    assert set(b) == {"scenarioId", "version", "enabled", "activeProfile", "profiles", "rules"}
    assert b["activeProfile"] == "UK" and b["enabled"] is True
    assert b["rules"][0]["host"] == "app.acme.com" and b["rules"][0]["selector"] == "#company-name"
    assert b["rules"][1]["type"] == "hide"
    assert client.get(f"/scenarios/{sid}/bundle?profile=Nope", headers=org_a).status_code == 404


def test_bundle_formats_per_country(client, org_a):
    sid = mk_scenario(client, org_a)
    mk_rule(client, org_a, sid, "#revenue", "{{revenue | currency}}")
    mk_profile(client, org_a, sid, "UK", UK)
    mk_profile(client, org_a, sid, "India", INDIA)
    b = client.get(f"/scenarios/{sid}/bundle", headers=org_a).json()
    key = b["rules"][0]["value"].strip("{}")
    assert b["profiles"]["UK"][key] == "£1,980,500"
    assert b["profiles"]["India"][key] == "₹8,45,20,000"
    assert b["profiles"]["UK"]["revenue"] == "1980500"           # raw values are left alone


def test_etag_gives_304_until_something_changes(client, org_a):
    sid = mk_scenario(client, org_a)
    mk_profile(client, org_a, sid, "UK", UK)
    first = client.get(f"/scenarios/{sid}/bundle", headers=org_a)
    etag = first.headers["etag"]
    assert client.get(f"/scenarios/{sid}/bundle", headers={**org_a, "If-None-Match": etag}).status_code == 304
    mk_rule(client, org_a, sid, "#a", "1")
    changed = client.get(f"/scenarios/{sid}/bundle", headers={**org_a, "If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["etag"] != etag


def test_duplicate_profile_name_is_rejected(client, org_a):
    sid = mk_scenario(client, org_a)
    mk_profile(client, org_a, sid, "UK", UK)
    r = client.post(f"/scenarios/{sid}/profiles", json={"name": "UK", "variables": {}}, headers=org_a)
    assert r.status_code == 409


def test_deleting_a_scenario_removes_its_rules_and_profiles(client, org_a):
    sid = mk_scenario(client, org_a)
    rid = mk_rule(client, org_a, sid, "#a", "1")
    pid = mk_profile(client, org_a, sid, "UK", UK)
    assert client.delete(f"/scenarios/{sid}", headers=org_a).status_code == 204
    assert client.put(f"/rules/{rid}", json={"selector": "#a"}, headers=org_a).status_code == 404
    assert client.put(f"/profiles/{pid}", json={"name": "UK", "variables": {}}, headers=org_a).status_code == 404


# ---------- preview used by the admin page ----------
def test_preview_uses_the_typed_variables_even_if_unsaved(client, org_a):
    sid = mk_scenario(client, org_a)
    mk_rule(client, org_a, sid, "#c", "{{company}}")
    mk_rule(client, org_a, sid, "#r", "{{revenue | currency}}")
    rules = client.get(f"/scenarios/{sid}/rules", headers=org_a).json()
    out = client.post(f"/scenarios/{sid}/preview", json={"variables": INDIA}, headers=org_a).json()
    assert [o["result"] for o in out] == ["Bharat", "₹8,45,20,000"]
    assert [o["id"] for o in out] == [r["id"] for r in rules]
    missing = client.post(f"/scenarios/{sid}/preview", json={"variables": {}}, headers=org_a).json()
    assert [o["result"] for o in missing] == ["{{company}}", "{{revenue | currency}}"]


# ---------- api rules (network override) ----------
def test_api_rules_are_stored_and_shipped_in_the_bundle(client, org_a):
    sid = mk_scenario(client, org_a, host="app.acme.com")
    edits = '[{"path": "kpis.revenue", "value": "{{revenue}}"}]'
    r = client.post(f"/scenarios/{sid}/rules", json={"selector": "*/api/dashboard*", "type": "api", "value": edits}, headers=org_a)
    assert r.status_code == 201, r.text
    mk_profile(client, org_a, sid, "UK", UK)
    rule = client.get(f"/scenarios/{sid}/bundle", headers=org_a).json()["rules"][0]
    assert rule["type"] == "api" and rule["selector"] == "*/api/dashboard*" and rule["value"] == edits
    assert rule["host"] == "app.acme.com"


def test_unknown_rule_types_are_still_rejected(client, org_a):
    sid = mk_scenario(client, org_a)
    r = client.post(f"/scenarios/{sid}/rules", json={"selector": "#a", "type": "script", "value": "x"}, headers=org_a)
    assert r.status_code == 422
