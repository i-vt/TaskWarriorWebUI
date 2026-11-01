"""TaskWarrior WebUI - multi-user web panel with admin management.

Every account gets an isolated Taskwarrior database under
``/data/users/u<id>/.task``. The first run creates a one-time admin
registration token; the admin then issues per-member registration links
from the admin panel. Authentication is session-based (username/password).
The optional Telegram bot (bot.py) links into the same accounts via
one-time codes generated on the profile page.
"""
import json
import logging
import logging.handlers
import os
import re
import shlex
import socket
import time
import uuid as uuid_mod
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (Flask, jsonify, request, render_template, Response,
                   session, redirect, url_for)

import store
import users
from users import (KIND_ADMIN_SETUP, KIND_REGISTER, KIND_RESET, KIND_TGLINK)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

APP_ROOT = os.environ.get("TASKWARRIOR_APP_ROOT", "/data/app")
SYSTEM_LOG = os.path.join(APP_ROOT, "system.log")
os.makedirs(APP_ROOT, exist_ok=True)
try:
    _file_handler = logging.handlers.RotatingFileHandler(
        SYSTEM_LOG, maxBytes=2_000_000, backupCount=3)
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(_file_handler)
except OSError as exc:  # read-only FS in some tests
    logger.warning("Could not open system log %s: %s", SYSTEM_LOG, exc)

app = Flask(__name__)

WEB_PORT = os.environ.get("WEB_PORT", "62304")
DOMAIN = os.environ.get("DOMAIN", "").strip()
DEFAULT_TIMEZONE = os.environ.get("TIMEZONE", "UTC")

# --------------------------------------------------------------------------
# Flask secret key: one 32-char key shared by every gunicorn worker,
# persisted to disk so sessions survive restarts. Env-overridable so tests
# and odd deployments can relocate the key file.
# --------------------------------------------------------------------------
KEY_PATH = os.environ.get("TASKWARRIOR_WEBUI_KEY_PATH",
                          os.path.join(APP_ROOT, "flask_secret.txt"))


def generate_key():
    """Return a fresh 32-char lowercase hex key."""
    return uuid_mod.uuid4().hex


def _read_key_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def _write_key_file(path, key):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key + "\n")
    finally:
        os.chmod(path, 0o600)


def _read_winner_key(path, timeout=5.0, interval=0.1):
    """Another worker won the O_EXCL race; wait for and read its key."""
    deadline = time.monotonic() + timeout
    while True:
        existing = _read_key_file(path)
        if len(existing) == 32:
            return existing
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
    raise RuntimeError(f"could not read a valid secret key from {path}")


def load_or_create_key(path=KEY_PATH):
    env_key = os.environ.get("TASKWARRIOR_WEBUI_KEY", "").strip()
    if env_key:
        _write_key_file(path, env_key)
        return env_key
    if os.path.exists(path):
        existing = _read_key_file(path)
        if len(existing) == 32:
            return existing
        os.remove(path)
    key = generate_key()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_winner_key(path)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    return key


app.secret_key = load_or_create_key()

# --------------------------------------------------------------------------
# Networking helpers for registration/reset links
# --------------------------------------------------------------------------
_public_ip_cache = None


def public_ip(timeout=3.0):
    global _public_ip_cache
    if _public_ip_cache is not None:
        return _public_ip_cache or None
    try:
        import urllib.request
        with urllib.request.urlopen("https://api.ipify.org",
                                    timeout=timeout) as r:
            _public_ip_cache = r.read().decode("utf-8", "replace").strip()
    except Exception as exc:
        logger.info("Public IP detection failed: %s", exc)
        _public_ip_cache = ""
    return _public_ip_cache or None


