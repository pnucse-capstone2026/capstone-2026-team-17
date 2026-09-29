"""Project an existing ResourcePlan into deterministic local pricing inputs.

This adapter is deliberately read-only: provider templates decide the topology;
pricing only describes meters for resources that template already created.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal

from app.cloudkb.costkb.dataset import load_specs
from app.cloudkb.costkb.deployment_cost import PricingInventory, UsageScenario
from app.cloudkb.costkb.resource_pricing import no_separate_meter_primitive_kinds

_Provider = Literal["aws", "azure", "gcp"]
_COMPUTE = frozenset({"compute-instance", "compute-group"})
_STORAGE = frozenset({"disk"})
_LOAD_BALANCER = frozenset({"load-balancer", "forwarding-rule"})
_NAT = frozenset({"nat-gateway", "cloud-nat"})
_PUBLIC_IP = frozenset({"public-ip", "nat-public-ip"})
_SECRET = frozenset({"secret-access-binding", "state-secret-access-binding"})
_AZURE_DISK_TIERS = (
    (32, "S4"),
    (64, "S6"),
    (128, "S10"),
    (256, "S15"),
    (512, "S20"),
    (1024, "S30"),
    (2048, "S40"),
    (4096, "S50"),
    (8192, "S60"),
    (16384, "S70"),
    (32767, "S80"),
)
_GCP_SHARED_CORE = frozenset({"f1-micro", "g1-small"})
_GCP_PUBLIC_L4 = "project-policy:gcp.public-l4-load-balancer"
_GCP_INTERNAL_L4 = "project-policy:gcp.internal-l4-load-balancer"
_GCP_DIRECT_IP = "project-policy:gcp.direct-public-address"


@dataclass(frozen=True)
class _Node:
    identifier: str
    kind: str
    handling: str
    attributes: Mapping[str, Any]
    logical_ref: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class _Block:
    identifier: str
    owner_ref: str
    attributes: Mapping[str, Any]
    source_refs: tuple[str, ...]


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _refs(prefix: str, identifier: str, values: object) -> tuple[str, ...]:
    items = values if isinstance(values, Iterable) and not isinstance(values, (str, bytes)) else ()
    return tuple(sorted({f"{prefix}:{identifier}", *(str(item) for item in items if str(item))}))


def _nodes(plan: Mapping[str, Any]) -> list[_Node]:
    result: list[_Node] = []
    for raw in plan.get("nodes") or []:
        item = _mapping(raw)
        identifier, kind = str(item.get("id") or ""), str(item.get("providerPrimitiveKind") or "")
        if identifier and kind:
            result.append(
                _Node(
                    identifier,
                    kind,
                    str(item.get("handling") or "create"),
                    _mapping(item.get("attributes")),
                    str(item.get("logicalRef") or ""),
                    _refs("provider-node", identifier, item.get("sourceRefs")),
                )
            )
    return result


def _blocks(plan: Mapping[str, Any]) -> list[_Block]:
    result: list[_Block] = []
    for raw in plan.get("embeddedBlocks") or []:
        item = _mapping(raw)
        identifier, owner = str(item.get("id") or ""), str(item.get("ownerRef") or "")
        if identifier and owner:
            result.append(
                _Block(
                    identifier,
                    owner,
                    _mapping(item.get("attributes")),
                    _refs("provider-embedded", identifier, item.get("sourceRefs")),
                )
            )
    return result


def _positive_int(value: object, fallback: int = 1) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _positive_decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _azure_disk_sku(capacity: object) -> str | None:
    requested = _positive_decimal(capacity)
    if requested is None:
        return None
    return next((sku for maximum, sku in _AZURE_DISK_TIERS if requested <= maximum), None)


@lru_cache(maxsize=1)
def _gcp_specs() -> dict[tuple[str, str], Mapping[str, Any] | None]:
    matches: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in load_specs():
        if (
            row.get("provider") == "gcp"
            and isinstance(row.get("region"), str)
            and isinstance(row.get("specName"), str)
        ):
            matches.setdefault((row["region"].lower(), row["specName"].lower()), []).append(row)
    return {key: rows[0] if len(rows) == 1 else None for key, rows in matches.items()}


def _selected_compute(compute: Mapping[str, Any]) -> tuple[str | None, int]:
    sku = compute.get("selectedVmSku") or compute.get("vmSku")
    return (
        sku.strip() if isinstance(sku, str) and sku.strip() else None,
        _positive_int(
            compute.get("selectedReplicaCount"), _positive_int(compute.get("replicaCount"))
        ),
    )


def _compute_values(provider: _Provider, compute: Mapping[str, Any], region: str) -> dict[str, Any]:
    sku, replicas = _selected_compute(compute)
    values: dict[str, Any] = {
        "instance_count": replicas,
        "region": region,
        "operating_system": "linux",
    }
    if provider == "aws":
        values.update(
            running_seconds=Decimal(730 * 3600),
            instance_type=sku,
            tenancy="shared",
            capacity_status="used",
            unit="Hrs",
        )
    elif provider == "azure":
        values.update(running_hours=Decimal(730), vm_size=sku)
    else:
        spec = _gcp_specs().get((region.lower(), sku.lower())) if sku else None
        values.update(
            running_seconds=Decimal(730 * 3600),
            machine_type=sku,
            machine_family=sku.split("-", 1)[0] if sku and spec else None,
            pricing_model=("shared-core-instance" if sku in _GCP_SHARED_CORE else "resource-based")
            if spec
            else None,
            vcpu_count=spec.get("vCPU") if spec else None,
            memory_gib=spec.get("memGiB") if spec else None,
            accelerator_count=spec.get("acceleratorCount") if spec else None,
            accelerator_memory_gib=spec.get("acceleratorMemoryGB") if spec else None,
            rate_dimensions={
                "vcpu_runtime_rate": {"unit": "vcpu-hour"},
                "memory_gib_runtime_rate": {"unit": "gib-hour"},
                "shared_core_instance_runtime_rate": {"unit": "instance-hour"},
            },
        )
    return values


def _storage_values(
    provider: _Provider, attributes: Mapping[str, Any], count: int, region: str
) -> dict[str, Any]:
    capacity = attributes.get("capacityGiB")
    if provider == "aws":
        return {
            "region": region,
            "volume_count": count,
            "capacity_gib": capacity,
            "provisioned_seconds": Decimal(730 * 3600),
            "volume_type": "gp3",
            "usage_type": "EBS:VolumeUsage.gp3",
            "unit": "GB-Mo",
        }
    if provider == "azure":
        return {
            "region": region,
            "disk_count": count,
            "requested_capacity_gib": capacity,
            "provisioned_hours": Decimal(730),
            "provisioned_seconds": Decimal(730 * 3600),
            "disk_sku": _azure_disk_sku(capacity),
        }
    return {
        "region": region,
        "disk_count": count,
        "capacity_gib": capacity,
        "provisioned_seconds": Decimal(730 * 3600),
        "disk_type": "pd-balanced",
        "disk_scope": "zonal",
        "unit": "gib-month",
    }


def _root_values(
    provider: _Provider,
    node: _Node,
    region: str,
    *,
    billable_rule_count: int | None = None,
    nat_vm_count: int | None = None,
    forwarding_rule_count: int | None = None,
) -> dict[str, Any]:
    values: dict[str, Any] = {"region": region, "sourceRefs": list(node.source_refs)}
    if node.kind in _LOAD_BALANCER:
        values["load_balancer_hours"] = Decimal(730)
        if provider == "aws":
            interfaces = node.attributes.get("publicInterfaces") or []
            first = _mapping(interfaces[0]) if isinstance(interfaces, list) and interfaces else {}
            values.update(protocol=str(first.get("protocol") or "").lower(), public_ipv4_count=1)
            if "project-policy:aws.public-l4-load-balancer" in node.source_refs:
                values.update(load_balancer_type="network", unit="Hrs")
        elif provider == "azure":
            values.update(tier="Standard", billable_rule_count=billable_rule_count)
        else:
            values.update(
                forwarding_rule_count=forwarding_rule_count,
                forwarding_rule_hours=Decimal(730),
                load_balancing_scheme="external",
                rate_dimensions={
                    "first_five_forwarding_rules_hour_rate": {"unit": "rule-hour"},
                    "additional_forwarding_rule_hour_rate": {"unit": "rule-hour"},
                    "inbound_processed_gib_rate": {"unit": "gib"},
                    "outbound_processed_gib_rate": {"unit": "gib"},
                },
            )
    elif node.kind in _NAT:
        values["gateway_hours"] = Decimal(730)
        if provider == "aws" and "project-policy:aws.nat-gateway" in node.source_refs:
            values.update(operation="NatGateway", usage_type="NatGateway-Hours", unit="Hrs")
        elif provider == "azure":
            values["sku"] = "Standard"
        elif provider == "gcp":
            values.update(
                assigned_vm_count=nat_vm_count,
                external_ip_count=None,
                nat_allocation_mode="auto-only",
                rate_dimensions={
                    "per_vm_gateway_hour_rate": {"unit": "vm-hour"},
                    "capped_gateway_hour_rate": {"unit": "gateway-hour"},
                    "processed_gib_rate": {"unit": "gib"},
                    "nat_external_ip_hour_rate": {"unit": "address-hour"},
                },
            )
    elif node.kind in _PUBLIC_IP:
        values.update(address_count=1, allocated_hours=Decimal(730))
        if provider == "aws":
            values.update(address_state="in-use", unit="Hrs")
        elif provider == "azure":
            values.update(sku="Standard", allocation_method="Static")
        else:
            values.update(
                attachment_class="standard-vm", network_tier="premium", unit="address-hour"
            )
    elif node.kind == "app-registry":
        if provider == "aws":
            values.update(
                repository_region=region, destination_region=None, repository_visibility="private"
            )
        elif provider == "azure":
            values.update(registry_days=Decimal(31), sku="Basic")
        else:
            values.update(repository_location=region, destination_location=None)
    return values


def _azure_rule_counts(nodes: Mapping[str, _Node], references: object) -> Counter[str]:
    counts: Counter[str] = Counter()
    for raw in references if isinstance(references, list) else []:
        item, consumer, producer = _mapping(raw), None, ""
        consumer = nodes.get(str(item.get("consumerRef") or ""))
        producer = str(item.get("producerRef") or "")
        if consumer and consumer.kind == "routing-rule" and producer in nodes:
            counts[producer] += 1
    return counts


def _gcp_forwarding_groups(nodes: Iterable[_Node]) -> tuple[dict[str, int], set[str]]:
    grouped: dict[tuple[str, str], list[_Node]] = {}
    for node in nodes:
        if node.kind != "forwarding-rule":
            continue
        scheme = (
            "external"
            if _GCP_PUBLIC_L4 in node.source_refs
            else "internal"
            if _GCP_INTERNAL_L4 in node.source_refs
            else ""
        )
        if scheme:
            grouped.setdefault((node.logical_ref, scheme), []).append(node)
    counts: dict[str, int] = {}
    leaders: set[str] = set()
    for (_, scheme), members in grouped.items():
        if scheme == "external":
            leader = min(member.identifier for member in members)
            leaders.add(leader)
            counts[leader] = len(members)
    return counts, leaders


def _gcp_nat_vm_count(plan: Mapping[str, Any]) -> int | None:
    computes = {
        str(item.get("id") or ""): _mapping(item)
        for item in plan.get("computeUnits") or []
        if isinstance(item, Mapping)
    }
    ids = {
        str(path.get("computeUnitRef") or "")
        for path in plan.get("networkPaths") or []
        if isinstance(path, Mapping) and path.get("kind") == "natEgress"
    }
    return (
        sum(
            _selected_compute(computes[identifier])[1]
            for identifier in ids
            if identifier in computes
        )
        if ids
        else None
    )


def _external_databases(graph: Mapping[str, Any]) -> list[UsageScenario]:
    scenarios: list[UsageScenario] = []
    for raw in graph.get("externalDependencies") or []:
        dependency = _mapping(raw)
        identifier = str(dependency.get("id") or "")
        description = " ".join(
            str(dependency.get(key) or "") for key in ("id", "name", "kind", "engine")
        ).casefold()
        if identifier and any(
            marker in description for marker in ("database", "postgres", "mysql", "mongo", " db")
        ):
            scenarios.append(
                UsageScenario(
                    primitive_kind="external-managed-database",
                    label=str(dependency.get("name") or identifier),
                    values={
                        "sourceRefs": list(
                            _refs("external-dependency", identifier, dependency.get("sourceRefs"))
                        ),
                        "reason": "Managed external database pricing is outside the supported VM ResourcePlan corpus.",
                    },
                )
            )
    return scenarios


def explicit_pricing_exclusion_reason(provider: str, node: Mapping[str, Any]) -> str | None:
    """Return the documented reason a generated node has no standalone meter.

    Keep this deliberately narrow.  The coverage test uses it as a tripwire:
    new provider primitives must gain a pricing root or a reviewed exclusion,
    rather than disappearing because the adapter has no matching branch.
    """

    source_refs = {str(value) for value in node.get("sourceRefs") or []}
    primitive = str(node.get("providerPrimitiveKind") or "")
    if (
        provider == "gcp"
        and any(ref.startswith("project-policy:gcp.internal-") for ref in source_refs)
        and primitive in {"forwarding-rule", "backend-service", "health-check"}
    ):
        return "The current GCP pricing corpus prices only the generated external L4 root."
    return None


def build_pricing_inventory(
    resource_plan: Mapping[str, Any],
    deployment_plan: Mapping[str, Any],
    workload_graph: Mapping[str, Any],
    *,
    monthly_budget_usd: Decimal | None = None,
) -> PricingInventory:
    """Translate only existing generated resources into pricing scenarios."""
    provider = str(resource_plan.get("provider") or "").lower()
    if provider not in {"aws", "azure", "gcp"}:
        raise ValueError("ResourcePlan provider must be aws, azure, or gcp.")
    typed: _Provider = provider  # type: ignore[assignment]
    region = str(resource_plan.get("region") or "")
    computes = {
        str(item.get("id") or ""): _mapping(item)
        for item in deployment_plan.get("computeUnits") or []
        if isinstance(item, Mapping)
    }
    nodes = _nodes(resource_plan)
    by_id = {node.identifier: node for node in nodes}
    azure_rules = _azure_rule_counts(by_id, resource_plan.get("references"))
    gcp_counts, gcp_leaders = _gcp_forwarding_groups(nodes)
    nat_vms = _gcp_nat_vm_count(deployment_plan)
    scenarios: list[UsageScenario] = []
    for node in nodes:
        if node.handling != "create" or node.kind in no_separate_meter_primitive_kinds(typed):
            continue
        compute = computes.get(node.logical_ref)
        if node.kind in _COMPUTE and compute is not None:
            values = _compute_values(typed, compute, region)
            values["sourceRefs"] = list(node.source_refs)
            scenarios.append(
                UsageScenario(primitive_kind=node.kind, label=node.identifier, values=values)
            )
            if typed == "gcp" and _positive_int(values.get("accelerator_count"), 0) > 0:
                scenarios.append(
                    UsageScenario(
                        primitive_kind="accelerator",
                        label=f"{node.identifier}-accelerator",
                        values={
                            "machine_type": values.get("machine_type"),
                            "accelerator_count": values.get("accelerator_count"),
                            "accelerator_memory_gib": values.get("accelerator_memory_gib"),
                            "sourceRefs": list(node.source_refs),
                            "reason": "Accelerator pricing has no supported generated-resource rule.",
                        },
                    )
                )
        elif node.kind in _STORAGE:
            values = _storage_values(typed, node.attributes, 1, region)
            values["sourceRefs"] = list(node.source_refs)
            scenarios.append(
                UsageScenario(primitive_kind="disk", label=node.identifier, values=values)
            )
        elif node.kind in (_LOAD_BALANCER | _NAT | _PUBLIC_IP | {"app-registry"} | _SECRET):
            if (
                typed == "gcp"
                and node.kind == "forwarding-rule"
                and node.identifier not in gcp_leaders
            ):
                continue
            if (
                typed == "gcp"
                and node.kind == "public-ip"
                and _GCP_DIRECT_IP not in node.source_refs
            ):
                continue
            scenarios.append(
                UsageScenario(
                    primitive_kind=node.kind,
                    label=node.identifier,
                    values=_root_values(
                        typed,
                        node,
                        region,
                        billable_rule_count=azure_rules.get(node.identifier),
                        nat_vm_count=nat_vms,
                        forwarding_rule_count=gcp_counts.get(node.identifier),
                    ),
                )
            )
    for block in _blocks(resource_plan):
        if block.attributes.get("perReplica"):
            owner = by_id.get(block.owner_ref)
            compute = computes.get(owner.logical_ref) if owner else None
            values = _storage_values(
                typed, block.attributes, _selected_compute(compute)[1] if compute else 1, region
            )
            values["sourceRefs"] = list(block.source_refs)
            scenarios.append(
                UsageScenario(primitive_kind="disk", label=block.identifier, values=values)
            )
    for raw in deployment_plan.get("networkPaths") or []:
        path = _mapping(raw)
        if path.get("kind") == "outbound":
            identifier = str(path.get("id") or "outbound")
            scenarios.append(
                UsageScenario(
                    primitive_kind="internet-egress",
                    label=identifier,
                    values={
                        "region": region,
                        "sourceRefs": list(
                            _refs("network-path", identifier, path.get("sourceRefs"))
                        ),
                    },
                )
            )
    scenarios.extend(_external_databases(workload_graph))
    return PricingInventory(
        provider=typed, scenarios=scenarios, monthly_budget_usd=monthly_budget_usd
    )


__all__ = ["build_pricing_inventory", "explicit_pricing_exclusion_reason"]
