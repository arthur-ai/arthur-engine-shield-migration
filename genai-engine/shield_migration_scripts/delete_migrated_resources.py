# delete_migrated_resources.py
"""
Deletes all Engine resources associated with the tasks that a specific
migration inserted. Reads the migrated_task_ids recorded in a migration save
file and, for each task, deletes its rule results, feedback, inferences,
task-to-rule links (plus any rules left unlinked from every task as a result),
and finally the task itself via the Engine migration API.

Usage:
    python delete_migrated_resources.py --save-file migration_states/migration_state_2026-01-09_to_2026-07-08.json
    python delete_migrated_resources.py --save-file <path> --execute
"""

import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

ENGINE_BASE_URL = os.getenv("ENGINE_BASE_URL")
ENGINE_API_KEY = os.getenv("ENGINE_API_KEY")

MIGRATION_TIMEOUT = int(
    os.getenv("MIGRATION_TIMEOUT", default=30),
)  # seconds for all HTTP calls

# Resource endpoints for a single task, in FK-safe deletion order: each child is
# removed before the parent it points at, ending with the task row itself.
DELETE_STEPS = [
    ("rule results", "/api/v1/migration/tasks/{task_id}/rule_results"),
    ("feedback", "/api/v1/migration/tasks/{task_id}/feedback"),
    ("inferences", "/api/v1/migration/tasks/{task_id}/inferences"),
    ("rules", "/api/v1/migration/tasks/{task_id}/rules"),
    ("task", "/api/v1/migration/tasks/{task_id}"),
]


def engine_delete(path: str) -> None:
    r = requests.delete(
        f"{ENGINE_BASE_URL}{path}",
        headers={
            "Authorization": f"Bearer {ENGINE_API_KEY}",
        },
        timeout=MIGRATION_TIMEOUT,
    )
    r.raise_for_status()


def load_state(save_file: str) -> dict:
    if not os.path.exists(save_file):
        print(f"Save file not found: {save_file}", file=sys.stderr)
        sys.exit(1)
    with open(save_file) as f:
        return json.load(f)


def delete_task_resources(task_id: str) -> None:
    for _, path_template in DELETE_STEPS:
        engine_delete(path_template.format(task_id=task_id))


def print_progress(label: str, done: int, total: int) -> None:
    # Print roughly every 10% (and on completion), not on every item.
    step = max(1, total // 10)
    if done % step == 0 or done == total:
        print(f"  {label}: {done}/{total} ({done * 100 // total}%)", flush=True)


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

    if not task_ids and not taskless_inference_ids:
        print("Nothing recorded in the save file to delete.")
        return

    print(f"Found in {args.save_file}:")
    print(f"  {len(task_ids)} task(s):")
    for task_id in task_ids:
        print(f"    {task_id}")
    print(f"  {len(taskless_inference_ids)} task-less inference(s)")

    if not args.execute:
        print(
            "\nDry run — no resources deleted. Re-run with --execute to delete "
            "the rule results, feedback, inferences, rules, and tasks listed "
            "above, along with the task-less inferences.",
        )
        return

    print("\nDeleting resources...")
    for i, task_id in enumerate(task_ids, start=1):
        delete_task_resources(task_id)
        print_progress("Tasks", i, len(task_ids))

    for i, inference_id in enumerate(taskless_inference_ids, start=1):
        engine_delete(f"/api/v1/migration/inferences/{inference_id}")
        print_progress("Task-less inferences", i, len(taskless_inference_ids))

    print(
        f"\nDone. Deleted resources for {len(task_ids)} task(s) "
        f"and {len(taskless_inference_ids)} task-less inference(s).",
    )


if __name__ == "__main__":
    main()
