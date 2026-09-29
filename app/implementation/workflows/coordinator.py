from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from app.design.contracts import bind_runtime_contract, build_provider_resource_plan
from app.metrics import langsmith as langsmith_metrics

from ..agents.runtime import (
    OwnerConversationIncomplete,
    effective_task_prompt_sha256,
    execute_openhands_task,
    write_execution_plan,
)
from ..agents.verification.build import WorkspaceVerificationError, verify_run_workspace
from ..delivery.container import render_local_container
from ..delivery.terraform import render_iac
from ..domain.implementation_ir import build_implementation_ir
from ..domain.models import JobSpec
from ..generation.orchestrator import (
    plan_backend_owner_task,
    plan_frontend_tasks,
    plan_persistence_tasks,
)
from ..runtime.observations import observe_runtime_contract
from .completion import audit_run_completion
from .conformance import (
    SourceDesignConformanceError,
    verify_source_design_conformance,
)
from .repair import (
    apply_repair_directives,
    repair_recheck_for_task,
    repair_recheck_task_ids,
    repair_task_ids,
    schedule_cross_phase_repair,
    schedule_source_conformance_repair,
)
from .traceability import build_rtm_traceability_map

WORKFLOW_SCHEMA = "implementation-workflow/v1alpha1"
PHASES = (
    (
        "backend",
        (),
        {
            "backend-implementation",
            "backend-unit-test",
            "control",
            "testing-static",
            "testing-package",
            "testing-iac",
            "testing-dynamic-functional",
        },
    ),
    ("frontend", ("backend",), {"frontend-implementation", "frontend-unit-test"}),
    ("integration", ("frontend",), {"integration-implementation"}),
)

PHASE_LABELS = {
    "backend": "Backend implementation",
    "frontend": "Frontend implementation",
    "integration": "Integration verification",
}


def plan_workflow(run_root: Path, spec: JobSpec) -> dict[str, object]:
    """Idempotently plan implemented phases and persist a resumable checkpoint."""
    run_root = run_root.resolve()
    if spec.job_type == "FEEDBACK_REVISION":
        apply_repair_directives(run_root)
        return reconcile_workflow_state(run_root)
    tasks = materialize_owner_tasks(run_root, spec)
    manifest_path = run_root / "reports" / "run-manifest.json"
    manifest = _read_json(manifest_path)
    manifest["agent_execution"] = write_execution_plan(
        run_root,
        tasks,
        spec.agent_mode,
    )
    _write_json_atomic(manifest_path, manifest)
    build_rtm_traceability_map(spec, run_root)
    apply_repair_directives(run_root)
    return reconcile_workflow_state(run_root)


def materialize_owner_tasks(run_root: Path, spec: JobSpec) -> list[dict[str, object]]:
    """Build the current initial-implementation owner task manifest without workflow effects."""
    run_root = run_root.resolve()
    if spec.job_type == "FEEDBACK_REVISION":
        raise ValueError("Owner-task materialization is only available for initial implementation")
    build_implementation_ir(spec, run_root)
    if spec.inputs.get("erdBceModel") is not None:
        plan_persistence_tasks(spec, run_root)
    plan_backend_owner_task(spec, run_root)
    plan_frontend_tasks(spec, run_root)
    manifest = _read_json(run_root / "reports" / "run-manifest.json")
    return [
        task
        for task in manifest.get("implementation_tasks", [])
        if isinstance(task, dict)
    ]


