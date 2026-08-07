# pre_migration_scope.py
"""
Queries the Shield PostgreSQL database directly and prints a stats report on the
number of tasks, rules, inferences, etc. that can be migrated to the Engine.
No data is written.

Shield DB connection:
    SHIELD_POSTGRES_USER
    SHIELD_POSTGRES_PASSWORD
    SHIELD_POSTGRES_URL
    SHIELD_POSTGRES_PORT
    SHIELD_POSTGRES_DB
    SHIELD_POSTGRES_USE_SSL         (optional, "true"/"false", default false)
    SHIELD_POSTGRES_SSL_ROOT_CERT   (optional, path to CA cert when SSL on)

A date window is required: pass --last-days, or --from-date (with an optional
--to-date, which defaults to now).

You may optionally pass --output-dir/-o to also write the results to a file
in that directory.

Pass --estimate to use the query planner's row estimates instead of exact
COUNT(*) queries. This is a cheap alternative to full scans, but numbers
are approximate and the per-task breakdown is skipped.

Usage:
    python pre_migration_scope.py --from-date 2025-01-01 --to-date 2026-01-01
    python pre_migration_scope.py --last-days 180
    python pre_migration_scope.py --last-days 180 --output-dir ./reports
    python pre_migration_scope.py --last-days 180 --estimate
"""

import argparse
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from progress import Heartbeat
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

load_dotenv()

# The report is buffered and printed at the end; progress is written as it
# happens. Line buffering keeps the two ordered under `| tee`.
sys.stdout.reconfigure(line_buffering=True)


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


def estimate_count(conn, sql, params):
    """
    Return the planner's estimated row count for a SELECT via EXPLAIN.
    """
    with Heartbeat(f"estimating {table_label(sql)}"):
        plan = conn.execute(
            text(f"EXPLAIN (FORMAT JSON) {sql}"),
            params,
        ).scalar_one()
    # psycopg2 returns the JSON already parsed into Python objects.
    return int(plan[0]["Plan"]["Plan Rows"])


def section(title):
    """Announce a section live. The report itself is buffered and not printed
    until every count has finished, so without this the script is silent for
    its whole run."""
    print(f"\n{title}")


def table_label(sql):
    """Name the table a FROM fragment or SELECT counts, for the progress line."""
    tokens = sql.split()
    if "FROM" in tokens:
        return tokens[tokens.index("FROM") + 1]
    return tokens[0] if tokens else "rows"


def and_clause(where, condition):
    """Append a condition to an existing WHERE fragment (or start one)."""
    return where + (" AND " if where else "WHERE ") + condition


def windowed_count(conn, table, where, params, estimate):
    """
    Returns either an exact count for the table, or the planner
    estimate if estimate == True.
    """
    if estimate:
        return estimate_count(conn, f"SELECT * FROM {table} {where}", params)

    # Exact counts join across billion-row tables and take minutes each — this
    # is the reason --estimate exists, and the reason they need a heartbeat.
    with Heartbeat(f"counting {table_label(table)}"):
        return conn.execute(
            text(f"SELECT COUNT(*) FROM {table} {where}"),
            params,
        ).scalar_one()


def format_count(value, estimate):
    """Format a count, prefixing '~' for estimates."""
    return ("~" + fmt(value)) if estimate else fmt(value)


