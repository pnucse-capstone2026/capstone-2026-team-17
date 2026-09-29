"""설계 규칙 지식베이스 — 무엇을 결함이라 부르는지, 그 근거는 무엇인지.

## 무엇이 없어서 이게 필요했나

클래스 다이어그램의 규칙은 **한 곳에만 있었다: 생성 프롬프트 산문.**
`services/class_diagram/extractor.py`의 통신 규칙(54–63행)과 자기검사 9번(91–95행)이 그것이다.
그리고 그 프롬프트는 LLM에게 *"finalize 전에 스스로 확인하라"*고 부탁한다.

**부탁이 검증인 적은 없다.** (a)~(e)와 통신 규칙은 전부 기계로 판정할 수 있는 것들인데,
지금까지 판정한 곳이 없었다. 그 사이 유일한 "검증"인 `common/validation.py`의
`validate_puml_artifact`는 문법만 보는데, 렌더러가 sanitize로 **구성에 의해** 유효한 PlantUML을
내므로 원리상 실패할 수 없다 — 코드 자신이 "트립와이어이지 수리 트리거가 아니다"라고
적어 두었다(`nodes/artifact.py`).

그래서 규칙을 **데이터로** 한 곳에 모은다. 생성 프롬프트도 검출기도 지적 문구도 여기서
파생된다. 규칙이 산문과 코드에 따로 있으면 갈라지고, 요구사항 쪽에서 실제로 갈라졌다
(`app/requirements/knowledge/rules.py` docstring §"생성 프롬프트도 여기서 조립한다").

## 심각도가 셋인 이유

`NON_RULE`이 있는 것이 핵심이다. "유스케이스당 Control 하나"와 "액터당 Boundary 하나"는
**규칙이 아니라는 사실 자체가 지켜야 할 지식**이다. 프롬프트가 이미 *"Create one Boundary
per distinct interaction concern, not automatically one per actor"*라고 경계하고 있는데,
그 경계가 산문에만 있으면 다음 사람이 관찰을 규칙으로 승격시킨다.

## 요구사항 지식과 설계 규칙의 경계

설계는 요구사항의 지식베이스나 내부 검출기를 import하지 않는다. 다만 요구사항 단계가
확정한 capability의 공개 판정 함수는 그대로 사용한다. 그래야 영속성 여부를 설계에서 다시
추측하거나 별도의 키 목록으로 복제하지 않는다.

## `stage` 필드를 지금 두는 이유

지금은 `class_diagram` 규칙만 있다. 그래도 필드를 두는 것은 나머지 네 산출물(시퀀스·API·
ERD·배포)이 같은 골격을 쓰기 때문이다(`nodes/artifact.py`의 `DesignArtifactSpec`). 확장은
여기에 레코드를 더하고 스펙에 `check=`를 채우는 일이 된다.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.design.knowledge import basis

# --- 심각도 -----------------------------------------------------------------
#: 위반이면 검증이 지적한다.
DEFECT = "defect"
#: 생성 쪽 지침. 위반을 지적하지는 않는다(판정 기준이 되기에는 무르다).
GUIDANCE = "guidance"
#: **규칙이 아니라는 기록.** 어디에서도 강제하지 않는다. 관찰이나 공학적 가드를 규칙으로
#: 승격시키지 않기 위해 둔다.
NON_RULE = "non_rule"

SEVERITIES = (DEFECT, GUIDANCE, NON_RULE)

# --- 누가 판정하는가 ---------------------------------------------------------
# `DEFECT`인데 판정하는 곳이 없는 규칙이 생길 수 있다. 그 사실을 `None` 하나로 뭉개면
# "LLM이 판정한다"와 "아무도 판정하지 않는다"가 같은 값이 된다.
#: `knowledge/detectors.py`의 결정론 검출기.
JUDGED_DETECTOR = "detector"
#: LLM 의미 검증자. **아직 없다** — 이 값을 가진 규칙은 지금 하나도 없어야 한다.
JUDGED_VALIDATOR = "validator"
#: **판정하는 곳이 없다.** 규칙은 적혀 있고 검증은 없다는 사실을 드러낸다.
JUDGED_NOWHERE = "nowhere"

JUDGES = (JUDGED_DETECTOR, JUDGED_VALIDATOR, JUDGED_NOWHERE)

# --- 스테이지 ---------------------------------------------------------------
# 이름은 `graphs/subgraphs.py`의 `DESIGN_STAGES`와 같다. import하지 않는 이유는 순환이다
# (subgraphs → services → knowledge). 두 목록이 맞는지는 테스트가 확인한다.
CLASS_DIAGRAM = "class_diagram"
SEQUENCE_DIAGRAM = "sequence_diagram"
API_SPEC = "api_spec"
ERD = "erd"
DEPLOYMENT_DIAGRAM = "deployment_diagram"


@dataclass(frozen=True)
class Rule:
    """규칙 하나. **규범 문장은 우리 표현이고, 인용은 좌표다**(원문 아님)."""

    id: str
    stage: str
    severity: str
    #: 규범 문장. 생성 프롬프트에 그대로 들어가므로 영어로 쓴다.
    statement: str
    #: 확인 좌표. 책 좌표이거나, 저장소 안의 코드 위치이거나, 재현 절차다.
    citation: str
    #: 근거 라벨(`basis.BASIS_OF_EVIDENCE`에 등록돼야 한다).
    evidence: str
    #: 짐작인 규칙은 반드시 있어야 한다 — **출처의 한계**를 적는다(위반의 의심이 아니다).
    caveat: str | None = None
    #: 이 규칙을 판정하는 곳. `DEFECT`는 반드시 밝힌다(없으면 `JUDGED_NOWHERE`).
    judged_by: str = JUDGED_NOWHERE
    #: 결정론 검출기 이름(`detectors.py`에 등록). `judged_by`가 검출기일 때만 있다.
    detector: str | None = None
    #: **생성 쪽에만** 주는 보조 문구 — 예시, 쓰는 법. 규범이 아니다.
    #:
    #: 규범은 `statement` 하나뿐이다. 여기에 새 제약을 적으면 아무도 판정하지 않는 규칙이
    #: 조용히 생긴다(그 사실을 드러내려고 `JUDGED_NOWHERE`를 둔 것인데, 이 자리는 그
    #: 표시를 우회한다). 여기 적을 수 있는 것은 `statement`가 이미 말한 것을 다시 보여
    #: 주는 것뿐이다.
    generation_note: str | None = None

    @property
    def hedged(self) -> bool:
        """지적할 때 출처의 한계를 함께 밝혀야 하는가."""
        return basis.needs_hedge(self.evidence)

    @property
    def tag(self) -> str:
        """지적 문구 꼬리표. 짐작인 규칙은 그 사실이 함께 붙는다."""
        parts = [self.id, self.citation]
        if self.hedged:
            parts.append("우리 판단")
        return f"[{' · '.join(parts)}]"

    def prompt_line(self) -> str:
        """생성 프롬프트 한 줄. 근거의 성격까지 모델에게 알린다.

        고지 문구는 라벨마다 다르다(`basis.prompt_note`) — "출처를 확인 못 했다"와
        "우리가 정했다"를 한 문구로 뭉개면 둘 중 하나는 거짓이 된다.
        """
        note = basis.prompt_note(self.evidence)
        return f"- ({self.id}) {self.statement}" + (f" [{note}]" if note else "")


# ---------------------------------------------------------------------------
# 규칙 목록
# ---------------------------------------------------------------------------
RULES: tuple[Rule, ...] = (
    # --- 참조 무결성 --------------------------------------------------------
    # 이 둘이 먼저 오는 이유: 나머지 규칙은 "다이어그램이 덜 좋다"는 말이지만, 이 둘은
    # **없는 것을 있다고 말하는** 것이라 하류 산출물까지 오염시킨다.
    Rule(
        id="class.relationship-endpoints-exist",
        stage=CLASS_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every relationship's source and target must name a class that exists in "
            "Classes. Never reference a class you did not declare."
        ),
        # 실측(2026-08-04): 클래스 하나만 선언하고 관계가 선언되지 않은 이름을 가리키는
        # PlantUML을 `java -jar plantuml.jar -syntax`에 넣으면 **오류 없이 통과하고
        # `(2 entities)`를 보고한다** — 선언하지 않은 이름이 그림에 빈 클래스로 생긴다.
        # 재현: tests/test_design_detectors.py::test_plantuml_invents_a_class_for_a_dangling_endpoint
        citation="plantuml.jar -syntax → '(2 entities)' (실측, 재현 테스트 있음)",
        evidence="plantuml-measured",
        judged_by=JUDGED_DETECTOR,
        detector="relationship_endpoints",
        generation_note=(
            "If a relationship needs a class that is not in your list, add the class — "
            "do not leave the endpoint dangling."
        ),
    ),
    Rule(
        id="class.usecase-ids-exist",
        stage=CLASS_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every id in use_case_ids must appear in the input use-case specification. "
            "An empty list is honest; an invented id is not."
        ),
        # 같은 판정을 `rtm.py`가 사후 보고로 이미 한다(`unknown_refs`). 판정은 한 곳에서
        # 나와야 하므로 검출기가 `rtm.upstream_names`를 쓴다 — 두 벌이면 갈라진다.
        citation="app/design/rtm.py (unknown_refs) — 같은 판정을 공유한다",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="usecase_ids",
        generation_note=(
            "Copy ids exactly as they appear in the input. If the input carries no ids, "
            "leave the list empty."
        ),
    ),
    # --- BCE 통신 규칙 ------------------------------------------------------
    Rule(
        id="class.stereotype-is-bce",
        stage=CLASS_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every class must carry exactly one of the stereotypes Boundary, Control, "
            "or Entity."
        ),
        # **이 규칙이 통신 규칙들보다 먼저 온다.** 스테레오타입을 못 읽으면 아래 세 규칙이
        # 전부 무판정이 되는데, 무판정은 통과처럼 보인다. 그 조용한 실패를 막는 자리다.
        citation="Jacobson, Object-Oriented Software Engineering (1992) — BCE",
        evidence="jacobson-unpinned",
        caveat=(
            "BCE 세 분류가 전부라는 것은 이 저장소가 확인하지 못했다 — 책 사본이 없다. "
            "다만 우리 스키마와 렌더러·하류 파서가 이 셋만 다루므로, 셋 밖의 값은 "
            "적어도 우리 파이프라인에서는 판정 불가다."
        ),
        judged_by=JUDGED_DETECTOR,
        detector="stereotype_is_bce",
    ),
    Rule(
        id="class.no-boundary-entity-link",
        stage=CLASS_DIAGRAM,
        severity=DEFECT,
        statement=(
            "A Boundary and an Entity must never be linked directly. Insert the Control "
            "that coordinates them instead."
        ),
        citation="Jacobson, Object-Oriented Software Engineering (1992) — BCE",
        evidence="jacobson-unpinned",
        caveat="BCE 통신 규칙이라고 알고 있으나 이 저장소에서 페이지를 확인하지 못했다.",
        judged_by=JUDGED_DETECTOR,
        detector="communication_rules",
    ),
    Rule(
        id="class.no-boundary-boundary-link",
        stage=CLASS_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Two Boundary classes must never be linked directly. A Boundary talks to an "
            "actor or to a Control."
        ),
        citation="Jacobson, Object-Oriented Software Engineering (1992) — BCE",
        evidence="jacobson-unpinned",
        caveat="BCE 통신 규칙이라고 알고 있으나 이 저장소에서 페이지를 확인하지 못했다.",
        judged_by=JUDGED_DETECTOR,
        detector="communication_rules",
    ),
    Rule(
        id="class.entity-does-not-initiate",
        stage=CLASS_DIAGRAM,
        severity=DEFECT,
        statement=(
            "An Entity never initiates action toward a Control or a Boundary. Entity-to-"
            "Entity links are allowed; an Entity as the source of a link to a Control or "
            "Boundary is not."
        ),
        citation="Jacobson, Object-Oriented Software Engineering (1992) — BCE",
        evidence="jacobson-unpinned",
        caveat="BCE 통신 규칙이라고 알고 있으나 이 저장소에서 페이지를 확인하지 못했다.",
        judged_by=JUDGED_DETECTOR,
        detector="communication_rules",
    ),
    # --- 관계가 데이터 모델까지 가려면 담아야 하는 것 ------------------------
    Rule(
        id="class.relationship-type-known",
        stage=CLASS_DIAGRAM,
        severity=DEFECT,
        statement=(
            "A relationship's type must be one of Inheritance, Dependency, Association, "
            "Aggregation, or Composition."
        ),
        # 렌더러는 모르는 종류를 조용히 단순 연관(`-->`)으로 그린다. 그림만 보면 모델이
        # 무엇을 말하려 했는지 알 수 없고, ERD 사상은 그 종류로 구조적 연관인지 행위
        # 링크인지를 가르므로 잘못 읽으면 관계가 통째로 사라지거나 잘못 생긴다.
        citation="app/design/services/class_diagram/plantuml.py (RELATION_SYMBOLS)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="relationship_type_known",
    ),
    Rule(
        id="class.entity-association-multiplicity",
        stage=CLASS_DIAGRAM,
        severity=DEFECT,
        statement=(
            "A structural relationship between two Entity classes must carry both "
            "sourceMultiplicity and targetMultiplicity, each one of \"1\", \"0..1\", "
            "\"*\", \"1..*\". Behavioural links through a Boundary or Control carry none."
        ),
        # 다중도가 없으면 사상이 성립하지 않는다. 예전에는 없는 채로 **전부 1:N이라고
        # 단정**했고, 그래서 다대다가 연결 테이블이 되는 경로가 코드에 아예 없었다.
        # 지금은 단정하지 않고 `Unmapped`로 남긴다 — 즉 다중도가 없으면 그 관계는
        # 그림에도 하류 스키마에도 **존재하지 않게 된다.**
        citation="app/design/services/erd/mapping.py (build_logical_model — Unmapped)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="entity_association_multiplicity",
        generation_note=(
            "Multiplicity is what decides whether a relationship becomes a foreign key, "
            "a unique foreign key, or a join table. There is no default: a missing one "
            "is not \"1\", it is \"not mapped\"."
        ),
    ),
    Rule(
        id="class.entity-data-associations",
        stage=CLASS_DIAGRAM,
        severity=GUIDANCE,
        statement=(
            "Entities that relate to each other as data should be connected by a "
            "structural relationship, not by naming a field after the other entity."
        ),
        citation="app/design/services/class_diagram/extractor.py (추출 절차 8)",
        evidence="project-convention",
        caveat=(
            "지침이지 규칙이 아니다. 어떤 필드가 참조인지 아닌지는 이름만으로 단정할 수 "
            "없어서(`외부 시스템의 id`일 수도 있다) 여기서 결함으로 세지 않는다. "
            "판정은 ERD 쪽에서 좁은 조건으로만 한다(`erd.field-looks-like-reference`)."
        ),
    ),
    Rule(
        id="class.fields-typed",
        stage=CLASS_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every declared BCE field must use the Java-targeted `name : Type` form. "
            "An untyped field cannot be compiled or mapped consistently downstream."
        ),
        citation="app/design/services/common/fields.py (split_field)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="fields_typed",
        generation_note=(
            "Choose a type grounded in the requirement semantics; do not leave the "
            "attribute type implicit."
        ),
    ),
    Rule(
        id="class.operation-inputs-explicit",
        stage=CLASS_DIAGRAM,
        severity=GUIDANCE,
        statement=(
            "When the use-case text says a caller submits, selects, filters, searches "
            "by, identifies, or supplies a value to an operation, declare that value as "
            "a named, typed parameter rather than hiding it behind an empty `()`."
        ),
        citation="app/design/services/class_diagram/extractor.py (method signature derivation)",
        evidence="project-convention",
        caveat=(
            "어떤 값이 실제로 경계를 넘어오는지는 유스케이스 문장의 의미를 읽어야 하므로, "
            "이 프로젝트는 빈 괄호만 보고 자동 결함으로 단정하지 않고 생성·수정 단계의 "
            "명시적 지침으로 둔다."
        ),
    ),
    # --- 형태 ---------------------------------------------------------------
    Rule(
        id="class.operation-contract-canonical",
        stage=CLASS_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Accepted class operations and collaboration calls must use canonical ids, "
            "resolve every referenced type and endpoint, and form one valid execution "
            "path for each accepted execution group."
        ),
        citation="app/design/services/class_diagram/validation.py (operation contract projection)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="operation_contract",
    ),
    Rule(
        id="class.operation-input-producers",
        stage=CLASS_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Each call argument must name exactly one finite source: its actor input step, "
            "an explicit precondition context, an ancestor call parameter, or an earlier "
            "compatible non-void call result."
        ),
        citation="app/design/services/class_diagram/validation.py (finite source validation)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="operation_input_producers",
    ),
    Rule(
        id="class.names-unique",
        stage=CLASS_DIAGRAM,
        severity=DEFECT,
        statement="Class names must be unique.",
        # 판정을 **sanitize 후 기준으로** 하는 것이 요점이다. `Payment Service!`와
        # `Payment_Service_`는 서로 다른 이름으로 들어오지만 렌더러가 같은 이름으로
        # 만든다 — 그림에서는 한 클래스가 되고, 그 사실을 지금까지 아무도 세지 않았다.
        citation="app/design/services/class_diagram/plantuml.py (sanitize_class_name)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="names_unique",
        generation_note=(
            "Names that differ only in punctuation or spacing collapse into one class in "
            "the rendered diagram — make them distinct words, not distinct symbols."
        ),
    ),
    Rule(
        id="class.name-pascal-case",
        stage=CLASS_DIAGRAM,
        severity=DEFECT,
        statement="Class names must be PascalCase identifiers.",
        citation="app/design/services/class_diagram/extractor.py (자기검사 9a)",
        evidence="project-convention",
        caveat="우리 표기 규약이다. 어떤 출처가 정한 것이 아니다.",
        judged_by=JUDGED_DETECTOR,
        detector="name_pascal_case",
    ),
    # --- 그라운딩 -----------------------------------------------------------
    Rule(
        id="class.covers-use-cases",
        stage=CLASS_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every use case in the input must be claimed by at least one class through "
            "use_case_ids."
        ),
        # 유스케이스를 통째로 빠뜨리는 것은 다이어그램이 조금 부실한 것이 아니라 **설계
        # 전체에서 그 기능이 사라지는** 것이다. 뒤의 네 산출물이 전부 이걸 재료로 쓴다.
        citation="app/design/graphs/subgraphs.py (DESIGN_STAGES — 나머지 넷의 재료)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="usecase_coverage",
    ),
    Rule(
        id="class.persistence-requires-entity",
        stage=CLASS_DIAGRAM,
        severity=DEFECT,
        statement=(
            "When accepted requirement and capability evidence requires persistent domain "
            "data, the class model must contain at least one Entity."
        ),
        citation=(
            "app/requirements/capability_contract.py (requires_persistent_storage); "
            "app/design/services/class_diagram/validation/diagram.py"
        ),
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="persistence_requires_entity",
    ),
    # --- 시퀀스 다이어그램: 모델 참조 무결성 -------------------------------
    Rule(
        id="sequence.message-participants-exist",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement="Every sequence message source and target must name a declared participant.",
        citation="app/design/services/sequence_diagram/plantuml.py (undeclared messages are omitted)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_participants",
    ),
    Rule(
        id="sequence.message-bce-flow",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement="Sequence calls must preserve the BCE communication boundaries represented by participant kinds.",
        citation="app/design/services/sequence_diagram/extractor.py (BCE communication rules)",
        evidence="project-convention",
        caveat="이 프로젝트가 사용하는 BCE 흐름 규약이다.",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_bce_flow",
    ),
    Rule(
        id="sequence.boundary-operation-direction",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Actor-to-Boundary calls must invoke input/event operations, while "
            "Control-to-Boundary calls must invoke declared output operations. "
            "Output-oriented operations such as display, show, render, prompt, notify, "
            "send, return, or respond are initiated by system components, not by the actor."
        ),
        citation="app/design/services/sequence_diagram/extractor.py (BCE communication rules)",
        evidence="project-convention",
        caveat="Boundary 입출력 방향을 메서드 동사로 구분하는 프로젝트 규약이다.",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_boundary_operation_direction",
    ),
    Rule(
        id="sequence.references-exist",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement="Sequence source classes and use-case ids must exist in their upstream artifacts.",
        citation="app/design/rtm.py (traceability references)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_traceability",
    ),
    Rule(
        id="sequence.class-diagram-version",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "A sequence collection must retain the exact hash of the class diagram "
            "whose receiver methods it was validated against."
        ),
        citation="app/design/services/sequence_diagram/reconcile.py (class diagram hash gate)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_class_diagram_version",
    ),
    Rule(
        id="sequence.participant-classes-exist",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every non-actor sequence participant must correspond to a class declared "
            "in the class diagram. If the participant has a source_class field, that "
            "class name is checked; otherwise the participant name itself is checked."
        ),
        citation="app/design/services/sequence_diagram/plantuml.py (participant ↔ class alignment)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_participant_classes",
    ),
    Rule(
        id="sequence.message-labels-match-methods",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every sequence message label on a non-return call must name a method "
            "defined in the target class's BCE model. The complete call signature, "
            "including its parameter declaration, must match; only visibility and the "
            "declared return type are stripped before comparison."
        ),
        citation="app/design/services/class_diagram/extractor.py (BCEClass.methods)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_message_methods",
    ),
    Rule(
        id="sequence.initial-message-entry",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "The initial non-return sequence message must be an Actor -> Boundary call. "
            "External interactions must enter through a boundary component."
        ),
        citation="app/design/services/sequence_diagram/extractor.py (BCE communication rules)",
        evidence="project-convention",
        caveat="Jacobson BCE 기법의 최초 진입점 규약이다.",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_initial_entry",
    ),
    Rule(
        id="sequence.unmatched-return-message",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every return-type message must consume one preceding matching call between "
            "the same pair of participants. A call can have at most one return message."
        ),
        citation="app/design/services/sequence_diagram/extractor.py (Return message guidelines)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_unmatched_returns",
    ),
    Rule(
        id="sequence.call-return-links",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every new sequence call must have a unique call_id, and every return must "
            "reference exactly one preceding call through reply_to with the reverse direction."
        ),
        citation="app/design/services/sequence_diagram/extractor.py (SequenceMessage call links)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_call_return_links",
    ),
    Rule(
        id="sequence.return-label-matches-method-return",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every return message must have a non-empty result label exactly matching "
            "the return type declared by its corresponding receiver-class method. Use "
            "the literal void for a method without a value return."
        ),
        citation="app/design/services/class_diagram/extractor.py (BCEClass.methods)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_return_values_match_methods",
    ),
    Rule(
        id="sequence.async-call-has-no-return",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "An async message is fire-and-forget and must not have a corresponding "
            "return message. Use sync when the caller consumes a result, or remove the return."
        ),
        citation="app/design/services/sequence_diagram/extractor.py (async message semantics)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_async_returns",
    ),
    Rule(
        id="sequence.call-requires-return",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every synchronous or self call must have exactly one corresponding "
            "return message, including calls whose declared return type is void."
        ),
        citation="app/design/services/class_diagram/extractor.py (BCEClass.methods)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_calls_have_returns",
    ),
    Rule(
        id="sequence.causal-call-chain",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "A non-actor participant may initiate a call only after it has been reached "
            "by an earlier call in the interaction."
        ),
        citation="app/design/services/sequence_diagram/extractor.py (interaction flow)",
        evidence="project-convention",
        caveat="메시지 목록이 하나의 시간 순 상호작용을 나타낸다는 프로젝트 모델 규약이다.",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_causal_call_chain",
    ),
    Rule(
        id="sequence.argument-data-flow",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every argument binding must match the receiver method parameter and identify "
            "a compatible input, state, literal, or preceding call result."
        ),
        citation="app/design/services/sequence_diagram/extractor.py (SequenceArgumentBinding)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_argument_data_flow",
    ),
    Rule(
        id="sequence.actor-step-involvement",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "A resolved flow step whose subject is an actor must contain at least one "
            "actor-originated call instead of being covered only by unrelated system messages. "
            "Distinct main-flow actor actions must not reuse one identical Boundary operation "
            "merely to claim step coverage."
        ),
        citation="app/design/services/sequence_diagram/extractor.py (Flow analysis and traceability)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_actor_step_involvement",
    ),
    Rule(
        id="sequence.usecase-step-coverage",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every main-scenario and extension handling step from the specification "
            "must be referenced by a sequence call or an explicit narrative/unresolved "
            "record, and every class operation traced to that flow must be invoked. "
            "Legacy specifications without step structure fall back to use_case_ids coverage."
        ),
        citation="app/design/rtm.py (traceability references)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_usecase_coverage",
    ),
    Rule(
        id="sequence.step-operation-distinctness",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Distinct main-scenario steps must not be covered solely by repeating one "
            "identical receiver operation. Reusing a request operation for validation, "
            "persistence, and response is not meaningful sequence coverage."
        ),
        citation="app/design/services/sequence_diagram/extractor.py (step-to-operation selection)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_step_operation_distinctness",
    ),
    Rule(
        id="sequence.flow-order",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Main-scenario messages must preserve step order, and each extension must be "
            "placed immediately after the main step from which it branches."
        ),
        citation="app/requirements/schemas.py (Extension.branch_step and ordered main_scenario)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_flow_order",
    ),
    Rule(
        id="sequence.unresolved-usecase-step",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "A use-case step marked or worded as unresolved must be clarified before a "
            "sequence diagram assigns concrete behavior to it."
        ),
        citation="app/requirements/schemas.py (flow-step specification)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_unresolved_steps",
    ),
    Rule(
        id="sequence.fragment-condition-consistency",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every fragment path entry must have a stable id, alt/loop/opt type and "
            "condition. Else branches are valid only for alt and follow its main branch; "
            "an alt has both main and else branches, while a single condition uses opt."
        ),
        citation="app/design/services/sequence_diagram/plantuml.py (Fragment rendering)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_fragment_condition_consistency",
    ),
    Rule(
        id="sequence.database-access-discipline",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Database participants must not be directly invoked by Actor or Boundary "
            "participants; access must be mediated through Control or Entity components."
        ),
        citation="app/design/services/sequence_diagram/extractor.py (Layered architecture rules)",
        evidence="project-convention",
        caveat="데이터 접근 계층 캡슐화 규약이다.",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_database_access_discipline",
    ),
    Rule(
        id="sequence.self-call-method-validation",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Self-calling messages (source == target) must have a non-empty label "
            "naming the internal operation being invoked."
        ),
        citation="app/design/services/sequence_diagram/plantuml.py (Self-call messages)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_self_call_method_validation",
    ),
    Rule(
        id="sequence.orphan-participant-detection",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every participant declared in the sequence model must be involved in "
            "at least one message as either source or target."
        ),
        citation="app/design/rtm.py (Participant active trace)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_orphan_participant_detection",
    ),
    Rule(
        id="sequence.duplicate-consecutive-messages",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Duplicate identical messages must not be emitted consecutively outside "
            "of explicit repetition fragments."
        ),
        citation="app/design/services/sequence_diagram/plantuml.py (Redundant message check)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_duplicate_consecutive_messages",
    ),
    Rule(
        id="sequence.extension-replays-anchor-operation",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "An extension must not repeat its branch-step operation merely to express "
            "that operation's failure. Retrying the operation requires an explicit loop."
        ),
        citation="app/requirements/schemas.py (Extension.branch_step and handling_steps)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_extension_replays_anchor_operation",
    ),
    Rule(
        id="sequence.message-naming-convention",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Message operation labels must follow camelCase or verbNoun() method "
            "naming conventions rather than PascalCase class names."
        ),
        citation="app/design/services/sequence_diagram/extractor.py (Operation naming rule)",
        evidence="project-convention",
        caveat="오퍼레이션 표기법 규약이다.",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_message_naming_convention",
    ),
    Rule(
        id="sequence.participant-kind-validity",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every participant kind must be one of the standard stereotypes: "
            "actor, boundary, control, entity, database."
        ),
        citation="app/design/services/sequence_diagram/extractor.py (Participant kinds)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_participant_kind_validity",
    ),
    Rule(
        id="sequence.message-type-validity",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every message type must be one of the standard call types: "
            "sync, async, return, self, activate, deactivate."
        ),
        citation="app/design/services/sequence_diagram/extractor.py (Message call types)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_message_type_validity",
    ),
    Rule(
        id="sequence.no-lifecycle-events",
        stage=SEQUENCE_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Sequence diagrams use the shared fixed lifeline template and must not "
            "contain activate or deactivate lifecycle events."
        ),
        citation="app/design/services/sequence_diagram/plantuml.py (shared sequence template)",
        evidence="project-convention",
        caveat="시퀀스 다이어그램의 시각적 통일성을 위한 프로젝트 규약이다.",
        judged_by=JUDGED_DETECTOR,
        detector="sequence_no_lifecycle_events",
    ),
    # --- API 명세: 모델 참조 무결성 -----------------------------------------
    Rule(
        id="api.operations-present",
        stage=API_SPEC,
        severity=DEFECT,
        statement=(
            "The API model must contain at least one operation grounded in a use case, "
            "a BCE Control method, and a sequence call before implementation starts."
        ),
        citation="OpenAPI Generator requires at least one path operation",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="api_operations_present",
    ),
    Rule(
        id="api.path-parameters-match",
        stage=API_SPEC,
        severity=DEFECT,
        statement="Every path variable must have one matching path parameter, and no extra path parameter is allowed.",
        citation="OpenAPI path template projected by app/design/services/api_spec/openapi.py",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="api_path_parameters",
    ),
    Rule(
        id="api.schema-references-exist",
        stage=API_SPEC,
        severity=DEFECT,
        statement="Request and response schema references must name schemas declared by the API model.",
        citation="app/design/services/api_spec/openapi.py (_body_schema fallback)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="api_schema_references",
    ),
    Rule(
        id="api.executable-schema-fields",
        stage=API_SPEC,
        severity=DEFECT,
        statement=(
            "Every named request or response schema must define object fields or "
            "enumeration values, and collection fields must name their item type."
        ),
        citation="app/design/services/api_spec/projection.py (OpenAPI schema projection)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="api_executable_schema_fields",
    ),
    Rule(
        id="api.operation-ids-unique",
        stage=API_SPEC,
        severity=DEFECT,
        statement="Every API endpoint must have a unique operation_id.",
        citation="app/design/services/api_spec/extractor.py (operation_id self-check)",
        evidence="project-convention",
        caveat="이 프로젝트의 OpenAPI 식별자 규약이다.",
        judged_by=JUDGED_DETECTOR,
        detector="api_operation_ids",
    ),
    Rule(
        id="api.references-exist",
        stage=API_SPEC,
        severity=DEFECT,
        statement="API source classes and use-case ids must exist in their upstream artifacts.",
        citation="app/design/rtm.py (traceability references)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="api_traceability",
    ),
    Rule(
        id="api.control-binding-exists",
        stage=API_SPEC,
        severity=DEFECT,
        statement=(
            "Every API endpoint must explicitly bind to one existing BCE Control "
            "method and list that Control in its source classes."
        ),
        citation="app/design/services/api_spec/extractor.py (ApiControlBinding)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="api_control_binding",
    ),
    Rule(
        id="api.control-arguments-match",
        stage=API_SPEC,
        severity=DEFECT,
        statement=(
            "An API Control binding must map every Control parameter exactly once "
            "from a declared path, query, or request-body value of a compatible type."
        ),
        citation="app/design/contracts/api_spec.py (ApiControlArgument)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="api_control_arguments",
    ),
    Rule(
        id="api.trusted-context-provenance",
        stage=API_SPEC,
        severity=DEFECT,
        statement=(
            "A Control argument sourced from trusted context must be explicitly "
            "bound to the same parameter from a validated trusted-context source in the BCE "
            "collaboration for that endpoint."
        ),
        citation="app/design/services/api_spec/normalization.py (_control_arguments)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="api_trusted_context_provenance",
    ),
    Rule(
        id="api.accepted-interactions-covered",
        stage=API_SPEC,
        severity=DEFECT,
        statement=(
            "Every accepted Boundary-to-Control interaction and use-case ID "
            "must be represented by an API endpoint bound to that Control."
        ),
        citation="app/design/services/api_spec/normalization.py (interaction_contracts)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="api_accepted_interactions_covered",
    ),
    Rule(
        id="api.control-outcomes-cover-responses",
        stage=API_SPEC,
        severity=DEFECT,
        statement=(
            "Every documented HTTP response status must have one explicit named "
            "Control outcome, and non-empty responses must not rely on Object/void."
        ),
        citation="app/design/services/api_spec/models.py (ApiControlOutcome)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="api_control_outcomes",
    ),
    Rule(
        id="api.control-call-in-sequence",
        stage=API_SPEC,
        severity=DEFECT,
        statement=(
            "The Control method bound to an API endpoint must occur in the matching "
            "sequence flow so the endpoint has an executable interaction path."
        ),
        citation="app/design/services/sequence_diagram/reconcile.py (call contracts)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="api_control_sequence",
    ),
    # --- 지침(지적하지 않는다) ----------------------------------------------
    Rule(
        id="class.control-per-use-case",
        stage=CLASS_DIAGRAM,
        severity=GUIDANCE,
        statement=(
            "Each use case is normally coordinated by at least one Control class."
        ),
        citation="Jacobson, Object-Oriented Software Engineering (1992) — BCE",
        evidence="jacobson-unpinned",
        caveat=(
            "지침이지 규칙이 아니다. 유스케이스가 순수 조회라 조정할 흐름이 없을 수 있다 "
            "— 그것을 결함으로 부를 근거가 없다."
        ),
    ),
    Rule(
        id="class.no-empty-class",
        stage=CLASS_DIAGRAM,
        severity=GUIDANCE,
        statement=(
            "A class should carry at least one field or one method. A class with neither "
            "has not been derived, only named."
        ),
        citation="app/design/services/class_diagram/extractor.py (필드·메서드 도출 5–6)",
        evidence="project-convention",
        caveat=(
            "지침이지 규칙이 아니다. 분석 수준 다이어그램에서 표식용 클래스가 정당할 수 "
            "있다."
        ),
    ),
    # --- 규칙이 아니라는 기록 ------------------------------------------------
    Rule(
        id="class.boundary-per-actor",
        stage=CLASS_DIAGRAM,
        severity=NON_RULE,
        statement=(
            "The number of Boundary classes is NOT tied to the number of actors. Create "
            "one Boundary per distinct interaction concern."
        ),
        citation="app/design/services/class_diagram/extractor.py (Boundary derivation 2)",
        evidence="engineering-guard",
        caveat=(
            "프롬프트가 명시적으로 'not automatically one per actor'라고 적어 두었다. "
            "이걸 규칙으로 승격시키면 모델이 액터 수에 맞추려고 Boundary를 지어내거나 "
            "합친다."
        ),
    ),
    Rule(
        id="class.count-bounds",
        stage=CLASS_DIAGRAM,
        severity=NON_RULE,
        statement=(
            "There is no upper or lower bound on how many classes a use case yields."
        ),
        citation="(관찰 없음 — 세어 본 적이 없다)",
        evidence="engineering-guard",
        caveat=(
            "개수 규칙을 두면 모델이 개수를 맞추려고 클래스를 지어내거나 지운다. "
            "코퍼스에서 분포를 세기 전에는 어떤 상한도 근거가 없다."
        ),
    ),
    # =======================================================================
    # ERD
    # =======================================================================
    # ERD 모델은 클래스 다이어그램과 **같은 BCE 스키마**이지만 자기 사본을 따로 편집한다
    # (`erd_bce_classes`). 그래서 클래스 쪽이 통과했다는 것이 이쪽의 보증이 아니다.
    #
    # 규칙이 두 층에 걸쳐 있다. 앞 몇 개는 BCE 모델을 보고, 나머지는 그것을 사상해서 나온
    # **논리 데이터 모델**(테이블·키·외래키)을 본다. 후자가 이 지식베이스에서 처음으로
    # 그림도 원본 모델도 아닌 것을 판정한다 — 사상이 별도 단계가 되면서 생긴 자리다.
    # --- 구조: ERD 사본이 성한가 ------------------------------------------
    # 이 셋이 먼저 오는 이유는 클래스 다이어그램의 참조 무결성 규칙이 먼저 오는 이유와
    # 같다. 나머지 ERD 규칙은 "데이터 모델이 덜 좋다"는 말이지만, 이 셋은 **있어야 할
    # 것이 말없이 사라지거나 없던 것이 생기는** 것이다.
    #
    # 클래스 쪽이 통과했다는 것이 여기의 보증이 아니다 — ERD 모델은 그 사본을 따로
    # 편집한 것이다(`erd/reviser.py`). 1차에서 ERD 검사를 붙인 근거가 정확히 그것이었는데
    # 정작 구조 규칙은 안 걸어 두어서, 이 셋이 전부 지적 없이 지나갔다.
    Rule(
        id="erd.relationship-endpoints-exist",
        stage=ERD,
        severity=DEFECT,
        statement=(
            "Every relationship's source and target must name a class that exists in "
            "Classes. Never reference a class you did not declare."
        ),
        # 클래스 쪽과 **결과가 정반대다.** 거기서는 PlantUML이 유령 클래스를 만들어 없던
        # 것이 생기고, 여기서는 사상이 그 관계를 지나가 있던 것이 사라진다. 그리고 그
        # 사라짐은 행위 링크를 정상적으로 건너뛰는 것과 같은 코드 경로라 흔적이 없다.
        citation="app/design/services/erd/mapping.py (_map_relationship — 끝이 표가 아니면 반환)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="erd_relationship_endpoints",
        generation_note=(
            "If a relationship needs a class that is not in your list, add the class — "
            "do not leave the endpoint dangling."
        ),
    ),
    Rule(
        id="erd.stereotype-is-bce",
        stage=ERD,
        severity=DEFECT,
        statement=(
            "Every class must carry exactly one of the stereotypes Boundary, Control, "
            "or Entity, including the ones an ERD never draws."
        ),
        # 딱지가 깨지면 그 클래스는 Entity로 안 읽히고 **표도 그 표에 걸린 관계도** 함께
        # 사라진다. ERD 수정 프롬프트가 "Boundary와 Control을 그대로 두라"고 요구하는
        # 것과 짝을 이룬다 — 요구만 하고 판정을 안 하면 그건 부탁이다.
        citation="app/design/services/common/fields.py (is_entity)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="erd_stereotype_is_bce",
    ),
    Rule(
        id="erd.entity-name-usable",
        stage=ERD,
        severity=DEFECT,
        statement=(
            "Every Entity must have a name that can become a table name — not empty, "
            "not punctuation only."
        ),
        # 빈 이름이면 사상이 `UnknownEntity`를 **지어낸다.** 그 이름이 하류에서 테이블
        # 이름이 되어 스키마에 남는다.
        citation="app/design/services/common/fields.py (sanitize_entity_name)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="erd_entity_name_usable",
    ),
    Rule(
        id="erd.source-entity-consistency",
        stage=ERD,
        severity=DEFECT,
        statement=(
            "The ERD must contain exactly the Entity names and Entity-to-Entity "
            "relationships accepted by the class model."
        ),
        citation="app/design/knowledge/detectors.py (erd_source_entity_consistency)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="erd_source_entity_consistency",
    ),
    Rule(
        id="erd.has-entity",
        stage=ERD,
        severity=DEFECT,
        statement=(
            "An applicable ERD must contain at least one <<Entity>> class. An empty "
            "standalone ERD is not a diagram."
        ),
        # 테이블이 0개면 렌더가 빈 문자열을 내고, 문법 검사가 "PlantUML code is empty."로
        # 잡는다. 그런데 그 칸의 뜻은 **"우리 렌더러가 깨졌다"**이다(`nodes/artifact.py`의
        # `render_node`). 원인은 모델인데 귀속이 렌더러로 간다. 여기서 먼저 잡아야
        # 귀속이 맞고 재생성 기회도 생긴다.
        citation="app/design/services/common/plantuml.py:42-43 (빈 입력 → syntax_errors)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="erd_has_entity",
    ),
    Rule(
        id="erd.relationship-mapped",
        stage=ERD,
        severity=DEFECT,
        statement=(
            "Every relationship between two Entity classes must be mappable to the "
            "relational model. One that is not carries no multiplicity, or is typed as a "
            "Dependency while joining two Entities."
        ),
        # **사상되지 못한 관계는 그림에 없다.** 예전에는 다중도가 없어도 1:N으로 단정해서
        # 무언가는 그려졌고, 그래서 틀린 선이 맞는 선처럼 보였다. 지금은 안 그려지므로,
        # 이 지적이 없으면 관계가 조용히 사라진 것을 아무도 못 본다.
        citation="app/design/services/erd/mapping.py (Unmapped)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="erd_relationships_mapped",
    ),
    Rule(
        id="erd.composition-owner-is-mandatory",
        stage=ERD,
        severity=DEFECT,
        statement=(
            "In a Composition, the multiplicity at the whole's end must be \"1\". A part "
            "cannot exist without its whole, and cannot belong to several at once."
        ),
        # 종류와 다중도가 서로 다른 말을 하는 자리다. **사상은 조정하지 않는다** — 어느
        # 쪽이 의도인지 우리가 모르기 때문이다. 한쪽으로 정리하면 모델이 적은 것을 우리가
        # 덮는 것이 되고, 그건 이 작업이 내내 막아 온 것이다. 모순을 가진 채 옮기고
        # 여기서 드러내면 재생성이 둘 중 하나를 고친다.
        citation="app/design/services/erd/mapping.py (_map_relationship — 합성은 식별 관계)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="erd_composition_owner",
        generation_note=(
            "If the part can exist on its own, or can belong to more than one whole, it "
            "is an Association or an Aggregation — not a Composition."
        ),
    ),
    Rule(
        id="erd.identifier-fields-exist",
        stage=ERD,
        severity=DEFECT,
        statement=(
            "Every name in an Entity's `identifier` must be one of that Entity's own "
            "fields. An empty `identifier` is honest; one naming a field that is not "
            "there is not."
        ),
        # 실재하지 않는 필드를 가리키면 사상이 자연키를 포기하고 대리키로 떨어진다.
        # 조용히 떨어지므로, 모델은 "이 자연키를 쓴다"고 말하는데 산출물은 다른 키를 쓴다.
        citation="app/design/services/erd/mapping.py (_build_tables — keyOrigin)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="erd_identifier_fields",
    ),
    Rule(
        id="erd.surrogate-key-collides",
        stage=ERD,
        severity=DEFECT,
        statement=(
            "Do not declare a field with the same name as the surrogate key this project "
            "would add (`<table>_id`). Either list it in `identifier` because it really is "
            "the key, or give it a different name."
        ),
        # 겹치면 대리키가 그 자리를 차지하고 선언한 필드가 밀려난다. 모델이
        # `order_id : String`이라 적었는데 산출물에는 `order_id : BIGINT`만 남는 것이라,
        # **아무도 고르지 않은 타입이 하류 DDL까지 간다.**
        #
        # 자연키로 승격시키지 않는 것이 요점이다. 이름이 `{표}_id`라고 해서 그것이
        # 식별자라고 읽으면, 그건 이름에서 의도를 읽는 것이고 이 작업이 지운 바로 그
        # 추론이다(`erd.fk-from-field-name` 참조).
        citation="app/design/services/erd/mapping.py (_build_tables — surrogateCollidesWith)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="erd_surrogate_key_collides",
    ),
    Rule(
        id="erd.table-names-unique",
        stage=ERD,
        severity=DEFECT,
        statement=(
            "Table names must be unique after rendering, counting the join tables and "
            "first-normal-form child tables the mapping creates."
        ),
        # 두 층이 겹치는 자리다. Entity 이름끼리 겹치는 것뿐 아니라, 연결 테이블
        # (`BookTag`)이나 1NF 자식(`LoanTags`)이 실재 Entity와 같은 이름이 될 수 있다.
        # 사상은 이름을 짓기만 하고 충돌을 막지 않는다 — 막으면 어느 쪽을 버릴지 우리가
        # 정하게 되고, 그건 모델이 정할 일이다.
        citation="app/design/services/erd/mapping.py (_junction · _multivalued_child)",
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="erd_table_names_unique",
    ),
    # "모든 테이블에 기본키가 있다"와 "외래키는 실재 테이블을 가리킨다"는 여기 없다.
    # 규칙으로 적어 봤다가 뺐다 — **사상이 구성에 의해 보장하므로 어떤 모델로도 위반을
    # 만들 수 없다.** 걸 수 없는 규칙은 모델 결함을 판정하는 규칙이 아니다. 대표 정상·실패
    # 입력은 `tests/test_design_detectors.py`에서 직접 validator에 넣어 확인한다.
    # 그 둘은 모델의 오류를 찾는 규칙이 아니라 **변환 코드가 항상 지켜야 할 조건**이다.
    # 따라서 `tests/test_erd_mapping.py`의 변환 테스트가 확인한다.
    Rule(
        id="erd.entity-typed-field-needs-relationship",
        stage=ERD,
        severity=DEFECT,
        statement=(
            "A field whose type is another Entity — `member : Member`, "
            "`lines : List<OrderLine>` — needs a relationship between the two Entities. "
            "The field alone becomes nothing: an Entity-typed field is not a column."
        ),
        # **조용히 사라지던 자리다.** 사상은 Entity 타입 필드를 컬럼으로 만들지 않는다
        # (그 사실을 들고 가는 것은 관계다). 관계까지 없으면 컬럼도 자식 표도 관계선도
        # `Unmapped` 항목도 없어서, 모델이 적은 링크가 산출물 어디에도 안 남고 아무도
        # 그것을 못 본다. 스칼라 쪽은 한술 더 떠서 `member : MEMBER`라는 SQL 아닌
        # 타입의 가짜 컬럼을 만들어 하류 DDL까지 보냈다.
        citation="app/design/services/erd/mapping.py (_build_tables) · app/design/services/common/fields.py (names_an_entity)",
        evidence="pipeline-invariant",
        caveat=(
            "`erd.fk-from-field-name`과 헷갈리지 말 것. 저쪽이 금지하는 것은 필드 "
            "**이름**(`memberId`)에서 외래키를 짐작하는 일이고, 이 규칙이 보는 것은 모델이 "
            "직접 적은 **자료형**이다. 짐작이 없으므로 오탐의 종류가 다르다 — 걸리는 것은 "
            "Entity와 이름이 같은 자료형을 쓴 경우뿐이다."
        ),
        judged_by=JUDGED_DETECTOR,
        detector="erd_entity_typed_field_needs_relationship",
        generation_note=(
            "Write the relationship with its multiplicities. The foreign key (or join "
            "table, or child table) is generated from it — not from the field."
        ),
    ),
    Rule(
        id="erd.field-looks-like-reference",
        stage=ERD,
        severity=DEFECT,
        statement=(
            "Do not point at another Entity by naming a field after it. A field named "
            "`<Other>Id` where `<Other>` is an Entity must be a relationship instead."
        ),
        # **이 규칙이 지워진 코드의 자리를 대신한다.** 예전 사상은 `Loan.memberId`를 보고
        # `Member`를 가리키는 외래키를 조용히 만들었다(이름에서 의도를 추론한 것이다).
        # 그 추론을 지우면서 신호까지 버리지는 않았다 — 만들지 않고 지적한다.
        citation="app/design/services/erd/mapping.py (모듈 docstring — 지워진 이름 추론)",
        evidence="pipeline-invariant",
        caveat=(
            "판정을 좁게 걸었다: 필드 이름이 `<X>Id`/`<X>_id`이고 `X`가 실재 Entity이며 "
            "**둘 사이에 관계가 없을 때만** 센다. 그래도 외부 시스템의 식별자를 그렇게 "
            "이름 붙인 경우를 오탐할 수 있다. 실데이터로 오탐률을 재 본 적은 없다."
        ),
        judged_by=JUDGED_DETECTOR,
        detector="erd_reference_like_fields",
        generation_note=(
            "Write the relationship with its multiplicities; the foreign-key column is "
            "generated from it."
        ),
    ),
    # --- 지침(지적하지 않는다) ----------------------------------------------
    Rule(
        id="erd.entity-has-field",
        stage=ERD,
        severity=GUIDANCE,
        statement=(
            "An Entity should carry at least one field. Methods do not appear in an ERD, "
            "so an Entity with only methods becomes a table with nothing but its key."
        ),
        citation="app/design/services/erd/mapping.py (_build_tables — fields만 읽는다)",
        evidence="project-convention",
        caveat=(
            "지침이지 규칙이 아니다. 클래스 쪽 `class.no-empty-class`는 필드 **또는** "
            "메서드를 세므로 여기와 판정이 다르다 — 메서드만 있는 Entity는 그쪽을 "
            "통과하고 여기서는 빈 테이블이 된다. 그래도 열 없는 테이블이 분석 수준에서 "
            "정당할 수 있어 결함으로 세지 않는다."
        ),
    ),
    Rule(
        id="erd.field-type-declared",
        stage=ERD,
        severity=GUIDANCE,
        statement=(
            "Write Entity fields as `name : Type`. A field with no type becomes a column "
            "with no type, and the downstream schema generator picks one instead."
        ),
        citation="app/design/services/common/fields.py (split_field · sql_type)",
        evidence="project-convention",
        caveat=(
            "지침이지 규칙이 아니다. 분석 수준에서 타입을 아직 정하지 않는 것이 정당할 수 "
            "있다. 다만 예전처럼 `VARCHAR(255)`를 지어내지는 않으므로, 안 적으면 그 칸은 "
            "비어서 하류로 간다."
        ),
    ),
    # --- 규칙이 아니라는 기록 ------------------------------------------------
    Rule(
        id="erd.fk-from-field-name",
        stage=ERD,
        severity=NON_RULE,
        statement=(
            "Field naming is NOT a foreign-key declaration. This project does not turn "
            "`memberId` into a key that points at `Member`; the relationship does that."
        ),
        citation="app/design/services/erd/mapping.py (모듈 docstring)",
        evidence="engineering-guard",
        caveat=(
            "이 코드는 실재하는 문제를 풀려고 들어왔었다 — 관계에서만 외래키를 뽑으니 "
            "ERD에 선이 거의 안 그려졌다. 지운 이유는 그 문제가 사라져서가 아니라 "
            "**해결 방향이 틀려서**다: 관계가 없는 것을 이름으로 메우는 대신 관계를 "
            "요구하도록 고쳤다. 적어 두지 않으면 다음 사람이 같은 증상을 보고 되살린다.\n"
            "**경계**: 금지되는 것은 *이름*에서 읽는 것뿐이다. `member : Member`처럼 모델이 "
            "**자료형**으로 Entity를 적은 것은 짐작이 아니라 선언이고, 그쪽은 "
            "`erd.entity-typed-field-needs-relationship`이 관계를 요구한다. 그것까지 "
            "이 NON_RULE로 읽으면 조용히 사라지는 필드가 다시 생긴다."
        ),
    ),
    Rule(
        id="erd.inheritance-strategy",
        stage=ERD,
        severity=NON_RULE,
        statement=(
            "The model does NOT choose how inheritance becomes tables. This project maps "
            "it one way (the subclass table's primary key is a foreign key to the "
            "superclass); do not restructure the classes to steer that choice."
        ),
        citation="app/design/services/erd/mapping.py (모듈 docstring — 상속)",
        evidence="engineering-guard",
        caveat=(
            "관계형에는 상속이 없어 세 전략(단일 테이블·클래스별 테이블·구체 클래스 "
            "테이블) 중 하나를 골라야 하고, **우리가 골랐다.** 어느 것이 옳은지는 질의 "
            "패턴에 달렸는데 우리는 그것을 모른다. 모델에게 고르게 하면 그건 데이터 "
            "모델이 아니라 물리 설계를 시키는 것이 된다."
        ),
    ),
    Rule(
        id="erd.table-count-bounds",
        stage=ERD,
        severity=NON_RULE,
        statement="There is no upper or lower bound on how many tables an ERD has.",
        citation="(관찰 없음 — 세어 본 적이 없다)",
        evidence="engineering-guard",
        caveat=(
            "클래스 개수와 같은 이유다. 개수 규칙을 두면 모델이 개수를 맞추려고 엔티티를 "
            "지어내거나 합친다."
        ),
    ),
    Rule(
        id="deployment.workload-graph-valid",
        stage=DEPLOYMENT_DIAGRAM,
        severity=DEFECT,
        statement=(
            "The WorkloadGraph must contain grounded workloads, valid references, supported "
            "runtime contracts, and all deployment inputs that cannot be derived later."
        ),
        citation=(
            "app/design/services/deployment_diagram/template_topology.py and "
            "normalization.py (validate_workload_graph)"
        ),
        evidence="pipeline-invariant",
        judged_by=JUDGED_DETECTOR,
        detector="deployment_workload_graph",
    ),
)


_BY_ID: dict[str, Rule] = {r.id: r for r in RULES}


def rule(rule_id: str) -> Rule:
    """id로 규칙 하나. 없으면 `KeyError` — 없는 규칙을 인용하는 것은 오류다."""
    return _BY_ID[rule_id]


def known_ids() -> frozenset[str]:
    """존재하는 규칙 id 전부. 지적이 댄 인용을 대조하는 데 쓴다."""
    return frozenset(_BY_ID)


def rules_for(stage: str, severity: str | None = None) -> tuple[Rule, ...]:
    """스테이지(+심각도)로 규칙을 고른다. 선언 순서를 유지한다."""
    return tuple(
        r for r in RULES
        if r.stage == stage and (severity is None or r.severity == severity)
    )


def judged_by(stage: str, judge: str) -> tuple[Rule, ...]:
    """이 스테이지에서 그 판정자가 보는 결함 규칙들."""
    return tuple(r for r in rules_for(stage, DEFECT) if r.judged_by == judge)


def tag_of(rule_id: str) -> str:
    """지적 문구에 붙일 꼬리표. 모르는 id는 그 사실을 드러낸다.

    조용히 빈 문자열을 돌려주면 **없는 규칙을 인용한 지적이 근거 있는 지적처럼** 보인다.
    그건 이 지식베이스를 두는 이유와 반대다.
    """
    found = _BY_ID.get(rule_id)
    return found.tag if found else f"[{rule_id} · 알 수 없는 규칙]"


def rule_of(issue: str) -> str | None:
    """지적 문구가 인용한 규칙 id. 못 찾으면 None.

    꼬리표는 우리가 만든다(`Rule.tag` → `[<id> · <좌표> …]`)므로 정확히 맞춰 찾는다.
    문구를 파싱하는 대신 **아는 id로 조회**하는 방향이라, 새 규칙이 생겨도 그대로다.
    """
    for rule_id in _BY_ID:
        if f"[{rule_id} ·" in issue:
            return rule_id
    return None


def generation_prompt_block(stage: str) -> str:
    """**생성** 프롬프트가 지켜야 할 규칙 목록.

    `DEFECT` + `GUIDANCE` 전부를 담는다. 쓰는 쪽에서는 "누가 잡느냐"가 상관없다 —
    검출기가 잡을 결함도 애초에 안 쓰는 편이 낫다.
    """
    lines: list[str] = []
    for r in rules_for(stage):
        if r.severity not in (DEFECT, GUIDANCE):
            continue
        lines.append(r.prompt_line())
        if r.generation_note:
            lines.append(f"  {r.generation_note}")
    return "\n".join(lines)


def non_rules_block(stage: str) -> str:
    """**규칙이 아니라고** 적어 둔 것들 — 생성 쪽에 그 사실 그대로 준다.

    과적합은 판정할 때가 아니라 **쓸 때** 일어난다: "액터당 Boundary 하나"를 목표로
    알아들은 모델은 필요 없는 Boundary를 지어내거나 필요한 것을 합친다. 그러니 이 사실을
    받아야 하는 쪽은 판정자가 아니라 생성자다.
    """
    return "\n".join(f"- ({r.id}) {r.statement}" for r in rules_for(stage, NON_RULE))


def unjudged_defects() -> tuple[Rule, ...]:
    """결함이라고 적어 놓고 **아무도 판정하지 않는** 규칙들.

    비어 있는 것이 목표다 — 지금은 비어 있고, 테스트가 그것을 고정한다. 의미 검증자를
    넣으면서 `JUDGED_VALIDATOR` 규칙을 추가할 때 이 목록이 잠깐 늘어날 수 있는데, 그
    사실이 조용히 지나가면 안 된다.
    """
    return tuple(r for r in RULES if r.severity == DEFECT and r.judged_by == JUDGED_NOWHERE)