def reconcile_workflow_state(run_root: Path) -> dict[str, object]:
    run_root = run_root.resolve()
    manifest = _read_json(run_root / "reports" / "run-manifest.json")
    state_path = run_root / "reports" / "workflow-state.json"
    previous = _read_json(state_path) if state_path.is_file() else {}
    previous_tasks = {
        item.get("task_id"): item for item in previous.get("tasks", []) if isinstance(item, dict)
    }
    repaired_tasks = repair_task_ids(run_root)
    recheck_tasks = repair_recheck_task_ids(run_root)
    tasks: list[dict[str, object]] = []
    manifest_tasks = [
        task
        for task in manifest.get("implementation_tasks", [])
        if isinstance(task, dict)
    ]
    for task in manifest_tasks:
        task_id = str(task["task_id"])
        prompt_sha = effective_task_prompt_sha256(task, manifest_tasks, run_root)
        phase = phase_for_task(str(task.get("task_type", "control")))
        old = previous_tasks.get(task_id, {})
        result_path = run_root / "reports" / "agent-executions" / f"{task_id}.result.json"
        result = _read_json(result_path) if result_path.is_file() else {}
        required_outputs = task.get("required_output_paths", task.get("allowed_write_paths", []))
        output_hashes = _output_hashes(run_root, required_outputs)
        complete_outputs = len(output_hashes) == len(required_outputs)
        # A successful owner is reusable while its required outputs remain and its
        # task prompt is unchanged. Final conformance and Testing verify their content.
        result_matches = (
            result.get("status") == "SUCCEEDED"
            and complete_outputs
            and result.get("promptSha256", prompt_sha) == prompt_sha
        )
        frozen_recheck = repair_recheck_for_task(run_root, task_id)
        recheck_complete = (
            frozen_recheck is not None
            and result.get("frozenRecheckCandidate")
            == frozen_recheck.get("frozenTestCandidate")
        )
        repair_replay_required = (task_id in recheck_tasks and not recheck_complete) or (
            task_id in repaired_tasks and result.get("promptSha256") != prompt_sha
        )
        repair_only = bool(task.get("repair_only", False))
        if repair_only and task_id not in repaired_tasks:
            # 정상 실행에서는 정형적인 Spring 설정을 generator와 각 기능 작업이 만든다.
            # 최종 검사가 실제 연결 오류를 찾았을 때만 repair plan이 이 작업을 깨운다.
            status = "SUCCEEDED"
        elif old.get("status") == "RUNNING":
            status = (
                "SUCCEEDED"
                if result.get("status") == "SUCCEEDED"
                and complete_outputs
                and not repair_replay_required
                else "INTERRUPTED"
            )
        elif (
            result.get("status") == "NEEDS_INPUT"
            and result.get("promptSha256", prompt_sha) == prompt_sha
        ):
            # A fresh admission decision supersedes a previously successful
            # implementation when the current behavior contract now has a gap.
            status = "NEEDS_INPUT"
        elif (
            old.get("status") == "SUCCEEDED"
            and complete_outputs
            and not repair_replay_required
            and (
                task.get("task_type") != "integration-implementation"
                or result.get("promptSha256") == prompt_sha
            )
        ) or (result_matches and not old):
            status = "SUCCEEDED"
        elif (
            result.get("status") == "FAILED"
            and result.get("promptSha256", prompt_sha) == prompt_sha
        ):
            status = "FAILED"
        elif (
            result.get("status") == "INTERRUPTED"
            and result.get("promptSha256", prompt_sha) == prompt_sha
        ):
            status = "INTERRUPTED"
        else:
            status = "PENDING"
        tasks.append(
            {
                "task_id": task_id,
                "taskType": str(task.get("task_type", "control")),
                "owner": str(task.get("owner", "")),
                "phase": phase,
                # Preserve the explicit owner dependency in the durable checkpoint.
                "dependsOn": [
                    str(item) for item in task.get("depends_on", task.get("dependsOn", []))
                ],
                "allowedWritePaths": [str(item) for item in task.get("allowed_write_paths", [])],
                "allowedWriteRoots": [str(item) for item in task.get("allowed_write_roots", [])],
                "status": status,
                "promptSha256": prompt_sha,
                "outputHashes": output_hashes,
                "attempts": int(old.get("attempts", 0)),
                "resultFile": (
                    result_path.relative_to(run_root).as_posix() if result_path.is_file() else None
                ),
                "lastError": (
                    result.get("error")
                    if result.get("status") in {"FAILED", "INTERRUPTED"}
                    else None
                ),
                "upstreamGap": (
                    result.get("upstreamGap")
                    if result.get("status") == "NEEDS_INPUT"
                    else None
                ),
                "candidateEvidence": (
                    result.get("candidateEvidence")
                    if result.get("status") == "NEEDS_INPUT"
                    else None
                ),
            }
        )

    phases = _phase_states(
        tasks,
        [phase for phase in previous.get("phases", []) if isinstance(phase, dict)],
    )
    current = next(
        (
            phase["phaseId"]
            for phase in phases
            if phase["status"]
            in {"PENDING", "RUNNING", "INTERRUPTED", "FAILED", "NEEDS_INPUT"}
        ),
        next(
            (phase["phaseId"] for phase in phases if phase["status"] == "UNPLANNED"),
            None,
        ),
    )
    pending = [task for task in tasks if task["status"] != "SUCCEEDED"]
    integration_complete = next(
        phase["status"] for phase in phases if phase["phaseId"] == "integration"
    ) == "SUCCEEDED"
    status = (
        "COMPLETE"
        if not pending and integration_complete and previous.get("status") == "COMPLETE"
        else "NEEDS_INPUT"
        if any(task["status"] == "NEEDS_INPUT" for task in pending)
        else "FAILED"
        if any(task["status"] == "FAILED" for task in pending)
        else "INTERRUPTED"
        if any(task["status"] == "INTERRUPTED" for task in pending)
        else ("READY" if pending else "READY_TO_FINALIZE")
    )
    state: dict[str, object] = {
        "schemaVersion": WORKFLOW_SCHEMA,
        "runId": run_root.name,
        "status": status,
        "currentPhase": current,
        "updatedAt": _now(),
        "phases": phases,
        "tasks": tasks,
        "nextRunnableTasks": _next_runnable_tasks(tasks, phases),
        "blockingReason": None,
        "blockingDetails": [],
    }
    if state["status"] == "NEEDS_INPUT":
        state["blockingReason"] = "An implementation task requires upstream design input."
        state["blockingDetails"] = [
            {
                "kind": "upstream_contract_gap",
                "taskId": task["task_id"],
                "sourceRef": (
                    task["upstreamGap"].get("sourceRef")
                    if isinstance(task.get("upstreamGap"), dict)
                    else None
                ),
                "summary": (
                    task["upstreamGap"].get("summary")
                    if isinstance(task.get("upstreamGap"), dict)
                    else None
                ),
                **(
                    {"options": task["upstreamGap"]["options"]}
                    if isinstance(task.get("upstreamGap"), dict)
                    and isinstance(task["upstreamGap"].get("options"), list)
                    and task["upstreamGap"]["options"]
                    else {}
                ),
            }
            for task in pending
            if task["status"] == "NEEDS_INPUT"
        ]
    elif state["status"] == "INTERRUPTED":
        interrupted_task = next(
            task for task in pending if task["status"] == "INTERRUPTED"
        )
        state["blockingReason"] = str(
            interrupted_task.get("lastError")
            or f"Implementation task interrupted: {interrupted_task['task_id']}"
        )
    elif not state["nextRunnableTasks"] and pending:
        task_status = {str(task["task_id"]): str(task.get("status")) for task in tasks}
        state["status"] = "NEEDS_PLANNER"
        state["blockingReason"] = (
            "No runnable implementation task; an incomplete task has a missing or "
            "unsatisfied dependency."
        )
        state["blockingDetails"] = [
            {
                "taskId": task["task_id"],
                "status": task.get("status"),
                "blockedBy": [
                    str(dependency)
                    for dependency in task.get("dependsOn", [])
                    if task_status.get(str(dependency)) != "SUCCEEDED"
                ],
            }
            for task in pending
        ]
    _write_json_atomic(state_path, state)
    return state


