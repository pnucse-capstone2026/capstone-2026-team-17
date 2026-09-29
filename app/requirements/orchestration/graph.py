"""요구사항 stage service를 배선하고 session 실행을 조율하는 canonical graph 경계다.

이 파일은 4단계 서브그래프(app/agent/subgraphs.py)를 상위 그래프로 배선·컴파일하는 단일
진입점이다. 세부 작업은 각 단계 서브그래프 안에 캡슐화돼 있다.

전체 워크플로우(상위 그래프, 노드명 = 단계별 동작):
  START → refine_requirements → analyze_cloud_inputs → structure_constraints → model_use_cases
      → write_specifications → draw_diagram → END
  (각 노드 = 컴파일된 스테이지 서브그래프. 세부 노드는 app/agent/subgraphs.py 참조)

대화형 게이트는 gated 그래프에서만 스테이지 사이에 부모-레벨 노드로 삽입한다:
  refine_requirements → analyze_cloud_inputs → structure_constraints → gate_requirements → model_use_cases
      → gate_use_cases → write_specifications → gate_specs → draw_diagram
      → gate_relationships → END
(게이트를 서브그래프 안이 아니라 부모 레벨에 두는 이유: LangGraph는 서브그래프가 interrupt로
멈추면 그 내부 누적 상태를 부모로 올리지 않으므로, 스테이지 완료 후 부모 게이트에서 멈춰야
멈춘 시점의 산출물이 응답에 실린다.)

라우팅은 전부 정적(컴파일 타임 엣지)이다. 게이트는 gate_route 마커 + add_conditional_edges로
advance(다음 스테이지)/loop(재생성 후 재질문)를 정적 선언한다. 게이트 on/off는 서로 다른 두
그래프로 컴파일한다(런타임 settings 분기 없음). build_graph(None)만 빌드 타임에 settings를 1회
읽어 토폴로지를 고른다(런타임 라우팅이 아니라 빌드 선택).
"""

from __future__ import annotations

from typing import cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Interrupt, interrupt

from app.requirements.config import settings
from app.requirements.contracts.request import (
    DeploymentPreferences,
    FeedbackEdit,
    IdentitySourceAnswer,
    ResourceAnswer,
)
from app.requirements.contracts.state import AgentState
from app.requirements.modeling.relationships import check_relationships
from app.requirements.modeling.specifications import check_specs
from app.requirements.modeling.use_cases import review_model
from app.requirements.orchestration import persistence
from app.requirements.orchestration.feedback_gates import (
    apply_feedback_upto,
    gate_relationships,
    gate_requirements,
    gate_specs,
    gate_use_cases,
    route_gate,
)
from app.requirements.orchestration.persistence import SqlCheckpointSaver
from app.requirements.orchestration.subgraphs import build_stage_subgraphs
from app.requirements.orchestration.supervisor import (
    blocking_findings,
    blocking_issues,
    repair_state,
    route_redo,
    supervise_for,
)
from app.requirements.resources.capability_contract import (
    apply_capability_answers,
    capability_resource_questions,
)
from app.requirements.runtime import telemetry

RequirementsGraph = CompiledStateGraph[AgentState, None, AgentState, AgentState]


def _handoff_gate(state: AgentState) -> dict:
    """Require feedback when completed requirements still cannot be handed off.

    This uses the ordinary feedback interrupt rather than adding a second public
    completion state.  A reply is applied through the final requirements stage,
    then the existing reports are recomputed before this gate checks again.
    """
    issues = blocking_issues(state)
    if not issues:
        return {"gate_route": "advance"}

    answer = interrupt({
        "stage": "requirements_handoff",
        "status": "need_feedback",
        "prompt": (
            "[requirements_handoff] Design handoff is blocked by unresolved "
            "requirements findings. Provide feedback to repair them."
        ),
        "summary": issues,
        "blocking_findings": blocking_findings(state),
        "requires_revision": True,
        "repair_state": repair_state(state),
        # The blocker may belong to use cases, specifications, or relationships. Do not make the
        # UI는 관계 수정 target을 직접 만들지 않는다. Workspace project tool이 owner를 검증한다.
        "edit_stage": None,
        "edit_targets": [],
        "resource_questions": capability_resource_questions(
            state.get("capability_contract") or {}
        ),
    })
    if isinstance(answer, ResourceAnswer):
        patch = apply_capability_answers(state, answer.answers)
        # An invalid or stale choice leaves the same question visible.  It must
        # never fall through to natural-language feedback classification.
        return {**patch, "gate_route": "loop"}
    empty = isinstance(answer, FeedbackEdit) and not answer.instruction.strip()
    if empty:
        # Unlike an ordinary review acknowledgement, a blank answer cannot
        # waive an unsafe downstream handoff.
        return {"gate_route": "loop"}

    if not isinstance(answer, FeedbackEdit):
        raise TypeError("Requirements handoff repair requires a validated FeedbackEdit.")
    updated = dict(state)
    apply_feedback_upto(cast(AgentState, updated), answer, up_to="diagram")
    updated.update(review_model(cast(AgentState, updated)))
    updated.update(check_specs(cast(AgentState, updated)))
    updated.update(check_relationships(cast(AgentState, updated)))
    return {**updated, "gate_route": "loop"}


