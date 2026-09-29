"""유스케이스 전체의 여러 actor root와 parameter provenance를 검사한다."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.design.services.class_diagram.scenario import (
    ExecutionGroup,
    ScenarioIndex,
    UseCase,
    text,
)
from app.design.services.class_diagram.trusted_context import (
    directly_available_sources,
    required_value_catalog,
)
from app.design.services.class_diagram.type_system import (
    required_value_type_compatible,
    structured_field_types,
    types_compatible,
)
from app.design.services.class_diagram.validation.model import (
    derived_value_parts,
    operation_catalog,
    optional_inner_type,
    runtime_value_source,
    type_can_default,
)
from app.validation import CheckSpec, Finding, ValidationReport, run_checks


@dataclass(frozen=True)
class CollaborationContext:
    """협업 검사를 한 유스케이스와 수락 model에 고정한다."""

    index: ScenarioIndex
    model: dict[str, Any]
    use_case: UseCase

    @property
    def groups(self) -> tuple[ExecutionGroup, ...]:
        """actor entry별 단계 범위는 기존 ScenarioIndex 계산을 재사용한다."""

        return tuple(
            group for group in self.index.groups
            if group.use_case_id == self.use_case.id
        )


def _root_positions(calls: list[dict[str, Any]]) -> list[int]:
    return [
        position for position, call in enumerate(calls)
        if not text(call.get("parentCallId"))
    ]


def _root_index(calls: list[dict[str, Any]], position: int) -> int | None:
    by_id = {text(call.get("callId")): index for index, call in enumerate(calls)}
    current = position
    visited: set[int] = set()
    while current not in visited:
        visited.add(current)
        parent_id = text(calls[current].get("parentCallId"))
        if not parent_id:
            return current
        parent = by_id.get(parent_id)
        if parent is None or parent >= current:
            return None
        current = parent
    return None


def _collaboration_contract(
    collaboration: dict[str, Any], context: CollaborationContext,
) -> list[Finding]:
    """참조, root 순서, 단계 범위와 최소 BCE 호출 방향만 검사한다."""

    operations = operation_catalog(context.model)
    calls = [item for item in collaboration.get("calls") or [] if isinstance(item, dict)]
    findings: list[Finding] = []
    location = context.use_case.id
    if text(collaboration.get("collaborationId")) != context.use_case.id:
        findings.append(Finding(
            "class.collaboration.contract",
            "collaborationId does not match its use case",
            location,
        ))
    groups = context.groups
    roots = _root_positions(calls)
    if len(roots) != len(groups):
        findings.append(Finding(
            "class.collaboration.contract",
            "root calls must match actor entry groups in scenario order",
            location,
        ))
    call_by_id = {text(call.get("callId")): call for call in calls}
    covered_by_group: list[set[str]] = [set() for _group in groups]
    root_ordinal = {position: ordinal for ordinal, position in enumerate(roots)}
    control_by_group = [False for _group in groups]
    direct_control_handoffs = [0 for _group in groups]
    for position, call in enumerate(calls):
        call_id = text(call.get("callId"))
        operation = operations.get(text(call.get("receiverOperationId")))
        if operation is None:
            findings.append(Finding(
                "class.collaboration.contract", "call receiver operation does not exist", call_id,
            ))
            continue
        parent_id = text(call.get("parentCallId"))
        if parent_id and parent_id not in {
            text(previous.get("callId")) for previous in calls[:position]
        }:
            findings.append(Finding(
                "class.collaboration.contract", "parentCallId must reference an earlier call", call_id,
            ))
        root_position = _root_index(calls, position)
        ordinal = root_ordinal.get(root_position, -1) if root_position is not None else -1
        if ordinal < 0 or ordinal >= len(groups):
            continue
        group = groups[ordinal]
        latest_root = max((root for root in roots if root <= position), default=-1)
        if root_position != latest_root:
            findings.append(Finding(
                "class.collaboration.contract",
                "a call cannot return to an earlier actor root",
                call_id,
            ))
        declared = {text(ref) for ref in operation.get("stepRefs") or []}
        refs = {text(ref) for ref in call.get("stepRefs") or []}
        if not refs or not refs <= declared or not refs <= set(group.required_step_ids):
            findings.append(Finding(
                "class.collaboration.contract", "call stepRefs are outside its actor entry scope", call_id,
            ))
        covered_by_group[ordinal].update(refs)
        expected = {
            text(parameter.get("name")) for parameter in operation.get("parameters") or []
            if isinstance(parameter, dict)
        }
        bound = {
            text(binding.get("parameter")) for binding in call.get("argumentBindings") or []
            if isinstance(binding, dict)
        }
        if bound != expected:
            findings.append(Finding(
                "class.collaboration.contract", "argumentBindings must match receiver parameters", call_id,
            ))
        if position == root_position:
            if text(operation.get("stereotype")) != "boundary":
                findings.append(Finding(
                    "class.collaboration.contract", "actor entry must start at Boundary", call_id,
                ))
            if group.actor_step and group.actor_step not in refs:
                findings.append(Finding(
                    "class.collaboration.contract", "root call must cover its actor entry step", call_id,
                ))
        if text(operation.get("stereotype")) == "control":
            control_by_group[ordinal] = True
        parent = call_by_id.get(parent_id)
        if parent:
            parent_operation = operations.get(text(parent.get("receiverOperationId")), {})
            root_operation = (
                operations.get(text(calls[root_position].get("receiverOperationId")), {})
                if root_position is not None else {}
            )
            source = text(parent_operation.get("stereotype"))
            target = text(operation.get("stereotype"))
            if source == "boundary" and target == "control":
                direct_control_handoffs[ordinal] += 1
            if (
                (source == "boundary" and target != "control")
                or (source == "entity" and target != "entity")
                or (
                    source == "control"
                    and target == "boundary"
                    and text(operation.get("className"))
                    == text(root_operation.get("className"))
                )
            ):
                findings.append(Finding(
                    "class.collaboration.contract",
                    "Boundary must hand off to Control; Entity may call only another "
                    "Entity; a root Boundary returns results instead of being called again",
                    call_id,
                ))
    for ordinal, group in enumerate(groups):
        if direct_control_handoffs[ordinal] != 1:
            findings.append(Finding(
                "class.collaboration.contract",
                "each actor entry Boundary must hand off directly to exactly one Control",
                group.id,
            ))
        if set(group.required_step_ids) - covered_by_group[ordinal]:
            findings.append(Finding(
                "class.collaboration.contract",
                "actor entry root does not cover every required step",
                group.id,
            ))
        if not control_by_group[ordinal]:
            findings.append(Finding(
                "class.collaboration.contract",
                "Boundary must delegate each actor entry flow to Control",
                group.id,
            ))
    # operation 단계가 이미 "이 흐름은 지속 상태를 사용한다"고 확정한 경우에만
    # 실제 호출 누락을 잡는다. Entity operation 자체가 없는 계산·외부 연동 흐름에는
    # 아무 조건도 추가하지 않는다.
    required_steps = {
        step_id for group in groups for step_id in group.required_step_ids
    }
    entity_operations = {
        operation_id
        for operation_id, operation in operations.items()
        if text(operation.get("stereotype")) == "entity"
        and required_steps & {text(ref) for ref in operation.get("stepRefs") or []}
    }
    called_operations = {
        text(call.get("receiverOperationId")) for call in calls
    }
    if entity_operations and not entity_operations & called_operations:
        findings.append(Finding(
            "class.collaboration.contract",
            "accepted Entity behavior for durable domain information must be called by Control",
            location,
        ))
    return findings


def _source_type(
    source_ref: str,
    previous_calls: list[dict[str, Any]],
    operations: dict[str, dict[str, Any]],
    fields_by_type: dict[str, dict[str, str]],
    model: dict[str, Any] | None = None,
) -> str:
    derived_type, field_sources = derived_value_parts(source_ref)
    if derived_type:
        target_fields = fields_by_type.get(derived_type, {})
        if not target_fields or not set(field_sources) <= set(target_fields):
            return ""
        for name, expected in target_fields.items():
            nested = field_sources.get(name, "")
            if not nested:
                if type_can_default(expected):
                    continue
                return ""
            if nested == runtime_value_source(expected):
                continue
            actual = _source_type(
                nested, previous_calls, operations, fields_by_type, model,
            )
            if not actual or not types_compatible(actual, expected):
                return ""
        return derived_type
    source_id, separator, path = source_ref.partition("#")
    if not separator:
        return ""
    if ":precondition:" in source_id:
        return "__precondition__"
    source_call = next(
        (call for call in previous_calls if text(call.get("stableId")) == source_id), None,
    )
    if source_call is None:
        return "__entry__"
    operation = operations.get(text(source_call.get("receiverOperationId")), {})
    if path == "result" or path.startswith("result."):
        source_type = text(operation.get("returnType"))
        field_path = path.removeprefix("result.") if path.startswith("result.") else ""
    else:
        parameter_ref, dot, field_path = path.partition(".")
        source_type = next((
            text(parameter.get("type")) for parameter in operation.get("parameters") or []
            if isinstance(parameter, dict)
            and text(parameter.get("stableRef")) == parameter_ref
        ), "")
        if not dot:
            field_path = ""
    field_types_by_owner: dict[str, dict[str, str]] = {}
    for item in [
        *((model or {}).get("Classes") or []),
        *((model or {}).get("DataTypes") or []),
    ]:
        if not isinstance(item, dict):
            continue
        owner = text(item.get("className") or item.get("name"))
        refs = {
            text(field_ref): text(field).partition(":")[2].strip()
            for field, field_ref in zip(
                item.get("fields") or [], item.get("fieldRefs") or [],
            )
            if text(field_ref)
        }
        if owner and refs:
            field_types_by_owner[owner] = refs
    # optional<T> 자체뿐 아니라 그 안의 field도 명시적으로 unwrap한 뒤 참조한다.
    # ``result.unwrap.id``는 optional<User> 결과의 User.id를 뜻한다.
    if field_path.startswith("unwrap."):
        source_type = optional_inner_type(source_type)
        if not source_type:
            return ""
        field_path = field_path.removeprefix("unwrap.")
    unwrap = field_path == "unwrap" or field_path.endswith(".unwrap")
    if unwrap:
        field_path = "" if field_path == "unwrap" else field_path.removesuffix(".unwrap")
    if field_path:
        resolved = source_type
        for field_ref in field_path.split("."):
            owner_fields = next((
                fields for owner, fields in field_types_by_owner.items()
                if types_compatible(owner, resolved)
            ), {})
            resolved = owner_fields.get(field_ref, "")
            if not resolved:
                return ""
    else:
        resolved = source_type
    return optional_inner_type(resolved) if unwrap else resolved


def _collaboration_bindings(
    collaboration: dict[str, Any], context: CollaborationContext,
) -> list[Finding]:
    operations = operation_catalog(context.model)
    fields_by_type = structured_field_types(context.model)
    calls = [item for item in collaboration.get("calls") or [] if isinstance(item, dict)]
    roots = _root_positions(calls)
    root_ordinal = {position: ordinal for ordinal, position in enumerate(roots)}
    findings: list[Finding] = []
    for position, call in enumerate(calls):
        operation = operations.get(text(call.get("receiverOperationId")), {})
        parameter_types = {
            text(parameter.get("name")): text(parameter.get("type"))
            for parameter in operation.get("parameters") or [] if isinstance(parameter, dict)
        }
        parameter_stable_refs = {
            text(parameter.get("name")): text(parameter.get("stableRef"))
            for parameter in operation.get("parameters") or [] if isinstance(parameter, dict)
        }
        root_position = _root_index(calls, position)
        ordinal = root_ordinal.get(root_position, -1) if root_position is not None else -1
        actor_step = (
            context.groups[ordinal].actor_step
            if 0 <= ordinal < len(context.groups) else None
        )
        for binding in call.get("argumentBindings") or []:
            if not isinstance(binding, dict):
                continue
            parameter = text(binding.get("parameter"))
            source_ref = text(binding.get("sourceRef"))
            expected = parameter_types.get(parameter, "")
            # Rebuild the same finite source catalog used by materialization.
            # This is intentionally a local import: collaboration imports this
            # validation module for the final report, while validation must also
            # reject hand-authored bindings that were never eligible.
            from app.design.services.class_diagram.collaboration import _binding_candidates

            eligible = set(_binding_candidates(
                context.model,
                context.use_case,
                actor_step,
                position == root_position,
                calls,
                position,
                {
                    "name": parameter,
                    "type": expected,
                    "stableRef": parameter_stable_refs.get(parameter, ""),
                    "requiredValueRef": next(
                        (
                            text(item.get("requiredValueRef"))
                            for item in operation.get("parameters") or []
                            if isinstance(item, dict)
                            and text(item.get("name")) == parameter
                        ),
                        "",
                    ),
                },
                operations,
                context.index.raw.get("actors") or [],
            ))
            source_type = _source_type(
                source_ref, calls[:position], operations, fields_by_type, context.model,
            )
            required_value = next((item for item in required_value_catalog(context.use_case)
                                   if item["sourceRef"] == source_ref), None)
            if required_value is not None:
                valid = bool(
                    source_ref in eligible
                    and source_ref in {item["sourceRef"] for item in directly_available_sources(context.use_case)}
                    and required_value_type_compatible(
                        required_value, expected, context.model.get("DataTypes") or [],
                    )
                )
            elif source_ref == runtime_value_source(expected):
                valid = source_ref in eligible
            elif source_type == "__entry__":
                valid = bool(
                    actor_step
                    and source_ref == f"{actor_step}#{parameter_stable_refs.get(parameter, '')}"
                    and source_ref in eligible
                )
            elif source_type == "__precondition__":
                valid = False
            else:
                valid = bool(
                    source_ref in eligible
                    and source_type
                    and types_compatible(source_type, expected)
                )
            if not valid:
                findings.append(Finding(
                    "class.collaboration.bindings",
                    "parameter source must be a compatible actor input, precondition, or earlier call value",
                    f"{text(call.get('callId'))}#{parameter}",
                ))
    return findings


def _collaboration_ancestor_result_bindings(
    collaboration: dict[str, Any], context: CollaborationContext,
) -> list[Finding]:
    """Reject a child argument sourced from a parent call's not-yet-produced result."""

    calls = [item for item in collaboration.get("calls") or [] if isinstance(item, dict)]
    calls_by_id = {text(call.get("callId")): call for call in calls if text(call.get("callId"))}
    stable_id_counts: dict[str, int] = {}
    for call in calls:
        stable_id = text(call.get("stableId"))
        if stable_id:
            stable_id_counts[stable_id] = stable_id_counts.get(stable_id, 0) + 1
    findings: list[Finding] = []
    for call in calls:
        ancestor_ids: set[str] = set()
        parent_id = text(call.get("parentCallId"))
        while parent_id and parent_id not in ancestor_ids:
            ancestor_ids.add(parent_id)
            parent = calls_by_id.get(parent_id)
            parent_id = text(parent.get("parentCallId")) if parent else ""
        if not ancestor_ids:
            continue
        for binding in call.get("argumentBindings") or []:
            if not isinstance(binding, dict):
                continue
            source_id, separator, path = text(binding.get("sourceRef")).partition("#")
            if not separator or not (path == "result" or path.startswith("result.")):
                continue
            if stable_id_counts.get(source_id) != 1:
                continue
            source = next(call for call in calls if text(call.get("stableId")) == source_id)
            if text(source.get("callId")) not in ancestor_ids:
                continue
            findings.append(Finding(
                "class.collaboration.ancestor-result-binding",
                f"Nested call {text(call.get('callId'))} parameter {text(binding.get('parameter'))} "
                "uses an ancestor call result. The ancestor receives that result only after the "
                "nested call returns; bind a value available before the call or from an earlier "
                "completed call.",
                context.use_case.id,
            ))
    return findings


COLLABORATION_CHECKS = (
    CheckSpec("class.collaboration.contract", _collaboration_contract),
    CheckSpec("class.collaboration.bindings", _collaboration_bindings),
    CheckSpec(
        "class.collaboration.ancestor-result-binding",
        _collaboration_ancestor_result_bindings,
    ),
)


def validate_collaboration(
    collaboration: dict[str, Any], context: CollaborationContext,
) -> ValidationReport:
    """한 유스케이스의 여러 root 호출과 binding을 검사한다."""

    return run_checks(COLLABORATION_CHECKS, collaboration or {}, context)


__all__ = ["COLLABORATION_CHECKS", "CollaborationContext", "validate_collaboration"]
