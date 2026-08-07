"""Tests for shield_migration_scripts/onboard_tasks_from_csv.py.

This script submits jobs that create models on the Arthur platform, so the
behaviour worth pinning down is everything that decides *whether* to submit:
the duplicate checks, the resume path that re-attaches to an existing job
instead of queueing a second one, and the poll loop's terminal conditions —
a job state it fails to recognise as terminal is an infinite loop.

Nothing here touches the network. Every platform client is a fake, and the
retry/poll sleeps are patched out so the timing paths run instantly.
"""

import csv
import json

import onboard_tasks_from_csv as otc
import pytest
from arthur_client.api_bindings import JobState
from arthur_client.api_bindings.exceptions import ApiException


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Retry backoff and poll intervals should not cost real seconds."""
    monkeypatch.setattr(otc, "sleep", lambda _seconds: None)


# ── Fakes ─────────────────────────────────────────────────────────────────────


class Rec:
    """Stand-in for an API record — the script only reads attributes."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Resp:
    def __init__(self, records):
        self.records = records


class FakeConnectors:
    """per_project maps a project to its engine-internal connectors."""

    def __init__(self, per_project=None):
        self.per_project = per_project or {}

    def get_connectors(self, project_id, connector_type, page_size, **kwargs):
        default = [Rec(id="conn-1", name="engine-internal-a")]
        return Resp(self.per_project.get(project_id, default))


class FakeModels:
    """existing: onboarding identifier -> model id. by_dataset: dataset id -> (id, name)."""

    def __init__(self, existing=None, by_dataset=None):
        self.existing = existing or {}
        self.by_dataset = by_dataset or {}

    def get_models(
        self,
        project_id,
        onboarding_identifier=None,
        dataset_id=None,
        page_size=None,
        **kwargs,
    ):
        if dataset_id is not None:
            found = self.by_dataset.get(dataset_id)
            return Resp([Rec(id=found[0], name=found[1])] if found else [])
        model_id = self.existing.get(onboarding_identifier)
        return Resp([Rec(id=model_id, name="model")] if model_id else [])


class FakeDatasets:
    """linked maps a task id to the dataset ids already naming it in the project."""

    def __init__(self, linked=None):
        self.linked = linked or {}
        self.calls = 0

    def get_datasets(self, project_id, page, page_size, **kwargs):
        self.calls += 1
        if page > 1:
            return Resp([])
        records = [
            Rec(
                id=dataset_id,
                dataset_locator=Rec(fields=[Rec(key="task_id", value=task_id)]),
            )
            for task_id, dataset_ids in self.linked.items()
            for dataset_id in dataset_ids
        ]
        # datasets from other connector types must not be mistaken for links
        records.append(
            Rec(id="ds-s3", dataset_locator=Rec(fields=[Rec(key="bucket", value="b")])),
        )
        records.append(Rec(id="ds-none", dataset_locator=None))
        return Resp(records)


class FakeTasks:
    def __init__(self, models=None, complete_on_submit=False, raise_on_submit=None):
        self.submissions = []
        self.models = models
        self.complete_on_submit = complete_on_submit
        self.raise_on_submit = raise_on_submit

    def project_create_model_link_task(
        self,
        project_id,
        post_link_task_request,
        **kwargs,
    ):
        if self.raise_on_submit:
            raise self.raise_on_submit
        self.submissions.append(post_link_task_request)
        if self.complete_on_submit and self.models is not None:
            self.models.existing[post_link_task_request.onboarding_identifier] = (
                f"model-{len(self.submissions)}"
            )
        return Rec(job_id=f"job-{len(self.submissions)}")

    @property
    def submitted_task_ids(self):
        return sorted(s.task_id for s in self.submissions)


class FakeJobs:
    """states maps a job id to the states returned on successive polls.

    The last state in a list sticks, so a job can be parked in `running`.
    """

    def __init__(self, states=None, inflight=None, raise_on_get=None):
        self.states = states or {}
        self.inflight = inflight or []
        self.raise_on_get = raise_on_get
        self.get_calls = 0

    def get_job(self, job_id, **kwargs):
        self.get_calls += 1
        if self.raise_on_get:
            raise self.raise_on_get
        sequence = self.states.get(job_id, [JobState.COMPLETED])
        return Rec(
            id=job_id, state=sequence.pop(0) if len(sequence) > 1 else sequence[0]
        )

    def get_jobs(self, project_id, kinds, states, page, page_size, **kwargs):
        return Resp(self.inflight if page == 1 else [])


