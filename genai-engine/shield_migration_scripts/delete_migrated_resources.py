# delete_migrated_resources.py
"""
Deletes all Engine resources associated with the tasks that a specific
migration inserted, directly against the Engine PostgreSQL database (no API).
Reads the migrated_task_ids recorded in a migration save file and deletes each
task's inference subtree in FK-safe order, in batches with a commit per batch,
then removes rule links, orphaned rules, and the tasks themselves.

Engine DB connection (same env vars as verify_counts.py):
    ENGINE_POSTGRES_USER
    ENGINE_POSTGRES_PASSWORD
    ENGINE_POSTGRES_URL
    ENGINE_POSTGRES_PORT
    ENGINE_POSTGRES_DB
    ENGINE_POSTGRES_USE_SSL         (optional, "true"/"false", default false)
    ENGINE_POSTGRES_SSL_ROOT_CERT   (optional, path to CA cert when SSL on)

Usage:
    python delete_migrated_resources.py --save-file migration_states/migration_state_2026-01-23_to_2026-07-22.json
    python delete_migrated_resources.py --save-file <path> --execute
"""

import argparse
import json
import os
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from progress import Heartbeat, Progress
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Connection, Engine

load_dotenv()

# Progress redraws a single line in place on a terminal; line buffering keeps it
# flowing under `| tee`, where stdout would otherwise be block-buffered.
sys.stdout.reconfigure(line_buffering=True)

BATCH_SIZE = int(os.getenv("MIGRATION_SQL_BATCH_SIZE", default=25000))
SQL_WORKERS = int(os.getenv("MIGRATION_SQL_WORKERS", default=4))

DETAIL_CHILD_TABLES = [
    "hallucination_claims",
    "pii_entities",
    "keyword_matches",
    "regex_matches",
    "toxicity_scores",
]


def get_engine() -> Engine:
    user = os.environ["ENGINE_POSTGRES_USER"]
    password = os.environ["ENGINE_POSTGRES_PASSWORD"]
    host = os.environ["ENGINE_POSTGRES_URL"]
    port = os.environ["ENGINE_POSTGRES_PORT"]
    db_name = os.environ["ENGINE_POSTGRES_DB"]

    params = {}
    if os.getenv("ENGINE_POSTGRES_USE_SSL", "false").lower() == "true":
        params["sslmode"] = "require"
        root_cert = os.getenv("ENGINE_POSTGRES_SSL_ROOT_CERT")
        if root_cert:
            params["sslrootcert"] = root_cert
    query = f"?{urllib.parse.urlencode(params)}" if params else ""

    return create_engine(
        f"postgresql://{user}:{urllib.parse.quote_plus(password)}@{host}:{port}/{db_name}{query}",
    )


def load_state(save_file: str) -> dict:
    if not os.path.exists(save_file):
        print(f"Save file not found: {save_file}", file=sys.stderr)
        sys.exit(1)
    with open(save_file) as f:
        return json.load(f)


def select_ids(conn: Connection, sql: str, **params) -> list:
    statement = text(sql)
    for name, value in params.items():
        if isinstance(value, list):
            statement = statement.bindparams(bindparam(name, expanding=True))
    return list(conn.execute(statement, params).scalars())


def delete_by_ids(conn: Connection, table: str, column: str, ids: list) -> int:
    if not ids:
        return 0
    statement = text(f"DELETE FROM {table} WHERE {column} IN :ids").bindparams(
        bindparam("ids", expanding=True),
    )
    return conn.execute(statement, {"ids": ids}).rowcount


PROMPT_PATH = (
    "prompt_rule_results r, inference_prompts p "
    "WHERE d.prompt_rule_result_id = r.id "
    "AND r.inference_prompt_id = p.id AND p.inference_id = ANY(:ids)"
)
RESPONSE_PATH = (
    "response_rule_results r, inference_responses p "
    "WHERE d.response_rule_result_id = r.id "
    "AND r.inference_response_id = p.id AND p.inference_id = ANY(:ids)"
)


def delete_inference_batch(conn: Connection, inference_ids: list) -> None:
    ids = {"ids": inference_ids}
    for child in DETAIL_CHILD_TABLES:
        for path in (PROMPT_PATH, RESPONSE_PATH):
            conn.execute(
                text(
                    f"DELETE FROM {child} t USING rule_result_details d, {path} "
                    f"AND t.rule_result_detail_id = d.id",
                ),
                ids,
            )
    for path in (PROMPT_PATH, RESPONSE_PATH):
        conn.execute(
            text(f"DELETE FROM rule_result_details d USING {path}"),
            ids,
        )
    conn.execute(
        text(
            "DELETE FROM prompt_rule_results r USING inference_prompts p "
            "WHERE r.inference_prompt_id = p.id AND p.inference_id = ANY(:ids)",
        ),
        ids,
    )
    conn.execute(
        text(
            "DELETE FROM response_rule_results r USING inference_responses p "
            "WHERE r.inference_response_id = p.id AND p.inference_id = ANY(:ids)",
        ),
        ids,
    )
    conn.execute(
        text("DELETE FROM inference_feedback WHERE inference_id = ANY(:ids)"),
        ids,
    )
    conn.execute(
        text(
            "DELETE FROM inference_prompt_contents c USING inference_prompts p "
            "WHERE c.inference_prompt_id = p.id AND p.inference_id = ANY(:ids)",
        ),
        ids,
    )
    conn.execute(
        text(
            "DELETE FROM inference_response_contents c USING inference_responses p "
            "WHERE c.inference_response_id = p.id AND p.inference_id = ANY(:ids)",
        ),
        ids,
    )
    conn.execute(
        text("DELETE FROM inference_prompts WHERE inference_id = ANY(:ids)"),
        ids,
    )
    conn.execute(
        text("DELETE FROM inference_responses WHERE inference_id = ANY(:ids)"),
        ids,
    )
    conn.execute(text("DELETE FROM inferences WHERE id = ANY(:ids)"), ids)


