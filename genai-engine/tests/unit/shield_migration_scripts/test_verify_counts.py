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


# ── id_lines() / link_lines() ─────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_id_lines_names_entries_when_the_name_is_known():
    lines = vc.id_lines(["a", "b"], {"a": "Support Copilot"})

    assert lines == [f"{vc.ROW}a (Support Copilot)", f"{vc.ROW}b"]


@pytest.mark.unit_tests
def test_id_lines_truncates_past_the_limit():
    lines = vc.id_lines([str(n) for n in range(10)], {}, limit=3)

    assert len(lines) == 4
    assert lines[-1].strip().startswith("… 7 more")


@pytest.mark.unit_tests
def test_id_lines_lists_everything_when_unlimited():
    lines = vc.id_lines([str(n) for n in range(10)], {})

    assert len(lines) == 10
    assert not any("more" in line for line in lines)


@pytest.mark.unit_tests
def test_link_lines_names_both_sides_of_the_pair():
    lines = vc.link_lines([("t1", "r1")], {"t1": "Copilot"}, {"r1": "PII"})

    assert lines == [f"{vc.ROW}t1 (Copilot) → r1 (PII)"]


@pytest.mark.unit_tests
def test_link_lines_falls_back_to_bare_ids_and_truncates():
    pairs = [(f"t{n}", f"r{n}") for n in range(10)]
    lines = vc.link_lines(pairs, {}, {}, limit=2)

    assert lines[0] == f"{vc.ROW}t0 → r0"
    assert lines[-1].strip().startswith("… 8 more")


# ── verify_api() config reconciliation ────────────────────────────────────────


def engine_task(task_id, name=None, rules=()):
    return {
        "id": task_id,
        "name": name,
        "rules": [{"id": r, "name": n} for r, n in rules],
    }


def rule(rule_id, name=None):
    return {"id": rule_id, "name": name}


@pytest.fixture
def api_tasks(monkeypatch):
    """Stub every search verify_api's config phase makes.

    Task searches: `shield` and `engine_scoped` back the task_ids-filtered ones,
    `engine_all` the unscoped one. Rule searches use the `*_rules` keys, with
    `shield_archived_rules` backing the include_archived request the Engine has
    no equivalent for.
    """
    state = {
        "shield": [],
        "engine_scoped": [],
        "engine_all": [],
        "shield_rules": [],
        "shield_archived_rules": [],
        "engine_rules": [],
        "engine_all_rules": [],
    }

    def shield_paginate(path, body, *a, **k):
        if "rules" in path:
            key = (
                "shield_archived_rules"
                if body.get("include_archived")
                else "shield_rules"
            )
            return state[key]
        return state["shield"]

    def engine_paginate(path, body, *a, **k):
        # The unscoped listings are the ones with an empty search body.
        if "rules" in path:
            return state["engine_rules" if body else "engine_all_rules"]
        return state["engine_scoped" if body else "engine_all"]

    monkeypatch.setattr(vc, "shield_paginate", shield_paginate)
    monkeypatch.setattr(vc, "engine_paginate", engine_paginate)
    return state


@pytest.mark.unit_tests
def test_verify_api_names_tasks_missing_from_the_engine(api_tasks, capsys):
    api_tasks["shield"] = [
        engine_task("a", "Support Copilot"),
        engine_task("b", "Claims"),
    ]
    api_tasks["engine_scoped"] = [engine_task("a", "Support Copilot")]
    api_tasks["engine_all"] = [engine_task("a", "Support Copilot")]

    ok = vc.verify_api(
        None,
        None,
        migrated_task_ids=["a", "b"],
        phases_completed=["config"],
    )

    out = capsys.readouterr().out
    assert ok is False
    assert "1 migrated task(s) missing from the Engine" in out
    assert "b (Claims)" in out  # named, so the operator needn't look it up


@pytest.mark.unit_tests
def test_verify_api_names_requested_tasks_that_never_migrated(api_tasks, capsys):
    """The regression this check exists for: every count agrees, yet a task the
    operator asked for is absent from the migration entirely."""
    api_tasks["shield"] = [engine_task("a"), engine_task("b")]
    api_tasks["engine_scoped"] = [engine_task("a"), engine_task("b")]
    api_tasks["engine_all"] = [engine_task("a"), engine_task("b")]

    ok = vc.verify_api(
        None,
        None,
        task_ids=["a", "b", "c"],
        migrated_task_ids=["a", "b"],
        phases_completed=["config"],
    )

    out = capsys.readouterr().out
    assert ok is False
    # The count comparison is happy — 2 recorded, 2 in the Engine.
    assert "✓          tasks" in out
    assert "1 requested task(s) never migrated" in out
    assert "       c" in out


