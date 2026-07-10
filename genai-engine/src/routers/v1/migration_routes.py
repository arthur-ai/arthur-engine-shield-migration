import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from dependencies import get_application_config, get_db_session
from repositories.metrics_repository import MetricRepository
from repositories.migration_repository import MigrationRepository
from repositories.organizations_repository import OrganizationsRepository
from repositories.rules_repository import RuleRepository
from repositories.tasks_repository import TaskRepository
from routers.route_handler import GenaiEngineRoute
from routers.v2 import multi_validator
from schemas.enums import PermissionLevelsEnum
from schemas.internal_schemas import ApplicationConfiguration, User
from schemas.migration_schemas import (
    BulkMigrateFeedbackRequest,
    BulkMigrateFeedbackResponse,
    BulkMigrateInferencesRequest,
    BulkMigrateInferencesResponse,
    BulkMigrateRulesRequest,
    BulkMigrateRulesResponse,
    BulkMigrateTasksRequest,
    BulkMigrateTasksResponse,
    BulkMigrateTaskToRuleLinksRequest,
    BulkMigrateTaskToRuleLinksResponse,
)
from utils.users import permission_checker

logger = logging.getLogger(__name__)

migration_routes = APIRouter(
    prefix="/api/v1",
    route_class=GenaiEngineRoute,
)


