"""전역 BCE 인벤토리의 LLM 제안, 정규화와 수락을 소유한다.

입력은 정규화된 ``ScenarioIndex``이며, LLM에는 프로젝트 원문 대신 역할·목표·단계와
유스케이스 관계만 압축해 전달한다. 응답 ``InventoryProposal``은 저장 shape로 정규화한 뒤
``INVENTORY_CHECKS``를 통과해야 ``AcceptedInventory``가 된다.

이 모듈은 LLM 호출과 이력 기반 inventory replacement라는 부작용을 가진다. 수리는 유한한
공유 예산 안에서만 수행한다. 연산, 협업, graph state와 저장소를 직접 참조하지 않는다.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from app.config import settings
from app.design.schemas.class_model import BCEModel
from app.design.services.class_diagram.cache import (
    AcceptedUnitCache,
    accepted_unit_key,
    configured_provider_identity,
    record_cache_outcome,
)
from app.design.services.class_diagram.models import AcceptedInventory, RepairBudget
from app.design.services.class_diagram.proposals import InventoryProposal
from app.design.services.class_diagram.scenario import ScenarioIndex, id_key, text
from app.design.services.class_diagram.type_system import (
    field_type,
    referenced_type_names,
    structure_type_contract,
)
from app.design.services.class_diagram.validation.inventory import INVENTORY_CHECKS
from app.design.services.common import fields
from app.design.services.common.structured import parse_structured
from app.llm_connection import build_llm_connection
from app.llm_profiles import effective_temperature
from app.validation import Finding, RepairAttempt, RepairLedger, run_checks, stable_digest

INVENTORY_PROMPT = (
    """
Build one fixed BCE inventory for the supplied accepted use-case
specifications. Return only items and Entity structural relationships. Classify
each item once as Boundary, Control, Entity, valueObject, or enumeration.

Boundary is an actor or external-system interface. Control coordinates cohesive
use-case behavior. Entity owns persistent business state. Actor roles are not
automatically classes. Boundary and Control retain no fields. Every Entity has
typed state and identifiers name declared fields. Declare a valueObject or
enumeration here only when an Entity field transitively requires it. Request,
criteria, summary, result, and export types belong to the later use-case
operation task and must not be declared in this inventory.
Use lowerCamelCase for every new business field name and identifier (for example,
orderId); do not use snake_case.
Use non-empty fields and no values for a valueObject. Use non-empty values and
no fields for an enumeration.

