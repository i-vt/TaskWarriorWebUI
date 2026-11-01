"""User accounts, registration/reset tokens, timezones and Telegram links.

Backed by SQLite on the shared /data volume so the web app and the Telegram
bot see the same accounts. Passwords are hashed with PBKDF2-HMAC-SHA256.
Tokens are URL-safe random strings with a kind, expiry, and use-count.
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

def app_root():
    return os.environ.get("TASKWARRIOR_APP_ROOT", "/data/app")


def db_path():
    return os.environ.get("USERS_DB", os.path.join(app_root(), "users.db"))

_PBKDF2_ITERATIONS = 200_000

# Token kinds
KIND_ADMIN_SETUP = "admin_setup"   # one-time, creates the first admin
KIND_REGISTER = "register"         # one-time, creates a member account
KIND_RESET = "reset"               # one-time, lets a user set a new password
KIND_TGLINK = "tglink"             # short code, typed into the bot via /link

_TTL = {
    KIND_ADMIN_SETUP: 24 * 3600,
    KIND_REGISTER: 7 * 24 * 3600,
    KIND_RESET: 3600,
    KIND_TGLINK: 15 * 60,
}

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")


def _hash_password(password, salt=None, iterations=_PBKDF2_ITERATIONS):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), iterations)
    return f"pbkdf2${iterations}${salt}${dk.hex()}"


def _verify_password(password, stored):
    try:
        _, iters, salt, _ = stored.split("$")
        candidate = _hash_password(password, salt=salt, iterations=int(iters))
        return hmac.compare_digest(candidate.encode(), stored.encode())
    except (ValueError, AttributeError):
        return False


@contextmanager
def db():
    os.makedirs(os.path.dirname(db_path()), exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(db_path()), exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                is_admin      INTEGER NOT NULL DEFAULT 0,
                disabled      INTEGER NOT NULL DEFAULT 0,
                timezone      TEXT NOT NULL DEFAULT 'UTC',
                telegram_id   INTEGER UNIQUE,
                telegram_chat INTEGER,
                reminders     INTEGER NOT NULL DEFAULT 1,
                created_at    REAL NOT NULL,
                last_login    REAL
            );
            CREATE TABLE IF NOT EXISTS tokens (
                token      TEXT PRIMARY KEY,
                kind       TEXT NOT NULL,
                user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
                created_by INTEGER,
                expires_at REAL NOT NULL,
                used_at    REAL,
                max_uses   INTEGER NOT NULL DEFAULT 1,
                use_count  INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS reminders (
                user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                task_uuid TEXT NOT NULL,
                kind      TEXT NOT NULL,          -- 'soon' | 'overdue'
                sent_at   REAL NOT NULL,
                PRIMARY KEY (user_id, task_uuid, kind)
            );
            CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id);
            """
        )


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
def create_user(username, password, *, is_admin=False, timezone="UTC",
                telegram_id=None, telegram_chat=None):
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise ValueError("Username must be 2-32 chars: letters, digits, _ . -")
    if not password or len(password) < 6:
        raise ValueError("Password must be at least 6 characters")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(timezone)
    except Exception:
        timezone = "UTC"
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, timezone,"
            " telegram_id, telegram_chat, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (username, _hash_password(password), int(is_admin), timezone,
             telegram_id, telegram_chat, time.time()))
        return cur.lastrowid


def get_user(user_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_username(username):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?",
                           ((username or "").strip(),)).fetchone()
        return dict(row) if row else None


def get_user_by_telegram(telegram_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE telegram_id=?",
                           (telegram_id,)).fetchone()
        return dict(row) if row else None


def list_users():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, username, is_admin, disabled, timezone, telegram_id,"
            " reminders, created_at, last_login FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def admin_exists():
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE is_admin=1").fetchone()
        return row["c"] > 0


def verify_login(username, password):
    user = get_user_by_username(username)
    if not user:
        return None
    if user["disabled"]:
        return "disabled"
    if not _verify_password(password or "", user["password_hash"]):
        return None
    with db() as conn:
        conn.execute("UPDATE users SET last_login=? WHERE id=?",
                     (time.time(), user["id"]))
    return user


def set_password(user_id, password):
    if not password or len(password) < 6:
        raise ValueError("Password must be at least 6 characters")
    with db() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                     (_hash_password(password), user_id))


def set_disabled(user_id, disabled):
    with db() as conn:
        conn.execute("UPDATE users SET disabled=? WHERE id=?",
                     (int(disabled), user_id))


