"""PIPELINE registry에서 요구사항 stage subgraph를 컴파일하는 canonical 조립 경계다.

각 단계를 독립 서브그래프로 컴파일한다. 상위 그래프의 노드명은 단계별 '동작'을 나타낸다:
  refine_requirements  — intake → clarify → classify        (구체화 + FR/NFR 분류)
  analyze_cloud_inputs — capability·클라우드 제약 추출 병렬 실행
  structure_constraints — build_resource_spec                (제약 → RESOURCE_SPEC 계약)
  model_use_cases      — identify_actors → identify_use_cases → review_model → check_coverage
  write_specifications — generate_specs → check_specs
  draw_diagram         — identify_relationships → check_relationships → render_diagram

단계별 세부 작업은 서브그래프 내부 노드로 캡슐화된다. 대화형 피드백 게이트는 상위 그래프의
스테이지 사이에 배치한다(서브그래프 안이 아님) — LangGraph는 서브그래프가 interrupt로 멈추면
그 내부 누적 상태를 부모로 올리지 않으므로, 게이트를 부모 레벨에 두어야 멈춘 시점의 산출물이
응답에 실린다.

라우팅은 전부 정적(컴파일 타임 엣지)이다. 서브그래프는 체크포인터 없이 컴파일한다 — 상위
그래프의 MemorySaver가 트리 전체를 관장한다.
"""
from __future__ import annotations

import time
from functools import wraps
from itertools import pairwise

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.requirements import stage_registry as stages

# ⚠ 아래 단계 함수들은 **이 모듈의 이름으로 존재해야 한다.** `build_stage`가
# `globals()[노드이름]`으로 찾기 때문이다(이유는 그 함수 docstring). 린터에게는 안 쓰는
# import로 보이지만 지우면 그래프가 만들어지지 않고, 테스트의 monkeypatch도 여기에 건다.
# 자동 수정(`ruff --fix`)이 지우지 않도록 명시적으로 막는다.
from app.requirements.contracts.state import AgentState
from app.requirements.modeling.diagram import render_diagram  # noqa: F401
from app.requirements.modeling.refinement import (  # noqa: F401
    clarify,
    classify,
    expand_requirements,
    intake,
)
from app.requirements.modeling.relationships import (  # noqa: F401
    check_relationships,
    identify_relationships,
)
from app.requirements.modeling.specifications import (  # noqa: F401
    check_specs,
    generate_specs,
)
from app.requirements.modeling.use_cases import (  # noqa: F401
    check_coverage,
    identify_actors,
    identify_use_cases,
    review_model,
)
from app.requirements.resources.cloud_inputs import analyze_cloud_inputs  # noqa: F401
from app.requirements.resources.service import build_resource_spec  # noqa: F401
from app.requirements.runtime import telemetry

StageGraph = CompiledStateGraph[AgentState, None, AgentState, AgentState]


def _observed_node(name: str, fn):
    """Keep the graph unchanged while exposing real node boundaries to observers."""

    if getattr(fn, "_easydep_emits_progress", False):
        return fn

    @wraps(fn)
    def run(state):
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

    return run


def build_stage(group: str) -> StageGraph:
    """한 스테이지의 서브그래프를 **단계 목록에서** 컴파일한다(노드·엣지 모두 §14).

    함수는 `globals()`로 찾는다 — `stages.Stage.fn`을 쓰면 한 단계를 가리키는 이름이 둘이
    되어 monkeypatch가 한쪽에만 걸린다(`runner._run_stages`도 같은 이유).
    """
    nodes = stages.nodes_in(group)
    if not nodes:  # pragma: no cover - 배선 오류
        raise KeyError(f"{group}: 단계 목록에 이 그룹의 노드가 없다")

    b = StateGraph(AgentState)
    for name in nodes:
        b.add_node(name, _observed_node(name, globals()[name]))
    b.add_edge(START, nodes[0])
    for src, dst in pairwise(nodes):
        b.add_edge(src, dst)
    b.add_edge(nodes[-1], END)
    return b.compile()


def build_stage_subgraphs() -> dict[str, StageGraph]:
    """4단계 순수 스테이지 서브그래프를 컴파일해 {동작이름: 컴파일된 서브그래프}로 반환한다.

    키(=상위 그래프 노드명)도 순서도 `stages.GROUPS`에서 파생한다.
    """
    return {group: build_stage(group) for group in stages.GROUPS}
