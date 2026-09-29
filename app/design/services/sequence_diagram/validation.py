"""현재 시퀀스 컬렉션과 legacy 단일 다이어그램을 같은 보고서 계약으로 검증한다.

입력은 저장 시퀀스 JSON과 use-case/class 산출물이 든 read-only state다. 출력은
``app.validation.ValidationReport``이며 검사 함수는 모델을 수정하거나 LLM을 호출하지
않는다. 등록된 ``CheckSpec`` 순서가 finding 순서이므로 병렬 실행 여부와 무관하게 UI와
repair 입력이 안정적이다.

새 deterministic projection은 ``SEQUENCE_COLLECTION_CHECKS``의 작고 엄격한 계약을 쓴다.
``SEQUENCE_CHECKS``는 이전 단일 다이어그램 체크포인트와 downstream API 검증을 위한
호환 lane이다. 어느 lane도 service나 renderer를 역참조해 재생성을 시작하지 않는다.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from app.design.knowledge import rules
from app.design.services.class_diagram.type_system import (
    structured_field_types,
    types_compatible,
)
from app.design.services.class_diagram.validation.diagram import _class_method_signatures
from app.design.services.class_diagram.validation.model import (
    operation_catalog,
    optional_inner_type,
)
from app.design.services.class_diagram.scenario import ScenarioIndex, build_scenario_index
from app.design.services.sequence_diagram.methods import (
    method_call_signature,
    method_name,
    method_return_type,
    normalize_return_type,
)
from app.design.services.sequence_diagram.projection import (
    sequence_findings as interaction_sequence_findings,
)
from app.validation import (
    CheckSpec,
    FindingOrigin,
    ValidationReport,
    run_checks,
)
from app.validation import Finding as ValidationFinding


def _known_use_case_ids_from_state(state: dict[str, Any]) -> set[str]:
    scenario = state.get("usecase_spec") or {}
    if not isinstance(scenario, dict):
        return set()
    return {
        str(item.get("id")).strip()
        for item in scenario.get("use_cases") or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
class Finding(ValidationFinding):
    """기존 UI가 요구하는 rule tag 표시를 보존한 typed finding이다."""

    def __init__(
        self,
        rule_id: str,
        message: str,
        location: str | None = None,
        requires_user_input: bool = False,
        origin: FindingOrigin = "deterministic",
        **data: Any,
    ) -> None:
        super().__init__(
            rule_id=rule_id,
            message=message,
            location=location,
            requires_user_input=requires_user_input,
            origin=origin,
            **data,
        )

    def as_issue(self) -> str:
        head = f"{self.location}: {self.message}" if self.location else self.message
        return f"{head} {rules.tag_of(self.rule_id)}"


def _findings_from_report(report: ValidationReport) -> list[Finding]:
    if report.errors:
        raise RuntimeError("; ".join(report.errors))
    return [Finding.model_validate(finding) for finding in report.findings]


# 아래 detector는 legacy 단일 다이어그램과 API downstream이 공유한다. 함수마다 자기
# rule_id의 finding만 만들고 mutation/repair는 수행하지 않는다.
def _class_names_from_puml(state: dict) -> set[str]:
    return set(re.findall(r"(?m)^\s*class\s+([A-Za-z_]\w*)\b", state.get("class_diagram_puml", "")))


def _known_use_case_ids(state: dict) -> set[str]:
    return _known_use_case_ids_from_state(state)


def _known_flow_step_ids(state: dict) -> set[str]:
    """Return canonical flow refs from the shared scenario index."""
    index = _scenario_index(state)
    return set(index.step_ids) if index else set()


def _scenario_index(state: dict) -> ScenarioIndex | None:
    raw = state.get("usecase_spec") or {}
    if not isinstance(raw, dict):
        return None
    try:
        return build_scenario_index(raw)
    except (TypeError, ValueError, KeyError):
        return None


def _extension_records(state: dict, use_case_id: str | None = None) -> list[dict[str, Any]]:
    """Project extension identity and display metadata from canonical Steps."""
    index = _scenario_index(state)
    if index is None:
        return []
    records: dict[str, dict[str, Any]] = {}
    for use_case in index.use_cases:
        if use_case_id and use_case.id != use_case_id:
            continue
        for step in use_case.steps:
            if not step.extension_ref:
                continue
            match = re.fullmatch(
                rf"{re.escape(use_case.id)}:extension:(\d+):(\d+)",
                step.extension_ref,
            )
            if not match:
                continue
            record = records.setdefault(step.extension_ref, {
                "use_case_id": use_case.id,
                "extension_ref": step.extension_ref,
                "branch_step": int(match.group(1)),
                "label": step.branch,
                "condition": step.condition,
                "step_ids": set(),
            })
            record["step_ids"].add(step.id)
    return [records[key] for key in sorted(records)]


def _flow_step_sentence(step: dict) -> str:
    return str(step.get("sentence") or step.get("description") or "").strip()


def _is_unresolved_step(step: dict) -> bool:
    if str(step.get("status") or "").strip().lower() == "unresolved":
        return True
    sentence = _flow_step_sentence(step).lower()
    return any(
        marker in sentence
        for marker in ("todo", "tbd", "what do we do", "to be decided", "미정", "결정 필요")
    )


def _unresolved_flow_step_ids(state: dict) -> set[str]:
    index = _scenario_index(state)
    if index is None:
        return set()
    known_ids = set(index.step_ids)
    unresolved: set[str] = set()
    specifications = {
        str(item.get("use_case_id") or "").strip(): item
        for item in (state.get("usecase_spec") or {}).get("use_case_specs") or []
        if isinstance(item, dict) and str(item.get("use_case_id") or "").strip()
    }
    for use_case in index.use_cases:
        specification = specifications.get(use_case.id, {})
        for raw_step in specification.get("main_scenario") or []:
            if not isinstance(raw_step, dict) or raw_step.get("step_number") is None:
                continue
            step_id = f"{use_case.id}:main:{raw_step['step_number']}"
            if step_id in known_ids and _is_unresolved_step(raw_step):
                unresolved.add(step_id)
        ordinals: dict[str, int] = {}
        for extension in specification.get("extensions") or []:
            if not isinstance(extension, dict):
                continue
            anchor = str(extension.get("branch_step") or "").strip()
            if not anchor:
                continue
            ordinals[anchor] = ordinals.get(anchor, 0) + 1
            extension_ref = f"{use_case.id}:extension:{anchor}:{ordinals[anchor]}"
            for raw_step in extension.get("handling_steps") or []:
                if not isinstance(raw_step, dict) or raw_step.get("sub_step") is None:
                    continue
                step_id = f"{extension_ref}:{raw_step['sub_step']}"
                if step_id in known_ids and _is_unresolved_step(raw_step):
                    unresolved.add(step_id)
    return unresolved


def _message_fragments(message: dict) -> list[dict]:
    """Read explicit fragment identities; legacy condition text cannot mint an ID."""
    fragments = message.get("fragments")
    if isinstance(fragments, list):
        return [item for item in fragments if isinstance(item, dict)]
    group = str(message.get("group") or "").strip()
    condition = str(message.get("condition") or "").strip()
    if not group and not condition:
        return []
    return [{"id": "", "type": group, "branch": "main", "condition": condition}]


def _participant_id(participant: dict) -> str:
    """메시지가 참조하는 참가자 ID. 옛 저장본은 name을 alias로 간주한다."""
    return str(participant.get("alias") or participant.get("name") or "").strip()


def _class_methods_from_state(state: dict) -> dict[str, set[str]]:
    """BCE 모델에서 클래스별 메서드 집합을 뽑는다.

    메서드 이름은 비교를 위해 **괄호와 앞뒤 공백을 벗긴** 형태로 정규화한다.
    BCE 모델의 `methods`는 `list[str]`이고, 각 원소는 `"registerMember()"` 같은
    자유 텍스트다. 시퀀스 메시지의 `label`도 같은 형태이므로 정규화된 이름으로
    대조한다.
    """
    classes = (state.get("extracted_bce_classes") or {}).get("Classes", [])
    result: dict[str, set[str]] = {}
    for c in classes:
        name = c.get("className")
        if not name:
            continue
        methods: set[str] = set()
        for m in _class_method_signatures(c):
            normalized = _normalize_method_name(str(m).strip())
            if normalized:
                methods.add(normalized)
        result[name] = methods
    return result


def _normalize_method_name(raw: str) -> str:
    """메서드 이름을 비교 가능한 형태로 정규화한다.

    BCE 모델의 메서드(`"+ registerMember(name: String): void"`)와 시퀀스 메시지의
    라벨(`"registerMember()"`)을 대조하려면 양쪽을 같은 형태로 만들어야 한다.
    가시성 기호(`+`, `-`, `#`, `~`)와 반환 타입(`: Type`), 매개변수 목록 안의
    내용을 전부 벗기고, 메서드 **이름만** 소문자로 남긴다.
    """
    # 가시성 기호 제거
    raw = re.sub(r'^[+\-#~]\s*', '', raw)
    # 괄호 이전의 이름만 추출
    match = re.match(r'([A-Za-z_]\w*)', raw)
    return match.group(1).lower() if match else raw.lower().strip()


def sequence_participants(model: dict, state: dict) -> list[Finding]:
    declared = {_participant_id(item) for item in model.get("Participants", []) if _participant_id(item)}
    found: list[Finding] = []
    for message in model.get("Messages", []):
        source, target = str(message.get("source", "")).strip(), str(message.get("target", "")).strip()
        label = f"{source} -> {target}".strip()
        if source not in declared:
            found.append(Finding("sequence.message-participants-exist", f"source '{source}'가 Participants에 없음", label))
        if target not in declared:
            found.append(Finding("sequence.message-participants-exist", f"target '{target}'가 Participants에 없음", label))
    return found


def sequence_bce_flow(model: dict, state: dict) -> list[Finding]:
    kinds = {_participant_id(item): str(item.get("kind", "")).strip().lower() for item in model.get("Participants", [])}
    allowed = {
        ("actor", "boundary"),
        ("boundary", "control"),
        ("control", "boundary"),
        ("control", "control"),
        ("control", "entity"),
        ("control", "database"),
        ("entity", "entity"),
        ("entity", "database"),
    }
    found: list[Finding] = []
    for message in model.get("Messages", []):
        if str(message.get("type", "sync")).lower() in {"return", "activate", "deactivate"}:
            continue
        source, target = str(message.get("source", "")).strip(), str(message.get("target", "")).strip()
        source_kind, target_kind = kinds.get(source), kinds.get(target)
        if source == target and source_kind and source_kind != "actor":
            continue
        if source_kind and target_kind and (source_kind, target_kind) not in allowed:
            found.append(Finding("sequence.message-bce-flow", f"{source_kind} → {target_kind} 호출은 BCE 흐름을 위반함", f"{source} -> {target}"))
    return found


def sequence_traceability(model: dict, state: dict) -> list[Finding]:
    classes, use_cases = _class_names_from_puml(state), _known_use_case_ids(state)
    flow_steps = _known_flow_step_ids(state)
    found: list[Finding] = []
    for participant in model.get("Participants", []):
        source_class, name = str(participant.get("source_class", "")).strip(), str(participant.get("name", "?")).strip()
        if source_class and source_class not in classes:
            found.append(Finding("sequence.references-exist", f"클래스 다이어그램에 없는 source_class '{source_class}'", name))
    if use_cases:
        for message in model.get("Messages", []):
            for use_case in message.get("use_case_ids", []):
                if use_case and use_case not in use_cases:
                    found.append(Finding("sequence.references-exist", f"입력에 없는 유스케이스 id '{use_case}'", f"{message.get('source', '?')} -> {message.get('target', '?')}"))
            for step_id in message.get("step_ids", []):
                if step_id and flow_steps and step_id not in flow_steps:
                    found.append(Finding("sequence.references-exist", f"입력에 없는 흐름 단계 id '{step_id}'", f"{message.get('source', '?')} -> {message.get('target', '?')}"))
    return found


def sequence_participant_classes(model: dict, state: dict) -> list[Finding]:
    """비-액터 참가자가 클래스 다이어그램에 실재하는 클래스인가.

    `sequence_traceability`는 추적 필드(`source_class`)만 본다 — 그 필드가 비어 있으면
    지적하지 않는다. 그런데 참가자 `name` 자체가 클래스 다이어그램에 없는 이름이면,
    그 참가자에 매달린 메시지가 전부 유령 상호작용이 된다. 여기서 잡는다.

    액터는 유스케이스 명세에서 오므로 클래스 목록에 없는 것이 정상이다 — 건너뛴다.
    """
    rule_id = "sequence.participant-classes-exist"
    classes = _class_names_from_puml(state)
    if not classes:
        return []  # 클래스 다이어그램이 없으면 대조할 것이 없다

    found: list[Finding] = []
    for participant in model.get("Participants", []):
        kind = str(participant.get("kind", "")).strip().lower()
        if kind == "actor":
            continue
        name = str(participant.get("name", "")).strip()
        if not name:
            continue
        # source_class가 있으면 그것으로 대조, 없으면 name으로 대조
        class_ref = str(participant.get("source_class", "")).strip() or name
        if class_ref not in classes:
            found.append(
                Finding(rule_id, f"클래스 다이어그램에 없는 참가자 '{name}' (대응 클래스 '{class_ref}')", name)
            )
    return found


def sequence_message_methods(model: dict, state: dict) -> list[Finding]:
    """메시지 라벨이 target 클래스의 실제 메서드인가.

    시퀀스 다이어그램의 메시지 라벨은 "호출되는 오퍼레이션"이다. 클래스 다이어그램에서
    해당 클래스의 `methods`에 정의되지 않은 오퍼레이션을 호출하면 설계가 불일치한다.

    라벨이 비어 있으면 건너뛴다 — 라벨 없는 메시지는 이름을 안 단 것이지 없는 메서드를
    부른 것이 아니다. return 타입 메시지도 건너뛴다 — 응답은 호출이 아니다.

    대조는 **전체 호출 시그니처 수준**이다. BCE 모델의 메서드가
    `"registerMember(name: String): void"`이면 메시지도 `"registerMember(name: String)"`
    이어야 한다. 반환 타입과 가시성만 비교에서 제외한다.
    """
    rule_id = "sequence.message-labels-match-methods"
    classes = (state.get("extracted_bce_classes") or {}).get("Classes", [])
    class_methods = {
        str(class_item.get("className")): {
            signature
            for raw_method in _class_method_signatures(class_item)
            if (signature := method_call_signature(str(raw_method)))
        }
        for class_item in classes
        if class_item.get("className")
    }
    if not class_methods:
        return []  # BCE 모델이 없으면 대조할 것이 없다

    # Semantic class identity must come from the explicit traceability reference.
    # A participant's display name or alias is not evidence of its class.
    participant_to_class: dict[str, str] = {}
    participants_without_class: set[str] = set()
    for participant in model.get("Participants", []):
        name = _participant_id(participant)
        kind = str(participant.get("kind", "")).strip().lower()
        if kind == "actor" or not name:
            continue
        class_ref = str(participant.get("source_class", "")).strip()
        if class_ref:
            participant_to_class[name] = class_ref
        else:
            participants_without_class.add(name)

    found: list[Finding] = []
    for message in model.get("Messages", []):
        if str(message.get("type", "sync")).lower() not in {"sync", "async", "self"}:
            continue
        label = str(message.get("label", "")).strip()
        if not label:
            continue
        target = str(message.get("target", "")).strip()
        source = str(message.get("source", "")).strip()
        target_class = participant_to_class.get(target)
        if not target_class:
            if target in participants_without_class:
                location = f"{source} -> {target} : {label}"
                found.append(Finding(
                    rule_id,
                    f"'{target}' 참가자에 source_class가 없어 '{label}' 메서드를 검증할 수 없음",
                    location,
                ))
            continue  # 액터이거나 매핑이 없다 — 다른 검출기가 잡는다

        methods = class_methods.get(target_class)
        if methods is None:
            continue  # 클래스 자체가 BCE에 없다 — participant_classes 검출기가 잡는다

        normalized_label = method_call_signature(label)
        if normalized_label and normalized_label not in methods:
            location = f"{source} -> {target} : {label}"
            found.append(
                Finding(
                    rule_id,
                    f"'{target_class}' 클래스에 '{label}' 메서드가 정의되어 있지 않음",
                    location,
                )
            )
    return found

def sequence_initial_entry(model: dict, state: dict) -> list[Finding]:
    """첫 번째 메시지는 반드시 Actor → Boundary 호출이어야 함.

    사용자가 시스템에 접근할 때 Control이나 Entity로 직접 진입하는 잘못된 상호작용을
    방지한다. 첫 번째 비-return 메시지를 기준으로 판정한다.
    """
    rule_id = "sequence.initial-message-entry"
    kinds = {
        _participant_id(p): str(p.get("kind", "")).strip().lower()
        for p in model.get("Participants", [])
    }

    first_msg = None
    for msg in model.get("Messages", []):
        if str(msg.get("type", "sync")).lower() not in {"return", "activate", "deactivate"}:
            first_msg = msg
            break

    if not first_msg:
        return []

    source = str(first_msg.get("source", "")).strip()
    target = str(first_msg.get("target", "")).strip()
    source_kind = kinds.get(source, "")
    target_kind = kinds.get(target, "")

    if source_kind != "actor" or target_kind != "boundary":
        location = f"{source} -> {target}"
        return [
            Finding(
                rule_id,
                f"시퀀스 다이어그램의 최초 호출은 Actor → Boundary이어야 함 (현재: {source_kind or source} → {target_kind or target})",
                location,
            )
        ]
    return []


def _uses_explicit_call_links(model: dict) -> bool:
    return any(
        "call_id" in message or "reply_to" in message
        for message in model.get("Messages", [])
        if isinstance(message, dict)
    )


def _explicit_calls(model: dict) -> dict[str, tuple[int, dict]]:
    return {
        str(message.get("call_id") or "").strip(): (index, message)
        for index, message in enumerate(model.get("Messages", []))
        if str(message.get("type", "sync")).lower() in {"sync", "async", "self"}
        and str(message.get("call_id") or "").strip()
    }


def sequence_call_return_links(model: dict, state: dict) -> list[Finding]:
    """새 모델의 호출 ID와 반환 reply_to가 정확히 한 호출을 연결하는가."""
    if not _uses_explicit_call_links(model):
        return []
    rule_id = "sequence.call-return-links"
    found: list[Finding] = []
    calls: dict[str, tuple[int, dict]] = {}
    reply_counts: dict[str, int] = {}
    for index, message in enumerate(model.get("Messages", [])):
        message_type = str(message.get("type", "sync")).strip().lower()
        source = str(message.get("source") or "").strip()
        target = str(message.get("target") or "").strip()
        location = f"{source} -> {target} : {message.get('label', '')}"
        if message_type in {"sync", "async", "self"}:
            call_id = str(message.get("call_id") or "").strip()
            if str(message.get("reply_to") or "").strip():
                found.append(Finding(rule_id, "호출 메시지에는 reply_to를 지정할 수 없음", location))
            if not call_id:
                found.append(Finding(rule_id, "호출 메시지의 call_id가 비어 있음", location))
            elif call_id in calls:
                found.append(Finding(rule_id, f"call_id '{call_id}'가 중복됨", location))
            else:
                calls[call_id] = (index, message)
        elif message_type == "return":
            reply_to = str(message.get("reply_to") or "").strip()
            if str(message.get("call_id") or "").strip():
                found.append(Finding(rule_id, "return 메시지에는 call_id를 지정할 수 없음", location))
            if not reply_to:
                found.append(Finding(rule_id, "return 메시지의 reply_to가 비어 있음", location))
                continue
            linked = calls.get(reply_to)
            if linked is None:
                found.append(Finding(rule_id, f"선행 호출 ID '{reply_to}'가 존재하지 않음", location))
                continue
            _, call = linked
            reply_counts[reply_to] = reply_counts.get(reply_to, 0) + 1
            if reply_counts[reply_to] > 1:
                found.append(Finding(rule_id, f"호출 '{reply_to}'에 반환이 둘 이상 연결됨", location))
            if (
                str(call.get("source") or "").strip() != target
                or str(call.get("target") or "").strip() != source
            ):
                found.append(Finding(rule_id, f"호출 '{reply_to}'과 반환 방향이 일치하지 않음", location))
    return found


def _declared_control_boundary_gateways(state: dict) -> set[tuple[str, str]]:
    """BCE 계약에서 Control → 외부 Boundary dependency를 가져온다.

    Boundary가 항상 presentation endpoint인 것은 아니다. 외부 API, identity provider,
    device adapter도 Boundary이며 class diagram이 dependency를 선언했다면 Control이 그
    operation을 호출하는 것이 맞다. 모든 Boundary method를 actor input으로 보면 이 유효한
    integration call과 Control이 UI를 역호출하는 오류를 구분할 수 없다.
    """
    pairs = {
        (str(item.get("source") or "").strip(), str(item.get("target") or "").strip())
        for item in (state.get("extracted_bce_classes") or {}).get("Relationships", []) or []
        if isinstance(item, dict)
        and str(item.get("source") or "").strip()
        and str(item.get("target") or "").strip()
    }
    return pairs


def sequence_boundary_operation_direction(model: dict, state: dict) -> list[Finding]:
    """Boundary 호출이 입력/출력 방향의 소유권을 지키는지 검사한다."""
    if isinstance(
        (state.get("extracted_bce_classes") or {}).get("Collaborations"), list,
    ) and (state.get("extracted_bce_classes") or {}).get("Collaborations"):
        # 저장된 collaboration에 actor entry와 위임 방향이 있으면 그 값을 검사 기준으로 쓴다.
        # method 이름 prefix 판정은 collaboration이 없는 과거 모델에만 적용한다.
        return []
    rule_id = "sequence.boundary-operation-direction"
    kinds = {
        _participant_id(item): str(item.get("kind", "")).strip().lower()
        for item in model.get("Participants", [])
    }
    classes = {
        _participant_id(item): str(
            item.get("source_class") or item.get("name") or ""
        ).strip()
        for item in model.get("Participants", [])
        if isinstance(item, dict)
    }
    gateway_pairs = _declared_control_boundary_gateways(state)
    found: list[Finding] = []
    for message in model.get("Messages", []):
        if str(message.get("type", "sync")).lower() not in {"sync", "async"}:
            continue
        source = str(message.get("source") or "").strip()
        target = str(message.get("target") or "").strip()
        if kinds.get(source) != "actor" or kinds.get(target) != "boundary":
            if kinds.get(source) != "control" or kinds.get(target) != "boundary":
                continue
            if (classes.get(source, ""), classes.get(target, "")) in gateway_pairs:
                continue
            signature = method_call_signature(str(message.get("label") or ""))
            found.append(
                Finding(
                    rule_id,
                    f"Control이 Boundary 입력 오퍼레이션 '{signature}'을 출력처럼 호출함",
                    f"{source} -> {target} : {message.get('label', '')}",
                )
            )
            continue
        # Actor-to-boundary direction is not encoded by an English method name.
    return found


def sequence_unmatched_returns(model: dict, state: dict) -> list[Finding]:
    """소비할 선행 호출 없이 독립적으로 존재하는 return 메시지 감지.

    하나의 호출은 최대 하나의 return만 소비할 수 있다. 호출을 반환 시점에 제거하여
    선행 호출 없는 반환뿐 아니라 한 호출에 여러 반환이 붙는 LLM 환각도 차단한다.
    """
    if _uses_explicit_call_links(model):
        return []
    rule_id = "sequence.unmatched-return-message"
    found: list[Finding] = []
    pending_calls: list[tuple[str, str]] = []

    for msg in model.get("Messages", []):
        m_type = str(msg.get("type", "sync")).lower()
        source = str(msg.get("source", "")).strip()
        target = str(msg.get("target", "")).strip()

        if m_type == "return":
            call_index = next(
                (
                    index
                    for index in range(len(pending_calls) - 1, -1, -1)
                    if pending_calls[index] == (target, source)
                ),
                None,
            )
            if call_index is None:
                location = f"{source} --> {target}"
                found.append(
                    Finding(
                        rule_id,
                        f"선행 호출 없이 고립된 return 메시지 ({source} → {target})",
                        location,
                    )
                )
            else:
                pending_calls.pop(call_index)
        elif m_type in {"sync", "async", "self"}:
            pending_calls.append((source, target))

    return found


def sequence_async_returns(model: dict, state: dict) -> list[Finding]:
    """fire-and-forget 비동기 호출에 연결된 반환 메시지를 검출한다."""
    rule_id = "sequence.async-call-has-no-return"
    found: list[Finding] = []
    if _uses_explicit_call_links(model):
        calls = _explicit_calls(model)
        for message in model.get("Messages", []):
            if str(message.get("type", "")).lower() != "return":
                continue
            linked = calls.get(str(message.get("reply_to") or "").strip())
            if linked is None:
                continue
            call = linked[1]
            if str(call.get("type", "sync")).lower() == "async":
                found.append(
                    Finding(
                        rule_id,
                        f"비동기 호출 '{call.get('label', '')}'은 반환 메시지를 가질 수 없음",
                        f"{message.get('source', '')} --> {message.get('target', '')}",
                    )
                )
        return found
    pending_calls: list[dict] = []
    for message in model.get("Messages", []):
        message_type = str(message.get("type", "sync")).strip().lower()
        source = str(message.get("source") or "").strip()
        target = str(message.get("target") or "").strip()
        if message_type in {"sync", "async", "self"}:
            pending_calls.append(message)
            continue
        if message_type != "return":
            continue
        call_index = next(
            (
                index
                for index in range(len(pending_calls) - 1, -1, -1)
                if str(pending_calls[index].get("source") or "").strip() == target
                and str(pending_calls[index].get("target") or "").strip() == source
            ),
            None,
        )
        if call_index is None:
            continue
        call = pending_calls.pop(call_index)
        if str(call.get("type", "sync")).strip().lower() == "async":
            found.append(
                Finding(
                    rule_id,
                    f"비동기 호출 '{call.get('label', '')}'은 반환 메시지를 가질 수 없음",
                    f"{source} --> {target}",
                )
            )
    return found


def sequence_return_values_match_methods(model: dict, state: dict) -> list[Finding]:
    """반환 라벨이 대응 호출 메서드의 클래스 선언 반환 타입과 같은가."""
    rule_id = "sequence.return-label-matches-method-return"
    participant_classes = {
        _participant_id(participant): str(
            participant.get("source_class") or participant.get("name") or ""
        ).strip()
        for participant in model.get("Participants", [])
        if str(participant.get("kind", "")).strip().lower() != "actor"
    }
    signatures: dict[str, dict[str, set[str]]] = {}
    for class_item in (state.get("extracted_bce_classes") or {}).get("Classes", []):
        owner_name = str(class_item.get("className") or "").strip()
        if not owner_name:
            continue
        by_method: dict[str, set[str]] = {}
        for raw_method in _class_method_signatures(class_item):
            signature = method_call_signature(str(raw_method))
            if not signature:
                continue
            return_type = method_return_type(str(raw_method))
            if return_type:
                by_method.setdefault(signature, set()).add(return_type)
            else:
                by_method.setdefault(signature, set())
        signatures[owner_name] = by_method

    explicit = _uses_explicit_call_links(model)
    calls_by_id = _explicit_calls(model) if explicit else {}
    pending_calls: list[dict] = []
    found: list[Finding] = []
    for message in model.get("Messages", []):
        message_type = str(message.get("type", "sync")).strip().lower()
        source = str(message.get("source") or "").strip()
        target = str(message.get("target") or "").strip()
        if not explicit and message_type in {"sync", "self"}:
            pending_calls.append(message)
            continue
        if message_type != "return":
            continue

        label = str(message.get("label") or "").strip()
        location = f"{source} --> {target} : {label or '<empty>'}"
        if not label:
            found.append(Finding(rule_id, "return 메시지의 결과 라벨이 비어 있음", location))
            continue

        if explicit:
            linked = calls_by_id.get(str(message.get("reply_to") or "").strip())
            if linked is None:
                continue
            call = linked[1]
        else:
            call_index = next(
                (
                    index
                    for index in range(len(pending_calls) - 1, -1, -1)
                    if str(pending_calls[index].get("source") or "").strip() == target
                    and str(pending_calls[index].get("target") or "").strip() == source
                ),
                None,
            )
            if call_index is None:
                continue  # 고립 반환은 sequence_unmatched_returns가 맡는다
            call = pending_calls.pop(call_index)
        class_name = participant_classes.get(str(call.get("target") or "").strip())
        called_method = method_call_signature(str(call.get("label") or ""))
        if not class_name or not called_method:
            continue
        class_signatures = signatures.get(class_name)
        if class_signatures is None or called_method not in class_signatures:
            continue  # 참가자/메서드 소유권 검출기가 맡는다

        declared = class_signatures[called_method]
        return_types = declared or {"void"}
        if normalize_return_type(label) not in {
            normalize_return_type(return_type) for return_type in return_types
        }:
            found.append(
                Finding(
                    rule_id,
                    f"return 라벨 '{label}'이 '{class_name}.{call.get('label', '')}'의 "
                    f"반환 타입 {sorted(return_types)}와 일치하지 않음",
                    location,
                )
            )
    return found


def sequence_calls_have_returns(model: dict, state: dict) -> list[Finding]:
    """모든 sync/self call에 정확히 하나의 명시적 return message가 있는지 검사한다."""
    rule_id = "sequence.call-requires-return"
    explicit = _uses_explicit_call_links(model)
    if explicit:
        returned_ids = {
            str(message.get("reply_to") or "").strip()
            for message in model.get("Messages", [])
            if str(message.get("type", "")).lower() == "return"
        }
        pending_calls = [
            message
            for message in model.get("Messages", [])
            if str(message.get("type", "sync")).lower() in {"sync", "self"}
            and str(message.get("call_id") or "").strip() not in returned_ids
        ]
    else:
        pending_calls = []
        for message in model.get("Messages", []):
            message_type = str(message.get("type", "sync")).strip().lower()
            source = str(message.get("source") or "").strip()
            target = str(message.get("target") or "").strip()
            if message_type in {"sync", "self"}:
                pending_calls.append(message)
                continue
            if message_type != "return":
                continue
            call_index = next(
                (
                    index
                    for index in range(len(pending_calls) - 1, -1, -1)
                    if str(pending_calls[index].get("source") or "").strip() == target
                    and str(pending_calls[index].get("target") or "").strip() == source
                ),
                None,
            )
            if call_index is not None:
                pending_calls.pop(call_index)

    found: list[Finding] = []
    for call in pending_calls:
        target = str(call.get("target") or "").strip()
        source = str(call.get("source") or "").strip()
        found.append(
            Finding(
                rule_id,
                "Synchronous/self call has no matching return message",
                f"{source} -> {target} : {call.get('label', '')}",
            )
        )
    return found


def _method_parameters(signature: str) -> dict[str, str]:
    inside = signature.partition("(")[2].rpartition(")")[0]
    if not inside:
        return {}
    values: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(inside):
        if character == "<":
            depth += 1
        elif character == ">":
            depth = max(0, depth - 1)
        elif character == "," and depth == 0:
            values.append(inside[start:index])
            start = index + 1
    values.append(inside[start:])
    result: dict[str, str] = {}
    for value in values:
        name, separator, type_name = value.partition(":")
        if separator and name and type_name:
            result[name] = type_name
    return result


def _stable_field_type(
    root_type: str, path: str, class_model: dict[str, Any],
) -> str:
    """Resolve an aligned Class/DataType fieldRef path without name fallback."""
    current = root_type
    for field_ref in filter(None, path.split(".")):
        owner = next((
            item for item in [
                *(class_model.get("Classes") or []),
                *(class_model.get("DataTypes") or []),
            ]
            if isinstance(item, dict)
            and types_compatible(
                str(item.get("className") or item.get("name") or ""), current,
            )
        ), None)
        if owner is None:
            return ""
        refs = owner.get("fieldRefs") or []
        fields = owner.get("fields") or []
        if len(refs) != len(fields):
            return ""
        try:
            current = str(fields[list(refs).index(field_ref)]).rpartition(":")[2].strip()
        except ValueError:
            return ""
        if not current:
            return ""
    return current


def _stable_source_type(
    source_ref: str, catalog: dict[str, dict[str, Any]], class_model: dict[str, Any],
) -> str:
    source_id, separator, value = source_ref.partition("#")
    if not separator:
        return ""
    call = catalog.get(source_id)
    if call is None:
        return ""
    operation = call["operation"]
    if value == "result" or value.startswith("result."):
        result = str(operation.get("returnType") or "")
        path = value.removeprefix("result.")
        if path == "unwrap" or path.startswith("unwrap."):
            result = optional_inner_type(result)
            path = path.removeprefix("unwrap.")
            if not result:
                return ""
        return _stable_field_type(result, path, class_model) if path else result
    parameter_ref, dot, path = value.partition(".")
    parameter = next((
        item for item in operation.get("parameters") or []
        if isinstance(item, dict) and str(item.get("stableRef") or "") == parameter_ref
    ), None)
    if parameter is None:
        return ""
    result = str(parameter.get("type") or "")
    return _stable_field_type(result, path, class_model) if dot else result


def sequence_argument_data_flow(model: dict, state: dict) -> list[Finding]:
    """새 호출 모델의 매개변수 타입과 값 출처가 선행 데이터 흐름에 근거하는가."""
    if not _uses_explicit_call_links(model):
        return []
    rule_id = "sequence.argument-data-flow"
    participant_classes = {
        _participant_id(participant): str(
            participant.get("source_class") or participant.get("name") or ""
        ).strip()
        for participant in model.get("Participants", [])
        if str(participant.get("kind", "")).strip().lower() != "actor"
    }
    contracts: dict[str, dict[str, tuple[dict[str, str], str | None]]] = {}
    class_model = state.get("extracted_bce_classes") or {}
    bce_operations = operation_catalog(class_model)
    stable_calls: dict[str, dict[str, Any]] = {}
    for collaboration in class_model.get("Collaborations", []):
        if not isinstance(collaboration, dict):
            continue
        for raw_call in collaboration.get("calls", []):
            if not isinstance(raw_call, dict):
                continue
            stable_id = str(raw_call.get("stableId") or "").strip()
            operation = bce_operations.get(str(raw_call.get("receiverOperationId") or "").strip())
            if stable_id and operation:
                stable_calls[stable_id] = {"call": raw_call, "operation": operation}
    for class_item in class_model.get("Classes", []):
        class_name = str(class_item.get("className") or "").strip()
        if not class_name:
            continue
        contracts[class_name] = {
            signature: (_method_parameters(signature), method_return_type(str(raw_method)))
            for raw_method in _class_method_signatures(class_item)
            if (signature := method_call_signature(str(raw_method)))
        }

    calls = _explicit_calls(model)
    calls_by_ref = {
        str(message.get("call_ref") or "").strip(): (call_index, message)
        for call_index, message in calls.values()
        if str(message.get("call_ref") or "").strip()
    }
    known_steps = _known_flow_step_ids(state)
    found: list[Finding] = []
    for call_id, (call_index, call) in calls.items():
        target = str(call.get("target") or "").strip()
        class_name = participant_classes.get(target, "")
        signature = method_call_signature(str(call.get("label") or ""))
        contract = contracts.get(class_name, {}).get(signature)
        if contract is None:
            continue
        expected, _ = contract
        raw_bindings = [
            binding for binding in call.get("arguments") or [] if isinstance(binding, dict)
        ]
        bindings = {
            str(binding.get("parameter") or "").strip(): binding for binding in raw_bindings
        }
        location = f"{call.get('source', '')} -> {target} : {call.get('label', '')}"
        binding_names = [str(binding.get("parameter") or "").strip() for binding in raw_bindings]
        duplicates = sorted({name for name in binding_names if name and binding_names.count(name) > 1})
        if duplicates:
            found.append(
                Finding(
                    rule_id,
                    f"호출 '{call_id}'의 인자 {duplicates}가 둘 이상 바인딩됨",
                    location,
                )
            )
        if set(bindings) != set(expected):
            found.append(
                Finding(
                    rule_id,
                    f"호출 '{call_id}'의 인자 {sorted(bindings)}가 메서드 매개변수 {sorted(expected)}와 일치하지 않음",
                    location,
                )
            )
        for parameter, binding in bindings.items():
            if parameter not in expected:
                continue
            bound_type = str(binding.get("type") or "").strip()
            if normalize_return_type(bound_type) != normalize_return_type(expected[parameter]):
                found.append(
                    Finding(
                        rule_id,
                        f"인자 '{parameter}' 타입 '{bound_type}'이 선언 타입 '{expected[parameter]}'과 일치하지 않음",
                        location,
                    )
                )
            source_kind = str(binding.get("source_kind") or "").strip()
            source_ref = str(binding.get("source_ref") or "").strip()
            source_id, source_separator, source_value = source_ref.partition("#")
            # Stable references are resolved only through accepted BCE identity
            # catalogs.  Their parameter and field tokens are opaque refs, not
            # display/Java names; the receiver binding name remains unchanged.
            if source_kind == "input":
                receiver = next((
                    operation for operation in bce_operations.values()
                    if str(operation.get("className") or "") == class_name
                    and method_call_signature(
                        f"{operation.get('name', '')}(" + ",".join(
                            f"{item.get('name', '')}:{item.get('type', '')}"
                            for item in operation.get("parameters") or [] if isinstance(item, dict)
                        ) + ")"
                    ) == signature
                ), None)
                expected_ref = next((
                    str(item.get("stableRef") or "")
                    for item in (receiver or {}).get("parameters") or []
                    if isinstance(item, dict) and str(item.get("name") or "") == parameter
                ), "")
                if (
                    not source_separator
                    or source_id not in known_steps
                    or source_value != expected_ref
                    or not expected_ref
                ):
                    found.append(Finding(rule_id, f"입력 원천 '{source_ref}'가 stable step/parameter ref와 일치하지 않음", location))
                continue
            if source_kind in {"call_parameter", "call_result"}:
                source_call = calls_by_ref.get(source_id) if source_separator else None
                if source_call is None or source_call[0] >= call_index:
                    found.append(Finding(rule_id, f"선행 stable 호출 원천 '{source_ref}'가 존재하지 않음", location))
                    continue
                producer = source_call[1]
                producer_owner = str(producer.get("target") or "").strip()
                consumer = str(call.get("source") or "").strip()
                if producer_owner != consumer:
                    found.append(Finding(rule_id, f"호출 원천 '{source_ref}'는 '{consumer}'가 직접 사용할 수 없음", location))
                    continue
                produced_type = _stable_source_type(source_ref, stable_calls, class_model)
                if not produced_type or not types_compatible(produced_type, bound_type):
                    found.append(Finding(rule_id, f"stable 원천 '{source_ref}' 타입이 '{parameter}:{bound_type}'과 일치하지 않음", location))
                continue
            if source_kind == "precondition":
                found.append(Finding(rule_id, f"선행조건 원천 '{source_ref}'는 stable ref가 아님", location))
                continue
    return found


def _flow_step_records(state: dict) -> list[tuple[str, str]]:
    """Return canonical scenario step IDs and their display sentences."""
    index = _scenario_index(state)
    if index is None:
        return []
    return [
        (step.id, step.sentence)
        for use_case in index.use_cases
        for step in use_case.steps
    ]


def sequence_actor_step_involvement(model: dict, state: dict) -> list[Finding]:
    """Require exact actor and step refs for actor-originated scenario calls."""
    rule_id = "sequence.actor-step-involvement"
    unresolved = _unresolved_flow_step_ids(state)
    found: list[Finding] = []
    actor_alias_by_ref: dict[str, str] = {}
    actor_aliases_missing_ref: list[str] = []
    for participant in model.get("Participants", []) or []:
        if not isinstance(participant, dict) or str(participant.get("kind") or "").casefold() != "actor":
            continue
        alias = _participant_id(participant)
        participant_ref = str(participant.get("participant_ref") or "").strip()
        if participant_ref:
            actor_alias_by_ref[participant_ref] = alias
        else:
            actor_aliases_missing_ref.append(alias)

    main_step_subjects: dict[str, str] = {}
    for use_case in (state.get("usecase_spec") or {}).get("use_case_specs") or []:
        if not isinstance(use_case, dict):
            continue
        use_case_id = str(use_case.get("use_case_id") or "").strip()
        if not use_case_id:
            continue
        for step in use_case.get("main_scenario") or []:
            if isinstance(step, dict) and step.get("step_number") is not None:
                main_step_subjects[f"{use_case_id}:main:{step['step_number']}"] = str(
                    step.get("subject_ref") or ""
                ).strip()

    for step_id, subject_ref in main_step_subjects.items():
        if step_id in unresolved:
            continue
        indexed_messages = [
            (index, message)
            for index, message in enumerate(model.get("Messages", []) or [])
            if step_id in {str(value).strip() for value in message.get("step_ids") or []}
            and str(message.get("type", "sync")).lower() in {"sync", "async", "self"}
        ]
        if not subject_ref:
            if indexed_messages:
                found.append(Finding(
                    rule_id,
                    "Tracked main step has no subject_ref; actor involvement cannot be inferred from its sentence",
                    step_id,
                ))
            continue
        if subject_ref.casefold() == "system" or not indexed_messages:
            continue
        actor_alias = actor_alias_by_ref.get(subject_ref)
        if not actor_alias:
            detail = (
                f"Actor participants lack participant_ref; cannot resolve subject_ref '{subject_ref}'"
                if actor_aliases_missing_ref
                else f"Tracked actor step references '{subject_ref}' but no actor participant has that participant_ref"
            )
            found.append(Finding(
                rule_id,
                detail,
                step_id,
            ))
            continue
        actor_messages = [
            (index, message)
            for index, message in indexed_messages
            if str(message.get("source") or "").strip() == actor_alias
        ]
        if not actor_messages:
            if actor_aliases_missing_ref:
                found.append(Finding(
                    rule_id,
                    f"Actor participants lack participant_ref; cannot verify actor-originated call for '{subject_ref}'",
                    step_id,
                ))
            else:
                found.append(Finding(
                    rule_id,
                    f"Actor step subject_ref '{subject_ref}' has no call sourced by its matching actor participant",
                    step_id,
                ))
    return found


def sequence_causal_call_chain(model: dict, state: dict) -> list[Finding]:
    """비-액터 호출 주체가 앞선 호출을 통해 먼저 도달 가능한 상태인가."""
    rule_id = "sequence.causal-call-chain"
    kinds = {
        _participant_id(participant): str(participant.get("kind", "")).strip().lower()
        for participant in model.get("Participants", [])
    }
    reached = {alias for alias, kind in kinds.items() if kind == "actor"}
    if not reached:
        return []  # 액터가 없는 부분 모델은 인과 시작점을 판정할 수 없다.

    found: list[Finding] = []
    reported: set[str] = set()
    for message in model.get("Messages", []):
        if str(message.get("type", "sync")).strip().lower() not in {"sync", "async", "self"}:
            continue
        source = str(message.get("source") or "").strip()
        target = str(message.get("target") or "").strip()
        if source not in reached:
            if source not in reported:
                found.append(
                    Finding(
                        rule_id,
                        f"'{source}'가 선행 호출로 활성화되기 전에 호출을 시작함",
                        f"{source} -> {target} : {message.get('label', '')}",
                    )
                )
                reported.add(source)
            continue
        reached.add(target)
    return found


def sequence_usecase_coverage(model: dict, state: dict) -> list[Finding]:
    """flow coverage와 이 use case를 추적하는 operation 호출 여부를 검사한다."""
    rule_id = "sequence.usecase-step-coverage"
    diagram_use_case_id = str(model.get("use_case_id") or "").strip()
    all_flow_steps = _known_flow_step_ids(state)
    flow_steps = all_flow_steps - _unresolved_flow_step_ids(state)
    if diagram_use_case_id:
        all_flow_steps = {
            step_id
            for step_id in all_flow_steps
            if step_id.startswith(f"{diagram_use_case_id}:")
        }
        flow_steps = {
            step_id
            for step_id in flow_steps
            if step_id.startswith(f"{diagram_use_case_id}:")
        }
    found: list[Finding] = []
    if flow_steps:
        covered_steps = {
            str(step_id).strip()
            for message in model.get("Messages", [])
            if str(message.get("type", "sync")).lower() not in {"activate", "deactivate"}
            for step_id in message.get("step_ids", [])
            if step_id
        }
        # 명시적으로 unresolved로 보존한 단계는 누락된 것이 아니다. 아래에서 review finding은
        # 남기되 다이어그램 전체가 생성되지 않은 것처럼 coverage를 이중 보고하지 않는다.
        covered_steps.update(
            str(item.get("step_id") or "").strip()
            for item in model.get("UnresolvedSteps", []) or []
            if isinstance(item, dict) and item.get("step_id")
        )
        # 조건·결과 문장은 별도 receiver method가 아니어도 interaction 의미에 포함될 수 있다.
        # 가짜 call이나 unresolved method로 바꾸지 않고 NarrativeSteps에서 명시적으로 추적한다.
        covered_steps.update(
            str(item.get("step_id") or "").strip()
            for item in model.get("NarrativeSteps", []) or []
            if isinstance(item, dict) and item.get("step_id")
        )
        found.extend([
            Finding(rule_id, f"시퀀스 다이어그램에 반영되지 않은 흐름 단계 id '{step_id}'", step_id)
            for step_id in sorted(flow_steps - covered_steps)
        ])
        class_model = state.get("extracted_bce_classes") or {}
        operations_by_id: dict[str, dict] = {}
        operation_names_by_ref: dict[str, str] = {}
        for class_item in class_model.get("Classes") or []:
            if not isinstance(class_item, dict):
                continue
            for operation in class_item.get("operations") or []:
                if not isinstance(operation, dict):
                    continue
                operation_id = str(operation.get("operationId") or "").strip()
                stable_ref = str(operation.get("stableId") or "").strip()
                if operation_id:
                    operations_by_id[operation_id] = operation
                if stable_ref:
                    operation_names_by_ref[stable_ref] = (
                        f"{str(class_item.get('className') or '').strip()}::"
                        f"{str(operation.get('name') or '').strip()}"
                    )

        collaborations = class_model.get("Collaborations")
        required_call_refs: set[str] = set()
        required_operation_refs: set[str] = set()
        if isinstance(collaborations, list):
            for collaboration in collaborations:
                if not isinstance(collaboration, dict) or diagram_use_case_id not in {
                    str(value).strip() for value in collaboration.get("useCaseIds") or []
                }:
                    continue
                for call in collaboration.get("calls") or []:
                    if not isinstance(call, dict) or not any(
                        str(step_ref).strip() in flow_steps
                        for step_ref in call.get("stepRefs") or []
                    ):
                        continue
                    call_ref = str(call.get("stableId") or "").strip()
                    operation = operations_by_id.get(
                        str(call.get("receiverOperationId") or "").strip(), {}
                    )
                    operation_ref = str(operation.get("stableId") or "").strip()
                    if not call_ref or not operation_ref:
                        found.append(Finding(
                            rule_id,
                            "Accepted collaboration call is missing stable call or operation identity",
                            str(call.get("receiverOperationId") or "<unknown operation>"),
                        ))
                        continue
                    required_call_refs.add(call_ref)
                    required_operation_refs.add(operation_ref)
        else:
            for class_item in class_model.get("Classes") or []:
                if not isinstance(class_item, dict):
                    continue
                for operation in class_item.get("operations") or []:
                    if not isinstance(operation, dict) or not any(
                        str(step_ref).strip() in flow_steps
                        for step_ref in (operation.get("stepRefs") or operation.get("step_refs") or [])
                    ):
                        continue
                    operation_ref = str(operation.get("stableId") or "").strip()
                    if operation_ref:
                        required_operation_refs.add(operation_ref)
                    else:
                        found.append(Finding(
                            rule_id,
                            "Step-traced class operation is missing stable operation identity",
                            str(operation.get("operationId") or operation.get("name") or "<unknown operation>"),
                        ))

        invoked_call_refs: set[str] = set()
        invoked_operation_refs: set[str] = set()
        for message in model.get("Messages") or []:
            if not isinstance(message, dict) or str(message.get("type") or "sync").casefold() not in {"sync", "async", "self"}:
                continue
            call_ref = str(message.get("call_ref") or "").strip()
            operation_ref = str(message.get("operation_ref") or "").strip()
            if not call_ref or not operation_ref:
                found.append(Finding(
                    rule_id,
                    "Sequence call is missing stable call_ref or operation_ref; coverage cannot be inferred from its label",
                    str(message.get("source") or "") + " -> " + str(message.get("target") or ""),
                ))
                continue
            invoked_call_refs.add(call_ref)
            invoked_operation_refs.add(operation_ref)

        found.extend(
            Finding(rule_id, "Accepted collaboration call is absent from the sequence diagram", call_ref)
            for call_ref in sorted(required_call_refs - invoked_call_refs)
        )
        found.extend(
            Finding(
                rule_id,
                f"Class operation '{operation_names_by_ref.get(operation_ref, operation_ref)}' is traced to this use case but is not invoked",
                operation_ref,
            )
            for operation_ref in sorted(required_operation_refs - invoked_operation_refs)
        )
        return found
    if all_flow_steps:
        return []  # 이 다이어그램의 알려진 단계가 모두 unresolved인 경우다.

    use_cases = _known_use_case_ids(state)
    if diagram_use_case_id:
        use_cases = {diagram_use_case_id}
    if not use_cases:
        return []

    covered: set[str] = set()
    for msg in model.get("Messages", []):
        for uc_id in msg.get("use_case_ids", []):
            if uc_id:
                covered.add(str(uc_id).strip())

    uncovered = use_cases - covered
    if not uncovered:
        return []

    for uc_id in sorted(uncovered):
        found.append(
            Finding(
                rule_id,
                f"시퀀스 다이어그램에 반영되지 않은 유스케이스 id '{uc_id}'",
                uc_id,
            )
        )
    return found


def sequence_step_operation_distinctness(model: dict, state: dict) -> list[Finding]:
    """Retained registry hook; operation reuse alone has no semantic contract."""
    # A shared operation may legitimately implement multiple steps. Exact step
    # coverage is checked independently by sequence_usecase_coverage.
    return []
    rule_id = "sequence.step-operation-distinctness"
    use_case_id = str(model.get("use_case_id") or "").strip()
    kinds = {
        _participant_id(participant): str(participant.get("kind") or "").strip().lower()
        for participant in model.get("Participants", []) or []
        if isinstance(participant, dict)
    }
    step_sentences = {
        f"{str(item.get('use_case_id') or '').strip()}:main:{step.get('step_number')}": str(
            step.get("sentence") or ""
        ).strip().lower()
        for item in (state.get("usecase_spec") or {}).get("use_case_specs") or []
        if isinstance(item, dict)
        for step in item.get("main_scenario") or []
        if isinstance(step, dict) and step.get("step_number") is not None
    }
    actor_names = {
        " ".join(str(participant.get("name") or "").casefold().split())
        for participant in model.get("Participants", []) or []
        if isinstance(participant, dict)
        and str(participant.get("kind") or "").casefold() == "actor"
        and str(participant.get("name") or "").strip()
    }

    def is_actor_step(step_id: str) -> bool:
        sentence = " ".join(step_sentences.get(step_id, "").split())
        return not actor_names or any(
            re.match(rf"^(?:the )?{re.escape(actor)}\b", sentence)
            for actor in actor_names
        )

    def is_single_submission(step_ids: set[str]) -> bool:
        """의도와 바로 뒤의 입력 data가 command 하나를 공유하는 경우를 허용한다.

        상세 use-case 명세는 한 사용자 제출을 "생성을 시작한다"와 "속성을 제공한다"로
        나누곤 한다. 명세가 두 번째 command를 식별하지 않는다면 독립 Boundary operation
        둘이 아니다. 문장마다 method 하나를 강제하면 유지보수 use case에 가짜 class method
        제안이 생긴다.
        """
        ordered = sorted(step_ids, key=lambda value: int(value.rsplit(":", 1)[-1]))
        if len(ordered) != 2:
            return False
        try:
            first, second = (int(value.rsplit(":", 1)[-1]) for value in ordered)
        except ValueError:
            return False
        if second != first + 1:
            return False
        intent = step_sentences.get(ordered[0], "")
        details = step_sentences.get(ordered[1], "")
        return (
            any(token in intent for token in ("initiat", "indicat", "intend", "begin", "start"))
            and any(token in details for token in ("suppl", "provid", "enter", "input", "attribute", "detail"))
        )

    calls: dict[str, set[str]] = {}
    for message in model.get("Messages") or []:
        if not isinstance(message, dict):
            continue
        if str(message.get("type", "sync")).strip().lower() not in {"sync", "async", "self"}:
            continue
        signature = method_call_signature(str(message.get("label") or ""))
        if not signature:
            continue
        source = str(message.get("source") or "").strip()
        target = str(message.get("target") or "").strip()
        if kinds.get(source) != "actor" or kinds.get(target) != "boundary":
            continue
        main_steps = {
            str(step_id).strip()
            for step_id in message.get("step_ids") or []
            if (
                str(step_id).startswith(f"{use_case_id}:main:")
                if use_case_id
                else ":main:" in str(step_id)
            )
            and is_actor_step(str(step_id).strip())
        }
        if main_steps:
            calls.setdefault(signature, set()).update(main_steps)
    return [
        Finding(
            rule_id,
            f"서로 다른 사용자 입력 단계 {sorted(step_ids)}가 동일한 Boundary 호출 '{signature}'으로만 표현됨",
            signature,
        )
        for signature, step_ids in sorted(calls.items())
        if len(step_ids) > 1 and not is_single_submission(step_ids)
    ]


def sequence_unresolved_steps(model: dict, state: dict) -> list[Finding]:
    """미결 요구사항 또는 method mapping 단계를 review finding으로 유지한다."""
    rule_id = "sequence.unresolved-usecase-step"
    diagram_use_case_id = str(model.get("use_case_id") or "").strip()
    unresolved = _unresolved_flow_step_ids(state)
    if diagram_use_case_id:
        unresolved = {
            step_id for step_id in unresolved if step_id.startswith(f"{diagram_use_case_id}:")
        }
    found = [
        Finding(
            rule_id,
            f"행동이 결정되지 않은 요구사항 단계 '{step_id}'는 시퀀스 생성 전에 보완해야 함",
            step_id,
        )
        for step_id in sorted(unresolved)
    ]
    known = {finding.location for finding in found}
    for item in model.get("UnresolvedSteps", []) or []:
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("step_id") or "").strip()
        if not step_id or step_id in known:
            continue
        reason = str(item.get("reason") or "grounded method selection failed").strip()
        found.append(
            Finding(
                rule_id,
                f"흐름 단계 '{step_id}'의 클래스 메서드를 확정하지 못함: {reason}",
                step_id,
            )
        )
    return found


def _main_step_number(step_id: str, use_case_id: str) -> int | None:
    match = re.fullmatch(rf"{re.escape(use_case_id)}:main:(\d+)", step_id)
    return int(match.group(1)) if match else None


def sequence_flow_order(model: dict, state: dict) -> list[Finding]:
    """주 흐름 순서와 확장 흐름의 분기 위치가 명세와 일치하는가."""
    rule_id = "sequence.flow-order"
    use_case_id = str(model.get("use_case_id") or "").strip()
    if not use_case_id:
        return []
    messages = model.get("Messages", [])
    found: list[Finding] = []
    last_main = -1
    seen_main: set[int] = set()
    main_positions: dict[int, list[int]] = {}
    for index, message in enumerate(messages):
        is_return = str(message.get("type") or "").casefold() == "return"
        numbers = sorted([
            number
            for step_id in message.get("step_ids") or []
            if (number := _main_step_number(str(step_id), use_case_id)) is not None
        ])
        # sync call 하나가 입력 단계에서 시작해 중첩 call 뒤의 출력 단계에서 끝날 수 있다.
        # preorder 위치는 가장 이른 trace만 전진시키되 coverage에는 모든 step ID를 남긴다.
        for number in numbers[:1]:
            # reply는 내부 call이 끝난 뒤 앞선 중첩 call을 닫는다. trace는 그 call의 근거지만
            # 새 시나리오 행동은 아니므로 main flow 순서를 역전시킬 수 없다.
            if is_return:
                continue
            main_positions.setdefault(number, []).append(index)
            if number not in seen_main and number < last_main:
                found.append(
                    Finding(
                        rule_id,
                        f"주 흐름 단계 {number}가 단계 {last_main} 뒤에 배치됨",
                        f"{message.get('source', '')} -> {message.get('target', '')} : {message.get('label', '')}",
                    )
                )
            if number not in seen_main:
                last_main = max(last_main, number)
                seen_main.add(number)

    for extension in _extension_records(state, use_case_id):
        extension_ref = str(extension["extension_ref"])
        label = str(extension.get("label") or "")
        branch_step = int(extension["branch_step"])
        extension_step_ids = set(extension["step_ids"])
        positions = [
            index
            for index, message in enumerate(messages)
            if str(message.get("type") or "").casefold() != "return"
            if extension_step_ids & {str(step_id).strip() for step_id in message.get("step_ids") or []}
        ]
        if not positions:
            continue
        fragment_id = extension_ref
        fragment_positions = [
            index
            for index in positions
            if any(
                str(fragment.get("id") or "").strip() == fragment_id
                for fragment in messages[index].get("fragments") or []
                if isinstance(fragment, dict)
            )
        ]
        # Typed projection에서는 fragment가 실제 조건부 실행 범위의 기준이다. 같은
        # extension step을 정상 흐름 operation이 근거로 함께 추적해도, fragment 밖의
        # 메시지를 조건 분기의 실행 위치로 오인하지 않는다.
        if fragment_positions:
            positions = fragment_positions
        # 분기 단계와 첫 extension 입력은 같은 호출이 함께 근거로 삼을 수 있다. 그
        # 호출은 extension이 늦게 배치된 것이 아니라 정확히 분기가 시작되는 위치다.
        # 실제 분기 이후 메시지만 다음 main 단계 전에 놓였는지 검사한다.
        branch_positions = {
            position for position in positions
            if any(
                _main_step_number(str(step_id), use_case_id) == branch_step
                for step_id in messages[position].get("step_ids") or []
            )
        }
        if all(
            any(
                (number := _main_step_number(str(step_id), use_case_id)) is not None
                and number >= branch_step
                for step_id in messages[position].get("step_ids") or []
            )
            for position in positions
        ):
            # 한 Control call이 분기 단계의 정상 결과와 extension 결과를 함께 소유할 수
            # 있다. 같은 메시지에 branch step 또는 그 이후 main step이 함께 적혀 있으면
            # 별도로 늦게 실행된 extension이 아니라 한 호출의 조건부 결과다.
            continue
        if branch_step not in main_positions:
            found.append(
                Finding(
                    rule_id,
                    f"확장 흐름 '{label}'의 분기 기준인 주 흐름 단계 {branch_step}가 없어 배치 위치를 검증할 수 없음",
                    extension_ref,
                )
            )
            continue
        branch_end = max(main_positions[branch_step])
        placed = [position for position in positions if position not in branch_positions]

        # A later-main call may be the operation whose conditional result realizes
        # the extension.  In a call tree that parent necessarily appears before its
        # nested opt message, even though the scenario branch itself has not advanced
        # past the extension yet.  Treat only *completed or unrelated* later-main
        # calls as chronological successors.  Otherwise a valid
        # ``call(main 2) -> opt(extension 1a) -> return(main 2)`` activation is
        # indistinguishable from an extension emitted after main step 2.
        return_positions = {
            str(message.get("reply_to") or "").strip(): index
            for index, message in enumerate(messages)
            if str(message.get("type") or "").casefold() == "return"
            and str(message.get("reply_to") or "").strip()
        }

        def encloses_extension(index: int) -> bool:
            call_id = str(messages[index].get("call_id") or "").strip()
            return_index = return_positions.get(call_id)
            return bool(
                placed
                and return_index is not None
                and all(index < position < return_index for position in placed)
            )

        later_main = [
            index
            for number, indexes in main_positions.items()
            if number > branch_step
            for index in indexes
            if index not in positions
            and not encloses_extension(index)
        ]
        next_main = min(later_main) if later_main else len(messages)
        if placed and (min(placed) <= branch_end or max(placed) >= next_main):
            found.append(
                Finding(
                    rule_id,
                    f"확장 흐름 '{label}'가 분기 단계 {branch_step} 직후에 배치되지 않음",
                    extension_ref,
                )
            )
    return found


def sequence_fragment_condition_consistency(model: dict, state: dict) -> list[Finding]:
    """복합 조각(group)과 조건문(condition) 간의 무결성 검사.

    group(alt/loop/opt)이 선언되었으면 condition설명이 필수이며, 반대로 group이
    없으면 condition만 독립적으로 유령 기입되어선 안 된다.
    """
    rule_id = "sequence.fragment-condition-consistency"
    found: list[Finding] = []

    definitions: dict[str, tuple[str, str]] = {}
    branches: dict[str, set[str]] = {}
    fragment_step_ids: dict[str, set[str]] = {}
    explicit_fragment_ids: set[str] = set()
    branch_positions: dict[str, dict[str, list[int]]] = {}
    extension_ref_by_step = {
        step_id: str(extension["extension_ref"])
        for extension in _extension_records(state)
        for step_id in extension["step_ids"]
    }
    index = _scenario_index(state)
    known_condition_refs = {
        record["extension_ref"] for record in _extension_records(state)
    }
    if index is not None:
        known_condition_refs.update(
            f"{relation.base_id}:extend:{relation.child_id}"
            for relation in index.relationships if relation.kind == "extend"
        )
    source_fragment_ids = set(known_condition_refs)
    for message_index, msg in enumerate(model.get("Messages", [])):
        source = str(msg.get("source", "")).strip()
        target = str(msg.get("target", "")).strip()
        label = str(msg.get("label", "")).strip()
        location = f"{source} -> {target} : {label}"
        for fragment in _message_fragments(msg):
            fragment_id = str(fragment.get("id") or "").strip()
            group = str(fragment.get("type") or "").strip()
            branch = str(fragment.get("branch") or "").strip()
            condition = str(fragment.get("condition") or "").strip()
            if not fragment_id or group not in {"alt", "opt", "loop"} or not condition:
                found.append(Finding(rule_id, "fragment의 id/type/condition이 완전하지 않음", location))
                continue
            condition_ref = str(fragment.get("condition_ref") or "").strip()
            if fragment_id in source_fragment_ids:
                if condition_ref != fragment_id:
                    found.append(Finding(
                        rule_id,
                        "source-governed fragment의 condition_ref가 fragment ID와 일치하지 않음",
                        location,
                    ))
            elif condition_ref and condition_ref not in known_condition_refs:
                found.append(Finding(
                    rule_id,
                    f"알 수 없는 condition_ref '{condition_ref}'",
                    location,
                ))
            branches.setdefault(fragment_id, set()).add(branch)
            fragment_step_ids.setdefault(fragment_id, set()).update(
                str(step_id).strip()
                for step_id in msg.get("step_ids") or []
                if str(step_id).strip()
            )
            branch_positions.setdefault(fragment_id, {}).setdefault(branch, []).append(
                message_index
            )
            if isinstance(msg.get("fragments"), list):
                explicit_fragment_ids.add(fragment_id)
            if branch == "else" and group != "alt":
                found.append(Finding(rule_id, "else branch는 alt fragment에서만 허용됨", location))
            prior = definitions.get(fragment_id)
            if prior and prior[0] != group:
                found.append(Finding(rule_id, f"fragment id '{fragment_id}'가 서로 다른 type을 사용함", location))
            definitions.setdefault(fragment_id, (group, condition))

    for fragment_id, (group, _) in definitions.items():
        if (
            fragment_id in explicit_fragment_ids
            and group == "alt"
            and branches.get(fragment_id) != {"main", "else"}
        ):
            found.append(
                Finding(
                    rule_id,
                    f"alt fragment '{fragment_id}'는 main과 else branch를 모두 가져야 함; 단일 조건은 opt를 사용해야 함",
                    fragment_id,
                )
            )
            continue
        positions = branch_positions.get(fragment_id, {})
        extension_refs = {
            extension_ref_by_step[step_id]
            for step_id in fragment_step_ids.get(fragment_id, set())
            if step_id in extension_ref_by_step
        }
        if (
            group == "alt"
            and extension_refs
            and not any(
                ":main:" in step_id
                for step_id in fragment_step_ids.get(fragment_id, set())
            )
        ):
            found.append(
                Finding(
                    rule_id,
                    f"extension trigger만 표현한 fragment '{fragment_id}'는 alt가 아니라 opt여야 함",
                    fragment_id,
                )
            )
        if (
            group == "alt"
            and positions.get("main")
            and positions.get("else")
            and min(positions["else"]) < min(positions["main"])
        ):
            found.append(
                Finding(
                    rule_id,
                    f"alt fragment '{fragment_id}'의 else branch가 main branch보다 먼저 나타남",
                    fragment_id,
                )
            )

    return found


def sequence_database_access_discipline(model: dict, state: dict) -> list[Finding]:
    """데이터베이스(database) 직접 접근 주체 규약 검사.

    Database 계층으로의 직접 접근은 Control 또는 Entity 계층에서만 허용되며,
    Actor나 Boundary 계층에서 DB를 직접 호출하는 것은 아키텍처 위반이다.
    """
    rule_id = "sequence.database-access-discipline"
    kinds = {
        _participant_id(p): str(p.get("kind", "")).strip().lower()
        for p in model.get("Participants", [])
    }
    found: list[Finding] = []

    for msg in model.get("Messages", []):
        if str(msg.get("type", "sync")).lower() in {"return", "activate", "deactivate"}:
            continue
        source = str(msg.get("source", "")).strip()
        target = str(msg.get("target", "")).strip()
        source_kind = kinds.get(source, "")
        target_kind = kinds.get(target, "")

        if target_kind == "database" and source_kind in ("actor", "boundary"):
            location = f"{source} -> {target}"
            found.append(
                Finding(
                    rule_id,
                    f"'{source_kind}' 계층({source})에서 데이터베이스({target})를 직접 호출함 (Control/Entity를 거쳐야 함)",
                    location,
                )
            )

    return found


def sequence_self_call_method_validation(model: dict, state: dict) -> list[Finding]:
    """자기 자신 호출(Self-Call) 오퍼레이션 검증.

    source == target 인 셀프 메시지가 발생할 때, 해당 호출 오퍼레이션이 정당하게
    선언되어 있는지 및 라벨 기입 여부를 검사한다.
    """
    rule_id = "sequence.self-call-method-validation"
    found: list[Finding] = []

    for msg in model.get("Messages", []):
        if str(msg.get("type", "sync")).lower() not in {"sync", "async", "self"}:
            continue
        source = str(msg.get("source", "")).strip()
        target = str(msg.get("target", "")).strip()
        label = str(msg.get("label", "")).strip()

        if source and source == target:
            if not label:
                location = f"{source} -> {target}"
                found.append(
                    Finding(
                        rule_id,
                        f"자기 자신({source})을 호출하는 메시지의 라벨(오퍼레이션명)이 비어 있음",
                        location,
                    )
                )

    return found


def sequence_orphan_participant_detection(model: dict, state: dict) -> list[Finding]:
    """메시지가 단 하나도 없는 고립된 참가자(Orphan Participant) 감지.

    Participants 목록에는 선언되어 있으나 전체 Messages 중 단 한 번도 source 나 target으로
    참여하지 않는 불필요한 유령 참가자를 탐지한다.
    """
    # review-only use case는 note에 실제 설계 문맥을 주기 위해 actor/Boundary를 유지한다.
    # 이 선언은 ghost participant가 아니며 설명용 다이어그램을 숨기게 해서는 안 된다.
    if any(
        isinstance(item, dict)
        for item in model.get("UnresolvedSteps", []) or []
    ):
        return []

    rule_id = "sequence.orphan-participant-detection"
    active_participants: set[str] = set()

    for msg in model.get("Messages", []):
        source = str(msg.get("source", "")).strip()
        target = str(msg.get("target", "")).strip()
        if source:
            active_participants.add(source)
        if target:
            active_participants.add(target)

    found: list[Finding] = []
    for participant in model.get("Participants", []):
        participant_id = _participant_id(participant)
        name = str(participant.get("name") or participant_id).strip()
        if participant_id and participant_id not in active_participants:
            found.append(
                Finding(
                    rule_id,
                    f"메시지상에서 한 번도 호출/응답하지 않는 고립된 참가자 '{name}'",
                    name,
                )
            )

    return found


def sequence_duplicate_consecutive_messages(model: dict, state: dict) -> list[Finding]:
    """무의미한 연속 중복 메시지 탐지.

    loop나 alt 같은 복합 조각 밖에서 동일한 source, target, label, type을 가진 메시지가
    연달아 기입된 경우 지적한다.
    """
    rule_id = "sequence.duplicate-consecutive-messages"
    found: list[Finding] = []
    messages = model.get("Messages", [])

    duplicate_run_start: int | None = None
    duplicate_run_key: tuple[str, str, str, str, str] | None = None

    def report_run(end_index: int) -> None:
        """repair 점수가 중복 증가를 숨기지 못하도록 연속 구간과 크기를 함께 보고한다."""
        nonlocal duplicate_run_start, duplicate_run_key
        if duplicate_run_start is None or duplicate_run_key is None:
            return
        source, target, label, _, _ = duplicate_run_key
        count = end_index - duplicate_run_start + 1
        found.append(
            Finding(
                rule_id,
                f"동일한 메시지 '{label}'가 {count}회 연달아 중복 기입되어 있음 ({source} → {target})",
                f"{source} -> {target} : {label} (messages {duplicate_run_start + 1}-{end_index + 1})",
            )
        )
        duplicate_run_start = None
        duplicate_run_key = None

    for i in range(1, len(messages)):
        prev = messages[i - 1]
        curr = messages[i]

        prev_key = (
            str(prev.get("source", "")).strip(),
            str(prev.get("target", "")).strip(),
            str(prev.get("label", "")).strip(),
            str(prev.get("type", "sync")).strip().lower(),
            repr(_message_fragments(prev)),
        )
        curr_key = (
            str(curr.get("source", "")).strip(),
            str(curr.get("target", "")).strip(),
            str(curr.get("label", "")).strip(),
            str(curr.get("type", "sync")).strip().lower(),
            repr(_message_fragments(curr)),
        )

        if prev_key == curr_key and prev_key[2]:  # label이 비어있지 않은 경우
            if duplicate_run_key != curr_key:
                report_run(i - 1)
                duplicate_run_start = i - 1
                duplicate_run_key = curr_key
            continue
        report_run(i - 1)

    report_run(len(messages) - 1)

    return found


def sequence_extension_replays_anchor_operation(
    model: dict, state: dict
) -> list[Finding]:
    """처리 중인 operation을 다시 실행하는 error branch를 검출한다.

    extension은 ``branch_step``의 조건부 결과다. ``opt``/``alt`` branch에서 같은 call을
    반복하면 실패한 validation/persistence operation이 두 번째 요청처럼 보인다. 실제 retry는
    명시적인 loop로 표현해야 하므로 구분할 수 있다.
    """

    rule_id = "sequence.extension-replays-anchor-operation"
    use_case_id = str(model.get("use_case_id") or "").strip()
    if not use_case_id:
        return []
    extensions = _extension_records(state, use_case_id)
    if not extensions:
        return []

    call_types = {"sync", "async", "self"}
    messages = [item for item in model.get("Messages") or [] if isinstance(item, dict)]
    participant_kinds = {
        _participant_id(item): str(item.get("kind") or "").strip().lower()
        for item in model.get("Participants") or []
        if isinstance(item, dict)
    }

    def is_command_or_input(message: dict) -> bool:
        """반복된 input/command만 두 번째 실행으로 판정한다.

        서로 다른 extension 결과가 Control→Boundary 표시 operation 하나를 재사용하는 것은
        정상이다. 이 presentation call은 branch anchor를 다시 실행하지 않는다. 반면
        Actor→Boundary input과 Control/Entity로 향하는 call은 두 번 실행될 수 있는 command다.
        """
        source_kind = participant_kinds.get(
            str(message.get("source") or "").strip(), ""
        )
        target_kind = participant_kinds.get(
            str(message.get("target") or "").strip(), ""
        )
        return (
            source_kind == "actor" and target_kind == "boundary"
        ) or target_kind in {"control", "entity", "database"}

    found: list[Finding] = []
    reported: set[tuple[str, str]] = set()
    for extension in extensions:
        extension_ref = str(extension["extension_ref"])
        extension_label = str(extension.get("label") or "")
        branch_step = int(extension["branch_step"])
        extension_step_ids = set(extension["step_ids"])
        anchor_step_id = f"{use_case_id}:main:{branch_step}"
        anchor_operations = {
            str(message.get("operation_ref") or "").strip()
            for message in messages
            if str(message.get("type") or "sync").lower() in call_types
            and is_command_or_input(message)
            and anchor_step_id in {str(item) for item in message.get("step_ids") or []}
            and str(message.get("operation_ref") or "").strip()
        }
        if not anchor_operations:
            continue
        for message in messages:
            if str(message.get("type") or "sync").lower() not in call_types:
                continue
            if not is_command_or_input(message):
                continue
            if not extension_step_ids & {str(step_id).strip() for step_id in message.get("step_ids") or []}:
                continue
            if anchor_step_id in {
                str(step_id) for step_id in message.get("step_ids") or []
            }:
                # 두 ref를 함께 가진 call은 공유 branch anchor이지 같은 operation의 두 번째
                # 실행이 아니다.
                continue
            if any(
                str(fragment.get("type") or "").lower() == "loop"
                for fragment in _message_fragments(message)
            ):
                continue
            operation_ref = str(message.get("operation_ref") or "").strip()
            call_ref = str(message.get("call_ref") or "").strip()
            if not operation_ref or not call_ref or operation_ref not in anchor_operations:
                continue
            if call_ref in {
                str(anchor.get("call_ref") or "").strip()
                for anchor in messages
                if anchor_step_id in {str(item) for item in anchor.get("step_ids") or []}
            }:
                continue
            key = (extension_ref, operation_ref)
            if key in reported:
                continue
            reported.add(key)
            found.append(
                Finding(
                    rule_id,
                    (
                        f"확장 흐름 '{extension_label}'가 분기 단계 {branch_step}의 "
                        f"operation_ref '{operation_ref}'를 반복함; 실패 결과는 별도 출력으로 "
                        "표현하고 재시도는 loop로 명시해야 함"
                    ),
                    call_ref,
                )
            )
    return found


def sequence_message_naming_convention(model: dict, state: dict) -> list[Finding]:
    """오퍼레이션 라벨 표기법 규약 검사.

    메시지 라벨이 클래스 이름 형태(PascalCase, 예: OrderControl)로 잘못 기입된 경우를
    지적한다. 오퍼레이션 라벨은 camelCase (예: registerOrder()) 또는 동사구이어야 한다.
    """
    rule_id = "sequence.message-naming-convention"
    found: list[Finding] = []

    for msg in model.get("Messages", []):
        if str(msg.get("type", "sync")).lower() not in {"sync", "async", "self"}:
            continue
        label = str(msg.get("label", "")).strip()
        if not label:
            continue

        # 괄호나 매개변수 이전의 첫 단어 추출
        raw_name = re.sub(r'^[+\-#~]\s*', '', label)
        match = re.match(r'([A-Za-z_]\w*)', raw_name)
        if match:
            first_word = match.group(1)
            # 첫 문자가 대문자이고(PascalCase), 단어가 오퍼레이션이 아닌 클래스명으로 오인될 수 있는 형태 검사
            # 단, ALL_CAPS 상수는 무시
            if first_word[0].isupper() and not first_word.isupper():
                source = str(msg.get("source", "")).strip()
                target = str(msg.get("target", "")).strip()
                location = f"{source} -> {target} : {label}"
                found.append(
                    Finding(
                        rule_id,
                        f"메시지 라벨 '{label}'이 클래스 명칭 형태(PascalCase)로 시작함 (camelCase 또는 verbNoun() 권장)",
                        location,
                    )
                )

    return found


def sequence_participant_kind_validity(model: dict, state: dict) -> list[Finding]:
    """참가자 종류(Kind) 표준성 검사.

    kind 필드가 5가지 표준 BCE/시퀀스 종류(actor, boundary, control, entity, database)
    내에 속하는지 검사한다.
    """
    rule_id = "sequence.participant-kind-validity"
    valid_kinds = {"actor", "boundary", "control", "entity", "database"}
    found: list[Finding] = []

    for participant in model.get("Participants", []):
        name = str(participant.get("name", "")).strip()
        kind = str(participant.get("kind", "")).strip().lower()

        if kind and kind not in valid_kinds:
            found.append(
                Finding(
                    rule_id,
                    f"참가자 '{name}'의 kind '{kind}'가 표준 종류(actor, boundary, control, entity, database)에 속하지 않음",
                    name,
                )
            )

    return found


def sequence_message_type_validity(model: dict, state: dict) -> list[Finding]:
    """메시지 호출 타입(Type) 표준성 검사.

    type 필드가 3가지 표준 호출 화살표 타입(sync, async, return) 내에 속하는지 검사한다.
    """
    rule_id = "sequence.message-type-validity"
    valid_types = {"sync", "async", "return", "self", "activate", "deactivate"}
    found: list[Finding] = []

    for msg in model.get("Messages", []):
        m_type = str(msg.get("type", "")).strip().lower()
        source = str(msg.get("source", "")).strip()
        target = str(msg.get("target", "")).strip()
        label = str(msg.get("label", "")).strip()
        location = f"{source} -> {target} : {label}"

        if m_type and m_type not in valid_types:
            found.append(
                Finding(
                    rule_id,
                    f"메시지 호출 타입 '{m_type}'이 표준 타입(sync, async, return)에 속하지 않음",
                    location,
                )
            )

    return found


def sequence_no_lifecycle_events(model: dict, state: dict) -> list[Finding]:
    """모든 생성 sequence가 activation 없는 고정 template을 따르는지 검사한다."""
    rule_id = "sequence.no-lifecycle-events"
    return [
        Finding(
            rule_id,
            "공통 시퀀스 템플릿은 activation/deactivation 네모 박스를 사용하지 않음",
            f"{message.get('source', '')} -> {message.get('target', '')}",
        )
        for message in model.get("Messages", []) or []
        if isinstance(message, dict)
        and str(message.get("type", "")).strip().lower()
        in {"activate", "deactivate"}
    ]


def sequence_class_diagram_version(model: dict, state: dict) -> list[Finding]:
    """다른 class method 계약에서 투영된 컬렉션을 version hash로 거부한다."""
    rule_id = "sequence.class-diagram-version"
    expected = str(model.get("class_diagram_hash") or "").strip()
    if not expected:
        return []  # 과거 단일 다이어그램은 version 계약 도입 전 산출물이다.
    actual = hashlib.sha256(
        str(state.get("class_diagram_puml") or "").encode("utf-8")
    ).hexdigest()
    if expected == actual:
        return []
    return [
        Finding(
            rule_id,
            "시퀀스가 현재 클래스 다이어그램과 다른 메서드 계약 버전에서 생성됨",
            "class_diagram_hash",
        )
    ]


SEQUENCE_DIAGRAM_DETECTORS: dict[str, Callable[[dict, dict], list[Finding]]] = {
    "sequence_participants": sequence_participants,
    "sequence_bce_flow": sequence_bce_flow,
    "sequence_boundary_operation_direction": sequence_boundary_operation_direction,
    "sequence_traceability": sequence_traceability,
    "sequence_class_diagram_version": sequence_class_diagram_version,
    "sequence_participant_classes": sequence_participant_classes,
    "sequence_message_methods": sequence_message_methods,
    "sequence_initial_entry": sequence_initial_entry,
    "sequence_call_return_links": sequence_call_return_links,
    "sequence_unmatched_returns": sequence_unmatched_returns,
    "sequence_async_returns": sequence_async_returns,
    "sequence_return_values_match_methods": sequence_return_values_match_methods,
    "sequence_calls_have_returns": sequence_calls_have_returns,
    "sequence_causal_call_chain": sequence_causal_call_chain,
    "sequence_argument_data_flow": sequence_argument_data_flow,
    "sequence_actor_step_involvement": sequence_actor_step_involvement,
    "sequence_usecase_coverage": sequence_usecase_coverage,
    "sequence_step_operation_distinctness": sequence_step_operation_distinctness,
    "sequence_flow_order": sequence_flow_order,
    "sequence_unresolved_steps": sequence_unresolved_steps,
    "sequence_fragment_condition_consistency": sequence_fragment_condition_consistency,
    "sequence_database_access_discipline": sequence_database_access_discipline,
    "sequence_self_call_method_validation": sequence_self_call_method_validation,
    "sequence_orphan_participant_detection": sequence_orphan_participant_detection,
    "sequence_duplicate_consecutive_messages": sequence_duplicate_consecutive_messages,
    "sequence_extension_replays_anchor_operation": sequence_extension_replays_anchor_operation,
    "sequence_message_naming_convention": sequence_message_naming_convention,
    "sequence_participant_kind_validity": sequence_participant_kind_validity,
    "sequence_message_type_validity": sequence_message_type_validity,
    "sequence_no_lifecycle_events": sequence_no_lifecycle_events,
}


# Legacy lane은 메시지 구조에서 의미·coverage 순으로 싼 검사를 먼저 실행한다. 등록 순서는
# 사용자에게 보이는 finding 순서이므로 set/dict 순회 결과로 재정렬하지 않는다.
SEQUENCE_CHECKS: tuple[CheckSpec[dict, dict], ...] = (
    CheckSpec("sequence.message-participants-exist", sequence_participants),
    CheckSpec("sequence.message-bce-flow", sequence_bce_flow),
    CheckSpec("sequence.boundary-operation-direction", sequence_boundary_operation_direction),
    CheckSpec("sequence.references-exist", sequence_traceability),
    CheckSpec("sequence.class-diagram-version", sequence_class_diagram_version),
    CheckSpec("sequence.participant-classes-exist", sequence_participant_classes),
    CheckSpec("sequence.message-labels-match-methods", sequence_message_methods),
    CheckSpec("sequence.initial-message-entry", sequence_initial_entry),
    CheckSpec("sequence.unmatched-return-message", sequence_unmatched_returns),
    CheckSpec("sequence.call-return-links", sequence_call_return_links),
    CheckSpec("sequence.return-label-matches-method-return", sequence_return_values_match_methods),
    CheckSpec("sequence.async-call-has-no-return", sequence_async_returns),
    CheckSpec("sequence.call-requires-return", sequence_calls_have_returns),
    CheckSpec("sequence.causal-call-chain", sequence_causal_call_chain),
    CheckSpec("sequence.argument-data-flow", sequence_argument_data_flow),
    CheckSpec("sequence.actor-step-involvement", sequence_actor_step_involvement),
    CheckSpec("sequence.usecase-step-coverage", sequence_usecase_coverage),
    CheckSpec("sequence.step-operation-distinctness", sequence_step_operation_distinctness),
    CheckSpec("sequence.flow-order", sequence_flow_order),
    CheckSpec("sequence.unresolved-usecase-step", sequence_unresolved_steps),
    CheckSpec("sequence.fragment-condition-consistency", sequence_fragment_condition_consistency),
    CheckSpec("sequence.database-access-discipline", sequence_database_access_discipline),
    CheckSpec("sequence.self-call-method-validation", sequence_self_call_method_validation),
    CheckSpec("sequence.orphan-participant-detection", sequence_orphan_participant_detection),
    CheckSpec("sequence.duplicate-consecutive-messages", sequence_duplicate_consecutive_messages),
    CheckSpec(
        "sequence.extension-replays-anchor-operation",
        sequence_extension_replays_anchor_operation,
    ),
    CheckSpec("sequence.message-naming-convention", sequence_message_naming_convention),
    CheckSpec("sequence.participant-kind-validity", sequence_participant_kind_validity),
    CheckSpec("sequence.message-type-validity", sequence_message_type_validity),
    CheckSpec("sequence.no-lifecycle-events", sequence_no_lifecycle_events),
)

def _sequence_rule_findings(model: dict, state: dict) -> list[Finding]:
    """legacy detector registry를 실행하고 기존 finding 목록으로 변환한다."""
    return _findings_from_report(run_checks(SEQUENCE_CHECKS, model or {}, state or {}))


def _collection_contract(model: dict, _state: dict) -> list[Finding]:
    return [
        Finding("sequence.call-return-links", message, "SequenceDiagramCollection")
        for message in interaction_sequence_findings(model or {})
    ]


def _collection_class_version(model: dict, state: dict) -> list[Finding]:
    expected_hash = hashlib.sha256(
        str(state.get("class_diagram_puml") or "").encode("utf-8")
    ).hexdigest()
    if str(model.get("class_diagram_hash") or "") == expected_hash:
        return []
    return [Finding(
        "sequence.class-diagram-version",
        "sequence model was not projected from the current class diagram",
        "SequenceDiagramCollection",
    )]


# 호출 순서와 BCE 의미는 원본인 클래스 collaboration에서 검사한다. 시퀀스는 그
# 수락된 모델의 결정론적 투영이므로 저장 스키마·call/return 참조와 클래스 버전만 본다.
# 여기서 같은 의미 규칙을 다시 실행하면 API 단계 직전에 새로운 사용자 수리가 생긴다.
SEQUENCE_COLLECTION_CHECKS: tuple[CheckSpec[dict, dict], ...] = (
    CheckSpec("sequence.call-return-links", _collection_contract),
    CheckSpec("sequence.class-diagram-version", _collection_class_version),
)


def validate_sequence_model(
    model: dict[str, Any], state: dict[str, Any]
) -> ValidationReport:
    """저장된 시퀀스 모델을 규칙 등록 순서대로 검증한다.

    Args:
        model: 시퀀스 컬렉션 또는 이전 단일 다이어그램 JSON이다.
        state: 유스케이스와 클래스 다이어그램 버전을 포함한 설계 상태다.

    Returns:
        규칙별 finding과 검사 오류를 포함한 불변 보고서다.

    Notes:
        ``Diagrams`` 컬렉션은 투영 자체의 최소 계약만 검사한다. 이전 단일
        다이어그램은 현재 생성 경로가 아니므로 기존 detector 집합에만 남아 있다.
    """
    diagrams = model.get("Diagrams") if isinstance(model, dict) else None
    checks = SEQUENCE_COLLECTION_CHECKS if isinstance(diagrams, list) else SEQUENCE_CHECKS
    return run_checks(checks, model or {}, state or {})


def sequence_diagram_findings(model: dict, state: dict) -> list[Finding]:
    """표시 계층을 위해 typed 보고서의 finding만 기존 목록 형태로 반환한다."""
    return _findings_from_report(validate_sequence_model(model or {}, state or {}))


__all__ = [
    "SEQUENCE_CHECKS",
    "SEQUENCE_COLLECTION_CHECKS",
    "SEQUENCE_DIAGRAM_DETECTORS",
    "Finding",
    "sequence_diagram_findings",
    "validate_sequence_model",
]
