"""유스케이스별 operation 제안 뒤 완성 skeleton에서 협업을 구체화한다."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.design.schemas.class_model import BCEModel, Collaboration
from app.design.services.class_diagram import collaboration, operations
from app.design.services.class_diagram.cache import (
    AcceptedUnitCache,
    accepted_unit_key,
    configured_provider_identity,
    record_cache_outcome,
)
from app.design.services.class_diagram.models import (
    AcceptedFragment,
    AcceptedInventory,
    ClassBindingStalled,
    Collision,
    DataTypeCollision,
    GenerationStalled,
    RepairBudget,
)
from app.design.services.class_diagram.proposals import (
    CallPlanProposal,
    CombinedUnitProposal,
    OperationFragment,
)
from app.design.services.class_diagram.scenario import (
    ExecutionGroup,
    ScenarioIndex,
    UseCase,
    id_key,
)
from app.design.services.class_diagram.identity import materialize_pre_collaboration_refs
from app.design.services.class_diagram.type_system import (
    type_expression_is_well_formed,
    type_is_resolved,
)
from app.design.services.class_diagram.validation.collaboration import (
    COLLABORATION_CHECKS,
    CollaborationContext,
)
from app.design.services.class_diagram.validation.model import validate_class_model
from app.design.services.common.structured import bind_context, parse_structured
from app.llm_connection import build_llm_connection
from app.llm_profiles import effective_temperature
from app.validation import run_checks, stable_digest

_COMBINED_PROMPT = operations.operation_prompt() + """

Also return a flat call forest for this complete use case. Refer to operations
only as ClassName.methodName. Each supplied actorEntry creates exactly one
Boundary root in the same order; no other root is allowed. Each non-root uses
the one-based position of an earlier call in the latest root as parentCallIndex.
The position is counted in the complete flat calls array and never restarts
after a new root.
When actorEntry.actor is present, its root Boundary must represent that actor's
interface. Do not use a supporting actor or external-system Boundary as that
root; those Boundaries are downstream calls from Control.
Always include parentCallIndex: use null for each root and an integer for every
non-root.
Ordinary results return through the existing call chain. Use a Control-to-
Boundary call only when the scenario explicitly requires the system to initiate
a separate interaction with an external actor or system through that Boundary,
such as an asynchronous notification, and parent it to Control. The Boundary
class used by a root must not appear again inside that root. Entities may
collaborate with other Entities, but do not call a Control or Boundary directly.
For one request-response actor entry, the root Boundary must call exactly one
direct orchestration Control operation that covers the complete actor-visible
behavior. Return multiple response values together in a concrete DTO or
valueObject. Do not nest sequential retrieval under a parent Control when a child
needs a value that the parent itself returns; a call's return goes back to its
caller and is not available to descendants. Keep Control delegation when child
inputs are independently available from entry inputs, preconditions, or earlier
completed calls. parentCallIndex identifies the caller only; it does not express
a data dependency.
The same operation may be called in several roots. Cover each actor entry's step
range through Boundary to Control and, when needed, Entity. If actorEntries is
empty, return no calls; its operations can be used by an including use case.
"""


class TypeFieldCorrection(BaseModel):
    """One narrowly-scoped replacement in an otherwise accepted proposal."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    path: str = Field(min_length=1)
    corrected_type: str = Field(alias="correctedType", min_length=1)


class TypeFieldRepairProposal(BaseModel):
    """The type-only repair response must not be able to alter calls or operations."""

    model_config = ConfigDict(extra="forbid")

    corrections: list[TypeFieldCorrection]


_TYPE_FIELD_REPAIR_PROMPT = """Repair only the listed type strings. Return JSON matching
the supplied schema: {"corrections":[{"path":"...","correctedType":"..."}]}.
Return every listed path exactly once; do not return another path. Do not change names,
operations, step references, DataType declarations, or calls.

Canonical type grammar: a scalar primitive, a declared name, or List<T>, Set<T>,
Collection<T>, Iterable<T>, or Optional<T>; containers take one recursively valid type.
`byte[]` is also valid. `?` optionally wraps a complete type. `void` is permitted only
as an operation return type. Every non-primitive name must be in declaredNames.

Examples (invalid -> valid): String[ -> String; listItem -> List<Item> when Item is
declared; optional list Item -> Optional<List<Item>> when Item is declared.
"""


