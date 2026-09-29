"""한 유스케이스의 여러 actor entry를 하나의 호출 계획으로 구체화한다."""
from __future__ import annotations

import json
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from app.config import settings
from app.design.schemas.class_model import BCEModel, Collaboration, canonical_call_id
from app.design.services.class_diagram.cache import (
    AcceptedUnitCache,
    accepted_unit_key,
    configured_provider_identity,
    record_cache_outcome,
)
from app.design.services.class_diagram.identity import materialize_pre_binding_call_refs
from app.design.services.class_diagram.proposals import CallPlanProposal, ProposedCall
from app.design.services.class_diagram.scenario import (
    ExecutionGroup,
    ScenarioIndex,
    UseCase,
    text,
)
from app.design.services.class_diagram.trusted_context import (
    directly_available_sources,
    required_value_catalog,
    value_source_allows_binding,
)
from app.design.services.class_diagram.type_system import (
    projected_field_type,
    required_value_type_compatible,
    structured_field_types,
    types_compatible,
)
from app.design.services.class_diagram.validation.collaboration import (
    COLLABORATION_CHECKS,
    CollaborationContext,
)
from app.design.services.class_diagram.validation.model import (
    operation_catalog,
    optional_inner_type,
    runtime_value_source,
)
from app.design.services.common.structured import parse_structured
from app.llm_connection import build_llm_connection
from app.llm_profiles import effective_temperature
from app.validation import Finding, run_checks

CALL_PLAN_PROMPT = """
Build one ordered call forest for the complete use case. Select only supplied
receiverOperationId values. Return only receiverOperationId and parentCallIndex.
Always include parentCallIndex: use null for a root and an earlier one-based
position for every non-root.
Each actorEntry creates exactly one root in the supplied order; no other root is
allowed. A root has no parent. Every non-root uses the one-based position of an
earlier call in the same root as parentCallIndex. The position is counted in the
complete flat calls array and never restarts after a new root. A root is Boundary and
represents actorEntry.actor when that value is present. Do not use a supporting actor
or external-system Boundary as that root; call it downstream from Control. The root
delegates to Control, which may delegate state work to Entity. Ordinary results
return through the existing call chain. Use a Control-to-Boundary call only when
the scenario explicitly requires the system to initiate a separate interaction
with an external actor or system through that Boundary, such as an asynchronous
notification; parent it to Control, never to Boundary. Entities may collaborate
with other Entities, but do not call a Control or Boundary directly. The Boundary
class used by a root must not appear again inside that root. Cover all
required steps inside the matching actor entry. The same operation may be used
in more than one root. Each Boundary root hands off directly to exactly one
orchestration Control that covers the actor-visible request-response behavior;
return multiple response values together in a concrete DTO or valueObject. Do
not nest sequential retrieval under a parent Control when a child needs a value
that the parent itself returns: the result returns to its caller and is not
available to descendants. Keep further Control collaboration under the root
Control when child inputs are independently available from entry inputs,
preconditions, or earlier completed calls. parentCallIndex identifies the caller,
not a data dependency. Do not return ids, step refs, values, or bindings.
For a root and all of its descendants, choose only that actorEntry's
eligibleReceiverOperationIds. Do not move an operation from another actor entry
into this root, even when it is semantically related to the same use case.
If the supplied operations contain Entity behavior for durable domain information
used by this execution, call that Entity behavior from Control. Read-only stored
data is still Entity behavior. When no Entity operation is supplied for the
execution, do not invent one in the call plan.
""".strip()

BINDING_PROMPT = """
Select one sourceRef for each supplied finite choice. Prefer the source whose
meaning, type, and structural provenance match the receiver parameter and the
use-case evidence. The same sourceRef may be selected for multiple parameters
when one value legitimately supplies each. Select NO_MATCH when no offered
source can be justified.
Return no explanation.
""".strip()

NO_BINDING_SOURCE = "NO_MATCH"

PARENT_SELECTION_PROMPT = """
Choose whether one supplied earlier call should become the parent of the rejected
target call. Select parent:N only when the scenario and operation meanings show
that call N should invoke the target. Select fallback when the target call should
instead be omitted, the operation selection or ordering is wrong, or none of the
offered parents is semantically justified. Return only selection. Do not redesign
the call plan.
""".strip()


def call_plan_reasoning_effort() -> str:
    return str(getattr(
        settings, "design_class_call_plan_reasoning_effort", settings.design_reasoning_effort,
    ))


def call_plan_max_completion_tokens() -> int:
    return int(getattr(
        settings,
        "design_class_call_plan_max_completion_tokens",
        settings.design_class_collaboration_max_completion_tokens,
    ))


def _finite_schema(name: str, **fields: Any) -> type[BaseModel]:
    return cast(type[BaseModel], create_model(name, **fields))


def _finding_text(findings: tuple[Finding, ...]) -> list[str]:
    return [
        f"{finding.location}: {finding.message}" if finding.location else finding.message
        for finding in findings
    ]


class CallPlanViolation(ValueError):
    """A rejected call edge with machine-readable repair alternatives."""

    def __init__(self, source: str, target: str, repair_context: dict[str, Any]) -> None:
        self.repair_context = repair_context
        super().__init__(
            f"BCE communication is invalid: {source} -> {target}; repairContext="
            + json.dumps(repair_context, ensure_ascii=False, separators=(",", ":"))
        )


class BindingSourceViolation(ValueError):
    """A parameter whose finite source search returned no candidate."""

    def __init__(
        self,
        repair_context: dict[str, Any],
        *,
        repair_slot: dict[str, int] | None = None,
    ) -> None:
        self.repair_context = repair_context
        self.repair_slot = repair_slot or {}
        super().__init__(
            f"no finite source for {repair_context['location']}; repairContext="
            + json.dumps(repair_context, ensure_ascii=False, separators=(",", ":"))
        )


def _communication_allowed(
    source: str,
    target: str,
    target_class: str,
    root_boundary_class: str,
) -> bool:
    return not (
        (source == "boundary" and target != "control")
        or (source == "entity" and target != "entity")
        or (
            source == "control"
            and target == "boundary"
            and target_class == root_boundary_class
        )
    )


