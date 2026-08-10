# Shield Migration Scripts

Standalone scripts for migrating data from Arthur Shield to Arthur Engine.

The typical flow is:

1. **`pre_migration_scope.py`** — size the migration before running it (read-only).
2. **`migrate_shield_to_engine.py`** — perform the migration.
3. **`verify_counts.py`** — confirm source and target row counts match afterward.
4. **`onboard_tasks_from_csv.py`** — link the migrated tasks to models in the Arthur platform.

`pre_migration_scope.py` and `migrate_shield_to_engine.py` take the same
date-window flags — `--last-days`, or `--from-date` (with an optional
`--to-date` that defaults to now). Use the **same window** for both so the
sizing and migration cover exactly the same data. `verify_counts.py` takes the
migration run's checkpoint file instead, so it always verifies exactly what
that run migrated.

There is also **`delete_migrated_resources.py`** for rolling back a migration. It deletes everything a specific migration run inserted into the Engine, using that run's checkpoint file.

> **Note:** Archived tasks and inferences/feedback referencing archived tasks are
> excluded from the migration. When running the pre_migration_scope.py in exact mode, the
> number of tasks and inferences not migrated will be reported. And, similarly, in the
> verify_counts.py script, the number that weren't migrated will be reported.

## Contents

- [Prerequisite: run the Engine in migration mode](#prerequisite-run-the-engine-in-migration-mode)
- [Progress output](#progress-output)
- [`pre_migration_scope.py`](#pre_migration_scopepy)
  - [Setup](#setup)
  - [Usage](#usage)
  - [Options](#options)
  - [Estimate mode (`--estimate`)](#estimate-mode---estimate)
  - [Output](#output)
- [`migrate_shield_to_engine.py`](#migrate_shield_to_enginepy)
  - [Setup](#setup-1)
  - [Usage](#usage-1)
  - [Options](#options-1)
  - [Per-task migration (`--task-ids`)](#per-task-migration---task-ids)
  - [Timing report (`--timing`)](#timing-report---timing)
- [`verify_counts.py`](#verify_countspy)
  - [Config reconciliation (API mode)](#config-reconciliation-api-mode)
  - [Caveats block](#caveats-block)
  - [Setup](#setup-2)
  - [Usage](#usage-2)
  - [Options](#options-2)
  - [Output](#output-1)
- [`onboard_tasks_from_csv.py`](#onboard_tasks_from_csvpy)
  - [A task is never linked twice](#a-task-is-never-linked-twice)
  - [Resuming](#resuming)
  - [Before a large CSV: raise the engine's API key limit](#before-a-large-csv-raise-the-engines-api-key-limit)
  - [Setup](#setup-3)
  - [Input CSV](#input-csv)
  - [Usage](#usage-3)
  - [Options](#options-3)
  - [Output](#output-2)
- [`delete_migrated_resources.py`](#delete_migrated_resourcespy)
  - [Setup](#setup-4)
  - [Usage](#usage-4)
  - [Options](#options-4)

## Prerequisite: run the Engine in migration mode

`migrate_shield_to_engine.py` writes through the Engine's migration API
(`POST /api/v1/migration/*`). **Those routes only exist when the Engine is
running with migration mode enabled**, and it is **disabled by default**. With it
off the routes are never registered, so every migration request comes back `404`
— which looks like a wrong `ENGINE_BASE_URL` rather than a config problem.

The Engine reads `GENAI_ENGINE_MIGRATION_MODE`; it must be `enabled`
(case-insensitive). Any other value, or unset, means off. Set it through
whichever deployment path you use:

| Deployment | Setting | Default |
|---|---|---|
| Helm | `genaiEngineMigrationMode: "enabled"` in `values.yaml` | `"disabled"` |
| CloudFormation (ECS) | `GenaiEngineMigrationMode: enabled` parameter | `disabled` |
| Docker / local | `GENAI_ENGINE_MIGRATION_MODE=enabled` | unset (off) |

Changing it requires a restart of the Engine — the flag is read once at app
startup when routes are registered.

```bash
# Helm
helm upgrade arthur-genai-engine ... --set genaiEngineMigrationMode=enabled

# Local
export GENAI_ENGINE_MIGRATION_MODE=enabled
uv run serve
```

Confirm it took effect before starting a migration — the endpoints show up in the
Engine's OpenAPI spec under the `Migration` tag:

```bash
curl -s "$ENGINE_BASE_URL/openapi.json" | grep -c '/api/v1/migration/'
# 0 => migration mode is off
```

> **Turn migration mode back off once the migration is verified.** It exposes
> bulk-write endpoints that insert tasks, rules, inferences, and feedback with
> caller-supplied IDs and timestamps, bypassing the normal validation path. Leave
> it enabled only for the duration of the migration.

`pre_migration_scope.py`, `verify_counts.py`, and `delete_migrated_resources.py`
talk to PostgreSQL directly and do **not** need migration mode.

Install the dependencies for all scripts:

```bash
pip install -r requirements.txt
```

Copy the **whole directory** — every script imports `progress.py` as a sibling
module, so a single `.py` lifted out on its own will not run.

Any of the environment variables below can be set in the shell or placed in a
`.env` file in this directory (loaded automatically).

## Progress output

These scripts routinely run for hours (a ~1B-inference migration, or a single
`COUNT(*)` joining across billion-row tables), so each one reports what it is
doing as it goes.

**On a terminal** a single line is rewritten in place, showing position, rate and
an estimated time remaining:

```
  [ 42.3%] 423,000 / 1,000,000 inferences scanned · 1,240.0/s · ETA 7m 45s · 5m 41s elapsed · 422,000 inserted, 1,000 skipped (page 84)
```

Work with no measurable position — the big `COUNT(*)`s in `verify_counts.py` and
`pre_migration_scope.py`, the bulk config inserts — ticks its elapsed time
instead, then reports how long it took:

```
  counting inference_prompt_contents (arthur_shield)… 4m 12s
  ✓ counting inference_prompt_contents (arthur_shield) (4m 31s)
```

**When the output is piped or redirected**, the same information is appended as
whole lines at most once every `MIGRATION_PROGRESS_INTERVAL` seconds (default
`2`) instead of being rewritten in place. A multi-hour run therefore produces a
few hundred log lines rather than one per page. Set
`MIGRATION_PROGRESS_INTERVAL=0` to drop the live line entirely; each step still
reports its completion and duration.

Everything goes to **stdout**, so `| tee migration.log` and `> report.txt`
capture exactly what the terminal showed, in order.

`onboard_tasks_from_csv.py` also logs a permanent line per row as each link
settles. Those scroll normally while the counter stays pinned to the bottom:

```
[task-a1b2] linked (model m-7f3e)
[task-c3d4] already linked (model m-9a1b), skipping
[ 42.0%] 210 / 500 rows onboarded · 0.4/s · ETA 12m 05s · 8m 30s elapsed · 4 running, 286 queued
```

> **Note:** `verify_counts.py` and `pre_migration_scope.py` buffer their report
> and print it in full at the end. Their section headers therefore appear twice —
> once live as each section starts, once in the final report. The report file
> `pre_migration_scope.py --output-dir` writes is built from the buffered lines
> and never contains progress output.

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

The target Engine must be running with **migration mode enabled**
(`genaiEngineMigrationMode` / `GENAI_ENGINE_MIGRATION_MODE`) or every write here
returns `404` — see [Prerequisite: run the Engine in migration mode](#prerequisite-run-the-engine-in-migration-mode).

| Variable | Description |
|---|---|
| `SHIELD_BASE_URL` | Base URL of the Shield API (no trailing slash) |
| `SHIELD_API_KEY` | Shield admin API key |
| `ENGINE_BASE_URL` | Base URL of the Engine API (no trailing slash) |
| `ENGINE_API_KEY` | Engine admin API key |
| `ENGINE_ORG_ID` | UUID of the Engine org to migrate data into |
| `MIGRATION_CHECKPOINT_DIR` | *(optional)* directory for checkpoint files, default `migration_states` |
| `SHIELD_PAGE_SIZE` | *(optional)* rows fetched per Shield page, default `4999`. Must be greater than 0 and less than 5000. |
| `ENGINE_BATCH_SIZE` | *(optional)* inferences per POST to the Engine, default `500` |
| `MIGRATION_TIMEOUT` | *(optional)* per-request HTTP timeout in seconds, default `30` |
| `MIGRATION_MAX_WORKERS` | *(optional)* concurrent Engine POSTs, default `10` |
| `MIGRATION_SHIELD_FETCH_WORKERS` | *(optional)* concurrent Shield page fetchers, default `3` |
| `MIGRATION_PREFETCH_PAGES` | *(optional)* Shield pages buffered ahead of the Engine writers, default `10` |
| `MIGRATION_PROGRESS_INTERVAL` | *(optional)* seconds between progress updates, default `2`. `0` disables the live line. Applies to every script — see [Progress output](#progress-output). |

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

# Migrate by task(s) (config, inferences, and feedback for that task only).
# A date window is still required, same as a full run.
python migrate_shield_to_engine.py --task-ids <task_id> --last-days 90
python migrate_shield_to_engine.py --task-ids <task_id_1> <task_id_2> --from-date 2025-01-01
```

### Options

| Flag | Description |
|---|---|
| `--phase` | Which phase to run: `all` (default), `config`, `inferences`, or `feedback`. |
| `--from-date` | Start date (inclusive), ISO format. |
| `--to-date` | End date (exclusive), ISO format. Optional; defaults to now. |
| `--last-days` | Shorthand to migrate the last N days. Takes precedence over `--from-date`/`--to-date`. |
| `--task-ids` | One or more Shield task IDs. Scopes all three phases to those tasks. A date window is still required. |
| `--resume` | Path to an existing checkpoint (`migration_state_*.json`) to resume from. |
| `--timing` | Print a timing report at the end of the run. |

> **Note:** Config (tasks, rules, default rules, and task–rule links) is always
> migrated in full; the date window only applies to inferences and feedback.
> Progress is checkpointed per phase, so re-running with the same window skips
> work that already completed.

> **Archived rules** are migrated at the **start of the inferences phase**, not
> during config, because what needs them is historical rule results: a
> `prompt_rule_results.rule_id` / `response_rule_results.rule_id` may point at a
> rule that has since been archived, and those rows cannot be inserted before the
> rule exists. They are always migrated in full — Shield cannot filter archived
> rules by task. The step is checkpointed separately
> (`archived_rules_migrated`), so resuming an interrupted inferences phase does
> not refetch them.
>
> Config still inserts any archived rule that a **live task links to**, since the
> task–rule link would otherwise reference a rule that does not exist yet. Those
> rules come from the task payload, which carries no archived flag, so the script
> asks Shield which of them are still active and marks the rest archived — a rule
> archived in Shield stays archived in the Engine and is not evaluated against new
> traffic.

### Per-task migration (`--task-ids`)

`--task-ids` scopes the run to specific tasks, so a large migration can be done
one task (or a few tasks) at a time:

- **Config** — only the selected tasks, their rules, and their task–rule links
  are migrated. Default rules are still migrated in full, because they are
  global.
- **Inferences** — filtered by task in Shield's migration export endpoint, so
  only the selected tasks' inferences are fetched. Task-less inferences are
  never migrated by a task-scoped run. Archived rules are migrated in full at
  the start of this phase regardless of task scope: the selected tasks'
  historical rule results may reference rules that have since been archived, and
  Shield cannot filter archived rules by task.
- **Feedback** — filtered server-side by Shield, so only the selected tasks'
  feedback is fetched.

Each task scope gets its own checkpoint file, so runs for different tasks never
conflict with each other, and resuming a task-scoped run picks up where that
scope left off. Rolling back with `delete_migrated_resources.py` works the same
as for a full run — pass that run's checkpoint file.

The checkpoint file also records the IDs of every task the run migrated
(`migrated_task_ids`), every rule it inserted (`migrated_rule_ids`), and every
migrated inference that has no task (`migrated_taskless_inference_ids`). These
lists are what `delete_migrated_resources.py` uses to roll the migration
back, so keep the checkpoint file around after a run completes.

### Timing report (`--timing`)

With `--timing`, a report is printed at the end of the run. The config phase
shows the duration of each step; the inferences phase shows its archived-rule
steps followed by total time, per-record average, and the **measured**
wall-clock time of each full 10k / 100k / 1m chunk (a chunk line appears only
once at least one full chunk of that size completed); the feedback phase shows
the same totals without the step lines. Chunks are measured for records
processed in the current run only, so a resumed run's numbers reflect that run
alone — and on a resume the archived-rule steps are absent, because that work is
already checkpointed as done.

The inferences `Total` includes the archived-rule steps, so on a first run the
`Average per inference` carries that fixed cost too. It is negligible on a large
run and can be misleading on a very small one.

```
=== Timing Report ===

config phase:
  Fetch tasks: 1.3s
  Fetch rules: 0.9s
  Insert rules: 1.7s
  Insert tasks: 3.4s
  Insert task-rule links: 1.1s
  Total: 8.4s

inferences phase:
  Fetch archived rules: 2.1s
  Insert archived rules: 4.8s
  Total: 48m 02s (1,010,260 inferences)
  Average per inference: 2.85 ms
  Per 10k inferences (measured): avg 28.5s, fastest 13.0s, slowest 1m 38s (101 full chunks)
  Per 100k inferences (measured): avg 4m 45s, fastest 4m 02s, slowest 5m 31s (10 full chunks)
  Per 1m inferences (measured): avg 47m 33s, fastest 47m 33s, slowest 47m 33s (1 full chunks)

feedback phase:
  Total: 0.5s (1 feedback records)
  Average per feedback record: 452.62 ms
```

## `verify_counts.py`

Run **after** a migration to confirm it was complete. It reads the date window,
task scope, and migrated IDs from the migration run's checkpoint file, then
connects to **both** databases directly and compares row counts: Shield (source)
vs Engine (target). Engine-side counts are scoped to the IDs the run recorded
(`migrated_task_ids`, `migrated_rule_ids`, `migrated_taskless_inference_ids`),
so data in the Engine that the run didn't insert never affects the comparison.
Each row prints `shield=… engine=…` with a ✓ when they match and ✗ when they
don't. A config section verifies every migrated task, rule, and task–rule link
exists in the Engine; a rule-result-details section compares the full detail
tree (`rule_result_details` plus hallucination claims, PII entities, keyword
matches, regex matches, and toxicity scores); a final section checks that no
org-scoped rows were written to the Engine without an `org_id`. Exits `0` if
everything matches, `1` otherwise.

Only sections whose phase is recorded as completed in the checkpoint file are
verified — a run that only finished `config` gets only the Config section.

With `--api-mode` the script verifies through the **Shield and Engine APIs**
instead of the databases. Coverage is shallower: inferences and feedback are
compared by window counts (plus per-ID checks of recorded task-less
inferences), and the rule-result/detail tables and org-id sanity check are
skipped since they aren't reachable through the APIs.

### Config reconciliation (API mode)

API mode adds a **Config reconciliation** block to the Config section, which
names tasks, rules, and task→rule links rather than only counting them. The
counting rows above it answer "how many"; this answers "which ones", so a red
line doesn't turn into a manual ID-by-ID hunt.

Each set is diffed against a different baseline, so none subsumes another:

| Group | Set | Diff | Cost | Exit code |
|---|---|---|---|---|
| Tasks | requested task(s) never migrated | (`task_ids` − `migrated_task_ids`) − Engine | free | fails |
| Tasks | requested task(s) in the Engine but not recorded | (`task_ids` − `migrated_task_ids`) ∩ Engine | free | informational |
| Tasks | migrated task(s) missing from the Engine | `migrated_task_ids` − Engine | free | fails |
| Tasks | active task(s) not part of this migration | Engine − `migrated_task_ids` | 1 call | informational |
| Links | link(s) missing from the Engine | Shield task rules − Engine task rules | free | fails |
| Links | unexpected link(s) in the Engine | Engine task rules − Shield task rules | free | fails |
| Rules | active rule(s) missing from the Engine | Shield-active − Engine-active | free¹ | fails |
| Rules | recorded rule(s) archived in Shield | Shield-archived − Shield-active | 1 call | informational |
| Rules | recorded rule(s) unknown to Shield | recorded − Shield-active − Shield-archived | free¹ | informational |
| Rules | active rule(s) not part of this migration | Engine − `migrated_rule_ids` | 1 call | informational |

¹ The rule searches were already being made; they are now paginated for IDs
instead of fetched for a bare count.

Four things worth knowing:

- **The link check sees what the `task_rule_links` row cannot.** That row sums
  link counts across tasks, so a migration that drops one link and gains another
  nets to zero and reports ✓. Comparing the per-task rule sets catches it. Both
  sides embed archived rules in a task's `rules`, so the comparison is
  like-for-like.
- **Archived rules cannot be verified through the Engine API at all.**
  `/api/v2/rules/search` returns active rules only — `SearchRulesRequest` has no
  `include_archived` field. But `migrated_rule_ids` *includes* archived rules,
  which `migrate_archived_rules` records. Diffing the recorded IDs straight
  against the Engine would therefore flag every archived rule as missing, so the
  recorded IDs are partitioned on the Shield side (which does honour
  `include_archived`) and archived ones are reported as unverifiable rather than
  missing. To actually check them, re-run without `--api-mode`.
- **"Never migrated" needs a `--task-ids` run.** Without one the checkpoint
  records no requested scope, and the line is omitted rather than shown as a ✓
  it can't support. This is the one check that catches a task dropping out of
  scope silently: if a requested ID is archived in Shield or mistyped,
  `migrate_shield_to_engine.py` warns once at migration time and records
  nothing, and every other count in this report is scoped to
  `migrated_task_ids` — so without it the run reports `ALL MATCH ✓`.
- **A requested task the run didn't record is not automatically a defect.**
  Re-running a phase over data the Engine already holds inserts nothing, so the
  bulk endpoints return nothing and the checkpoint records nothing — leaving
  `phases_completed: ["config"]` alongside an empty `migrated_task_ids` even
  though the tasks are present. Those are reported separately as
  `! requested task(s) are in the Engine but were not recorded by this run`,
  pointing at `--recover` to rebuild the checkpoint, and they do not affect the
  exit code. Only a requested task that is *also* absent from the Engine counts
  as never migrated.
- **The "not part of this migration" lists span every org and cover active rows
  only.** Neither the task nor the rule search has an org filter, and
  `ENGINE_API_KEY` is an `ORG_ADMIN` key, so both lists cover every org the key
  can see rather than just `ENGINE_ORG_ID`. Both searches also exclude archived
  rows, so a migrated task later archived in the Engine shows up as missing
  rather than as an extra. The rule list necessarily includes every default
  rule, since those are never migration-specific.

### Caveats block

Both modes close with a **Caveats** block spelling out what the result does and
does not cover, so a screen of green ticks isn't mistaken for proof the
migration was byte-for-byte correct. The bullets are computed from the run, not
boilerplate: phases that weren't checked are named, and caveats about output
that wasn't produced are omitted. The standing ones are that every comparison is
a row count rather than a field-level check; that the Config `shield=` column
for tasks (and, in SQL mode, rules) is the count the checkpoint recorded rather
than a live Shield query; and that Engine-side counts are scoped to the IDs the
checkpoint recorded, so a truncated checkpoint narrows what is compared instead
of failing.

### Setup

SQL mode (default) needs both database connections. The Shield variables are the
same `SHIELD_POSTGRES_*` set used by `pre_migration_scope.py`; the Engine
variables are the identical set with an `ENGINE_` prefix.

API mode instead uses the same API variables as `migrate_shield_to_engine.py`:
`SHIELD_BASE_URL`, `SHIELD_API_KEY`, `ENGINE_BASE_URL`, `ENGINE_API_KEY`
(plus optional `MIGRATION_TIMEOUT` / `MIGRATION_MAX_WORKERS`).

| Variable | Description |
|---|---|
| `VERIFY_LIST_LIMIT` | *(optional)* rows to print of the unbounded reconciliation lists — the "not part of this migration" task and rule sets, and the task→rule link sets. Default `20`; `0` lists them all. Lists bounded by the checkpoint's own recorded IDs (tasks and rules reported as never migrated, missing, or unknown) always print in full. |

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

Pass the checkpoint file of the migration run to verify with `--save-file`.
The date window and task scope (if the run used `--task-ids`) are read from it.

```bash
python verify_counts.py --save-file migration_states/migration_state_2020-01-01_to_2021-01-01.json

# Verify through the APIs instead of direct SQL
python verify_counts.py --save-file migration_states/migration_state_2020-01-01_to_2021-01-01.json --api-mode
```

### Options

| Flag | Description |
|---|---|
| `--save-file` | Path to the `migration_state_*.json` checkpoint of the run to verify. Required. |
| `--api-mode` | Verify through the Shield and Engine APIs instead of direct SQL. Shallower coverage; see above. |

### Output

For a task-scoped run, a `Tasks:` line lists the task IDs and all counts are
scoped to those tasks.

```
======================================================================
  Shield → Engine Migration Verification
  Window: from 2020-01-01 to 2021-01-01
  Org:    <target-org-uuid>
  Phases: config, inferences, feedback
======================================================================

Config (migrated IDs recorded in the save file)
  ✓          tasks                        shield=42  engine=42
  ✓          task_rule_links              shield=213  engine=213
  ✓          rules                        shield=124  engine=124

Shield tasks archived (not migrated): 7,561

Inferences
  ✓          inferences                   shield=1,204,556  engine=1,204,556
  ✓          inference_prompts            shield=1,204,556  engine=1,204,556
  ...

Validation (rule) results
  ✓          prompt_rule_results          shield=1,204,556  engine=1,204,556
  ✓          response_rule_results        shield=1,150,003  engine=1,150,003

Rule result details
  ✓          rule_result_details          shield=2,354,559  engine=2,354,559
  ✓          hallucination_claims         shield=88,411  engine=88,411
  ✓          pii_entities                 shield=41,006  engine=41,006
  ✓          keyword_matches              shield=12,733  engine=12,733
  ✓          regex_matches                shield=8,290  engine=8,290
  ✓          toxicity_scores              shield=2,354,559  engine=2,354,559

Feedback
  ✓          inference_feedback           shield=9,812  engine=9,812

Rows for org-scoped resources missing an org_id (each should be 0)
  ✓          prompt_rule_results          rows missing org_id: 0
  ...

======================================================================
  RESULT: ALL MATCH ✓
======================================================================

```
With `--api-mode`:
```
======================================================================
  Shield → Engine Migration Verification
  Window: from 2020-01-01 to 2021-01-01
  Mode:   api
  Phases: config, inferences, feedback
======================================================================

Config (migrated IDs recorded in the save file)
  ✓          tasks                        shield=42  engine=42
  ✓          task_rule_links              shield=213  engine=213
  ✓          rules (active)               shield=124  engine=124

  Config reconciliation
    Tasks
      ✓ all 42 requested task(s) migrated
      ✓ all 42 migrated task(s) present in the Engine
      ✓ no tasks in the Engine outside this migration
    Task→rule links
      ✓ all 213 task→rule link(s) reproduced in the Engine
    Rules
      ✓ all 124 active rule(s) present in the Engine
      ✓ no rules in the Engine outside this migration

Inferences
  ✓          inferences                   shield=1,204,556  engine=1,204,556
  ✓          taskless_inferences          shield=31  engine=31

Feedback
  ✓          inference_feedback           shield=9,812  engine=9,812

======================================================================
  RESULT: ALL MATCH ✓
======================================================================

Caveats — what the result above does and does not cover:
  • Counts only. Matching totals do not prove field-level fidelity: an
    inference whose contents were truncated or mangled in transit still
    counts as one row on each side.
  • In the Config section the shield= column for tasks is the count the save
    file recorded, not a live Shield query. Those rows prove the Engine
    holds what the run believed it migrated — not that the run picked up
    everything Shield held.
  • Engine-side counts are scoped to the IDs the save file recorded, so a
    truncated or mismatched save file narrows what is compared rather than
    failing. A ✓ against a save file that recorded little proves little.
  • API mode does not check the rule-result tables, the rule-result detail
    tree (hallucination claims, PII entities, keyword and regex matches,
    toxicity scores), or that org-scoped Engine rows carry an org_id. Re-run
    without --api-mode to cover those.
  • Archived rules cannot be verified through the Engine API at all — its
    rule search returns active rules only. The rules row and the missing-
    rule list cover the active subset; archived rules are counted and named
    as unverifiable, never as present.
  • The "not part of this migration" lists span every org visible to
    ENGINE_API_KEY — neither the task nor the rule search has an org filter
    — and cover active rows only, so archived Engine tasks and rules never
    appear in them.
```

When the reconciliation finds something, it names what is wrong instead of
leaving the operator to diff IDs by hand. Note the `task_rule_links` row: it is
green because one dropped link and one spurious link cancel out in the sum, and
only the set diff below it sees the difference.

```
Config (migrated IDs recorded in the save file)
  ✗ MISMATCH tasks                        shield=42  engine=40
  ✓          task_rule_links              shield=213  engine=213
  ✗ MISMATCH rules (active)               shield=124  engine=122

  Config reconciliation
    Tasks
      ✗ 1 requested task(s) never migrated:
         9z8y7x6w-…
        (absent from Shield when the run fetched tasks, or the config phase
         stopped before recording them)
      ✗ 2 migrated task(s) missing from the Engine:
         a1b2c3d4-… (Support Copilot)
         c3d4e5f6-… (Claims Triage)
      ! 415 active task(s) in the Engine were not part of this migration
        (spans every org visible to ENGINE_API_KEY; informational only):
         e5f6a7b8-… (Legacy Chatbot)
         g7h8i9j0-… (Doc QA)
         … 413 more (set VERIFY_LIST_LIMIT=0 to list them all)
    Task→rule links
      ✗ 1 link(s) missing from the Engine:
         a1b2c3d4-… (Support Copilot) → 7f3a… (PII Check)
      ✗ 1 unexpected link(s) in the Engine:
         a1b2c3d4-… (Support Copilot) → 2a6f… (Keyword)
    Rules
      ✗ 2 active rule(s) missing from the Engine:
         7f3a… (PII Check)
         9c1b… (Toxicity)
      ! 14 recorded rule(s) are archived in Shield — the Engine rule
        search returns active rules only, so this cannot confirm them.
        Re-run without --api-mode.
      ! 37 active rule(s) in the Engine were not part of this migration
        (includes every default rule; spans every org visible to
        ENGINE_API_KEY; informational only):
         d1f4… (Default Hallucination)
         … 36 more (set VERIFY_LIST_LIMIT=0 to list them all)
```

## `onboard_tasks_from_csv.py`

Run after a migration to onboard the migrated tasks as GenAI applications in
the Arthur platform. For each row in an input CSV, it links an existing engine
task to a new model in a scope project via the platform's `link_task` job — the
same flow as the UI's "link existing application". The job creates the dataset,
the model, and the task's validation key server-side, so no dataset upload or
refresh is needed first.

The script keeps up to `--max-in-flight` link jobs open at a time, polls each
to completion, and writes a results CSV with the outcome of every row.

### A task is never linked twice

The platform does **not** reject a second link for a task that already has one
([UP-4804](https://arthurai.atlassian.net/browse/UP-4804)) — it silently creates
another model, another dataset, and another engine API key, both pointing at
the same task. The script therefore checks before every submission, in two
ways:

| Check | Catches | Result status |
|---|---|---|
| Model with onboarding identifier `csv-link:<task_id>` in the project | links made by an earlier run of this script | `skipped_already_linked` |
| Any dataset in the project whose locator has `task_id = <task_id>`, and that backs a model | links made by anyone — the **UI**, another tool, a hand-rolled API call | `skipped_pre_existing` |

The second check is what covers UI-created links, which carry no onboarding
identifier. There is no server-side filter on locator values, so the project's
datasets are listed once per project and scanned locally.

Rows caught by either check are skipped, kept in the results CSV with the
existing model's ID in `model_id` and an explanation in `detail`, and
`skipped_pre_existing` rows are additionally listed in a block at the end of
the run:

```
1 task(s) were already linked outside this script and were NOT linked again:
  9c3f… (project 4b21…) -> model 7ade…
  Remove them from the CSV, or investigate the existing models — linking again
  would have created duplicates.
```

A pre-existing link is information, not a failure — it does not change the exit
code. A dataset that names the task but backs **no** model is treated as a
half-finished link rather than a link, and the row proceeds.

### Resuming

Every change is checkpointed to a state file
(`onboarding_states/onboarding_state_<input>.json`) as it happens, so an
interrupted run — Ctrl-C, a crash, an expired token, a job that outlives
`--max-wait` — is resumed by re-running **the same command**:

- rows that finished are skipped without an API call;
- rows whose link job was still running are re-attached to **by job ID** rather
  than resubmitted;
- rows that failed are retried from the top.

Before submitting, each row is also checked against the project's in-flight
link jobs, so a job that was created but never recorded (a crash between the
API call and the checkpoint write) is adopted instead of duplicated. Pass
`--restart` to discard the checkpoint and run the CSV from scratch.

> **Note:** a link job that fails *after* creating the model leaves that model
> in place. A later run finds it by onboarding identifier and reports
> `skipped_already_linked`, so cross-check any row that failed once against the
> project activity log before treating a later skip as a success.
>
> Both duplicate checks are read-then-write with no server-side constraint
> behind them (UP-4804), so they cannot close every race. Two copies of this
> script run concurrently over the same CSV can both pass the checks and both
> link. Run one at a time.

### Before a large CSV: raise the engine's API key limit

Every link job creates a validation API key in the GenAI Engine, and the engine
caps **total active keys** at `MAX_API_KEYS` (default `100`) across all
purposes, not just validation keys. Onboarding more tasks than the remaining
headroom fails the excess rows with
`400 Bad Request: Maximum number of active keys reached`.

Raise `MAX_API_KEYS` on the engine before running a CSV larger than that, then
re-run the same command — only the failed rows are retried.

### Setup

Talks to the **Arthur platform API** (not the Shield or Engine databases), so
it needs the platform host and, for non-interactive auth, a service account:

| Variable | Description |
|---|---|
| `ARTHUR_API_HOST` | Arthur platform base URL (e.g. `https://platform.arthur.ai`). Required. |
| `ARTHUR_CLIENT_ID` | Service account client ID |
| `ARTHUR_CLIENT_SECRET` | Service account client secret |
| `ONBOARDING_RESULTS_DIR` | *(optional)* directory for results CSVs, default `genai-engine/onboarding_results` |
| `ONBOARDING_STATE_DIR` | *(optional)* directory for resume checkpoints, default `genai-engine/onboarding_states` |
| `ONBOARDING_REQUEST_TIMEOUT` | *(optional)* seconds per platform API call, default `30` |
| `MIGRATION_PROGRESS_INTERVAL` | *(optional)* seconds between progress updates, default `2`. `0` disables the live line. See [Progress output](#progress-output). |

When `ARTHUR_CLIENT_ID`/`ARTHUR_CLIENT_SECRET` are not set, the script falls
back to interactive browser (device) auth.

### Input CSV

A header row is required. Column names are matched case-insensitively and a
byte-order mark (from an Excel export) is tolerated. Rows that repeat the same
`task_id`/`project_id` pair are dropped with a warning — each would otherwise
create its own model.

| Column | Required | Description |
|---|---|---|
| `task_id` | yes | Engine task ID to link |
| `project_id` | yes | Scope project to create the model in |
| `org_id` | no | Informational only, echoed in the results CSV |
| `connector_id` | no | Engine-internal connector ID; auto-resolved from the project when omitted |

A project gets one engine-internal connector per associated data plane. When a
project has more than one, auto-resolution is ambiguous and the row fails with
the candidates listed — set `connector_id` explicitly to choose the engine.

### Usage

```bash
python onboard_tasks_from_csv.py --csv-path tasks.csv

# Resume an interrupted run — same command
python onboard_tasks_from_csv.py --csv-path tasks.csv

# Start over, ignoring the checkpoint
python onboard_tasks_from_csv.py --csv-path tasks.csv --restart

# Custom results path, gentler on the data plane
python onboard_tasks_from_csv.py --csv-path tasks.csv --results-csv results.csv \
    --poll-interval 5 --max-in-flight 3
```

### Options

| Flag | Description |
|---|---|
| `--csv-path` | Input CSV with `task_id`/`project_id` columns. Required. |
| `--results-csv` | Output CSV path. Default: `onboarding_results/<input>_results_<timestamp>.csv`. |
| `--state-file` | Resume checkpoint path. Default: `onboarding_states/onboarding_state_<input>.json`. |
| `--restart` | Delete the checkpoint and run every row again. |
| `--poll-interval` | Seconds between job status polls, default `2`. |
| `--max-in-flight` | Link jobs kept open at once, default `10`. |
| `--max-wait` | Seconds to wait on a single job before giving up on it, default `1800`. `0` waits indefinitely. |

### Output

Progress is printed per row, and a results CSV is written with one row per
input row: `task_id`, `project_id`, `org_id`, `connector_id`, `status`,
`job_id`, `model_id`, `detail`, and `error`.

| Status | Meaning |
|---|---|
| `linked` | this run created the model |
| `skipped_already_linked` | an earlier run of this script had already linked it |
| `skipped_pre_existing` | already linked outside this script; see `detail` |
| `failed` | see `error`, which carries the platform's own message; for failed jobs also check the project activity log |

Both the results CSV and the checkpoint are written even when the run is
interrupted or aborts. The script exits `0` when every row is onboarded, `1`
when any row failed or was left unfinished, and `130` when interrupted.

```
[task-1] link job submitted: 6f2c…
[task-2] already linked (model 9a1b…), skipping
[task-3] already linked outside this script to model 'Support Agent' (7ade…) via dataset 51bc…, skipping
1 job(s) still running...
[task-1] linked (model 3c4d…)
Checkpoint saved to onboarding_states/onboarding_state_tasks.json
Results written to onboarding_results/tasks_results_20260806_141530.csv

1 task(s) were already linked outside this script and were NOT linked again:
  task-3 (project 4b21…) -> model 7ade…
  Remove them from the CSV, or investigate the existing models — linking again would have created duplicates.

Done: 1 linked, 1 already linked, 1 pre-existing, 0 failed
```

## `delete_migrated_resources.py`

Rolls back a migration. It reads the `migrated_task_ids`,
`migrated_taskless_inference_ids`, and `migrated_rule_ids` recorded in a
migration run's checkpoint file and deletes everything that run inserted,
directly against the **Engine PostgreSQL database**.

For each migrated task it deletes the inference subtree (rule results and
their detail rows, feedback, prompt/response contents, inferences) in
FK-safe batched transactions, then removes task–rule links, migrated and
orphaned rules, and the tasks themselves. Task-less inferences recorded in
the checkpoint are deleted the same way.

**The script is a dry run by default** — it only lists what it would delete. Pass `--execute` to actually delete.

### Setup

Uses the same `ENGINE_POSTGRES_*` environment variables as `verify_counts.py`.

| Variable | Description |
|---|---|
| `MIGRATION_SQL_BATCH_SIZE` | *(optional)* inferences deleted per transaction, default `25000` |
| `MIGRATION_SQL_WORKERS` | *(optional)* parallel delete connections, default `4` |

### Usage

```bash
# Dry run — list the tasks, inferences, and rules that would be deleted
python delete_migrated_resources.py --save-file migration_states/migration_state_2026-01-09_to_2026-07-08.json

# Actually delete
python delete_migrated_resources.py --save-file migration_states/migration_state_2026-01-09_to_2026-07-08.json --execute
```

### Options

| Flag | Description |
|---|---|
| `--save-file` | Path to the `migration_state_*.json` checkpoint of the run to roll back. Required. |
| `--execute` | Actually delete. Without it, the script only prints what it would delete. |

> **Warning:** Deletion is permanent. Run without `--execute` first and review
> the list of tasks, then re-run with `--execute`.