@migration_routes.post(
    "/migration/tasks/bulk",
    summary="Bulk migrate tasks",
    description="Bulk migrate tasks",
    response_model=BulkMigrateTasksResponse,
    response_model_exclude_none=True,
    tags=["Migration"],
)
@permission_checker(permissions=PermissionLevelsEnum.MIGRATION_WRITE.value)
def bulk_migrate_tasks(
    request: BulkMigrateTasksRequest,
    db_session: Session = Depends(get_db_session),
    application_config: ApplicationConfiguration = Depends(get_application_config),
    current_user: User | None = Depends(multi_validator.validate_api_multi_auth),
) -> BulkMigrateTasksResponse:
    org_repo = OrganizationsRepository(db_session)
    if org_repo.get_organization_by_id(request.org_id) is None:
        raise HTTPException(
            status_code=400,
            detail=f"Organization {request.org_id} does not exist",
        )

    if len(request.tasks) == 0:
        logger.warning("No tasks to migrate")
        return BulkMigrateTasksResponse(tasks=[], org_id=request.org_id)

    try:
        tasks_repo = TaskRepository(
            db_session,
            RuleRepository(db_session),
            MetricRepository(db_session),
            application_config,
        )

        migration_repo = MigrationRepository(db_session, task_repository=tasks_repo)
        created_tasks = migration_repo.bulk_migrate_tasks(request, request.org_id)
        task_responses = [task._to_response_model() for task in created_tasks]
        return BulkMigrateTasksResponse(tasks=task_responses, org_id=request.org_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@migration_routes.post(
    "/migration/rules/bulk",
    summary="Bulk migrate rules",
    description="Bulk migrate rules",
    response_model=BulkMigrateRulesResponse,
    response_model_exclude_none=True,
    tags=["Migration"],
)
@permission_checker(permissions=PermissionLevelsEnum.MIGRATION_WRITE.value)
def bulk_migrate_rules(
    request: BulkMigrateRulesRequest,
    db_session: Session = Depends(get_db_session),
    current_user: User | None = Depends(multi_validator.validate_api_multi_auth),
) -> BulkMigrateRulesResponse:
    if len(request.rules) == 0:
        logger.warning("No rules to migrate")
        return BulkMigrateRulesResponse(rules=[])

    try:
        rule_repo = RuleRepository(db_session)

        migration_repo = MigrationRepository(db_session, rule_repository=rule_repo)
        created_rules = migration_repo.bulk_migrate_rules(request)
        rule_responses = [rule._to_response_model() for rule in created_rules]
        return BulkMigrateRulesResponse(rules=rule_responses)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@migration_routes.post(
    "/migration/task_rule_links/bulk",
    summary="Bulk migrate task to rule links",
    description="Bulk migrate task to rule links",
    response_model=BulkMigrateTaskToRuleLinksResponse,
    response_model_exclude_none=True,
    tags=["Migration"],
)
@permission_checker(permissions=PermissionLevelsEnum.MIGRATION_WRITE.value)
def bulk_migrate_task_rule_links(
    request: BulkMigrateTaskToRuleLinksRequest,
    db_session: Session = Depends(get_db_session),
    current_user: User | None = Depends(multi_validator.validate_api_multi_auth),
) -> BulkMigrateTaskToRuleLinksResponse:
    if len(request.task_to_rule_links) == 0:
        logger.warning("No task to rule links to migrate")
        return BulkMigrateTaskToRuleLinksResponse(task_to_rule_links=[])

    try:
        migration_repo = MigrationRepository(db_session)
        created_links = migration_repo.bulk_migrate_task_rule_links(request)
        link_responses = [link.to_response_model() for link in created_links]
        return BulkMigrateTaskToRuleLinksResponse(task_to_rule_links=link_responses)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@migration_routes.post(
    "/migration/inferences/bulk",
    summary="Bulk migrate inferences",
    description="Bulk migrate inferences",
    response_model=BulkMigrateInferencesResponse,
    response_model_exclude_none=True,
    tags=["Migration"],
)
@permission_checker(permissions=PermissionLevelsEnum.MIGRATION_WRITE.value)
def bulk_migrate_inferences(
    request: BulkMigrateInferencesRequest,
    db_session: Session = Depends(get_db_session),
    current_user: User | None = Depends(multi_validator.validate_api_multi_auth),
) -> BulkMigrateInferencesResponse:
    if len(request.inferences) == 0:
        logger.warning("No inferences to migrate")
        return BulkMigrateInferencesResponse(
            inserted=0,
            skipped=0,
            org_id=request.org_id,
        )

    try:
        migration_repo = MigrationRepository(db_session)
        inserted, skipped = migration_repo.bulk_migrate_inferences(
            request,
            request.org_id,
        )
        return BulkMigrateInferencesResponse(
            inserted=inserted,
            skipped=skipped,
            org_id=request.org_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@migration_routes.post(
    "/migration/feedback/bulk",
    summary="Bulk migrate feedback",
    description="Bulk migrate feedback",
    response_model=BulkMigrateFeedbackResponse,
    response_model_exclude_none=True,
    tags=["Migration"],
)
@permission_checker(permissions=PermissionLevelsEnum.MIGRATION_WRITE.value)
def bulk_migrate_feedback(
    request: BulkMigrateFeedbackRequest,
    db_session: Session = Depends(get_db_session),
    current_user: User | None = Depends(multi_validator.validate_api_multi_auth),
) -> BulkMigrateFeedbackResponse:
    if len(request.feedback) == 0:
        logger.warning("No feedback to migrate")
        return BulkMigrateFeedbackResponse(inserted=0, skipped=0, org_id=request.org_id)

    try:
        migration_repo = MigrationRepository(db_session)
        inserted, skipped = migration_repo.bulk_migrate_feedback(
            request,
            request.org_id,
        )
        return BulkMigrateFeedbackResponse(
            inserted=inserted,
            skipped=skipped,
            org_id=request.org_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@migration_routes.delete(
    "/migration/tasks/{task_id}/rule_results",
    summary="Delete rule results for a migrated task",
    description="Delete all prompt and response rule results (and their detail rows) for a task",
    tags=["Migration"],
)
@permission_checker(permissions=PermissionLevelsEnum.MIGRATION_WRITE.value)
def delete_migrated_rule_results(
    task_id: str,
    db_session: Session = Depends(get_db_session),
    current_user: User | None = Depends(multi_validator.validate_api_multi_auth),
) -> Response:
    try:
        MigrationRepository(db_session).delete_rule_results_for_task(task_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@migration_routes.delete(
    "/migration/tasks/{task_id}/feedback",
    summary="Delete feedback for a migrated task",
    description="Delete all feedback attached to a task's inferences",
    tags=["Migration"],
)
@permission_checker(permissions=PermissionLevelsEnum.MIGRATION_WRITE.value)
def delete_migrated_feedback(
    task_id: str,
    db_session: Session = Depends(get_db_session),
    current_user: User | None = Depends(multi_validator.validate_api_multi_auth),
) -> Response:
    try:
        MigrationRepository(db_session).delete_feedback_for_task(task_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@migration_routes.delete(
    "/migration/tasks/{task_id}/inferences",
    summary="Delete inferences for a migrated task",
    description="Delete all inferences (and their prompt/response subtree) for a task",
    tags=["Migration"],
)
@permission_checker(permissions=PermissionLevelsEnum.MIGRATION_WRITE.value)
def delete_migrated_inferences(
    task_id: str,
    db_session: Session = Depends(get_db_session),
    current_user: User | None = Depends(multi_validator.validate_api_multi_auth),
) -> Response:
    try:
        MigrationRepository(db_session).delete_inferences_for_task(task_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@migration_routes.delete(
    "/migration/tasks/{task_id}/rules",
    summary="Delete rule links and orphaned rules for a migrated task",
    description="Delete all task-to-rule links for a task, and any rules left unlinked from every task as a result",
    tags=["Migration"],
)
@permission_checker(permissions=PermissionLevelsEnum.MIGRATION_WRITE.value)
def delete_migrated_rules(
    task_id: str,
    db_session: Session = Depends(get_db_session),
    current_user: User | None = Depends(multi_validator.validate_api_multi_auth),
) -> Response:
    try:
        MigrationRepository(db_session).delete_orphaned_rules_for_task(task_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@migration_routes.delete(
    "/migration/tasks/{task_id}",
    summary="Delete a migrated task",
    description="Delete the task row itself. Children must be deleted first.",
    tags=["Migration"],
)
@permission_checker(permissions=PermissionLevelsEnum.MIGRATION_WRITE.value)
def delete_migrated_task(
    task_id: str,
    db_session: Session = Depends(get_db_session),
    current_user: User | None = Depends(multi_validator.validate_api_multi_auth),
) -> Response:
    try:
        MigrationRepository(db_session).delete_migrated_task(task_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@migration_routes.delete(
    "/migration/inferences/{inference_id}",
    summary="Delete a migrated task-less inference",
    description="Delete a single inference (and its full subtree) that has no task",
    tags=["Migration"],
)
@permission_checker(permissions=PermissionLevelsEnum.MIGRATION_WRITE.value)
def delete_migrated_taskless_inference(
    inference_id: str,
    db_session: Session = Depends(get_db_session),
    current_user: User | None = Depends(multi_validator.validate_api_multi_auth),
) -> Response:
    try:
        MigrationRepository(db_session).delete_taskless_inference(inference_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
