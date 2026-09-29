"""검증된 implementation RTM ref를 작업 파일로 투영한다."""

from __future__ import annotations

from typing import Any, TypedDict


class _TargetCandidate(TypedDict):
    file: str
    refs: list[str]


def resolve_feedback_targets(
    rtm_map: dict[str, Any] | None,
    *,
    confirmed_refs: list[str],
) -> dict[str, object]:
    """Workspace가 검증한 ref와 실제 RTM의 교집합만 구현 작업에 전달한다."""

    candidates = _target_candidates(rtm_map)
    available = {
        ref
        for candidate in candidates
        for ref in candidate["refs"]
        if isinstance(ref, str)
    }
    confirmed = sorted(set(confirmed_refs) & available)
    files = sorted(
        {
            str(candidate["file"])
            for candidate in candidates
            if set(candidate["refs"]) & set(confirmed)
        }
    )
    return {
        "source": "confirmed",
        "confirmedTargetRefs": confirmed,
        "relatedFiles": files,
    }


def _target_candidates(rtm_map: dict[str, Any] | None) -> list[_TargetCandidate]:
    mappings = rtm_map.get("mappings") if isinstance(rtm_map, dict) else None
    if not isinstance(mappings, list):
        return []
    result: list[_TargetCandidate] = []
    for item in mappings:
        if not isinstance(item, dict):
            continue
        file_path = str(item.get("target_file") or "").strip()
        if not file_path:
            continue
        refs = {
            str(ref)
            for ref in item.get("sourceRefs") or []
            if isinstance(ref, str) and ref
        }
        task_id = str(item.get("taskId") or "").strip()
        if task_id:
            refs.add(f"task:{task_id}")
        refs.add(f"file:{file_path}")
        result.append({"file": file_path, "refs": sorted(refs)})
    return result


__all__ = ["resolve_feedback_targets"]
