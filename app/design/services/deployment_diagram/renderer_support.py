"""두 PlantUML renderer가 공유하는 순수 표시 context와 helper를 제공한다."""

from __future__ import annotations

import copy
import re
from typing import Any

_DISPLAYABLE_PROVISIONING_RELATIONSHIPS = {
    "attaches",
    "belongs to",
    "binds",
    "checks with",
    "contains instance",
    "contains role",
    "configures",
    "selects subnetwork",
    "creates instances from",
    "depends on",
    "evaluates targets with",
    "exposes",
    "forwards to",
    "grants pull access to",
    "grants secret read to",
    "is attached to",
    "is deployed in",
    "is placed in",
    "joins",
    "joins through",
    "matches",
    "places instances in",
    "provides egress for",
    "pulls image digest from",
    "registers instance",
    "registers instances with",
    "registers with",
    "routes to",
    "scopes pull access to",
    "scopes secret read to",
    "serves region of",
    "uses",
    "uses address",
    "uses backend",
    "uses identity",
    "uses image",
    "uses policy",
    "uses secret identity",
}

# These entries remain first-class ResourcePlan nodes because Terraform must
# create or configure them explicitly.  In the reader-facing diagram they are
# projected as labelled bindings between the cloud resources they join.
# This keeps IaC validation exact without presenting provider implementation
# objects such as aws_volume_attachment as if they were CSP resources.
_FOLDED_RELATION_KINDS = {
    "aws": {
        "application-default-route",
        "application-ingress-rule",
        "application-route-association",
        "disk-attachment",
        "ingress-default-route",
        "ingress-route-association",
        "registry-pull-binding",
        "secret-access-binding",
        "state-secret-access-binding",
    },
    "azure": {
        "backend-membership",
        "disk-attachment",
        "nat-association",
        "nat-public-ip-association",
        "registry-pull-binding",
        "secret-access-binding",
        "security-group-association",
        "state-secret-access-binding",
    },
    "gcp": {
        "disk-attachment",
        "registry-pull-binding",
        "secret-access-binding",
        "state-secret-access-binding",
    },
}

_FOLDED_ASSOCIATION_LABELS = {
    "application-default-route": "default route to NAT Gateway",
    "application-ingress-rule": "allows application port",
    "application-route-association": "uses route table",
    "backend-membership": "registered in backend pool",
    "disk-attachment": "attached",
    "ingress-default-route": "default route to Internet Gateway",
    "ingress-route-association": "uses route table",
    "nat-association": "uses NAT Gateway",
    "nat-public-ip-association": "uses public IP",
    "registry-pull-binding": "grants Registry pull",
    "secret-access-binding": "grants Secret read",
    "security-group-association": "applies network security group",
    "state-secret-access-binding": "grants State Secret read",
}


def _id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", value) or "unknown"


def _text(value: Any) -> str:
    return str(value or "").replace('"', "'").replace("\n", " ")


def _display_image_reference(value: Any) -> str:
    reference = str(value or "prebuilt image")
    return re.sub(r"@sha256:[0-9a-fA-F]{32,}$", "", reference)


def _binding_text(value: Any) -> str:
    if isinstance(value, dict) and value.get("binding") == "late":
        return f"late:{value.get('field') or 'value'}"
    return _text(value)


def _compact_reference_role(path: Any) -> str:
    """Return a short, reader-facing discriminator for duplicate references."""

    leaf = str(path or "reference").split(".")[-1].replace("[]", "")
    leaf = re.sub(r"_(?:ids?|arns?|names?|values?)$", "", leaf)
    aliases = {
        "ami": "image",
        "image": "image",
        "vpc": "network",
        "virtual_network": "network",
        "network": "network",
        "vpc_zone_identifier": "subnets",
        "subnet": "subnet",
        "security_group": "traffic filter",
        "network_security_group": "traffic filter",
        "target_group": "backend",
        "backend_pool": "backend",
        "backend_service": "backend",
        "iam_instance_profile": "identity",
        "service_account": "identity",
        "role": "identity",
        "volume": "storage",
        "disk": "storage",
    }
    return aliases.get(leaf, leaf.replace("_", " "))


