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
"""

import argparse
import json
import os
import sys
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

CHECKPOINT_DIR = os.getenv("MIGRATION_CHECKPOINT_DIR", default="migration_states")

SHIELD_PAGE_SIZE = int(
    os.getenv("SHIELD_PAGE_SIZE", default=5000),
)  # max page size supported by Shield API
ENGINE_BATCH_SIZE = int(
    os.getenv("ENGINE_BATCH_SIZE", default=500),
)  # inferences per POST to the Engine
MIGRATION_TIMEOUT = int(
    os.getenv("MIGRATION_TIMEOUT", default=30),
)  # seconds for all HTTP calls

ALL_PHASES = ["config", "inferences", "feedback"]

# ── Shield API helpers ────────────────────────────────────────────────────────


def shield_get(path, params=None):
    """GET request to Shield. Params dict may include lists for repeated keys."""
    r = requests.get(
        f"{SHIELD_BASE_URL}/{path}",
        headers={
            "Authorization": f"Bearer {SHIELD_API_KEY}",
        },
        params=params or {},
        timeout=MIGRATION_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def shield_post(path, body):
    r = requests.post(
        f"{SHIELD_BASE_URL}/{path}",
        json=body,
        headers={
            "Authorization": f"Bearer {SHIELD_API_KEY}",
        },
        timeout=MIGRATION_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def shield_paginate(path, body, count_key, items_key):
    """Fetch all pages from a Shield POST search endpoint. Pages start at 0."""
    page, all_items = 0, []
    while True:
        resp = shield_post(path, {**body, "page_size": SHIELD_PAGE_SIZE, "page": page})
        total = resp.get(count_key, 0)
        batch = resp.get(items_key, [])
        all_items.extend(batch)
        if len(all_items) >= total or not batch:
            break
        page += 1
    return all_items


# ── Engine API helpers ────────────────────────────────────────────────────────


def engine_post(path, body):
    r = requests.post(
        f"{ENGINE_BASE_URL}{path}",
        json=body,
        headers={
            "Authorization": f"Bearer {ENGINE_API_KEY}",
        },
        timeout=MIGRATION_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


# ── Utilities ─────────────────────────────────────────────────────────────────


def parse_window(args):
    now = datetime.now(timezone.utc)

    if args.last_days:
        return now - timedelta(days=args.last_days), now

    from_dt = datetime.fromisoformat(args.from_date) if args.from_date else None
    to_dt = datetime.fromisoformat(args.to_date) if args.to_date else now

    return from_dt, to_dt


def checkpoint_path(from_dt, to_dt) -> str:
    window_slug = (
        f"{from_dt.date() if from_dt else 'all'}"
        f"_to_{to_dt.date() if to_dt else 'now'}"
    )
    return f"{CHECKPOINT_DIR}/migration_state_{window_slug}.json"


def latest_checkpoint_path():
    """Path of the most-recently-updated checkpoint, or None if none exist.

    Ranks by each checkpoint's own last_updated_at field rather than file mtime.
    """
    if not os.path.isdir(CHECKPOINT_DIR):
        return None
    latest_path, latest_ts = None, None
    for f in os.listdir(CHECKPOINT_DIR):
        if not f.endswith(".json"):
            continue
        path = os.path.join(CHECKPOINT_DIR, f)
        with open(path) as fh:
            ts = json.load(fh).get("last_updated_at")
        if ts is not None and (latest_ts is None or ts > latest_ts):
            latest_path, latest_ts = path, ts
    return latest_path


# ── Checkpoint ────────────────────────────────────────────────────────────────


class Checkpoint:
    """
    Persists migration state so any phase can be safely resumed.

    State keys:
      from_dt / to_dt         — date window (ISO strings or null)
      phases_completed        — list of phase names that finished successfully
      inference_page          — next Shield inference page to fetch (starts at 0)
      feedback_page           — next Shield feedback page to fetch (starts at 0)
      migrated_task_ids       — task IDs inserted into the Engine (for cleanup)
      started_at / last_updated_at — ISO timestamps
    """

    def __init__(self, path: str):
        self.path = path
        if os.path.exists(path):
            with open(path) as f:
                self.state = json.load(f)
            # Backfill keys added after this checkpoint was first written.
            self.state.setdefault("migrated_task_ids", [])
        else:
            self.state = {
                "from_dt": None,
                "to_dt": None,
                "phases_completed": [],
                "inference_page": 0,
                "feedback_page": 0,
                "migrated_task_ids": [],
                "started_at": datetime.now(timezone.utc).isoformat(),
                "last_updated_at": datetime.now(timezone.utc).isoformat(),
            }
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


def migrate_config(ckpt: Checkpoint):
    if ckpt.phase_completed("config"):
        print("[config] Already completed — skipping.")
        return

    print("=== Phase 1: Config ===")

    tasks = shield_paginate("/api/v2/tasks/search", {}, "count", "tasks")
    print(f"  Fetched {len(tasks)} tasks")

    default_rules = shield_get("/api/v2/default_rules")
    task_rules = shield_paginate(
        "/api/v2/rules/search",
        {"rule_scopes": ["task"]},
        "count",
        "rules",
    )
    archived_rules = shield_paginate(
        "/api/v2/rules/search",
        {"include_archived": True},
        "count",
        "rules",
    )
    for rule in archived_rules:
        rule["archived"] = True
    all_rules = default_rules + task_rules + archived_rules
    print(
        f"  Fetched {len(default_rules)} default + {len(task_rules)} task-scoped "
        f"+ {len(archived_rules)} archived rules",
    )

    resp = engine_post("/api/v1/migration/rules/bulk", {"rules": all_rules})
    inserted = len(resp.get("rules", []))
    print(
        f"  Rules: "
        f"    {inserted} inserted"
        f"    {len(all_rules) - inserted} skipped (already existed)",
    )

    # Strip the embedded rules array before sending tasks — links are sent
    # separately below so the Engine endpoint does NOT auto-link rules on insert.
    task_rows = [{k: v for k, v in t.items() if k != "rules"} for t in tasks]
    resp = engine_post(
        "/api/v1/migration/tasks/bulk",
        {"tasks": task_rows, "org_id": ENGINE_ORG_ID},
    )
    inserted_tasks = resp.get("tasks", [])
    ckpt.record_migrated_tasks(
        [t["id"] for t in inserted_tasks if t.get("id")],
    )
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
    resp = engine_post(
        "/api/v1/migration/task_rule_links/bulk",
        {"task_to_rule_links": links},
    )
    inserted = len(resp.get("task_to_rule_links", []))
    print(
        f"  Task–rule links: "
        f"    {inserted} inserted"
        f"    {len(links) - inserted} skipped (already existed)",
    )

    ckpt.phase_done("config")
    print("=== Phase 1: Complete ===")
    print()


# ── Phase 2: Inferences ───────────────────────────────────────────────────────


def migrate_inferences(ckpt: Checkpoint, from_dt, to_dt):
    if ckpt.phase_completed("inferences"):
        print("[inferences] Already completed — skipping.")
        return

    print(
        f"=== Phase 2: Inferences "
        f"(from={from_dt.date() if from_dt else 'beginning'} "
        f"to={to_dt.date() if to_dt else 'now'}) ===",
    )

    # /api/v2/migration/inferences/query returns the full inference subtree with
    # real rule-result/detail IDs and token counts intact, so inferences are
    # forwarded to the Engine unchanged. Pages start at 0.
    inf_params: dict = {}
    if from_dt:
        inf_params["start_time"] = from_dt.isoformat()
    if to_dt:
        inf_params["end_time"] = to_dt.isoformat()

    start_page = ckpt.state["inference_page"]
    if start_page > 0:
        print(
            f"  Resuming from page {start_page} "
            f"(~{start_page * SHIELD_PAGE_SIZE:,} already committed)",
        )

    page = start_page
    processed = start_page * SHIELD_PAGE_SIZE
    inserted = 0
    skipped = 0

    while True:
        fetch_params = {
            **inf_params,
            "page_size": SHIELD_PAGE_SIZE,
            "page": page,
            "sort": "asc",
        }
        resp = shield_get("/api/v2/migration/inferences/query", params=fetch_params)
        batch = resp.get("inferences", [])
        total_count = resp.get("count", 0)
        if not batch:
            break

        for i in range(0, len(batch), ENGINE_BATCH_SIZE):
            post_resp = engine_post(
                "/api/v1/migration/inferences/bulk",
                {
                    "inferences": batch[i : i + ENGINE_BATCH_SIZE],
                    "org_id": ENGINE_ORG_ID,
                },
            )
            inserted += post_resp.get("inserted", 0)
            skipped += post_resp.get("skipped", 0)

        processed += len(batch)
        page += 1
        ckpt.update_inference_page(page)  # checkpoint: next page to fetch on resume
        pct = processed / total_count * 100 if total_count else 0
        print(
            f"  [{pct:5.1f}%] {processed:,} / {total_count:,} inferences processed "
            f"({inserted:,} inserted, {skipped:,} skipped) (page {page - 1})",
        )

        if len(batch) < SHIELD_PAGE_SIZE:
            break

    ckpt.phase_done("inferences")
    print()
    print(f"Total Inferences Inserted: {inserted:,}")
    print(f"Total Inferences Skipped: {skipped:,}")
    print("=== Phase 2: Complete ===")
    print()


# ── Phase 3: Feedback ─────────────────────────────────────────────────────────


def migrate_feedback(ckpt: Checkpoint, from_dt, to_dt):
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

    start_page = ckpt.state["feedback_page"]
    page = start_page
    processed = start_page * SHIELD_PAGE_SIZE
    inserted = 0
    skipped = 0

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

        for i in range(0, len(batch), ENGINE_BATCH_SIZE):
            post_resp = engine_post(
                "/api/v1/migration/feedback/bulk",
                {"feedback": batch[i : i + ENGINE_BATCH_SIZE], "org_id": ENGINE_ORG_ID},
            )
            inserted += post_resp.get("inserted", 0)
            skipped += post_resp.get("skipped", 0)

        processed += len(batch)
        page += 1
        ckpt.update_feedback_page(page)
        print(
            f"  {processed:,} / {total_count:,} feedback records processed "
            f"({inserted:,} inserted, {skipped:,} skipped) (page {page - 1})",
        )

        if len(batch) < SHIELD_PAGE_SIZE:
            break

    ckpt.phase_done("feedback")
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


def resolve_conflict(ckpt, from_dt, to_dt, to_is_now):
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
        new_ckpt = Checkpoint(checkpoint_path(prior_to, to_dt))
        new_ckpt.set_window(prior_to, to_dt)
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
        "--resume",
        default=None,
        metavar="STATE_FILE",
        help="Path to a migration_state_*.json file to resume from",
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

    # Phases to run. Defaults to the --phase flag; a "Continue" choice below
    # lets the user override this interactively.
    if args.phase == "all":
        phases = list(ALL_PHASES)
    else:
        phases = [args.phase]

    if args.resume:
        # Explicit resume: use the checkpoint as-is, honoring its stored window.
        ckpt = Checkpoint(args.resume)
        stored_from = ckpt.state.get("from_dt")
        stored_to = ckpt.state.get("to_dt")
        from_dt = datetime.fromisoformat(stored_from) if stored_from else None
        to_dt = datetime.fromisoformat(stored_to) if stored_to else None
    else:
        to_is_now = args.to_date is None
        latest = latest_checkpoint_path()
        if latest:
            ckpt = Checkpoint(latest)
            ckpt, from_dt, to_dt, chosen_phases = resolve_conflict(
                ckpt,
                from_dt,
                to_dt,
                to_is_now,
            )
            if chosen_phases is not None:
                phases = chosen_phases
        else:
            ckpt = Checkpoint(checkpoint_path(from_dt, to_dt))
            ckpt.set_window(from_dt, to_dt)

    if "config" in phases:
        migrate_config(ckpt)
    if "inferences" in phases:
        migrate_inferences(ckpt, from_dt, to_dt)
    if "feedback" in phases:
        migrate_feedback(ckpt, from_dt, to_dt)

    print(f"\nMigration complete. State saved to: {ckpt.path}")


if __name__ == "__main__":
    main()
