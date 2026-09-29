"""배포 capability와 클라우드 제약을 제한적으로 병렬 분석한다.

두 작업은 분류가 끝난 동일 요구사항을 읽지만 서로의 산출물을 만들지는 않는다.
따라서 LLM 호출 구간만 최대 두 스레드로 겹치고, 결과는 고정된 순서로 병합한다.
사용자 질문과 RESOURCE_SPEC 확정은 뒤의 ``build_resource_spec``에서 수행한다.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from app.requirements.common.state_contract import contract
from app.requirements.contracts.state import AgentState
from app.requirements.resources.capability_extraction import derive_deployment_needs
from app.requirements.resources.service import extract_resource_constraints
from app.requirements.runtime import telemetry

CloudInputPatch = dict[str, object]
CloudInputCall = Callable[[AgentState], CloudInputPatch]


def _observed_branch(
    name: str, fn: CloudInputCall, state: AgentState
) -> CloudInputPatch:
    started = time.perf_counter()
    telemetry.emit_progress("analysisStepStarted", step=name)
    try:
        result = fn(state)
    except BaseException as error:
        telemetry.emit_progress(
            "analysisStepFinished",
            step=name,
            status="failed",
            errorType=type(error).__name__,
            elapsedSeconds=round(time.perf_counter() - started, 6),
        )
        raise
    telemetry.emit_progress(
        "analysisStepFinished",
        step=name,
        status="completed",
        elapsedSeconds=round(time.perf_counter() - started, 6),
    )
    return result


@contract(
    "analyze_cloud_inputs",
    requires=("classified",),
    produces=(
        "deployment_needs",
        "capability_contract",
        "resource_constraint_extraction",
    ),
)
def analyze_cloud_inputs(
    state: AgentState,
    *,
    deployment_call: CloudInputCall | None = None,
    constraint_call: CloudInputCall | None = None,
) -> CloudInputPatch:
    """독립적인 capability와 resource proposal 분석을 병렬 실행한다.

    Args:
        state: 두 분석이 함께 읽는 분류 완료 단계 상태다.
        deployment_call: capability 분석을 대체하는 공개 주입 함수다.
        constraint_call: 자유문장 resource proposal 분석을 대체하는 공개 주입 함수다.

    Returns:
        두 branch 결과를 기존 순서로 병합한 graph-state patch다.

    Notes:
        최대 worker 수 2, ContextVar 전파, progress event 이름과 결과 병합 순서를
        유지한다. 두 branch는 서로의 결과를 읽지 않는다.
    """
    jobs = (
        ("derive_deployment_needs", deployment_call or derive_deployment_needs),
        (
            "extract_resource_constraints",
            constraint_call or extract_resource_constraints,
        ),
    )
    results: dict[str, CloudInputPatch] = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="requirements-cloud") as pool:
        futures = {
            name: pool.submit(
                telemetry.bind_context(_observed_branch), name, fn, state
            )
            for name, fn in jobs
        }
        for name, _fn in jobs:
            results[name] = futures[name].result()

    merged: CloudInputPatch = {}
    for name, _fn in jobs:
        merged.update(results[name])
    merged["phase"] = "cloud_inputs"
    return merged


analyze_cloud_inputs._easydep_emits_progress = True  # type: ignore[attr-defined]
