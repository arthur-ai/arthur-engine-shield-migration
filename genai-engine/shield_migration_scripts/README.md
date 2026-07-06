# Shield Migration Scripts

Standalone scripts for migrating data from Arthur Shield to Arthur Engine.

The typical flow is:

1. **`pre_migration_scope.py`** — size the migration before running it (read-only).
2. **`migrate_shield_to_engine.py`** — perform the migration.
3. **`verify_counts.py`** — confirm source and target row counts match afterward.

All three take the same date-window flags — `--last-days`, or `--from-date`
(with an optional `--to-date` that defaults to now). Use the **same window**
across all three so the sizing, migration, and verification cover exactly the
same data.

Shared dependencies:

```bash
pip install sqlalchemy psycopg2-binary requests python-dotenv
```

Any of the environment variables below can be set in the shell or placed in a
`.env` file in this directory (loaded automatically).

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

A date window is **required**: pass `--last-days`, or `--from-date` (with an
optional `--to-date`, which defaults to now). Scoping all of time is not allowed.

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
| `--from-date` | Start date (inclusive), ISO format e.g. `2020-01-01`. Applies to inferences, validation results, and feedback only. |
| `--to-date` | End date (exclusive), ISO format e.g. `2021-01-01`. Optional; defaults to now. Applies to inferences, validation results, and feedback only. |
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

## `migrate_shield_to_engine.py`

Performs the migration. It reads from Shield through the **Shield API** and writes
to Arthur Engine through the **Engine migration API** — it does not connect to
either database directly. Work is split into three phases (`config`, `inferences`,
`feedback`) and checkpointed so a run can be safely resumed.

### Setup

| Variable | Description |
|---|---|
| `SHIELD_BASE_URL` | Base URL of the Shield API (no trailing slash) |
| `SHIELD_API_KEY` | Shield admin API key |
| `ENGINE_BASE_URL` | Base URL of the Engine API (no trailing slash) |
| `ENGINE_API_KEY` | Engine admin API key |
| `ENGINE_ORG_ID` | UUID of the Engine org to migrate data into |
| `MIGRATION_CHECKPOINT_DIR` | *(optional)* directory for checkpoint files, default `migration_states` |
| `SHIELD_PAGE_SIZE` | *(optional)* rows fetched per Shield page, default `5000` |
| `ENGINE_BATCH_SIZE` | *(optional)* inferences per POST to the Engine, default `500` |
| `MIGRATION_TIMEOUT` | *(optional)* per-request HTTP timeout in seconds, default `30` |

### Usage

A date window is **required**: pass `--last-days`, or `--from-date` (with an
optional `--to-date`, which defaults to now). Use the same window you scoped
with. When resuming with `--resume`, the window is read from the checkpoint and
these flags are not needed.

```bash
# Migrate everything in the window (config, then inferences, then feedback)
python migrate_shield_to_engine.py --phase all --last-days 90

# Migrate a single phase
python migrate_shield_to_engine.py --phase config
python migrate_shield_to_engine.py --phase inferences --from-date 2020-01-01 --to-date 2021-01-01

# Resume an interrupted run from its checkpoint file
python migrate_shield_to_engine.py --phase inferences --resume migration_states/migration_state_2020-01-01_to_2021-01-01.json
```

### Options

| Flag | Description |
|---|---|
| `--phase` | Which phase to run: `all` (default), `config`, `inferences`, or `feedback`. |
| `--from-date` | Start date (inclusive), ISO format. |
| `--to-date` | End date (exclusive), ISO format. Optional; defaults to now. |
| `--last-days` | Shorthand to migrate the last N days. Takes precedence over `--from-date`/`--to-date`. |
| `--resume` | Path to an existing checkpoint (`migration_state_*.json`) to resume from. |

> **Note:** Config (tasks, rules, default rules, and task–rule links) is always
> migrated in full; the date window only applies to inferences and feedback.
> Progress is checkpointed per phase, so re-running with the same window skips
> work that already completed.

