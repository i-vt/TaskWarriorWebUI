"""Tests for the auth key lifecycle, authentication and API integration.

No real `task` binary is needed: app.run_task / app.export are monkeypatched
where a route would otherwise shell out to taskwarrior.
"""
import importlib
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_app(tmp_path, monkeypatch):
    """(Re)load app.py with TASKDATA and the key path pointed at tmp dirs."""
    monkeypatch.setenv("TASKDATA", str(tmp_path / "taskdata"))
    monkeypatch.setenv("TASKWARRIOR_WEBUI_KEY_PATH", str(tmp_path / "key.txt"))
    monkeypatch.delenv("TASKWARRIOR_WEBUI_KEY", raising=False)
    import app
    importlib.reload(app)
    return app


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    return _load_app(tmp_path, monkeypatch)


@pytest.fixture
def client(app_module):
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as c:
        yield c


# --------------------------------------------------------------------------
# generate_key
# --------------------------------------------------------------------------
def test_generate_key_returns_32_lowercase_hex(app_module):
    key = app_module.generate_key()
    assert len(key) == 32
    assert all(c in "0123456789abcdef" for c in key)


def test_generate_key_unique_across_calls(app_module):
    keys = {app_module.generate_key() for _ in range(200)}
    assert len(keys) == 200


# --------------------------------------------------------------------------
# load_or_create_key
# --------------------------------------------------------------------------
def test_load_or_create_key_creates_file_0600(app_module, tmp_path):
    path = str(tmp_path / "created.txt")
    key = app_module.load_or_create_key(path=path)
    assert len(key) == 32
    with open(path) as f:
        assert f.read().strip() == key
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_load_or_create_key_reuses_existing(app_module, tmp_path):
    path = str(tmp_path / "reuse.txt")
    first = app_module.load_or_create_key(path=path)
    second = app_module.load_or_create_key(path=path)
    assert first == second


def test_load_or_create_key_env_override(app_module, tmp_path, monkeypatch):
    env_key = "ab" * 16
    monkeypatch.setenv("TASKWARRIOR_WEBUI_KEY", env_key)
    path = str(tmp_path / "env.txt")
    key = app_module.load_or_create_key(path=path)
    assert key == env_key
    # The env key must still be written to the file with strict perms.
    with open(path) as f:
        assert f.read().strip() == env_key
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_load_or_create_key_race_precreated_file(app_module, tmp_path):
    # Another worker already wrote a valid key: it must be reused, not
    # overwritten.
    path = str(tmp_path / "race.txt")
    winner = "cd" * 16
    with open(path, "w") as f:
        f.write(winner + "\n")
    assert app_module.load_or_create_key(path=path) == winner


def test_load_or_create_key_race_o_excl_loser(app_module, tmp_path, monkeypatch):
    # Force the O_EXCL creation race: hide the file from the reuse check so
    # os.open hits FileExistsError and the winner's key is read back.
    path = str(tmp_path / "race2.txt")
    winner = "ef" * 16
    with open(path, "w") as f:
        f.write(winner + "\n")
    monkeypatch.setattr(app_module.os.path, "exists", lambda p: False)
    assert app_module.load_or_create_key(path=path) == winner


def test_module_startup_persists_key_and_sets_secret(app_module, tmp_path):
    key_path = tmp_path / "key.txt"
    assert key_path.exists()
    assert key_path.read_text().strip() == app_module.AUTH_KEY
    assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600
    assert app_module.app.secret_key == app_module.AUTH_KEY


# --------------------------------------------------------------------------
# Auth guard
# --------------------------------------------------------------------------
def test_root_redirects_to_login_when_unauthenticated(client):
    r = client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_api_tasks_401_when_unauthenticated(client):
    r = client.get("/api/tasks")
    assert r.status_code == 401
    assert r.get_json() == {"error": "Unauthorized"}


def test_login_page_renders_without_auth(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert b"TASK" in r.data


def test_login_wrong_key_returns_401(client):
    r = client.post("/login", data={"key": "definitely-wrong"})
    assert r.status_code == 401
    assert b"Invalid key" in r.data


def test_login_correct_key_sets_session(client, app_module):
    r = client.post("/login", data={"key": app_module.AUTH_KEY})
    assert r.status_code == 302
    assert r.headers["Location"] == "/"
    r = client.get("/")
    assert r.status_code == 200


def test_login_accepts_json_body(client, app_module):
    r = client.post("/login", json={"key": app_module.AUTH_KEY})
    assert r.status_code == 302
    r = client.get("/")
    assert r.status_code == 200


def test_x_api_key_header_auth(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "export", lambda filt=None: [])
    r = client.get("/api/tasks", headers={"X-Api-Key": app_module.AUTH_KEY})
    assert r.status_code == 200


def test_authorization_bearer_header_auth(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "export", lambda filt=None: [])
    r = client.get(
        "/api/tasks",
        headers={"Authorization": f"Bearer {app_module.AUTH_KEY}"},
    )
    assert r.status_code == 200


def test_wrong_header_key_rejected(client):
    r = client.get("/api/tasks", headers={"X-Api-Key": "0" * 32})
    assert r.status_code == 401


def test_healthz_open_without_auth(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "run_task",
                        lambda *a, **k: ("3.0.0", "", 0))
    r = client.get("/api/healthz")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}


def test_logout_clears_session(client, app_module):
    client.post("/login", data={"key": app_module.AUTH_KEY})
    r = client.post("/logout")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]
    r = client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


# --------------------------------------------------------------------------
# Happy-path API integration behind auth
# --------------------------------------------------------------------------
def test_api_tasks_happy_path_authenticated(client, app_module, monkeypatch):
    tasks = [
        {"uuid": "abc123", "description": "write tests", "status": "pending",
         "urgency": 1.5},
        {"uuid": "def456", "description": "ship it", "status": "pending",
         "urgency": 0.5},
    ]
    monkeypatch.setattr(app_module, "export", lambda filt=None: list(tasks))
    r = client.get("/api/tasks", headers={"X-Api-Key": app_module.AUTH_KEY})
    assert r.status_code == 200
    data = r.get_json()
    assert len(data) == 2
    # Default sort is urgency descending.
    assert data[0]["description"] == "write tests"
    assert data[1]["description"] == "ship it"
