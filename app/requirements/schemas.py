"""Pydantic 스키마 모음.

두 종류가 있다:
  1. Workspace가 요구사항 실행에 넘기는 입력 계약 — AnalyzeRequest
  2. LLM 구조화 출력 — Assessment / ClarifyOnlyResult 등
LLM 구조화 출력은 graph.py에서 ChatOpenAI.with_structured_output(...) 에 넘겨,
설정된 LLM이 스키마에 맞는 JSON을 반환하도록 강제하는 데 쓴다.
(FR/NFR 분류는 LLM이 아니라 파인튜닝 BERT가 단독 수행한다 → step1 classify.)
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from app.requirements.contracts.request import (
    AnalyzeRequest as AnalyzeRequest,
)
from app.requirements.contracts.request import (
    DeploymentPreferences as DeploymentPreferences,
)
from app.requirements.contracts.request import (
    DeploymentTarget as DeploymentTarget,
)
from app.requirements.contracts.request import (
    FeedbackEdit as FeedbackEdit,
)
from app.requirements.contracts.request import (
    IdentitySourceAnswer as IdentitySourceAnswer,
)
from app.requirements.contracts.request import (
    InitialCloudConstraints as InitialCloudConstraints,
)
from app.requirements.contracts.request import (
    ResourceAnswer as ResourceAnswer,
)

# FR/NFR 라벨 타입 (BERT 매핑과 동일: 0=NFR, 1=FR)
ReqType = Literal["FR", "NFR"]


# ----------------------------------------------------------------------------
# LLM 구조화 출력 스키마
# ----------------------------------------------------------------------------
class ConstraintLink(BaseModel):
    """분리된 품질 제약(NFR)과 그것이 한정하는 기능 요구(FR)의 링크(추적성).

    두 문자열은 refined_requirements에 나온 문장과 (공백 정규화 후) 일치해야 classify가 id로 해소한다.
    """

    constraint: str = Field(
        description="The non-functional / quality constraint sentence — MUST also appear "
        "verbatim in requirementDrafts[].text."
    )
    qualifies: str = Field(
        description="The functional requirement sentence this constraint qualifies — MUST also "
        "appear verbatim in requirementDrafts[].text."
    )


class RefinedRequirementProposal(BaseModel):
    """One refined requirement together with its RAW-input provenance."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    text: str = Field(min_length=1)
    source_refs: list[str] = Field(alias="sourceRefs", min_length=1)


class ExpandedRequirementsResult(BaseModel):
    """Concrete requirement sentences expanded from one initial product idea."""

    model_config = ConfigDict(extra="forbid")

    requirements: list[str] = Field(min_length=1)


class ClarifyOnlyResult(BaseModel):
    """요구사항 구체화 결과 출력."""

    requirement_drafts: list[RefinedRequirementProposal] = Field(
        alias="requirementDrafts",
        default_factory=list,
        description="Refined English requirements bundled directly with their RAW source refs. "
        "Preserve one item per source statement by default; split only independently verifiable "
        "quality constraints. Do not return provenance as a separate list.",
    )
    constraint_links: list[ConstraintLink] = Field(
        default_factory=list,
        description="For EACH quality constraint you split OUT of a compound requirement, one link "
        "mapping the constraint sentence to the functional sentence it qualifies. Both strings MUST "
        "appear verbatim in requirementDrafts[].text. Empty if no constraint was separated.",
    )


# ----------------------------------------------------------------------------
# STEP 2 — 액터/유스케이스 구조화 출력
# ----------------------------------------------------------------------------
UseCaseLevel = Literal["summary", "user_goal", "subfunction"]