def _groups(index: ScenarioIndex, use_case: UseCase) -> tuple[ExecutionGroup, ...]:
    return tuple(group for group in index.groups if group.use_case_id == use_case.id)


def _use_case_operations(
    index: ScenarioIndex, model: dict[str, Any], use_case: UseCase,
) -> dict[str, dict[str, Any]]:
    groups = _groups(index, use_case)
    allowed = {use_case.id} | {
        use_case_id for group in groups for use_case_id in group.trace_use_case_ids
    }
    class_scope = {
        text(item.get("className")): {
            text(value) for value in item.get("use_case_ids") or []
        }
        for item in model.get("Classes") or [] if isinstance(item, dict)
    }
    return {
        operation_id: operation
        for operation_id, operation in operation_catalog(model).items()
        if allowed & class_scope.get(text(operation.get("className")), set())
    }


def _eligible_operation_ids_by_group(
    groups: tuple[ExecutionGroup, ...],
    operations: dict[str, dict[str, Any]],
) -> tuple[frozenset[str], ...]:
    """Return the finite operation set that can cover each actor-entry flow."""

    return tuple(
        frozenset(
            operation_id
            for operation_id, operation in operations.items()
            if set(group.required_step_ids) & {
                text(ref) for ref in operation.get("stepRefs") or []
            }
        )
        for group in groups
    )


def _use_case_payload(
    index: ScenarioIndex, model: dict[str, Any], use_case: UseCase,
) -> dict[str, Any]:
    groups = _groups(index, use_case)
    operations = _use_case_operations(index, model, use_case)
    step_by_id = {
        step.id: step for group in groups for use_case_id in group.trace_use_case_ids
        for step in index.use_case(use_case_id).steps
    }
    required = {ref for group in groups for ref in group.required_step_ids}
    eligible_by_group = _eligible_operation_ids_by_group(groups, operations)
    return {
        "collaborationId": use_case.id,
        "actorEntries": [
            {
                "actorStepRef": group.actor_step,
                "actor": group.entry_actor,
                "requiredStepRefs": list(group.required_step_ids),
                "eligibleReceiverOperationIds": sorted(eligible_by_group[ordinal]),
            }
            for ordinal, group in enumerate(groups)
        ],
        "steps": [
            {"id": step_id, "sentence": step_by_id[step_id].sentence}
            for step_id in dict.fromkeys(
                ref for group in groups for ref in group.required_step_ids
            )
            if step_id in step_by_id
        ],
        "receiverOperations": [
            {
                "operationId": operation_id,
                "className": operation["className"],
                "stereotype": operation["stereotype"],
                "parameters": operation.get("parameters") or [],
                "returnType": operation.get("returnType"),
                "stepRefs": operation.get("stepRefs") or [],
            }
            for operation_id, operation in sorted(operations.items())
            if required & {text(ref) for ref in operation.get("stepRefs") or []}
        ],
    }


def _entry_scoped_plan_schema(
    operation_ids: tuple[str, ...],
    eligible_by_group: tuple[frozenset[str], ...],
) -> type[CallPlanProposal]:
    """Build a flat-plan schema that rejects cross-entry operation choices.

    Calls deliberately remain a single ordered forest. Their actor-entry ownership
    is determined from roots and parent chains, exactly as materialization does, so
    a plan-only repair cannot make an otherwise valid operation cover a different
    actor entry merely because the use case has several related flows.
    """

    finite_call = _finite_schema(
        "FiniteEntryScopedUseCaseCall",
        __base__=ProposedCall,
        receiver_operation_id=(
            Literal.__getitem__(operation_ids), Field(alias="receiverOperationId"),
        ),
    )
    root_count = len(eligible_by_group)

    class EntryScopedCallPlan(CallPlanProposal):
        calls: list[finite_call] = Field(min_length=root_count)  # type: ignore[valid-type]

        @model_validator(mode="after")
        def enforce_actor_entry_operation_scope(self) -> EntryScopedCallPlan:
            try:
                _, assignments = _root_assignments(self, root_count)
            except ValueError:
                # Keep structural-plan findings on the existing materialization path.
                # This schema gate owns only cross-entry operation selection.
                return self
            for position, call in enumerate(self.calls, start=1):
                group = assignments[position]
                if call.receiver_operation_id not in eligible_by_group[group]:
                    raise ValueError(
                        "receiverOperationId must belong to the actor entry resolved "
                        f"for calls[{position - 1}]"
                    )
            return self

    return EntryScopedCallPlan


