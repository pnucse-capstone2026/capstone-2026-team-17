"""유스케이스 operation 제안을 정규화·검사하고 BCE 모델로 조립한다.

생성 순서와 collaboration은 ``generation``이 맡는다. 이 모듈은 operation payload,
피드백 수리, 결정론적 정규화·검사·compose와 진행 preview만 제공한다.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from app.config import settings
from app.design import progress as design_progress
from app.design.contracts.type_system import (
    DesignTypeError,
    canonical_design_type,
)
from app.design.schemas.class_model import BCEModel, canonical_operation_id
from app.design.services.class_diagram.cache import (
    AcceptedUnitCache,
    accepted_unit_key,
    configured_provider_identity,
    record_cache_outcome,
)
from app.design.services.class_diagram.inventory import finding_text
from app.design.services.class_diagram.models import AcceptedFragment, AcceptedInventory
from app.design.services.class_diagram.models import (
    Collision as _Collision,
)
from app.design.services.class_diagram.models import (
    DataTypeCollision as _DataTypeCollision,
)
from app.design.services.class_diagram.plantuml import generate_plantuml_from_bce_json
from app.design.services.class_diagram.proposals import (
    OperationFragment,
    legacy_data_type,
    structured_data_type,
)
from app.design.services.class_diagram.scenario import ScenarioIndex, UseCase, id_key, text
from app.design.services.class_diagram.trusted_context import required_value_evidence
from app.design.services.class_diagram.type_system import (
    field_type,
    referenced_type_names,
    structure_type_contract,
)
from app.design.services.class_diagram.validation import OPERATION_CHECKS, OperationContext
from app.design.services.class_diagram.validation.model import (
    class_name,
)
from app.design.services.common import fields
from app.design.services.common.structured import parse_structured
from app.llm_connection import build_llm_connection
from app.llm_profiles import effective_temperature
from app.validation import Finding, RepairAttempt, RepairLedger, run_checks, stable_digest

logger = logging.getLogger(__name__)


class OperationValidationError(ValueError):
    """Accepted-fragment validation failure retaining structured findings."""

    def __init__(
        self,
        message: str,
        findings: tuple[Finding, ...] = (),
        errors: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.findings = findings
        self.errors = errors


_OPERATION_PROMPT = (
    """
Build the complete operation fragment for exactly one use case. Use only
fixedClasses, fixedDataTypes, reserved contracts, and locally declared DataTypes.
Cover every allowedStepRef. Return only the fields required by the response
schema; do not return bindings, relationships, operation IDs, or classes outside
the fixed inventory.

Add new operations to fixedClasses when the current use case needs them.
reservedOperations are signatures already accepted for other use cases, not an
exhaustive list of allowed operations. Reuse an exact reserved signature when it
has the required behavior; otherwise add a distinct operation for this use case.
Never edit an existing reserved signature.
Use lowerCamelCase for every new operation and parameter name (for example,
submitOrder and orderId); do not use snake_case.

Follow the standard BCE roles: Boundary receives actor-facing input, Control
coordinates the use-case flow, and Entity may own explicitly selected persistent
state behavior. An Entity listed for this use case is a candidate, not proof that
the scenario needs an Entity operation. Close
an ordinary request-response flow through return values: the root Boundary
operation may cover both the actor input and the resulting actor-visible output.
For one request-response actor entry, use exactly one direct orchestration Control
operation to cover its complete actor-visible behavior. When the response needs
multiple values, return one concrete DTO or valueObject containing them. Do not
split sequential retrieval into nested Control calls when a child needs a value
that its parent Control returns: that parent result is not available to its child.
Keep Control delegation when the child inputs are independently available from
entry inputs, preconditions, or earlier completed calls.
Add an Entity operation only when the supplied scenario explicitly requires a
durable read or state change and that behavior targets or sources state declared
by a supplied Entity. A Boundary-to-Control flow is valid when it completely
expresses the supplied behavior. Do not infer an Entity call from use-case
membership alone, and do not invent dummy or no-op operations merely to keep a
structural class in the diagram.
Do not add present/show/confirm/notify Boundary operations merely to deliver the
result of the current request. Add a separate outbound Boundary operation only
when the scenario explicitly requires an out-of-band push, callback, or later
notification. Choose concrete operations supported by the supplied steps. Every
parameter and return type must resolve to a fixed class/type, a primitive, or a
local DataType.
Use canonical type spellings and explicit generic syntax. When the fixed
inventory declares class Item, valid types include String, List<Item>, and
Optional<List<Item>>. Write invalid "listItem" or "optional list Item" as
List<Item> or Optional<List<Item>>, respectively.
Before returning, audit every named parameter and return type: reuse an exact
fixed or reserved type when it has the required shape, and otherwise declare a
concrete local DataType in this fragment; never leave a referenced name undeclared.
For a Boundary actor-facing parameter, never use the opaque exact type Object.
Declare a named local valueObject with fields grounded in the supplied scenario
instead, and preserve any requiredValueRef on that parameter exactly.

Keep signatures as a closed value flow. A delegated parameter must be available
from an entry input, an earlier operation result, an explicit precondition, or a
supported runtime value. Declare a result type when later work needs several
values produced earlier. Do not invent caller input merely to satisfy a signature.
Honor `useCase.specification.public_contract.required_values` according to each entry's `usage`.
When a required value declares non-empty `allowed_values`, preserve that finite catalog as
a local or reusable DataType with kind=enumeration and the exact values, and use that enum
as the compatible Control parameter type carrying its requiredValueRef. Do not widen it to
String or another primitive.
For `control` or `both`, cite its exact short `valueRef` from `requiredValueSources` in `requiredValueRef` on the compatible
Control parameter that receives the value, and bind that parameter in the call. A `result`-only
value is evidenced by a concrete non-void Control return; do not attach its ref to an input parameter
just because its name or type looks related. A `system_result` may also be cited on a downstream
parameter only when the flow shows an earlier operation producing that same value and the parameter
type is compatible. For a `control` or `both` value that enters at a Boundary, a Boundary annotation
alone is insufficient: also put the same exact ref on the compatible receiving Control parameter and
bind it from that Boundary input. Never change a parameter type merely to make a ref compatible.
Every declared `system_result` with usage `result` or `both` must be represented by a compatible
non-Optional field in the successful Control return DataType. A failed outcome does not make a
required success result optional.
These declarations are evidence, not runtime values: caller_input values must come from actor-facing steps, and
system_result inputs must come from prior call results. Only a catalog entry marked server_context
is directly available for a Boundary-to-Control handoff. Do not create a wrapper class or DTO solely to relay a catalog value; a
Session/AuthContext or other context type may be modeled when it has a concrete design
responsibility beyond relaying one catalog value, even if the requirements do not name it.
Do not expose server_context values as actor-facing Boundary inputs. Never infer a binding
from an actor name, prose, parameter name, or type.