def make_rows(*task_project_pairs):
    return [
        otc.RowResult(
            task_id=task_id,
            project_id=project_id,
            onboarding_identifier=f"{otc.ONBOARDING_ID_PREFIX}:{task_id}",
        )
        for task_id, project_id in task_project_pairs
    ]


@pytest.fixture
def build(tmp_path):
    """Builds an Onboarder over a real checkpoint file in tmp_path."""

    def _build(
        rows,
        jobs=None,
        models=None,
        tasks=None,
        connectors=None,
        datasets=None,
        max_wait=1800.0,
        state_name="state.json",
    ):
        models = models if models is not None else FakeModels()
        tasks = tasks if tasks is not None else FakeTasks(models=models)
        state = otc.OnboardingState(str(tmp_path / state_name), "tasks.csv")
        rows = state.merge_csv_rows(rows)
        onboarder = otc.Onboarder(
            state=state,
            connectors_client=connectors or FakeConnectors(),
            models_client=models,
            tasks_client=tasks,
            jobs_client=jobs or FakeJobs(),
            datasets_client=datasets or FakeDatasets(),
            poll_interval=0.01,
            max_in_flight=10,
            max_wait=max_wait,
        )
        return rows, state, onboarder

    return _build


# ── Submitting and polling ────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_links_every_row_and_records_the_model(build):
    models = FakeModels()
    tasks = FakeTasks(models=models, complete_on_submit=True)
    jobs = FakeJobs(
        {
            "job-1": [JobState.QUEUED, JobState.RUNNING, JobState.COMPLETED],
            "job-2": [JobState.COMPLETED],
        },
    )
    rows, _, onboarder = build(
        make_rows(("t1", "p1"), ("t2", "p1")),
        jobs=jobs,
        models=models,
        tasks=tasks,
    )

    onboarder.run(rows)

    assert [r.status for r in rows] == [otc.STATUS_LINKED, otc.STATUS_LINKED]
    assert all(r.model_id for r in rows)
    assert all(r.connector_id == "conn-1" for r in rows)
    assert tasks.submitted_task_ids == ["t1", "t2"]


@pytest.mark.unit_tests
@pytest.mark.parametrize("end_state", [JobState.FAILED, JobState.CANCELLED])
def test_job_ending_outside_completed_is_terminal(build, end_state):
    """A state the poller does not treat as terminal is an infinite loop."""
    jobs = FakeJobs({"job-1": [JobState.RUNNING, end_state]})
    rows, _, onboarder = build(make_rows(("t1", "p1")), jobs=jobs)

    onboarder.run(rows)

    assert rows[0].status == otc.STATUS_FAILED
    assert end_state.value in rows[0].error
    # keeping the job id lets a re-run pick the same job back up
    assert rows[0].job_id == "job-1"


@pytest.mark.unit_tests
def test_job_that_never_finishes_hits_max_wait(build):
    jobs = FakeJobs({"job-1": [JobState.QUEUED]})
    rows, _, onboarder = build(make_rows(("t1", "p1")), jobs=jobs, max_wait=-1)

    onboarder.run(rows)

    assert rows[0].status == otc.STATUS_FAILED
    assert "re-run to resume" in rows[0].error


@pytest.mark.unit_tests
def test_persistent_poll_failure_gives_up_but_keeps_the_job_id(build):
    jobs = FakeJobs(raise_on_get=ApiException(status=503, reason="Service Unavailable"))
    rows, _, onboarder = build(make_rows(("t1", "p1")), jobs=jobs)

    onboarder.run(rows)

    assert rows[0].status == otc.STATUS_FAILED
    assert rows[0].job_id == "job-1"
    assert jobs.get_calls == otc.MAX_ATTEMPTS * otc.MAX_POLL_ERRORS


