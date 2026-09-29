"""정적 검사와 동적 검사를 한 번의 공통 검증 절차로 실행한다.

Testing HTTP API와 전체 파이프라인은 모두 생성된 앱을 실행하고 같은 병렬 검증 경계를
사용해야 한다. 이 모듈에 공통 절차를 두어 두 진입점의 성공·실패 판정이 달라지지 않게 한다.
"""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from app.demo_validation import demo_skip_validation_enabled
from app.metrics import langsmith as langsmith_metrics
from app.testing.graphs.testing_graph import create_testing_graph, initial_state
from app.testing.progress import emit_testing_progress
from app.testing.runtime.app_container import (
    ApplicationLaunchError,
    _log_excerpt,
    application_log,
    running_application,
)
from app.testing.runtime.evidence_store import store_application_log
from app.testing.utils.gates import aggregate_gate_report, gate_status
from app.validation import stable_digest

_ALL_GATES = frozenset({"static", "package", "iac", "dynamicFunctional"})


def _files_digest(root: Path, files: list[Path], *, extra: object = None) -> str:
    """선택 gate가 실제로 읽는 파일만 안정적인 digest로 만든다."""

    entries = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(files)
        if path.is_file()
    ]
    return stable_digest({"files": entries, "extra": extra})


def _gate_input_digests(
    application_dir: str,
    *,
    testing_input: dict[str, Any] | None,
) -> dict[str, str]:
    """gate별 관련 파일을 나눠 무관한 수정 때문에 전체 검사가 반복되지 않게 한다."""

    application = (
        Path(application_dir) if application_dir else Path("__easydep_missing_application__")
    )
    all_files = (
        [path for path in application.rglob("*") if path.is_file()] if application.is_dir() else []
    )
    deployment = application / "deployment"
    tofu = deployment / "tofu"
    static_files = [
        path
        for path in all_files
        if path.name == "Dockerfile"
        or path.suffix.lower() in {".tf", ".tfvars", ".yaml", ".yml", ".json"}
    ]
    tofu_files = [path for path in tofu.rglob("*") if path.is_file()] if tofu.is_dir() else []
    package_files = [
        path for path in all_files if deployment in path.parents and tofu not in path.parents
    ]
    # package 구조 검사는 tofu 파일 내용이 아니라 필수 파일의 존재 여부도 확인한다.
    tofu_layout = sorted(path.relative_to(tofu).as_posix() for path in tofu_files)
    dynamic_files = [
        path
        for path in all_files
        if deployment not in path.parents
        and "frontend" not in path.relative_to(application).parts
        and path.name != "Dockerfile"
    ]
    frozen_input = testing_input or {}
    contracts = frozen_input.get("contract_artifacts") or frozen_input.get("contractArtifacts")
    contracts = contracts if isinstance(contracts, dict) else {}
    deployment_contract = contracts.get("deployment") or {}
    dynamic_contracts = {
        key: contracts.get(key)
        for key in ("requirements", "use_cases", "useCases", "openapi")
        if contracts.get(key) is not None
    }
    return {
        "static": _files_digest(
            application, static_files, extra={"deployment": deployment_contract}
        ),
        "package": _files_digest(
            application,
            package_files,
            extra={"tofuLayout": tofu_layout, "deployment": deployment_contract},
        ),
        "iac": _files_digest(application, tofu_files, extra={"deployment": deployment_contract}),
        "dynamicFunctional": _files_digest(
            application,
            dynamic_files,
            # 계획은 이 gate의 출력이다. source와 고정 계약이 같으면 직전 실행 결과를
            # 재사용할 수 있으며, 선택 재실행 때는 service가 그 계획을 별도로 넘긴다.
            extra={"contracts": dynamic_contracts},
        ),
    }


def _previous_input_digest(reports: dict[str, Any], gate: str) -> str:
    """이전 통합 보고서에서 gate별 입력 digest를 읽는다."""

    if gate == "static":
        report = reports.get("static") or {}
        report = report.get("trivyScan") or report
    elif gate == "package":
        report = (reports.get("static") or {}).get("deploymentPackage") or {}
    else:
        report = reports.get(gate) or {}
    # 동적 차단 실패 때문에 도구를 실행하지 않은 report는 파일 입력이 같아도 재사용
    # 근거가 아니다. 다음 동적 수리 성공 뒤에는 해당 gate를 실제로 실행해야 한다.
    if isinstance(report, dict) and report.get("deferred") is True:
        return ""
    return str(report.get("inputDigest") or "") if isinstance(report, dict) else ""


