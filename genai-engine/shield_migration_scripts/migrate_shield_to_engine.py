# migrate_shield_to_engine.py
"""
Orchestrates Shield → Engine migration using the Shield API and Engine API.
No direct database access to Shield required.

Usage:
    python migrate_shield_to_engine.py --phase all --from-date 2025-01-01 --to-date 2026-01-01
    python migrate_shield_to_engine.py --phase all --last-days 90
    python migrate_shield_to_engine.py --phase inferences --resume migration_state_2025-01-01_to_2026-01-01.json
    python migrate_shield_to_engine.py --phase config --from-date 2025-01-01 --to-date 2026-01-01
    python migrate_shield_to_engine.py --phase inferences --from-date 2026-01-01
    python migrate_shield_to_engine.py --phase feedback --last-days 180
    python migrate_shield_to_engine.py --task-ids <task_id> --last-days 90
    python migrate_shield_to_engine.py --task-ids <task_id_1> <task_id_2> --from-date 2025-01-01
"""

import argparse
import hashlib
import json
import os
import queue
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

SHIELD_BASE_URL = os.getenv("SHIELD_BASE_URL")
SHIELD_API_KEY = os.getenv("SHIELD_API_KEY")

ENGINE_BASE_URL = os.getenv("ENGINE_BASE_URL")
ENGINE_API_KEY = os.getenv("ENGINE_API_KEY")
ENGINE_ORG_ID = os.getenv("ENGINE_ORG_ID")

CHECKPOINT_DIR = os.getenv(
    "MIGRATION_CHECKPOINT_DIR",
    default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "migration_states",
    ),
)

SHIELD_PAGE_SIZE = int(
    os.getenv("SHIELD_PAGE_SIZE", default=4999),
)  # Shield requires page_size > 0 and < 5000
ENGINE_BATCH_SIZE = int(
    os.getenv("ENGINE_BATCH_SIZE", default=500),
)  # inferences per POST to the Engine
MIGRATION_TIMEOUT = int(
    os.getenv("MIGRATION_TIMEOUT", default=30),
)  # seconds for all HTTP calls
MAX_WORKERS = int(os.getenv("MIGRATION_MAX_WORKERS", default=10))
PREFETCH_PAGES = int(os.getenv("MIGRATION_PREFETCH_PAGES", default=10))
SHIELD_FETCH_WORKERS = int(os.getenv("MIGRATION_SHIELD_FETCH_WORKERS", default=3))
MAX_ATTEMPTS = 6
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

ALL_PHASES = ["config", "inferences", "feedback"]

# ── HTTP helpers ──────────────────────────────────────────────────────────────


def request_with_retry(method, url, **kwargs):
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = requests.request(
                method,
                url,
                timeout=MIGRATION_TIMEOUT,
                **kwargs,
            )
            if response.status_code not in RETRYABLE_STATUS_CODES:
                response.raise_for_status()
                return response
            last_error = f"{response.status_code} {response.text}"
        except requests.HTTPError:
            raise
        except requests.RequestException as e:
            last_error = str(e)
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(min(2**attempt, 30))
    raise Exception(
        f"{method} {url} failed after {MAX_ATTEMPTS} attempts: {last_error}",
    )


# ── Shield API helpers ────────────────────────────────────────────────────────


def shield_get(path, params=None):
    """GET request to Shield. Params dict may include lists for repeated keys."""
    r = request_with_retry(
        "GET",
        f"{SHIELD_BASE_URL}/{path}",
        headers={
            "Authorization": f"Bearer {SHIELD_API_KEY}",
        },
        params=params or {},
    )
    return r.json()


def shield_post(path, body, params=None):
    r = request_with_retry(
        "POST",
        f"{SHIELD_BASE_URL}/{path}",
        json=body,
        params=params or {},
        headers={
            "Authorization": f"Bearer {SHIELD_API_KEY}",
        },
    )
    return r.json()


def shield_paginate(path, body, count_key, items_key):
    """Fetch all pages from a Shield POST search endpoint. Pages start at 0.

    page/page_size are query params — Shield silently ignores them if sent in
    the POST body and returns the same default-sized first page every time.
    """
    page, all_items = 0, []
    while True:
        resp = shield_post(
            path,
            body,
            params={"page_size": SHIELD_PAGE_SIZE, "page": page},
        )
        total = resp.get(count_key, 0)
        batch = resp.get(items_key, [])
        all_items.extend(batch)
        if len(all_items) >= total or not batch:
            break
        page += 1
    return all_items


# ── Engine API helpers ────────────────────────────────────────────────────────


def engine_call(method, path, body=None, params=None):
    r = request_with_retry(
        method,
        f"{ENGINE_BASE_URL}{path}",
        json=body,
        params=params or {},
        headers={
            "Authorization": f"Bearer {ENGINE_API_KEY}",
        },
    )
    return r.json()


def engine_paginate(path, body, count_key, items_key):
    """Fetch all pages from an Engine POST search endpoint. Pages start at 0."""
    page, all_items = 0, []
    while True:
        resp = engine_call(
            "POST",
            path,
            body=body,
            params={"page_size": SHIELD_PAGE_SIZE, "page": page},
        )
        total = resp.get(count_key, 0)
        batch = resp.get(items_key, [])
        all_items.extend(batch)
        if len(all_items) >= total or not batch:
            break
        page += 1
    return all_items