def set_timezone(user_id, timezone):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown timezone: {timezone}")
    with db() as conn:
        conn.execute("UPDATE users SET timezone=? WHERE id=?",
                     (timezone, user_id))


def set_reminders(user_id, enabled):
    with db() as conn:
        conn.execute("UPDATE users SET reminders=? WHERE id=?",
                     (int(enabled), user_id))


def delete_user(user_id):
    with db() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))


def count_users():
    with db() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


# --------------------------------------------------------------------------
# Telegram account sync
# --------------------------------------------------------------------------
def link_telegram(user_id, telegram_id, chat_id):
    """Attach a Telegram identity to a web account.

    Raises ValueError on conflicts (already linked elsewhere).
    """
    telegram_id = int(telegram_id)
    chat_id = int(chat_id) if chat_id else telegram_id
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
        ).fetchone()
        if existing and existing["id"] != user_id:
            raise ValueError("This Telegram account is already linked"
                             " to another user")
        conn.execute(
            "UPDATE users SET telegram_id=?, telegram_chat=? WHERE id=?",
            (telegram_id, chat_id, user_id))
        # Drop any unlinked shadow row previously auto-created for this
        # telegram id (kept rows tidy; its tasks are merged by the caller).
        conn.execute(
            "DELETE FROM users WHERE telegram_id IS NULL AND"
            " username=? AND id != ?",
            (f"tg_{telegram_id}", user_id))


def unlink_telegram(user_id):
    with db() as conn:
        conn.execute("UPDATE users SET telegram_id=NULL, telegram_chat=NULL"
                     " WHERE id=?", (user_id,))


# --------------------------------------------------------------------------
# Tokens (registration / reset / telegram link codes)
# --------------------------------------------------------------------------
def create_token(kind, *, user_id=None, created_by=None, ttl=None):
    if kind not in _TTL:
        raise ValueError(f"Unknown token kind: {kind}")
    token = secrets.token_urlsafe(24)
    with db() as conn:
        conn.execute(
            "INSERT INTO tokens (token, kind, user_id, created_by, expires_at)"
            " VALUES (?,?,?,?,?)",
            (token, kind, user_id, created_by,
             time.time() + (ttl or _TTL[kind])))
    return token


def create_short_code(kind, *, user_id=None, ttl=None):
    """Human-typable 8-char code for /link in Telegram."""
    if kind not in _TTL:
        raise ValueError(f"Unknown token kind: {kind}")
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no ambiguous chars
    for _ in range(10):
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        with db() as conn:
            try:
                conn.execute(
                    "INSERT INTO tokens (token, kind, user_id, expires_at)"
                    " VALUES (?,?,?,?)",
                    (code, kind, user_id, time.time() + (ttl or _TTL[kind])))
                return code
            except sqlite3.IntegrityError:
                continue
    raise RuntimeError("could not generate unique code")


def peek_token(token, kind=None):
    """Return the token row if valid (not expired, not exhausted), else None."""
    with db() as conn:
        row = conn.execute("SELECT * FROM tokens WHERE token=?",
                           ((token or "").strip(),)).fetchone()
        if not row:
            return None
        row = dict(row)
        if kind and row["kind"] != kind:
            return None
        if row["used_at"] or time.time() > row["expires_at"]:
            return None
        if row["use_count"] >= row["max_uses"]:
            return None
        return row


def consume_token(token, kind=None):
    """Validate + mark a token as used. Returns the row or None."""
    row = peek_token(token, kind)
    if not row:
        return None
    with db() as conn:
        conn.execute(
            "UPDATE tokens SET use_count=use_count+1, used_at=?"
            " WHERE token=? AND use_count < max_uses AND used_at IS NULL",
            (time.time(), row["token"]))
    return row


def cleanup_tokens():
    with db() as conn:
        conn.execute("DELETE FROM tokens WHERE expires_at < ?",
                     (time.time() - 86400,))


# --------------------------------------------------------------------------
# Reminder bookkeeping
# --------------------------------------------------------------------------
def reminder_sent(user_id, task_uuid, kind):
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM reminders WHERE user_id=? AND task_uuid=? AND kind=?",
            (user_id, task_uuid, kind)).fetchone()
        return row is not None


def mark_reminder_sent(user_id, task_uuid, kind):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO reminders (user_id, task_uuid, kind, sent_at)"
            " VALUES (?,?,?,?)",
            (user_id, task_uuid, kind, time.time()))


def purge_reminders_for(user_id):
    with db() as conn:
        conn.execute("DELETE FROM reminders WHERE user_id=?", (user_id,))