@pytest.mark.unit_tests
def test_submission_failure_is_recorded_against_the_row_only(build):
    error = ApiException(status=400, reason="Bad Request")
    error.body = json.dumps({"detail": "Maximum number of active keys reached"})
    tasks = FakeTasks(raise_on_submit=error)
    rows, _, onboarder = build(make_rows(("t1", "p1"), ("t2", "p1")), tasks=tasks)

    onboarder.run(rows)

    assert [r.status for r in rows] == [otc.STATUS_FAILED, otc.STATUS_FAILED]
    assert "Maximum number of active keys reached" in rows[0].error


# ── Not linking a task twice ──────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_row_this_script_already_linked_is_skipped(build):
    models = FakeModels(existing={"csv-link:t1": "model-existing"})
    tasks = FakeTasks(models=models)
    rows, _, onboarder = build(make_rows(("t1", "p1")), models=models, tasks=tasks)

    onboarder.run(rows)

    assert rows[0].status == otc.STATUS_SKIPPED
    assert rows[0].model_id == "model-existing"
    assert not tasks.submissions


@pytest.mark.unit_tests
def test_task_linked_outside_this_script_is_reported_not_duplicated(build):
    """The UI leaves no onboarding identifier, so only the locator finds it."""
    models = FakeModels(by_dataset={"ds-1": ("model-ui", "UI App")})
    tasks = FakeTasks(models=models)
    datasets = FakeDatasets(linked={"t1": ["ds-1"]})
    rows, _, onboarder = build(
        make_rows(("t1", "p1")),
        models=models,
        tasks=tasks,
        datasets=datasets,
    )

    onboarder.run(rows)

    assert rows[0].status == otc.STATUS_PRE_EXISTING
    assert rows[0].model_id == "model-ui"
    assert "UI App" in rows[0].detail
    assert not tasks.submissions


@pytest.mark.unit_tests
def test_dataset_naming_the_task_but_backing_no_model_does_not_block(build):
    """A half-finished link must not lock the task out of onboarding forever."""
    models = FakeModels()  # ds-1 resolves to no model
    tasks = FakeTasks(models=models, complete_on_submit=True)
    datasets = FakeDatasets(linked={"t1": ["ds-1"]})
    rows, _, onboarder = build(
        make_rows(("t1", "p1")),
        models=models,
        tasks=tasks,
        datasets=datasets,
    )

    onboarder.run(rows)

    assert rows[0].status == otc.STATUS_LINKED
    assert tasks.submitted_task_ids == ["t1"]


@pytest.mark.unit_tests
def test_locators_from_other_connector_types_are_ignored(build):
    models = FakeModels(by_dataset={"ds-s3": ("model-x", "X")})
    tasks = FakeTasks(models=models, complete_on_submit=True)
    rows, _, onboarder = build(
        make_rows(("t1", "p1")),
        models=models,
        tasks=tasks,
        datasets=FakeDatasets(linked={}),
    )

    onboarder.run(rows)

    assert rows[0].status == otc.STATUS_LINKED


@pytest.mark.unit_tests
def test_project_datasets_are_listed_once_per_project(build):
    models = FakeModels(by_dataset={"ds-1": ("model-ui", "UI App")})
    tasks = FakeTasks(models=models, complete_on_submit=True)
    datasets = FakeDatasets(linked={"t1": ["ds-1"]})
    rows, _, onboarder = build(
        make_rows(("t1", "p1"), ("t2", "p1"), ("t3", "p1")),
        models=models,
        tasks=tasks,
        datasets=datasets,
    )

    onboarder.run(rows)

    assert datasets.calls == 1
    assert tasks.submitted_task_ids == ["t2", "t3"]


@pytest.mark.unit_tests
def test_link_job_already_in_flight_is_adopted(build):
    """Covers a job created by a run that died before checkpointing it."""
    models = FakeModels()
    tasks = FakeTasks(models=models)
    jobs = FakeJobs(
        {"orphan-1": [JobState.RUNNING, JobState.COMPLETED]},
        inflight=[Rec(id="orphan-1", job_spec=Rec(actual_instance=Rec(task_id="t1")))],
    )
    rows, _, onboarder = build(
        make_rows(("t1", "p1")), jobs=jobs, models=models, tasks=tasks
    )

    onboarder.run(rows)

    assert rows[0].job_id == "orphan-1"
    assert not tasks.submissions