def _primary(bundle: dict[str, Any]) -> dict[str, Any] | None:
    """현재 화면과 IaC가 사용할 projection 하나를 고른다.

    후보가 하나면 그대로 사용한다. 후보가 여러 개면 배열의 첫 항목을 암묵적으로
    선택하지 않고 bundle에 저장된 ``selectedTarget``과 정확히 일치하는 항목만 사용한다.
    """

    projections = [
        item for item in bundle.get("projections") or [] if isinstance(item, dict)
    ]
    selected = bundle.get("selectedTarget")
    if isinstance(selected, dict):
        selected_id = str(selected.get("id") or "")
        matches = [
            projection
            for projection in projections
            if isinstance(projection.get("target"), dict)
            and (
                str(projection["target"].get("id") or "") == selected_id
                if selected_id
                else str(projection["target"].get("provider") or "").lower()
                == str(selected.get("provider") or "").lower()
                and str(projection["target"].get("region") or "")
                == str(selected.get("region") or "")
            )
        ]
        if len(matches) == 1:
            return matches[0]
    return projections[0] if len(projections) == 1 else None


def _fallback(bundle: dict[str, Any], message: str = "") -> str:
    del bundle
    puml = '@startuml\n!theme plain\nnode "Deployment target unresolved"\n@enduml'
    if message:
        puml = puml.replace("@enduml", f"note bottom\n  {_text(message)}\nend note\n@enduml")
    return puml


def _provider_label(provider: str) -> str:
    return {"aws": "AWS", "azure": "Microsoft Azure", "gcp": "Google Cloud"}.get(
        provider, provider.upper()
    )


def _node_by_kind(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(node.get("providerKind") or node.get("id") or ""): node
        for node in plan.get("nodes") or []
        if isinstance(node, dict) and node.get("providerKind")
    }


def _workload_alias(workload: dict[str, Any]) -> str:
    return "runtime_" + _id(str(workload.get("id") or "application"))


def _replica_alias(workload: dict[str, Any], replica: int) -> str:
    return f"{_workload_alias(workload)}_replica_{replica}"


def _instance_label(provider: str) -> str:
    return {
        "aws": "EC2 Instance",
        "azure": "Virtual Machine Scale Set instance",
        "gcp": "Compute Engine VM",
    }.get(provider, "VM instance")


def _persistent_workload(workload: dict[str, Any]) -> bool:
    """workload가 직접 소유한 영구 저장소가 있는지 반환한다."""

    return any(
        storage.get("persistence") == "persistent" for storage in workload.get("storage") or []
    )


def _runtime_contract_lines(plan: dict[str, Any], workload: dict[str, Any]) -> list[str]:
    """Keep long app/resource contracts inside the workload, not on arrows."""

    workload_id = str(workload.get("id") or "")
    contract_lines: list[str] = []
    bound_configuration: set[str] = set()
    for binding in plan.get("runtimeBindings") or []:
        if str(binding.get("workloadRef") or "") != workload_id:
            continue
        kind = str(binding.get("kind") or "")
        if kind == "endpointEnvironment":
            name = str(binding.get("environmentName") or "")
            if name:
                contract_lines.append(f"[env] {name}")
            bound_configuration.add(str(binding.get("configurationRef") or ""))
        elif kind == "secretEnvironment":
            name = str(binding.get("environmentName") or "")
            if name:
                contract_lines.append(f"[secret] {name}")
            bound_configuration.add(str(binding.get("configurationRef") or ""))
        elif kind == "storageMount":
            mount_path = binding.get("mountPath")
            if isinstance(mount_path, str) and mount_path:
                contract_lines.append(f"[mount] {mount_path}")
    for configuration in workload.get("configuration") or []:
        configuration_id = str(configuration.get("id") or "")
        name = str(configuration.get("name") or "")
        if name and configuration_id not in bound_configuration:
            contract_lines.append(f"[env] {name}")
    for interface in workload.get("interfaces") or []:
        port = interface.get("port")
        if isinstance(port, int):
            protocol = str(interface.get("protocol") or interface.get("id") or "port")
            contract_lines.append(f"[listen] {protocol} :{port}")
        health_path = interface.get("healthPath")
        if isinstance(health_path, str) and health_path:
            contract_lines.append(f"[health] {health_path}")
    artifact_kind = str((workload.get("artifact") or {}).get("kind") or "")
    if artifact_kind == "generatedApplication":
        contract_lines.append("[image] digest binding")
    elif artifact_kind == "prebuiltImage":
        contract_lines.append("[image] pinned digest")
    return list(dict.fromkeys(contract_lines))