def _declared_type_names(
    raw: dict[str, Any], inventory: AcceptedInventory, reserved_types: list[dict[str, Any]],
) -> set[str]:
    """Names available to a fragment before its type-only repair is applied."""

    names = {
        str(item.get("className") or item.get("name") or "").strip()
        for item in inventory.as_payload().get("Classes") or []
        if isinstance(item, dict)
    }
    for items in (
        inventory.as_payload().get("DataTypes") or [],
        reserved_types,
        raw.get("fragment", {}).get("DataTypes") or [],
    ):
        names.update(
            str(item.get("name") or "").strip()
            for item in items if isinstance(item, dict)
        )
    names.discard("")
    return names


def _invalid_type_fields(
    raw: dict[str, Any], inventory: AcceptedInventory, reserved_types: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Enumerate proposal type strings that fail the same grammar/reference contract."""

    fragment = raw.get("fragment") if isinstance(raw.get("fragment"), dict) else {}
    names = _declared_type_names(raw, inventory, reserved_types)
    findings: list[dict[str, Any]] = []

    def add(path: str, value: object, *, allow_void: bool) -> None:
        type_name = str(value or "").strip()
        if type_is_resolved(type_name, names, allow_void=allow_void):
            return
        reason = (
            "type does not match the canonical grammar"
            if not type_expression_is_well_formed(type_name)
            else "type references a name outside declaredNames"
        )
        findings.append({"path": path, "value": type_name, "reason": reason,
                         "allowVoid": allow_void})

    for data_type_index, data_type in enumerate(fragment.get("DataTypes") or []):
        if not isinstance(data_type, dict):
            continue
        for field_index, field in enumerate(data_type.get("fields") or []):
            if isinstance(field, dict):
                add(f"/fragment/DataTypes/{data_type_index}/fields/{field_index}/type",
                    field.get("type"), allow_void=False)
    for class_index, class_set in enumerate(fragment.get("Classes") or []):
        if not isinstance(class_set, dict):
            continue
        for operation_index, operation in enumerate(class_set.get("operations") or []):
            if not isinstance(operation, dict):
                continue
            for parameter_index, parameter in enumerate(operation.get("parameters") or []):
                if isinstance(parameter, dict):
                    add(f"/fragment/Classes/{class_index}/operations/{operation_index}"
                        f"/parameters/{parameter_index}/type", parameter.get("type"),
                        allow_void=False)
            add(f"/fragment/Classes/{class_index}/operations/{operation_index}/returnType",
                operation.get("returnType"), allow_void=True)
    return findings


def _path_target(raw: dict[str, Any], path: str) -> dict[str, Any] | None:
    """Resolve a known JSON pointer to the dict that owns its final type property."""

    current: Any = raw
    parts = [part for part in path.split("/") if part]
    if not parts or parts[-1] not in {"type", "returnType"}:
        return None
    for part in parts[:-1]:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current if isinstance(current, dict) else None


def _repair_invalid_type_fields(
    raw: dict[str, Any], inventory: AcceptedInventory, reserved_types: list[dict[str, Any]],
    *, use_case_id: str,
) -> dict[str, Any]:
    """Attempt one validated, type-only patch; leave the candidate for full repair on failure."""

    findings = _invalid_type_fields(raw, inventory, reserved_types)
    if not findings:
        return raw
    fragment = raw.get("fragment") if isinstance(raw.get("fragment"), dict) else {}
    payload = {
        "invalidTypes": findings,
        "declaredNames": sorted(_declared_type_names(raw, inventory, reserved_types)),
        "fragmentContext": {
            "DataTypes": fragment.get("DataTypes") or [],
            "Classes": [
                {"className": item.get("className"), "operations": item.get("operations")}
                for item in fragment.get("Classes") or [] if isinstance(item, dict)
            ],
        },
    }
    try:
        repaired = parse_structured(
            [
                {"role": "system", "content": _TYPE_FIELD_REPAIR_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            TypeFieldRepairProposal,
            reasoning_effort=operations.operation_reasoning_effort(),
            max_completion_tokens=min(1024, operations.operation_max_completion_tokens()),
            operation="InteractionCombinedUnitTypeFieldRepair",
            metadata={"useCaseId": use_case_id, "invalidTypeCount": len(findings)},
        )
        response = TypeFieldRepairProposal.model_validate(repaired)
    except (TypeError, ValueError):
        # A malformed narrow repair is handled by the existing full repair.
        return raw
    expected_paths = {item["path"] for item in findings}
    corrections = response.corrections
    paths = [item.path for item in corrections]
    if len(paths) != len(set(paths)) or set(paths) != expected_paths:
        return raw
    allow_void = {item["path"]: item["allowVoid"] for item in findings}
    names = _declared_type_names(raw, inventory, reserved_types)
    if any(not type_is_resolved(item.corrected_type, names, allow_void=allow_void[item.path])
           for item in corrections):
        return raw
    patched = deepcopy(raw)
    for correction in corrections:
        target = _path_target(patched, correction.path)
        if target is None:
            return raw
        target[correction.path.rsplit("/", 1)[-1]] = correction.corrected_type
    return patched


def _same_boundary_response_operations(raw: dict[str, Any]) -> set[str]:
    """결합 call forest가 최초 Boundary로 되돌아간 operation을 찾는다.

    같은 root 안에서 최초 Boundary 클래스가 다시 receiver로 등장하면 현재 요청의
    결과를 별도 호출로 표현한 것이다. 다른 Boundary 클래스 호출은 외부 시스템과의
    상호작용일 수 있으므로 그대로 둔다.
    """

    result: set[str] = set()
    root_owner = ""
    for call in raw.get("calls") or []:
        if not isinstance(call, dict):
            continue
        operation_ref = str(call.get("operationRef") or "")
        owner = operation_ref.partition(".")[0]
        if call.get("parentCallIndex") is None:
            root_owner = owner
        elif root_owner and owner == root_owner:
            result.add(operation_ref)
    return result


def _groups(index: ScenarioIndex, use_case: UseCase) -> tuple[ExecutionGroup, ...]:
    return tuple(group for group in index.groups if group.use_case_id == use_case.id)


def _payload(
    index: ScenarioIndex,
    inventory: AcceptedInventory,
    use_case: UseCase,
    *,
    reserved: list[dict[str, Any]],
    reserved_types: list[dict[str, Any]],
    previous: dict[str, Any] | None = None,
    issue: str = "",
    history: list[dict[str, str]] | None = None,
    repair_context: dict[str, Any] | None = None,
    repair_guidance: str | None = None,
) -> dict[str, Any]:
    payload = operations.operation_payload(
        index,
        inventory,
        use_case,
        reserved=reserved,
        reserved_types=reserved_types,
        allowed_step_ids=tuple(step.id for step in use_case.steps),
    )
    payload["actorEntries"] = [
        {
            "actorStepRef": group.actor_step,
            "actor": group.entry_actor,
            "requiredStepRefs": list(group.required_step_ids),
        }
        for group in _groups(index, use_case)
    ]
    if issue:
        payload.update({
            "task": "Return a full replacement for this use-case unit.",
            "previousCombined": previous,
            "finding": issue,
            "repairHistory": history or [],
        })
    if repair_context is not None:
        payload["repairContext"] = repair_context
    if repair_guidance and repair_guidance.strip():
        payload["repairGuidance"] = repair_guidance.strip()
    return payload


def _propose_unit(
    index: ScenarioIndex,
    inventory: AcceptedInventory,
    use_case: UseCase,
    *,
    reserved: list[dict[str, Any]],
    reserved_types: list[dict[str, Any]],
    budget: RepairBudget,
    previous: dict[str, Any] | None = None,
    initial_issue: str = "",
    repair_history: list[dict[str, str]] | None = None,
    repair_context: dict[str, Any] | None = None,
    repair_guidance: str | None = None,
) -> tuple[AcceptedFragment, dict[str, Any]]:
    """operation 검사를 통과할 때까지 한 유스케이스 제안만 전체 교체한다."""

    issue = initial_issue
    # 바깥의 call-plan 검사에서 다시 결합 수리로 돌아온 경우에도 이전 후보와
    # 실패 이유를 이어받는다.
    history = repair_history if repair_history is not None else []
    seen_states: set[str] = set()
    prior = previous
    while True:
        if issue:
            budget.consume(issue)
        prompt_payload = _payload(
            index,
            inventory,
            use_case,
            reserved=reserved,
            reserved_types=reserved_types,
            previous=prior,
            issue=issue,
            history=history,
            repair_context=repair_context,
            repair_guidance=repair_guidance,
        )
        parsed = parse_structured(
            [
                {"role": "system", "content": _COMBINED_PROMPT},
                {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
            ],
            CombinedUnitProposal,
            reasoning_effort=operations.operation_reasoning_effort(),
            max_completion_tokens=operations.operation_max_completion_tokens(),
            operation="InteractionCombinedUnitRepair" if issue else "InteractionCombinedUnit",
            metadata={
                "useCaseId": use_case.id,
                "executionSlice": use_case.id,
                "candidateCount": len(prompt_payload["fixedClasses"]),
            },
        )
        raw = CombinedUnitProposal.model_validate(parsed).model_dump(by_alias=True)
        raw = _repair_invalid_type_fields(
            raw,
            inventory,
            reserved_types,
            use_case_id=use_case.id,
        )
        try:
            fragment = operations.normalize_operation_fragment(
                raw["fragment"],
                index,
                inventory,
                use_case,
                reserved=reserved,
                reserved_types=reserved_types,
                allowed_step_ids=tuple(step.id for step in use_case.steps),
                same_boundary_response_operations=(
                    _same_boundary_response_operations(raw)
                ),
            )
            fragment = operations.validate_operation_fragment(
                fragment,
                index,
                inventory,
                use_case,
                reserved_types=reserved_types,
                allowed_step_ids=tuple(step.id for step in use_case.steps),
            )
            return fragment, raw
        except (ValueError, TypeError) as error:
            issue = f"{type(error).__name__}: {error}"
            candidate_digest = stable_digest(raw)
            state_digest = stable_digest({
                "candidate": candidate_digest, "finding": issue,
            })
            repeated = state_digest in seen_states
            seen_states.add(state_digest)
            history.append({"candidateDigest": candidate_digest, "error": issue})
            if repeated:
                issue += (
                    "\nThe same candidate and finding repeated. Return a materially "
                    "different complete operation fragment and call forest."
                )
            prior = raw


def _repair_history_item(
    candidate: dict[str, Any], issue: str,
) -> dict[str, str]:
    """다음 LLM 요청에 넣을 수 있도록 실패 후보를 짧게 기록한다."""

    return {
        "candidateDigest": stable_digest(candidate),
        # issue 뒤에는 바로 앞 call-plan의 상세 ledger가 붙을 수 있다. 과거 후보마다
        # 이를 다시 중첩하지 않고 실제 검사 메시지만 보존한다.
        "error": issue.partition("\n\n")[0],
    }


def _catalog(model: BCEModel) -> dict[str, str]:
    return {
        f"{owner.class_name}.{operation.name}": operation.operation_id
        for owner in model.Classes for operation in owner.operations
    }


def _resolved_plan(raw: dict[str, Any], model: BCEModel) -> CallPlanProposal:
    """정규화로 사라진 call을 빼고 자식을 가장 가까운 남은 조상에 연결한다."""

    proposal = CombinedUnitProposal.model_validate(raw)
    catalog = _catalog(model)
    raw_refs = {
        f"{class_set.class_name}.{operation.name}"
        for class_set in proposal.fragment.Classes
        for operation in class_set.operations
    }
    unknown = {
        call.operation_ref for call in proposal.calls
        if call.operation_ref not in raw_refs and call.operation_ref not in catalog
    }
    if unknown:
        raise ValueError("unknown operationRef: " + ", ".join(sorted(unknown)))
    calls = proposal.calls
    kept = [
        position for position, call in enumerate(calls, start=1)
        if call.operation_ref in catalog
    ]
    positions = {old: new for new, old in enumerate(kept, start=1)}
    resolved: list[dict[str, Any]] = []
    for old in kept:
        call = calls[old - 1]
        parent = call.parent_call_index
        visited: set[int] = set()
        while parent is not None and parent not in positions:
            if parent < 1 or parent >= old or parent in visited:
                raise ValueError("parentCallIndex must reference an earlier call")
            visited.add(parent)
            parent = calls[parent - 1].parent_call_index
        resolved.append({
            "receiverOperationId": catalog[call.operation_ref],
            "parentCallIndex": positions.get(parent) if parent is not None else None,
        })
    return CallPlanProposal.model_validate({"calls": resolved})


def _materialize_use_case(
    index: ScenarioIndex,
    skeleton: BCEModel,
    use_case: UseCase,
    raw: dict[str, Any],
    budget: RepairBudget,
    binding_source_decision: dict[str, Any] | None = None,
) -> Collaboration:
    """임시 calls를 쓰고, 실패하면 operation을 보존한 call-plan 수리를 시작한다."""

    previous: CallPlanProposal | None = None
    try:
        previous = _resolved_plan(raw, skeleton)
        return collaboration.materialize(
            index, skeleton, use_case, previous,
            binding_source_decision=(
                binding_source_decision
                if binding_source_decision
                and binding_source_decision.get("useCaseId") == use_case.id
                else None
            ),
        )
    except ValueError as error:
        finding = f"{type(error).__name__}: {error}"
        # A missing finite value source is not a call-order problem.  Retrying a
        # call-plan-only proposal cannot create one, so hand it directly to the
        # existing combined-unit seam that owns both the operation and its calls.
        if isinstance(error, collaboration.BindingSourceViolation):
            if previous is None:
                raise
            raise collaboration.CombinedReplacementRequired(
                use_case.id,
                finding,
                previous,
                _binding_repair_context(error.repair_context, error.repair_slot),
            ) from error
        allowed_parents = (
            error.repair_context.get("allowedParentCallIndexes") or []
            if isinstance(error, collaboration.CallPlanViolation)
            else []
        )
        if previous is not None and allowed_parents:
            budget.consume(finding)
            repaired = collaboration.repair_communication_parent(
                index, skeleton, use_case, previous, error,
            )
            if repaired is not None:
                try:
                    return collaboration.materialize(
                        index, skeleton, use_case, repaired,
                    )
                except ValueError as repaired_error:
                    previous = repaired
                    finding = f"{type(repaired_error).__name__}: {repaired_error}"
            budget.consume(finding)
        else:
            budget.consume(finding)
        return collaboration.process_use_case(
            index,
            skeleton,
            use_case,
            directive=(
                "Preserve every operation and replace only the call plan. "
                f"Resolve this exact issue: {finding}"
            ),
            previous=previous,
        )


def _binding_repair_context(
    context: dict[str, Any], slot: dict[str, int],
) -> dict[str, Any]:
    """Add the canonical call pointer needed to bound an owning-unit repair."""

    result = dict(context)
    result.update(slot)
    return result


def _same_binding_event(
    first: dict[str, Any] | None,
    repeated: dict[str, Any] | None,
    previous_candidate: dict[str, Any],
    current_candidate: dict[str, Any],
) -> bool:
    """Match a missing-source stall only when its complete candidate also repeats."""

    if not first or not repeated:
        return False
    if first.get("code") != "BINDING_SOURCE_UNAVAILABLE":
        return False
    if repeated.get("code") != "BINDING_SOURCE_UNAVAILABLE":
        return False
    fields = ("useCaseId", "actorEntryIndex", "callIndex", "parameterIndex")
    return (
        all(first.get(field) == repeated.get(field) for field in fields)
        and stable_digest(previous_candidate) == stable_digest(current_candidate)
    )


def _collaboration_valid(
    index: ScenarioIndex,
    skeleton: BCEModel,
    use_case: UseCase,
    value: Collaboration,
) -> bool:
    report = run_checks(
        COLLABORATION_CHECKS,
        value.model_dump(by_alias=True),
        CollaborationContext(index, skeleton.model_dump(by_alias=True), use_case),
    )
    return not report.errors and not report.findings


def _build_uncached(
    index: ScenarioIndex,
    inventory: AcceptedInventory,
    *,
    repair_guidance: str | None = None,
    binding_source_decision: dict[str, Any] | None = None,
) -> BCEModel:
    use_cases = sorted(index.use_cases, key=lambda item: id_key(item.id))
    budgets = {use_case.id: RepairBudget(use_case.id) for use_case in use_cases}
    inventory_model = operations.compose_operation_units(inventory, [])
    reserved: list[dict[str, Any]] = []
    reserved_types = [item.model_dump(by_alias=True) for item in inventory_model.DataTypes]
    # 1단계: 모든 유스케이스는 같은 inventory snapshot을 보고 설정된 수만큼 병렬 제안한다.
    workers = max(1, min(
        len(use_cases) or 1,
        int(getattr(settings, "design_class_behavior_parallelism", 4)),
    ))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                bind_context(_propose_unit),
                index,
                inventory,
                use_case,
                reserved=reserved,
                reserved_types=reserved_types,
                budget=budgets[use_case.id],
                repair_guidance=repair_guidance,
            )
            for use_case in use_cases
        ]
        proposed = [future.result() for future in futures]
    committed: list[AcceptedFragment] = []
    raw_by_use_case: dict[str, dict[str, Any]] = {}
    for position, (use_case, (fragment, raw)) in enumerate(
        zip(use_cases, proposed, strict=True), start=1,
    ):
        collision_states: set[str] = set()
        collision_history: list[dict[str, str]] = []
        while True:
            try:
                preview = operations.compose_operation_units(inventory, [*committed, fragment])
                break
            except (Collision, DataTypeCollision) as error:
                snapshot = operations.compose_operation_units(inventory, committed)
                issue = f"{type(error).__name__}: {error}"
                state = stable_digest({"candidate": raw, "finding": issue})
                if state in collision_states:
                    issue += (
                        "\nThe same colliding candidate repeated. Return a materially "
                        "different complete unit."
                    )
                collision_states.add(state)
                collision_history.append(_repair_history_item(raw, issue))
                fragment, raw = _propose_unit(
                    index,
                    inventory,
                    use_case,
                    reserved=operations.reserved_operations(snapshot),
                    reserved_types=[
                        item.model_dump(by_alias=True) for item in snapshot.DataTypes
                    ],
                    budget=budgets[use_case.id],
                    previous=raw,
                    initial_issue=issue,
                    repair_history=collision_history,
                    repair_guidance=repair_guidance,
                )
        committed.append(fragment)
        raw_by_use_case[use_case.id] = raw
        operations.emit_preview(
            preview.model_dump(by_alias=True),
            "operations", use_case.id, position + 1, len(use_cases) + 1,
        )
    skeleton = materialize_pre_collaboration_refs(
        None, operations.compose_operation_units(inventory, committed, final=True),
    )

    # 2단계: 완성된 operation catalog에서 provisional calls를 구체화한다. actor 없는
    # include는 독립 collaboration을 만들지 않고 부모 수리에서 후보 operation으로 쓰인다.
    accepted: dict[str, Collaboration] = {}
    standalone = [use_case for use_case in use_cases if _groups(index, use_case)]
    while len(accepted) < len(standalone):
        for use_case in standalone:
            current = accepted.get(use_case.id)
            if current is not None and _collaboration_valid(
                index, skeleton, use_case, current,
            ):
                continue
            try:
                value = _materialize_use_case(
                    index,
                    skeleton,
                    use_case,
                    raw_by_use_case[use_case.id],
                    budgets[use_case.id],
                    binding_source_decision,
                )
            except collaboration.CombinedReplacementRequired as signal:
                # 같은 call-plan 상태가 반복되면 현재 유스케이스의 operation+calls만
                # 다시 받고, 이미 수락된 다른 협업은 새 skeleton에서 재검사한다.
                unit_index = use_cases.index(use_case)
                others = [
                    fragment for position, fragment in enumerate(committed)
                    if position != unit_index
                ]
                snapshot = operations.compose_operation_units(inventory, others)
                previous = raw_by_use_case[use_case.id]
                issue = signal.issue
                repair_context = signal.repair_context
                repair_history = [_repair_history_item(previous, issue)]
                while True:
                    fragment, raw = _propose_unit(
                        index,
                        inventory,
                        use_case,
                        reserved=operations.reserved_operations(snapshot),
                        reserved_types=[
                            item.model_dump(by_alias=True) for item in snapshot.DataTypes
                        ],
                        budget=budgets[use_case.id],
                        previous=previous,
                        initial_issue=issue,
                        repair_history=repair_history,
                        repair_context=repair_context,
                        repair_guidance=repair_guidance,
                    )
                    candidate_fragments = list(committed)
                    candidate_fragments[unit_index] = fragment
                    try:
                        skeleton = materialize_pre_collaboration_refs(
                            None,
                            operations.compose_operation_units(
                                inventory, candidate_fragments, final=True,
                            ),
                        )
                    except (Collision, DataTypeCollision) as error:
                        previous = raw
                        issue = f"{type(error).__name__}: {error}"
                        repair_history.append(_repair_history_item(raw, issue))
                        continue
                    try:
                        # 결합 수리는 operation뿐 아니라 calls도 교체한다. 새 calls를
                        # 여기서 검사하지 않으면 바깥 루프가 새 수리 이력을 만들며 같은
                        # call-plan 실패로 되돌아간다.
                        value = _materialize_use_case(
                            index, skeleton, use_case, raw, budgets[use_case.id],
                            binding_source_decision,
                        )
                    except collaboration.CombinedReplacementRequired as repeated:
                        if _same_binding_event(
                            repair_context,
                            repeated.repair_context,
                            previous,
                            raw,
                        ):
                            raise ClassBindingStalled(
                                use_case.id, repeated.issue, repeated.repair_context,
                            ) from repeated
                        previous = raw
                        issue = repeated.issue
                        repair_context = repeated.repair_context
                        repair_history.append(_repair_history_item(raw, issue))
                        continue
                    break
                committed = candidate_fragments
                raw_by_use_case[use_case.id] = raw
                accepted = {
                    owner: collaboration_value
                    for owner, collaboration_value in accepted.items()
                    if _collaboration_valid(
                        index,
                        skeleton,
                        index.use_case(owner),
                        collaboration_value,
                    )
                }
            accepted[use_case.id] = value
            ordered_accepted = [
                accepted[item.id] for item in standalone if item.id in accepted
            ]
            operations.emit_preview(
                {
                    **skeleton.model_dump(by_alias=True),
                    "Collaborations": [
                        item.model_dump(by_alias=True) for item in ordered_accepted
                    ],
                },
                "collaborations", use_case.id, len(accepted), len(standalone),
            )
    return BCEModel.model_validate({
        **skeleton.model_dump(by_alias=True),
        "Collaborations": [
            accepted[use_case.id] for use_case in standalone
        ],
    })


def _previous_combined_unit(
    fragment: AcceptedFragment,
    model: BCEModel,
    plan: CallPlanProposal,
) -> dict[str, Any]:
    """분리 수리에서 반복된 call plan을 결합 수리 입력으로 되돌린다."""

    operation_refs = {
        operation.operation_id: f"{owner.class_name}.{operation.name}"
        for owner in model.Classes for operation in owner.operations
    }
    return {
        "fragment": fragment.as_payload(),
        "calls": [
            {
                "operationRef": operation_refs[call.receiver_operation_id],
                "parentCallIndex": call.parent_call_index,
            }
            for call in plan.calls
        ],
    }


def replace_use_case_unit(
    index: ScenarioIndex,
    current: BCEModel,
    use_case: UseCase,
    signal: collaboration.CombinedReplacementRequired,
) -> tuple[BCEModel, Collaboration]:
    """반복된 call-plan 수리를 operation과 calls의 결합 교체로 넓힌다."""

    from app.design.services.class_diagram.feedback import (
        fragments_from_model,
        inventory_from_model,
    )

    inventory = inventory_from_model(current)
    fragments = fragments_from_model(index, current)
    fragment = fragments.get(use_case.id)
    if fragment is None:
        raise ValueError(f"use case has no operation fragment: {use_case.id}")
    others = {key: value for key, value in fragments.items() if key != use_case.id}
    snapshot = operations.compose_fragments(inventory, others)
    previous = _previous_combined_unit(fragment, current, signal.previous_plan)
    issue = signal.issue
    repair_context = signal.repair_context
    budget = RepairBudget(use_case.id)
    collision_states: set[str] = set()
    repair_history = [_repair_history_item(previous, issue)]
    while True:
        replacement, raw = _propose_unit(
            index,
            inventory,
            use_case,
            reserved=operations.reserved_operations(snapshot),
            reserved_types=[
                item.model_dump(by_alias=True) for item in snapshot.DataTypes
            ],
            budget=budget,
            previous=previous,
            initial_issue=issue,
            repair_history=repair_history,
            repair_context=repair_context,
        )
        candidate_fragments = {**others, use_case.id: replacement}
        try:
            skeleton = materialize_pre_collaboration_refs(
                current,
                operations.compose_fragments(inventory, candidate_fragments),
            )
        except (Collision, DataTypeCollision) as error:
            issue = f"{type(error).__name__}: {error}"
            state = stable_digest({"candidate": raw, "finding": issue})
            if state in collision_states:
                issue += (
                    "\nThe same colliding candidate repeated. Return a materially "
                    "different complete unit."
                )
            collision_states.add(state)
            previous = raw
            repair_history.append(_repair_history_item(raw, issue))
            continue
        operations.emit_preview(
            skeleton.model_dump(by_alias=True),
            "operations",
            use_case.id,
            len(candidate_fragments) + 1,
            len(index.use_cases) + 1,
        )
        try:
            accepted = _materialize_use_case(index, skeleton, use_case, raw, budget)
        except collaboration.CombinedReplacementRequired as repeated:
            if _same_binding_event(
                repair_context,
                repeated.repair_context,
                previous,
                raw,
            ):
                raise ClassBindingStalled(
                    use_case.id, repeated.issue, repeated.repair_context,
                ) from repeated
            previous = raw
            issue = repeated.issue
            repair_context = repeated.repair_context
            repair_history.append(_repair_history_item(raw, issue))
            continue
        return skeleton, accepted


def _model_cache_key(
    index: ScenarioIndex,
    inventory: AcceptedInventory,
    binding_source_decision: dict[str, Any] | None = None,
) -> str:
    return accepted_unit_key(
        "complete-class-model",
        unit_slice=index.raw,
        inventory=inventory.as_payload(),
        feedback={},
        prompt=_COMBINED_PROMPT,
        schema=BCEModel,
        provider=configured_provider_identity(build_llm_connection().base_url),
        model=settings.model,
        seed=settings.seed,
        temperature=effective_temperature(settings.model, settings.temperature),
        reasoning_effort=operations.operation_reasoning_effort(),
        max_completion_tokens=operations.operation_max_completion_tokens(),
        extra={
            "bindingSourceDecision": binding_source_decision,
            "combinedProposalSchema": CombinedUnitProposal.model_json_schema(),
            "operationFragmentSchema": OperationFragment.model_json_schema(),
            "callPlanPrompt": collaboration.CALL_PLAN_PROMPT,
            "callPlanCap": collaboration.call_plan_max_completion_tokens(),
            "parentSelectionPrompt": collaboration.PARENT_SELECTION_PROMPT,
            "bindingPrompt": collaboration.BINDING_PROMPT,
            "version": 5,
        },
    )


def build_model(
    index: ScenarioIndex,
    inventory: AcceptedInventory,
    *,
    cache: AcceptedUnitCache | None = None,
    repair_guidance: str | None = None,
    binding_source_decision: dict[str, Any] | None = None,
) -> BCEModel:
    """두 단계 생성 결과 전체만 cache하고 hit에서도 최종 검사를 다시 실행한다."""

    if cache is None:
        record_cache_outcome(None, operation="InteractionClassModel", unit="class-model")
        model = _build_uncached(
            index,
            inventory,
            repair_guidance=repair_guidance,
            binding_source_decision=binding_source_decision,
        )
    else:
        result = cache.get_or_compute(
            _model_cache_key(index, inventory, binding_source_decision),
            lambda: _build_uncached(
                index, inventory, repair_guidance=repair_guidance,
                binding_source_decision=binding_source_decision,
            ).model_dump(by_alias=True),
        )
        record_cache_outcome(result, operation="InteractionClassModel", unit="class-model")
        model = BCEModel.model_validate(result.value)
        if result.status in {"hit", "coalesced"}:
            # whole-model cache도 operation 수락 경계를 건너뛰지 않는다. 저장 모델에서
            # 유스케이스 fragment를 복원해 schema·step/type 검사를 다시 실행한다.
            from app.design.services.class_diagram.feedback import fragments_from_model

            fragments = fragments_from_model(index, model)
            reserved_types = [item.model_dump(by_alias=True) for item in model.DataTypes]
            for use_case in index.use_cases:
                fragment = fragments.get(use_case.id)
                if fragment is not None:
                    operations.validate_operation_fragment(
                        fragment,
                        index,
                        inventory,
                        use_case,
                        reserved_types=reserved_types,
                        allowed_step_ids=tuple(step.id for step in use_case.steps),
                    )
    report = validate_class_model(model, index)
    if report.errors or report.findings:
        details = [
            *report.errors,
            *(
                f"{finding.rule_id} {finding.location}: {finding.message}"
                for finding in report.findings
            ),
        ]
        raise ValueError("class model is invalid: " + "; ".join(details))
    return model


__all__ = [
    "ClassBindingStalled", "GenerationStalled", "build_model", "replace_use_case_unit",
]
