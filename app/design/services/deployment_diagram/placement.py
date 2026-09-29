"""정규화된 WorkloadGraph를 provider-neutral DeploymentPlan으로 배치한다."""

from __future__ import annotations

import copy
from typing import Any

from app.design.services.deployment_diagram.digest import (
    deployment_plan_structure_digest,
    workload_graph_structure_digest,
)
from app.design.services.deployment_diagram.normalization import _constraint_value
from app.design.services.deployment_diagram.planning_constants import (
    DEPLOYMENT_PLAN_SCHEMA,
)
from app.design.services.deployment_diagram.planning_primitives import (
    issue as _issue,
)
from app.design.services.deployment_diagram.planning_primitives import (
    refs as _refs,
)
from app.design.services.deployment_diagram.planning_primitives import (
    slug as _slug,
)


def _constraints_by_workload(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    policies: dict[str, dict[str, Any]] = {
        str(item.get("id")): {
            "replicaCount": 1,
            "replicationConfirmed": False,
            "zones": [],
            "minimumZones": 1,
            "managedReplacement": False,
            "isolation": False,
            "colocate": set(),
            "sourceRefs": [],
        }
        for item in graph.get("workloads") or []
    }
    for constraint in graph.get("constraints") or []:
        kind = str(constraint.get("kind") or "")
        workload_refs = [str(item) for item in constraint.get("workloadRefs") or []]
        refs = _refs(constraint.get("sourceRefs"))
        for workload_ref in workload_refs:
            policy = policies.get(workload_ref)
            if policy is None:
                continue
            policy["sourceRefs"] = _refs([*policy["sourceRefs"], *refs])
            if kind == "replicaCount":
                value = _constraint_value(constraint, 1)
                if isinstance(value, int) and not isinstance(value, bool):
                    policy["replicaCount"] = value
            elif kind == "replicationConfirmation":
                policy["replicationConfirmed"] = _constraint_value(constraint, False) is True
            elif kind in {"zoneSpread", "zonePlacement"}:
                value = _constraint_value(constraint, [])
                if isinstance(value, list):
                    policy["zones"] = list(value)
                    policy["minimumZones"] = max(1, len(value))
                elif isinstance(value, dict):
                    policy["zones"] = list(value.get("zones") or [])
                    minimum = value.get("minimumZones") or value.get("count") or 1
                    if isinstance(minimum, int) and not isinstance(minimum, bool):
                        policy["minimumZones"] = minimum
                elif isinstance(value, int) and not isinstance(value, bool):
                    policy["minimumZones"] = value
            elif kind == "managedReplacement":
                policy["managedReplacement"] = bool(_constraint_value(constraint, True))
            elif kind in {"separate", "isolate", "securityIsolation", "resourceIsolation"}:
                policy["isolation"] = True
            elif kind == "colocate":
                policy["colocate"].update(workload_refs)
    return policies


def build_deployment_plan(
    graph: dict[str, Any], planning_context_value: dict[str, Any] | None = None
) -> dict[str, Any]:
    """고정 정책으로 WorkloadGraph를 provider-neutral compute에 배치한다.

    Args:
        graph: 정규화와 검증을 마친 WorkloadGraph다.
        planning_context_value: provider-neutral 지역·zone·용량 문맥이다.

    Returns:
        기존 ID·sourceRef·issue·derivation 순서의 DeploymentPlan이다.

    Notes:
        provider resource를 선택하거나 LLM을 호출하지 않는다.
    """

    context = copy.deepcopy(planning_context_value or {})
    issues = [copy.deepcopy(item) for item in graph.get("issues") or []]
    policies = _constraints_by_workload(graph)
    workloads = list(graph.get("workloads") or [])
    by_id = {str(item.get("id")): item for item in workloads}

    candidate_zones = _refs(context.get("candidateZones"))
    for workload_id, policy in policies.items():
        minimum_zones = int(policy.get("minimumZones") or 1)
        if minimum_zones > 1 and not policy["zones"]:
            if len(candidate_zones) >= minimum_zones:
                policy["zones"] = candidate_zones[:minimum_zones]
            else:
                issues.append(
                    _issue(
                        f"constraints.zoneSpread.{workload_id}",
                        f"At least {minimum_zones} candidate zones are required.",
                        source_refs=policy["sourceRefs"],
                    )
                )
        if policy["zones"] and len(set(policy["zones"])) < minimum_zones:
            issues.append(
                _issue(
                    f"constraints.zoneSpread.{workload_id}",
                    f"Zone selection does not satisfy minimumZones={minimum_zones}.",
                    classification="invalid",
                    source_refs=policy["sourceRefs"],
                )
            )
        if minimum_zones > int(policy["replicaCount"] or 1):
            issues.append(
                _issue(
                    f"constraints.zoneSpread.{workload_id}",
                    "Occupied zone count cannot exceed fixed replica count.",
                    classification="invalid",
                    source_refs=policy["sourceRefs"],
                )
            )

    for workload_id, policy in policies.items():
        count = int(policy["replicaCount"] or 1)
        if count < 1:
            issues.append(
                _issue(
                    f"constraints.replicaCount.{workload_id}",
                    "replicaCount must be at least one.",
                    classification="invalid",
                    source_refs=policy["sourceRefs"],
                )
            )
            policy["replicaCount"] = 1
            count = 1
        safety = str(by_id[workload_id].get("replicationSafety") or "unknown")
        if count > 1 and safety != "interchangeable" and not policy["replicationConfirmed"]:
            issues.append(
                _issue(
                    f"workloads.{workload_id}.replicationSafety",
                    "Multiple replicas require explicit interchangeable replication safety or a confirmed user decision.",
                    source_refs=policy["sourceRefs"] or _refs(by_id[workload_id].get("sourceRefs")),
                )
            )
        if count > 1:
            for storage in by_id[workload_id].get("storage") or []:
                if storage.get("replicaSemantics") != "perReplica":
                    issues.append(
                        _issue(
                            f"workloads.{workload_id}.storage.{storage.get('id')}.replicaSemantics",
                            "Persistent block storage with multiple replicas requires explicit perReplica semantics; shared filesystems are out of scope.",
                            classification="unsupported",
                            source_refs=_refs(storage.get("sourceRefs")) or policy["sourceRefs"],
                        )
                    )

    for constraint in graph.get("constraints") or []:
        if str(constraint.get("kind") or "") != "colocate":
            continue
        refs = [str(item) for item in constraint.get("workloadRefs") or []]
        signatures = {
            (
                policies[item]["replicaCount"],
                tuple(sorted(policies[item]["zones"])),
                policies[item]["managedReplacement"],
                policies[item]["minimumZones"],
            )
            for item in refs
            if item in policies
        }
        if len(signatures) > 1:
            issues.append(
                _issue(
                    f"constraints.{constraint.get('id')}",
                    "Colocated workloads must have identical replica, zone, and managed-lifecycle policies.",
                    classification="invalid",
                    source_refs=_refs(constraint.get("sourceRefs")),
                )
            )

    # Compatible workloads share a compute unit.  A policy signature is the
    # structural lifecycle boundary; explicit isolation gets its own signature.
    groups: dict[tuple[Any, ...], list[str]] = {}
    for workload in workloads:
        workload_id = str(workload.get("id"))
        policy = policies[workload_id]
        zones = tuple(sorted(str(item) for item in policy["zones"] if str(item)))
        signature = (
            int(policy["replicaCount"]),
            zones,
            bool(policy["managedReplacement"]),
            int(policy["minimumZones"]),
            workload_id if policy["isolation"] else "shared",
        )
        groups.setdefault(signature, []).append(workload_id)

    # A separate constraint naming several workloads means pairwise separation,
    # even when their other lifecycle policies match.
    for constraint in graph.get("constraints") or []:
        if str(constraint.get("kind") or "") not in {
            "separate",
            "isolate",
            "securityIsolation",
            "resourceIsolation",
        }:
            continue
        refs = [str(item) for item in constraint.get("workloadRefs") or []]
        for workload_ref in refs:
            for signature, members in list(groups.items()):
                if workload_ref in members and len(members) > 1:
                    members.remove(workload_ref)
                    groups[(*signature, workload_ref)] = [workload_ref]

    compute_units: list[dict[str, Any]] = []
    placements: list[dict[str, Any]] = []
    for index, (signature, workload_ids) in enumerate(groups.items(), start=1):
        replica_count = int(signature[0])
        placement_zones = list(signature[1])
        if not placement_zones and candidate_zones:
            placement_zones = candidate_zones[:1]
        managed = replica_count > 1 or bool(signature[2])
        compute_id = f"compute-{index}"
        cpu_values = [
            (by_id[item].get("resourceRequirements") or {}).get("minVCpu") for item in workload_ids
        ]
        memory_values = [
            (by_id[item].get("resourceRequirements") or {}).get("minMemoryGiB")
            for item in workload_ids
        ]
        cpu_known = all(
            isinstance(item, (int, float)) and not isinstance(item, bool) for item in cpu_values
        )
        memory_known = all(
            isinstance(item, (int, float)) and not isinstance(item, bool) for item in memory_values
        )
        requirements: dict[str, Any] = {}
        if cpu_known:
            requirements["minVCpu"] = sum(cpu_values)
        if memory_known:
            requirements["minMemoryGiB"] = sum(memory_values)
        compute_units.append(
            {
                "id": compute_id,
                "kind": "managedVmGroup" if managed else "standaloneVm",
                "replicaCount": replica_count,
                "zones": placement_zones,
                "managedReplacement": managed,
                "resourceRequirements": requirements,
                "vmSku": {"binding": "late", "field": "vmSku"},
                "sourceRefs": _refs(
                    item
                    for workload_id in workload_ids
                    for item in (
                        policies[workload_id]["sourceRefs"]
                        or ["project-policy:default-colocation-and-single-replica"]
                    )
                ),
            }
        )
        for workload_id in workload_ids:
            placements.append(
                {
                    "id": f"placement-{_slug(workload_id)}",
                    "workloadRef": workload_id,
                    "computeUnitRef": compute_id,
                    "sourceRefs": policies[workload_id]["sourceRefs"]
                    or ["project-policy:default-colocation-and-single-replica"],
                }
            )

    placement_by_workload = {item["workloadRef"]: item["computeUnitRef"] for item in placements}
    compute_by_id = {item["id"]: item for item in compute_units}
    storage_bindings: list[dict[str, Any]] = []
    for workload in workloads:
        workload_id = str(workload.get("id"))
        for storage in workload.get("storage") or []:
            storage_bindings.append(
                {
                    "id": f"storage-binding-{_slug(storage.get('id'))}",
                    "workloadRef": workload_id,
                    "storageRef": storage.get("id"),
                    "computeUnitRef": placement_by_workload.get(workload_id),
                    "kind": "blockDisk",
                    "capacityGiB": storage.get("capacityGiB"),
                    "mountPath": storage.get("mountPath")
                    or {"binding": "late", "field": "mountPath"},
                    "deletionPolicy": storage.get("deletionPolicy"),
                    "replicaSemantics": storage.get("replicaSemantics", "singleAttachment"),
                    "sourceRefs": _refs(storage.get("sourceRefs"))
                    or _refs(workload.get("sourceRefs")),
                }
            )

    network_paths: list[dict[str, Any]] = []
    public_compute: set[str] = set()
    for workload in workloads:
        workload_id = str(workload.get("id"))
        compute_ref = placement_by_workload.get(workload_id)
        for interface in workload.get("interfaces") or []:
            exposure = str(interface.get("exposure") or "unknown")
            if exposure != "public":
                continue
            public_compute.add(str(compute_ref))
            compute = compute_by_id.get(str(compute_ref), {})
            ingress_kind = (
                "loadBalancer" if compute.get("kind") == "managedVmGroup" else "directPublicIp"
            )
            network_paths.append(
                {
                    "id": f"public-{_slug(workload_id)}-{_slug(interface.get('id'))}",
                    "kind": "publicIngress",
                    "ingressKind": ingress_kind,
                    "targetWorkloadRef": workload_id,
                    "targetInterfaceRef": interface.get("id"),
                    "computeUnitRef": compute_ref,
                    "protocol": str(interface.get("protocol") or "").lower(),
                    "port": interface.get("port") or {"binding": "late", "field": "containerPort"},
                    "sourceRefs": _refs(interface.get("sourceRefs"))
                    or _refs(workload.get("sourceRefs")),
                }
            )

    for connection in graph.get("connections") or []:
        source = str(connection.get("sourceRef") or "")
        target = str(connection.get("targetRef") or "")
        kind = "internal" if source in by_id and target in by_id else "outbound"
        target_interface: dict[str, Any] = next(
            (
                item
                for item in (by_id.get(target) or {}).get("interfaces") or []
                if str(item.get("id") or "") == str(connection.get("targetInterfaceRef") or "")
            ),
            {},
        )
        network_paths.append(
            {
                "id": f"network-{_slug(connection.get('id'))}",
                "kind": kind,
                "connectionRef": connection.get("id"),
                "sourceRef": source,
                "targetRef": target,
                "protocol": str(connection.get("protocol") or "").lower(),
                "port": target_interface.get("port")
                or {"binding": "late", "field": "containerPort"},
                "sourceRefs": _refs(connection.get("sourceRefs")),
            }
        )

    for compute in compute_units:
        if compute["id"] not in public_compute or compute["kind"] == "managedVmGroup":
            network_paths.append(
                {
                    "id": f"egress-{compute['id']}",
                    "kind": "natEgress",
                    "computeUnitRef": compute["id"],
                    "purposes": ["registryPull", "externalHttp"],
                    "sourceRefs": ["project-policy:private-compute-nat-egress"],
                }
            )

    runtime_bindings: list[dict[str, Any]] = []
    connections_by_id = {str(item.get("id") or ""): item for item in graph.get("connections") or []}
    external_ids = {str(item.get("id") or "") for item in graph.get("externalDependencies") or []}
    for workload in workloads:
        workload_id = str(workload.get("id") or "")
        for storage in workload.get("storage") or []:
            runtime_bindings.append(
                {
                    "id": f"runtime-mount-{_slug(workload_id)}-{_slug(storage.get('id'))}",
                    "kind": "storageMount",
                    "workloadRef": workload_id,
                    "storageRef": storage.get("id"),
                    "mountPath": storage.get("mountPath"),
                    "sourceRefs": _refs(storage.get("sourceRefs"))
                    or _refs(workload.get("sourceRefs")),
                }
            )
        for configuration in workload.get("configuration") or []:
            configuration_id = str(configuration.get("id") or "")
            kind = str(configuration.get("kind") or "value")
            if kind in {"secret", "secretBinding"}:
                runtime_bindings.append(
                    {
                        "id": f"runtime-secret-{_slug(workload_id)}-{_slug(configuration_id)}",
                        "kind": "secretEnvironment",
                        "workloadRef": workload_id,
                        "configurationRef": configuration_id,
                        "environmentName": configuration.get("name"),
                        "secretReference": {
                            "binding": "deployment",
                            "field": f"workloads.{workload_id}.configuration.{configuration_id}.secretRef",
                        },
                        "sourceRefs": _refs(configuration.get("sourceRefs"))
                        or _refs(workload.get("sourceRefs")),
                    }
                )
            elif kind == "endpointBinding":
                connection_ref = str(configuration.get("connectionRef") or "")
                connection = connections_by_id.get(connection_ref) or {}
                target_ref = str(connection.get("targetRef") or "")
                source_compute = placement_by_workload.get(workload_id)
                target_compute = placement_by_workload.get(target_ref)
                if target_ref in external_ids:
                    strategy = "externalInput"
                elif source_compute and source_compute == target_compute:
                    strategy = "containerDns"
                elif (compute_by_id.get(str(target_compute)) or {}).get("kind") == "managedVmGroup":
                    strategy = "internalLoadBalancer"
                else:
                    strategy = "staticPrivateIp"
                target_interface = next(
                    (
                        item
                        for item in (by_id.get(target_ref) or {}).get("interfaces") or []
                        if str(item.get("id") or "")
                        == str(connection.get("targetInterfaceRef") or "")
                    ),
                    {},
                )
                runtime_bindings.append(
                    {
                        "id": f"runtime-endpoint-{_slug(workload_id)}-{_slug(configuration_id)}",
                        "kind": "endpointEnvironment",
                        "workloadRef": workload_id,
                        "configurationRef": configuration_id,
                        "environmentName": configuration.get("name"),
                        "connectionRef": connection_ref,
                        "targetWorkloadRef": target_ref,
                        "targetComputeUnitRef": target_compute,
                        "targetInterfaceRef": connection.get("targetInterfaceRef"),
                        "strategy": strategy,
                        "projection": configuration.get("projection"),
                        "protocol": str(connection.get("protocol") or "").lower(),
                        "port": target_interface.get("port")
                        or {"binding": "late", "field": "containerPort"},
                        "sourceRefs": _refs(configuration.get("sourceRefs"))
                        or _refs(connection.get("sourceRefs")),
                    }
                )

    selected_zones = _refs(zone for compute in compute_units for zone in compute.get("zones") or [])
    plan = {
        "schemaVersion": DEPLOYMENT_PLAN_SCHEMA,
        "workloadGraphDigest": graph.get("structureDigest")
        or workload_graph_structure_digest(graph),
        "computeUnits": compute_units,
        "placements": placements,
        "storageBindings": storage_bindings,
        "networkPaths": network_paths,
        "runtimeBindings": runtime_bindings,
        "locationPlan": {
            "region": context.get("region"),
            "zonePolicy": (
                "catalogBased"
                if selected_zones
                and context.get("zoneSelectionSource") == "catalogBased"
                else "explicit"
                if selected_zones
                else "providerSelectedSingleZone"
            ),
            "selectedZones": selected_zones,
            "candidateZones": _refs(context.get("candidateZones")),
            "singleRegion": True,
        },
        "issues": issues,
    }
    plan["structureDigest"] = deployment_plan_structure_digest(plan)
    return plan


def validate_deployment_plan(plan: dict[str, Any]) -> None:
    """DeploymentPlan의 식별자와 reference 무결성을 검사한다.

    Args:
        plan: 검증할 provider-neutral DeploymentPlan이다.

    Returns:
        검증 성공 시 ``None``이다.

    Notes:
        계약 위반은 기존 ``ValueError`` 유형과 메시지로 보고한다.
    """

    if plan.get("schemaVersion") != DEPLOYMENT_PLAN_SCHEMA:
        raise ValueError("unsupported DeploymentPlan schemaVersion")
    compute_ids = [str(item.get("id") or "") for item in plan.get("computeUnits") or []]
    if any(not item for item in compute_ids) or len(compute_ids) != len(set(compute_ids)):
        raise ValueError("DeploymentPlan compute unit ids must be non-empty and unique")
    known_compute = set(compute_ids)
    workload_refs: set[str] = set()
    for placement in plan.get("placements") or []:
        if str(placement.get("computeUnitRef") or "") not in known_compute:
            raise ValueError("DeploymentPlan placement has a dangling compute reference")
        workload_ref = str(placement.get("workloadRef") or "")
        if not workload_ref or workload_ref in workload_refs:
            raise ValueError("Each workload must have exactly one placement")
        workload_refs.add(workload_ref)
    for binding in plan.get("storageBindings") or []:
        if str(binding.get("computeUnitRef") or "") not in known_compute:
            raise ValueError("DeploymentPlan storage binding has a dangling compute reference")
        if str(binding.get("workloadRef") or "") not in workload_refs:
            raise ValueError("DeploymentPlan storage binding has a dangling workload reference")
    binding_ids: set[str] = set()
    for binding in plan.get("runtimeBindings") or []:
        binding_id = str(binding.get("id") or "")
        if not binding_id or binding_id in binding_ids:
            raise ValueError("DeploymentPlan runtime binding ids must be non-empty and unique")
        binding_ids.add(binding_id)
        if str(binding.get("workloadRef") or "") not in workload_refs:
            raise ValueError("DeploymentPlan runtime binding has a dangling workload reference")
