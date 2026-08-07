"""Tests for shield_migration_scripts/verify_counts.py.

count() is the function every one of the ~40 call sites funnels through, and the
one this change edited, so it gets the most attention here.
"""

import progress
import pytest
import verify_counts as vc


@pytest.fixture(autouse=True)
def quiet_progress(monkeypatch):
    monkeypatch.setattr(progress, "PROGRESS_INTERVAL", 0)


# ── count() ───────────────────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_count_returns_the_scalar_and_builds_the_expected_sql(stub_conn):
    stub_conn.results = [42]
    assert (
        vc.count(stub_conn, "inferences i", "WHERE i.task_id = :t", {"t": "t1"}) == 42
    )

    sql, params = stub_conn.executed[0]
    assert sql == "SELECT COUNT(*) FROM inferences i WHERE i.task_id = :t"
    assert params == {"t": "t1"}


@pytest.mark.unit_tests
def test_count_defaults_params_to_empty(stub_conn):
    stub_conn.results = [7]
    assert vc.count(stub_conn, "tasks") == 7
    assert stub_conn.executed[0][1] == {}


@pytest.mark.unit_tests
def test_count_expands_list_params_without_any(stub_conn):
    """IN :ids needs an expanding bindparam; ANY(:ids) does not. Regression
    guard on the branch inside the edited function."""
    stub_conn.results = [3]
    vc.count(stub_conn, "tasks", "WHERE id IN :ids", {"ids": ["a", "b"]})

    # An expanded bindparam renders as a POSTCOMPILE placeholder; a plain one
    # would still read ":ids" here and fail against Postgres.
    assert "POSTCOMPILE_ids" in stub_conn.executed[0][0]


@pytest.mark.unit_tests
def test_count_does_not_expand_when_any_is_used(stub_conn):
    stub_conn.results = [3]
    vc.count(stub_conn, "tasks", "WHERE id = ANY(:ids)", {"ids": ["a", "b"]})

    stmt = stub_conn.executed[0][0]
    assert "ANY(:ids)" in stmt
    assert "POSTCOMPILE" not in stmt


@pytest.mark.unit_tests
def test_count_propagates_errors(stub_conn):
    def boom(stmt, params=None):
        raise RuntimeError("connection lost")

    stub_conn.execute = boom
    with pytest.raises(RuntimeError, match="connection lost"):
        vc.count(stub_conn, "inferences i")


# ── Progress labelling ────────────────────────────────────────────────────────


@pytest.mark.unit_tests
@pytest.mark.parametrize(
    "table,expected",
    [
        ("inferences i", "inferences"),
        ("tasks", "tasks"),
        (
            "inference_prompts c JOIN inferences i ON c.inference_id = i.id",
            "inference_prompts",
        ),
        (
            "inference_prompt_contents c "
            "JOIN inference_prompts p ON c.inference_prompt_id = p.id "
            "JOIN inferences i ON p.inference_id = i.id",
            "inference_prompt_contents",
        ),
        ("rule_result_details d", "rule_result_details"),
        (
            "hallucination_claims c JOIN rule_result_details d "
            "ON c.rule_result_detail_id = d.id",
            "hallucination_claims",
        ),
        (
            "inference_feedback f JOIN inferences i ON f.inference_id = i.id",
            "inference_feedback",
        ),
    ],
)
def test_count_label_names_the_counted_table_and_database(stub_conn, table, expected):
    label = vc.count_label(stub_conn, table)
    assert label == f"counting {expected} (arthur_shield)"


@pytest.mark.unit_tests
def test_count_label_survives_a_connection_without_an_engine_url():
    class Bare:
        pass

    assert vc.count_label(Bare(), "inferences i") == "counting inferences"


@pytest.mark.unit_tests
def test_count_emits_a_heartbeat(stub_conn, monkeypatch, capsys):
    monkeypatch.setattr(progress, "PROGRESS_INTERVAL", 2.0)
    stub_conn.results = [5]
    vc.count(stub_conn, "inference_prompts c JOIN inferences i ON c.id = i.id")

    out = capsys.readouterr().out
    assert "counting inference_prompts (arthur_shield)" in out


# ── Section headers ───────────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_section_records_and_announces(capsys):
    lines = []
    vc.section(lines, "Inferences")

    # Recorded for the buffered report...
    assert lines == ["Inferences"]
    # ...and announced live, since the report only prints once everything ends.
    assert "Inferences" in capsys.readouterr().out


# ── compare() ─────────────────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_compare_marks_matches_and_mismatches():
    assert "✓" in vc.compare("inferences", 10, 10)
    assert "MISMATCH" in vc.compare("inferences", 10, 9)


# ── verify() ──────────────────────────────────────────────────────────────────


class StubEngine:
    """An Engine whose connect() yields a canned connection."""

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


@pytest.mark.unit_tests
def test_verify_reports_all_match_when_both_sides_agree(stub_conn, capsys):
    """Progress must not disturb the buffered report or the exit status."""
    stub_conn.default = 0  # every count agrees at zero
    engine = StubEngine(stub_conn)

    ok = vc.verify(
        engine,
        engine,
        None,
        None,
        "org-1",
        phases_completed=["inferences", "feedback"],
    )

    out = capsys.readouterr().out
    assert ok is True
    assert "RESULT: ALL MATCH ✓" in out
    assert "Inferences" in out
    assert "Feedback" in out


@pytest.mark.unit_tests
def test_verify_reports_a_mismatch(monkeypatch, stub_conn, capsys):
    engine = StubEngine(stub_conn)
    counts = iter([5, 4] + [0] * 200)  # shield=5, engine=4 on the first pair
    monkeypatch.setattr(vc, "count", lambda *a, **k: next(counts))

    ok = vc.verify(engine, engine, None, None, "org-1", phases_completed=["inferences"])

    assert ok is False
    assert "MISMATCHES FOUND ✗" in capsys.readouterr().out


@pytest.mark.unit_tests
def test_verify_flags_rows_missing_an_org_id(monkeypatch, stub_conn, capsys):
    engine = StubEngine(stub_conn)
    monkeypatch.setattr(vc, "count", lambda *a, **k: 3)  # every count non-zero

    ok = vc.verify(engine, engine, None, None, "org-1", phases_completed=[])

    assert ok is False
    assert "missing org ids: 3" in capsys.readouterr().out
