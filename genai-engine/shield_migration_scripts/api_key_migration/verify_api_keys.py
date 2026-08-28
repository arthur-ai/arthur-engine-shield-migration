# verify_api_keys.py
"""
Verifies an API key migration by comparing the Shield and Engine api_keys tables
directly. Reports a ✓ when a check passes, ✗ when it does not.

Checks:
  - Counts       — Shield keys vs the same ids in the Engine
  - Fidelity     — key_hash, description, is_active, deactivated_at and roles
                   match per key. key_hash is what makes a migrated key keep
                   working, so a mismatch there means the key is dead.
  - Org scope    — every migrated key carries the org_id the run recorded

The ids recorded by a migrate_api_keys.py run define the scope: every one of
them is checked in both databases, whether or not the key is still active.

Shield DB connection (source):
    SHIELD_POSTGRES_USER
    SHIELD_POSTGRES_PASSWORD
    SHIELD_POSTGRES_URL
    SHIELD_POSTGRES_PORT
    SHIELD_POSTGRES_DB
    SHIELD_POSTGRES_USE_SSL         (optional, "true"/"false", default false)
    SHIELD_POSTGRES_SSL_ROOT_CERT   (optional, path to CA cert when SSL on)

Engine DB connection (target): same variables with an ENGINE_ prefix
    ENGINE_POSTGRES_USER
    ENGINE_POSTGRES_PASSWORD
    ENGINE_POSTGRES_URL
    ENGINE_POSTGRES_PORT
    ENGINE_POSTGRES_DB
    ENGINE_POSTGRES_USE_SSL         (optional, "true"/"false", default false)
    ENGINE_POSTGRES_SSL_ROOT_CERT   (optional, path to CA cert when SSL on)

Usage:
    python verify_api_keys.py --save-file ../../migration_states/api_key_migration_<stamp>.json
"""

import argparse
import json
import os
import sys
import urllib.parse

from dotenv import load_dotenv
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Engine

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from progress import Heartbeat  # noqa: E402

load_dotenv(os.path.join(SCRIPTS_DIR, ".env"))

sys.stdout.reconfigure(line_buffering=True)

COMPARED_COLUMNS = ["key_hash", "description", "is_active", "deactivated_at", "roles"]

BULLET = "      "
ROW = "         "


def build_engine(prefix: str) -> Engine:
    user = os.environ[f"{prefix}_POSTGRES_USER"]
    password = os.environ[f"{prefix}_POSTGRES_PASSWORD"]
    host = os.environ[f"{prefix}_POSTGRES_URL"]
    port = os.environ[f"{prefix}_POSTGRES_PORT"]
    db_name = os.environ[f"{prefix}_POSTGRES_DB"]

    params = {}
    if os.getenv(f"{prefix}_POSTGRES_USE_SSL", "false").lower() == "true":
        params["sslmode"] = "verify-full"
        root_cert = os.getenv(f"{prefix}_POSTGRES_SSL_ROOT_CERT")
        if root_cert:
            params["sslrootcert"] = root_cert
    query = f"?{urllib.parse.urlencode(params)}" if params else ""

    return create_engine(
        f"postgresql+psycopg2://{user}:{urllib.parse.quote_plus(password)}"
        f"@{host}:{port}/{db_name}{query}",
        pool_pre_ping=True,
    )


def fmt(n) -> str:
    return f"{n:,}"


def section(lines: list, title: str) -> None:
    lines.append(title)
    print(f"\n{title}")


def compare(label: str, shield_n: int, engine_n: int) -> str:
    status = "✓" if shield_n == engine_n else "✗ MISMATCH"
    return f"  {status:<10} {label:<28} shield={fmt(shield_n)}  engine={fmt(engine_n)}"


def normalize(column: str, value):
    """Make a column comparable across the two databases."""
    if value is None:
        return None
    if column == "roles":
        # BYTEA holding UTF-8 JSON; psycopg2 returns a memoryview. Compare the
        # decoded role set so byte-level ordering differences are not failures.
        try:
            return tuple(sorted(json.loads(bytes(value).decode("utf-8"))))
        except (UnicodeDecodeError, ValueError):
            return "<unreadable>"
    return value


def fetch_keys(engine: Engine, label: str, key_ids=None):
    columns = ", ".join(["id"] + COMPARED_COLUMNS)
    where, params = "", {}
    if key_ids is not None:
        where = "WHERE id IN :ids"
        params["ids"] = key_ids

    statement = text(f"SELECT {columns} FROM api_keys {where}")
    if key_ids is not None:
        statement = statement.bindparams(bindparam("ids", expanding=True))

    with Heartbeat(f"reading {label} api_keys"):
        with engine.connect() as conn:
            rows = conn.execute(statement, params).mappings()
            return {row["id"]: dict(row) for row in rows}


def engine_has_org_id(engine: Engine) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'api_keys' AND column_name = 'org_id'",
                ),
            ).scalar(),
        )


