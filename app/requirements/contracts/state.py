"""요구사항 분석 단계가 공유하는 canonical graph state 계약이다.

모든 노드가 공유하는 단일 상태. 마일스톤1 필드 + 2~4단계 필드가 함께 있어,
단계가 늘어도 State는 이 파일 한 곳에서 확장한다.
"""

from __future__ import annotations

from typing import Annotated, Literal, NotRequired

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class RequirementItem(TypedDict):
    """분류된 개별 요구사항 (BERT 단독 분류 결과).

    id는 유형+순번(FR1, NFR2 …) 형식. 분류는 파인튜닝 BERT가 단독 수행한다.
    """

    id: str
    text: str
    type: Literal["FR", "NFR"]
    # Stable refinement identity and the RAW inputs that produced this item.
    # ``id`` is RR1..N for raw analysis; type is an independent BERT label.
    draft_ref: NotRequired[str]
    source_refs: NotRequired[list[str]]
    # Phase 2(RTM): 이 NFR이 한정하는(qualify) FR id들. clarify가 복합요구에서 분리해낸
    # 제약이면 classify가 부모 FR id로 채운다(NFR에만, 없으면 부재). 추적성 링크.
    qualifies: NotRequired[list[str]]


class ActorItem(TypedDict):
    actor_ref: str
    """도출된 액터(역할). FR에서만 도출한다. SuD(설계 대상 시스템)는 액터가 아님."""

    name: str
    description: str
    parent_actor: str | None  # 일반화(상속) 부모 액터, 없으면 None
    parent_actor_ref: NotRequired[str | None]
    source_refs: list[str]  # Accepted requirement IDs supporting this actor role.


class UseCaseItem(TypedDict):
    primary_actor_ref: str
    supporting_actor_refs: list[str]
    """도출된 유스케이스 (user-goal 고도). FR 추적성·NFR 제약을 담는다."""

    id: str
    name: str
    primary_actor: str
    supporting_actors: list[str]
    level: Literal["summary", "user_goal", "subfunction"]
    goal: str
    requirement_ids: list[str]  # 커버하는 FR id (서브펑션 흡수 포함, 추적성)
    nfr_ids: list[str]  # 이 UC를 한정하는 NFR id
    # 주 시나리오/확장(예외·대안)은 step3에서 생성한다 (여기서 만들지 않음).


class GuaranteeItem(TypedDict):
    """명세 보장 문장과 이를 실현하는 요구사항 ID."""

    sentence: str
    covered_req_ids: list[str]


class IdentityObligationItem(TypedDict):
    obligation_ref: str
    subject_ref: str
    subject: str
    obligation: Literal["identify", "authenticate", "act_on_behalf"]
    identity_source_kind: Literal[
        "caller_input", "authenticated_context", "system_result", "unresolved"
    ]
    source_authenticate_obligation_ref: NotRequired[str | None]
    requirement_ids: list[str]


class RequiredValueItem(TypedDict):
    value_ref: str
    name: str
    source: Literal["caller_input", "authenticated_actor_context", "system_result"]
    value_type: Literal[
        "string", "integer", "number", "boolean", "date", "datetime", "identifier",
        "object", "array", "unknown",
    ]
    usage: Literal["control", "result", "both"]
    requirement_ids: list[str]
    allowed_values: NotRequired[list[str]]
    identity_obligation_ref: NotRequired[str]


class PublicBehaviorContractItem(TypedDict):
    schema_version: Literal["PublicBehaviorContract/v1"]
    identity_obligations: list[IdentityObligationItem]
    required_values: list[RequiredValueItem]


