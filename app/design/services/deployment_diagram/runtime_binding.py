"""구현에서 관측한 runtime 값을 구조 변경 없이 배포 계약에 결합한다."""

from __future__ import annotations

import copy
from typing import Any

from app.design.services.deployment_diagram.digest import (
    deployment_plan_structure_digest,
    workload_graph_structure_digest,
)
from app.design.services.deployment_diagram.placement import build_deployment_plan
from app.design.services.deployment_diagram.planning_constants import (
    RUNTIME_BINDING_SCHEMA,
)
from app.design.services.deployment_diagram.planning_primitives import issue as _issue


def bind_runtime_contract(
    graph: dict[str, Any],
    deployment_plan: dict[str, Any],
    runtime_contracts: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any]:
    """관측 runtime 값을 계획 구조를 바꾸지 않고 배포 계약에 결합한다.

    Args:
        graph: 승인 WorkloadGraph다.
        deployment_plan: 승인 DeploymentPlan이다.
        runtime_contracts: 구현 단계가 관측한 workload runtime 계약이다.

    Returns:
        bound 결과 또는 배포 설계 재생성 요구 문서다.

    Notes:
        결합 전후 structure digest가 달라지면 기존 계획을 반환하고 재생성을 요구한다.
    """

    contracts = (
        list(runtime_contracts)
        if isinstance(runtime_contracts, list)
        else list(runtime_contracts.get("workloads") or [runtime_contracts])
    )
    bound_graph = copy.deepcopy(graph)
    before_graph_digest = workload_graph_structure_digest(bound_graph)
    before_plan_digest = deployment_plan_structure_digest(deployment_plan)
    workloads = {str(item.get("id")): item for item in bound_graph.get("workloads") or []}
    structural_changes: list[dict[str, Any]] = []

    for contract in contracts:
        workload_id = str(contract.get("workloadId") or contract.get("workloadRef") or "")
        workload = workloads.get(workload_id)
        if workload is None:
            structural_changes.append(
                _issue(
                    f"runtimeContracts.{workload_id}",
                    "Implementation observed a workload absent from WorkloadGraph.",
                    classification="requiresRegeneration",
                )
            )
            continue
        artifact = workload.setdefault("artifact", {})
        if contract.get("imageDigest"):
            artifact["imageDigest"] = contract["imageDigest"]
        interfaces = {str(item.get("id")): item for item in workload.get("interfaces") or []}
        for observed in contract.get("interfaces") or []:
            interface_id = str(observed.get("interfaceId") or observed.get("id") or "")
            interface = interfaces.get(interface_id)
            if interface is None:
                structural_changes.append(
                    _issue(
                        f"runtimeContracts.{workload_id}.interfaces.{interface_id}",
                        "Implementation introduced a new interface; deployment design must be regenerated.",
                        classification="requiresRegeneration",
                    )
                )
                continue
            if observed.get("exposure") and observed.get("exposure") != interface.get("exposure"):
                structural_changes.append(
                    _issue(
                        f"runtimeContracts.{workload_id}.interfaces.{interface_id}.exposure",
                        "Implementation changed endpoint exposure; deployment design must be regenerated.",
                        classification="requiresRegeneration",
                    )
                )
                continue
            for field in ("port", "healthPath"):
                if observed.get(field) is not None:
                    interface[field] = observed[field]
        known_storage = {str(item.get("id")): item for item in workload.get("storage") or []}
        observed_storage_ids: set[str] = set()
        for mount in contract.get("mounts") or []:
            storage_id = str(mount.get("storageId") or "")
            observed_storage_ids.add(storage_id)
            storage = known_storage.get(storage_id)
            if storage is None:
                structural_changes.append(
                    _issue(
                        f"runtimeContracts.{workload_id}.mounts.{storage_id}",
                        "Implementation requires new storage; deployment design must be regenerated.",
                        classification="requiresRegeneration",
                    )
                )
            elif mount.get("mountPath"):
                if mount["mountPath"] != storage.get("mountPath"):
                    structural_changes.append(
                        _issue(
                            f"runtimeContracts.{workload_id}.mounts.{storage_id}.mountPath",
                            "Implementation mount path differs from the deployment design contract.",
                            classification="requiresRegeneration",
                        )
                    )
        known_configuration = {
            str(item.get("name")): item for item in workload.get("configuration") or []
        }
        observed_configuration_names: set[str] = set()
        for observed in contract.get("configuration") or []:
            name = str(observed.get("name") or "")
            observed_configuration_names.add(name)
            target = known_configuration.get(name)
            if target is None:
                structural_changes.append(
                    _issue(
                        f"runtimeContracts.{workload_id}.configuration.{name}",
                        "Implementation consumes undeclared configuration; deployment design must be regenerated.",
                        classification="requiresRegeneration",
                    )
                )
                continue
            if observed.get("secretRef"):
                target["secretRef"] = observed["secretRef"]
            elif "value" in observed and target.get("kind") not in {"secret", "secretBinding"}:
                target["value"] = observed["value"]
        if artifact.get("kind") == "generatedApplication":
            for storage_id in sorted(set(known_storage) - observed_storage_ids):
                structural_changes.append(
                    _issue(
                        f"runtimeContracts.{workload_id}.mounts.{storage_id}",
                        "Implementation does not expose a planned storage mount.",
                        classification="requiresRegeneration",
                    )
                )
            for name in sorted(set(known_configuration) - observed_configuration_names):
                structural_changes.append(
                    _issue(
                        f"runtimeContracts.{workload_id}.configuration.{name}",
                        "Implementation does not consume a planned environment binding.",
                        classification="requiresRegeneration",
                    )
                )

    if structural_changes:
        return {
            "schemaVersion": RUNTIME_BINDING_SCHEMA,
            "status": "requiresDeploymentDesignRegeneration",
            "issues": structural_changes,
            "workloadGraph": graph,
            "deploymentPlan": deployment_plan,
            "structureDigest": before_plan_digest,
        }

    bound_plan = build_deployment_plan(
        bound_graph,
        {
            "region": (deployment_plan.get("locationPlan") or {}).get("region"),
            "candidateZones": (deployment_plan.get("locationPlan") or {}).get("candidateZones")
            or [],
            "zoneSelectionSource": (
                deployment_plan.get("locationPlan") or {}
            ).get("zonePolicy"),
        },
    )
    # Runtime observation may fill ports and image digests but must not discard
    # the user's late sizing inputs. The topology digest excludes these values;
    # replicaCount itself still carries the structural scale decision.
    prior_compute = {
        str(item.get("id") or ""): item
        for item in deployment_plan.get("computeUnits") or []
        if isinstance(item, dict)
    }
    for compute in bound_plan.get("computeUnits") or []:
        previous = prior_compute.get(str(compute.get("id") or ""), {})
        for field in (
            "resourceRequirements",
            "vmSku",
            "selectedVmSku",
            "selectedReplicaCount",
        ):
            if field in previous:
                compute[field] = copy.deepcopy(previous[field])
    after_graph_digest = workload_graph_structure_digest(bound_graph)
    after_plan_digest = deployment_plan_structure_digest(bound_plan)
    if before_graph_digest != after_graph_digest or before_plan_digest != after_plan_digest:
        return {
            "schemaVersion": RUNTIME_BINDING_SCHEMA,
            "status": "requiresDeploymentDesignRegeneration",
            "issues": [
                _issue(
                    "runtimeContract",
                    "Runtime observations changed the deployment structure digest.",
                    classification="requiresRegeneration",
                )
            ],
            "workloadGraph": graph,
            "deploymentPlan": deployment_plan,
            "structureDigest": before_plan_digest,
        }
    return {
        "schemaVersion": RUNTIME_BINDING_SCHEMA,
        "status": "bound",
        "workloadGraph": bound_graph,
        "deploymentPlan": bound_plan,
        "structureDigest": before_plan_digest,
        "issues": [],
    }