def _build_plain_graph(saver: BaseCheckpointSaver[str]) -> RequirementsGraph:
    """게이트 없는 파이프라인 + **되돌아가기**.

    START → refine_requirements → analyze_cloud_inputs → structure_constraints
          → model_use_cases → supervise_model
          → write_specifications → supervise_specs → draw_diagram → supervise_diagram → END

    각 `supervise_*`는 남은 결함을 보고 **그 결함을 낸 단계의 그룹으로 되돌린다**
    (`agent/supervisor.py`). 되돌아가기가 없던 동안 두 가지가 막혀 있었다:
    `review_model`이 찾은 결함은 아무도 고치지 못했고(원인이 `identify_actors`에 있다),
    `repair_stopped="no_improvement"`는 "위에 원인이 있다"는 신호인데 올려보낼 통로가 없었다.

    사이클을 **그래프 엣지로** 만드는 이유: 노드 안에서 상위 단계를 직접 부르면 그림
    (`docs/graph/*.png`)이 실제 흐름을 말하지 않게 된다. 되돌아가기는 이 파이프라인의
    구조이므로 구조로 드러나야 한다.

    같은 finding/input에서 이미 쓴 strategy는 supervisor가 다시 고르지 않는다. 따라서
    숫자 상한 없이 진전하는 동안 계속하고, 전략이 소진되면 handoff blocker로 드러난다.
    """
    subs = build_stage_subgraphs()
    builder = StateGraph(AgentState)
    builder.add_node("refine_requirements", subs["refine_requirements"])
    builder.add_node("analyze_cloud_inputs", subs["analyze_cloud_inputs"])
    builder.add_node("structure_constraints", subs["structure_constraints"])
    builder.add_node("model_use_cases", subs["model_use_cases"])
    builder.add_node("write_specifications", subs["write_specifications"])
    builder.add_node("draw_diagram", subs["draw_diagram"])
    # 자리마다 되돌릴 수 있는 그룹이 다르다 — 아직 돌지 않은 그룹은 대상이 아니다.
    builder.add_node(  # type: ignore[call-overload]
        "supervise_model", supervise_for("model_use_cases")
    )
    builder.add_node(  # type: ignore[call-overload]
        "supervise_specs", supervise_for("model_use_cases", "write_specifications")
    )
    builder.add_node(  # type: ignore[call-overload]
        "supervise_diagram",
        supervise_for("model_use_cases", "write_specifications", "draw_diagram"),
    )
    builder.add_node("gate_handoff", _handoff_gate)

    builder.add_edge(START, "refine_requirements")
    # 배포 필요사항은 `classified`에서 파생되는 인계 산출물이라 되돌아가기 대상이 아니다.
    builder.add_edge("refine_requirements", "analyze_cloud_inputs")
    # 제약 구조화도 같은 성격이다 — 결함을 내지 않고 `classified`·제약 원문에서만 파생된다.
    builder.add_edge("analyze_cloud_inputs", "structure_constraints")
    builder.add_edge("structure_constraints", "model_use_cases")
    builder.add_edge("model_use_cases", "supervise_model")
    builder.add_edge("write_specifications", "supervise_specs")
    builder.add_edge("draw_diagram", "supervise_diagram")

    # 되돌릴 수 있는 대상은 **이미 지나온 그룹**뿐이다. 아직 돌지 않은 그룹으로 "되돌리는"
    # 것은 되돌리기가 아니라 건너뛰기이고, 그러면 산출물이 없는 채로 아래가 돈다.
    builder.add_conditional_edges(
        "supervise_model",
        route_redo,
        {"advance": "write_specifications", "model_use_cases": "model_use_cases"},
    )
    builder.add_conditional_edges(
        "supervise_specs",
        route_redo,
        {
            "advance": "draw_diagram",
            "model_use_cases": "model_use_cases",
            "write_specifications": "write_specifications",
        },
    )
    builder.add_conditional_edges(
        "supervise_diagram",
        route_redo,
        {
            "advance": "gate_handoff",
            "model_use_cases": "model_use_cases",
            "write_specifications": "write_specifications",
            "draw_diagram": "draw_diagram",
        },
    )
    builder.add_conditional_edges(
        "gate_handoff", route_gate, {"advance": END, "loop": "gate_handoff"}
    )

    # 체크포인터는 상위 그래프에만 둔다 — 서브그래프의 interrupt도 상위로 전파돼 상위
    # invoke(Command(resume=...))로 재개된다(서브그래프는 무-체크포인터로 컴파일됨).
    return builder.compile(checkpointer=saver)


