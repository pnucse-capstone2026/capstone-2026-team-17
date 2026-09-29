"""수락된 use-case 관계를 결정론적 PlantUML로 투영한다."""

from __future__ import annotations

import hashlib
import re
import textwrap
import unicodedata
from collections import defaultdict
from typing import cast

from app.requirements.common.state_contract import contract
from app.requirements.contracts.state import AgentState
from app.requirements.modeling.contracts import ModelingStagePatch


def _clean_text(value: object) -> str:
    visible = "".join(
        character
        for character in str(value or "")
        if unicodedata.category(character) != "Cf"
    )
    return " ".join(visible.split())


def _san(name: str) -> str:
    alias = re.sub(r"\W+", "_", name).strip("_") or "n"
    safe = alias if alias[0].isalpha() else f"n_{alias}"
    return f"{safe}_{hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]}"


def _plantuml_label(value: object) -> str:
    return _clean_text(value).replace("\\", "/").replace('"', "'")


def _label_lines(value: object, width: int) -> list[str]:
    compact = _plantuml_label(value)
    if not compact:
        return []
    return textwrap.wrap(
        compact,
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [compact]


def _extend_label(relation: dict[str, object]) -> str:
    condition = "\\n".join(_label_lines(relation.get("condition"), width=32))
    return f"<<extend>>\\n[{condition}]" if condition else "<<extend>>"


@contract(
    "render_diagram",
    requires=("relationships", "use_cases", "actors"),
    produces=("diagram",),
)
def render_diagram(state: AgentState) -> ModelingStagePatch:
    """검증된 안정 id 관계를 기존 순서의 PlantUML로 렌더링한다.

    Args:
        state: actor, use case와 accepted relationship을 담은 modeling 상태다.

    Returns:
        기존 ``diagram`` 문자열과 ``phase=diagram`` state patch다.

    Notes:
        LLM을 호출하지 않으며 alias·label·node·edge 정렬과 빈 diagram 결과를 보존한다.
    """
    actors = cast(list[dict[str, object]], state.get("actors") or [])
    use_cases = cast(list[dict[str, object]], state.get("use_cases") or [])
    rel = cast(dict[str, object], state.get("relationships") or {})
    if not use_cases:
        return {"diagram": "@startuml\n@enduml", "phase": "diagram"}

    actor_alias = {str(actor["actor_ref"]): _san(str(actor["actor_ref"])) for actor in actors}
    use_cases_by_id = {str(use_case["id"]): use_case for use_case in use_cases}
    derived = cast(list[dict[str, object]], rel.get("derived_use_cases") or [])
    derived_by_id = {str(item["use_case_id"]): item for item in derived}
    uc_alias = {
        use_case_id: _san(use_case_id)
        for use_case_id in [*use_cases_by_id, *derived_by_id]
    }
    extension_points: dict[str, list[str]] = defaultdict(list)
    for extension in cast(list[dict[str, object]], rel.get("extends") or []):
        base_id = str(extension.get("base_use_case_id") or "")
        point = _plantuml_label(
            extension.get("extension_point_name") or extension.get("extension_point")
        )
        if base_id in use_cases_by_id and point and point not in extension_points[base_id]:
            extension_points[base_id].append(point)

    primary_refs = {str(use_case.get("primary_actor_ref") or "") for use_case in use_cases}
    supporting_refs = {
        str(actor_ref)
        for use_case in use_cases
        for actor_ref in cast(list[object], use_case.get("supporting_actor_refs") or [])
    }
    primary = [
        actor
        for actor in actors
        if str(actor["actor_ref"]) in primary_refs
        or str(actor["actor_ref"]) not in supporting_refs
    ]
    supporting = [
        actor
        for actor in actors
        if str(actor["actor_ref"]) in supporting_refs
        and str(actor["actor_ref"]) not in primary_refs
    ]

    lines = ["@startuml", "left to right direction"]
    for actor in primary:
        name = str(actor["name"])
        lines.append(f'actor "{_plantuml_label(name)}" as {actor_alias[str(actor["actor_ref"])]}')
    lines.append("rectangle System {")
    for use_case in use_cases:
        use_case_id = str(use_case["id"])
        label = _plantuml_label(use_case.get("name"))
        point_lines = [
            line
            for point in extension_points.get(use_case_id, [])
            for line in _label_lines(point, width=40)
        ]
        if point_lines:
            label += "\\n-- extension points --\\n" + "\\n".join(point_lines)
        lines.append(f'  usecase "{label}" as {uc_alias[use_case_id]}')
    for item in derived:
        use_case_id = str(item["use_case_id"])
        lines.append(
            f'  usecase "{_plantuml_label(item.get("name"))}" as {uc_alias[use_case_id]}'
        )
    lines.append("}")
    for actor in supporting:
        name = str(actor["name"])
        lines.append(f'actor "{_plantuml_label(name)}" as {actor_alias[str(actor["actor_ref"])]}')

    for association in cast(list[dict[str, object]], rel.get("associations") or []):
        actor_ref = str(association.get("actor_ref") or "")
        use_case_id = str(association.get("use_case_id") or "")
        associated_use_case = use_cases_by_id.get(use_case_id)
        if actor_ref not in actor_alias or associated_use_case is None:
            continue
        supporting_actors = cast(
            list[object], associated_use_case.get("supporting_actor_refs") or []
        )
        if actor_ref in supporting_actors:
            lines.append(f"{uc_alias[use_case_id]} --- {actor_alias[actor_ref]}")
        else:
            lines.append(f"{actor_alias[actor_ref]} --- {uc_alias[use_case_id]}")
    for include in cast(list[dict[str, object]], rel.get("includes") or []):
        base_id = str(include.get("base_use_case_id") or "")
        included_id = str(include.get("included_use_case_id") or "")
        if base_id in uc_alias and included_id in uc_alias:
            lines.append(f"{uc_alias[base_id]} ..> {uc_alias[included_id]} : <<include>>")
    for extend in cast(list[dict[str, object]], rel.get("extends") or []):
        base_id = str(extend.get("base_use_case_id") or "")
        extending_id = str(extend.get("extending_use_case_id") or "")
        if base_id in uc_alias and extending_id in uc_alias:
            lines.append(
                f"{uc_alias[base_id]} <.. {uc_alias[extending_id]} : {_extend_label(extend)}"
            )
    for generalization in cast(
        list[dict[str, object]], rel.get("generalizations") or []
    ):
        if generalization.get("kind") != "actor":
            continue
        parent = str(generalization.get("parent_actor_ref") or "")
        child = str(generalization.get("child_actor_ref") or "")
        if parent in actor_alias and child in actor_alias:
            lines.append(f"{actor_alias[parent]} <|-- {actor_alias[child]}")
    lines.append("@enduml")
    return {"diagram": "\n".join(lines), "phase": "diagram"}


__all__ = ["render_diagram"]