def run_workflow(
    run_root: Path,
    spec: JobSpec,
    *,
    retry_failed: bool = False,
    executor: Callable[[Path, str], dict[str, object]] = execute_openhands_task,
    auditor: Callable[[Path], dict[str, object]] = audit_run_completion,
) -> dict[str, object]:
    """Trace the implementation workflow independently of its caller process."""

    with langsmith_metrics.trace_scope(
        "easydep.implementation.workflow",
        metadata={
            "agent": "implementation",
            "operation": "workflow",
            "run_id": run_root.name,
            "app_id": spec.app_id,
        },
    ):
        return _run_workflow(
            run_root,
            spec,
            retry_failed=retry_failed,
            executor=executor,
            auditor=auditor,
        )


def _run_workflow(
    run_root: Path,
    spec: JobSpec,
    *,
    retry_failed: bool = False,
    executor: Callable[[Path, str], dict[str, object]] = execute_openhands_task,
    auditor: Callable[[Path], dict[str, object]] = audit_run_completion,
) -> dict[str, object]:
    """Resume planned phases, checkpointing before and after every external task."""
    run_root = run_root.resolve()
    state = plan_workflow(run_root, spec)
    if state.get("status") == "COMPLETE":
        return state
    if state.get("status") == "NEEDS_INPUT":
        return state
    runnable = list(state.get("nextRunnableTasks", []))
    failed_runnable = [
        task_id
        for task_id in runnable
        if next(task for task in state["tasks"] if task["task_id"] == task_id)["status"] == "FAILED"
    ]
    if failed_runnable and not retry_failed:
        raise RuntimeError(
            "Workflow has failed tasks; inspect evidence and use --retry-failed: "
            + ", ".join(failed_runnable)
        )
    if not runnable:
        if state.get("status") == "NEEDS_PLANNER":
            return state
        return _finalize_workflow(
            run_root,
            spec,
            state,
            auditor=auditor,
        )

    # 구현 단계 진입이 곧 실행 요청이다. 별도의 승인 파일 없이 현재 dependency가
    # 충족된 작업만 실행하고, 다음 묶음은 갱신된 workflow에서 이어서 고른다.
    authorized_task_ids = set(runnable)
    state["status"] = "RUNNING"
    _write_json_atomic(run_root / "reports" / "workflow-state.json", state)

    while True:
        runnable_phases: list[str] = []
        runnable_tasks: list[dict[str, object]] = []
        for phase_id, _dependencies, _types in PHASES:
            phase_tasks = [
                task
                for task in state["tasks"]
                if task["phase"] == phase_id
                and task["task_id"] in authorized_task_ids
                and (
                    task["status"] in {"PENDING", "INTERRUPTED"}
                    or (retry_failed and task["status"] == "FAILED")
                )
            ]
            if phase_tasks and _dependencies_succeeded(state, phase_id):
                runnable_phases.append(phase_id)
                runnable_tasks.extend(phase_tasks)
        if not runnable_tasks:
            break

        # The dependency graph makes one owner phase runnable at a time.
        state["currentPhase"] = runnable_phases[0]
        state["currentPhases"] = runnable_phases
        failures = _execute_task_batch(
            run_root,
            state,
            runnable_tasks,
            executor,
        )
        if failures:
            task, error = failures[0]
            if isinstance(error, OwnerConversationIncomplete):
                # The candidate and SDK checkpoint are already preserved.
                # This is an execution budget/stuck boundary, not evidence
                # for creating another source-repair prompt.
                paused_state = plan_workflow(run_root, spec)
                paused_state["blockingReason"] = str(error)
                _write_json_atomic(
                    run_root / "reports" / "workflow-state.json",
                    paused_state,
                )
                return paused_state
            if isinstance(error, WorkspaceVerificationError):
                repair = schedule_cross_phase_repair(
                    run_root, str(task["task_id"]), error.evidence
                )
                if repair is not None:
                    repaired_state = plan_workflow(run_root, spec)
                    repaired_state["repairPlan"] = "reports/repair-plan.json"
                    repaired_state["blockingReason"] = None
                    _write_json_atomic(
                        run_root / "reports" / "workflow-state.json",
                        repaired_state,
                    )
                    return repaired_state
            raise error
        if any(task["status"] == "NEEDS_INPUT" for task in runnable_tasks):
            return plan_workflow(run_root, spec)
        for phase_id in runnable_phases:
            next(phase for phase in state["phases"] if phase["phaseId"] == phase_id)["status"] = (
                "SUCCEEDED"
            )
        state["updatedAt"] = _now()
        _write_json_atomic(run_root / "reports" / "workflow-state.json", state)
    state.pop("currentPhases", None)

    # Reconcile the completed owner result and any short repair directive.
    final_state = plan_workflow(run_root, spec)
    if final_state.get("status") in {"NEEDS_INPUT", "NEEDS_PLANNER"}:
        return final_state
    if final_state.get("nextRunnableTasks"):
        # Every work unit performs its own focused verification.  Do not scan
        # the incomplete application after each work unit; the final audit and
        # full workspace build run only when no work unit remains.
        final_state["status"] = "READY"
        final_state["blockingReason"] = None
        final_state.pop("currentActivity", None)
        _write_json_atomic(run_root / "reports" / "workflow-state.json", final_state)
        return final_state
    return _finalize_workflow(
        run_root,
        spec,
        final_state,
        auditor=auditor,
    )


