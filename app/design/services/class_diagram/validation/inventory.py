"""전역 BCE 인벤토리의 이름·타입·관계·범위를 결정론적으로 검사한다."""
from __future__ import annotations

import re
from typing import Any

from app.design.services.class_diagram.scenario import ScenarioIndex, text
from app.design.services.class_diagram.type_system import (
    field_name,
    field_type,
    type_is_resolved,
)
from app.design.services.class_diagram.validation.model import class_name
from app.design.services.common import fields, multiplicity
from app.validation import CheckSpec, Finding, ValidationReport, run_checks


def _inventory_names(
    inventory: dict[str, Any], _index: ScenarioIndex,
) -> list[Finding]:
    findings: list[Finding] = []
    names: list[str] = []
    for item in inventory.get("Classes") or []:
        if not isinstance(item, dict):
            continue
        name = class_name(item)
        names.append(name)
        if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", name) or "unknownclass" in name.casefold():
            findings.append(Finding(
                "class.inventory.names", "className must be concrete PascalCase", name,
            ))
    for item in inventory.get("DataTypes") or []:
        if isinstance(item, dict):
            names.append(text(item.get("name")))
    if len(names) != len(set(names)):
        findings.append(Finding(
            "class.inventory.names", "class and DataType names must be unique",
        ))
    return findings


def _inventory_types(
    inventory: dict[str, Any], _index: ScenarioIndex,
) -> list[Finding]:
    classes = {
        class_name(item): item for item in inventory.get("Classes") or []
        if isinstance(item, dict)
    }
    data_types = {
        text(item.get("name")): item for item in inventory.get("DataTypes") or []
        if isinstance(item, dict)
    }
    declared = set(classes) | set(data_types)
    entities = {
        name for name, item in classes.items()
        if text(item.get("stereotype")) == "Entity"
    }
    named_types = {
        name: text(item.get("kind")) for name, item in data_types.items()
    }
    findings: list[Finding] = []
    for name, item in classes.items():
        stereotype = text(item.get("stereotype"))
        raw_fields = list(item.get("fields") or [])
        identifiers = list(item.get("identifier") or [])
        values = list(item.get("values") or [])
        if stereotype == "Entity" and (not raw_fields or values):
            findings.append(Finding(
                "class.inventory.types", "Entity requires typed persistent fields and no literals", name,
            ))
        if stereotype in {"Boundary", "Control"} and (
            raw_fields or identifiers or values
        ):
            findings.append(Finding(
                "class.inventory.types", "Boundary and Control cannot retain fields, identifiers, or literals", name,
            ))
        field_names = {field_name(value) for value in raw_fields}
        for value in raw_fields:
            resolved = bool(field_name(value)) and type_is_resolved(
                field_type(value), declared, allow_void=False,
            )
            if not resolved:
                findings.append(Finding(
                    "class.inventory.types", f"unresolved field declaration: {value}", name,
                ))
            elif stereotype == "Entity" and not fields.entity_field_is_erd_projectable(
                field_type(value),
                entity_names=entities,
                named_types=named_types,
            ):
                findings.append(Finding(
                    "class.inventory.types",
                    f"Entity field type cannot be projected to the relational model: {value}",
                    name,
                ))
        if not set(identifiers) <= field_names:
            findings.append(Finding(
                "class.inventory.types", "Entity identifiers must name declared fields", name,
            ))
    for name, item in data_types.items():
        kind = text(item.get("kind"))
        raw_fields = list(item.get("fields") or [])
        values = list(item.get("values") or [])
        identifiers = list(item.get("identifier") or [])
        if kind == "valueObject" and (
            not raw_fields or values or identifiers
        ):
            findings.append(Finding(
                "class.inventory.types", "valueObject requires typed fields only", name,
            ))
        if kind == "enumeration" and (raw_fields or identifiers or not values):
            findings.append(Finding(
                "class.inventory.types", "enumeration requires values and no fields", name,
            ))
        for value in raw_fields:
            if not field_name(value) or not type_is_resolved(
                field_type(value), declared, allow_void=False,
            ):
                findings.append(Finding(
                    "class.inventory.types", f"unresolved DataType field: {value}", name,
                ))
    return findings