def engine_post_batches(path, items_key, items):
    """POST items to an Engine bulk endpoint in parallel ENGINE_BATCH_SIZE chunks.

    Returns (inserted, skipped). On failure the page checkpoint is never
    advanced, so a resume refetches the page and skip-existing dedupes any
    chunks that already committed.
    """
    chunks = [
        items[i : i + ENGINE_BATCH_SIZE]
        for i in range(0, len(items), ENGINE_BATCH_SIZE)
    ]
    inserted = skipped = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(
                engine_call,
                "POST",
                path,
                {items_key: chunk, "org_id": ENGINE_ORG_ID},
            )
            for chunk in chunks
        ]
        for future in as_completed(futures):
            try:
                resp = future.result()
            except Exception:
                executor.shutdown(cancel_futures=True)
                raise
            inserted += resp.get("inserted", 0)
            skipped += resp.get("skipped", 0)
    return inserted, skipped


# ── Utilities ─────────────────────────────────────────────────────────────────


def format_duration(seconds: float) -> str:
    """Human-readable duration, e.g. '1h 02m 03s', '4m 09s', '12.3s'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


TIMINGS: dict = {}  # phase -> steps, total, count, unit, chunk_times

CHUNK_SIZES = [10_000, 100_000, 1_000_000]


def format_chunk_size(size: int) -> str:
    if size >= 1_000_000:
        return f"{size // 1_000_000}m"
    return f"{size // 1_000}k"


class ChunkTimer:
    """Measures the actual wall-clock time of each full N-record chunk as
    records stream through — one independent series per size in CHUNK_SIZES."""

    def __init__(self, start: float):
        self.times = {size: [] for size in CHUNK_SIZES}
        self.chunk_starts = {size: start for size in CHUNK_SIZES}
        self.boundaries = {size: size for size in CHUNK_SIZES}

    def update(self, processed: int):
        for size in CHUNK_SIZES:
            if processed >= self.boundaries[size]:
                now = time.monotonic()
                self.times[size].append(now - self.chunk_starts[size])
                self.chunk_starts[size] = now
                self.boundaries[size] += size


def record_step_timing(phase: str, label: str, seconds: float):
    TIMINGS.setdefault(phase, {"steps": []})["steps"].append((label, seconds))


def record_phase_timing(phase: str, total: float, count=0, unit="", chunk_times=None):
    """Store a phase's total runtime and, for record-streaming phases, the
    record count and the measured chunk times ({size: [seconds, ...]})."""
    entry = TIMINGS.setdefault(phase, {"steps": []})
    entry["total"] = total
    entry["count"] = count
    entry["unit"] = unit
    entry["chunk_times"] = chunk_times or {}


def print_timing_report():
    if not TIMINGS:
        return
    print()
    print("=== Timing Report ===")
    for phase in ALL_PHASES:
        entry = TIMINGS.get(phase)
        if entry is None:
            continue
        print()
        print(f"{phase} phase:")
        for label, seconds in entry["steps"]:
            print(f"  {label}: {format_duration(seconds)}")
        total = entry.get("total")
        if total is None:
            continue
        count, unit = entry.get("count", 0), entry.get("unit", "")
        if count and unit:
            print(f"  Total: {format_duration(total)} ({count:,} {unit}s)")
            print(f"  Average per {unit}: {total / count * 1000:.2f} ms")
        else:
            print(f"  Total: {format_duration(total)}")
        chunk_times = entry.get("chunk_times") or {}
        for size in sorted(chunk_times):
            times = chunk_times[size]
            if not times:
                continue
            avg = sum(times) / len(times)
            print(
                f"  Per {format_chunk_size(size)} {unit}s (measured): "
                f"avg {format_duration(avg)}, "
                f"fastest {format_duration(min(times))}, "
                f"slowest {format_duration(max(times))} "
                f"({len(times)} full chunks)",
            )


def parse_window(args):
    now = datetime.now(timezone.utc)

    if args.last_days:
        return now - timedelta(days=args.last_days), now

    from_dt = datetime.fromisoformat(args.from_date) if args.from_date else None
    to_dt = datetime.fromisoformat(args.to_date) if args.to_date else now

    return from_dt, to_dt


def task_scope_slug(task_ids) -> str:
    """Filename suffix identifying a run's task scope ('' for full runs)."""
    if not task_ids:
        return ""
    if len(task_ids) == 1:
        return f"_task_{task_ids[0]}"
    digest = hashlib.sha1(",".join(task_ids).encode()).hexdigest()[:8]
    return f"_tasks_{len(task_ids)}_{digest}"


def checkpoint_path(from_dt, to_dt, task_ids=None) -> str:
    window_slug = (
        f"{from_dt.date() if from_dt else 'all'}"
        f"_to_{to_dt.date() if to_dt else 'now'}"
    )
    return (
        f"{CHECKPOINT_DIR}/migration_state_{window_slug}"
        f"{task_scope_slug(task_ids)}.json"
    )


def latest_checkpoint_path(task_ids=None):
    """Path of the most-recently-updated checkpoint with the same task scope,
    or None if none exist.

    Task-scoped and full-migration runs never conflict with each other, so only
    checkpoints whose task_ids match the requested scope are considered. Ranks
    by each checkpoint's own last_updated_at field rather than file mtime.
    """
    if not os.path.isdir(CHECKPOINT_DIR):
        return None
    requested_scope = sorted(task_ids or [])
    latest_path, latest_ts = None, None
    for f in os.listdir(CHECKPOINT_DIR):
        if not f.endswith(".json"):
            continue
        path = os.path.join(CHECKPOINT_DIR, f)
        with open(path) as fh:
            state = json.load(fh)
        if sorted(state.get("task_ids") or []) != requested_scope:
            continue
        ts = state.get("last_updated_at")
        if ts is not None and (latest_ts is None or ts > latest_ts):
            latest_path, latest_ts = path, ts
    return latest_path


# ── Checkpoint ────────────────────────────────────────────────────────────────