def _finalize_workflow(
    run_root: Path,
    spec: JobSpec,
    state: dict[str, object],
    *,
    auditor: Callable[[Path], dict[str, object]],
) -> dict[str, object]:
    """작업 완결성과 설계 계약을 확인하고 Testing에 넘길 파일을 만든다.

    작업별 compile·관련 테스트는 각 코딩 에이전트가 이미 통과했다. 전체 Gradle 테스트,
    frontend build와 실제 container 실행은 저장된 동일 산출물을 사용하는 Testing 단계가 담당한다.
    단, 기존 코드를 고친 피드백 작업은 영향 범위가 여러 작업에 걸칠 수 있으므로 backend 테스트를
    한 번 다시 통과한 뒤 Testing으로 넘긴다.
    """
    state["status"] = "FINALIZING"
    state["currentPhase"] = "integration"
    _set_phase_status(state, "integration", "RUNNING")
    state["blockingReason"] = None
    state["currentActivity"] = {
        "id": "integration",
        "owner": "integration",
        "phase": "integration",
        "label": "Integration verification",
        "status": "RUNNING",
        "detail": "Checking owner outputs and preparing the Testing handoff.",
    }
    _write_json_atomic(run_root / "reports" / "workflow-state.json", state)

    try:
        audit = auditor(run_root)
    except Exception as error:
        _record_workflow_failure(run_root, state, error)
        raise
    state["audit"] = "reports/implementation-completion-audit.json"
    if audit.get("status") != "COMPLETE":
        repaired = _continue_after_incomplete_audit(run_root, spec, audit)
        if repaired is not None:
            return repaired
        state["status"] = "NEEDS_PLANNER"
        state["blockingReason"] = "The audit contains work for which no implementation task exists."
        state.pop("currentActivity", None)
        _write_json_atomic(run_root / "reports" / "workflow-state.json", state)
        return state

    try:
        conformance = verify_source_design_conformance(run_root, spec)
    except SourceDesignConformanceError as error:
        repaired = _continue_after_conformance_failure(run_root, spec, error)
        if repaired is not None:
            return repaired
        _record_workflow_failure(run_root, state, error)
        raise

    integration_owner = next(
        (
            task
            for task in state.get("tasks", [])
            if isinstance(task, dict)
            and task.get("taskType") == "integration-implementation"
            and task.get("status") == "SUCCEEDED"
        ),
        None,
    )
    if integration_owner is not None:
        state["backendRegression"] = str(integration_owner.get("resultFile") or "")
    elif not str(getattr(spec, "repair_task_type", "")).startswith("testing-"):
        feedback_revision = spec.job_type == "FEEDBACK_REVISION"
        regression_report = (
            "feedback-regression.json"
            if feedback_revision
            else "backend-regression.json"
        )
        state["currentActivity"] = {
            "id": "backend-regression",
            "owner": "integration",
            "phase": "integration",
            "label": "Backend regression tests",
            "status": "RUNNING",
            "detail": "Running the complete backend test suite once after all implementation slices.",
        }
        _write_json_atomic(run_root / "reports" / "workflow-state.json", state)
        try:
            verify_run_workspace(
                run_root,
                regression_report,
                verify_frontend=False,
                verify_end_to_end=False,
            )
        except WorkspaceVerificationError as error:
            repaired = _continue_after_backend_regression_failure(
                run_root,
                spec,
                error,
                failed_task_id=(
                    "apply-source-feedback"
                    if feedback_revision
                    else _regression_owner_task_id(run_root, error.evidence)
                ),
            )
            if repaired is not None:
                return repaired
            _record_workflow_failure(run_root, state, error)
            raise
        state["backendRegression"] = f"reports/{regression_report}"
        if feedback_revision:
            state["feedbackRegression"] = f"reports/{regression_report}"

    _complete_implementation(run_root, spec, state, conformance)
    _set_phase_status(state, "integration", "SUCCEEDED")
    state["blockingReason"] = None
    state["currentActivity"] = {
        "id": "integration",
        "owner": "integration",
        "phase": "integration",
        "label": "Integration verification",
        "status": "SUCCEEDED",
        "detail": "Owner outputs are complete; static and dynamic gates continue in Testing.",
    }
    _write_json_atomic(run_root / "reports" / "workflow-state.json", state)
    return state


