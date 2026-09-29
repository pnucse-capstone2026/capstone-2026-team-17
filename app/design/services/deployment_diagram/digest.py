"""배포 graph·plan의 canonical structure digest 규칙을 소유한다."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


def canonical_digest(value: Any) -> str:
    """JSON 값의 SHA-256 canonical digest를 계산한다.

    Args:
        value: JSON 직렬화 가능한 값이다.

    Returns:
        key 정렬과 고정 separator를 사용한 16진수 SHA-256 digest다.

    Notes:
        비ASCII 문자는 escape하지 않아 기존 structureDigest 규칙을 유지한다.
    """

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


_canonical_digest = canonical_digest


def workload_graph_structure_digest(graph: dict[str, Any]) -> str:
    """WorkloadGraph의 구조적 필드만 canonical digest에 포함한다.

    Args:
        graph: 정규화된 WorkloadGraph다.

    Returns:
        runtime 관측값과 진단을 제외한 structure digest다.

    Notes:
        입력 객체는 변경하지 않는다.
    """

    structural = copy.deepcopy(graph)
    for field in ("issues", "derivations", "inputArtifacts", "inputDigest", "structureDigest"):
        structural.pop(field, None)
    for workload in structural.get("workloads") or []:
        # 사람이 읽는 이름은 같은 배포 구조의 표시 값이다. 이름 피드백만으로
        # DeploymentPlan이나 IaC가 다른 구조로 판정되면 안 된다.
        workload.pop("name", None)
        artifact = workload.get("artifact") or {}
        artifact.pop("imageDigest", None)
        for interface in workload.get("interfaces") or []:
            interface.pop("name", None)
            interface.pop("port", None)
            interface.pop("healthPath", None)
        for configuration in workload.get("configuration") or []:
            configuration.pop("value", None)
            configuration.pop("secretRef", None)
    for dependency in structural.get("externalDependencies") or []:
        dependency.pop("name", None)
        for interface in dependency.get("interfaces") or []:
            interface.pop("name", None)
            interface.pop("port", None)
            interface.pop("healthPath", None)
    return _canonical_digest(structural)


def deployment_plan_structure_digest(plan: dict[str, Any]) -> str:
    """DeploymentPlan의 구조적 필드만 canonical digest에 포함한다.

    Args:
        plan: provider-neutral DeploymentPlan이다.

    Returns:
        late binding과 진단을 제외한 structure digest다.

    Notes:
        입력 객체는 변경하지 않는다.
    """

    structural = copy.deepcopy(plan)
    for field in ("issues", "structureDigest"):
        structural.pop(field, None)
    for compute in structural.get("computeUnits") or []:
        # SKU 선택과 sizing minimum은 topology가 확정된 뒤 같은 compute unit에
        # 결합되는 값이다. replicaCount 자체는 구조에 남기되, 그 선택을 중복 기록한
        # 필드와 capacity tuning 값은 preview stale guard에서 제외한다.
        for field in ("vmSku", "selectedVmSku", "selectedReplicaCount"):
            compute.pop(field, None)
        compute["resourceRequirements"] = {}
    for path in structural.get("networkPaths") or []:
        path.pop("port", None)
    return _canonical_digest(structural)


def resource_plan_structure_digest(plan: dict[str, Any]) -> str:
    """provider template 규칙으로 ResourcePlan structure digest를 계산한다.

    Args:
        plan: provider ResourcePlan이다.

    Returns:
        provider template과 같은 canonical structure digest다.

    Notes:
        다음 provider projection 분리 전까지 기존 digest 구현에 위임한다.
    """

    from app.design.services.deployment_diagram.provider_template import (
        provider_template_structure_digest,
    )

    return provider_template_structure_digest(plan)
