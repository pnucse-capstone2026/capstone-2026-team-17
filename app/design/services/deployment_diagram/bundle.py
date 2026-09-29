"""Compose the design-owned WorkloadGraph deployment bundle."""

from __future__ import annotations

import copy
import json
from typing import Any

from app.cloudkb import region_catalog
from app.design.services.deployment_diagram.planner import (
    BLOCKING_CLASSES,
    build_deployment_plan,
    build_provider_resource_plan,
    extract_planning_facts,
    normalize_workload_graph,
    planning_context,
)


def _catalog_zones(provider: Any, region: Any) -> list[str]:
    wanted_provider = str(provider or "").lower()
    wanted_region = str(region or "").lower()
    match = next(
        (
            item
            for item in region_catalog.catalog(provider=wanted_provider)
            if item.code.lower() == wanted_region
        ),
        None,
    )
    return list(match.zones) if match else []


def _target_descriptor(value: dict[str, Any]) -> dict[str, Any]:
    """배열 순서와 무관하게 비교할 수 있는 최소 target 식별자를 만든다."""

    provider = str(value.get("provider") or "").lower()
    region = str(value.get("region") or "")
    zones = sorted({str(item) for item in value.get("zones") or [] if str(item)})
    identity = {"provider": provider, "region": region, "zones": zones}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {**identity, "id": encoded}


def _target_matches(candidate: dict[str, Any], requested: Any) -> bool:
    if isinstance(requested, str):
        return candidate.get("id") == requested
    if not isinstance(requested, dict):
        return False
    if requested.get("id") and str(requested["id"]) == candidate.get("id"):
        return True
    for field in ("provider", "region"):
        if field not in requested:
            return False
        if str(requested.get(field) or "").lower() != str(candidate.get(field) or "").lower():
            return False
    if "zones" not in requested:
        return True
    zones = sorted({str(item) for item in requested.get("zones") or [] if str(item)})
    return zones == candidate.get("zones")


def _selected_projection(
    projections: list[dict[str, Any]], selected_target: Any
) -> dict[str, Any] | None:
    matches = [
        item
        for item in projections
        if _target_matches(dict(item.get("target") or {}), selected_target)
    ]
    return matches[0] if len(matches) == 1 else None


def _target_contexts(resource_spec: dict[str, Any]) -> list[dict[str, Any]]:
    targets = [
        dict(item)
        for item in resource_spec.get("deploymentTargets") or []
        if isinstance(item, dict)
    ]
    contexts: list[dict[str, Any]] = []
    for target in targets or [None]:
        provider = (
            target.get("provider") if target is not None else resource_spec.get("provider")
        )
        region = target.get("region") if target is not None else resource_spec.get("region")
        explicit = list(
            (target.get("zones") or [])
            if target is not None
            else resource_spec.get("selectedZones")
            or resource_spec.get("candidateZones")
            or []
        )
        catalog = _catalog_zones(provider, region)
        values = {
            **resource_spec,
            "provider": provider,
            "region": region,
            "candidateZones": explicit or catalog,
            "selectedZones": explicit,
            "zoneSelectionSource": (
                "explicit"
                if explicit
                else "catalogBased"
                if catalog
                else "providerSelected"
            ),
        }
        if target is not None:
            # Candidate zones are scoped to this target and never inherit the
            # primary resource spec's zones.
            values["deploymentTargets"] = [target]
        contexts.append(
            planning_context(values)
        )
    return contexts