def _build_gated_graph(saver: BaseCheckpointSaver[str]) -> RequirementsGraph:
    """대화형 피드백 게이트가 낀 파이프라인: 각 스테이지 뒤에 부모-레벨 게이트를 둔다.

    각 게이트는 advance→다음 스테이지(마지막은 END), loop→게이트 자신(재생성 후 재질문).
    게이트를 서브그래프 안이 아니라 부모 레벨에 두는 이유는 모듈 상단 docstring 참조.
    """
    subs = build_stage_subgraphs()
    builder = StateGraph(AgentState)
    builder.add_node("refine_requirements", subs["refine_requirements"])
    builder.add_node("analyze_cloud_inputs", subs["analyze_cloud_inputs"])
    builder.add_node("structure_constraints", subs["structure_constraints"])
    builder.add_node("model_use_cases", subs["model_use_cases"])
    builder.add_node("write_specifications", subs["write_specifications"])
    builder.add_node("draw_diagram", subs["draw_diagram"])
    builder.add_node("gate_requirements", gate_requirements)
    builder.add_node("gate_use_cases", gate_use_cases)
    builder.add_node("gate_specs", gate_specs)
    builder.add_node("gate_relationships", gate_relationships)
    builder.add_node("gate_handoff", _handoff_gate)

    builder.add_edge(START, "refine_requirements")
    # 게이트 앞에 둬 사용자가 요구사항과 배포 필요사항을 함께 확인하게 한다.
    builder.add_edge("refine_requirements", "analyze_cloud_inputs")
    # 제약 구조화도 게이트 **앞**이다. 못 채운 필수 칸의 되묻기가 사용자가 요구사항을
    # 확인하는 바로 그 자리에서 함께 보여야 한다 — 뒤에 두면 이미 넘어간 뒤에 나온다.
    builder.add_edge("analyze_cloud_inputs", "structure_constraints")
    builder.add_edge("structure_constraints", "gate_requirements")
    # loop가 게이트 자신이 아니라 `analyze_cloud_inputs`로 돌아간다. 이 게이트는 루프에서
    # classify 결과가 바뀌었으므로 배포 capability와 자유문장 제약을 새 근거로 다시 읽는다.
    builder.add_conditional_edges(
        "gate_requirements",
        route_gate,
        {
            "advance": "model_use_cases",
            "loop": "analyze_cloud_inputs",
            # 되묻기의 답만 온 경우. 이 분기는 `classify`를 안 돌려 `classified`가 그대로이고,
            # 제약 답변만 바뀌면 classified는 그대로이므로 배포 필요사항을 다시 부르지 않는다.
            "answers": "structure_constraints",
        },
    )
    builder.add_edge("model_use_cases", "gate_use_cases")
    builder.add_conditional_edges(
        "gate_use_cases",
        route_gate,
        {"advance": "write_specifications", "loop": "gate_use_cases"},
    )
    builder.add_edge("write_specifications", "gate_specs")
    builder.add_conditional_edges(
        "gate_specs",
        route_gate,
        {"advance": "draw_diagram", "loop": "gate_specs"},
    )
    builder.add_edge("draw_diagram", "gate_relationships")
    builder.add_conditional_edges(
        "gate_relationships",
        route_gate,
        {"advance": "gate_handoff", "loop": "gate_relationships"},
    )
    builder.add_conditional_edges(
        "gate_handoff", route_gate, {"advance": END, "loop": "gate_handoff"}
    )

    return builder.compile(checkpointer=saver)