def _effective_scope(
    requested: set[str] | None,
    previous_reports: dict[str, Any],
    input_digests: dict[str, str],
) -> set[str] | None:
    """관련 입력이 실제로 달라진 gate만 요청 범위에 추가한다."""

    if requested is None:
        return None
    selected = set(requested)
    for gate in _ALL_GATES - selected:
        previous = _previous_input_digest(previous_reports, gate)
        # 입력 digest가 없는 예전 보고서는 파일이 같은지 증명할 수 없다. 한 번 실제로
        # 실행해 현재 형식의 digest를 만든 뒤에만 다음 수리에서 재사용한다.
        if not previous or previous != input_digests[gate]:
            selected.add(gate)
    return selected


def _attach_input_digests(result: dict[str, Any], digests: dict[str, str]) -> None:
    """다음 선택 재검사가 안전하게 재사용 여부를 판단할 근거를 남긴다."""

    static = result.get("static_report")
    if isinstance(static, dict):
        static["inputDigest"] = stable_digest(
            {"static": digests["static"], "package": digests["package"]}
        )
        trivy = static.get("trivyScan")
        if isinstance(trivy, dict):
            trivy["inputDigest"] = digests["static"]
        package = static.get("deploymentPackage")
        if isinstance(package, dict):
            package["inputDigest"] = digests["package"]
    iac = result.get("iac_report")
    if isinstance(iac, dict):
        iac["inputDigest"] = digests["iac"]
    dynamic = result.get("dynamic_functional_report")
    if isinstance(dynamic, dict):
        dynamic["inputDigest"] = digests["dynamicFunctional"]


def _runtime_evidence(runtime: dict[str, Any]) -> dict[str, Any]:
    """수리 담당에 필요한 런타임 특성만 남기고 내부 Docker 식별자는 숨긴다."""

    source = str(runtime.get("source") or "unknown")
    evidence: dict[str, Any] = {"source": source}
    for key in ("profile", "database"):
        value = runtime.get(key)
        if isinstance(value, (str, int, float, bool)) and value != "":
            evidence[key] = value
    return evidence


def _attach_dynamic_failure_evidence(
    result: dict[str, Any],
    runtime: dict[str, Any],
    *,
    app_id: str,
    run_id: str,
) -> None:
    """컨텍스트 cleanup 전에 dynamic failure finding에 실행 증거를 붙인다."""

    report = result.get("dynamic_functional_report")
    if not isinstance(report, dict) or gate_status(report) not in {"FAIL", "INCONCLUSIVE"}:
        return
    finding = report.get("finding")
    if not isinstance(finding, dict):
        finding = {
            "code": "DYNAMIC_FUNCTIONAL_UNAVAILABLE",
            "message": str(report.get("reason") or "Dynamic functional verification failed."),
        }
        report["finding"] = finding
    finding["runtime"] = _runtime_evidence(runtime)
    logs = application_log(runtime)
    # 정상 4xx, schema/semantic failure처럼 stack trace가 없을 수 있다. 빈 로그를
    # 증거인 것처럼 넣지 않고 실제로 읽힌 내용만 보존한다.
    if logs:
        finding["applicationLogRef"] = store_application_log(app_id, run_id, logs)
        finding["applicationLogExcerpt"] = _log_excerpt(logs, limit=6000)


