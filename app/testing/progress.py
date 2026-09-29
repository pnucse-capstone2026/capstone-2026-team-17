"""Small, deterministic Testing progress events and checkpoint projection."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

TestingProgressObserver = Callable[[dict[str, Any]], None]

_observer: ContextVar[TestingProgressObserver | None] = ContextVar(
    "easydep_testing_progress_observer",
    default=None,
)

_PHASES = frozenset({"prepare", "planning", "dynamic", "static", "summary", "repair", "rerun"})
_SCOPES = frozenset({"phase", "workflow", "step", "gate"})
_STATUSES = frozenset(
    {
        "PENDING",
        "RUNNING",
        "PASS",
        "FAIL",
        "INCONCLUSIVE",
        "DEFERRED",
        "REUSED",
        "SKIPPED",
    }
)
_log = logging.getLogger(__name__)


def testing_progress_event(
    *,
    phase: str,
    scope: str,
    status: str,
    label: str,
    detail: str = "",
    workflow_id: str = "",
    use_case_id: str = "",
    use_case_name: str = "",
    step_id: str = "",
    gate: str = "",
    operation_id: str = "",
    method: str = "",
    path: str = "",
    status_code: int | None = None,
    contract_status: str = "",
    semantic_status: str = "",
    control: str = "",
    attempt: int | None = None,
    completed_workflows: int | None = None,
    total_workflows: int | None = None,
    total_steps: int | None = None,
    elapsed_ms: int | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Build the one public progress-event shape used by Testing."""

    normalized_status = status.upper()
    if phase not in _PHASES:
        raise ValueError(f"Unknown Testing progress phase: {phase}")
    if scope not in _SCOPES:
        raise ValueError(f"Unknown Testing progress scope: {scope}")
    if normalized_status not in _STATUSES:
        raise ValueError(f"Unknown Testing progress status: {status}")
    if not label.strip():
        raise ValueError("Testing progress events require a label.")
    if scope in {"workflow", "step"} and not workflow_id:
        raise ValueError(f"Testing {scope} progress requires workflow_id.")
    if scope == "step" and not step_id:
        raise ValueError("Testing step progress requires step_id.")
    if scope == "gate" and not gate:
        raise ValueError("Testing gate progress requires gate.")

    values: dict[str, Any] = {
        "progress_event": "testingProgressUpdated",
        "phase": phase,
        "scope": scope,
        "status": normalized_status,
        "progress_status": normalized_status.lower(),
        "progress_card_label": "Testing progress",
        "progress_step_label": label.strip(),
        "updated_at": updated_at or datetime.now(UTC).isoformat(),
    }
    optional: dict[str, Any] = {
        "progress_detail": detail.strip(),
        "workflow_id": workflow_id,
        "use_case_id": use_case_id,
        "use_case_name": use_case_name,
        "step_id": step_id,
        "gate": gate,
        "operation_id": operation_id,
        "method": method.upper(),
        "path": path,
        "status_code": status_code,
        "contract_status": contract_status.upper(),
        "semantic_status": semantic_status.upper(),
        "control": control,
        "attempt": attempt,
        "completed_workflows": completed_workflows,
        "total_workflows": total_workflows,
        "total_steps": total_steps,
        "elapsed_ms": elapsed_ms,
    }
    values.update(
        {
            key: value
            for key, value in optional.items()
            if value is not None and value != ""
        }
    )
    return values


def emit_testing_progress(
    observer: TestingProgressObserver | None = None,
    **values: Any,
) -> None:
    """Notify an optional observer without changing the Testing outcome."""

    target = observer or _observer.get()
    if target is None:
        return
    try:
        target(testing_progress_event(**values))
    except Exception:  # noqa: BLE001 - UI observation must never fail a test run.
        _log.warning("Testing progress observer failed.", exc_info=True)


def emit_dynamic_workflow_terminal(
    *,
    status: str,
    label: str,
    workflow_id: str,
    use_case_id: str = "",
    use_case_name: str = "",
    total_steps: int | None = None,
    completed_workflows: int | None = None,
    total_workflows: int | None = None,
    elapsed_ms: int | None = None,
) -> None:
    """Emit the shared terminal event for one dynamic workflow."""

    emit_testing_progress(
        phase="dynamic",
        scope="workflow",
        status=status,
        label=label,
        workflow_id=workflow_id,
        use_case_id=use_case_id,
        use_case_name=use_case_name,
        total_steps=total_steps,
        completed_workflows=completed_workflows,
        total_workflows=total_workflows,
        elapsed_ms=elapsed_ms,
    )


def emit_dynamic_workflow_planned(
    *,
    label: str,
    workflow_id: str,
    use_case_id: str = "",
    use_case_name: str = "",
    total_workflows: int,
    total_steps: int | None = None,
) -> None:
    """Emit the shared dynamic execution-lane row before it can finish."""

    emit_testing_progress(
        phase="dynamic",
        scope="workflow",
        status="PENDING",
        label=label,
        workflow_id=workflow_id,
        use_case_id=use_case_id,
        use_case_name=use_case_name,
        total_workflows=total_workflows,
        total_steps=total_steps,
    )


def testing_progress_enabled() -> bool:
    """Return whether the current execution context has a progress consumer."""

    return _observer.get() is not None


