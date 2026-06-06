# Taskwarrior WebUI

A lightweight, terminal-aesthetic web interface for **Taskwarrior** — running fully in
Docker with local data persistence. It aims to expose the *complete* practical
Taskwarrior feature surface (every task attribute, the full task lifecycle,
dependencies, recurrence, annotations, contexts, UDAs, filters, and reports)
through a single-page UI, while talking to the real `task` binary underneath.

See **[FEATURES.md](FEATURES.md)** for a feature-by-feature coverage matrix.

## Quick Start

### Docker Compose (recommended)

```bash
docker compose up -d --build
```

Open <http://localhost:62304>

> By default the container is bound to `127.0.0.1` only. The **Filter** box
> accepts raw Taskwarrior expressions, so this is meant as a single-user local
> tool. To expose it on your LAN, change the port mapping in
> `docker-compose.yml` to `"62304:62304"` and put it behind auth / a reverse
> proxy.

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

## Data Persistence

All task data is stored in `./taskdata/` on your host. The `.task` folder inside
it is a standard Taskwarrior data directory — back it up, sync it, or import/export
normally. The app writes a default `.taskrc` there on first start if one is
missing, so an empty volume "just works".

## Features

**Task attributes (full create + edit):** description, project (with hierarchy),
priority, due, scheduled, wait, until, recurrence, tags, dependencies,
annotations, and any configured User Defined Attributes (UDAs).

**Lifecycle & operations:** add, edit/modify, complete (done), start/stop,
delete, **purge** (permanent), **duplicate**, **log** (record an already-completed
task), annotate/denotate, and global **undo**.

**Views:** Pending, Active, Due Soon, Overdue, Scheduled, Blocked, Blocking,
Waiting, Recurring, Completed, Deleted, All — plus per-project and per-tag
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
| `Esc` | Close any open dialog                |
| `Ctrl/Cmd`+`Enter` | Submit the task form    |

## Importing an Existing Setup

Copy your `~/.task/` folder into `./taskdata/.task/` before starting:

```bash
cp -r ~/.task ./taskdata/.task
```

Or start the container and use the **⬆ import** button with a `task export` JSON file.

## Notes on Taskwarrior version

The `python:3.12-slim` base image is Debian *bookworm* → **Taskwarrior 2.6.2**.
The backend only uses the stable CLI + JSON `export`/`import` contract, so the
same code also runs against Taskwarrior 3.x (Debian *trixie*) if you change the
base image.

A few CLI-only concerns are intentionally **out of scope** for a web UI — remote
`sync`/server, the `$EDITOR`-based `task edit`, hook scripts, and shell-level
aliases. See FEATURES.md for the full list and rationale.

## Stopping

```bash
docker compose down
```