def enumerate_ipv4():
    """All local IPv4 addresses (plus localhost) for admin setup links."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no traffic is actually sent
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    ips.add("127.0.0.1")
    return sorted(ips)


def external_base_url():
    """Base URL for links handed to users (public IP preferred)."""
    if DOMAIN:
        return f"https://{DOMAIN}"
    host = public_ip() or "127.0.0.1"
    return f"http://{host}:{WEB_PORT}"


# --------------------------------------------------------------------------
# Per-user task environment
# --------------------------------------------------------------------------
def current_task_env():
    uid = session.get("user_id")
    if uid:
        d = store.user_data_dir(uid)
        return d, os.path.join(d, ".taskrc")
    return store.DEFAULT_TASKDATA, store.DEFAULT_TASKRC


def ensure_user_taskenv(user_id):
    d = store.user_data_dir(user_id)
    store.ensure_taskrc(d)
    return d


# Compatibility wrappers (also used by the test-suite).
def run_task(*args, input_data=None, **kwargs):
    data_dir, rc_path = current_task_env()
    return store.run_task(*args, data_dir=data_dir, rc_path=rc_path,
                          input_data=input_data, **kwargs)


def export(filter_tokens=None):
    data_dir, rc_path = current_task_env()
    return store.export(filter_tokens, data_dir=data_dir, rc_path=rc_path)


def sanitize_filter(raw):
    return store.sanitize_filter(raw)


def latest():
    data_dir, rc_path = current_task_env()
    return store.latest(data_dir=data_dir, rc_path=rc_path)


def uuid_filter(u):
    return store.uuid_filter(u)


def build_set_args(data, *, creating):
    return store.build_set_args(data, creating=creating)


def _parse_tw_date(s):
    return store.parse_tw_date(s)


def _now_iso_compact():
    return store.now_iso_compact()


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    u = users.get_user(uid)
    if not u or u["disabled"]:
        session.clear()
        return None
    return u


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u:
            return jsonify({"error": "Unauthorized"}), 401
        if not u["is_admin"]:
            return jsonify({"error": "Admin only"}), 403
        return fn(*args, **kwargs)
    return wrapper


@app.before_request
def require_auth():
    path = request.path
    public = (path == "/login" or path == "/api/healthz"
              or path.startswith("/static/")
              or path.startswith("/register/")
              or path.startswith("/reset/"))
    if public:
        return None
    if current_user():
        return None
    if path.startswith("/api/"):
        return jsonify({"error": "Unauthorized"}), 401
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        result = users.verify_login(username, password)
        if result == "disabled":
            logger.warning("Disabled login attempt for %r from %s",
                           username, request.remote_addr)
            return render_template("login.html",
                                   error="This account is disabled"), 403
        if result:
            session.clear()
            session["user_id"] = result["id"]
            session.permanent = True
            ensure_user_taskenv(result["id"])
            logger.info("User '%s' logged in from %s",
                        result["username"], request.remote_addr)
            nxt = request.args.get("next") or url_for("index")
            if not nxt.startswith("/"):
                nxt = url_for("index")
            return redirect(nxt)
        logger.warning("Failed login attempt for %r from %s",
                       username, request.remote_addr)
        return render_template("login.html", error="Invalid credentials"), 401
    return render_template("login.html", error=None)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    u = current_user()
    if u:
        logger.info("User '%s' logged out", u["username"])
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# Registration & password reset (token-based)
# --------------------------------------------------------------------------
@app.route("/register/<token>", methods=["GET", "POST"])
def register(token):
    row = users.peek_token(token)
    if not row or row["kind"] not in (KIND_ADMIN_SETUP, KIND_REGISTER):
        return render_template("message.html",
                               title="Invalid link",
                               message="This registration link is invalid, "
                                       "already used, or has expired."), 400
    if row["kind"] == KIND_ADMIN_SETUP and users.admin_exists():
        return render_template("message.html",
                               title="Admin exists",
                               message="An admin account already exists."), 400
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if password != confirm:
            return render_template("register.html", token=token,
                                   is_admin=row["kind"] == KIND_ADMIN_SETUP,
                                   error="Passwords do not match"), 400
        try:
            user_id = users.create_user(
                username, password,
                is_admin=(row["kind"] == KIND_ADMIN_SETUP),
                timezone=DEFAULT_TIMEZONE)
        except ValueError as exc:
            return render_template("register.html", token=token,
                                   is_admin=row["kind"] == KIND_ADMIN_SETUP,
                                   error=str(exc)), 400
        users.consume_token(token, row["kind"])
        ensure_user_taskenv(user_id)
        if row["kind"] == KIND_ADMIN_SETUP:
            logger.info("SYSTEM: admin account '%s' created via one-time "
                        "registration link (token used)", username)
        else:
            logger.info("SYSTEM: member account '%s' registered via invite "
                        "link", username)
        session.clear()
        session["user_id"] = user_id
        return redirect(url_for("index"))
    return render_template("register.html", token=token, error=None,
                           is_admin=row["kind"] == KIND_ADMIN_SETUP)


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    row = users.peek_token(token, KIND_RESET)
    if not row:
        return render_template("message.html",
                               title="Invalid link",
                               message="This password reset link is invalid, "
                                       "already used, or has expired."), 400
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if password != confirm:
            return render_template("reset.html", token=token,
                                   error="Passwords do not match"), 400
        try:
            users.set_password(row["user_id"], password)
        except ValueError as exc:
            return render_template("reset.html", token=token,
                                   error=str(exc)), 400
        users.consume_token(token, KIND_RESET)
        u = users.get_user(row["user_id"])
        logger.info("SYSTEM: password reset completed for user '%s'",
                    u["username"] if u else row["user_id"])
        return render_template("message.html",
                               title="Password updated",
                               message="Your password has been updated. "
                                       "You can sign in now.")
    return render_template("reset.html", token=token, error=None)


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    return render_template("index.html", user=current_user())


@app.route("/admin")
@login_required
def admin_page():
    u = current_user()
    if not u["is_admin"]:
        return redirect(url_for("index"))
    return render_template("admin.html", user=u)


@app.route("/profile")
@login_required
def profile_page():
    return render_template("profile.html", user=current_user())


# ──────────────────────────────────────────────────────────────────────────
# Task collection (scoped to the signed-in user's database)
# ──────────────────────────────────────────────────────────────────────────
@app.route("/api/tasks", methods=["GET"])
@login_required
def list_tasks():
    status = request.args.get("status", "")
    vtag = request.args.get("vtag", "")          # virtual tag, e.g. OVERDUE
    project = request.args.get("project", "")
    tag = request.args.get("tag", "")
    raw = request.args.get("filter", "")
    sort = request.args.get("sort", "urgency")

    filt = []
    if status:
        filt.append(f"status:{status}")
    if vtag:
        filt.append(f"+{re.sub(r'[^A-Za-z]', '', vtag)}")
    if project:
        filt.append(f"project:{project}")
    if tag:
        filt.append(f"+{tag}")
    filt += sanitize_filter(raw)

    tasks = export(filt)

    reverse = True
    keyname = sort
    if sort.startswith("-"):
        reverse = False
        keyname = sort[1:]

    def sort_key(t):
        v = t.get(keyname)
        if keyname in ("urgency",):
            return float(v or 0)
        return (v is None, v or "")

    try:
        tasks.sort(key=sort_key, reverse=reverse)
    except TypeError:
        tasks.sort(key=lambda t: float(t.get("urgency", 0) or 0), reverse=True)
    return jsonify(tasks)


@app.route("/api/tasks", methods=["POST"])
@login_required
def add_task():
    data = request.json or {}
    created, err = store.add_task(data, *current_task_env())
    if err:
        status = 400 if "Description" in err else 500
        return jsonify({"error": err}), status
    return jsonify(created or {"message": "added"}), 201


@app.route("/api/tasks/log", methods=["POST"])
@login_required
def log_task():
    """Record an already-completed task (taskwarrior `log`)."""
    data = request.json or {}
    description = (data.get("description") or "").strip()
    if not description:
        return jsonify({"error": "Description required"}), 400
    args = list(store.RC) + ["log"]
    args += build_set_args(data, creating=True)
    for t in data.get("tags", []) or []:
        t = str(t).strip()
        if t:
            args.append(f"+{t}")
    args.append(description)
    out, err, code = run_task(*args)
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    return jsonify({"message": "logged"}), 201


# ──────────────────────────────────────────────────────────────────────────
# Single task
# ──────────────────────────────────────────────────────────────────────────
@app.route("/api/tasks/<uuid>", methods=["GET"])
@login_required
def get_task(uuid):
    tasks = export(uuid_filter(uuid))
    if not tasks:
        return jsonify({"error": "Not found"}), 404
    task = tasks[0]

    dep_uuids = task.get("depends") or []
    if isinstance(dep_uuids, str):
        dep_uuids = [d for d in dep_uuids.split(",") if d]
    resolved = []
    for du in dep_uuids:
        d = export([f"uuid:{du}"])
        if d:
            resolved.append({
                "uuid": d[0].get("uuid"),
                "id": d[0].get("id"),
                "description": d[0].get("description"),
                "status": d[0].get("status"),
            })
        else:
            resolved.append({"uuid": du, "description": "(unknown)"})
    task["depends_detail"] = resolved
    return jsonify(task)


@app.route("/api/tasks/<uuid>", methods=["PATCH"])
@login_required
def modify_task(uuid):
    data = request.json or {}
    args = list(store.RC) + uuid_filter(uuid) + ["modify"]

    if "description" in data and str(data["description"]).strip():
        args.append(str(data["description"]).strip())

    args += build_set_args(data, creating=False)

    for t in data.get("tags_add", []) or []:
        t = str(t).strip()
        if t:
            args.append(f"+{t}")
    for t in data.get("tags_remove", []) or []:
        t = str(t).strip()
        if t:
            args.append(f"-{t}")

    if data.get("depends_clear"):
        args.append("depends:")
    else:
        add_deps = [d for d in (data.get("depends_add") or []) if str(d).strip()]
        if add_deps:
            args.append("depends:" + ",".join(str(d).strip() for d in add_deps))
        for d in (data.get("depends_remove") or []):
            d = str(d).strip()
            if d:
                args.append(f"depends:-{d}")

    out, err, code = run_task(*args)
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500

    task = export(uuid_filter(uuid))
    return jsonify(task[0] if task else {"message": "modified"})


def _simple_action(uuid, verb, ok_msg):
    out, err, code = run_task(*store.RC, *uuid_filter(uuid), verb)
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    return jsonify({"message": ok_msg})


@app.route("/api/tasks/<uuid>/done", methods=["POST"])
@login_required
def complete_task(uuid):
    return _simple_action(uuid, "done", "Task completed")


@app.route("/api/tasks/<uuid>/start", methods=["POST"])
@login_required
def start_task(uuid):
    return _simple_action(uuid, "start", "Task started")


@app.route("/api/tasks/<uuid>/stop", methods=["POST"])
@login_required
def stop_task(uuid):
    return _simple_action(uuid, "stop", "Task stopped")


@app.route("/api/tasks/<uuid>", methods=["DELETE"])
@login_required
def delete_task(uuid):
    return _simple_action(uuid, "delete", "Task deleted")


@app.route("/api/tasks/<uuid>/purge", methods=["POST"])
@login_required
def purge_task(uuid):
    """Permanently remove a task. Taskwarrior only allows purging a task
    that is already deleted, and only when addressed by uuid."""
    tasks = export(uuid_filter(uuid))
    if tasks and tasks[0].get("status") != "deleted":
        run_task(*store.RC, *uuid_filter(uuid), "delete")
    out, err, code = run_task(*store.RC, *uuid_filter(uuid), "purge")
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    return jsonify({"message": "Task purged"})


@app.route("/api/tasks/<uuid>/duplicate", methods=["POST"])
@login_required
def duplicate_task(uuid):
    out, err, code = run_task(*store.RC, *uuid_filter(uuid), "duplicate")
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    return jsonify(latest() or {"message": "duplicated"}), 201


@app.route("/api/tasks/<uuid>/annotate", methods=["POST"])
@login_required
def annotate_task(uuid):
    text = (request.json or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "Annotation text required"}), 400
    out, err, code = run_task(*store.RC, *uuid_filter(uuid), "annotate", text)
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    task = export(uuid_filter(uuid))
    return jsonify(task[0] if task else {"message": "annotated"})


@app.route("/api/tasks/<uuid>/denotate", methods=["POST"])
@login_required
def denotate_task(uuid):
    text = (request.json or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "Annotation text required"}), 400
    out, err, code = run_task(*store.RC, *uuid_filter(uuid), "denotate", text)
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    task = export(uuid_filter(uuid))
    return jsonify(task[0] if task else {"message": "denotated"})


# ──────────────────────────────────────────────────────────────────────────
# Global operations
# ──────────────────────────────────────────────────────────────────────────
@app.route("/api/undo", methods=["POST"])
@login_required
def undo():
    out, err, code = run_task("rc.confirmation=no", "rc.verbose=nothing", "undo")
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    return jsonify({"message": "Last change undone"})


@app.route("/api/export", methods=["GET"])
@login_required
def export_all():
    out, err, code = run_task("rc.json.array=on", "rc.verbose=nothing", "export")
    return Response(
        out or "[]",
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=tasks-export.json"},
    )


@app.route("/api/import", methods=["POST"])
@login_required
def import_tasks():
    payload = None
    if request.files.get("file"):
        payload = request.files["file"].read().decode("utf-8", "replace")
    elif request.is_json:
        payload = json.dumps(request.json)
    elif request.data:
        payload = request.data.decode("utf-8", "replace")
    if not payload:
        return jsonify({"error": "No import data"}), 400
    out, err, code = run_task(
        "rc.verbose=nothing", "rc.confirmation=no", "import", "-",
        input_data=payload,
    )
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    return jsonify({"message": out.strip() or "Imported"})


# ──────────────────────────────────────────────────────────────────────────
# Metadata: projects, tags, stats, udas, contexts
# ──────────────────────────────────────────────────────────────────────────
@app.route("/api/projects", methods=["GET"])
@login_required
def list_projects():
    out, _, _ = run_task("rc.verbose=nothing", "_projects")
    return jsonify([p.strip() for p in out.splitlines() if p.strip()])


@app.route("/api/tags", methods=["GET"])
@login_required
def list_tags():
    out, _, _ = run_task("rc.verbose=nothing", "_tags")
    skip = {"next", "nocolor", "nocal", "nonag"}
    return jsonify([t.strip() for t in out.splitlines()
                    if t.strip() and t.strip() not in skip])


@app.route("/api/stats", methods=["GET"])
@login_required
def get_stats():
    pending = export(["status:pending"])
    now = _now_iso_compact()
    active = sum(1 for t in pending if t.get("start"))
    overdue = sum(1 for t in pending if t.get("due") and t["due"] < now)
    blocked = len(export(["+BLOCKED"]))
    return jsonify({
        "pending": len(pending),
        "active": active,
        "overdue": overdue,
        "blocked": blocked,
        "waiting": len(export(["status:waiting"])),
        "recurring": len(export(["status:recurring"])),
        "completed": len(export(["status:completed"])),
        "deleted": len(export(["status:deleted"])),
    })


@app.route("/api/overview", methods=["GET"])
@login_required
def overview():
    """One round-trip powering the sidebar + stat bar."""
    pending = export(["status:pending"])
    now = _now_iso_compact()

    proj_counts, tag_counts = {}, {}
    active = overdue = 0
    for t in pending:
        if t.get("start"):
            active += 1
        if t.get("due") and t["due"] < now:
            overdue += 1
        p = t.get("project")
        if p:
            proj_counts[p] = proj_counts.get(p, 0) + 1
        for tg in t.get("tags", []) or []:
            tag_counts[tg] = tag_counts.get(tg, 0) + 1

    blocked = len(export(["+BLOCKED"]))
    blocking = len(export(["+BLOCKING"]))
    waiting = export(["status:waiting"])
    recurring = export(["status:recurring"])
    scheduled = len(export(["status:pending", "+SCHEDULED"]))
    due_soon = len(export(["status:pending", "+DUE"]))

    return jsonify({
        "stats": {
            "pending": len(pending),
            "active": active,
            "overdue": overdue,
            "blocked": blocked,
            "blocking": blocking,
            "scheduled": scheduled,
            "due_soon": due_soon,
            "waiting": len(waiting),
            "recurring": len(recurring),
            "completed": len(export(["status:completed"])),
            "deleted": len(export(["status:deleted"])),
        },
        "projects": sorted(
            [{"name": k, "count": v} for k, v in proj_counts.items()],
            key=lambda x: x["name"],
        ),
        "tags": sorted(
            [{"name": k, "count": v} for k, v in tag_counts.items()],
            key=lambda x: (-x["count"], x["name"]),
        ),
        "contexts": _read_contexts(),
        "udas": _read_udas(),
        "priorities": _read_priorities(),
    })


def _read_priorities():
    out, _, _ = run_task("rc.verbose=nothing", "_get", "rc.uda.priority.values")
    vals = [v for v in out.strip().split(",")]
    return [v for v in vals if v]


def _read_udas():
    out, _, _ = run_task("rc.verbose=nothing", "_udas")
    names = [n.strip() for n in out.splitlines() if n.strip()]
    udas = []
    for name in names:
        if name == "priority":
            continue
        label, _, _ = run_task("rc.verbose=nothing", "_get", f"rc.uda.{name}.label")
        typ, _, _ = run_task("rc.verbose=nothing", "_get", f"rc.uda.{name}.type")
        values, _, _ = run_task("rc.verbose=nothing", "_get", f"rc.uda.{name}.values")
        udas.append({
            "name": name,
            "label": label.strip() or name,
            "type": typ.strip() or "string",
            "values": [v for v in values.strip().split(",") if v],
        })
    return udas


def _read_contexts():
    out, _, _ = run_task("rc.verbose=nothing", "_context")
    names = [c.strip() for c in out.splitlines() if c.strip()]
    contexts = []
    for name in names:
        rd, _, _ = run_task("rc.verbose=nothing", "_get", f"rc.context.{name}.read")
        legacy, _, _ = run_task("rc.verbose=nothing", "_get", f"rc.context.{name}")
        contexts.append({"name": name, "filter": (rd.strip() or legacy.strip())})
    cur, _, _ = run_task("rc.verbose=nothing", "_get", "rc.context")
    active = cur.strip() or None
    return {"list": contexts, "active": active}


@app.route("/api/udas", methods=["GET"])
@login_required
def get_udas():
    return jsonify(_read_udas())


@app.route("/api/contexts", methods=["GET"])
@login_required
def get_contexts():
    return jsonify(_read_contexts())


@app.route("/api/contexts", methods=["POST"])
@login_required
def define_context():
    data = request.json or {}
    name = re.sub(r"[^A-Za-z0-9_]", "", data.get("name", ""))
    flt = (data.get("filter") or "").strip()
    if not name or not flt:
        return jsonify({"error": "name and filter required"}), 400
    args = ["rc.verbose=nothing", "rc.confirmation=no", "context", "define", name]
    args += shlex.split(flt)
    out, err, code = run_task(*args)
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    return jsonify({"message": f"Context '{name}' defined"})


@app.route("/api/contexts/<name>", methods=["DELETE"])
@login_required
def delete_context(name):
    name = re.sub(r"[^A-Za-z0-9_]", "", name)
    out, err, code = run_task("rc.verbose=nothing", "rc.confirmation=no",
                              "context", "delete", name)
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    return jsonify({"message": f"Context '{name}' deleted"})


@app.route("/api/context", methods=["POST"])
@login_required
def set_context():
    name = (request.json or {}).get("name", "").strip()
    if not name or name.lower() == "none":
        out, err, code = run_task("rc.verbose=nothing", "context", "none")
    else:
        name = re.sub(r"[^A-Za-z0-9_]", "", name)
        out, err, code = run_task("rc.verbose=nothing", "context", name)
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    return jsonify({"message": out.strip() or "Context set"})


# ──────────────────────────────────────────────────────────────────────────
# Reports / insights
# ──────────────────────────────────────────────────────────────────────────
@app.route("/api/reports/summary", methods=["GET"])
@login_required
def report_summary():
    rows = {}
    for t in export(["status:pending"]):
        p = t.get("project") or "(none)"
        rows.setdefault(p, {"pending": 0, "completed": 0})["pending"] += 1
    for t in export(["status:completed"]):
        p = t.get("project") or "(none)"
        rows.setdefault(p, {"pending": 0, "completed": 0})["completed"] += 1
    out = []
    for name, c in sorted(rows.items()):
        total = c["pending"] + c["completed"]
        pct = round(100 * c["completed"] / total) if total else 0
        out.append({"project": name, "pending": c["pending"],
                    "completed": c["completed"], "total": total, "pct": pct})
    return jsonify(out)


@app.route("/api/reports/burndown", methods=["GET"])
@login_required
def report_burndown():
    days = max(7, min(180, int(request.args.get("days", 30))))
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)

    added = {start + timedelta(days=i): 0 for i in range(days)}
    completed = dict(added)

    for t in export([]):
        e = _parse_tw_date(t.get("entry"))
        if e and start <= e.date() <= today:
            added[e.date()] += 1
        end = _parse_tw_date(t.get("end"))
        if end and t.get("status") == "completed" and start <= end.date() <= today:
            completed[end.date()] += 1

    series = [{
        "date": d.isoformat(),
        "added": added[d],
        "completed": completed[d],
    } for d in sorted(added.keys())]
    return jsonify(series)


# ──────────────────────────────────────────────────────────────────────────
# Profile (current user): timezone, password, Telegram sync
# ──────────────────────────────────────────────────────────────────────────
@app.route("/api/me", methods=["GET"])
@login_required
def me():
    u = current_user()
    return jsonify({
        "id": u["id"], "username": u["username"], "is_admin": bool(u["is_admin"]),
        "timezone": u["timezone"], "reminders": bool(u["reminders"]),
        "telegram_linked": u["telegram_id"] is not None,
    })


@app.route("/api/profile/timezone", methods=["POST"])
@login_required
def profile_timezone():
    tz = (request.json or {}).get("timezone", "").strip()
    try:
        users.set_timezone(current_user()["id"], tz)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    logger.info("User '%s' set timezone to %s", current_user()["username"], tz)
    return jsonify({"message": "Timezone updated", "timezone": tz})


@app.route("/api/profile/password", methods=["POST"])
@login_required
def profile_password():
    data = request.json or {}
    old, new = data.get("old_password", ""), data.get("new_password", "")
    u = current_user()
    if not users._verify_password(old, u["password_hash"]):
        return jsonify({"error": "Current password is incorrect"}), 403
    try:
        users.set_password(u["id"], new)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    logger.info("User '%s' changed their password", u["username"])
    return jsonify({"message": "Password updated"})


@app.route("/api/profile/telegram", methods=["GET"])
@login_required
def telegram_status():
    u = current_user()
    return jsonify({
        "linked": u["telegram_id"] is not None,
        "telegram_id": u["telegram_id"],
    })


@app.route("/api/profile/telegram/code", methods=["POST"])
@login_required
def telegram_code():
    code = users.create_short_code(KIND_TGLINK, user_id=current_user()["id"])
    logger.info("User '%s' generated a Telegram link code",
                current_user()["username"])
    return jsonify({"code": code, "ttl_seconds": users._TTL[KIND_TGLINK]})


@app.route("/api/profile/telegram/unlink", methods=["POST"])
@login_required
def telegram_unlink():
    users.unlink_telegram(current_user()["id"])
    logger.info("User '%s' unlinked their Telegram account",
                current_user()["username"])
    return jsonify({"message": "Telegram account unlinked"})


# ──────────────────────────────────────────────────────────────────────────
# Admin APIs
# ──────────────────────────────────────────────────────────────────────────
@app.route("/api/admin/users", methods=["GET"])
@admin_required
def admin_users():
    return jsonify(users.list_users())


@app.route("/api/admin/users/<int:user_id>/disable", methods=["POST"])
@admin_required
def admin_disable(user_id):
    target = users.get_user(user_id)
    if not target:
        return jsonify({"error": "User not found"}), 404
    if target["id"] == current_user()["id"]:
        return jsonify({"error": "You cannot disable yourself"}), 400
    if target["is_admin"]:
        return jsonify({"error": "You cannot disable an admin"}), 400
    users.set_disabled(user_id, True)
    logger.info("SYSTEM: admin '%s' disabled user '%s'",
                current_user()["username"], target["username"])
    return jsonify({"message": f"User '{target['username']}' disabled"})


@app.route("/api/admin/users/<int:user_id>/enable", methods=["POST"])
@admin_required
def admin_enable(user_id):
    target = users.get_user(user_id)
    if not target:
        return jsonify({"error": "User not found"}), 404
    users.set_disabled(user_id, False)
    logger.info("SYSTEM: admin '%s' enabled user '%s'",
                current_user()["username"], target["username"])
    return jsonify({"message": f"User '{target['username']}' enabled"})


@app.route("/api/admin/users/<int:user_id>/reset", methods=["POST"])
@admin_required
def admin_reset(user_id):
    target = users.get_user(user_id)
    if not target:
        return jsonify({"error": "User not found"}), 404
    token = users.create_token(KIND_RESET, user_id=user_id,
                               created_by=current_user()["id"])
    link = f"{external_base_url()}/reset/{token}"
    logger.info("SYSTEM: admin '%s' created a password reset link for user "
                "'%s'", current_user()["username"], target["username"])
    return jsonify({"link": link, "username": target["username"]})


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@admin_required
def admin_delete(user_id):
    target = users.get_user(user_id)
    if not target:
        return jsonify({"error": "User not found"}), 404
    if target["id"] == current_user()["id"]:
        return jsonify({"error": "You cannot delete yourself"}), 400
    if target["is_admin"]:
        return jsonify({"error": "You cannot delete an admin"}), 400
    import shutil
    users.delete_user(user_id)
    shutil.rmtree(store.user_data_dir(user_id), ignore_errors=True)
    logger.info("SYSTEM: admin '%s' deleted user '%s' and all their data",
                current_user()["username"], target["username"])
    return jsonify({"message": f"User '{target['username']}' and all their "
                               "data were deleted"})


@app.route("/api/admin/register-link", methods=["POST"])
@admin_required
def admin_register_link():
    token = users.create_token(KIND_REGISTER, created_by=current_user()["id"])
    link = f"{external_base_url()}/register/{token}"
    logger.info("SYSTEM: admin '%s' generated a member registration link",
                current_user()["username"])
    return jsonify({"link": link, "expires_in_days": 7})


@app.route("/api/admin/logs", methods=["GET"])
@admin_required
def admin_logs():
    lines = max(10, min(1000, int(request.args.get("lines", 200))))
    try:
        with open(SYSTEM_LOG, "r", encoding="utf-8", errors="replace") as f:
            content = f.readlines()[-lines:]
    except OSError:
        content = []
    return jsonify({"lines": "".join(content).splitlines()})


@app.route("/api/healthz")
def healthz():
    _, _, code = store.run_task("rc.verbose=nothing", "_version")
    return jsonify({"ok": code == 0})


# --------------------------------------------------------------------------
# Startup: create the one-time admin registration token when no admin exists
# and log a registration link for every enumerated IP address.
# --------------------------------------------------------------------------
def bootstrap_admin_token():
    users.init_db()
    users.cleanup_tokens()
    if users.admin_exists():
        return
    token = users.create_token(KIND_ADMIN_SETUP)
    logger.info("=" * 64)
    logger.info("SYSTEM: no admin account exists yet.")
    logger.info("SYSTEM: a one-time admin registration token was created "
                "(valid 24h, single use).")
    if DOMAIN:
        logger.info("SYSTEM: admin registration link: https://%s/register/%s",
                    DOMAIN, token)
    for ip in enumerate_ipv4():
        logger.info("SYSTEM: admin registration link via %s: "
                    "http://%s:%s/register/%s", ip, ip, WEB_PORT, token)
    logger.info("=" * 64)


bootstrap_admin_token()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(WEB_PORT), debug=False)
