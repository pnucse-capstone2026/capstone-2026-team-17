from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.testing.nodes.dynamic_functional import dynamic_functional_node
from app.testing.nodes.static_verification import static_verification_node
from app.testing.progress import emit_testing_progress
from app.testing.schemas.testing_state import TestingState
from app.testing.utils.gates import gate_status


def _dynamic_branch(state: TestingState) -> dict[str, Any]:
    """Run dynamic verification without writing keys owned by the static branch."""

    update = dict(dynamic_functional_node(state))
    update.pop("current_node", None)

    scope = state.get("gate_scope")
    selected = (
        set(scope)
        if scope is not None
        else {"static", "package", "iac", "dynamicFunctional"}
    )
    # static-only repair는 이전 dynamic FAIL을 고치는 작업이 아니다. 이전 report를
    # 재사용했더라도 요청된 정적 gate는 실제로 실행한다.
    if "dynamicFunctional" not in selected:
        return update
    dynamic = update.get("dynamic_functional_report") or {}
    dynamic_status = gate_status(dynamic)
    emit_testing_progress(
        phase="dynamic",
        scope="gate",
        status=(
            "REUSED"
            if dynamic.get("reused") is True
            else dynamic_status
            if dynamic_status in {"PASS", "FAIL", "INCONCLUSIVE"}
            else "SKIPPED"
        ),
        label="Completed dynamic API verification",
        gate="dynamicFunctional",
    )
    return update


def _static_branch(state: TestingState) -> dict[str, Any]:
    """Run static gates without racing on the shared current_node state key."""

    update = dict(static_verification_node(state))
    update.pop("current_node", None)
    return update


def _join_verification(_state: TestingState) -> dict[str, str]:
    """Publish one deterministic node after both independent branches finish."""

    return {"current_node": "verification_complete"}


def create_testing_graph():
    """Run dynamic and static verification concurrently, then join their reports."""
    workflow = StateGraph(TestingState)

    workflow.add_node("dynamic_functional", _dynamic_branch)
    workflow.add_node("static_verification", _static_branch)
    workflow.add_node("join_verification", _join_verification)

    workflow.add_edge(START, "dynamic_functional")
    workflow.add_edge(START, "static_verification")
    workflow.add_edge("dynamic_functional", "join_verification")
    workflow.add_edge("static_verification", "join_verification")
    workflow.add_edge("join_verification", END)
    return workflow.compile()


def initial_state(
    *,
    run_id: str,
    app_id: str,
    target_url: str = "",
    application_dir: str = "",
    repair_history: dict | None = None,
    fixed_arazzo_document: dict | None = None,
    fixed_workflow_inputs: dict[str, dict[str, Any]] | None = None,
    fixed_input_values: dict[str, list[dict[str, Any]]] | None = None,
    preserved_workflow_results: list[dict] | None = None,
    priority_workflow_id: str = "",
    testing_input: dict | None = None,
    iac_expected: bool | None = None,
    deployment_package_expected: bool | None = None,
    gate_scope: list[str] | None = None,
    previous_reports: dict | None = None,
    previous_job_id: str = "",
    validation_skipped: bool = False,
) -> dict:
    """호출 인자를 빠짐없이 채운 graph 시작 상태를 만든다."""
    return {
        "run_id": run_id,
        "app_id": app_id,
        "testing_input": testing_input or {},
        "application_dir": application_dir,
        "target_url": target_url,
        "repair_history": repair_history or {},
        "fixed_arazzo_document": fixed_arazzo_document,
        "fixed_workflow_inputs": fixed_workflow_inputs or {},
        "fixed_input_values": fixed_input_values or {},
        "preserved_workflow_results": preserved_workflow_results or [],
        "priority_workflow_id": priority_workflow_id,
        "iac_expected": iac_expected,
        "deployment_package_expected": deployment_package_expected,
        "gate_scope": gate_scope,
        "previous_reports": previous_reports or {},
        "previous_job_id": previous_job_id,
        "validation_skipped": validation_skipped,
        "current_node": "",
        "errors": [],
        "static_report": None,
        "dynamic_functional_report": None,
        "iac_report": None,
    }