# ── Connector resolution ──────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_ambiguous_engine_connector_fails_the_row_with_the_candidates(build):
    """A project has one engine-internal connector per data plane, so >1 is real."""
    connectors = FakeConnectors(
        {
            "p1": [
                Rec(id="c1", name="engine-internal-a"),
                Rec(id="c2", name="engine-internal-b"),
            ],
        },
    )
    tasks = FakeTasks()
    rows, _, onboarder = build(
        make_rows(("t1", "p1")), tasks=tasks, connectors=connectors
    )

    onboarder.run(rows)

    assert rows[0].status == otc.STATUS_FAILED
    assert "engine-internal-a" in rows[0].error
    assert "engine-internal-b" in rows[0].error
    assert not tasks.submissions


@pytest.mark.unit_tests
def test_missing_engine_connector_fails_the_row(build):
    rows, _, onboarder = build(
        make_rows(("t1", "p1")),
        connectors=FakeConnectors({"p1": []}),
    )

    onboarder.run(rows)

    assert rows[0].status == otc.STATUS_FAILED
    assert "no engine-internal connector" in rows[0].error


# ── Resume ────────────────────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_resume_reattaches_to_a_running_job_instead_of_resubmitting(build):
    first_rows, state, _ = build(make_rows(("t1", "p1")), state_name="resume.json")
    first_rows[0].status, first_rows[0].job_id = otc.STATUS_SUBMITTED, "job-99"
    state.rows = {r.key: r for r in first_rows}
    state.save()

    models = FakeModels(existing={"csv-link:t1": "model-from-job-99"})
    tasks = FakeTasks(models=models)
    rows, resumed_state, onboarder = build(
        make_rows(("t1", "p1")),
        jobs=FakeJobs({"job-99": [JobState.COMPLETED]}),
        models=models,
        tasks=tasks,
        state_name="resume.json",
    )

    assert resumed_state.loaded
    assert rows[0].job_id == "job-99"

    onboarder.run(rows)

    assert rows[0].status == otc.STATUS_LINKED
    assert not tasks.submissions


@pytest.mark.unit_tests
def test_resume_retries_a_row_whose_job_died(build):
    first_rows, state, _ = build(make_rows(("t1", "p1")), state_name="dead.json")
    first_rows[0].status = otc.STATUS_FAILED
    first_rows[0].job_id = "job-99"
    first_rows[0].error = "job ended in state failed"
    state.rows = {r.key: r for r in first_rows}
    state.save()

    models = FakeModels()
    tasks = FakeTasks(models=models, complete_on_submit=True)
    rows, _, onboarder = build(
        make_rows(("t1", "p1")),
        jobs=FakeJobs({"job-99": [JobState.FAILED], "job-1": [JobState.COMPLETED]}),
        models=models,
        tasks=tasks,
        state_name="dead.json",
    )

    onboarder.run(rows)

    assert tasks.submitted_task_ids == ["t1"]
    assert rows[0].status == otc.STATUS_LINKED


@pytest.mark.unit_tests
def test_checkpoint_survives_a_partial_write(build, tmp_path):
    rows, state, _ = build(make_rows(("t1", "p1")))
    rows[0].status = otc.STATUS_LINKED
    state.rows = {r.key: r for r in rows}
    state.save()

    assert not list(tmp_path.glob("*.tmp"))
    stored = json.loads((tmp_path / "state.json").read_text())
    assert stored["rows"][0]["status"] == otc.STATUS_LINKED
    assert stored["last_updated_at"]


# ── CSV input ─────────────────────────────────────────────────────────────────


def write_csv(tmp_path, text, name="tasks.csv", encoding="utf-8"):
    path = tmp_path / name
    path.write_text(text, encoding=encoding)
    return str(path)


