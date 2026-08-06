"""Onboard GenAI applications from a CSV of existing engine tasks.

Each row links an existing task to a new model in a project via the link_task
job — the same flow as the UI's "link existing application". The job creates
the traces + guardrails datasets and the model server-side, so no available
dataset upload or list-datasets refresh is required first.

CSV columns (header row required):
    task_id       required  engine task id to link
    project_id    required  scope project to create the model in
    org_id        optional  informational only, echoed in results
    connector_id  optional  engine-internal connector id; auto-resolved from
                            the project when omitted

Auth (env vars):
    ARTHUR_API_HOST       arthur platform base url (e.g. https://platform.arthur.ai)
    ARTHUR_CLIENT_ID      service account client id
    ARTHUR_CLIENT_SECRET  service account client secret

Usage:
    python onboard_tasks_from_csv.py tasks.csv [--results-csv results.csv]
"""

import argparse
import csv
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from time import sleep
from typing import Optional
from dotenv import load_dotenv

from arthur_client.api_bindings import (
    ConnectorsV1Api,
    ConnectorType,
    JobsV1Api,
    JobState,
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

load_dotenv()

TERMINAL_JOB_STATES = {JobState.COMPLETED, JobState.FAILED}
ONBOARDING_ID_PREFIX = "csv-link"

# defaults to genai-engine/onboarding_results
RESULTS_DIR = os.getenv(
    "ONBOARDING_RESULTS_DIR",
    default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "onboarding_results",
    ),
)


ARTHUR_API_HOST = os.environ.get("ARTHUR_API_HOST")
ARTHUR_CLIENT_ID = os.environ.get("ARTHUR_CLIENT_ID")
ARTHUR_CLIENT_SECRET = os.environ.get("ARTHUR_CLIENT_SECRET")

@dataclass
class RowResult:
    task_id: str
    project_id: str
    org_id: str = ""
    connector_id: str = ""
    status: str = "pending"
    job_id: str = ""
    model_id: str = ""
    error: str = ""
    onboarding_identifier: str = field(default="", repr=False)


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


def format_error(e: Exception) -> str:
    if isinstance(e, ApiException):
        return f"{e.status} {e.reason}"
    return str(e)


def read_rows(csv_path: str) -> list[RowResult]:
    rows = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or not {"task_id", "project_id"}.issubset(
            reader.fieldnames
        ):
            raise ValueError("CSV must have a header with task_id and project_id columns")
        for line_num, raw in enumerate(reader, start=2):
            task_id = (raw.get("task_id") or "").strip()
            project_id = (raw.get("project_id") or "").strip()
            if not task_id or not project_id:
                raise ValueError(f"Row {line_num}: task_id and project_id are required")
            rows.append(
                RowResult(
                    task_id=task_id,
                    project_id=project_id,
                    org_id=(raw.get("org_id") or "").strip(),
                    connector_id=(raw.get("connector_id") or "").strip(),
                    onboarding_identifier=f"{ONBOARDING_ID_PREFIX}:{task_id}",
                )
            )
    return rows


def resolve_connector_id(
    connectors_client: ConnectorsV1Api,
    project_id: str,
    cache: dict[str, Optional[str]],
) -> Optional[str]:
    """Engine-internal connector for the project, cached per project."""
    if project_id not in cache:
        resp = connectors_client.get_connectors(
            project_id=project_id,
            connector_type=ConnectorType.ENGINE_INTERNAL,
            page_size=1,
        )
        cache[project_id] = resp.records[0].id if resp.records else None
    return cache[project_id]


def find_existing_model_id(
    models_client: ModelsV1Api,
    row: RowResult,
) -> Optional[str]:
    resp = models_client.get_models(
        project_id=row.project_id,
        onboarding_identifier=row.onboarding_identifier,
        page_size=1,
    )
    return resp.records[0].id if resp.records else None


