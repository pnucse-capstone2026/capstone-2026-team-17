"""기존 dict·PlantUML 기반 WorkloadGraph 생성 import를 보존하는 호환 facade다."""
from __future__ import annotations

from typing import Any

from app.design.services.deployment_diagram.models import (
    DeploymentModel,
    ExternalDependency,
    ResourceRequirements,
    Workload,
    WorkloadArtifact,
    WorkloadConfiguration,
    WorkloadConnection,
    WorkloadConstraint,
    WorkloadGraph,
    WorkloadGraphProposal,
    WorkloadInterface,
    WorkloadStorage,
)
from app.design.services.deployment_diagram.prompts import DEPLOYMENT_LABEL_SYSTEM_PROMPT
from app.design.services.deployment_diagram.service import propose_workload_graph


def extract_deployment_model(
    scenario_text: str,
    class_diagram_puml: str,
    sequence_diagram_puml: str,
    api_spec: dict[str, Any],
    erd_puml: str,
    *,
    refined_requirements: Any = None,
    capability_contract: dict[str, Any] | None = None,
    resource_intake: dict[str, Any] | None = None,
    resource_spec: dict[str, Any] | None = None,
    class_model: Any = None,
    sequence_model: Any = None,
    erd_model: Any = None,
    deployment_planning_facts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """기존 입력 형식을 템플릿 구조와 이름 제안 서비스에 연결한다."""

    if not scenario_text:
        return {}
    structured = {
        "refinedRequirements": refined_requirements or [],
        "capabilityContract": capability_contract or {},
        "resourceIntake": resource_intake or {},
        "resourceSpec": resource_spec or {},
        "useCaseSpecification": scenario_text,
        "apiSpec": api_spec,
        "deploymentPlanningFacts": deployment_planning_facts or [],
    }
    if class_model:
        structured["classModel"] = class_model
    else:
        structured["classDiagramPlantUML"] = class_diagram_puml
    if sequence_model:
        structured["sequenceModel"] = sequence_model
    else:
        structured["sequenceDiagramPlantUML"] = sequence_diagram_puml
    if erd_model:
        structured["erdModel"] = erd_model
    else:
        structured["erdPlantUML"] = erd_puml
    return propose_workload_graph(structured).model_dump()


__all__ = [
    "DEPLOYMENT_LABEL_SYSTEM_PROMPT",
    "DeploymentModel",
    "ExternalDependency",
    "ResourceRequirements",
    "Workload",
    "WorkloadArtifact",
    "WorkloadConfiguration",
    "WorkloadConnection",
    "WorkloadConstraint",
    "WorkloadGraph",
    "WorkloadGraphProposal",
    "WorkloadInterface",
    "WorkloadStorage",
    "extract_deployment_model",
]