def _deferred_static_reports(reason: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """동적 차단 실패 뒤 실제로 실행하지 않은 정적 gate의 안정적인 결과 형식."""

    def deferred(gate: str) -> dict[str, Any]:
        return {
            "status": "DEFERRED",
            "gateStatus": "NOT_APPLICABLE",
            "deferred": True,
            "deferredGate": gate,
            "reason": reason,
        }

    trivy = deferred("static")
    package = deferred("package")
    return (
        {
            **deferred("static"),
            "issues": [],
            "trivyScan": trivy,
            "deploymentPackage": package,
        },
        deferred("iac"),
    )


def _launch(
    app_id: str,
    target_url: str,
    *,
    launch_id: str,
    application_dir: str,
):
    """호출자가 URL을 주면 재사용하고, 아니면 복원된 폴더를 실행한다."""
    if target_url:
        return nullcontext((target_url, {"source": "caller"}))
    if not application_dir:
        raise ApplicationLaunchError("No generated application directory is available to run.")
    return running_application(
        app_id,
        application_dir,
        launch_id=launch_id,
    )


def run_verification_graph(
    *,
    run_id: str,
    app_id: str,
    target_url: str = "",
    application_dir: str = "",
    repair_history: dict[str, Any] | None = None,
    fixed_arazzo_document: dict[str, Any] | None = None,
    fixed_workflow_inputs: dict[str, dict[str, Any]] | None = None,
    fixed_input_values: dict[str, list[dict[str, Any]]] | None = None,
    preserved_workflow_results: list[dict[str, Any]] | None = None,
    priority_workflow_id: str = "",
    implementation_job_id: str | None = None,
    testing_input: dict[str, Any] | None = None,
    iac_expected: bool | None = None,
    deployment_package_expected: bool | None = None,
    gate_scope: set[str] | None = None,
    previous_reports: dict[str, Any] | None = None,
    previous_job_id: str = "",
) -> dict[str, Any]:
    with langsmith_metrics.trace_scope(
        "easydep.testing.verification",
        metadata={
            "agent": "testing",
            "operation": "verification",
            "run_id": run_id,
            "app_id": app_id,
            "implementation_job_id": implementation_job_id,
            "gate_scope": sorted(gate_scope) if gate_scope is not None else "all",
        },
    ):
        return _run_verification_graph(
            run_id=run_id,
            app_id=app_id,
            target_url=target_url,
            application_dir=application_dir,
            repair_history=repair_history,
            fixed_arazzo_document=fixed_arazzo_document,
            fixed_workflow_inputs=fixed_workflow_inputs,
            fixed_input_values=fixed_input_values,
            preserved_workflow_results=preserved_workflow_results,
            priority_workflow_id=priority_workflow_id,
            implementation_job_id=implementation_job_id,
            testing_input=testing_input,
            iac_expected=iac_expected,
            deployment_package_expected=deployment_package_expected,
            gate_scope=gate_scope,
            previous_reports=previous_reports,
            previous_job_id=previous_job_id,
        )


def _demo_pass_statuses(value: Any) -> Any:
    """Keep demo validation metadata internal without exposing a third status.

    A graph node may carry an older/reused ``SKIPPED`` report.  Once the shared
    demo switch elected the PASS-only public contract, normalize that status at
    the one result boundary while retaining every other evidence field.
    """

    if isinstance(value, dict):
        projected = {key: _demo_pass_statuses(item) for key, item in value.items()}
        if str(projected.get("status") or "").upper() == "SKIPPED":
            projected["status"] = "PASSED"
            projected["gateStatus"] = "PASS"
        return projected
    if isinstance(value, list):
        return [_demo_pass_statuses(item) for item in value]
    return value


def _run_verification_graph(
    *,
    run_id: str,
    app_id: str,
    target_url: str = "",
    application_dir: str = "",
    repair_history: dict[str, Any] | None = None,
    fixed_arazzo_document: dict[str, Any] | None = None,
    fixed_workflow_inputs: dict[str, dict[str, Any]] | None = None,
    fixed_input_values: dict[str, list[dict[str, Any]]] | None = None,
    preserved_workflow_results: list[dict[str, Any]] | None = None,
    priority_workflow_id: str = "",
    implementation_job_id: str | None = None,
    testing_input: dict[str, Any] | None = None,
    iac_expected: bool | None = None,
    deployment_package_expected: bool | None = None,
    gate_scope: set[str] | None = None,
    previous_reports: dict[str, Any] | None = None,
    previous_job_id: str = "",
) -> dict[str, Any]:
    """저장된 애플리케이션을 실행한 뒤 병렬 Testing graph를 호출한다."""
    graph = create_testing_graph()
    validation_skipped = demo_skip_validation_enabled()
    previous_reports = dict(previous_reports or {})
    input_digests = _gate_input_digests(
        application_dir,
        testing_input=testing_input,
    )
    selected = _effective_scope(gate_scope, previous_reports, input_digests)
    application: dict[str, Any] = {}
    launch_error: str | None = None
    launch_defect_class = "SUT_DEFECT"

    def invoke(url: str = "") -> dict[str, Any]:
        return graph.invoke(
            initial_state(
                run_id=run_id,
                app_id=app_id,
                target_url=url,
                application_dir=application_dir,
                repair_history=repair_history,
                fixed_arazzo_document=fixed_arazzo_document,
                fixed_workflow_inputs=fixed_workflow_inputs,
                fixed_input_values=fixed_input_values,
                preserved_workflow_results=preserved_workflow_results,
                priority_workflow_id=priority_workflow_id,
                testing_input=testing_input,
                iac_expected=iac_expected,
                deployment_package_expected=deployment_package_expected,
                gate_scope=sorted(selected) if selected is not None else None,
                previous_reports=previous_reports,
                previous_job_id=previous_job_id,
                validation_skipped=validation_skipped,
            )
        )

    dynamic_selected = selected is None or "dynamicFunctional" in selected
    try:
        if validation_skipped:
            emit_testing_progress(
                phase="prepare",
                scope="phase",
                status="SKIPPED",
                label="Runtime and verification execution skipped for demo",
                detail="Arazzo plans and Testing artifacts are still generated.",
            )
            # The graph still plans and validates the Arazzo artifact, but the
            # runtime, HTTP executor, and static validation tools are never invoked.
            result = invoke()
        elif not dynamic_selected:
            emit_testing_progress(
                phase="prepare",
                scope="phase",
                status="SKIPPED",
                label="Application runtime is not required",
                detail="The selected repair scope has no dynamic functional gate.",
            )
            # 정적 수리에서는 Spring/Gradle을 띄우지 않는다. graph의 dynamic node는
            # 아래 고정 이전 보고서를 복사하므로 결과 shape와 전체 gate 판정은 유지된다.
            result = invoke()
        else:
            emit_testing_progress(
                phase="prepare",
                scope="phase",
                status="RUNNING",
                label="Starting the application runtime",
            )
            with _launch(
                app_id,
                target_url,
                launch_id=run_id,
                application_dir=application_dir,
            ) as (url, application):
                emit_testing_progress(
                    phase="prepare",
                    scope="phase",
                    status="PASS",
                    label="Application runtime is ready",
                )
                result = invoke(url)
                _attach_dynamic_failure_evidence(
                    result,
                    application,
                    app_id=app_id,
                    run_id=run_id,
                )
            emit_testing_progress(
                phase="prepare",
                scope="phase",
                status="PASS",
                label="Application runtime stopped",
            )
    except ApplicationLaunchError as error:
        launch_error = str(error)
        launch_defect_class = error.defect_class
        emit_testing_progress(
            phase="prepare",
            scope="phase",
            status=("INCONCLUSIVE" if launch_defect_class == "ENVIRONMENT_DEFECT" else "FAIL"),
            label="Application runtime could not start",
        )
        deferred_static, deferred_iac = _deferred_static_reports(
            "Deferred because the Testing application could not start."
        )
        # launch 실패는 첫 runtime 차단 원인이다. 이 뒤에 정적 도구를 실행하면 수리
        # 시작만 늦고 다음 재검사 때 사용할 이전 report도 만들어지므로 명시적 deferred
        # report만 남긴다.
        result = {
            "current_node": "application_launch_failed",
            "errors": [],
            "static_report": deferred_static,
            "iac_report": deferred_iac,
        }

        # 앱을 띄우지 못했는데 동적 검사를 NOT_APPLICABLE로 두면 최종 finding에서
        # 시작 실패가 사라진다. Docker 환경 문제는 재실행 대기, 생성 앱 문제는 구현
        # 수리로 보낼 수 있도록 같은 dynamic gate에 명시적인 실패를 남긴다.
        environment_failure = launch_defect_class == "ENVIRONMENT_DEFECT"
        launch_runtime = {
            "source": "application",
            "profile": "test",
            "database": "h2-mysql-mode",
        }
        launch_finding: dict[str, Any] = {
            "code": "APPLICATION_LAUNCH_FAILED",
            "message": launch_error,
            "runtime": launch_runtime,
        }
        if error.application_log:
            launch_finding["applicationLogRef"] = store_application_log(
                app_id, run_id, error.application_log
            )
            launch_finding["applicationLogExcerpt"] = _log_excerpt(
                error.application_log, limit=6000
            )
        result["dynamic_functional_report"] = {
            "status": "UNAVAILABLE" if environment_failure else "FAILED",
            "gateStatus": "INCONCLUSIVE" if environment_failure else "FAIL",
            "reason": launch_error,
            "defectClass": launch_defect_class,
            "deferredGates": ["static", "package", "iac"],
            "finding": launch_finding,
            "defect": {
                "class": launch_defect_class,
                "defectClass": launch_defect_class,
                "route": "environment" if environment_failure else "implementation",
                "preserveTests": True,
            },
        }

    _attach_input_digests(result, input_digests)
    reports = {
        "static": result.get("static_report"),
        "iac": result.get("iac_report"),
        "dynamicFunctional": result.get("dynamic_functional_report"),
    }
    if validation_skipped:
        reports = _demo_pass_statuses(reports)
    required = {
        "static": True,
        # An unspecified IaC contract is a legacy/no-IaC application. The service
        # supplies True when the fixed implementation snapshot contains IaC.
        "iac": iac_expected is True,
        "dynamicFunctional": True,
    }
    aggregate = aggregate_gate_report(reports, required=required)
    blocking = (
        f"Dynamic testing could not run because the application did not start: {launch_error}"
        if launch_error
        else blocking_reason(reports)
    )
    if blocking is None and aggregate["status"] == "INCONCLUSIVE":
        blocking = "A required Testing gate could not run, so the result is inconclusive."
    if blocking is None and aggregate["status"] == "FAIL":
        blocking = "A required Testing gate failed."
    diagnostics = misconfiguration_diagnostics(reports)
    if launch_error:
        diagnostics.insert(
            0,
            {
                "code": "APPLICATION_LAUNCH_FAILED",
                "message": launch_error,
                "defectClass": launch_defect_class,
            },
        )
    return {
        "reports": reports,
        "application": application,
        "applicationLaunchError": launch_error,
        "errors": result.get("errors") or [],
        "passed": blocking is None,
        "status": aggregate["status"],
        "gateStatus": aggregate["status"],
        "gates": aggregate["gates"],
        "gateCounts": aggregate["counts"],
        "deferredGates": list((reports.get("dynamicFunctional") or {}).get("deferredGates") or []),
        "blockingReason": blocking,
        "diagnostics": diagnostics,
        "validationSkipped": validation_skipped,
        "validationSkipReason": "demo" if validation_skipped else None,
        "executedWorkflowCount": (
            0
            if validation_skipped
            else len((reports.get("dynamicFunctional") or {}).get("workflows") or [])
        ),
        "workflowCounts": dict(
            (reports.get("dynamicFunctional") or {}).get("workflowCounts") or {}
        ),
    }
def blocking_reason(reports: dict[str, Any]) -> str | None:
    """정적 또는 동적 필수 검사가 실패한 첫 번째 이유를 반환한다."""
    for label, key in (("Deployment configuration", "static"), ("IaC", "iac")):
        report = reports.get(key) or {}
        if gate_status(report) == "FAIL":
            issues = report.get("issues") or []
            detail = str(issues[0]) if issues else str(report.get("message") or "")
            return f"{label} verification failed: {detail}".rstrip()

    report = reports.get("dynamicFunctional") or {}
    if gate_status(report) == "FAIL":
        return str(
            report.get("reason") or report.get("stderr") or "Dynamic functional testing failed."
        )[-2000:]
    return None


def misconfiguration_diagnostics(reports: dict[str, Any]) -> list[dict[str, str]]:
    """정적 검사 미실행과 발견된 설정 문제를 사용자가 읽을 수 있는 진단으로 바꾼다."""
    diagnostics = []
    for subject, key in (("DEPLOYMENT", "static"), ("IAC", "iac")):
        report = reports.get(key) or {}
        issues = report.get("issues") or []
        if gate_status(report) == "INCONCLUSIVE":
            # 검사를 실행하지 못한 산출물을 문제가 없는 산출물처럼 표시하면 안 된다.
            diagnostics.append(
                {
                    "code": f"{subject}_NOT_SCANNED",
                    "message": str(report.get("message") or "Nothing was scanned."),
                }
            )
        elif gate_status(report) == "FAIL" and issues:
            diagnostics.append(
                {
                    "code": f"{subject}_MISCONFIGURATION",
                    "message": f"Static verification found {len(issues)} issue(s): "
                    + "; ".join(issues[:5]),
                }
            )
    return diagnostics