@pytest.mark.unit_tests
def test_reads_byte_order_marks_and_untidy_headers(tmp_path):
    """Excel exports carry a BOM, which otherwise hides the first column."""
    path = write_csv(
        tmp_path,
        "Task_ID, project_id\nt1,p1\n",
        encoding="utf-8-sig",
    )

    rows = otc.read_rows(path)

    assert [(r.task_id, r.project_id) for r in rows] == [("t1", "p1")]


@pytest.mark.unit_tests
def test_duplicate_rows_are_dropped_but_the_same_task_may_span_projects(tmp_path):
    """Two rows for one task in one project would each create a model."""
    path = write_csv(tmp_path, "task_id,project_id\nt1,p1\nt1,p1\nt1,p2\n")

    rows = otc.read_rows(path)

    assert [(r.task_id, r.project_id) for r in rows] == [("t1", "p1"), ("t1", "p2")]


@pytest.mark.unit_tests
def test_every_invalid_row_is_reported_at_once(tmp_path):
    path = write_csv(tmp_path, "task_id,project_id\n,p1\nt2,\nt3,p3\n")

    with pytest.raises(otc.CsvError) as excinfo:
        otc.read_rows(path)

    assert "row 2" in str(excinfo.value)
    assert "row 3" in str(excinfo.value)


@pytest.mark.unit_tests
def test_missing_columns_are_named(tmp_path):
    path = write_csv(tmp_path, "task,project\nt1,p1\n")

    with pytest.raises(otc.CsvError) as excinfo:
        otc.read_rows(path)

    assert "task_id" in str(excinfo.value)
    assert "project_id" in str(excinfo.value)


# ── Error reporting ───────────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_api_error_keeps_the_platform_message():
    error = ApiException(status=400, reason="Bad Request")
    error.body = json.dumps({"detail": "Connector id X not found in project Y."})

    assert "Connector id X not found" in otc.format_error(error)


@pytest.mark.unit_tests
def test_api_error_handles_a_bytes_body_and_a_detail_list():
    error = ApiException(status=422, reason="Unprocessable")
    error.body = b'{"detail": [{"msg": "bad uuid"}]}'

    assert "bad uuid" in otc.format_error(error)


@pytest.mark.unit_tests
@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (503, True), (400, False), (404, False)],
)
def test_only_server_side_failures_are_retried(status, retryable):
    assert otc.is_retryable(ApiException(status=status, reason="")) is retryable


# ── End to end through main() ─────────────────────────────────────────────────


@pytest.fixture
def run_main(monkeypatch, tmp_path):
    """Runs main() against fakes, returning (exit_code, results_rows)."""
    state = {"models": {}, "datasets": {}, "dataset_models": {}, "submits": []}
    results_dir = tmp_path / "results"
    monkeypatch.setattr(otc, "ARTHUR_API_HOST", "https://example.invalid")
    monkeypatch.setattr(otc, "RESULTS_DIR", str(results_dir))
    monkeypatch.setattr(otc, "STATE_DIR", str(tmp_path / "states"))
    monkeypatch.setattr(otc, "build_api_client", lambda host: None)

    class Tasks(FakeTasks):
        def __init__(self, api_client=None):
            super().__init__()

        def project_create_model_link_task(
            self, project_id, post_link_task_request, **kw
        ):
            state["submits"].append(post_link_task_request.task_id)
            state["models"][
                post_link_task_request.onboarding_identifier
            ] = f"model-{post_link_task_request.task_id}"
            return Rec(job_id=f"job-{post_link_task_request.task_id}")

    class Models(FakeModels):
        def __init__(self, api_client=None):
            super().__init__(state["models"], state["dataset_models"])

    class Datasets(FakeDatasets):
        def __init__(self, api_client=None):
            super().__init__(
                {task: [ds] for task, ds in state["datasets"].items()},
            )

    class Connectors(FakeConnectors):
        def __init__(self, api_client=None):
            super().__init__()

    class Jobs(FakeJobs):
        def __init__(self, api_client=None):
            super().__init__()

    for name, fake in (
        ("ConnectorsV1Api", Connectors),
        ("ModelsV1Api", Models),
        ("TasksV1Api", Tasks),
        ("JobsV1Api", Jobs),
        ("DatasetsV1Api", Datasets),
    ):
        monkeypatch.setattr(otc, name, fake)

    def _run(csv_path, *extra_args):
        monkeypatch.setattr(
            "sys.argv",
            ["onboard_tasks_from_csv.py", "--csv-path", csv_path, *extra_args],
        )
        code = otc.main()
        newest = sorted(results_dir.iterdir())[-1]
        with open(newest) as f:
            return code, list(csv.DictReader(f))

    _run.state = state
    return _run


