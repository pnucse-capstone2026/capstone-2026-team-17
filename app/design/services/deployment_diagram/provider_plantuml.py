"""Runtime·provisioning PlantUML renderer의 기존 public facade다."""

from __future__ import annotations

from typing import Any

from app.design.services.deployment_diagram.provisioning_renderer import (
    render_provisioning_dependencies,
)
from app.design.services.deployment_diagram.runtime_renderer import (
    render_runtime_deployment,
)


def deployment_bundle_runtime_puml(bundle: dict[str, Any]) -> str:
    """검증된 deployment bundle의 runtime PlantUML을 반환한다.

    Args:
        bundle: 단일 provider projection을 포함한 deployment bundle이다.

    Returns:
        기존 byte 순서를 유지한 runtime PlantUML 문자열이다.

    Notes:
        지원하지 않는 bundle schema는 기존 ValueError로 거부한다.
    """

    if bundle.get("schemaVersion") != "easydep-deployment-diagram":
        raise ValueError("unsupported deployment diagram schema")
    return render_runtime_deployment(bundle)


def deployment_bundle_provisioning_puml(bundle: dict[str, Any]) -> str:
    """검증된 deployment bundle의 provisioning PlantUML을 반환한다.

    Args:
        bundle: 단일 provider projection을 포함한 deployment bundle이다.

    Returns:
        기존 byte 순서를 유지한 provisioning PlantUML 문자열이다.

    Notes:
        지원하지 않는 bundle schema는 기존 ValueError로 거부한다.
    """

    if bundle.get("schemaVersion") != "easydep-deployment-diagram":
        raise ValueError("unsupported deployment diagram schema")
    return render_provisioning_dependencies(bundle)


__all__ = [
    "deployment_bundle_provisioning_puml",
    "deployment_bundle_runtime_puml",
    "render_provisioning_dependencies",
    "render_runtime_deployment",
]
