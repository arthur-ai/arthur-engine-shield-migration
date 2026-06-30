# pre_migration_scope.py
"""
Queries the Shield API and prints a stats report on the number of 
tasks, rules, inferences, etc. that can be migrated to the Engine.
No data is written.

Usage:
    python pre_migration_scope.py
    python pre_migration_scope.py --from-date 2020-01-01 --to-date 2021-01-01
    python pre_migration_scope.py --last-days 180
"""
import argparse
import os
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
import time
import requests
SHIELD_BASE_URL = os.getenv("SHIELD_BASE_URL")
SHIELD_API_KEY  = os.getenv("SHIELD_API_KEY")
PAGE_SIZE       = 5000   # max allowed by Shield API
TIMEOUT         = 30     # seconds

def shield_get(path, params=None, _retries=3):
    for attempt in range(_retries):
        try:
            r = requests.get(
                f"{SHIELD_BASE_URL}/{path}",
                headers={
                    "Authorization": f"Bearer {SHIELD_API_KEY}",
                },
                params=params or {},
                timeout=TIMEOUT
            )
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                wait = 2 ** attempt
                print(f"  [retry] 429 rate-limited, waiting {wait}s…")
                time.sleep(wait)
            elif attempt < _retries - 1:
                time.sleep(1)
            else:
                raise

def shield_post(path, body):
    r = requests.post(
        f"{SHIELD_BASE_URL}/{path}",
        json=body,
        headers={
            "Authorization": f"Bearer {SHIELD_API_KEY}",
        },
        timeout=TIMEOUT
    )
    r.raise_for_status()
    return r.json()

def parse_window(args):
    now = datetime.now(timezone.utc)
    if args.last_days:
        return now - timedelta(days=args.last_days), now
    from_dt = datetime.fromisoformat(args.from_date) if args.from_date else None
    to_dt   = datetime.fromisoformat(args.to_date)   if args.to_date   else None
    return from_dt, to_dt

def fmt(n):
    return f"{n:,}"

def paginate_search(path, body, count_key, items_key):
    """Page through a Shield POST search endpoint (tasks, rules). Pages start at 0."""
    page, items = 0, []
    body = {**body, "page_size": PAGE_SIZE}
    while True:
        resp  = shield_post(path, {**body, "page": page})
        total = resp.get(count_key, 0)
        batch = resp.get(items_key, [])
        items.extend(batch)
        if len(items) >= total or not batch:
            break
        page += 1
    return total, items

def scope_report(from_dt, to_dt):
    window_label = "all time"
    if from_dt:
        window_label = f"from {from_dt.date()}"
    if to_dt:
        window_label += f" to {to_dt.date()}"

    print(f"\n{'='*60}")
    print(f"  Shield → Engine Migration Scope Report")
    print(f"  Window: {window_label}")
    print(f"{'='*60}\n")

    # ── Config ────────────────────────────────────────────────────────────────
    task_count, tasks = paginate_search("/api/v2/tasks/search", {}, "count", "tasks")
    rule_count, _     = paginate_search("/api/v2/rules/search", {}, "count", "rules")
    default_rules     = shield_get("/api/v2/default_rules")
    n_default         = len(default_rules) if default_rules else 0
    n_links           = sum(len(t.get("rules", [])) for t in tasks)

    print("Config (always migrated in full, date window does not apply)")
    print(f"  Tasks              : {fmt(task_count)}")
    print(f"  Task-scoped rules  : {fmt(rule_count)}")
    print(f"  Default rules      : {fmt(n_default)}")
    print(f"  Task–rule links    : {fmt(n_links)}")
    print()

    # ── Inferences ────────────────────────────────────────────────────────────
    # /api/v2/inferences/query is a GET endpoint; filters are query params.
    # start_time / end_time are ISO-8601 strings, pages start at 0.
    inf_params = {"page_size": 1, "page": 0}
    if from_dt:
        inf_params["start_time"] = from_dt.isoformat()
    if to_dt:
        inf_params["end_time"] = to_dt.isoformat()

    resp       = shield_get("/api/v2/inferences/query", params=inf_params)
    total_infs = resp.get("count", 0) if resp else 0

    # Per-task counts: one GET per task, run up to 10 at a time.
    # Individual task failures return -1 rather than aborting the whole report.
    def fetch_task_count(task):
        try:
            r = shield_get("/api/v2/inferences/query",
                           params={**inf_params, "task_ids": task["id"], "page_size": 1})
            count = r.get("count", 0) if r else 0
            return task["name"], count
        except Exception as e:
            print(f"  [warning] Could not fetch count for task {task['name']!r}: {e}")
            return task["name"], -1

    with ThreadPoolExecutor(max_workers=10) as pool:
        task_counts = dict(pool.map(fetch_task_count, tasks))

    tasks_with_data = sum(1 for n in task_counts.values() if n > 0)

    print("Inferences")
    print(f"  Total              : {fmt(total_infs)}")
    print(f"  Tasks with data    : {fmt(tasks_with_data)} / {fmt(task_count)}")
    print()

    # ── Feedback ──────────────────────────────────────────────────────────────
    # /api/v2/feedback/query returns total_count (not count)
    fb_params = {"page_size": 1, "page": 0}
    if from_dt:
        fb_params["start_time"] = from_dt.isoformat()
    if to_dt:
        fb_params["end_time"] = to_dt.isoformat()
    resp = shield_get("/api/v2/feedback/query", params=fb_params)
    n_fb = resp.get("total_count", 0) if resp else 0
    print(f"Feedback               : {fmt(n_fb)}")
    print()

    # ── Per-task breakdown ────────────────────────────────────────────────────
    print("Per-task inference counts")
    for name, count in sorted(task_counts.items(), key=lambda x: -x[1]):
        print(f"  {name:<40} {fmt(count)}")
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-date", default=None, help="Start date (inclusive), e.g. 2020-01-01")
    parser.add_argument("--to-date",   default=None, help="End date (exclusive), e.g. 2021-01-01")
    parser.add_argument("--last-days", type=int, default=None,
                        help="Shorthand: scope the last N days")
    args = parser.parse_args()
    from_dt, to_dt = parse_window(args)
    scope_report(from_dt, to_dt)


if __name__ == "__main__":
    main()