@pytest.mark.unit_tests
def test_main_links_every_row_and_exits_clean(run_main, tmp_path):
    path = write_csv(tmp_path, "task_id,project_id,org_id\nt1,p1,org-a\nt2,p2,org-b\n")

    code, results = run_main(path)

    assert code == 0
    assert {r["status"] for r in results} == {otc.STATUS_LINKED}
    assert {r["org_id"] for r in results} == {"org-a", "org-b"}
    assert all(r["connector_id"] == "conn-1" for r in results)


@pytest.mark.unit_tests
def test_main_is_a_no_op_when_rerun(run_main, tmp_path):
    """The checkpoint settles the row without going near the API."""
    path = write_csv(tmp_path, "task_id,project_id\nt1,p1\n")
    run_main(path)
    run_main.state["submits"].clear()

    code, results = run_main(path)

    assert code == 0
    assert not run_main.state["submits"]
    assert results[0]["status"] == otc.STATUS_LINKED


@pytest.mark.unit_tests
def test_main_restart_finds_the_model_an_earlier_run_created(run_main, tmp_path):
    """With the checkpoint gone, the onboarding identifier is the only guard."""
    path = write_csv(tmp_path, "task_id,project_id\nt1,p1\n")
    run_main(path)
    run_main.state["submits"].clear()

    code, results = run_main(path, "--restart")

    assert code == 0
    assert not run_main.state["submits"]
    assert results[0]["status"] == otc.STATUS_SKIPPED


@pytest.mark.unit_tests
def test_main_reports_a_task_linked_outside_the_script(run_main, tmp_path):
    path = write_csv(tmp_path, "task_id,project_id\nt1,p1\nt2,p1\n")
    run_main.state["datasets"]["t2"] = "ds-ui"
    run_main.state["dataset_models"]["ds-ui"] = ("model-ui", "Linked In UI")

    code, results = run_main(path)

    pre_existing = [r for r in results if r["status"] == otc.STATUS_PRE_EXISTING]
    assert code == 0  # a pre-existing link is information, not a failure
    assert run_main.state["submits"] == ["t1"]
    assert [r["task_id"] for r in pre_existing] == ["t2"]
    assert pre_existing[0]["model_id"] == "model-ui"
    assert "Linked In UI" in pre_existing[0]["detail"]


@pytest.mark.unit_tests
def test_main_writes_results_and_a_checkpoint_when_interrupted(run_main, tmp_path):
    path = write_csv(tmp_path, "task_id,project_id\nt1,p1\nt2,p1\n")

    def interrupt_after_one(project_id, post_link_task_request, **kwargs):
        if run_main.state["submits"]:
            raise KeyboardInterrupt
        run_main.state["submits"].append(post_link_task_request.task_id)
        return Rec(job_id="job-t1")

    otc.TasksV1Api.project_create_model_link_task = staticmethod(interrupt_after_one)

    code, results = run_main(path)

    assert code == 130
    assert len(results) == 2
    checkpoint = json.loads(
        (tmp_path / "states" / "onboarding_state_tasks.json").read_text(),
    )
    submitted = [r for r in checkpoint["rows"] if r["job_id"]]
    assert submitted, "the job id must be checkpointed so a re-run can re-attach"


@pytest.mark.unit_tests
def test_main_rejects_an_unusable_csv_without_a_traceback(run_main, tmp_path, capsys):
    path = write_csv(tmp_path, "task,project\nt1,p1\n")
    import sys

    sys.argv = ["onboard_tasks_from_csv.py", "--csv-path", path]

    assert otc.main() == 1
    assert "task_id" in capsys.readouterr().err