def _execute_task_batch(
    run_root: Path,
    state: dict[str, object],
    tasks: list[dict[str, object]],
    executor: Callable[[Path, str], dict[str, object]],
) -> list[tuple[dict[str, object], Exception]]:
    """Execute one owner at a time and stop at the first blocking outcome."""
    state_path = run_root / "reports" / "workflow-state.json"
    for task in tasks:
        task["status"] = "PENDING"
    state["updatedAt"] = _now()
    _write_json_atomic(state_path, state)

    failures: list[tuple[dict[str, object], Exception]] = []
    for task in tasks:
        task["status"] = "RUNNING"
        task["attempts"] = int(task.get("attempts", 0)) + 1
        task["lastError"] = None
        state["updatedAt"] = _now()
        _write_json_atomic(state_path, state)
        try:
            result = executor(run_root, str(task["task_id"]))
            if result.get("status") not in {"SUCCEEDED", "NEEDS_INPUT"}:
                raise RuntimeError(
                    f"Task returned non-success status: {task['task_id']}"
                )
        except Exception as error:
            task["status"] = (
                "INTERRUPTED"
                if isinstance(error, OwnerConversationIncomplete)
                else "FAILED"
            )
            task["lastError"] = str(error)
            failures.append((task, error))
            state["updatedAt"] = _now()
            _write_json_atomic(state_path, state)
            break
        if result.get("status") == "NEEDS_INPUT":
            task["status"] = "NEEDS_INPUT"
            task["resultFile"] = f"reports/agent-executions/{task['task_id']}.result.json"
            task["upstreamGap"] = result.get("upstreamGap")
            task["candidateEvidence"] = result.get("candidateEvidence")
            task["lastError"] = None
            state["updatedAt"] = _now()
            _write_json_atomic(state_path, state)
            break
        task["status"] = "SUCCEEDED"
        task["resultFile"] = f"reports/agent-executions/{task['task_id']}.result.json"
        task["outputHashes"] = _task_output_hashes(run_root, str(task["task_id"]))
        task["lastError"] = None
        state["updatedAt"] = _now()
        _write_json_atomic(state_path, state)

    blocking_failures = failures
    if blocking_failures:
        failed_task, error = blocking_failures[0]
        state["status"] = (
            "INTERRUPTED"
            if isinstance(error, OwnerConversationIncomplete)
            else "FAILED"
        )
        state["blockingReason"] = (
            f"Task interrupted: {failed_task['task_id']}"
            if isinstance(error, OwnerConversationIncomplete)
            else f"Task failed: {failed_task['task_id']}"
        )
        state["updatedAt"] = _now()
        _write_json_atomic(state_path, state)
    return blocking_failures


def _regression_owner_task_id(
    run_root: Path,
    evidence: dict[str, object],
) -> str:
    """Return an unambiguous implementation owner for backend regression repair.

    The backend phase now has one production owner.  Older runs may still
    report an explicit task id, which is safe to honor; parsing a JUnit class
    name to rediscover a historical slice is not, because test files are no
    longer part of the implementation ownership contract.
    """

    for key in ("failedTaskId", "failed_task_id", "taskId", "task_id"):
        value = evidence.get(key)
        if isinstance(value, str) and value:
            return value
    manifest = _read_json(run_root / "reports" / "run-manifest.json")
    owners = [
        task
        for task in manifest.get("implementation_tasks", [])
        if isinstance(task, dict)
        and task.get("task_type") == "backend-implementation"
        and task.get("task_id")
    ]
    if len(owners) == 1:
        return str(owners[0]["task_id"])
    return "backend-regression"


def _record_workflow_failure(run_root: Path, state: dict[str, object], error: Exception) -> None:
    """완료 감사나 산출물 생성 실패를 workflow checkpoint에 기록한다."""
    activity = state.get("currentActivity")
    failed_activity = dict(activity) if isinstance(activity, dict) else {}
    activity_id = str(failed_activity.get("id") or "")
    phase_id = str(failed_activity.get("phase") or "")
    if not phase_id:
        phase_id = activity_id.removeprefix("verify-").removeprefix("audit-")
    if phase_id in PHASE_LABELS:
        for phase in state.get("phases", []):
            if isinstance(phase, dict) and phase.get("phaseId") == phase_id:
                phase["status"] = "FAILED"
                break

    detail = (str(error).strip() or type(error).__name__)[-1000:]
    label = str(failed_activity.get("label") or "Implementation verification")
    failed_activity.update(
        {
            "id": activity_id or "workflow-verification",
            "owner": str(failed_activity.get("owner") or phase_id or "integration"),
            "phase": phase_id or "integration",
            "label": label,
            "status": "FAILED",
            "detail": f"{label} failed: {detail}",
        }
    )
    state["currentActivity"] = failed_activity
    state["status"] = "FAILED"
    state["blockingReason"] = failed_activity["detail"]
    state["updatedAt"] = _now()
    _write_json_atomic(run_root / "reports" / "workflow-state.json", state)


