"""LLM 제안, 저장 모델과 provenance 검사가 공유하는 설계 타입 규칙이다.

입력은 문자열 타입 표현과 수락된 class/DataType catalog이고 출력은 구문·참조 해석 결과다.
primitive와 generic container의 닫힌 vocabulary를 한 곳에서 제공하여 prompt와 validator가
서로 다른 타입을 허용하지 않게 한다. 이 모듈은 순수 함수만 제공하며 LLM이나 state를
참조하지 않는다.
"""
from __future__ import annotations

import re
from typing import Any

from app.design.contracts.type_system import (
    PROMPT_CONTAINERS,
    PROMPT_PRIMITIVES,
    DesignTypeError,
    parse_type_expression,
    referenced_names,
    types_equivalent,
)

PRIMITIVES = PROMPT_PRIMITIVES
GENERIC_CONTAINERS = PROMPT_CONTAINERS
TYPE_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def type_expression_is_well_formed(type_name: str) -> bool:
    """중첩 generic, 배열과 optional suffix를 가진 설계 타입 구문인지 검사한다."""

    try:
        parse_type_expression(type_name)
    except DesignTypeError:
        return False
    return True


def structure_type_inventory() -> dict[str, tuple[str, ...] | str]:
    """구조 proposal prompt와 validator가 함께 쓰는 닫힌 타입 vocabulary를 반환한다."""
    return {
        "primitives": tuple(sorted(PRIMITIVES)),
        "genericContainers": tuple(sorted(GENERIC_CONTAINERS)),
        "arraySyntax": "byte[]",
    }


def structure_type_contract() -> str:
    """닫힌 vocabulary를 BCE 구조 생성 prompt에 넣을 영어 계약 문장으로 만든다."""
    inventory = structure_type_inventory()
    primitives = ", ".join(inventory["primitives"])
    containers = ", ".join(inventory["genericContainers"])
    return (
        "Type references are closed. Allowed primitive tokens: "
        f"{primitives}. Allowed generic container tokens: {containers}. "
        "For binary or array values use the recognized `byte[]` syntax or one of those "
        "containers; do not invent an array alias. Every other type token must exactly name "
        "a declared Class or DataType in this same proposal."
    )


def referenced_type_names(type_name: str) -> set[str]:
    """primitive/container를 제외하고 선언 해소가 필요한 타입 이름만 반환한다."""

    try:
        return referenced_names(parse_type_expression(type_name))
    except DesignTypeError:
        return {
            token for token in TYPE_TOKEN.findall(type_name)
            if token.casefold() not in PRIMITIVES | GENERIC_CONTAINERS
        }


