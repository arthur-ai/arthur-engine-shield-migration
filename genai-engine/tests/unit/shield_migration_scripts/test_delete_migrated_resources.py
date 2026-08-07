"""Tests for shield_migration_scripts/delete_migrated_resources.py.

This script deletes directly from the Engine database, so the dry-run guard is
the single most important thing to hold in place.
"""

import json

import delete_migrated_resources as dmr
import progress
import pytest


@pytest.fixture(autouse=True)
def quiet_progress(monkeypatch):
    monkeypatch.setattr(progress, "PROGRESS_INTERVAL", 0)


@pytest.fixture
def save_file(tmp_path):
    def write(**state):
        path = tmp_path / "migration_state.json"
        path.write_text(json.dumps(state))
        return str(path)

    return write


class FakeEngine:
    """Records the batches handed to delete_batch_transaction."""

    def __init__(self, ids=()):
        self.ids = list(ids)

    def connect(self):
        return self._ctx()

    def begin(self):
        return self._ctx()

    def _ctx(engine_self):
        class Conn:
            def execute(self, stmt, params=None):
                class Result:
                    def scalar(self):
                        return len(engine_self.ids)

                    def scalars(self):
                        return engine_self.ids

                    def __iter__(self):
                        return iter((i,) for i in engine_self.ids)

                return Result()

        class Ctx:
            def __enter__(self):
                return Conn()

            def __exit__(self, *exc):
                return False

        return Ctx()


# ── Batch deletion ────────────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_delete_taskless_inferences_batches_and_reports(monkeypatch, capsys):
    """This loop was entirely silent before — verify it batches correctly and
    now reports."""
    monkeypatch.setattr(dmr, "BATCH_SIZE", 10)
    monkeypatch.setattr(progress, "PROGRESS_INTERVAL", 2.0)
    batches = []
    monkeypatch.setattr(
        dmr,
        "delete_batch_transaction",
        lambda engine, batch: batches.append(list(batch)) or len(batch),
    )

    ids = [f"i{n}" for n in range(25)]
    dmr.delete_taskless_inferences(object(), ids)

    assert [len(b) for b in batches] == [10, 10, 5]
    assert [i for b in batches for i in b] == ids
    assert "task-less inferences deleted" in capsys.readouterr().out


@pytest.mark.unit_tests
def test_delete_taskless_inferences_with_nothing_to_do_is_silent(monkeypatch, capsys):
    monkeypatch.setattr(progress, "PROGRESS_INTERVAL", 2.0)
    monkeypatch.setattr(
        dmr,
        "delete_batch_transaction",
        lambda engine, batch: pytest.fail("should not delete"),
    )

    dmr.delete_taskless_inferences(object(), [])

    assert capsys.readouterr().out == ""


@pytest.mark.unit_tests
def test_delete_task_inferences_counts_every_batch(monkeypatch):
    monkeypatch.setattr(dmr, "BATCH_SIZE", 10)
    monkeypatch.setattr(dmr, "SQL_WORKERS", 2)
    ids = [f"i{n}" for n in range(25)]
    monkeypatch.setattr(dmr, "select_ids", lambda conn, sql, ids=None: list(ids_source))
    ids_source = ids
    monkeypatch.setattr(
        dmr,
        "delete_batch_transaction",
        lambda engine, batch: len(batch),
    )

    deleted = dmr.delete_task_inferences(FakeEngine(ids), ["t1"])

    assert deleted == 25


# ── Dry run ───────────────────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_dry_run_deletes_nothing(monkeypatch, save_file, capsys):
    """Without --execute the script must list and exit before any deletion."""
    for name in (
        "delete_task_inferences",
        "delete_taskless_inferences",
        "delete_rules_and_tasks",
    ):
        monkeypatch.setattr(
            dmr,
            name,
            lambda *a, **k: pytest.fail("dry run must not delete"),
        )
    monkeypatch.setattr(dmr, "get_engine", lambda: FakeEngine(["i1", "i2"]))
    monkeypatch.setattr(
        "sys.argv",
        [
            "delete_migrated_resources.py",
            "--save-file",
            save_file(
                migrated_task_ids=["t1"],
                migrated_taskless_inference_ids=["i9"],
                migrated_rule_ids=["r1"],
            ),
        ],
    )

    dmr.main()

    out = capsys.readouterr().out
    assert "Dry run — no resources deleted" in out
    assert "1 task(s) with 2 inference(s)" in out


@pytest.mark.unit_tests
def test_execute_runs_every_deletion_step(monkeypatch, save_file, capsys):
    called = []
    for name in (
        "delete_task_inferences",
        "delete_taskless_inferences",
        "delete_rules_and_tasks",
    ):
        monkeypatch.setattr(
            dmr,
            name,
            lambda *a, _name=name, **k: called.append(_name),
        )
    monkeypatch.setattr(dmr, "get_engine", lambda: FakeEngine(["i1"]))
    monkeypatch.setattr(
        "sys.argv",
        [
            "delete_migrated_resources.py",
            "--save-file",
            save_file(migrated_task_ids=["t1"], migrated_rule_ids=["r1"]),
            "--execute",
        ],
    )

    dmr.main()

    assert called == [
        "delete_task_inferences",
        "delete_taskless_inferences",
        "delete_rules_and_tasks",
    ]
    assert "Done." in capsys.readouterr().out


@pytest.mark.unit_tests
def test_an_empty_save_file_exits_before_connecting(monkeypatch, save_file, capsys):
    monkeypatch.setattr(
        dmr,
        "get_engine",
        lambda: pytest.fail("must not connect with nothing to delete"),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["delete_migrated_resources.py", "--save-file", save_file()],
    )

    dmr.main()

    assert "Nothing recorded in the save file to delete." in capsys.readouterr().out
