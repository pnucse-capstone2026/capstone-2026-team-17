"""시퀀스 상호작용 모델(JSON)을 시퀀스 다이어그램 PlantUML로 변환한다.

클래스 다이어그램(class_diagram.plantuml)·ERD와 같은 결정론적 변환이다. 참가자 이름과
라벨은 LLM이 만든 자유 텍스트라 PlantUML 구조 문자를 품을 수 있으므로, 식별자는 단어
문자로만 남기고 라벨은 한 줄로 중화한다. 그래서 이 변환은 "구성에 의해" 항상 문법적으로
유효한 PlantUML을 낸다 — 별도의 문법 수리 루프가 필요 없다.

jar 실행·렌더는 common.plantuml이 맡고, 여기서는 시퀀스 다이어그램 고유의
"무엇을 그릴지"(참가자 종류·화살표 모양·조각 묶기)만 다룬다.
"""
from __future__ import annotations

import re
from typing import Any

#: 참가자 종류 → PlantUML 선언 키워드. BCE 스테레오타입을 그림으로 잇는다.
_PARTICIPANT_KEYWORD = {
    "actor": "actor",
    "boundary": "boundary",
    "control": "control",
    "entity": "entity",
    "database": "database",
}

# lifeline은 입력 배열 순서가 아니라 architecture 역할 순서로 배치한다. 사용하지 않는
# 역할은 생략해도, 존재하는 역할은 항상 같은 좌→우 영역에 있어 실행 간 시각 diff가 작다.
_PARTICIPANT_ORDER = {
    "actor": 0,
    "boundary": 1,
    "control": 2,
    "entity": 3,
    "database": 4,
}

#: 메시지 종류 → PlantUML 화살표.
_ARROW = {
    "sync": "->",
    "async": "->>",
    "return": "-->",
    "self": "->",
}

#: 조각 종류 → PlantUML 블록 키워드. 모르는 값은 그리지 않고 주 흐름으로 둔다.
_FRAGMENTS = {"alt": "alt", "loop": "loop", "opt": "opt"}


def sanitize_identifier(name: str) -> str:
    """참가자 별칭에 안전한 단어 문자만 남긴다."""
    if not name:
        return "Unknown"
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def sanitize_text(text: str) -> str:
    """라벨/조건 텍스트를 한 줄의 PlantUML-안전 토큰으로 만든다."""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("‑", "-")
    # 줄바꿈은 이미 없앴고, 따옴표는 라벨을 깨뜨리므로 바꾼다.
    return text.replace('"', "'")


def _display_message_label(label: str, message_type: str) -> str:
    """모델 시그니처의 이름은 보존하면서 긴 parameter 목록만 여러 줄로 표시한다."""

    safe = sanitize_text(label)
    if message_type not in {"sync", "self"}:
        return safe
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\((.*)\)", safe)
    if match is None or not match.group(2):
        return safe
    raw = match.group(2)
    arguments: list[str] = []
    current: list[str] = []
    depth = 0
    for character in raw:
        if character == "<":
            depth += 1
        elif character == ">":
            depth = max(0, depth - 1)
        if character == "," and depth == 0:
            arguments.append("".join(current).partition(":")[0].strip())
            current = []
        else:
            current.append(character)
    arguments.append("".join(current).partition(":")[0].strip())
    rows = [", ".join(arguments[index: index + 3]) for index in range(0, len(arguments), 3)]
    return f"{match.group(1)}(" + "\\n".join(rows) + ")"