class Actor(BaseModel):
    """유스케이스와 상호작용하는 액터(역할). FR에서만 도출한다.

    설계 대상 시스템(SuD)은 경계이지 액터가 아니다. Primary/supporting은
    액터의 고정 속성이 아니라 유스케이스별 역할이다.
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(description="Actor role name, e.g. 'Registered User'.")
    actor_ref: str = Field(min_length=1, alias="actorRef", description="Unique proposal-local actor ref, e.g. ACT1.")
    parent_actor_ref: str | None = Field(default=None, alias="parentActorRef", description="Exact actor ref of the parent role, when supplied.")
    description: str = Field(description="One sentence describing the actor's role.")
    parent_actor: str | None = Field(
        default=None,
        description="If this actor specializes another (e.g. Member specializes Guest), the "
        "parent role name; grounds an actor generalization. Null if none.",
    )
    source_refs: list[str] = Field(
        alias="sourceRefs",
        min_length=1,
        description="Accepted requirement IDs that explicitly support this actor role.",
    )


class UseCase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    """user-goal(EBP) 고도의 유스케이스. FR을 묶고 NFR을 제약으로 참조한다."""

    name: str = Field(description="Active-verb goal phrase, e.g. 'Place an order'.")
    primary_actor_ref: str | None = Field(default=None, alias="primaryActorRef", description="Exact actor ref from the supplied actor catalog.")
    supporting_actor_refs: list[str] = Field(default_factory=list, alias="supportingActorRefs", description="Exact actor refs from the supplied actor catalog.")
    primary_actor: str = Field(
        description="Name of the primary actor (must be one of the given actors)."
    )
    supporting_actors: list[str] = Field(
        default_factory=list,
        description="Names of external actors whose services the system calls while carrying "
        "out this use case. A recipient of system output is not supporting merely because it "
        "receives that output. Use only given actor names. An actor may be primary in one use "
        "case and supporting in another.",
    )
    level: UseCaseLevel = Field(
        default="user_goal",
        description="Cockburn goal altitude; prefer user_goal (sea level, an EBP).",
    )
    goal: str = Field(description="One sentence: what the primary actor wants to achieve.")
    requirement_ids: list[str] = Field(
        default_factory=list,
        description="IDs of the FR requirements this use case covers, including "
        "subfunction-level FRs folded into it (e.g. 'R1','R3'). Use only provided ids. "
        "The main scenario and its extensions are produced later (step 3), not here.",
    )
    nfr_ids: list[str] = Field(
        default_factory=list,
        description="IDs of NFR requirements that constrain this use case.",
    )


class RuleVerdict(BaseModel):
    """규칙 하나에 대한 판정. **어느 규칙을 봤는지 반드시 댄다.**

    처음에는 `is_valid` + 자유문 findings였다. 두 가지가 문제였다.

    1. **근거 없는 지적을 구별할 수 없었다.** 검증자가 지식베이스에 없는 기준을 스스로
       만들어 지적해도 근거 있는 지적과 같은 모양이었다. rule_id를 요구하면 대조할 수
       있다(`app/requirements/knowledge/rules.py`의 `known_ids`).
    2. **"봤는데 깨끗하다"와 "안 봤다"가 같은 값이었다.** 규칙 6개 중 2개만 훑고 깨끗하다고
       답해도 결과가 같다. verification subagent의 알려진 실패 모드(early victory)가 이것이다.
       그래서 규칙마다 한 줄씩 판정을 받고, 빠진 규칙은 세어 저하로 남긴다.

    `is_valid`는 없앴다 — `violated`의 합에서 파생되므로 따로 두면 둘이 어긋날 수 있고,
    실제로 어긋난 응답을 방어하는 코드가 있었다.
    """

    rule_id: str = Field(
        description="The rule id, copied exactly from the rule list.",
    )
    violated: bool = Field(
        description="True only if the artifact actually breaks this rule.",
    )
    directive: str = Field(
        default="",
        description=(
            "When violated, one short imperative repair directive (at most two "
            "sentences). Empty when not violated."
        ),
    )


class Critique(BaseModel):
    """의미 검증자의 판정 한 벌 — 단계와 무관하다(어느 규칙을 보는지는 지식베이스가 정한다).

    예전에는 `SpecCritique`·`RelationshipCritique`로 나뉘어 있었지만 모양이 같았고 다른
    점은 어느 규칙을 보느냐뿐이었다. 그건 이제 프롬프트가 지식베이스에서 조립한다.
    """

    verdicts: list[RuleVerdict] = Field(
        default_factory=list,
        description="One verdict per rule in the rule list, in the same order.",
    )


class ConcernLink(BaseModel):
    """관심사 하나에 대한 링크 판정 — **어느 요구가 이것을 다루는가.**

    `RuleVerdict`와 모양은 닮았지만 묻는 것이 반대다(위반이 아니라 다뤄짐). 닮은 부분은
    의도적이다 — 관심사 id를 반드시 대게 해서 **목록에 없는 관심사를 지어낸 응답**과
    **판정을 빠뜨린 응답**을 둘 다 드러낸다.
    """

    concern_id: str = Field(
        description="The concern id, copied exactly from the concern list.",
    )
    requirement_ids: list[str] = Field(
        default_factory=list,
        description=("Ids of the requirements that address this concern. Empty when none do."),
    )


class ConcernLinkage(BaseModel):
    """관심사 링크 한 벌."""

    links: list[ConcernLink] = Field(
        default_factory=list,
        description="One entry per concern in the concern list, in the same order.",
    )


class DeploymentNeed(BaseModel):
    """One generic deployment capability grounded in existing requirement IDs."""

    model_config = ConfigDict(populate_by_name=True)

    role: str = Field(description="What the deployment must provide and why it is needed.")
    required: bool = Field(description="True when mandatory; false when a preference.")
    requirement_ids: list[str] = Field(
        alias="requirementIds",
        min_length=1,
        description="Exact IDs of requirements supporting this need.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Need-specific details; unresolved questions may use an unresolved list.",
    )
    evidence_spans: list[str] = Field(
        alias="evidenceSpans",
        default_factory=list,
        description="Exact requirement substrings supporting this capability proposal.",
    )
    origin: Literal["explicit", "inferred"] = "inferred"
    dependency_capability_ids: list[str] = Field(
        alias="dependencyCapabilityIds",
        default_factory=list,
        description=(
            "Stable dependency-model capability IDs recognized by the supplied registry; "
            "a recognized ID may still be outside the current generation scope."
        ),
    )


class DeploymentNeedsResult(BaseModel):
    """Dynamic deployment needs keyed by LLM-chosen snake_case identifiers."""

    model_config = ConfigDict(populate_by_name=True)

    deployment_needs: dict[str, DeploymentNeed] = Field(
        alias="deploymentNeeds", default_factory=dict
    )


class CapabilityDecision(BaseModel):
    """One auditable CapabilityContract/v1 decision."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    statement: str
    requirement_ids: list[str] = Field(alias="requirementIds", min_length=1)
    evidence_spans: list[str] = Field(alias="evidenceSpans", default_factory=list)
    origin: Literal["explicit", "inferred"]
    necessity: Literal["required", "preferred"]
    decision: Literal["accepted", "needsQuestion", "abstained"]
    decision_reason: str = Field(alias="decisionReason")
    raw_confidence: float = Field(alias="rawConfidence", ge=0, le=1)
    calibrated_confidence: float | None = Field(
        alias="calibratedConfidence", default=None, ge=0, le=1
    )
    threshold_version: str = Field(alias="thresholdVersion")
    confirmation: Literal["notRequired", "pending", "userConfirmed", "reviewerConfirmed"]
    alternatives: list[str] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(alias="unresolvedFields", default_factory=list)
    dependency_capability_ids: list[str] = Field(
        alias="dependencyCapabilityIds", default_factory=list
    )


