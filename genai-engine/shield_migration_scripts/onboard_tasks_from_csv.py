"""Onboard GenAI applications from a CSV of existing engine tasks.

Each row links an existing task to a new model in a project via the link_task
job — the same flow as the UI's "link existing application". The job creates the
dataset, the model, and the task's validation key server-side, so no dataset
upload or list-datasets refresh is required first.

CSV columns (header row required, names are case-insensitive):
    task_id       required  engine task id to link
    project_id    required  scope project to create the model in
    org_id        optional  informational only, echoed in results
    connector_id  optional  engine-internal connector id; auto-resolved from
                            the project when omitted

Runs are resumable. Progress is checkpointed to a state file after every
change, so an interrupted run — Ctrl-C, crash, expired token, timed-out job —
can be restarted with the same command: rows that finished are skipped, jobs
that were still running are re-attached to by job id instead of resubmitted,
and failed rows are retried.

Auth (env vars):
    ARTHUR_API_HOST       arthur platform base url (e.g. https://platform.arthur.ai)
    ARTHUR_CLIENT_ID      service account client id
    ARTHUR_CLIENT_SECRET  service account client secret

Usage:
    python onboard_tasks_from_csv.py --csv-path tasks.csv
    python onboard_tasks_from_csv.py --csv-path tasks.csv --restart
"""

import argparse
import csv
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from time import monotonic, sleep
from typing import Optional

from arthur_client.api_bindings import (
    ConnectorsV1Api,
    ConnectorType,
    JobKind,
    JobState,
    JobsV1Api,
    ModelsV1Api,
    PostLinkTaskRequest,
    TasksV1Api,
)
from arthur_client.api_bindings.api_client import ApiClient
from arthur_client.api_bindings.exceptions import ApiException
from arthur_client.auth import (
    ArthurClientCredentialsAPISession,
    ArthurOAuthSessionAPIConfiguration,
    ArthurOIDCMetadata,
    DeviceAuthorizer,
)
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

ARTHUR_API_HOST = os.environ.get("ARTHUR_API_HOST")
ARTHUR_CLIENT_ID = os.environ.get("ARTHUR_CLIENT_ID")
ARTHUR_CLIENT_SECRET = os.environ.get("ARTHUR_CLIENT_SECRET")

# defaults to genai-engine/onboarding_results and genai-engine/onboarding_states
_GENAI_ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.getenv(
    "ONBOARDING_RESULTS_DIR",
    default=os.path.join(_GENAI_ENGINE_DIR, "onboarding_results"),
)
STATE_DIR = os.getenv(
    "ONBOARDING_STATE_DIR",
    default=os.path.join(_GENAI_ENGINE_DIR, "onboarding_states"),
)

REQUEST_TIMEOUT = float(
    os.getenv("ONBOARDING_REQUEST_TIMEOUT", default=30),
)  # seconds for a single platform API call
MAX_ATTEMPTS = 5  # attempts per retryable read call
MAX_POLL_ERRORS = 5  # consecutive get_job failures before a row is given up on
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

CONNECTOR_PAGE_SIZE = 10  # enough to detect an ambiguous project
JOB_SCAN_PAGE_SIZE = 200
JOB_SCAN_MAX_PAGES = 25

ONBOARDING_ID_PREFIX = "csv-link"

# Anything outside these is terminal. Written as the complement of the
# in-progress states so a state added to the API later cannot hang the poller.
IN_PROGRESS_JOB_STATES = {JobState.QUEUED, JobState.RUNNING}

STATUS_PENDING = "pending"
STATUS_SUBMITTED = "submitted"
STATUS_LINKED = "linked"
STATUS_SKIPPED = "skipped_already_linked"
STATUS_FAILED = "failed"

# Reached the goal — never retried, and never re-checked against the API.
DONE_STATUSES = {STATUS_LINKED, STATUS_SKIPPED}


class CsvError(Exception):
    """The input CSV is unusable — reported without a traceback."""


class RowError(Exception):
    """A row cannot be onboarded — recorded against that row only."""


# ── Rows and state ────────────────────────────────────────────────────────────


