import os
import re
from copy import deepcopy
from pathlib import Path

from app.implementation.delivery.verification import check_deployment_package
from app.testing.progress import emit_testing_progress
from app.testing.schemas.testing_state import TestingState
from app.testing.utils.static_analysis import scan_stage

_ISSUE_PATH = re.compile(
    r"(?:^|\[|:\s)(?P<path>(?:deployment/)?(?:tofu|runtime|scripts)/[^\]\s:]+)"
)


def _progress_status(report: dict) -> str:
    if report.get("reused") is True:
        return "REUSED"
    value = str(report.get("gateStatus") or "INCONCLUSIVE").upper()
    return {
        "NOT_APPLICABLE": "SKIPPED",
        "DEFERRED": "DEFERRED",
    }.get(value, value if value in {"PASS", "FAIL", "INCONCLUSIVE"} else "INCONCLUSIVE")


def _review_trivy_findings(
    resource_plan: dict,
    issues: list[str],
    *,
    findings: list[dict] | None = None,
    application: Path | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """배포 구조상 필요한 좁은 예외만 남기고 나머지 finding은 그대로 차단한다.

    AWS security group의 제한된 HTTP/HTTPS 외부 통신, 공개 Load Balancer, GCP VM의
    직접 공개 주소는 선택한 배포 구조에서 명시적으로 요청될 수 있다. rule ID만 보고
    무시하지 않고 ResourcePlan에 그 구조가 실제로 있을 때만 허용한다. 같은 rule이 사설
    구조에서 나오거나 다른 보안 설정을 가리키면 계속 차단한다.
    """

    nodes = [item for item in resource_plan.get("nodes") or [] if isinstance(item, dict)]
    terraform_types = {str(kind) for node in nodes for kind in node.get("terraformTypes") or []}
    has_compute = bool(terraform_types & {"aws_instance", "aws_launch_template"})
    has_registry = "aws_ecr_repository" in terraform_types
    has_external_route = any(
        "aws_route" in (node.get("terraformTypes") or [])
        and (node.get("attributes") or {}).get("destination") == "0.0.0.0/0"
        for node in nodes
    )
    # ResourcePlan은 외부 통신 필요성을 알려 주지만 실제 port 범위는 renderer가
    # 작성한 HCL에 있다. 따라서 생성 파일도 함께 확인해 80/443 TCP보다 넓은 규칙이
    # 하나라도 있으면 AWS-0104를 허용하지 않는다.
    main_tf = (
        (application / "deployment" / "tofu" / "main.tf").read_text(encoding="utf-8")
        if application is not None and (application / "deployment" / "tofu" / "main.tf").is_file()
        else ""
    )
    external_egress = [
        block
        for block in re.findall(r"egress\s*\{([^{}]*)\}", main_tf, flags=re.DOTALL)
        if '"0.0.0.0/0"' in block
    ]
    egress_is_limited = bool(external_egress) and all(
        re.search(r'protocol\s*=\s*"tcp"', block)
        and (from_port := re.search(r"from_port\s*=\s*(\d+)", block))
        and (to_port := re.search(r"to_port\s*=\s*(\d+)", block))
        and from_port.group(1) == to_port.group(1)
        and from_port.group(1) in {"80", "443"}
        for block in external_egress
    )
    allow_https_bootstrap = (
        resource_plan.get("provider") == "aws"
        and has_compute
        and has_registry
        and has_external_route
        and egress_is_limited
    )

    def address(node: dict, kind: str) -> str:
        """ResourcePlan node ID를 renderer와 같은 Terraform 주소로 바꾼다."""

        label = re.sub(r"[^a-zA-Z0-9_]", "_", str(node.get("id") or "resource"))
        if label[:1].isdigit():
            label = f"r_{label}"
        return f"{kind}.{label or 'resource'}"

    aws_security_groups = {
        address(node, "aws_security_group")
        for node in nodes
        if "aws_security_group" in (node.get("terraformTypes") or [])
    }
    public_aws_load_balancers = {
        address(node, "aws_lb")
        for node in nodes
        if "aws_lb" in (node.get("terraformTypes") or [])
        and (node.get("attributes") or {}).get("scheme") == "public"
    }
    direct_public_compute_refs = {
        str(interface.get("computeUnitRef") or "")
        for node in nodes
        if "google_compute_address" in (node.get("terraformTypes") or [])
        for interface in (node.get("attributes") or {}).get("interfaces") or []
        if isinstance(interface, dict)
        and interface.get("kind") == "publicIngress"
        and interface.get("ingressKind") == "directPublicIp"
        and interface.get("computeUnitRef")
    }
    direct_gcp_public_instances = {
        address(node, "google_compute_instance")
        for node in nodes
        if "google_compute_instance" in (node.get("terraformTypes") or [])
        and str(node.get("id") or "") in direct_public_compute_refs
    }

    details_by_issue: dict[str, list[dict]] = {}
    for item in findings or []:
        if not isinstance(item, dict) or not item.get("finding"):
            continue
        details_by_issue.setdefault(str(item["finding"]), []).append(item)

    blocking: list[str] = []
    allowed: list[dict[str, str]] = []
    for issue in issues:
        # 같은 rule이 한 파일의 여러 resource에서 같은 문장을 낼 수 있다. 문자열을
        # dict key 하나로 덮어쓰지 않고 Trivy가 반환한 순서대로 한 건씩 대응한다.
        matching_details = details_by_issue.get(issue) or []
        detail = matching_details.pop(0) if matching_details else {}
        rule_id = str(detail.get("ruleId") or "")
        target = str(detail.get("target") or "").replace("\\", "/").lstrip("/")
        resource = str(detail.get("resource") or "")
        is_generated_main = target.endswith("deployment/tofu/main.tf")
        if (
            allow_https_bootstrap
            and rule_id == "AWS-0104"
            and is_generated_main
            and resource in aws_security_groups
        ):
            allowed.append(
                {
                    "ruleId": "AWS-0104",
                    "finding": issue,
                    "target": target,
                    "resource": resource,
                    "condition": (
                        "AWS compute + ECR registry + explicit default route + "
                        "only TCP 80/443 external egress"
                    ),
                    "reason": (
                        "Generated hosts need outbound HTTP/HTTPS for bootstrap packages "
                        "and immutable container image pulls; all-protocol egress remains closed."
                    ),
                }
            )
        elif (
            resource_plan.get("provider") == "aws"
            and rule_id == "AWS-0053"
            and is_generated_main
            and resource in public_aws_load_balancers
        ):
            allowed.append(
                {
                    "ruleId": "AWS-0053",
                    "finding": issue,
                    "target": target,
                    "resource": resource,
                    "condition": "AWS Load Balancer with scheme=public",
                    "reason": (
                        "The selected topology explicitly exposes its managed load "
                        "balancer as the application's public ingress."
                    ),
                }
            )
        elif (
            resource_plan.get("provider") == "gcp"
            and rule_id == "GCP-0031"
            and is_generated_main
            and resource in direct_gcp_public_instances
        ):
            allowed.append(
                {
                    "ruleId": "GCP-0031",
                    "finding": issue,
                    "target": target,
                    "resource": resource,
                    "condition": "GCP publicIngress with ingressKind=directPublicIp",
                    "reason": (
                        "The selected standalone-VM topology explicitly uses a direct "
                        "public address as its ingress endpoint."
                    ),
                }
            )
        else:
            blocking.append(issue)
    return blocking, allowed


def _relative_application_path(application: Path, value: str) -> str | None:
    """검사 도구가 남긴 절대/상대 경로를 구현 snapshot 경로로 맞춘다."""
    normalized = value.replace("\\", "/").strip(" /`'\"")
    application_text = application.resolve().as_posix().rstrip("/")
    if normalized.startswith(application_text + "/"):
        normalized = normalized[len(application_text) + 1 :]
    # Docker Trivy는 애플리케이션을 /src에 mount한다.
    normalized = normalized.removeprefix("src/")
    if normalized.startswith("application/"):
        return normalized
    if normalized.startswith(("deployment/", "Dockerfile", "k8s/")):
        return f"application/{normalized}"
    if normalized.startswith(("tofu/", "runtime/", "scripts/")):
        return f"application/deployment/{normalized}"
    return None


def _package_targets(application: Path, package: dict) -> list[str]:
    """실패한 배포 검사 명령과 issue에서 실제 수정 후보 파일만 찾는다."""
    targets: set[str] = set()
    for issue in package.get("issues") or []:
        for match in _ISSUE_PATH.finditer(str(issue)):
            if target := _relative_application_path(application, match.group("path")):
                targets.add(target)
    deployment = application / "deployment"
    for command in package.get("commands") or []:
        if not isinstance(command, dict) or command.get("status") == "PASS":
            continue
        name = str(command.get("name") or "").lower()
        parts = [str(item) for item in command.get("command") or []]
        if "tofu" in name or "terraform" in name:
            targets.update(
                f"application/{path.relative_to(application).as_posix()}"
                for path in sorted((deployment / "tofu").glob("*.tf"))
            )
        elif "bash -n" in " ".join(parts).lower() and parts:
            candidate = deployment / "scripts" / Path(parts[-1]).name
            if candidate.is_file():
                targets.add(f"application/{candidate.relative_to(application).as_posix()}")
        elif "compose" in name:
            targets.add("application/deployment/runtime/compose.yaml")
        elif "cloud-init" in name:
            for candidate in (
                deployment / "tofu" / "cloud-init.yaml",
                deployment / "tofu" / "cloud-init.yaml.tftpl",
            ):
                if candidate.is_file():
                    targets.add(f"application/{candidate.relative_to(application).as_posix()}")
        elif any(token in " ".join(parts).lower() for token in ("powershell", "pwsh")):
            targets.update(
                f"application/{path.relative_to(application).as_posix()}"
                for path in sorted((deployment / "scripts").glob("*.ps1"))
            )
    return sorted(targets)


def _resource_plan(state: TestingState) -> dict:
    """Read the selected projection's final ResourcePlan from frozen deployment input."""
    raw = (state.get("testing_input") or {}).get("contract_artifacts") or {}
    deployment = raw.get("deployment") if isinstance(raw, dict) else None
    content = deployment.get("content") if isinstance(deployment, dict) else None
    if not isinstance(content, dict):
        return {}
    for key in ("resourcePlan", "resource_plan"):
        value = content.get(key)
        if isinstance(value, dict):
            return value
    projections = content.get("projections") or []
    selected = content.get("selectedTarget") or content.get("selected_target")

    selected_id = str(selected.get("id") or "") if isinstance(selected, dict) else ""
    selected_provider = (
        str(selected.get("provider") or selected.get("target") or "")
        if isinstance(selected, dict)
        else str(selected or "")
    )
    for projection in projections:
        if not isinstance(projection, dict):
            continue
        target = projection.get("target") or projection.get("provider") or projection.get("id")
        target_id = str(target.get("id") or "") if isinstance(target, dict) else ""
        target_provider = (
            str(target.get("provider") or target.get("target") or "")
            if isinstance(target, dict)
            else str(target or "")
        )
        # ID가 있으면 같은 provider의 다른 region 후보를 고르지 않도록 ID를 우선한다.
        # 이전 저장 형식처럼 ID가 없는 경우에만 provider 문자열로 비교한다.
        if selected_id and target_id != selected_id:
            continue
        if not selected_id and selected_provider and target_provider != selected_provider:
            continue
        for key in ("resourcePlan", "resource_plan"):
            value = projection.get(key)
            if isinstance(value, dict):
                return value
    return {}


def _demo_static_plan(state: TestingState) -> tuple[dict, dict, dict]:
    """Project real, not-run validation work for an explicit demo result.

    This reads the already materialized snapshot and frozen deployment profile;
    it never calls a scanner, shell, container, or package verifier.  Keeping
    the planned commands and concrete targets makes a PASS-only demo result
    expandable without claiming that a command produced output.
    """

    directory = str(state.get("application_dir") or "").strip()
    application = Path(directory) if directory else None
    deployment = application / "deployment" if application is not None else None
    targets: list[str] = []
    if application is not None and application.is_dir():
        for path in sorted(application.glob("Dockerfile")):
            targets.append(f"application/{path.relative_to(application).as_posix()}")
        if deployment is not None and deployment.is_dir():
            targets.extend(
                f"application/{path.relative_to(application).as_posix()}"
                for path in sorted(deployment.rglob("*"))
                if path.is_file()
            )
    targets = sorted(set(targets))
    trivy_command = {
        "name": "trivy config",
        "tool": "trivy",
        "command": [
            "trivy", "config", ".", "--format", "json", "--severity", "HIGH,CRITICAL",
            "--skip-check-update", "--disable-telemetry", "--skip-version-check", "--quiet",
        ],
        "profile": "config-high-critical",
        "planned": True,
    }
    package_commands: list[dict] = [
        {
            "name": "deployment package structure",
            "profile": "deployment-package",
            "planned": True,
            "targets": targets,
        }
    ]
    tofu = deployment / "tofu" if deployment is not None else None
    tofu_targets = [target for target in targets if "/deployment/tofu/" in target]
    tofu_commands: list[dict] = []
    if tofu is not None and tofu.is_dir():
        tofu_commands = [
            {"name": "tofu fmt", "command": ["tofu", "fmt", "-check", "-recursive"], "planned": True},
            {"name": "tofu init", "command": ["tofu", "init", "-backend=false", "-input=false", "-no-color"], "planned": True},
            {"name": "tofu validate", "command": ["tofu", "validate", "-no-color"], "planned": True},
        ]
        for path in sorted(tofu.glob("cloud-init_*.yaml.tftpl")):
            tofu_commands.append(
                {"name": "cloud-init schema", "command": ["cloud-init", "schema", "--config-file", path.relative_to(deployment).as_posix()], "planned": True}
            )
        for path in sorted(tofu.glob("bootstrap_*.sh.tftpl")):
            tofu_commands.append(
                {"name": "bash syntax", "command": ["bash", "-n", path.relative_to(deployment).as_posix()], "planned": True}
            )
    if deployment is not None and (deployment / "runtime" / "compose.yaml").is_file():
        package_commands.append(
            {"name": "docker compose config", "command": ["docker", "compose", "-f", "runtime/compose.yaml", "config"], "profile": "deployment-package", "planned": True}
        )
    package_commands.extend(tofu_commands)
    common = {
        "status": "PASSED",
        "gateStatus": "PASS",
        "issues": [],
        "validationSkipped": True,
        "validationSkipReason": "demo",
        "targets": targets,
        "resourcePlan": _resource_plan(state),
    }
    trivy = {
        **common,
        "tool": "trivy",
        "commands": [trivy_command],
        "checkNames": ["Trivy configuration scan"],
        "checkCounts": {"total": 1, "passed": 1, "failed": 0},
    }
    package = {
        **common,
        "targets": sorted(set(targets)),
        "commands": package_commands,
        "checkNames": [item["name"] for item in package_commands],
        "checkCounts": {"total": len(package_commands), "passed": len(package_commands), "failed": 0},
    }
    iac = {
        **common,
        "targets": tofu_targets,
        "commands": tofu_commands,
        "checkNames": [item["name"] for item in tofu_commands],
        "checkCounts": {"total": len(tofu_commands), "passed": len(tofu_commands), "failed": 0},
    }
    return trivy, package, iac


def _selected_gates(state: TestingState) -> set[str]:
    """이번 실행에서 실제 도구를 호출할 정적 gate를 반환한다."""

    scope = state.get("gate_scope")
    return set(scope) if scope is not None else {"static", "package", "iac"}


def _reused_report(value: object, state: TestingState) -> dict:
    """이전 보고서를 복사하고 새로 실행하지 않았음을 표시한다."""

    report = deepcopy(value) if isinstance(value, dict) else {}
    if not report:
        return {
            "status": "UNAVAILABLE",
            "gateStatus": "INCONCLUSIVE",
            "issues": ["A reusable report for the unchanged gate is unavailable."],
        }
    report["reused"] = True
    previous_job_id = str(state.get("previous_job_id") or "")
    if previous_job_id:
        report["reusedFromJobId"] = previous_job_id
    return report


def _previous_static_parts(state: TestingState) -> tuple[dict, dict, dict]:
    """이전 통합 보고서에서 Trivy, package, IaC 결과를 각각 꺼낸다."""

    reports = state.get("previous_reports") or {}
    previous_static = reports.get("static") if isinstance(reports, dict) else None
    previous_static = previous_static if isinstance(previous_static, dict) else {}
    trivy = previous_static.get("trivyScan") or previous_static
    package = previous_static.get("deploymentPackage") or {}
    iac = reports.get("iac") if isinstance(reports, dict) else None
    return (
        trivy if isinstance(trivy, dict) else {},
        package if isinstance(package, dict) else {},
        iac if isinstance(iac, dict) else {},
    )


def static_verification_node(state: TestingState) -> dict:
    """복원한 애플리케이션 전체에서 배포 설정 문제를 찾는다."""
    if state.get("validation_skipped"):
        trivy, package, iac = _demo_static_plan(state)
        return {
            "current_node": "static_verification",
            "errors": [],
            "static_report": {
                **trivy,
                "trivyScan": trivy,
                "deploymentPackage": package,
                "checkCounts": {"total": 2, "passed": 2, "failed": 0},
            },
            "iac_report": iac,
        }
    selected = _selected_gates(state)
    previous_trivy, previous_package, previous_iac = _previous_static_parts(state)
    if "static" in selected:
        emit_testing_progress(
            phase="static",
            scope="gate",
            status="RUNNING",
            label="Scanning deployment configuration",
            gate="static",
        )
        scanned = scan_stage(
            node="static_verification",
            directory=state.get("application_dir", ""),
            subject="deployment file",
            report_key="static_report",
        )
        report = scanned["static_report"]
    else:
        report = _reused_report(previous_trivy, state)
        emit_testing_progress(
            phase="static",
            scope="gate",
            status="REUSED",
            label="Reusing deployment configuration scan",
            gate="static",
        )
        scanned = {
            "current_node": "static_verification",
            "errors": [],
            "static_report": report,
        }
    resource_plan = _resource_plan(state)
    if "static" in selected:
        blocking_issues, allowed_findings = _review_trivy_findings(
            resource_plan,
            [str(item) for item in report.get("issues") or []],
            findings=[
                dict(item) for item in report.get("findings") or [] if isinstance(item, dict)
            ],
            application=Path(state.get("application_dir", "")),
        )
        if allowed_findings:
            report["issues"] = blocking_issues
            report["allowedFindings"] = allowed_findings
            if not blocking_issues and report.get("gateStatus") == "FAIL":
                report["status"] = "PASSED"
                report["gateStatus"] = "PASS"
                report["message"] = "Trivy config scan passed after reviewed topology exceptions."
    # Trivy 결과를 배포 package 검사와 합치기 전에 별도로 보존한다. 합친 summary만
    # 전달하면 수리 에이전트가 규칙 ID와 대상 파일을 잃고 다른 파일을 추측하게 된다.
    report["trivyScan"] = {
        key: report.get(key)
        for key in (
            "status",
            "gateStatus",
            "issues",
            "commands",
            "tool",
            "targets",
            "findings",
            "allowedFindings",
            "source",
            "message",
            "inputDigest",
            "reused",
            "reusedFromJobId",
        )
        if report.get(key) is not None
    }
    expected = state.get("deployment_package_expected")
    # Dockerfile도 DEPLOYMENT_FILE snapshot에 저장되지만 사용자 배포 패키지는 아니다.
    # 확정 ResourcePlan이 있거나 호출자가 명시적으로 요구한 경우에만 누락을 차단한다.
    if selected & {"package", "iac"}:
        checked_package = check_deployment_package(
            state.get("application_dir", ""),
            expected=bool(resource_plan) if expected is None else expected,
            resource_plan=resource_plan,
            include_plan=str(os.getenv("TESTING_IAC_PLAN") or "").lower()
            in {"1", "true", "yes", "on"},
            gate_scope=selected & {"package", "iac"},
        )
    else:
        checked_package = {}
    package = checked_package if "package" in selected else _reused_report(previous_package, state)
    # A package is part of the deployment gate only when it exists/was expected;
    # absent packages are represented as NOT_APPLICABLE by the package checker.
    report["deploymentPackage"] = package
    if "package" in selected:
        # top-level static 보고서는 Trivy와 package의 합계다. Trivy만 재사용했더라도
        # package를 새로 검사했다면 합계 전체를 재사용했다고 표시하면 안 된다.
        report.pop("reused", None)
        report.pop("reusedFromJobId", None)
    application = Path(state.get("application_dir", ""))
    package["targets"] = _package_targets(application, package)
    report["targets"] = sorted(
        {
            *[str(item) for item in report.get("targets") or []],
            *[str(item) for item in package.get("targets") or []],
        }
    )
    package_gate = str(package.get("gateStatus") or "").upper()
    report_gate = str(report.get("gateStatus") or "").upper()
    if package_gate == "INCONCLUSIVE" and report_gate in {"", "PASS"}:
        report["gateStatus"] = "INCONCLUSIVE"
        report["status"] = "UNAVAILABLE"
    elif package_gate == "FAIL":
        report["gateStatus"] = "FAIL"
        report["status"] = "FAILED"
    if package.get("issues"):
        report["issues"] = [*(report.get("issues") or []), *package["issues"]]
    scanned["errors"] = report.get("issues") or []
    if "iac" in selected:
        scanned["iac_report"] = checked_package.get("openTofu") or {
            "status": "SKIPPED",
            "gateStatus": "NOT_APPLICABLE",
            "issues": [],
            "source": {"source": "none", "directory": state.get("application_dir", "")},
        }
    else:
        scanned["iac_report"] = _reused_report(previous_iac, state)
    if isinstance(scanned["iac_report"], dict):
        scanned["iac_report"]["targets"] = sorted(
            f"application/{path.relative_to(application).as_posix()}"
            for path in (application / "deployment" / "tofu").glob("*.tf")
        )
    emit_testing_progress(
        phase="static",
        scope="gate",
        status=_progress_status(
            report["trivyScan"] if isinstance(report.get("trivyScan"), dict) else report
        ),
        label="Completed deployment configuration scan",
        gate="static",
    )
    if "package" not in selected:
        emit_testing_progress(
            phase="static",
            scope="gate",
            status=_progress_status(package),
            label="Reusing deployment package check",
            gate="package",
        )
    if "iac" not in selected:
        emit_testing_progress(
            phase="static",
            scope="gate",
            status=_progress_status(scanned["iac_report"]),
            label="Reusing infrastructure validation",
            gate="iac",
        )
    return scanned
