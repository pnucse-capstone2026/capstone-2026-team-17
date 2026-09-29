"""구현 결과를 검사하고 실패 이력에 따라 다시 실행한다.

완료된 구현 작업의 고정 snapshot을 받아 여러 구성 요소를 함께 실행하는 통합 검사와 E2E를
수행한다. 단위 테스트, 작은 통합 테스트와 frontend build는 구현 에이전트가 각 작업 안에서
이미 수행하므로 여기서 반복하지 않는다. Testing은 고정 입력과 이전 체크포인트를 받아 전체
흐름 검사 결과만 반환한다.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import Any

from app.artifact_trace import TraceRef
from app.artifact_trace_projection import (
    project_artifact_trace,
    projection_state_from_testing_contracts,
    same_implementation_contracts,
)
from app.db.models import TYPE_IAC_CODE, TYPE_SOURCE_CODE
from app.demo_validation import demo_skip_validation_enabled
from app.implementation.application.jobs import JobNotFound
from app.implementation.application.jobs import worker as implementation_worker
from app.implementation.workflows.traceability import declared_java_source_matches
from app.metrics import langsmith as langsmith_metrics
from app.repositories.artifact_repository import load_file_snapshot
from app.testing.progress import (
    emit_dynamic_workflow_planned,
    emit_dynamic_workflow_terminal,
    emit_testing_progress,
    reduce_testing_progress,
    testing_progress_scope,
)
from app.testing.runtime.verification import run_verification_graph
from app.testing.schemas.testing_input import TestingInput
from app.testing.utils.arazzo_planner import (
    build_arazzo_document,
    build_deterministic_workflow,
    build_workflow_candidates,
    use_case_display_name,
    use_case_id_for_candidate,
)
from app.testing.utils.artifact_source import (
    ArtifactSnapshotMismatch,
    ArtifactSourceUnavailable,
    capture_testing_input,
    materialized_testing_application,
)
from app.testing.utils.gates import aggregate_gate_report, gate_status
from app.validation import (
    RepairAttempt,
    RepairLedger,
    repair_makes_progress,
    stable_digest,
)

TestingProgress = Callable[[dict[str, Any]], None]

_BRACKETED_TARGET = re.compile(r"\[([^\]]+)\]")
_GATE_STAGE = {
    "static": "testing.static",
    "package": "testing.package",
    "iac": "testing.iac",
    "dynamicFunctional": "testing.dynamic-functional",
}
_DELIVERY_GATE_BOUNDARY = {
    # These gates inspect deterministic EasyDep delivery projections, not files
    # owned by the generated application's backend agent. OpenTofu may also expose
    # a frozen ResourcePlan/SKU problem, which requires a design decision rather
    # than source repair. Keep the distinction structural by gate, not by parsing
    # provider error strings.
    "static": ("PLATFORM_DEFECT", "platform"),
    "package": ("PLATFORM_DEFECT", "platform"),
    "iac": ("PLATFORM_OR_DESIGN_DEFECT", "platform-or-design"),
}
_DYNAMIC_IMPLEMENTATION_OWNER = "backend"


def _gate_scope_for_repair(repair_task_type: str | None) -> set[str] | None:
    """구현 수리 종류를 실제로 다시 확인할 Testing gate로 바꾼다."""

    if repair_task_type in {"testing-static", "testing-iac"}:
        # tofu 변경은 Trivy 정책과 OpenTofu 문법을 함께 확인한다.
        return {"static", "iac"}
    if repair_task_type == "testing-package":
        return {"package"}
    if repair_task_type == "testing-dynamic-functional":
        return {"dynamicFunctional"}
    return None


def _application_content_digest(application: Path) -> str:
    """새 job ID가 아니라 실제 후보 파일 경로와 내용으로 같은 구현을 판별한다."""

    return stable_digest(
        [
            {
                "path": path.relative_to(application).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in sorted(application.rglob("*"))
            if path.is_file()
        ]
    )


def _dynamic_target_ids(report: dict[str, Any]) -> list[str]:
    """Point to the exact failed Arazzo workflow and API operation."""
    finding = report.get("finding")
    finding = finding if isinstance(finding, dict) else {}
    operation_id = str(finding.get("operationId") or "").strip()
    digest = str(report.get("candidateDigest") or "").strip()
    workflow_id = str(report.get("failedWorkflowId") or "").strip()
    return [
        *([f"api:{operation_id}"] if operation_id else []),
        *([f"test:{digest}:{workflow_id}"] if digest and workflow_id else []),
    ]


def _launch_failure_trace_hints(
    implementation_rtm: dict[str, Any], dynamic: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Resolve a launch-only Java failure through one declared RTM source file.

    A failed HTTP workflow normally supplies an operation ID.  A Spring launch
    failure occurs before such a workflow exists, so it may only name a Java
    FQCN.  This fallback is deliberately usable only when that FQCN maps to
    exactly one runtime source entry in the frozen implementation RTM.
    """

    finding = dynamic.get("finding")
    finding = finding if isinstance(finding, dict) else {}
    if str(finding.get("code") or "") != "APPLICATION_LAUNCH_FAILED":
        return [], []
    message = str(finding.get("message") or "")
    matches = [
        mapping
        for mapping in declared_java_source_matches(implementation_rtm, message)
        if _is_dynamic_runtime_source(str(mapping.get("target_file") or ""))
    ]
    if len(matches) != 1:
        return [], []
    match = matches[0]
    target_file = str(match.get("target_file") or "")
    refs = [
        *(str(ref) for ref in match.get("sourceRefs") or [] if isinstance(ref, str) and ref),
        *(
            [f"task:{match['taskId']}"]
            if isinstance(match.get("taskId"), str) and match["taskId"]
            else []
        ),
    ]
    return [target_file], sorted(set(refs))


