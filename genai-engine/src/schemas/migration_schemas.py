from datetime import datetime
from enum import Enum
from typing import List, Optional
from uuid import UUID

from arthur_common.models.common_schemas import (
    ExamplesConfig,
    KeywordsConfig,
    PIIConfig,
    RegexConfig,
    ToxicityConfig,
)
from arthur_common.models.enums import (
    InferenceFeedbackTarget,
    RuleResultEnum,
    RuleType,
    ToxicityViolationType,
)
from arthur_common.models.response_schemas import RuleResponse, TaskResponse
from pydantic import BaseModel, Field

from db_models.inference_models import (
    DatabaseInference,
    DatabaseInferenceFeedback,
    DatabaseInferencePrompt,
    DatabaseInferencePromptContent,
    DatabaseInferenceResponse,
    DatabaseInferenceResponseContent,
)
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
from schemas.enums import RuleScoringMethod
from schemas.internal_schemas import Rule, RuleData, RuleScope, Task
from schemas.response_schemas import TaskToRuleLinkResponse
from schemas.rules_schema_utils import CONFIG_CHECKERS


class ShieldRuleType(str, Enum):
    KEYWORD = "KeywordRule"
    MODEL_HALLUCINATION = "ModelHallucinationRule"
    MODEL_HALLUCINATION_V2 = "ModelHallucinationRuleV2"
    MODEL_HALLUCINATION_V3 = "ModelHallucinationRuleV3"
    MODEL_SENSITIVE_DATA = "ModelSensitiveDataRule"
    PII_DATA = "PIIDataRule"
    PROMPT_INJECTION = "PromptInjectionRule"
    REGEX = "RegexRule"
    TOXICITY = "ToxicityRule"


class ShieldTask(BaseModel):
    """A task as returned by the Shield /tasks/search endpoint.

    Shield serializes task timestamps as unix milliseconds
    (see Task._to_response_model / _serialize_datetime), so created_at and
    updated_at are ints here, not datetimes. to_engine_task() converts them.
    """

    id: str
    name: str
    # Shield emits timestamps as unix milliseconds.
    created_at: int
    updated_at: int

    def to_engine_task(self, org_id: UUID) -> Task:
        return Task(
            id=self.id,
            name=self.name,
            created_at=datetime.utcfromtimestamp(self.created_at / 1000),
            updated_at=datetime.utcfromtimestamp(self.updated_at / 1000),
            org_id=org_id,
        )


class ShieldRule(BaseModel):
    """A rule as returned by the Shield /default_rules and /rules/search
    endpoints (Shield's RuleResponse).

    Shield serializes rules via Rule._to_response_model: apply_to_prompt/
    apply_to_response flags, unix-millisecond timestamps, and an optional
    nested typed config object. to_engine_rule() converts this into the engine
    Rule, deriving rule_data from config the same way the engine does for a
    NewRuleRequest.
    """

    id: str
    name: str
    type: ShieldRuleType
    apply_to_prompt: bool
    apply_to_response: bool
    scope: RuleScope
    # Shield emits timestamps as unix milliseconds.
    created_at: int
    updated_at: int
    archived: bool = False
    config: Optional[
        KeywordsConfig | RegexConfig | ExamplesConfig | ToxicityConfig | PIIConfig
    ] = None

    def to_engine_rule(self) -> Rule:
        if (
            self.type == ShieldRuleType.MODEL_HALLUCINATION
            or self.type == ShieldRuleType.MODEL_HALLUCINATION_V3
        ):
            rule_type = RuleType.MODEL_HALLUCINATION_V2
        else:
            rule_type = RuleType(self.type)

        rule_data: List[RuleData] = []
        if self.config is not None:
            rule_data = CONFIG_CHECKERS[rule_type.value](self.config)

        return Rule(
            id=self.id,
            name=self.name,
            type=rule_type,
            prompt_enabled=self.apply_to_prompt,
            response_enabled=self.apply_to_response,
            scoring_method=RuleScoringMethod.BINARY,
            created_at=datetime.utcfromtimestamp(self.created_at / 1000),
            updated_at=datetime.utcfromtimestamp(self.updated_at / 1000),
            rule_data=rule_data,
            scope=self.scope,
            archived=self.archived,
        )


class ShieldTaskToRuleLink(BaseModel):
    task_id: str
    rule_id: str
    enabled: bool


class BulkMigrateTasksRequest(BaseModel):
    tasks: List[ShieldTask] = Field(..., description="The tasks to migrate")
    org_id: UUID = Field(..., description="The org ID to migrate the tasks to")


class BulkMigrateTasksResponse(BaseModel):
    tasks: List[TaskResponse] = Field(..., description="The tasks that were migrated")
    org_id: UUID = Field(..., description="The org ID that the tasks were migrated to")


class BulkMigrateRulesRequest(BaseModel):
    rules: List[ShieldRule] = Field(..., description="The rules to migrate")


class BulkMigrateRulesResponse(BaseModel):
    rules: List[RuleResponse] = Field(..., description="The rules that were migrated")


