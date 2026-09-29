"""typed 클래스 모델 검증 보고서를 legacy UI·downstream detector에 연결한다.

현재 클래스 생성 경로의 최종 계약은 ``validation.model.validate_class_model``이 소유한다.
이 모듈에는 이전 저장본과 API·ERD readiness가 소비하는 표시용 detector도 남아 있다.
검사는 read-only이며 LLM이나 service repair를 시작하지 않는다.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from app.design.knowledge import rules
from app.design.schemas.class_model import operation_method_signature
from app.design.services.class_diagram.plantuml import RELATION_SYMBOLS, sanitize_class_name
from app.design.services.class_diagram.scenario import (
    build_scenario_index,
)
from app.design.services.class_diagram.validation.model import validate_class_model
from app.design.services.common import fields, multiplicity
from app.design.services.erd import mapping
from app.design.services.persistence_scope import erd_disposition
from app.validation import CheckSpec, FindingOrigin, ValidationReport, run_checks
from app.validation import Finding as ValidationFinding

BOUNDARY = "boundary"
CONTROL = "control"
ENTITY = "entity"
BCE_STEREOTYPES = (BOUNDARY, CONTROL, ENTITY)
_PASCAL_CASE = re.compile(r"^[A-Z][A-Za-z0-9]*$")


def _known_use_case_ids(state: dict[str, Any]) -> set[str]:
    scenario = state.get("usecase_spec") or {}
    if not isinstance(scenario, dict):
        return set()
    return {
        str(item.get("id")).strip()
        for item in scenario.get("use_cases") or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }


class Finding(ValidationFinding):
    """기존 UI 표시용 rule tag 동작을 보존한 typed finding이다."""

    def __init__(
        self,
        rule_id: str,
        message: str,
        location: str | None = None,
        requires_user_input: bool = False,
        origin: FindingOrigin = "deterministic",
        **data: Any,
    ) -> None:
        super().__init__(
            rule_id=rule_id,
            message=message,
            location=location,
            requires_user_input=requires_user_input,
            origin=origin,
            **data,
        )

    def as_issue(self) -> str:
        head = f"{self.location}: {self.message}" if self.location else self.message
        return f"{head} {rules.tag_of(self.rule_id)}"


def _findings_from_report(report: ValidationReport) -> list[Finding]:
    if report.errors:
        raise RuntimeError("; ".join(report.errors))
    return [Finding.model_validate(finding) for finding in report.findings]


# ---------------------------------------------------------------------------
# 모델 읽기 도우미
# ---------------------------------------------------------------------------
def _classes(model: dict[str, Any]) -> list[dict]:
    return [c for c in (model.get("Classes") or []) if isinstance(c, dict)]


def _class_method_signatures(class_item: dict[str, Any]) -> list[str]:
    """typed operation을 표시 시그니처로 투영하고 과거 산출물만 methods를 읽는다."""

    operations = class_item.get("operations")
    if isinstance(operations, list):
        return [
            operation_method_signature(
                str(operation.get("name") or ""),
                list(operation.get("parameters") or []),
                str(operation.get("returnType") or "void"),
            )
            for operation in operations
            if isinstance(operation, dict) and str(operation.get("name") or "").strip()
        ]
    return [str(method) for method in class_item.get("methods") or []]


def _relationships(model: dict[str, Any]) -> list[dict]:
    return [r for r in (model.get("Relationships") or []) if isinstance(r, dict)]


def _stereotype_of(class_item: dict) -> str:
    """`<<Control>>`·`Control`·`control` 을 전부 `control` 로 읽는다.

    렌더러의 `sanitize_stereotype`이 하는 정규화와 같은 관대함이어야 한다. 여기서 더
    엄격하면 그림에는 멀쩡히 나오는 것을 결함이라 부르게 된다.
    """
    raw = str(class_item.get("stereotype", ""))
    return raw.replace("<", "").replace(">", "").strip().lower()


def _relation_label(relationship: dict) -> str:
    """관계 하나를 가리키는 이름. 관계에는 id가 없어서 조합으로 가리킨다."""
    return "{} -> {}".format(
        relationship.get("source") or "?", relationship.get("target") or "?"
    )


# ---------------------------------------------------------------------------
# 검출기
# ---------------------------------------------------------------------------
def _dangling_endpoints(model: dict, rule_id: str, consequence: str) -> list[Finding]:
    """관계의 양 끝이 선언된 클래스인가 — **판정은 한 벌, 문구는 스테이지별로.**

    같은 결함이 두 산출물에서 **정반대로** 나타나기 때문에 문구를 나눈다. 클래스
    다이어그램에서는 PlantUML이 그 이름으로 유령 클래스를 *만들고*, ERD에서는 사상이
    그 관계를 *버린다*. 하나는 없던 것이 생기고 하나는 있던 것이 사라진다 — 고치는
    사람에게는 전혀 다른 이야기다.

    그렇다고 검출기를 두 벌 쓰면 갈라진다. 그래서 로직은 여기 하나이고, 부르는 쪽이
    자기 규칙 id와 결과 설명을 준다.
    """
    declared = {c.get("className") for c in _classes(model) if c.get("className")}
    found: list[Finding] = []
    for relationship in _relationships(model):
        label = _relation_label(relationship)
        for end in ("source", "target"):
            name = relationship.get(end)
            if name and name not in declared:
                found.append(
                    Finding(rule_id, f"{end} '{name}'가 Classes에 없음 — {consequence}", label)
                )
    return found


def relationship_endpoints(model: dict, state: dict) -> list[Finding]:
    """관계의 양 끝이 선언된 클래스인가.

    **가장 값나가는 검사다.** 매달린 끝은 조용히 실패한다:

      - PlantUML은 오류를 내지 않고 그 이름으로 **빈 클래스를 하나 만든다**(실측:
        선언 1개 + 매달린 끝 1개 → `-syntax`가 `(2 entities)`를 보고하고 통과).
      - 구현 단계는 검증을 통과한 typed 모델을 직접 사용하므로 표시 문자열을 다시 읽지는
        않지만, 잘못된 관계가 모델에 남으면 구현 계약도 존재하지 않는 클래스를 참조한다.

    즉 그림 문법 검증만으로는 막을 수 없으므로 모델을 저장하기 전에 여기서 막아야 한다.
    """
    return _dangling_endpoints(
        model,
        "class.relationship-endpoints-exist",
        "그림에 그 이름의 빈 클래스가 생긴다",
    )


def usecase_ids(model: dict, state: dict) -> list[Finding]:
    """`use_case_ids`가 입력 유스케이스의 실제 id인가 (환각 참조).

    판정은 `rtm.upstream_names`에서 나온다 — 추적표가 사후에 하는 판정과 **같은 것**이고,
    두 벌이면 갈라진다.

    ⚠ 입력에 유스케이스 id가 하나도 없으면 검사하지 않는다. 그때는 **모든** id가 unknown이
    되는데, 그건 "LLM이 지어냈다"가 아니라 "대조할 상류가 없다"는 뜻이다. 대조할 것이
    없는데 전건 위반을 내면 재생성이 고칠 수 없는 지적으로 예산만 태운다.
    """
    rule_id = "class.usecase-ids-exist"
    known = _known_use_case_ids(state)
    relationships = state.get("relationships")
    if not isinstance(relationships, dict):
        specification = state.get("usecase_spec") or {}
        relationships = specification.get("relationships") if isinstance(specification, dict) else None
    if isinstance(relationships, dict):
        known.update(
            str(item.get("use_case_id") or "").strip()
            for item in relationships.get("derived_use_cases") or []
            if isinstance(item, dict) and str(item.get("use_case_id") or "").strip()
        )
    if not known:
        return []

    found: list[Finding] = []
    for class_item in _classes(model):
        name = class_item.get("className") or "?"
        for ref in class_item.get("use_case_ids") or []:
            if ref and ref not in known:
                found.append(
                    Finding(rule_id, f"입력에 없는 유스케이스 id '{ref}'", name)
                )
    return found


def _broken_stereotypes(model: dict, rule_id: str, consequence: str = "") -> list[Finding]:
    """스테레오타입이 Boundary/Control/Entity 중 하나인가 — 판정 한 벌, 문구는 스테이지별.

    `_dangling_endpoints`와 같은 이유로 공유한다: 판정은 같고 **결과가 다르다**. 클래스
    다이어그램에서는 통신 규칙이 무판정이 되고, ERD에서는 그 표와 관계가 사라진다.
    """
    tail = f" — {consequence}" if consequence else ""
    found: list[Finding] = []
    for class_item in _classes(model):
        name = class_item.get("className") or "?"
        stereotype = _stereotype_of(class_item)
        if not stereotype:
            found.append(Finding(rule_id, f"스테레오타입 없음{tail}", name))
        elif stereotype not in BCE_STEREOTYPES:
            found.append(
                Finding(rule_id, f"BCE 밖의 스테레오타입 '{stereotype}'{tail}", name)
            )
    return found


def stereotype_is_bce(model: dict, state: dict) -> list[Finding]:
    """**통신 규칙보다 먼저 돈다.** 이게 깨지면 아래 세 규칙이 무판정이 되고, 무판정은
    겉보기에 통과와 같다.
    """
    return _broken_stereotypes(model, "class.stereotype-is-bce")


#: 금지된 (source 스테레오타입, target 스테레오타입) 조합 → (규칙 id, 왜 안 되는지).
#:
#: 방향이 뜻을 갖는 것과 안 갖는 것이 섞여 있다. Boundary-Entity 와 Boundary-Boundary 는
#: **연결 자체가** 금지라 양방향을 다 적고, Entity→Control/Boundary 는 **개시**가 금지라
#: 한 방향만 적는다 — Control→Entity 는 정상이다.
_FORBIDDEN_LINKS: dict[tuple[str, str], tuple[str, str]] = {
    (BOUNDARY, ENTITY): (
        "class.no-boundary-entity-link",
        "Boundary와 Entity를 직접 이었다 — 사이에 Control이 있어야 한다",
    ),
    (ENTITY, BOUNDARY): (
        "class.no-boundary-entity-link",
        "Entity와 Boundary를 직접 이었다 — 사이에 Control이 있어야 한다",
    ),
    (BOUNDARY, BOUNDARY): (
        "class.no-boundary-boundary-link",
        "Boundary끼리 직접 이었다 — Boundary는 액터 또는 Control과 통신한다",
    ),
    (ENTITY, CONTROL): (
        "class.entity-does-not-initiate",
        "Entity가 Control을 향해 관계를 시작했다 — Entity는 행위를 개시하지 않는다",
    ),
}


def communication_rules(model: dict, state: dict) -> list[Finding]:
    """BCE 통신 규칙 위반 (Boundary↔Entity, Boundary↔Boundary, Entity의 개시).

    Entity→Boundary는 `_FORBIDDEN_LINKS`에서 **직결 금지** 쪽으로 잡히므로 개시 규칙에서
    또 세지 않는다. 같은 관계 하나가 지적 둘이 되면 재생성이 하나를 고치고도 수가 안
    줄어 `no_improvement`로 멈춘다.

    스테레오타입이 BCE 밖이거나 양 끝이 선언되지 않은 관계는 **건너뛴다.** 그건 각각
    `stereotype_is_bce`와 `relationship_endpoints`가 이미 지적했고, 여기서 또 세면 한
    결함이 여러 지적이 된다.
    """
    stereotype_by_name = {
        c["className"]: _stereotype_of(c)
        for c in _classes(model)
        if c.get("className")
    }

    found: list[Finding] = []
    for relationship in _relationships(model):
        source = stereotype_by_name.get(relationship.get("source"))
        target = stereotype_by_name.get(relationship.get("target"))
        if source not in BCE_STEREOTYPES or target not in BCE_STEREOTYPES:
            continue
        violation = _FORBIDDEN_LINKS.get((source, target))
        if violation:
            rule_id, message = violation
            found.append(Finding(rule_id, message, _relation_label(relationship)))
    return found


def relationship_type_known(model: dict, state: dict) -> list[Finding]:
    """관계의 종류가 렌더러가 아는 다섯 중 하나인가.

    모르는 값은 그림에서 단순 연관(`-->`)이 되고, ERD 사상에서는 구조적 연관으로 세지지
    않아 관계가 통째로 사라진다. **판정 기준을 여기 다시 적지 않고** 렌더러의 표를
    그대로 쓴다 — 두 벌이면 표를 늘릴 때 판정이 안 따라온다.
    """
    rule_id = "class.relationship-type-known"
    found: list[Finding] = []
    for relationship in _relationships(model):
        kind = str(relationship.get("type") or "")
        if kind and kind not in RELATION_SYMBOLS:
            found.append(
                Finding(rule_id, f"모르는 관계 종류 '{kind}'", _relation_label(relationship))
            )
    return found


def entity_association_multiplicity(model: dict, state: dict) -> list[Finding]:
    """Entity 사이의 **구조적** 관계가 양끝 다중도를 갖고 있는가.

    행위 링크(Boundary·Control이 낀 것)와 상속은 세지 않는다. 전자는 다중도를 가질 것이
    아니고, 후자는 일반화라 UML에서도 다중도를 달지 않는다.

    스테레오타입이 BCE 밖이거나 끝이 선언되지 않은 관계도 건너뛴다 — 그건
    `stereotype_is_bce`와 `relationship_endpoints`가 이미 지적했고, 여기서 또 세면 한
    결함이 여러 지적이 된다.
    """
    rule_id = "class.entity-association-multiplicity"
    stereotype_by_name = {
        c["className"]: _stereotype_of(c) for c in _classes(model) if c.get("className")
    }

    found: list[Finding] = []
    for relationship in _relationships(model):
        if str(relationship.get("type") or "Association") not in mapping.STRUCTURAL_TYPES:
            continue
        ends = (relationship.get("source"), relationship.get("target"))
        if any(stereotype_by_name.get(end) != ENTITY for end in ends):
            continue
        label = _relation_label(relationship)
        for side in ("source", "target"):
            value = str(relationship.get(f"{side}Multiplicity") or "").strip()
            # **판정을 사상과 같은 함수로 한다.** 두 벌이면 검출기는 통과시키는데 사상은
            # 못 옮기는 어긋남이 나고, 그러면 아무 지적 없이 선이 사라진다.
            if multiplicity.is_known(value):
                continue
            # 안 적은 것과 못 읽는 것을 구별해서 말한다 — 고치는 쪽이 할 일이 다르다.
            found.append(
                Finding(rule_id, f"{side} 다중도가 없음", label)
                if not value
                else Finding(
                    rule_id,
                    f"{side} 다중도 '{value}'는 아는 표기가 아님 "
                    f"(쓸 수 있는 것: {', '.join(multiplicity.CANONICAL)})",
                    label,
                )
            )
    return found


def fields_typed(model: dict, state: dict) -> list[Finding]:
    """선언된 모든 BCE field에 Java로 표현 가능한 타입이 있는지 검사한다.

    downstream BCE generator는 타입 없는 PlantUML attribute를 유효한 Java로 표현할 수
    없고 legacy parser는 이를 ``void``로 바꾼다. 타입 없는 field는 persistence code에도
    일관되게 mapping할 수 없다.
    """
    rule_id = "class.fields-typed"
    found: list[Finding] = []
    for class_item in _classes(model):
        class_name = str(class_item.get("className") or "?")
        for raw_field in class_item.get("fields") or []:
            field_name, field_type = fields.split_field(str(raw_field))
            if field_name and not field_type:
                found.append(
                    Finding(
                        rule_id,
                        f"{class_name}.{field_name}: 필드 타입이 선언되지 않음 — 'name : Type' 형식이 필요함",
                        class_name,
                    )
                )
    return found


def _interaction_contract_findings(model: dict, state: dict) -> list[ValidationFinding]:
    # 과거 구조-only BCE 산출물은 계속 표시한다. 새 class 산출물은 미완성이어도 항상
    # Collaborations key를 저장하므로 그 key가 있을 때만 현재 interaction 계약을 적용한다.
    if "Collaborations" not in (model or {}):
        return []
    scenario = state.get("usecase_spec") if isinstance(state, dict) else None
    if not isinstance(scenario, dict):
        return []
    scenario = {**scenario, "relationships": state.get("relationships") or {}}
    report = validate_class_model(model, build_scenario_index(scenario))
    if report.errors:
        raise RuntimeError("; ".join(report.errors))
    return list(report.findings)


def operation_contract(model: dict, state: dict) -> list[Finding]:
    """현재 operation과 call-tree 저장 계약을 typed 보고서로 검증한다."""
    if (
        stereotype_is_bce(model, state)
        or names_unique(model, state)
        or name_pascal_case(model, state)
    ):
        return []
    return [
        Finding(
            "class.operation-contract-canonical",
            finding.message,
            finding.location,
            origin=finding.origin,
        )
        for finding in _interaction_contract_findings(model, state)
        if finding.rule_id != "class.collaboration.bindings"
    ]


def operation_input_producers(model: dict, state: dict) -> list[Finding]:
    """저장된 유한 input source와 producer 선행 순서를 검사한다."""
    return [
        Finding(
            "class.operation-input-producers",
            finding.message,
            finding.location,
            origin=finding.origin,
        )
        for finding in _interaction_contract_findings(model, state)
        if finding.rule_id == "class.collaboration.bindings"
    ]


def names_unique(model: dict, state: dict) -> list[Finding]:
    """클래스 이름이 유일한가 — **렌더 후 기준으로.**

    `sanitize_class_name`을 통과시킨 뒤 비교하는 것이 요점이다. `Payment Service!`와
    `Payment_Service_`는 모델에서는 다른 이름이지만 그림에서는 한 클래스가 된다. 원본만
    비교하면 그림에서 두 클래스가 합쳐진 것을 아무도 못 본다.
    """
    rule_id = "class.names-unique"
    seen: dict[str, str] = {}
    found: list[Finding] = []
    for class_item in _classes(model):
        name = class_item.get("className")
        if not name:
            continue
        rendered = sanitize_class_name(name)
        first = seen.get(rendered)
        if first is None:
            seen[rendered] = name
        elif first == name:
            found.append(Finding(rule_id, "같은 이름의 클래스가 둘 이상", name))
        else:
            found.append(
                Finding(
                    rule_id,
                    f"'{first}'와 렌더 후 같은 이름('{rendered}')이 된다",
                    name,
                )
            )
    return found


def name_pascal_case(model: dict, state: dict) -> list[Finding]:
    """클래스 이름이 PascalCase 식별자인가."""
    rule_id = "class.name-pascal-case"
    found: list[Finding] = []
    for class_item in _classes(model):
        name = class_item.get("className")
        if not name:
            found.append(Finding(rule_id, "className이 비어 있음"))
        elif not _PASCAL_CASE.match(name):
            found.append(Finding(rule_id, "PascalCase 식별자가 아님", name))
    return found


def usecase_coverage(model: dict, state: dict) -> list[Finding]:
    """입력의 모든 유스케이스가 최소 한 클래스에 붙잡혔는가.

    유스케이스를 통째로 빠뜨리는 것은 다이어그램이 조금 부실한 것이 아니다 — 설계
    다섯 장이 전부 이 모델에서 나오므로, 여기서 빠진 기능은 **설계 전체에서 사라진다.**

    ⚠ `usecase_ids`와 같은 이유로, 입력에 id가 없으면 검사하지 않는다.
    """
    rule_id = "class.covers-use-cases"
    known = _known_use_case_ids(state)
    if not known:
        return []

    claimed = {
        ref
        for class_item in _classes(model)
        for ref in (class_item.get("use_case_ids") or [])
        if ref
    }
    return [
        Finding(rule_id, f"유스케이스 '{uc}'를 가리키는 클래스가 없음")
        for uc in sorted(known - claimed)
    ]


def persistence_requires_entity(model: dict, state: dict) -> list[Finding]:
    """Persistent domain data must be represented before the class stage is accepted."""

    if erd_disposition(model, state) != "class_revision_required":
        return []
    return [Finding(
        "class.persistence-requires-entity",
        "Accepted persistence requirements are not represented by an Entity class.",
    )]

CLASS_DIAGRAM_DETECTORS: dict[str, Callable[[dict, dict], list[Finding]]] = {
    "relationship_endpoints": relationship_endpoints,
    "usecase_ids": usecase_ids,
    "stereotype_is_bce": stereotype_is_bce,
    "communication_rules": communication_rules,
    "relationship_type_known": relationship_type_known,
    "entity_association_multiplicity": entity_association_multiplicity,
    "fields_typed": fields_typed,
    "operation_contract": operation_contract,
    "operation_input_producers": operation_input_producers,
    "names_unique": names_unique,
    "name_pascal_case": name_pascal_case,
    "usecase_coverage": usecase_coverage,
    "persistence_requires_entity": persistence_requires_entity,
}


def _communication_rule_findings(
    model: dict, state: dict, rule_id: str
) -> list[Finding]:
    """여러 rule을 반환하는 legacy detector를 지정한 소유 rule 하나로 투영한다."""
    return [
        finding
        for finding in communication_rules(model, state)
        if finding.rule_id == rule_id
    ]


def class_no_boundary_entity_link(model: dict, state: dict) -> list[Finding]:
    return _communication_rule_findings(model, state, "class.no-boundary-entity-link")


def class_no_boundary_boundary_link(model: dict, state: dict) -> list[Finding]:
    return _communication_rule_findings(model, state, "class.no-boundary-boundary-link")


def class_entity_does_not_initiate(model: dict, state: dict) -> list[Finding]:
    return _communication_rule_findings(model, state, "class.entity-does-not-initiate")


CLASS_DIAGRAM_CHECKS: tuple[CheckSpec[dict, dict], ...] = (
    CheckSpec("class.relationship-endpoints-exist", relationship_endpoints),
    CheckSpec("class.usecase-ids-exist", usecase_ids),
    CheckSpec("class.stereotype-is-bce", stereotype_is_bce),
    CheckSpec("class.no-boundary-entity-link", class_no_boundary_entity_link),
    CheckSpec("class.no-boundary-boundary-link", class_no_boundary_boundary_link),
    CheckSpec("class.entity-does-not-initiate", class_entity_does_not_initiate),
    CheckSpec("class.relationship-type-known", relationship_type_known),
    CheckSpec("class.entity-association-multiplicity", entity_association_multiplicity),
    CheckSpec("class.fields-typed", fields_typed),
    CheckSpec("class.operation-contract-canonical", operation_contract),
    CheckSpec("class.operation-input-producers", operation_input_producers),
    CheckSpec("class.names-unique", names_unique),
    CheckSpec("class.name-pascal-case", name_pascal_case),
    CheckSpec("class.covers-use-cases", usecase_coverage),
    CheckSpec("class.persistence-requires-entity", persistence_requires_entity),
)


def class_diagram_validation_report(model: dict, state: dict) -> ValidationReport:
    """BCE class 모델 하나에 대한 공통 검증 근거를 typed 보고서로 반환한다."""
    return run_checks(CLASS_DIAGRAM_CHECKS, model or {}, state or {})


def class_diagram_findings(model: dict, state: dict) -> list[Finding]:
    """BCE 모델 하나에 대한 결정론 검증 전부.

    `state`가 필요한 이유는 `usecase_ids`·`usecase_coverage`가 입력 유스케이스 명세를
    봐야 해서다 — 모델만으로는 "지어낸 id"와 "정당한 id"를 구별할 수 없다.
    """
    return _findings_from_report(class_diagram_validation_report(model, state))

__all__ = [
    "CLASS_DIAGRAM_CHECKS",
    "CLASS_DIAGRAM_DETECTORS",
    "Finding",
    "class_diagram_findings",
    "class_diagram_validation_report",
    "communication_rules",
    "name_pascal_case",
    "names_unique",
    "persistence_requires_entity",
    "relationship_endpoints",
    "stereotype_is_bce",
    "usecase_coverage",
    "usecase_ids",
]