@pytest.mark.unit_tests
def test_verify_api_skips_the_never_migrated_check_when_unscoped(api_tasks, capsys):
    """An unscoped run has no requested set, so the check must stay silent
    rather than claim a ✓ it cannot support."""
    api_tasks["shield"] = [engine_task("a")]
    api_tasks["engine_scoped"] = [engine_task("a")]
    api_tasks["engine_all"] = [engine_task("a")]

    ok = vc.verify_api(
        None,
        None,
        task_ids=None,
        migrated_task_ids=["a"],
        phases_completed=["config"],
    )

    out = capsys.readouterr().out
    assert ok is True
    assert "requested task(s)" not in out


@pytest.mark.unit_tests
def test_verify_api_reconciles_when_the_config_phase_recorded_nothing(
    api_tasks,
    capsys,
):
    """Every requested ID missing from Shield leaves migrated_task_ids empty, so
    none of the searches run and the results keys are absent."""
    ok = vc.verify_api(
        None,
        None,
        task_ids=["a", "b"],
        migrated_task_ids=None,
        phases_completed=["config"],
    )

    out = capsys.readouterr().out
    assert ok is False
    assert "2 requested task(s) never migrated" in out
    assert "       a" in out
    assert "       b" in out


@pytest.mark.unit_tests
def test_verify_api_lists_engine_tasks_outside_the_migration(api_tasks, capsys):
    api_tasks["shield"] = [engine_task("a")]
    api_tasks["engine_scoped"] = [engine_task("a")]
    api_tasks["engine_all"] = [engine_task("a"), engine_task("x", "Legacy Chatbot")]

    ok = vc.verify_api(
        None,
        None,
        migrated_task_ids=["a"],
        phases_completed=["config"],
    )

    out = capsys.readouterr().out
    # Informational only — the Engine legitimately holds data this run didn't
    # insert, so it must not fail the verification.
    assert ok is True
    assert "1 active task(s) in the Engine were not part of this migration" in out
    assert "spans every org visible to ENGINE_API_KEY" in out
    assert "x (Legacy Chatbot)" in out


@pytest.mark.unit_tests
def test_verify_api_reports_a_clean_reconciliation(api_tasks, capsys):
    api_tasks["shield"] = [engine_task("a")]
    api_tasks["engine_scoped"] = [engine_task("a")]
    api_tasks["engine_all"] = [engine_task("a")]

    ok = vc.verify_api(
        None,
        None,
        task_ids=["a"],
        migrated_task_ids=["a"],
        phases_completed=["config"],
    )

    out = capsys.readouterr().out
    assert ok is True
    assert "✓ all 1 requested task(s) migrated" in out
    assert "✓ all 1 migrated task(s) present in the Engine" in out
    assert "✓ no tasks in the Engine outside this migration" in out
    assert "RESULT: ALL MATCH ✓" in out


# ── caveat_lines() ────────────────────────────────────────────────────────────

ALL_PHASES = ["config", "inferences", "feedback"]


def caveats(*args, **kwargs):
    """The caveat block as one whitespace-normalized string.

    The lines are textwrap'd to the report width, so a phrase under assertion
    is as likely as not to straddle a line break."""
    return " ".join(" ".join(vc.caveat_lines(*args, **kwargs)).split())


@pytest.mark.unit_tests
def test_caveats_always_say_the_comparison_is_counts_only():
    for api_mode in (True, False):
        text = caveats(api_mode, ALL_PHASES)
        assert "Counts only" in text
        assert "field-level fidelity" in text


@pytest.mark.unit_tests
def test_caveats_name_the_phases_that_were_not_checked():
    text = caveats(True, ["config"])

    assert "Not checked here: inferences, feedback." in text


@pytest.mark.unit_tests
def test_caveats_omit_the_phase_bullet_when_everything_ran():
    text = caveats(True, ALL_PHASES)

    assert "Not checked here" not in text


@pytest.mark.unit_tests
def test_api_mode_caveats_name_the_checks_only_sql_mode_covers():
    text = caveats(True, ALL_PHASES)

    assert "rule-result detail tree" in text
    assert "org_id" in text
    assert "Re-run without --api-mode" in text