class BulkMigrateTaskToRuleLinksRequest(BaseModel):
    task_to_rule_links: List[ShieldTaskToRuleLink] = Field(
        ...,
        description="The task to rule links to migrate",
    )


class BulkMigrateTaskToRuleLinksResponse(BaseModel):
    task_to_rule_links: List[TaskToRuleLinkResponse] = Field(
        ...,
        description="The task to rule links that were migrated",
    )


class ShieldHallucinationClaim(BaseModel):
    id: str
    claim: str
    valid: bool
    reason: str
    order_number: int

    def to_engine_db_model(
        self,
        rule_result_detail_id: str,
        org_id: UUID,
    ) -> DatabaseHallucinationClaim:
        return DatabaseHallucinationClaim(
            id=self.id,
            rule_result_detail_id=rule_result_detail_id,
            claim=self.claim,
            valid=self.valid,
            reason=self.reason,
            order_number=self.order_number,
            org_id=org_id,
        )


class ShieldPIIEntity(BaseModel):
    id: str
    entity: str
    span: str
    confidence: Optional[float] = None

    def to_engine_db_model(
        self,
        rule_result_detail_id: str,
        org_id: UUID,
    ) -> DatabasePIIEntity:
        return DatabasePIIEntity(
            id=self.id,
            rule_result_detail_id=rule_result_detail_id,
            entity=self.entity,
            span=self.span,
            confidence=self.confidence,
            org_id=org_id,
        )


class ShieldKeywordMatch(BaseModel):
    id: str
    keyword: str

    def to_engine_db_model(
        self,
        rule_result_detail_id: str,
        org_id: UUID,
    ) -> DatabaseKeywordEntity:
        return DatabaseKeywordEntity(
            id=self.id,
            rule_result_detail_id=rule_result_detail_id,
            keyword=self.keyword,
            org_id=org_id,
        )


class ShieldRegexMatch(BaseModel):
    id: str
    matching_text: str
    pattern: Optional[str] = None

    def to_engine_db_model(
        self,
        rule_result_detail_id: str,
        org_id: UUID,
    ) -> DatabaseRegexEntity:
        return DatabaseRegexEntity(
            id=self.id,
            rule_result_detail_id=rule_result_detail_id,
            matching_text=self.matching_text,
            pattern=self.pattern,
            org_id=org_id,
        )


class ShieldToxicityScore(BaseModel):
    id: str
    toxicity_score: float
    toxicity_violation_type: ToxicityViolationType

    def to_engine_db_model(
        self,
        rule_result_detail_id: str,
        org_id: UUID,
    ) -> DatabaseToxicityScore:
        return DatabaseToxicityScore(
            id=self.id,
            rule_result_detail_id=rule_result_detail_id,
            toxicity_score=self.toxicity_score,
            toxicity_violation_type=self.toxicity_violation_type.value,
            org_id=org_id,
        )


class ShieldRuleDetails(BaseModel):
    id: str
    score: Optional[bool] = None
    message: Optional[str] = None
    claims: List[ShieldHallucinationClaim] = []
    pii_entities: List[ShieldPIIEntity] = []
    keyword_matches: List[ShieldKeywordMatch] = []
    regex_matches: List[ShieldRegexMatch] = []
    toxicity_score: Optional[ShieldToxicityScore] = None

    def to_engine_db_model(self, org_id: UUID) -> DatabaseRuleResultDetail:
        return DatabaseRuleResultDetail(
            id=self.id,
            score=self.score,
            message=self.message,
            org_id=org_id,
            claims=[claim.to_engine_db_model(self.id, org_id) for claim in self.claims],
            pii_entities=[
                entity.to_engine_db_model(self.id, org_id)
                for entity in self.pii_entities
            ],
            keyword_matches=[
                match.to_engine_db_model(self.id, org_id)
                for match in self.keyword_matches
            ],
            regex_matches=[
                match.to_engine_db_model(self.id, org_id)
                for match in self.regex_matches
            ],
            toxicity_score=(
                self.toxicity_score.to_engine_db_model(self.id, org_id)
                if self.toxicity_score
                else None
            ),
        )


class ShieldInferenceRule(BaseModel):
    id: str
    name: str
    type: RuleType
    scope: RuleScope


class ShieldInferenceRuleResult(BaseModel):
    id: str
    rule: ShieldInferenceRule
    rule_result: RuleResultEnum
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    created_at: datetime
    updated_at: datetime
    rule_details: Optional[ShieldRuleDetails] = None

    def to_engine_prompt_db_model(
        self,
        inference_prompt_id: str,
        org_id: UUID,
    ) -> DatabasePromptRuleResult:
        return DatabasePromptRuleResult(
            id=self.id,
            inference_prompt_id=inference_prompt_id,
            rule_id=self.rule.id,
            rule_result=self.rule_result.value,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            latency_ms=self.latency_ms,
            created_at=self.created_at,
            updated_at=self.updated_at,
            org_id=org_id,
            rule_details=(
                self.rule_details.to_engine_db_model(org_id)
                if self.rule_details
                else None
            ),
        )

    def to_engine_response_db_model(
        self,
        inference_response_id: str,
        org_id: UUID,
    ) -> DatabaseResponseRuleResult:
        return DatabaseResponseRuleResult(
            id=self.id,
            inference_response_id=inference_response_id,
            rule_id=self.rule.id,
            rule_result=self.rule_result.value,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            latency_ms=self.latency_ms,
            created_at=self.created_at,
            updated_at=self.updated_at,
            org_id=org_id,
            rule_details=(
                self.rule_details.to_engine_db_model(org_id)
                if self.rule_details
                else None
            ),
        )


