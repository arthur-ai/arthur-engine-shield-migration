# delete_migrated_api_keys.py
"""
Deletes the API keys that migrate_api_keys.py inserted into the Engine, directly
against the Engine PostgreSQL database (no API).

Migrated keys are indistinguishable from keys created natively in the Engine —
same table, same shape — so this script only ever touches ids recorded in a
migrate_api_keys.py save file. It never infers what to delete from the Shield
database or from a description pattern.

Engine DB connection (same env vars as delete_migrated_resources.py):
    ENGINE_POSTGRES_USER
    ENGINE_POSTGRES_PASSWORD
    ENGINE_POSTGRES_URL
    ENGINE_POSTGRES_PORT
    ENGINE_POSTGRES_DB
    ENGINE_POSTGRES_USE_SSL         (optional, "true"/"false", default false)
    ENGINE_POSTGRES_SSL_ROOT_CERT   (optional, path to CA cert when SSL on)

Usage:
    python delete_migrated_api_keys.py --save-file migration_states/api_key_migration_<stamp>.json
    python delete_migrated_api_keys.py --save-file <path> --execute
    python delete_migrated_api_keys.py --save-file <path> --deactivate --execute
"""

import argparse
import json
import os
import sys
import urllib.parse

from dotenv import load_dotenv
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

load_dotenv(os.path.join(SCRIPTS_DIR, ".env"))

sys.stdout.reconfigure(line_buffering=True)


def get_engine() -> Engine:
    user = os.environ["ENGINE_POSTGRES_USER"]
    password = os.environ["ENGINE_POSTGRES_PASSWORD"]
    host = os.environ["ENGINE_POSTGRES_URL"]
    port = os.environ["ENGINE_POSTGRES_PORT"]
    db_name = os.environ["ENGINE_POSTGRES_DB"]

    params = {}
    if os.getenv("ENGINE_POSTGRES_USE_SSL", "false").lower() == "true":
        params["sslmode"] = "verify-full"
        root_cert = os.getenv("ENGINE_POSTGRES_SSL_ROOT_CERT")
        if root_cert:
            params["sslrootcert"] = root_cert
    query = f"?{urllib.parse.urlencode(params)}" if params else ""

    return create_engine(
        f"postgresql+psycopg2://{user}:{urllib.parse.quote_plus(password)}"
        f"@{host}:{port}/{db_name}{query}",
        pool_pre_ping=True,
    )


def load_key_ids(save_file: str) -> list:
    if not os.path.exists(save_file):
        print(f"Save file not found: {save_file}", file=sys.stderr)
        sys.exit(1)
    with open(save_file) as f:
        state = json.load(f)
    return state.get("migrated_api_key_ids", [])


def fetch_present(engine: Engine, key_ids: list) -> list:
    statement = text(
        "SELECT id, description, is_active FROM api_keys WHERE id IN :ids "
        "ORDER BY is_active DESC, id",
    ).bindparams(bindparam("ids", expanding=True))
    with engine.connect() as conn:
        return list(conn.execute(statement, {"ids": key_ids}).mappings())


def main():
    parser = argparse.ArgumentParser(
        description="Delete the API keys recorded in a migrate_api_keys.py save file.",
    )
    parser.add_argument(
        "--save-file",
        required=True,
        help="Path to the api_key_migration_*.json file written by migrate_api_keys.py",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually apply. Without this flag the script only lists what it would do.",
    )
    parser.add_argument(
        "--deactivate",
        action="store_true",
        help="Deactivate the keys instead of deleting them, preserving the audit "
        "trail. Deactivated keys can no longer authenticate and no longer count "
        "toward the Engine's MAX_API_KEYS cap.",
    )
    args = parser.parse_args()

    key_ids = load_key_ids(args.save_file)
    if not key_ids:
        print(f"No migrated_api_key_ids recorded in {args.save_file}.")
        return

    engine = get_engine()
    present = fetch_present(engine, key_ids)
    present_ids = [row["id"] for row in present]
    missing = [key_id for key_id in key_ids if key_id not in set(present_ids)]

    verb, gerund, past = (
        ("deactivate", "Deactivating", "deactivated")
        if args.deactivate
        else ("delete", "Deleting", "deleted")
    )
    print(f"{len(key_ids)} key id(s) recorded in {args.save_file}")
    for row in present:
        state = "active" if row["is_active"] else "inactive"
        description = row["description"] or "(no description)"
        print(f"  - {row['id']}  {state:8}  {description}")
    for key_id in missing:
        print(f"  = {key_id}  already gone from the Engine")

    if not present_ids:
        print("\nNothing to do — none of the recorded keys are in the Engine.")
        return

    if not args.execute:
        print(
            f"\nDry run — nothing changed. Re-run with --execute to {verb} "
            f"the {len(present_ids)} key(s) above.",
        )
        return

    print(f"\n{gerund} {len(present_ids)} key(s)...")
    if args.deactivate:
        statement = text(
            "UPDATE api_keys SET is_active = false, deactivated_at = now() "
            "WHERE id IN :ids AND is_active",
        ).bindparams(bindparam("ids", expanding=True))
    else:
        statement = text("DELETE FROM api_keys WHERE id IN :ids").bindparams(
            bindparam("ids", expanding=True),
        )

    try:
        with engine.begin() as conn:
            affected = conn.execute(statement, {"ids": present_ids}).rowcount
    except SQLAlchemyError as e:
        print(
            f"\nFailed — no keys were changed, the Engine is unchanged.\n"
            f"  {type(e).__name__}: {str(e).splitlines()[0]}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"\nDone. {affected} API key(s) {past}.")


if __name__ == "__main__":
    main()