class CapabilityContract(BaseModel):
    """Requirement-to-capability boundary consumed by design agents and evaluators."""

    model_config = ConfigDict(populate_by_name=True)

    schema_version: Literal["CapabilityContract/v1"] = Field(
        alias="schemaVersion", default="CapabilityContract/v1"
    )
    capabilities: list[CapabilityDecision] = Field(default_factory=list)
    questions: list[dict[str, str]] = Field(default_factory=list)


class ActorResult(BaseModel):
    """identify_actors 노드의 구조화 출력."""

    actors: list[Actor]


class UseCaseResult(BaseModel):
    """identify_use_cases 노드의 구조화 출력."""

    use_cases: list[UseCase]


# ----------------------------------------------------------------------------
# STEP 3 — 유스케이스 명세(Cockburn 풀 템플릿) 구조화 출력
#
# 설계 원칙(문장/분기/종료를 파싱 없이 구조로 표현):
#  - 모든 문장은 plain black-box 서술. 마크다운/프로토콜/내부컴포넌트 금지.
#  - 주 시나리오 스텝은 step_number(int)로 식별하고 스텝별 요구 추적(covered_req_ids).
#  - 확장은 branch_step(int|null=전역)로 분기 스텝을, outcome(enum)+resume_at_step으로
#    종료 방식(주흐름 복귀/대안성공/실패)을 구조적으로 명시한다.
# ----------------------------------------------------------------------------
Outcome = Literal["resume", "alternate_success", "fail"]


