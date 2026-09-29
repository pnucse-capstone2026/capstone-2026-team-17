"""기존 dict 기반 WorkloadGraph 수정 import를 보존하는 호환 facade다."""
from __future__ import annotations

from typing import Any

from app.design.services.deployment_diagram.models import DeploymentModel, WorkloadGraph
from app.design.services.deployment_diagram.prompts import (
    DEPLOYMENT_LABEL_REVISION_SYSTEM_PROMPT,
)
from app.design.services.deployment_diagram.service import revise_workload_graph


def revise_deployment_model(
    current_model: dict[str, Any],
    feedback: str,
    targets: set[str] | None = None,
) -> dict[str, Any]:
    """기존 dict 입력을 이름만 수정하는 typed service에 연결한다."""

    if not current_model or not feedback:
        return current_model or {}
    revised = revise_workload_graph(
        WorkloadGraph.model_validate(current_model),
        feedback,
        targets,
    )
    return revised.model_dump()


__all__ = [
    "DEPLOYMENT_LABEL_REVISION_SYSTEM_PROMPT",
    "DeploymentModel",
    "revise_deployment_model",
]
