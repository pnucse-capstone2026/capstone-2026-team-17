"""현재 프로젝트와 로컬 Cloud KB를 대화 답변용의 작은 근거로 투영한다."""

from __future__ import annotations

import re
from typing import Any, Literal

from app.cloudkb import region_catalog
from app.cloudkb.costkb.dataset import find_by_name
from app.cloudkb.costkb.free_tier import vm_free_tier_status
from app.design.services.deployment_diagram.sizing import compute_sizing_guidance
from app.repositories import artifact_repository

CloudTopic = Literal["provider_region", "sku", "free_tier", "topology"]

_PROVIDER_ALIASES = {
    "aws": ("aws", "amazon web services"),
    "azure": ("azure", "microsoft azure"),
    "gcp": ("gcp", "google cloud", "google cloud platform"),
}
_SKU_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)+(?![A-Za-z0-9])")


def cloud_guidance_evidence(app_id: str, topic: CloudTopic, query: str) -> dict[str, Any]:
    """Return bounded, read-only evidence for an existing grounded chat answer."""

    state = artifact_repository.load_state(app_id)
    resource_spec = _mapping(state.get("resource_spec"))
    bundle = _mapping(state.get("deployment_diagram_bundle"))
    deployment_plan = _mapping(state.get("deployment_plan"))
    resource_plan = _mapping(state.get("deployment_resource_plan"))
    workload_graph = _mapping(state.get("deployment_workload_graph"))
    provider, region = _current_target(resource_spec, bundle, resource_plan)

    evidence: dict[str, Any] = {
        "topic": topic,
        "current": {
            "provider": provider or None,
            "region": region or None,
            "deploymentTargets": [
                {
                    "provider": str(item.get("provider") or ""),
                    "region": str(item.get("region") or ""),
                }
                for item in _records(resource_spec.get("deploymentTargets"))[:3]
            ],
            "minimumCapacity": {
                "vCPU": resource_spec.get("minVCpu"),
                "memoryGiB": resource_spec.get("minMemoryGiB"),
            },
            "computeSelections": _compute_selections(deployment_plan),
        },
    }
    if topic == "provider_region":
        evidence.update(_provider_region_evidence(query, provider))
    elif topic in {"sku", "free_tier"}:
        evidence.update(
            _sizing_evidence(
                query=query,
                provider=provider,
                region=region,
                deployment_plan=deployment_plan,
                workload_graph=workload_graph,
            )
        )
    else:
        evidence["topology"] = _topology_evidence(workload_graph, deployment_plan, resource_plan)
    return evidence


def _provider_region_evidence(query: str, current_provider: str) -> dict[str, Any]:
    mentioned = _mentioned_providers(query)
    relevant = mentioned or ([current_provider] if current_provider else [])
    counts = {
        provider: len(region_catalog.catalog(provider=provider))
        for provider in region_catalog.providers()
    }
    result: dict[str, Any] = {"supportedProviders": counts}
    if relevant:
        result["regionCatalogs"] = [
            {
                "provider": provider,
                "regions": [
                    {"code": item.code, "name": item.name}
                    for item in region_catalog.catalog(provider=provider)
                ],
            }
            for provider in relevant
            if provider in counts
        ]
    else:
        result["guidance"] = (
            "Choose a provider before listing regions; no provider was selected or named."
        )
    return result


