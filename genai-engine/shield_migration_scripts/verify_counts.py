# verify_counts.py
"""
Verifies a Shield → Engine migration by comparing row counts between the two
PostgreSQL databases directly (no Shield API), over the same date window and
task scope recorded in the migration run's checkpoint file.

For each section it counts the rows in Shield (source) and in Engine (target)
and reports a ✓ when they match, ✗ when they don't. Shield inferences that
reference an archived task are excluded from the Shield-side counts. A final
section runs an Engine-side sanity check that no org-scoped rows were inserted
without an org_id.

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

The Engine org the data was migrated into:
    ENGINE_ORG_ID

Usage:
    python verify_counts.py --save-file migration_states/migration_state_2026-01-21_to_2026-07-20.json
"""

import argparse
import json
import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests
from dotenv import load_dotenv
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Engine

load_dotenv()

# API-mode configuration, matching migrate_shield_to_engine.py.
SHIELD_BASE_URL = os.getenv("SHIELD_BASE_URL")
SHIELD_API_KEY = os.getenv("SHIELD_API_KEY")
ENGINE_BASE_URL = os.getenv("ENGINE_BASE_URL")
ENGINE_API_KEY = os.getenv("ENGINE_API_KEY")

SHIELD_PAGE_SIZE = int(
    os.getenv("SHIELD_PAGE_SIZE", default=4999),
)  # Shield requires page_size > 0 and < 5000
MIGRATION_TIMEOUT = int(
    os.getenv("MIGRATION_TIMEOUT", default=30),
)  # seconds for all HTTP calls
MAX_WORKERS = int(os.getenv("MIGRATION_MAX_WORKERS", default=10))
MAX_ATTEMPTS = 6
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


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


def shield_get(path, params=None):
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
    """Fetch all pages from a Shield POST search endpoint. Pages start at 0."""
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


def get_engine(prefix: str) -> Engine:
    """Build a SQLAlchemy engine from <prefix>_POSTGRES_* env vars."""
    user = os.environ[f"{prefix}_POSTGRES_USER"]
    password = os.environ[f"{prefix}_POSTGRES_PASSWORD"]
    host = os.environ[f"{prefix}_POSTGRES_URL"]
    port = os.environ[f"{prefix}_POSTGRES_PORT"]
    db_name = os.environ[f"{prefix}_POSTGRES_DB"]

    params = {}
    if os.getenv(f"{prefix}_POSTGRES_USE_SSL", "false").lower() == "true":
        params["sslmode"] = "verify-full"
        ssl_root_cert = os.getenv(f"{prefix}_POSTGRES_SSL_ROOT_CERT")
        if ssl_root_cert:
            params["sslrootcert"] = ssl_root_cert

    query = urllib.parse.urlencode(params)
    conn_str = (
        f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db_name}?" + query
    )
    return create_engine(conn_str, pool_pre_ping=True)


def fmt(n):
    return f"{n:,}"


def window_clause(from_dt, to_dt, column="created_at"):
    """Build a WHERE fragment + params for an optional created_at window."""
    clauses, params = [], {}
    if from_dt:
        clauses.append(f"{column} >= :from_dt")
        params["from_dt"] = from_dt
    if to_dt:
        clauses.append(f"{column} < :to_dt")
        params["to_dt"] = to_dt
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def and_clause(where, condition):
    """Append a condition to an existing WHERE fragment (or start one)."""
    return where + (" AND " if where else "WHERE ") + condition


def task_clause(where, params, task_ids):
    """Append an i.task_id filter to a WHERE fragment when task-scoped."""
    if not task_ids:
        return where, params
    return (
        and_clause(where, "i.task_id IN :task_ids"),
        {**params, "task_ids": task_ids},
    )