def build_deployment_diagram_bundle(
    workload_graph_candidate: dict[str, Any],
    resource_spec: dict[str, Any] | None,
    *,
    planning_facts: dict[str, Any] | None = None,
    planning_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build WorkloadGraph, provider-neutral plan, and CSP projections.

    ``planning_inputs`` is accepted at the design adapter boundary so structured
    requirement/design models and their digests travel with the bundle.  Tests and
    importers may supply an already-normalized ``planning_facts`` document.
    """

    spec = copy.deepcopy(resource_spec or {})
    facts = copy.deepcopy(planning_facts) if planning_facts else extract_planning_facts(
        resource_spec=spec, **(planning_inputs or {})
    )
    graph = normalize_workload_graph(workload_graph_candidate, planning_facts=facts)
    graph_blocking = [
        item
        for item in graph.get("issues") or []
        if item.get("classification") in BLOCKING_CLASSES
    ]
    projections: list[dict[str, Any]] = []
    for context in _target_contexts(spec):
        target_zones = (
            []
            if context.get("zoneSelectionSource") != "explicit"
            else context.get("selectedZones") or context.get("candidateZones")
        )
        target = _target_descriptor(
            {
                "provider": context.get("provider"),
                "region": context.get("region"),
                "zones": target_zones,
            }
        )
        # A graph can originate from an older persisted artifact or another
        # importer, bypassing the Pydantic extraction schema.  Do not send an
        # invalid graph to the placement planner: duplicate workload ids would
        # otherwise surface as a raw "exactly one placement" exception.
        if graph_blocking:
            projections.append(
                {
                    "status": "needsInput",
                    "target": target,
                    "provider": context.get("provider"),
                    "region": context.get("region"),
                    "planningContext": context,
                    "deploymentPlan": {},
                    "deploymentPlanStructureDigest": "",
                    "resourcePlan": {},
                    "resourcePlanStructureDigest": "",
                    "issues": copy.deepcopy(graph_blocking),
                }
            )
            continue
        deployment_plan = build_deployment_plan(graph, context)
        resource_plan = build_provider_resource_plan(
            deployment_plan,
            graph,
            provider=str(context.get("provider") or ""),
            region=str(context.get("region") or ""),
        )
        blocking = [
            item
            for item in resource_plan.get("issues") or []
            if item.get("classification") in BLOCKING_CLASSES
        ]
        projections.append(
            {
                "status": "needsInput" if blocking else "completed",
                "target": target,
                "provider": context.get("provider"),
                "region": context.get("region"),
                "planningContext": context,
                "deploymentPlan": deployment_plan,
                "deploymentPlanStructureDigest": deployment_plan["structureDigest"],
                "resourcePlan": resource_plan,
                "resourcePlanStructureDigest": resource_plan["structureDigest"],
                "issues": blocking,
            }
        )
    requested_target = spec.get("selectedTarget")
    selected: dict[str, Any] | None
    if len(projections) == 1:
        selected = projections[0]
    else:
        selected = _selected_projection(projections, requested_target)
    selected_target = dict(selected.get("target") or {}) if selected else None
    selection_status = (
        "selected"
        if selected is not None
        else "needsInput"
    )
    return {
        "schemaVersion": "easydep-deployment-diagram",
        "status": "completed"
        if selected is not None and selected.get("status") == "completed"
        else "needsInput",
        "mode": "single" if len(projections) == 1 else "alternatives",
        "selectedTarget": selected_target,
        "selection": {
            "status": selection_status,
            "reason": "A deployment target must be selected before generating a deployment package."
            if selected is None
            else "",
        },
        "planningFacts": facts,
        "workloadGraph": graph,
        "resourceSpec": spec,
        "projections": projections,
    }


def select_deployment_target(
    bundle: dict[str, Any], selected_target: dict[str, Any] | str
) -> dict[str, Any]:
    """한 CSP projection을 최종 배포 대상으로 선택하고 다시 계산한다.

    후보 배열의 순서는 비교용 정보일 뿐 선택된 target의 DeploymentPlan·ResourcePlan
    계산에 영향을 주지 않는다. 선택되지 않은 projection은 UI 비교를 위해 그대로 둔다.
    """

    if bundle.get("schemaVersion") != "easydep-deployment-diagram":
        raise ValueError("unsupported deployment diagram schema")
    result = copy.deepcopy(bundle)
    projections = [
        item for item in result.get("projections") or [] if isinstance(item, dict)
    ]
    projection = _selected_projection(projections, selected_target)
    if projection is None:
        raise ValueError("selected deployment target does not match exactly one projection")
    graph = result.get("workloadGraph")
    context = projection.get("planningContext")
    if not isinstance(graph, dict) or not isinstance(context, dict):
        raise TypeError("deployment projection has no graph or planning context")
    graph_blocking = [
        item
        for item in graph.get("issues") or []
        if isinstance(item, dict) and item.get("classification") in BLOCKING_CLASSES
    ]
    if graph_blocking:
        projection.update(
            {
                "status": "needsInput",
                "deploymentPlan": {},
                "deploymentPlanStructureDigest": "",
                "resourcePlan": {},
                "resourcePlanStructureDigest": "",
                "issues": copy.deepcopy(graph_blocking),
            }
        )
    else:
        deployment_plan = build_deployment_plan(graph, context)
        resource_plan = build_provider_resource_plan(
            deployment_plan,
            graph,
            provider=str(context.get("provider") or ""),
            region=str(context.get("region") or ""),
        )
        blocking = [
            item
            for item in resource_plan.get("issues") or []
            if isinstance(item, dict) and item.get("classification") in BLOCKING_CLASSES
        ]
        projection.update(
            {
                "status": "needsInput" if blocking else "completed",
                "deploymentPlan": deployment_plan,
                "deploymentPlanStructureDigest": deployment_plan.get("structureDigest", ""),
                "resourcePlan": resource_plan,
                "resourcePlanStructureDigest": resource_plan.get("structureDigest", ""),
                "issues": blocking,
            }
        )
    result["projections"] = projections
    result["selectedTarget"] = dict(projection.get("target") or {})
    if isinstance(result.get("resourceSpec"), dict):
        result["resourceSpec"]["selectedTarget"] = copy.deepcopy(result["selectedTarget"])
    result["selection"] = {"status": "selected", "reason": ""}
    result["status"] = "completed" if projection.get("status") == "completed" else "needsInput"
    return result


def hydrate_deployment_diagram_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Hydrate the sole supported WorkloadGraph deployment bundle."""

    if bundle.get("schemaVersion") != "easydep-deployment-diagram":
        raise ValueError("unsupported deployment diagram schema")
    projections = [item for item in bundle.get("projections") or [] if isinstance(item, dict)]
    primary = _selected_projection(projections, bundle.get("selectedTarget")) or {}
    planning_facts = bundle.get("planningFacts") or {}
    data_execution_facts = [
        copy.deepcopy(item)
        for item in planning_facts.get("facts") or []
        if isinstance(item, dict)
        and item.get("kind") == "dataExecutionMode"
        and item.get("authority") == "explicit"
        and item.get("status") == "accepted"
    ]
    hydrated = {
        "deployment_diagram_bundle": bundle,
        "deployment_diagram_model": dict(bundle.get("workloadGraph") or {}),
        "deployment_workload_graph": dict(bundle.get("workloadGraph") or {}),
        "deployment_plan": dict(primary.get("deploymentPlan") or {}),
        "deployment_resource_plan": dict(primary.get("resourcePlan") or {}),
    }
    if data_execution_facts:
        hydrated["deployment_planning_facts"] = data_execution_facts
    return hydrated