def _inventory_relationships(
    inventory: dict[str, Any], _index: ScenarioIndex,
) -> list[Finding]:
    entities = {
        class_name(item) for item in inventory.get("Classes") or []
        if isinstance(item, dict) and text(item.get("stereotype")) == "Entity"
    }
    findings: list[Finding] = []
    seen: set[frozenset[str]] = set()
    for relationship in inventory.get("Relationships") or []:
        if not isinstance(relationship, dict):
            continue
        source = text(relationship.get("source"))
        target = text(relationship.get("target"))
        location = f"{source}->{target}"
        if source not in entities or target not in entities:
            findings.append(Finding(
                "class.inventory.relationships",
                "structural relationships may connect only Entity classes",
                location,
            ))
        for side in ("source", "target"):
            value = text(relationship.get(f"{side}Multiplicity"))
            if not value:
                findings.append(Finding(
                    "class.inventory.relationships",
                    f"{side} endpoint multiplicity is required",
                    location,
                ))
            elif not multiplicity.is_known(value):
                findings.append(Finding(
                    "class.inventory.relationships",
                    f"{side} endpoint multiplicity '{value}' is unknown; use one of "
                    f"{', '.join(multiplicity.CANONICAL)}",
                    location,
                ))
        pair = frozenset((source, target))
        if pair in seen:
            findings.append(Finding(
                "class.inventory.relationships",
                "one semantic relationship must not be emitted in both directions",
                location,
            ))
        seen.add(pair)
    return findings


def _inventory_scope(
    inventory: dict[str, Any], index: ScenarioIndex,
) -> list[Finding]:
    """각 operation prompt가 inventory에 선언된 유스케이스 범위를 넘지 않는지 검사한다."""

    known = {use_case.id for use_case in index.use_cases}
    findings: list[Finding] = []
    items = [
        item for item in inventory.get("Classes") or [] if isinstance(item, dict)
    ]
    scopes = {
        class_name(item) or text(item.get("name")): set(item.get("useCaseIds") or [])
        for item in items
    }
    for name, scope in scopes.items():
        if not scope or not scope <= known:
            findings.append(Finding(
                "class.inventory.scope",
                "useCaseIds must be a non-empty subset of supplied use cases",
                name,
            ))
    for use_case in index.use_cases:
        selected = [
            item for item in inventory.get("Classes") or []
            if isinstance(item, dict) and use_case.id in set(item.get("useCaseIds") or [])
        ]
        stereotypes = {text(item.get("stereotype")) for item in selected}
        if use_case.primary_actor and "Boundary" not in stereotypes:
            findings.append(Finding(
                "class.inventory.scope",
                "actor-driven use case scope requires a Boundary candidate",
                use_case.id,
            ))
        if "Control" not in stereotypes:
            findings.append(Finding(
                "class.inventory.scope",
                "use case scope requires a Control candidate",
                use_case.id,
            ))
    return findings


# 값싼 이름·타입 검사부터 관계·유스케이스 범위 순서로 실행한다. 각 함수는 등록된
# rule_id 하나만 발생시키며 proposal을 정규화하거나 수정하지 않는다.
INVENTORY_CHECKS = (
    CheckSpec("class.inventory.names", _inventory_names),
    CheckSpec("class.inventory.types", _inventory_types),
    CheckSpec("class.inventory.relationships", _inventory_relationships),
    CheckSpec("class.inventory.scope", _inventory_scope),
)


def validate_inventory(
    inventory: dict[str, Any], index: ScenarioIndex
) -> ValidationReport:
    """수락 전 인벤토리를 변경하지 않고 검사한다.

    Args:
        inventory: 별칭 JSON 형태의 BCE 인벤토리 후보다.
        index: 허용된 유스케이스 식별자를 제공하는 시나리오 인덱스다.

    Returns:
        이름, 타입, 관계, 범위 규칙을 등록 순서로 담은 보고서다.

    Notes:
        finding은 inventory service가 누적 이력을 포함한 전체 교체 repair 입력으로 바꾼다.
        이 함수 자체는 LLM 호출이나 repair 예산을 소유하지 않는다.
    """
    return run_checks(INVENTORY_CHECKS, inventory or {}, index)


__all__ = ["INVENTORY_CHECKS", "validate_inventory"]