@dataclass
class RowResult:
    task_id: str
    project_id: str
    org_id: str = ""
    connector_id: str = ""
    status: str = STATUS_PENDING
    job_id: str = ""
    model_id: str = ""
    error: str = ""
    onboarding_identifier: str = field(default="", repr=False)

    @property
    def key(self) -> str:
        return f"{self.project_id}|{self.task_id}"


RESULT_COLUMNS = [
    "task_id",
    "project_id",
    "org_id",
    "connector_id",
    "status",
    "job_id",
    "model_id",
    "error",
]


class OnboardingState:
    """Checkpoint of every row's progress, saved after each change.

    A run is resumed by reloading this file: rows that reached a terminal
    success are skipped, rows holding a job id are re-attached to that job
    rather than resubmitted, and everything else is retried. Writes go to a
    temp file and are renamed into place, so an interrupt mid-write cannot
    leave a truncated checkpoint behind.
    """

    def __init__(self, path: str, csv_path: str) -> None:
        self.path = path
        self.rows: dict[str, RowResult] = {}
        self.loaded = False
        self.state = {
            "csv_path": os.path.abspath(csv_path),
            "started_at": _utc_now(),
            "last_updated_at": None,
        }
        if os.path.exists(path):
            self._load(csv_path)

    def _load(self, csv_path: str) -> None:
        try:
            with open(self.path) as f:
                stored = json.load(f)
        except (OSError, ValueError) as e:
            raise CsvError(
                f"Could not read state file {self.path}: {e}\n"
                f"Fix or delete it, or re-run with --restart to start over.",
            )
        for key in ("csv_path", "started_at"):
            if stored.get(key):
                self.state[key] = stored[key]
        for raw in stored.get("rows", []):
            row = RowResult(**{k: raw.get(k, "") for k in _row_fields()})
            self.rows[row.key] = row
        self.loaded = True

        stored_csv = self.state.get("csv_path")
        if stored_csv and stored_csv != os.path.abspath(csv_path):
            print(
                f"WARNING: state file was created from {stored_csv}, "
                f"now running against {os.path.abspath(csv_path)}",
            )

    def merge_csv_rows(self, csv_rows: list[RowResult]) -> list[RowResult]:
        """Return the CSV's rows with any checkpointed progress applied.

        The CSV is authoritative for inputs (org_id, connector_id); the state
        file is authoritative for progress. Checkpointed rows that are no
        longer in the CSV are dropped, since the results file reports on the
        CSV that was actually run.
        """
        merged = []
        for row in csv_rows:
            stored = self.rows.get(row.key)
            if stored:
                row.status = stored.status
                row.job_id = stored.job_id
                row.model_id = stored.model_id
                row.error = stored.error
                if not row.connector_id:
                    row.connector_id = stored.connector_id
            merged.append(row)
        dropped = len(self.rows) - sum(1 for r in merged if r.key in self.rows)
        if dropped > 0:
            print(f"WARNING: {dropped} checkpointed row(s) are no longer in the CSV")
        self.rows = {row.key: row for row in merged}
        return merged

    def save(self) -> None:
        self.state["last_updated_at"] = _utc_now()
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = dict(self.state)
        payload["rows"] = [asdict(row) for row in self.rows.values()]
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, self.path)