def generate_sequence_from_model(model: dict[str, Any]) -> str:
    """상호작용 모델을 시퀀스 다이어그램 PlantUML로 변환한다.

    Args:
        model: 현재 컬렉션 또는 유스케이스 하나의 시퀀스 JSON이다.

    Returns:
        컬렉션이면 다이어그램별 문서, 단일 모델이면 한 PlantUML 문서다. 완전히 빈
        모델만 빈 문자열이고 unresolved step은 검토 가능한 note로 그린다.

    Notes:
        이 함수는 renderer다. 의미를 수리하거나 LLM을 호출하지 않으며 미선언 participant를
        자동 생성하지 않는다.
    """
    if not model:
        return ""

    diagrams = model.get("Diagrams")
    if isinstance(diagrams, list):
        return "\n\n".join(
            rendered
            for diagram in diagrams
            if isinstance(diagram, dict)
            and (rendered := generate_sequence_from_model(diagram))
        )

    participants = model.get("Participants", [])
    messages = model.get("Messages", [])
    unresolved_steps = [
        item for item in model.get("UnresolvedSteps", []) if isinstance(item, dict)
    ]
    if not participants and not messages and not unresolved_steps:
        return ""

    diagram_id = sanitize_identifier(str(model.get("use_case_id") or ""))
    start = f"@startuml {diagram_id}" if model.get("use_case_id") else "@startuml"
    lines = [
        start,
        "!theme plain",
        "hide footbox",
        "skinparam sequenceMessageAlign center",
    ]
    title = " - ".join(
        value
        for value in (
            sanitize_text(str(model.get("use_case_id") or "")),
            sanitize_text(str(model.get("use_case_name") or "")),
        )
        if value
    )
    if title:
        lines.append(f"title {title}")
    lines.append("")

    # 선언된 참가자만 메시지에 쓸 수 있다. 모델이 어긋나 선언에 없는 이름이 나오면
    # 그 메시지는 버린다 — 미선언 참가자를 그리면 그림이 조용히 거짓말을 한다.
    declared: set[str] = set()
    for participant in sorted(
        (item for item in participants if isinstance(item, dict)),
        key=lambda item: (
            _PARTICIPANT_ORDER.get(str(item.get("kind", "")).strip().lower(), 5),
            sanitize_identifier(str(item.get("alias") or item.get("name", ""))),
        ),
    ):
        alias = sanitize_identifier(participant.get("alias") or participant.get("name", ""))
        if alias in declared:
            continue
        declared.add(alias)

        keyword = _PARTICIPANT_KEYWORD.get(
            str(participant.get("kind", "")).strip().lower(), "participant"
        )
        display = sanitize_text(participant.get("name", "")) or alias
        lines.append(f'{keyword} "{display}" as {alias}')

    # 의미 매핑이 실패해도 review용 문서와 gallery card는 남긴다. fallback은 실제 class
    # participant가 아니라 actor/Boundary조차 없는 잘못된 import에 note를 붙일 anchor다.
    if unresolved_steps and not declared:
        declared.add("Unresolved")
        lines.append('participant "Unresolved interaction" as Unresolved')

    lines.append("")

    # 현재 열린 fragment 경로. 새 모델은 메시지마다 바깥→안쪽 경로를 들고 있어
    # alt/else와 중첩을 잃지 않는다. 옛 저장본의 group/condition은 아래에서 깊이 1의
    # fragment로 읽어 재생 가능성을 유지한다.
    open_fragments: list[dict[str, str]] = []

    def fragment_path(message: dict[str, Any]) -> list[dict[str, str]]:
        path = message.get("fragments")
        if isinstance(path, list):
            return [item for item in path if isinstance(item, dict)]
        legacy_type = _FRAGMENTS.get(str(message.get("group", "")).strip().lower(), "")
        if not legacy_type:
            return []
        condition = sanitize_text(message.get("condition", ""))
        return [{
            "id": f"legacy:{legacy_type}:{condition}",
            "type": legacy_type,
            "branch": "main",
            "condition": condition,
        }]

    def close_to(depth: int) -> None:
        while len(open_fragments) > depth:
            lines.append("end")
            open_fragments.pop()

    def transition_fragments(wanted: list[dict[str, str]]) -> None:
        common = 0
        limit = min(len(open_fragments), len(wanted))
        while common < limit:
            current = open_fragments[common]
            candidate = wanted[common]
            if current.get("id") != candidate.get("id") or current.get("type") != candidate.get("type"):
                break
            if current.get("branch", "main") != candidate.get("branch", "main"):
                # 같은 alt의 다른 branch는 fragment를 닫지 않고 PlantUML else로 전환한다.
                close_to(common + 1)
                if current.get("type") == "alt" and candidate.get("branch") == "else":
                    lines.append(f"else {sanitize_text(candidate.get('condition', ''))}".rstrip())
                    open_fragments[common] = dict(candidate)
                    common += 1
                break
            common += 1

        close_to(common)
        for fragment in wanted[common:]:
            kind = _FRAGMENTS.get(str(fragment.get("type", "")).lower(), "")
            if not kind:
                continue
            condition = sanitize_text(fragment.get("condition", ""))
            lines.append(f"{kind} {condition}".rstrip())
            open_fragments.append(dict(fragment))

    for message in messages:
        source = sanitize_identifier(message.get("source", ""))
        target = sanitize_identifier(message.get("target", ""))
        if source not in declared or target not in declared:
            continue

        message_type = str(message.get("type", "sync")).strip().lower()
        # 공통 템플릿은 activation bar를 사용하지 않는다. 과거 모델의 lifecycle event는
        # fragment 전환 전에 버려 빈 fragment나 다른 그림과 다른 시각 문법을 남기지 않는다.
        if message_type in {"activate", "deactivate"}:
            continue

        transition_fragments(fragment_path(message))

        arrow = _ARROW.get(message_type, "->")
        label = _display_message_label(
            str(message.get("label", "")), message_type,
        )
        line = f"{source} {arrow} {target}"
        if label:
            line += f" : {label}"
        lines.append(f"  {line}" if open_fragments else line)

    close_to(0)

    if unresolved_steps:
        anchor = min(declared) if declared else "Unresolved"
        lines.append("")
        lines.append(f"note over {anchor}")
        lines.append("  Needs review: no grounded class method was selected.")
        for item in unresolved_steps:
            step_id = sanitize_text(str(item.get("step_id") or "step"))
            reason = sanitize_text(str(item.get("reason") or "Unresolved step"))
            lines.append(f"  {step_id}: {reason}")
        lines.append("end note")

    lines.append("")
    lines.append("@enduml")

    return "\n".join(lines).replace("\xa0", " ").replace("​", "")