When this use case reuses a reserved operation, include that operation in the
fragment with its exact supplied name, parameters, and returnType, plus this use
case's stepRefs. Referencing it only from calls does not assign those steps. The
same class must not overload one method name with a different signature.
Reuse fixed and reserved DataTypes by name. A new DataType must have resolved
fields or enum values and must be referenced by an operation signature. Use
kind=valueObject with non-empty fields and no values, or kind=enumeration with
non-empty values and no fields. When a valueObject represents more than one
valid outcome, declare every field that is absent in any outcome as Optional<T>;
never represent that absence by returning null for a non-Optional field.
Use Optional<T> only for a field that may genuinely be absent in a valid successful outcome;
failure outcomes do not make a required system_result optional.
""".strip()
    + "\n\n"
    + structure_type_contract()
)


def operation_prompt() -> str:
    """결합 생성기도 공유하는 operation 생성 규칙을 반환한다."""

    return _OPERATION_PROMPT


def operation_reasoning_effort() -> str:
    """한 유스케이스 연산에 적용할 reasoning 수준을 반환한다."""

    return str(
        getattr(
            settings,
            "design_class_operation_reasoning_effort",
            settings.design_reasoning_effort,
        )
    )


def operation_max_completion_tokens() -> int:
    """한 유스케이스 연산의 reasoning과 JSON을 합친 출력 상한을 반환한다."""

    return int(
        getattr(
            settings,
            "design_class_operation_max_completion_tokens",
            settings.design_class_collaboration_max_completion_tokens,
        )
    )


def _reserved_operations(model: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "className": class_name(item),
            "operations": list(item.get("operations") or []),
        }
        for item in model.get("Classes") or []
        if isinstance(item, dict) and item.get("operations")
    ]


def _required_value_handles(use_case: UseCase) -> tuple[dict[str, str], dict[str, str]]:
    """Build stable short-handle maps for this use case's accepted value catalog."""
    catalog = required_value_evidence(use_case)
    canonical = {text(item.get("valueRef")) for item in catalog}
    prefix = "RV"
    while any(f"{prefix}{index}" in canonical for index in range(1, len(catalog) + 1)):
        prefix += "_"
    handle_to_ref = {f"{prefix}{index}": text(item["valueRef"])
                     for index, item in enumerate(catalog, 1)}
    return handle_to_ref, {value_ref: handle for handle, value_ref in handle_to_ref.items()}


def _allowed_value_findings(
    fragment: dict[str, Any], inventory: dict[str, Any], use_case: UseCase,
) -> list[Finding]:
    """Require finite required-value catalogs to stay enum-typed on Control inputs."""
    contract = use_case.specification.get("public_contract")
    required_values = contract.get("required_values") if isinstance(contract, dict) else []
    catalogs = {
        text(item.get("value_ref")): item
        for item in required_values or []
        if isinstance(item, dict)
        and text(item.get("value_ref"))
        and isinstance(item.get("allowed_values"), list)
        and item.get("allowed_values")
    }
    if not catalogs:
        return []
    type_index = {
        text(item.get("name")): item
        for item in [*(inventory.get("DataTypes") or []), *(fragment.get("DataTypes") or [])]
        if isinstance(item, dict)
    }
    stereotypes = {
        class_name(item): text(item.get("stereotype"))
        for item in inventory.get("Classes") or [] if isinstance(item, dict)
    }
    findings: list[Finding] = []
    referenced: set[str] = set()
    for owner in fragment.get("Classes") or []:
        if not isinstance(owner, dict) or stereotypes.get(text(owner.get("className")), "").casefold() != "control":
            continue
        for operation in owner.get("operations") or []:
            if not isinstance(operation, dict):
                continue
            for parameter in operation.get("parameters") or []:
                if not isinstance(parameter, dict):
                    continue
                value_ref = text(parameter.get("requiredValueRef"))
                catalog = catalogs.get(value_ref)
                if catalog is None:
                    continue
                referenced.add(value_ref)
                allowed = catalog["allowed_values"]
                parameter_type = text(parameter.get("type"))
                declaration = type_index.get(parameter_type)
                if not (
                    isinstance(declaration, dict)
                    and text(declaration.get("kind")).casefold() == "enumeration"
                    and list(declaration.get("values") or []) == allowed
                ):
                    findings.append(Finding(
                        "class.operation.allowed-values",
                        f"requiredValueRef '{value_ref}' has a finite allowed_values catalog; "
                        "the Control parameter must use an enumeration DataType with the exact values.",
                        f"{use_case.id}:{owner.get('className')}.{operation.get('name')}#{parameter.get('name')}",
                    ))
    for value_ref, catalog in catalogs.items():
        if catalog.get("usage") in {"control", "both"} and value_ref not in referenced:
            findings.append(Finding(
                "class.operation.allowed-values",
                f"required value '{value_ref}' has a finite allowed_values catalog and must be "
                "preserved by a Control parameter with the same requiredValueRef.",
                use_case.id,
            ))
    return findings


def _expand_required_value_handles(candidate: dict[str, Any], use_case: UseCase) -> dict[str, Any]:
    """Expand exact known handles; leave unknown values untouched for rejection."""
    handles, _ = _required_value_handles(use_case)
    normalized = deepcopy(candidate)
    for class_set in normalized.get("Classes") or []:
        if not isinstance(class_set, dict):
            continue
        for operation in class_set.get("operations") or []:
            if not isinstance(operation, dict):
                continue
            for parameter in operation.get("parameters") or []:
                if isinstance(parameter, dict):
                    handle = text(parameter.get("requiredValueRef"))
                    if handle in handles:
                        parameter["requiredValueRef"] = handles[handle]
    return normalized


