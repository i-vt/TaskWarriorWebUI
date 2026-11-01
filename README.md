# Taskwarrior WebUI

A multi-user web interface for **Taskwarrior** with an optional **Telegram
bot**, running fully in Docker. Each account gets an isolated task database;
an admin invites members, manages accounts, and can link everything to a
Telegram bot that answers to due/overdue reminders in each user's own
timezone.

## Features

- **Web UI** — full task lifecycle (add/modify/done/delete/purge, start/stop,
  dependencies, recurrence, annotations, tags, projects, contexts, UDAs,
  filters, reports), scoped per user.
- **Accounts** — username/password login (PBKDF2-hashed). The first admin is
  created through a **one-time registration link** generated at first start;
  the link is printed for every detected IP address in the system log.
  Members are invited from the **admin panel** via copy/paste links that use
  the public IP when reachable. Admins can **disable/enable** a user,
  **reset a password** (one-time reset link) and **delete a user together
  with all their data**.
- **Telegram bot** (`TELEGRAM_BOT_TOKEN`) — `/add`, `/list`, `/today`,
  `/done`, `/del`, `/begin`, `/stop`, `/tz`, `/remind`, `/whoami`. Every
  Telegram user gets an **individual task database** until they link their
  WebUI account with `/link CODE` (code generated on the profile page);
  tasks created in Telegram then show up in the web UI and vice versa.
- **Reminders** — the bot scans every linked user's tasks and sends
  *due soon* and *overdue* notifications, rendered in the user's timezone.
- **Per-user timezones** — settable in the profile page or via `/tz`.

## Deployment (Debian 12/13, root, Docker pre-installed)

```bash
./install.sh            # build & start; creates .env from .env.example
```

- Plain **HTTP** on `WEB_PORT` (default `62304`) is the default.
- For **HTTPS**, set `DOMAIN` (and `ACME_EMAIL`) in `.env`; a Caddy
  container is started automatically with Let's Encrypt certificates.
- `install.sh --reset --yes` performs a **full reset**: stops the stack,
  deletes Docker volumes and **all user data**, then reinstalls fresh.
- `./uninstall.sh` removes the stack (add `--purge` to also delete user
  data, `.env` and images).
- The stack **self-recovers after reboot**: containers use
  `restart: unless-stopped` and a systemd unit
  (`taskwarrior-webui.service`) brings the compose project up on boot.
- All installer output is written to `logs/install-<timestamp>.log`; the
  application writes to `${DATA_DIR}/app/system.log` (viewable in the
  admin panel). Missing OS packages (docker, compose plugin, curl) are
  installed automatically by the deployment scripts.

### First login

After install, fetch the one-time admin registration link(s):

```bash
docker logs taskwarrior-webui 2>&1 | grep "admin registration link"
```

Open one of the printed links, create the admin account, then use the
**Admin panel** to invite members and manage users.

### Telegram bot

1. Create a bot with [@BotFather](https://t.me/BotFather), copy the token
   into `TELEGRAM_BOT_TOKEN` in `.env`.
2. `docker compose up -d bot`
3. In the WebUI profile page generate a link code, then send
   `/link CODE` to your bot.

Reminder timing is tuned with `TELEGRAM_REMINDER_INTERVAL` (scan interval)
and `TELEGRAM_DUE_SOON_MINUTES` (how long before the due time to nudge).

## Configuration

All knobs live in `.env` (see `.env.example`): `WEB_PORT`, `BIND_ADDR`,
`DOMAIN`, `ACME_EMAIL`, `TIMEZONE`, `DATA_DIR`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_REMINDER_INTERVAL`, `TELEGRAM_DUE_SOON_MINUTES`.

## Layout

```
app.py        Flask web app (per-user task scoping, admin, profile)
bot.py        Telegram bot (shadow accounts, /link sync, reminders)
store.py      Taskwarrior CLI access per data directory
users.py      SQLite accounts, tokens, timezones, telegram links
install.sh    Debian installer / reset
uninstall.sh  Remover (--purge deletes data)
deploy/       boot-start.sh + Caddyfile for the HTTPS profile
```

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```
