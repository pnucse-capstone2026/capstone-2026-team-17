"""피드백을 가장 작은 설계 수정 대상에 배정하고 그 부분만 교체한다.

입력은 현재 ``BCEModel``, 이를 만든 ``ScenarioIndex``, 사용자 피드백과 선택적 target
ID다. 출력은 inventory·operation·collaboration 중 하나로 좁혀진 ``FeedbackScope``와
해당 단계의 수락 결과다. 명시 target은 코드가 해석하고, 자연어만으로 어느 후보인지
결정해야 할 때는 LLM에 유한 후보 분류를 요청한다.

이 모듈의 LLM 부작용은 scope fallback, inventory 교체, 선택된 collaboration 교체다.
저장소나 graph state를 읽지 않으며, 선택되지 않은 소유자의 내용을 LLM 응답으로
덮어쓰지 않는다. 공개 서비스의 전체 generate/resume 흐름도 이 모듈에서 시작하지 않는다.
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Set as AbstractSet
from copy import deepcopy
from typing import Any

from app.config import settings
from app.design.schemas.class_model import BCEModel
from app.design.services.class_diagram import inventory
from app.design.services.class_diagram.cache import (
    AcceptedUnitCache,
    accepted_unit_key,
    configured_provider_identity,
    record_cache_outcome,
)
from app.design.services.class_diagram.models import (
    AcceptedFragment,
    AcceptedInventory,
    GenerationStalled,
    RepairBudget,
)
from app.design.services.class_diagram.proposals import (
    FeedbackScope,
    InventoryProposal,
)
from app.design.services.class_diagram.scenario import ScenarioIndex, id_key, text
from app.design.services.class_diagram.type_system import (
    field_name,
    field_type,
    referenced_type_names,
)
from app.design.services.class_diagram.validation.inventory import INVENTORY_CHECKS
from app.design.services.class_diagram.validation.model import class_name
from app.design.services.common.structured import parse_structured
from app.llm_connection import build_llm_connection
from app.llm_profiles import effective_temperature
from app.validation import Finding, RepairAttempt, RepairLedger, run_checks, stable_digest


def _inventory_from_model(model: dict[str, Any]) -> dict[str, Any]:
    """영속 BCE 모델에서 operation-local 선언을 제외한 구조 inventory를 복원한다.

    Entity field에서 도달 가능한 DataType만 구조 소유로 간주한다. operation signature에만
    등장하는 DTO는 operation fragment의 소유이므로 여기 포함하면 inventory 피드백이
    의도하지 않은 유스케이스 계약까지 바꾸게 된다.
    """

    all_data_types = {
        text(item.get("name")): deepcopy(item)
        for item in model.get("DataTypes") or []
        if isinstance(item, dict) and text(item.get("name"))
    }
    structural_names: set[str] = set()
    pending = {
        name
        for item in model.get("Classes") or []
        if isinstance(item, dict) and text(item.get("stereotype")) == "Entity"
        for value in item.get("fields") or []
        for name in referenced_type_names(field_type(value))
        if name in all_data_types
    }
    # Entity가 직접 참조한 타입에서 시작해 중첩 valueObject까지 고정점으로 따라간다.
    # 예: Order -> Address -> PostalCode는 세 타입 모두 구조 inventory에 속한다.
    while pending:
        name = pending.pop()
        if name in structural_names:
            continue
        structural_names.add(name)
        for value in all_data_types[name].get("fields") or []:
            pending.update(
                referenced_type_names(field_type(value))
                & (all_data_types.keys() - structural_names)
            )
    data_types = {name: item for name, item in all_data_types.items() if name in structural_names}
    type_scopes: dict[str, set[str]] = {name: set() for name in data_types}
    classes: list[dict[str, Any]] = []
    for item in model.get("Classes") or []:
        if not isinstance(item, dict):
            continue
        scope = {text(value) for value in item.get("use_case_ids") or [] if text(value)}
        classes.append({
            **{
                key: deepcopy(value) for key, value in item.items()
                if key not in {"use_case_ids", "operations"}
            },
            "useCaseIds": sorted(scope, key=id_key),
        })
        referenced = {
            name
            for value in item.get("fields") or []
            for name in referenced_type_names(field_type(value))
        }
        for name in referenced & type_scopes.keys():
            type_scopes[name].update(scope)
    # 타입 자신은 useCaseIds를 저장하지 않으므로 그 타입을 소유한 클래스의 범위를
    # 참조 그래프를 따라 전파해 proposal 계약을 다시 만든다.
    changed = True
    while changed:
        changed = False
        for name, item in data_types.items():
            targets = {
                target
                for value in item.get("fields") or []
                for target in referenced_type_names(field_type(value))
                if target in type_scopes
            }
            for target in targets:
                before = len(type_scopes[target])
                type_scopes[target].update(type_scopes[name])
                changed = changed or len(type_scopes[target]) != before
    return {
        "Classes": classes,
        "DataTypes": [
            {**item, "useCaseIds": sorted(type_scopes[name], key=id_key)}
            for name, item in data_types.items()
        ],
        "Relationships": [
            deepcopy(item) for item in model.get("Relationships") or []
            if isinstance(item, dict) and text(item.get("type")) != "Dependency"
        ],
    }


def _inventory_as_proposal(inventory_model: dict[str, Any]) -> dict[str, Any]:
    """영속 inventory alias를 ``InventoryProposal`` 입력 모양으로 되돌린다.

    저장 모델은 BCE class와 DataType을 분리하지만 LLM 계약은 ``items`` 한 목록이다.
    이 변환은 feedback 요청에 현재 상태를 제공하기 위한 것으로 operation과 dependency는
    포함하지 않는다.
    """

    items: list[dict[str, Any]] = []
    for item in inventory_model.get("Classes") or []:
        if not isinstance(item, dict):
            continue
        items.append({
            "name": class_name(item),
            "kind": text(item.get("stereotype")),
            "description": text(item.get("description")),
            "fields": [
                {"name": field_name(value), "type": field_type(value)}
                for value in item.get("fields") or []
            ],
            "identifier": list(item.get("identifier") or []),
            "values": [],
            "useCaseIds": list(item.get("useCaseIds") or []),
        })
    for item in inventory_model.get("DataTypes") or []:
        if not isinstance(item, dict):
            continue
        items.append({
            "name": text(item.get("name")),
            "kind": text(item.get("kind")),
            "description": "",
            "fields": [
                {"name": field_name(value), "type": field_type(value)}
                for value in item.get("fields") or []
            ],
            "identifier": [],
            "values": list(item.get("values") or []),
            "useCaseIds": list(item.get("useCaseIds") or []),
        })
    return {
        "items": items,
        "Relationships": deepcopy(inventory_model.get("Relationships") or []),
    }


def _fragments_from_model(
    index: ScenarioIndex, model: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """영속 BCE 모델을 유스케이스별 operation-local fragment로 역투영한다.

    ``stepRefs``가 소유 유스케이스를 결정하고, signature에서 도달하는 비-inventory
    DataType을 함께 복원한다. 이 역투영 덕분에 resume/revise가 기존 체크포인트를 별도
    내부 포맷으로 마이그레이션하지 않고도 국소 교체할 수 있다.
    """

    inventory_type_names = {
        text(item.get("name"))
        for item in _inventory_from_model(model).get("DataTypes") or []
        if isinstance(item, dict)
    }
    all_types = {
        text(item.get("name")): item
        for item in model.get("DataTypes") or []
        if isinstance(item, dict)
    }
    result: dict[str, dict[str, Any]] = {}
    for use_case in index.use_cases:
        class_sets: list[dict[str, Any]] = []
        for item in model.get("Classes") or []:
            if not isinstance(item, dict):
                continue
            operations: list[dict[str, Any]] = []
            for operation in item.get("operations") or []:
                if not isinstance(operation, dict):
                    continue
                refs = [
                    text(ref) for ref in operation.get("stepRefs") or []
                    if text(ref).startswith(f"{use_case.id}:")
                ]
                if refs:
                    operations.append({
                        key: deepcopy(value) for key, value in operation.items()
                        if key != "operationId"
                    } | {"stepRefs": refs})
            if operations:
                class_sets.append({"className": class_name(item), "operations": operations})
        if class_sets:
            local_names = {
                name
                for class_set in class_sets
                for operation in class_set.get("operations") or []
                for type_expression in [
                    *(text(parameter.get("type")) for parameter in operation.get("parameters") or []),
                    text(operation.get("returnType")),
                ]
                for name in referenced_type_names(type_expression)
                if name in all_types and name not in inventory_type_names
            }
            pending = list(local_names)
            while pending:
                name = pending.pop()
                for value in all_types[name].get("fields") or []:
                    for target in referenced_type_names(field_type(value)):
                        if target in all_types and target not in inventory_type_names and target not in local_names:
                            local_names.add(target)
                            pending.append(target)
            result[use_case.id] = {
                "DataTypes": [deepcopy(all_types[name]) for name in sorted(local_names)],
                "Classes": class_sets,
            }
    return result


def _feedback_scope(
    index: ScenarioIndex,
    model: dict[str, Any],
    feedback: str,
    targets: set[str],
) -> FeedbackScope:
    """결정론적 단서를 우선 사용해 가장 작은 수정 소유자를 선택한다.

    명시 target은 ID로 확정하고, target이 없거나 서로 다른 종류가 섞였으면 유한 후보
    LLM 분류로 보낸다. 자연어에 타입 이름이 포함됐다는 이유만으로 소유자를 추측하지 않는다.
    """

    inventory_ids = {
        class_name(item) for item in model.get("Classes") or [] if isinstance(item, dict)
    } | {
        text(item.get("name"))
        for item in _inventory_from_model(model).get("DataTypes") or []
        if isinstance(item, dict)
    }
    fragments = _fragments_from_model(index, model)
    local_type_owners: dict[str, set[str]] = {}
    operation_owners: dict[str, set[str]] = {}
    for use_case_id, fragment in fragments.items():
        for item in fragment.get("DataTypes") or []:
            if isinstance(item, dict):
                local_type_owners.setdefault(text(item.get("name")), set()).add(use_case_id)
        for item in fragment.get("Classes") or []:
            if not isinstance(item, dict):
                continue
            for operation in item.get("operations") or []:
                if not isinstance(operation, dict):
                    continue
                for key in ("stableId", "operationId"):
                    operation_id = text(operation.get(key))
                    if operation_id:
                        operation_owners.setdefault(operation_id, set()).add(use_case_id)
    use_case_ids = {use_case.id for use_case in index.use_cases}
    # The reversible fragment intentionally omits canonical operationId because it is
    # recomputed after an edit.  UI/RTM targets can still use that current ID, so map
    # both identities from the persisted model through their explicit step ownership.
    for item in model.get("Classes") or []:
        if not isinstance(item, dict):
            continue
        for operation in item.get("operations") or []:
            if not isinstance(operation, dict):
                continue
            owners = {
                ref.split(":", 1)[0]
                for value in operation.get("stepRefs") or []
                if (ref := text(value)) and ref.split(":", 1)[0] in use_case_ids
            }
            for key in ("stableId", "operationId"):
                operation_id = text(operation.get(key))
                if operation_id and owners:
                    operation_owners.setdefault(operation_id, set()).update(owners)
    collaboration_ids = {
        use_case.id for use_case in index.use_cases
        if any(group.use_case_id == use_case.id for group in index.groups)
    }
    collaboration_owners: dict[str, set[str]] = {}
    call_owners: dict[str, set[str]] = {}
    for collaboration in model.get("Collaborations") or []:
        if not isinstance(collaboration, dict):
            continue
        collaboration_id = text(collaboration.get("collaborationId"))
        owners = {
            text(use_case_id)
            for use_case_id in collaboration.get("useCaseIds") or []
            if text(use_case_id) in collaboration_ids
        }
        if collaboration_id in collaboration_ids:
            owners.add(collaboration_id)
        if not owners:
            continue
        collaboration_owners[collaboration_id] = owners
        for call in collaboration.get("calls") or []:
            if not isinstance(call, dict):
                continue
            for key in ("stableId", "callId"):
                call_id = text(call.get(key))
                if call_id:
                    call_owners.setdefault(call_id, set()).update(owners)
    # 1. UI나 finding이 정확한 ID를 보냈다면 LLM을 호출하지 않는다. target 전체가 한
    # 소유 집합에 포함될 때만 확정해 부분적으로 잘못 해석된 ID를 조용히 버리지 않는다.
    if targets:
        if targets <= inventory_ids:
            return FeedbackScope(kind="inventory", ids=sorted(targets))
        if targets <= local_type_owners.keys():
            owners = {
                use_case_id for target in targets for use_case_id in local_type_owners[target]
            }
            return FeedbackScope(kind="operation", ids=sorted(owners, key=id_key))
        if targets <= operation_owners.keys():
            owners = {
                use_case_id for target in targets for use_case_id in operation_owners[target]
            }
            return FeedbackScope(kind="operation", ids=sorted(owners, key=id_key))
        legacy_collaboration_targets = set(collaboration_owners) - collaboration_ids
        if targets <= legacy_collaboration_targets:
            owners = {
                use_case_id for target in targets
                for use_case_id in collaboration_owners[target]
            }
            return FeedbackScope(kind="collaboration", ids=sorted(owners, key=id_key))
        if targets <= call_owners.keys():
            owners = {
                collaboration_id for target in targets
                for collaboration_id in call_owners[target]
            }
            return FeedbackScope(kind="collaboration", ids=sorted(owners, key=id_key))
    # 2. 결정론적 단서가 없을 때만 LLM이 종류와 ID를 고른다. 아래 candidates 밖의 ID는
    # 응답 검증 직후 거부되며, 이 호출 자체가 설계 내용을 생성하지는 않는다.
    parsed = parse_structured(
        [
            {
                "role": "system",
                "content": (
                    "Classify the feedback into exactly one smallest design owner. "
                    "inventory changes classes, fields, types, or structural relationships; "
                    "operation changes one or more use-case method contracts; collaboration "
                    "changes call order or delegation only. A request to add a method to an "
                    "existing class is an operation change, not an inventory change. Use the "
                    "selected collaboration context and use-case steps to identify the operation "
                    "owners without treating those collaborations as part of the method contract. "
                    "Select ids only from candidates."
                ),
            },
            {"role": "user", "content": json.dumps({
                "feedback": feedback,
                "selectedTargets": sorted(targets),
                "candidates": {
                    "inventory": sorted(inventory_ids),
                    "operation": [
                        {
                            "id": use_case.id,
                            "name": use_case.name,
                            "steps": [
                                {"id": step.id, "sentence": step.sentence}
                                for step in use_case.steps
                            ],
                        }
                        for use_case in sorted(index.use_cases, key=lambda item: id_key(item.id))
                    ],
                    "collaboration": sorted(collaboration_ids, key=id_key),
                },
            }, ensure_ascii=False)},
        ],
        FeedbackScope,
        reasoning_effort="low",
        max_completion_tokens=settings.design_class_collaboration_max_completion_tokens,
        operation="InteractionFeedbackScope",
    )
    scope = FeedbackScope.model_validate(parsed)
    context_operation_owners = {
        use_case_id
        for target in targets
        for use_case_id in collaboration_owners.get(target, set())
    }
    if scope.kind == "operation" and context_operation_owners:
        # The conversation layer already selected these collaborations from
        # semantic search evidence.  This inner classifier chooses the owner
        # kind; it must not silently discard one of the frozen companion flows.
        scope = scope.model_copy(
            update={"ids": sorted(context_operation_owners, key=id_key)}
        )
    allowed = {
        "inventory": inventory_ids,
        "operation": use_case_ids,
        "collaboration": collaboration_ids,
    }[scope.kind]
    if not set(scope.ids) <= allowed:
        raise ValueError("feedback scope selected an unknown target")
    return scope


def _propose_inventory_revision(
    index: ScenarioIndex,
    inventory_model: dict[str, Any],
    feedback: str,
    target_ids: set[str],
) -> dict[str, Any]:
    """전체 inventory 제안을 요청하되 지정하지 않은 소유자는 원본으로 되돌린다.

    LLM 입력은 feedback, target ID, 현재 inventory, 원시 scenario다. 출력은 기존
    ``InventoryProposal``과 같은 전체 교체안이다. target이 있으면 해당 item과 그 item이
    닿는 구조 관계만 취하고, 나머지는 원본을 보존한다. 검증 finding이 있으면 유한 수리
    루프에 보내며 통과한 후보만 반환한다.
    """

    current = _inventory_as_proposal(inventory_model)
    # 전체 모양을 받는 이유는 구조 관계와 타입 참조를 한 번에 schema 검증하기 위해서다.
    # 실제 수정 권한은 아래 merge에서 target_ids로 다시 축소된다.
    original = InventoryProposal.model_validate(current)
    original_names = {item.name for item in original.items}
    if target_ids and not target_ids <= original_names:
        raise ValueError("inventory feedback target does not exist")
    baseline = inventory._normalize_inventory(original) if target_ids else None
    source = {
        "feedback": feedback,
        "targetIds": sorted(target_ids),
        "currentInventory": current,
        "scenario": index.raw,
    }
    messages = [
        {"role": "system", "content": inventory.INVENTORY_PROMPT},
        {"role": "user", "content": json.dumps({
            "task": "Apply the user feedback to the inventory and return one full replacement inventory. Only targetIds are authorized to change; preserve every other item and unrelated relationship.",
            **source,
        }, ensure_ascii=False)},
    ]
    ledger = RepairLedger()
    input_digest = stable_digest(source)
    budget = RepairBudget("inventory")
    candidate: dict[str, Any] | None = None
    last_findings: tuple[Finding, ...] = ()
    attempt = 0
    while True:
        operation = "InteractionInventoryFeedback" if attempt == 0 else "InteractionInventoryFeedbackRepair"
        prompt = messages
        if candidate is not None:
            budget.consume("; ".join(ledger.attempts[-1].finding_keys_after))
            prompt = [
                *messages,
                {"role": "user", "content": json.dumps({
                    "task": "Repair the current feedback candidate. Preserve valid decisions, resolve every finding, and return the complete inventory.",
                    "candidate": candidate,
                    "findings": [finding.model_dump(mode="json") for finding in last_findings],
                    "repairHistory": json.loads(ledger.prompt_context()),
                }, ensure_ascii=False)},
            ]
        parsed = parse_structured(
            prompt,
            InventoryProposal,
            reasoning_effort=inventory.inventory_reasoning_effort(),
            max_completion_tokens=inventory.inventory_max_completion_tokens(),
            operation=operation,
            metadata={
                "executionSlice": "inventory",
                "candidateCount": len(target_ids) or len(current["items"]),
                "semanticRepair": attempt > 0,
                "repairAttempt": attempt,
            },
        )
        proposal = InventoryProposal.model_validate(parsed)
        if target_ids:
            # 선택된 item만 교체하고 관계 권한도 선택 item의 끝점으로 한정한다.
            replacement = {item.name: item for item in proposal.items}
            merged_items = [
                replacement.get(item.name, item) if item.name in target_ids else item
                for item in original.items
            ]
            replacement_relationships = [
                relationship for relationship in proposal.Relationships
                if relationship.source in target_ids or relationship.target in target_ids
            ]
            retained_relationships = [
                relationship for relationship in original.Relationships
                if relationship.source not in target_ids and relationship.target not in target_ids
            ]
            proposal = InventoryProposal(
                items=merged_items,
                Relationships=[*retained_relationships, *replacement_relationships],
            )
        candidate = inventory._normalize_inventory(proposal)
        errors, findings = _inventory_validation_regressions(index, candidate, baseline=baseline)
        repair_findings = (*findings, *(
            Finding(rule_id="inventory.validation_error", message=error, origin="deterministic")
            for error in errors
        ))
        if not repair_findings:
            return candidate
        finding_keys = tuple(sorted({
            *inventory.finding_text(findings),
            *(f"inventory.validation_error: {error}" for error in errors),
        }))
        candidate_digest = stable_digest(candidate)
        repeated = ledger.candidate_seen(
            input_digest=input_digest, candidate_digest=candidate_digest,
        ) or ledger.failure_seen(input_digest=input_digest, finding_keys=finding_keys)
        unchanged = bool(ledger.attempts) and (
            ledger.attempts[-1].finding_keys_after == finding_keys
        )
        ledger.record(RepairAttempt(
            stage="design.class.inventory.feedback",
            target_ids=tuple(sorted(target_ids)),
            strategy_key=f"full-replacement-{attempt + 1}",
            input_digest=input_digest,
            candidate_digest=candidate_digest,
            finding_keys_before=finding_keys,
            finding_keys_after=finding_keys,
            outcome="repeated_candidate" if repeated else "no_improvement",
            detail="; ".join(finding_keys),
        ))
        if repeated or unchanged:
            raise GenerationStalled("inventory", "; ".join(finding_keys))
        last_findings = repair_findings
        attempt += 1


def _inventory_validation_regressions(
    index: ScenarioIndex,
    candidate: dict[str, Any],
    *,
    baseline: dict[str, Any] | None,
) -> tuple[tuple[str, ...], tuple[Finding, ...]]:
    """Return all findings for a replacement, or only new ones for a local edit."""

    report = run_checks(
        INVENTORY_CHECKS, candidate, index, deduplicate=baseline is None
    )
    if baseline is None:
        return report.errors, report.findings
    baseline_report = run_checks(
        INVENTORY_CHECKS, baseline, index, deduplicate=False
    )
    return (
        tuple((Counter(report.errors) - Counter(baseline_report.errors)).elements()),
        tuple((Counter(report.findings) - Counter(baseline_report.findings)).elements()),
    )


def inventory_from_model(model: BCEModel) -> AcceptedInventory:
    """수락된 모델에서 구조 inventory를 불변 경계로 재구성한다.

    Args:
        model: 영속 schema로 검증된 현재 BCE 모델이다.

    Returns:
        operation-local 선언을 제외한 ``AcceptedInventory``다.
    """
    return AcceptedInventory.from_payload(_inventory_from_model(model.model_dump(by_alias=True)))


def inventory_as_proposal(inventory_model: AcceptedInventory) -> dict[str, Any]:
    """수락된 inventory를 LLM 제안 계약으로 되살린다.

    Args:
        inventory_model: 구조 단계의 불변 수락 단위다.

    Returns:
        ``InventoryProposal``로 검증 가능한 별칭 JSON이다.
    """
    return _inventory_as_proposal(inventory_model.as_payload())


def fragments_from_model(
    index: ScenarioIndex, model: BCEModel,
) -> dict[str, AcceptedFragment]:
    """수락된 BCE 모델에서 유스케이스별 연산 조각을 복원한다.

    Args:
        index: step ID와 유스케이스 소유권의 기준이다.
        model: operation과 DataType이 합쳐진 영속 모델이다.

    Returns:
        유스케이스 ID를 수락된 operation fragment에 연결한 mapping이다.
    """
    fragments = _fragments_from_model(index, model.model_dump(by_alias=True))
    return {
        use_case_id: AcceptedFragment(use_case_id=use_case_id, payload=fragment)
        for use_case_id, fragment in fragments.items()
    }


def feedback_scope(
    index: ScenarioIndex,
    model: BCEModel,
    feedback: str,
    targets: AbstractSet[str],
) -> FeedbackScope:
    """피드백이 수정할 가장 작은 수락 경계를 결정한다.

    Args:
        index: 허용된 유스케이스·실행 그룹 ID 집합이다.
        model: 현재 수락된 BCE 모델이다.
        feedback: 사용자의 자연어 수정 요청이다.
        targets: UI 또는 finding이 이미 알고 있는 소유 ID다.

    Returns:
        종류 하나와 그 종류에 속하는 유한 ID 목록이다.

    Notes:
        명시 target으로 확정할 수 있으면 LLM 호출은 발생하지 않는다.
    """
    return _feedback_scope(
        index, model.model_dump(by_alias=True), feedback, set(targets),
    )


def propose_inventory_revision(
    index: ScenarioIndex,
    inventory_model: AcceptedInventory,
    feedback: str,
    target_ids: AbstractSet[str],
    *,
    cache: AcceptedUnitCache | None = None,
) -> AcceptedInventory:
    """피드백을 반영한 inventory 교체안을 검사해 수락한다.

    Args:
        index: inventory 규칙이 참조할 시나리오 인덱스다.
        inventory_model: 현재 수락된 구조 inventory다.
        feedback: LLM에 전달할 사용자 수정 요청이다.
        target_ids: 실제로 변경을 허용할 inventory item 이름이다.

    Returns:
        정규화와 ``INVENTORY_CHECKS``를 통과한 새 수락 단위다.

    Raises:
        ValueError: target이 없거나 교체안이 inventory 구성 규칙을 위반한 경우다.

    Notes:
        LLM은 전체 교체안을 반환하지만 target 밖 item은 코드가 원본으로 복원한다.
    """
    targets = set(target_ids)
    payload = inventory_model.as_payload()
    metadata = {
        "executionSlice": "inventory",
        "candidateCount": len(targets) or len(index.use_cases),
    }
    def compute() -> dict[str, Any]:
        return _propose_inventory_revision(index, payload, feedback, targets)
    if cache is None:
        record_cache_outcome(
            None,
            operation="InteractionInventoryFeedback",
            unit="inventory",
            metadata=metadata,
        )
        candidate = compute()
    else:
        key = accepted_unit_key(
            "inventory-revision",
            unit_slice=inventory.inventory_payload(index),
            inventory=payload,
            feedback={
                "feedback": " ".join(feedback.split()),
                "targetIds": sorted(targets),
            },
            prompt=inventory.INVENTORY_PROMPT,
            schema=InventoryProposal,
            provider=configured_provider_identity(build_llm_connection().base_url),
            model=settings.model,
            seed=settings.seed,
            temperature=effective_temperature(settings.model, settings.temperature),
            reasoning_effort=inventory.inventory_reasoning_effort(),
            max_completion_tokens=inventory.inventory_max_completion_tokens(),
        )
        result = cache.get_or_compute(key, compute)
        record_cache_outcome(
            result,
            operation="InteractionInventoryFeedback",
            unit="inventory",
            metadata=metadata,
        )
        candidate = result.value
    accepted = AcceptedInventory.from_payload(candidate)
    # cache hit도 저장 BCE schema와 inventory 규칙을 같은 순서로 재실행한다.
    inventory.inventory_model(accepted)
    errors, findings = _inventory_validation_regressions(
        index,
        accepted.as_payload(),
        baseline=payload if targets else None,
    )
    if errors or findings:
        raise ValueError("cached inventory feedback is invalid: " + "; ".join([
            *errors, *inventory.finding_text(findings),
        ]))
    return accepted


revise_inventory = propose_inventory_revision

__all__ = [
    "feedback_scope",
    "fragments_from_model",
    "inventory_as_proposal",
    "inventory_from_model",
    "propose_inventory_revision",
    "revise_inventory",
]
