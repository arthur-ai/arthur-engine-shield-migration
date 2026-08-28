# migrate_api_keys.py
"""
Copies API keys from the Shield PostgreSQL database into the Engine database.

Shield never exposes key_hash over its REST API — ApiKeyResponse omits it — so
this migration reads both databases directly instead of going through the API
like migrate_shield_to_engine.py does.

Both systems hash keys with bcrypt (rounds=9) and hand them out as
base64("{id}:{key}"), and their api_keys tables agree column for column apart
from the Engine's org_id. Copying id and key_hash verbatim therefore keeps every
existing Shield key working against the Engine untouched — nothing has to be
reissued or redistributed. Keys are global in both systems, not task-scoped.

The roles column is BYTEA holding UTF-8 JSON on both sides (JsonType), and the
role strings themselves are identical, so roles copy through as raw bytes with
no transformation.

Shield DB connection:
    SHIELD_POSTGRES_USER
    SHIELD_POSTGRES_PASSWORD
    SHIELD_POSTGRES_URL
    SHIELD_POSTGRES_PORT
    SHIELD_POSTGRES_DB
    SHIELD_POSTGRES_USE_SSL         (optional, "true"/"false", default false)
    SHIELD_POSTGRES_SSL_ROOT_CERT   (optional, path to CA cert when SSL on)

Engine DB connection:
    ENGINE_POSTGRES_USER
    ENGINE_POSTGRES_PASSWORD
    ENGINE_POSTGRES_URL
    ENGINE_POSTGRES_PORT
    ENGINE_POSTGRES_DB
    ENGINE_POSTGRES_USE_SSL         (optional, "true"/"false", default false)
    ENGINE_POSTGRES_SSL_ROOT_CERT   (optional, path to CA cert when SSL on)
    ENGINE_ORG_ID                   (optional, default for --org-id; stamped on
                                    every migrated key. Unset means org_id NULL,
                                    which makes the keys cross-org admin keys.)

Engine API (used only to read the Engine's live MAX_API_KEYS cap):
    ENGINE_BASE_URL

Every key in scope is migrated. If that leaves the Engine over its cap, the run
warns but still copies everything. The cap is read from the Engine itself, so it
always reflects what the target instance enforces.

Every --execute run records the ids it inserted to a save file, so the migrated
keys can be told apart from keys created natively in the Engine and removed
again with delete_migrated_api_keys.py --save-file <path>. The file lands in
MIGRATION_CHECKPOINT_DIR unless --save-file names another path.

Usage:
    python migrate_api_keys.py
    python migrate_api_keys.py --execute
    python migrate_api_keys.py --include-inactive --execute
    python migrate_api_keys.py --org-id <uuid> --execute
    python migrate_api_keys.py --key-ids <key_id_1> <key_id_2> --execute
    python migrate_api_keys.py --from-date 2025-01-01 --to-date 2026-01-01 --execute
    python migrate_api_keys.py --last-days 90 --execute
    python migrate_api_keys.py --execute --save-file ./api_key_migration.json
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from progress import Heartbeat  # noqa: E402

load_dotenv(os.path.join(SCRIPTS_DIR, ".env"))

# Same genai-engine/migration_states that migrate_shield_to_engine.py uses.
CHECKPOINT_DIR = os.getenv(
    "MIGRATION_CHECKPOINT_DIR",
    default=os.path.join(os.path.dirname(SCRIPTS_DIR), "migration_states"),
)

# Line buffering keeps progress lines and the report ordered under `| tee`.
sys.stdout.reconfigure(line_buffering=True)

# Columns copied verbatim from Shield. org_id is Engine-only and set separately.
COPIED_COLUMNS = [
    "id",
    "key_hash",
    "description",
    "is_active",
    "created_at",
    "deactivated_at",
    "roles",
]

# The Engine interpolates its MAX_API_KEYS into the create-key endpoint's
# description, so its OpenAPI spec reports the cap the running instance enforces.
MAX_KEYS_PATTERN = re.compile(r"Up to (\d+) active keys")


def build_engine(prefix: str) -> Engine:
    """Build a SQLAlchemy engine for the Shield or Engine database."""
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


def fetch_max_api_keys() -> int:
    """Read the cap the target Engine actually enforces, from its OpenAPI spec.

    The Engine builds the create-key endpoint's description from the same
    Config.max_api_key_limit() its cap check uses, so this cannot drift.
    """
    base_url = os.getenv("ENGINE_BASE_URL")
    if not base_url:
        print(
            "ENGINE_BASE_URL is not set; it is needed to read the Engine's "
            "MAX_API_KEYS cap.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        response = requests.get(f"{base_url.rstrip('/')}/openapi.json", timeout=30)
        response.raise_for_status()
        spec = response.json()
    except (requests.RequestException, ValueError) as e:
        print(
            f"Could not read the Engine's MAX_API_KEYS cap from {base_url}: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    for path, operations in spec.get("paths", {}).items():
        if "api_keys" not in path:
            continue
        for operation in operations.values():
            match = MAX_KEYS_PATTERN.search(operation.get("description") or "")
            if match:
                return int(match.group(1))

    print(
        f"Could not find the MAX_API_KEYS cap in the Engine's OpenAPI spec at "
        f"{base_url}.",
        file=sys.stderr,
    )
    sys.exit(1)


def engine_has_org_id(conn: Connection) -> bool:
    return bool(
        conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'api_keys' AND column_name = 'org_id'",
            ),
        ).scalar(),
    )


def parse_window(args):
    """Resolve the created_at window, matching migrate_shield_to_engine.py."""
    now = datetime.now(timezone.utc)

    if args.last_days:
        return now - timedelta(days=args.last_days), now

    from_dt = datetime.fromisoformat(args.from_date) if args.from_date else None
    to_dt = datetime.fromisoformat(args.to_date) if args.to_date else None

    return from_dt, to_dt


def fetch_shield_keys(
    conn: Connection,
    include_inactive: bool,
    key_ids=None,
    from_dt=None,
    to_dt=None,
) -> list:
    columns = ", ".join(COPIED_COLUMNS)
    clauses, params = [], {}
    if key_ids:
        # An explicit id list is the scope: an id names one key, so honour it
        # whether or not that key is still active.
        clauses.append("id IN :ids")
        params["ids"] = key_ids
    elif not include_inactive:
        clauses.append("is_active")
    if from_dt:
        clauses.append("created_at >= :from_dt")
        params["from_dt"] = from_dt
    if to_dt:
        clauses.append("created_at < :to_dt")
        params["to_dt"] = to_dt
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    statement = text(f"SELECT {columns} FROM api_keys {where} ORDER BY created_at")
    if key_ids:
        statement = statement.bindparams(bindparam("ids", expanding=True))

    with Heartbeat("reading Shield api_keys"):
        rows = conn.execute(statement, params).mappings()
        return [dict(row) for row in rows]


def existing_engine_keys(conn: Connection, keys: list) -> tuple[set, set]:
    """Return the ids and key_hashes already present in the Engine.

    Both are checked: id is the primary key and key_hash carries a unique
    constraint, so either one colliding would abort the insert.
    """
    if not keys:
        return set(), set()

    id_stmt = text("SELECT id FROM api_keys WHERE id IN :ids").bindparams(
        bindparam("ids", expanding=True),
    )
    ids = set(conn.execute(id_stmt, {"ids": [k["id"] for k in keys]}).scalars())

    hash_stmt = text(
        "SELECT key_hash FROM api_keys WHERE key_hash IN :hashes",
    ).bindparams(bindparam("hashes", expanding=True))
    hashes = set(
        conn.execute(hash_stmt, {"hashes": [k["key_hash"] for k in keys]}).scalars(),
    )
    return ids, hashes


def describe_roles(raw) -> str:
    """Render the BYTEA roles column for the report."""
    if raw is None:
        return "-"
    try:
        # psycopg2 hands BYTEA back as a memoryview.
        value = bytes(raw).decode("utf-8") if not isinstance(raw, str) else raw
        return ",".join(json.loads(value))
    except (UnicodeDecodeError, ValueError):
        return "<unreadable>"


def insert_keys(conn: Connection, keys: list, org_id, with_org_id: bool) -> int:
    columns = list(COPIED_COLUMNS)
    if with_org_id:
        columns.append("org_id")
    placeholders = ", ".join(f":{c}" for c in columns)
    statement = text(
        f"INSERT INTO api_keys ({', '.join(columns)}) VALUES ({placeholders})",
    )

    inserted = 0
    for key in keys:
        row = dict(key)
        if with_org_id:
            row["org_id"] = org_id
        conn.execute(statement, row)
        inserted += 1
    return inserted


def default_save_path() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return os.path.join(CHECKPOINT_DIR, f"api_key_migration_{stamp}.json")


def write_save_file(path: str, keys: list, org_id, with_org_id: bool) -> None:
    """Record the inserted ids so delete_migrated_resources.py can undo the run.

    Migrated keys are indistinguishable from keys created natively in the
    Engine, so without this record there is nothing to roll back against.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    state = {
        "migrated_api_key_ids": [key["id"] for key in keys],
        "org_id": str(org_id) if with_org_id and org_id else None,
        "migrated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def report(keys: list, skipped_ids: set, skipped_hashes: set) -> None:
    print(f"\nFound {len(keys) + len(skipped_ids) + len(skipped_hashes)} Shield key(s)")
    for key in keys:
        state = "active" if key["is_active"] else "inactive"
        description = key["description"] or "(no description)"
        print(
            f"  + {key['id']}  {state:8}  [{describe_roles(key['roles'])}]  "
            f"{description}",
        )
    for key_id in sorted(skipped_ids):
        print(f"  = {key_id}  already migrated")
    for key_id in sorted(skipped_hashes):
        print(f"  ! {key_id}  key_hash already present under a different id")


def main():
    parser = argparse.ArgumentParser(
        description="Copy API keys from the Shield database into the Engine database.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually insert. Without this flag the script only lists what it would copy.",
    )
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Also copy deactivated keys. By default only active keys are "
        "migrated. Ignored with --key-ids, which always copies the ids named.",
    )
    parser.add_argument(
        "--key-ids",
        nargs="+",
        default=None,
        metavar="KEY_ID",
        help="Migrate only these Shield API keys, active or not.",
    )
    parser.add_argument(
        "--from-date",
        default=None,
        help="Start of created_at window (inclusive), e.g. 2020-01-01",
    )
    parser.add_argument(
        "--to-date",
        default=None,
        help="End of created_at window (exclusive), e.g. 2021-01-01",
    )
    parser.add_argument(
        "--last-days",
        type=int,
        default=None,
        help="Shorthand: migrate keys created in the last N days",
    )
    parser.add_argument(
        "--org-id",
        default=os.getenv("ENGINE_ORG_ID"),
        help="Org to stamp on migrated keys. Defaults to ENGINE_ORG_ID. "
        "Omit for cross-org admin keys (org_id NULL).",
    )
    parser.add_argument(
        "--save-file",
        help="Where to record the migrated key ids for rollback. Defaults to a "
        "timestamped file in MIGRATION_CHECKPOINT_DIR.",
    )
    args = parser.parse_args()

    shield_engine = build_engine("SHIELD")
    engine_engine = build_engine("ENGINE")

    from_dt, to_dt = parse_window(args)

    with shield_engine.connect() as shield_conn:
        shield_keys = fetch_shield_keys(
            shield_conn,
            args.include_inactive,
            args.key_ids,
            from_dt,
            to_dt,
        )

    if args.key_ids:
        found = {key["id"] for key in shield_keys}
        missing = [key_id for key_id in args.key_ids if key_id not in found]
        if missing:
            print(f"  WARNING: key ids not found in Shield: {', '.join(missing)}")

    if not shield_keys:
        print("No API keys found in Shield to migrate.")
        return

    with engine_engine.connect() as engine_conn:
        with_org_id = engine_has_org_id(engine_conn)
        existing_ids, existing_hashes = existing_engine_keys(engine_conn, shield_keys)
        active_before = engine_conn.execute(
            text("SELECT count(*) FROM api_keys WHERE is_active"),
        ).scalar_one()

    # A key already migrated shows up by id. A key whose hash is present under a
    # different id would violate the unique constraint, so it is reported and
    # skipped rather than retried on every run.
    pending, skipped_ids, skipped_hashes = [], set(), set()
    for key in shield_keys:
        if key["id"] in existing_ids:
            skipped_ids.add(key["id"])
        elif key["key_hash"] in existing_hashes:
            skipped_hashes.add(key["id"])
        else:
            pending.append(key)

    report(pending, skipped_ids, skipped_hashes)

    if not with_org_id:
        print(
            "\nNote: this Engine database has no api_keys.org_id column; "
            "keys are being migrated without org scoping.",
        )
    elif not args.org_id:
        print(
            "\nWarning: no --org-id or ENGINE_ORG_ID set. Migrated keys will have "
            "org_id NULL, making them cross-org api keys.",
        )

    max_keys = fetch_max_api_keys()
    incoming_active = sum(1 for k in pending if k["is_active"])
    active_after = active_before + incoming_active
    if incoming_active and active_after > max_keys:
        print(
            f"\nWarning: the Engine will hold {active_after} active keys. This is over its "
            f"MAX_API_KEYS limit of {max_keys}. Please update the max api key limit "
            f"if you would like to be able to create more api keys.",
        )

    if not pending:
        print("\nNothing to migrate — every Shield key is already in the Engine.")
        return

    if not args.execute:
        print(
            f"\nDry run — nothing written. Re-run with --execute to copy the "
            f"{len(pending)} key(s) marked + above into the Engine database.",
        )
        return

    print(f"\nMigrating {len(pending)} key(s)...")
    try:
        # One transaction: a failure partway leaves the Engine untouched rather
        # than half-migrated.
        with engine_engine.begin() as engine_conn:
            inserted = insert_keys(engine_conn, pending, args.org_id, with_org_id)
    except SQLAlchemyError as e:
        print(
            f"\nMigration failed — no keys were copied, the Engine is unchanged.\n"
            f"  {type(e).__name__}: {str(e).splitlines()[0]}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Written after the commit so the file only ever lists keys that landed.
    save_file = args.save_file or default_save_path()
    write_save_file(save_file, pending, args.org_id, with_org_id)

    print(
        f"\nDone. Copied {inserted} API key(s). They keep their original secrets — "
        f"existing Shield keys now authenticate against the Engine unchanged.",
    )
    print(f"  Recorded {inserted} key id(s) in {save_file}")
    print(f"  To undo: python delete_migrated_api_keys.py --save-file {save_file}")


if __name__ == "__main__":
    main()
