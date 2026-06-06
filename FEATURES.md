# Taskwarrior → WebUI feature coverage

This is the review of Taskwarrior's feature surface against the WebUI, with what
the original build covered, what was added, and the handful of items
intentionally left out (with reasons). All "Added" items were verified against a
live Taskwarrior **2.6.2** instance via the app's own API.

Legend: ✅ supported · ➕ added in this pass · ⛔ intentionally out of scope

## 1. Task attributes (create **and** edit)

| Attribute | Before | Now | Notes |
|---|---|---|---|
| description | ✅ | ✅ | |
| project (hierarchical) | ✅ | ✅ | datalist of existing projects; sidebar drill-down |
| priority (H/M/L) | ✅ | ✅ | options now read live from `rc.uda.priority.values` |
| tags (+tag) | ✅ | ✅ | add on create; add/remove on edit (computed diff) |
| due | ✅ | ✅ | `datetime-local`, sent as absolute UTC to avoid TZ ambiguity |
| scheduled | ❌ | ➕ | |
| wait | ❌ | ➕ | hides task until the date (status → waiting) |
| until (expire) | ❌ | ➕ | |
| recurrence (recur) | ❌ | ➕ | presets + free text; UI enforces "needs a due date" |
| dependencies (depends) | ❌ | ➕ | picker of existing tasks; chips; resolved + removable in detail |
| annotations | view-only | ➕ | add/remove on create and live in the detail modal |
| UDAs (User Defined Attributes) | ❌ | ➕ | discovered from config; typed inputs (numeric/date/enum/string) |
| urgency | ✅ (bar) | ✅ | number + bar in list; flag breakdown in detail |
| entry / modified / start / end | partial | ✅ | all shown with date+time and relative age |
| uuid / id | ✅ | ✅ | |

## 2. Lifecycle & operations

| Operation | Before | Now | Notes |
|---|---|---|---|
| add | ✅ | ✅ | now carries every attribute above |
| modify (edit) | partial | ➕ | full edit modal incl. clearing date attributes |
| done | ✅ | ✅ | row checkbox + detail button |
| start / stop | ✅ | ✅ | |
| delete | ✅ | ✅ | |
| **purge** (permanent) | ❌ | ➕ | only offered on already-deleted tasks; addressed by uuid |
| **duplicate** | ❌ | ➕ | |
| **log** (pre-completed task) | ❌ | ➕ | "Log completed" mode in the add modal |
| **annotate / denotate** | ❌ | ➕ | |
| **undo** | ❌ | ➕ | toolbar button + `u` shortcut |
| append / prepend | ❌ | ⛔ | covered by full description editing in the edit modal |

## 3. Filtering, views & virtual tags

| Capability | Before | Now | Notes |
|---|---|---|---|
| status views: pending / completed / deleted | ✅ | ✅ | |
| **Active** (`+ACTIVE`) | broken* | ➕ | *old build filtered `status:active`, which never matches |
| **Overdue** (`+OVERDUE`) | ❌ | ➕ | |
| **Due Soon** (`+DUE`) | ❌ | ➕ | |
| **Scheduled** (`+SCHEDULED`) | ❌ | ➕ | |
| **Blocked** (`+BLOCKED`) | ❌ | ➕ | |
| **Blocking** (`+BLOCKING`) | ❌ | ➕ | |
| **Waiting** (`status:waiting`) | ❌ | ➕ | |
| **Recurring** (`status:recurring`) | ❌ | ➕ | |
| **All** | ❌ | ➕ | |
| project filter | ✅ | ✅ | with live counts |
| tag filter | ✅ | ✅ | with live counts |
| **raw filter expression** | ❌ | ➕ | sidebar Filter box, sanitized; combined with the view |
| client-side search | ✅ | ✅ | now also searches annotations |
| sorting | urgency only | ➕ | urgency / due / created / project / description |

## 4. Contexts

| Capability | Before | Now |
|---|---|---|
| list contexts | ❌ | ➕ |
| switch active context (incl. "none") | ❌ | ➕ |
| define a new context | ❌ | ➕ |
| delete a context | ❌ | ➕ (API) |

## 5. Reports & insights

| Capability | Before | Now | Notes |
|---|---|---|---|
| live stat bar (pending/active/overdue/done) | ✅ | ✅ | overdue calc fixed (old build string-compared mismatched date formats) |
| status dashboard | ❌ | ➕ | 10 counters incl. blocked/blocking/scheduled/waiting/recurring |
| project progress (`summary`) | ❌ | ➕ | per-project completion bars |
| burndown / history | ❌ | ➕ | 30-day created-vs-completed chart |

## 6. Data management

| Capability | Before | Now |
|---|---|---|
| JSON export (download) | ❌ | ➕ |
| JSON import (upload) | ❌ | ➕ |
| undo | ❌ | ➕ |

## 7. Intentionally out of scope (and why)

| Feature | Why it isn't a web-UI control |
|---|---|
| `sync` / Taskserver / cloud sync | Server credential & transport concern; configure it in the data dir / taskd and the WebUI reads the synced store. Not a per-click UI action. |
| `task edit` ($EDITOR raw record) | Opens a terminal editor on the raw record; the structured edit modal covers the same fields safely. |
| hook scripts | Server-side executables in `~/.task/hooks`; managed on disk, run automatically on every command. |
| shell aliases / custom CLI reports in `.taskrc` | Configuration of the CLI itself; the WebUI provides its own views + a raw-filter box instead. |
| `calc`, `diagnostics`, `_*` helper commands | Internal/CLI utilities, not end-user task actions. |
| `import`-from-other-tools converters | Out of band; use `task export`/`import` JSON, which is supported. |

## 8. Bugs fixed along the way

- **Broken build layout:** the app renders `templates/index.html` but the
  original archive shipped `index.html` at the repo root with no `templates/`
  dir, so `docker build` (which does `COPY templates/ templates/`) would fail.
  Now correctly under `templates/`.
- **"Active" view returned nothing:** it filtered `status:active`, which is not
  a real status. Now uses the `+ACTIVE` virtual tag.
- **Overdue counter was unreliable:** it compared Taskwarrior's compact
  `YYYYMMDDTHHMMSSZ` dates against a dashed ISO string. Now uses a matching
  compact-UTC comparison (and the `+OVERDUE` tag for the view).
- **Dead double-fetch** in the task loader removed.
- **Volume-shadowed config:** a bind-mount hid the image's baked `.taskrc`; the
  app now writes a default config on startup so an empty volume works.
- **Dependency editing** uses the version-safe pattern (set as a UUID list;
  remove via `depends:-<uuid>`; clear via `depends:`), since `depends:+id` is
  rejected by 2.6.2.