class Checkpoint:
    """
    Persists migration state so any phase can be safely resumed.

    State keys:
      from_dt / to_dt         — date window (ISO strings or null)
      task_ids                — task IDs the run is scoped to ([] = all tasks)
      phases_completed        — list of phase names that finished successfully
      inference_page          — next Shield inference page to fetch (starts at 0)
      feedback_page           — next Shield feedback page to fetch (starts at 0)
      migrated_task_ids       — task IDs inserted into the Engine (for cleanup)
      migrated_rule_ids       — rule IDs inserted into the Engine (for cleanup)
      migrated_taskless_inference_ids — inserted inferences with no task, which a
                                task-scoped cleanup cannot reach (for cleanup)
      archived_rules_migrated — archived rules finished migrating, so a resumed
                                inferences phase skips refetching them
      started_at / last_updated_at — ISO timestamps
    """

    def __init__(self, path: str):
        self.path = path
        if os.path.exists(path):
            with open(path) as f:
                self.state = json.load(f)
            # Backfill keys added after this checkpoint was first written.
            self.state.setdefault("migrated_task_ids", [])
            self.state.setdefault("migrated_taskless_inference_ids", [])
            self.state.setdefault("migrated_rule_ids", [])
            self.state.setdefault("task_ids", [])
            self.state.setdefault("archived_rules_migrated", False)
        else:
            self.state = {
                "from_dt": None,
                "to_dt": None,
                "task_ids": [],
                "phases_completed": [],
                "inference_page": 0,
                "feedback_page": 0,
                "migrated_task_ids": [],
                "migrated_taskless_inference_ids": [],
                "migrated_rule_ids": [],
                "archived_rules_migrated": False,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "last_updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save()

    def set_task_scope(self, task_ids):
        self.state["task_ids"] = sorted(task_ids or [])
        self._save()

    def set_window(self, from_dt, to_dt):
        new_from = from_dt.isoformat() if from_dt else None
        new_to = to_dt.isoformat() if to_dt else None
        stored_from = self.state.get("from_dt")
        stored_to = self.state.get("to_dt")
        if (stored_from is not None or stored_to is not None) and (
            new_from != stored_from or new_to != stored_to
        ):
            raise ValueError(
                f"Date window mismatch on resume.\n"
                f"  Checkpoint: {stored_from} → {stored_to}\n"
                f"  Command:    {new_from} → {new_to}\n"
                f"Pass --resume with the same date arguments, or start a new run.",
            )
        self.state["from_dt"] = new_from
        self.state["to_dt"] = new_to
        self._save()

    def phase_done(self, phase: str):
        if phase not in self.state["phases_completed"]:
            self.state["phases_completed"].append(phase)
        self._save()

    def phase_completed(self, phase: str) -> bool:
        return phase in self.state["phases_completed"]

    def record_migrated_tasks(self, task_ids):
        """Append newly inserted task IDs, de-duped, preserving order."""
        existing = set(self.state["migrated_task_ids"])
        for task_id in task_ids:
            if task_id not in existing:
                self.state["migrated_task_ids"].append(task_id)
                existing.add(task_id)
        self._save()

    def record_migrated_rules(self, rule_ids):
        """Append newly inserted rule IDs, de-duped, preserving order."""
        existing = set(self.state["migrated_rule_ids"])
        for rule_id in rule_ids:
            if rule_id not in existing:
                self.state["migrated_rule_ids"].append(rule_id)
                existing.add(rule_id)
        self._save()

    def set_archived_rules_migrated(self):
        self.state["archived_rules_migrated"] = True
        self._save()

    def record_taskless_inferences(self, inference_ids):
        """Append IDs of migrated inferences that have no task, de-duped.

        These inferences are not reachable by a task-scoped cleanup, so the
        delete script uses this list to remove them by ID.
        """
        existing = set(self.state["migrated_taskless_inference_ids"])
        for inference_id in inference_ids:
            if inference_id not in existing:
                self.state["migrated_taskless_inference_ids"].append(inference_id)
                existing.add(inference_id)
        self._save()

    def update_inference_page(self, page: int):
        self.state["inference_page"] = page
        self._save()

    def update_feedback_page(self, page: int):
        self.state["feedback_page"] = page
        self._save()

    def _save(self):
        self.state["last_updated_at"] = datetime.now(timezone.utc).isoformat()
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.state, f, indent=2)


# ── Phase 1: Config (tasks + rules) ──────────────────────────────────────────


