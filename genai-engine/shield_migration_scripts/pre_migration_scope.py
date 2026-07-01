# pre_migration_scope.py
"""
Queries the Shield PostgreSQL database directly and prints a stats report on the
number of tasks, rules, inferences, etc. that can be migrated to the Engine.
No data is written.

Reads over SQL rather than the Shield API so counts stay fast for customers with
~1B inferences (the API's OFFSET pagination does not hold up at that scale).

Shield DB connection:
    SHIELD_POSTGRES_USER
    SHIELD_POSTGRES_PASSWORD
    SHIELD_POSTGRES_URL
    SHIELD_POSTGRES_PORT
    SHIELD_POSTGRES_DB
    SHIELD_POSTGRES_USE_SSL         (optional, "true"/"false", default false)
    SHIELD_POSTGRES_SSL_ROOT_CERT   (optional, path to CA cert when SSL on)

A date window is required: pass either --last-days, or both --from-date and
--to-date. Scoping all of time is not allowed.

Usage:
    python pre_migration_scope.py --from-date 2020-01-01 --to-date 2021-01-01
    python pre_migration_scope.py --last-days 180
"""

import argparse
import os
import urllib.parse
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


def get_shield_engine() -> Engine:
    """Build a SQLAlchemy engine for the Shield database from env vars."""
    user = os.environ["SHIELD_POSTGRES_USER"]
    password = os.environ["SHIELD_POSTGRES_PASSWORD"]
    host = os.environ["SHIELD_POSTGRES_URL"]
    port = os.environ["SHIELD_POSTGRES_PORT"]
    db_name = os.environ["SHIELD_POSTGRES_DB"]

    params = {}
    if os.getenv("SHIELD_POSTGRES_USE_SSL", "false").lower() == "true":
        params["sslmode"] = "verify-full"
        ssl_root_cert = os.getenv("SHIELD_POSTGRES_SSL_ROOT_CERT")
        if ssl_root_cert:
            params["sslrootcert"] = ssl_root_cert

    query = urllib.parse.urlencode(params)
    conn_str = (
        f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db_name}?" + query
    )
    return create_engine(conn_str, pool_pre_ping=True)


def parse_window(args):
    now = datetime.now(timezone.utc)
    if args.last_days:
        return now - timedelta(days=args.last_days), now
    from_dt = datetime.fromisoformat(args.from_date) if args.from_date else None
    to_dt = datetime.fromisoformat(args.to_date) if args.to_date else None
    return from_dt, to_dt


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


def scope_report(engine: Engine, from_dt, to_dt):
    window_label = "all time"
    if from_dt:
        window_label = f"from {from_dt.date()}"
    if to_dt:
        window_label += f" to {to_dt.date()}"

    print(f"\n{'='*60}")
    print(f"  Shield → Engine Migration Scope Report")
    print(f"  Window: {window_label}")
    print(f"{'='*60}\n")

    with engine.connect() as conn:
        # ── Config ──────────────────────────────────────────────────────────
        task_count = conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar_one()
        rule_count = conn.execute(
            text("SELECT COUNT(*) FROM rules WHERE scope = 'task'"),
        ).scalar_one()
        n_default = conn.execute(
            text("SELECT COUNT(*) FROM rules WHERE scope = 'default'"),
        ).scalar_one()
        n_links = conn.execute(text("SELECT COUNT(*) FROM tasks_to_rules")).scalar_one()

        print("Config (always migrated in full, date window does not apply)")
        print(f"  Tasks                          {fmt(task_count)}")
        print(f"  Task-scoped rules              {fmt(rule_count)}")
        print(f"  Default rules                  {fmt(n_default)}")
        print(f"  Task–rule links                {fmt(n_links)}")
        print()

        # ── Inferences ──────────────────────────────────────────────────────
        inf_where, inf_params = window_clause(from_dt, to_dt)
        total_infs = conn.execute(
            text(f"SELECT COUNT(*) FROM inferences {inf_where}"),
            inf_params,
        ).scalar_one()

        # Per-task counts in a single grouped query (NULL task_id => 'untasked').
        # Qualify the window column as i.created_at: both inferences and tasks
        # have a created_at, so a bare reference is ambiguous in this join.
        inf_join_where = inf_where.replace("created_at", "i.created_at")
        task_count_rows = conn.execute(
            text(f"""
                SELECT i.task_id AS task_id,
                       COALESCE(t.name, '(no task)') AS name,
                       COUNT(*) AS n
                FROM inferences i
                LEFT JOIN tasks t ON t.id = i.task_id
                {inf_join_where}
                GROUP BY i.task_id, t.name
                """),
            inf_params,
        ).mappings()
        rows = list(task_count_rows)
        task_counts = {row["name"]: row["n"] for row in rows}
        # Task-less inferences (NULL task_id, shown as '(no task)') are still
        # migratable, but they are not a task — don't count them here.
        tasks_with_data = sum(1 for row in rows if row["task_id"] is not None)

        print("Inferences")
        print(f"  Total Inferences               {fmt(total_infs)}")
        print(
            f"  Tasks with data                {fmt(tasks_with_data)} / {fmt(task_count)}",
        )
        print()

        # ── Validation results ──────────────────────────────────────────────
        # One rule result per rule run against a prompt / response. These carry
        # their own created_at, so the window filters them directly.
        prr_where, prr_params = window_clause(from_dt, to_dt)
        n_prompt_rr = conn.execute(
            text(f"SELECT COUNT(*) FROM prompt_rule_results {prr_where}"),
            prr_params,
        ).scalar_one()
        n_response_rr = conn.execute(
            text(f"SELECT COUNT(*) FROM response_rule_results {prr_where}"),
            prr_params,
        ).scalar_one()

        print("Validation (rule) results")
        print(f"  Validate Prompt Results        {fmt(n_prompt_rr)}")
        print(f"  Validate Response Results      {fmt(n_response_rr)}")
        print(f"  Total Validation Results       {fmt(n_prompt_rr + n_response_rr)}")
        print()

        # ── Feedback ────────────────────────────────────────────────────────
        fb_where, fb_params = window_clause(from_dt, to_dt)
        n_fb = conn.execute(
            text(f"SELECT COUNT(*) FROM inference_feedback {fb_where}"),
            fb_params,
        ).scalar_one()
        print(f"Feedback")
        print(f"  Total Feedback                 {fmt(n_fb)}")
        print()

        # ── Per-task breakdown ──────────────────────────────────────────────
        print("Per-task inference counts")
        for name, count in sorted(task_counts.items(), key=lambda x: -x[1]):
            print(f"  {name:<30} {fmt(count)}")
        print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--from-date",
        default=None,
        help="Start date (inclusive), e.g. 2020-01-01",
    )
    parser.add_argument(
        "--to-date",
        default=None,
        help="End date (exclusive), e.g. 2021-01-01",
    )
    parser.add_argument(
        "--last-days",
        type=int,
        default=None,
        help="Shorthand: scope the last N days",
    )
    args = parser.parse_args()

    # Require an explicit window: either --last-days, or both --from-date and
    # --to-date. Scoping all of time is not allowed.
    if args.last_days is None and not (args.from_date and args.to_date):
        parser.error(
            "specify a date window: either --last-days N, "
            "or both --from-date and --to-date",
        )

    from_dt, to_dt = parse_window(args)
    engine = get_shield_engine()
    scope_report(engine, from_dt, to_dt)


if __name__ == "__main__":
    main()
