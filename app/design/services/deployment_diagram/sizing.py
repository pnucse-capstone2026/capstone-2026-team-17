"""선택된 deployment target의 VM 용량·compute 가격 가이드를 만든다."""

from __future__ import annotations

import copy
import math
import os
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.cloudkb.costkb.dataset import filter_specs, find_by_name, load_dataset
from app.cloudkb.costkb.deployment_cost import (
    DeploymentCostQuote,
    RetailRateResolver,
    estimate_deployment_cost,
)
from app.cloudkb.costkb.free_tier import (
    preferred_vm_free_tier_skus,
    vm_free_tier_notice,
    vm_free_tier_status,
)
from app.cloudkb.costkb.retail_rates import (
    build_retail_rate_resolver,
    load_retail_rate_snapshot,
)
from app.cloudkb.perfkb.agent_api import recommendation_profile
from app.design.services.deployment_diagram.bundle import select_deployment_target
from app.design.services.deployment_diagram.costing import build_pricing_inventory
from app.design.services.deployment_diagram.planner import (
    build_deployment_plan,
    build_provider_resource_plan,
)

HOURS_PER_MONTH = 730
_BUNDLED_RETAIL_RATE_SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[3]
    / "cloudkb"
    / "costkb"
    / "retail_rates_all_providers_20260915.json"
)


class ComputeSelection(BaseModel):
    """한 provider compute group에 적용할 사용자 선택이다."""

    compute_unit_id: str = Field(alias="computeUnitId", min_length=1)
    sku: str = Field(min_length=1)
    replica_count: int = Field(alias="replicaCount", ge=1)
    replication_confirmed: bool = Field(default=False, alias="replicationConfirmed")


