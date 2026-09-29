"""WorkloadGraph 이후의 결정론적 배포 계획 public facade다.

planning fact, graph 정규화, placement, runtime binding, digest 구현은 각 bounded
module이 소유한다. 이 module은 기존 import 계약을 재노출하고 provider template projection만
조율한다. LLM이나 graph state·repository에는 의존하지 않는다.
"""

from __future__ import annotations

import copy
from typing import Any

from app.design.services.deployment_diagram.digest import (
    deployment_plan_structure_digest,
    resource_plan_structure_digest,
    workload_graph_structure_digest,
)
from app.design.services.deployment_diagram.normalization import (
    normalize_workload_graph,
    validate_workload_graph,
)
from app.design.services.deployment_diagram.placement import (
    build_deployment_plan,
    validate_deployment_plan,
)
from app.design.services.deployment_diagram.planning_constants import (
    BLOCKING_CLASSES,
    DEPLOYMENT_PLAN_SCHEMA,
    ENVIRONMENT_NAME,
    RESOURCE_PLAN_SCHEMA,
    RUNTIME_BINDING_SCHEMA,
    SUPPORTED_PREBUILT_RUNTIME_CATALOG,
    SUPPORTED_PROTOCOLS,
    SUPPORTED_PROVIDERS,
    WORKLOAD_GRAPH_SCHEMA,
)
from app.design.services.deployment_diagram.planning_facts import (
    extract_planning_facts,
    planning_context,
    planning_inputs_stale,
)
from app.design.services.deployment_diagram.planning_primitives import (
    issue as _issue,
)
from app.design.services.deployment_diagram.provider_template import (
    build_complete_provider_template,
    provider_template_structure_digest,
    validate_complete_provider_template,
)
from app.design.services.deployment_diagram.runtime_binding import (
    bind_runtime_contract,
)


def build_provider_resource_plan(
    deployment_plan: dict[str, Any],
    graph: dict[str, Any],
    *,
    provider: str,
    region: str,
) -> dict[str, Any]:
    """provider-neutral 결정을 하나의 완전한 CSP ResourcePlan으로 투영한다.

    Args:
        deployment_plan: 검증된 provider-neutral DeploymentPlan이다.
        graph: 해당 DeploymentPlan의 WorkloadGraph다.
        provider: 명시적으로 선택된 CSP 식별자다.
        region: 명시적으로 선택된 CSP region이다.

    Returns:
        완전한 provider ResourcePlan 또는 기존 unresolved plan이다.

    Notes:
        provider나 region을 추론하지 않고 provider template 구현에 결정론적으로 위임한다.
    """

    validate_deployment_plan(deployment_plan)
    normalized_provider = str(provider or "").lower()
    if normalized_provider not in SUPPORTED_PROVIDERS or not region:
        issues = copy.deepcopy(deployment_plan.get("issues") or [])
        if normalized_provider not in SUPPORTED_PROVIDERS:
            issues.append(
                _issue(
                    "provider",
                    "Provider must be selected as aws, azure, or gcp.",
                    classification="needsInput",
                    source_refs=["project-policy:explicit-deployment-target"],
                )
            )
        if not region:
            issues.append(
                _issue(
                    "region",
                    "A provider region must be selected.",
                    classification="needsInput",
                    source_refs=["project-policy:explicit-deployment-target"],
                )
            )
        unresolved = {
            "schemaVersion": RESOURCE_PLAN_SCHEMA,
            "provider": normalized_provider,
            "region": str(region or ""),
            "deploymentPlanDigest": deployment_plan.get("structureDigest")
            or deployment_plan_structure_digest(deployment_plan),
            "nodes": [],
            "edges": [],
            "workloads": copy.deepcopy(graph.get("workloads") or []),
            "placements": copy.deepcopy(deployment_plan.get("placements") or []),
            "storageBindings": copy.deepcopy(deployment_plan.get("storageBindings") or []),
            "networkPaths": copy.deepcopy(deployment_plan.get("networkPaths") or []),
            "runtimeBindings": copy.deepcopy(deployment_plan.get("runtimeBindings") or []),
            "locationPlan": copy.deepcopy(deployment_plan.get("locationPlan") or {}),
            "runtimeUnits": [],
            "bindingSlots": [],
            "issues": issues,
            "unresolved": [
                item for item in issues if item.get("classification") in BLOCKING_CLASSES
            ],
        }
        unresolved["structureDigest"] = provider_template_structure_digest(unresolved)
        return unresolved
    return build_complete_provider_template(
        deployment_plan,
        graph,
        provider=normalized_provider,
        region=region,
    )


def validate_provider_resource_plan(plan: dict[str, Any]) -> None:
    """provider ResourcePlan의 완전한 template 계약을 검사한다.

    Args:
        plan: 검증할 provider ResourcePlan이다.

    Returns:
        검증 성공 시 ``None``이다.

    Notes:
        다음 provider projection 분리 전까지 기존 validator에 그대로 위임한다.
    """

    validate_complete_provider_template(plan)


__all__ = [
    "BLOCKING_CLASSES",
    "DEPLOYMENT_PLAN_SCHEMA",
    "ENVIRONMENT_NAME",
    "RESOURCE_PLAN_SCHEMA",
    "RUNTIME_BINDING_SCHEMA",
    "SUPPORTED_PREBUILT_RUNTIME_CATALOG",
    "SUPPORTED_PROTOCOLS",
    "SUPPORTED_PROVIDERS",
    "WORKLOAD_GRAPH_SCHEMA",
    "bind_runtime_contract",
    "build_deployment_plan",
    "build_provider_resource_plan",
    "deployment_plan_structure_digest",
    "extract_planning_facts",
    "normalize_workload_graph",
    "planning_context",
    "planning_inputs_stale",
    "resource_plan_structure_digest",
    "validate_deployment_plan",
    "validate_provider_resource_plan",
    "validate_workload_graph",
    "workload_graph_structure_digest",
]
