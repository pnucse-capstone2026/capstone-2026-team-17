"""DeploymentPlan을 AWS·Azure·GCP ResourcePlan template으로 투영한다."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import re
from collections import defaultdict
from typing import Any, cast

from app.cloudkb.provider_primitives import (
    _PROVIDER_MODELS as PROVIDER_CATALOG,
)
from app.cloudkb.provider_primitives import (
    _TERRAFORM_TYPES as TERRAFORM_TYPES,
)
from app.design.services.deployment_diagram.provider_template_validation import (
    validate_complete_provider_template,
)

RESOURCE_PLAN_SCHEMA = "easydep-resource-plan"
PROVIDER_TEMPLATE_CATALOG = "docker-on-vm-provider-template"
SUPPORTED_PROVIDERS = frozenset({"aws", "azure", "gcp"})
BLOCKING_CLASSES = frozenset({"invalid", "unsupported", "needsInput", "unjustified"})

_CONTRACT_KIND_ALIASES = {
    "aws": {"public-ip": "elastic-ip", "disk": "block-disk"},
    "azure": {
        "network": "virtual-network",
        "compute-instance": "virtual-machine",
        "compute-group": "virtual-machine-scale-set",
        "disk": "managed-disk",
    },
    "gcp": {
        "network": "vpc-network",
        "subnet": "subnetwork",
        "compute-group": "managed-instance-group",
        "compute-template": "instance-template",
        "public-ip": "external-address",
        "disk": "persistent-disk",
    },
}


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-") or "item"


def _refs(*values: Any) -> list[str]:
    refs: list[str] = []
    for value in values:
        if isinstance(value, str):
            value = [value]
        for item in value or []:
            text = str(item).strip()
            if text and text not in refs:
                refs.append(text)
    return refs


def _slot(value: Any, field: str) -> bool:
    return (
        isinstance(value, dict) and value.get("binding") == "late" and value.get("field") == field
    )


class _Template:
    def __init__(self, provider: str, region: str) -> None:
        self.provider = provider
        self.region = region
        self.catalog = PROVIDER_CATALOG[provider]
        self.types = TERRAFORM_TYPES[provider]
        self.nodes: dict[str, dict[str, Any]] = {}
        self.references: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.shared_values: dict[str, dict[str, Any]] = {}
        self.embedded_blocks: dict[str, dict[str, Any]] = {}
        self.bindings: dict[str, dict[str, Any]] = {}

    def add_node(
        self,
        node_id: str,
        provider_kind: str,
        *,
        handling: str = "create",
        entity_class: str | None = None,
        logical_ref: str | None = None,
        owner_ref: str | None = None,
        attributes: dict[str, Any] | None = None,
        source_refs: Any = None,
        rule: str,
    ) -> str:
        if node_id in self.nodes:
            node = self.nodes[node_id]
            node["sourceRefs"] = _refs(node.get("sourceRefs"), source_refs)
            node["attributes"].update(copy.deepcopy(attributes or {}))
            if logical_ref:
                existing_ref = str(node.get("logicalRef") or "")
                if existing_ref and existing_ref != logical_ref:
                    raise ValueError(
                        f"Provider node {node_id} has conflicting logical owners: "
                        f"{existing_ref}, {logical_ref}"
                    )
                node["logicalRef"] = logical_ref
            return node_id
        resource = self.catalog.get("resources", {}).get(provider_kind)
        name, group = resource or (provider_kind.replace("-", " ").title(), "config")
        terraform_types = list(self.types.get(provider_kind, ()))
        if entity_class is None:
            entity_class = (
                "externalArtifact" if handling == "referenceExisting" else "providerResource"
            )
        self.nodes[node_id] = {
            "id": node_id,
            "name": name,
            "group": group,
            "providerKind": _CONTRACT_KIND_ALIASES[self.provider].get(provider_kind, provider_kind),
            "providerPrimitiveKind": provider_kind,
            "entityClass": entity_class,
            "handling": handling,
            "terraformTypes": [] if handling == "referenceExisting" else terraform_types,
            "attributes": copy.deepcopy(attributes or {}),
            "sourceRefs": _refs(source_refs, f"project-policy:{rule}"),
            **({"logicalRef": logical_ref} if logical_ref else {}),
            **({"ownerRef": owner_ref} if owner_ref else {}),
        }
        return node_id

    def reference(
        self,
        consumer: str,
        producer: str,
        *,
        consumer_path: str,
        producer_attribute: str = "id",
        cardinality: str = "one",
        source_refs: Any = None,
        rule: str,
    ) -> None:
        key = (consumer, producer, consumer_path, producer_attribute)
        self.references[key] = {
            "id": f"ref-{_digest(key)[:16]}",
            "consumerRef": consumer,
            "consumerPath": consumer_path,
            "producerRef": producer,
            "producerAttribute": producer_attribute,
            "cardinality": cardinality,
            "sourceRefs": _refs(source_refs, f"project-policy:{rule}"),
        }

    def shared_value(
        self,
        value_id: str,
        *,
        name: str,
        value: Any,
        value_type: str = "string",
        source_refs: Any = None,
        rule: str,
    ) -> str:
        self.shared_values[value_id] = {
            "id": value_id,
            "name": name,
            "value": copy.deepcopy(value),
            "valueType": value_type,
            "sourceRefs": _refs(source_refs, f"project-policy:{rule}"),
        }
        return value_id

    def embedded_block(
        self,
        block_id: str,
        *,
        owner_ref: str,
        block_path: str,
        name: str,
        attributes: dict[str, Any] | None = None,
        logical_ref: str | None = None,
        source_refs: Any = None,
        rule: str,
    ) -> str:
        self.embedded_blocks[block_id] = {
            "id": block_id,
            "name": name,
            "ownerRef": owner_ref,
            "blockPath": block_path,
            "attributes": copy.deepcopy(attributes or {}),
            "sourceRefs": _refs(source_refs, f"project-policy:{rule}"),
            **({"logicalRef": logical_ref} if logical_ref else {}),
        }
        return block_id

    def binding(
        self,
        binding_id: str,
        *,
        field: str,
        kind: str,
        phase: str,
        required: bool = True,
        structural: bool = False,
        default: Any = None,
        source_refs: Any = None,
    ) -> None:
        self.bindings[binding_id] = {
            "id": binding_id,
            "field": field,
            "kind": kind,
            "phase": phase,
            "required": required,
            "structural": structural,
            "sourceRefs": _refs(source_refs, "project-policy:typed-binding"),
            **({"default": copy.deepcopy(default)} if default is not None else {}),
        }


def _zones(compute: dict[str, Any], location_plan: dict[str, Any]) -> list[str]:
    zones = [str(item) for item in compute.get("zones") or [] if str(item)]
    if zones:
        return zones
    candidates = [str(item) for item in location_plan.get("candidateZones") or [] if str(item)]
    return candidates[:1]


def _cidr(slot: int) -> str:
    return f"10.80.{slot}.0/24"


def _add_registry_delivery(
    template: _Template,
    *,
    graph: dict[str, Any],
    placements: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    registries: dict[str, str] = {}
    identity_by_compute: dict[str, str] = {}
    generated = [
        workload
        for workload in graph.get("workloads") or []
        if (workload.get("artifact") or {}).get("kind") == "generatedApplication"
    ]
    for workload in generated:
        workload_id = str(workload.get("id") or "")
        refs = workload.get("sourceRefs") or []
        registry_kind = "app-registry"
        registry_id = f"registry-{workload_id}"
        template.add_node(
            registry_id,
            registry_kind,
            logical_ref=workload_id,
            attributes={"repositoryPerWorkload": True, "immutableDigest": True},
            source_refs=refs,
            rule=f"{template.provider}.generated-application-registry",
        )
        registries[workload_id] = registry_id
        template.binding(
            f"image-digest-{workload_id}",
            field=f"workloads.{workload_id}.artifact.imageDigest",
            kind="imageDigest",
            phase="implementation",
            source_refs=refs,
        )
        compute_id = placements.get(workload_id, "")
        if not compute_id:
            continue
        identity_id = identity_by_compute.get(compute_id)
        if identity_id is None:
            identity_id = f"registry-pull-identity-{compute_id}"
            identity_by_compute[compute_id] = identity_id
            template.add_node(
                identity_id,
                "registry-pull-identity",
                logical_ref=compute_id,
                source_refs=refs,
                rule=f"{template.provider}.registry-pull-identity",
            )
            if template.provider == "aws":
                profile_id = f"registry-instance-profile-{compute_id}"
                template.add_node(
                    profile_id,
                    "registry-instance-profile",
                    logical_ref=compute_id,
                    source_refs=refs,
                    rule="aws.registry-instance-profile",
                )
                template.reference(
                    profile_id,
                    identity_id,
                    consumer_path="role",
                    producer_attribute="name",
                    rule="aws.registry-instance-profile",
                )
        binding_id = f"registry-pull-binding-{compute_id}-{workload_id}"
        template.add_node(
            binding_id,
            "registry-pull-binding",
            logical_ref=workload_id,
            source_refs=refs,
            rule=f"{template.provider}.registry-pull-grant",
        )
        template.reference(
            binding_id,
            identity_id,
            consumer_path={"aws": "role", "azure": "principal_id", "gcp": "member"}[
                template.provider
            ],
            producer_attribute={
                "aws": "name",
                "azure": "principal_id",
                "gcp": "email",
            }[template.provider],
            rule=f"{template.provider}.registry-pull-grant",
        )
        if template.provider != "aws":
            template.reference(
                binding_id,
                registry_id,
                consumer_path="scope",
                rule=f"{template.provider}.registry-pull-grant",
            )
    return registries, identity_by_compute


def _add_secret_delivery(
    template: _Template,
    *,
    graph: dict[str, Any],
    placements: dict[str, str],
    identity_by_compute: dict[str, str],
) -> None:
    for workload in graph.get("workloads") or []:
        workload_id = str(workload.get("id") or "")
        refs = workload.get("sourceRefs") or []
        for config in workload.get("configuration") or []:
            if not (
                config.get("sensitive") is True
                or str(config.get("kind") or "") in {"secret", "secretBinding"}
            ):
                continue
            config_id = str(config.get("id") or config.get("name") or "secret")
            secret_id = f"secret-ref-{workload_id}-{config_id}"
            compute_id = placements.get(workload_id, "")
            identity_id = identity_by_compute.get(compute_id)
            if identity_id is None:
                identity_id = f"secret-identity-{compute_id}"
                identity_by_compute[compute_id] = identity_id
            grant_id = f"secret-access-binding-{workload_id}-{config_id}"
            template.add_node(
                secret_id,
                "secret-ref",
                handling="referenceExisting",
                attributes={"credentialCollectionByEasyDep": False},
                source_refs=config.get("sourceRefs") or refs,
                rule=f"{template.provider}.external-secret-reference",
            )
            template.add_node(
                identity_id,
                "state-secret-identity",
                source_refs=config.get("sourceRefs") or refs,
                rule=f"{template.provider}.secret-consumer-identity",
            )
            if template.provider == "aws" and identity_id.startswith("secret-identity-"):
                profile_id = f"secret-instance-profile-{compute_id}"
                template.add_node(
                    profile_id,
                    "state-secret-instance-profile",
                    source_refs=config.get("sourceRefs") or refs,
                    rule="aws.secret-instance-profile",
                )
                template.reference(
                    profile_id,
                    identity_id,
                    consumer_path="role",
                    producer_attribute="name",
                    rule="aws.secret-instance-profile",
                )
            template.add_node(
                grant_id,
                "state-secret-access-binding",
                source_refs=config.get("sourceRefs") or refs,
                rule=f"{template.provider}.least-privilege-secret-read",
            )
            secret_binding_id = f"secret-reference-{workload_id}-{config_id}"
            template.reference(
                grant_id,
                secret_binding_id,
                consumer_path="scope",
                producer_attribute="value",
                rule=f"{template.provider}.least-privilege-secret-read",
            )
            template.reference(
                grant_id,
                identity_id,
                consumer_path={"aws": "role", "azure": "principal_id", "gcp": "member"}[
                    template.provider
                ],
                producer_attribute={
                    "aws": "id",
                    "azure": "principal_id",
                    "gcp": "email",
                }[template.provider],
                rule=f"{template.provider}.least-privilege-secret-read",
            )
            template.binding(
                secret_binding_id,
                field=f"workloads.{workload_id}.configuration.{config_id}.secretRef",
                kind="secretReference",
                phase="deployment",
                source_refs=config.get("sourceRefs") or refs,
            )


def _runtime_units(
    graph: dict[str, Any],
    deployment_plan: dict[str, Any],
    registries: dict[str, str],
    *,
    compute_details: dict[str, dict[str, Any]],
    endpoint_producers: dict[str, str],
) -> list[dict[str, Any]]:
    workloads = {str(item.get("id") or ""): item for item in graph.get("workloads") or []}
    placement = {
        str(item.get("workloadRef") or ""): str(item.get("computeUnitRef") or "")
        for item in deployment_plan.get("placements") or []
    }
    storage_by_workload: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in deployment_plan.get("storageBindings") or []:
        storage_by_workload[str(item.get("workloadRef") or "")].append(copy.deepcopy(item))
    connections = list(graph.get("connections") or [])
    runtime_bindings_by_workload: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for binding in deployment_plan.get("runtimeBindings") or []:
        resolved = copy.deepcopy(binding)
        if resolved.get("kind") == "endpointEnvironment":
            strategy = str(resolved.get("strategy") or "")
            target_compute = str(resolved.get("targetComputeUnitRef") or "")
            if strategy == "containerDns":
                resolved["endpointHost"] = resolved.get("targetWorkloadRef")
            elif strategy == "staticPrivateIp":
                resolved["endpointHost"] = (compute_details.get(target_compute) or {}).get(
                    "privateIp"
                )
            elif strategy == "internalLoadBalancer":
                resolved["endpointProducerRef"] = endpoint_producers.get(
                    str(resolved.get("connectionRef") or "")
                )
            elif strategy == "externalInput":
                resolved["endpointValueBindingRef"] = (
                    f"external-endpoint-{_slug(resolved.get('connectionRef'))}"
                )
        runtime_bindings_by_workload[str(binding.get("workloadRef") or "")].append(resolved)
    result: list[dict[str, Any]] = []
    for compute in deployment_plan.get("computeUnits") or []:
        compute_id = str(compute.get("id") or "")
        containers: list[dict[str, Any]] = []
        for workload_id, workload in workloads.items():
            if placement.get(workload_id) != compute_id:
                continue
            artifact = copy.deepcopy(workload.get("artifact") or {})
            generated = artifact.get("kind") == "generatedApplication"
            image = (
                {"binding": "late", "field": f"workloads.{workload_id}.artifact.imageDigest"}
                if generated
                else artifact.get("image")
            )
            containers.append(
                {
                    "workloadRef": workload_id,
                    "containerName": workload_id,
                    "image": image,
                    "registryRef": registries.get(workload_id),
                    "interfaces": copy.deepcopy(workload.get("interfaces") or []),
                    "configuration": copy.deepcopy(workload.get("configuration") or []),
                    "runtimeBindings": runtime_bindings_by_workload.get(workload_id, []),
                    "mounts": storage_by_workload.get(workload_id, []),
                    "restartPolicy": "always",
                    "dependsOn": [
                        str(item.get("targetRef") or "")
                        for item in connections
                        if str(item.get("sourceRef") or "") == workload_id
                        and placement.get(str(item.get("targetRef") or "")) == compute_id
                    ],
                    "sourceRefs": _refs(workload.get("sourceRefs")),
                }
            )
        result.append(
            {
                "id": f"runtime-{compute_id}",
                "computeUnitRef": compute_id,
                "bootstrapOwnerRef": (
                    (compute_details.get(compute_id) or {}).get("template")
                    or (compute_details.get(compute_id) or {}).get("compute")
                ),
                "orchestrator": "docker-compose",
                "containerNetwork": f"easydep-{compute_id}",
                "containers": containers,
                "sourceRefs": _refs(compute.get("sourceRefs")),
            }
        )
    return result


def _provider_kind(provider: str, generic: str) -> str:
    return {
        "aws": {
            "network": "network",
            "subnet": "subnet",
            "compute": "compute-instance",
            "group": "compute-group",
            "template": "compute-template",
            "filter": "security-group",
            "publicIp": "public-ip",
            "disk": "disk",
            "diskAttachment": "disk-attachment",
        },
        "azure": {
            "network": "network",
            "subnet": "subnet",
            "compute": "compute-instance",
            "group": "compute-group",
            "template": "compute-group",
            "filter": "security-group",
            "publicIp": "public-ip",
            "disk": "disk",
            "diskAttachment": "disk-attachment",
        },
        "gcp": {
            "network": "network",
            "subnet": "subnet",
            "compute": "compute-instance",
            "group": "compute-group",
            "template": "compute-template",
            "filter": "firewall",
            "publicIp": "public-ip",
            "disk": "disk",
            "diskAttachment": "disk-attachment",
        },
    }[provider][generic]


def _add_network_and_compute(
    template: _Template,
    *,
    deployment_plan: dict[str, Any],
    identity_by_compute: dict[str, str],
) -> dict[str, dict[str, Any]]:
    provider = template.provider
    location = deployment_plan.get("locationPlan") or {}
    computes = {
        str(item.get("id") or ""): item for item in deployment_plan.get("computeUnits") or []
    }
    public_paths = [
        item
        for item in deployment_plan.get("networkPaths") or []
        if item.get("kind") == "publicIngress"
    ]
    public_by_compute: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in public_paths:
        public_by_compute[str(path.get("computeUnitRef") or "")].append(path)
    nat_compute = {
        str(item.get("computeUnitRef") or "")
        for item in deployment_plan.get("networkPaths") or []
        if item.get("kind") == "natEgress"
    }
    subnet_slot = 0

    def allocate_subnet_cidr() -> str:
        nonlocal subnet_slot
        subnet_slot += 1
        if subnet_slot > 254:
            raise ValueError("The regional /16 network cannot hold more than 254 /24 subnets")
        return _cidr(subnet_slot)

    template.add_node(
        "network",
        _provider_kind(provider, "network"),
        attributes={"cidr": "10.80.0.0/16", "region": template.region},
        rule=f"{provider}.isolated-network",
    )
    if provider == "azure":
        template.add_node(
            "resource-group",
            "resource-group",
            attributes={"region": template.region},
            rule="azure.resource-group",
        )
        template.reference(
            "network",
            "resource-group",
            consumer_path="resource_group_name",
            producer_attribute="name",
            rule="azure.resource-group",
        )
    boot_kind = "boot-image"
    template.add_node(
        "boot-image",
        boot_kind,
        handling="referenceExisting",
        attributes={"selectionPolicy": "providerDefaultLinuxImage"},
        rule=f"{provider}.linux-boot-image-reference",
    )
    template.binding(
        "resource-prefix",
        field="deployment.resourcePrefix",
        kind="resourceNamePrefix",
        phase="deployment",
        default="easydep",
    )
    if provider == "aws":
        template.binding(
            "boot-image-id",
            field="deployment.bootImageId",
            kind="bootImageId",
            phase="deployment",
        )
    public_zones = sorted(
        {zone for compute in computes.values() for zone in _zones(compute, location)}
    ) or [""]
    needs_public_network = bool(public_paths or nat_compute or identity_by_compute)
    if provider == "aws" and needs_public_network:
        template.add_node(
            "internet-gateway",
            "internet-gateway",
            rule="aws.internet-gateway",
        )
        template.reference(
            "internet-gateway",
            "network",
            consumer_path="vpc_id",
            rule="aws.internet-gateway",
        )
        template.add_node(
            "public-route-table",
            "ingress-route-table",
            rule="aws.public-route-table",
        )
        template.reference(
            "public-route-table",
            "network",
            consumer_path="vpc_id",
            rule="aws.public-route-table",
        )
        template.add_node(
            "public-default-route",
            "ingress-default-route",
            attributes={"destination": "0.0.0.0/0"},
            rule="aws.public-default-route",
        )
        template.reference(
            "public-default-route",
            "public-route-table",
            consumer_path="route_table_id",
            rule="aws.public-default-route",
        )
        template.reference(
            "public-default-route",
            "internet-gateway",
            consumer_path="gateway_id",
            rule="aws.public-default-route",
        )
    public_subnets: list[str] = []
    if nat_compute or any(path.get("ingressKind") == "loadBalancer" for path in public_paths):
        for index, zone in enumerate(public_zones, start=1):
            subnet_id = f"public-subnet-{index}"
            public_subnets.append(subnet_id)
            template.add_node(
                subnet_id,
                _provider_kind(provider, "subnet"),
                logical_ref="public-network",
                attributes={
                    "zone": zone,
                    "cidr": allocate_subnet_cidr(),
                    "public": True,
                    "purpose": "publicIngressOrEgress",
                },
                rule=f"{provider}.public-subnet",
            )
            template.reference(
                subnet_id,
                "network",
                consumer_path={
                    "aws": "vpc_id",
                    "azure": "virtual_network_name",
                    "gcp": "network",
                }[provider],
                producer_attribute="name" if provider == "azure" else "id",
                rule=f"{provider}.public-subnet",
            )
            if provider == "aws":
                association_id = f"public-route-association-{index}"
                template.add_node(
                    association_id,
                    "ingress-route-association",
                    rule="aws.public-route-association",
                )
                template.reference(
                    association_id,
                    subnet_id,
                    consumer_path="subnet_id",
                    rule="aws.public-route-association",
                )
                template.reference(
                    association_id,
                    "public-route-table",
                    consumer_path="route_table_id",
                    rule="aws.public-route-association",
                )
    if nat_compute:
        if not public_subnets:
            raise ValueError("NAT closure requires one public subnet")
        if provider == "aws":
            template.add_node(
                "nat-public-ip",
                "nat-public-ip",
                rule="aws.nat-public-ip",
            )
            template.add_node(
                "nat-gateway",
                "nat-gateway",
                rule="aws.nat-gateway",
            )
            template.reference(
                "nat-gateway",
                "nat-public-ip",
                consumer_path="allocation_id",
                rule="aws.nat-gateway",
            )
            template.reference(
                "nat-gateway",
                public_subnets[0],
                consumer_path="subnet_id",
                rule="aws.nat-gateway",
            )
        elif provider == "azure":
            template.add_node("nat-public-ip", "nat-public-ip", rule="azure.nat-public-ip")
            template.add_node("nat-gateway", "nat-gateway", rule="azure.nat-gateway")
            template.add_node(
                "nat-public-ip-association",
                "nat-public-ip-association",
                rule="azure.nat-public-ip-association",
            )
            template.reference(
                "nat-public-ip-association",
                "nat-gateway",
                consumer_path="nat_gateway_id",
                rule="azure.nat-public-ip-association",
            )
            template.reference(
                "nat-public-ip-association",
                "nat-public-ip",
                consumer_path="public_ip_address_id",
                rule="azure.nat-public-ip-association",
            )
        else:
            template.add_node("cloud-router", "cloud-router", rule="gcp.cloud-router")
            template.add_node("cloud-nat", "cloud-nat", rule="gcp.cloud-nat")
            template.reference(
                "cloud-router",
                "network",
                consumer_path="network",
                rule="gcp.cloud-router",
            )
            template.reference(
                "cloud-nat",
                "cloud-router",
                consumer_path="router",
                producer_attribute="name",
                rule="gcp.cloud-nat",
            )
    details: dict[str, dict[str, Any]] = {}
    for compute_id, compute in sorted(computes.items()):
        refs = compute.get("sourceRefs") or []
        zones = _zones(compute, location)
        managed = compute.get("kind") == "managedVmGroup"
        direct_public = any(
            path.get("ingressKind") == "directPublicIp"
            for path in public_by_compute.get(compute_id, [])
        )
        subnet_ids: list[str] = []
        for zone_index, zone in enumerate(zones or [""], start=1):
            subnet_id = f"subnet-{compute_id}-{zone_index}"
            subnet_ids.append(subnet_id)
            is_public = direct_public and not managed
            template.add_node(
                subnet_id,
                _provider_kind(provider, "subnet"),
                logical_ref=compute_id,
                attributes={
                    "zone": zone,
                    "cidr": allocate_subnet_cidr(),
                    "public": is_public,
                    "purpose": "computePlacement",
                },
                source_refs=refs,
                rule=f"{provider}.compute-subnet",
            )
            template.reference(
                subnet_id,
                "network",
                consumer_path={
                    "aws": "vpc_id",
                    "azure": "virtual_network_name",
                    "gcp": "network",
                }[provider],
                producer_attribute="name" if provider == "azure" else "id",
                source_refs=refs,
                rule=f"{provider}.compute-subnet",
            )
            if provider == "aws":
                if is_public:
                    route_table = "public-route-table"
                    association_kind = "ingress-route-association"
                    rule = "aws.public-compute-route-association"
                else:
                    route_table = f"private-route-table-{compute_id}"
                    association_kind = "application-route-association"
                    rule = "aws.private-compute-route-association"
                    template.add_node(
                        route_table,
                        "application-route-table",
                        source_refs=refs,
                        rule="aws.private-route-table",
                    )
                    template.reference(
                        route_table,
                        "network",
                        consumer_path="vpc_id",
                        rule="aws.private-route-table",
                    )
                    default_route = f"private-default-route-{compute_id}"
                    template.add_node(
                        default_route,
                        "application-default-route",
                        attributes={"destination": "0.0.0.0/0"},
                        source_refs=refs,
                        rule="aws.private-default-route",
                    )
                    template.reference(
                        default_route,
                        route_table,
                        consumer_path="route_table_id",
                        rule="aws.private-default-route",
                    )
                    template.reference(
                        default_route,
                        "nat-gateway",
                        consumer_path="nat_gateway_id",
                        rule="aws.private-default-route",
                    )
                association_id = f"route-association-{compute_id}-{zone_index}"
                template.add_node(
                    association_id,
                    association_kind,
                    source_refs=refs,
                    rule=rule,
                )
                template.reference(
                    association_id,
                    subnet_id,
                    consumer_path="subnet_id",
                    rule=rule,
                )
                template.reference(
                    association_id,
                    route_table,
                    consumer_path="route_table_id",
                    rule=rule,
                )
            elif provider == "azure" and compute_id in nat_compute:
                association_id = f"nat-association-{compute_id}-{zone_index}"
                template.add_node(
                    association_id,
                    "nat-association",
                    source_refs=refs,
                    rule="azure.subnet-nat-association",
                )
                template.reference(
                    association_id,
                    subnet_id,
                    consumer_path="subnet_id",
                    rule="azure.subnet-nat-association",
                )
                template.reference(
                    association_id,
                    "nat-gateway",
                    consumer_path="nat_gateway_id",
                    rule="azure.subnet-nat-association",
                )
        if provider == "gcp" and compute_id in nat_compute:
            template.reference(
                "cloud-nat",
                subnet_ids[0],
                consumer_path="subnetwork[].name",
                cardinality="many",
                rule="gcp.cloud-nat",
            )
        filter_id = f"traffic-filter-{compute_id}"
        public_interfaces = copy.deepcopy(public_by_compute.get(compute_id, []))
        # GCP firewall rules require an allow/deny block.  A compute unit with no
        # public path must not be represented by a synthetic 0.0.0.0/0 all-port
        # rule merely to preserve the network-tag topology.  Keep the tag anchor
        # as a reference-only node; connection-specific internal firewalls are
        # added later by _add_internal_traffic with their real target port.
        create_filter = provider != "gcp" or bool(public_interfaces)
        template.add_node(
            filter_id,
            _provider_kind(provider, "filter"),
            handling="create" if create_filter else "referenceExisting",
            logical_ref=compute_id,
            attributes={"publicInterfaces": public_interfaces},
            source_refs=refs,
            rule=f"{provider}.compute-traffic-filter",
        )
        if create_filter and provider in {"aws", "gcp"}:
            template.reference(
                filter_id,
                "network",
                consumer_path="vpc_id" if provider == "aws" else "network",
                rule=f"{provider}.compute-traffic-filter",
            )
        compute_node = compute_id
        selected_sku = compute.get("vmSku")
        sku_attributes = {"vmSku": selected_sku} if isinstance(selected_sku, str) and selected_sku.strip() else {}
        if managed:
            template_id = f"compute-template-{compute_id}"
            if provider != "azure":
                template.add_node(
                    template_id,
                    _provider_kind(provider, "template"),
                    logical_ref=compute_id,
                    attributes={"zones": zones, **sku_attributes},
                    source_refs=refs,
                    rule=f"{provider}.managed-compute-template",
                )
            template.add_node(
                compute_node,
                _provider_kind(provider, "group"),
                logical_ref=compute_id,
                attributes={
                    "replicaCount": compute.get("replicaCount"),
                    "zones": zones,
                    "managedReplacement": True,
                    **sku_attributes,
                },
                source_refs=refs,
                rule=f"{provider}.managed-compute-group",
            )
            if provider != "azure":
                template.reference(
                    compute_node,
                    template_id,
                    consumer_path=(
                        "launch_template.id" if provider == "aws" else "version.instance_template"
                    ),
                    producer_attribute="id" if provider == "aws" else "self_link",
                    rule=f"{provider}.managed-compute-group",
                )
            if provider == "aws":
                for subnet_id in subnet_ids:
                    template.reference(
                        compute_node,
                        subnet_id,
                        consumer_path="vpc_zone_identifier[]",
                        cardinality="many",
                        rule="aws.managed-compute-network",
                    )
                template.reference(
                    template_id,
                    filter_id,
                    consumer_path="vpc_security_group_ids[]",
                    cardinality="many",
                    rule="aws.managed-compute-network",
                )
            elif provider == "azure":
                template.reference(
                    compute_node,
                    subnet_ids[0],
                    consumer_path="network_interface.ip_configuration.subnet_id",
                    rule="azure.managed-compute-network",
                )
                template.reference(
                    compute_node,
                    filter_id,
                    consumer_path="network_interface.network_security_group_id",
                    rule="azure.managed-compute-network",
                )
            else:
                template.reference(
                    template_id,
                    subnet_ids[0],
                    consumer_path="network_interface.subnetwork",
                    rule="gcp.managed-compute-network",
                )
        else:
            template.add_node(
                compute_node,
                _provider_kind(provider, "compute"),
                logical_ref=compute_id,
                attributes={
                    "replicaCount": 1,
                    "zone": zones[0] if zones else "",
                    **sku_attributes,
                    "privateIp": str(
                        ipaddress.ip_network(
                            template.nodes[subnet_ids[0]]["attributes"]["cidr"]
                        ).network_address
                        + 10
                    ),
                },
                source_refs=refs,
                rule=f"{provider}.standalone-compute",
            )
            if provider == "azure":
                nic_id = f"network-interface-{compute_id}"
                template.add_node(
                    nic_id,
                    "network-interface",
                    logical_ref=compute_id,
                    attributes={
                        "privateAddressAllocation": "Static",
                        "privateIp": template.nodes[compute_node]["attributes"]["privateIp"],
                    },
                    source_refs=refs,
                    rule="azure.standalone-network-interface",
                )
                template.reference(
                    compute_node,
                    nic_id,
                    consumer_path="network_interface_ids[]",
                    cardinality="many",
                    rule="azure.standalone-network-interface",
                )
                template.reference(
                    nic_id,
                    subnet_ids[0],
                    consumer_path="subnet_id",
                    rule="azure.standalone-network-interface",
                )
                association_id = f"security-group-association-{compute_id}"
                template.add_node(
                    association_id,
                    "security-group-association",
                    source_refs=refs,
                    rule="azure.nic-security-group-association",
                )
                template.reference(
                    association_id,
                    nic_id,
                    consumer_path="network_interface_id",
                    rule="azure.nic-security-group-association",
                )
                template.reference(
                    association_id,
                    filter_id,
                    consumer_path="network_security_group_id",
                    rule="azure.nic-security-group-association",
                )
            else:
                template.reference(
                    compute_node,
                    subnet_ids[0],
                    consumer_path=(
                        "subnet_id" if provider == "aws" else "network_interface.subnetwork"
                    ),
                    rule=f"{provider}.standalone-compute-network",
                )
                if provider == "aws":
                    template.reference(
                        compute_node,
                        filter_id,
                        consumer_path="vpc_security_group_ids[]",
                        cardinality="many",
                        rule="aws.standalone-compute-network",
                    )
        if provider == "gcp":
            network_tag_id = template.shared_value(
                f"network-tag-{compute_id}",
                name="Compute network tag",
                value=filter_id,
                source_refs=refs,
                rule="gcp.compute-firewall-network-tag",
            )
            tag_consumer = template_id if managed else compute_node
            template.reference(
                tag_consumer,
                network_tag_id,
                consumer_path="tags[]",
                producer_attribute="value",
                cardinality="many",
                rule="gcp.compute-firewall-network-tag",
            )
            if create_filter:
                template.reference(
                    filter_id,
                    network_tag_id,
                    consumer_path="target_tags[]",
                    producer_attribute="value",
                    cardinality="many",
                    rule="gcp.compute-firewall-network-tag",
                )
            else:
                # A no-ingress compute still needs two real consumers for the
                # shared tag.  The provider-visible label gives operators a
                # traceable anchor without manufacturing an all-port firewall.
                template.reference(
                    tag_consumer,
                    network_tag_id,
                    consumer_path="labels.easydep_traffic_tag",
                    producer_attribute="value",
                    rule="gcp.compute-traffic-label",
                )
        boot_consumer = (
            compute_node if provider == "azure" or not managed else f"compute-template-{compute_id}"
        )
        if provider != "azure":
            template.reference(
                boot_consumer,
                "boot-image",
                consumer_path=(
                    "ami"
                    if provider == "aws" and not managed
                    else "image_id"
                    if provider == "aws"
                    else "boot_disk.initialize_params.image"
                ),
                producer_attribute="value",
                rule=f"{provider}.linux-boot-image-reference",
            )
        identity_id = identity_by_compute.get(compute_id)
        if identity_id:
            owner = (
                compute_node
                if provider == "azure" or not managed
                else f"compute-template-{compute_id}"
            )
            identity_producer = identity_id
            if provider == "aws":
                identity_producer = (
                    f"registry-instance-profile-{compute_id}"
                    if f"registry-instance-profile-{compute_id}" in template.nodes
                    else f"secret-instance-profile-{compute_id}"
                )
            template.reference(
                owner,
                identity_producer,
                consumer_path={
                    "aws": "iam_instance_profile",
                    "azure": "identity.identity_ids[]",
                    "gcp": "service_account.email",
                }[provider],
                producer_attribute={"aws": "name", "azure": "id", "gcp": "email"}[provider],
                cardinality="many" if provider == "azure" else "one",
                rule=f"{provider}.compute-registry-identity",
            )
        if not isinstance(compute.get("vmSku"), str) or not str(compute.get("vmSku")).strip():
            template.binding(
                f"vm-sku-{compute_id}",
                field=f"computeUnits.{compute_id}.vmSku",
                kind="vmSku",
                phase="deployment",
                source_refs=refs,
            )
        details[compute_id] = {
            "compute": compute_node,
            "template": (
                f"compute-template-{compute_id}" if managed and provider != "azure" else None
            ),
            "subnets": subnet_ids,
            "filter": filter_id,
            "publicPaths": public_by_compute.get(compute_id, []),
            "managed": managed,
            "zones": zones,
            "privateIp": (
                template.nodes[compute_node]["attributes"].get("privateIp") if not managed else None
            ),
        }
        if provider == "azure":
            for node_id in [compute_node, filter_id, *subnet_ids]:
                template.reference(
                    node_id,
                    "resource-group",
                    consumer_path="resource_group_name",
                    producer_attribute="name",
                    rule="azure.resource-group",
                )
    return details


def _add_ingress(
    template: _Template,
    *,
    compute_details: dict[str, dict[str, Any]],
) -> None:
    provider = template.provider
    for compute_id, details in compute_details.items():
        paths = details["publicPaths"]
        if not paths:
            continue
        direct = [path for path in paths if path.get("ingressKind") == "directPublicIp"]
        balanced = [path for path in paths if path.get("ingressKind") == "loadBalancer"]
        if direct:
            address_id = f"public-ip-{compute_id}"
            template.add_node(
                address_id,
                _provider_kind(provider, "publicIp"),
                logical_ref=compute_id,
                attributes={"interfaces": copy.deepcopy(direct)},
                source_refs=[ref for path in direct for ref in path.get("sourceRefs") or []],
                rule=f"{provider}.direct-public-address",
            )
            address_owner = (
                f"network-interface-{compute_id}" if provider == "azure" else details["compute"]
            )
            if provider == "aws":
                template.reference(
                    address_id,
                    address_owner,
                    consumer_path="instance",
                    rule="aws.direct-public-address",
                )
            else:
                template.reference(
                    address_owner,
                    address_id,
                    consumer_path=(
                        "public_ip_address_id"
                        if provider == "azure"
                        else "network_interface.access_config.nat_ip"
                    ),
                    producer_attribute="id" if provider == "azure" else "address",
                    rule=f"{provider}.direct-public-address",
                )
        if not balanced:
            continue
        lb_kind = "load-balancer" if provider in {"aws", "azure"} else "forwarding-rule"
        lb_id = f"load-balancer-{compute_id}"
        template.add_node(
            lb_id,
            lb_kind,
            logical_ref=compute_id,
            attributes={"scheme": "public", "zones": details["zones"]},
            source_refs=[ref for path in balanced for ref in path.get("sourceRefs") or []],
            rule=f"{provider}.public-l4-load-balancer",
        )
        if provider == "aws":
            for subnet in [
                node_id for node_id in template.nodes if node_id.startswith("public-subnet-")
            ]:
                template.reference(
                    lb_id,
                    subnet,
                    consumer_path="subnets[]",
                    cardinality="many",
                    rule="aws.public-l4-load-balancer",
                )
        elif provider == "azure":
            frontend_id = f"frontend-ip-config-{compute_id}"
            frontend_name_id = f"frontend-name-{compute_id}"
            address_id = f"public-ip-{compute_id}"
            template.add_node(
                address_id,
                "public-ip",
                logical_ref=compute_id,
                rule="azure.load-balancer-public-ip",
            )
            template.embedded_block(
                frontend_id,
                owner_ref=lb_id,
                block_path="frontend_ip_configuration",
                name="Frontend IP configuration",
                logical_ref=compute_id,
                attributes={"scheme": "public"},
                rule="azure.load-balancer-frontend",
            )
            template.reference(
                frontend_id,
                address_id,
                consumer_path="public_ip_address_id",
                rule="azure.load-balancer-frontend",
            )
            template.shared_value(
                frontend_name_id,
                name="Load balancer frontend name",
                value="public",
                rule="azure.load-balancer-frontend-name",
            )
            template.reference(
                frontend_id,
                frontend_name_id,
                consumer_path="name",
                producer_attribute="value",
                rule="azure.load-balancer-frontend-name",
            )
        for index, path in enumerate(balanced, start=1):
            refs = path.get("sourceRefs") or []
            workload_id = str(path.get("targetWorkloadRef") or "")
            if provider == "aws":
                backend_id = f"backend-group-{compute_id}-{index}"
                health_id = f"health-check-{compute_id}-{index}"
                listener_id = f"listener-{compute_id}-{index}"
                template.add_node(
                    backend_id,
                    "backend-group",
                    logical_ref=workload_id,
                    attributes={"port": copy.deepcopy(path.get("port")), "protocol": "TCP"},
                    source_refs=refs,
                    rule="aws.load-balancer-target-group",
                )
                template.reference(
                    backend_id,
                    "network",
                    consumer_path="vpc_id",
                    rule="aws.load-balancer-target-group",
                )
                template.add_node(
                    listener_id,
                    "listener",
                    logical_ref=workload_id,
                    attributes={"port": copy.deepcopy(path.get("port")), "protocol": "TCP"},
                    source_refs=refs,
                    rule="aws.load-balancer-listener",
                )
                template.embedded_block(
                    health_id,
                    owner_ref=backend_id,
                    block_path="health_check",
                    name="Health check",
                    logical_ref=workload_id,
                    attributes={
                        "protocol": "HTTP",
                        "path": {"binding": "late", "field": "healthPath"},
                    },
                    source_refs=refs,
                    rule="aws.load-balancer-health-check",
                )
                template.reference(
                    listener_id,
                    lb_id,
                    consumer_path="load_balancer_arn",
                    producer_attribute="arn",
                    rule="aws.load-balancer-listener",
                )
                template.reference(
                    listener_id,
                    backend_id,
                    consumer_path="default_action.target_group_arn",
                    producer_attribute="arn",
                    rule="aws.load-balancer-listener",
                )
                template.reference(
                    details["compute"],
                    backend_id,
                    consumer_path="target_group_arns[]",
                    producer_attribute="arn",
                    cardinality="many",
                    rule="aws.autoscaling-target-registration",
                )
            elif provider == "azure":
                backend_id = f"backend-group-{compute_id}-{index}"
                health_id = f"health-check-{compute_id}-{index}"
                rule_id = f"routing-rule-{compute_id}-{index}"
                frontend_name_id = f"frontend-name-{compute_id}"
                template.add_node(
                    backend_id,
                    "backend-group",
                    logical_ref=workload_id,
                    source_refs=refs,
                    rule="azure.load-balancer-backend-pool",
                )
                template.add_node(
                    health_id,
                    "health-check",
                    logical_ref=workload_id,
                    attributes={
                        "port": copy.deepcopy(path.get("port")),
                        "protocol": "Http",
                        "path": {"binding": "late", "field": "healthPath"},
                    },
                    source_refs=refs,
                    rule="azure.load-balancer-probe",
                )
                template.add_node(
                    rule_id,
                    "routing-rule",
                    logical_ref=workload_id,
                    attributes={
                        "frontendPort": copy.deepcopy(path.get("port")),
                        "backendPort": copy.deepcopy(path.get("port")),
                        "protocol": "Tcp",
                    },
                    source_refs=refs,
                    rule="azure.load-balancer-rule",
                )
                for child in (backend_id, health_id, rule_id):
                    template.reference(
                        child,
                        lb_id,
                        consumer_path="loadbalancer_id",
                        rule="azure.load-balancer-components",
                    )
                template.reference(
                    rule_id,
                    frontend_name_id,
                    consumer_path="frontend_ip_configuration_name",
                    producer_attribute="value",
                    rule="azure.load-balancer-rule",
                )
                template.reference(
                    rule_id,
                    backend_id,
                    consumer_path="backend_address_pool_ids[]",
                    cardinality="many",
                    rule="azure.load-balancer-rule",
                )
                template.reference(
                    rule_id,
                    health_id,
                    consumer_path="probe_id",
                    rule="azure.load-balancer-rule",
                )
                template.reference(
                    details["compute"],
                    backend_id,
                    consumer_path="network_interface.ip_configuration.load_balancer_backend_address_pool_ids[]",
                    cardinality="many",
                    rule="azure.vmss-backend-membership",
                )
            else:
                backend_id = f"backend-service-{compute_id}-{index}"
                health_id = f"health-check-{compute_id}-{index}"
                group_id = f"backend-group-{compute_id}-{index}"
                template.add_node(
                    backend_id,
                    "backend-service",
                    logical_ref=workload_id,
                    attributes={"protocol": "TCP"},
                    source_refs=refs,
                    rule="gcp.load-balancer-backend-service",
                )
                template.add_node(
                    health_id,
                    "health-check",
                    logical_ref=workload_id,
                    attributes={
                        "port": copy.deepcopy(path.get("port")),
                        "path": {"binding": "late", "field": "healthPath"},
                    },
                    source_refs=refs,
                    rule="gcp.load-balancer-health-check",
                )
                template.embedded_block(
                    group_id,
                    owner_ref=backend_id,
                    block_path="backend",
                    name="Managed instance group backend",
                    logical_ref=workload_id,
                    source_refs=refs,
                    rule="gcp.managed-instance-group-backend",
                )
                template.reference(
                    lb_id,
                    backend_id,
                    consumer_path="backend_service",
                    rule="gcp.forwarding-rule",
                )
                template.reference(
                    backend_id,
                    health_id,
                    consumer_path="health_checks[]",
                    cardinality="many",
                    rule="gcp.load-balancer-health-check",
                )
                template.reference(
                    group_id,
                    details["compute"],
                    consumer_path="group",
                    producer_attribute="instance_group",
                    rule="gcp.managed-instance-group-backend",
                )
            interface_id = str(path.get("targetInterfaceRef") or "")
            template.binding(
                f"health-path-{workload_id}-{interface_id}",
                field=f"workloads.{workload_id}.interfaces.{interface_id}.healthPath",
                kind="healthPath",
                phase="implementation",
                source_refs=refs,
            )


def _add_storage(
    template: _Template,
    *,
    deployment_plan: dict[str, Any],
    compute_details: dict[str, dict[str, Any]],
) -> None:
    provider = template.provider
    attachment_index: dict[str, int] = {}
    for binding in deployment_plan.get("storageBindings") or []:
        compute_id = str(binding.get("computeUnitRef") or "")
        storage_id = str(binding.get("storageRef") or "storage")
        workload_id = str(binding.get("workloadRef") or "")
        index = attachment_index.get(compute_id, 0)
        attachment_index[compute_id] = index + 1
        details = compute_details.get(compute_id) or {}
        refs = binding.get("sourceRefs") or []
        if details.get("managed") and binding.get("replicaSemantics") == "perReplica":
            owner = details.get("template") or details.get("compute")
            template.embedded_block(
                f"data-disk-{storage_id}",
                owner_ref=cast(str, owner),
                block_path={
                    "aws": "block_device_mappings",
                    "azure": "data_disk",
                    "gcp": "disk",
                }[provider],
                name="Per-replica data disk",
                logical_ref=workload_id,
                attributes={
                    "capacityGiB": binding.get("capacityGiB"),
                    "deletionPolicy": binding.get("deletionPolicy"),
                    "perReplica": True,
                    "attachmentIndex": index,
                    "storageRef": storage_id,
                },
                source_refs=refs,
                rule=f"{provider}.per-replica-data-disk",
            )
            continue
        disk_id = f"data-disk-{storage_id}"
        attachment_id = f"disk-attachment-{storage_id}"
        template.add_node(
            disk_id,
            _provider_kind(provider, "disk"),
            logical_ref=workload_id,
            attributes={
                "capacityGiB": binding.get("capacityGiB"),
                "deletionPolicy": binding.get("deletionPolicy"),
                "zone": (details.get("zones") or [""])[0],
                "attachmentIndex": index,
                "storageRef": storage_id,
            },
            source_refs=refs,
            rule=f"{provider}.singleton-data-disk",
        )
        zone_producer = (
            (details.get("subnets") or [""])[0]
            if provider == "aws"
            else details.get("compute") or compute_id
        )
        template.reference(
            disk_id,
            zone_producer,
            consumer_path={
                "aws": "availability_zone",
                "azure": "zone",
                "gcp": "zone",
            }[provider],
            producer_attribute={
                "aws": "availability_zone",
                "azure": "zone",
                "gcp": "zone",
            }[provider],
            rule=f"{provider}.singleton-data-disk-zone",
        )
        template.add_node(
            attachment_id,
            _provider_kind(provider, "diskAttachment"),
            logical_ref=workload_id,
            attributes={"attachmentIndex": index, "storageRef": storage_id},
            source_refs=refs,
            rule=f"{provider}.singleton-data-disk-attachment",
        )
        template.reference(
            attachment_id,
            disk_id,
            consumer_path={
                "aws": "volume_id",
                "azure": "managed_disk_id",
                "gcp": "disk",
            }[provider],
            rule=f"{provider}.singleton-data-disk-attachment",
        )
        template.reference(
            attachment_id,
            details.get("compute") or compute_id,
            consumer_path={
                "aws": "instance_id",
                "azure": "virtual_machine_id",
                "gcp": "instance",
            }[provider],
            rule=f"{provider}.singleton-data-disk-attachment",
        )
        if provider == "gcp":
            template.reference(
                attachment_id,
                details.get("compute") or compute_id,
                consumer_path="zone",
                producer_attribute="zone",
                rule="gcp.singleton-data-disk-attachment",
            )
        if isinstance(binding.get("mountPath"), dict):
            template.binding(
                f"mount-path-{storage_id}",
                field=f"storageBindings.{storage_id}.mountPath",
                kind="mountPath",
                phase="implementation",
                source_refs=refs,
            )


def _add_internal_traffic(
    template: _Template,
    *,
    graph: dict[str, Any],
    deployment_plan: dict[str, Any],
    compute_details: dict[str, dict[str, Any]],
) -> dict[str, str]:
    endpoint_producers: dict[str, str] = {}
    placement = {
        str(item.get("workloadRef") or ""): str(item.get("computeUnitRef") or "")
        for item in deployment_plan.get("placements") or []
    }
    compute_by_id = {
        str(item.get("id") or ""): item for item in deployment_plan.get("computeUnits") or []
    }
    for connection in graph.get("connections") or []:
        connection_id = str(connection.get("id") or _digest(connection)[:12])
        source_workload = str(connection.get("sourceRef") or "")
        target_workload = str(connection.get("targetRef") or "")
        source_compute = placement.get(source_workload, "")
        target_compute = placement.get(target_workload, "")
        if not source_compute or not target_compute or source_compute == target_compute:
            continue
        refs = connection.get("sourceRefs") or []
        network_path: dict[str, Any] = next(
            (
                item
                for item in deployment_plan.get("networkPaths") or []
                if item.get("kind") == "internal"
                and str(item.get("connectionRef") or "") == connection_id
            ),
            {},
        )
        target_interface = str(connection.get("targetInterfaceRef") or "")
        endpoint_key = f"{target_workload}-{target_interface or connection_id}"
        target_port = copy.deepcopy(
            network_path.get("port") or {"binding": "late", "field": "containerPort"}
        )
        target = compute_by_id.get(target_compute) or {}
        if target.get("kind") == "managedVmGroup":
            lb_id = f"internal-load-balancer-{target_compute}-{endpoint_key}"
            kind = "load-balancer" if template.provider in {"aws", "azure"} else "forwarding-rule"
            template.add_node(
                lb_id,
                kind,
                logical_ref=target_workload,
                attributes={
                    "scheme": "internal",
                    "connectionRef": connection_id,
                    "port": target_port,
                },
                source_refs=refs,
                rule=f"{template.provider}.internal-l4-load-balancer",
            )
            endpoint_producers[connection_id] = lb_id
            target_details = compute_details[target_compute]
            if template.provider == "aws":
                backend_id = f"internal-backend-group-{target_compute}-{endpoint_key}"
                health_id = f"internal-health-check-{target_compute}-{endpoint_key}"
                listener_id = f"internal-listener-{target_compute}-{endpoint_key}"
                template.add_node(
                    backend_id,
                    "backend-group",
                    logical_ref=target_workload,
                    attributes={"port": target_port, "protocol": "TCP"},
                    source_refs=refs,
                    rule="aws.internal-load-balancer-target-group",
                )
                template.reference(
                    backend_id,
                    "network",
                    consumer_path="vpc_id",
                    rule="aws.internal-load-balancer-target-group",
                )
                template.embedded_block(
                    health_id,
                    owner_ref=backend_id,
                    block_path="health_check",
                    name="Health check",
                    logical_ref=target_workload,
                    attributes={
                        "protocol": "HTTP",
                        "path": {"binding": "late", "field": "healthPath"},
                    },
                    source_refs=refs,
                    rule="aws.internal-load-balancer-health-check",
                )
                template.add_node(
                    listener_id,
                    "listener",
                    logical_ref=target_workload,
                    attributes={"port": target_port, "protocol": "TCP"},
                    source_refs=refs,
                    rule="aws.internal-load-balancer-listener",
                )
                for subnet_id in target_details["subnets"]:
                    template.reference(
                        lb_id,
                        subnet_id,
                        consumer_path="subnets[]",
                        cardinality="many",
                        rule="aws.internal-l4-load-balancer",
                    )
                template.reference(
                    listener_id,
                    lb_id,
                    consumer_path="load_balancer_arn",
                    producer_attribute="arn",
                    rule="aws.internal-load-balancer-listener",
                )
                template.reference(
                    listener_id,
                    backend_id,
                    consumer_path="default_action.target_group_arn",
                    producer_attribute="arn",
                    rule="aws.internal-load-balancer-listener",
                )
                template.reference(
                    target_details["compute"],
                    backend_id,
                    consumer_path="target_group_arns[]",
                    producer_attribute="arn",
                    cardinality="many",
                    rule="aws.internal-autoscaling-target-registration",
                )
                # NLB 상태 검사는 source security group이 아니라 Load Balancer node의
                # 사설 IP에서 들어온다. workload 사이의 허용 규칙만으로는 이 요청이
                # 대상 VM에 닿지 않으므로, 대상 포트와 VPC 내부 범위를 명시한다.
                target_filter = target_details["filter"]
                template.nodes[target_filter].setdefault("attributes", {}).setdefault(
                    "loadBalancerHealthChecks", []
                ).append(
                    {
                        "port": target_port,
                        "targetWorkloadRef": target_workload,
                        "targetInterfaceRef": target_interface,
                    }
                )
            elif template.provider == "azure":
                frontend_id = f"internal-frontend-ip-config-{target_compute}-{endpoint_key}"
                backend_id = f"internal-backend-group-{target_compute}-{endpoint_key}"
                health_id = f"internal-health-check-{target_compute}-{endpoint_key}"
                rule_id = f"internal-routing-rule-{target_compute}-{endpoint_key}"
                template.embedded_block(
                    frontend_id,
                    owner_ref=lb_id,
                    block_path="frontend_ip_configuration",
                    name="Frontend IP configuration",
                    logical_ref=target_workload,
                    attributes={"scheme": "internal"},
                    source_refs=refs,
                    rule="azure.internal-load-balancer-frontend",
                )
                frontend_name_id = template.shared_value(
                    f"internal-frontend-name-{target_compute}-{endpoint_key}",
                    name="Load balancer frontend name",
                    value="internal",
                    source_refs=refs,
                    rule="azure.internal-load-balancer-frontend-name",
                )
                template.reference(
                    frontend_id,
                    frontend_name_id,
                    consumer_path="name",
                    producer_attribute="value",
                    rule="azure.internal-load-balancer-frontend-name",
                )
                template.reference(
                    frontend_id,
                    target_details["subnets"][0],
                    consumer_path="subnet_id",
                    rule="azure.internal-load-balancer-frontend",
                )
                template.add_node(
                    backend_id,
                    "backend-group",
                    logical_ref=target_workload,
                    source_refs=refs,
                    rule="azure.internal-load-balancer-backend-pool",
                )
                template.add_node(
                    health_id,
                    "health-check",
                    logical_ref=target_workload,
                    attributes={
                        "port": target_port,
                        "protocol": "Http",
                        "path": {"binding": "late", "field": "healthPath"},
                    },
                    source_refs=refs,
                    rule="azure.internal-load-balancer-probe",
                )
                template.add_node(
                    rule_id,
                    "routing-rule",
                    logical_ref=target_workload,
                    attributes={
                        "frontendPort": target_port,
                        "backendPort": target_port,
                        "protocol": "Tcp",
                        "scheme": "internal",
                    },
                    source_refs=refs,
                    rule="azure.internal-load-balancer-rule",
                )
                for child_id in (backend_id, health_id, rule_id):
                    template.reference(
                        child_id,
                        lb_id,
                        consumer_path="loadbalancer_id",
                        rule="azure.internal-load-balancer-components",
                    )
                template.reference(
                    rule_id,
                    frontend_name_id,
                    consumer_path="frontend_ip_configuration_name",
                    producer_attribute="value",
                    rule="azure.internal-load-balancer-rule",
                )
                template.reference(
                    rule_id,
                    backend_id,
                    consumer_path="backend_address_pool_ids[]",
                    cardinality="many",
                    rule="azure.internal-load-balancer-rule",
                )
                template.reference(
                    rule_id,
                    health_id,
                    consumer_path="probe_id",
                    rule="azure.internal-load-balancer-rule",
                )
                template.reference(
                    target_details["compute"],
                    backend_id,
                    consumer_path="network_interface.ip_configuration.load_balancer_backend_address_pool_ids[]",
                    cardinality="many",
                    rule="azure.internal-vmss-backend-membership",
                )
            else:
                backend_id = f"internal-backend-service-{target_compute}-{endpoint_key}"
                health_id = f"internal-health-check-{target_compute}-{endpoint_key}"
                group_id = f"internal-backend-group-{target_compute}-{endpoint_key}"
                template.add_node(
                    backend_id,
                    "backend-service",
                    logical_ref=target_workload,
                    attributes={
                        "port": target_port,
                        "protocol": "TCP",
                        "scheme": "internal",
                    },
                    source_refs=refs,
                    rule="gcp.internal-load-balancer-backend-service",
                )
                template.add_node(
                    health_id,
                    "health-check",
                    logical_ref=target_workload,
                    attributes={
                        "port": target_port,
                        "path": {"binding": "late", "field": "healthPath"},
                    },
                    source_refs=refs,
                    rule="gcp.internal-load-balancer-health-check",
                )
                template.embedded_block(
                    group_id,
                    owner_ref=backend_id,
                    block_path="backend",
                    name="Managed instance group backend",
                    logical_ref=target_workload,
                    source_refs=refs,
                    rule="gcp.internal-managed-instance-group-backend",
                )
                template.reference(
                    lb_id,
                    backend_id,
                    consumer_path="backend_service",
                    rule="gcp.internal-forwarding-rule",
                )
                template.reference(
                    lb_id,
                    target_details["subnets"][0],
                    consumer_path="subnetwork",
                    rule="gcp.internal-forwarding-rule",
                )
                template.reference(
                    lb_id,
                    "network",
                    consumer_path="network",
                    rule="gcp.internal-forwarding-rule",
                )
                template.reference(
                    backend_id,
                    health_id,
                    consumer_path="health_checks[]",
                    cardinality="many",
                    rule="gcp.internal-load-balancer-health-check",
                )
                template.reference(
                    group_id,
                    target_details["compute"],
                    consumer_path="group",
                    producer_attribute="instance_group",
                    rule="gcp.internal-managed-instance-group-backend",
                )
            template.binding(
                f"internal-health-path-{target_workload}-{target_interface}",
                field=(f"workloads.{target_workload}.interfaces.{target_interface}.healthPath"),
                kind="healthPath",
                phase="implementation",
                source_refs=refs,
            )
        if template.provider == "azure":
            target_filter = compute_details[target_compute]["filter"]
            target_node = template.nodes[target_filter]
            target_node.setdefault("attributes", {}).setdefault("internalRules", []).append(
                {
                    "connectionRef": connection_id,
                    "port": target_port,
                    "protocol": connection.get("protocol"),
                }
            )
            template.reference(
                target_filter,
                compute_details[source_compute]["subnets"][0],
                consumer_path=f"security_rule[{connection_id}].source_address_prefix",
                producer_attribute="address_prefixes[0]",
                source_refs=refs,
                rule="azure.internal-workload-traffic",
            )
            continue
        filter_id = f"internal-filter-{connection_id}"
        filter_kind = "security-group" if template.provider == "aws" else "firewall"
        template.add_node(
            filter_id,
            filter_kind,
            logical_ref=target_workload,
            attributes={
                "public": False,
                "protocol": connection.get("protocol"),
                "sourceComputeRef": source_compute,
                "targetComputeRef": target_compute,
                # Every source SG / source tag rule is scoped to the target
                # interface selected by this connection.  Rendering from this
                # explicit port list prevents a later generic fallback from
                # opening 1-65535 for private traffic.
                "allowedPorts": [target_port],
            },
            source_refs=refs,
            rule=f"{template.provider}.internal-workload-traffic",
        )
        template.reference(
            filter_id,
            "network",
            consumer_path="vpc_id" if template.provider == "aws" else "network",
            rule=f"{template.provider}.internal-workload-traffic",
        )
        target_consumer = (
            compute_details[target_compute].get("template")
            or compute_details[target_compute]["compute"]
        )
        if template.provider == "aws":
            template.reference(
                filter_id,
                compute_details[source_compute]["filter"],
                consumer_path="ingress.security_groups[]",
                cardinality="many",
                rule="aws.internal-workload-traffic",
            )
            template.reference(
                target_consumer,
                filter_id,
                consumer_path="vpc_security_group_ids[]",
                cardinality="many",
                rule="aws.internal-workload-traffic",
            )
        else:
            target_tag = template.shared_value(
                f"network-tag-{filter_id}",
                name="Internal target network tag",
                value=filter_id,
                source_refs=refs,
                rule="gcp.internal-workload-network-tag",
            )
            template.reference(
                target_consumer,
                target_tag,
                consumer_path="tags[]",
                producer_attribute="value",
                cardinality="many",
                rule="gcp.internal-workload-network-tag",
            )
            template.reference(
                filter_id,
                target_tag,
                consumer_path="target_tags[]",
                producer_attribute="value",
                cardinality="many",
                rule="gcp.internal-workload-network-tag",
            )
            template.reference(
                filter_id,
                f"network-tag-{source_compute}",
                consumer_path="source_tags[]",
                producer_attribute="value",
                cardinality="many",
                rule="gcp.internal-workload-network-tag",
            )

    return endpoint_producers


def _complete_azure_resource_groups(template: _Template) -> None:
    """Attach every Azure resource with an RG field to the shared RG.

    The provider template is assembled by capability modules (delivery,
    network, ingress, storage).  Closing this cross-cutting Azure prerequisite
    here keeps those modules composable without making any of them infer a
    workload or topology.
    """

    if template.provider != "azure":
        return
    requires_resource_group = {
        "azurerm_container_registry",
        "azurerm_user_assigned_identity",
        "azurerm_virtual_network",
        "azurerm_network_interface",
        "azurerm_network_security_group",
        "azurerm_linux_virtual_machine",
        "azurerm_linux_virtual_machine_scale_set",
        "azurerm_public_ip",
        "azurerm_lb",
        "azurerm_nat_gateway",
        "azurerm_managed_disk",
    }
    for node_id, node in list(template.nodes.items()):
        if node_id == "resource-group" or node.get("handling") != "create":
            continue
        if requires_resource_group.intersection(node.get("terraformTypes") or []):
            template.reference(
                node_id,
                "resource-group",
                consumer_path="resource_group_name",
                producer_attribute="name",
                rule="azure.resource-group-closure",
            )


def build_complete_provider_template(
    deployment_plan: dict[str, Any],
    graph: dict[str, Any],
    *,
    provider: str,
    region: str,
) -> dict[str, Any]:
    """DeploymentPlan을 완전한 provider ResourcePlan으로 투영한다.

    Args:
        deployment_plan: 검증된 provider-neutral DeploymentPlan이다.
        graph: 해당 계획의 정규화 WorkloadGraph다.
        provider: 명시적으로 선택된 aws, azure 또는 gcp 식별자다.
        region: 명시적으로 선택된 provider region이다.

    Returns:
        기존 node·reference·binding·derivation 순서의 ResourcePlan이다.

    Notes:
        생성 결과를 독립 validator로 검사한 뒤 동일 canonical digest를 기록하며 LLM을
        호출하지 않는다.
    """

    normalized_provider = provider.strip().lower()
    if normalized_provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported provider: {provider or '<empty>'}")
    template = _Template(normalized_provider, region)
    placements = {
        str(item.get("workloadRef") or ""): str(item.get("computeUnitRef") or "")
        for item in deployment_plan.get("placements") or []
    }
    registries, identities = _add_registry_delivery(template, graph=graph, placements=placements)
    _add_secret_delivery(
        template,
        graph=graph,
        placements=placements,
        identity_by_compute=identities,
    )
    compute_details = _add_network_and_compute(
        template, deployment_plan=deployment_plan, identity_by_compute=identities
    )
    _add_ingress(template, compute_details=compute_details)
    _add_storage(template, deployment_plan=deployment_plan, compute_details=compute_details)
    endpoint_producers = _add_internal_traffic(
        template,
        graph=graph,
        deployment_plan=deployment_plan,
        compute_details=compute_details,
    )
    for binding in deployment_plan.get("runtimeBindings") or []:
        if binding.get("kind") != "endpointEnvironment":
            continue
        connection_ref = str(binding.get("connectionRef") or "")
        strategy = str(binding.get("strategy") or "")
        environment_name = str(binding.get("environmentName") or "")
        source_compute = placements.get(str(binding.get("workloadRef") or ""), "")
        owner = (compute_details.get(source_compute) or {}).get("template") or (
            compute_details.get(source_compute) or {}
        ).get("compute")
        if strategy == "internalLoadBalancer" and owner:
            producer = endpoint_producers.get(connection_ref)
            if producer:
                template.reference(
                    owner,
                    producer,
                    consumer_path=f"bootstrap.environment.{environment_name}",
                    producer_attribute={
                        "aws": "dns_name",
                        "azure": "frontend_ip_configuration[0].private_ip_address",
                        "gcp": "ip_address",
                    }[normalized_provider],
                    source_refs=binding.get("sourceRefs"),
                    rule=f"{normalized_provider}.runtime-endpoint-injection",
                )
        elif strategy == "externalInput":
            template.binding(
                f"external-endpoint-{_slug(connection_ref)}",
                field=f"connections.{connection_ref}.endpoint",
                kind="externalEndpoint",
                phase="deployment",
                source_refs=binding.get("sourceRefs"),
            )
    _complete_azure_resource_groups(template)
    runtime_units = _runtime_units(
        graph,
        deployment_plan,
        registries,
        compute_details=compute_details,
        endpoint_producers=endpoint_producers,
    )
    issues = copy.deepcopy(deployment_plan.get("issues") or [])
    plan = {
        "schemaVersion": RESOURCE_PLAN_SCHEMA,
        "providerTemplateCatalog": PROVIDER_TEMPLATE_CATALOG,
        "provider": normalized_provider,
        "region": region,
        "deploymentPlanDigest": deployment_plan.get("structureDigest"),
        "locationBinding": copy.deepcopy(deployment_plan.get("locationPlan") or {}),
        "nodes": list(template.nodes.values()),
        "references": list(template.references.values()),
        "sharedValues": list(template.shared_values.values()),
        "embeddedBlocks": list(template.embedded_blocks.values()),
        "workloads": copy.deepcopy(graph.get("workloads") or []),
        "placements": copy.deepcopy(deployment_plan.get("placements") or []),
        "storageBindings": copy.deepcopy(deployment_plan.get("storageBindings") or []),
        "networkPaths": copy.deepcopy(deployment_plan.get("networkPaths") or []),
        "runtimeBindings": copy.deepcopy(deployment_plan.get("runtimeBindings") or []),
        "runtimeUnits": runtime_units,
        "bindingSlots": list(template.bindings.values()),
        "issues": issues,
        "unresolved": [item for item in issues if item.get("classification") in BLOCKING_CLASSES],
    }
    validate_complete_provider_template(plan)
    plan["structureDigest"] = provider_template_structure_digest(plan)
    return plan


def provider_template_structure_digest(plan: dict[str, Any]) -> str:
    """Provider ResourcePlan의 구조적 필드 digest를 계산한다.

    Args:
        plan: 생성 또는 복원된 provider ResourcePlan이다.

    Returns:
        runtime 값과 진단을 제외한 기존 SHA-256 structure digest다.

    Notes:
        node와 reference 등 목록 순서는 그대로 digest 입력에 포함한다.
    """

    structural = {
        "schemaVersion": plan.get("schemaVersion"),
        "providerTemplateCatalog": plan.get("providerTemplateCatalog"),
        "provider": plan.get("provider"),
        "nodes": [
            {
                "id": node.get("id"),
                "providerKind": node.get("providerKind"),
                "providerPrimitiveKind": node.get("providerPrimitiveKind"),
                "entityClass": node.get("entityClass"),
                "handling": node.get("handling"),
                "terraformTypes": node.get("terraformTypes"),
                "ownerRef": node.get("ownerRef"),
            }
            for node in plan.get("nodes") or []
        ],
        "references": [
            {
                "consumerRef": reference.get("consumerRef"),
                "consumerPath": reference.get("consumerPath"),
                "producerRef": reference.get("producerRef"),
                "producerAttribute": reference.get("producerAttribute"),
                "cardinality": reference.get("cardinality"),
            }
            for reference in plan.get("references") or []
        ],
        "sharedValues": [
            {
                "id": value.get("id"),
                "valueType": value.get("valueType"),
            }
            for value in plan.get("sharedValues") or []
        ],
        "embeddedBlocks": [
            {
                "id": block.get("id"),
                "ownerRef": block.get("ownerRef"),
                "blockPath": block.get("blockPath"),
            }
            for block in plan.get("embeddedBlocks") or []
        ],
        "runtimeUnits": [
            {
                "id": unit.get("id"),
                "computeUnitRef": unit.get("computeUnitRef"),
                "workloads": [item.get("workloadRef") for item in unit.get("containers") or []],
            }
            for unit in plan.get("runtimeUnits") or []
        ],
        "runtimeBindings": [
            {
                "id": binding.get("id"),
                "kind": binding.get("kind"),
                "workloadRef": binding.get("workloadRef"),
                "configurationRef": binding.get("configurationRef"),
                "connectionRef": binding.get("connectionRef"),
                "storageRef": binding.get("storageRef"),
                "environmentName": binding.get("environmentName"),
                "projection": binding.get("projection"),
                "strategy": binding.get("strategy"),
                "mountPath": binding.get("mountPath"),
            }
            for binding in plan.get("runtimeBindings") or []
        ],
        "bindingSlots": [
            {
                "id": item.get("id"),
                "field": item.get("field"),
                "kind": item.get("kind"),
                "phase": item.get("phase"),
                "structural": item.get("structural"),
            }
            for item in plan.get("bindingSlots") or []
        ],
    }
    return _digest(structural)
