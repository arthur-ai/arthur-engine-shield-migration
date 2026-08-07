"""Tests for shield_migration_scripts/migrate_shield_to_engine.py.

Shield and Engine are replaced with in-process fakes; nothing here touches the
network.
"""

import migrate_shield_to_engine as mig
import progress
import pytest


@pytest.fixture(autouse=True)
def quiet_progress(monkeypatch):
    """Silence the live line so tests assert on behaviour, not rendering."""
    monkeypatch.setattr(progress, "PROGRESS_INTERVAL", 0)


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    """Fail loudly instead of hanging.

    SHIELD_BASE_URL/ENGINE_BASE_URL are unset under test, so a call that slips
    through the fakes reaches requests, raises MissingSchema, and then spends
    ~31s inside request_with_retry's exponential backoff before surfacing.
    """

    def unstubbed(*args, **kwargs):
        raise AssertionError(f"unstubbed HTTP call: {args!r}")

    for name in ("shield_get", "shield_post", "engine_call"):
        monkeypatch.setattr(mig, name, unstubbed)


class FakeCheckpoint:
    """Stands in for Checkpoint without touching the filesystem."""

    def __init__(self, inference_page=0, feedback_page=0, archived_rules_migrated=True):
        self.state = {
            "inference_page": inference_page,
            "feedback_page": feedback_page,
            # migrate_inferences runs migrate_archived_rules first; default it
            # to done so the inference tests exercise only the inference path.
            "archived_rules_migrated": archived_rules_migrated,
        }
        self.inference_pages = []
        self.feedback_pages = []
        self.taskless = []
        self.rules = []
        self.phases_done = []

    def phase_completed(self, phase):
        return False

    def phase_done(self, phase):
        self.phases_done.append(phase)

    def update_inference_page(self, page):
        self.state["inference_page"] = page
        self.inference_pages.append(page)

    def update_feedback_page(self, page):
        self.state["feedback_page"] = page
        self.feedback_pages.append(page)

    def record_taskless_inferences(self, ids):
        self.taskless.extend(ids)

    def record_migrated_rules(self, ids):
        self.rules.extend(ids)

    def set_archived_rules_migrated(self):
        pass


def paged(items, page, page_size):
    return items[page * page_size : (page + 1) * page_size]


# ── format_duration was moved into progress.py ────────────────────────────────


@pytest.mark.unit_tests
def test_format_duration_is_imported_not_redefined():
    assert mig.format_duration is progress.format_duration


@pytest.mark.unit_tests
def test_timing_report_still_renders(capsys):
    mig.TIMINGS.clear()
    try:
        mig.record_step_timing("config", "Fetch tasks", 3.5)
        mig.record_phase_timing("config", 65.0)
        mig.print_timing_report()
        out = capsys.readouterr().out
        assert "Fetch tasks: 3.5s" in out
        assert "Total: 1m 05s" in out
    finally:
        mig.TIMINGS.clear()


# ── Pagination ────────────────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_shield_paginate_returns_the_same_items_with_and_without_progress(
    monkeypatch,
):
    """A label must change what is displayed, never what is fetched."""
    items = [{"id": f"r{i}"} for i in range(2500)]
    monkeypatch.setattr(mig, "SHIELD_PAGE_SIZE", 1000)

    def fake_post(path, body, params=None):
        page = params["page"]
        return {"count": len(items), "rules": paged(items, page, 1000)}

    monkeypatch.setattr(mig, "shield_post", fake_post)

    silent = mig.shield_paginate("/rules", {}, "count", "rules")
    labelled = mig.shield_paginate("/rules", {}, "count", "rules", label="rules")
    assert silent == labelled == items


@pytest.mark.unit_tests
def test_engine_paginate_stops_on_a_short_page(monkeypatch):
    items = [{"id": f"t{i}"} for i in range(3)]
    monkeypatch.setattr(mig, "SHIELD_PAGE_SIZE", 1000)
    calls = []

    def fake_call(method, path, body=None, params=None):
        calls.append(params["page"])
        return {"count": len(items), "tasks": paged(items, params["page"], 1000)}

    monkeypatch.setattr(mig, "engine_call", fake_call)

    assert mig.engine_paginate("/t", {}, "count", "tasks", label="tasks") == items
    assert calls == [0]


