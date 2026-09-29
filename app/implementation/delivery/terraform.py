"""선택된 ResourcePlan을 사용자 실행용 OpenTofu 패키지로 렌더링한다.

이 모듈에는 이전 cloud resource JSON이나 Kubernetes Terraform 호환 경로가 없다.
설계에서 사용자가 선택하고 검증한 ResourcePlan만 받아, 같은 계획에서 OpenTofu와
cloud-init·Compose·실행 스크립트를 만든다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.design.contracts.deployment import validate_provider_resource_plan
from app.implementation.delivery.iac_renderer import render_open_tofu
from app.implementation.delivery.package import render_deployment_package
from app.implementation.delivery.verification import check_deployment_package

SCHEMA_VERSION = "easydep-iac-render/v1alpha1"
SUPPORTED_PROVIDERS = ("azure", "aws", "gcp")
DEPLOYMENT_BUNDLE_SCHEMA = "easydep-deployment-diagram"


def render_iac(run_root: Path, spec: Any) -> dict[str, object]:
    """완료된 selected ResourcePlan을 `application/deployment`에 한 번만 쓴다."""

    resource_plan, source_evidence = _iac_design_source(spec)
    provider = str(resource_plan.get("provider") or "").lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported OpenTofu provider: {provider or '<empty>'}. "
            f"Supported: {', '.join(SUPPORTED_PROVIDERS)}"
        )

    # The design-source adapter above returns the already parsed ResourcePlan. Keep
    # one boundary check there instead of validating the same plan again here.
    files = render_open_tofu(resource_plan)
    application = run_root / "application"
    application.mkdir(parents=True, exist_ok=True)
    package = render_deployment_package(application, resource_plan, files)
    verification = check_deployment_package(
        application,
        expected=True,
        resource_plan=resource_plan,
    )
    if verification.get("gateStatus") != "PASS":
        raise RuntimeError(
            "Generated deployment package did not pass validation: "
            + "; ".join(str(item) for item in verification.get("issues") or [])
        )
    rendered_files = sorted(
        f"application/deployment/tofu/{name}"
        for name in files
        if name.endswith((".tf", ".tftpl"))
    )
    report: dict[str, object] = {
        "schemaVersion": SCHEMA_VERSION,
        "renderer": f"deterministic-opentofu-{provider}",
        "provider": provider,
        "renderedFiles": rendered_files,
        "requiredVariables": _rendered_required_variables(files),
        "sourceConformance": {
            "status": "SUCCEEDED",
            "errors": [],
            "warnings": [],
            "mode": "resource_plan_exact_rendering",
        },
        "kubernetesManifests": False,
        "sourceEvidence": source_evidence,
        "deploymentPackage": package.relative_to(application).as_posix(),
        "verification": verification,
    }
    reports = run_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "iac-render.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _iac_design_source(spec: Any) -> tuple[dict[str, Any], dict[str, object]]:
    """완료된 deployment bundle에서 선택된 ResourcePlan 하나만 읽는다."""

    bundle_path = spec.inputs.get("deploymentBundle")
    if bundle_path is None or not bundle_path.is_file():
        raise ValueError("IaC rendering requires a completed deployment diagram bundle")
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Deployment diagram bundle could not be read: {error}") from error
    if not isinstance(bundle, dict) or bundle.get("schemaVersion") != DEPLOYMENT_BUNDLE_SCHEMA:
        raise ValueError("IaC rendering requires a valid deployment diagram bundle")
    if bundle.get("status") != "completed":
        raise ValueError("IaC rendering requires a completed deployment diagram bundle")
    selected_target = bundle.get("selectedTarget")
    if not isinstance(selected_target, dict) or not selected_target:
        raise ValueError("IaC rendering requires a selected deployment target")
    projection = _deployment_projection(bundle, selected_target)
    resource_plan = projection.get("resourcePlan")
    if not isinstance(resource_plan, dict):
        raise ValueError("Selected deployment projection has no ResourcePlan")
    validate_provider_resource_plan(resource_plan)
    digest = projection.get("resourcePlanStructureDigest") or resource_plan.get(
        "structureDigest"
    )
    return resource_plan, {
        "resourceSpecSource": "deployment_diagram_resource_plan",
        "deploymentDiagramBundle": True,
        "deploymentDiagramSchema": bundle.get("schemaVersion"),
        "deploymentDiagramResourcePlanDigest": digest,
        "selectedTarget": selected_target,
    }


def _deployment_projection(
    bundle: dict[str, Any], selected_target: dict[str, Any]
) -> dict[str, Any]:
    """선택 ID 또는 provider·region과 정확히 일치하는 완료 projection을 찾는다."""

    selected_id = str(selected_target.get("id") or "")
    matches = [
        item
        for item in bundle.get("projections") or []
        if isinstance(item, dict)
        and isinstance(item.get("target"), dict)
        and (
            str(item["target"].get("id") or "") == selected_id
            if selected_id
            else str(item["target"].get("provider") or "").lower()
            == str(selected_target.get("provider") or "").lower()
            and str(item["target"].get("region") or "")
            == str(selected_target.get("region") or "")
        )
        and item.get("status") == "completed"
    ]
    if len(matches) != 1:
        raise ValueError(
            "Deployment bundle must contain exactly one completed projection for selectedTarget"
        )
    return matches[0]


def _rendered_required_variables(files: dict[str, str]) -> list[dict[str, str]]:
    """렌더된 OpenTofu variable 이름을 사용자 입력 목록으로 돌려준다."""

    variables = sorted(
        set(
            re.findall(
                r'^variable\s+"([^"]+)"\s*\{',
                "\n".join(files.values()),
                flags=re.MULTILINE,
            )
        )
    )
    return [
        {"name": name, "description": "required deployment input"}
        for name in variables
    ]


__all__ = ["render_iac"]
