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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from dotenv import load_dotenv

load_dotenv()

ENGINE_BASE_URL = os.getenv("ENGINE_BASE_URL")
ENGINE_API_KEY = os.getenv("ENGINE_API_KEY")

MIGRATION_TIMEOUT = int(
    os.getenv("MIGRATION_TIMEOUT", default=30),
)  # seconds for all HTTP calls

MAX_WORKERS = int(os.getenv("MIGRATION_MAX_WORKERS", default=10))
MAX_ATTEMPTS = 6
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Per-task child resources, in FK-safe deletion order. Safe to run concurrently
# across tasks since none of these rows are shared between tasks.
TASK_RESOURCE_STEPS = [
    ("rule results", "/api/v1/migration/tasks/{task_id}/rule_results"),
    ("feedback", "/api/v1/migration/tasks/{task_id}/feedback"),
    ("inferences", "/api/v1/migration/tasks/{task_id}/inferences"),
]

# The rules endpoint deletes rules left unlinked from every task, which races
# when tasks sharing a rule are deleted concurrently — run these sequentially.
TASK_FINAL_STEPS = [
    ("rules", "/api/v1/migration/tasks/{task_id}/rules"),
    ("task", "/api/v1/migration/tasks/{task_id}"),
]


def engine_delete(path: str) -> None:
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = requests.delete(
                f"{ENGINE_BASE_URL}{path}",
                headers={
                    "Authorization": f"Bearer {ENGINE_API_KEY}",
                },
                timeout=MIGRATION_TIMEOUT,
            )
            if response.status_code not in RETRYABLE_STATUS_CODES:
                response.raise_for_status()
                return
            last_error = f"{response.status_code} {response.text}"
        except requests.HTTPError:
            raise
        except requests.RequestException as e:
            last_error = str(e)
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(min(2**attempt, 30))
    raise Exception(
        f"DELETE {path} failed after {MAX_ATTEMPTS} attempts: {last_error}",
    )


def load_state(save_file: str) -> dict:
    if not os.path.exists(save_file):
        print(f"Save file not found: {save_file}", file=sys.stderr)
        sys.exit(1)
    with open(save_file) as f:
        return json.load(f)


def delete_task_resources(task_id: str) -> None:
    for _, path_template in TASK_RESOURCE_STEPS:
        engine_delete(path_template.format(task_id=task_id))


def delete_task_final(task_id: str) -> None:
    for _, path_template in TASK_FINAL_STEPS:
        engine_delete(path_template.format(task_id=task_id))


def delete_taskless_inference(inference_id: str) -> None:
    engine_delete(f"/api/v1/migration/inferences/{inference_id}")


def print_progress(label: str, done: int, total: int) -> None:
    # Print roughly every 10% (and on completion), not on every item.
    step = max(1, total // 10)
    if done % step == 0 or done == total:
        print(f"  {label}: {done}/{total} ({done * 100 // total}%)", flush=True)


def run_parallel(label: str, items: list, fn) -> None:
    if not items:
        return
    completed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(fn, item) for item in items]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                executor.shutdown(cancel_futures=True)
                raise
            completed += 1
            print_progress(label, completed, len(items))


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
    run_parallel("Task resources", task_ids, delete_task_resources)

    for i, task_id in enumerate(task_ids, start=1):
        delete_task_final(task_id)
        print_progress("Tasks", i, len(task_ids))

    run_parallel(
        "Task-less inferences",
        taskless_inference_ids,
        delete_taskless_inference,
    )

    print(
        f"\nDone. Deleted resources for {len(task_ids)} task(s) "
        f"and {len(taskless_inference_ids)} task-less inference(s).",
    )


if __name__ == "__main__":
    main()