def migrated_scope_clause(where, params, migrated_task_ids, taskless_inference_ids):
    """Scope engine-side counts to the rows this migration actually inserted."""
    conditions = []
    scope_params = {}
    if migrated_task_ids:
        conditions.append("i.task_id = ANY(:migrated_task_ids)")
        scope_params["migrated_task_ids"] = migrated_task_ids
    if taskless_inference_ids:
        conditions.append("i.id = ANY(:taskless_inference_ids)")
        scope_params["taskless_inference_ids"] = taskless_inference_ids
    if not conditions:
        return where, params
    return (
        and_clause(where, "(" + " OR ".join(conditions) + ")"),
        {**params, **scope_params},
    )


def count(conn, table, where="", params=None):
    params = params or {}
    sql = f"SELECT COUNT(*) FROM {table} {where}"
    stmt = text(sql)
    for key, value in params.items():
        if isinstance(value, list) and f"ANY(:{key})" not in sql:
            stmt = stmt.bindparams(bindparam(key, expanding=True))
    return conn.execute(stmt, params).scalar_one()


def compare(label, shield_n, engine_n):
    status = "✓" if shield_n == engine_n else "✗ MISMATCH"
    return f"  {status:<10} {label:<28} shield={fmt(shield_n)}  engine={fmt(engine_n)}"


def shield_paginate_feedback(params):
    """Fetch all feedback rows from Shield's GET query. Pages start at 0."""
    page, rows = 0, []
    while True:
        resp = shield_get(
            "/api/v2/feedback/query",
            {**params, "page_size": SHIELD_PAGE_SIZE, "page": page},
        )
        batch = resp.get("feedback", [])
        rows.extend(batch)
        if not batch or len(rows) >= resp.get("total_count", 0):
            return rows
        page += 1


def parent_inference_in_window(inference_id, from_dt, to_dt):
    """True when the Shield inference exists and was created inside the window."""
    resp = shield_get(
        "/api/v2/inferences/query",
        {"inference_id": inference_id, "page_size": 1, "page": 0},
    )
    inferences = resp.get("inferences", [])
    if not inferences:
        return False
    created = inferences[0].get("created_at", 0) / 1000
    if from_dt and created < from_dt.timestamp():
        return False
    if to_dt and created >= to_dt.timestamp():
        return False
    return True