def migrate_config(ckpt: Checkpoint, task_ids=None, recover=False):
    if not recover and ckpt.phase_completed("config"):
        print("[config] Already completed — skipping.")
        return

    print("=== Phase 1: Config ===")
    phase_start = time.monotonic()

    search_body = {"task_ids": task_ids} if task_ids else {}
    step_start = time.monotonic()
    tasks = shield_paginate("/api/v2/tasks/search", search_body, "count", "tasks")
    record_step_timing("config", "Fetch tasks", time.monotonic() - step_start)
    print(f"  Fetched {len(tasks)} tasks")

    if task_ids:
        found = {t["id"] for t in tasks}
        missing = [t for t in task_ids if t not in found]

        if missing:
            print(f"  WARNING: task ids not found in Shield: {', '.join(missing)}")

    step_start = time.monotonic()
    default_rules = shield_get("/api/v2/default_rules")

    embedded_rules = []  # rules taken from task payloads; archived flag unknown
    if task_ids:
        # /rules/search has no task filter, but each selected task embeds its
        # full rule objects — so task-scoped rules come from there instead.
        # The embedded `enabled` flag is link data, not rule data; drop it.
        default_ids = {r["id"] for r in default_rules}
        seen: set = set()
        task_rules = []
        for task in tasks:
            for rule in task.get("rules", []):
                if rule["id"] in default_ids or rule["id"] in seen:
                    continue
                seen.add(rule["id"])
                task_rules.append(
                    {k: v for k, v in rule.items() if k != "enabled"},
                )
        embedded_rules = list(task_rules)
    else:
        task_rules = shield_paginate(
            "/api/v2/rules/search",
            {"rule_scopes": ["task"]},
            "count",
            "rules",
        )

    all_rules = default_rules + task_rules

    # Tasks may link to rules missing from the searches above (e.g. archived
    # rules still linked to live tasks). Insert those from the embedded rule
    # objects so the link insert below can never hit a missing rule.
    known_rule_ids = {rule["id"] for rule in all_rules}
    link_target_count = 0
    for task in tasks:
        for rule in task.get("rules", []):
            if rule["id"] in known_rule_ids:
                continue
            known_rule_ids.add(rule["id"])
            embedded_rule = {k: v for k, v in rule.items() if k != "enabled"}
            all_rules.append(embedded_rule)
            embedded_rules.append(embedded_rule)
            link_target_count += 1

    # Embedded rule objects don't carry the archived flag; any of them the
    # active-rules search doesn't return is archived in Shield.
    if embedded_rules:
        active_rules = shield_paginate(
            "/api/v2/rules/search",
            {"rule_ids": [rule["id"] for rule in embedded_rules]},
            "count",
            "rules",
        )
        active_ids = {rule["id"] for rule in active_rules}
        for rule in embedded_rules:
            if rule["id"] not in active_ids:
                rule["archived"] = True

    record_step_timing("config", "Fetch rules", time.monotonic() - step_start)
    print(
        f"  Fetched {len(default_rules)} default + {len(task_rules)} task-scoped "
        f"+ {link_target_count} link-target rules",
    )

    # Recover mode: verify what already exists in the Engine and record it in
    # the save file. Nothing is written to the Engine.
    if recover:
        step_start = time.monotonic()
        engine_task_ids = {
            t["id"]
            for t in engine_paginate("/api/v2/tasks/search", {}, "count", "tasks")
        }
        recovered_tasks = [t["id"] for t in tasks if t["id"] in engine_task_ids]
        ckpt.record_migrated_tasks(recovered_tasks)
        print(f"  Tasks: {len(recovered_tasks)}/{len(tasks)} verified in Engine")

        engine_rule_ids = {
            r["id"]
            for r in engine_paginate(
                "/api/v2/rules/search",
                {"include_archived": True},
                "count",
                "rules",
            )
        }

        recovered_rules = [r["id"] for r in all_rules if r["id"] in engine_rule_ids]
        ckpt.record_migrated_rules(recovered_rules)
        print(f"  Rules: {len(recovered_rules)}/{len(all_rules)} verified in Engine")

        if len(recovered_tasks) == len(tasks) and len(recovered_rules) == len(
            all_rules,
        ):
            ckpt.phase_done("config")
            print("  All config resources verified — phase marked completed")

        record_step_timing(
            "config",
            "Verify in Engine",
            time.monotonic() - step_start,
        )
        record_phase_timing("config", time.monotonic() - phase_start)

        print("=== Phase 1: Recovered ===")
        print()
        return

    step_start = time.monotonic()
    resp = engine_call("POST", "/api/v1/migration/rules/bulk", {"rules": all_rules})
    record_step_timing("config", "Insert rules", time.monotonic() - step_start)
    ckpt.record_migrated_rules(
        [rule["id"] for rule in resp.get("rules", []) if rule.get("id")],
    )
    inserted = len(resp.get("rules", []))
    print(
        f"  Rules: "
        f"    {inserted} inserted"
        f"    {len(all_rules) - inserted} skipped (already existed)",
    )

    # Strip the embedded rules array before sending tasks — links are sent
    # separately below so the Engine endpoint does NOT auto-link rules on insert.
    task_rows = [{k: v for k, v in t.items() if k != "rules"} for t in tasks]
    step_start = time.monotonic()
    resp = engine_call(
        "POST",
        "/api/v1/migration/tasks/bulk",
        {"tasks": task_rows, "org_id": ENGINE_ORG_ID},
    )
    inserted_tasks = resp.get("tasks", [])
    ckpt.record_migrated_tasks(
        [t["id"] for t in inserted_tasks if t.get("id")],
    )
    record_step_timing("config", "Insert tasks", time.monotonic() - step_start)
    inserted = len(inserted_tasks)
    print(
        f"  Tasks: "
        f"    {inserted} inserted"
        f"    {len(task_rows) - inserted} skipped (already existed)",
    )

    # Task→rule links come from each TaskResponse.rules (full RuleResponse objects
    # with an `enabled` field). Extract just the link data.
    links = []
    for task in tasks:
        for rule_link in task.get("rules", []):
            links.append(
                {
                    "task_id": task["id"],
                    "rule_id": rule_link["id"],
                    "enabled": rule_link.get("enabled", True),
                },
            )
    step_start = time.monotonic()
    resp = engine_call(
        "POST",
        "/api/v1/migration/task_rule_links/bulk",
        {"task_to_rule_links": links},
    )
    record_step_timing(
        "config",
        "Insert task-rule links",
        time.monotonic() - step_start,
    )
    inserted = len(resp.get("task_to_rule_links", []))
    print(
        f"  Task–rule links: "
        f"    {inserted} inserted"
        f"    {len(links) - inserted} skipped (already existed)",
    )

    ckpt.phase_done("config")
    record_phase_timing("config", time.monotonic() - phase_start)
    print("=== Phase 1: Complete ===")
    print()


# ── Phase 2: Inferences ───────────────────────────────────────────────────────