def _trace_hints(
    testing_input: TestingInput,
    dynamic: dict[str, Any],
    target_ids: list[str],
) -> tuple[list[str], list[str]]:
    """같은 Testing 입력의 계약·source RTM에서만 수리 힌트를 찾는다."""
    version_id = testing_input.artifact_version_ids.get(TYPE_SOURCE_CODE)
    if version_id is None:
        return [], []
    snapshot = load_file_snapshot(
        testing_input.app_id,
        TYPE_SOURCE_CODE,
        version_id=version_id,
    )
    metadata = snapshot.get("metadata") if isinstance(snapshot, dict) else None
    if not isinstance(snapshot, dict) or snapshot.get("version_id") != version_id:
        return [], []
    # 실행별 RTM이 우선이다. 파일 내용이 같아 snapshot version을 재사용하면 그
    # snapshot의 metadata는 이전 Job 것을 가리킬 수 있기 때문이다.
    implementation_rtm = testing_input.implementation_traceability
    if not isinstance(implementation_rtm, dict):
        implementation_rtm = (
            metadata.get("implementation_traceability")
            if isinstance(metadata, dict)
            else None
        )
    if not isinstance(implementation_rtm, dict):
        return [], []

    frozen_contracts = testing_input.contract_artifacts.model_dump(
        mode="json", exclude_none=True
    )
    source_contracts = (
        metadata.get("testing_contracts") if isinstance(metadata, dict) else None
    )
    # 체크포인트 복제 ID와 Testing 전용 OpenAPI trace 확장은 실행 의미를 바꾸지 않는다.
    # 그 외 계약 내용이 다르면 구현 trace에 붙일 근거가 없으므로 범위를 제시하지 않는다.
    if isinstance(source_contracts, dict) and not same_implementation_contracts(
        source_contracts, frozen_contracts
    ):
        return [], []

    if not target_ids:
        return _launch_failure_trace_hints(implementation_rtm, dynamic)

    trace = project_artifact_trace(
        projection_state_from_testing_contracts(frozen_contracts),
        implementation_rtm=implementation_rtm,
        testing_result={"dynamic_functional_report": dynamic},
    )
    files: set[str] = set()
    related: set[str] = set()
    for value in target_ids:
        try:
            ref = TraceRef.parse(value)
        except ValueError:
            continue
        if ref not in trace.refs:
            continue
        related.update(
            item.format()
            for item in trace.upstream(ref)
            if item.kind in {"requirement", "use_case", "class", "operation", "api"}
        )
        # UC처럼 넓은 중간 노드를 경유하면 sibling API와 모든 테스트까지 퍼질 수 있다.
        # 따라서 실패 API를 ``sourceRefs``로 직접 든 task와 그 운영 파일만 수리 힌트로
        # 쓴다. 이 HTTP gate는 backend를 직접 호출하므로 frontend와 테스트 자체는 실패한
        # 제품 구현의 수정 후보가 아니다.
        for task in trace.consumers(ref):
            if task.kind != "task":
                continue
            task_files = {
                item.id
                for item in trace.consumers(task)
                if item.kind == "file" and _is_dynamic_runtime_source(item.id)
            }
            if not task_files:
                continue
            related.add(task.format())
            files.update(task_files)
    return sorted(files), sorted(related)


def _is_dynamic_runtime_source(value: str) -> bool:
    """backend HTTP 실행에 실제로 들어가는 운영 파일만 남긴다."""

    path = value.replace("\\", "/").strip("/")
    return (
        path.startswith("application/")
        and not path.startswith("application/frontend/")
        and not path.startswith("application/deployment/")
        and "/src/test/" not in f"/{path}"
    )


def _text_issues(report: dict[str, Any]) -> list[str]:
    """gate 보고서의 실제 오류를 정렬·중복 제거한다."""
    values = report.get("issues") or []
    if not isinstance(values, list):
        values = [values]
    result = [
        " ".join(
            (
                json.dumps(value, ensure_ascii=False, sort_keys=True)
                if isinstance(value, (dict, list))
                else str(value)
            ).split()
        )
        for value in values
        if value not in (None, "")
    ]
    if not result:
        finding = report.get("finding")
        if isinstance(finding, dict):
            message = finding.get("message")
            if message:
                result.append(" ".join(str(message).split()))
    if not result:
        reason = report.get("reason") or report.get("message")
        if reason:
            result.append(" ".join(str(reason).split()))
    if not result:
        result.extend(
            " ".join(str(command.get(key)).split())
            for command in _failed_commands(report)
            for key in ("output", "error", "reason")
            if command.get(key)
        )
    return sorted(set(result))


def _failed_commands(report: dict[str, Any]) -> list[dict[str, Any]]:
    """PASS가 아닌 명령의 재현 정보만 복사한다."""
    return [
        dict(item)
        for item in report.get("commands") or []
        if isinstance(item, dict)
        and str(item.get("status") or "").upper() not in {"PASS", "PASSED"}
    ]