Prefer cohesive reusable classes over one class per sentence or use case.
Boundary classes represent actor channels or cohesive interfaces, not use-case
titles. Control classes represent domain capabilities and may coordinate a
related lifecycle or query family across several use cases; do not mirror the
use-case list with one Control per item. Reuse an item by assigning multiple
useCaseIds whenever the same responsibility and business state are involved.
Declare only types required by the supplied behavior. Relationships connect
only independently grounded Entities, appear in one direction, and include
both endpoint multiplicities. Do not return operations, calls, dependencies,
source bindings, or review commentary. For every Boundary, Control, and Entity,
declare all and only the supplied useCaseIds whose operations may use that
class. Use an empty useCaseIds list for valueObjects and enumerations; their
availability is derived from Entity fields. Return every array field explicitly,
using an empty array only when the selected kind requires none. Class ids are proposal scope only and are not
persisted as a separate design decision.
For each use case, decide whether its state must still exist after the request ends or
whether it reads state created by an earlier request. Assign an Entity candidate only
when that durable state is read or changed. A transient calculation, formatted result,
or external interaction does not require an Entity. A read-only use case still needs an
Entity when durable domain information is its source; classify by information ownership
and lifetime, not by whether the operation is a command or query.
For Entity items, useCaseIds means that the main or extension flow directly
reads or changes that Entity. Authentication, actor presence, a precondition,
or indirect domain context alone does not justify assigning an Entity to a use
case. Candidate scope does not force the later operation task to select it.
""".strip()
    + "\n\n"
    + structure_type_contract()
    + "\n\n"
    + (
        "Every generic container must include exactly one item type. Persistent Entity "
        "fields may use relational scalars, declared enumerations or valueObjects, direct "
        "Entity references, and at most one collection layer. Do not use Object or nest "
        "containers in an Entity field because no deterministic relational storage "
        "decision is present in this model."
    )
)


def inventory_reasoning_effort() -> str:
    """inventory 전용 reasoning 설정이 없던 실행도 기존 정책으로 유지한다."""

    return str(
        getattr(
            settings,
            "design_class_inventory_reasoning_effort",
            settings.design_reasoning_effort,
        )
    )


def inventory_max_completion_tokens() -> int:
    """inventory 전용 output cap이 없던 실행은 기존 구조 단계 cap을 유지한다."""

    return int(
        getattr(
            settings,
            "design_class_inventory_max_completion_tokens",
            settings.design_class_structure_max_completion_tokens,
        )
    )


def finding_text(findings: tuple[Finding, ...]) -> list[str]:
    """검사 finding을 수리 프롬프트에 넣을 간결한 문자열로 바꾼다."""

    return [
        f"{finding.location}: {finding.message}" if finding.location else finding.message
        for finding in findings
    ]


def _normalize_inventory(proposal: InventoryProposal) -> dict[str, Any]:
    """제안 schema를 저장 직전의 BCE inventory shape로 멱등 변환한다.

    LLM은 필드를 ``{"name": ..., "type": ...}``로 반환하지만 저장 모델은
    ``name : Type`` 문자열을 사용한다. DataType의 use-case 범위는 LLM의 빈 배열을 믿지
    않고 Entity 필드의 전이 참조에서 다시 계산한다.
    """

    raw = proposal.model_dump(by_alias=True)
    kinds = {item["name"]: item["kind"] for item in raw["items"]}
    classes: list[dict[str, Any]] = []
    data_types: list[dict[str, Any]] = []
    for item in raw["items"]:
        typed_fields = [
            fields.normalize_java_field_candidate(
                f"{field['name']} : {field['type']}"
            )
            for field in item["fields"]
        ]
        if item["kind"] in {"Boundary", "Control", "Entity"}:
            classes.append(
                {
                    "className": item["name"],
                    "stereotype": item["kind"],
                    "description": item["description"],
                    "fields": typed_fields,
                    "identifier": list(item["identifier"]),
                    "values": list(item["values"]),
                    "useCaseIds": list(item["useCaseIds"]),
                }
            )
        else:
            data_types.append(
                {
                    "name": item["name"],
                    "kind": item["kind"],
                    "fields": typed_fields,
                    "values": list(item["values"]),
                    "identifier": list(item["identifier"]),
                    "useCaseIds": [],
                }
            )
    # 구조 타입은 독립된 행동 소유자가 아니다. 사용 범위를 Entity 필드 참조에서
    # 유도해야 operation 단계가 관련 없는 DTO 후보를 받지 않는다.
    scopes = {item["className"]: set(item.get("useCaseIds") or []) for item in classes}
    type_index = {item["name"]: item for item in data_types}
    type_scopes: dict[str, set[str]] = {name: set() for name in type_index}
    for item in classes:
        for raw_field in item.get("fields") or []:
            for name in referenced_type_names(field_type(raw_field)) & type_index.keys():
                type_scopes[name].update(scopes[item["className"]])
    # 한 타입이 다른 타입을 중첩할 수 있으므로 고정점까지 전파한다. 직접 참조만 보면
    # Entity -> Address -> CountryCode에서 CountryCode의 scope가 사라진다.
    changed = True
    while changed:
        changed = False
        for name, item in type_index.items():
            for raw_field in item.get("fields") or []:
                for target in referenced_type_names(field_type(raw_field)) & type_index.keys():
                    before = len(type_scopes[target])
                    type_scopes[target].update(type_scopes[name])
                    changed = changed or before != len(type_scopes[target])
    for name, item in type_index.items():
        item["useCaseIds"] = sorted(type_scopes[name], key=id_key)
    return {
        "Classes": classes,
        "DataTypes": data_types,
        # 값 객체는 Entity field의 타입으로 이미 연결된다. 모델이 같은 포함 관계를
        # structural relationship으로 한 번 더 적어도 버리고, 독립 Entity 사이의
        # 관계만 저장한다. 알 수 없는 이름은 검사기가 정확한 오류를 보고하도록 남긴다.
        "Relationships": [
            relationship
            for relationship in raw["Relationships"]
            if (
                relationship["source"] not in kinds
                or relationship["target"] not in kinds
                or (
                    kinds[relationship["source"]] == "Entity"
                    and kinds[relationship["target"]] == "Entity"
                )
            )
        ],
    }


def inventory_payload(index: ScenarioIndex) -> dict[str, Any]:
    """전역 구조 결정에 필요한 시나리오 근거만 LLM payload로 투영한다.

    Args:
        index: 유스케이스, 단계와 관계를 정규화한 입력이다.

    Returns:
        ``useCases``와 ``relationships``만 포함하는 JSON 직렬화 가능 payload다.

    Notes:
        원문 명세의 구현 메모나 이미 생성된 산출물은 보내지 않는다. 선택 공간을 줄이면서
        BCE 책임과 추적 범위를 결정하는 근거는 모두 보존한다.
    """

    summaries = {
        text(item.get("id")): item
        for item in index.raw.get("use_cases") or []
        if isinstance(item, dict) and text(item.get("id"))
    }
    return {
        "useCases": [
            {
                "id": use_case.id,
                "name": use_case.name,
                "goal": text(summaries.get(use_case.id, {}).get("goal")),
                "primaryActor": use_case.primary_actor,
                "supportingActors": list(
                    summaries.get(use_case.id, {}).get("supporting_actors") or []
                ),
                # 단계 문장만으로는 "저장 후에도 남아야 하는 상태"와 단순 응답 값을
                # 구별하기 어렵다. 성공 후 상태와 사전 조건은 그 판단에 직접 필요한
                # 근거이므로 중복되는 main/extension 본문 없이 작게 전달한다.
                "context": {
                    "trigger": deepcopy(use_case.specification.get("trigger")),
                    "preconditions": deepcopy(use_case.specification.get("preconditions") or []),
                    "successGuarantee": deepcopy(
                        use_case.specification.get("success_guarantee") or []
                    ),
                    "minimalGuarantee": deepcopy(
                        use_case.specification.get("minimal_guarantee") or []
                    ),
                },
                "steps": [
                    {
                        "stepRef": step.id,
                        "branch": step.branch,
                        "subject": step.subject,
                        "sentence": step.sentence,
                        "condition": step.condition,
                    }
                    for step in use_case.steps
                ],
            }
            for use_case in index.use_cases
        ],
        "relationships": [
            {
                "kind": relationship.kind,
                "baseUseCaseId": relationship.base_id,
                "relatedUseCaseId": relationship.child_id,
                "anchorStepRefs": list(relationship.anchor_step_ids),
            }
            for relationship in index.relationships
        ],
    }


def _inventory_proposal_uncached(index: ScenarioIndex) -> AcceptedInventory:
    """전역 inventory를 생성하고 이력 기반 전체 replacement로 수락한다.

    Args:
        index: inventory의 허용 이름·유스케이스 범위를 제공하는 시나리오 인덱스다.

    Returns:
        정규화와 모든 inventory 검사를 통과한 frozen 수락 단위다.

    Raises:
        RuntimeError: 검사기 자체가 예외를 내 검증을 완료하지 못한 경우다.

    Notes:
        repair에는 최초 messages, 현재 전체 candidate, 모든 finding과 누적 실패 이력을 함께
        보낸다. 부분 patch는 허용하지 않으며 모든 결과는 같은 schema와 규칙을 통과해야 한다.
    """

    # 1. 원문을 재전송하지 않고 inventory 결정에 필요한 압축 payload를 한 번 만든다.
    source_payload = inventory_payload(index)
    messages = [
        {"role": "system", "content": INVENTORY_PROMPT},
        {"role": "user", "content": json.dumps(source_payload, ensure_ascii=False)},
    ]
    ledger = RepairLedger()
    input_digest = stable_digest(source_payload)
    candidate: dict[str, Any] | None = None
    budget = RepairBudget("inventory")
    attempt = 0
    while True:
        operation = "InteractionInventory" if attempt == 0 else "InteractionInventoryRepair"
        prompt = messages
        if candidate is not None:
            budget.consume("; ".join(ledger.attempts[-1].finding_keys_after))
            repeated_state = ledger.attempts[-1].outcome == "repeated_candidate"
            prompt = [
                *messages,
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": (
                                "The previous response repeated the same rejected state. Choose a "
                                "materially different structure and return the complete inventory."
                                if repeated_state
                                else "Return one materially different full repaired inventory. Preserve valid "
                                "decisions, resolve every finding, and do not repeat a rejected candidate."
                            ),
                            "candidate": candidate,
                            "findings": list(ledger.attempts[-1].finding_keys_after),
                            "repairHistory": json.loads(ledger.prompt_context()),
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
        parsed = parse_structured(
            prompt,
            InventoryProposal,
            reasoning_effort=inventory_reasoning_effort(),
            max_completion_tokens=inventory_max_completion_tokens(),
            operation=operation,
            metadata={
                "executionSlice": "inventory",
                "candidateCount": (
                    len(source_payload["useCases"])
                    if candidate is None
                    else len(candidate["Classes"]) + len(candidate["DataTypes"])
                ),
                "semanticRepair": attempt > 0,
                "repairAttempt": attempt,
            },
        )
        candidate = _normalize_inventory(InventoryProposal.model_validate(parsed))
        report = run_checks(INVENTORY_CHECKS, candidate, index)
        if report.errors:
            raise RuntimeError("; ".join(report.errors))
        if not report.findings:
            return AcceptedInventory.from_payload(candidate)

        findings = tuple(sorted(set(finding_text(report.findings))))
        candidate_digest = stable_digest(candidate)
        repeated = ledger.candidate_seen(
            input_digest=input_digest,
            candidate_digest=candidate_digest,
        ) or ledger.failure_seen(
            input_digest=input_digest,
            finding_keys=findings,
        )
        ledger.record(
            RepairAttempt(
                stage="design.class.inventory",
                target_ids=("inventory",),
                strategy_key=f"full-replacement-{attempt + 1}",
                input_digest=input_digest,
                candidate_digest=candidate_digest,
                finding_keys_before=findings,
                finding_keys_after=findings,
                outcome="repeated_candidate" if repeated else "no_improvement",
                detail="; ".join(findings),
            )
        )
        attempt += 1


def _inventory_cache_key(index: ScenarioIndex) -> str:
    """현재 시나리오와 LLM 정책에만 대응하는 inventory cache key다."""

    return accepted_unit_key(
        "inventory",
        unit_slice=inventory_payload(index),
        inventory={},
        feedback="",
        prompt=INVENTORY_PROMPT,
        schema=InventoryProposal,
        provider=configured_provider_identity(build_llm_connection().base_url),
        model=settings.model,
        seed=settings.seed,
        temperature=effective_temperature(settings.model, settings.temperature),
        reasoning_effort=inventory_reasoning_effort(),
        max_completion_tokens=inventory_max_completion_tokens(),
    )


def _accepted_inventory_from_cache(
    payload: dict[str, Any],
    index: ScenarioIndex,
) -> AcceptedInventory:
    """cache value도 schema와 inventory checks를 다시 통과시킨다."""

    accepted = AcceptedInventory.from_payload(payload)
    # 저장 BCE schema를 다시 통과시켜 cache가 typed boundary를 우회하지 못하게 한다.
    inventory_model(accepted)
    report = run_checks(INVENTORY_CHECKS, accepted.as_payload(), index)
    if report.errors or report.findings:
        raise ValueError(
            "cached class inventory is invalid: "
            + "; ".join([*report.errors, *finding_text(report.findings)])
        )
    return accepted


def inventory_proposal(
    index: ScenarioIndex,
    *,
    cache: AcceptedUnitCache | None = None,
) -> AcceptedInventory:
    """전역 inventory를 수락하고 지정 cache가 있으면 완성 단위만 재사용한다.

    Args:
        index: 정규화된 전체 use-case 시나리오다.
        cache: graph adapter가 선택적으로 주입하는 process-local accepted-unit cache다.

    Returns:
        Pydantic과 결정론 검사를 모두 통과한 불변 inventory다.

    Notes:
        cache를 생략하면 기존 LLM 호출 경로를 유지한다. hit도 저장 BCE schema와 inventory
        checks를 다시 실행하며 raw·partial·repair 중간 응답은 저장하지 않는다.
    """

    metadata = {
        "executionSlice": "inventory",
        "candidateCount": len(index.use_cases),
    }
    if cache is None:
        record_cache_outcome(
            None,
            operation="InteractionInventory",
            unit="inventory",
            metadata=metadata,
        )
        return _inventory_proposal_uncached(index)
    result = cache.get_or_compute(
        _inventory_cache_key(index),
        lambda: _inventory_proposal_uncached(index).as_payload(),
    )
    record_cache_outcome(
        result,
        operation="InteractionInventory",
        unit="inventory",
        metadata=metadata,
    )
    return _accepted_inventory_from_cache(result.value, index)


def normalize_inventory(proposal: InventoryProposal) -> AcceptedInventory:
    """제안 계약을 단계 경계의 불변 inventory로 정규화한다.

    Args:
        proposal: Pydantic 검증을 마친 일시적 LLM 응답이다.

    Returns:
        저장 alias와 파생 scope가 확정된 ``AcceptedInventory``다.
    """

    return AcceptedInventory.from_payload(_normalize_inventory(proposal))


def inventory_model(inventory: AcceptedInventory) -> BCEModel:
    """수락 inventory를 연산·협업이 비어 있는 BCE skeleton으로 투영한다.

    Args:
        inventory: 구조 단계에서 수락한 클래스, 타입과 관계다.

    Returns:
        operation과 collaboration을 후속 단계가 채울 수 있는 유효 ``BCEModel``이다.

    Notes:
        proposal 전용 scope 필드는 저장 schema에 맞게 제거·변환한다. 이 함수는 LLM을
        호출하거나 입력 inventory를 수정하지 않는다.
    """

    payload = inventory.as_payload()
    return BCEModel.model_validate(
        {
            "Classes": [
                {
                    **{
                        key: value
                        for key, value in item.items()
                        if key not in {"useCaseIds", "values"}
                    },
                    "use_case_ids": [],
                    "operations": [],
                }
                for item in payload["Classes"]
            ],
            "DataTypes": [
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"useCaseIds", "identifier"}
                }
                for item in payload["DataTypes"]
                if isinstance(item, dict)
            ],
            "Relationships": payload["Relationships"],
            "Collaborations": [],
        }
    )
