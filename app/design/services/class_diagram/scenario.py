"""원시 유스케이스 JSON을 모든 클래스·시퀀스 단계가 공유하는 인덱스로 정규화한다.

입력은 graph adapter가 전달한 기존 use-case JSON이고 출력은 frozen ``ScenarioIndex``다.
main/extension/precondition을 안정적인 provenance ID로 바꾸며 include·extend 관계와 actor
entry 단위의 ``ExecutionGroup``을 한 번 계산한다. 이후 단계가 각자 raw JSON을 해석하지
않게 해 같은 단계가 operation, collaboration, sequence에서 다른 ID를 갖는 일을 막는다.

이 모듈은 순수 변환만 수행한다. LLM, 설정, 저장소, graph state에 의존하지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


def text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def id_key(value: str) -> tuple[Any, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    )


@dataclass(frozen=True)
class Step:
    """한 유스케이스 흐름 문장과 안정적인 provenance 식별자다."""
    id: str
    use_case_id: str
    subject: str
    subject_ref: str
    sentence: str
    order: int
    branch: str
    condition: str = ""
    extension_ref: str = ""


@dataclass(frozen=True)
class UseCase:
    """정규화한 한 유스케이스와 그 단계·precondition 원천이다."""
    id: str
    name: str
    primary_actor: str
    primary_actor_ref: str
    specification: dict[str, Any]
    steps: tuple[Step, ...]
    precondition_refs: tuple[str, ...]


@dataclass(frozen=True)
class Relationship:
    """별칭을 실제 ID로 해소한 include 또는 extend 관계다."""
    kind: str
    base_id: str
    child_id: str
    anchor_step_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionGroup:
    """독립적으로 operation/collaboration을 생성할 수 있는 actor-entry 실행 슬라이스다."""
    id: str
    use_case_id: str
    step_ids: tuple[str, ...]
    actor_step: str | None
    entry_actor: str | None
    trace_use_case_ids: tuple[str, ...]
    required_step_ids: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioIndex:
    """설계 단계 전체에서 공유하는 불변 시나리오 조회 모델이다."""
    raw: dict[str, Any]
    use_cases: tuple[UseCase, ...]
    relationships: tuple[Relationship, ...]
    groups: tuple[ExecutionGroup, ...]

    def use_case(self, use_case_id: str) -> UseCase:
        for use_case in self.use_cases:
            if use_case.id == use_case_id:
                return use_case
        raise KeyError(use_case_id)

    @property
    def step_ids(self) -> frozenset[str]:
        return frozenset(step.id for use_case in self.use_cases for step in use_case.steps)


def _steps(
    use_case_id: str,
    specification: dict[str, Any],
    actor_names: dict[str, str],
) -> tuple[Step, ...]:
    result: list[Step] = []
    order = 0
    for raw in specification.get("main_scenario") or []:
        if not isinstance(raw, dict) or raw.get("step_number") is None:
            continue
        result.append(Step(
            id=f"{use_case_id}:main:{raw['step_number']}",
            use_case_id=use_case_id,
            subject=("System" if text(raw.get("subject_ref")) == "system"
                     else actor_names.get(text(raw.get("subject_ref")), "")),
            subject_ref=text(raw.get("subject_ref")),
            sentence=text(raw.get("sentence") or raw.get("description")),
            order=order,
            branch="main",
        ))
        order += 1
    for extension, extension_ref in _extension_specs(use_case_id, specification):
        label = text(extension.get("label"))
        condition = text(extension.get("condition"))
        for raw in extension.get("handling_steps") or []:
            if not isinstance(raw, dict) or raw.get("sub_step") is None:
                continue
            result.append(Step(
                id=f"{extension_ref}:{raw['sub_step']}",
                use_case_id=use_case_id,
                subject=("System" if text(raw.get("subject_ref")) == "system"
                         else actor_names.get(text(raw.get("subject_ref")), "")),
                subject_ref=text(raw.get("subject_ref")),
                sentence=text(raw.get("sentence") or raw.get("description")),
                order=order,
                branch=label,
                condition=condition,
                extension_ref=extension_ref,
            ))
            order += 1
    return tuple(result)


def _extension_specs(
    use_case_id: str, specification: dict[str, Any],
) -> list[tuple[dict[str, Any], str]]:
    """Return extensions paired with identity from their anchor and sibling order."""
    ordinals: dict[str, int] = {}
    result: list[tuple[dict[str, Any], str]] = []
    for extension in specification.get("extensions") or []:
        if not isinstance(extension, dict):
            continue
        anchor = text(extension.get("branch_step"))
        if not anchor:
            raise ValueError(f"extension in {use_case_id} requires branch_step")
        ordinals[anchor] = ordinals.get(anchor, 0) + 1
        result.append((extension, f"{use_case_id}:extension:{anchor}:{ordinals[anchor]}"))
    return result


def _preconditions(use_case_id: str, specification: dict[str, Any]) -> tuple[str, ...]:
    raw = specification.get("preconditions") or []
    values = list(raw.values()) if isinstance(raw, dict) else list(raw)
    return tuple(
        f"{use_case_id}:precondition:{index}"
        for index, value in enumerate(values, start=1)
        if text(value)
    )


def _actor_steps(use_case: UseCase) -> set[str]:
    """새 시스템 처리를 시작하는 주 액터 단계만 반환한다.

    첫 액터 단계는 유스케이스의 시작점이다. 이후 액터 단계는 다음 시스템 단계가 있을
    때만 새 입력으로 본다. 마지막의 "사용자가 결과를 받는다" 같은 단계는 이미 앞선
    시스템 호출의 결과이므로 별도 호출 루트를 만들지 않는다.
    """

    actor_ref = use_case.primary_actor_ref
    if not actor_ref:
        return set()
    main_steps = [step for step in use_case.steps if step.branch == "main"]
    candidates = [
        position for position, step in enumerate(main_steps)
        if step.subject_ref == actor_ref
    ]
    if not candidates:
        return set()
    entries = {main_steps[candidates[0]].id}
    for ordinal, position in enumerate(candidates[1:], start=1):
        previous_actor = candidates[ordinal - 1]
        next_actor = candidates[ordinal + 1] if ordinal + 1 < len(candidates) else len(main_steps)
        # 같은 actor가 요청 내용을 여러 문장으로 연달아 제공한 것은 하나의 입력이다.
        # 두 actor 단계 사이에 시스템 처리가 있었을 때만 후속 입력을 새 호출 root로
        # 나눈다. 그렇지 않으면 데이터가 아직 없는 첫 문장만 별도 Boundary→Control
        # 흐름이 되어 의미 없는 start 메서드를 만들게 된다.
        if previous_actor + 1 < position and position + 1 < next_actor:
            entries.add(main_steps[position].id)
    return entries


def _relationships(
    raw: dict[str, Any], use_cases: tuple[UseCase, ...],
) -> tuple[Relationship, ...]:
    source = raw.get("relationships")
    if not isinstance(source, dict):
        return ()
    accepted_ids = {use_case.id for use_case in use_cases}
    result: list[Relationship] = []
    for kind, collection, child_keys in (
        ("include", "includes", "included_use_case_id"),
        ("extend", "extends", "extending_use_case_id"),
    ):
        for item in source.get(collection) or []:
            if not isinstance(item, dict):
                continue
            base_id = text(item.get("base_use_case_id"))
            child_id = text(item.get(child_keys))
            if base_id not in accepted_ids or child_id not in accepted_ids or base_id == child_id:
                continue
            anchors = {
                f"{base_id}:{text(ref.get('step_ref'))}"
                for ref in item.get("step_refs") or []
                if isinstance(ref, dict)
                and text(ref.get("use_case_id")) == base_id
                and text(ref.get("step_ref"))
            }
            if kind == "extend" and not anchors and text(item.get("extension_point")):
                anchors.add(f"{base_id}:{text(item.get('extension_point'))}")
            result.append(Relationship(kind, base_id, child_id, tuple(sorted(anchors))))
    unique = {(item.kind, item.base_id, item.child_id, item.anchor_step_ids): item for item in result}
    return tuple(unique[key] for key in sorted(unique))


def _groups(
    use_cases: tuple[UseCase, ...], relationships: tuple[Relationship, ...],
) -> tuple[ExecutionGroup, ...]:
    by_id = {use_case.id: use_case for use_case in use_cases}
    # actor가 없는 included use case는 호출자의 한 execution group 안에서 실행된다. 별도
    # root group까지 만들면 같은 step을 두 collaboration이 중복 소유하게 된다.
    internal_includes = {
        relation.child_id for relation in relationships
        if relation.kind == "include" and not _actor_steps(by_id[relation.child_id])
    }
    groups: list[ExecutionGroup] = []
    for use_case in use_cases:
        if not use_case.steps or use_case.id in internal_includes:
            continue
        actor_steps = _actor_steps(use_case)
        main_steps = [step for step in use_case.steps if step.branch == "main"]
        active: str | None = None
        leading_steps: list[str] = []
        grouped: dict[str, list[str]] = {}
        owner_by_step: dict[str, str] = {}
        # 새 시스템 처리를 시작하는 actor 입력부터 다음 입력 직전까지가 호출 루트 하나다.
        # actor가 결과를 받는 마지막 단계는 새 입력이 아니므로 앞선 루트에 포함된다.
        # 화면 표시처럼 첫 actor 입력보다 앞선 시스템 단계는 첫 루트의 준비 과정이다.
        # 이를 버리면 클래스 모델은 통과해도 시퀀스에서 갑자기 누락 단계가 생긴다.
        # actor 입력이 전혀 없는 use case는 root 그룹 하나로 다룬다.
        for step in main_steps:
            if step.id in actor_steps:
                active = step.id
                group = grouped.setdefault(active, [])
                if leading_steps:
                    group.extend(leading_steps)
                    owner_by_step.update(dict.fromkeys(leading_steps, active))
                    leading_steps.clear()
            if active:
                grouped[active].append(step.id)
                owner_by_step[step.id] = active
            else:
                leading_steps.append(step.id)
        if not grouped:
            root = f"{use_case.id}:root"
            grouped[root] = [step.id for step in main_steps]
            # actor 표기가 요약과 문장에서 다르면 안전하게 유스케이스 전체를 root 하나로
            # 묶는다. 이때도 extension의 branch step이 어느 root에 속하는지는 남겨야
            # 조건 흐름이 required step과 이후 collaboration에서 빠지지 않는다.
            owner_by_step.update({step.id: root for step in main_steps})
        # extension handling은 branch_step을 포함하는 actor slice에 붙인다. 별도 group으로
        # 떼면 조건 분기의 call/return이 주 흐름과 인과적으로 분리된다.
        for extension, extension_ref in _extension_specs(use_case.id, use_case.specification):
            branch_step = text(extension.get("branch_step"))
            owner = owner_by_step.get(f"{use_case.id}:main:{branch_step}")
            if owner and extension_ref:
                grouped[owner].extend(
                    step.id for step in use_case.steps
                    if step.extension_ref == extension_ref
                )
        for group_id, base_steps in grouped.items():
            actor_step = group_id if group_id in actor_steps else None
            trace_ids = [use_case.id]
            required = list(base_steps)
            # include의 anchor가 현재 slice에 있을 때만 child 단계를 required set으로 합친다.
            # anchor 없는 include는 use case 전역 관계이므로 모든 해당 slice에서 추적한다.
            for relation in relationships:
                if relation.kind != "include" or relation.base_id != use_case.id:
                    continue
                if relation.anchor_step_ids and not set(relation.anchor_step_ids) & set(base_steps):
                    continue
                child = by_id.get(relation.child_id)
                if child:
                    trace_ids.append(child.id)
                    required.extend(step.id for step in child.steps)
            groups.append(ExecutionGroup(
                id=group_id,
                use_case_id=use_case.id,
                step_ids=tuple(base_steps),
                actor_step=actor_step,
                entry_actor=use_case.primary_actor if actor_step else None,
                trace_use_case_ids=tuple(dict.fromkeys(trace_ids)),
                required_step_ids=tuple(dict.fromkeys(required)),
            ))
    return tuple(groups)


def build_scenario_index(raw: dict[str, Any]) -> ScenarioIndex:
    """기존 use-case JSON을 정렬되고 불변인 설계 입력으로 검증·정규화한다.

    Args:
        raw: ``use_cases``, ``use_case_specs``와 선택적 ``relationships``를 포함한 JSON이다.

    Returns:
        canonical step ID, 해소된 관계와 execution group을 가진 ``ScenarioIndex``다.

    Raises:
        TypeError: 최상위 입력이 JSON object가 아닌 경우다.
        ValueError: 같은 use-case specification ID가 중복된 경우다.

    Notes:
        main 단계 ``UC1:main:2``와 extension 단계 ``UC1:extension:2a:1``처럼 ID를
        만든다. 입력 배열 순서가 달라도 use-case ID의 자연 정렬 순서로 결과가 고정된다.
    """
    if not isinstance(raw, dict):
        raise TypeError("use-case specification must be an object")
    summaries = {
        text(item.get("id")): item
        for item in raw.get("use_cases") or []
        if isinstance(item, dict) and text(item.get("id"))
    }
    actor_names = {
        text(actor.get("actor_ref")): text(actor.get("name"))
        for actor in raw.get("actors") or []
        if isinstance(actor, dict) and text(actor.get("actor_ref")) and text(actor.get("name"))
    }
    use_cases: list[UseCase] = []
    seen: set[str] = set()
    specifications = sorted(
        (
            item for item in raw.get("use_case_specs") or []
            if isinstance(item, dict) and text(item.get("use_case_id"))
        ),
        key=lambda item: id_key(text(item.get("use_case_id"))),
    )
    for specification in specifications:
        use_case_id = text(specification.get("use_case_id"))
        if use_case_id in seen:
            raise ValueError(f"duplicate use-case specification: {use_case_id}")
        seen.add(use_case_id)
        summary = summaries.get(use_case_id, {})
        use_cases.append(UseCase(
            id=use_case_id,
            name=text(summary.get("name") or specification.get("use_case_name")),
            primary_actor=text(
                specification.get("primary_actor") or summary.get("primary_actor")
            ),
            primary_actor_ref=text(
                specification.get("primary_actor_ref") or summary.get("primary_actor_ref")
            ),
            specification=specification,
            steps=_steps(use_case_id, specification, actor_names),
            precondition_refs=_preconditions(use_case_id, specification),
        ))
    # raw relationship은 이름 또는 ID를 사용할 수 있다. 모든 use case를 수락한 뒤에만
    # alias를 해소해야 중복 이름을 임의의 ID에 연결하지 않는다.
    accepted = tuple(use_cases)
    relationships = _relationships(raw, accepted)
    return ScenarioIndex(raw, accepted, relationships, _groups(accepted, relationships))
