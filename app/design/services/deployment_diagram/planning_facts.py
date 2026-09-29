"""상류 설계 산출물을 감사 가능한 배포 planning fact로 투영한다."""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import Any

from app.design.services.deployment_diagram.digest import (
    canonical_digest as _canonical_digest,
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


def _version_fields(value: Any) -> dict[str, Any]:
    """저장소 version metadata와 기존 정수 입력을 같은 출력으로 정리한다."""

    if not isinstance(value, dict):
        return {"version": value}
    return {
        "version": value.get("version_no"),
        "versionId": value.get("version_id"),
        "storedDigest": value.get("stored_digest"),
    }


def extract_planning_facts(
    *,
    refined_requirements: Any = None,
    capability_contract: dict[str, Any] | None = None,
    resource_intake: dict[str, Any] | None = None,
    resource_spec: dict[str, Any] | None = None,
    usecase_spec: Any = None,
    class_model: Any = None,
    sequence_model: Any = None,
    api_spec: Any = None,
    erd_model: Any = None,
    artifact_versions: dict[str, Any] | None = None,
    additional_planning_facts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """설계 입력을 감사 가능한 PlanningFact 문서로 정규화한다.

    Args:
        refined_requirements: 정제 요구사항 JSON이다.
        capability_contract: 승인 capability 계약이다.
        resource_intake: resource 입력과 provenance다.
        resource_spec: 명시된 배포 대상과 용량 문맥이다.
        usecase_spec: 유스케이스 명세 JSON이다.
        class_model: BCE model JSON이다.
        sequence_model: sequence model JSON이다.
        api_spec: API 계약 JSON이다.
        erd_model: logical data model JSON이다.
        artifact_versions: 입력 artifact별 version이다.
        additional_planning_facts: 승인된 deployment-only fact다.

    Returns:
        입력 digest, fact, issue 순서를 보존한 PlanningFact 문서다.

    Notes:
        자연어 문장을 ResourcePlan에 복사하지 않으며 ``needsQuestion`` capability는
        placement 전에 blocking issue로 남긴다.
    """

    artifacts = {
        "refinedRequirements": refined_requirements or [],
        "capabilityContract": capability_contract or {},
        "resourceIntake": resource_intake or {},
        "resourceSpec": resource_spec or {},
        "usecaseSpec": usecase_spec or {},
        "classModel": class_model or {},
        "sequenceModel": sequence_model or {},
        "apiSpec": api_spec or {},
        "erdModel": erd_model or {},
        "deploymentPlanningFacts": additional_planning_facts or [],
    }
    inputs = [
        {
            "artifact": name,
            **_version_fields((artifact_versions or {}).get(name)),
            "digest": _canonical_digest(value),
        }
        for name, value in artifacts.items()
    ]
    facts: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    def add_fact(
        fact_id: str,
        kind: str,
        value: Any,
        source_refs: Iterable[str],
        rule: str,
        authority: str,
        status: str = "accepted",
    ) -> None:
        facts.append(
            {
                "id": fact_id,
                "kind": kind,
                "value": value,
                "sourceRefs": _refs(source_refs),
                "derivationRule": rule,
                "authority": authority,
                "status": status,
            }
        )

    spec = dict(resource_spec or {})
    for field in (
        "workloads",
        "provider",
        "region",
        "candidateZones",
        "deploymentTargets",
        "monthlyBudgetUSD",
        "minVCpu",
        "minMemoryGiB",
        "trafficPattern",
        "scale",
        "dataResidency",
    ):
        if field in spec:
            add_fact(
                f"resource-{_slug(field)}",
                "planningContext",
                {"field": field, "value": copy.deepcopy(spec[field])},
                [f"resourceSpec:{field}"],
                "resource-spec-copy",
                "explicit",
            )

    provenance = list((resource_intake or {}).get("provenance") or [])
    for index, item in enumerate(provenance, start=1):
        if not isinstance(item, dict):
            continue
        add_fact(
            f"resource-provenance-{index}",
            "resourceProvenance",
            {"field": item.get("field"), "value": item.get("value")},
            [f"resourceIntake:provenance:{index}"],
            "resource-intake-provenance",
            "observed",
        )

    for index, capability in enumerate(
        (capability_contract or {}).get("capabilities") or [], start=1
    ):
        if not isinstance(capability, dict):
            continue
        decision = str(capability.get("decision") or "")
        source_refs = [
            *(f"requirement:{item}" for item in capability.get("requirementIds") or []),
            *(
                f"capability:{capability.get('id')}:evidence:{evidence_index}"
                for evidence_index, _span in enumerate(
                    capability.get("evidenceSpans") or [], start=1
                )
            ),
        ]
        add_fact(
            f"capability-{_slug(capability.get('id') or index)}",
            "capability",
            {
                "capabilityId": capability.get("id"),
                "necessity": capability.get("necessity"),
                "dependencyCapabilityIds": list(capability.get("dependencyCapabilityIds") or []),
                "typedConstraints": copy.deepcopy(capability.get("typedConstraints") or []),
            },
            source_refs,
            "accepted-capability-to-planning-candidate",
            "explicit" if capability.get("origin") == "explicit" else "derived",
            decision or "unknown",
        )
        if decision == "needsQuestion":
            issues.append(
                _issue(
                    f"capabilityContract.capabilities.{index - 1}",
                    "A capability decision still needs a user answer before deployment planning.",
                    source_refs=source_refs,
                )
            )

    api_paths = (api_spec or {}).get("paths") if isinstance(api_spec, dict) else None
    if api_paths:
        add_fact(
            "design-http-api",
            "applicationInterfaceCandidate",
            {
                "protocol": "http",
                "endpointCount": sum(len(v) for v in api_paths.values() if isinstance(v, dict)),
            },
            ["apiSpec:paths"],
            "structured-api-interface",
            "derived",
        )

    erd_present = bool(erd_model)
    if isinstance(erd_model, str):
        erd_present = bool(erd_model.strip() and "@startuml" in erd_model)
    if erd_present:
        add_fact(
            "design-logical-data-model",
            "persistentDataRequirement",
            {"schemaMigrationRequired": True, "runtimeEngine": None},
            ["erdModel"],
            "erd-proves-logical-data-not-runtime",
            "derived",
        )

    for fact in additional_planning_facts or []:
        if not isinstance(fact, dict):
            continue
        facts.append(copy.deepcopy(fact))

    return {
        "schemaVersion": "easydep-planning-facts",
        "facts": facts,
        "inputArtifacts": inputs,
        "inputDigest": _canonical_digest(inputs),
        "issues": issues,
    }


def planning_context(resource_spec: dict[str, Any] | None) -> dict[str, Any]:
    """resource spec에서 provider-neutral planning context를 만든다.

    Args:
        resource_spec: 요구사항 단계가 승인한 resource specification이다.

    Returns:
        기존 key와 deployment target 순서를 유지한 planning context다.

    Notes:
        누락 값은 추론하지 않고 기존 ``None`` 또는 빈 목록으로 유지한다.
    """

    spec = dict(resource_spec or {})
    targets = [dict(item) for item in spec.get("deploymentTargets") or [] if isinstance(item, dict)]
    return {
        "schemaVersion": "easydep-planning-context",
        "workloads": copy.deepcopy(spec.get("workloads")),
        "provider": spec.get("provider"),
        "region": spec.get("region"),
        "candidateZones": list(spec.get("candidateZones") or spec.get("selectedZones") or []),
        "selectedZones": list(spec.get("selectedZones") or []),
        "zoneSelectionSource": spec.get("zoneSelectionSource"),
        "deploymentTargets": targets,
        "monthlyBudgetUSD": spec.get("monthlyBudgetUSD"),
        "minVCpu": spec.get("minVCpu"),
        "minMemoryGiB": spec.get("minMemoryGiB"),
        "trafficPattern": spec.get("trafficPattern"),
        "scale": copy.deepcopy(spec.get("scale")),
        "dataResidency": spec.get("dataResidency"),
    }


def planning_inputs_stale(
    persisted_facts: dict[str, Any],
    *,
    refined_requirements: Any = None,
    capability_contract: dict[str, Any] | None = None,
    resource_intake: dict[str, Any] | None = None,
    resource_spec: dict[str, Any] | None = None,
    usecase_spec: Any = None,
    class_model: Any = None,
    sequence_model: Any = None,
    api_spec: Any = None,
    erd_model: Any = None,
    artifact_versions: dict[str, Any] | None = None,
    additional_planning_facts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """저장된 planning fact와 현재 상류 artifact의 stale 여부를 비교한다.

    Args:
        persisted_facts: 체크포인트에 저장된 PlanningFact 문서다.
        refined_requirements: 현재 정제 요구사항 JSON이다.
        capability_contract: 현재 capability 계약이다.
        resource_intake: 현재 resource 입력이다.
        resource_spec: 현재 resource specification이다.
        usecase_spec: 현재 유스케이스 명세다.
        class_model: 현재 BCE model이다.
        sequence_model: 현재 sequence model이다.
        api_spec: 현재 API 계약이다.
        erd_model: 현재 logical data model이다.
        artifact_versions: 현재 artifact version이다.
        additional_planning_facts: 현재 deployment-only fact다.

    Returns:
        stale flag, 변경 artifact 목록, 현재 fact 문서다.

    Notes:
        digest와 version 중 하나라도 달라지면 기존 순서대로 변경 항목을 기록한다.
    """

    current = extract_planning_facts(
        refined_requirements=refined_requirements,
        capability_contract=capability_contract,
        resource_intake=resource_intake,
        resource_spec=resource_spec,
        usecase_spec=usecase_spec,
        class_model=class_model,
        sequence_model=sequence_model,
        api_spec=api_spec,
        erd_model=erd_model,
        artifact_versions=artifact_versions,
        additional_planning_facts=additional_planning_facts,
    )
    old_by_name = {
        str(item.get("artifact")): item for item in persisted_facts.get("inputArtifacts") or []
    }
    changed = [
        {
            "artifact": item.get("artifact"),
            "persistedVersion": (old_by_name.get(str(item.get("artifact"))) or {}).get("version"),
            "currentVersion": item.get("version"),
            "persistedDigest": (old_by_name.get(str(item.get("artifact"))) or {}).get("digest"),
            "currentDigest": item.get("digest"),
        }
        for item in current.get("inputArtifacts") or []
        if (old_by_name.get(str(item.get("artifact"))) or {}).get("digest") != item.get("digest")
        or (old_by_name.get(str(item.get("artifact"))) or {}).get("version") != item.get("version")
    ]
    return {"stale": bool(changed), "changedArtifacts": changed, "current": current}
