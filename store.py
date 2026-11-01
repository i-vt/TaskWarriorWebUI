"""Low-level Taskwarrior access, parameterized per data directory.

Both the web app (app.py) and the Telegram bot (bot.py) operate on the real
`task` binary, but every user has an isolated database under
``/data/users/u<id>/.task``. All helpers in this module therefore take an
explicit ``data_dir``/``rc_path`` pair instead of relying on global state.

Everything user-specific (auth, tokens, timezone) lives in users.py.
"""
import json
import logging
import os
import re
import shlex
import subprocess
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DEFAULT_TASKDATA = os.environ.get("TASKDATA", "/data/.task")
DEFAULT_TASKRC = os.environ.get("TASKRC", os.path.join(DEFAULT_TASKDATA, ".taskrc"))

# Commands that must never be reachable through a user-supplied read filter.
_BLOCKED_FILTER_TOKENS = {
    "add", "modify", "delete", "done", "start", "stop", "purge", "undo",
    "edit", "import", "export", "log", "duplicate", "annotate", "denotate",
    "append", "prepend", "config", "execute", "context", "synchronize",
    "sync", "merge", "push", "pull", "diagnostics",
}

# Common rc flags injected on every call so behaviour is deterministic.
RC = ["rc.json.array=on", "rc.verbose=nothing", "rc.confirmation=no",
      "rc.recurrence=on", "rc.hooks=off"]

USERS_ROOT = os.environ.get("TASKWARRIOR_USERS_ROOT", "/data/users")


def ensure_taskrc(data_dir, rc_path=None):
    """Write a sane default .taskrc for ``data_dir`` if none exists."""
    rc_path = rc_path or os.path.join(data_dir, ".taskrc")
    if os.path.exists(rc_path):
        return rc_path
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(rc_path, "w") as f:
            f.write(
                f"data.location={data_dir}\n"
                "confirmation=no\n"
                "verbose=nothing\n"
                "recurrence=on\n"
                "gc=on\n"
                "json.array=on\n"
            )
    except OSError:
        # Read-only deployments/tests point TASKDATA somewhere unwritable;
        # taskwarrior itself will surface any real problem.
        pass
    return rc_path


def users_root():
    return os.environ.get("TASKWARRIOR_USERS_ROOT", USERS_ROOT)


def user_data_dir(user_id):
    """Isolated task database directory for a user id (int or str)."""
    uid = re.sub(r"[^0-9a-zA-Z]", "", str(user_id))
    return os.path.join(users_root(), f"u{uid}")


def run_task(*args, data_dir=None, rc_path=None, input_data=None):
    data_dir = data_dir or DEFAULT_TASKDATA
    rc_path = rc_path or os.path.join(data_dir, ".taskrc")
    env = os.environ.copy()
    env["TASKDATA"] = data_dir
    env["TASKRC"] = rc_path
    cmd = ["task"] + [str(a) for a in args]
    result = subprocess.run(
        cmd, capture_output=True, text=True, env=env, input=input_data
    )
    return result.stdout, result.stderr, result.returncode


def export(filter_tokens=None, data_dir=None, rc_path=None):
    """Return a list of task dicts for the given filter."""
    args = list(RC)
    if filter_tokens:
        args += list(filter_tokens)
    args.append("export")
    out, err, code = run_task(*args, data_dir=data_dir, rc_path=rc_path)
    out = out.strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        logger.warning("task export returned non-JSON output (code=%s): %s",
                       code, out[:200])
        return []


def sanitize_filter(raw):
    """Split a raw taskwarrior filter string into safe argv tokens.

    Strips anything that could turn a read into a mutating command or
    override our rc flags. This is defence-in-depth: the UI binds filters
    to taskwarrior, so a stray report filter should never delete data.
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


def latest(data_dir=None, rc_path=None):
    """The most recently added/modified task (uses the +LATEST virtual tag)."""
    tasks = export(["+LATEST"], data_dir=data_dir, rc_path=rc_path)
    return tasks[0] if tasks else None


def uuid_filter(uuid):
    # Always address tasks by uuid so the reference survives status changes
    # (deleted tasks lose their short id, which breaks numeric filters).
    return [f"uuid:{uuid}"]


# --------------------------------------------------------------------------
# Argument builders shared by add / modify
# --------------------------------------------------------------------------
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

    keys = ("project", "priority", "recur", *_DATE_ATTRS)
    if creating:
        for k in keys:
            if data.get(k):
                emit(k, data[k])
    else:
        for k in keys:
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


def add_task(data, data_dir=None, rc_path=None):
    """Add a task from a payload dict. Returns (task_dict_or_None, error)."""
    description = (data.get("description") or "").strip()
    if not description:
        return None, "Description required"

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
    out, err, code = run_task(*args, data_dir=data_dir, rc_path=rc_path)
    if code != 0:
        return None, (err.strip() or out.strip() or "task add failed")

    created = latest(data_dir=data_dir, rc_path=rc_path)

    # Annotations cannot be supplied to `add`; attach them to the new task.
    annos = [a for a in (data.get("annotations") or []) if str(a).strip()]
    if created and annos:
        for a in annos:
            run_task(*RC, f"uuid:{created['uuid']}", "annotate", str(a),
                     data_dir=data_dir, rc_path=rc_path)
        refreshed = export(uuid_filter(created["uuid"]),
                           data_dir=data_dir, rc_path=rc_path)
        created = refreshed[0] if refreshed else created

    return created, None


def parse_tw_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def now_iso_compact():
    # Taskwarrior stores dates as YYYYMMDDTHHMMSSZ (no separators).
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
