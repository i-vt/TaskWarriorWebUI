import hmac
import logging
import os
import re
import json
import shlex
import subprocess
import time
import uuid
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request, render_template, Response, session, redirect, url_for

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

TASK_DATA_DIR = os.environ.get("TASKDATA", "/data/.task")
TASK_RC = os.environ.get("TASKRC", os.path.join(TASK_DATA_DIR, ".taskrc"))
try:
    os.makedirs(TASK_DATA_DIR, exist_ok=True)
except OSError:
    # Tests and read-only deployments may point TASKDATA somewhere
    # unwritable; taskwarrior itself will surface any real problem.
    pass


def _ensure_taskrc():
    """Write a sane default config if none exists.

    A runtime bind-mount can shadow any config baked into the image, so the
    container must be able to create its own .taskrc on first start. We still
    pass rc.* flags per-invocation, but a real file avoids first-run prompts
    and lets recurrence generation happen on reads.
    """
    if os.path.exists(TASK_RC):
        return
    try:
        with open(TASK_RC, "w") as f:
            f.write(
                f"data.location={TASK_DATA_DIR}\n"
                "confirmation=no\n"
                "verbose=nothing\n"
                "recurrence=on\n"
                "gc=on\n"
                "json.array=on\n"
            )
    except OSError:
        pass


_ensure_taskrc()


# --------------------------------------------------------------------------
# Auth key: one 32-char key shared by every gunicorn worker, persisted to
# disk so operators can retrieve it from inside the container.
# --------------------------------------------------------------------------

# Env-overridable so tests and odd deployments can relocate the key file.
KEY_PATH = os.environ.get("TASKWARRIOR_WEBUI_KEY_PATH",
                          "/tmp/TaskWarriorWebUIKey.txt")


def generate_key():
    """Return a fresh 32-char lowercase hex key."""
    return uuid.uuid4().hex


def _read_key_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def _log_key_io_error(action, path, exc):
    # Full diagnostics for operators, but never the key value itself.
    parent = os.path.dirname(os.path.abspath(path))
    try:
        parent_perms = oct(os.stat(parent).st_mode & 0o777)
    except OSError:
        parent_perms = "<unavailable>"
    logger.error(
        "Failed to %s auth key file (path=%s, errno=%s, error=%s, "
        "parent dir=%s, parent perms=%s)",
        action, path, getattr(exc, "errno", None), exc, parent, parent_perms)


def _log_key_source(source, path):
    logger.info("Auth key source: %s (file: %s)", source, path)
    logger.info("Retrieve the key with: docker exec taskwarrior-webui cat %s",
                path)


def _write_key_file(path, key):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key + "\n")
    finally:
        # Make sure the perms are 0600 even if the file already existed.
        os.chmod(path, 0o600)


