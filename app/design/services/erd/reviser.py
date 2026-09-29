"""기존 dict 기반 ERD BCE 수정 import를 보존하는 호환 facade다."""
from __future__ import annotations

from typing import Any

from app.design.schemas.class_model import BCEModel
from app.design.services.erd.service import (
    ERD_BCE_REVISION_SYSTEM_PROMPT,
    revise_erd_model,
)


def revise_erd_classes(
    current_bce: dict[str, Any],
    feedback: str,
    scenario_text: str = "",
    targets: set[str] | None = None,
) -> dict[str, Any]:
    """기존 dict 입력과 alias JSON 반환 shape를 typed service에 연결한다."""

    if not current_bce or not feedback:
        return current_bce or {}
    revised = revise_erd_model(
        BCEModel.model_validate(current_bce),
        feedback,
        scenario_text,
        targets,
    )
    return revised.model_dump(by_alias=True)


__all__ = ["ERD_BCE_REVISION_SYSTEM_PROMPT", "revise_erd_classes"]