def build_graph(
    feedback_gates: bool | None = None,
    *,
    persistent: bool = False,
    saver: BaseCheckpointSaver[str] | None = None,
) -> RequirementsGraph:
    """settings(또는 인자)에 따라 두 플랫 빌더 중 하나를 골라 컴파일한 그래프를 반환한다.

    feedback_gates=None이면 빌드 타임에 settings.enable_feedback_gates 를 1회 읽는다(기존
    호출부/테스트 호환). 게이트 on/off는 런타임 분기가 아니라 서로 다른 정적 그래프다.

    persistent=True면 체크포인트를 MySQL에 쓴다(서버 재시작을 넘어 세션이 살아남는다).
    False면 프로세스 메모리 — CLI·배치처럼 프로세스가 끝나면 세션도 의미가 없는 경로용이고,
    DB 없이 돌 수 있어야 하는 경로이기도 하다.
    """
    gated = settings.enable_feedback_gates if feedback_gates is None else feedback_gates
    checkpoint_saver = cast(
        BaseCheckpointSaver[str],
        saver or (SqlCheckpointSaver() if persistent else MemorySaver()),
    )
    return _build_gated_graph(checkpoint_saver) if gated else _build_plain_graph(checkpoint_saver)


# 앱 전역에서 재사용할 컴파일된 그래프 (모듈 로드 시 1회 생성)
graph = build_graph()

# (게이트 on/off) × (영속 여부) 네 토폴로지를 미리 컴파일해 두고 세션에 맞춰 고른다.
# 게이트 on/off는 서로 다른 그래프라 **세션을 시작한 쪽으로만 재개해야** 체크포인트가 맞는다.
# 그래서 thread_id → 모드를 기억한다. 영속 세션은 그 기억도 DB에 둔다(재시작을 넘겨야 하므로).
_GRAPHS: dict[tuple[bool, bool], RequirementsGraph] = {}


def _compiled(gated: bool, persistent: bool) -> RequirementsGraph:
    key = (gated, persistent)
    if key not in _GRAPHS:
        _GRAPHS[key] = build_graph(feedback_gates=gated, persistent=persistent)
    return _GRAPHS[key]


# 비영속 세션의 모드 기억. 영속 세션은 session_store가 대신한다.
_thread_gates: dict[str, bool] = {}


def rebuild_graph() -> RequirementsGraph:
    """settings 변경(예: enable_feedback_gates) 후 컴파일된 그래프들을 버린다."""
    global graph
    graph = build_graph()
    _GRAPHS.clear()
    return graph


def _remember_mode(thread_id: str, gated: bool, persistent: bool) -> None:
    if persistent:
        persistence.remember_session_mode(thread_id, gated)
    else:
        _thread_gates[thread_id] = gated


def _recall_mode(thread_id: str, persistent: bool) -> bool:
    """이 세션이 시작된 토폴로지. 모르면 서버 기본값으로 떨어진다."""
    remembered = (
        persistence.session_mode(thread_id) if persistent else _thread_gates.get(thread_id)
    )
    return settings.enable_feedback_gates if remembered is None else remembered