def _read_winner_key(path, timeout=5.0, interval=0.1):
    """Another worker won the O_EXCL race; wait for and read its key."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            existing = _read_key_file(path)
        except OSError as exc:
            _log_key_io_error("read", path, exc)
            raise
        if len(existing) == 32:
            _log_key_source("key file created by another worker", path)
            return existing
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
    logger.error(
        "Key file %s was created by another worker but no 32-char key "
        "appeared within %.1fs", path, timeout)
    raise RuntimeError(f"could not read a valid auth key from {path}")


def load_or_create_key(path=KEY_PATH):
    """Return the shared auth key, creating the key file if needed.

    Order of preference:
    1. TASKWARRIOR_WEBUI_KEY env var (still written to the file).
    2. An existing key file containing a 32-char key.
    3. A freshly generated key, written with O_EXCL so concurrent gunicorn
       workers cannot clobber each other; the loser of the race reads the
       winner's key.

    A panel without its key file is unusable, so any OSError here is logged
    with full diagnostics and re-raised.
    """
    env_key = os.environ.get("TASKWARRIOR_WEBUI_KEY", "").strip()
    if env_key:
        if len(env_key) != 32:
            logger.warning(
                "TASKWARRIOR_WEBUI_KEY is %d characters, expected 32; "
                "accepting it anyway", len(env_key))
        try:
            _write_key_file(path, env_key)
        except OSError as exc:
            _log_key_io_error("write", path, exc)
            raise
        _log_key_source("TASKWARRIOR_WEBUI_KEY env var", path)
        return env_key

    if os.path.exists(path):
        try:
            existing = _read_key_file(path)
        except OSError as exc:
            _log_key_io_error("read", path, exc)
            raise
        if len(existing) == 32:
            _log_key_source("reused existing key file", path)
            return existing
        logger.warning(
            "Key file %s does not contain a 32-char key; replacing it", path)
        try:
            os.remove(path)
        except OSError as exc:
            _log_key_io_error("remove", path, exc)
            raise

    key = generate_key()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_winner_key(path)
    except OSError as exc:
        _log_key_io_error("create", path, exc)
        raise
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key + "\n")
    except OSError as exc:
        _log_key_io_error("write", path, exc)
        raise
    _log_key_source("generated new key", path)
    return key


AUTH_KEY = load_or_create_key()
app.secret_key = AUTH_KEY

# Commands that must never be reachable through a user-supplied read filter.
_BLOCKED_FILTER_TOKENS = {
    "add", "modify", "delete", "done", "start", "stop", "purge", "undo",
    "edit", "import", "export", "log", "duplicate", "annotate", "denotate",
    "append", "prepend", "config", "execute", "context", "synchronize",
    "sync", "merge", "push", "pull", "import", "diagnostics",
}


# ──────────────────────────────────────────────────────────────────────────
# Low level taskwarrior invocation
# ──────────────────────────────────────────────────────────────────────────
def run_task(*args, input_data=None):
    env = os.environ.copy()
    env["TASKDATA"] = TASK_DATA_DIR
    env["TASKRC"] = TASK_RC
    cmd = ["task"] + [str(a) for a in args]
    result = subprocess.run(
        cmd, capture_output=True, text=True, env=env, input=input_data
    )
    return result.stdout, result.stderr, result.returncode


# Common rc flags injected on every call so behaviour is deterministic.
RC = ["rc.json.array=on", "rc.verbose=nothing", "rc.confirmation=no",
      "rc.recurrence=on", "rc.hooks=off"]


def export(filter_tokens=None):
    """Return a list of task dicts for the given filter."""
    args = list(RC)
    if filter_tokens:
        args += list(filter_tokens)
    args.append("export")
    out, err, code = run_task(*args)
    out = out.strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def sanitize_filter(raw):
    """Split a raw taskwarrior filter string into safe argv tokens.

    Strips anything that could turn a read into a mutating command or
    override our rc flags. This is defence-in-depth: the UI is intended to
    bind to localhost, but a stray report filter should never delete data.
    """
    if not raw:
        return []
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    safe = []
    for t in tokens:
        low = t.lower()
        if low in _BLOCKED_FILTER_TOKENS:
            continue
        if low.startswith("rc.") or low.startswith("rc:"):
            continue
        safe.append(t)
    return safe


def latest():
    """The most recently added/modified task (uses the +LATEST virtual tag)."""
    tasks = export(["+LATEST"])
    return tasks[0] if tasks else None


def uuid_filter(uuid):
    # Always address tasks by uuid so the reference survives status changes
    # (deleted tasks lose their short id, which breaks numeric filters).
    return [f"uuid:{uuid}"]


# ──────────────────────────────────────────────────────────────────────────
# Argument builders shared by add / modify
# ──────────────────────────────────────────────────────────────────────────
_DATE_ATTRS = ("due", "scheduled", "wait", "until")
_KNOWN_ATTRS = {"description", "project", "priority", "recur"} | set(_DATE_ATTRS)


def build_set_args(data, *, creating):
    """Build `name:value` modifier tokens from a payload dict.

    `creating` distinguishes `add` (only emit keys that are present and
    truthy) from `modify` (emit a key whenever it is present, allowing
    `""` to clear an attribute).
    """
    args = []

    def emit(key, value):
        if value is None:
            return
        value = str(value).strip()
        if creating and value == "":
            return
        args.append(f"{key}:{value}")

    if creating:
        for k in ("project", "priority", "recur", *_DATE_ATTRS):
            if data.get(k):
                emit(k, data[k])
    else:
        for k in ("project", "priority", "recur", *_DATE_ATTRS):
            if k in data:
                emit(k, data[k])

    # User Defined Attributes (arbitrary name:value pairs)
    udas = data.get("uda") or {}
    if isinstance(udas, dict):
        for name, value in udas.items():
            name = re.sub(r"[^A-Za-z0-9_]", "", str(name))
            if not name:
                continue
            if creating and (value is None or str(value).strip() == ""):
                continue
            emit(name, "" if value is None else value)

    return args


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
def _key_matches(candidate):
    if not candidate:
        return False
    # Encode both sides so non-ASCII input cannot raise inside compare_digest.
    return hmac.compare_digest(candidate.strip().encode("utf-8"),
                               AUTH_KEY.encode("utf-8"))


def _is_authenticated():
    if session.get("authenticated") is True:
        return True
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and _key_matches(auth_header[7:]):
        return True
    if _key_matches(request.headers.get("X-Api-Key", "")):
        return True
    return False


@app.before_request
def require_auth():
    # Everything requires auth except the login page, static assets and the
    # health probe.
    path = request.path
    if path == "/login" or path == "/api/healthz" or path.startswith("/static/"):
        return None
    if _is_authenticated():
        return None
    if path.startswith("/api/"):
        return jsonify({"error": "Unauthorized"}), 401
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.is_json:
            submitted = (request.get_json(silent=True) or {}).get("key", "")
        else:
            submitted = request.form.get("key", "")
        if _key_matches(submitted or ""):
            session["authenticated"] = True
            return redirect(url_for("index"))
        logger.warning("Failed login attempt from %s", request.remote_addr)
        return render_template("login.html", error="Invalid key"), 401
    return render_template("login.html", error=None)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ──────────────────────────────────────────────────────────────────────────
# Page
# ──────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


# ──────────────────────────────────────────────────────────────────────────
# Task collection
# ──────────────────────────────────────────────────────────────────────────
@app.route("/api/tasks", methods=["GET"])
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
def add_task():
    data = request.json or {}
    description = (data.get("description") or "").strip()
    if not description:
        return jsonify({"error": "Description required"}), 400

    args = list(RC) + ["add"]
    args += build_set_args(data, creating=True)

    for t in data.get("tags", []) or []:
        t = str(t).strip()
        if t:
            args.append(f"+{t}")

    deps = [d for d in (data.get("depends") or []) if str(d).strip()]
    if deps:
        args.append("depends:" + ",".join(str(d).strip() for d in deps))

    args.append(description)
    out, err, code = run_task(*args)
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500

    created = latest()

    # Annotations cannot be supplied to `add`; attach them to the new task.
    annos = [a for a in (data.get("annotations") or []) if str(a).strip()]
    if created and annos:
        for a in annos:
            run_task(*RC, f"uuid:{created['uuid']}", "annotate", str(a))
        created = export(uuid_filter(created["uuid"]))
        created = created[0] if created else None

    return jsonify(created or {"message": "added"}), 201


@app.route("/api/tasks/log", methods=["POST"])
def log_task():
    """Record an already-completed task (taskwarrior `log`)."""
    data = request.json or {}
    description = (data.get("description") or "").strip()
    if not description:
        return jsonify({"error": "Description required"}), 400
    args = list(RC) + ["log"]
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
def get_task(uuid):
    tasks = export(uuid_filter(uuid))
    if not tasks:
        return jsonify({"error": "Not found"}), 404
    task = tasks[0]

    # Resolve dependency uuids to lightweight {uuid, id, description, status}.
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
def modify_task(uuid):
    data = request.json or {}
    args = list(RC) + uuid_filter(uuid) + ["modify"]

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
    out, err, code = run_task(*RC, *uuid_filter(uuid), verb)
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    return jsonify({"message": ok_msg})


@app.route("/api/tasks/<uuid>/done", methods=["POST"])
def complete_task(uuid):
    return _simple_action(uuid, "done", "Task completed")


@app.route("/api/tasks/<uuid>/start", methods=["POST"])
def start_task(uuid):
    return _simple_action(uuid, "start", "Task started")


@app.route("/api/tasks/<uuid>/stop", methods=["POST"])
def stop_task(uuid):
    return _simple_action(uuid, "stop", "Task stopped")


@app.route("/api/tasks/<uuid>", methods=["DELETE"])
def delete_task(uuid):
    return _simple_action(uuid, "delete", "Task deleted")


@app.route("/api/tasks/<uuid>/purge", methods=["POST"])
def purge_task(uuid):
    """Permanently remove a task. Taskwarrior only allows purging a task
    that is already deleted, and only when addressed by uuid."""
    tasks = export(uuid_filter(uuid))
    if tasks and tasks[0].get("status") != "deleted":
        run_task(*RC, *uuid_filter(uuid), "delete")
    out, err, code = run_task(*RC, *uuid_filter(uuid), "purge")
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    return jsonify({"message": "Task purged"})


@app.route("/api/tasks/<uuid>/duplicate", methods=["POST"])
def duplicate_task(uuid):
    out, err, code = run_task(*RC, *uuid_filter(uuid), "duplicate")
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    return jsonify(latest() or {"message": "duplicated"}), 201


@app.route("/api/tasks/<uuid>/annotate", methods=["POST"])
def annotate_task(uuid):
    text = (request.json or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "Annotation text required"}), 400
    out, err, code = run_task(*RC, *uuid_filter(uuid), "annotate", text)
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    task = export(uuid_filter(uuid))
    return jsonify(task[0] if task else {"message": "annotated"})


@app.route("/api/tasks/<uuid>/denotate", methods=["POST"])
def denotate_task(uuid):
    text = (request.json or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "Annotation text required"}), 400
    out, err, code = run_task(*RC, *uuid_filter(uuid), "denotate", text)
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    task = export(uuid_filter(uuid))
    return jsonify(task[0] if task else {"message": "denotated"})


# ──────────────────────────────────────────────────────────────────────────
# Global operations
# ──────────────────────────────────────────────────────────────────────────
@app.route("/api/undo", methods=["POST"])
def undo():
    out, err, code = run_task("rc.confirmation=no", "rc.verbose=nothing", "undo")
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    return jsonify({"message": "Last change undone"})


@app.route("/api/export", methods=["GET"])
def export_all():
    out, err, code = run_task("rc.json.array=on", "rc.verbose=nothing", "export")
    return Response(
        out or "[]",
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=tasks-export.json"},
    )


@app.route("/api/import", methods=["POST"])
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
# Metadata: projects, tags, stats, udas, contexts, reports
# ──────────────────────────────────────────────────────────────────────────
def _now_iso_compact():
    # Taskwarrior stores dates as YYYYMMDDTHHMMSSZ (no separators).
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@app.route("/api/projects", methods=["GET"])
def list_projects():
    out, _, _ = run_task("rc.verbose=nothing", "_projects")
    return jsonify([p.strip() for p in out.splitlines() if p.strip()])


@app.route("/api/tags", methods=["GET"])
def list_tags():
    out, _, _ = run_task("rc.verbose=nothing", "_tags")
    skip = {"next", "nocolor", "nocal", "nonag"}
    return jsonify([t.strip() for t in out.splitlines()
                    if t.strip() and t.strip() not in skip])


@app.route("/api/stats", methods=["GET"])
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
    # Keep "" (no priority) handling on the client; expose only real values.
    return [v for v in vals if v]


def _read_udas():
    """Discover configured UDAs (name, label, type, allowed values)."""
    out, _, _ = run_task("rc.verbose=nothing", "_udas")
    names = [n.strip() for n in out.splitlines() if n.strip()]
    udas = []
    for name in names:
        if name == "priority":
            continue  # built-in, surfaced as its own control
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
def get_udas():
    return jsonify(_read_udas())


@app.route("/api/contexts", methods=["GET"])
def get_contexts():
    return jsonify(_read_contexts())


@app.route("/api/contexts", methods=["POST"])
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
def delete_context(name):
    name = re.sub(r"[^A-Za-z0-9_]", "", name)
    out, err, code = run_task("rc.verbose=nothing", "rc.confirmation=no",
                              "context", "delete", name)
    if code != 0:
        return jsonify({"error": err.strip() or out.strip()}), 500
    return jsonify({"message": f"Context '{name}' deleted"})


@app.route("/api/context", methods=["POST"])
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
def report_summary():
    """Per-project progress (pending vs completed)."""
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


def _parse_tw_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@app.route("/api/reports/burndown", methods=["GET"])
def report_burndown():
    """Daily added vs completed counts over the last N days."""
    days = max(7, min(180, int(request.args.get("days", 30))))
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)

    added = {start + timedelta(days=i): 0 for i in range(days)}
    completed = dict(added)

    for t in export([]):  # everything
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


@app.route("/api/healthz")
def healthz():
    _, _, code = run_task("rc.verbose=nothing", "_version")
    return jsonify({"ok": code == 0})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=62304, debug=False)