class UseCaseSpecItem(TypedDict):
    """유스케이스 명세 (step3 산출, Cockburn 풀 템플릿)."""

    use_case_id: str
    name: str
    requirement_ids: list[str]
    nfr_ids: list[str]
    preconditions: list[str]
    trigger: str
    main_scenario: list[dict]  # {step_number, sentence, covered_req_ids}
    extensions: list[
        dict
    ]  # {label, branch_step, condition, handling_steps, outcome, resume_at_step}
    success_guarantee: list[GuaranteeItem]
    minimal_guarantee: list[GuaranteeItem]
    issues: list[str]  # 검증 위반(정적+의미). reflection 루프 후 남은 것.
    repair_iters: int  # 반성 루프에서 재생성한 횟수
    # 의미 검증(LLM)을 실제로 거쳤는지.
    # "ok"|"disabled"|"failed"|"ungrounded"(지식베이스에 없는 규칙만 인용해 버렸다)|"pending".
    # issues가 비었다는 것만으로는 "깨끗함"과 "확인 못 함"을 구별할 수 없어서 둔다.
    # 예전 spec item과 섞일 수 있으므로 NotRequired.
    public_contract: NotRequired[PublicBehaviorContractItem]
    semantic_status: NotRequired[str]
    # 생성이 성공했는지. False면 이 항목은 자리만 지키는 빈 명세다(형제를 살리려고
    # 남긴다 — 목록에서 빼면 산출물에서 조용히 사라진다). 없으면 성공한 것으로 본다.
    generated: NotRequired[bool]
    # 반성 루프가 멈춘 이유: clean|stalled|waiting_external|error|not_generated.
    repair_stopped: NotRequired[str]
    # 수리 시도·전략·후보 digest를 보존하는 RepairHistory/v1 객체.
    repair_history: NotRequired[dict]


