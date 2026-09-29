"""Map visible class execution rows to their bounded artifact merge owners."""
from __future__ import annotations

from typing import Any

from app.artifact_trace import TraceRef


class UnknownClassMergeTarget(ValueError):
    pass


class AmbiguousClassMergeTarget(ValueError):
    pass


def class_execution_merge_targets(
    model: dict[str, Any], requested_targets: set[str]
) -> set[str]:
    """Return top-level class/collaboration owners for class execution refs."""
    class_names = {
        str(item.get("className") or "").strip()
        for item in model.get("Classes") or []
        if isinstance(item, dict) and str(item.get("className") or "").strip()
    }
    operation_owners: dict[str, set[str]] = {}
    collaboration_ids: set[str] = set()
    call_owners: dict[str, set[str]] = {}
    for item in model.get("Classes") or []:
        if not isinstance(item, dict):
            continue
        owner = str(item.get("className") or "").strip()
        for operation in item.get("operations") or []:
            if isinstance(operation, dict) and str(operation.get("operationId") or "").strip():
                operation_owners.setdefault(str(operation["operationId"]).strip(), set()).add(owner)
    for item in model.get("Collaborations") or []:
        if not isinstance(item, dict):
            continue
        collaboration = str(item.get("collaborationId") or "").strip()
        if not collaboration:
            continue
        collaboration_ids.add(collaboration)
        for call in item.get("calls") or []:
            if isinstance(call, dict) and str(call.get("callId") or "").strip():
                call_owners.setdefault(str(call["callId"]).strip(), set()).add(collaboration)

    merged: set[str] = set()
    for ref in requested_targets:
        try:
            parsed = TraceRef.parse(ref)
        except (TypeError, ValueError):
            parsed = None
        candidate = parsed.id if parsed is not None and parsed.kind == "class_diagram" else ref
        owners = operation_owners.get(candidate, set())
        call_id = candidate.split("#", 1)[0]
        call_collaborations = call_owners.get(call_id, set())
        if candidate in class_names or candidate in collaboration_ids:
            merged.add(candidate)
        elif len(owners) == 1:
            merged.update(owners)
        elif len(call_collaborations) == 1:
            merged.update(call_collaborations)
        elif len(owners) > 1 or len(call_collaborations) > 1:
            raise AmbiguousClassMergeTarget(
                f"Class execution target {ref!r} maps to more than one merge unit."
            )
        else:
            raise UnknownClassMergeTarget(
                f"{ref} is not a current class execution target."
            )
    return merged
