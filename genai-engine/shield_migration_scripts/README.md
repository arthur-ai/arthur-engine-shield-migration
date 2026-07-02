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

The date window should match the data retention policy of the Engine, so that
only inferences you intend to keep are migrated. For example, if the retention
period of the Engine is 90 days, use `--last-days 90`.

```bash
# Scope only the last N days
python pre_migration_scope.py --last-days 180

# Scope a fixed date window (from-date inclusive, to-date exclusive, format YYYY-MM-DD)
python pre_migration_scope.py --from-date 2020-01-01 --to-date 2021-01-01

# Use fast planner estimates instead of exact counts (for very large tables)
python pre_migration_scope.py --last-days 180 --estimate
```

### Options

| Flag | Description |
|---|---|
| `--from-date` | Start date (inclusive), ISO format e.g. `2020-01-01`. Applies to inferences, validation results, and feedback only. Must be paired with `--to-date`. |
| `--to-date` | End date (exclusive), ISO format e.g. `2021-01-01`. Applies to inferences, validation results, and feedback only. Must be paired with `--from-date`. |
| `--last-days` | Shorthand to scope the last N days. Takes precedence over `--from-date`/`--to-date`. |
| `--output-dir` / `-o` | Directory to write the report file to. |
| `--estimate` | Use planner row estimates instead of exact counts. Cheap on huge tables, but skips the per-task breakdown. |

> **Note:** Config objects (tasks, rules, default rules, and task–rule links) are
> always reported in full — the date window does **not** apply to them. The window
> only filters inferences, validation results, and feedback.

### Estimate mode (`--estimate`)

Use this mode for large tables to get a rough estimate of th volume of resources to be migrated. Exact counts must visit every matching row. Estimate mode instead runs each count as `EXPLAIN (FORMAT JSON) SELECT ...` and reads the planner's `Plan Rows` estimate.

### Output

The report is printed to stdout and can also be written to a file when `--output-dir` is specified. These are the sections written:

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
  Total Inferences               1,204,556
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

With `--estimate`, counts are approximate (`~`) and the per-task breakdown is
omitted:

```
============================================================
  Shield → Engine Migration Scope Report
  Window: from 2020-01-01 to 2021-01-01
  Mode:   estimate
============================================================

Config (always migrated in full, date window does not apply)
  Tasks                          42
  Task-scoped rules              118
  Default rules                  6
  Task–rule links                213

Inferences
  Total Inferences               ~1,198,400

Validation (rule) results
  Validate Prompt Results        ~1,198,400
  Validate Response Results      ~1,142,900
  Total Validation Results       ~2,341,300

Feedback
  Total Feedback                 ~9,750
```