class AgentState(TypedDict):
    """요구사항 PIPELINE 단계가 공유하는 current 실행 상태 계약이다."""

    messages: Annotated[list, add_messages]
    raw_requirements: list[str]
    # Expansion is a working set only.  Never overwrite raw_requirements: RAW
    # identifiers always denote the user's submitted statements.
    expanded_requirements: NotRequired[list[str]]
    expanded_source_refs: NotRequired[list[list[str]]]
    refined_requirements: list[str]
    # Traceable clarification proposals.  Each item is {ref, text, sourceRefs}
    # where ref is stable RR1..N and sourceRefs contains RAW1..N identifiers.
    requirement_drafts: NotRequired[list[dict]]
    requirement_source_issues: NotRequired[list[str]]
    # Phase 2(RTM): clarify가 분리한 (constraint 문장 → qualify하는 functional 문장) 링크.
    # classify가 이 문장쌍을 id로 해소해 RequirementItem.qualifies를 채운다.
    constraint_links: NotRequired[list[dict]]
    classified: list[RequirementItem]
    phase: str
    # 요구사항에서 도출한 제네릭 배포 필요사항. 구체 클라우드 리소스 선택은 설계 책임이다.
    # 각 need는 기존 요구사항 ID를 참조해 근거를 추적한다.
    deployment_needs: NotRequired[dict]
    # 감사 가능한 selective-prediction 계약이다. ``deployment_needs``는 현재 요구사항·구현
    # 소비자가 사용하는 projection이고, 이 필드는 판정 근거와 보정값까지 보존한다.
    capability_contract: NotRequired[dict]
    # 사람이 확인한 capability id → accepted|abstained. LLM 수리 결과가 아니라
    # 선택형 질문에 대한 명시적 제품 결정이며 계약 감사 기록으로 보존한다.
    capability_answers: NotRequired[dict[str, str]]
    # 사용자가 쓴 클라우드 제약 원문(`apps.resource_constraints_text`). 요구사항 문장과
    # **따로** 받는다 — 실측상 provider·region·예산은 요구사항 산문에 아예 없고(0건),
    # 없는 곳을 뒤지면 오탐만 남는다(`resources/service.py`).
    resource_constraints_text: NotRequired[str]
    # 최초 화면의 구조화 입력. provider·region·월 예산은 이 값을 결정론적으로
    # 정규화하고, 자유문장 제약은 보조 추출 경로로만 사용한다.
    initial_cloud_constraints: NotRequired[dict]
    # capability 분석과 병렬로 만든 자유문장 제약 해석. 질문과 계약 확정은
    # build_resource_spec이 담당하며 이 중간 객체는 사용자 산출물이 아니다.
    resource_constraint_extraction: NotRequired[dict]
    # 되묻기의 답: 계약 칸 이름 → 사용자가 쓴 문자열. **값이 아니라 답이다** — 제약
    # 구조화 에이전트가 산문과 같은 규율로 해석한다("서울"은 여전히 카탈로그를 거쳐
    # 코드로 풀려야 하고, 후보가 여럿이면 여전히 모호하다).
    resource_answers: NotRequired[dict[str, str]]
    # 자유문장 resource 답변. 질문 field와 원문을 함께 보존해 제약 추출 모델이 복수
    # 필드를 다시 해석할 수 있게 한다. direct answers와 섞지 않는다.
    resource_free_text_answers: NotRequired[list[dict[str, str]]]
    # 제약 구조화의 작업 기록: 초안·질문·근거·버린 후보(`resources/service.py`).
    # 계약을 만족하지 못해도 **여기는 늘 존재한다** — 왜 못 채웠는지가 사라지면 안 된다.
    resource_intake: NotRequired[dict]
    # `RESOURCE_SPEC` 계약 산출물. **계약을 만족할 때만 존재한다.** 반쯤 채운 사양을
    # 내보내면 뒤 단계(배포 구성)가 그것을 사양으로 알고 조인을 돌린다.
    resource_spec: NotRequired[dict]
    # 2단계 — 액터/유스케이스 도출 + FR 커버리지 점검
    actors: list[ActorItem]
    use_cases: list[UseCaseItem]
    # 요구사항 분류와 독립적인 RTM 제약 간선: requirement id -> 적용 UC ids.
    # `requirement_ids`는 실현 주장이고, 이 맵은 기능형 정책/불변조건까지 포함한 제약이다.
    constraint_applicability: NotRequired[dict[str, list[str]]]
    coverage: dict  # check_coverage의 결정론적 커버리지 결과
    # 요구사항 id 중심 RTM. actor/UC/capability 근거와 realizes/constrains를 구분한다.
    traceability: NotRequired[dict]
    # review_model(독립 의미 검증자)의 판정. {issues, semantic_status, unexamined_rules}.
    # 커버리지와 나란히 두는 이유: 하나는 "빠진 게 없나"(결정론), 다른 하나는 "모델이
    # 규칙을 지켰나"(의미)이고 둘은 서로를 대신하지 못한다.
    model_review: NotRequired[dict]
    # 3단계 — 유스케이스별 명세(병렬 생성) + 검증 요약
    use_case_specs: list[UseCaseSpecItem]
    spec_report: dict  # check_specs의 명세 검증 집계
    # A single source-grounded product ambiguity discovered after deterministic
    # specification checks.  It is rendered as the existing Workspace Question
    # envelope there, because only the Workspace owns catalog versions.
    semantic_ambiguity_question: NotRequired[dict]
    semantic_ambiguity_questioned: NotRequired[bool]
    # Deterministic typed identity-source question and user-selected source overrides,
    # keyed by minted obligation_ref so local specification regeneration preserves answers.
    identity_source_question: NotRequired[dict | None]
    identity_source_overrides: NotRequired[dict[str, dict[str, str]]]
    # 4단계 — 관계 식별(LLM) + 검증 요약 + 다이어그램 렌더(결정론)
    relationships: dict  # {associations, includes, extends, generalizations, derived_use_cases}
    relationship_report: dict  # check_relationships의 관계 검증 집계
    diagram: str  # PlantUML 유스케이스 다이어그램 텍스트
    # 정적 라우팅 마커 — 피드백 게이트가 "advance"(다음 단계)/"loop"(재생성 후 재질문)를 써 두면
    # 서브그래프의 조건부 엣지(route_gate)가 이를 읽어 분기한다. Command(goto) 동적 라우팅 대체.
    gate_route: NotRequired[str]
    # --- 되돌아가기(supervisor) ---
    # 결함을 낸 단계로 몇 번 되돌렸는지. 표시·계측용이며 종료 예산으로 쓰지 않는다.
    redo_rounds: NotRequired[int]
    # 되돌린 기록: {owner, reason, escalated, rule_ids, strategy_key, input_digest}.
    # 남아 있지 않으면, 산출물만 보고는 되돌리기가 있었는지조차 알 수 없다.
    redo_history: NotRequired[list[dict]]
    # 되돌릴 단계에 들려 보내는 지시. 단계 함수가 `feedback` 인자 대신 여기서 읽는다 —
    # 그래프 엣지로 되돌릴 때는 인자를 넘길 자리가 없다.
    stage_feedback: NotRequired[dict[str, str]]
    # 정적 라우팅 마커 — 감독 노드가 "advance" 또는 되돌릴 **그룹 이름**을 써 두면
    # 조건부 엣지(route_redo)가 읽어 분기한다. gate_route와 같은 방식이다.
    redo_route: NotRequired[str]