def propose_call_plan(
    index: ScenarioIndex,
    model: BCEModel,
    use_case: UseCase,
    *,
    previous: CallPlanProposal | None = None,
    finding: str = "",
) -> CallPlanProposal:
    """완성 skeleton에서 유스케이스 전체의 multiple-root 호출 계획을 제안한다."""

    payload = _use_case_payload(index, model.model_dump(by_alias=True), use_case)
    if previous is not None:
        payload["previousPlan"] = previous.model_dump(by_alias=True)
    if finding:
        payload["task"] = "Return a full repaired call plan and resolve the finding."
        payload["finding"] = finding
    operation_ids = tuple(item["operationId"] for item in payload["receiverOperations"])
    if not operation_ids:
        raise ValueError(f"use case has no receiver operations: {use_case.id}")
    groups = _groups(index, use_case)
    operations = _use_case_operations(index, model.model_dump(by_alias=True), use_case)
    finite_plan = _entry_scoped_plan_schema(
        operation_ids,
        _eligible_operation_ids_by_group(groups, operations),
    )
    parsed = parse_structured(
        [
            {"role": "system", "content": CALL_PLAN_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        finite_plan,
        reasoning_effort=call_plan_reasoning_effort(),
        max_completion_tokens=call_plan_max_completion_tokens(),
        operation="InteractionCallPlanRepair" if finding else "InteractionCallPlan",
        metadata={
            "useCaseId": use_case.id,
            "executionSlice": use_case.id,
            "candidateCount": len(operation_ids),
        },
    )
    return CallPlanProposal.model_validate(
        finite_plan.model_validate(parsed).model_dump(by_alias=True),
    )


def repair_communication_parent(
    index: ScenarioIndex,
    model: BCEModel,
    use_case: UseCase,
    previous: CallPlanProposal,
    violation: CallPlanViolation,
) -> CallPlanProposal | None:
    """Select one admissible parent and patch only the rejected edge.

    ``None`` explicitly hands the candidate to the existing full-plan repair.
    Local BCE compatibility is not treated as proof of semantic correctness.
    """

    context = violation.repair_context
    allowed = tuple(dict.fromkeys(
        int(value) for value in context.get("allowedParentCallIndexes") or []
    ))
    if not allowed:
        return None
    location = str(context.get("location") or "")
    prefix = "calls["
    suffix = "].parentCallIndex"
    if not location.startswith(prefix) or not location.endswith(suffix):
        raise ValueError(f"invalid call-plan repair location: {location}")
    offset = int(location[len(prefix):-len(suffix)])
    if offset < 0 or offset >= len(previous.calls):
        raise ValueError(f"call-plan repair location is out of range: {location}")
    if any(position < 1 or position > len(previous.calls) for position in allowed):
        raise ValueError("allowed parent call index is out of range")

    selections = (*(f"parent:{position}" for position in allowed), "fallback")
    finite_selection = cast(type[BaseModel], create_model(
        "FiniteParentRepairSelection",
        __config__=ConfigDict(extra="forbid", populate_by_name=True),
        selection=(Literal.__getitem__(selections), Field()),
    ))
    use_case_payload = _use_case_payload(
        index, model.model_dump(by_alias=True), use_case,
    )
    payload = {
        "actorEntries": use_case_payload["actorEntries"],
        "steps": use_case_payload["steps"],
        "previousPlan": previous.model_dump(by_alias=True),
        "target": context.get("observed") or {},
        "alternatives": [
            {
                "selection": f"parent:{position}",
                "sourceOperationId": previous.calls[
                    position - 1
                ].receiver_operation_id,
            }
            for position in allowed
        ],
        "fallback": {
            "selection": "fallback",
            "meaning": "Use the existing full-plan repair path.",
        },
    }
    parsed = parse_structured(
        [
            {"role": "system", "content": PARENT_SELECTION_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        finite_selection,
        reasoning_effort=call_plan_reasoning_effort(),
        max_completion_tokens=min(call_plan_max_completion_tokens(), 1024),
        operation="InteractionParentSelectionRepair",
        metadata={
            "useCaseId": use_case.id,
            "executionSlice": use_case.id,
            "candidateCount": len(selections),
        },
    )
    selection = str(finite_selection.model_validate(parsed).selection)
    if selection == "fallback":
        return None
    selected_parent = int(selection.partition(":")[2])
    repaired = previous.model_dump(by_alias=True)
    repaired["calls"][offset]["parentCallIndex"] = selected_parent
    return CallPlanProposal.model_validate(repaired)


def _root_assignments(
    plan: CallPlanProposal, root_count: int,
) -> tuple[list[int], dict[int, int]]:
    roots = [
        position for position, call in enumerate(plan.calls, start=1)
        if call.parent_call_index is None
    ]
    if len(roots) != root_count:
        raise ValueError("root calls must match actor entries in scenario order")
    root_ordinal = {position: ordinal for ordinal, position in enumerate(roots)}
    assignments: dict[int, int] = {}
    latest_root = -1
    for position, call in enumerate(plan.calls, start=1):
        if position in root_ordinal:
            latest_root = root_ordinal[position]
        parent = call.parent_call_index
        if parent is not None and parent >= position:
            raise ValueError("parentCallIndex must reference an earlier call")
        current = position
        visited: set[int] = set()
        while current not in root_ordinal:
            if current in visited:
                raise ValueError("call parent chain contains a cycle")
            visited.add(current)
            parent = plan.calls[current - 1].parent_call_index
            if parent is None or parent >= current:
                raise ValueError("every non-root call requires an earlier parent")
            current = parent
        assignments[position] = root_ordinal[current]
        if assignments[position] != latest_root:
            raise ValueError("a call cannot return to an earlier actor root")
    return roots, assignments


def _ancestors(calls: list[dict[str, Any]], index: int) -> list[dict[str, Any]]:
    previous = {text(call.get("callId")): call for call in calls[:index]}
    parent_id = text(calls[index].get("parentCallId"))
    result: list[dict[str, Any]] = []
    while parent_id and parent_id in previous:
        call = previous[parent_id]
        result.append(call)
        parent_id = text(call.get("parentCallId"))
    return result


def _is_boundary_control_handoff(
    calls: list[dict[str, Any]],
    call_index: int,
    operations: dict[str, dict[str, Any]],
) -> bool:
    """Trusted context crosses only the entry Boundary→Control handoff."""

    call = calls[call_index]
    parent_call_id = text(call.get("parentCallId"))
    parent = next(
        (item for item in calls if text(item.get("callId")) == parent_call_id), None
    )
    parent_operation = (
        operations.get(text(parent.get("receiverOperationId"))) if parent else None
    )
    target_operation = operations.get(text(call.get("receiverOperationId")))
    return bool(
        parent_operation
        and target_operation
        and not text(parent.get("parentCallId"))
        and parent_operation.get("stereotype") == "boundary"
        and target_operation.get("stereotype") == "control"
    )


def _binding_candidates(
    model: dict[str, Any],
    use_case: UseCase,
    actor_step: str | None,
    is_root: bool,
    calls: list[dict[str, Any]],
    call_index: int,
    parameter: dict[str, Any],
    operations: dict[str, dict[str, Any]],
    actor_contracts: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> list[str]:
    target_type = text(parameter.get("type"))
    fields_by_type = structured_field_types(model)
    field_refs_by_type: dict[str, dict[str, str]] = {}
    for item in [*(model.get("Classes") or []), *(model.get("DataTypes") or [])]:
        if not isinstance(item, dict):
            continue
        type_name = text(item.get("className") or item.get("name"))
        fields = item.get("fields") or []
        refs = item.get("fieldRefs") or []
        field_refs = {
            field.strip().partition(":")[0].strip(): text(ref)
            for field, ref in zip(fields, refs)
            if field.strip().partition(":")[0].strip() and text(ref)
        }
        if type_name and field_refs:
            field_refs_by_type[type_name] = field_refs

    def stable_field_path(root_type: str, path: str) -> str:
        current_type = root_type
        refs: list[str] = []
        for component in path.split("."):
            field_ref = field_refs_by_type.get(current_type, {}).get(component)
            if not field_ref:
                return ""
            refs.append(field_ref)
            current_type = projected_field_type(current_type, component, fields_by_type)
        return ".".join(refs)

    candidates: list[str] = []
    target_parameter_ref = text(parameter.get("stableRef"))
    if is_root and actor_step and target_parameter_ref:
        candidates.append(f"{actor_step}#{target_parameter_ref}")
    ancestors = _ancestors(calls, call_index)
    boundary_handoff = _is_boundary_control_handoff(calls, call_index, operations)
    for ancestor in ancestors:
        operation = operations.get(text(ancestor.get("receiverOperationId")))
        if operation is None:
            # Validation can inspect a stale collaboration while an owning
            # operation fragment is being replaced.  A missing operation is
            # reported by the collaboration contract; it is not a value source.
            continue
        for source in operation.get("parameters") or []:
            if not isinstance(source, dict):
                continue
            source_type = text(source.get("type"))
            source_ref = (
                f"{text(ancestor.get('stableId'))}#{text(source.get('stableRef'))}"
                if text(ancestor.get("stableId")) and text(source.get("stableRef"))
                else ""
            )
            if not source_ref:
                continue
            compatible = types_compatible(source_type, target_type)
            # A parent call is the finite structural scope for a direct value
            # handoff.  Its formal name is evidence for semantic selection,
            # not an eligibility gate: harmless renames (for example
            # currentRegistrationId -> registrationId) must remain selectable.
            # materialize() deliberately does not auto-bind a name-mismatched
            # candidate, even when this leaves just one candidate.
            if compatible:
                candidates.append(source_ref)
            for field_path in fields_by_type.get(source_type, {}):
                projected = projected_field_type(source_type, field_path, fields_by_type)
                stable_path = stable_field_path(source_type, field_path)
                if not stable_path:
                    continue
                field_ref = f"{source_ref}.{stable_path}"
                if types_compatible(projected, target_type):
                    candidates.append(field_ref)
                elif types_compatible(optional_inner_type(projected), target_type):
                    candidates.append(field_ref + ".unwrap")
    # 하나의 유스케이스가 여러 사용자 입력으로 나뉘면 뒤 입력에서 앞 입력의 값을
    # 다시 사용할 수 있다. 예를 들어 첫 요청에서 받은 주문 ID를 다음 선택 요청 뒤의
    # Control 호출에 전달하는 경우다. 이전 root의 입력은 call/parameter ref로
    # 식별하고 타입 호환성으로 범위를 좁힌 뒤, 여러 후보는 아래 selector가 판단한다.
    for earlier in calls[:call_index]:
        if text(earlier.get("parentCallId")):
            continue
        operation = operations.get(text(earlier.get("receiverOperationId")))
        if operation is None:
            continue
        for source in operation.get("parameters") or []:
            if not isinstance(source, dict):
                continue
            source_type = text(source.get("type"))
            source_ref = (
                f"{text(earlier.get('stableId'))}#{text(source.get('stableRef'))}"
                if text(earlier.get("stableId")) and text(source.get("stableRef"))
                else ""
            )
            if not source_ref:
                continue
            if types_compatible(source_type, target_type):
                candidates.append(source_ref)
            for field_path in fields_by_type.get(source_type, {}):
                projected = projected_field_type(source_type, field_path, fields_by_type)
                stable_path = stable_field_path(source_type, field_path)
                if not stable_path:
                    continue
                field_ref = f"{source_ref}.{stable_path}"
                if types_compatible(projected, target_type):
                    candidates.append(field_ref)
                elif types_compatible(optional_inner_type(projected), target_type):
                    candidates.append(field_ref + ".unwrap")
    ancestor_ids = {text(item.get("callId")) for item in ancestors}
    for earlier in reversed(calls[:call_index]):
        # Ancestor calls are still in flight while their descendants execute;
        # their parameters are available above, but their results are not.
        if text(earlier.get("callId")) in ancestor_ids:
            continue
        operation = operations.get(text(earlier.get("receiverOperationId")))
        if operation is None:
            continue
        return_type = text(operation.get("returnType"))
        stable_call_ref = text(earlier.get("stableId"))
        if not stable_call_ref:
            continue
        result_ref = f"{stable_call_ref}#result"
        if return_type.casefold() != "void" and types_compatible(return_type, target_type):
            candidates.append(result_ref)
        elif types_compatible(optional_inner_type(return_type), target_type):
            candidates.append(result_ref + ".unwrap")
        # Optional<T>를 반환한 조회도 unwrap 뒤 T의 field를 다음 호출에 전달할 수 있다.
        # 예: optional<User> 결과의 id는 ``result.unwrap.id``로 표현한다.
        projection_type = optional_inner_type(return_type) or return_type
        projection_ref = (
            f"{result_ref}.unwrap" if optional_inner_type(return_type) else result_ref
        )
        for field_path in fields_by_type.get(projection_type, {}):
            projected = projected_field_type(
                projection_type, field_path, fields_by_type,
            )
            stable_path = stable_field_path(projection_type, field_path)
            if not stable_path:
                continue
            field_ref = f"{projection_ref}.{stable_path}"
            if types_compatible(projected, target_type):
                candidates.append(field_ref)
            elif types_compatible(optional_inner_type(projected), target_type):
                candidates.append(field_ref + ".unwrap")
    if not candidates and runtime_value_source(target_type):
        candidates.append(runtime_value_source(target_type))
    if boundary_handoff:
        required_ref = text(parameter.get("requiredValueRef"))
        candidates.extend(
            source["sourceRef"] for source in directly_available_sources(use_case)
            if source["valueRef"] == required_ref
            and required_value_type_compatible(
                source, target_type, (model or {}).get("DataTypes") or [],
            )
        )
    required_ref = text(parameter.get("requiredValueRef"))
    declaration = next((item for item in required_value_catalog(use_case)
                        if item["valueRef"] == required_ref), None)
    if declaration is not None:
        candidates = [
            source_ref for source_ref in candidates
            if _bound_required_value_forward(
                source_ref, required_ref, calls, operations, use_case,
            ) is not None
            or value_source_allows_binding(
                declaration,
                _candidate_source_kind(source_ref, calls, use_case, operations),
                boundary_handoff=boundary_handoff,
            )
        ]
    return list(dict.fromkeys(candidates))


def _binding_search_scopes(
    use_case: UseCase,
    actor_step: str | None,
    is_root: bool,
) -> list[str]:
    """Describe the source categories actually considered by the finite search."""

    scopes: list[str] = []
    if is_root and actor_step:
        scopes.append("actor-entry-input")
    if use_case.precondition_refs:
        scopes.append("trusted-context")
    scopes.extend([
        "ancestor-call-parameter",
        "earlier-root-input",
        "previous-call-result",
        "derived-structured-value",
        "runtime-value",
    ])
    return scopes


def _candidate_source_kind(
    source_ref: str,
    calls: list[dict[str, Any]],
    use_case: UseCase | None = None,
    operations: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Return the only user-selectable provenance kinds for a finite source."""

    if use_case and any(item["sourceRef"] == source_ref for item in required_value_catalog(use_case)):
        return "required_value"
    if use_case and operations and _bound_required_value_forward(
        source_ref, None, calls, operations, use_case,
    ) is not None:
        return "required_value"
    if source_ref.startswith("derived#"):
        return "derive_from_existing_inputs"
    if source_ref.startswith("runtime#"):
        return "runtime_value"
    _source_call_id, separator, path = source_ref.partition("#")
    if separator and path.startswith("result"):
        return "earlier_step_result"
    # A formal parameter on any ancestor call represents a value originally
    # introduced by the use-case input path.  It may cross several BCE calls,
    # but remains a caller-provided value rather than a new computed source.
    return "use_case_input"


def _bound_required_value_forward(
    source_ref: str,
    target_value_ref: str | None,
    calls: list[dict[str, Any]],
    operations: dict[str, dict[str, Any]],
    use_case: UseCase,
) -> dict[str, Any] | None:
    """Return provenance only when a call parameter forwards its accepted binding."""

    stable_id, separator, parameter_ref = source_ref.partition("#")
    if not separator or not stable_id or not parameter_ref or "." in parameter_ref:
        return None
    call = next((item for item in calls if text(item.get("stableId")) == stable_id), None)
    if call is None:
        return None
    operation = operations.get(text(call.get("receiverOperationId")), {})
    parameter = next((
        item for item in operation.get("parameters") or []
        if isinstance(item, dict) and text(item.get("stableRef")) == parameter_ref
    ), None)
    if parameter is None:
        return None
    value_ref = text(parameter.get("requiredValueRef"))
    if not value_ref or (target_value_ref and value_ref != target_value_ref):
        return None
    value = next((item for item in required_value_catalog(use_case)
                  if item["valueRef"] == value_ref), None)
    if value is None:
        return None
    binding = next((
        item for item in call.get("argumentBindings") or []
        if isinstance(item, dict)
        and text(item.get("parameter")) == text(parameter.get("name"))
    ), None)
    if not binding or text(binding.get("sourceRef")) != value["sourceRef"]:
        return None
    return value


def _candidate_detail(
    source_ref: str,
    target_type: str,
    calls: list[dict[str, Any]],
    operations: dict[str, dict[str, Any]],
    model: dict[str, Any] | None = None,
    use_case: UseCase | None = None,
) -> dict[str, Any]:
    """Describe an already-compatible finite candidate without widening it."""

    detail: dict[str, Any] = {
        "sourceRef": source_ref,
        "targetType": target_type,
        "sourceKind": _candidate_source_kind(source_ref, calls, use_case, operations),
    }
    value = next((row for row in required_value_catalog(use_case)
                  if row["sourceRef"] == source_ref), {}) if use_case else {}
    if value:
        detail.update({"kind": "required_value", **value})
        return detail
    if source_ref.startswith("runtime#"):
        detail["kind"] = "runtime_value"
        return detail
    if source_ref.startswith("derived#"):
        detail["kind"] = "derived_value"
        return detail
    source_call_id, separator, path = source_ref.partition("#")
    source_call = next(
        (call for call in calls if text(call.get("stableId")) == source_call_id),
        None,
    )
    if source_call is None:
        detail["kind"] = "actor_entry_input" if separator else "unknown"
        return detail
    operation = operations.get(text(source_call.get("receiverOperationId")), {})
    field_names_by_ref = {
        text(field_ref): field.strip().partition(":")[0].strip()
        for item in [
            *((model or {}).get("Classes") or []),
            *((model or {}).get("DataTypes") or []),
        ]
        if isinstance(item, dict)
        for field, field_ref in zip(
            item.get("fields") or [], item.get("fieldRefs") or [],
        )
        if text(field_ref)
    }
    detail.update({
        "kind": "previous_result" if path.startswith("result") else "call_parameter",
        "sourceCallStableId": source_call_id,
        "sourceOperationId": source_call.get("receiverOperationId"),
        "sourceClass": operation.get("className"),
        "sourceStereotype": operation.get("stereotype"),
    })
    if path.startswith("result"):
        source_path = ["result", *path.split(".")[1:]]
        detail["sourceDeclaredType"] = operation.get("returnType")
    else:
        source_parameter_ref, _dot, rest = path.partition(".")
        declared_type = next(
            (
                item.get("type") for item in operation.get("parameters") or []
                if isinstance(item, dict)
                and text(item.get("stableRef")) == source_parameter_ref
            ),
            None,
        )
        if declared_type:
            detail["sourceDeclaredType"] = declared_type
        detail["sourceParameter"] = next((
            text(item.get("name")) for item in operation.get("parameters") or []
            if isinstance(item, dict)
            and text(item.get("stableRef")) == source_parameter_ref
        ), "")
        source_path = [detail["sourceParameter"], *rest.split(".")] if rest else [
            detail["sourceParameter"],
        ]
    detail["sourcePath"] = ".".join(
        field_names_by_ref.get(component, component)
        for component in source_path if component
    )
    return detail


def select_ambiguous_bindings(
    use_case: UseCase,
    ambiguous: dict[str, list[str]],
    parameter_types: dict[str, str] | None = None,
    *,
    calls: list[dict[str, Any]] | None = None,
    operations: dict[str, dict[str, Any]] | None = None,
    semantic_locations: set[str] | None = None,
    model: dict[str, Any] | None = None,
) -> dict[str, str]:
    fields: dict[str, tuple[Any, Any]] = {}
    choices: list[dict[str, Any]] = []
    locations: dict[str, str] = {}
    call_by_id = {text(call.get("callId")): call for call in calls or []}
    for position, (parameter, candidates) in enumerate(sorted(ambiguous.items()), start=1):
        field_name = f"choice{position}"
        values = tuple(dict.fromkeys([
            *candidates,
            *([NO_BINDING_SOURCE] if parameter in (semantic_locations or set()) else []),
        ]))
        fields[field_name] = (
            Literal.__getitem__(values), Field(description=f"Source for {parameter}"),
        )
        choices.append({
            "choice": field_name,
            "target": {
                "location": parameter,
                "parameter": parameter.partition("#")[2],
                "type": (parameter_types or {}).get(parameter, ""),
                "receiverOperationId": call_by_id.get(
                    parameter.partition("#")[0],
                ).get("receiverOperationId") if parameter.partition("#")[0] in call_by_id else None,
                "receiverClass": operations.get(text(call_by_id.get(
                    parameter.partition("#")[0],
                ).get("receiverOperationId")), {}).get("className")
                if parameter.partition("#")[0] in call_by_id else None,
            },
            "candidates": list(values),
            "candidateDetails": [
                _candidate_detail(
                    value,
                    (parameter_types or {}).get(parameter, ""),
                    calls or [],
                    operations or {},
                    model,
                    use_case,
                )
                if value != NO_BINDING_SOURCE else {
                    "sourceRef": NO_BINDING_SOURCE,
                    "kind": "no_match",
                    "meaning": "No offered source is semantically justified.",
                }
                for value in values
            ],
        })
        locations[field_name] = parameter
    finite_choices = _finite_schema(
        "FiniteBindingChoices", __config__=ConfigDict(extra="forbid"), **fields,
    )

    class BindingChoices(finite_choices):
        pass

    schema = BindingChoices
    parsed = parse_structured(
        [
            {"role": "system", "content": BINDING_PROMPT},
            {"role": "user", "content": json.dumps({
                "collaborationId": use_case.id, "choices": choices,
                "steps": [
                    {"id": step.id, "sentence": step.sentence}
                    for step in use_case.steps
                ],
                "publicContract": (
                    use_case.specification.get("public_contract")
                    or use_case.specification.get("publicContract")
                    or {}
                ),
            }, ensure_ascii=False)},
        ],
        schema,
        reasoning_effort="low",
        max_completion_tokens=min(settings.design_class_collaboration_max_completion_tokens, 2048),
        operation="InteractionBindingSelection",
        metadata={
            "useCaseId": use_case.id,
            "executionSlice": use_case.id,
            "candidateCount": sum(len(choice["candidates"]) for choice in choices),
        },
    )
    selected = schema.model_validate(parsed).model_dump()
    return {locations[field_name]: source_ref for field_name, source_ref in selected.items()}


def _matching_binding_source_kind(
    decision: dict[str, Any] | None,
    *,
    use_case_id: str,
    actor_entry_index: int,
    call_index: int,
    parameter_index: int,
    receiver_operation_id: str,
    parameter_name: str,
) -> str | None:
    """Use an answer only for the exact questioned binding slot."""

    if not isinstance(decision, dict):
        return None
    if all(decision.get(key) == value for key, value in {
        "useCaseId": use_case_id,
        "actorEntryIndex": actor_entry_index,
        "callIndex": call_index,
        "parameterIndex": parameter_index,
    }.items()) and (
        not decision.get("receiverOperationId")
        or decision.get("receiverOperationId") == receiver_operation_id
    ) and (
        not decision.get("parameterName")
        or decision.get("parameterName") == parameter_name
    ):
        source_kind = text(decision.get("sourceKind"))
        if source_kind in {
            "use_case_input",
            "required_value",
            "earlier_step_result",
            "derive_from_existing_inputs",
        }:
            return source_kind
    return None


def materialize(
    index: ScenarioIndex,
    model: BCEModel,
    use_case: UseCase,
    plan: CallPlanProposal,
    *,
    binding_source_decision: dict[str, Any] | None = None,
) -> Collaboration:
    """flat multiple-root 계획을 canonical call·step·binding 협업으로 만든다."""

    model_payload = model.model_dump(by_alias=True)
    operations = _use_case_operations(index, model_payload, use_case)
    groups = _groups(index, use_case)
    roots, assignments = _root_assignments(plan, len(groups))
    root_set = set(roots)
    calls: list[dict[str, Any]] = []
    for position, proposed in enumerate(plan.calls, start=1):
        operation_id = text(proposed.receiver_operation_id)
        operation = operations.get(operation_id)
        if operation is None:
            raise ValueError(f"unknown receiverOperationId: {operation_id}")
        group = groups[assignments[position]]
        refs = [
            ref for ref in group.required_step_ids
            if ref in {text(value) for value in operation.get("stepRefs") or []}
        ]
        if not refs:
            raise ValueError("selected operation has no declared step in its actor entry")
        calls.append({
            "callId": canonical_call_id(use_case.id, position),
            "parentCallId": (
                canonical_call_id(use_case.id, proposed.parent_call_index)
                if proposed.parent_call_index else None
            ),
            "receiverOperationId": operation_id,
            "stepRefs": refs,
            "argumentBindings": [],
        })
    root_boundary_classes = {
        assignments[position]: text(
            operations[calls[position - 1]["receiverOperationId"]].get("className")
        )
        for position in roots
    }
    control_roots: set[int] = set()
    for position, call in enumerate(calls, start=1):
        operation = operations[call["receiverOperationId"]]
        stereotype = text(operation.get("stereotype"))
        if position in root_set:
            if stereotype != "boundary":
                raise ValueError("actor entry must start at Boundary")
        else:
            parent = calls[(plan.calls[position - 1].parent_call_index or 1) - 1]
            parent_operation = operations[parent["receiverOperationId"]]
            source = text(parent_operation.get("stereotype"))
            target_class = text(operation.get("className"))
            root_boundary_class = root_boundary_classes[assignments[position]]
            if not _communication_allowed(
                source, stereotype, target_class, root_boundary_class,
            ):
                allowed_parents = [
                    candidate_position
                    for candidate_position in range(position - 1, 0, -1)
                    if assignments.get(candidate_position) == assignments[position]
                    and _communication_allowed(
                        text(operations[
                            calls[candidate_position - 1]["receiverOperationId"]
                        ].get("stereotype")),
                        stereotype,
                        target_class,
                        root_boundary_class,
                    )
                ]
                raise CallPlanViolation(
                    source,
                    stereotype,
                    {
                        "code": "BCE_COMMUNICATION_INVALID",
                        "location": f"calls[{position - 1}].parentCallIndex",
                        "observed": {
                            "parentCallIndex": plan.calls[position - 1].parent_call_index,
                            "sourceOperationId": parent["receiverOperationId"],
                            "sourceStereotype": source,
                            "receiverOperationId": call["receiverOperationId"],
                            "receiverStereotype": stereotype,
                        },
                        "allowedParentCallIndexes": allowed_parents,
                        "instruction": (
                            "Choose a listed parent index, or omit this call only if it "
                            "represents a return through the existing call chain."
                        ),
                    },
                )
        if stereotype == "control":
            control_roots.add(assignments[position])
    if control_roots != set(range(len(groups))):
        raise ValueError("each Boundary root must delegate to Control")
    # Calls now have their complete structural identity (including parent
    # links).  Issue stable IDs before finite binding candidates and the
    # selector see them; persisted sourceRefs use these stable IDs.
    materialize_pre_binding_call_refs(model, use_case.id, calls)
    ambiguous: dict[str, list[str]] = {}
    parameter_types: dict[str, str] = {}
    semantic_locations: set[str] = set()
    binding_slots: dict[str, dict[str, int]] = {}
    for call_index, call in enumerate(calls):
        operation = operations[call["receiverOperationId"]]
        group = groups[assignments[call_index + 1]]
        for parameter_index, parameter in enumerate(operation.get("parameters") or []):
            if not isinstance(parameter, dict):
                raise TypeError("operation parameter must be an object")
            candidates = _binding_candidates(
                model_payload, use_case, group.actor_step,
                call_index + 1 in root_set, calls, call_index, parameter, operations,
                index.raw.get("actors") or [],
            )
            location = f"{call['callId']}#{text(parameter.get('name'))}"
            binding_slots[location] = {
                "actorEntryIndex": assignments[call_index + 1],
                "callIndex": call_index,
                "parameterIndex": parameter_index,
            }
            requested_source_kind = _matching_binding_source_kind(
                binding_source_decision,
                use_case_id=use_case.id,
                actor_entry_index=assignments[call_index + 1],
                call_index=call_index,
                parameter_index=parameter_index,
                receiver_operation_id=call["receiverOperationId"],
                parameter_name=text(parameter.get("name")),
            )
            if requested_source_kind:
                candidates = [
                    source_ref for source_ref in candidates
                    if _candidate_source_kind(
                        source_ref, calls, use_case, operations,
                    ) == requested_source_kind
                ]
            if not candidates:
                raise BindingSourceViolation({
                    "code": "BINDING_SOURCE_UNAVAILABLE",
                    "useCaseId": use_case.id,
                    "location": location,
                    "receiverOperationId": call["receiverOperationId"],
                    "parameter": {
                        "name": text(parameter.get("name")),
                        "type": text(parameter.get("type")),
                    },
                    "searchedSourceScopes": _binding_search_scopes(
                        use_case,
                        group.actor_step,
                        call_index + 1 in root_set,
                    ),
                    "availableSources": [],
                    "instruction": (
                        "No finite compatible source exists. Change this operation so every "
                        "parameter is supplied by an entry input, evidence-backed trusted "
                        "context, an earlier result, a supported runtime value, or a "
                        "derivable structured value."
                    ),
                }, repair_slot=binding_slots[location])
            is_root_binding = call_index + 1 in root_set
            if is_root_binding and len(candidates) == 1:
                call["argumentBindings"].append({
                    "parameter": text(parameter.get("name")), "sourceRef": candidates[0],
                })
            elif (
                not is_root_binding
                and len(candidates) == 1
                and any(
                    source["sourceRef"] == candidates[0]
                    for source in directly_available_sources(use_case)
                )
                and _is_boundary_control_handoff(calls, call_index, operations)
            ):
                # A sole accepted required value on the Boundary→Control edge
                # is already fully identified; bind it now so descendants can
                # inherit only the actually delivered value.
                call["argumentBindings"].append({
                    "parameter": text(parameter.get("name")), "sourceRef": candidates[0],
                })
            else:
                ambiguous[location] = candidates
                parameter_types[location] = text(parameter.get("type"))
                if not is_root_binding:
                    semantic_locations.add(location)
    selected = (
        select_ambiguous_bindings(
            use_case,
            ambiguous,
            parameter_types,
            calls=calls,
            operations=operations,
            semantic_locations=semantic_locations,
            model=model_payload,
        )
        if ambiguous else {}
    )
    for call in calls:
        operation = operations[call["receiverOperationId"]]
        existing = {text(item.get("parameter")) for item in call["argumentBindings"]}
        for parameter in operation.get("parameters") or []:
            name = text(parameter.get("name"))
            if name not in existing:
                source_ref = selected[f"{call['callId']}#{name}"]
                if source_ref == NO_BINDING_SOURCE:
                    raise BindingSourceViolation({
                        "code": "BINDING_SOURCE_UNAVAILABLE",
                        "useCaseId": use_case.id,
                        "location": f"{call['callId']}#{name}",
                        "receiverOperationId": call["receiverOperationId"],
                        "parameter": {"name": name, "type": text(parameter.get("type"))},
                        "availableSources": ambiguous[f"{call['callId']}#{name}"],
                        "instruction": "No finite compatible source was semantically justified.",
                    }, repair_slot=binding_slots[f"{call['callId']}#{name}"])
                call["argumentBindings"].append({
                    "parameter": name, "sourceRef": source_ref,
                })
    trace_ids = list(dict.fromkeys(
        [use_case.id, *(value for group in groups for value in group.trace_use_case_ids)]
    ))
    candidate = {
        "collaborationId": use_case.id,
        "useCaseIds": trace_ids,
        "entryActor": use_case.primary_actor or None,
        "calls": calls,
    }
    report = run_checks(
        COLLABORATION_CHECKS,
        candidate,
        CollaborationContext(index, model_payload, use_case),
    )
    if report.errors or report.findings:
        raise ValueError("; ".join([*report.errors, *_finding_text(report.findings)]))
    return Collaboration.model_validate(candidate)


class CombinedReplacementRequired(RuntimeError):
    """call-plan 수리가 반복되어 유스케이스 전체 교체가 필요함을 알린다."""

    def __init__(
        self,
        use_case_id: str,
        issue: str,
        previous_plan: CallPlanProposal,
        repair_context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(issue)
        self.use_case_id = use_case_id
        self.issue = issue
        self.previous_plan = previous_plan
        self.repair_context = repair_context


def _accepted_payload(
    index: ScenarioIndex,
    model: BCEModel,
    use_case: UseCase,
    directive: str,
    previous: CallPlanProposal | None = None,
) -> dict[str, Any]:
    """call plan을 한 번 교체하고, 실패하면 operation까지 고치도록 알린다.

    호출 순서만 잘못된 경우에는 이 한 번의 국소 수리로 충분하다. 새 계획도 실행할 수
    없다면 오류 문구가 달라질 때마다 같은 범위에서 계속 시도하지 않는다. 상위 흐름이
    해당 유스케이스의 operation과 calls를 함께 교체하며 전체 자동 수리는 계속된다.
    """

    # Provider/schema 예외는 semantic finding으로 바꾸지 않는다.
    candidate = propose_call_plan(
        index,
        model,
        use_case,
        previous=previous,
        finding=directive,
    )
    try:
        return materialize(index, model, use_case, candidate).model_dump(by_alias=True)
    except ValueError as error:
        if isinstance(error, CallPlanViolation):
            repaired = repair_communication_parent(
                index, model, use_case, candidate, error,
            )
            if repaired is not None:
                candidate = repaired
                try:
                    return materialize(
                        index, model, use_case, candidate,
                    ).model_dump(by_alias=True)
                except ValueError as repaired_error:
                    error = repaired_error
        error_text = f"{type(error).__name__}: {error}"
        raise CombinedReplacementRequired(
            use_case.id,
            error_text,
            candidate,
        ) from error


def _cache_key(
    index: ScenarioIndex,
    model: BCEModel,
    use_case: UseCase,
    directive: str,
    previous: CallPlanProposal | None = None,
) -> str:
    unit_slice = _use_case_payload(index, model.model_dump(by_alias=True), use_case)
    if previous is not None:
        unit_slice["previousPlan"] = previous.model_dump(by_alias=True)
    return accepted_unit_key(
        "use-case-collaboration",
        unit_slice=unit_slice,
        inventory=model.model_dump(by_alias=True),
        feedback=" ".join(directive.split()),
        prompt=CALL_PLAN_PROMPT,
        schema=CallPlanProposal,
        provider=configured_provider_identity(build_llm_connection().base_url),
        model=settings.model,
        seed=settings.seed,
        temperature=effective_temperature(settings.model, settings.temperature),
        reasoning_effort=call_plan_reasoning_effort(),
        max_completion_tokens=call_plan_max_completion_tokens(),
        extra={
            "bindingPrompt": BINDING_PROMPT,
            "bindingReasoningEffort": "low",
            "bindingMaxCompletionTokens": min(
                settings.design_class_collaboration_max_completion_tokens, 2048,
            ),
            "bindingCandidateVersion": 5,
        },
    )


def process_use_case(
    index: ScenarioIndex,
    model: BCEModel,
    use_case: UseCase,
    directive: str = "",
    *,
    previous: CallPlanProposal | None = None,
    cache: AcceptedUnitCache | None = None,
) -> Collaboration:
    """call plan을 국소 교체하고 필요하면 상위 결합 수리로 범위를 넓힌다."""

    if not _groups(index, use_case):
        raise ValueError("use case has no actor entry")
    if cache is None:
        record_cache_outcome(None, operation="InteractionCallPlan", unit=use_case.id)
        payload = _accepted_payload(index, model, use_case, directive, previous)
    else:
        result = cache.get_or_compute(
            _cache_key(index, model, use_case, directive, previous),
            lambda: _accepted_payload(index, model, use_case, directive, previous),
        )
        record_cache_outcome(result, operation="InteractionCallPlan", unit=use_case.id)
        payload = result.value
    accepted = Collaboration.model_validate(payload)
    report = run_checks(
        COLLABORATION_CHECKS,
        accepted.model_dump(by_alias=True),
        CollaborationContext(index, model.model_dump(by_alias=True), use_case),
    )
    if report.errors or report.findings:
        raise ValueError("cached collaboration is invalid")
    return accepted


__all__ = [
    "BindingSourceViolation",
    "CallPlanViolation",
    "CombinedReplacementRequired",
    "materialize",
    "process_use_case",
    "propose_call_plan",
    "repair_communication_parent",
    "select_ambiguous_bindings",
]
