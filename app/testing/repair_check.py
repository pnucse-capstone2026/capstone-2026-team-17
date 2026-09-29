"""Testing에서 실패한 gate를 수리 작업 안에서 다시 실행한다.

이 모듈은 새 검증 framework가 아니다. 최초 Testing이 사용하는 Trivy,
배포 package, OpenTofu, HTTP 실행기를 같은 입력으로 다시 호출하는 작은
adapter다. 따라서 Gradle 통과만으로 Trivy 수리가 완료되는 잘못된 판정을
피할 수 있다.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from app.testing.nodes.static_verification import static_verification_node
from app.testing.runtime.app_container import ApplicationLaunchError, running_application
from app.testing.schemas.arazzo import ArazzoValidationError, validate_arazzo_document
from app.testing.utils.arazzo_executor import execute_arazzo_workflow
from app.testing.utils.functional_executor import InputValueRequest
from app.testing.utils.gates import gate_status


def _profile_openapi(profile: dict[str, Any]) -> dict[str, Any] | None:
    value = profile.get("openapi")
    if isinstance(value, dict):
        return value
    testing_input = profile.get("testing_input") or profile.get("testingInput")
    contracts = (
        testing_input.get("contract_artifacts") or testing_input.get("contractArtifacts")
        if isinstance(testing_input, dict)
        else None
    )
    artifact = contracts.get("openapi") if isinstance(contracts, dict) else None
    content = artifact.get("content") if isinstance(artifact, dict) else None
    return content if isinstance(content, dict) else None


def _profile_document(profile: dict[str, Any], openapi: dict[str, Any]) -> dict[str, Any]:
    """Load only a portable Arazzo document; resume values stay outside it."""
    document = profile.get("candidate_plan") or profile.get("candidatePlan")
    if not isinstance(document, dict):
        raise TypeError("Dynamic Testing repair is missing the canonical Arazzo candidatePlan.")
    legacy = {"cases", "inputValues", "workflowInputs"}.intersection(document)
    if legacy:
        raise ValueError(
            "candidatePlan must be a pure Arazzo document; unsupported fields: "
            + ", ".join(sorted(legacy))
        )
    return validate_arazzo_document(document, openapi=openapi)


def _failed_workflow_id(profile: dict[str, Any], document: dict[str, Any]) -> str:
    workflow_id = str(profile.get("failed_workflow_id") or profile.get("failedWorkflowId") or "").strip()
    if not workflow_id:
        raise ValueError("Dynamic Testing repair is missing failedWorkflowId.")
    if workflow_id not in {
        str(workflow.get("workflowId")) for workflow in document.get("workflows") or [] if isinstance(workflow, dict)
    }:
        raise ValueError(f"failedWorkflowId does not exist in candidatePlan: {workflow_id}")
    return workflow_id


def _workflow_inputs(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    values = profile.get("workflow_inputs")
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise TypeError("workflowInputs must map workflow IDs to input objects.")
    result: dict[str, dict[str, Any]] = {}
    for workflow_id, selected in values.items():
        if not isinstance(workflow_id, str) or not isinstance(selected, dict):
            raise TypeError("workflowInputs must map workflow IDs to input objects.")
        result[workflow_id] = dict(selected)
    return result


def _concrete_inputs(
    profile: dict[str, Any],
) -> dict[str, dict[tuple[str, str], Any]]:
    values = profile.get("input_values")
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise TypeError("inputValues must map workflow IDs to concrete input records.")
    result: dict[str, dict[tuple[str, str], Any]] = {}
    for workflow_id, selected in values.items():
        if not isinstance(workflow_id, str) or not isinstance(selected, list):
            raise TypeError("inputValues must map workflow IDs to concrete input arrays.")
        workflow_values: dict[tuple[str, str], Any] = {}
        for item in selected:
            if not isinstance(item, dict):
                raise TypeError(f"inputValues for {workflow_id} contains a non-object record.")
            record_operation_id = item.get("operationId")
            record_location = item.get("location")
            if (
                not isinstance(record_operation_id, str)
                or not record_operation_id
                or not isinstance(record_location, str)
                or not record_location
                or "value" not in item
            ):
                raise ValueError(f"inputValues for {workflow_id} contains an invalid record.")
            key = (record_operation_id, record_location)
            if key in workflow_values:
                raise ValueError(
                    f"inputValues for {workflow_id} repeats "
                    f"{record_operation_id}|{record_location}."
                )
            workflow_values[key] = item["value"]
        result[workflow_id] = workflow_values
    return result


def _failed_command(report: dict[str, Any]) -> dict[str, Any]:
    return next(
        (
            dict(item)
            for item in report.get("commands") or []
            if isinstance(item, dict)
            and str(item.get("status") or "").upper() not in {"PASS", "PASSED"}
        ),
        {},
    )


def _command_evidence(gate: str, report: dict[str, Any]) -> dict[str, object]:
    """gate 보고서를 기존 OpenHands 검사 오류 형식으로 바꾼다."""
    failed = _failed_command(report)
    issues = [str(item) for item in report.get("issues") or []]
    status = gate_status(report)
    return {
        "command": failed.get("command") or [f"testing-{gate}"],
        "exitCode": failed.get("exitCode") if failed else (0 if status == "PASS" else 1),
        "durationMs": 0,
        "stdout": "",
        "stderr": "\n".join(issues)
        or str(failed.get("output") or failed.get("error") or failed.get("reason") or ""),
        "testResults": "",
        "gate": gate,
        "gateStatus": status,
        "gateEvidence": report,
    }


def _static_gate(
    sandbox: Path,
    gate: str,
    profile: dict[str, Any],
) -> dict[str, object]:
    state = {
        "application_dir": str(sandbox / "application"),
        "testing_input": profile.get("testing_input") or profile.get("testingInput") or {},
        "deployment_package_expected": profile.get("deployment_package_expected"),
        # 구현 에이전트 안의 수리 확인은 원래 실패한 gate 하나만 실행한다. 결과에서
        # 하나를 골라내기만 하면 Trivy·package·OpenTofu가 모두 실행되어 선택 검사의
        # 시간 절약과 오류 분리가 사라진다.
        "gate_scope": [gate],
    }
    result = static_verification_node(state)  # type: ignore[arg-type]
    static = result.get("static_report") or {}
    selected = {
        # static finding은 Trivy config scan 자체이다. package/OpenTofu는 각자
        # 별도 task type으로 재현해 서로 무관한 도구 오류에 막히지 않게 한다.
        "static": static.get("trivyScan") or static,
        "package": static.get("deploymentPackage") or {},
        "iac": result.get("iac_report") or {},
    }[gate]
    return _command_evidence(gate, selected)


def _dynamic_gate(sandbox: Path, profile: dict[str, Any]) -> dict[str, object]:
    openapi = _profile_openapi(profile)
    if openapi is None:
        message = "Dynamic Testing repair is missing the frozen OpenAPI document."
        return _command_evidence(
            "dynamicFunctional",
            {
                "gateStatus": "FAIL",
                "defectClass": "TEST_DEFECT",
                "issues": [message],
                "commands": [],
            },
        )
    try:
        document = _profile_document(profile, openapi)
        workflow_id = _failed_workflow_id(profile, document)
        workflow_inputs = _workflow_inputs(profile)
        concrete_inputs = _concrete_inputs(profile)
    except (ArazzoValidationError, TypeError, ValueError) as error:
        return _command_evidence(
            "dynamicFunctional",
            {
                "gateStatus": "FAIL",
                "defectClass": "TEST_DEFECT",
                "issues": [str(error)],
                "commands": [],
            },
        )

    def propose(request: InputValueRequest) -> Any:
        input_workflow_id = request.operation_context or workflow_id
        key = (request.operation_id, request.location)
        workflow_values = concrete_inputs.get(input_workflow_id, {})
        if key not in workflow_values:
            raise ValueError(
                "No preserved concrete input value exists for "
                f"{input_workflow_id}:{request.operation_id}|{request.location}."
            )
        return workflow_values[key]

    app_id = str(profile.get("app_id") or profile.get("appId") or "testing-repair")
    try:
        with running_application(
            app_id,
            str(sandbox / "application"),
            launch_id=f"repair-{uuid.uuid4().hex[:12]}",
        ) as (target_url, _application):
            result = execute_arazzo_workflow(
                document,
                workflow_id,
                openapi=openapi,
                target_url=target_url,
                workflow_inputs=workflow_inputs.get(workflow_id),
                workflow_inputs_by_id=workflow_inputs,
                propose_input=propose if concrete_inputs else None,
            )
    except ApplicationLaunchError as error:
        return _command_evidence(
            "dynamicFunctional",
            {
                "gateStatus": "INCONCLUSIVE"
                if error.defect_class == "ENVIRONMENT_DEFECT"
                else "FAIL",
                "issues": [str(error)],
                "commands": [],
            },
        )
    except Exception as error:
        return _command_evidence(
            "dynamicFunctional",
            {
                "gateStatus": "FAIL",
                "defectClass": "TEST_DEFECT",
                "issues": [f"Arazzo repair execution failed: {error}"],
                "commands": [],
            },
        )
    commands = []
    for item in result.get("steps") or []:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("method"), str)
            or not isinstance(item.get("path"), str)
        ):
            continue
        passed = (
            str(item.get("status") or "").lower() != "failed"
            and str(item.get("contractStatus") or "").upper() != "FAIL"
            and str(item.get("semanticStatus") or "").upper() != "FAIL"
            and not item.get("finding")
        )
        commands.append(
            {
                "name": (
                    f"{item.get('workflowId', workflow_id)}: "
                    f"HTTP {item['method']} {item['path']}"
                ),
                "command": [item.get("method"), item.get("path")],
                "status": "PASS" if passed else "FAIL",
                "exitCode": 0 if passed else 1,
                "output": str(item.get("responseBody") or ""),
            }
        )
    failed_step_id = str(
        result.get("failedStepId")
        or (result.get("finding") or {}).get("stepId")
        or next(
            (
                item.get("stepId")
                for item in result.get("steps") or []
                if isinstance(item, dict)
                and (item.get("finding") or item.get("semanticStatus") == "FAIL")
            ),
            "",
        )
        or ""
    )
    actual_failed_workflow_id = str(
        result.get("failedWorkflowId")
        or (result.get("finding") or {}).get("workflowId")
        or workflow_id
    )
    workflow = next(
        (
            item
            for item in document.get("workflows") or []
            if isinstance(item, dict)
            and item.get("workflowId") == actual_failed_workflow_id
        ),
        {},
    )
    failed_step = next(
        (
            item
            for item in workflow.get("steps") or []
            if isinstance(item, dict) and item.get("stepId") == failed_step_id
        ),
        {},
    ) if isinstance(workflow, dict) else {}
    evidence = {
        "rerunWorkflowId": workflow_id,
        "failedWorkflowId": actual_failed_workflow_id,
        "failedStepId": failed_step_id,
        "successCriteria": list(failed_step.get("successCriteria") or []) if isinstance(failed_step, dict) else [],
        "finding": dict(result.get("finding") or {}),
        "steps": [
            dict(item)
            for item in result.get("steps") or []
            if isinstance(item, dict) and isinstance(item.get("operationId"), str)
        ],
    }
    return _command_evidence(
        "dynamicFunctional",
        {
            **result,
            "failedWorkflowId": actual_failed_workflow_id,
            "failedStepId": failed_step_id,
            "repairEvidence": evidence,
            "issues": [str(result.get("reason"))] if result.get("reason") else [],
            "commands": commands,
        },
    )


def verify_testing_repair_gate(
    sandbox: Path,
    gate: str,
    profile: dict[str, Any] | None = None,
) -> dict[str, object]:
    """수리 작업에 지정된 원래 Testing gate 하나를 다시 실행한다."""
    normalized = gate.removeprefix("testing-")
    values = dict(profile or {})
    if normalized in {"static", "package", "iac"}:
        return _static_gate(sandbox, normalized, values)
    if normalized in {"dynamic", "dynamic-functional", "dynamicFunctional"}:
        return _dynamic_gate(sandbox, values)
    raise ValueError(f"Unknown Testing repair gate: {gate}")


__all__ = ["verify_testing_repair_gate"]
