import logging
from typing import Any, Iterator, List, Optional
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from config.config import Config
from db_models.inference_models import (
    DatabaseInference,
    DatabaseInferenceFeedback,
    DatabaseInferencePrompt,
    DatabaseInferencePromptContent,
    DatabaseInferenceResponse,
    DatabaseInferenceResponseContent,
)
from db_models.rule_models import DatabaseRule, DatabaseRuleData
from db_models.rule_result_models import (
    DatabaseHallucinationClaim,
    DatabaseKeywordEntity,
    DatabasePIIEntity,
    DatabasePromptRuleResult,
    DatabaseRegexEntity,
    DatabaseResponseRuleResult,
    DatabaseRuleResultDetail,
    DatabaseToxicityScore,
)
from db_models.task_models import DatabaseTask, DatabaseTaskToRules
from repositories.rules_repository import RuleRepository
from repositories.tasks_repository import TaskRepository
from schemas.internal_schemas import Rule, Task, TaskToRuleLink
from schemas.migration_schemas import (
    BulkMigrateFeedbackRequest,
    BulkMigrateInferencesRequest,
    BulkMigrateRulesRequest,
    BulkMigrateTasksRequest,
    BulkMigrateTaskToRuleLinksRequest,
)

logger = logging.getLogger(__name__)


def chunked(items: list[str]) -> Iterator[list[str]]:
    size = Config.migration_delete_chunk_size()
    for i in range(0, len(items), size):
        yield items[i : i + size]