class MainScenarioStep(BaseModel):
    """주 성공 시나리오의 한 스텝."""

    step_number: int = Field(description="Sequential step number starting at 1.")
    subject_ref: str = Field(
        description="Exact accepted actor_ref for the step subject, or 'system' for the system."
    )
    sentence: str = Field(
        description="One plain black-box business action. Subject is a supplied use-case actor "
        "or 'System'. No markdown/bold/asterisks, no UI widgets, no protocols "
        "(HTTP/SQL) or internal components (Server, Database)."
    )
    covered_req_ids: list[str] = Field(
        default_factory=list,
        description="IDs of the FR requirements this step realizes (traceability).",
    )


class Guarantee(BaseModel):
    """성공·실패 뒤 반드시 성립하는 상태와 그 요구사항 근거."""

    model_config = ConfigDict(extra="forbid")

    sentence: str = Field(
        description="One concise, testable postcondition stated in plain business prose."
    )
    covered_req_ids: list[str] = Field(
        default_factory=list,
        description=(
            "IDs of accepted functional requirements realized by this guarantee. "
            "Leave empty when the guarantee only records a constraint."
        ),
    )


class ExtensionHandlingStep(BaseModel):
    """확장 흐름의 처리 스텝."""

    sub_step: str = Field(description="Cockburn hierarchical code, e.g. '3a1', '3a2'.")
    subject_ref: str = Field(
        description="Exact accepted actor_ref for the step subject, or 'system' for the system."
    )
    sentence: str = Field(
        description="One plain black-box action handling the condition. Same style rules "
        "as a main-scenario step; no 'Success!'/'Fail!' tokens in the prose."
    )


class Extension(BaseModel):
    """확장(예외/대안 흐름) 하나. 분기 스텝과 종료 방식을 구조로 표현한다."""

    label: str = Field(description="Cockburn extension label, e.g. '3a' or '*a' (global).")
    branch_step: int | None = Field(
        default=None,
        description="Main-scenario step_number this extension branches from. Null means it "
        "may occur at ANY step (a global extension, label like '*a').",
    )
    condition: str = Field(
        description="The objective exception/alternate state that triggers this extension. "
        "Plain sentence, no trailing colon."
    )
    handling_steps: list[ExtensionHandlingStep] = Field(default_factory=list)
    outcome: Outcome = Field(
        description="How the extension ends: 'resume' = rejoin the main scenario; "
        "'alternate_success' = the use case still succeeds via a different path; "
        "'fail' = the use case aborts without achieving the goal."
    )
    resume_at_step: int | None = Field(
        default=None,
        description="Required IFF outcome == 'resume': the main-scenario step_number to "
        "resume from. Must be null for 'alternate_success' and 'fail'.",
    )


class UseCaseSpec(BaseModel):
    """단일 유스케이스의 Cockburn 스타일 명세 (generate_specs의 UC당 구조화 출력)."""

    preconditions: list[str] = Field(
        default_factory=list,
        description="Verifiable state guaranteed true before start; never re-checked in "
        "steps. Plain sentences, no markdown.",
    )
    trigger: str = Field(description="The business event that starts the use case.")
    main_scenario: list[MainScenarioStep] = Field(
        default_factory=list,
        description="Ordered steps of the main success scenario (the happy path).",
    )
    extensions: list[Extension] = Field(
        default_factory=list,
        description="Exception/alternate flows, each branching from a main-scenario step.",
    )
    success_guarantee: list[Guarantee] = Field(
        default_factory=list,
        description="Typed postconditions guaranteed when the use case succeeds.",
    )
    minimal_guarantee: list[Guarantee] = Field(
        default_factory=list,
        description=(
            "Typed failure-path guarantees directly supported by supplied requirements. "
            "Use an empty list when no such guarantee is supported."
        ),
    )
    public_contract: "PublicBehaviorContract" = Field(
        default_factory=lambda: PublicBehaviorContract(),
        description=(
            "Optional source-grounded projection of public identity obligations and values "
            "obligations needed to complete this goal. Leave lists empty when requirements "
            "establish no such obligation; cite only this use case's functional requirement IDs."
        ),
    )


class SemanticAmbiguityOption(BaseModel):
    """One user-visible consequence of a source-grounded product choice."""

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)
    requested_effect: str = Field(alias="requestedEffect", min_length=1)


