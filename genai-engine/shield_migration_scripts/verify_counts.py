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
import urllib.parse
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Engine

load_dotenv()


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


def verify(
    shield: Engine,
    engine: Engine,
    from_dt,
    to_dt,
    org_id,
    task_ids=None,
    migrated_task_ids=None,
    taskless_inference_ids=None,
):
    lines = []

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
        # ── Inferences ──────────────────────────────────────────────────────
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
        archived_tasks_n = count(sc, "tasks", "WHERE archived")
        lines.append(
            f"\nShield tasks archived (not migrated): {fmt(archived_tasks_n)}",
        )
        lines.append(
            f"Shield inferences referencing archived tasks"
            f" (not migrated, excluded from the counts above): {fmt(excluded_n)}",
        )
        lines.append("")

        # ── Validation results ──────────────────────────────────────────────
        # Shield counts by created_at; Engine counts by created_at + org_id.
        lines.append("Validation (rule) results")
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

        # ── Feedback ────────────────────────────────────────────────────────
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
    )
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
