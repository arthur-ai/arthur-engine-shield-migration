import uuid
from datetime import datetime, timezone
from typing import Generator

import pytest

from db_models.inference_models import DatabaseInference, DatabaseInferenceFeedback
from db_models.organization_models import DatabaseOrganization
from db_models.rule_models import DatabaseRule
from db_models.task_models import DatabaseTask
from repositories.organizations_repository import OrganizationsRepository
from tests.clients.base_test_client import (
    GenaiEngineTestClientBase,
    override_get_db_session,
)

# Shield serializes task/rule timestamps as unix milliseconds; inference and
# feedback timestamps are ISO datetimes.
MIGRATED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
MIGRATED_AT_MS = int(MIGRATED_AT.timestamp() * 1000)
MIGRATED_AT_ISO = MIGRATED_AT.isoformat()


def delete_rows(model, *row_ids):
    db = override_get_db_session()
    try:
        for row_id in row_ids:
            db.query(model).filter(model.id == row_id).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture
def migration_org() -> Generator[str, None, None]:
    """Create a real organization and return its id.

    The tasks endpoint validates that the target org exists, so tests need an
    org that lives in the same test DB the route reads from.
    """
    db = override_get_db_session()
    try:
        org = OrganizationsRepository(db).create_organization(
            name=f"migration-test-org-{uuid.uuid4()}",
        )
        org_id = str(org.id)
    finally:
        db.close()

    yield org_id

    db = override_get_db_session()
    try:
        db.query(DatabaseTask).filter(
            DatabaseTask.org_id == uuid.UUID(org_id),
        ).delete()
        db.query(DatabaseOrganization).filter(
            DatabaseOrganization.id == uuid.UUID(org_id),
        ).delete()
        db.commit()
    finally:
        db.close()


# ── Empty payloads short-circuit ─────────────────────────────────────────────


@pytest.mark.unit_tests
def test_bulk_migrate_tasks_empty(
    client: GenaiEngineTestClientBase,
    migration_org: str,
):
    status, body = client.bulk_migrate_tasks(tasks=[], org_id=migration_org)
    assert status == 200
    assert body.tasks == []
    assert str(body.org_id) == migration_org


@pytest.mark.unit_tests
def test_bulk_migrate_rules_empty(client: GenaiEngineTestClientBase):
    status, body = client.bulk_migrate_rules(rules=[])
    assert status == 200
    assert body.rules == []


@pytest.mark.unit_tests
def test_bulk_migrate_task_rule_links_empty(client: GenaiEngineTestClientBase):
    status, body = client.bulk_migrate_task_rule_links(task_to_rule_links=[])
    assert status == 200
    assert body.task_to_rule_links == []


@pytest.mark.unit_tests
def test_bulk_migrate_inferences_empty(
    client: GenaiEngineTestClientBase,
    migration_org: str,
):
    status, body = client.bulk_migrate_inferences(inferences=[], org_id=migration_org)
    assert status == 200
    assert body.inserted == 0
    assert body.skipped == 0
    assert str(body.org_id) == migration_org


@pytest.mark.unit_tests
def test_bulk_migrate_feedback_empty(
    client: GenaiEngineTestClientBase,
    migration_org: str,
):
    status, body = client.bulk_migrate_feedback(feedback=[], org_id=migration_org)
    assert status == 200
    assert body.inserted == 0
    assert body.skipped == 0


# ── Tasks: org validation ────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_bulk_migrate_tasks_unknown_org_returns_400(client: GenaiEngineTestClientBase):
    status, body = client.bulk_migrate_tasks(
        tasks=[
            {
                "id": str(uuid.uuid4()),
                "name": "t",
                "created_at": MIGRATED_AT_MS,
                "updated_at": MIGRATED_AT_MS,
            },
        ],
        org_id=str(uuid.uuid4()),
    )
    assert status == 400
    assert body is None


# ── Happy paths ──────────────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_bulk_migrate_tasks_creates_task(
    client: GenaiEngineTestClientBase,
    migration_org: str,
):
    task_id = str(uuid.uuid4())
    status, body = client.bulk_migrate_tasks(
        tasks=[
            {
                "id": task_id,
                "name": "migrated-task",
                "created_at": MIGRATED_AT_MS,
                "updated_at": MIGRATED_AT_MS,
            },
        ],
        org_id=migration_org,
    )
    assert status == 200
    assert str(body.org_id) == migration_org
    assert len(body.tasks) == 1
    assert body.tasks[0].id == task_id
    assert body.tasks[0].name == "migrated-task"


@pytest.mark.unit_tests
def test_bulk_migrate_tasks_idempotent(
    client: GenaiEngineTestClientBase,
    migration_org: str,
):
    task = {
        "id": str(uuid.uuid4()),
        "name": "dupe-task",
        "created_at": MIGRATED_AT_MS,
        "updated_at": MIGRATED_AT_MS,
    }
    status, first = client.bulk_migrate_tasks(tasks=[task], org_id=migration_org)
    assert status == 200
    assert len(first.tasks) == 1

    # Re-sending the same task is skipped (already exists), not an error.
    status, second = client.bulk_migrate_tasks(tasks=[task], org_id=migration_org)
    assert status == 200
    assert second.tasks == []