class SemanticAmbiguityQuestion(BaseModel):
    """A single unresolved product meaning in one use-case contract.

    This is deliberately not a generic validation finding.  The source evidence
    and the two mutually exclusive outcomes let the Workspace turn it into its
    existing version-pinned Question envelope without trusting a model-selected
    artifact target.
    """

    use_case_id: str = Field(alias="useCaseId", min_length=1)
    source_requirement_ids: list[str] = Field(alias="sourceRequirementIds", min_length=1)
    evidence_spans: list[str] = Field(alias="evidenceSpans", min_length=1)
    prompt: str = Field(min_length=1)
    options: list[SemanticAmbiguityOption] = Field(min_length=2, max_length=2)


class SemanticAmbiguityReview(BaseModel):
    """Selective semantic review: abstain unless a real product choice remains."""

    question: SemanticAmbiguityQuestion | None = None


class IdentityObligation(BaseModel):
    """An identity or delegation condition explicitly required by a functional requirement."""

    model_config = ConfigDict(extra="forbid")

    # These refs are optional in a model proposal. The deterministic normalizer
    # assigns them at first acceptance, where the owning use-case ID is known.
    obligation_ref: str | None = Field(default=None, min_length=1)
    subject_ref: str | None = Field(default=None, min_length=1)
    subject: str = Field(min_length=1, description="Person or party whose identity matters.")
    obligation: Literal["identify", "authenticate", "act_on_behalf"] = Field(
        description=(
            "identify distinguishes the selected subject; authenticate verifies that subject's "
            "identity; act_on_behalf means one subject exercises delegated authority for a "
            "different subject. Authentication alone is not delegation."
        )
    )
    source_authenticate_obligation_index: int | None = Field(
        default=None,
        ge=1,
        description=(
            "For an identify obligation whose linked RequiredValue.source is "
            "authenticated_actor_context, select the exact authenticate obligation by its "
            "1-based position in this proposal's identity_obligations list. Do not set otherwise."
        ),
    )
    requirement_ids: list[str] = Field(
        min_length=1,
        description="Functional requirement IDs that explicitly establish this obligation.",
    )


class RequiredValue(BaseModel):
    """A named information value that a functional requirement says is required."""

    model_config = ConfigDict(extra="forbid")

    # Proposal-only 1-based reference into this proposal's identity_obligations.
    # normalize_specification converts it to the accepted exact obligation_ref.
    identity_obligation_index: int | None = Field(
        default=None, ge=1,
        description=(
            "For an identifier representing an identified subject, select the exact identify "
            "obligation by its "
            "1-based position in this proposal's identity_obligations list. Do not set otherwise."
        ),
    )
    name: str = Field(min_length=1, description="The required public input or value.")
    source: Literal[
        "caller_input", "authenticated_actor_context", "system_result"
    ] = Field(
        description=(
            "Classify where the value comes from according to the requirement, not whether it is "
            "trusted: caller_input means the actor supplies it in the request; "
            "authenticated_actor_context means the requirement says the system obtains it from "
            "the current authenticated session/actor context; system_result means the system "
            "creates or returns it. For example, an identifier explicitly read from the "
            "authenticated session is authenticated_actor_context, while an identifier entered "
            "by the caller is caller_input even if used to select a record."
        )
    )
    value_type: Literal[
        "string", "integer", "number", "boolean", "date", "datetime", "identifier",
        "object", "array", "unknown",
    ] = Field(description="The requirement-grounded value type; use unknown if unspecified.")
    usage: Literal["control", "result", "both"] = Field(
        description="Whether the value controls an operation, is an observable result, or both."
    )
    requirement_ids: list[str] = Field(
        min_length=1,
        description="Functional requirement IDs that explicitly establish this required value.",
    )
    allowed_values: list[StrictStr] | None = Field(
        default=None,
        description=(
            "Optional finite set of explicitly stated alternatives for this value, supported by "
            "the cited requirements or their branches. Omit for open-ended/calculator-style "
            "inputs; never invent examples or derive a list from prose."
        ),
    )

    @field_validator("allowed_values")
    @classmethod
    def validate_allowed_values(cls, values: list[StrictStr] | None) -> list[StrictStr] | None:
        if values is None:
            return None
        if not values or any(not value.strip() for value in values):
            raise ValueError("allowed_values must contain nonempty strings")
        if len({value.strip().casefold() for value in values}) != len(values):
            raise ValueError("allowed_values must contain unique values")
        return values