def delete_batch_transaction(engine: Engine, inference_ids: list) -> int:
    with engine.begin() as conn:
        delete_inference_batch(conn, inference_ids)
    return len(inference_ids)


def delete_task_inferences(engine: Engine, task_ids: list) -> int:
    with engine.connect() as conn:
        with Heartbeat("selecting inference ids"):
            all_ids = select_ids(
                conn,
                "SELECT id FROM inferences WHERE task_id IN :ids",
                ids=task_ids,
            )
    batches = [
        all_ids[start : start + BATCH_SIZE]
        for start in range(0, len(all_ids), BATCH_SIZE)
    ]
    deleted = 0
    with Progress(len(all_ids), "inferences deleted") as tracker:
        with ThreadPoolExecutor(max_workers=SQL_WORKERS) as executor:
            futures = [
                executor.submit(delete_batch_transaction, engine, batch)
                for batch in batches
            ]
            for future in as_completed(futures):
                batch_deleted = future.result()
                deleted += batch_deleted
                tracker.update(batch_deleted)
    return deleted


def delete_taskless_inferences(engine: Engine, inference_ids: list) -> None:
    with Progress(len(inference_ids), "task-less inferences deleted") as tracker:
        for start in range(0, len(inference_ids), BATCH_SIZE):
            batch = inference_ids[start : start + BATCH_SIZE]
            delete_batch_transaction(engine, batch)
            tracker.update(len(batch))


def delete_rules_and_tasks(engine: Engine, task_ids: list, rule_ids: list) -> None:
    # One large transaction with no intermediate progress to report.
    with Heartbeat("deleting rules, links and tasks"), engine.begin() as conn:
        linked_rule_ids = select_ids(
            conn,
            "SELECT DISTINCT rule_id FROM tasks_to_rules WHERE task_id IN :ids",
            ids=task_ids,
        )
        delete_by_ids(conn, "tasks_to_rules", "task_id", task_ids)
        still_linked = (
            set(
                select_ids(
                    conn,
                    "SELECT DISTINCT rule_id FROM tasks_to_rules WHERE rule_id IN :ids",
                    ids=linked_rule_ids,
                ),
            )
            if linked_rule_ids
            else set()
        )
        orphaned_rule_ids = [r for r in linked_rule_ids if r not in still_linked]
        doomed_rule_ids = list(set(orphaned_rule_ids) | set(rule_ids))
        delete_by_ids(conn, "tasks_to_rules", "rule_id", doomed_rule_ids)
        delete_by_ids(conn, "rule_data", "rule_id", doomed_rule_ids)
        delete_by_ids(conn, "rules", "id", doomed_rule_ids)

        # Live validate/trace traffic can write spans and trace metadata for a
        # task after migration; they'd block the task delete below.
        conn.execute(
            text(
                "DELETE FROM metric_results m USING spans s "
                "WHERE m.span_id = s.id AND s.task_id = ANY(:ids)",
            ),
            {"ids": task_ids},
        )
        conn.execute(
            text("DELETE FROM spans WHERE task_id = ANY(:ids)"),
            {"ids": task_ids},
        )
        conn.execute(
            text(
                "DELETE FROM agentic_annotations a USING trace_metadata t "
                "WHERE a.trace_id = t.trace_id AND t.task_id = ANY(:ids)",
            ),
            {"ids": task_ids},
        )
        conn.execute(
            text("DELETE FROM trace_metadata WHERE task_id = ANY(:ids)"),
            {"ids": task_ids},
        )

        delete_by_ids(conn, "tasks", "id", task_ids)
        print(
            f"  Rules: {len(doomed_rule_ids)} rule(s) deleted, "
            f"{len(task_ids)} task(s) deleted",
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--save-file",
        required=True,
        help="Path to the migration_state_*.json file whose tasks should be deleted",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete. Without this flag the script only lists what it would delete.",
    )
    args = parser.parse_args()

    state = load_state(args.save_file)
    task_ids = state.get("migrated_task_ids", [])
    taskless_inference_ids = state.get("migrated_taskless_inference_ids", [])
    rule_ids = state.get("migrated_rule_ids", [])

    if not task_ids and not taskless_inference_ids and not rule_ids:
        print("Nothing recorded in the save file to delete.")
        return

    engine = get_engine()

    with engine.connect() as conn:
        with Heartbeat("counting inferences to delete"):
            inference_count = conn.execute(
                text(
                    "SELECT count(*) FROM inferences WHERE task_id IN :ids",
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": task_ids},
            ).scalar()

    print(f"Found in {args.save_file}:")
    print(f"  {len(task_ids)} task(s) with {inference_count} inference(s)")
    print(f"  {len(taskless_inference_ids)} task-less inference(s)")
    print(f"  {len(rule_ids)} migrated rule(s)")

    if not args.execute:
        print(
            "\nDry run — no resources deleted. Re-run with --execute to delete "
            "the inference subtrees, rules, and tasks listed above directly "
            "from the Engine database.",
        )
        return

    print("\nDeleting resources...")
    delete_task_inferences(engine, task_ids)
    delete_taskless_inferences(engine, taskless_inference_ids)
    delete_rules_and_tasks(engine, task_ids, rule_ids)

    print(
        f"\nDone. Deleted resources for {len(task_ids)} task(s) "
        f"and {len(taskless_inference_ids)} task-less inference(s).",
    )


if __name__ == "__main__":
    main()
