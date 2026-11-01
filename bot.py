"""Telegram bot interface for TaskWarrior WebUI.

- Every Telegram user gets an individual task database (a shadow account
  named tg_<telegram_id>) until they link their WebUI account with
  `/link CODE` (codes are generated on the WebUI profile page).
- Supports task CRUD, timezone setting and due-soon/overdue reminders
  delivered in the user's own timezone.
"""
import asyncio
import logging
import logging.handlers
import os
import random
import re
import string
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import store
import users
from users import KIND_TGLINK

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

APP_ROOT = os.environ.get("TASKWARRIOR_APP_ROOT", "/data/app")
os.makedirs(APP_ROOT, exist_ok=True)
try:
    _fh = logging.handlers.RotatingFileHandler(
        os.path.join(APP_ROOT, "system.log"), maxBytes=2_000_000, backupCount=3)
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(_fh)
except OSError:
    pass

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
REMINDER_INTERVAL = int(os.environ.get("TELEGRAM_REMINDER_INTERVAL", "60"))
DUE_SOON_MINUTES = int(os.environ.get("TELEGRAM_DUE_SOON_MINUTES", "60"))

_help_text = (
    "TaskWarrior bot commands:\n"
    "  /add <task> [due:YYYY-MM-DD] [project:X] [+tag] — add a task\n"
    "  /list [N] — pending tasks (default 15)\n"
    "  /today — tasks due today (your timezone)\n"
    "  /done <id> — mark task done\n"
    "  /del <id> — delete a task\n"
    "  /begin <id> /stop <id> — start/stop tracking\n"
    "  /tz <Zone> — set timezone, e.g. /tz Europe/Berlin\n"
    "  /remind on|off — toggle due reminders\n"
    "  /link CODE — sync with your WebUI account (code from the profile page)\n"
    "  /unlink — disconnect your WebUI account\n"
    "  /whoami — show account info\n"
)


# --------------------------------------------------------------------------
# Account resolution: one shadow account per Telegram identity
# --------------------------------------------------------------------------
def _random_password(length=20):
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def resolve_user(telegram_id, chat_id=None, username=None):
    """Return the users row for this Telegram identity, creating a shadow
    account (with its own task database) on first contact."""
    u = users.get_user_by_telegram(telegram_id)
    if u:
        if chat_id and u.get("telegram_chat") != chat_id:
            users.link_telegram(u["id"], telegram_id, chat_id)
            u["telegram_chat"] = chat_id
        return u
    name = f"tg_{telegram_id}"
    try:
        uid = users.create_user(name, _random_password(),
                                telegram_id=telegram_id,
                                telegram_chat=chat_id or telegram_id)
    except ValueError:
        # Extremely unlikely collision; reuse the existing shadow row.
        u = users.get_user_by_username(name)
        if u:
            users.link_telegram(u["id"], telegram_id, chat_id or telegram_id)
            return users.get_user_by_telegram(telegram_id)
        raise
    d = store.user_data_dir(uid)
    store.ensure_taskrc(d)
    logger.info("Telegram: created shadow account '%s' (id=%s)", name, uid)
    return users.get_user(uid)


def task_env_for(u):
    d = store.user_data_dir(u["id"])
    store.ensure_taskrc(d)
    return d


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _safe_tokens(raw_args):
    """Reject tokens that could mutate rc state or invoke other commands."""
    blocked = store._BLOCKED_FILTER_TOKENS | {"undo", "purge", "import",
                                              "export", "duplicate", "config"}
    safe = []
    for tok in raw_args:
        low = tok.lower()
        if low.startswith("rc.") or low.startswith("rc:") or low in blocked:
            return None, tok
        safe.append(tok)
    return safe, None


def _fmt_due(due, tzname):
    dt = store.parse_tw_date(due)
    if not dt:
        return ""
    local = dt.astimezone(ZoneInfo(tzname))
    now = datetime.now(timezone.utc).astimezone(ZoneInfo(tzname))
    if local.date() == now.date():
        return " due " + local.strftime("%H:%M")
    return " due " + local.strftime("%b %d %H:%M")