def scope_report(engine: Engine, from_dt, to_dt, output_dir=None, estimate=False):
    lines = []

    window_label = "all time"
    if from_dt:
        window_label = f"from {from_dt.date()}"
    if to_dt:
        window_label += f" to {to_dt.date()}"

    lines.append(f"\n{'='*60}")
    lines.append(f"  Shield → Engine Migration Scope Report")
    lines.append(f"  Window: {window_label}")

    if estimate:
        lines.append(f"  Mode:   estimate")

    lines.append(f"{'='*60}\n")

    with engine.connect() as conn:
        # ── Config ──────────────────────────────────────────────────────────
        section("Config")
        with Heartbeat("counting tasks, rules and links"):
            task_count = conn.execute(
                text("SELECT COUNT(*) FROM tasks WHERE NOT archived"),
            ).scalar_one()
            archived_task_count = conn.execute(
                text("SELECT COUNT(*) FROM tasks WHERE archived"),
            ).scalar_one()
            rule_count = conn.execute(
                text("SELECT COUNT(*) FROM rules WHERE scope = 'task'"),
            ).scalar_one()
            n_default = conn.execute(
                text("SELECT COUNT(*) FROM rules WHERE scope = 'default'"),
            ).scalar_one()
            n_links = conn.execute(
                text("SELECT COUNT(*) FROM tasks_to_rules"),
            ).scalar_one()

        lines.append("Config (always migrated in full, date window does not apply)")
        lines.append(f"  Tasks                          {fmt(task_count)}")
        lines.append(
            f"  Archived tasks (not migrated)  {fmt(archived_task_count)}",
        )
        lines.append(f"  Task-scoped rules              {fmt(rule_count)}")
        lines.append(f"  Default rules                  {fmt(n_default)}")
        lines.append(f"  Task–rule links                {fmt(n_links)}")
        lines.append("")

        # ── Inferences ──────────────────────────────────────────────────────
        section("Inferences")
        inf_where, inf_params = window_clause(from_dt, to_dt)

        # Exclude inferences referencing archived tasks from being migrated
        archived_join = "LEFT JOIN tasks t ON i.task_id = t.id"
        kept = "(i.task_id IS NULL OR COALESCE(t.archived, TRUE) = FALSE)"
        excluded = "(i.task_id IS NOT NULL AND COALESCE(t.archived, TRUE) = TRUE)"

        excluded_infs = 0
        if estimate:
            total_infs = windowed_count(
                conn,
                "inferences",
                inf_where,
                inf_params,
                estimate,
            )
        else:
            parent_where, parent_params = window_clause(
                from_dt,
                to_dt,
                column="i.created_at",
            )
            total_infs = windowed_count(
                conn,
                f"inferences i {archived_join}",
                and_clause(parent_where, kept),
                parent_params,
                estimate,
            )
            excluded_infs = windowed_count(
                conn,
                f"inferences i {archived_join}",
                and_clause(parent_where, excluded),
                parent_params,
                estimate,
            )

        # The per-task GROUP BY needs a full scan the planner can't estimate
        # per-group, so skip if in estimate mode.
        task_counts = {}
        tasks_with_data = 0
        if not estimate:
            inf_join_where = inf_where.replace("created_at", "i.created_at")
            # The slowest query in the script: a full scan the planner cannot
            # estimate per-group.
            with Heartbeat("per-task inference counts (full scan)"):
                task_count_rows = conn.execute(
                    text(f"""
                    SELECT i.task_id AS task_id,
                           COALESCE(t.name, '(no task)') AS name,
                           COUNT(*) AS n
                    FROM inferences i
                    LEFT JOIN tasks t ON t.id = i.task_id
                    {and_clause(inf_join_where, kept)}
                    GROUP BY i.task_id, t.name
                    """),
                    inf_params,
                ).mappings()
                rows = list(task_count_rows)
            task_counts = {row["name"]: row["n"] for row in rows}
            tasks_with_data = sum(1 for row in rows if row["task_id"] is not None)

        lines.append("Inferences")
        lines.append(
            f"  Total Inferences               {format_count(total_infs, estimate)}",
        )

        if not estimate:
            lines.append(
                f"  Tasks with data                {fmt(tasks_with_data)} / {fmt(task_count)}",
            )
            lines.append(
                f"  Referencing archived tasks    "
                f" {fmt(excluded_infs)} (will not be migrated, excluded above)",
            )

        lines.append("")

        # ── Validation results ──────────────────────────────────────────────
        section("Validation (rule) results")
        if estimate:
            prr_where, prr_params = window_clause(from_dt, to_dt)
            n_prompt_rr = windowed_count(
                conn,
                "prompt_rule_results",
                prr_where,
                prr_params,
                estimate,
            )
            n_response_rr = windowed_count(
                conn,
                "response_rule_results",
                prr_where,
                prr_params,
                estimate,
            )
        else:
            rr_where, rr_params = window_clause(from_dt, to_dt, column="rr.created_at")
            n_prompt_rr = windowed_count(
                conn,
                "prompt_rule_results rr "
                "JOIN inference_prompts p ON rr.inference_prompt_id = p.id "
                f"JOIN inferences i ON p.inference_id = i.id {archived_join}",
                and_clause(rr_where, kept),
                rr_params,
                estimate,
            )
            n_response_rr = windowed_count(
                conn,
                "response_rule_results rr "
                "JOIN inference_responses r ON rr.inference_response_id = r.id "
                f"JOIN inferences i ON r.inference_id = i.id {archived_join}",
                and_clause(rr_where, kept),
                rr_params,
                estimate,
            )

        lines.append("Validation (rule) results")
        lines.append(
            f"  Validate Prompt Results        {format_count(n_prompt_rr, estimate)}",
        )
        lines.append(
            f"  Validate Response Results      {format_count(n_response_rr, estimate)}",
        )
        lines.append(
            f"  Total Validation Results       {format_count(n_prompt_rr + n_response_rr, estimate)}",
        )
        lines.append("")

        # ── Feedback ────────────────────────────────────────────────────────
        section("Feedback")
        if estimate:
            fb_where, fb_params = window_clause(from_dt, to_dt)
            n_fb = windowed_count(
                conn,
                "inference_feedback",
                fb_where,
                fb_params,
                estimate,
            )
        else:
            fb_where, fb_params = window_clause(from_dt, to_dt, column="f.created_at")
            n_fb = windowed_count(
                conn,
                "inference_feedback f "
                f"JOIN inferences i ON f.inference_id = i.id {archived_join}",
                and_clause(fb_where, kept),
                fb_params,
                estimate,
            )
        lines.append("Feedback")
        lines.append(f"  Total Feedback                 {format_count(n_fb, estimate)}")
        lines.append("")

        # ── Per-task breakdown ──────────────────────────────────────────────
        # Skip if in estimate mode.
        if not estimate:
            lines.append("Per-task inference counts")
            for name, count in sorted(task_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  {name:<30} {fmt(count)}")
            lines.append("")

    report = "\n".join(lines)
    print(report)
    if output_dir is not None:
        from_label = from_dt.date().isoformat()
        to_label = to_dt.date().isoformat() if to_dt else "now"
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(
            output_dir,
            f"shield_migration_scope_{from_label}_to_{to_label}.txt",
        )
        with open(output_path, "w") as f:
            f.write(report + "\n")
        print(f"Report written to {output_path}")


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
    parser.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Directory to write the report file to.",
    )
    parser.add_argument(
        "--estimate",
        action="store_true",
        help="Use planner row estimates instead of exact counts. "
        "Cheap on huge tables, but skips the per-task breakdown.",
    )
    args = parser.parse_args()

    # Require a window start:
    # --last-days, or --from-date (--to-date optional, defaults to now).
    if args.last_days is None and args.from_date is None:
        parser.error(
            "specify a date window: --last-days N, or --from-date "
            "(with optional --to-date)",
        )

    from_dt, to_dt = parse_window(args)
    engine = get_shield_engine()
    scope_report(
        engine,
        from_dt,
        to_dt,
        output_dir=args.output_dir,
        estimate=args.estimate,
    )


if __name__ == "__main__":
    main()