def _continue_after_incomplete_audit(
    run_root: Path,
    spec: JobSpec,
    audit: dict[str, object],
) -> dict[str, object] | None:
    """마지막 감사가 기존 task의 부족한 산출물을 찾으면 그 task부터 다시 실행한다.

    감사 결과에는 이미 담당 ``task_id``가 들어 있다. 새 planner나 사용자 선택을
    요구하지 않고, 해당 task에 감사 근거를 붙인 뒤 같은 실행에서 자동 수리한다.
    아직 구현 task가 없는 새 종류의 작업만 기존 ``NEEDS_PLANNER`` 상태로 남는다.
    """

    backlog = audit.get("backlog")
    if not isinstance(backlog, list):
        return None
    for item in backlog:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("task_id") or "")
        if not task_id:
            continue
        repair = schedule_cross_phase_repair(
            run_root,
            task_id,
            {
                "command": ["completion-audit"],
                "exitCode": 1,
                "stderr": json.dumps(item, ensure_ascii=False, indent=2),
            },
        )
        if repair is None:
            continue
        state = plan_workflow(run_root, spec)
        state["repairPlan"] = "reports/repair-plan.json"
        state["blockingReason"] = None
        _write_json_atomic(run_root / "reports" / "workflow-state.json", state)
        return state
    return None


def workflow_status(run_root: Path) -> dict[str, object]:
    path = run_root.resolve() / "reports" / "workflow-state.json"
    if not path.is_file():
        raise ValueError("Workflow has not been planned for this run")
    return _read_json(path)


def run_workflow_to_completion(
    run_root: Path,
    spec: JobSpec,
    *,
    retry_failed: bool = False,
    max_cycles: int | None = None,
) -> dict[str, object]:
    """완료 또는 명확한 중단 상태까지 실행한다.

    기본 실행에는 repair 횟수 상한이 없다. 각 repair 계획은 실패 지문, 수정 전 코드와 사용한
    전략을 저장하며, 같은 결과가 반복되어도 이 이력을 다음 요청에 포함해 다른 수정을 시도한다.
    ``max_cycles``는 테스트가 실행 주기를 제한할 때 쓰는 선택 사항이다.
    """
    run_root = run_root.resolve()
    cycle = 0
    while True:
        cycle += 1
        state = plan_workflow(run_root, spec)
        if state.get("status") == "COMPLETE":
            return state
        state = run_workflow(
            run_root,
            spec,
            retry_failed=retry_failed,
        )
        status = str(state.get("status", ""))
        if status == "COMPLETE":
            _write_json_atomic(run_root / "reports" / "workflow-state.json", state)
            return state
        if status in {"FAILED", "NEEDS_INPUT", "NEEDS_PLANNER"}:
            raise RuntimeError(
                f"Run-to-completion stopped in {status}: {state.get('blockingReason')}"
            )
        if max_cycles is not None and cycle >= max_cycles:
            raise RuntimeError(f"Run-to-completion exceeded {max_cycles} workflow cycles")