@contextmanager
def testing_progress_scope(observer: TestingProgressObserver | None):
    """Expose one observer to nested graph, executor, and tool calls."""

    token: Token[TestingProgressObserver | None] = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def _record_values(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(event[key])
        for key in (
            "status",
            "progress_step_label",
            "progress_detail",
            "updated_at",
            "elapsed_ms",
            "operation_id",
            "method",
            "path",
            "status_code",
            "contract_status",
            "semantic_status",
            "control",
            "attempt",
            "use_case_id",
            "use_case_name",
        )
        if key in event
    }


def _workflow_counts(workflows: Mapping[str, Any]) -> dict[str, int]:
    counts = {
        "total": len(workflows),
        "passed": 0,
        "failed": 0,
        "running": 0,
        "pending": 0,
        "reused": 0,
        "inconclusive": 0,
        "deferred": 0,
    }
    mapping = {
        "PASS": "passed",
        "FAIL": "failed",
        "RUNNING": "running",
        "PENDING": "pending",
        "REUSED": "reused",
        "INCONCLUSIVE": "inconclusive",
        "DEFERRED": "deferred",
    }
    for value in workflows.values():
        if not isinstance(value, Mapping):
            continue
        bucket = mapping.get(str(value.get("status") or "").upper())
        if bucket:
            counts[bucket] += 1
    return counts


def reduce_testing_progress(
    current: Mapping[str, Any] | None,
    event: Mapping[str, Any],
) -> dict[str, Any]:
    """Fold one normalized event into the restart-safe progress snapshot."""

    if event.get("progress_event") != "testingProgressUpdated":
        raise ValueError("Only testingProgressUpdated events can update Testing progress.")
    phase = str(event.get("phase") or "")
    scope = str(event.get("scope") or "")
    status = str(event.get("status") or "").upper()
    if phase not in _PHASES or scope not in _SCOPES or status not in _STATUSES:
        raise ValueError("Testing progress event is not normalized.")

    result = deepcopy(dict(current or {}))
    result.update(
        {
            "phase": phase,
            "status": status,
            "updated_at": event.get("updated_at"),
            "last_event": deepcopy(dict(event)),
        }
    )
    phases = result.setdefault("phases", {})
    if not isinstance(phases, dict):
        phases = result["phases"] = {}
    if scope == "phase":
        phases[phase] = _record_values(event)

    # Plan validation is not an application-test PASS. Keep these rows separate
    # from execution workflows so the test result counters remain truthful.
    if phase == "planning" and scope == "workflow":
        workflow_id = str(event.get("workflow_id") or "")
        plans = result.setdefault("plans", {})
        plans[workflow_id] = {"workflow_id": workflow_id, **_record_values(event)}
        result["plan_counts"] = _workflow_counts(plans)
        return result

    workflows = result.setdefault("workflows", {})
    if not isinstance(workflows, dict):
        workflows = result["workflows"] = {}
    workflow_id = str(event.get("workflow_id") or "")
    if scope in {"workflow", "step"} and workflow_id:
        workflow = workflows.setdefault(workflow_id, {"workflow_id": workflow_id, "steps": {}})
        if not isinstance(workflow, dict):
            workflow = workflows[workflow_id] = {
                "workflow_id": workflow_id,
                "steps": {},
            }
        if scope == "workflow":
            workflow.update(_record_values(event))
            if event.get("total_steps") is not None:
                workflow["total_steps"] = event["total_steps"]
        else:
            steps = workflow.setdefault("steps", {})
            if not isinstance(steps, dict):
                steps = workflow["steps"] = {}
            step_id = str(event.get("step_id") or "")
            steps[step_id] = {"step_id": step_id, **_record_values(event)}

    gates = result.setdefault("gates", {})
    if not isinstance(gates, dict):
        gates = result["gates"] = {}
    gate = str(event.get("gate") or "")
    if scope == "gate" and gate:
        gates[gate] = {"gate": gate, **_record_values(event)}

    if status == "RUNNING":
        result["active_workflow_id"] = workflow_id if scope in {"workflow", "step"} else ""
        result["active_step_id"] = str(event.get("step_id") or "") if scope == "step" else ""
        result["active_gate"] = gate if scope == "gate" else ""
    else:
        if (
            scope == "workflow"
            and workflow_id
            and result.get("active_workflow_id") == workflow_id
        ):
            result["active_workflow_id"] = ""
        if (
            scope == "step"
            and event.get("step_id")
            and result.get("active_step_id") == event.get("step_id")
        ):
            result["active_step_id"] = ""
        if scope == "gate" and gate and result.get("active_gate") == gate:
            result["active_gate"] = ""

    previous_counts = result.get("workflow_counts")
    counts = _workflow_counts(workflows)
    if isinstance(previous_counts, Mapping):
        counts["total"] = max(counts["total"], int(previous_counts.get("total") or 0))
    result["workflow_counts"] = counts
    if event.get("completed_workflows") is not None:
        result["workflow_counts"]["completed"] = int(event["completed_workflows"])
    if event.get("total_workflows") is not None:
        result["workflow_counts"]["total"] = int(event["total_workflows"])
    return result


__all__ = [
    "TestingProgressObserver",
    "emit_dynamic_workflow_planned",
    "emit_dynamic_workflow_terminal",
    "emit_testing_progress",
    "reduce_testing_progress",
    "testing_progress_enabled",
    "testing_progress_event",
    "testing_progress_scope",
]