def _row_fields() -> list[str]:
    return list(RowResult.__dataclass_fields__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_name(state) -> str:
    """Render a JobState as its wire value rather than its enum repr."""
    return getattr(state, "value", str(state))


# ── API helpers ───────────────────────────────────────────────────────────────


def build_api_client(arthur_host: str) -> ApiClient:
    if ARTHUR_CLIENT_ID and ARTHUR_CLIENT_SECRET:
        sess = ArthurClientCredentialsAPISession(
            client_id=ARTHUR_CLIENT_ID,
            client_secret=ARTHUR_CLIENT_SECRET,
            metadata=ArthurOIDCMetadata(arthur_host=arthur_host),
        )
    else:
        print("ARTHUR_CLIENT_ID/ARTHUR_CLIENT_SECRET not set, using browser auth")
        sess = DeviceAuthorizer(arthur_host=arthur_host).authorize()
    return ApiClient(configuration=ArthurOAuthSessionAPIConfiguration(session=sess))


def is_retryable(e: Exception) -> bool:
    """Retry server-side hiccups and transport errors, never client errors."""
    if isinstance(e, ApiException):
        return e.status in RETRYABLE_STATUS_CODES
    # connection resets, read timeouts and DNS blips surface as transport
    # errors rather than ApiException
    return not isinstance(e, (RowError, CsvError))


def call_with_retry(fn, **kwargs):
    """Call a *read-only* endpoint, retrying transient failures.

    Only used for GETs. The link_task submission is deliberately never retried
    here: a timed-out POST may still have created the job server-side, and a
    blind retry would create a duplicate model. That case is recovered instead
    by the pre-submit checks in `Onboarder._resolve_row_state`.
    """
    last_error: Optional[Exception] = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return fn(_request_timeout=REQUEST_TIMEOUT, **kwargs)
        except Exception as e:
            if not is_retryable(e) or attempt == MAX_ATTEMPTS - 1:
                raise
            last_error = e
            delay = 2**attempt
            print(f"  transient error ({format_error(e)}), retrying in {delay}s")
            sleep(delay)
    raise last_error  # unreachable, kept for type-checkers


def format_error(e: Exception) -> str:
    """Human-readable error, including the server's message when there is one."""
    if isinstance(e, ApiException):
        message = f"{e.status} {e.reason}"
        detail = _api_error_detail(e)
        return f"{message}: {detail}" if detail else message
    return f"{type(e).__name__}: {e}"


def _api_error_detail(e: ApiException) -> str:
    body = getattr(e, "body", None)
    if not body:
        return ""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(body)
    except ValueError:
        return str(body).strip()[:500]
    if isinstance(parsed, dict):
        detail = parsed.get("detail") or parsed.get("message") or parsed
    else:
        detail = parsed
    if not isinstance(detail, str):
        detail = json.dumps(detail)
    return detail.strip()[:500]


# ── CSV input ─────────────────────────────────────────────────────────────────


def read_rows(csv_path: str) -> list[RowResult]:
    """Parse the input CSV, reporting every bad row at once.

    Read as utf-8-sig so a byte-order mark from an Excel export does not turn
    the first header into an unrecognized column.
    """
    rows: list[RowResult] = []
    errors: list[str] = []
    seen: dict[tuple[str, str], int] = {}

    try:
        handle = open(csv_path, newline="", encoding="utf-8-sig")
    except OSError as e:
        raise CsvError(f"Could not open {csv_path}: {e}")

    with handle as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames
        if headers is None:
            raise CsvError(f"{csv_path} is empty")
        # normalize so " Task_ID" and "task_id" are the same column
        reader.fieldnames = [(h or "").strip().lower() for h in headers]
        missing = {"task_id", "project_id"} - set(reader.fieldnames)
        if missing:
            raise CsvError(
                f"{csv_path} is missing required column(s): "
                f"{', '.join(sorted(missing))}. Found: "
                f"{', '.join(reader.fieldnames)}",
            )

        for line_num, raw in enumerate(reader, start=2):
            task_id = _cell(raw, "task_id")
            project_id = _cell(raw, "project_id")
            if not task_id or not project_id:
                errors.append(f"row {line_num}: task_id and project_id are required")
                continue
            duplicate_of = seen.get((task_id, project_id))
            if duplicate_of:
                # both rows would pass the already-linked check and each submit
                # its own job, producing two models for one task
                print(
                    f"WARNING: row {line_num} duplicates row {duplicate_of} "
                    f"({task_id} in {project_id}), skipping the duplicate",
                )
                continue
            seen[(task_id, project_id)] = line_num
            rows.append(
                RowResult(
                    task_id=task_id,
                    project_id=project_id,
                    org_id=_cell(raw, "org_id"),
                    connector_id=_cell(raw, "connector_id"),
                    onboarding_identifier=f"{ONBOARDING_ID_PREFIX}:{task_id}",
                ),
            )

    if errors:
        shown = errors[:20]
        suffix = (
            ""
            if len(errors) == len(shown)
            else f"\n... and {len(errors) - len(shown)} more"
        )
        raise CsvError(
            f"{csv_path} has invalid rows:\n  " + "\n  ".join(shown) + suffix
        )
    if not rows:
        raise CsvError(f"{csv_path} has a header but no usable rows")
    return rows


def _cell(raw: dict, column: str) -> str:
    return (raw.get(column) or "").strip()


# ── Lookups ───────────────────────────────────────────────────────────────────


def resolve_connector_id(
    connectors_client: ConnectorsV1Api,
    project_id: str,
    cache: dict[str, list],
) -> str:
    """Engine-internal connector for the project, cached per project.

    A project gets one engine-internal connector per associated data plane, so
    more than one is possible; picking arbitrarily would link the task against
    the wrong engine. Ambiguity is an error the CSV has to resolve.
    """
    if project_id not in cache:
        resp = call_with_retry(
            connectors_client.get_connectors,
            project_id=project_id,
            connector_type=ConnectorType.ENGINE_INTERNAL,
            page_size=CONNECTOR_PAGE_SIZE,
        )
        cache[project_id] = list(resp.records)
    records = cache[project_id]
    if not records:
        raise RowError(f"no engine-internal connector in project {project_id}")
    if len(records) > 1:
        listed = ", ".join(f"{c.name} ({c.id})" for c in records[:CONNECTOR_PAGE_SIZE])
        raise RowError(
            f"{len(records)} engine-internal connectors in project {project_id} "
            f"[{listed}] — set connector_id in the CSV to choose one",
        )
    return records[0].id


def find_existing_model_id(
    models_client: ModelsV1Api,
    row: RowResult,
) -> Optional[str]:
    resp = call_with_retry(
        models_client.get_models,
        project_id=row.project_id,
        onboarding_identifier=row.onboarding_identifier,
        page_size=1,
    )
    return resp.records[0].id if resp.records else None


def find_inflight_link_job(
    jobs_client: JobsV1Api,
    row: RowResult,
    cache: dict[str, dict[str, str]],
) -> Optional[str]:
    """Job id of an unfinished link job for this task, if one is already queued.

    Covers the gap the model lookup cannot: a job submitted by an earlier run
    (or by the UI) that has not created its model yet. Without this, a resume
    whose checkpoint missed the submission would queue a second job for the
    same task. Best-effort — if the job list cannot be read, onboarding still
    proceeds.
    """
    if row.project_id not in cache:
        mapping: dict[str, str] = {}
        try:
            page = 1
            while page <= JOB_SCAN_MAX_PAGES:
                resp = call_with_retry(
                    jobs_client.get_jobs,
                    project_id=row.project_id,
                    kinds=[JobKind.CREATE_MODEL_LINK_TASK],
                    states=sorted(IN_PROGRESS_JOB_STATES),
                    page=page,
                    page_size=JOB_SCAN_PAGE_SIZE,
                )
                for job in resp.records:
                    spec = getattr(job.job_spec, "actual_instance", None)
                    task_id = getattr(spec, "task_id", None)
                    if task_id:
                        mapping.setdefault(str(task_id), job.id)
                if len(resp.records) < JOB_SCAN_PAGE_SIZE:
                    break
                page += 1
        except Exception as e:
            print(
                f"WARNING: could not list in-flight link jobs for project "
                f"{row.project_id} ({format_error(e)}); a job submitted by an "
                f"interrupted run may be duplicated",
            )
        cache[row.project_id] = mapping
    return cache[row.project_id].get(row.task_id)


# ── Onboarding ────────────────────────────────────────────────────────────────


class Onboarder:
    def __init__(
        self,
        state: OnboardingState,
        connectors_client: ConnectorsV1Api,
        models_client: ModelsV1Api,
        tasks_client: TasksV1Api,
        jobs_client: JobsV1Api,
        poll_interval: float,
        max_in_flight: int,
        max_wait: float,
    ) -> None:
        self.state = state
        self.connectors_client = connectors_client
        self.models_client = models_client
        self.tasks_client = tasks_client
        self.jobs_client = jobs_client
        self.poll_interval = poll_interval
        self.max_in_flight = max_in_flight
        self.max_wait = max_wait
        self.connector_cache: dict[str, list] = {}
        self.job_scan_cache: dict[str, dict[str, str]] = {}
        self.deadlines: dict[str, float] = {}
        self.poll_errors: dict[str, int] = {}

    # -- resume -----------------------------------------------------------

    def reattach(self, rows: list[RowResult]) -> list[RowResult]:
        """Re-attach checkpointed rows that still hold a job id.

        Returns the rows whose jobs are still in flight. A job that already
        finished is settled here; one that ended badly is reset to pending so
        the run retries it (the already-linked check still guards duplicates).
        """
        in_flight = []
        for row in rows:
            if row.status in DONE_STATUSES or not row.job_id:
                continue
            try:
                job = call_with_retry(self.jobs_client.get_job, job_id=row.job_id)
            except Exception as e:
                print(
                    f"[{row.task_id}] could not read job {row.job_id} "
                    f"({format_error(e)}), retrying the row from scratch",
                )
                row.status, row.job_id = STATUS_PENDING, ""
                continue
            if job.state in IN_PROGRESS_JOB_STATES:
                print(
                    f"[{row.task_id}] re-attached to job {row.job_id} "
                    f"({_state_name(job.state)})",
                )
                row.status = STATUS_SUBMITTED
                self.deadlines[row.key] = monotonic() + self.max_wait
                in_flight.append(row)
            elif job.state == JobState.COMPLETED:
                self._mark_linked(row)
            else:
                print(
                    f"[{row.task_id}] previous job {row.job_id} ended in state "
                    f"{_state_name(job.state)}, retrying",
                )
                row.status, row.job_id, row.error = STATUS_PENDING, "", ""
        self.state.save()
        return in_flight

    # -- submission -------------------------------------------------------

    def submit(self, row: RowResult) -> bool:
        """Submit one row. Returns True when a job is now in flight for it."""
        try:
            if self._resolve_row_state(row):
                # already onboarded, or an earlier job is still running for it
                return row.status == STATUS_SUBMITTED

            if not row.connector_id:
                row.connector_id = resolve_connector_id(
                    self.connectors_client,
                    row.project_id,
                    self.connector_cache,
                )

            resp = self.tasks_client.project_create_model_link_task(
                project_id=row.project_id,
                post_link_task_request=PostLinkTaskRequest(
                    task_id=row.task_id,
                    connector_id=row.connector_id,
                    onboarding_identifier=row.onboarding_identifier,
                ),
                _request_timeout=REQUEST_TIMEOUT,
            )
            row.job_id = resp.job_id
            row.status = STATUS_SUBMITTED
            row.error = ""
            self.deadlines[row.key] = monotonic() + self.max_wait
            print(f"[{row.task_id}] link job submitted: {row.job_id}")
            return True
        except RowError as e:
            self._mark_failed(row, str(e))
            return False
        except Exception as e:
            # the job may or may not have been created; a resume re-checks for
            # both the model and an in-flight job before submitting again
            self._mark_failed(row, format_error(e), prefix="submission failed")
            return False

    def _resolve_row_state(self, row: RowResult) -> bool:
        """Settle a row that is already onboarded or already in flight.

        Returns True when no submission is needed — either because the model
        exists (status becomes skipped_already_linked) or because a link job
        for the task is already queued (status becomes submitted).
        """
        existing_model_id = find_existing_model_id(self.models_client, row)
        if existing_model_id:
            row.status = STATUS_SKIPPED
            row.model_id = existing_model_id
            row.error = ""
            print(
                f"[{row.task_id}] already linked (model {existing_model_id}), skipping"
            )
            return True

        orphan_job_id = find_inflight_link_job(
            self.jobs_client,
            row,
            self.job_scan_cache,
        )
        if orphan_job_id:
            row.job_id = orphan_job_id
            row.status = STATUS_SUBMITTED
            row.error = ""
            self.deadlines[row.key] = monotonic() + self.max_wait
            print(f"[{row.task_id}] already has link job {orphan_job_id} in flight")
            return True
        return False

    # -- polling ----------------------------------------------------------

    def poll(self, row: RowResult) -> bool:
        """Poll one in-flight row. Returns True once it reaches a terminal state."""
        try:
            job = call_with_retry(self.jobs_client.get_job, job_id=row.job_id)
        except Exception as e:
            self.poll_errors[row.key] = self.poll_errors.get(row.key, 0) + 1
            attempts = self.poll_errors[row.key]
            if attempts >= MAX_POLL_ERRORS:
                self._mark_failed(
                    row,
                    f"could not read job {row.job_id} after {attempts} attempts "
                    f"({format_error(e)}); re-run to resume polling",
                    keep_job_id=True,
                )
                return True
            print(f"[{row.task_id}] poll failed ({format_error(e)}), will retry")
            return False

        self.poll_errors.pop(row.key, None)
        if job.state in IN_PROGRESS_JOB_STATES:
            if monotonic() > self.deadlines.get(row.key, float("inf")):
                self._mark_failed(
                    row,
                    f"job {row.job_id} still {_state_name(job.state)} after "
                    f"{int(self.max_wait)}s; re-run to resume polling",
                    keep_job_id=True,
                )
                return True
            return False
        if job.state == JobState.COMPLETED:
            self._mark_linked(row)
        else:
            self._mark_failed(
                row,
                f"job {row.job_id} ended in state {_state_name(job.state)}, "
                f"see the project activity log",
                keep_job_id=True,
            )
        return True

    # -- outcomes ---------------------------------------------------------

    def _mark_linked(self, row: RowResult) -> None:
        row.status = STATUS_LINKED
        row.error = ""
        try:
            row.model_id = find_existing_model_id(self.models_client, row) or ""
        except Exception as e:
            # the link itself succeeded; only the id lookup did not
            row.model_id = ""
            print(
                f"[{row.task_id}] linked, but model lookup failed ({format_error(e)})"
            )
            return
        print(f"[{row.task_id}] linked (model {row.model_id or 'unknown'})")

    def _mark_failed(
        self,
        row: RowResult,
        message: str,
        prefix: str = "",
        keep_job_id: bool = False,
    ) -> None:
        row.status = STATUS_FAILED
        row.error = message
        if not keep_job_id:
            row.job_id = ""
        print(f"[{row.task_id}] {prefix + ': ' if prefix else ''}{message}")

    # -- driver -----------------------------------------------------------

    def run(self, rows: list[RowResult]) -> None:
        """Submit and poll every row, keeping at most max_in_flight jobs open."""
        in_flight = self.reattach(rows)
        attached = {row.key for row in in_flight}
        queued = [
            r for r in rows if r.status not in DONE_STATUSES and r.key not in attached
        ]
        for row in queued:
            # a previous run's failure is retried from the top
            row.status = STATUS_PENDING

        already_done = len(rows) - len(queued) - len(in_flight)
        if already_done:
            print(f"{already_done} row(s) already onboarded, skipping")

        while queued or in_flight:
            while queued and len(in_flight) < self.max_in_flight:
                row = queued.pop(0)
                if self.submit(row):
                    in_flight.append(row)
                self.state.save()
            if not in_flight:
                continue

            sleep(self.poll_interval)
            still_running = []
            settled = False
            for row in in_flight:
                if self.poll(row):
                    settled = True
                else:
                    still_running.append(row)
            in_flight = still_running
            if settled:
                self.state.save()
            if in_flight:
                waiting = f", {len(queued)} queued" if queued else ""
                print(f"{len(in_flight)} job(s) still running{waiting}...")


# ── Output ────────────────────────────────────────────────────────────────────


def default_state_path(csv_path: str) -> str:
    csv_stem = os.path.splitext(os.path.basename(csv_path))[0]
    return os.path.join(STATE_DIR, f"onboarding_state_{csv_stem}.json")


def default_results_path(csv_path: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_stem = os.path.splitext(os.path.basename(csv_path))[0]
    return os.path.join(RESULTS_DIR, f"{csv_stem}_results_{stamp}.csv")


def write_results(rows: list[RowResult], results_path: str) -> None:
    parent = os.path.dirname(results_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(results_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(RESULT_COLUMNS)
        for r in rows:
            writer.writerow([getattr(r, column) for column in RESULT_COLUMNS])
    print(f"Results written to {results_path}")


def summarize(rows: list[RowResult], state_path: str, interrupted: bool) -> int:
    counts = {status: 0 for status in (STATUS_LINKED, STATUS_SKIPPED, STATUS_FAILED)}
    unfinished = 0
    for row in rows:
        if row.status in counts:
            counts[row.status] += 1
        else:
            unfinished += 1

    summary = (
        f"Done: {counts[STATUS_LINKED]} linked, "
        f"{counts[STATUS_SKIPPED]} already linked, "
        f"{counts[STATUS_FAILED]} failed"
    )
    if unfinished:
        summary += f", {unfinished} unfinished"
    print(summary)

    if counts[STATUS_FAILED] or unfinished or interrupted:
        print(
            f"Re-run the same command to resume from {state_path} — "
            f"onboarded rows are skipped and unfinished jobs are re-attached to.",
        )
        return 1
    return 0


# ── Entrypoint ────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--csv-path",
        required=True,
        help="input CSV with task_id/project_id columns",
    )
    parser.add_argument(
        "--results-csv",
        default=None,
        help="output CSV path (default: onboarding_results/<input>_results_<timestamp>.csv)",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="resume checkpoint path (default: onboarding_states/onboarding_state_<input>.json)",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="ignore any existing checkpoint and start the CSV over",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="seconds between job status polls",
    )
    parser.add_argument(
        "--max-in-flight",
        type=int,
        default=10,
        help="maximum link jobs to keep queued at once",
    )
    parser.add_argument(
        "--max-wait",
        type=float,
        default=1800.0,
        help="seconds to wait for a single link job before giving up on it "
        "(0 waits indefinitely)",
    )
    args = parser.parse_args()

    if args.poll_interval <= 0:
        parser.error("--poll-interval must be greater than 0")
    if args.max_in_flight < 1:
        parser.error("--max-in-flight must be at least 1")
    if args.max_wait < 0:
        parser.error("--max-wait cannot be negative")
    return args


def main() -> int:
    args = parse_args()

    if not ARTHUR_API_HOST:
        print("ARTHUR_API_HOST env var is required", file=sys.stderr)
        return 1

    state_path = args.state_file or default_state_path(args.csv_path)
    if args.restart and os.path.exists(state_path):
        os.remove(state_path)
        print(f"Removed existing checkpoint {state_path}")

    try:
        csv_rows = read_rows(args.csv_path)
        state = OnboardingState(state_path, args.csv_path)
    except CsvError as e:
        print(e, file=sys.stderr)
        return 1

    rows = state.merge_csv_rows(csv_rows)
    print(f"Loaded {len(rows)} row(s) from {args.csv_path}")
    if state.loaded:
        print(f"Resuming from checkpoint {state_path}")

    try:
        api_client = build_api_client(ARTHUR_API_HOST)
    except Exception as e:
        print(
            f"Could not authenticate to {ARTHUR_API_HOST}: {format_error(e)}",
            file=sys.stderr,
        )
        return 1

    onboarder = Onboarder(
        state=state,
        connectors_client=ConnectorsV1Api(api_client=api_client),
        models_client=ModelsV1Api(api_client=api_client),
        tasks_client=TasksV1Api(api_client=api_client),
        jobs_client=JobsV1Api(api_client=api_client),
        poll_interval=args.poll_interval,
        max_in_flight=args.max_in_flight,
        max_wait=args.max_wait or float("inf"),
    )

    interrupted = False
    aborted = False
    try:
        onboarder.run(rows)
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted — checkpointing progress")
    except Exception as e:
        # unexpected: still report and checkpoint what was accomplished
        aborted = True
        print(f"\nRun aborted: {format_error(e)}", file=sys.stderr)
    finally:
        # the results file and the checkpoint are the record of what happened,
        # so a failure writing one must not skip the other
        try:
            state.save()
            print(f"Checkpoint saved to {state_path}")
        except Exception as e:
            print(f"Could not write checkpoint: {e}", file=sys.stderr)
        try:
            write_results(
                rows,
                args.results_csv or default_results_path(args.csv_path),
            )
        except Exception as e:
            print(f"Could not write results: {e}", file=sys.stderr)

    exit_code = summarize(rows, state_path, interrupted or aborted)
    return 130 if interrupted else exit_code


if __name__ == "__main__":
    sys.exit(main())