@pytest.mark.unit_tests
def test_shield_paginate_handles_an_empty_result(monkeypatch):
    monkeypatch.setattr(
        mig,
        "shield_post",
        lambda path, body, params=None: {"count": 0, "rules": []},
    )
    assert mig.shield_paginate("/rules", {}, "count", "rules", label="rules") == []


# ── Bulk posting ──────────────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_engine_post_batches_chunks_and_sums(monkeypatch):
    monkeypatch.setattr(mig, "ENGINE_BATCH_SIZE", 500)
    monkeypatch.setattr(mig, "MAX_WORKERS", 2)
    seen = []

    def fake_call(method, path, body=None, params=None):
        chunk = body["feedback"]
        seen.append(len(chunk))
        return {"inserted": len(chunk), "skipped": 0}

    monkeypatch.setattr(mig, "engine_call", fake_call)

    inserted, skipped = mig.engine_post_batches(
        "/api/v1/migration/feedback/bulk",
        "feedback",
        list(range(1001)),
    )
    assert sorted(seen) == [1, 500, 500]
    assert (inserted, skipped) == (1001, 0)


# ── Archived rules ────────────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_migrate_archived_rules_batches_and_flags_every_rule(monkeypatch, capsys):
    monkeypatch.setattr(mig, "SHIELD_PAGE_SIZE", 1000)
    monkeypatch.setattr(mig, "ENGINE_BATCH_SIZE", 40)
    rules = [{"id": f"r{i}"} for i in range(100)]
    posted = []

    monkeypatch.setattr(
        mig,
        "shield_post",
        lambda path, body, params=None: {"count": len(rules), "rules": rules},
    )

    def fake_call(method, path, body=None, params=None):
        posted.append(body["rules"])
        return {"rules": body["rules"]}

    monkeypatch.setattr(mig, "engine_call", fake_call)

    ckpt = FakeCheckpoint(archived_rules_migrated=False)
    mig.migrate_archived_rules(ckpt)

    assert [len(chunk) for chunk in posted] == [40, 40, 20]
    # Shield cannot filter archived rules by task, so every rule fetched here is
    # flagged archived before it is sent.
    assert all(rule["archived"] for chunk in posted for rule in chunk)
    assert len(ckpt.rules) == 100
    assert "Archived rules:" in capsys.readouterr().out


@pytest.mark.unit_tests
def test_migrate_archived_rules_skips_when_already_done(capsys):
    ckpt = FakeCheckpoint(archived_rules_migrated=True)
    mig.migrate_archived_rules(ckpt)  # no_real_http would fire on any request
    assert "already migrated" in capsys.readouterr().out


# ── Inferences phase ──────────────────────────────────────────────────────────


def wire_inference_fakes(monkeypatch, total, page_size, taskless_ids=()):
    """Serve `total` inferences over Shield pages of `page_size`."""
    monkeypatch.setattr(mig, "SHIELD_PAGE_SIZE", page_size)
    monkeypatch.setattr(mig, "ENGINE_BATCH_SIZE", page_size)
    monkeypatch.setattr(mig, "SHIELD_FETCH_WORKERS", 1)
    monkeypatch.setattr(mig, "MAX_WORKERS", 2)
    inferences = [
        {"id": f"i{i}", "task_id": None if f"i{i}" in taskless_ids else "t1"}
        for i in range(total)
    ]

    def fake_get(path, params=None):
        page = params["page"]
        return {"count": total, "inferences": paged(inferences, page, page_size)}

    def fake_call(method, path, body=None, params=None):
        return {"inserted": len(body["inferences"]), "skipped": 0}

    monkeypatch.setattr(mig, "shield_get", fake_get)
    monkeypatch.setattr(mig, "engine_call", fake_call)
    return inferences


@pytest.mark.unit_tests
def test_migrate_inferences_commits_every_page_in_order(monkeypatch, capsys):
    wire_inference_fakes(monkeypatch, total=25, page_size=10)
    ckpt = FakeCheckpoint()

    mig.migrate_inferences(ckpt, None, None)

    assert ckpt.inference_pages == [1, 2, 3]
    assert ckpt.phases_done == ["inferences"]
    out = capsys.readouterr().out
    assert "Total Inferences Inserted: 25" in out


@pytest.mark.unit_tests
def test_migrate_inferences_records_taskless_ids(monkeypatch):
    wire_inference_fakes(monkeypatch, total=10, page_size=10, taskless_ids={"i3", "i7"})
    ckpt = FakeCheckpoint()

    mig.migrate_inferences(ckpt, None, None)

    assert sorted(ckpt.taskless) == ["i3", "i7"]


