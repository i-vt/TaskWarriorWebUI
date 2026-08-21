# Taskwarrior WebUI

A lightweight, terminal-aesthetic web interface for **Taskwarrior** - running fully in
Docker with local data persistence. It aims to expose the *complete* practical
Taskwarrior feature surface (every task attribute, the full task lifecycle,
dependencies, recurrence, annotations, contexts, UDAs, filters, and reports)
through a single-page UI, while talking to the real `task` binary underneath.

See **[FEATURES.md](FEATURES.md)** for a feature-by-feature coverage matrix.

<img width="2252" height="1776" alt="image" src="https://github.com/user-attachments/assets/fec6b77a-20ab-43e4-8f95-7c2623c18e2c" />

<img width="1277" height="1359" alt="image" src="https://github.com/user-attachments/assets/504849e3-4a31-429f-a080-c357f754c771" />


## Quick Start

### Docker Compose (recommended)

```bash
docker compose up -d --build
```

Open <http://localhost:62304>

You will be greeted by a login screen. The panel and the API are protected by a
32-character access key that is generated fresh on every container start - see
[Authentication](#authentication) below for how to retrieve it.

> By default the container is bound to `127.0.0.1` only. The **Filter** box
> accepts raw Taskwarrior expressions, so this is meant as a single-user local
> tool. To expose it on your LAN, change the port mapping in
> `docker-compose.yml` to `"62304:62304"`. The built-in key authentication
> protects both the panel and the API, but a reverse proxy with TLS is still
> recommended anywhere off localhost.

### Docker directly

```bash
docker build -t taskwarrior-webui .
docker run -d \
  --name taskwarrior-webui \
  -p 127.0.0.1:62304:62304 \
  -v "$(pwd)/taskdata:/data" \
  --restart unless-stopped \
  taskwarrior-webui
```

## Authentication

On startup the application generates a **32-character hexadecimal access key**
and writes it to:

```
/tmp/TaskWarriorWebUIKey.txt
```

inside the container (file permissions `0600`). The same key unlocks the web
panel (via the login page) and authenticates every API call.

Retrieve the key from a running container:

```bash
# plain docker
docker exec taskwarrior-webui cat /tmp/TaskWarriorWebUIKey.txt

# docker compose
docker compose exec taskwarrior-webui cat /tmp/TaskWarriorWebUIKey.txt
```

Notes:

- The key is generated on first start and reused while the container exists;
  recreating the container generates a **new key** and invalidates old
  sessions. The key is never printed to the logs - only the file location
  and retrieval command are logged.
- To pin a fixed key across restarts (e.g. for scripts), set the environment
  variable `TASKWARRIOR_WEBUI_KEY` (any 32-character string):

  ```bash
  docker run -d -e TASKWARRIOR_WEBUI_KEY=0123456789abcdef0123456789abcdef ...
  ```

  or add it under `environment:` in `docker-compose.yml`.
- The key file location itself can be relocated with
  `TASKWARRIOR_WEBUI_KEY_PATH`.
- Logging in creates a signed session cookie; use the **logout** button in the
  top bar to end a session.

## API

Every endpoint under `/api/` requires the access key. Send it in either header:

```
Authorization: Bearer <key>
X-Api-Key: <key>
```

(The browser session cookie from the login page also works.) Unauthenticated
API calls return `401 {"error": "Unauthorized"}`. `/api/healthz` is the only
unauthenticated endpoint, for container health checks.

### Endpoint reference

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/tasks` | List tasks. Query params: `status`, `vtag`, `project`, `tag`, `filter` (raw Taskwarrior filter), `sort` |
| POST | `/api/tasks` | Add a task (`description` required; plus `project`, `priority`, `due`, `scheduled`, `wait`, `until`, `recur`, `tags`, `depends`, `annotations`, `uda`) |
| POST | `/api/tasks/log` | Record an already-completed task |
| GET | `/api/tasks/<uuid>` | Task detail incl. resolved `depends_detail` |
| PATCH | `/api/tasks/<uuid>` | Modify attributes, `tags_add`/`tags_remove`, `depends_add`/`depends_remove`/`depends_clear` |
| DELETE | `/api/tasks/<uuid>` | Delete a task |
| POST | `/api/tasks/<uuid>/done` | Mark completed |
| POST | `/api/tasks/<uuid>/start` | Start (mark active) |
| POST | `/api/tasks/<uuid>/stop` | Stop |
| POST | `/api/tasks/<uuid>/purge` | Permanently purge (task must be deleted first) |
| POST | `/api/tasks/<uuid>/duplicate` | Duplicate a task |
| POST | `/api/tasks/<uuid>/annotate` | Add annotation (`{"text": ...}`) |
| POST | `/api/tasks/<uuid>/denotate` | Remove annotation (`{"text": ...}`) |
| POST | `/api/undo` | Undo last change |
| GET | `/api/export` | Download all tasks as Taskwarrior JSON |
| POST | `/api/import` | Import Taskwarrior JSON (file upload or body) |
| GET | `/api/projects` | List projects |
| GET | `/api/tags` | List tags |
| GET | `/api/stats` | Status counts |
| GET | `/api/overview` | Stats + projects + tags + contexts + UDAs + priorities in one call |
| GET | `/api/udas` | Configured User Defined Attributes |
| GET | `/api/contexts` | List contexts + active context |
| POST | `/api/contexts` | Define a context (`name`, `filter`) |
| DELETE | `/api/contexts/<name>` | Delete a context |
| POST | `/api/context` | Set active context (`{"name": ...}`, `"none"` to clear) |
| GET | `/api/reports/summary` | Per-project pending/completed progress |
| GET | `/api/reports/burndown` | Daily created vs completed, `?days=7..180` |
| GET | `/api/healthz` | Health check (no auth) |

### Examples

```bash
KEY=$(docker exec taskwarrior-webui cat /tmp/TaskWarriorWebUIKey.txt)

# List pending tasks sorted by urgency
curl -H "X-Api-Key: $KEY" "http://localhost:62304/api/tasks?status=pending&sort=urgency"

# Add a task
curl -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"description": "Write release notes", "project": "work", "priority": "H", "tags": ["docs"]}' \
  http://localhost:62304/api/tasks

