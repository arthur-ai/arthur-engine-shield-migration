# Shield Migration Scripts

Standalone scripts for migrating data from Arthur Shield to Arthur Engine.

## `pre_migration_scope.py`

Queries the Shield API and prints a stats report on the number of tasks, rules,
inferences, and feedback records that can be migrated to the Engine. **It is
read-only — no data is written.**

Use it before a migration to estimate scope, confirm connectivity to the Shield
API, and see how data is distributed across the platform.

### Setup

The script reads two environment variables:

| Variable | Description |
|---|---|
| `SHIELD_BASE_URL` | Base URL of the Shield API (no trailing slash), e.g. `https://shield.example.com` |
| `SHIELD_API_KEY` | API key/token sent as `Authorization: Bearer <key>` |

Export them in your shell before running:

```bash
export SHIELD_BASE_URL="https://shield.example.com"
export SHIELD_API_KEY="your-api-key"
```

The only third-party dependency is `requests`:

```bash
pip install requests
```

### Usage

```bash
# Full scope across all time
python pre_migration_scope.py

# Scope a fixed date window (from-date inclusive, to-date exclusive, Format YYYY-MM-DD)
python pre_migration_scope.py --from-date 2020-01-01 --to-date 2021-01-01

# Scope only the last N days
python pre_migration_scope.py --last-days 180
```

### Options

| Flag | Description |
|---|---|
| `--from-date` | Start date (inclusive), ISO format e.g. `2020-01-01`. Applies to inferences and feedback only. |
| `--to-date` | End date (exclusive), ISO format e.g. `2021-01-01`. Applies to inferences and feedback only. |
| `--last-days` | Shorthand to scope the last N days. Takes precedence over `--from-date`/`--to-date`. |

> **Note:** Config objects (tasks, rules, default rules, and task–rule links) are
> always reported in full — the date window does **not** apply to them. The window
> only filters inferences and feedback.

### Output

The report is printed to stdout in four sections:

- **Config** — total tasks, task-scoped rules, default rules, and task–rule links.
- **Inferences** — total inference count in the window, and how many tasks have data.
- **Feedback** — total feedback records in the window.
- **Per-task inference counts** — inference count per task, sorted descending.
  A count of `-1` means the count could not be fetched for that task (a warning is
  printed inline).

Example:

```
============================================================
  Shield → Engine Migration Scope Report
  Window: all time
============================================================

Config (always migrated in full, date window does not apply)
  Tasks              : 42
  Task-scoped rules  : 118
  Default rules      : 6
  Task–rule links    : 213

Inferences
  Total              : 1,204,556
  Tasks with data    : 38 / 42

Feedback               : 9,812

Per-task inference counts
  fraud-detection                          512,003
  support-summarizer                       301,991
  ...
```

### Behavior notes

- Requests retry up to 3 times. On HTTP 429 (rate limit) the script backs off
  exponentially (`2 ** attempt` seconds).
- Per-task inference counts are fetched concurrently (up to 10 at a time).
- A failure fetching a single task's count does not abort the report; it logs a
  warning and reports `-1` for that task.
- Pagination uses a page size of 5000 (the max allowed by the Shield API).