def verify_api(
    from_dt,
    to_dt,
    task_ids=None,
    migrated_task_ids=None,
    taskless_inference_ids=None,
    migrated_rule_ids=None,
    phases_completed=None,
):
    """Verify through the Shield and Engine APIs only — no database access."""
    phases_completed = phases_completed or []
    lines = []
    all_match = True

    window_label = "all time"
    if from_dt:
        window_label = f"from {from_dt.date()}"
    if to_dt:
        window_label += f" to {to_dt.date()}"

    lines.append(f"\n{'='*70}")
    lines.append(f"  Shield → Engine Migration Verification")
    lines.append(f"  Window: {window_label}")
    if task_ids:
        lines.append(f"  Tasks:  {', '.join(task_ids)}")
    lines.append(f"  Mode:   api")
    lines.append(f"  Phases: {', '.join(phases_completed) or 'none completed'}")
    lines.append(f"{'='*70}\n")

    window_params = {}
    if from_dt:
        window_params["start_time"] = from_dt.isoformat()
    if to_dt:
        window_params["end_time"] = to_dt.isoformat()
    count_page = {"page_size": 1, "page": 0}
    inf_params = {**window_params, **count_page}
    fb_params = dict(window_params)
    if task_ids:
        inf_params["task_ids"] = task_ids
        fb_params["task_id"] = task_ids

    # All fetches are independent — run them concurrently, then report.
    futures = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        if "config" in phases_completed and migrated_task_ids:
            futures["shield_tasks"] = executor.submit(
                shield_paginate,
                "/api/v2/tasks/search",
                {"task_ids": migrated_task_ids},
                "count",
                "tasks",
            )
            futures["engine_tasks"] = executor.submit(
                engine_paginate,
                "/api/v2/tasks/search",
                {"task_ids": migrated_task_ids},
                "count",
                "tasks",
            )
        if "config" in phases_completed and migrated_rule_ids:
            # The Engine rule search only returns active rules, so both sides
            # compare active rules only.
            futures["shield_rules"] = executor.submit(
                shield_post,
                "/api/v2/rules/search",
                {"rule_ids": migrated_rule_ids},
                count_page,
            )
            futures["engine_rules"] = executor.submit(
                engine_call,
                "POST",
                "/api/v2/rules/search",
                {"rule_ids": migrated_rule_ids},
                count_page,
            )
        inference_chunks = []
        if "inferences" in phases_completed and migrated_task_ids:
            # Scope both sides to the migrated tasks (chunked to keep GET URLs
            # short) so archived-task inferences never enter the counts.
            for start in range(0, len(migrated_task_ids), 100):
                chunk_params = {
                    **inf_params,
                    "task_ids": migrated_task_ids[start : start + 100],
                }
                inference_chunks.append(
                    (
                        executor.submit(
                            shield_get,
                            "/api/v2/migration/inferences/query",
                            chunk_params,
                        ),
                        executor.submit(
                            engine_call,
                            "GET",
                            "/api/v2/inferences/query",
                            None,
                            chunk_params,
                        ),
                    ),
                )
        elif "inferences" in phases_completed:
            futures["shield_inferences"] = executor.submit(
                shield_get,
                "/api/v2/migration/inferences/query",
                inf_params,
            )
            futures["engine_inferences"] = executor.submit(
                engine_call,
                "GET",
                "/api/v2/inferences/query",
                None,
                inf_params,
            )
        feedback_chunks = []
        if "feedback" in phases_completed:
            # Scope both sides to migrated tasks (and task-less parents by
            # inference_id) so archived-task feedback never enters the counts.
            task_id_list = migrated_task_ids or []
            taskless_list = taskless_inference_ids or []
            feedback_scopes = []
            for start in range(0, len(task_id_list), 100):
                feedback_scopes.append(
                    {"task_id": task_id_list[start : start + 100]},
                )
            for start in range(0, len(taskless_list), 100):
                feedback_scopes.append(
                    {"inference_id": taskless_list[start : start + 100]},
                )
            if not feedback_scopes:
                feedback_scopes = [{}]
            for scope in feedback_scopes:
                feedback_chunks.append(
                    (
                        executor.submit(
                            shield_paginate_feedback,
                            {**fb_params, **scope},
                        ),
                        executor.submit(
                            engine_call,
                            "GET",
                            "/api/v2/feedback/query",
                            None,
                            {**fb_params, **scope, **count_page},
                        ),
                    ),
                )
        taskless_futures = []
        if "inferences" in phases_completed and taskless_inference_ids:
            taskless_futures = [
                executor.submit(
                    engine_call,
                    "GET",
                    "/api/v2/inferences/query",
                    None,
                    {"inference_id": inference_id, **count_page},
                )
                for inference_id in taskless_inference_ids
            ]
        results = {name: future.result() for name, future in futures.items()}
        taskless_found = sum(
            1 for future in taskless_futures if future.result().get("count", 0) > 0
        )

    # ── Config ────────────────────────────────────────────────────────────
    if "config" in phases_completed:
        lines.append("Config (migrated IDs recorded in the save file)")
        if migrated_task_ids:
            engine_tasks = results["engine_tasks"]
            all_match = all_match and len(engine_tasks) == len(migrated_task_ids)
            lines.append(compare("tasks", len(migrated_task_ids), len(engine_tasks)))
            s = sum(len(t.get("rules", [])) for t in results["shield_tasks"])
            e = sum(len(t.get("rules", [])) for t in engine_tasks)
            all_match = all_match and s == e
            lines.append(compare("task_rule_links", s, e))
        if migrated_rule_ids:
            s = results["shield_rules"].get("count", 0)
            e = results["engine_rules"].get("count", 0)
            all_match = all_match and s == e
            lines.append(compare("rules (active)", s, e))
        if not migrated_task_ids and not migrated_rule_ids:
            lines.append("  (save file records no migrated task or rule IDs)")
        lines.append("")

    # ── Inferences ────────────────────────────────────────────────────────
    if "inferences" in phases_completed:
        lines.append("Inferences")
        if inference_chunks:
            s = sum(sf.result().get("count", 0) for sf, _ in inference_chunks)
            e = sum(ef.result().get("count", 0) for _, ef in inference_chunks)
        else:
            s = results["shield_inferences"].get("count", 0)
            e = results["engine_inferences"].get("count", 0)
        all_match = all_match and s == e
        lines.append(compare("inferences", s, e))
        if taskless_inference_ids:
            all_match = all_match and taskless_found == len(taskless_inference_ids)
            lines.append(
                compare(
                    "taskless_inferences",
                    len(taskless_inference_ids),
                    taskless_found,
                ),
            )
        lines.append("")

    # ── Feedback ──────────────────────────────────────────────────────────
    if "feedback" in phases_completed:
        lines.append("Feedback")
        e = sum(ef.result().get("total_count", 0) for _, ef in feedback_chunks)
        shield_rows = []
        for sf, _ in feedback_chunks:
            shield_rows.extend(sf.result())
        # The feedback API can't filter on the parent inference's window, so
        # check each row's parent the way SQL mode's join does.
        parent_ids = {row["inference_id"] for row in shield_rows}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            parents_in_window = {
                inference_id: executor.submit(
                    parent_inference_in_window,
                    inference_id,
                    from_dt,
                    to_dt,
                )
                for inference_id in parent_ids
            }
            s = sum(
                1
                for row in shield_rows
                if parents_in_window[row["inference_id"]].result()
            )
        all_match = all_match and s == e
        lines.append(compare("inference_feedback", s, e))
        lines.append("")

    lines.append("=" * 70)
    lines.append("  RESULT: " + ("ALL MATCH ✓" if all_match else "MISMATCHES FOUND ✗"))
    lines.append("=" * 70)

    print("\n".join(lines))
    return all_match