class PublicBehaviorContract(BaseModel):
    """Compact source-grounded identity and required-value obligations for design closure."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["PublicBehaviorContract/v1"] = "PublicBehaviorContract/v1"
    identity_obligations: list[IdentityObligation] = Field(default_factory=list)
    required_values: list[RequiredValue] = Field(default_factory=list)


# ----------------------------------------------------------------------------
# STEP 4 — 액터/유스케이스 관계(다이어그램용) 구조화 출력
# 관계 식별은 LLM(의미 판단), 다이어그램 텍스트 렌더링은 결정론적으로 분리한다.
# The relationship projection keeps display names for consumers, but every
# relationship join is made through a stable ``use_case_id``.
# ----------------------------------------------------------------------------
class IncludeBaseStepRef(BaseModel):
    """One existing base step selected for an existing-use-case include candidate."""

    model_config = ConfigDict(extra="forbid")

    use_case_id: str = Field(description="Stable ID copied from the candidate base-step options.")
    step_ref: str = Field(description="Exact supplied main:<number> base step reference.")


class IncludeSelection(BaseModel):
    """Semantic decision for one evidence-bounded shared-step candidate."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(description="ID copied exactly from the supplied candidates.")
    decision: Literal["approve", "reject"]
    included_use_case_name: str = Field(
        default="",
        max_length=60,
        description="Concise shared behavior name; required only when approved.",
    )


class ExistingIncludeSelection(BaseModel):
    """Bound selection of an existing reusable use case and its mandatory bases."""

    model_config = ConfigDict(extra="forbid")

    included_use_case_id: str = Field(description="Stable existing target ID copied from options.")
    base_step_refs: list[IncludeBaseStepRef] = Field(
        description="At least two supplied exact mandatory base steps from distinct use cases."
    )


class ExistingIncludeModel(BaseModel):
    """Transient focused output for selecting existing reusable use cases."""

    model_config = ConfigDict(extra="forbid")

    existing_includes: list[ExistingIncludeSelection] = Field(default_factory=list)


class ExtendSelection(BaseModel):
    """One semantic ``extend`` selection inside the supplied use-case/step space."""

    model_config = ConfigDict(extra="forbid")

    base_use_case_id: str = Field(
        description="Stable ID of the existing use case that owns the extension point."
    )
    extending_use_case_id: str = Field(description="Stable ID of the existing optional use case.")
    base_step_ref: str = Field(
        description="Exact supplied base main-scenario step reference, formatted as main:<number>."
    )
    extension_point_name: str = Field(
        min_length=1,
        max_length=60,
        description=(
            "Short natural-language location in the base behavior, such as 'after schedule "
            "presented'; never a code identifier or the extending action name."
        ),
    )
    condition: str = Field(
        min_length=1,
        max_length=120,
        description=(
            "Concise observable condition that activates the optional use case. The diagram "
            "renderer wraps it across lines when needed."
        ),
    )


class RelationshipModel(BaseModel):
    """Bounded semantic choices for shared ``include`` and existing-UC ``extend``."""

    model_config = ConfigDict(extra="forbid")

    includes: list[IncludeSelection] = Field(default_factory=list)
    extends: list[ExtendSelection] = Field(default_factory=list)


# ----------------------------------------------------------------------------
# BASELINE — 다단계 파이프라인의 대조군(순진한 2콜: 명세 원샷 + 다이어그램 원샷)
# 우리 시스템의 "단계 분해 + 검증/반성" 가치를 정량 비교하기 위한 최소 프롬프트 구조.
# ----------------------------------------------------------------------------
class BaselineActor(BaseModel):
    """Baseline actor without the production source-evidence requirement."""

    name: str
    description: str
    parent_actor: str | None = None


class BaselineAssociation(BaseModel):
    """Free-form baseline actor-to-use-case association."""

    actor: str = Field(description="Actor name supplied to the baseline call.")
    use_case: str = Field(description="Use-case name supplied to the baseline call.")


class BaselineIncludeRelation(BaseModel):
    """Free-form baseline include relation, expressed by display names."""

    base_use_case: str
    included_use_case: str
    rationale: str = ""


class BaselineExtendRelation(BaseModel):
    """Free-form baseline extend relation, expressed by display names."""

    base_use_case: str
    extending_use_case: str
    extension_point: str = ""
    rationale: str = ""


class BaselineGeneralizationRelation(BaseModel):
    """Free-form baseline actor or use-case generalization."""

    parent: str
    child: str
    kind: Literal["actor", "use_case"]
    rationale: str = ""