def submit_rows(
    rows: list[RowResult],
    connectors_client: ConnectorsV1Api,
    models_client: ModelsV1Api,
    tasks_client: TasksV1Api,
) -> None:
    connector_cache: dict[str, Optional[str]] = {}
    for row in rows:
        try:
            # idempotency: a model with this onboarding identifier means the
            # row was already linked by a previous run
            existing_model_id = find_existing_model_id(models_client, row)
            if existing_model_id:
                row.status = "skipped_already_linked"
                row.model_id = existing_model_id
                print(f"[{row.task_id}] already linked (model {existing_model_id}), skipping")
                continue

            connector_id = row.connector_id or resolve_connector_id(
                connectors_client, row.project_id, connector_cache
            )
            if not connector_id:
                row.status = "failed"
                row.error = f"no engine-internal connector in project {row.project_id}"
                print(f"[{row.task_id}] {row.error}")
                continue

            resp = tasks_client.project_create_model_link_task(
                project_id=row.project_id,
                post_link_task_request=PostLinkTaskRequest(
                    task_id=row.task_id,
                    connector_id=connector_id,
                    onboarding_identifier=row.onboarding_identifier,
                ),
            )
            row.job_id = resp.job_id
            row.status = "submitted"
            print(f"[{row.task_id}] link job submitted: {row.job_id}")
        except Exception as e:
            row.status = "failed"
            row.error = format_error(e)
            print(f"[{row.task_id}] submission failed: {row.error}")


def wait_for_jobs(
    rows: list[RowResult],
    jobs_client: JobsV1Api,
    models_client: ModelsV1Api,
    poll_interval: float,
) -> None:
    pending = [r for r in rows if r.status == "submitted"]
    while pending:
        sleep(poll_interval)
        still_pending = []
        for row in pending:
            job = jobs_client.get_job(job_id=row.job_id)
            if job.state not in TERMINAL_JOB_STATES:
                still_pending.append(row)
                continue
            if job.state == JobState.COMPLETED:
                row.status = "linked"
                row.model_id = find_existing_model_id(models_client, row) or ""
                print(f"[{row.task_id}] linked (model {row.model_id or 'unknown'})")
            else:
                row.status = "failed"
                row.error = f"job {row.job_id} ended in state {job.state}, see the project activity log"
                print(f"[{row.task_id}] {row.error}")
        if still_pending:
            print(f"{len(still_pending)} job(s) still running...")
        pending = still_pending


def default_results_path(csv_path: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_stem = os.path.splitext(os.path.basename(csv_path))[0]
    return f"{RESULTS_DIR}/{csv_stem}_results_{stamp}.csv"


def write_results(rows: list[RowResult], results_path: str) -> None:
    parent = os.path.dirname(results_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(results_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["task_id", "project_id", "org_id", "status", "job_id", "model_id", "error"])
        for r in rows:
            writer.writerow([r.task_id, r.project_id, r.org_id, r.status, r.job_id, r.model_id, r.error])
    print(f"Results written to {results_path}")


def main() -> int:
    if not ARTHUR_API_HOST:
        print("ARTHUR_API_HOST env var is required", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv-path", required=True, help="input CSV with task_id/project_id columns")
    parser.add_argument("--results-csv", default=None, help="output CSV path (default: onboarding_results/<input>_results_<timestamp>.csv)")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="seconds between job status polls")
    args = parser.parse_args()


    rows = read_rows(args.csv_path)
    print(f"Loaded {len(rows)} row(s) from {args.csv_path}")

    api_client = build_api_client(ARTHUR_API_HOST)
    connectors_client = ConnectorsV1Api(api_client=api_client)
    models_client = ModelsV1Api(api_client=api_client)
    tasks_client = TasksV1Api(api_client=api_client)
    jobs_client = JobsV1Api(api_client=api_client)

    submit_rows(rows, connectors_client, models_client, tasks_client)
    wait_for_jobs(rows, jobs_client, models_client, args.poll_interval)

    results_path = args.results_csv or default_results_path(args.csv_path)
    write_results(rows, results_path)

    failed = sum(1 for r in rows if r.status == "failed")
    linked = sum(1 for r in rows if r.status == "linked")
    skipped = sum(1 for r in rows if r.status == "skipped_already_linked")
    print(f"Done: {linked} linked, {skipped} already linked, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