@pytest.mark.unit_tests
def test_sql_mode_caveats_do_not_claim_api_mode_limitations():
    text = caveats(False, ALL_PHASES)

    assert "--api-mode" not in text
    assert "ENGINE_API_KEY" not in text
    # …but it does disclose its own archived-task exclusion.
    assert "archived tasks" in text


@pytest.mark.unit_tests
def test_api_mode_caveats_flag_that_an_unscoped_run_cannot_check_for_skipped_tasks():
    unscoped = caveats(True, ALL_PHASES, task_ids=None)
    scoped = caveats(True, ALL_PHASES, task_ids=["a"])

    assert "not --task-ids scoped" in unscoped
    assert "not --task-ids scoped" not in scoped


@pytest.mark.unit_tests
def test_caveats_mention_taskless_inferences_only_when_that_row_was_printed():
    printed = caveats(True, ALL_PHASES, taskless_inference_ids=["i1"])
    # The row only renders when the inferences phase completed, so claiming a
    # caveat about it otherwise would describe output that isn't there.
    phase_skipped = caveats(True, ["config"], taskless_inference_ids=["i1"])
    none_recorded = caveats(True, ALL_PHASES)

    assert "taskless_inferences row" in printed
    assert "taskless_inferences row" not in phase_skipped
    assert "taskless_inferences row" not in none_recorded


@pytest.mark.unit_tests
def test_caveats_are_appended_to_both_reports(api_tasks, stub_conn, capsys):
    api_tasks["engine_all"] = [engine_task("a")]
    vc.verify_api(None, None, migrated_task_ids=["a"], phases_completed=["config"])
    assert "Caveats — what the result above does and does not cover" in (
        capsys.readouterr().out
    )

    stub_conn.default = 0
    vc.verify(StubEngine(stub_conn), StubEngine(stub_conn), None, None, "org-1")
    assert "Caveats — what the result above does and does not cover" in (
        capsys.readouterr().out
    )


# ── link_reconciliation() ─────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_link_reconciliation_names_missing_and_unexpected_links():
    shield = [engine_task("a", "Support Copilot", [("r1", "PII"), ("r2", "Toxicity")])]
    engine = [engine_task("a", "Support Copilot", [("r1", "PII"), ("r9", "Keyword")])]

    lines, ok = vc.link_reconciliation(shield, engine)
    text = "\n".join(lines)

    assert ok is False
    assert "1 link(s) missing from the Engine" in text
    assert "a (Support Copilot) → r2 (Toxicity)" in text
    assert "1 unexpected link(s) in the Engine" in text
    assert "a (Support Copilot) → r9 (Keyword)" in text


@pytest.mark.unit_tests
def test_link_reconciliation_catches_offsetting_link_changes(api_tasks, capsys):
    """The whole reason this check exists: the task_rule_links row sums link
    counts, so one dropped link and one added link cancel out and it reports ✓."""
    api_tasks["shield"] = [engine_task("a", "Copilot", [("r1", "PII")])]
    api_tasks["engine_scoped"] = [engine_task("a", "Copilot", [("r9", "Keyword")])]
    api_tasks["engine_all"] = api_tasks["engine_scoped"]

    ok = vc.verify_api(
        None,
        None,
        migrated_task_ids=["a"],
        phases_completed=["config"],
    )

    out = capsys.readouterr().out
    # The count row is happy — one link on each side.
    assert "✓          task_rule_links              shield=1  engine=1" in out
    # …and the set diff is not.
    assert ok is False
    assert "1 link(s) missing from the Engine" in out
    assert "1 unexpected link(s) in the Engine" in out


@pytest.mark.unit_tests
def test_link_reconciliation_treats_an_engine_only_task_as_unexpected_links():
    """A Shield-keyed loop would drop this task's links entirely."""
    lines, ok = vc.link_reconciliation([], [engine_task("x", "Rogue", [("r1", "PII")])])
    text = "\n".join(lines)

    assert ok is False
    assert "1 unexpected link(s) in the Engine" in text
    assert "x (Rogue) → r1 (PII)" in text


@pytest.mark.unit_tests
def test_link_reconciliation_reports_a_clean_diff():
    tasks = [engine_task("a", "Copilot", [("r1", "PII"), ("r2", "Toxicity")])]

    lines, ok = vc.link_reconciliation(tasks, tasks)

    assert ok is True
    assert "✓ all 2 task→rule link(s) reproduced in the Engine" in "\n".join(lines)