def _operation_payload(
    index: ScenarioIndex,
    inventory: dict[str, Any],
    use_case: UseCase,
    *,
    previous: dict[str, Any] | None = None,
    findings: list[str] | None = None,
    reserved: list[dict[str, Any]] | None = None,
    reserved_types: list[dict[str, Any]] | None = None,
    allowed_step_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """한 실행 슬라이스의 유한한 연산 선택 공간을 JSON payload로 만든다.

    ``fixedClasses``와 ``fixedDataTypes``는 inventory가 수락한 후보만 담고,
    ``allowedStepRefs``는 현재 unit이 소유할 수 있는 단계만 담는다. ``previousFragment``와
    ``findings``는 최초 실패 뒤 full replacement를 요청할 때만 추가한다.
    """
    source_summary = next(
        (
            item
            for item in index.raw.get("use_cases") or []
            if isinstance(item, dict) and text(item.get("id")) == use_case.id
        ),
        {},
    )
    # 원문 시나리오 본문은 executionSlice.steps로만 전달한다. 하지만 goal, actor,
    # pre/postcondition, business rule 같은 use-case 문맥은 operation 설계에 필요하므로
    # specification 전체를 버리지 않고 main/extension flow만 제거한다.
    specification = use_case.specification
    _, ref_to_handle = _required_value_handles(use_case)
    if not settings.design_class_compact_operation_payload:
        summary = deepcopy(source_summary)
        if isinstance(specification, dict):
            summary["specification"] = deepcopy(specification)
    else:
        summary = {
            key: deepcopy(value)
            for key, value in source_summary.items()
            if key not in {"main_scenario", "extensions"}
        }
    if settings.design_class_compact_operation_payload and isinstance(specification, dict):
        compact_specification = {
            key: deepcopy(value)
            for key, value in specification.items()
            if key not in {"main_scenario", "extensions"}
        }
        # raw use-case의 goal·actor 문맥은 top-level에 유지한다. specification 안에만 있던
        # pre/postcondition과 business rule은 아래 compact specification에 한 번만 남긴다.
        summary["specification"] = compact_specification
    summary_spec = summary.get("specification")
    if isinstance(summary_spec, dict):
        contract = summary_spec.get("public_contract")
        values = contract.get("required_values") if isinstance(contract, dict) else None
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict) and text(item.get("value_ref")) in ref_to_handle:
                    item["value_ref"] = ref_to_handle[text(item["value_ref"])]
    scoped_classes = []
    for item in inventory.get("Classes") or []:
        if use_case.id not in set(item.get("useCaseIds") or []):
            continue
        role = text(item.get("stereotype"))
        compact: dict[str, Any] = {
            "className": class_name(item),
            "stereotype": role,
            "description": text(item.get("description")),
        }
        # Boundary와 Control은 상태를 소유하지 않는다. Entity를 고를 때만 실제 상태와
        # 식별자가 필요하므로 다른 역할에 빈 배열까지 반복 전송하지 않는다.
        if role == "Entity":
            compact["fields"] = list(item.get("fields") or [])
            compact["identifier"] = list(item.get("identifier") or [])
        scoped_classes.append(compact)
    scoped_types = [
        structured_data_type({key: value for key, value in item.items() if key not in {"useCaseIds", "identifier"}})
        for item in inventory.get("DataTypes") or []
        if use_case.id in set(item.get("useCaseIds") or [])
    ]
    scoped_names = {class_name(item) for item in scoped_classes}
    scoped_reserved = [
        {
            "className": text(item.get("className")),
            "operations": [
                {
                    "name": text(operation.get("name")),
                    "parameters": list(operation.get("parameters") or []),
                    "returnType": text(operation.get("returnType")),
                }
                for operation in item.get("operations") or []
                if isinstance(operation, dict)
            ],
        }
        for item in (reserved or [])
        if text(item.get("className")) in scoped_names
    ]
    allowed = set(allowed_step_ids) or {step.id for step in use_case.steps}
    contract = use_case.specification.get("public_contract")
    raw_required_values = contract.get("required_values") if isinstance(contract, dict) else []
    allowed_values = {
        text(item.get("value_ref")): list(item["allowed_values"])
        for item in raw_required_values or []
        if isinstance(item, dict)
        and text(item.get("value_ref"))
        and isinstance(item.get("allowed_values"), list)
        and item.get("allowed_values")
    }
    payload: dict[str, Any] = {
        "useCase": summary,
        "executionSlice": {
            "id": use_case.id,
            "steps": [
                {
                    "stepRef": step.id,
                    "subject": step.subject,
                    "sentence": step.sentence,
                    "condition": step.condition,
                }
                for step in use_case.steps
                if step.id in allowed
            ],
        },
        "allowedStepRefs": sorted(allowed, key=id_key),
        "fixedClasses": scoped_classes,
        "fixedDataTypes": scoped_types,
        "reservedOperations": scoped_reserved,
        "reservedDataTypes": [structured_data_type(item) for item in (reserved_types or [])],
        "requiredValueSources": [
            {
                **item,
                **({"allowedValues": allowed_values[item["valueRef"]]}
                   if item["valueRef"] in allowed_values else {}),
                "valueRef": ref_to_handle[item["valueRef"]],
                "sourceRef": f"value#{ref_to_handle[item['valueRef']]}"}
            for item in required_value_evidence(use_case)
        ],
        "valueSourcePolicy": {
            "requestInputs": "actor-facing Boundary parameters declared by the scenario",
            "requiredValues": "requiredValueRef cites a declaration only; actual binding must use an eligible finite actor input, prior result, server_context catalog entry, runtime, or derived value",
            "previousResults": "an earlier collaboration operation result",
            "runtimeValues": "supported clock values only",
            "derivedValues": "a DataType whose required fields have eligible sources",
        },
    }
    if previous is not None:
        payload["previousFragment"] = previous
    if findings:
        payload["task"] = (
            "Return a full replacement for this use-case fragment only. "
            "Preserve valid operations and resolve every finding."
        )
        payload["findings"] = findings
    return payload