def migrate_archived_rules(ckpt: Checkpoint, recover=False):
    """Archived rules are migrated in full at the start of the inferences
    phase: historical rule results may reference rules that have since been
    archived, and Shield cannot filter archived rules by task."""
    if not recover and ckpt.state.get("archived_rules_migrated"):
        print("  Archived rules already migrated — skipping.")
        return

    step_start = time.monotonic()
    archived_rules = shield_paginate(
        "/api/v2/rules/search",
        {"include_archived": True},
        "count",
        "rules",
    )
    for rule in archived_rules:
        rule["archived"] = True
    record_step_timing(
        "inferences",
        "Fetch archived rules",
        time.monotonic() - step_start,
    )

    if recover:
        print("  Recovering archived rules...")
        step_start = time.monotonic()
        engine_rule_ids = {
            r["id"]
            for r in engine_paginate(
                "/api/v2/rules/search",
                {"include_archived": True},
                "count",
                "rules",
            )
        }
        recovered = [r["id"] for r in archived_rules if r["id"] in engine_rule_ids]
        ckpt.record_migrated_rules(recovered)
        record_step_timing(
            "inferences",
            "Verify archived rules in Engine",
            time.monotonic() - step_start,
        )

        if len(recovered) == len(archived_rules):
            ckpt.set_archived_rules_migrated()

        print(f"    {len(recovered)}/{len(archived_rules)} verified in Engine")
        print()
        return

    step_start = time.monotonic()
    inserted = 0
    for start in range(0, len(archived_rules), ENGINE_BATCH_SIZE):
        chunk = archived_rules[start : start + ENGINE_BATCH_SIZE]
        resp = engine_call("POST", "/api/v1/migration/rules/bulk", {"rules": chunk})
        ckpt.record_migrated_rules(
            [rule["id"] for rule in resp.get("rules", []) if rule.get("id")],
        )
        inserted += len(resp.get("rules", []))

    record_step_timing(
        "inferences",
        "Insert archived rules",
        time.monotonic() - step_start,
    )
    ckpt.set_archived_rules_migrated()
    print(
        f"  Archived rules: "
        f"    {inserted} inserted"
        f"    {len(archived_rules) - inserted} skipped (already existed)",
    )


def recover_taskless_inferences(ckpt: Checkpoint, from_dt, to_dt, task_ids=None):
    """Scan the Shield window for task-less inferences and record the ones
    that exist in the Engine. Writes nothing to the Engine."""
    print("  Recovering task-less inferences...")
    if task_ids:
        print("    Task-scoped runs migrate none — skipping.")
        return

    inf_params: dict = {}
    if from_dt:
        inf_params["start_time"] = from_dt.isoformat()
    if to_dt:
        inf_params["end_time"] = to_dt.isoformat()

    step_start = time.monotonic()
    page_queue: queue.Queue = queue.Queue(maxsize=PREFETCH_PAGES)
    fetch_errors: list = []
    cursor = {"lock": threading.Lock(), "next_page": 0, "end_page": None}
    for _ in range(SHIELD_FETCH_WORKERS):
        threading.Thread(
            target=fetch_shield_inference_pages,
            args=(inf_params, cursor, page_queue, fetch_errors),
            daemon=True,
        ).start()

    candidate_ids = []
    active_fetchers = SHIELD_FETCH_WORKERS
    pages_done = 0
    total_pages = None
    while active_fetchers:
        item = page_queue.get()
        if item is None:
            active_fetchers -= 1
            continue
        _, batch, total = item
        candidate_ids.extend(inf["id"] for inf in batch if not inf.get("task_id"))
        pages_done += 1
        if total_pages is None and total:
            total_pages = -(-total // SHIELD_PAGE_SIZE)
        if pages_done % 10 == 0:
            print(
                f"    Scanning Shield: {pages_done}/{total_pages or '?'} pages",
                flush=True,
            )
    if fetch_errors:
        raise fetch_errors[0]
    record_step_timing(
        "inferences",
        "Scan for task-less inferences",
        time.monotonic() - step_start,
    )

    step_start = time.monotonic()
    verified_ids = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                engine_call,
                "GET",
                "/api/v2/inferences/query",
                None,
                {"inference_id": inference_id, "page_size": 1},
            ): inference_id
            for inference_id in candidate_ids
        }
        for future in as_completed(futures):
            if future.result().get("count", 0) > 0:
                verified_ids.append(futures[future])
    ckpt.record_taskless_inferences(verified_ids)
    record_step_timing(
        "inferences",
        "Verify task-less inferences in Engine",
        time.monotonic() - step_start,
    )
    print(
        f"    {len(verified_ids)}/{len(candidate_ids)} verified in Engine",
    )


def fetch_shield_inference_pages(inf_params, cursor, page_queue, errors):
    """Fetcher: claim pages from the shared cursor until the end page is known."""
    try:
        while True:
            with cursor["lock"]:
                if (
                    cursor["end_page"] is not None
                    and cursor["next_page"] > cursor["end_page"]
                ):
                    break
                page = cursor["next_page"]
                cursor["next_page"] += 1
            resp = shield_get(
                "/api/v2/migration/inferences/query",
                params={
                    **inf_params,
                    "page_size": SHIELD_PAGE_SIZE,
                    "page": page,
                    "sort": "asc",
                },
            )
            batch = resp.get("inferences", [])
            if not batch or len(batch) < SHIELD_PAGE_SIZE:
                with cursor["lock"]:
                    if cursor["end_page"] is None or page < cursor["end_page"]:
                        cursor["end_page"] = page
            page_queue.put((page, batch, resp.get("count", 0)))
    except BaseException as e:
        errors.append(e)
    finally:
        page_queue.put(None)


