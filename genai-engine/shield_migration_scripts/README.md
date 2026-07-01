# Shield Migration Scripts

Standalone scripts for migrating data from Arthur Shield to Arthur Engine.

## `pre_migration_scope.py`

Queries the Shield **PostgreSQL database directly** and prints a stats report on
the number of tasks, rules, inferences, validation results, and feedback records
that can be migrated to the Engine. **It is read-only — no data is written.**

Use it before a migration to estimate scope, confirm connectivity to the Shield
database, and see how data is distributed across the platform.

It reads over SQL rather than the Shield API so counts stay fast for customers
with ~1B inferences (the API's OFFSET pagination does not hold up at that scale).

### Setup

The script connects to the Shield database using these environment variables:

| Variable | Description |
|---|---|
| `SHIELD_POSTGRES_USER` | Database user |
| `SHIELD_POSTGRES_PASSWORD` | Database password |
| `SHIELD_POSTGRES_URL` | Database host |
| `SHIELD_POSTGRES_PORT` | Database port |
| `SHIELD_POSTGRES_DB` | Database name |
| `SHIELD_POSTGRES_USE_SSL` | *(optional)* `"true"`/`"false"`, default `false` |
| `SHIELD_POSTGRES_SSL_ROOT_CERT` | *(optional)* path to CA cert, used when SSL is on |

Export them in your shell before running:

```bash
export SHIELD_POSTGRES_USER="postgres"
export SHIELD_POSTGRES_PASSWORD="changeme_pg"
export SHIELD_POSTGRES_URL="localhost"
export SHIELD_POSTGRES_PORT="5432"
export SHIELD_POSTGRES_DB="arthur_shield"
```

Dependencies are `sqlalchemy` and the `psycopg2` driver:

```bash
pip install sqlalchemy psycopg2-binary
```

### Usage

A date window is **required**: pass either `--last-days`, or **both**
`--from-date` and `--to-date`. Scoping all of time is not allowed.

```bash
# Scope a fixed date window (from-date inclusive, to-date exclusive, format YYYY-MM-DD)
python pre_migration_scope.py --from-date 2020-01-01 --to-date 2021-01-01

# Scope only the last N days
python pre_migration_scope.py --last-days 180
```

### Options

| Flag | Description |
|---|---|
| `--from-date` | Start date (inclusive), ISO format e.g. `2020-01-01`. Applies to inferences, validation results, and feedback only. Must be paired with `--to-date`. |
| `--to-date` | End date (exclusive), ISO format e.g. `2021-01-01`. Applies to inferences, validation results, and feedback only. Must be paired with `--from-date`. |
| `--last-days` | Shorthand to scope the last N days. Takes precedence over `--from-date`/`--to-date`. |

> **Note:** Config objects (tasks, rules, default rules, and task–rule links) are
> always reported in full — the date window does **not** apply to them. The window
> only filters inferences, validation results, and feedback.

### Output

The report is printed to stdout in these sections:

- **Config** — total tasks, task-scoped rules, default rules, and task–rule links.
- **Inferences** — total inference count in the window, and how many tasks have data.
- **Validation results** — prompt-stage and response-stage rule result counts, plus
  their total. Filtered by each rule result's own `created_at`.
- **Feedback** — total feedback records in the window.
- **Per-task inference counts** — inference count per task, sorted descending.
  Inferences with no task are grouped under `(no task)`.

Example:

```
============================================================
  Shield → Engine Migration Scope Report
  Window: from 2020-01-01 to 2021-01-01
============================================================

Config (always migrated in full, date window does not apply)
  Tasks                          42
  Task-scoped rules              118
  Default rules                  6
  Task–rule links                213

Inferences
  Total                          1,204,556
  Tasks with data                38 / 42

Validation (rule) results
  Validate Prompt Results        1,204,556
  Validate Response Results      1,150,003
  Total Validation Results       2,354,559

Feedback
  Total Feedback                 9,812

Per-task inference counts
  fraud-detection                512,003
  support-summarizer             301,991
```
