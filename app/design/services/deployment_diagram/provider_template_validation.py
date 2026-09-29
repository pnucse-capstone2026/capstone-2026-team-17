"""Provider ResourcePlan template의 완전성과 reference 무결성을 검증한다."""

from __future__ import annotations

import ipaddress
from collections import defaultdict
from typing import Any

RESOURCE_PLAN_SCHEMA = "easydep-resource-plan"
SUPPORTED_PROVIDERS = frozenset({"aws", "azure", "gcp"})


def validate_complete_provider_template(plan: dict[str, Any]) -> None:
    """Provider ResourcePlan의 완전성과 IaC reference 무결성을 검사한다.

    Args:
        plan: 검증할 AWS, Azure 또는 GCP ResourcePlan이다.

    Returns:
        모든 계약을 만족하면 ``None``이다.

    Notes:
        검증 순서와 기존 ``ValueError`` 유형·메시지를 유지하며 입력을 변경하지 않는다.
    """

    if plan.get("schemaVersion") != RESOURCE_PLAN_SCHEMA:
        raise ValueError("unsupported ResourcePlan schemaVersion")
    provider = str(plan.get("provider") or "")
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("ResourcePlan provider is unsupported")
    nodes = list(plan.get("nodes") or [])
    node_ids = [str(item.get("id") or "") for item in nodes]
    if any(not item for item in node_ids) or len(node_ids) != len(set(node_ids)):
        raise ValueError("ResourcePlan node ids must be non-empty and unique")
    known = set(node_ids)
    node_by_id = {str(item["id"]): item for item in nodes}
    for node in nodes:
        handling = str(node.get("handling") or "")
        if handling == "create" and not node.get("terraformTypes"):
            raise ValueError(
                f"Created provider node has no Terraform realization: {node.get('id')}"
            )
        if not node.get("sourceRefs"):
            raise ValueError(f"Provider node has no source reference: {node.get('id')}")
    embedded_blocks = list(plan.get("embeddedBlocks") or [])
    embedded_ids = [str(item.get("id") or "") for item in embedded_blocks]
    if any(not item for item in embedded_ids) or len(embedded_ids) != len(set(embedded_ids)):
        raise ValueError("ResourcePlan embedded block ids must be non-empty and unique")
    for block in embedded_blocks:
        owner = str(block.get("ownerRef") or "")
        if owner not in known or not block.get("blockPath"):
            raise ValueError(f"Embedded block has no valid owner/path: {block.get('id')}")
        if not block.get("sourceRefs"):
            raise ValueError(f"Embedded block has no source reference: {block.get('id')}")
    shared_values = list(plan.get("sharedValues") or [])
    shared_ids = [str(item.get("id") or "") for item in shared_values]
    if any(not item for item in shared_ids) or len(shared_ids) != len(set(shared_ids)):
        raise ValueError("ResourcePlan shared value ids must be non-empty and unique")
    for value in shared_values:
        if value.get("value") is None or value.get("valueType") not in {"string", "number", "bool"}:
            raise ValueError(f"Shared value is incomplete: {value.get('id')}")
        if not value.get("sourceRefs"):
            raise ValueError(f"Shared value has no source reference: {value.get('id')}")
    network_cidr = str((node_by_id.get("network", {}).get("attributes") or {}).get("cidr") or "")
    try:
        network_range: Any = ipaddress.ip_network(network_cidr) if network_cidr else None
        subnet_ranges = [
            (
                str(node.get("id") or ""),
                ipaddress.ip_network(str((node.get("attributes") or {}).get("cidr") or "")),
            )
            for node in nodes
            if node.get("providerPrimitiveKind") == "subnet"
        ]
    except ValueError as error:
        raise ValueError(f"ResourcePlan contains an invalid network CIDR: {error}") from error
    if network_range:
        for subnet_id, subnet_range in subnet_ranges:
            if not subnet_range.subnet_of(network_range):
                raise ValueError(
                    f"ResourcePlan subnet {subnet_id} is outside network {network_cidr}"
                )
    subnet_by_logical_ref = {
        str(node.get("logicalRef") or ""): subnet_range
        for (subnet_id, subnet_range), node in zip(
            subnet_ranges,
            [item for item in nodes if item.get("providerPrimitiveKind") == "subnet"],
        )
        if str(node.get("logicalRef") or "")
    }
    for node in nodes:
        private_ip = (node.get("attributes") or {}).get("privateIp")
        logical_ref = str(node.get("logicalRef") or "")
        if not private_ip or logical_ref not in subnet_by_logical_ref:
            continue
        address = ipaddress.ip_address(str(private_ip))
        subnet_range = subnet_by_logical_ref[logical_ref]
        if address not in subnet_range or address in {
            subnet_range.network_address,
            subnet_range.broadcast_address,
        }:
            raise ValueError(
                f"ResourcePlan private IP {private_ip} is not usable in {subnet_range}"
            )
    for index, (left_id, left_range) in enumerate(subnet_ranges):
        for right_id, right_range in subnet_ranges[index + 1 :]:
            if left_range.overlaps(right_range):
                raise ValueError(
                    "ResourcePlan subnets overlap: "
                    f"{left_id}={left_range}, {right_id}={right_range}"
                )
    consumer_ids = known | set(embedded_ids)
    binding_ids = [str(item.get("id") or "") for item in plan.get("bindingSlots") or []]
    producer_ids = known | set(shared_ids) | set(binding_ids)
    reference_keys: set[tuple[str, str, str, str]] = set()
    shared_consumer_counts: dict[str, int] = defaultdict(int)
    for reference in plan.get("references") or []:
        source = str(reference.get("consumerRef") or "")
        target = str(reference.get("producerRef") or "")
        if source not in consumer_ids or target not in producer_ids or source == target:
            raise ValueError(f"ResourcePlan reference has an invalid endpoint: {source}->{target}")
        key = (
            source,
            target,
            str(reference.get("consumerPath") or ""),
            str(reference.get("producerAttribute") or ""),
        )
        if key in reference_keys:
            raise ValueError(f"ResourcePlan contains a duplicate reference: {key}")
        reference_keys.add(key)
        if not reference.get("consumerPath") or not reference.get("producerAttribute"):
            raise ValueError(f"ResourcePlan reference is incomplete: {source}->{target}")
        if reference.get("cardinality") not in {"one", "many"}:
            raise ValueError(f"ResourcePlan reference has invalid cardinality: {source}->{target}")
        if not reference.get("sourceRefs"):
            raise ValueError(f"ResourcePlan reference has no source reference: {source}->{target}")
        if target in shared_ids:
            shared_consumer_counts[target] += 1
    unused_shared = sorted(item for item in shared_ids if shared_consumer_counts[item] < 2)
    if unused_shared:
        raise ValueError(f"Shared values must have at least two consumers: {unused_shared}")
    workload_ids = {str(item.get("id") or "") for item in plan.get("workloads") or []}
    runtime_workloads = [
        str(container.get("workloadRef") or "")
        for unit in plan.get("runtimeUnits") or []
        for container in unit.get("containers") or []
    ]
    if set(runtime_workloads) != workload_ids or len(runtime_workloads) != len(
        set(runtime_workloads)
    ):
        raise ValueError("Every workload must occur in exactly one runtime unit")
    if any(not item for item in binding_ids) or len(binding_ids) != len(set(binding_ids)):
        raise ValueError("ResourcePlan binding slot ids must be non-empty and unique")
    if any(
        not item.get("field") or not item.get("kind") or not item.get("sourceRefs")
        for item in plan.get("bindingSlots") or []
    ):
        raise ValueError("Every binding slot must be typed and source-grounded")
    required = {"network", "boot-image"}
    if not required.issubset(known):
        raise ValueError(f"ResourcePlan misses base provider closure: {sorted(required - known)}")
    created_types = {
        item
        for node in nodes
        if node.get("handling") == "create"
        for item in node.get("terraformTypes") or []
    }
    expected_compute = {
        "aws": {"aws_instance", "aws_autoscaling_group"},
        "azure": {"azurerm_linux_virtual_machine", "azurerm_linux_virtual_machine_scale_set"},
        "gcp": {
            "google_compute_instance",
            "google_compute_region_instance_group_manager",
            "google_compute_instance_group_manager",
        },
    }[provider]
    if not created_types.intersection(expected_compute):
        raise ValueError("ResourcePlan contains no provider compute realization")

    placements = {
        str(item.get("workloadRef") or ""): str(item.get("computeUnitRef") or "")
        for item in plan.get("placements") or []
    }
    for workload in plan.get("workloads") or []:
        workload_id = str(workload.get("id") or "")
        compute_id = placements.get(workload_id, "")
        if compute_id not in known:
            raise ValueError(f"Workload has no provider compute realization: {workload_id}")
        if (workload.get("artifact") or {}).get("kind") == "generatedApplication":
            if f"registry-{workload_id}" not in known:
                raise ValueError(f"Generated workload has no Registry: {workload_id}")
            if not any(
                item.get("field") == f"workloads.{workload_id}.artifact.imageDigest"
                for item in plan.get("bindingSlots") or []
            ):
                raise ValueError(f"Generated workload has no image digest binding: {workload_id}")

    reference_pairs = {
        (item["consumerRef"], item["producerRef"]) for item in plan.get("references") or []
    }
    for compute_id in set(placements.values()):
        compute = node_by_id[compute_id]
        template_id = f"compute-template-{compute_id}"
        network_owners = {compute_id, template_id} if template_id in known else {compute_id}
        nic_id = f"network-interface-{compute_id}"
        has_subnet = any(
            source in network_owners and str(target).startswith(f"subnet-{compute_id}-")
            for source, target in reference_pairs
        ) or (
            (compute_id, nic_id) in reference_pairs
            and any(
                source == nic_id and str(target).startswith(f"subnet-{compute_id}-")
                for source, target in reference_pairs
            )
        )
        if not has_subnet:
            raise ValueError(f"Compute has no subnet binding: {compute_id}")
        if provider != "azure" and not any(
            (owner, "boot-image") in reference_pairs for owner in network_owners
        ):
            raise ValueError(f"Compute has no boot image binding: {compute_id}")
        filter_id = f"traffic-filter-{compute_id}"
        filter_association = f"security-group-association-{compute_id}"
        has_filter = (
            any((owner, filter_id) in reference_pairs for owner in network_owners)
            or any(
                (owner, f"network-tag-{compute_id}") in reference_pairs for owner in network_owners
            )
            or (
                filter_association in known
                and (filter_association, filter_id) in reference_pairs
                and (filter_association, nic_id) in reference_pairs
            )
        )
        if filter_id not in known or not has_filter:
            raise ValueError(f"Compute has no traffic filter binding: {compute_id}")
        if compute.get("handling") != "create":
            raise ValueError(f"Compute must be a provider resource: {compute_id}")

    for path in plan.get("networkPaths") or []:
        compute_id = str(path.get("computeUnitRef") or "")
        if path.get("kind") == "publicIngress":
            expected = (
                f"load-balancer-{compute_id}"
                if path.get("ingressKind") == "loadBalancer"
                else f"public-ip-{compute_id}"
            )
            if expected not in known:
                raise ValueError(f"Public ingress has no provider endpoint: {path.get('id')}")
        elif path.get("kind") == "natEgress":
            expected = {"aws": "nat-gateway", "azure": "nat-gateway", "gcp": "cloud-nat"}[provider]
            if expected not in known:
                raise ValueError(f"Outbound path has no provider NAT: {path.get('id')}")

    for binding in plan.get("storageBindings") or []:
        storage_id = str(binding.get("storageRef") or "")
        disk_id = f"data-disk-{storage_id}"
        per_replica = binding.get("replicaSemantics") == "perReplica"
        if disk_id not in known and disk_id not in set(embedded_ids):
            raise ValueError(f"Storage binding has no provider disk: {storage_id}")
        if not per_replica and f"disk-attachment-{storage_id}" not in known:
            raise ValueError(f"Storage binding has no disk attachment: {storage_id}")