def migrate_inferences(ckpt: Checkpoint, from_dt, to_dt, task_ids=None, recover=False):
    if not recover and ckpt.phase_completed("inferences"):
        print("[inferences] Already completed — skipping.")
        return

    scope_note = f" tasks={','.join(task_ids)}" if task_ids else ""
    print(
        f"=== Phase 2: Inferences "
        f"(from={from_dt.date() if from_dt else 'beginning'} "
        f"to={to_dt.date() if to_dt else 'now'}{scope_note}) ===",
    )

    phase_start = time.monotonic()
    migrate_archived_rules(ckpt, recover=recover)

    if recover:
        recover_taskless_inferences(ckpt, from_dt, to_dt, task_ids)
        record_phase_timing("inferences", time.monotonic() - phase_start)
        print("=== Phase 2: Recovered ===")
        print()
        return

    # /api/v2/migration/inferences/query returns the full inference subtree with
    # real rule-result/detail IDs and token counts intact, so inferences are
    # forwarded to the Engine unchanged. Pages start at 0.
    inf_params: dict = {}
    if from_dt:
        inf_params["start_time"] = from_dt.isoformat()
    if to_dt:
        inf_params["end_time"] = to_dt.isoformat()
    if task_ids:
        inf_params["task_ids"] = task_ids

    start_page = ckpt.state["inference_page"]
    if start_page > 0:
        print(
            f"  Resuming from page {start_page} "
            f"(~{start_page * SHIELD_PAGE_SIZE:,} already committed)",
        )

    processed = start_page * SHIELD_PAGE_SIZE
    matched = 0
    inserted = 0
    skipped = 0
    total_count = 0
    wanted_tasks = set(task_ids) if task_ids else None
    processed_this_run = 0
    chunk_timer = ChunkTimer(time.monotonic())

    # Fetcher pool prefetches Shield pages while workers post earlier pages.
    page_queue: queue.Queue = queue.Queue(maxsize=PREFETCH_PAGES)
    producer_error: list = []
    cursor = {"lock": threading.Lock(), "next_page": start_page, "end_page": None}
    for _ in range(SHIELD_FETCH_WORKERS):
        threading.Thread(
            target=fetch_shield_inference_pages,
            args=(inf_params, cursor, page_queue, producer_error),
            daemon=True,
        ).start()

    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    in_flight = {}  # future -> page number
    pending_chunks = {}  # page -> chunk count not yet completed
    page_info = {}  # page -> (batch_len, sent_len, taskless_ids)
    page_results = {}  # page -> [inserted, skipped]
    next_commit = start_page
    max_in_flight = MAX_WORKERS * 2
    active_fetchers = SHIELD_FETCH_WORKERS

    try:
        while active_fetchers or in_flight:
            while active_fetchers and len(in_flight) < max_in_flight:
                try:
                    if in_flight:
                        item = page_queue.get(block=False)
                    else:
                        item = page_queue.get()
                except queue.Empty:
                    break
                if item is None:
                    active_fetchers -= 1
                    continue
                page_no, batch, page_total = item
                total_count = page_total or total_count
                if not batch:
                    continue
                if wanted_tasks is not None:
                    to_send = [
                        inf for inf in batch if inf.get("task_id") in wanted_tasks
                    ]
                else:
                    to_send = batch
                page_info[page_no] = (
                    len(batch),
                    len(to_send),
                    [inf["id"] for inf in batch if not inf.get("task_id")],
                )
                page_results[page_no] = [0, 0]
                chunks = [
                    to_send[i : i + ENGINE_BATCH_SIZE]
                    for i in range(0, len(to_send), ENGINE_BATCH_SIZE)
                ]
                pending_chunks[page_no] = len(chunks)
                for chunk in chunks:
                    future = executor.submit(
                        engine_call,
                        "POST",
                        "/api/v1/migration/inferences/bulk",
                        {"inferences": chunk, "org_id": ENGINE_ORG_ID},
                    )
                    in_flight[future] = page_no

            if in_flight:
                completed, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
                for future in completed:
                    page_no = in_flight.pop(future)
                    resp = future.result()
                    page_results[page_no][0] += resp.get("inserted", 0)
                    page_results[page_no][1] += resp.get("skipped", 0)
                    pending_chunks[page_no] -= 1

            # Checkpoint advances only through contiguously completed pages.
            while pending_chunks.get(next_commit) == 0:
                del pending_chunks[next_commit]
                batch_len, sent_len, taskless_ids = page_info.pop(next_commit)
                page_inserted, page_skipped = page_results.pop(next_commit)
                matched += sent_len
                inserted += page_inserted
                skipped += page_skipped
                if wanted_tasks is None:
                    ckpt.record_taskless_inferences(taskless_ids)
                processed += batch_len
                processed_this_run += batch_len
                chunk_timer.update(processed_this_run)
                ckpt.update_inference_page(next_commit + 1)
                pct = processed / total_count * 100 if total_count else 0
                matched_note = f", {matched:,} matched tasks" if wanted_tasks else ""
                print(
                    f"  [{pct:5.1f}%] {processed:,} / {total_count:,} inferences scanned"
                    f"{matched_note} "
                    f"({inserted:,} inserted, {skipped:,} skipped) (page {next_commit})",
                )
                next_commit += 1
    finally:
        executor.shutdown(cancel_futures=True)

    if producer_error:
        raise producer_error[0]

    ckpt.phase_done("inferences")
    phase_total = time.monotonic() - phase_start
    record_phase_timing(
        "inferences",
        phase_total,
        processed_this_run,
        "inference",
        chunk_timer.times,
    )
    print()
    print(f"Total Inferences Inserted: {inserted:,}")
    print(f"Total Inferences Skipped: {skipped:,}")
    print("=== Phase 2: Complete ===")
    print()


# ── Phase 3: Feedback ─────────────────────────────────────────────────────────