@pytest.mark.unit_tests
def test_migrate_inferences_filters_to_the_requested_tasks(monkeypatch):
    monkeypatch.setattr(mig, "SHIELD_PAGE_SIZE", 10)
    monkeypatch.setattr(mig, "ENGINE_BATCH_SIZE", 10)
    monkeypatch.setattr(mig, "SHIELD_FETCH_WORKERS", 1)
    inferences = [{"id": f"i{i}", "task_id": f"t{i % 2}"} for i in range(10)]
    sent = []

    monkeypatch.setattr(
        mig,
        "shield_get",
        lambda path, params=None: {
            "count": 10,
            "inferences": paged(inferences, params["page"], 10),
        },
    )

    def fake_call(method, path, body=None, params=None):
        sent.extend(body["inferences"])
        return {"inserted": len(body["inferences"]), "skipped": 0}

    monkeypatch.setattr(mig, "engine_call", fake_call)

    ckpt = FakeCheckpoint()
    mig.migrate_inferences(ckpt, None, None, task_ids=["t0"])

    assert {inf["task_id"] for inf in sent} == {"t0"}
    assert len(sent) == 5
    # Task-scoped runs migrate no task-less inferences, so none are recorded.
    assert ckpt.taskless == []


@pytest.mark.unit_tests
def test_migrate_inferences_resume_skips_committed_pages(monkeypatch):
    """A resume must not refetch pages the checkpoint already committed."""
    wire_inference_fakes(monkeypatch, total=30, page_size=10)
    requested = []
    original = mig.shield_get
    monkeypatch.setattr(
        mig,
        "shield_get",
        lambda path, params=None: (
            requested.append(params["page"]),
            original(path, params),
        )[1],
    )

    ckpt = FakeCheckpoint(inference_page=2)
    mig.migrate_inferences(ckpt, None, None)

    assert min(requested) == 2  # pages 0 and 1 are never refetched


@pytest.mark.unit_tests
def test_migrate_inferences_progress_starts_at_the_resume_offset(monkeypatch):
    """The rate must exclude the records a resume inherited — see
    test_progress.test_resume_offset_is_excluded_from_the_rate."""
    wire_inference_fakes(monkeypatch, total=30, page_size=10)
    created = {}
    real_progress = progress.Progress

    def spy(total, unit, **kwargs):
        if unit == "inferences scanned":
            created.update({"total": total, **kwargs})
        return real_progress(total, unit, **kwargs)

    monkeypatch.setattr(mig, "Progress", spy)

    mig.migrate_inferences(FakeCheckpoint(inference_page=2), None, None)

    assert created["start"] == 2 * mig.SHIELD_PAGE_SIZE


@pytest.mark.unit_tests
def test_migrate_inferences_propagates_a_fetch_error(monkeypatch):
    monkeypatch.setattr(mig, "SHIELD_PAGE_SIZE", 10)
    monkeypatch.setattr(mig, "SHIELD_FETCH_WORKERS", 1)

    def boom(path, params=None):
        raise RuntimeError("shield is down")

    monkeypatch.setattr(mig, "shield_get", boom)

    with pytest.raises(RuntimeError, match="shield is down"):
        mig.migrate_inferences(FakeCheckpoint(), None, None)


# ── Feedback phase ────────────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_migrate_feedback_pages_until_a_short_page(monkeypatch, capsys):
    monkeypatch.setattr(mig, "SHIELD_PAGE_SIZE", 10)
    monkeypatch.setattr(mig, "ENGINE_BATCH_SIZE", 10)
    rows = [{"id": f"f{i}", "inference_id": "i1"} for i in range(25)]

    monkeypatch.setattr(
        mig,
        "shield_get",
        lambda path, params=None: {
            "total_count": len(rows),
            "feedback": paged(rows, params["page"], 10),
        },
    )
    monkeypatch.setattr(
        mig,
        "engine_call",
        lambda method, path, body=None, params=None: {
            "inserted": len(body["feedback"]),
            "skipped": 0,
        },
    )

    ckpt = FakeCheckpoint()
    mig.migrate_feedback(ckpt, None, None)

    assert ckpt.feedback_pages == [1, 2, 3]
    assert ckpt.phases_done == ["feedback"]
    assert "Total Feedback Inserted: 25" in capsys.readouterr().out