# ── rule_reconciliation() ─────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_rule_reconciliation_names_active_rules_missing_from_the_engine():
    lines, ok = vc.rule_reconciliation(
        ["r1", "r2"],
        shield_rules=[rule("r1", "PII"), rule("r2", "Toxicity")],
        shield_archived_rules=[],
        engine_rules=[rule("r1", "PII")],
        engine_all_rules=[],
    )
    text = "\n".join(lines)

    assert ok is False
    assert "1 active rule(s) missing from the Engine" in text
    assert "r2 (Toxicity)" in text


@pytest.mark.unit_tests
def test_rule_reconciliation_reports_archived_rules_as_unverifiable_not_missing():
    """migrated_rule_ids includes archived rules, and the Engine rule search can
    never return them. Diffing naively would flag every one as missing."""
    lines, ok = vc.rule_reconciliation(
        ["r1", "arch1", "arch2"],
        shield_rules=[rule("r1", "PII")],
        shield_archived_rules=[rule("arch1", "Old PII"), rule("arch2", "Old Tox")],
        engine_rules=[rule("r1", "PII")],
        engine_all_rules=[],
    )
    text = "\n".join(lines)

    assert ok is True  # archived rules must not fail the run
    assert "2 recorded rule(s) are archived in Shield" in text
    assert "missing from the Engine" not in text
    assert "unknown to Shield" not in text


@pytest.mark.unit_tests
def test_rule_reconciliation_computes_the_archived_bucket_by_set_difference():
    """If Shield's include_archived ever returned actives too, a naive count
    would inflate the archived bucket and hide a real miss."""
    lines, ok = vc.rule_reconciliation(
        ["r1", "arch1"],
        shield_rules=[rule("r1", "PII")],
        # Echoes the active rule alongside the archived one.
        shield_archived_rules=[rule("r1", "PII"), rule("arch1", "Old PII")],
        engine_rules=[],
        engine_all_rules=[],
    )
    text = "\n".join(lines)

    assert "1 recorded rule(s) are archived in Shield" in text
    # r1 is active in Shield and absent from the Engine — still a defect.
    assert ok is False
    assert "1 active rule(s) missing from the Engine" in text


@pytest.mark.unit_tests
def test_rule_reconciliation_flags_rules_unknown_to_shield():
    lines, ok = vc.rule_reconciliation(
        ["r1", "ghost"],
        shield_rules=[rule("r1", "PII")],
        shield_archived_rules=[],
        engine_rules=[rule("r1", "PII")],
        engine_all_rules=[],
    )
    text = "\n".join(lines)

    assert ok is True  # informational, not a defect
    assert "1 recorded rule(s) are unknown to Shield entirely" in text
    assert "ghost" in text


@pytest.mark.unit_tests
def test_rule_reconciliation_lists_engine_rules_outside_the_migration():
    lines, ok = vc.rule_reconciliation(
        ["r1"],
        shield_rules=[rule("r1", "PII")],
        shield_archived_rules=[],
        engine_rules=[rule("r1", "PII")],
        engine_all_rules=[rule("r1", "PII"), rule("d1", "Default Hallucination")],
    )
    text = "\n".join(lines)

    assert ok is True  # informational only
    assert "1 active rule(s) in the Engine were not part of this migration" in text
    assert "includes every default rule" in text
    assert "d1 (Default Hallucination)" in text


@pytest.mark.unit_tests
def test_rule_reconciliation_reports_a_clean_diff():
    lines, ok = vc.rule_reconciliation(
        ["r1"],
        shield_rules=[rule("r1", "PII")],
        shield_archived_rules=[],
        engine_rules=[rule("r1", "PII")],
        engine_all_rules=[rule("r1", "PII")],
    )
    text = "\n".join(lines)

    assert ok is True
    assert "✓ all 1 active rule(s) present in the Engine" in text
    assert "✓ no rules in the Engine outside this migration" in text


@pytest.mark.unit_tests
def test_caveats_flag_that_archived_rules_cannot_be_checked_via_the_api():
    with_rules = caveats(True, ALL_PHASES, migrated_rule_ids=["r1"])
    without = caveats(True, ALL_PHASES)

    assert "Archived rules cannot be verified through the Engine API" in with_rules
    assert "Archived rules cannot be verified" not in without
