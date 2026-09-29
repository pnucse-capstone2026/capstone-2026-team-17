"""수락된 BCE collaboration을 결정론적 시퀀스 저장 모델로 투영한다.

입력은 ``ScenarioIndex``, operation·call tree·argument provenance가 검증된 ``BCEModel``과
선택적 class PlantUML이다. 출력은 유스케이스별 participant, call/return, fragment를 가진
``SequenceCollection``이다. class PlantUML은 내용 hash만 저장해 어느 클래스 다이어그램
버전에서 투영했는지 검증할 수 있게 한다.

이 모듈은 LLM, 설정, 저장소, graph state에 의존하지 않는다. operation 선택이나 repair를
하지 않으며 입력에 모순이 있으면 ``ValueError``로 실패한다. 같은 입력은 participant 별칭,
message 순서와 hash까지 같은 결과를 만든다.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from copy import deepcopy
from typing import Any

from app.design.schemas.class_model import BCEModel
from app.design.schemas.sequence_model import (
    SequenceArgument,
    SequenceCollection,
    SequenceFragment,
    SequenceMessage,
    SequenceParticipant,
    UseCaseSequence,
)
from app.design.services.class_diagram.scenario import (
    ScenarioIndex,
    id_key,
    text,
)
from app.design.services.class_diagram.validation.model import operation_catalog

__all__ = [
    "SequenceArgument",
    "SequenceCollection",
    "SequenceFragment",
    "SequenceMessage",
    "SequenceParticipant",
    "UseCaseSequence",
    "project_sequence_model",
    "sequence_findings",
]


def _alias(value: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not candidate:
        return "Participant"
    return f"P_{candidate}" if candidate[0].isdigit() else candidate


def _signature(operation: dict[str, Any]) -> str:
    parameters = ",".join(
        f"{text(parameter.get('name'))}:{text(parameter.get('type'))}"
        for parameter in operation.get("parameters") or [] if isinstance(parameter, dict)
    )
    return f"{text(operation.get('name'))}({parameters})"


def _parameter_type(operation: dict[str, Any], name: str) -> str:
    return next((
        text(parameter.get("type"))
        for parameter in operation.get("parameters") or []
        if isinstance(parameter, dict) and text(parameter.get("name")) == name
    ), "Object")


def _argument_kind(source_ref: str, call_ids: set[str], step_ids: set[str]) -> str:
    source_id, separator, path = source_ref.partition("#")
    if ":precondition:" in source_id:
        return "precondition"
    if source_id in call_ids and separator:
        return "call_result" if path == "result" or path.startswith("result.") else "call_parameter"
    if source_id in step_ids and separator:
        return "input"
    return "state"


def _extension_fragments(index: ScenarioIndex) -> dict[str, dict[str, dict[str, str]]]:
    """조건이 있는 extension step을 재사용 가능한 ``opt`` 경로로 만든다."""
    result: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    for use_case in index.use_cases:
        for step in use_case.steps:
            if step.branch == "main" or not step.condition:
                continue
            result[use_case.id][step.id] = {
                "id": step.extension_ref,
                "condition_ref": step.extension_ref,
                "type": "opt",
                "branch": "main",
                "condition": step.condition,
            }
    return result


def _fragment_path(
    refs: list[str], fragments: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    if not refs or any(ref not in fragments for ref in refs):
        return []
    selected = [fragments[ref] for ref in refs]
    if not selected or len({item["id"] for item in selected}) != 1:
        return []
    return [dict(selected[0])]


def _project_collaboration(
    collaboration: dict[str, Any],
    operations: dict[str, dict[str, Any]],
    use_case_id: str,
    fragments: dict[str, dict[str, str]],
    actor_ref: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """call tree 하나를 깊이 우선 call/return 메시지와 participant로 펼친다.

    자식 call은 부모 call 뒤, 부모 return 앞에 위치한다. 따라서 ``A→B, B→C, C⇢B,
    B⇢A`` 순서가 입력 call tree만으로 정해지고 LLM 문장이나 renderer가 순서를 고르지 않는다.
    """
    calls = [item for item in collaboration.get("calls") or [] if isinstance(item, dict)]
    if not calls:
        raise ValueError("accepted collaboration cannot be empty")
    call_by_id = {text(call.get("callId")): call for call in calls}
    if len(call_by_id) != len(calls) or "" in call_by_id:
        raise ValueError("collaboration call IDs must be nonblank and unique")
    children: dict[str, list[str]] = defaultdict(list)
    roots: list[str] = []
    seen: set[str] = set()
    # parent는 반드시 앞에 있어야 한다. 이 제약은 순환을 별도 탐색하지 않고도 차단하며
    # root 배열 순서는 같은 actor가 다시 시스템을 호출하는 시간 순서다.
    for call in calls:
        call_id = text(call.get("callId"))
        parent_id = text(call.get("parentCallId"))
        operation_id = text(call.get("receiverOperationId"))
        if operation_id not in operations:
            raise ValueError(f"unknown receiver operation: {operation_id}")
        if not text(call.get("stableId")):
            raise ValueError(f"accepted call has no stableId: {call_id}")
        if not text(operations[operation_id].get("stableId")):
            raise ValueError(f"accepted operation has no stableId: {operation_id}")
        if parent_id:
            if parent_id not in seen:
                raise ValueError("parent call must precede its child")
            children[parent_id].append(call_id)
        else:
            roots.append(call_id)
        seen.add(call_id)

    if not roots:
        raise ValueError("a use-case collaboration requires at least one root call")

    participants: dict[str, dict[str, Any]] = {}
    class_aliases: dict[str, str] = {}
    actor = text(collaboration.get("entryActor"))
    actor_alias = _alias(actor) if actor else ""
    if actor:
        if not actor_ref:
            raise ValueError(f"accepted actor has no actor_ref for {use_case_id}: {actor}")
        participants[actor_alias] = {
            "name": actor,
            "alias": actor_alias,
            "kind": "actor",
            "description": "",
            "source_class": "",
            "participant_ref": actor_ref,
        }

    def participant(operation: dict[str, Any]) -> str:
        """BCE owner를 충돌 없는 안정적 lifeline alias에 한 번만 연결한다."""
        owner = operation["className"]
        if owner in class_aliases:
            return class_aliases[owner]
        candidate = _alias(owner)
        if candidate in participants:
            candidate = _alias(f"{owner}_{text(operation.get('stereotype')).title()}")
        suffix = 2
        alias = candidate
        while alias in participants:
            alias = f"{candidate}_{suffix}"
            suffix += 1
        class_aliases[owner] = alias
        participants[alias] = {
            "name": owner,
            "alias": alias,
            "kind": operation["stereotype"],
            "description": "",
            "source_class": owner,
            "participant_ref": text(operation.get("classStableId")),
        }
        if not participants[alias]["participant_ref"]:
            raise ValueError(f"accepted class has no stableId: {owner}")
        return alias

    messages: list[dict[str, Any]] = []
    stable_call_ids = {
        text(call.get("stableId")) or text(call.get("callId")) for call in calls
    }
    all_step_ids = {
        text(ref) for call in calls for ref in call.get("stepRefs") or []
    }

    def append(
        call_id: str,
        caller: str,
        inherited_fragments: list[dict[str, str]] | None = None,
    ) -> None:
        """한 call과 모든 자식을 기록한 다음 정확히 한 return을 닫는다."""
        call = call_by_id[call_id]
        operation = operations[text(call.get("receiverOperationId"))]
        call_ref = text(call.get("stableId"))
        operation_ref = text(operation.get("stableId"))
        callee = participant(operation)
        refs = [text(ref) for ref in call.get("stepRefs") or []]
        fragment_path = [dict(item) for item in inherited_fragments or []]
        for item in _fragment_path(refs, fragments):
            if all(existing["id"] != item["id"] for existing in fragment_path):
                fragment_path.append(item)
        arguments = [
            {
                "parameter": text(binding.get("parameter")),
                "type": _parameter_type(operation, text(binding.get("parameter"))),
                "source_kind": _argument_kind(
                    text(binding.get("sourceRef")), stable_call_ids, all_step_ids,
                ),
                "source_ref": text(binding.get("sourceRef")),
            }
            for binding in call.get("argumentBindings") or []
            if isinstance(binding, dict)
        ]
        actual_caller = caller or callee
        # 승인 operation signature와 binding provenance를 표시 형식으로만 투영한다.
        # source_kind는 sourceRef 문법에서 파생하며 새로운 값을 만들지 않는다.
        messages.append({
            "source": actual_caller,
            "target": callee,
            "label": _signature(operation),
            "type": "self" if actual_caller == callee else "sync",
            "fragments": fragment_path,
            "use_case_ids": [use_case_id],
            "step_ids": refs,
            "call_id": call_id,
            "reply_to": "",
            "arguments": arguments,
            "call_ref": call_ref,
            "operation_ref": operation_ref,
        })
        # 깊이 우선 순회는 activation stack과 같은 call/return 중첩을 자연스럽게 만든다.
        for child_id in children.get(call_id, []):
            # 조건부 호출의 자식은 그 호출이 실행될 때만 실행된다. 자식 자신의 stepRefs가
            # main과 extension을 함께 추적하더라도 부모의 조건 경로를 잃지 않게 전달한다.
            append(child_id, callee, fragment_path)
        messages.append({
            "source": callee,
            "target": actual_caller,
            "label": text(operation.get("returnType")) or "void",
            "type": "return",
            "fragments": fragment_path,
            "use_case_ids": [use_case_id],
            "step_ids": refs,
            "call_id": "",
            "reply_to": call_id,
            "arguments": [],
            "call_ref": call_ref,
            "operation_ref": operation_ref,
        })

    for root in roots:
        append(root, actor_alias)
    return messages, list(participants.values())


def _merge_diagram(
    diagrams: dict[str, dict[str, Any]],
    use_case_id: str,
    name: str,
    messages: list[dict[str, Any]],
    participants: list[dict[str, Any]],
) -> None:
    if use_case_id not in diagrams:
        diagrams[use_case_id] = {
            "use_case_id": use_case_id,
            "use_case_name": name,
            "Participants": participants,
            "Messages": messages,
            "UnresolvedSteps": [],
            "NarrativeSteps": [],
        }
        return
    current = diagrams[use_case_id]
    aliases = {text(item.get("alias")) for item in current["Participants"]}
    current["Participants"].extend(
        item for item in participants if text(item.get("alias")) not in aliases
    )
    current["Messages"].extend(messages)


def _scoped_include_collaboration(
    owner: str,
    collaboration: dict[str, Any],
) -> dict[str, Any] | None:
    """부모 collaboration에 내장된 include 단계만 child 다이어그램용으로 잘라낸다.

    child가 actor 없는 내부 include이면 독립 collaboration이 없다. 이때 child step을 가진
    call만 선택하고 call ID와 외부 sourceRef를 child 범위로 재매핑한다. 승인 모델 자체는
    수정하지 않는다.
    """
    selected = [
        call for call in collaboration.get("calls") or []
        if isinstance(call, dict)
        and any(text(ref).startswith(f"{owner}:") for ref in call.get("stepRefs") or [])
    ]
    if not selected:
        return None
    # This is a view of the accepted collaboration: call identity and source
    # provenance stay intact, while an omitted parent simply becomes a root.
    selected_ids = {text(call.get("callId")) for call in selected}
    calls: list[dict[str, Any]] = []

    for call in selected:
        old_id = text(call.get("callId"))
        parent_id = text(call.get("parentCallId"))
        parent = parent_id if parent_id in selected_ids else None
        bindings: list[dict[str, str]] = []
        for binding in call.get("argumentBindings") or []:
            if not isinstance(binding, dict):
                continue
            bindings.append({
                "parameter": text(binding.get("parameter")),
                "sourceRef": text(binding.get("sourceRef")),
            })
        calls.append({
            "callId": old_id,
            "stableId": text(call.get("stableId")) or None,
            "parentCallId": parent,
            "receiverOperationId": text(call.get("receiverOperationId")),
            "stepRefs": [
                text(ref) for ref in call.get("stepRefs") or []
                if text(ref).startswith(f"{owner}:")
            ],
            "argumentBindings": bindings,
        })
    return {
        "collaborationId": f"{owner}:scoped",
        "useCaseIds": [owner],
        "entryActor": None,
        "calls": calls,
    }


def _embed_extending_use_cases(
    index: ScenarioIndex,
    diagrams: dict[str, dict[str, Any]],
) -> None:
    """extend 다이어그램 메시지를 base의 anchor 뒤 ``opt`` fragment로 복사한다."""
    for relationship in index.relationships:
        if relationship.kind != "extend":
            continue
        base = diagrams.get(relationship.base_id)
        extension = diagrams.get(relationship.child_id)
        if base is None or extension is None:
            continue
        raw_relationships = (index.raw.get("relationships") or {}).get("extends") or []
        raw = next((
            item for item in raw_relationships if isinstance(item, dict)
            and text(item.get("base_use_case_id")) == relationship.base_id
            and text(item.get("extending_use_case_id")) == relationship.child_id
        ), {})
        condition = text(raw.get("condition"))
        if not condition:
            continue
        aliases = {text(item.get("alias")) for item in base["Participants"]}
        base["Participants"].extend(
            deepcopy(item) for item in extension["Participants"]
            if text(item.get("alias")) not in aliases
        )
        fragment = {
            "id": f"{relationship.base_id}:extend:{relationship.child_id}",
            "condition_ref": f"{relationship.base_id}:extend:{relationship.child_id}",
            "type": "opt",
            "branch": "main",
            "condition": condition,
        }
        # extension 자체 다이어그램은 유지한다. base에는 깊은 복사본만 삽입해 두
        # projection의 use_case_ids와 fragment path가 서로 영향을 주지 않게 한다.
        messages = deepcopy(extension["Messages"])
        for message in messages:
            message["fragments"] = [fragment, *(message.get("fragments") or [])]
            message["use_case_ids"] = [relationship.base_id]
        anchor_ids = set(relationship.anchor_step_ids)
        insertion = len(base["Messages"])
        if anchor_ids:
            positions = [
                position for position, message in enumerate(base["Messages"])
                if anchor_ids & {text(ref) for ref in message.get("step_ids") or []}
            ]
            if positions:
                insertion = max(positions) + 1
        base["Messages"][insertion:insertion] = messages


def project_sequence_model(
    index: ScenarioIndex,
    class_model: BCEModel,
    class_puml: str = "",
) -> SequenceCollection:
    """수락된 협업을 연산 선택이나 repair 없이 시퀀스로 투영한다.

    Args:
        index: 유스케이스 순서와 include/extend 관계를 제공하는 인덱스다.
        class_model: 연산과 협업이 모두 수락된 BCE 모델이다.
        class_puml: 투영 버전을 고정할 클래스 PlantUML 문자열이다.

    Returns:
        유스케이스 입력 순서대로 정렬된 시퀀스 컬렉션이다.

    Raises:
        ValueError: 협업이 없거나 유스케이스 하나를 결정론적으로 투영할 수 없는 경우다.

    Notes:
        이 함수는 LLM을 호출하지 않는다. 동일한 세 입력에는 메시지 순서와
        ``class_diagram_hash``까지 동일한 결과를 반환한다.
    """

    # 1. 저장 alias로 한 번 투영하고 operation ID catalog를 만든다. 이후 모든 message
    # label과 participant owner는 이 승인 catalog에서만 나온다.
    model_payload = class_model.model_dump(by_alias=True)
    operations = operation_catalog(model_payload)
    classes_by_name = {
        text(item.get("className")): item
        for item in model_payload.get("Classes") or [] if isinstance(item, dict)
    }
    for operation in operations.values():
        operation["classStableId"] = text(
            classes_by_name.get(text(operation.get("className")), {}).get("stableId")
        )
    collaborations = [
        item for item in model_payload.get("Collaborations") or [] if isinstance(item, dict)
    ]
    if not collaborations:
        raise ValueError("class model has no accepted Collaborations")
    fragments = _extension_fragments(index)
    step_positions = {
        step.id: step.order for use_case in index.use_cases for step in use_case.steps
    }
    # 2. 병렬 생성 완료 순서가 저장 message 순서에 새어 나오지 않도록 use case,
    # earliest step, collaboration ID 순으로 고정한다.
    collaborations.sort(key=lambda collaboration: (
        id_key(text((collaboration.get("useCaseIds") or [""])[0])),
        min(
            (
                step_positions.get(text(ref), 10**9)
                for call in collaboration.get("calls") or [] if isinstance(call, dict)
                for ref in call.get("stepRefs") or []
            ),
            default=10**9,
        ),
        id_key(text(collaboration.get("collaborationId"))),
    ))
    diagrams: dict[str, dict[str, Any]] = {}
    # 3. collaboration의 첫 useCaseId가 다이어그램 소유자다. 같은 owner의 여러 actor
    # slice는 participant를 deduplicate하고 message를 정렬된 순서로 이어 붙인다.
    for collaboration in collaborations:
        scope = [text(value) for value in collaboration.get("useCaseIds") or []]
        if not scope:
            raise ValueError("collaboration has no useCaseIds")
        owner = scope[0]
        messages, participants = _project_collaboration(
            collaboration, operations, owner, fragments.get(owner, {}),
            index.use_case(owner).primary_actor_ref,
        )
        _merge_diagram(
            diagrams, owner, index.use_case(owner).name, messages, participants,
        )
    # 4. actor 없는 include는 부모 collaboration에 내장되어 있으므로 child step 범위만
    # 잘라 독립 child 다이어그램을 복원한다.
    for use_case in index.use_cases:
        if use_case.id in diagrams:
            continue
        source = next((
            collaboration for collaboration in collaborations
            if use_case.id in [text(value) for value in collaboration.get("useCaseIds") or []][1:]
        ), None)
        scoped = _scoped_include_collaboration(use_case.id, source) if source else None
        if scoped:
            messages, participants = _project_collaboration(
                scoped, operations, use_case.id, fragments.get(use_case.id, {}),
            )
            _merge_diagram(diagrams, use_case.id, use_case.name, messages, participants)
    missing = [use_case.id for use_case in index.use_cases if use_case.id not in diagrams]
    if missing:
        raise ValueError("missing accepted collaboration projection for " + ", ".join(missing))
    # 5. 모든 use case가 투영된 뒤 extend를 base anchor에 삽입한다. 먼저 합치면 아직 없는
    # participant나 child diagram 때문에 입력 순서에 따라 결과가 달라질 수 있다.
    _embed_extending_use_cases(index, diagrams)
    ordered = [diagrams[use_case.id] for use_case in index.use_cases]
    return SequenceCollection(
        Diagrams=[UseCaseSequence.model_validate(diagram) for diagram in ordered],
        class_diagram_hash=hashlib.sha256(class_puml.encode("utf-8")).hexdigest(),
        MethodProposals=[],
    )


def sequence_findings(model: SequenceCollection | dict[str, Any]) -> list[str]:
    """repair를 시작하지 않고 현재 컬렉션의 최소 참조 계약 위반을 반환한다.

    Args:
        model: typed 컬렉션 또는 같은 JSON 모양이다.

    Returns:
        schema 오류, call/return 불일치와 미선언 participant 참조 메시지 목록이다.

    Notes:
        이 함수는 현재 sequence stage가 만든 projection의 최소 계약만 검사한다.
    """

    try:
        parsed = model if isinstance(model, SequenceCollection) else SequenceCollection.model_validate(model)
    except Exception as error:
        return [str(error)]
    findings: list[str] = []
    for diagram in parsed.Diagrams:
        calls = [message.call_id for message in diagram.Messages if message.call_id]
        replies = [message.reply_to for message in diagram.Messages if message.type == "return"]
        if Counter(replies) != Counter(calls) or len(calls) != len(set(calls)):
            findings.append(
                f"{diagram.use_case_id}: every call requires exactly one matching return"
            )
        participant_aliases = {participant.alias for participant in diagram.Participants}
        for message in diagram.Messages:
            if message.source not in participant_aliases or message.target not in participant_aliases:
                findings.append(
                    f"{diagram.use_case_id}: message references an undeclared participant"
                )
                break
    return findings