def reachable_data_type_names(
    classes: list[dict[str, Any]], data_types: list[dict[str, Any]],
) -> set[str]:
    """class contract에서 전이적으로 도달 가능한 DataType 이름을 반환한다.

    class field와 operation signature가 root다. 도달한 valueObject의 field가 다른
    DataType을 가리키면 계속 따라간다. 자기 참조뿐이거나 다른 root와 연결되지 않은 순환
    선언은 실행 가능한 class contract의 일부가 아니다.
    """

    declared = {
        str(item.get("name") or "").strip(): item
        for item in data_types
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    pending: set[str] = set()
    for class_item in classes:
        if not isinstance(class_item, dict):
            continue
        for field in class_item.get("fields") or []:
            pending.update(referenced_type_names(field_type(field)) & declared.keys())
        for operation in class_item.get("operations") or []:
            if not isinstance(operation, dict):
                continue
            for parameter in operation.get("parameters") or []:
                if isinstance(parameter, dict):
                    pending.update(
                        referenced_type_names(str(parameter.get("type") or ""))
                        & declared.keys()
                    )
            pending.update(
                referenced_type_names(str(
                    operation.get("returnType")
                    or operation.get("return_type")
                    or ""
                ))
                & declared.keys()
            )

    reachable: set[str] = set()
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        for field in declared[name].get("fields") or []:
            pending.update(
                (referenced_type_names(field_type(field)) & declared.keys()) - reachable
            )
    return reachable


def type_is_resolved(type_name: str, names: set[str], *, allow_void: bool) -> bool:
    """타입 구문이 유효하고 모든 사용자 정의 token이 허용 이름으로 해소되는지 검사한다."""

    try:
        expression = parse_type_expression(type_name)
    except DesignTypeError:
        return False
    if not allow_void and expression.kind == "scalar" and expression.name == "void":
        return False
    if "unknownclass" in str(type_name or "").casefold().replace(" ", ""):
        return False
    return referenced_names(expression) <= names


def field_type(field: object) -> str:
    """영속 ``name : Type`` field에서 타입 부분을 가져온다."""
    text = " ".join(str(field or "").split())
    return text.rpartition(":")[2].strip() if ":" in text else ""


def field_name(field: object) -> str:
    """영속 ``name : Type`` field에서 선언 이름을 가져온다."""

    text = " ".join(str(field or "").split())
    return text.partition(":")[0].strip() if ":" in text else ""


def structured_field_types(model: dict[str, Any]) -> dict[str, dict[str, str]]:
    """선언 class와 valueObject의 접근 가능한 field를 타입별로 인덱싱한다.

    collaboration은 이미 가진 구조 값의 field를 새 producer 없이 전달할 수 있다. 이
    인덱스를 수락 모델에서만 파생해 provenance 후보를 유한하고 독립적으로 검증 가능하게
    유지한다.
    """

    result: dict[str, dict[str, str]] = {}
    for item in [*(model.get("Classes") or []), *(model.get("DataTypes") or [])]:
        if not isinstance(item, dict):
            continue
        type_name = str(item.get("className") or item.get("name") or "").strip()
        if not type_name:
            continue
        fields = {
            field_name(field): field_type(field)
            for field in item.get("fields") or []
            if field_name(field) and field_type(field)
        }
        if fields:
            result[type_name] = fields
    return result


def types_compatible(left: str, right: str) -> bool:
    """별칭과 표기 차이를 제거한 canonical 설계 타입 의미를 비교한다."""

    return types_equivalent(left, right)


def required_value_type_compatible(
    required_value: dict[str, Any], target_type: str, data_types: list[dict[str, Any]],
) -> bool:
    """Check a required-value type, allowing constrained local DTO refinement.

    Ordinary type compatibility remains strict. The exception requires a catalog valueRef,
    an epistemically broad ``Object`` or ``unknown`` declaration, and an exact named
    nonempty local valueObject/dataType declaration.
    """

    source_type = str(required_value.get("designType") or "").strip()
    if types_compatible(source_type, target_type):
        return True
    if (
        source_type.casefold() not in {"object", "unknown"}
        or not str(required_value.get("valueRef") or "").strip()
    ):
        return False

    return _is_nonempty_local_value_refinement(target_type, data_types)


def _is_nonempty_local_value_refinement(
    target_type: str, data_types: list[dict[str, Any]],
) -> bool:
    """Whether ``target_type`` is an exact, usable locally declared structured value."""

    target_name = str(target_type or "").strip()
    return any(
        isinstance(item, dict)
        and str(item.get("name") or "").strip() == target_name
        and str(item.get("kind") or "").casefold() in {"valueobject", "datatype"}
        and bool(item.get("fields"))
        for item in data_types
    )


def projected_field_type(
    root_type: str, path: str, fields_by_type: dict[str, dict[str, str]],
) -> str:
    """``details.offeringId`` 같은 점 경로가 가리키는 최종 field 타입을 해소한다."""

    current_type = str(root_type or "").strip()
    for component in (part.strip() for part in str(path or "").split(".")):
        if not component:
            return ""
        type_name = next(
            (name for name in fields_by_type if types_compatible(name, current_type)),
            "",
        )
        if not type_name:
            return ""
        current_type = fields_by_type[type_name].get(component, "")
        if not current_type:
            return ""
    return current_type