def bind_deployment_runtime(run_root: Path, deployment_bundle: Path) -> Path | None:
    """완료된 배포 설계에 생성 앱의 실제 실행값을 넣어 새 bundle을 만든다.

    원래 설계 파일은 입력 snapshot이므로 수정하지 않는다. 배포 설계가 아직 질문을
    남긴 상태라면 로컬 Docker 검증만 계속하고, 완료된 설계에서 실행 계약이 다르면
    IaC를 만들지 않고 구현 오류로 보고한다.
    """
    source = deployment_bundle
    if not source.is_file():
        return None
    try:
        bundle = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Deployment bundle could not be read: {error}") from error
    if not isinstance(bundle, dict) or bundle.get("schemaVersion") != "easydep-deployment-diagram":
        raise ValueError("Implementation requires a valid deployment diagram bundle")

    reports = run_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / "deployment-runtime.json"
    observed = observe_runtime_contract(bundle, run_root / "application")
    if bundle.get("status") != "completed":
        _write_json_atomic(
            report_path,
            {
                "schemaVersion": "easydep-implementation-runtime/v1alpha1",
                "status": "NOT_APPLICABLE",
                "reason": "Deployment design still needs input; local container verification continues.",
                "runtimeContracts": observed,
            },
        )
        return None

    graph = bundle.get("workloadGraph")
    projections = bundle.get("projections")
    if not isinstance(graph, dict) or not isinstance(projections, list) or not projections:
        raise ValueError("Completed deployment bundle has no workload graph or projections")
    selected_target = bundle.get("selectedTarget")
    if not isinstance(selected_target, dict):
        # A persisted v1 single-target bundle predates selectedTarget. It is
        # still deterministic; alternatives must be selected explicitly.
        if len(projections) != 1 or not isinstance(projections[0], dict):
            raise ValueError("Completed deployment bundle has no selected target")
        selected_target = {
            "provider": projections[0].get("provider"),
            "region": projections[0].get("region"),
        }
    selected_id = str(selected_target.get("id") or "")
    selected_matches = [
        projection
        for projection in projections
        if isinstance(projection, dict)
        and (
            str(projection["target"].get("id") or "") == selected_id
            if selected_id and isinstance(projection.get("target"), dict)
            else str(projection.get("provider") or "").lower()
            == str(selected_target.get("provider") or "").lower()
            and str(projection.get("region") or "") == str(selected_target.get("region") or "")
        )
    ]
    if len(selected_matches) != 1 or selected_matches[0].get("status") != "completed":
        raise ValueError("Selected deployment target has no completed projection")
    projection = selected_matches[0]
    deployment_plan = projection.get("deploymentPlan")
    if not isinstance(deployment_plan, dict):
        raise TypeError("Selected deployment projection has no deployment plan")
    binding = bind_runtime_contract(graph, deployment_plan, observed)
    if binding.get("status") != "bound":
        issues = binding.get("issues") or []
        _write_json_atomic(
            report_path,
            {
                "schemaVersion": "easydep-implementation-runtime/v1alpha1",
                "status": "FAILED",
                "runtimeContracts": observed,
                "issues": issues,
            },
        )
        reasons = [str(item.get("reason") or item) for item in issues if isinstance(item, dict)]
        raise RuntimeError(
            "Generated application does not satisfy the deployment runtime contract: "
            + "; ".join(reasons or ["unknown runtime mismatch"])
        )
    current_graph = binding.get("workloadGraph")
    current_plan = binding.get("deploymentPlan")
    if not isinstance(current_graph, dict) or not isinstance(current_plan, dict):
        raise TypeError("Runtime binding returned no bound graph or deployment plan")
    resource_plan = build_provider_resource_plan(
        current_plan,
        current_graph,
        provider=str(projection.get("provider") or ""),
        region=str(projection.get("region") or ""),
    )
    previous_digest = str(projection.get("resourcePlanStructureDigest") or "")
    current_digest = str(resource_plan.get("structureDigest") or "")
    if previous_digest and previous_digest != current_digest:
        raise RuntimeError("Runtime binding changed the ResourcePlan structure")
    rebound = [
        {
            **item,
            "deploymentPlan": current_plan,
            "deploymentPlanStructureDigest": current_plan.get("structureDigest"),
            "resourcePlan": resource_plan,
            "resourcePlanStructureDigest": current_digest,
            "issues": [],
        }
        if item is projection
        else item
        for item in projections
        if isinstance(item, dict)
    ]

    bound_bundle = {**bundle, "workloadGraph": current_graph, "projections": rebound}
    target = reports / "runtime-bound-deployment-bundle.json"
    _write_json_atomic(target, bound_bundle)
    _write_json_atomic(
        report_path,
        {
            "schemaVersion": "easydep-implementation-runtime/v1alpha1",
            "status": "BOUND",
            "runtimeContracts": observed,
            "boundBundle": target.relative_to(run_root).as_posix(),
        },
    )
    return target