def migrate_feedback(ckpt: Checkpoint, from_dt, to_dt, task_ids=None):
    if ckpt.phase_completed("feedback"):
        print("[feedback] Already completed — skipping.")
        return

    print("=== Phase 3: Feedback ===")

    # /api/v2/feedback/query uses total_count (not count) and pages start at 0.
    fb_params: dict = {}
    if from_dt:
        fb_params["start_time"] = from_dt.isoformat()
    if to_dt:
        fb_params["end_time"] = to_dt.isoformat()
    if task_ids:
        fb_params["task_id"] = task_ids

    start_page = ckpt.state["feedback_page"]
    page = start_page
    processed = start_page * SHIELD_PAGE_SIZE
    inserted = 0
    skipped = 0
    phase_start = time.monotonic()
    processed_this_run = 0
    chunk_timer = ChunkTimer(phase_start)

    while True:
        fetch_params = {
            **fb_params,
            "page_size": SHIELD_PAGE_SIZE,
            "page": page,
            "sort": "asc",
        }
        resp = shield_get("/api/v2/feedback/query", params=fetch_params)
        batch = resp.get("feedback", [])
        total_count = resp.get("total_count", 0)
        if not batch:
            break

        page_inserted, page_skipped = engine_post_batches(
            "/api/v1/migration/feedback/bulk",
            "feedback",
            batch,
        )
        inserted += page_inserted
        skipped += page_skipped

        processed += len(batch)
        processed_this_run += len(batch)
        chunk_timer.update(processed_this_run)
        page += 1
        ckpt.update_feedback_page(page)
        print(
            f"  {processed:,} / {total_count:,} feedback records processed "
            f"({inserted:,} inserted, {skipped:,} skipped) (page {page - 1})",
        )

        if len(batch) < SHIELD_PAGE_SIZE:
            break

    ckpt.phase_done("feedback")
    phase_total = time.monotonic() - phase_start
    record_phase_timing(
        "feedback",
        phase_total,
        processed_this_run,
        "feedback record",
        chunk_timer.times,
    )
    print()
    print(f"Total Feedback Inserted: {inserted:,}")
    print(f"Total Feedback Skipped: {skipped:,}")
    print("=== Phase 3: Complete ===")
    print()


# ── Conflict resolution ───────────────────────────────────────────────────────


def format_date(dt) -> str:
    if dt is None:
        return "now"
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    # Convert to the machine's local timezone for display.
    return dt.astimezone().strftime("%Y-%m-%d %I:%M %p")