# Mark a task done
curl -X POST -H "X-Api-Key: $KEY" http://localhost:62304/api/tasks/<uuid>/done
```

## Mobile Support

The UI is fully responsive and works on phones:

- The sidebar collapses into a slide-in drawer (hamburger button in the top
  bar, or press `m`). Picking a view, project, or tag closes the drawer.
- Modals become full-screen sheets, form fields stack vertically, and touch
  targets are enlarged.
- The task list compacts to description / priority / due columns; the top bar
  scrolls horizontally on narrow screens.

## Data Persistence

All task data is stored in `./taskdata/` on your host. The `.task` folder inside
it is a standard Taskwarrior data directory - back it up, sync it, or import/export
normally. The app writes a default `.taskrc` there on first start if one is
missing, so an empty volume "just works".

## Project Structure

```
.
├── app.py               # Flask backend: key generation, auth, task CLI bridge, API routes
├── templates/
│   ├── index.html       # Panel markup
│   └── login.html       # Key entry page
├── static/
│   ├── css/style.css    # All panel styles, incl. responsive/mobile rules
│   └── js/app.js        # All client-side logic
├── tests/
│   └── test_app.py      # pytest suite (auth, key handling, API)
├── requirements.txt     # Runtime deps (flask, gunicorn)
├── requirements-dev.txt # Dev deps (pytest)
├── Dockerfile
├── docker-compose.yml
├── README.md
└── FEATURES.md
```

## Features

**Task attributes (full create + edit):** description, project (with hierarchy),
priority, due, scheduled, wait, until, recurrence, tags, dependencies,
annotations, and any configured User Defined Attributes (UDAs).

**Lifecycle & operations:** add, edit/modify, complete (done), start/stop,
delete, **purge** (permanent), **duplicate**, **log** (record an already-completed
task), annotate/denotate, and global **undo**.

**Views:** Pending, Active, Due Soon, Overdue, Scheduled, Blocked, Blocking,
Waiting, Recurring, Completed, Deleted, All - plus per-project and per-tag
drill-down, each with live counts.

**Contexts:** switch the active context from the sidebar, or define a new one;
the chosen context's filter is applied to every view (just like the CLI).

**Filtering & search:** a raw Taskwarrior **Filter** box (e.g. `priority:H`,
`+work due.before:eom`) combined with the current view, client-side live search,
and sortable columns (urgency, due, created, project, description).

**Reports:** a dashboard with status counts, per-project progress bars, and a
30-day created-vs-completed activity chart.

**Import / Export:** download all tasks as Taskwarrior JSON, or import a JSON
file back in.

**Urgency:** numeric score + visual bar on every row, with the full breakdown of
status flags (OVERDUE / ACTIVE / BLOCKED / SCHEDULED / WAITING / RECURRING) shown
on the task detail.

## Keyboard Shortcuts

| Key   | Action                               |
|-------|--------------------------------------|
| `n`   | New task                             |
| `/`   | Focus search                         |
| `u`   | Undo last change                     |
| `r`   | Open Reports                         |
| `m`   | Toggle the sidebar menu (mobile)     |
| `Esc` | Close any open dialog                |
| `Ctrl/Cmd`+`Enter` | Submit the task form    |

## Importing an Existing Setup

Copy your `~/.task/` folder into `./taskdata/.task/` before starting:

```bash
cp -r ~/.task ./taskdata/.task
```

Or start the container and use the **⬆ import** button with a `task export` JSON file.

## Development

Run the test suite (no Docker or `task` binary needed, the CLI bridge is mocked):

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -v
```

## Notes on Taskwarrior version

The `python:3.12-slim` base image is Debian *bookworm* → **Taskwarrior 2.6.2**.
The backend only uses the stable CLI + JSON `export`/`import` contract, so the
same code also runs against Taskwarrior 3.x (Debian *trixie*) if you change the
base image.

A few CLI-only concerns are intentionally **out of scope** for a web UI - remote
`sync`/server, the `$EDITOR`-based `task edit`, hook scripts, and shell-level
aliases. See FEATURES.md for the full list and rationale.

## Stopping

```bash
docker compose down
```