def _tasks_lines(tasks, tzname, limit=15):
    lines = []
    for t in tasks[:limit]:
        bits = [f"#{t.get('id')} {t.get('description', '')}"]
        if t.get("project"):
            bits.append(f"[{t['project']}]")
        if t.get("priority"):
            bits.append(f"({t['priority']})")
        due = _fmt_due(t.get("due"), tzname)
        if due:
            bits.append(due)
        if t.get("start"):
            bits.append("▶")
        lines.append(" ".join(bits))
    if len(tasks) > limit:
        lines.append(f"… and {len(tasks) - limit} more")
    return lines or ["(no tasks)"]


def _to_thread(fn, *args, **kwargs):
    return asyncio.to_thread(fn, *args, **kwargs)


# --------------------------------------------------------------------------
# Command handlers
# --------------------------------------------------------------------------
async def cmd_start(update, context):
    u = resolve_user(update.effective_user.id, update.effective_chat.id)
    linked = users.get_user_by_telegram(update.effective_user.id)
    name = (linked["username"] if linked else u["username"])
    await update.message.reply_text(
        f"Welcome, {name}! Your tasks live in your personal database.\n"
        f"Sync them with the WebUI: generate a code on the profile page and "
        f"send /link CODE here.\n\n" + _help_text)


async def cmd_begin(update, context):
    await _id_action(update, context, "start", "started")


async def cmd_help(update, context):
    await update.message.reply_text(_help_text)


async def cmd_add(update, context):
    u = resolve_user(update.effective_user.id, update.effective_chat.id)
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /add <description> "
                                        "[due:2026-12-01] [project:X] [+tag]")
        return
    safe, bad = _safe_tokens(args)
    if safe is None:
        await update.message.reply_text(f"Rejected token: {bad}")
        return
    desc = " ".join(safe).strip()
    if not desc:
        await update.message.reply_text("Usage: /add <description> …")
        return
    def _do():
        data = {"description": desc}
        return store.add_task(data, data_dir=task_env_for(u))
    created, err = await _to_thread(_do)
    if err:
        await update.message.reply_text(f"Error: {err}")
        return
    tid = created.get("id") if created else "?"
    await update.message.reply_text(f"Added task #{tid}: {desc}")


async def cmd_list(update, context):
    u = resolve_user(update.effective_user.id, update.effective_chat.id)
    limit = 15
    for a in context.args:
        if a.isdigit():
            limit = max(1, min(50, int(a)))
    def _do():
        tasks = store.export(["status:pending"], data_dir=task_env_for(u))
        tasks.sort(key=lambda t: float(t.get("urgency", 0) or 0), reverse=True)
        return tasks
    tasks = await _to_thread(_do)
    await update.message.reply_text(
        "\n".join(_tasks_lines(tasks, u["timezone"], limit)))


async def cmd_today(update, context):
    u = resolve_user(update.effective_user.id, update.effective_chat.id)
    def _do():
        tasks = store.export(["status:pending"], data_dir=task_env_for(u))
        tz = ZoneInfo(u["timezone"])
        today = datetime.now(timezone.utc).astimezone(tz).date()
        out = []
        for t in tasks:
            dt = store.parse_tw_date(t.get("due"))
            if dt and dt.astimezone(tz).date() == today:
                out.append(t)
        out.sort(key=lambda t: t.get("due") or "")
        return out
    tasks = await _to_thread(_do)
    header = f"Tasks due today ({u['timezone']}):"
    await update.message.reply_text(
        header + "\n" + "\n".join(_tasks_lines(tasks, u["timezone"], 30)))