@pytest.mark.unit_tests
def test_bulk_migrate_rules_creates_rule(client: GenaiEngineTestClientBase):
    rule_id = str(uuid.uuid4())
    status, body = client.bulk_migrate_rules(
        rules=[
            {
                "id": rule_id,
                "name": "migrated-pii-rule",
                "type": "PIIDataRule",
                "apply_to_prompt": True,
                "apply_to_response": False,
                "scope": "default",
                "created_at": MIGRATED_AT_MS,
                "updated_at": MIGRATED_AT_MS,
                "config": None,
            },
        ],
    )
    assert status == 200
    assert len(body.rules) == 1
    assert body.rules[0].id == rule_id

    delete_rows(DatabaseRule, rule_id)


@pytest.mark.unit_tests
def test_bulk_migrate_rules_dedupes_within_batch(client: GenaiEngineTestClientBase):
    """The same rule ID twice in one request inserts once, not a 500."""
    rule_id = str(uuid.uuid4())
    rule = {
        "id": rule_id,
        "name": "dupe-rule",
        "type": "PIIDataRule",
        "apply_to_prompt": True,
        "apply_to_response": True,
        "scope": "task",
        "created_at": MIGRATED_AT_MS,
        "updated_at": MIGRATED_AT_MS,
        "config": None,
    }
    status, body = client.bulk_migrate_rules(rules=[rule, dict(rule)])
    assert status == 200
    assert len(body.rules) == 1
    assert body.rules[0].id == rule_id

    delete_rows(DatabaseRule, rule_id)


@pytest.mark.unit_tests
def test_bulk_migrate_inferences_inserts_and_skips(
    client: GenaiEngineTestClientBase,
    migration_org: str,
):
    inference = {
        "id": str(uuid.uuid4()),
        "result": "Pass",
        "created_at": MIGRATED_AT_ISO,
        "updated_at": MIGRATED_AT_ISO,
        "task_id": None,
        "conversation_id": None,
        "user_id": None,
        "inference_prompt": None,
        "inference_response": None,
        "inference_feedback": [],
    }
    status, first = client.bulk_migrate_inferences(
        inferences=[inference],
        org_id=migration_org,
    )
    assert status == 200
    assert first.inserted == 1
    assert first.skipped == 0

    # Second run: the inference already exists, so it's skipped.
    status, second = client.bulk_migrate_inferences(
        inferences=[inference],
        org_id=migration_org,
    )
    assert status == 200
    assert second.inserted == 0
    assert second.skipped == 1

    delete_rows(DatabaseInference, inference["id"])


@pytest.mark.unit_tests
def test_bulk_migrate_rules_preserves_archived_flag(
    client: GenaiEngineTestClientBase,
):
    rule_id = str(uuid.uuid4())
    status, body = client.bulk_migrate_rules(
        rules=[
            {
                "id": rule_id,
                "name": "migrated-archived-rule",
                "type": "PIIDataRule",
                "apply_to_prompt": True,
                "apply_to_response": False,
                "scope": "default",
                "created_at": MIGRATED_AT_MS,
                "updated_at": MIGRATED_AT_MS,
                "archived": True,
                "config": None,
            },
        ],
    )
    assert status == 200
    assert len(body.rules) == 1

    db = override_get_db_session()
    try:
        db_rule = db.query(DatabaseRule).filter(DatabaseRule.id == rule_id).one()
        assert db_rule.archived is True
    finally:
        db.close()

    delete_rows(DatabaseRule, rule_id)


@pytest.mark.unit_tests
def test_bulk_migrate_feedback_skips_missing_parent_inference(
    client: GenaiEngineTestClientBase,
    migration_org: str,
):
    inference_id = str(uuid.uuid4())
    status, _ = client.bulk_migrate_inferences(
        inferences=[
            {
                "id": inference_id,
                "result": "Pass",
                "created_at": MIGRATED_AT_ISO,
                "updated_at": MIGRATED_AT_ISO,
                "task_id": None,
                "conversation_id": None,
                "user_id": None,
                "inference_prompt": None,
                "inference_response": None,
                "inference_feedback": [],
            },
        ],
        org_id=migration_org,
    )
    assert status == 200

    def feedback_row(parent_id: str) -> dict:
        return {
            "id": str(uuid.uuid4()),
            "inference_id": parent_id,
            "target": "context",
            "score": 1,
            "reason": None,
            "user_id": None,
            "created_at": MIGRATED_AT_ISO,
            "updated_at": MIGRATED_AT_ISO,
        }

    valid_feedback = feedback_row(inference_id)
    orphan_feedback = feedback_row(str(uuid.uuid4()))

    status, body = client.bulk_migrate_feedback(
        feedback=[valid_feedback, orphan_feedback],
        org_id=migration_org,
    )
    assert status == 200
    assert body.inserted == 1
    assert body.skipped == 1

    delete_rows(DatabaseInferenceFeedback, valid_feedback["id"])
    delete_rows(DatabaseInference, inference_id)
