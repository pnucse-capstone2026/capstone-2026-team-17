"""완성된 BCE 클래스 모델의 저장 계약과 클래스 간 협업 규칙을 검증한다."""
from __future__ import annotations

import re
from typing import Any

from app.design.schemas.class_model import BCEModel
from app.design.services.class_diagram.scenario import ScenarioIndex, text
from app.validation import CheckSpec, Finding, ValidationReport, run_checks


def class_name(item: dict[str, Any]) -> str:
    return text(item.get("className") or item.get("class_name"))


def runtime_value_source(type_expression: str) -> str:
    """시간 타입에 허용되는 명시적 런타임 값 출처를 반환한다."""

    normalized = re.sub(r"\s+", "", text(type_expression)).casefold()
    normalized = normalized.removeprefix("java.time.")
    return {
        "date": "runtime#currentDate",
        "localdate": "runtime#currentDate",
        "datetime": "runtime#currentDateTime",
        "localdatetime": "runtime#currentDateTime",
        "offsetdatetime": "runtime#currentDateTime",
        "zoneddatetime": "runtime#currentDateTime",
        "instant": "runtime#currentInstant",
        "timestamp": "runtime#currentInstant",
    }.get(normalized, "")


def type_can_default(type_expression: str) -> bool:
    normalized = re.sub(r"\s+", "", text(type_expression)).casefold()
    return normalized.startswith("optional<") or normalized.startswith("optional[")


def optional_inner_type(type_expression: str) -> str:
    normalized = re.sub(r"\s+", "", text(type_expression))
    match = re.fullmatch(r"(?i:optional)[<\[](.+)[>\]]", normalized)
    return match.group(1) if match else ""


def derived_value_source(target_type: str, field_sources: dict[str, str]) -> str:
    assignments = ",".join(
        f"{name}={field_sources[name]}" for name in sorted(field_sources)
    )
    return f"derived#{target_type}({assignments})"


def derived_value_parts(source_ref: str) -> tuple[str, dict[str, str]]:
    match = re.fullmatch(r"derived#([A-Za-z_][A-Za-z0-9_]*)\((.*)\)", source_ref)
    if not match:
        return "", {}
    assignments: dict[str, str] = {}
    if match.group(2):
        for raw in match.group(2).split(","):
            name, separator, value = raw.partition("=")
            if not separator or not name or not value or name in assignments:
                return "", {}
            assignments[name] = value
    return match.group(1), assignments


