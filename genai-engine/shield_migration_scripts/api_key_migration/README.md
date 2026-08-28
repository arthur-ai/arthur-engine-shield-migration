# API Key Migration

Standalone scripts for migrating API keys from Arthur Shield to Arthur Engine.

The typical flow is:

1. **`migrate_api_keys.py`** — copy the keys (dry run by default).
2. **`verify_api_keys.py`** — confirm the copy is faithful.
3. **`delete_migrated_api_keys.py`** — roll back if needed.

Every `--execute` run writes a save file recording the ids it inserted. The
other two scripts take that file, so they always act on exactly what that run
migrated — never on keys created natively in the Engine.

## Contents

- [API Key Migration](#api-key-migration)
  - [Contents](#contents)
  - [Why these read the database directly](#why-these-read-the-database-directly)
  - [Setup](#setup)
  - [`migrate_api_keys.py`](#migrate_api_keyspy)
    - [Options](#options)
    - [Idempotency](#idempotency)
  - [`verify_api_keys.py`](#verify_api_keyspy)
  - [`delete_migrated_api_keys.py`](#delete_migrated_api_keyspy)
  - [The MAX\_API\_KEYS cap](#the-max_api_keys-cap)
  - [Org scoping](#org-scoping)

## Why these read the database directly

Unlike [`migrate_shield_to_engine.py`](../migrate_shield_to_engine.py), which
goes through both REST APIs, these scripts connect to both PostgreSQL databases.

Shield never exposes `key_hash` over its API — its `ApiKeyResponse` omits the
field entirely, and a plaintext key is shown exactly once at creation. Reading
the database is the only way to get it.

That matters because both systems hash keys with bcrypt (rounds 9) and hand them
out as `base64("{id}:{key}")`, and their `api_keys` tables agree column for
column apart from the Engine's `org_id`. Copying `id` and `key_hash` verbatim
therefore keeps **every existing Shield key working against the Engine
untouched** — nothing has to be reissued or redistributed to users.

The `roles` column is `BYTEA` holding UTF-8 JSON on both sides, and the role
strings are identical (`ORG-ADMIN`, `TASK-ADMIN`, `VALIDATION-USER`,
`DEFAULT-RULE-ADMIN`, `ORG-AUDITOR`), so roles copy through with no
transformation. The Engine adds `TENANT-USER`, which Shield cannot produce.

API keys are **global in both systems** — there is no `task_id` on `api_keys`,
so keys are not scoped to tasks or applications.

## Setup

These scripts read the `.env` in the parent
[`shield_migration_scripts/`](../) directory, so they can be run from anywhere.
Required variables:

| Variable | Purpose |
| --- | --- |
| `SHIELD_POSTGRES_USER` / `_PASSWORD` / `_URL` / `_PORT` / `_DB` | Shield database (source) |
| `ENGINE_POSTGRES_USER` / `_PASSWORD` / `_URL` / `_PORT` / `_DB` | Engine database (target) |
| `ENGINE_BASE_URL` | Engine API, used only to read its live `MAX_API_KEYS` cap |

Optional:

| Variable | Purpose |
| --- | --- |
| `SHIELD_POSTGRES_USE_SSL` / `ENGINE_POSTGRES_USE_SSL` | `"true"`/`"false"`, default false |
| `SHIELD_POSTGRES_SSL_ROOT_CERT` / `ENGINE_POSTGRES_SSL_ROOT_CERT` | Path to CA cert when SSL is on |
| `ENGINE_ORG_ID` | Default for `--org-id`; see [Org scoping](#org-scoping) |
| `MIGRATION_CHECKPOINT_DIR` | Where save files land, default `genai-engine/migration_states/` |

> **Note:** Shield and the Engine usually run on different PostgreSQL ports.
> Pointing `ENGINE_POSTGRES_PORT` at Shield's port yields a confusing
> authentication failure rather than an obvious error.

## `migrate_api_keys.py`

Copies keys from Shield into the Engine. **Dry run by default** — it lists what
it would copy and writes nothing until you pass `--execute`.

```bash
python migrate_api_keys.py                                          # dry run, all active keys
python migrate_api_keys.py --execute
python migrate_api_keys.py --include-inactive --execute
python migrate_api_keys.py --key-ids <key_id_1> <key_id_2> --execute
python migrate_api_keys.py --from-date 2025-01-01 --to-date 2026-01-01 --execute
python migrate_api_keys.py --last-days 90 --execute
python migrate_api_keys.py --execute --save-file ./api_key_migration.json
```

### Options

| Option | Effect |
| --- | --- |
| `--execute` | Actually insert. Without it, nothing is written. |
| `--include-inactive` | Also copy deactivated keys. Ignored with `--key-ids`. |
| `--key-ids ID [ID ...]` | Migrate only these keys, active or not. |
| `--from-date` / `--to-date` | `created_at` window: from is inclusive, to is exclusive. |
| `--last-days N` | Shorthand for the last N days. Wins over the date flags. |
| `--org-id` | Org stamped on migrated keys. Defaults to `ENGINE_ORG_ID`. |
| `--save-file` | Where to record inserted ids. Defaults to a timestamped file in `MIGRATION_CHECKPOINT_DIR`. |

All scoping flags are optional; with none of them, every active key is migrated.
The date flags match `migrate_shield_to_engine.py`'s semantics, but unlike that
script a window is **not** required here.

`--key-ids` names specific keys, so it overrides the active-only default — a
deactivated key migrates when you name its id. A date window applies on top of
whatever set is selected.

### Idempotency

Re-running is safe. Keys already in the Engine are reported as
`= already migrated` and skipped. A key whose `key_hash` is present under a
*different* id is reported with `!` and skipped, since the unique constraint on
`key_hash` would otherwise abort the insert.

The insert runs in **one transaction** — a failure partway leaves the Engine
untouched rather than half-migrated.

## `verify_api_keys.py`

Compares the two databases for the ids in a save file and reports ✓ / ✗ per
check. Exits non-zero if anything fails.

```bash
python verify_api_keys.py --save-file ../../migration_states/api_key_migration_<stamp>.json
```

The save file defines the scope: every recorded id is checked in both databases,
whether or not the key is still active.

| Check | What it proves |
| --- | --- |
| **Counts** | Every recorded id is present in both Shield and the Engine. |
| **Fidelity** | `key_hash`, `description`, `is_active`, `deactivated_at` and `roles` match per key. |
| **Org scope** | Every migrated key carries the `org_id` the run recorded in the save file. |

A `key_hash` mismatch is called out explicitly — that is the field that decides
whether a migrated key still authenticates, so a difference there means the key
is dead even though the row exists. `roles` is decoded from `BYTEA` and compared
as a set, so byte ordering is not a false failure.

## `delete_migrated_api_keys.py`

Rolls back a migration run. Only ever touches ids recorded in a save file — it
never infers what to delete from Shield or from a description pattern, so keys
created natively in the Engine are safe.

```bash
python delete_migrated_api_keys.py --save-file <path>                        # dry run
python delete_migrated_api_keys.py --save-file <path> --execute              # delete
python delete_migrated_api_keys.py --save-file <path> --deactivate --execute
```

`--deactivate` sets `is_active = false` and stamps `deactivated_at` instead of
deleting the rows. The keys stop authenticating but the audit trail survives —
usually the safer choice in production. Note that deactivated rows still occupy
their ids and `key_hash` values, so a later re-migration of the same keys will
be skipped as already present.

Ids already gone from the Engine are reported and skipped, so re-running is
safe.

## The MAX_API_KEYS cap

The Engine refuses to create a key once `MAX_API_KEYS` active keys exist
(default 100). The check runs *before* each insert, so a cap of 50 means the
Engine will hold at most 50 keys.

A direct database insert bypasses that check, so a migration can legitimately
leave the Engine holding more than `MAX_API_KEYS` active keys. Every key in
scope is migrated regardless.

When that happens the run prints a warning. Raise `MAX_API_KEYS` on the Engine
if you want to be able to create new keys afterward.

The cap is read from the **target Engine's** OpenAPI spec via `ENGINE_BASE_URL`,
not from a local environment variable. The Engine builds that endpoint
description from the same `Config.max_api_key_limit()` its enforcement uses, so
the number cannot drift from what the instance actually enforces.

Only **active** keys count toward the cap, on both sides — deactivated keys
migrate without consuming a slot.

## Org scoping

Shield is single-tenant: it has no `org_id`, no organizations table, and no
concept of org scoping anywhere in its codebase. The Engine is multi-tenant, so
the migration has to *choose* a value rather than carry one over.

In the Engine, `api_keys.org_id` is meaningful:

- **Non-null** — the key is a tenant key scoped to that org, and reaches only
  that org's tasks.
- **NULL** — the key is a **cross-org api key**, exempt from org-scope
  enforcement.

`--org-id` (defaulting to `ENGINE_ORG_ID`) stamps migrated keys with an org so
they line up with the tasks and inferences the main migration placed there. With
neither set, keys land as `org_id NULL` — cross-org api keys — and the script
warns loudly.

The Engine's own `POST /auth/api_keys/` cannot set `org_id` at all, so direct
SQL is currently the only way to create org-scoped keys.
