"""Tests for shield_migration_scripts/pre_migration_scope.py."""

import pre_migration_scope as scope
import progress
import pytest


@pytest.fixture(autouse=True)
def quiet_progress(monkeypatch):
    monkeypatch.setattr(progress, "PROGRESS_INTERVAL", 0)


# ── Counting helpers ──────────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_windowed_count_returns_an_exact_count(stub_conn):
    stub_conn.results = [1234]
    n = scope.windowed_count(
        stub_conn, "inferences", "WHERE created_at > :d", {}, False
    )

    assert n == 1234
    assert stub_conn.executed[0][0].startswith("SELECT COUNT(*) FROM inferences")


@pytest.mark.unit_tests
def test_windowed_count_routes_estimates_through_explain(stub_conn):
    stub_conn.results = [[{"Plan": {"Plan Rows": 987}}]]
    n = scope.windowed_count(stub_conn, "inferences", "", {}, True)

    assert n == 987
    assert stub_conn.executed[0][0].startswith("EXPLAIN (FORMAT JSON) SELECT *")


@pytest.mark.unit_tests
@pytest.mark.parametrize(
    "sql,expected",
    [
        ("inferences", "inferences"),
        ("inferences i LEFT JOIN tasks t ON i.task_id = t.id", "inferences"),
        ("SELECT * FROM inference_feedback f WHERE x", "inference_feedback"),
        (
            "prompt_rule_results rr JOIN inference_prompts p ON rr.id = p.id",
            "prompt_rule_results",
        ),
        ("", "rows"),
    ],
)
def test_table_label(sql, expected):
    assert scope.table_label(sql) == expected


@pytest.mark.unit_tests
def test_windowed_count_emits_a_heartbeat(stub_conn, monkeypatch, capsys):
    monkeypatch.setattr(progress, "PROGRESS_INTERVAL", 2.0)
    stub_conn.results = [1]
    scope.windowed_count(
        stub_conn, "inference_feedback f JOIN inferences i", "", {}, False
    )

    assert "counting inference_feedback" in capsys.readouterr().out


@pytest.mark.unit_tests
def test_format_count_marks_estimates():
    assert scope.format_count(1234, False) == "1,234"
    assert scope.format_count(1234, True) == "~1,234"


# ── scope_report ──────────────────────────────────────────────────────────────


class ScopeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value

    def mappings(self):
        return self._value


class ScopeConn:
    """A connection shaped for scope_report's three query kinds: scalar counts,
    an EXPLAIN plan, and the per-task GROUP BY rows."""

    def __init__(self, counts=(), task_rows=(), plan_rows=100):
        self.counts = list(counts)
        self.task_rows = list(task_rows)
        self.plan_rows = plan_rows
        self.executed = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append(sql)
        if sql.lstrip().startswith("EXPLAIN"):
            return ScopeResult([{"Plan": {"Plan Rows": self.plan_rows}}])
        if "GROUP BY" in sql:
            return ScopeResult(self.task_rows)
        return ScopeResult(self.counts.pop(0) if self.counts else 0)


class StubEngine:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        conn = self._conn

        class Ctx:
            def __enter__(self):
                return conn

            def __exit__(self, *exc):
                return False

        return Ctx()


TASK_ROWS = [
    {"task_id": "t1", "name": "Alpha", "n": 90},
    {"task_id": "t2", "name": "Beta", "n": 10},
    {"task_id": None, "name": "(no task)", "n": 5},
]


@pytest.mark.unit_tests
def test_scope_report_renders_every_section(capsys):
    # tasks, archived tasks, task-scoped rules, default rules, links
    conn = ScopeConn(counts=[3, 1, 4, 2, 5], task_rows=TASK_ROWS)

    scope.scope_report(StubEngine(conn), None, None)

    out = capsys.readouterr().out
    assert "Config (always migrated in full" in out
    assert "Tasks                          3" in out
    assert "Archived tasks (not migrated)  1" in out
    assert "Task–rule links                5" in out
    assert "Inferences" in out
    assert "Validation (rule) results" in out
    assert "Feedback" in out
    # Only the two rows with a real task_id count as tasks with data.
    assert "Tasks with data                2 / 3" in out
    assert "Per-task inference counts" in out
    assert "Alpha                          90" in out


@pytest.mark.unit_tests
def test_scope_report_writes_a_file_free_of_progress_output(tmp_path, capsys):
    """The report file is built from the buffered lines, so live progress must
    never leak into it."""
    from datetime import datetime

    conn = ScopeConn(counts=[3, 1, 4, 2, 5], task_rows=TASK_ROWS)
    scope.scope_report(
        StubEngine(conn),
        datetime(2025, 1, 1),
        datetime(2026, 1, 1),
        output_dir=str(tmp_path),
    )
    capsys.readouterr()

    written = list(tmp_path.iterdir())
    assert len(written) == 1
    report = written[0].read_text()

    assert "\r" not in report
    assert "\x1b" not in report
    assert "counting" not in report
    assert "Shield → Engine Migration Scope Report" in report
    assert "Total Inferences" in report


@pytest.mark.unit_tests
def test_scope_report_estimate_mode_skips_the_per_task_scan(capsys):
    conn = ScopeConn(counts=[3, 1, 4, 2, 5], plan_rows=100)

    scope.scope_report(StubEngine(conn), None, None, estimate=True)

    out = capsys.readouterr().out
    assert "Mode:   estimate" in out
    assert "~100" in out
    # The GROUP BY full scan is skipped entirely in estimate mode.
    assert "Per-task inference counts" not in out
    assert not any("GROUP BY" in sql for sql in conn.executed)