def verify(
    shield: Engine,
    engine: Engine,
    from_dt,
    to_dt,
    org_id,
    task_ids=None,
    migrated_task_ids=None,
    taskless_inference_ids=None,
    migrated_rule_ids=None,
    phases_completed=None,
):
    lines = []
    phases_completed = phases_completed or []

    window_label = "all time"
    if from_dt:
        window_label = f"from {from_dt.date()}"
    if to_dt:
        window_label += f" to {to_dt.date()}"

    lines.append(f"\n{'='*70}")
    lines.append(f"  Shield → Engine Migration Verification")
    lines.append(f"  Window: {window_label}")
    if task_ids:
        lines.append(f"  Tasks:  {', '.join(task_ids)}")
    lines.append(f"  Org:    {org_id}")
    lines.append(f"  Phases: {', '.join(phases_completed) or 'none completed'}")
    lines.append(f"{'='*70}\n")

    inf_where, inf_params = window_clause(from_dt, to_dt)
    # Engine's org-scoped tables filter by created_at window AND org_id.
    org_where = inf_where + (" AND " if inf_where else "WHERE ") + "org_id = :org_id"
    org_params = {**inf_params, "org_id": org_id}

    all_match = True

    parent_where, parent_params = window_clause(from_dt, to_dt, column="i.created_at")
    inference_tables = (
        ("inferences", "inferences i"),
        (
            "inference_prompts",
            "inference_prompts c JOIN inferences i ON c.inference_id = i.id",
        ),
        (
            "inference_responses",
            "inference_responses c JOIN inferences i ON c.inference_id = i.id",
        ),
        (
            "inference_prompt_contents",
            "inference_prompt_contents c "
            "JOIN inference_prompts p ON c.inference_prompt_id = p.id "
            "JOIN inferences i ON p.inference_id = i.id",
        ),
        (
            "inference_response_contents",
            "inference_response_contents c "
            "JOIN inference_responses r ON c.inference_response_id = r.id "
            "JOIN inferences i ON r.inference_id = i.id",
        ),
    )

    # Exclude Shield inferences referencing archived tasks from the
    # Shield-side counts and reported separately.
    archived_join = "LEFT JOIN tasks t ON i.task_id = t.id"
    kept = "(i.task_id IS NULL OR COALESCE(t.archived, TRUE) = FALSE)"
    excluded = "(i.task_id IS NOT NULL AND COALESCE(t.archived, TRUE) = TRUE)"

    with shield.connect() as sc, engine.connect() as ec:
        # ── Config (tasks / rules / links recorded in the save file) ────────
        if "config" in phases_completed:
            lines.append("Config (migrated IDs recorded in the save file)")
            if migrated_task_ids:
                e = count(
                    ec,
                    "tasks",
                    "WHERE id = ANY(:mids)",
                    {"mids": migrated_task_ids},
                )
                all_match = all_match and e == len(migrated_task_ids)
                lines.append(compare("tasks", len(migrated_task_ids), e))
                s = count(
                    sc,
                    "tasks_to_rules",
                    "WHERE task_id = ANY(:mids)",
                    {"mids": migrated_task_ids},
                )
                e = count(
                    ec,
                    "tasks_to_rules",
                    "WHERE task_id = ANY(:mids)",
                    {"mids": migrated_task_ids},
                )
                all_match = all_match and s == e
                lines.append(compare("task_rule_links", s, e))
            if migrated_rule_ids:
                e = count(
                    ec,
                    "rules",
                    "WHERE id = ANY(:mids)",
                    {"mids": migrated_rule_ids},
                )
                all_match = all_match and e == len(migrated_rule_ids)
                lines.append(compare("rules", len(migrated_rule_ids), e))
            if not migrated_task_ids and not migrated_rule_ids:
                lines.append("  (save file records no migrated task or rule IDs)")
            archived_tasks_n = count(sc, "tasks", "WHERE archived")
            lines.append(
                f"\nShield tasks archived (not migrated): {fmt(archived_tasks_n)}",
            )
            lines.append("")

        # ── Inferences ──────────────────────────────────────────────────────
        verify_inferences = "inferences" in phases_completed
        if verify_inferences:
            lines.append("Inferences")
            for label, from_sql in inference_tables:
                s_where, s_params = task_clause(
                    and_clause(parent_where, kept),
                    parent_params,
                    task_ids,
                )
                e_where, e_params = task_clause(parent_where, parent_params, task_ids)
                e_where, e_params = migrated_scope_clause(
                    e_where,
                    e_params,
                    migrated_task_ids,
                    taskless_inference_ids,
                )
                s = count(sc, f"{from_sql} {archived_join}", s_where, s_params)
                e = count(ec, from_sql, e_where, e_params)
                all_match = all_match and s == e
                lines.append(compare(label, s, e))
            excluded_where, excluded_params = task_clause(
                and_clause(parent_where, excluded),
                parent_params,
                task_ids,
            )
            excluded_n = count(
                sc,
                f"inferences i {archived_join}",
                excluded_where,
                excluded_params,
            )
            lines.append(
                f"\nShield inferences referencing archived tasks"
                f" (not migrated, excluded from the counts above): {fmt(excluded_n)}",
            )
            lines.append("")

        # ── Validation results ──────────────────────────────────────────────
        # Shield counts by created_at; Engine counts by created_at + org_id.
        rr_where, rr_params = window_clause(from_dt, to_dt, column="rr.created_at")
        rule_result_tables = (
            (
                "prompt_rule_results",
                "prompt_rule_results rr "
                "JOIN inference_prompts p ON rr.inference_prompt_id = p.id "
                "JOIN inferences i ON p.inference_id = i.id",
            ),
            (
                "response_rule_results",
                "response_rule_results rr "
                "JOIN inference_responses r ON rr.inference_response_id = r.id "
                "JOIN inferences i ON r.inference_id = i.id",
            ),
        )
        if verify_inferences:
            lines.append("Validation (rule) results")
            for table, shield_from_sql in rule_result_tables:
                s_where, s_params = task_clause(
                    and_clause(rr_where, kept),
                    rr_params,
                    task_ids,
                )
                s = count(sc, f"{shield_from_sql} {archived_join}", s_where, s_params)
                if task_ids or migrated_task_ids or taskless_inference_ids:
                    e_where, e_params = task_clause(
                        and_clause(rr_where, "rr.org_id = :org_id"),
                        {**rr_params, "org_id": org_id},
                        task_ids,
                    )
                    e_where, e_params = migrated_scope_clause(
                        e_where,
                        e_params,
                        migrated_task_ids,
                        taskless_inference_ids,
                    )
                    e = count(ec, shield_from_sql, e_where, e_params)
                else:
                    e = count(ec, table, org_where, org_params)
                all_match = all_match and s == e
                lines.append(compare(table, s, e))
            lines.append("")

        # ── Rule result details ─────────────────────────────────────────────
        # Each detail hangs off either a prompt or a response rule result, so
        # every table is counted once per path and the two counts are summed.
        prompt_tail = (
            "JOIN prompt_rule_results rr ON d.prompt_rule_result_id = rr.id "
            "JOIN inference_prompts p ON rr.inference_prompt_id = p.id "
            "JOIN inferences i ON p.inference_id = i.id"
        )
        response_tail = (
            "JOIN response_rule_results rr ON d.response_rule_result_id = rr.id "
            "JOIN inference_responses r ON rr.inference_response_id = r.id "
            "JOIN inferences i ON r.inference_id = i.id"
        )
        detail_child_join = (
            "JOIN rule_result_details d ON c.rule_result_detail_id = d.id"
        )
        detail_tables = (
            ("rule_result_details", "rule_result_details d", "d"),
            (
                "hallucination_claims",
                f"hallucination_claims c {detail_child_join}",
                "c",
            ),
            ("pii_entities", f"pii_entities c {detail_child_join}", "c"),
            ("keyword_matches", f"keyword_matches c {detail_child_join}", "c"),
            ("regex_matches", f"regex_matches c {detail_child_join}", "c"),
            ("toxicity_scores", f"toxicity_scores c {detail_child_join}", "c"),
        )
        scoped = bool(task_ids or migrated_task_ids or taskless_inference_ids)
        if verify_inferences:
            lines.append("Rule result details")
            for label, base_sql, org_alias in detail_tables:
                s_total = 0
                e_total = 0
                for tail in (prompt_tail, response_tail):
                    from_sql = f"{base_sql} {tail}"
                    s_where, s_params = task_clause(
                        and_clause(rr_where, kept),
                        rr_params,
                        task_ids,
                    )
                    s_total += count(
                        sc,
                        f"{from_sql} {archived_join}",
                        s_where,
                        s_params,
                    )
                    if scoped:
                        e_where, e_params = task_clause(
                            and_clause(rr_where, f"{org_alias}.org_id = :org_id"),
                            {**rr_params, "org_id": org_id},
                            task_ids,
                        )
                        e_where, e_params = migrated_scope_clause(
                            e_where,
                            e_params,
                            migrated_task_ids,
                            taskless_inference_ids,
                        )
                        e_total += count(ec, from_sql, e_where, e_params)
                if not scoped:
                    e_total = count(ec, label, org_where, org_params)
                all_match = all_match and s_total == e_total
                lines.append(compare(label, s_total, e_total))
            lines.append("")

        # ── Feedback ────────────────────────────────────────────────────────
        if "feedback" in phases_completed:
            lines.append("Feedback")
            fb_clauses = []
            for column in ("f.created_at", "i.created_at"):
                if from_dt:
                    fb_clauses.append(f"{column} >= :from_dt")
                if to_dt:
                    fb_clauses.append(f"{column} < :to_dt")
            fb_where = ("WHERE " + " AND ".join(fb_clauses)) if fb_clauses else ""
            s_where, s_params = task_clause(
                and_clause(fb_where, kept),
                inf_params,
                task_ids,
            )
            s = count(
                sc,
                "inference_feedback f "
                f"JOIN inferences i ON f.inference_id = i.id {archived_join}",
                s_where,
                s_params,
            )
            if task_ids or migrated_task_ids or taskless_inference_ids:
                fb_engine_where, fb_engine_params = window_clause(
                    from_dt,
                    to_dt,
                    column="f.created_at",
                )
                e_where, e_params = task_clause(
                    and_clause(fb_engine_where, "f.org_id = :org_id"),
                    {**fb_engine_params, "org_id": org_id},
                    task_ids,
                )
                e_where, e_params = migrated_scope_clause(
                    e_where,
                    e_params,
                    migrated_task_ids,
                    taskless_inference_ids,
                )
                e = count(
                    ec,
                    "inference_feedback f JOIN inferences i ON f.inference_id = i.id",
                    e_where,
                    e_params,
                )
            else:
                e = count(ec, "inference_feedback", org_where, org_params)
            all_match = all_match and s == e
            lines.append(compare("inference_feedback", s, e))
            lines.append("")

        # ── Engine org_id sanity check ──────────────────────────────────────
        # No org-scoped row should have been inserted without an org_id.
        lines.append(
            "Rows for org-scoped resources missing an org_id (each should be 0)",
        )
        for table in (
            "prompt_rule_results",
            "response_rule_results",
            "rule_result_details",
            "hallucination_claims",
            "pii_entities",
            "keyword_matches",
            "regex_matches",
            "toxicity_scores",
            "inference_feedback",
        ):
            null_n = count(ec, table, "WHERE org_id IS NULL")
            status = "✓" if null_n == 0 else "✗"
            all_match = all_match and null_n == 0
            lines.append(f"  {status:<10} {table:<28} missing org ids: {fmt(null_n)}")
        lines.append("")

    lines.append("=" * 70)
    lines.append("  RESULT: " + ("ALL MATCH ✓" if all_match else "MISMATCHES FOUND ✗"))
    lines.append("=" * 70)

    print("\n".join(lines))
    return all_match


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--save-file",
        required=True,
        metavar="STATE_FILE",
        help="Path to the migration_state_*.json checkpoint of the run to verify",
    )
    parser.add_argument(
        "--api-mode",
        action="store_true",
        help="Verify through the Shield and Engine APIs instead of direct SQL",
    )
    args = parser.parse_args()

    with open(args.save_file) as f:
        state = json.load(f)

    stored_from = state.get("from_dt")
    stored_to = state.get("to_dt")
    from_dt = datetime.fromisoformat(stored_from) if stored_from else None
    to_dt = datetime.fromisoformat(stored_to) if stored_to else None
    task_ids = sorted(state.get("task_ids") or []) or None
    migrated_task_ids = state.get("migrated_task_ids") or None
    taskless_inference_ids = state.get("migrated_taskless_inference_ids") or None
    migrated_rule_ids = state.get("migrated_rule_ids") or None
    phases_completed = state.get("phases_completed") or []

    if args.api_mode:
        ok = verify_api(
            from_dt,
            to_dt,
            task_ids,
            migrated_task_ids,
            taskless_inference_ids,
            migrated_rule_ids,
            phases_completed,
        )
        raise SystemExit(0 if ok else 1)

    org_id = os.environ["ENGINE_ORG_ID"]
    shield = get_engine("SHIELD")
    engine = get_engine("ENGINE")

    ok = verify(
        shield,
        engine,
        from_dt,
        to_dt,
        org_id,
        task_ids,
        migrated_task_ids,
        taskless_inference_ids,
        migrated_rule_ids,
        phases_completed,
    )
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