## `verify_counts.py`

Run **after** a migration to confirm it was complete. It connects to **both**
databases directly and compares row counts for the same date window: Shield
(source) vs Engine (target). Each row prints `shield=… engine=…` with a ✓ when
they match and ✗ when they don't. A final section checks that no org-scoped rows
were written to the Engine without an `org_id`. Exits `0` if everything matches,
`1` otherwise.

### Setup

Needs both database connections. The Shield variables are the same
`SHIELD_POSTGRES_*` set used by `pre_migration_scope.py`; the Engine variables are
the identical set with an `ENGINE_` prefix.

Shield (source) database:

| Variable | Description |
|---|---|
| `SHIELD_POSTGRES_USER` | Database user |
| `SHIELD_POSTGRES_PASSWORD` | Database password |
| `SHIELD_POSTGRES_URL` | Database host |
| `SHIELD_POSTGRES_PORT` | Database port |
| `SHIELD_POSTGRES_DB` | Database name |
| `SHIELD_POSTGRES_USE_SSL` | *(optional)* `"true"`/`"false"`, default `false` |
| `SHIELD_POSTGRES_SSL_ROOT_CERT` | *(optional)* path to CA cert, used when SSL is on |

Engine (target) database:

| Variable | Description |
|---|---|
| `ENGINE_POSTGRES_USER` | Database user |
| `ENGINE_POSTGRES_PASSWORD` | Database password |
| `ENGINE_POSTGRES_URL` | Database host |
| `ENGINE_POSTGRES_PORT` | Database port |
| `ENGINE_POSTGRES_DB` | Database name |
| `ENGINE_POSTGRES_USE_SSL` | *(optional)* `"true"`/`"false"`, default `false` |
| `ENGINE_POSTGRES_SSL_ROOT_CERT` | *(optional)* path to CA cert, used when SSL is on |
| `ENGINE_ORG_ID` | UUID of the Engine org the data was migrated into |

Example `.env` for a local setup (Shield on 5432, Engine on 5433):

```bash
SHIELD_POSTGRES_USER="postgres"
SHIELD_POSTGRES_PASSWORD="changeme_pg"
SHIELD_POSTGRES_URL="localhost"
SHIELD_POSTGRES_PORT="5432"
SHIELD_POSTGRES_DB="arthur_shield"

ENGINE_POSTGRES_USER="postgres"
ENGINE_POSTGRES_PASSWORD="changeme_pg_password"
ENGINE_POSTGRES_URL="localhost"
ENGINE_POSTGRES_PORT="5433"
ENGINE_POSTGRES_DB="arthur_genai_engine"

ENGINE_ORG_ID="<target-org-uuid>"
```

### Usage

A date window is **required** — pass `--last-days`, or `--from-date` (with an
optional `--to-date`, which defaults to now). Use the **same window** the
migration ran with.

```bash
python verify_counts.py --last-days 90
python verify_counts.py --from-date 2020-01-01 --to-date 2021-01-01
```

### Output

```
======================================================================
  Shield → Engine Migration Verification
  Window: from 2020-01-01 to 2021-01-01
  Org:    <target-org-uuid>
======================================================================

Inferences
  ✓          inferences                   shield=1,204,556  engine=1,204,556
  ✓          inference_prompts            shield=1,204,556  engine=1,204,556
  ...

Validation (rule) results
  ✓          prompt_rule_results          shield=1,204,556  engine=1,204,556
  ✓          response_rule_results        shield=1,150,003  engine=1,150,003

Feedback
  ✓          inference_feedback           shield=9,812  engine=9,812

Rows for org-scoped resources missing an org_id (each should be 0)
  ✓          prompt_rule_results          rows missing org_id: 0
  ...

======================================================================
  RESULT: ALL MATCH ✓
======================================================================
```