def check_presence(
    lines: list,
    key_ids: list,
    shield_keys: dict,
    engine_keys: dict,
) -> bool:
    section(lines, "API keys")
    lines.append(compare("keys", len(shield_keys), len(engine_keys)))
    print(lines[-1])

    start = len(lines)
    missing_engine = sorted(set(key_ids) - set(engine_keys))
    missing_shield = sorted(set(key_ids) - set(shield_keys))
    if missing_engine:
        lines.append(
            f"{BULLET}✗ {fmt(len(missing_engine))} recorded key(s) missing from "
            f"the Engine:",
        )
        lines.extend(f"{ROW}{key_id}" for key_id in missing_engine)
    if missing_shield:
        lines.append(
            f"{BULLET}✗ {fmt(len(missing_shield))} recorded key(s) missing from "
            f"Shield:",
        )
        lines.extend(f"{ROW}{key_id}" for key_id in missing_shield)
    if not missing_engine and not missing_shield:
        lines.append(
            f"{BULLET}✓ all {fmt(len(key_ids))} recorded key(s) present in both",
        )
    print("\n".join(lines[start:]))
    return not missing_engine and not missing_shield


def check_fidelity(lines: list, shield_keys: dict, engine_keys: dict) -> bool:
    section(lines, "Field fidelity")
    shared = sorted(set(shield_keys) & set(engine_keys))
    mismatches = []
    for key_id in shared:
        for column in COMPARED_COLUMNS:
            source = normalize(column, shield_keys[key_id][column])
            target = normalize(column, engine_keys[key_id][column])
            if source != target:
                mismatches.append((key_id, column, source, target))

    start = len(lines)
    if mismatches:
        lines.append(
            f"  {'✗ MISMATCH':<10} {'fields':<28} "
            f"{fmt(len(mismatches))} difference(s) across {fmt(len(shared))} key(s)",
        )
        for key_id, column, source, target in mismatches:
            note = " — migrated key will NOT authenticate" if column == "key_hash" else ""
            lines.append(f"{ROW}{key_id} {column}: shield={source!r} engine={target!r}{note}")
    else:
        lines.append(
            f"  {'✓':<10} {'fields':<28} "
            f"all {fmt(len(shared))} key(s) match on {', '.join(COMPARED_COLUMNS)}",
        )
    print("\n".join(lines[start:]))
    return not mismatches


def check_org_scope(lines: list, engine: Engine, key_ids: list, expected_org) -> bool:
    section(lines, "Org scope")
    if not engine_has_org_id(engine):
        lines.append(f"  {'-':<10} {'org_id':<28} column not present in this Engine")
        print(lines[-1])
        return True

    statement = text(
        "SELECT COALESCE(CAST(org_id AS TEXT), 'NULL') AS org, count(*) "
        "FROM api_keys WHERE id IN :ids GROUP BY org",
    ).bindparams(bindparam("ids", expanding=True))
    with engine.connect() as conn:
        counts = dict(conn.execute(statement, {"ids": key_ids}).fetchall())

    expected = str(expected_org) if expected_org else "NULL"
    matching = counts.get(expected, 0)
    total = sum(counts.values())
    status = "✓" if matching == total and total else "✗ MISMATCH"

    start = len(lines)
    lines.append(f"  {status:<10} {'org_id':<28} {fmt(matching)}/{fmt(total)} = {expected}")
    for org, count in sorted(counts.items()):
        if org != expected:
            lines.append(f"{ROW}{fmt(count)} key(s) with org_id={org}")
    if expected == "NULL" and matching:
        lines.append(f"{BULLET}note: org_id NULL means cross-org admin keys")
    print("\n".join(lines[start:]))
    return matching == total and bool(total)


def main():
    parser = argparse.ArgumentParser(
        description="Verify an API key migration by comparing Shield and Engine.",
    )
    parser.add_argument(
        "--save-file",
        required=True,
        help="Path to the api_key_migration_*.json file written by migrate_api_keys.py",
    )
    args = parser.parse_args()

    if not os.path.exists(args.save_file):
        print(f"Save file not found: {args.save_file}", file=sys.stderr)
        sys.exit(1)
    with open(args.save_file) as f:
        state = json.load(f)
    key_ids = state.get("migrated_api_key_ids", [])
    if not key_ids:
        print(f"No migrated_api_key_ids recorded in {args.save_file}.")
        return

    expected_org = state.get("org_id")

    shield_engine = build_engine("SHIELD")
    engine_engine = build_engine("ENGINE")

    shield_keys = fetch_keys(shield_engine, "Shield", key_ids)
    engine_keys = fetch_keys(engine_engine, "Engine", key_ids)

    lines = []
    results = [
        check_presence(lines, key_ids, shield_keys, engine_keys),
        check_fidelity(lines, shield_keys, engine_keys),
        check_org_scope(lines, engine_engine, key_ids, expected_org),
    ]

    print()
    print("=" * 72)
    print("\n".join(lines))
    print("=" * 72)
    result = "ALL MATCH ✓" if all(results) else "MISMATCHES FOUND ✗"
    print(f"  RESULT: {result}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