def _render_deployment_if_configured(
    run_root: Path, spec: JobSpec
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    deployment = spec.inputs.get("deployment")
    deployment_bundle = spec.inputs.get("deploymentBundle")
    has_bundle = bool(deployment_bundle and deployment_bundle.is_file())
    if has_bundle:
        # observer가 실제 EXPOSE와 실행 사용자를 읽을 수 있도록 먼저 결정론적인
        # 로컬 Dockerfile을 만든다. 이후 IaC는 같은 파일과 bound bundle을 사용한다.
        render_local_container(run_root)
    bound_bundle = (
        bind_deployment_runtime(run_root, deployment_bundle)
        if has_bundle and deployment_bundle is not None
        else None
    )
    if (
        deployment
        and deployment.is_file()
        and not (deployment_bundle and deployment_bundle.is_file())
    ):
        raise ValueError(
            "Deployment rendering requires deploymentIntent or a cloud resource specification"
        )
    else:
        deployment_report = None
    iac_report = None
    if bound_bundle is not None:
        iac_spec = replace(
            spec,
            inputs={**spec.inputs, "deploymentBundle": bound_bundle},
        )
        iac_report = render_iac(run_root, iac_spec)
    return deployment_report, iac_report


def _complete_implementation(
    run_root: Path,
    spec: JobSpec,
    state: dict[str, object],
    conformance: dict[str, object],
) -> None:
    """배포 입력을 코드로 만든 뒤 구현 산출물을 완료 상태로 바꾼다."""
    state["status"] = "COMPLETE"
    state["sourceDesignConformance"] = conformance.get("status")
    try:
        deployment, _iac = _render_deployment_if_configured(run_root, spec)
        if deployment is None:
            render_local_container(run_root)
        build_rtm_traceability_map(spec, run_root)
    except Exception as error:
        state["status"] = "FAILED"
        _set_phase_status(state, "integration", "FAILED")
        state["blockingReason"] = str(error)
        _write_json_atomic(run_root / "reports" / "workflow-state.json", state)
        raise
    state["testingRequired"] = True


def _continue_after_conformance_failure(
    run_root: Path,
    spec: JobSpec,
    error: SourceDesignConformanceError,
) -> dict[str, object] | None:
    """공개 계약 오류를 사용자 버튼 대신 기존 기능 작업의 수리로 되돌린다."""
    repair = schedule_source_conformance_repair(run_root, error.report)
    if repair is None:
        return None
    state = plan_workflow(run_root, spec)
    state["repairPlan"] = "reports/repair-plan.json"
    state["blockingReason"] = None
    state.pop("currentActivity", None)
    _write_json_atomic(run_root / "reports" / "workflow-state.json", state)
    return state


def _continue_after_backend_regression_failure(
    run_root: Path,
    spec: JobSpec,
    error: WorkspaceVerificationError,
    *,
    failed_task_id: str,
) -> dict[str, object] | None:
    """Route the one final backend regression failure to its owning slice."""
    repair = schedule_cross_phase_repair(
        run_root,
        failed_task_id,
        error.evidence,
    )
    if repair is None:
        return None
    state = plan_workflow(run_root, spec)
    state["repairPlan"] = "reports/repair-plan.json"
    state["blockingReason"] = None
    state.pop("currentActivity", None)
    _write_json_atomic(run_root / "reports" / "workflow-state.json", state)
    return state


def phase_for_task(task_type: str) -> str:
    for phase_id, _, types in PHASES:
        if task_type in types:
            return phase_id
    # 모르는 task를 조용히 건너뛰면 기존 파일이 있다는 이유만으로 workflow가 완료될
    # 수 있다. 새 task 종류를 추가할 때 실행 phase 연결도 함께 하도록 즉시 알린다.
    raise ValueError(f"Unknown implementation task type: {task_type}")


def _phase_states(
    tasks: list[dict[str, object]],
    previous: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    previous_statuses = {
        str(phase.get("phaseId")): str(phase.get("status"))
        for phase in previous or []
    }
    phases: list[dict[str, object]] = []
    for phase_id, dependencies, _ in PHASES:
        phase_tasks = [task for task in tasks if task["phase"] == phase_id]
        if phase_id == "integration":
            status = (
                "SUCCEEDED"
                if previous_statuses.get(phase_id) == "SUCCEEDED"
                and all(task["status"] == "SUCCEEDED" for task in tasks)
                else "PENDING"
            )
        elif not phase_tasks:
            status = "UNPLANNED"
        elif all(task["status"] == "SUCCEEDED" for task in phase_tasks):
            status = "SUCCEEDED"
        elif any(task["status"] == "RUNNING" for task in phase_tasks):
            status = "RUNNING"
        elif any(task["status"] == "NEEDS_INPUT" for task in phase_tasks):
            status = "NEEDS_INPUT"
        elif any(task["status"] == "FAILED" for task in phase_tasks):
            status = "FAILED"
        elif any(task["status"] == "INTERRUPTED" for task in phase_tasks):
            status = "INTERRUPTED"
        else:
            status = "PENDING"
        phases.append(
            {
                "phaseId": phase_id,
                "dependsOn": list(dependencies),
                "status": status,
                "taskIds": [task["task_id"] for task in phase_tasks],
            }
        )
    return phases


def _set_phase_status(
    state: dict[str, object], phase_id: str, status: str
) -> None:
    for phase in state.get("phases", []):
        if isinstance(phase, dict) and phase.get("phaseId") == phase_id:
            phase["status"] = status
            return


def _next_runnable_tasks(
    tasks: list[dict[str, object]], phases: list[dict[str, object]]
) -> list[str]:
    phase_by_id = {phase["phaseId"]: phase for phase in phases}
    task_by_id = {str(task["task_id"]): task for task in tasks}
    runnable: list[str] = []
    for phase_id, dependencies, _ in PHASES:
        candidates = [
            str(task["task_id"])
            for task in tasks
            if task["phase"] == phase_id
            and task["status"] in {"PENDING", "INTERRUPTED", "FAILED"}
            and all(
                task_by_id.get(str(dependency), {}).get("status") == "SUCCEEDED"
                for dependency in task.get("dependsOn", [])
            )
        ]
        if candidates and all(
            phase_by_id[dependency]["status"] in {"SUCCEEDED", "UNPLANNED"}
            for dependency in dependencies
        ):
            runnable.extend(candidates)
    return runnable


def _dependencies_succeeded(state: dict[str, object], phase_id: str) -> bool:
    phase_by_id = {phase["phaseId"]: phase for phase in state["phases"]}
    dependencies = next(item[1] for item in PHASES if item[0] == phase_id)
    return all(phase_by_id[item]["status"] in {"SUCCEEDED", "UNPLANNED"} for item in dependencies)


def _task_output_hashes(run_root: Path, task_id: str) -> dict[str, str]:
    manifest = _read_json(run_root / "reports" / "run-manifest.json")
    task = next(
        item for item in manifest.get("implementation_tasks", []) if item.get("task_id") == task_id
    )
    return _output_hashes(
        run_root,
        task.get("required_output_paths", task.get("allowed_write_paths", [])),
    )


def _output_hashes(run_root: Path, relative_paths: list[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in relative_paths:
        path = run_root / relative
        if path.is_file():
            hashes[str(relative)] = _sha256(path)
    return hashes


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        # A shared ``workflow-state.json.tmp`` collides when a retry or a
        # parallel worker persists state at the same time.  Windows also
        # briefly rejects replace while an antivirus/indexer has the target
        # open, so use a unique file and retry only that transient operation.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
        temporary = Path(temporary_name)
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                temporary_name = None
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _now() -> str:
    return datetime.now(UTC).isoformat()