def _invoke(gates: bool, thread_id: str, graph_input, persistent: bool):
    """모드에 맞는 정적 그래프를 골라 실행한다(런타임 라우팅 없음 → 직렬화 불필요)."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    return _compiled(gates, persistent).invoke(graph_input, config)  # type: ignore[attr-defined]


def _has_checkpoint(gates: bool, thread_id: str, persistent: bool) -> bool:
    """이 실행 ID로 다시 시작할 수 있는 요구사항 checkpoint가 있는지 확인한다.

    LangGraph는 저장된 상태가 없는 상태에서 ``invoke(None, ...)``을 호출해도 곧바로
    오류를 내지 않을 수 있다. 그러면 재시도 요청이 새 빈 실행처럼 처리되어 원래
    요구사항과 관계없는 결과를 만들 수 있다. 따라서 재시도 전에 저장된 값이 실제로
    있는지 먼저 확인한다.
    """
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    snapshot = _compiled(gates, persistent).get_state(config)  # type: ignore[attr-defined]
    return bool(snapshot.values)


def capture_analysis_checkpoint(
    thread_id: str,
    *,
    persist: bool = False,
) -> dict[str, object]:
    """Capture the current graph position for compensating a failed revision."""

    gates = _recall_mode(thread_id, persist)
    compiled = _compiled(gates, persist)
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    snapshot = compiled.get_state(config)  # type: ignore[attr-defined]
    if not snapshot.values or not snapshot.config:
        raise ValueError(
            f"No saved checkpoint was found for requirements run {thread_id!r}."
        )
    next_nodes = [str(node) for node in snapshot.next]
    if next_nodes:
        tasks = list(snapshot.tasks)
        safe_interrupt = (
            len(next_nodes) == 1
            and next_nodes[0].startswith("gate_")
            and len(tasks) == 1
            and str(tasks[0].name) == next_nodes[0]
            and tasks[0].error is None
            and len(tasks[0].interrupts) == 1
        )
        if not safe_interrupt:
            raise ValueError(
                "The requirements run is not at a safe feedback checkpoint; retry it first."
            )
    return {
        "config": dict(snapshot.config),
        "next": next_nodes,
    }


def restore_analysis_checkpoint(
    thread_id: str,
    checkpoint: dict[str, object],
    *,
    persist: bool = False,
) -> None:
    """Restore a completed or feedback-gated requirements checkpoint."""

    base_config = checkpoint.get("config")
    if not isinstance(base_config, dict) or not base_config:
        raise ValueError("A requirements checkpoint snapshot is required for restoration.")
    next_nodes = [str(node) for node in checkpoint.get("next") or []]
    if len(next_nodes) > 1:
        raise ValueError("A requirements checkpoint with parallel next nodes cannot be restored.")

    gates = _recall_mode(thread_id, persist)
    compiled = _compiled(gates, persist)
    configurable = base_config.get("configurable")
    if not isinstance(configurable, dict) or configurable.get("thread_id") != thread_id:
        raise ValueError("The requirements checkpoint belongs to a different run.")
    config = cast(RunnableConfig, base_config)

    if not next_nodes:
        compiled.update_state(  # type: ignore[attr-defined]
            config,
            {},
            as_node="gate_handoff",
        )
        return

    node = next_nodes[0]
    predecessor = {
        "gate_requirements": "structure_constraints",
        "gate_use_cases": "model_use_cases",
        "gate_specs": "write_specifications",
        "gate_relationships": "draw_diagram",
        "gate_handoff": "gate_relationships",
    }.get(node)
    if predecessor is None:
        raise ValueError(
            f"Cannot restore a requirements revision from non-gate node {node!r}."
        )
    restored_config = compiled.update_state(  # type: ignore[attr-defined]
        config,
        {},
        as_node=predecessor,
    )
    waiting = cast(  # type: ignore[attr-defined]
        dict[str, object], compiled.invoke(None, restored_config)
    )
    interrupts = cast(list[Interrupt], waiting.get("__interrupt__") or [])
    value = interrupts[0].value if interrupts else {}
    expected_stage = {
        "gate_requirements": "requirements",
        "gate_use_cases": "use_cases",
        "gate_specs": "specs",
        "gate_relationships": "relationships",
        "gate_handoff": "requirements_handoff",
    }[node]
    if not isinstance(value, dict) or value.get("stage") != expected_stage:
        raise RuntimeError(f"Requirements checkpoint restoration did not re-enter {node}.")


# ----------------------------------------------------------------------------
# 서빙 헬퍼 (main.py에서 사용)
# ----------------------------------------------------------------------------
#: 응답에 실을 step2~4 산출물 키. 있을 때만 싣는다(파이프라인이 어디까지 갔는지에 따라 다르다).
#: **한 곳에만 적는다** — 예전에는 게이트 응답과 완료 응답이 같은 목록을 따로 들고 있어서,
#: 새 산출물을 추가하면 한쪽에만 들어가 화면에서 조용히 사라질 수 있었다.
ARTIFACT_KEYS = (
    "deployment_needs",
    "capability_contract",
    "resource_spec",
    "resource_intake",
    "actors",
    "use_cases",
    "model_review",
    "coverage",
    "traceability",
    "use_case_specs",
    "spec_report",
    "semantic_ambiguity_question",
    "identity_source_question",
    "relationships",
    "relationship_report",
    "diagram",
)


def result_payload(
    result: dict[str, object],
    thread_id: str,
    stats: telemetry.RunStats | None = None,
) -> dict[str, object]:
    """그래프 실행 결과를 API 응답 형태(dict)로 변환한다.

    stats를 주면 이번 호출의 계측(호출 수·토큰·저하 목록)을 함께 싣는다. 저하가 있으면
    산출물 일부가 검증을 못 거친 것이므로, 화면이 그 사실을 알 수 있어야 한다.
    """

    def _finish(payload: dict[str, object]) -> dict[str, object]:
        if stats is not None:
            payload["telemetry"] = stats.as_dict()
        return payload

    interrupts = cast(list[Interrupt], result.get("__interrupt__") or [])
    if interrupts:
        value = interrupts[0].value
        # 대화형 피드백 게이트(step1~4 말미)
        if isinstance(value, dict) and value.get("status") == "need_feedback":
            payload = {
                "thread_id": thread_id,
                "phase": value.get("stage", "feedback"),
                "status": "need_feedback",
                "feedback_prompt": value.get("prompt"),
                "feedback_summary": value.get("summary"),
                # 화면이 구조화 편집을 만들 재료. 없으면(step1) 자연어만 받는다.
                "edit_stage": value.get("edit_stage"),
                "edit_targets": value.get("edit_targets"),
                # 되묻기. 화면이 이걸 받아야 `resource_answers`를 만들 수 있다 —
                # 안 실으면 질문이 상태에만 있고 사용자에게는 영영 안 보인다.
                "resource_questions": value.get("resource_questions"),
                "blocking_findings": value.get("blocking_findings"),
                "requires_revision": value.get("requires_revision"),
                "repair_state": value.get("repair_state"),
                "requirements": result.get("classified", []),
            }
            # 게이트에서 멈춘 시점까지 누적된 step2~4 산출물도 함께 실어 UI가 진행 상황을 보여준다.
            for key in ARTIFACT_KEYS:
                val = result.get(key)
                if val:
                    payload[key] = val
            return _finish(payload)
        # 요구사항 구체화(clarify)
        questions = value.get("questions", []) if isinstance(value, dict) else value
        return _finish(
            {
                "thread_id": thread_id,
                "phase": "clarify",
                "status": "need_clarification",
                "questions": questions,
            }
        )
    payload = {
        "thread_id": thread_id,
        "phase": result.get("phase", "completed"),
        "status": "completed",
        "requirements": result.get("classified", []),
    }
    # step2~4 산출물은 파이프라인이 돌았을 때만 존재하므로, 있을 때만 응답에 싣는다.
    for key in ARTIFACT_KEYS:
        value = result.get(key)
        if value:
            payload[key] = value
    return _finish(payload)


def start_analysis(
    requirements: list[str],
    thread_id: str,
    feedback_gates: bool | None = None,
    *,
    persist: bool = False,
    constraints_text: str = "",
    cloud_constraints: dict[str, object] | None = None,
) -> dict[str, object]:
    """새 요구사항 분석 세션을 시작한다.

    feedback_gates=None이면 서버 기본값(settings)을 따른다. 세션 모드는 thread_id에 기록해
    이후 resume_analysis가 같은 토폴로지의 그래프로 재개하도록 한다.

    persist=True면 체크포인트와 세션 모드를 MySQL에 남긴다 — 서버가 재시작해도 이어진다.
    서빙 경로(api.py)가 켜고, CLI·배치는 끈 채로 둔다(프로세스와 수명이 같고 DB 없이도 돌아야 한다).

    `constraints_text`는 사용자가 **요구사항과 따로** 쓴 클라우드 제약 원문이다
    (`apps.resource_constraints_text`). 요구사항 문장에 섞어 넣지 않는 이유는
    `resources/service.py`에 있다 — 실측상 provider·region·예산은 요구사항 산문에
    0건이고, 섞으면 분류기가 그것들을 FR/NFR로 판정해야 한다.
    """
    gates = settings.enable_feedback_gates if feedback_gates is None else feedback_gates
    _remember_mode(thread_id, gates, persist)
    # 초기 입력은 부분 상태(나머지 키는 노드가 채움)라 AgentState로 캐스팅.
    initial: dict[str, object] = {"raw_requirements": requirements}
    if constraints_text.strip():
        initial["resource_constraints_text"] = constraints_text
    if cloud_constraints:
        initial["initial_cloud_constraints"] = dict(cloud_constraints)
    with telemetry.run_scope(f"analyze:{thread_id}") as stats:
        result = _invoke(gates, thread_id, cast(AgentState, initial), persist)
        return result_payload(cast(dict[str, object], result), thread_id, stats)


def resume_analysis(
    answer: str | FeedbackEdit | ResourceAnswer | IdentitySourceAnswer | DeploymentPreferences,
    thread_id: str,
    *,
    persist: bool = False,
) -> dict[str, object]:
    """clarifying question 또는 피드백 게이트에 대한 사용자 입력으로 세션을 재개한다.

    answer는 셋 중 하나다:
      - 자연어 문자열 — 의도 분류가 어느 단계를 고칠지 정한다.
      - `FeedbackEdit` — 화면이 대상을 이미 알 때. 의도 분류 LLM 호출을 건너뛴다
        (app/requirements/feedback.py: resolve_intent).
      - `ResourceAnswer` — **되묻기의 답**. 요구사항 피드백이 아니므로 재분류를 돌리지
        않고 `resource_answers`에 쌓인 뒤 제약 구조화만 다시 돈다.
      - `DeploymentPreferences` — 실행 중 별도 카드에서 받은 CSP 대안을 그대로 합친다.

    persist는 start_analysis 때와 같아야 한다 — 체크포인트가 있는 곳에서 찾아야 한다.
    """
    gates = _recall_mode(thread_id, persist)
    with telemetry.run_scope(f"resume:{thread_id}") as stats:
        result = _invoke(gates, thread_id, Command(resume=answer), persist)
        return result_payload(cast(dict[str, object], result), thread_id, stats)


def revise_analysis(
    edit: FeedbackEdit,
    thread_id: str,
    *,
    persist: bool = False,
) -> dict[str, object]:
    """Re-enter the exact requirements feedback gate for a completed run."""

    gates = _recall_mode(thread_id, persist)
    if not gates:
        raise ValueError(
            "A completed requirements revision requires a feedback-gated checkpoint."
        )
    compiled = _compiled(gates, persist)
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    checkpoint = capture_analysis_checkpoint(thread_id, persist=persist)
    predecessor = {
        "actors": "model_use_cases",
        "use_cases": "model_use_cases",
        "specs": "write_specifications",
        "relationships": "draw_diagram",
    }[edit.stage]
    with telemetry.run_scope(f"revise:{thread_id}") as stats:
        try:
            compiled.update_state(config, {}, as_node=predecessor)  # type: ignore[attr-defined]
            waiting = cast(dict[str, object], compiled.invoke(None, config))  # type: ignore[attr-defined]
            if not waiting.get("__interrupt__"):
                raise RuntimeError(
                    f"Requirements revision could not enter the {edit.stage} feedback gate."
                )
            result = compiled.invoke(Command(resume=edit), config)  # type: ignore[attr-defined]
            return result_payload(cast(dict[str, object], result), thread_id, stats)
        except Exception:
            restore_analysis_checkpoint(thread_id, checkpoint, persist=persist)
            raise


def retry_analysis(thread_id: str, *, persist: bool = False) -> dict[str, object]:
    """실패한 요구사항 node를 같은 MySQL checkpoint에서 다시 실행한다.

    새 요구사항을 넣지 않고 ``None``으로 graph를 호출하면 마지막으로 성공한 node 다음부터
    재개한다. 따라서 이미 완료해 저장한 단계와 LLM 호출을 처음부터 반복하지 않는다. 공개
    Workspace는 사용자가 명시적으로 ``retry_requirements``를 선택했을 때만 이 함수를 쓴다.
    """
    gates = _recall_mode(thread_id, persist)
    if not _has_checkpoint(gates, thread_id, persist):
        raise ValueError(
            f"No saved checkpoint was found for requirements run {thread_id!r}."
        )
    with telemetry.run_scope(f"retry:{thread_id}") as stats:
        result = _invoke(gates, thread_id, None, persist)
        return result_payload(cast(dict[str, object], result), thread_id, stats)