def _reuse_reserved_operation_signatures(
    candidate: dict[str, Any],
    reserved: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """이미 승인된 메서드 이름을 고르면 그 서명을 그대로 재사용한다.

    한 클래스에서 메서드 이름은 유일하므로 LLM이 같은 parameter와 return type을 다시
    작성하게 할 필요가 없다. 현재 유스케이스의 ``stepRefs``만 후보가 소유하고, 기존
    서명은 코드가 복사한다. 이후 값 전달 검사는 이 서명을 실제로 호출할 수 있는지
    별도로 확인한다.
    """

    signatures = {
        (text(owner.get("className")), text(operation.get("name"))): operation
        for owner in reserved or []
        if isinstance(owner, dict)
        for operation in owner.get("operations") or []
        if isinstance(operation, dict)
    }
    if not signatures:
        return candidate
    normalized = deepcopy(candidate)
    for owner in normalized.get("Classes") or []:
        if not isinstance(owner, dict):
            continue
        class_name_value = text(owner.get("className"))
        for operation in owner.get("operations") or []:
            if not isinstance(operation, dict):
                continue
            accepted = signatures.get((class_name_value, text(operation.get("name"))))
            if accepted is None:
                continue
            operation["parameters"] = deepcopy(accepted.get("parameters") or [])
            operation["returnType"] = text(accepted.get("returnType"))
    return normalized


def _canonicalize_step_ownership(
    candidate: dict[str, Any],
    inventory: dict[str, Any],
    actor_entry_refs: set[str],
    shared_actor_entry_refs: set[str],
    allowed_step_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """actor 입력 단계의 일반적인 Boundary 소유 규칙을 투영한다.

    실행 묶음이 actor 입력 한 단계뿐이면 그 문장에 조회나 저장 같은 시스템 처리까지
    함께 적힌 것이다. 이때는 Control이 같은 단계 근거를 공유해야 Boundary가 실제로
    위임할 수 있다. 뒤에 별도 시스템 단계가 있는 묶음에서만 actor 입력을 Boundary가
    단독으로 소유한다.
    """

    normalized = deepcopy(candidate)
    allowed_refs = set(allowed_step_ids)
    allowed_use_cases = {ref.split(":", 1)[0] for ref in allowed_refs}
    stereotypes = {
        class_name(item): text(item.get("stereotype")).casefold()
        for item in inventory.get("Classes") or []
        if isinstance(item, dict)
    }
    class_sets: list[dict[str, Any]] = []
    for class_set in normalized.get("Classes") or []:
        if not isinstance(class_set, dict):
            continue
        owner = text(class_set.get("className"))
        operations: list[dict[str, Any]] = []
        for operation in class_set.get("operations") or []:
            if not isinstance(operation, dict):
                continue
            owned = deepcopy(operation)
            if allowed_refs:
                owned["stepRefs"] = [
                    ref
                    for ref in owned.get("stepRefs") or []
                    if text(ref) in allowed_refs
                    or text(ref).split(":", 1)[0] not in allowed_use_cases
                ]
            if stereotypes.get(owner) != "boundary":
                owned["stepRefs"] = [
                    ref
                    for ref in owned.get("stepRefs") or []
                    if (text(ref) not in actor_entry_refs or text(ref) in shared_actor_entry_refs)
                ]
            if owned.get("stepRefs"):
                operations.append(owned)
        if operations:
            class_sets.append({**class_set, "operations": operations})
    normalized["Classes"] = class_sets
    return normalized


def _fold_same_boundary_responses(
    candidate: dict[str, Any],
    inventory: dict[str, Any],
    index: ScenarioIndex,
    use_case: UseCase,
    operation_refs: set[str],
) -> dict[str, Any]:
    """같은 요청의 응답 단계를 최초 Boundary operation으로 합친다.

    한 유스케이스 안에서 최초 Boundary operation을 다시 호출해 화면 표시나 확인을
    전달하면 ``Control -> Boundary`` 왕복이 생긴다. 동기 요청의 결과는 최초 호출의
    반환으로 전달되므로, 결합 제안의 call forest에서 실제로 같은 Boundary로 되돌아간
    operation만 최초 operation의 ``stepRefs``로 옮긴다. 다른 Boundary 클래스는 외부
    시스템 호출일 수 있으므로 건드리지 않는다.

    이 변환은 메서드 이름이나 업무 분야를 보지 않는다. ``ScenarioIndex``가 계산한
    actor entry 범위와 BCE 클래스 역할만 사용하므로 특정 골드셋 표현에 의존하지 않는다.
    """

    normalized = deepcopy(candidate)
    stereotypes = {
        class_name(item): text(item.get("stereotype")).casefold()
        for item in inventory.get("Classes") or []
        if isinstance(item, dict)
    }
    class_sets = {
        text(item.get("className")): item
        for item in normalized.get("Classes") or []
        if isinstance(item, dict)
    }
    groups = tuple(
        group for group in index.groups if group.use_case_id == use_case.id and group.actor_step
    )
    actor_entries = {group.actor_step for group in groups}

    for group in groups:
        root: tuple[str, dict[str, Any]] | None = None
        for owner, class_set in class_sets.items():
            if stereotypes.get(owner) != "boundary":
                continue
            for operation in class_set.get("operations") or []:
                if isinstance(operation, dict) and group.actor_step in {
                    text(ref) for ref in operation.get("stepRefs") or []
                }:
                    root = owner, operation
                    break
            if root is not None:
                break
        # operation 검증기가 누락된 actor entry를 정확히 보고하도록, 여기서 임의의
        # Boundary operation을 root로 추측하지 않는다.
        if root is None:
            continue

        owner, root_operation = root
        required = set(group.required_step_ids)
        class_set = class_sets[owner]
        for operation in class_set.get("operations") or []:
            if not isinstance(operation, dict) or operation is root_operation:
                continue
            operation_ref = f"{owner}.{text(operation.get('name'))}"
            if operation_ref not in operation_refs:
                continue
            refs = [text(ref) for ref in operation.get("stepRefs") or []]
            response_refs = [ref for ref in refs if ref in required and ref not in actor_entries]
            if not response_refs:
                continue
            root_operation["stepRefs"] = list(
                dict.fromkeys(
                    [
                        *(text(ref) for ref in root_operation.get("stepRefs") or []),
                        *response_refs,
                    ]
                )
            )
            operation["stepRefs"] = [ref for ref in refs if ref not in response_refs]

    normalized["Classes"] = [
        {
            **class_set,
            "operations": [
                operation
                for operation in class_set.get("operations") or []
                if isinstance(operation, dict) and operation.get("stepRefs")
            ],
        }
        for class_set in normalized.get("Classes") or []
        if isinstance(class_set, dict)
        and any(
            isinstance(operation, dict) and operation.get("stepRefs")
            for operation in class_set.get("operations") or []
        )
    ]
    return normalized


def operation_payload(
    index: ScenarioIndex,
    inventory: AcceptedInventory,
    use_case: UseCase,
    **kwargs: Any,
) -> dict[str, Any]:
    """한 실행 슬라이스의 결합 LLM 입력을 typed 경계에서 만든다."""

    return _operation_payload(index, inventory.as_payload(), use_case, **kwargs)


def normalize_operation_fragment(
    proposal: OperationFragment | Mapping[str, Any],
    index: ScenarioIndex,
    inventory: AcceptedInventory,
    use_case: UseCase,
    *,
    reserved: list[dict[str, Any]] | None = None,
    reserved_types: list[dict[str, Any]] | None = None,
    allowed_step_ids: tuple[str, ...] = (),
    same_boundary_response_operations: set[str] | None = None,
) -> AcceptedFragment:
    """raw operation 제안을 기존 저장 표기로 정규화한다."""

    inventory_payload = inventory.as_payload()
    proposal_payload = (
        deepcopy(dict(proposal))
        if isinstance(proposal, Mapping)
        else proposal.model_dump(by_alias=True)
    )
    proposal_payload["DataTypes"] = [
        structured_data_type(item) for item in proposal_payload.get("DataTypes") or []
    ]
    candidate = OperationFragment.model_validate(proposal_payload).model_dump(by_alias=True)
    candidate = _expand_required_value_handles(candidate, use_case)
    fixed_names = (
        {
            class_name(item)
            for item in inventory_payload.get("Classes") or []
            if isinstance(item, dict)
        }
        | {
            text(item.get("name"))
            for item in inventory_payload.get("DataTypes") or []
            if isinstance(item, dict)
        }
        | {text(item.get("name")) for item in reserved_types or [] if isinstance(item, dict)}
    )
    candidate["DataTypes"] = [
        {
            **item,
            "fields": [
                structured_data_type({
                    "fields": [fields.normalize_java_field_candidate(
                        f"{field['name']} : {field['type']}"
                    )],
                })["fields"][0]
                for field in item.get("fields") or []
            ],
        }
        for item in candidate.get("DataTypes") or []
        if text(item.get("name")) not in fixed_names
    ]
    candidate = _reuse_reserved_operation_signatures(candidate, reserved)
    actor_entry_refs = {
        group.actor_step
        for group in index.groups
        if group.use_case_id == use_case.id and group.actor_step
    }
    shared_actor_entry_refs = {
        group.actor_step
        for group in index.groups
        if group.use_case_id == use_case.id
        and group.actor_step
        and len(group.required_step_ids) == 1
    }
    candidate = _canonicalize_step_ownership(
        candidate,
        inventory_payload,
        actor_entry_refs,
        shared_actor_entry_refs,
        allowed_step_ids,
    )
    if same_boundary_response_operations:
        candidate = _fold_same_boundary_responses(
            candidate,
            inventory_payload,
            index,
            use_case,
            same_boundary_response_operations,
        )
    return AcceptedFragment(use_case.id, candidate)


def _propose_fragment(
    index: ScenarioIndex,
    inventory: dict[str, Any],
    use_case: UseCase,
    *,
    previous: dict[str, Any] | None = None,
    findings: list[str] | None = None,
    reserved: list[dict[str, Any]] | None = None,
    reserved_types: list[dict[str, Any]] | None = None,
    allowed_step_ids: tuple[str, ...] = (),
    operation: str = "InteractionOperations",
) -> dict[str, Any]:
    """한 연산 unit을 LLM에 요청하고 저장 직전 fragment shape로 정규화한다.

    응답은 ``OperationFragment``로 제한된다. 이 함수는 규칙 finding을 판단하지 않으며,
    deterministic normalization까지만 소유한다. 검사와 이력 기반 replacement는
    ``_checked_fragment``가 담당한다.
    """
    # 1. 허용 step, 고정 타입과 예약 이름을 먼저 payload로 좁힌다. LLM이 전체 모델을
    # 보지 않으므로 다른 use case의 성공한 연산을 임의로 다시 작성할 수 없다.
    prompt_payload = _operation_payload(
        index,
        inventory,
        use_case,
        previous=previous,
        findings=findings,
        reserved=reserved,
        reserved_types=reserved_types,
        allowed_step_ids=allowed_step_ids,
    )
    parsed = parse_structured(
        [
            {"role": "system", "content": _OPERATION_PROMPT},
            {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
        ],
        OperationFragment,
        reasoning_effort=operation_reasoning_effort(),
        max_completion_tokens=operation_max_completion_tokens(),
        operation=operation,
        metadata={
            "useCaseId": use_case.id,
            "executionSlice": use_case.id,
            "candidateCount": len(prompt_payload["fixedClasses"])
            + len(prompt_payload["executionSlice"]["steps"]),
        },
    )
    # 2. 설명문이나 임의 필드를 거부하고 일시적 proposal schema만 수락한다.
    candidate = OperationFragment.model_validate(parsed).model_dump(by_alias=True)
    candidate = _expand_required_value_handles(candidate, use_case)
    fixed_names = (
        {class_name(item) for item in inventory.get("Classes") or [] if isinstance(item, dict)}
        | {
            text(item.get("name"))
            for item in inventory.get("DataTypes") or []
            if isinstance(item, dict)
        }
        | {text(item.get("name")) for item in reserved_types or [] if isinstance(item, dict)}
    )
    # 3. 전역·앞서 수락된 타입은 지역 선언에서 제거한다. 같은 이름의 다른
    # 정의는 compose 시 충돌로 드러나며 조용히 덮어쓰지 않는다.
    candidate["DataTypes"] = [
        {
            **item,
            "fields": [
                fields.normalize_java_field_candidate(
                    f"{field['name']} : {field['type']}"
                )
                for field in item.get("fields") or []
            ],
        }
        for item in candidate.get("DataTypes") or []
        if text(item.get("name")) not in fixed_names
    ]
    # 4. Reserved signatures are reused exactly; authored DTO parameter types
    # remain intact for the validator to accept or diagnose.
    candidate = _reuse_reserved_operation_signatures(candidate, reserved)
    actor_entry_refs = {
        group.actor_step
        for group in index.groups
        if group.use_case_id == use_case.id and group.actor_step
    }
    shared_actor_entry_refs = {
        group.actor_step
        for group in index.groups
        if group.use_case_id == use_case.id
        and group.actor_step
        and len(group.required_step_ids) == 1
    }
    # 5. actor entry는 Boundary만 소유한다. Control/Entity의 중복 stepRef를 제거한 뒤
    # 근거가 사라진 placeholder operation도 함께 제외한다.
    return _canonicalize_step_ownership(
        candidate,
        inventory,
        actor_entry_refs,
        shared_actor_entry_refs,
        allowed_step_ids,
    )


def _checked_fragment_uncached(
    index: ScenarioIndex,
    inventory: dict[str, Any],
    use_case: UseCase,
    *,
    previous: dict[str, Any] | None = None,
    findings: list[str] | None = None,
    reserved: list[dict[str, Any]] | None = None,
    reserved_types: list[dict[str, Any]] | None = None,
    allowed_step_ids: tuple[str, ...] = (),
    operation: str = "InteractionOperations",
) -> dict[str, Any]:
    """연산 fragment를 제안·검사하고 현재 unit만 이력 기반으로 교체한다.

    각 보고서의 finding과 거절 후보 이력을 ``previousFragment``와 함께 다음 repair 호출에
    전달한다. 숫자 상한은 없으며 같은 후보가 반복되면 그 사실까지 알려 다른 전체 조각을
    계속 요청한다.
    """
    validation_inventory = {
        **inventory,
        "DataTypes": [
            *(inventory.get("DataTypes") or []),
            *(
                item
                for item in (reserved_types or [])
                if isinstance(item, dict)
                and text(item.get("name"))
                not in {
                    text(existing.get("name"))
                    for existing in inventory.get("DataTypes") or []
                    if isinstance(existing, dict)
                }
            ),
        ],
    }
    # 검증 context는 앞서 예약된 타입까지 포함한다. 그렇지 않으면 유효한 재사용
    # 타입을 "존재하지 않음"으로 오판한다.
    context = OperationContext(
        index,
        validation_inventory,
        use_case,
        allowed_step_ids,
    )
    ledger = RepairLedger()
    input_digest = stable_digest(
        {
            "useCaseId": use_case.id,
            "inventory": inventory,
            "reserved": reserved or [],
            "reservedTypes": reserved_types or [],
            "allowedStepIds": allowed_step_ids,
            "initialFindings": findings or [],
        }
    )
    candidate = previous
    repair_findings = list(findings or [])
    attempt = 0
    while True:
        candidate = _propose_fragment(
            index,
            inventory,
            use_case,
            previous=candidate,
            findings=repair_findings or None,
            reserved=reserved,
            reserved_types=reserved_types,
            allowed_step_ids=allowed_step_ids,
            operation=(
                operation
                if attempt == 0
                else "InteractionOperationsRepair"
                if operation == "InteractionOperations"
                else f"{operation}Repair"
            ),
        )
        # Keep the generic unresolved-type repair in front of both targeted and
        # combined-unit validation. The local import avoids a module import cycle:
        # generation owns the shared type-only repair prompt and schema.
        from app.design.services.class_diagram.generation import (
            _repair_invalid_type_fields,
        )

        repaired = _repair_invalid_type_fields(
            {"fragment": candidate},
            AcceptedInventory.from_payload(inventory),
            reserved_types or [],
            use_case_id=use_case.id,
        )
        candidate = repaired.get("fragment", candidate)
        report = run_checks(OPERATION_CHECKS, candidate, context)
        allowed_value_findings = _allowed_value_findings(candidate, validation_inventory, use_case)
        if report.errors:
            raise RuntimeError("; ".join(report.errors))
        if not report.findings and not allowed_value_findings:
            return candidate

        current_findings = tuple(sorted(set([
            *finding_text(report.findings), *finding_text(allowed_value_findings),
        ])))
        candidate_digest = stable_digest(candidate)
        repeated = ledger.candidate_seen(
            input_digest=input_digest,
            candidate_digest=candidate_digest,
        ) or ledger.failure_seen(
            input_digest=input_digest,
            finding_keys=current_findings,
        )
        ledger.record(
            RepairAttempt(
                stage="design.class.operations",
                target_ids=(use_case.id,),
                strategy_key=f"full-fragment-replacement-{attempt + 1}",
                input_digest=input_digest,
                candidate_digest=candidate_digest,
                finding_keys_before=current_findings,
                finding_keys_after=current_findings,
                outcome="repeated_candidate" if repeated else "no_improvement",
                detail="; ".join(current_findings),
            )
        )
        repair_findings = [
            *current_findings,
            *(
                [
                    "The previous candidate repeated the same rejected state. "
                    "Return a materially different complete fragment."
                ]
                if repeated
                else []
            ),
            (
                "Accumulated repair history (do not repeat any strategy or candidate):\n"
                + ledger.prompt_context()
            ),
        ]
        attempt += 1


def _operation_cache_key(
    index: ScenarioIndex,
    inventory: dict[str, Any],
    use_case: UseCase,
    *,
    previous: dict[str, Any] | None,
    findings: list[str] | None,
    reserved: list[dict[str, Any]] | None,
    reserved_types: list[dict[str, Any]] | None,
    allowed_step_ids: tuple[str, ...],
    operation: str,
) -> str:
    """한 유스케이스의 수락 fragment에 대응하는 cache key를 만든다."""

    payload = _operation_payload(
        index,
        inventory,
        use_case,
        previous=previous,
        findings=findings,
        reserved=reserved,
        reserved_types=reserved_types,
        allowed_step_ids=allowed_step_ids,
    )
    if isinstance(payload.get("findings"), list):
        payload["findings"] = [" ".join(str(item).split()) for item in payload["findings"]]
    return accepted_unit_key(
        "operation-fragment",
        unit_slice=payload,
        inventory=inventory,
        feedback={
            "findings": [" ".join(str(item).split()) for item in findings or []],
            "operation": operation,
        },
        prompt=_OPERATION_PROMPT,
        schema=OperationFragment,
        provider=configured_provider_identity(build_llm_connection().base_url),
        model=settings.model,
        seed=settings.seed,
        temperature=effective_temperature(settings.model, settings.temperature),
        reasoning_effort=operation_reasoning_effort(),
        max_completion_tokens=operation_max_completion_tokens(),
    )


def _validate_accepted_fragment(
    candidate: dict[str, Any],
    index: ScenarioIndex,
    inventory: dict[str, Any],
    use_case: UseCase,
    *,
    reserved_types: list[dict[str, Any]] | None,
    allowed_step_ids: tuple[str, ...],
) -> dict[str, Any]:
    """cache hit을 Pydantic과 기존 operation validator로 다시 수락한다."""

    # Keep the accepted proposal in the structured contract. Legacy cached
    # values are adapted at the persistence/validation boundary below.
    # Persisted class-model fragments contain generated identities such as
    # stableId/operationId.  They are not proposal fields, so project the
    # stored value explicitly instead of weakening the strict proposal schema.
    def proposal_data_type(item: dict[str, Any]) -> dict[str, Any]:
        normalized_type = structured_data_type(item)
        return {
            key: normalized_type.get(key)
            for key in ("name", "kind", "fields", "values")
            if key in normalized_type
        }

    proposal_payload = {
        "DataTypes": [
            proposal_data_type(item)
            for item in candidate.get("DataTypes") or []
            if isinstance(item, dict)
        ],
        "Classes": [
            {
                "className": owner.get("className"),
                "operations": [
                    {
                        "name": operation.get("name"),
                        "parameters": [
                            {
                                "name": parameter.get("name"),
                                "type": parameter.get("type"),
                                "requiredValueRef": parameter.get("requiredValueRef"),
                            }
                            for parameter in operation.get("parameters") or []
                            if isinstance(parameter, dict)
                        ],
                        "returnType": operation.get("returnType"),
                        "stepRefs": list(operation.get("stepRefs") or []),
                    }
                    for operation in owner.get("operations") or []
                    if isinstance(operation, dict)
                ],
            }
            for owner in candidate.get("Classes") or []
            if isinstance(owner, dict)
        ],
    }
    normalized = OperationFragment.model_validate(proposal_payload).model_dump(by_alias=True)
    # 실제 cache value는 이미 ``name : Type`` 수락 표기다. test double이나 호환 caller가
    # parse 직후의 field mapping을 넣어도 같은 수락 표기로만 바꾼 뒤 영속 BCE schema를
    # 검증한다. raw ``OperationFragment`` schema로 normalized value를 다시 읽지는 않는다.
    validation_inventory = {
        **inventory,
        "DataTypes": [
            *(inventory.get("DataTypes") or []),
            *(
                legacy_data_type(item)
                for item in (reserved_types or [])
                if isinstance(item, dict)
                and text(item.get("name"))
                not in {
                    text(existing.get("name"))
                    for existing in inventory.get("DataTypes") or []
                    if isinstance(existing, dict)
                }
            ),
        ],
    }
    try:
        _compose(validation_inventory, [(use_case.id, normalized)])
    except Exception as error:
        raise ValueError(
            f"cached operation fragment {use_case.id} is not a valid accepted BCE fragment: {error}"
        ) from error
    report = run_checks(
        OPERATION_CHECKS,
        {
            **normalized,
            "DataTypes": [legacy_data_type(item) for item in normalized.get("DataTypes") or []],
        },
        OperationContext(
            index,
            validation_inventory,
            use_case,
            allowed_step_ids,
        ),
    )
    allowed_value_findings = _allowed_value_findings(normalized, validation_inventory, use_case)
    if report.errors or report.findings or allowed_value_findings:
        raise OperationValidationError(
            f"cached operation fragment {use_case.id} is invalid: "
            + "; ".join([
                *report.errors, *finding_text(report.findings),
                *finding_text(allowed_value_findings),
            ]),
            tuple([*report.findings, *allowed_value_findings]),
            tuple(report.errors),
        )
    return normalized


def validate_operation_fragment(
    fragment: AcceptedFragment,
    index: ScenarioIndex,
    inventory: AcceptedInventory,
    use_case: UseCase,
    *,
    reserved_types: list[dict[str, Any]] | None = None,
    allowed_step_ids: tuple[str, ...] = (),
) -> AcceptedFragment:
    """정규화한 fragment에 최소 operation 검사를 다시 실행한다."""

    payload = _validate_accepted_fragment(
        fragment.as_payload(),
        index,
        inventory.as_payload(),
        use_case,
        reserved_types=reserved_types,
        allowed_step_ids=allowed_step_ids,
    )
    return AcceptedFragment(use_case.id, payload)


def _checked_fragment(
    index: ScenarioIndex,
    inventory: dict[str, Any],
    use_case: UseCase,
    *,
    previous: dict[str, Any] | None = None,
    findings: list[str] | None = None,
    reserved: list[dict[str, Any]] | None = None,
    reserved_types: list[dict[str, Any]] | None = None,
    allowed_step_ids: tuple[str, ...] = (),
    operation: str = "InteractionOperations",
    cache: AcceptedUnitCache | None = None,
) -> dict[str, Any]:
    """수락된 fragment만 저장하고 cache hit도 동일한 검사를 다시 수행한다."""

    def compute() -> dict[str, Any]:
        return _checked_fragment_uncached(
            index,
            inventory,
            use_case,
            previous=previous,
            findings=findings,
            reserved=reserved,
            reserved_types=reserved_types,
            allowed_step_ids=allowed_step_ids,
            operation=operation,
        )

    prompt_payload = _operation_payload(
        index,
        inventory,
        use_case,
        previous=previous,
        findings=findings,
        reserved=reserved,
        reserved_types=reserved_types,
        allowed_step_ids=allowed_step_ids,
    )
    metadata = {
        "executionSlice": use_case.id,
        "candidateCount": len(prompt_payload["fixedClasses"])
        + len(prompt_payload["fixedDataTypes"]),
    }
    if cache is None:
        record_cache_outcome(
            None,
            operation=operation,
            unit=use_case.id,
            metadata=metadata,
        )
        candidate = compute()
    else:
        result = cache.get_or_compute(
            _operation_cache_key(
                index,
                inventory,
                use_case,
                previous=previous,
                findings=findings,
                reserved=reserved,
                reserved_types=reserved_types,
                allowed_step_ids=allowed_step_ids,
                operation=operation,
            ),
            compute,
        )
        record_cache_outcome(
            result,
            operation=operation,
            unit=use_case.id,
            metadata=metadata,
        )
        candidate = result.value
    return _validate_accepted_fragment(
        candidate,
        index,
        inventory,
        use_case,
        reserved_types=reserved_types,
        allowed_step_ids=allowed_step_ids,
    )


def _operation_signature(operation: dict[str, Any]) -> tuple[Any, ...]:
    return (
        tuple(
            (text(parameter.get("name")), _canonical_signature_type(parameter.get("type")))
            for parameter in operation.get("parameters") or []
            if isinstance(parameter, dict)
        ),
        _canonical_signature_type(operation.get("returnType")),
    )


def _canonical_signature_type(value: object) -> str:
    """Use the shared type vocabulary when comparing reserved contracts."""

    raw = text(value)
    try:
        return canonical_design_type(raw)
    except DesignTypeError:
        return raw


def _data_type_signature(item: dict[str, Any]) -> tuple[Any, ...]:
    fields_signature = tuple(
        (text(field.get("name")), text(field.get("type")))
        if isinstance(field, dict)
        else tuple(text(part.strip()) for part in str(field).split(":", 1))
        for field in item.get("fields") or []
    )
    return (
        text(item.get("kind")),
        fields_signature,
        tuple(item.get("values") or []),
    )


def _compose(
    inventory: dict[str, Any],
    fragments: list[tuple[str, dict[str, Any]]],
    *,
    final: bool = False,
) -> dict[str, Any]:
    """Inventory와 수락 fragment를 canonical ``BCEModel`` skeleton으로 합친다.

    동일 이름·동일 시그니처는 step provenance를 합치고, 동일 이름·다른 정의는
    ``Collision``로 현재 fragment repair를 요구한다. ``final=True``이면 operation에서
    도달할 수 없는 클래스·관계·지역 타입을 제거한다.
    """
    # BCEModel is the persistence/display boundary and still stores field
    # declarations as strings.  Keep proposal payloads structured until this
    # point, then adapt once for composition.
    inventory = dict(inventory)
    inventory["DataTypes"] = [
        legacy_data_type(item) for item in inventory.get("DataTypes") or []
    ]
    fragments = [
        (unit_id, {**fragment, "DataTypes": [
            legacy_data_type(item) for item in fragment.get("DataTypes") or []
        ]})
        for unit_id, fragment in fragments
    ]
    classes = {
        class_name(item): {
            **{
                key: deepcopy(value)
                for key, value in item.items()
                if key not in {"useCaseIds", "values"}
            },
            "use_case_ids": [],
            "operations": [],
        }
        for item in inventory.get("Classes") or []
        if isinstance(item, dict)
    }
    data_type_index = {
        text(item.get("name")): {
            key: deepcopy(value)
            for key, value in item.items()
            if key not in {"useCaseIds", "identifier"}
        }
        for item in inventory.get("DataTypes") or []
        if isinstance(item, dict)
    }
    # 입력 순서가 곧 충돌 소유권이다. 앞서 수락된 fragment는 고정하고 현재 fragment가
    # 충돌을 해결해야 병렬 완료 순서와 무관한 모델을 얻는다.
    for use_case_id, fragment in fragments:
        for proposed_type in fragment.get("DataTypes") or []:
            if not isinstance(proposed_type, dict):
                continue
            name = text(proposed_type.get("name"))
            existing_type = data_type_index.get(name)
            if existing_type is not None:
                if _data_type_signature(existing_type) != _data_type_signature(proposed_type):
                    raise _DataTypeCollision(name)
                continue
            data_type_index[name] = deepcopy(proposed_type)
        for class_set in fragment.get("Classes") or []:
            if not isinstance(class_set, dict):
                continue
            owner = text(class_set.get("className"))
            if owner not in classes:
                raise ValueError(f"fragment selected class outside inventory: {owner}")
            target = classes[owner]
            for proposed in class_set.get("operations") or []:
                if not isinstance(proposed, dict):
                    continue
                existing = next(
                    (
                        item
                        for item in target["operations"]
                        if text(item.get("name")) == text(proposed.get("name"))
                    ),
                    None,
                )
                if existing is not None:
                    if _operation_signature(existing) != _operation_signature(proposed):
                        raise _Collision(owner, text(proposed.get("name")))
                    # Preserve the exact accepted declaration link while merging step refs.
                    if any(
                        text(parameter.get("requiredValueRef"))
                        for parameter in proposed.get("parameters") or []
                        if isinstance(parameter, dict)
                    ):
                        existing["parameters"] = deepcopy(proposed.get("parameters") or [])
                    existing["stepRefs"] = list(
                        dict.fromkeys(
                            [
                                *(existing.get("stepRefs") or []),
                                *(proposed.get("stepRefs") or []),
                            ]
                        )
                    )
                    continue
                parameters = list(proposed.get("parameters") or [])
                target["operations"].append(
                    {
                        "operationId": canonical_operation_id(
                            owner,
                            text(proposed.get("name")),
                            parameters,
                        ),
                        **deepcopy(proposed),
                    }
                )
            if class_set.get("operations") and use_case_id not in target["use_case_ids"]:
                target["use_case_ids"].append(use_case_id)
    result_classes = list(classes.values())
    relationships = deepcopy(inventory.get("Relationships") or [])
    data_types = [data_type_index[name] for name in sorted(data_type_index)]
    if final:
        # 화면을 단순화하기 위한 임의 삭제가 아니다. 실제 operation 계약에서 도달할 수
        # 없는 구조만 제거해 API/sequence 소비자가 쓸 수 없는 타입을 저장하지 않는다.
        retained = {class_name(item) for item in result_classes if item.get("operations")}
        # operation이 없는 구조 클래스라도 수락된 class의 field나 signature가 참조하면
        # 타입 계약의 일부다. 이를 지우면 `RegistrationPeriod.term : AcademicTerm`처럼
        # 검증을 통과했던 선언이 최종 조립 과정에서 갑자기 미해소 타입이 된다.
        class_index = {class_name(item): item for item in result_classes}
        data_type_definitions = {
            text(item.get("name")): item
            for item in data_types
            if isinstance(item, dict)
        }
        reachable_data_types: set[str] = set()
        pending = [("class", name) for name in retained]
        while pending:
            kind, current_name = pending.pop()
            item = (
                class_index[current_name]
                if kind == "class"
                else data_type_definitions[current_name]
            )
            referenced: set[str] = set()
            for raw_field in item.get("fields") or []:
                referenced.update(referenced_type_names(field_type(raw_field)))
            if kind == "class":
                for operation in item.get("operations") or []:
                    if not isinstance(operation, dict):
                        continue
                    referenced.update(
                        referenced_type_names(text(operation.get("returnType")))
                    )
                    for parameter in operation.get("parameters") or []:
                        if isinstance(parameter, dict):
                            referenced.update(
                                referenced_type_names(text(parameter.get("type")))
                            )
            for name in referenced & class_index.keys() - retained:
                retained.add(name)
                pending.append(("class", name))
            for name in referenced & data_type_definitions.keys() - reachable_data_types:
                reachable_data_types.add(name)
                pending.append(("dataType", name))
        result_classes = [item for item in result_classes if class_name(item) in retained]
        relationships = [
            item
            for item in relationships
            if isinstance(item, dict)
            and text(item.get("source")) in retained
            and text(item.get("target")) in retained
        ]
        data_types = [
            item
            for item in data_types
            if isinstance(item, dict)
            and text(item.get("name")) in reachable_data_types
        ]
    return BCEModel.model_validate(
        {
            "Classes": result_classes,
            "DataTypes": data_types,
            "Relationships": relationships,
            "Collaborations": [],
        }
    ).model_dump(by_alias=True)


def emit_preview(
    model: dict[str, Any],
    phase: str,
    unit: str,
    completed: int,
    total: int,
) -> None:
    """한 수락 경계의 BCE skeleton을 UI 진행 스냅샷으로 발행한다.

    빈 PlantUML은 이벤트를 만들지 않는다. 이 함수가 operations 모듈의 유일한 UI 부작용이며
    이벤트 실패를 validation 결과로 위장하지 않는다.
    """
    puml = generate_plantuml_from_bce_json(model)
    if puml:
        design_progress.emit_progress(
            "classDiagramSnapshotAccepted",
            puml=puml,
            phase=phase,
            unit=unit,
            completed=completed,
            total=total,
            detail={
                "inventory": "Building the class inventory",
                "operations": f"Adding operations for {unit}",
                "collaborations": f"Planning collaboration {unit}",
            }.get(phase, "Updating the class contract"),
        )


def _compose_fragments(
    inventory: dict[str, Any],
    fragments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return _compose(
        inventory,
        sorted(fragments.items(), key=lambda item: id_key(item[0])),
        final=True,
    )


def reserved_operations(model: BCEModel) -> list[dict[str, Any]]:
    """수락 모델에서 이후 조각 생성에 예약할 연산 catalog를 읽는다.

    Args:
        model: 앞선 unit까지 조립된 BCE skeleton이다.

    Returns:
        class 이름과 수락 operation 목록만 포함하는 LLM payload 조각이다.
    """
    return _reserved_operations(model.model_dump(by_alias=True))


def checked_fragment(
    index: ScenarioIndex,
    inventory: AcceptedInventory,
    use_case: UseCase,
    **kwargs: Any,
) -> AcceptedFragment:
    """검사와 이력 기반 replacement를 거친 연산 fragment를 생성한다.

    Args:
        index: 시나리오와 허용 단계의 기준이다.
        inventory: 고정된 전역 BCE 구조다.
        use_case: fragment 소유 유스케이스다.
        **kwargs: 이전 후보, finding, 예약 catalog와 operation 이름이다.

    Returns:
        모든 ``OPERATION_CHECKS``를 통과한 ``AcceptedFragment``다.

    Raises:
        RuntimeError: 검사기 자체가 완료되지 못한 경우다.
        ValueError: 공급자 응답 또는 수락 후보를 스키마로 읽을 수 없는 경우다.
    """
    previous = kwargs.pop("previous", None)
    candidate = _checked_fragment(
        index,
        inventory.as_payload(),
        use_case,
        previous=previous.as_payload() if isinstance(previous, AcceptedFragment) else previous,
        **kwargs,
    )
    return AcceptedFragment(use_case_id=use_case.id, payload=candidate)


def compose_fragments(
    inventory: AcceptedInventory,
    fragments: Mapping[str, AcceptedFragment],
) -> BCEModel:
    """수락된 fragment를 하나의 canonical BCE skeleton으로 조립한다.

    Args:
        inventory: 클래스·구조 타입·관계의 고정 원본이다.
        fragments: use-case ID로 소유권이 지정된 수락 연산 조각이다.

    Returns:
        collaboration이 비어 있고 도달 가능한 계약만 남은 ``BCEModel``이다.
    """
    return BCEModel.model_validate(
        _compose_fragments(
            inventory.as_payload(),
            {key: value.as_payload() for key, value in fragments.items()},
        )
    )


def compose_operation_units(
    inventory: AcceptedInventory,
    fragments: list[AcceptedFragment],
    *,
    final: bool = False,
) -> BCEModel:
    """유스케이스별 fragment를 입력 순서대로 조립한다."""

    return BCEModel.model_validate(
        _compose(
            inventory.as_payload(),
            [(item.use_case_id, item.as_payload()) for item in fragments],
            final=final,
        )
    )


__all__ = [
    "checked_fragment",
    "compose_fragments",
    "compose_operation_units",
    "emit_preview",
    "normalize_operation_fragment",
    "operation_payload",
    "operation_prompt",
    "reserved_operations",
    "validate_operation_fragment",
]