class ShieldInferencePrompt(BaseModel):
    id: str
    inference_id: str
    result: RuleResultEnum
    created_at: datetime
    updated_at: datetime
    message: Optional[str] = None
    tokens: Optional[int] = None
    rule_results: List[ShieldInferenceRuleResult] = []

    def to_engine_db_model(self, org_id: UUID) -> DatabaseInferencePrompt:
        return DatabaseInferencePrompt(
            id=self.id,
            inference_id=self.inference_id,
            result=self.result.value,
            created_at=self.created_at,
            updated_at=self.updated_at,
            tokens=self.tokens,
            content=DatabaseInferencePromptContent(
                inference_prompt_id=self.id,
                content=self.message,
            ),
            prompt_rule_results=[
                rule_result.to_engine_prompt_db_model(self.id, org_id)
                for rule_result in self.rule_results
            ],
        )


class ShieldInferenceResponse(BaseModel):
    id: str
    inference_id: str
    result: RuleResultEnum
    created_at: datetime
    updated_at: datetime
    message: Optional[str] = None
    context: Optional[str] = None
    tokens: Optional[int] = None
    rule_results: List[ShieldInferenceRuleResult] = []

    def to_engine_db_model(self, org_id: UUID) -> DatabaseInferenceResponse:
        return DatabaseInferenceResponse(
            id=self.id,
            inference_id=self.inference_id,
            result=self.result.value,
            created_at=self.created_at,
            updated_at=self.updated_at,
            tokens=self.tokens,
            content=DatabaseInferenceResponseContent(
                inference_response_id=self.id,
                content=self.message,
                context=self.context,
            ),
            response_rule_results=[
                rule_result.to_engine_response_db_model(self.id, org_id)
                for rule_result in self.rule_results
            ],
        )


class ShieldInferenceFeedback(BaseModel):
    id: str
    inference_id: str
    target: InferenceFeedbackTarget
    score: int
    reason: Optional[str] = None
    user_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    def to_engine_db_model(self, org_id: UUID) -> DatabaseInferenceFeedback:
        return DatabaseInferenceFeedback(
            id=self.id,
            inference_id=self.inference_id,
            target=self.target.value,
            score=self.score,
            reason=self.reason,
            user_id=self.user_id,
            created_at=self.created_at,
            updated_at=self.updated_at,
            org_id=org_id,
        )


class ShieldInference(BaseModel):
    id: str
    result: RuleResultEnum
    created_at: datetime
    updated_at: datetime
    task_id: Optional[str] = None
    conversation_id: Optional[str] = None
    user_id: Optional[str] = None
    inference_prompt: Optional[ShieldInferencePrompt] = None
    inference_response: Optional[ShieldInferenceResponse] = None
    inference_feedback: List[ShieldInferenceFeedback] = []

    def to_engine_db_model(self, org_id: UUID) -> DatabaseInference:
        return DatabaseInference(
            id=self.id,
            result=self.result.value,
            created_at=self.created_at,
            updated_at=self.updated_at,
            task_id=self.task_id,
            conversation_id=self.conversation_id,
            user_id=self.user_id,
            inference_prompt=(
                self.inference_prompt.to_engine_db_model(org_id)
                if self.inference_prompt
                else None
            ),
            inference_response=(
                self.inference_response.to_engine_db_model(org_id)
                if self.inference_response
                else None
            ),
        )


class BulkMigrateInferencesRequest(BaseModel):
    inferences: List[ShieldInference] = Field(
        ...,
        description="The inferences to migrate",
    )
    org_id: UUID = Field(..., description="The org ID to migrate the inferences to")


class BulkMigrateInferencesResponse(BaseModel):
    inserted: int = Field(
        ...,
        description="The number of inferences that were inserted",
    )
    skipped: int = Field(
        ...,
        description="The number of inferences that already existed and were skipped",
    )
    org_id: UUID = Field(
        ...,
        description="The org ID that the inferences were migrated to",
    )


class BulkMigrateFeedbackRequest(BaseModel):
    feedback: List[ShieldInferenceFeedback] = Field(
        ...,
        description="The feedback to migrate",
    )
    org_id: UUID = Field(..., description="The org ID to migrate the feedback to")


class BulkMigrateFeedbackResponse(BaseModel):
    inserted: int = Field(
        ...,
        description="The number of feedback items that were inserted",
    )
    skipped: int = Field(
        ...,
        description="The number of feedback items that were skipped because they "
        "already existed or their parent inference was not migrated",
    )
    org_id: UUID = Field(
        ...,
        description="The org ID that the feedback was migrated to",
    )
