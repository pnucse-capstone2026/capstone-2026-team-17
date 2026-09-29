"""배포 계획 projection이 공유하는 순서 보존 primitive를 제공한다."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


def slug(value: Any) -> str:
    """기존 ID slug 규칙으로 안정적인 projection 식별자를 만든다."""

    normalized = re.sub(
        r"[^a-z0-9]+",
        "-",
        str(value or "").strip().lower(),
    ).strip("-")
    return normalized or "item"


def refs(value: Any) -> list[str]:
    """입력 순서를 유지하며 비어 있지 않은 source reference를 중복 제거한다."""

    return list(dict.fromkeys(str(item) for item in value or [] if str(item).strip()))


def issue(
    field: str,
    reason: str,
    *,
    classification: str = "needsInput",
    source_refs: Iterable[str] = (),
) -> dict[str, Any]:
    """기존 key와 sourceRef 순서를 유지하는 계획 issue를 만든다."""

    return {
        "field": field,
        "classification": classification,
        "reason": reason,
        "sourceRefs": list(dict.fromkeys(str(item) for item in source_refs if item)),
    }


def derivation(
    rule: str,
    decision: str,
    *,
    source_refs: Iterable[str] = (),
) -> dict[str, Any]:
    """입력 순서를 유지하는 derivation과 정책 기본 reference를 만든다."""

    normalized_refs = list(dict.fromkeys(str(item) for item in source_refs if item))
    if not normalized_refs:
        normalized_refs = [f"project-policy:{rule}"]
    return {
        "rule": rule,
        "decision": decision,
        "sourceRefs": normalized_refs,
    }