def _runtime_workload_label(
    plan: dict[str, Any],
    workload: dict[str, Any],
    *,
    fallback: str,
    replica: int | None = None,
) -> str:
    parts = [_text(workload.get("name") or fallback)]
    if replica is not None:
        parts.append(f"replica {replica}")
    parts.append("<<Docker container>>")
    contracts = _runtime_contract_lines(plan, workload)
    if contracts:
        parts.extend(["[runtime contract]", *contracts])
    return "\\n".join(parts)


def _render_context(bundle: dict[str, Any]) -> dict[str, Any]:
    """Build renderer-only values from WorkloadGraph, DeploymentPlan, and ResourcePlan."""

    projections = list(bundle.get("projections") or [])
    projection = projections[0] if projections else {}
    deployment_plan = dict(projection.get("deploymentPlan") or {})
    resource_plan = copy.deepcopy(projection.get("resourcePlan") or {})
    graph = dict(bundle.get("workloadGraph") or {})
    compute_units = {
        str(item.get("id") or ""): item for item in deployment_plan.get("computeUnits") or []
    }
    placements = list(deployment_plan.get("placements") or [])
    public_paths = [
        item
        for item in deployment_plan.get("networkPaths") or []
        if item.get("kind") == "publicIngress"
    ]
    primary_compute_id = str(
        (public_paths[0].get("computeUnitRef") if public_paths else "")
        or (placements[0].get("computeUnitRef") if placements else "")
        or next(iter(compute_units), "")
    )
    primary_compute = compute_units.get(primary_compute_id, {})
    zones = list(primary_compute.get("zones") or [])
    replica_count = int(primary_compute.get("replicaCount") or 1)
    managed = primary_compute.get("kind") == "managedVmGroup"
    workload_by_id = {
        str(item.get("id") or ""): copy.deepcopy(item) for item in graph.get("workloads") or []
    }
    placement_by_workload = {
        str(item.get("workloadRef") or ""): str(item.get("computeUnitRef") or "")
        for item in placements
    }
    workload_names_by_compute: dict[str, list[str]] = {}
    for workload_id, compute_id in placement_by_workload.items():
        workload = workload_by_id.get(workload_id, {})
        workload_names_by_compute.setdefault(compute_id, []).append(
            str(workload.get("name") or workload_id)
        )
    ingress = (
        "loadBalanced"
        if any(item.get("ingressKind") == "loadBalancer" for item in public_paths)
        else "directPublicIp"
        if public_paths
        else "privateEgressOnly"
    )
    display_nodes: list[dict[str, Any]] = []
    for node in resource_plan.get("nodes") or []:
        display = copy.deepcopy(node)
        display["providerKind"] = str(
            node.get("providerPrimitiveKind") or node.get("providerKind") or ""
        )
        node_id = str(node.get("id") or "")
        logical_ref = str(node.get("logicalRef") or "")
        attributes = dict(node.get("attributes") or {})
        zone = str(attributes.get("zone") or "")
        if logical_ref in workload_by_id:
            display_role = str(workload_by_id[logical_ref].get("name") or logical_ref)
        elif logical_ref in compute_units:
            workload_names = workload_names_by_compute.get(logical_ref) or [logical_ref]
            display_role = f"Compute: {' + '.join(sorted(workload_names))}"
            if zone:
                display_role += f" / {zone}"
        elif logical_ref == "public-network":
            display_role = "Public ingress / egress"
            if zone:
                display_role += f" / {zone}"
        else:
            display_role = node_id
        display["displayRole"] = display_role
        if node_id.startswith("traffic-filter-"):
            display["logicalRef"] = node_id.removeprefix("traffic-filter-")
        display_nodes.append(display)
    for value in resource_plan.get("sharedValues") or []:
        display_nodes.append(
            {
                **copy.deepcopy(value),
                "name": (f"{value.get('name') or value.get('id')}\\n{value.get('value')}"),
                "providerKind": "shared-value",
                "entityClass": "sharedValue",
                "handling": "local",
            }
        )
    for block in resource_plan.get("embeddedBlocks") or []:
        block_path = str(block.get("blockPath") or "")
        block_kind = {
            "health_check": "health-check",
            "frontend_ip_configuration": "frontend-ip-config",
            "backend": "backend-group",
            "block_device_mappings": "disk",
            "data_disk": "disk",
            "disk": "disk",
        }.get(block_path, "embedded-block")
        display_nodes.append(
            {
                **copy.deepcopy(block),
                "providerKind": block_kind,
                "entityClass": "embeddedBlock",
                "handling": "inline",
            }
        )
    if public_paths:
        display_nodes.append(
            {
                "id": "public-endpoint",
                "name": "Public HTTP endpoint",
                "group": "endpoint",
                "providerKind": "endpoint",
                "entityClass": "runtimeElement",
                "handling": "runtimeDerived",
            }
        )
    display_edges = []
    for reference in resource_plan.get("references") or []:
        display = copy.deepcopy(reference)
        display["from"] = reference.get("consumerRef")
        display["to"] = reference.get("producerRef")
        display["label"] = (
            f"{reference.get('consumerPath') or 'value'} = "
            f"{reference.get('producerAttribute') or 'id'}"
        )
        display_edges.append(display)
    for connection in graph.get("connections") or []:
        display_edges.append(
            {
                "from": connection.get("sourceRef"),
                "to": connection.get("targetRef"),
                "label": connection.get("protocol") or "connects",
                "evidence": ["design-connection"],
            }
        )
    subnets = [
        node
        for node in display_nodes
        if node.get("providerKind") == "subnet"
        and str(node.get("id") or "").startswith(f"subnet-{primary_compute_id}-")
    ]
    ingress_subnets = [
        node
        for node in display_nodes
        if node.get("providerKind") == "subnet"
        and str(node.get("id") or "").startswith("public-subnet-")
    ]
    display_plan = {
        **resource_plan,
        "nodes": display_nodes,
        "edges": display_edges,
        "workloads": list(workload_by_id.values()),
        "allocations": [
            {
                "workloadRef": item.get("workloadRef"),
                "computeRef": item.get("computeUnitRef"),
            }
            for item in placements
        ],
        "computeNodeId": primary_compute_id,
        "placementConstraints": {
            "minimumSubnets": max(1, len(subnets)),
            "minimumIngressSubnets": max(1, len(ingress_subnets)),
            "selectedIngressZones": [
                str((node.get("attributes") or {}).get("zone") or "") for node in ingress_subnets
            ],
        },
    }
    logical_artifacts = [
        {
            "name": f"{workload.get('name') or workload_id} image source",
            "deployed_on": workload_id,
        }
        for workload_id, workload in workload_by_id.items()
        if (workload.get("artifact") or {}).get("kind") == "generatedApplication"
    ]
    return {
        "plan": display_plan,
        "logicalArtifacts": logical_artifacts,
        "settings": {
            "computeManagement": "managedGroup" if managed else "standalone",
            "replicaCount": replica_count,
            "selectedZones": zones,
            "selectedIngressZones": display_plan["placementConstraints"]["selectedIngressZones"],
            "zoneLayout": "multiZoneSpread" if len(zones) > 1 else "singleZone",
            "publicIngress": ingress,
        },
    }