async def _id_action(update, context, verb, past):
    u = resolve_user(update.effective_user.id, update.effective_chat.id)
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(f"Usage: /{verb} <task id>")
        return
    tid = context.args[0]
    def _do():
        return store.run_task(*store.RC, tid, verb, data_dir=task_env_for(u))
    out, err, code = await _to_thread(_do)
    if code != 0:
        await update.message.reply_text(f"Error: {(err or out).strip()}")
        return
    await update.message.reply_text(f"Task #{tid} {past}.")


async def cmd_done(update, context):
    await _id_action(update, context, "done", "completed")


async def cmd_del(update, context):
    await _id_action(update, context, "delete", "deleted")


async def cmd_stop(update, context):
    await _id_action(update, context, "stop", "stopped")


async def cmd_tz(update, context):
    u = resolve_user(update.effective_user.id, update.effective_chat.id)
    if not context.args:
        await update.message.reply_text(
            f"Current timezone: {u['timezone']}\n"
            f"Usage: /tz <IANA name>, e.g. /tz Europe/Berlin")
        return
    tz = context.args[0]
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        await update.message.reply_text(f"Unknown timezone: {tz}. "
                                        f"Examples: Europe/Berlin, America/New_York, UTC")
        return
    users.set_timezone(u["id"], tz)
    logger.info("Telegram: user '%s' set timezone to %s", u["username"], tz)
    await update.message.reply_text(f"Timezone set to {tz}.")


async def cmd_remind(update, context):
    u = resolve_user(update.effective_user.id, update.effective_chat.id)
    if not context.args or context.args[0].lower() not in ("on", "off"):
        state = "on" if u["reminders"] else "off"
        await update.message.reply_text(
            f"Reminders are currently {state}. Usage: /remind on|off")
        return
    on = context.args[0].lower() == "on"
    users.set_reminders(u["id"], on)
    await update.message.reply_text(
        f"Reminders {'enabled' if on else 'disabled'}.")


async def cmd_link(update, context):
    tg_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Usage: /link CODE — generate the code on the WebUI profile page.")
        return
    code = context.args[0].strip().upper()
    row = users.consume_token(code, KIND_TGLINK)
    if not row:
        await update.message.reply_text(
            "Invalid or expired code. Generate a fresh one on the WebUI "
            "profile page (valid 15 minutes).")
        return
    target = users.get_user(row["user_id"])
    if not target:
        await update.message.reply_text("That code no longer belongs to a user.")
        return
    shadow = users.get_user_by_telegram(tg_id)
    try:
        if shadow and shadow["id"] != target["id"]:
            # Migrate the shadow database into the web account when empty.
            src = store.user_data_dir(shadow["id"])
            dst = store.user_data_dir(target["id"])
            src_has_data = os.path.exists(src) and os.path.isdir(src) \
                and len(os.listdir(src)) > 0
            dst_has_data = os.path.exists(dst) and os.path.isdir(dst) \
                and len(os.listdir(dst)) > 0
            if src_has_data and dst_has_data:
                await update.message.reply_text(
                    "Both this Telegram account and the WebUI account already "
                    "contain tasks. Complete or export one side first, then "
                    "link again.")
                return
            if src_has_data:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst):
                    os.rmdir(dst)
                os.rename(src, dst)
                logger.info("Telegram: migrated shadow data %s -> %s",
                            src, dst)
            users.delete_user(shadow["id"])
        users.link_telegram(target["id"], tg_id, update.effective_chat.id)
    except ValueError as exc:
        await update.message.reply_text(f"Could not link: {exc}")
        return
    logger.info("Telegram: linked tg %s to web account '%s'",
                tg_id, target["username"])
    await update.message.reply_text(
        f"Linked to WebUI account '{target['username']}'. "
        f"Both interfaces now share the same tasks. /add away!")