class MigrationRepository:
    def __init__(
        self,
        db_session: Session,
        task_repository: Optional[TaskRepository] = None,
        rule_repository: Optional[RuleRepository] = None,
    ):
        self.db_session = db_session
        self.task_repository = task_repository
        self.rule_repository = rule_repository

    def bulk_migrate_tasks(
        self,
        shield_tasks: BulkMigrateTasksRequest,
        org_id: UUID,
    ) -> List[Task]:
        if self.task_repository is None:
            raise ValueError("Task repository is not set")

        existing_ids = {
            row[0]
            for row in self.db_session.query(DatabaseTask.id).filter(
                DatabaseTask.id.in_([t.id for t in shield_tasks.tasks]),
            )
        }

        created_tasks = []
        for shield_task in shield_tasks.tasks:
            if shield_task.id in existing_ids:
                logger.info("Skipping task %s, already exists", shield_task.id)
                continue

            existing_ids.add(shield_task.id)
            task = self.task_repository.create_task(
                task=shield_task.to_engine_task(org_id),
                with_default_rules=False,
                commit=False,
            )
            created_tasks.append(task)

        self.db_session.commit()
        return created_tasks

    def bulk_migrate_rules(self, shield_rules: BulkMigrateRulesRequest) -> List[Rule]:
        if self.rule_repository is None:
            raise ValueError("Rule repository is not set")

        existing_ids = {
            row[0]
            for row in self.db_session.query(DatabaseRule.id).filter(
                DatabaseRule.id.in_([r.id for r in shield_rules.rules]),
            )
        }

        created_rules = []
        for shield_rule in shield_rules.rules:
            if shield_rule.id in existing_ids:
                logger.info("Skipping rule %s, already exists", shield_rule.id)
                continue

            existing_ids.add(shield_rule.id)
            rule = self.rule_repository.create_rule(
                rule=shield_rule.to_engine_rule(),
                commit=False,
            )
            created_rules.append(rule)

        self.db_session.commit()
        return created_rules

    def bulk_migrate_task_rule_links(
        self,
        shield_links: BulkMigrateTaskToRuleLinksRequest,
    ) -> List[TaskToRuleLink]:
        existing_pairs = {
            (row[0], row[1])
            for row in self.db_session.query(
                DatabaseTaskToRules.task_id,
                DatabaseTaskToRules.rule_id,
            ).filter(
                DatabaseTaskToRules.task_id.in_(
                    [link.task_id for link in shield_links.task_to_rule_links],
                ),
            )
        }

        db_links = []
        for shield_link in shield_links.task_to_rule_links:
            if (shield_link.task_id, shield_link.rule_id) in existing_pairs:
                logger.info(
                    "Skipping task-rule link (%s, %s), already exists",
                    shield_link.task_id,
                    shield_link.rule_id,
                )
                continue

            existing_pairs.add((shield_link.task_id, shield_link.rule_id))
            db_link = DatabaseTaskToRules(
                task_id=shield_link.task_id,
                rule_id=shield_link.rule_id,
                enabled=shield_link.enabled,
            )
            self.db_session.add(db_link)
            db_links.append(db_link)

        self.db_session.commit()
        return [TaskToRuleLink._from_database_model(db_link) for db_link in db_links]

    def bulk_migrate_inferences(
        self,
        request: BulkMigrateInferencesRequest,
        org_id: UUID,
    ) -> tuple[int, int]:
        existing_ids = {
            row[0]
            for row in self.db_session.query(DatabaseInference.id).filter(
                DatabaseInference.id.in_([inf.id for inf in request.inferences]),
            )
        }

        existing_task_ids = {
            row[0]
            for row in self.db_session.query(DatabaseTask.id).filter(
                DatabaseTask.id.in_(
                    {
                        inf.task_id
                        for inf in request.inferences
                        if inf.task_id is not None
                    },
                ),
            )
        }

        inserted = 0
        skipped = 0
        for shield_inference in request.inferences:
            if shield_inference.id in existing_ids:
                logger.info(
                    "Skipping inference %s, already exists",
                    shield_inference.id,
                )
                skipped += 1
                continue

            # Archived Shield tasks are not migrated (Shield's task search
            # cannot return them), so their inferences are skipped too.
            if (
                shield_inference.task_id is not None
                and shield_inference.task_id not in existing_task_ids
            ):
                logger.warning(
                    "Skipping inference %s, task %s was not migrated",
                    shield_inference.id,
                    shield_inference.task_id,
                )
                skipped += 1
                continue

            existing_ids.add(shield_inference.id)

            self.db_session.add(shield_inference.to_engine_db_model(org_id))
            inserted += 1

        self.db_session.commit()
        return inserted, skipped

    def bulk_migrate_feedback(
        self,
        request: BulkMigrateFeedbackRequest,
        org_id: UUID,
    ) -> tuple[int, int]:
        existing_ids = {
            row[0]
            for row in self.db_session.query(DatabaseInferenceFeedback.id).filter(
                DatabaseInferenceFeedback.id.in_([fb.id for fb in request.feedback]),
            )
        }

        existing_inference_ids = {
            row[0]
            for row in self.db_session.query(DatabaseInference.id).filter(
                DatabaseInference.id.in_(
                    {fb.inference_id for fb in request.feedback},
                ),
            )
        }

        inserted = 0
        skipped = 0
        for shield_feedback in request.feedback:
            if shield_feedback.id in existing_ids:
                logger.info("Skipping feedback %s, already exists", shield_feedback.id)
                skipped += 1
                continue

            if shield_feedback.inference_id not in existing_inference_ids:
                logger.warning(
                    "Skipping feedback %s, parent inference %s does not exist",
                    shield_feedback.id,
                    shield_feedback.inference_id,
                )
                skipped += 1
                continue

            existing_ids.add(shield_feedback.id)

            self.db_session.add(shield_feedback.to_engine_db_model(org_id))
            inserted += 1

        self.db_session.commit()
        return inserted, skipped

    def inference_ids_for_task(self, task_id: str) -> Select[tuple[str]]:
        return select(DatabaseInference.id).where(
            DatabaseInference.task_id == task_id,
        )

    def materialize_ids(
        self,
        ids: list[str] | Select[tuple[str]],
    ) -> list[str]:
        if isinstance(ids, list):
            return ids
        return list(self.db_session.execute(ids).scalars())

    def select_ids_chunked(
        self,
        id_column: InstrumentedAttribute[str],
        filter_column: InstrumentedAttribute[str],
        parent_ids: list[str],
    ) -> list[str]:
        ids: list[str] = []
        for chunk in chunked(parent_ids):
            ids.extend(
                self.db_session.execute(
                    select(id_column).where(filter_column.in_(chunk)),
                ).scalars(),
            )
        return ids

    def delete_chunked(
        self,
        model: type[Any],
        filter_column: InstrumentedAttribute[str],
        ids: list[str],
    ) -> int:
        deleted = 0
        for chunk in chunked(ids):
            deleted += (
                self.db_session.query(model)
                .filter(filter_column.in_(chunk))
                .delete(synchronize_session=False)
            )
        return deleted

    def delete_rule_results_for_inferences(
        self,
        inference_ids: list[str] | Select[tuple[str]],
    ) -> int:
        inference_id_list = self.materialize_ids(inference_ids)
        prompt_ids = self.select_ids_chunked(
            DatabaseInferencePrompt.id,
            DatabaseInferencePrompt.inference_id,
            inference_id_list,
        )
        response_ids = self.select_ids_chunked(
            DatabaseInferenceResponse.id,
            DatabaseInferenceResponse.inference_id,
            inference_id_list,
        )

        prompt_result_ids = self.select_ids_chunked(
            DatabasePromptRuleResult.id,
            DatabasePromptRuleResult.inference_prompt_id,
            prompt_ids,
        )
        response_result_ids = self.select_ids_chunked(
            DatabaseResponseRuleResult.id,
            DatabaseResponseRuleResult.inference_response_id,
            response_ids,
        )

        detail_ids = self.select_ids_chunked(
            DatabaseRuleResultDetail.id,
            DatabaseRuleResultDetail.prompt_rule_result_id,
            prompt_result_ids,
        ) + self.select_ids_chunked(
            DatabaseRuleResultDetail.id,
            DatabaseRuleResultDetail.response_rule_result_id,
            response_result_ids,
        )

        detail_child_models = (
            DatabaseHallucinationClaim,
            DatabasePIIEntity,
            DatabaseKeywordEntity,
            DatabaseRegexEntity,
            DatabaseToxicityScore,
        )
        for detail_child_model in detail_child_models:
            self.delete_chunked(
                detail_child_model,
                detail_child_model.rule_result_detail_id,
                detail_ids,
            )

        self.delete_chunked(
            DatabaseRuleResultDetail,
            DatabaseRuleResultDetail.id,
            detail_ids,
        )

        deleted = self.delete_chunked(
            DatabasePromptRuleResult,
            DatabasePromptRuleResult.id,
            prompt_result_ids,
        )
        deleted += self.delete_chunked(
            DatabaseResponseRuleResult,
            DatabaseResponseRuleResult.id,
            response_result_ids,
        )

        self.db_session.commit()
        return deleted

    def delete_feedback_for_inferences(
        self,
        inference_ids: list[str] | Select[tuple[str]],
    ) -> int:
        deleted = self.delete_chunked(
            DatabaseInferenceFeedback,
            DatabaseInferenceFeedback.inference_id,
            self.materialize_ids(inference_ids),
        )
        self.db_session.commit()
        return deleted

    def delete_inferences_by_id(
        self,
        inference_ids: list[str] | Select[tuple[str]],
    ) -> int:
        inference_id_list = self.materialize_ids(inference_ids)
        prompt_ids = self.select_ids_chunked(
            DatabaseInferencePrompt.id,
            DatabaseInferencePrompt.inference_id,
            inference_id_list,
        )
        response_ids = self.select_ids_chunked(
            DatabaseInferenceResponse.id,
            DatabaseInferenceResponse.inference_id,
            inference_id_list,
        )

        self.delete_chunked(
            DatabaseInferencePromptContent,
            DatabaseInferencePromptContent.inference_prompt_id,
            prompt_ids,
        )
        self.delete_chunked(
            DatabaseInferenceResponseContent,
            DatabaseInferenceResponseContent.inference_response_id,
            response_ids,
        )

        self.delete_chunked(
            DatabaseInferencePrompt,
            DatabaseInferencePrompt.id,
            prompt_ids,
        )
        self.delete_chunked(
            DatabaseInferenceResponse,
            DatabaseInferenceResponse.id,
            response_ids,
        )

        deleted = self.delete_chunked(
            DatabaseInference,
            DatabaseInference.id,
            inference_id_list,
        )
        self.db_session.commit()
        return deleted

    def delete_rule_results_for_task(self, task_id: str) -> int:
        return self.delete_rule_results_for_inferences(
            self.inference_ids_for_task(task_id),
        )

    def delete_feedback_for_task(self, task_id: str) -> int:
        return self.delete_feedback_for_inferences(
            self.inference_ids_for_task(task_id),
        )

    def delete_inferences_for_task(self, task_id: str) -> int:
        return self.delete_inferences_by_id(self.inference_ids_for_task(task_id))

    def delete_taskless_inference(self, inference_id: str) -> int:
        """Delete a single inference (with no task) and its full subtree.

        Removes rule results, feedback, and the inference subtree in FK-safe
        order for one inference ID. Used for inferences migrated with no task,
        which a task-scoped cleanup cannot reach.
        """
        inference_ids = [inference_id]
        self.delete_rule_results_for_inferences(inference_ids)
        self.delete_feedback_for_inferences(inference_ids)
        return self.delete_inferences_by_id(inference_ids)

    def delete_orphaned_rules_for_task(self, task_id: str) -> int:
        linked_rule_ids = [
            row[0]
            for row in self.db_session.query(DatabaseTaskToRules.rule_id).filter(
                DatabaseTaskToRules.task_id == task_id,
            )
        ]

        self.db_session.query(DatabaseTaskToRules).filter(
            DatabaseTaskToRules.task_id == task_id,
        ).delete(synchronize_session=False)

        still_linked_rule_ids = {
            row[0]
            for row in self.db_session.query(DatabaseTaskToRules.rule_id)
            .filter(DatabaseTaskToRules.rule_id.in_(linked_rule_ids))
            .distinct()
        }
        orphaned_rule_ids = [
            rule_id
            for rule_id in linked_rule_ids
            if rule_id not in still_linked_rule_ids
        ]

        self.db_session.query(DatabaseRuleData).filter(
            DatabaseRuleData.rule_id.in_(orphaned_rule_ids),
        ).delete(synchronize_session=False)

        deleted = (
            self.db_session.query(DatabaseRule)
            .filter(
                DatabaseRule.id.in_(orphaned_rule_ids),
            )
            .delete(synchronize_session=False)
        )
        self.db_session.commit()
        return deleted

    def delete_migrated_task(self, task_id: str) -> int:
        deleted = (
            self.db_session.query(DatabaseTask)
            .filter(
                DatabaseTask.id == task_id,
            )
            .delete(synchronize_session=False)
        )
        self.db_session.commit()
        return deleted