def _sizing_evidence(
    *,
    query: str,
    provider: str,
    region: str,
    deployment_plan: dict[str, Any],
    workload_graph: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "catalogScope": (
            "Local catalog snapshot only. Free Tier eligibility and usage limits depend on the "
            "account and may not make the whole deployment free."
        )
    }
    if provider and region and deployment_plan:
        result["sizing"] = _compact_sizing(
            compute_sizing_guidance(
                deployment_plan,
                provider=provider,
                region=region,
                workload_graph=workload_graph,
                limit=5,
            )
        )
    else:
        result["sizing"] = {
            "status": "unavailable",
            "reason": ("SKU guidance requires a selected provider, region, and deployment plan."),
        }

    requested: list[dict[str, Any]] = []
    for sku in _sku_names(query)[:5]:
        rows = find_by_name(sku, provider=provider or None)
        if provider and region:
            rows = [
                row for row in rows if str(row.get("region") or "").casefold() == region.casefold()
            ]
        matches = []
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            key = (
                str(row.get("provider") or ""),
                str(row.get("region") or ""),
                str(row.get("specName") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            match = {
                "provider": key[0],
                "region": key[1],
                "sku": key[2],
                "vCPU": row.get("vCPU"),
                "memoryGiB": row.get("memGiB"),
                "hourlyComputeUSD": row.get("hourlyUSD"),
            }
            if key[0] and key[1] and key[2]:
                match["freeTier"] = vm_free_tier_status(provider=key[0], region=key[1], sku=key[2])
            matches.append(match)
            if len(matches) == 6:
                break
        requested.append(
            {
                "sku": sku,
                "matches": matches,
                "status": "found" if matches else "notFoundForCurrentTarget",
            }
        )
    if requested:
        result["requestedSkus"] = requested
    return result


def _compact_sizing(guidance: dict[str, Any]) -> dict[str, Any]:
    sources: list[str] = []
    units = []
    for unit in _records(guidance.get("computeUnits"))[:12]:
        candidates = []
        for candidate in _records(unit.get("candidates"))[:5]:
            free_tier = _mapping(candidate.get("freeTier"))
            for url in free_tier.get("sourceUrls") or []:
                value = str(url)
                if value and value not in sources:
                    sources.append(value)
            candidates.append(
                {
                    "sku": candidate.get("sku"),
                    "vCPU": candidate.get("vCPU"),
                    "memoryGiB": candidate.get("memoryGiB"),
                    "hourlyComputeUSD": candidate.get("hourlyComputeUSD"),
                    "monthlyComputeUSD": candidate.get("monthlyComputeUSD"),
                    "replicaCount": candidate.get("replicaCount"),
                    "freeTier": {
                        key: free_tier.get(key) for key in ("status", "label", "summary", "asOf")
                    },
                    "performance": _compact_performance(candidate.get("performance")),
                }
            )
        units.append(
            {
                "computeUnitId": unit.get("computeUnitId"),
                "status": unit.get("status"),
                "reason": unit.get("reason"),
                "minimumReplicaCount": unit.get("minimumReplicaCount"),
                "minimumRequirements": unit.get("minimumRequirements"),
                "candidates": candidates,
            }
        )
    return {
        "provider": guidance.get("provider"),
        "region": guidance.get("region"),
        "scope": guidance.get("scope"),
        "freeTierNotice": guidance.get("freeTierNotice"),
        "freeTierSources": sources[:6],
        "computeUnits": units,
    }


def _compact_performance(value: object) -> dict[str, Any]:
    performance = _mapping(value)
    return {
        "status": performance.get("status"),
        "warning": performance.get("warning"),
        "sustainedCpu": performance.get("sustainedCpu"),
        "attributes": [
            _pick(item, "key", "label", "display", "warning")
            for item in _records(performance.get("attributes"))[:8]
        ],
    }


def _topology_evidence(
    workload_graph: dict[str, Any],
    deployment_plan: dict[str, Any],
    resource_plan: dict[str, Any],
) -> dict[str, Any]:
    return {
        "workloads": [
            _pick(item, "id", "name", "kind", "runtime", "persistence")
            for item in _records(workload_graph.get("workloads"))[:20]
        ],
        "computeUnits": [
            _pick(
                item,
                "id",
                "replicaCount",
                "selectedReplicaCount",
                "selectedVmSku",
                "vmSku",
                "resourceRequirements",
            )
            for item in _records(deployment_plan.get("computeUnits"))[:20]
        ],
        "resources": [
            _pick(
                item,
                "id",
                "name",
                "group",
                "providerKind",
                "terraformTypes",
                "templateRuleId",
                "sourceRefs",
            )
            for item in _records(resource_plan.get("nodes"))[:30]
        ],
        "placements": _records(deployment_plan.get("placements"))[:30],
        "issues": _records(resource_plan.get("issues"))[:12],
    }


def _current_target(
    resource_spec: dict[str, Any],
    bundle: dict[str, Any],
    resource_plan: dict[str, Any],
) -> tuple[str, str]:
    selected = _mapping(bundle.get("selectedTarget"))
    provider = str(
        resource_plan.get("provider")
        or selected.get("provider")
        or resource_spec.get("provider")
        or ""
    ).lower()
    region = str(
        resource_plan.get("region") or selected.get("region") or resource_spec.get("region") or ""
    )
    return provider, region


def _compute_selections(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "computeUnitId": str(item.get("id") or ""),
            "sku": item.get("selectedVmSku") or item.get("vmSku"),
            "replicaCount": item.get("selectedReplicaCount") or item.get("replicaCount"),
        }
        for item in _records(plan.get("computeUnits"))[:20]
    ]


def _mentioned_providers(text: str) -> list[str]:
    normalized = " ".join(text.casefold().split())
    return [
        provider
        for provider, aliases in _PROVIDER_ALIASES.items()
        if any(
            re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", normalized)
            for alias in aliases
        )
    ]


def _sku_names(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(0) for match in _SKU_TOKEN.finditer(text)))


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _records(value: object) -> list[dict[str, Any]]:
    return (
        [dict(item) for item in value or [] if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _pick(item: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: item[key] for key in keys if key in item}


__all__ = ["CloudTopic", "cloud_guidance_evidence"]