def prompt_choice(options: list) -> int:
    """Print a numbered menu and return the chosen 1-based index.

    Aborts if stdin is not a TTY — automation should pass an explicit --resume.
    """
    if not sys.stdin.isatty():
        print(
            "\nAn existing migration conflicts with this one and this is a "
            "non-interactive session.\nRe-run with --resume <state_file>, or "
            "delete the checkpoint to start fresh.",
            file=sys.stderr,
        )
        sys.exit(1)

    for i, label in enumerate(options, start=1):
        print(f"  {i}. {label}")
    while True:
        answer = input("Choose: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)


def prompt_phases(available: list) -> list:
    """Ask which of `available` phases to run. Returns a subset in order.

    Accepts 'all' or a comma-separated list of phase names/numbers.
    """
    print("\nWhich phases? (comma-separated, or 'all')")
    for i, phase in enumerate(available, start=1):
        print(f"  {i}. {phase}")
    while True:
        answer = input("Phases: ").strip().lower()
        if answer in ("", "all"):
            return list(available)
        chosen = []
        for token in answer.split(","):
            token = token.strip()
            if token.isdigit() and 1 <= int(token) <= len(available):
                chosen.append(available[int(token) - 1])
            elif token in available:
                chosen.append(token)
            else:
                chosen = None
                break
        if chosen:
            # Preserve canonical order, drop dupes.
            return [p for p in available if p in chosen]


def resolve_conflict(ckpt, from_dt, to_dt, to_is_now, task_ids=None):
    """Reconcile a requested window against an existing checkpoint's window.

    Returns (effective_ckpt, from_dt, to_dt). The returned checkpoint may be a
    new one when the user chooses to continue from where the last run ended.
    """
    stored_from = ckpt.state.get("from_dt")
    stored_to = ckpt.state.get("to_dt")
    new_from = from_dt.isoformat() if from_dt else None
    new_to = to_dt.isoformat() if to_dt else None

    # No prior window, or an exact match — nothing to reconcile.
    if (stored_from is None and stored_to is None) or (
        new_from == stored_from and new_to == stored_to
    ):
        ckpt.set_window(from_dt, to_dt)
        return ckpt, from_dt, to_dt, None

    prior_from = datetime.fromisoformat(stored_from) if stored_from else None
    prior_to = datetime.fromisoformat(stored_to) if stored_to else None
    completed = all(ckpt.phase_completed(p) for p in ALL_PHASES)

    label = "Completed" if completed else "Existing"
    print(
        f"\n{label} Migration Found From {format_date(prior_from)} → "
        f"{format_date(prior_to)}. Would you like to:",
    )

    end_label = "now" if to_is_now else format_date(to_dt)
    if completed:
        choice = prompt_choice(
            [
                f"Continue from {format_date(prior_to)} → {end_label}",
                "Abort",
            ],
        )
        if choice == 2:
            sys.exit(0)
        # Fresh checkpoint for the continuation window prior_to → to_dt.
        new_ckpt = Checkpoint(checkpoint_path(prior_to, to_dt, task_ids))
        new_ckpt.set_window(prior_to, to_dt)
        new_ckpt.set_task_scope(task_ids)
        return new_ckpt, prior_to, to_dt, prompt_phases(ALL_PHASES)

    choice = prompt_choice(
        [
            "Continue where it left off",
            f"Continue from {format_date(prior_to)} → {end_label}",
            "Abort",
        ],
    )
    if choice == 3:
        sys.exit(0)
    if choice == 1:
        incomplete = [p for p in ALL_PHASES if not ckpt.phase_completed(p)]
        return ckpt, prior_from, prior_to, prompt_phases(incomplete)
    # Continue: extend the window end on the SAME checkpoint so completed phases
    # stay marked done and only unfinished ones run. Reset page counters — the
    # old counts index into the previous window and don't apply to the new one;
    # skip-existing on the Engine side prevents any duplicate inserts.
    ckpt.state["from_dt"] = stored_to
    ckpt.state["to_dt"] = new_to
    ckpt.state["inference_page"] = 0
    ckpt.state["feedback_page"] = 0
    ckpt.state["archived_rules_migrated"] = False
    ckpt._save()
    return ckpt, prior_to, to_dt, prompt_phases(ALL_PHASES)


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--phase",
        choices=["all", "config", "inferences", "feedback"],
        default="all",
    )
    parser.add_argument(
        "--from-date",
        default=None,
        help="Start of date window (inclusive), e.g. 2020-01-01",
    )
    parser.add_argument(
        "--to-date",
        default=None,
        help="End of date window (exclusive), e.g. 2021-01-01",
    )
    parser.add_argument(
        "--last-days",
        type=int,
        default=None,
        help="Shorthand: migrate the last N days",
    )
    parser.add_argument(
        "--task-ids",
        nargs="+",
        default=None,
        metavar="TASK_ID",
        help="Migrate only these tasks: their config, inferences, and feedback. "
        "A date window is still required.",
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="STATE_FILE",
        help="Path to a migration_state_*.json file to resume from",
    )
    parser.add_argument(
        "--timing",
        action="store_true",
        help="Print a timing report at the end of the run",
    )
    parser.add_argument(
        "--recover",
        action="store_true",
        help="Rebuild the save file for the selected phases by verifying what "
        "already exists in the Engine. Writes nothing to the Engine.",
    )
    args = parser.parse_args()

    # Require a window start unless resuming (then it comes from the checkpoint).
    # --last-days, or --from-date (--to-date optional, defaults to now).
    if not args.resume and args.last_days is None and args.from_date is None:
        parser.error(
            "specify a date window: --last-days N, or --from-date "
            "(with optional --to-date)",
        )

    from_dt, to_dt = parse_window(args)
    task_ids = sorted(set(args.task_ids)) if args.task_ids else None

    # Phases to run. Defaults to the --phase flag; a "Continue" choice below
    # lets the user override this interactively.
    if args.phase == "all":
        phases = list(ALL_PHASES)
    else:
        phases = [args.phase]

    if args.recover:
        # Recovery bypasses the interactive conflict prompts: it writes the
        # verified IDs into the window's save file (creating it if missing).
        ckpt = Checkpoint(args.resume or checkpoint_path(from_dt, to_dt, task_ids))
        if args.resume:
            stored_from = ckpt.state.get("from_dt")
            stored_to = ckpt.state.get("to_dt")
            from_dt = datetime.fromisoformat(stored_from) if stored_from else None
            to_dt = datetime.fromisoformat(stored_to) if stored_to else None
            task_ids = ckpt.state.get("task_ids") or None
        else:
            # The requested window is authoritative in recovery — overwrite the
            # stored one directly, set_window's mismatch guard doesn't apply.
            ckpt.state["from_dt"] = from_dt.isoformat() if from_dt else None
            ckpt.state["to_dt"] = to_dt.isoformat() if to_dt else None
            ckpt._save()
            ckpt.set_task_scope(task_ids)
        if "config" in phases:
            migrate_config(ckpt, task_ids, recover=True)
        if "inferences" in phases:
            migrate_inferences(ckpt, from_dt, to_dt, task_ids, recover=True)
        if "feedback" in phases:
            print("[feedback] Nothing recorded in the save file for feedback.")
        if args.timing:
            print_timing_report()
        print(f"\nRecovery complete. State saved to: {ckpt.path}")
        return

    if args.resume:
        # Explicit resume: use the checkpoint as-is, honoring its stored window.
        ckpt = Checkpoint(args.resume)
        stored_from = ckpt.state.get("from_dt")
        stored_to = ckpt.state.get("to_dt")
        from_dt = datetime.fromisoformat(stored_from) if stored_from else None
        to_dt = datetime.fromisoformat(stored_to) if stored_to else None
        stored_task_ids = ckpt.state.get("task_ids") or []
        if task_ids is not None and task_ids != sorted(stored_task_ids):
            parser.error(
                f"task scope mismatch on resume: checkpoint is scoped to "
                f"{stored_task_ids or 'all tasks'}, command requested {task_ids}. "
                f"Pass --resume without --task-ids, or start a new run.",
            )
        task_ids = stored_task_ids or None
    else:
        to_is_now = args.to_date is None
        latest = latest_checkpoint_path(task_ids)
        if latest:
            ckpt = Checkpoint(latest)
            ckpt, from_dt, to_dt, chosen_phases = resolve_conflict(
                ckpt,
                from_dt,
                to_dt,
                to_is_now,
                task_ids,
            )
            if chosen_phases is not None:
                phases = chosen_phases
        else:
            ckpt = Checkpoint(checkpoint_path(from_dt, to_dt, task_ids))
            ckpt.set_window(from_dt, to_dt)
            ckpt.set_task_scope(task_ids)

    if "config" in phases:
        migrate_config(ckpt, task_ids)
    if "inferences" in phases:
        migrate_inferences(ckpt, from_dt, to_dt, task_ids)
    if "feedback" in phases:
        migrate_feedback(ckpt, from_dt, to_dt, task_ids)

    if args.timing:
        print_timing_report()

    print(f"\nMigration complete. State saved to: {ckpt.path}")


if __name__ == "__main__":
    main()