class CapacityOverride(BaseModel):
    """Late sizing minimums for one existing compute unit.

    These values tune SKU guidance only.  They must not become workload-graph
    requirements, because the graph remains the derived requirements artifact.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    compute_unit_id: str = Field(alias="computeUnitId", min_length=1)
    min_vcpu: float = Field(alias="minVCpu", gt=0)
    min_memory_gib: float = Field(alias="minMemoryGiB", gt=0)

    @field_validator("min_vcpu", "min_memory_gib", mode="before")
    @classmethod
    def _finite_number(cls, value: object) -> object:
        if isinstance(value, bool):
            raise TypeError("capacity overrides must be numbers")
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise ValueError("capacity overrides must be numbers") from error
        if not math.isfinite(number):
            raise ValueError("capacity overrides must be finite")
        return value


def _parse_capacity_overrides(
    deployment_plan: dict[str, Any], capacity_overrides: list[dict[str, Any] | CapacityOverride]
) -> list[CapacityOverride]:
    """Validate every late sizing override before a projection is changed."""

    try:
        parsed = [
            item if isinstance(item, CapacityOverride) else CapacityOverride.model_validate(item)
            for item in capacity_overrides
        ]
    except ValidationError as error:
        raise ValueError(f"Invalid capacity override: {error}") from error
    compute_ids = {
        str(item.get("id") or "")
        for item in deployment_plan.get("computeUnits") or []
        if isinstance(item, dict)
    }
    override_ids = [item.compute_unit_id for item in parsed]
    if len(override_ids) != len(set(override_ids)):
        raise ValueError("Each compute unit can have only one capacity override.")
    for override in parsed:
        if override.compute_unit_id not in compute_ids:
            raise ValueError(f"Unknown compute unit: {override.compute_unit_id}.")
    return parsed


def _apply_parsed_capacity_overrides(
    deployment_plan: dict[str, Any], overrides: list[CapacityOverride]
) -> dict[str, Any]:
    """Return a plan copy with late minimums, leaving its topology digest intact."""

    result = copy.deepcopy(deployment_plan)
    compute_by_id = {
        str(item.get("id") or ""): item
        for item in result.get("computeUnits") or []
        if isinstance(item, dict)
    }
    for override in overrides:
        requirements = dict(compute_by_id[override.compute_unit_id].get("resourceRequirements") or {})
        requirements.update(
            {
                "minVCpu": override.min_vcpu,
                "minMemoryGiB": override.min_memory_gib,
            }
        )
        compute_by_id[override.compute_unit_id]["resourceRequirements"] = requirements
    return result


def apply_capacity_overrides(
    deployment_plan: dict[str, Any],
    capacity_overrides: list[dict[str, Any] | CapacityOverride] | None,
) -> tuple[dict[str, Any], list[CapacityOverride]]:
    """Apply validated late sizing input to a copied deployment plan.

    ``structureDigest`` intentionally remains the pre-capacity topology digest.  It
    is the preview stale guard, not a digest of price/SKU tuning input.
    """

    overrides = _parse_capacity_overrides(deployment_plan, capacity_overrides or [])
    return _apply_parsed_capacity_overrides(deployment_plan, overrides), overrides


def _catalog_candidates(
    *, provider: str, region: str, min_vcpu: float, min_memory_gib: float, limit: int
) -> list[dict[str, Any]]:
    """catalog의 정확한 provider·region 후보만 좁혀 반환한다."""

    priced_rows = filter_specs(
        vcpu_min=max(0, math.ceil(min_vcpu)),
        mem_min_gib=max(0, min_memory_gib),
        provider=provider,
        region=region,
        limit=max(1, limit * 4),
        fold_regions=False,
    )
    priced_rows = [
        dict(row)
        for row in priced_rows
        if str(row.get("provider") or "").lower() == provider
        and str(row.get("region") or "").lower() == region.lower()
    ]
    free_tier_rows = [
        dict(row)
        for sku in preferred_vm_free_tier_skus(provider=provider, region=region)
        for row in find_by_name(sku, provider=provider)
        if str(row.get("region") or "").lower() == region.lower()
        and float(row.get("vCPU") or 0) >= min_vcpu
        and float(row.get("memGiB") or 0) >= min_memory_gib
        and str(row.get("architecture") or "x86_64").lower() == "x86_64"
    ]
    free_tier_rows.sort(
        key=lambda row: (
            row.get("hourlyUSD") is None,
            float(row.get("hourlyUSD") or 0),
            str(row.get("specName") or ""),
        )
    )
    combined: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in [*free_tier_rows, *priced_rows]:
        name = str(row.get("specName") or "").lower()
        if not name or name in seen:
            continue
        seen.add(name)
        combined.append(row)
    return combined[:limit]


def _catalog_snapshot_at() -> str:
    """가격 목록을 실제로 읽은 시간을 안내 metadata로만 기록한다."""

    # 가격은 외부 API 호출 값이 아니라 저장된 catalog snapshot이다. 이 timestamp는
    # 구조 digest나 ResourcePlan에 넣지 않아 같은 설계의 IaC가 흔들리지 않게 한다.
    load_dataset()
    return datetime.now(UTC).isoformat()


def compute_sizing_guidance(
    deployment_plan: dict[str, Any],
    *,
    provider: str,
    region: str,
    workload_graph: dict[str, Any] | None = None,
    capacity_overrides: list[dict[str, Any] | CapacityOverride] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """compute unit별 SKU 후보와 VM 비교용 월 가격을 반환한다.

    후보의 ``monthlyComputeUSD``는 VM SKU를 비교하는 detail일 뿐 전체 배포 견적이
    아니다. 전체 ResourcePlan 가격·예산 비교는 선택 구성을 materialize한 뒤
    ``estimate_selected_deployment_cost``가 known floor와 unknown term으로 제공한다.
    """

    deployment_plan, _overrides = apply_capacity_overrides(
        deployment_plan, capacity_overrides
    )
    provider = provider.lower()
    guidance: list[dict[str, Any]] = []
    workloads = {
        str(item.get("id") or ""): item
        for item in (workload_graph or {}).get("workloads") or []
        if isinstance(item, dict)
    }
    workloads_by_compute = _workloads_by_compute(deployment_plan)
    for compute in deployment_plan.get("computeUnits") or []:
        if not isinstance(compute, dict):
            continue
        requirements = dict(compute.get("resourceRequirements") or {})
        min_vcpu = requirements.get("minVCpu")
        min_memory = requirements.get("minMemoryGiB")
        minimum_replicas = max(1, int(compute.get("replicaCount") or 1))
        safety_values = {
            str((workloads.get(workload_id) or {}).get("replicationSafety") or "unknown")
            for workload_id in workloads_by_compute.get(str(compute.get("id") or ""), [])
        }
        replication_safety = (
            "singleton"
            if "singleton" in safety_values
            else "interchangeable"
            if safety_values == {"interchangeable"}
            else "unknown"
        )
        item: dict[str, Any] = {
            "computeUnitId": str(compute.get("id") or ""),
            "minimumReplicaCount": minimum_replicas,
            "selectedReplicaCount": minimum_replicas,
            "replicationSafety": replication_safety,
            "minimumRequirements": {
                "minVCpu": min_vcpu,
                "minMemoryGiB": min_memory,
            },
            "candidates": [],
        }
        if not isinstance(min_vcpu, (int, float)) or isinstance(min_vcpu, bool) or not isinstance(
            min_memory, (int, float)
        ) or isinstance(min_memory, bool):
            item.update(
                {
                    "status": "needsInput",
                    "reason": "Minimum vCPU and memory are required before choosing a VM SKU.",
                }
            )
            guidance.append(item)
            continue
        rows = _catalog_candidates(
            provider=provider,
            region=region,
            min_vcpu=float(min_vcpu),
            min_memory_gib=float(min_memory),
            limit=limit,
        )
        item["status"] = "completed" if rows else "needsInput"
        if not rows:
            item["reason"] = "The local cloud catalog has no priced SKU for this provider, region, and minimum capacity."
        for row in rows:
            raw_hourly = row.get("hourlyUSD")
            hourly = float(raw_hourly) if raw_hourly is not None else None
            item["candidates"].append(
                {
                    "sku": row["specName"],
                    "vCPU": row["vCPU"],
                    "memoryGiB": row["memGiB"],
                    "hourlyComputeUSD": hourly,
                    "monthlyComputeUSD": (
                        round(hourly * HOURS_PER_MONTH * minimum_replicas, 4)
                        if hourly is not None
                        else None
                    ),
                    "replicaCount": minimum_replicas,
                    "freeTier": vm_free_tier_status(
                        provider=provider,
                        region=region,
                        sku=str(row["specName"]),
                    ),
                    "performance": recommendation_profile(
                        provider=provider,
                        spec_name=str(row["specName"]),
                        spec_id=str(row.get("id") or "") or None,
                    ),
                }
            )
        guidance.append(item)
    return {
        "schemaVersion": "easydep-compute-sizing/v1alpha1",
        "provider": provider,
        "region": region,
        "currency": "USD",
        "hoursPerMonth": HOURS_PER_MONTH,
        "priceRetrievedAt": _catalog_snapshot_at(),
        "scope": "Compute on-demand list price only; excludes storage, databases, network, load balancers, taxes, support, and discounts.",
        "freeTierNotice": vm_free_tier_notice(),
        "computeUnits": guidance,
    }


def _workloads_by_compute(plan: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for placement in plan.get("placements") or []:
        if not isinstance(placement, dict):
            continue
        compute_id = str(placement.get("computeUnitRef") or "")
        workload_id = str(placement.get("workloadRef") or "")
        if compute_id and workload_id:
            result.setdefault(compute_id, []).append(workload_id)
    return result


def _selection_issues(
    graph: dict[str, Any], plan: dict[str, Any], selections: list[ComputeSelection]
) -> list[dict[str, str]]:
    workloads = {
        str(item.get("id") or ""): item
        for item in graph.get("workloads") or []
        if isinstance(item, dict)
    }
    compute_ids = {str(item.get("id") or "") for item in plan.get("computeUnits") or []}
    workloads_by_compute = _workloads_by_compute(plan)
    issues: list[dict[str, str]] = []
    seen: set[str] = set()
    for selection in selections:
        if selection.compute_unit_id in seen:
            issues.append({"field": "computeUnitId", "reason": "Each compute unit can be selected once."})
            continue
        seen.add(selection.compute_unit_id)
        if selection.compute_unit_id not in compute_ids:
            issues.append({"field": "computeUnitId", "reason": f"Unknown compute unit: {selection.compute_unit_id}."})
            continue
        for workload_id in workloads_by_compute.get(selection.compute_unit_id, []):
            safety = str((workloads.get(workload_id) or {}).get("replicationSafety") or "unknown")
            if selection.replica_count <= 1 or safety == "interchangeable":
                continue
            if safety == "unknown" and selection.replication_confirmed:
                continue
            issues.append(
                {
                    "field": f"computeUnits.{selection.compute_unit_id}.replicaCount",
                    "reason": (
                        "Replica counts above one require replicationConfirmed=true when replicationSafety is unknown."
                        if safety == "unknown"
                        else f"Workload {workload_id} is not safe for multiple replicas."
                    ),
                }
            )
    missing = sorted(compute_ids - seen)
    if missing:
        issues.append(
            {
                "field": "computeUnits",
                "reason": f"Choose a VM SKU and replica count for: {', '.join(missing)}.",
            }
        )
    return issues


def apply_compute_selections(
    bundle: dict[str, Any],
    selections: list[dict[str, Any]],
    *,
    selected_target: dict[str, Any] | str | None = None,
    capacity_overrides: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """선택 SKU·replica를 target ResourcePlan과 IaC 입력으로 다시 투영한다.

    unknown replication safety의 scale-out은 명시적인 confirmation을 요구한다. 이
    함수는 예외 대신 ``sizing.status=needsInput``을 반환해 UI가 같은 선택 화면에서
    확인을 요청할 수 있게 한다.
    """

    result = select_deployment_target(bundle, selected_target or bundle.get("selectedTarget") or {})
    projection = next(
        item
        for item in result.get("projections") or []
        if isinstance(item, dict) and item.get("target") == result.get("selectedTarget")
    )
    original_plan = dict(projection.get("deploymentPlan") or {})
    stored_capacity_overrides = list(
        (projection.get("sizing") or {}).get("capacityOverrides") or []
    )
    plan, parsed_capacity = apply_capacity_overrides(
        original_plan,
        stored_capacity_overrides if capacity_overrides is None else capacity_overrides,
    )
    graph = copy.deepcopy(result.get("workloadGraph") or {})
    parsed = [ComputeSelection.model_validate(item) for item in selections]
    issues = _selection_issues(graph, plan, parsed)
    if issues:
        sizing = {"status": "needsInput", "issues": issues}
        projection["sizing"] = sizing
        result["status"] = "needsInput"
        # Keep the old top-level field as a summary for callers that rendered
        # it before projections gained their own sizing state.  The source of
        # truth is the selected projection, so another target cannot consume
        # these selections.
        result["sizing"] = {"target": copy.deepcopy(result["selectedTarget"]), **sizing}
        return result

    compute_by_id = {
        str(item.get("id") or ""): item
        for item in plan.get("computeUnits") or []
        if isinstance(item, dict)
    }
    guidance = compute_sizing_guidance(
        plan,
        provider=str(projection.get("provider") or "").lower(),
        region=str(projection.get("region") or ""),
        workload_graph=graph,
        limit=50,
    )
    guidance_by_compute = {
        str(item.get("computeUnitId") or ""): item
        for item in guidance.get("computeUnits") or []
        if isinstance(item, dict)
    }
    for selection in parsed:
        compute = compute_by_id[selection.compute_unit_id]
        minimum = max(1, int(compute.get("replicaCount") or 1))
        if selection.replica_count < minimum:
            raise ValueError("Selected replica count cannot be below the planned minimum.")
        candidates = guidance_by_compute.get(selection.compute_unit_id, {}).get("candidates") or []
        match = next(
            (item for item in candidates if item.get("sku") == selection.sku),
            None,
        )
        if match is None:
            raise ValueError("Selected VM SKU is absent from the exact provider and region catalog candidates.")
        compute["replicaCount"] = selection.replica_count
        compute["vmSku"] = selection.sku
        compute["selectedVmSku"] = selection.sku
        compute["selectedReplicaCount"] = selection.replica_count

    # build_deployment_plan owns topology changes (a selected replica count can
    # turn a standalone VM into a managed group). Reflect confirmed unknown
    # safety as an explicit user decision in this selected deployment only.
    workloads_by_compute = _workloads_by_compute(plan)
    constraints = graph.setdefault("constraints", [])
    for selection in parsed:
        for workload_id in workloads_by_compute.get(selection.compute_unit_id, []):
            workload = next(
                (item for item in graph.get("workloads") or [] if item.get("id") == workload_id),
                None,
            )
            if workload is None:
                continue
            constraint_id = f"selected-replicas-{workload_id}"
            constraints[:] = [item for item in constraints if item.get("id") != constraint_id]
            constraints.append(
                {
                    "id": constraint_id,
                    "kind": "replicaCount",
                    "workloadRefs": [workload_id],
                    "value": selection.replica_count,
                    "required": True,
                    "sourceRefs": ["user:compute-sizing-selection"],
                }
            )
            if selection.replica_count > 1 and selection.replication_confirmed:
                # 사용자가 "어느 인스턴스가 처리해도 같다"는 의미를 확인한 결과는
                # 별도 가짜 constraint가 아니라 그 질문의 실제 답인 replicationSafety에
                # 기록한다. 저장 후 다시 Pydantic 검증해도 같은 WorkloadGraph 계약이다.
                workload["replicationSafety"] = "interchangeable"
                workload["sourceRefs"] = list(
                    dict.fromkeys(
                        [
                            *(workload.get("sourceRefs") or []),
                            "user:compute-sizing-selection",
                        ]
                    )
                )
    context = dict(projection.get("planningContext") or {})
    selected_plan = build_deployment_plan(graph, context)
    # Capacity is deliberately not written back to WorkloadGraph. Reapply it only
    # after topology has been regenerated so a design rebind keeps the same late
    # sizing input without redefining requirements.
    selected_plan = _apply_parsed_capacity_overrides(selected_plan, parsed_capacity)
    selected_by_workloads = _workloads_by_compute(selected_plan)
    for selection in parsed:
        selected_workloads = set(workloads_by_compute.get(selection.compute_unit_id, []))
        target_compute = next(
            (
                compute_id
                for compute_id, workload_ids in selected_by_workloads.items()
                if selected_workloads.intersection(workload_ids)
            ),
            "",
        )
        selected_compute: dict[str, Any] | None = next(
            (item for item in selected_plan.get("computeUnits") or [] if item.get("id") == target_compute),
            None,
        )
        if selected_compute is not None:
            selected_compute["vmSku"] = selection.sku
            selected_compute["selectedVmSku"] = selection.sku
            selected_compute["selectedReplicaCount"] = selection.replica_count
    resource_plan = build_provider_resource_plan(
        selected_plan,
        graph,
        provider=str(projection.get("provider") or ""),
        region=str(projection.get("region") or ""),
    )
    sizing = {
        "status": "completed",
        "guidance": guidance,
        "selected": [item.model_dump(by_alias=True) for item in parsed],
        "capacityOverrides": [item.model_dump(by_alias=True) for item in parsed_capacity],
    }
    projection.update(
        {
            "status": "completed",
            "deploymentPlan": selected_plan,
            "deploymentPlanStructureDigest": selected_plan.get("structureDigest", ""),
            "resourcePlan": resource_plan,
            "resourcePlanStructureDigest": resource_plan.get("structureDigest", ""),
            "issues": [],
            "sizing": sizing,
        }
    )
    result["workloadGraph"] = graph
    result["status"] = "completed"
    result["sizing"] = {"target": copy.deepcopy(result["selectedTarget"]), **sizing}
    return result


def _configured_rate_resolver() -> tuple[RetailRateResolver, str | None]:
    """Load the reviewed local snapshot; a sizing request never calls a CSP."""

    configured = os.getenv("EASYDEP_RETAIL_RATE_SNAPSHOT_PATH", "").strip()
    path = Path(configured) if configured else _BUNDLED_RETAIL_RATE_SNAPSHOT_PATH
    if not path.is_file():
        raise ValueError(
            "Configured retail-rate snapshot file does not exist."
            if configured
            else "Bundled retail-rate snapshot file does not exist."
        )
    try:
        snapshot = load_retail_rate_snapshot(path.read_bytes())
    except Exception as error:
        raise ValueError("Retail-rate snapshot is invalid.") from error
    return build_retail_rate_resolver(snapshot), snapshot.snapshot_digest


def _monthly_budget(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        budget = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return budget if budget >= 0 else None


def _public_quote(quote: DeploymentCostQuote, snapshot_digest: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "currency": quote.currency,
        "knownFloorUSD": float(quote.known_floor_usd),
        "complete": quote.complete,
        "monthlyBudgetUSD": float(quote.monthly_budget_usd) if quote.monthly_budget_usd is not None else None,
        "status": quote.verdict.value,
        "components": [
            {
                "ruleId": component.rule_id,
                "primitiveKind": component.primitive_kind,
                "term": component.term,
                "amountUSD": float(component.amount_usd) if component.amount_usd is not None else None,
                "rateKey": component.rate_key,
                "known": component.known,
                "reason": component.reason,
            }
            for component in quote.components
        ],
    }
    if snapshot_digest:
        result["sourceMetadata"] = {"snapshotDigest": snapshot_digest}
    return result


def estimate_selected_deployment_cost(
    bundle: dict[str, Any],
    selections: list[dict[str, Any]],
    *,
    target: dict[str, Any] | str,
    capacity_overrides: list[dict[str, Any]] | None = None,
    resolver: RetailRateResolver | None = None,
    snapshot_digest: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Materialize the exact VM choices, then quote its entire existing plan."""

    updated = apply_compute_selections(
        bundle,
        selections,
        selected_target=target,
        capacity_overrides=capacity_overrides,
    )
    if updated.get("status") != "completed":
        return updated, None
    if resolver is None:
        resolver, snapshot_digest = _configured_rate_resolver()
    projection = next(
        item for item in updated.get("projections") or []
        if isinstance(item, dict) and item.get("target") == updated.get("selectedTarget")
    )
    resource_spec = updated.get("resourceSpec")
    inventory = build_pricing_inventory(
        dict(projection.get("resourcePlan") or {}),
        dict(projection.get("deploymentPlan") or {}),
        dict(updated.get("workloadGraph") or {}),
        monthly_budget_usd=_monthly_budget(
            resource_spec.get("monthlyBudgetUSD") if isinstance(resource_spec, dict) else None
        ),
    )
    return updated, _public_quote(estimate_deployment_cost(inventory, resolver), snapshot_digest)


__all__ = [
    "CapacityOverride",
    "ComputeSelection",
    "apply_capacity_overrides",
    "apply_compute_selections",
    "compute_sizing_guidance",
    "estimate_selected_deployment_cost",
]