async def cmd_unlink(update, context):
    tg_id = update.effective_user.id
    u = users.get_user_by_telegram(tg_id)
    if not u:
        await update.message.reply_text("This Telegram account is not linked.")
        return
    users.unlink_telegram(u["id"])
    logger.info("Telegram: unlinked tg %s from '%s'", tg_id, u["username"])
    await update.message.reply_text(
        f"Unlinked from '{u['username']}'. From now on this chat uses its own "
        f"separate task database again.")


async def cmd_whoami(update, context):
    u = resolve_user(update.effective_user.id, update.effective_chat.id)
    status = ("linked WebUI account"
              if not u["username"].startswith("tg_") else "own Telegram database")
    await update.message.reply_text(
        f"Account: {u['username']} ({status})\n"
        f"Timezone: {u['timezone']}\n"
        f"Reminders: {'on' if u['reminders'] else 'off'}")


# --------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------
def _collect_reminders():
    """Blocking scan; returns [(chat_id, text, uid, uuid, kind), ...]."""
    users.init_db()
    out = []
    with users.db() as conn:
        rows = conn.execute(
            "SELECT id, timezone, telegram_chat, reminders FROM users"
            " WHERE telegram_chat IS NOT NULL AND disabled=0").fetchall()
    for row in rows:
        uid, tzname, chat_id = row["id"], row["timezone"], row["telegram_chat"]
        if not row["reminders"]:
            continue
        try:
            tz = ZoneInfo(tzname)
        except Exception:
            tz = timezone.utc
        data_dir = store.user_data_dir(uid)
        if not os.path.isdir(data_dir):
            continue
        now_utc = datetime.now(timezone.utc)
        soon_edge = now_utc + timedelta(minutes=DUE_SOON_MINUTES)
        tasks = store.export(["status:pending"], data_dir=data_dir)
        for t in tasks:
            due = store.parse_tw_date(t.get("due"))
            if not due:
                continue
            if due < now_utc:
                kind = "overdue"
            elif due <= soon_edge:
                kind = "soon"
            else:
                continue
            if users.reminder_sent(uid, t["uuid"], kind):
                continue
            local = due.astimezone(tz)
            icon = "🔴" if kind == "overdue" else "⏰"
            label = "OVERDUE" if kind == "overdue" else "due soon"
            text = (f"{icon} {label}: #{t.get('id')} {t.get('description','')}"
                    f" — {local.strftime('%b %d %H:%M')} ({tzname})")
            out.append((chat_id, text, uid, t["uuid"], kind))
    return out


async def reminder_job(context):
    """Scan all linked users' tasks and nudge about due-soon/overdue items."""
    try:
        pending = await _to_thread(_collect_reminders)
        for chat_id, text, uid, uuid, kind in pending:
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
                users.mark_reminder_sent(uid, uuid, kind)
            except Exception:
                logger.exception("failed to send reminder to user %s", uid)
    except Exception:
        logger.exception("reminder scan failed")


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------
def main():
    if not TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN is not set — bot disabled. "
                       "Set it in .env and redeploy to enable.")
        while True:  # keep the container alive; env-only toggle
            time.sleep(3600)
    users.init_db()
    from telegram.ext import Application

    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("add", cmd_add))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("done", cmd_done))
    application.add_handler(CommandHandler("del", cmd_del))
    application.add_handler(CommandHandler("delete", cmd_del))
    application.add_handler(CommandHandler("begin", cmd_begin))
    application.add_handler(CommandHandler("stop", cmd_stop))
    application.add_handler(CommandHandler("tz", cmd_tz))
    application.add_handler(CommandHandler("remind", cmd_remind))
    application.add_handler(CommandHandler("link", cmd_link))
    application.add_handler(CommandHandler("unlink", cmd_unlink))
    application.add_handler(CommandHandler("whoami", cmd_whoami))
    application.job_queue.run_repeating(
        reminder_job, interval=REMINDER_INTERVAL, first=15)
    logger.info("Telegram bot starting (reminder interval=%ss, due-soon=%smin)",
                REMINDER_INTERVAL, DUE_SOON_MINUTES)
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