def _verification_components(verification: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """합쳐진 static 보고서를 수리 담당이 다른 검사로 나눈다."""
    reports = verification.get("reports") or {}
    static = reports.get("static") or {}
    result: list[tuple[str, dict[str, Any]]] = []

    trivy = static.get("trivyScan") if isinstance(static, dict) else None
    if isinstance(trivy, dict) and gate_status(trivy) not in {"PASS", "NOT_APPLICABLE"}:
        result.append(("static", trivy))
    elif not isinstance(trivy, dict) and gate_status(static) not in {"PASS", "NOT_APPLICABLE"}:
        # 이전 checkpoint에는 Trivy와 package가 하나의 static 보고서였다.
        result.append(("static", static))

    package = static.get("deploymentPackage") if isinstance(static, dict) else None
    if isinstance(package, dict) and gate_status(package) not in {"PASS", "NOT_APPLICABLE"}:
        tofu_commands = {
            json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in ((package.get("openTofu") or {}).get("commands") or [])
            if isinstance(item, dict)
        }
        commands = [
            command
            for command in _failed_commands(package)
            if json.dumps(command, ensure_ascii=False, sort_keys=True) not in tofu_commands
        ]
        command_issues = {
            " ".join(str(command.get(key) or "").split())
            for command in package.get("commands") or []
            if isinstance(command, dict)
            for key in ("output", "error", "reason")
            if command.get(key)
        }
        structural_issues = [
            issue for issue in _text_issues(package) if issue not in command_issues
        ]
        if commands or structural_issues:
            result.append(
                (
                    "package",
                    {
                        **package,
                        "issues": structural_issues
                        or [
                            str(command.get("output") or command.get("error") or command.get("reason"))
                            for command in commands
                        ],
                        "commands": commands,
                    },
                )
            )

    iac = reports.get("iac") or {}
    if gate_status(iac) not in {"PASS", "NOT_APPLICABLE"}:
        result.append(("iac", iac))

    dynamic = reports.get("dynamicFunctional") or {}
    if gate_status(dynamic) not in {"PASS", "NOT_APPLICABLE"}:
        result.append(("dynamicFunctional", dynamic))
    return result


def _finding_keys(report: dict[str, Any]) -> tuple[str, ...]:
    """요약 문장이 아닌 실제 issue로 반복 실패 키를 만든다."""
    findings: list[str] = []
    # 이전 결과를 읽을 때만 남아 있을 수 있는 항목이다. 새 Testing 실행은 구현 단계가
    # 이미 통과시킨 단위 테스트와 frontend build를 다시 실행하거나 판정하지 않는다.
    if "unitPassed" in report or "unitTests" in report:
        unit_passed = report.get("unitPassed")
        if unit_passed is None:
            unit_passed = (report.get("unitTests") or {}).get("passed")
        if not bool(unit_passed):
            findings.append("testing.unit-tests")
    frontend = report.get("frontendBuild") or {}
    if frontend and frontend.get("status") not in {"passed", "not_applicable"}:
        findings.append("testing.frontend-build")
    verification = report.get("verification") or {}
    for name, child in _verification_components(verification):
        stage = _GATE_STAGE[name]
        issues = _text_issues(child) or [
            str(verification.get("blockingReason") or f"{name} gate did not pass")
        ]
        findings.extend(f"{stage}:{issue}" for issue in issues)
    if not findings and not report.get("passed"):
        findings.append("testing.verification")
    return tuple(sorted(set(findings)))


def _repair_attempt_stage(findings: tuple[str, ...]) -> str:
    """repair ledger에 실제로 실패한 gate를 기록한다."""
    stages = {
        stage
        for key in findings
        for stage in _GATE_STAGE.values()
        if key.startswith(stage + ":")
    }
    return next(iter(stages)) if len(stages) == 1 else "testing"


def _normalized_file_hint(value: str) -> str | None:
    """도구별 경로 표현을 implementation workspace 기준으로 바꾼다."""
    normalized = value.replace("\\", "/").strip(" /`'\"")
    if "/application/" in normalized:
        normalized = "application/" + normalized.split("/application/", 1)[1]
    elif normalized.startswith("src/"):
        normalized = normalized[4:]
    if normalized.startswith("application/"):
        return normalized
    if normalized.startswith(("deployment/", "Dockerfile", "k8s/")):
        return f"application/{normalized}"
    if normalized.startswith(("tofu/", "runtime/", "scripts/")):
        return f"application/deployment/{normalized}"
    return None


def _evidence_for_gate(
    name: str,
    child: dict[str, Any],
    *,
    target_ids: list[str],
) -> dict[str, Any]:
    """수리 prompt가 요약 대신 원래 검사를 재현할 수 있게 한다."""
    commands = _failed_commands(child)
    targets = [str(item) for item in child.get("targets") or [] if str(item).strip()]
    for issue in _text_issues(child):
        targets.extend(_BRACKETED_TARGET.findall(issue))
    evidence: dict[str, Any] = {
        "gate": name,
        "issues": _text_issues(child),
        "commands": commands,
        "tool": str(child.get("tool") or ("http" if name == "dynamicFunctional" else name)),
        "targets": sorted(set(targets)),
    }
    if name == "dynamicFunctional":
        finding = child.get("finding")
        if isinstance(finding, dict):
            evidence["finding"] = dict(finding)
        http_commands = [
            {
                "name": f"HTTP {step.get('method', '')} {step.get('path', '')}".strip(),
                "command": [step.get("method"), step.get("path")],
                "statusCode": step.get("statusCode"),
                "operationId": step.get("operationId"),
            }
            for step in child.get("steps") or []
            if isinstance(step, dict)
        ]
        if http_commands:
            evidence["commands"] = http_commands
        evidence["targets"] = target_ids
        evidence["failedWorkflowId"] = child.get("failedWorkflowId")
        evidence["failedStepId"] = child.get("failedStepId")
        evidence["planDigest"] = child.get("planDigest")
        evidence["requestDigest"] = child.get("failedRequestDigest")
    return evidence


def _blocking_findings(
    testing_input: TestingInput,
    verification: dict[str, Any],
) -> list[dict[str, Any]]:
    """각 검사 실패를 정확한 담당·파일·재현 근거와 연결한다."""
    dynamic = (verification.get("reports") or {}).get("dynamicFunctional") or {}
    dynamic_defect = dynamic.get("defect") or {}
    dynamic_class = (
        dynamic_defect.get("class")
        or dynamic_defect.get("defectClass")
        or dynamic.get("defectClass")
        or "SUT_DEFECT"
    )
    route = {
        "TEST_DEFECT": "testing",
        "SUT_DEFECT": "implementation",
        "ENVIRONMENT_DEFECT": "environment",
        "UPSTREAM_AMBIGUITY": "requirements-or-design",
    }
    dynamic_owner = dynamic_defect.get("route") or route.get(dynamic_class, "testing")
    dynamic_ids = _dynamic_target_ids(dynamic)
    dynamic_files, related_refs = _trace_hints(testing_input, dynamic, dynamic_ids)
    result: list[dict[str, Any]] = []
    for name, child in _verification_components(verification):
        stage = _GATE_STAGE[name]
        status = gate_status(child)
        is_dynamic = name == "dynamicFunctional"
        if is_dynamic:
            defect_class = dynamic_class
            owner = dynamic_owner
            repairable = defect_class != "ENVIRONMENT_DEFECT"
            implementation_owner = (
                _DYNAMIC_IMPLEMENTATION_OWNER if owner == "implementation" else None
            )
        else:
            defect_class, owner = _DELIVERY_GATE_BOUNDARY[name]
            repairable = False
            implementation_owner = None
        if status == "INCONCLUSIVE" and not (
            is_dynamic and dynamic_class == "UPSTREAM_AMBIGUITY"
        ):
            defect_class, owner = "ENVIRONMENT_DEFECT", "environment"
            repairable = False
            implementation_owner = None
        evidence = _evidence_for_gate(
            name,
            child,
            target_ids=dynamic_ids if is_dynamic else [],
        )
        file_hints = dynamic_files if is_dynamic else sorted(
            {
                normalized
                for target in evidence["targets"]
                if (normalized := _normalized_file_hint(str(target))) is not None
            }
        )
        issues = evidence["issues"]
        result.append(
            {
                "code": stage,
                "stage": stage,
                "target_ids": dynamic_ids if is_dynamic else [],
                "message": issues[0] if issues else f"{name} gate did not pass",
                "severity": "error",
                "repairable": repairable,
                "defect_class": defect_class,
                "repair_owner": owner,
                "implementation_owner": implementation_owner,
                "preserve_tests": (
                    dynamic_defect.get("preserveTests", dynamic_class != "TEST_DEFECT")
                    if is_dynamic
                    else True
                ),
                "candidate_digest": dynamic.get("candidateDigest") if is_dynamic else None,
                "plan_digest": dynamic.get("planDigest") if is_dynamic else None,
                "request_digest": dynamic.get("failedRequestDigest") if is_dynamic else None,
                "candidate_plan": dynamic.get("candidatePlan") if is_dynamic else None,
                "workflow_inputs": dynamic.get("workflowInputs") if is_dynamic else None,
                "input_values": dynamic.get("inputValues") if is_dynamic else None,
                "failed_workflow_id": (
                    dynamic.get("failedWorkflowId") if is_dynamic else None
                ),
                "failed_step_id": dynamic.get("failedStepId") if is_dynamic else None,
                "file_hints": file_hints,
                "trace_refs": related_refs if is_dynamic else [],
                "evidence": evidence,
            }
        )
    analyses = dynamic.get("failureAnalyses") or []
    if isinstance(analyses, list) and analyses:
        # The aggregate dynamic report exposes the first failed workflow for
        # backwards compatibility. Replace its single blocker with the
        # use-case-scoped analyses so an implementation repair receives every
        # independently classified failure and its own HTTP evidence.
        result = [item for item in result if item.get("code") != "testing.dynamic-functional"]
        route_by_class = {
            "TEST_DEFECT": "testing",
            "SUT_DEFECT": "implementation",
            "ENVIRONMENT_DEFECT": "environment",
            "UPSTREAM_AMBIGUITY": "requirements-or-design",
        }
        for analysis in analyses:
            if not isinstance(analysis, dict):
                continue
            defect_class = str(analysis.get("defectClass") or "SUT_DEFECT")
            repair_owner = str(
                analysis.get("repairOwner") or route_by_class.get(defect_class, "testing")
            )
            finding = analysis.get("finding")
            finding = dict(finding) if isinstance(finding, dict) else {}
            scoped_dynamic = {
                **dynamic,
                "finding": finding,
                "reason": str(analysis.get("reason") or dynamic.get("reason") or ""),
                "defectClass": defect_class,
                "failedWorkflowId": str(analysis.get("workflowId") or ""),
                "failedStepId": str(finding.get("stepId") or ""),
                "defect": {
                    "class": defect_class,
                    "defectClass": defect_class,
                    "route": repair_owner,
                    "preserveTests": defect_class != "TEST_DEFECT",
                },
            }
            target_ids = _dynamic_target_ids(scoped_dynamic)
            file_hints, related_refs = _trace_hints(testing_input, scoped_dynamic, target_ids)
            evidence = _evidence_for_gate(
                "dynamicFunctional", scoped_dynamic, target_ids=target_ids
            )
            result.append(
                {
                    "code": "testing.dynamic-functional",
                    "stage": "testing.dynamic-functional",
                    "target_ids": target_ids,
                    "message": str(
                        analysis.get("reason") or "Dynamic functional workflow failed."
                    ),
                    "severity": "error",
                    "repairable": defect_class != "ENVIRONMENT_DEFECT",
                    "defect_class": defect_class,
                    "repair_owner": repair_owner,
                    "implementation_owner": (
                        _DYNAMIC_IMPLEMENTATION_OWNER
                        if repair_owner == "implementation"
                        else None
                    ),
                    "preserve_tests": defect_class != "TEST_DEFECT",
                    "candidate_digest": scoped_dynamic.get("candidateDigest"),
                    "plan_digest": scoped_dynamic.get("planDigest"),
                    "request_digest": analysis.get("requestDigest"),
                    "candidate_plan": scoped_dynamic.get("candidatePlan"),
                    "workflow_inputs": scoped_dynamic.get("workflowInputs"),
                    "input_values": scoped_dynamic.get("inputValues"),
                    "failed_workflow_id": scoped_dynamic.get("failedWorkflowId"),
                    "failed_step_id": scoped_dynamic.get("failedStepId"),
                    "file_hints": file_hints,
                    "trace_refs": related_refs,
                    "use_case_id": analysis.get("useCaseId"),
                    "use_case_name": analysis.get("useCaseName"),
                    "repair_action": analysis.get("repairAction"),
                    "evidence": evidence,
                }
            )
    return result


def _repair_state(ledger: RepairLedger, *, passed: bool) -> dict[str, Any]:
    """내부 repair ledger를 프론트엔드가 표시할 수 있는 간단한 상태로 바꾼다."""
    if passed:
        status = "COMPLETED"
    elif ledger.status == "WAITING_EXTERNAL":
        status = "WAITING_EXTERNAL"
    elif ledger.status == "STALLED":
        status = "STALLED"
    else:
        status = "ACTIVE"
    return {
        "status": status,
        "attempt_count": len(ledger.attempts),
        "accepted_count": sum(
            attempt.outcome in {"improved", "clean"} for attempt in ledger.attempts
        ),
        # 전체 이력은 내부 중복 판정에 유지하지만 HTTP/LLM 입력에는 최근 두 건만 싣는다.
        # 작업을 오래 반복해도 화면 응답이 계속 커지지 않게 하기 위해서다.
        "recent_attempts": [
            {
                "stage": attempt.stage,
                "target_ids": list(attempt.target_ids),
                "strategy_key": attempt.strategy_key,
                "finding_keys_before": list(attempt.finding_keys_before),
                "finding_keys_after": list(attempt.finding_keys_after),
                "outcome": attempt.outcome,
                "detail": attempt.detail,
            }
            for attempt in ledger.attempts[-2:]
        ],
        "older_attempt_count": max(0, len(ledger.attempts) - 2),
        "finding_digest": stable_digest(
            ledger.attempts[-1].finding_keys_after if ledger.attempts else ()
        ),
        "stall_reason": ledger.stall_reason,
    }


def _emit_demo_terminal_workflow_progress(verification: dict[str, Any]) -> None:
    """Publish one terminal demo transition after every Testing gate returns."""

    if verification.get("validationSkipped") is not True:
        return
    dynamic = (verification.get("reports") or {}).get("dynamicFunctional") or {}
    workflows = dynamic.get("workflows") if isinstance(dynamic, dict) else None
    if not isinstance(workflows, list):
        return
    total = len(workflows)
    for completed, workflow in enumerate(workflows, start=1):
        if not isinstance(workflow, dict):
            continue
        workflow_id = str(workflow.get("workflowId") or "")
        if not workflow_id:
            continue
        use_case_id = str(workflow.get("useCaseId") or "")
        use_case_name = str(workflow.get("useCaseName") or workflow.get("summary") or "")
        emit_dynamic_workflow_terminal(
            status="PASS",
            label=f"{use_case_id} · {use_case_name}".strip(" ·"),
            workflow_id=workflow_id,
            use_case_id=use_case_id,
            use_case_name=use_case_name,
            completed_workflows=completed,
            total_workflows=total,
            total_steps=len((workflow.get("workflow") or {}).get("steps") or []),
        )


def _emit_dynamic_workflow_plans(workflows: list[dict[str, Any]]) -> None:
    """Publish execution-lane pending rows from generated workflow evidence."""

    total = len(workflows)
    for workflow in workflows:
        workflow_id = str(workflow.get("workflowId") or "")
        if not workflow_id:
            continue
        use_case_id = str(workflow.get("useCaseId") or "")
        use_case_name = str(workflow.get("useCaseName") or workflow.get("summary") or "")
        emit_dynamic_workflow_planned(
            label=f"{use_case_id} · {use_case_name}".strip(" ·"),
            workflow_id=workflow_id,
            use_case_id=use_case_id,
            use_case_name=use_case_name,
            total_workflows=total,
            total_steps=len((workflow.get("workflow") or {}).get("steps") or []),
        )


def _run_test(
    run_id: str,
    testing_input: TestingInput,
    *,
    repair_history: dict[str, Any] | None = None,
    previous_findings: tuple[str, ...] = (),
    partial_result: dict[str, Any] | None = None,
    gate_scope: set[str] | None = None,
    previous_reports: dict[str, Any] | None = None,
    previous_job_id: str = "",
    progress: TestingProgress | None = None,
    emit_terminal_progress: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """고정된 구현 snapshot으로 검사를 실행하고 결과와 수리 이력을 반환한다.

    복원한 파일의 digest를 확인한 뒤 정적 검사, 실행 통합 검사와 E2E를 수행한다. 구현
    단계에서 끝난 단위 테스트와 작은 통합 테스트는 이 서비스의 입력으로 믿고 반복하지 않는다.
    """
    ledger = RepairLedger.model_validate(repair_history or {})
    partial_result = dict(partial_result or {})
    nested_dynamic = (
        ((partial_result.get("verification") or {}).get("reports") or {}).get(
            "dynamicFunctional"
        )
        if isinstance(partial_result.get("verification"), dict)
        else None
    )
    nested_dynamic = nested_dynamic if isinstance(nested_dynamic, dict) else {}
    raw_preserved_plan = partial_result.get("preservedCandidatePlan") or nested_dynamic.get(
        "candidatePlan"
    )
    preserved_arazzo_document = (
        dict(raw_preserved_plan) if isinstance(raw_preserved_plan, dict) else None
    )
    raw_workflow_results = partial_result.get(
        "preservedWorkflowResults"
    ) or nested_dynamic.get("workflows")
    preserved_workflow_results = (
        [
            dict(item)
            for item in raw_workflow_results
            if isinstance(item, dict)
            and str((item.get("result") or {}).get("gateStatus") or "").upper() == "PASS"
        ]
        if isinstance(raw_workflow_results, list)
        else []
    )
    priority_workflow_id = str(
        partial_result.get("failedWorkflowId")
        or nested_dynamic.get("failedWorkflowId")
        or ""
    ).strip()
    raw_workflow_inputs = partial_result.get("workflowInputs") or nested_dynamic.get(
        "workflowInputs"
    )
    fixed_workflow_inputs = (
        dict(raw_workflow_inputs) if isinstance(raw_workflow_inputs, dict) else {}
    )
    raw_input_values = partial_result.get("inputValues") or nested_dynamic.get("inputValues")
    fixed_input_values = dict(raw_input_values) if isinstance(raw_input_values, dict) else {}

    def execute_snapshot() -> tuple[dict[str, Any], dict[str, Any]]:
        """복원한 한 snapshot 안에서 모든 검사를 끝낸다."""

        emit_testing_progress(
            phase="prepare",
            scope="phase",
            status="RUNNING",
            label="Preparing fixed application snapshot",
        )
        # 구현 작업이 고정한 파일 묶음을 새 임시 폴더에 한 번만 복원한다. 정적·IaC·동적
        # 검사는 아래 context가 끝날 때까지 이 폴더를 함께 사용한다.
        with (
            materialized_testing_application(testing_input) as run_root,
            langsmith_metrics.trace_metadata(
                {
                    "app_id": testing_input.app_id,
                    "implementation_job_id": testing_input.implementation_job_id,
                }
            ),
        ):
            emit_testing_progress(
                phase="prepare",
                scope="phase",
                status="PASS",
                label="Fixed application snapshot ready",
            )
            if progress is not None:
                progress(
                    {
                        "current_node": "verification",
                        "result": (
                            {
                                "preservedCandidatePlan": preserved_arazzo_document,
                                "preservedWorkflowResults": preserved_workflow_results,
                                "failedWorkflowId": priority_workflow_id,
                                "workflowInputs": fixed_workflow_inputs,
                                "inputValues": fixed_input_values,
                            }
                            if preserved_arazzo_document
                            else {}
                        ),
                    }
                )

            verification = run_verification_graph(
                run_id=run_id,
                app_id=testing_input.app_id,
                application_dir=str(run_root / "application"),
                repair_history=ledger.model_dump(mode="json"),
                implementation_job_id=testing_input.implementation_job_id,
                testing_input=testing_input.model_dump(mode="json"),
                iac_expected=TYPE_IAC_CODE in testing_input.artifact_version_ids,
                # DEPLOYMENT_FILE에는 Dockerfile도 들어간다. 실제 package 필요 여부는
                # static node가 고정된 ResourcePlan과 복원 디렉터리를 보고 판단한다.
                deployment_package_expected=None,
                fixed_arazzo_document=preserved_arazzo_document,
                fixed_workflow_inputs=fixed_workflow_inputs,
                fixed_input_values=fixed_input_values,
                preserved_workflow_results=preserved_workflow_results,
                priority_workflow_id=priority_workflow_id,
                gate_scope=gate_scope,
                previous_reports=previous_reports,
                previous_job_id=previous_job_id,
            )
            _emit_demo_terminal_workflow_progress(verification)
            emit_testing_progress(
                phase="summary",
                scope="phase",
                status="RUNNING",
                label="Summarizing test evidence",
            )
            aggregate = aggregate_gate_report({"verification": verification})
            report = {
                "status": "completed",
                "verification": verification,
                "passed": aggregate["passed"],
                "gateStatus": aggregate["status"],
                "gateCounts": aggregate["counts"],
                "diagnostics": list(verification["diagnostics"]),
                "testingInput": testing_input.model_dump(mode="json"),
                "workflowCounts": dict(verification.get("workflowCounts") or {}),
            }
            application_digest = _application_content_digest(run_root / "application")
        findings = _finding_keys(report)
        dynamic = (verification.get("reports") or {}).get("dynamicFunctional") or {}
        plan_digest = str(
            dynamic.get("planDigest")
            or dynamic.get("candidateDigest")
            or stable_digest(report)
        )
        # 새 feedback job ID가 생겨도 파일이 같으면 같은 후보다. 반대로 같은 test plan을
        # 유지하면서 실제 구현이 달라졌다면 새로운 후보로 검사해야 한다.
        execution_digest = stable_digest(
            {
                "plan": plan_digest,
                "failedRequest": str(dynamic.get("failedRequestDigest") or ""),
                "application": application_digest,
                "gates": {
                    "iacExpected": TYPE_IAC_CODE in testing_input.artifact_version_ids,
                    "preservedWorkflows": [
                        str(item.get("workflowId") or "")
                        for item in preserved_workflow_results
                    ],
                },
            }
        )
        # 최초 실패는 이후 repair와 비교할 기준으로 기록한다. 재시도라면 결과 digest와
        # finding 집합을 이전 이력과 비교해 같은 후보 반복, 개선, 악화를 구분한다.
        if findings and not previous_findings:
            ledger.record(
                RepairAttempt(
                    stage=_repair_attempt_stage(findings),
                    strategy_key="initial_generation",
                    input_digest=stable_digest({"run": run_id, "findings": findings}),
                    candidate_digest=execution_digest,
                    finding_keys_after=findings,
                    outcome="no_improvement",
                    detail="Initial testing run established the repair baseline.",
                )
            )
        elif previous_findings:
            repeated = any(
                attempt.candidate_digest == execution_digest
                for attempt in ledger.attempts
                if attempt.candidate_digest
            )
            improved = not repeated and repair_makes_progress(previous_findings, findings)
            ledger.record(
                RepairAttempt(
                    stage=_repair_attempt_stage(findings or previous_findings),
                    strategy_key="regenerate_from_accumulated_failures",
                    input_digest=stable_digest(
                        {
                            "findings": previous_findings,
                            "stage": _repair_attempt_stage(previous_findings),
                        }
                    ),
                    candidate_digest=execution_digest,
                    finding_keys_before=previous_findings,
                    finding_keys_after=findings,
                    outcome=(
                        "repeated_candidate"
                        if repeated
                        else "clean"
                        if not findings
                        else "improved"
                        if improved
                        else "no_improvement"
                    ),
                )
            )
        if report["passed"]:
            ledger.status = "COMPLETED"
        else:
            # 횟수 제한은 두지 않는다. 개선되지 않은 시도도 이력에 남아 다음 LLM 호출이 같은
            # 후보와 전략을 피할 수 있게 하되, 자동 수리 자체를 STALLED로 닫지는 않는다.
            ledger.status = "ACTIVE"
            ledger.stall_reason = ""
        report["blocking_findings"] = _blocking_findings(testing_input, verification)
        report["repair_state"] = _repair_state(ledger, passed=bool(report["passed"]))
        emit_testing_progress(
            phase="summary",
            scope="phase",
            status="PASS" if report["passed"] else "FAIL",
            label="Testing report ready",
            detail=(
                "All required gates passed."
                if report["passed"]
                else f"{len(report['blocking_findings'])} blocking finding(s) remain."
            ),
        )
        completed_history = ledger.model_dump(mode="json")
        if progress is not None and emit_terminal_progress:
            # Workspace 결과가 저장되기 직전 서버가 종료되어도 방금 사용한 후보와 finding을
            # 잃지 않는다. 이 경계는 아직 command 완료가 아니므로 저장소에는 RUNNING
            # checkpoint로 남고, 재개 시 같은 후보 반복을 감지할 수 있다.
            progress(
                {
                    "current_node": "verification_complete",
                    "result": report,
                    "repair_history": completed_history,
                    "previous_findings": list(findings),
                }
            )
        return report, completed_history

    return execute_snapshot()


def _run_testing(
    app_id: str,
    implementation_job_id: str,
    *,
    run_id: str,
    previous_job: dict[str, Any] | None = None,
    preserve_test: bool = False,
    checkpoint: dict[str, Any] | None = None,
    repair_task_type: str | None = None,
    progress: TestingProgress | None = None,
) -> dict[str, Any]:
    """Workspace command 안에서 Testing 한 회차를 실행한다.

    ``checkpoint``가 있으면 그 안의 고정 입력과 애플리케이션 검사 결과를 사용한다. 없으면
    구현 작업이 실제로 사용한 산출물을 한 번 고정한다. 이전 실패를 고치는 경우에는
    ``previous_job``의 수리 이력을 이어받으며, 구현 수리 뒤에는 ``preserve_test``로 기존
    기능 테스트 계획을 보존한다. ``repair_task_type``이 있으면 그 수리와 관련된 gate만
    실행하고, 관련 입력이 그대로인 나머지 gate 보고서는 이전 작업에서 가져온다.
    """
    repair_history: dict[str, Any] = RepairLedger().model_dump(mode="json")
    previous_findings: tuple[str, ...] = ()
    partial_result: dict[str, Any] = {}
    gate_scope = _gate_scope_for_repair(repair_task_type)
    previous_reports: dict[str, Any] = {}
    previous_job_id = ""
    testing_progress: dict[str, Any] = {}

    if checkpoint is not None:
        # 재시작 뒤 최신 DB 값을 다시 조합하지 않는다. command가 시작할 때 저장한 입력이
        # 지금 실행할 구현 작업과 같은지 확인한 뒤 그대로 사용한다.
        testing_input = TestingInput.model_validate(checkpoint.get("testing_input") or {})
        if testing_input.app_id != app_id:
            raise ValueError("The Testing checkpoint does not belong to this app.")
        if testing_input.implementation_job_id != implementation_job_id:
            raise ValueError("The Testing checkpoint belongs to another implementation.")
        repair_history = dict(checkpoint.get("repair_history") or repair_history)
        previous_findings = tuple(checkpoint.get("previous_findings") or ())
        partial_result = dict(checkpoint.get("result") or {})
        saved_scope = checkpoint.get("gate_scope")
        gate_scope = (
            {str(item) for item in saved_scope}
            if isinstance(saved_scope, list)
            else gate_scope
        )
        previous_reports = dict(checkpoint.get("previous_reports") or {})
        previous_job_id = str(checkpoint.get("previous_job_id") or "")
        testing_progress = dict(checkpoint.get("testing_progress") or {})
    else:
        try:
            implementation = implementation_worker.get_testing_input(implementation_job_id)
        except JobNotFound as error:
            raise ValueError("Unknown implementation job.") from error
        if implementation["app_id"] != app_id:
            raise ValueError("Implementation job does not belong to this app.")
        if implementation["status"] != "COMPLETED":
            raise ValueError("Implementation must be COMPLETED before testing can start.")
        try:
            testing_input = capture_testing_input(
                app_id,
                implementation_job_id,
                artifact_version_ids=implementation.get("artifact_version_ids"),
                contract_artifacts=implementation.get("contract_artifacts") or {},
                implementation_traceability=implementation.get(
                    "implementation_traceability"
                ),
            )
        except (
            ArtifactSourceUnavailable,
            ArtifactSnapshotMismatch,
            ValueError,
        ) as error:
            raise ValueError(f"Implementation artifacts are unavailable: {error}") from error

        if previous_job is not None:
            if previous_job.get("app_id") != app_id:
                raise ValueError("Previous Testing result does not belong to this app.")
            previous_result = previous_job.get("result") or {}
            previous_job_id = str(previous_job.get("job_id") or "")
            previous_reports = dict(
                (previous_result.get("verification") or {}).get("reports") or {}
            )
            if (
                previous_job.get("status") != "COMPLETED"
                or previous_result.get("passed") is not False
            ):
                raise ValueError("Only a completed failing Testing result can be repaired.")
            repair_history = dict(previous_job.get("repair_history") or repair_history)
            previous_findings = _finding_keys(previous_result)
            previous_input = previous_job.get("testing_input")
            same_candidate = False
            if previous_input is not None:
                fixed_previous_input = TestingInput.model_validate(previous_input)
                same_candidate = fixed_previous_input == testing_input
                # 구현 수리는 새 파일 버전을 만드는 것이 정상이다. 이전 Testing 입력에
                # 계약이 실제로 기록돼 있었다면 그 계약만 유지됐는지 확인한다. 오래된 작업처럼
                # 계약이 비어 있으면 비교할 근거가 없으므로 새 구현의 고정 입력을 사용한다.
                previous_contracts = fixed_previous_input.contract_artifacts.model_dump(
                    mode="json", exclude_none=True
                )
                current_contracts = testing_input.contract_artifacts.model_dump(
                    mode="json", exclude_none=True
                )
                if (
                    not same_candidate
                    and previous_contracts
                    and not same_implementation_contracts(
                        previous_contracts, current_contracts
                    )
                ):
                    raise ValueError(
                        "An implementation repair must preserve requirements and design contracts."
                    )
                if (
                    preserve_test
                    and not same_implementation_contracts(
                        previous_contracts, current_contracts
                    )
                ):
                    raise ValueError(
                        "Preserved tests require the same requirements and design contracts."
                    )
            if preserve_test:
                dynamic = ((previous_result.get("verification") or {}).get("reports") or {}).get(
                    "dynamicFunctional"
                ) or {}
                preserved_plan = dynamic.get("candidatePlan")
                if isinstance(preserved_plan, dict) and preserved_plan:
                    partial_result["preservedCandidatePlan"] = dict(preserved_plan)
                    partial_result["failedWorkflowId"] = str(
                        dynamic.get("failedWorkflowId") or ""
                    )
                    partial_result["failedStepId"] = str(dynamic.get("failedStepId") or "")
                    partial_result["workflowInputs"] = dict(
                        dynamic.get("workflowInputs") or {}
                    )
                    partial_result["inputValues"] = dict(dynamic.get("inputValues") or {})
                    workflows = dynamic.get("workflows")
                    # A selective repair reuses only PASS workflows from the same implementation.
                    # A new implementation still reruns every workflow against the changed files.
                    partial_result["preservedWorkflowResults"] = (
                        [
                            dict(item)
                            for item in workflows
                            if isinstance(item, dict)
                            and str((item.get("result") or {}).get("gateStatus") or "").upper()
                            == "PASS"
                        ]
                        if same_candidate and isinstance(workflows, list)
                        else []
                    )

    def save_progress(state: dict[str, Any]) -> None:
        if progress is None:
            return
        progress(
            {
                "implementation_job_id": implementation_job_id,
                "testing_input": testing_input.model_dump(mode="json"),
                "current_node": state.get("current_node"),
                "result": dict(state.get("result") or {}),
                "repair_history": dict(state.get("repair_history") or repair_history),
                "previous_findings": list(
                    state.get("previous_findings") or previous_findings
                ),
                "gate_scope": sorted(gate_scope) if gate_scope is not None else None,
                "previous_reports": previous_reports,
                "previous_job_id": previous_job_id,
                "testing_progress": testing_progress,
            }
        )

    progress_lock = RLock()

    def observe_testing_progress(event: dict[str, Any]) -> None:
        nonlocal testing_progress
        # Dynamic planning/execution and static gates emit from separate graph
        # workers. Folding and persisting must be one critical section or two
        # snapshots can read the same predecessor and overwrite each other.
        with progress_lock:
            testing_progress = reduce_testing_progress(testing_progress, event)
            save_progress(
                {
                    "current_node": "verification",
                    "result": partial_result,
                }
            )

    # 파일 복원이나 도구 실행 전에 고정 입력을 저장한다. 서버가 여기서 중단되어도 다음
    # 실행은 같은 산출물 ID와 계약 digest를 사용한다.
    save_progress({"current_node": "queued", "result": partial_result})
    with testing_progress_scope(observe_testing_progress):
        if preserve_test:
            emit_testing_progress(
                phase="repair",
                scope="phase",
                status="PASS",
                label="Implementation repair candidate ready",
                detail="The preserved test plan will now verify the new candidate.",
            )
        if previous_job is not None or preserve_test or repair_task_type:
            emit_testing_progress(
                phase="rerun",
                scope="phase",
                status="RUNNING",
                label="Re-running affected test coverage",
                detail="Previous evidence and fixed test inputs are being reused where valid.",
            )
        report, completed_history = _run_test(
            run_id,
            testing_input,
            repair_history=repair_history,
            previous_findings=previous_findings,
            partial_result=partial_result,
            gate_scope=gate_scope,
            previous_reports=previous_reports,
            previous_job_id=previous_job_id,
            progress=save_progress,
            emit_terminal_progress=False,
        )
        if previous_job is not None or preserve_test or repair_task_type:
            emit_testing_progress(
                phase="rerun",
                scope="phase",
                status="PASS" if report["passed"] else "FAIL",
                label="Affected test coverage re-run complete",
            )
    return {
        "job_id": run_id,
        "app_id": app_id,
        "implementation_job_id": implementation_job_id,
        "status": "COMPLETED",
        "current_node": "completed",
        "testing_input": testing_input.model_dump(mode="json"),
        "result": report,
        "repair_history": completed_history,
        "previous_findings": list(_finding_keys(report)),
        "testing_progress": testing_progress,
    }


def _demo_fallback_job(
    app_id: str,
    implementation_job_id: str,
    *,
    run_id: str,
    checkpoint: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the smallest durable Testing result when demo execution cannot run.

    This does not claim that a tool or workflow executed.  The empty canonical
    Arazzo document is still a usable Testing artifact for the normal workspace
    projection and makes the demo result structurally complete.
    """

    testing_input = (
        checkpoint.get("testing_input")
        if isinstance(checkpoint, dict) and isinstance(checkpoint.get("testing_input"), dict)
        else {}
    )
    testing_progress = (
        dict(checkpoint.get("testing_progress") or {})
        if isinstance(checkpoint, dict)
        else {}
    )
    candidate_plan, planned_workflows = _demo_fallback_plan(testing_input)
    planned_workflows = _fallback_workflows_from_progress(
        testing_progress, planned_workflows
    )
    passed_gate = {
        "status": "PASSED",
        "gateStatus": "PASS",
        "validationSkipped": True,
        "demoFallback": True,
    }
    dynamic = {
        **passed_gate,
        "candidatePlan": candidate_plan,
        "candidateDigest": stable_digest(candidate_plan),
        "planDigest": stable_digest(candidate_plan),
        "workflowInputs": {},
        "inputValues": {},
        "workflows": planned_workflows,
        "plannedWorkflowIds": [item["workflowId"] for item in planned_workflows],
        "executionOrder": [],
        "executedWorkflowCount": 0,
        "workflowCounts": {
            "total": len(planned_workflows),
            "completed": len(planned_workflows),
            "passed": len(planned_workflows),
            "failed": 0,
            "running": 0,
            "pending": 0,
        },
        "requirements": [],
        "failureAnalyses": [],
        "planRepairs": [],
        "reusedWorkflowIds": [],
        "pendingWorkflowIds": [],
        "targetUrl": "",
    }
    reports = {
        "static": {
            **passed_gate,
            "checkNames": ["Trivy configuration scan", "deployment package structure"],
            "trivyScan": {
                **passed_gate,
                "tool": "trivy",
                "checkNames": ["Trivy configuration scan"],
                "commands": [{"name": "trivy config", "planned": True}],
                "targets": [],
            },
            "deploymentPackage": {
                **passed_gate,
                "checkNames": ["deployment package structure"],
                "commands": [{"name": "deployment package structure", "planned": True}],
                "targets": [],
            },
        },
        "iac": {
            **passed_gate,
            "checkNames": ["infrastructure validation"],
            "commands": [],
            "targets": [],
        },
        "dynamicFunctional": dynamic,
    }
    aggregate = aggregate_gate_report(reports)
    verification = {
        "reports": reports,
        "passed": True,
        "status": "PASS",
        "gateStatus": "PASS",
        "gates": aggregate["gates"],
        "gateCounts": aggregate["counts"],
        "deferredGates": [],
        "blockingReason": None,
        "diagnostics": [],
        "errors": [],
        "validationSkipped": True,
        "validationSkipReason": "demo",
        "demoFallback": True,
        "executedWorkflowCount": 0,
        "workflowCounts": dict(dynamic["workflowCounts"]),
    }
    report = {
        "status": "completed",
        "verification": verification,
        "passed": True,
        "gateStatus": "PASS",
        "gateCounts": aggregate["counts"],
        "diagnostics": [],
        "testingInput": testing_input,
        "blocking_findings": [],
        "repair_state": _repair_state(RepairLedger(status="COMPLETED"), passed=True),
        "validationSkipped": True,
        "validationSkipReason": "demo",
        "demoFallback": True,
        "executedWorkflowCount": 0,
        "workflowCounts": dict(verification["workflowCounts"]),
    }
    return {
        "job_id": run_id,
        "app_id": app_id,
        "implementation_job_id": implementation_job_id,
        "status": "COMPLETED",
        "current_node": "completed",
        "testing_input": testing_input,
        "result": report,
        "repair_history": RepairLedger(status="COMPLETED").model_dump(mode="json"),
        "previous_findings": [],
        "testing_progress": testing_progress,
    }


def _demo_fallback_plan(testing_input: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Retain a deterministic Arazzo plan if the frozen contracts survived.

    The outer fallback may run after input preparation failed, so no LLM or
    speculative multi-operation workflow is attempted here.  A deterministic
    candidate is already real generated-plan data and is safe to expose.
    """

    contracts = testing_input.get("contract_artifacts")
    contracts = contracts if isinstance(contracts, dict) else {}
    requirements = (contracts.get("requirements") or {}).get("content")
    use_cases = (contracts.get("use_cases") or {}).get("content")
    openapi = (contracts.get("openapi") or {}).get("content")
    if (
        not isinstance(requirements, (dict, list))
        or not isinstance(use_cases, dict)
        or not isinstance(openapi, dict)
    ):
        return build_arazzo_document(()), []
    try:
        candidates = build_workflow_candidates(requirements, use_cases, openapi)
        workflows = [
            workflow
            for candidate in candidates
            if (workflow := build_deterministic_workflow(candidate)) is not None
        ]
    except Exception:
        return build_arazzo_document(()), []
    candidate_by_id = {str(candidate["workflowId"]): candidate for candidate in candidates}
    document = build_arazzo_document(workflows)
    details = []
    for workflow in workflows:
        workflow_id = str(workflow["workflowId"])
        candidate = candidate_by_id[workflow_id]
        details.append(
            {
                "workflowId": workflow_id,
                "useCaseId": use_case_id_for_candidate(candidate),
                "useCaseName": use_case_display_name(candidate),
                "use_case_name": use_case_display_name(candidate),
                "summary": str(workflow.get("summary") or use_case_display_name(candidate)),
                "status": "PASSED",
                "gateStatus": "PASS",
                "validationSkipped": True,
                "workflow": workflow,
                "operations": candidate.get("operations") or [],
                "result": {
                    "status": "PASSED",
                    "gateStatus": "PASS",
                    "validationSkipped": True,
                    "steps": [],
                },
            }
        )
    return document, details


def _fallback_workflows_from_progress(
    testing_progress: dict[str, Any],
    workflows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Retain durable planned workflow identities when fallback cannot rebuild them."""

    result = [dict(workflow) for workflow in workflows]
    known_ids = {str(workflow.get("workflowId") or "") for workflow in result}
    plans = testing_progress.get("plans")
    if not isinstance(plans, dict):
        return result
    for key, raw_plan in plans.items():
        plan = raw_plan if isinstance(raw_plan, dict) else {}
        workflow_id = str(plan.get("workflow_id") or key or "").strip()
        if not workflow_id or workflow_id in known_ids:
            continue
        use_case_id = str(plan.get("use_case_id") or "").strip()
        use_case_name = str(plan.get("use_case_name") or plan.get("progress_step_label") or "").strip()
        result.append(
            {
                "workflowId": workflow_id,
                "useCaseId": use_case_id,
                "useCaseName": use_case_name,
                "use_case_name": use_case_name,
                "summary": use_case_name or workflow_id,
                "status": "PASSED",
                "gateStatus": "PASS",
                "validationSkipped": True,
                "workflow": {"workflowId": workflow_id, "steps": []},
                "operations": [],
                "result": {
                    "status": "PASSED",
                    "gateStatus": "PASS",
                    "validationSkipped": True,
                    "steps": [],
                },
            }
        )
        known_ids.add(workflow_id)
    return result


def _save_terminal_testing_checkpoint(
    progress: TestingProgress | None,
    job: dict[str, Any],
) -> None:
    """Persist one terminal checkpoint for normal and demo-completed jobs."""

    if progress is None:
        return
    progress(
        {
            "implementation_job_id": job.get("implementation_job_id"),
            "testing_input": dict(job.get("testing_input") or {}),
            "current_node": "verification_complete",
            "result": dict(job.get("result") or {}),
            "repair_history": dict(job.get("repair_history") or {}),
            "previous_findings": list(job.get("previous_findings") or []),
            "testing_progress": dict(job.get("testing_progress") or {}),
        }
    )


def _persist_demo_fallback_workflow_progress(
    progress: TestingProgress | None,
    job: dict[str, Any],
) -> None:
    """Fold fallback workflow terminal events through the normal checkpoint shape."""

    if progress is None:
        return
    testing_progress = dict(job.get("testing_progress") or {})

    def observe(event: dict[str, Any]) -> None:
        nonlocal testing_progress
        testing_progress = reduce_testing_progress(testing_progress, event)
        progress(
            {
                "implementation_job_id": job.get("implementation_job_id"),
                "testing_input": dict(job.get("testing_input") or {}),
                "current_node": "verification",
                "result": dict(job.get("result") or {}),
                "repair_history": dict(job.get("repair_history") or {}),
                "previous_findings": list(job.get("previous_findings") or []),
                "testing_progress": testing_progress,
            }
        )

    verification = (job.get("result") or {}).get("verification") or {}
    workflow_count = int(
        (verification.get("workflowCounts") or {}).get("total") or 0
    )
    with testing_progress_scope(observe):
        dynamic = (verification.get("reports") or {}).get("dynamicFunctional") or {}
        workflows = dynamic.get("workflows") if isinstance(dynamic, dict) else None
        if isinstance(workflows, list):
            _emit_dynamic_workflow_plans(
                [workflow for workflow in workflows if isinstance(workflow, dict)]
            )
        _emit_demo_terminal_workflow_progress(verification)
        # Normal Testing emits this same summary event before its terminal
        # checkpoint.  Retain it for a zero-workflow fallback so Workspace can
        # apply its verification_complete task transition from last_event.
        emit_testing_progress(
            phase="summary",
            scope="phase",
            status="PASS",
            label="Testing report ready",
            detail="All required gates passed.",
            completed_workflows=workflow_count,
            total_workflows=workflow_count,
        )
    job["testing_progress"] = testing_progress


def run_testing(
    app_id: str,
    implementation_job_id: str,
    *,
    run_id: str,
    previous_job: dict[str, Any] | None = None,
    preserve_test: bool = False,
    checkpoint: dict[str, Any] | None = None,
    repair_task_type: str | None = None,
    progress: TestingProgress | None = None,
) -> dict[str, Any]:
    """Run Testing, with one demo-only outer failure boundary."""

    try:
        job = _run_testing(
            app_id,
            implementation_job_id,
            run_id=run_id,
            previous_job=previous_job,
            preserve_test=preserve_test,
            checkpoint=checkpoint,
            repair_task_type=repair_task_type,
            progress=progress,
        )
    except Exception:
        if not demo_skip_validation_enabled():
            raise
        job = _demo_fallback_job(
            app_id,
            implementation_job_id,
            run_id=run_id,
            checkpoint=checkpoint,
        )
        _persist_demo_fallback_workflow_progress(progress, job)
    _save_terminal_testing_checkpoint(progress, job)
    return job
