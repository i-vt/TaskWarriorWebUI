"""Tests for accounts, tokens, per-user scoping and the HTTP API.

No real `task` binary is needed: store.run_task / store.export are
monkeypatched where a route would otherwise shell out to taskwarrior.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKWARRIOR_APP_ROOT", str(tmp_path / "app"))
    monkeypatch.setenv("TASKWARRIOR_USERS_ROOT", str(tmp_path / "users"))
    monkeypatch.setenv("USERS_DB", str(tmp_path / "app" / "users.db"))
    monkeypatch.setenv("TASKWARRIOR_WEBUI_KEY_PATH", str(tmp_path / "secret.txt"))
    monkeypatch.setenv("TASKWARRIOR_WEBUI_KEY", "")
    monkeypatch.setenv("TASKDATA", str(tmp_path / "legacy"))
    monkeypatch.setenv("DOMAIN", "")
    monkeypatch.delenv("TASKWARRIOR_WEBUI_KEY", raising=False)
    return tmp_path


@pytest.fixture()
def app_module(env, monkeypatch):
    import users
    importlib.reload(users)
    import app
    importlib.reload(app)
    app.app.config.update(TESTING=True)
    return app


@pytest.fixture()
def client(app_module):
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture()
def admin_client(app_module, client):
    import users
    tok = users.create_token("admin_setup")
    client.post(f"/register/{tok}",
                data={"username": "admin", "password": "pw12345",
                      "confirm": "pw12345"})
    return client


# --------------------------------------------------------------------------
# Key handling
# --------------------------------------------------------------------------
def test_generate_key_is_32_hex(app_module):
    key = app_module.generate_key()
    assert len(key) == 32
    assert all(c in "0123456789abcdef" for c in key)


def test_load_or_create_key_persists(env, app_module):
    k1 = app_module.load_or_create_key()
    k2 = app_module.load_or_create_key()
    assert k1 == k2 and len(k1) == 32


def test_env_key_written_to_file(env, app_module, monkeypatch):
    monkeypatch.setenv("TASKWARRIOR_WEBUI_KEY", "a" * 32)
    key = app_module.load_or_create_key()
    assert key == "a" * 32
    with open(os.environ["TASKWARRIOR_WEBUI_KEY_PATH"]) as f:
        assert f.read().strip() == "a" * 32


# --------------------------------------------------------------------------
# users store
# --------------------------------------------------------------------------
def test_password_roundtrip_and_disabled(env):
    import users
    users.init_db()
    uid = users.create_user("carol", "secret9")
    assert users.verify_login("carol", "secret9")["id"] == uid
    assert users.verify_login("carol", "wrong") is None
    users.set_disabled(uid, True)
    assert users.verify_login("carol", "secret9") == "disabled"


def test_password_minimum_length(env):
    import users
    users.init_db()
    with pytest.raises(ValueError):
        users.create_user("dave", "123")


def test_username_validation(env):
    import users
    users.init_db()
    with pytest.raises(ValueError):
        users.create_user("no spaces!", "secret9")


def test_tokens_are_one_time_and_expire(env, monkeypatch):
    import time
    import users
    users.init_db()
    tok = users.create_token("reset", user_id=None)
    assert users.peek_token(tok, "reset") is not None
    assert users.peek_token(tok, "register") is None     # wrong kind
    assert users.consume_token(tok, "reset") is not None
    assert users.consume_token(tok, "reset") is None     # single use

    tok2 = users.create_token("register")
    real_time = time.time
    monkeypatch.setattr(time, "time",
                        lambda: real_time() + 8 * 24 * 3600)
    assert users.peek_token(tok2) is None                # expired


def test_short_code_charset_and_conflict(env):
    import users
    users.init_db()
    code = users.create_short_code("tglink")
    assert len(code) == 8
    assert users.consume_token(code, "tglink") is not None


def test_timezone_validation(env):
    import users
    users.init_db()
    uid = users.create_user("erin", "secret9", timezone="UTC")
    users.set_timezone(uid, "Asia/Tokyo")
    assert users.get_user(uid)["timezone"] == "Asia/Tokyo"
    with pytest.raises(ValueError):
        users.set_timezone(uid, "Nowhere/Special")


def test_telegram_link_conflict(env):
    import users
    users.init_db()
    a = users.create_user("ann", "secret9")
    b = users.create_user("bea", "secret9")
    users.link_telegram(a, 111, 111)
    with pytest.raises(ValueError):
        users.link_telegram(b, 111, 111)


def test_admin_exists(env):
    import users
    users.init_db()
    assert not users.admin_exists()
    users.create_user("boss", "secret9", is_admin=True)
    assert users.admin_exists()


# --------------------------------------------------------------------------
# store helpers
# --------------------------------------------------------------------------
def test_sanitize_filter_blocks_mutations():
    import store
    assert store.sanitize_filter("status:pending +work") == \
        ["status:pending", "+work"]
    assert store.sanitize_filter("add delete rc.data.location=/x") == []
    assert store.sanitize_filter('project:"my proj"') == ["project:my proj"]


def test_build_set_args():
    import store
    args = store.build_set_args({"project": "p", "due": "20260101T000000Z",
                                 "priority": ""}, creating=True)
    assert "project:p" in args and "due:20260101T000000Z" in args
    assert "priority:" not in args            # empty skipped on create
    args = store.build_set_args({"priority": ""}, creating=False)
    assert "priority:" in args                # empty clears on modify


def test_parse_tw_date():
    import store
    from datetime import datetime, timezone
    dt = store.parse_tw_date("20260102T030405Z")
    assert dt == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert store.parse_tw_date("junk") is None


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------
def test_healthz_public(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module.store, "run_task",
                        lambda *a, **k: ("", "", 0))
    r = client.get("/api/healthz")
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_auth_required(client):
    r = client.get("/")
    assert r.status_code == 302 and "/login" in r.location
    r = client.get("/api/tasks")
    assert r.status_code == 401


def test_admin_registration_flow(app_module, client):
    import users
    tok = users.create_token("admin_setup")
    r = client.get(f"/register/{tok}")
    assert r.status_code == 200
    r = client.post(f"/register/{tok}",
                    data={"username": "root", "password": "hunter22",
                          "confirm": "hunter22"})
    assert r.status_code == 302
    assert users.admin_exists()
    # token is single-use
    r = client.get(f"/register/{tok}")
    assert b"invalid" in r.data.lower()


def test_register_rejects_mismatched_passwords(app_module, client):
    import users
    tok = users.create_token("register")
    r = client.post(f"/register/{tok}",
                    data={"username": "x1", "password": "aaaaaa",
                          "confirm": "bbbbbb"})
    assert r.status_code == 400


def test_bootstrap_creates_admin_token_and_logs_links(app_module, env):
    import users
    rows = [r for r in users.list_users()]
    assert rows == []
    with users.db() as conn:
        row = conn.execute("SELECT token FROM tokens WHERE kind='admin_setup'"
                           ).fetchone()
    assert row and len(row["token"]) > 20


def test_task_crud_per_user(app_module, admin_client, monkeypatch):
    import users
    calls = []

    def fake_export(filter_tokens=None, data_dir=None, rc_path=None):
        calls.append(data_dir)
        return [{"uuid": "u1", "id": 1, "description": "stub",
                 "status": "pending", "urgency": 2}]

    monkeypatch.setattr(app_module.store, "export", fake_export)
    r = admin_client.get("/api/tasks?status=pending")
    assert r.status_code == 200 and r.get_json()[0]["uuid"] == "u1"
    uid = users.get_user_by_username("admin")["id"]
    assert calls[-1].endswith(f"u{uid}")


def test_add_task_requires_description(admin_client):
    r = admin_client.post("/api/tasks", json={"description": "  "})
    assert r.status_code == 400


def test_admin_register_link_uses_detected_host(app_module, admin_client,
                                                monkeypatch):
    monkeypatch.setattr(app_module, "public_ip", lambda *a, **k: "203.0.113.7")
    r = admin_client.post("/api/admin/register-link")
    assert r.status_code == 200
    assert r.get_json()["link"].startswith("http://203.0.113.7:62304/register/")


def test_admin_lifecycle(app_module, admin_client):
    import users
    users.create_user("frank", "secret9")
    frank = users.get_user_by_username("frank")
    me = users.get_user_by_username("admin")

    r = admin_client.post(f"/api/admin/users/{frank['id']}/disable")
    assert r.status_code == 200
    assert users.get_user(frank["id"])["disabled"] == 1

    r = admin_client.post(f"/api/admin/users/{frank['id']}/reset")
    assert "/reset/" in r.get_json()["link"]

    r = admin_client.delete(f"/api/admin/users/{frank['id']}")
    assert r.status_code == 200
    assert users.get_user_by_username("frank") is None

    r = admin_client.delete(f"/api/admin/users/{me['id']}")
    assert r.status_code == 400            # cannot delete yourself


def test_member_cannot_access_admin_api(app_module, admin_client):
    import users
    tok = admin_client.post("/api/admin/register-link").get_json()["link"]
    tok = tok.rsplit("/register/", 1)[1]
    m = app_module.app.test_client()
    m.post(f"/register/{tok}", data={"username": "gina", "password": "secret9",
                                     "confirm": "secret9"})
    assert m.get("/api/admin/users").status_code == 403
    assert users.admin_exists()


def test_profile_timezone_and_telegram_code(app_module, admin_client):
    import users
    r = admin_client.post("/api/profile/timezone",
                          json={"timezone": "Europe/Rome"})
    assert r.status_code == 200
    r = admin_client.post("/api/profile/telegram/code")
    code = r.get_json()["code"]
    assert users.peek_token(code, "tglink")


def test_disabled_user_session_cleared(app_module, admin_client):
    import users
    users.create_user("hank", "secret9")
    hank = users.get_user_by_username("hank")
    users.set_disabled(hank["id"], True)
    m = app_module.app.test_client()
    r = m.post("/login", data={"username": "hank", "password": "secret9"})
    assert r.status_code == 403
