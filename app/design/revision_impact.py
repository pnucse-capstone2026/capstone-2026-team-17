"""Deterministic design impact scope shared by planning and execution.

The broad design RTM answers where a class is used. A revision needs a
smaller answer: which separately persisted design projections must be updated
for the exact selected element. Keeping this calculation pure lets callers
freeze the same scope before execution.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.artifact_trace import TraceRef
from app.design.rtm import affected_by_element, linked_elements

_CASCADE_STAGES = frozenset(
    {"sequence_diagram", "api_spec", "erd", "deployment_diagram"}
)


def design_revision_impact(
    state: Mapping[str, object],
    rtm: Mapping[str, object],
    stage: str,
    element: str,
) -> dict[str, set[str]]:
    """Return the exact persisted projections affected by one design edit.

    Exact operation/collaboration/call targets follow only strong design links.
    A whole class target additionally follows its broad forward provenance.
    Nested members of that same class artifact are merge details, not separate
    downstream approvals, so they are deliberately absent from this result.
    """

    if stage != "class_diagram":
        return {}

    scheduled: dict[str, set[str]] = {}
    for value in linked_elements(dict(rtm), stage, element):
        target = _design_ref(value)
        if target is None or target.kind not in {"sequence_diagram", "api_spec"}:
            continue
        scheduled.setdefault(target.kind, set()).add(target.id)

    class_names = {
        str(item.get("className") or "").strip()
        for item in _records(_mapping(state.get("extracted_bce_classes")).get("Classes"))
        if str(item.get("className") or "").strip()
    }
    if element in class_names:
        for value in affected_by_element(dict(rtm), stage, element):
            target = _design_ref(value)
            if target is None or target.kind not in _CASCADE_STAGES:
                continue
            scheduled.setdefault(target.kind, set()).add(target.id)

    scheduled.get(stage, set()).discard(element)
    return {key: values for key, values in scheduled.items() if values}


def format_design_revision_impact(scope: Mapping[str, set[str]]) -> list[str]:
    """Format a design scope as public refs used by Workspace planning."""

    result: list[str] = []
    for stage, elements in scope.items():
        for element in elements:
            result.append(
                str(TraceRef("entity", element))
                if stage == "erd"
                else str(TraceRef(stage, element))
            )
    return sorted(set(result))


def _design_ref(value: str) -> TraceRef | None:
    try:
        parsed = TraceRef.parse(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.kind in _CASCADE_STAGES | {"class_diagram"} else None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _records(value: object) -> list[Mapping[str, object]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


__all__ = ["design_revision_impact", "format_design_revision_impact"]