def operation_catalog(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for class_item in model.get("Classes") or []:
        if not isinstance(class_item, dict):
            continue
        owner = class_name(class_item)
        stereotype = text(class_item.get("stereotype")).casefold()
        for operation in class_item.get("operations") or []:
            if not isinstance(operation, dict):
                continue
            operation_id = text(operation.get("operationId"))
            if operation_id:
                result[operation_id] = {
                    **operation,
                    "className": owner,
                    "stereotype": stereotype,
                }
    return result


def _model_schema(model: dict[str, Any], _index: ScenarioIndex) -> list[Finding]:
    try:
        BCEModel.model_validate(model)
    except Exception as error:  # Pydantic supplies the exact schema location.
        return [Finding("class.model.schema", str(error), "BCEModel", origin="schema")]
    return []


def _collaborations(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        text(item.get("collaborationId")): item
        for item in model.get("Collaborations") or []
        if isinstance(item, dict)
    }


def _collaboration_coverage(
    model: dict[str, Any], index: ScenarioIndex,
) -> list[Finding]:
    collaborations = _collaborations(model)
    expected = {
        use_case.id for use_case in index.use_cases
        if any(group.use_case_id == use_case.id for group in index.groups)
    }
    if set(collaborations) == expected:
        return []
    return [
        Finding(
            "class.model.collaboration-coverage",
            f"collaborations must exactly cover standalone use cases; missing={sorted(expected - set(collaborations))}, extra={sorted(set(collaborations) - expected)}",
            "Collaborations",
        )
    ]


def _collaboration_rule(
    rule_id: str,
) -> CheckSpec[dict[str, Any], ScenarioIndex]:
    """협업 검사를 완성 모델의 유스케이스 순서로 실행한다.

    협업 검증 모듈이 이 모듈의 타입 도우미를 사용하므로 import는 실행 시점에 한다.
    각 검사기는 자신의 ``rule_id``만 반환하며, 등록 순서가 보고서 순서가 된다.
    """
    def check(model: dict[str, Any], index: ScenarioIndex) -> list[Finding]:
        from app.design.services.class_diagram.validation.collaboration import (
            CollaborationContext,
            _collaboration_ancestor_result_bindings,
            _collaboration_bindings,
            _collaboration_contract,
        )

        rules = {
            "class.collaboration.contract": _collaboration_contract,
            "class.collaboration.bindings": _collaboration_bindings,
            "class.collaboration.ancestor-result-binding": _collaboration_ancestor_result_bindings,
        }
        owned_check = rules[rule_id]
        collaborations = _collaborations(model)
        findings: list[Finding] = []
        for use_case in index.use_cases:
            collaboration = collaborations.get(use_case.id)
            if collaboration:
                context = CollaborationContext(index, model, use_case)
                findings.extend(owned_check(collaboration, context))
        return findings

    return CheckSpec(rule_id=rule_id, run=check)


def _boundary_public_object_parameters(
    model: dict[str, Any], index: ScenarioIndex,
) -> list[Finding]:
    """Reject an opaque public ``Object`` only when one UC owns the operation.

    A Boundary signature is the public hand-off to downstream API and code
    generation.  ``Object`` there loses the shape which those stages need.  We
    deliberately require an exact, single owner from persisted ``stepRefs``;
    without it a fragment repair would not have a safe UC-local scope.
    """

    known_use_cases = {use_case.id for use_case in index.use_cases}
    findings: list[Finding] = []
    for class_item in model.get("Classes") or []:
        if not isinstance(class_item, dict):
            continue
        if text(class_item.get("stereotype")).casefold() != "boundary":
            continue
        for operation in class_item.get("operations") or []:
            if not isinstance(operation, dict):
                continue
            use_case_ids = {
                step_ref.split(":", 1)[0]
                for value in operation.get("stepRefs") or []
                if (step_ref := text(value))
                and ":" in step_ref
                and step_ref.split(":", 1)[0] in known_use_cases
            }
            if len(use_case_ids) != 1:
                continue
            for parameter in operation.get("parameters") or []:
                if not isinstance(parameter, dict) or text(parameter.get("type")) != "Object":
                    continue
                findings.append(Finding(
                    "class.boundary-public-object-parameter",
                    "Boundary public parameter uses opaque Object; replace it with a named "
                    "scenario-grounded valueObject while preserving its requiredValueRef.",
                    next(iter(use_case_ids)),
                ))
    return findings


# schema와 유스케이스 coverage를 확인한 뒤 실제 호출 참조와 binding만 다시 검사한다.
CLASS_MODEL_CHECKS: tuple[CheckSpec[dict[str, Any], ScenarioIndex], ...] = (
    CheckSpec("class.model.schema", _model_schema),
    CheckSpec("class.model.collaboration-coverage", _collaboration_coverage),
    _collaboration_rule("class.collaboration.contract"),
    _collaboration_rule("class.collaboration.bindings"),
    _collaboration_rule("class.collaboration.ancestor-result-binding"),
    CheckSpec("class.boundary-public-object-parameter", _boundary_public_object_parameters),
)


def validate_class_model(
    model: BCEModel | dict[str, Any], index: ScenarioIndex
) -> ValidationReport:
    """저장할 BCE 모델 전체를 결정론적으로 검증한다.

    Args:
        model: 타입 모델 또는 별칭을 사용하는 저장 JSON이다.
        index: 유스케이스와 실행 그룹의 정규화된 입력이다.

    Returns:
        규칙 등록 순서로 정렬된 finding을 담은 불변 보고서다.

    Notes:
        검증은 모델을 수정하거나 repair를 시작하지 않는다. repair 여부는 서비스가
        보고서를 받은 뒤 수정 대상별로 결정한다.
    """
    if isinstance(model, BCEModel):
        payload = model.model_dump(by_alias=True)
    else:
        payload = model
        try:
            BCEModel.model_validate(payload)
        except Exception:  # 정확한 위치와 메시지는 등록된 schema 검사가 소유한다.
            return run_checks(CLASS_MODEL_CHECKS[:1], payload, index)
    return run_checks(CLASS_MODEL_CHECKS, payload, index)


__all__ = [
    "CLASS_MODEL_CHECKS",
    "class_name",
    "derived_value_parts",
    "derived_value_source",
    "operation_catalog",
    "optional_inner_type",
    "runtime_value_source",
    "type_can_default",
    "validate_class_model",
]