class BaselineDerivedUseCase(BaseModel):
    """A use case introduced freely by the baseline relationship call."""

    name: str
    origin: Literal["factored_include", "promoted_extend"]
    rationale: str = ""


class BaselineRelationshipModel(BaseModel):
    """One-shot baseline relationship output; deliberately not candidate decisions."""

    associations: list[BaselineAssociation] = Field(default_factory=list)
    includes: list[BaselineIncludeRelation] = Field(default_factory=list)
    extends: list[BaselineExtendRelation] = Field(default_factory=list)
    generalizations: list[BaselineGeneralizationRelation] = Field(default_factory=list)
    derived_use_cases: list[BaselineDerivedUseCase] = Field(default_factory=list)


class BaselineSpec(UseCaseSpec):
    """baseline 원샷 명세: UseCaseSpec 필드 + 어느 유스케이스에 속하는지."""

    use_case_name: str = Field(
        description="Name of the use case this specification belongs to; "
        "must exactly match one of the use case names."
    )


class BaselineModelResult(BaseModel):
    """baseline 1콜: 액터 + 유스케이스 + Cockburn 명세를 한 번에 생성."""

    actors: list[BaselineActor]
    use_cases: list[UseCase]
    specs: list[BaselineSpec]


class CoverageVerdict(BaseModel):
    """한 유스케이스가 특정 요구사항을 '실제로' 실현하는지에 대한 의미론적 판정."""

    requirement_id: str = Field(description="The requirement id being judged.")
    realized: bool = Field(
        description="True only if the use case's goal and main scenario genuinely realize "
        "this requirement's intent. False if the coverage claim is unsupported by the behavior "
        "(the model merely asserted it)."
    )
    reason: str = Field(description="One short sentence justifying the verdict.")


class CoverageJudgment(BaseModel):
    """한 유스케이스가 주장한 requirement_ids 각각에 대한 의미론적 커버리지 판정 묶음."""

    verdicts: list[CoverageVerdict]


# ----------------------------------------------------------------------------
# 피드백 기반 재생성 — Workspace가 검증한 구조화 수정 의도
# ----------------------------------------------------------------------------
class FeedbackIntent(BaseModel):
    """검증된 `FeedbackEdit`을 modeling 재생성에 전달하는 결과."""

    stage: Literal["actors", "use_cases", "specs", "relationships"] = Field(
        description="Which pipeline artifact the feedback targets. Feedback about the diagram "
        "maps to 'relationships' (the diagram is rendered from it)."
    )
    scope: Literal["local", "broad"] = Field(
        description="local = concerns specific items named in target_ids; broad = re-derive the "
        "whole stage output."
    )
    target_ids: list[str] = Field(
        default_factory=list,
        description="For local scope, the affected item ids (e.g. ['UC3'] for a use case/spec, "
        "or an actor name). Empty for broad scope.",
    )
    instruction: str = Field(
        description="One concise imperative directive restating what to change."
    )


class CloudConstraintExtraction(BaseModel):
    """One-pass LLM extraction of user-stated cloud constraints.

    Every evidence field must be an exact substring of the input. Missing or ambiguous
    values stay null; validation and normalization happen in code after extraction.
    """

    provider: str | None = None
    provider_evidence: str = ""
    region_as_written: str | None = None
    region_evidence: str = ""
    monthly_budget_amount: float | None = None
    monthly_budget_currency: str | None = None
    monthly_budget_evidence: str = ""
    min_vcpu: int | None = None
    min_vcpu_evidence: str = ""
    min_memory_gib: float | None = None
    min_memory_evidence: str = ""
    traffic_pattern: Literal["steady", "spiky"] | None = None
    traffic_pattern_evidence: str = ""
    scale_value: float | None = None
    scale_unit: Literal["concurrentUsers", "requestsPerSecond"] | None = None
    scale_evidence: str = ""
    data_residency: str | None = None
    data_residency_evidence: str = ""
    ambiguous_fields: list[str] = Field(default_factory=list)
    understanding: str = ""


# `ResourceFieldRead`·`ResourceReading`은 없앴다(2026-07-29). 제약 구조화를 **한 번
# 읽고 끝내는 구조화 출력**에서 도구를 쓰는 에이전트 루프로 바꾸면서, 읽기의 결과는
# 스키마가 아니라 도구 호출(`record_field`)로 들어온다 — 인용 대조는 그 문에서 그대로
# 한다(`resources/service.py`).
