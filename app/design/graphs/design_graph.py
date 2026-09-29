"""설계 파이프라인 조립기 + 서빙 헬퍼.

이 파일은 5개 스테이지 서브그래프(`subgraphs.py`)를 하나의 상위 그래프로 배선·컴파일하는
단일 진입점이다. 세부 작업은 각 스테이지 서브그래프 안에 캡슐화돼 있다.

전체 워크플로우(상위 그래프):

    START → gen_class_diagram → persist_class_diagram → gate_class_diagram ─advance─┐
                    ↑                      │                                        │
                    └── fb_class_diagram ←─┘ loop                                   │
                                                                                    ↓
            gen_sequence_diagram → persist_sequence_diagram → gate_sequence_diagram ─advance─┐
            ... api_spec ... erd ... deployment_diagram → gate_deployment_diagram ─advance─→ END

각 스테이지는 세 노드로 이루어진다:
  gen_{stage}     — 생성 서브그래프 (예: class는 extract → convert → validate)
  fb_{stage}      — 피드백 서브그래프 (게이트가 loop을 낼 때만 돈다)
  persist_{stage} — 저장소에 새 버전으로 남긴다 (`nodes/persist.py` 참조)
  gate_{stage}    — 산출물을 보여주고 interrupt로 멈춘다 (`nodes/gates.py` 참조)

`persist`가 생성 쪽과 피드백 쪽 양쪽에서 들어오므로, 저장소는 항상 사용자가 게이트에서
보고 있는 것과 일치한다.

라우팅은 전부 정적(컴파일 타임 엣지)이다. 게이트는 gate_route 마커 +
add_conditional_edges로 advance(다음 스테이지)/loop(재생성 후 재질문)를 선언한다.
스테이지 순서는 `DESIGN_STAGES` 한 곳에서만 나온다 — 배선도 그림도 그것을 따른다.
"""
from __future__ import annotations

from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.artifacts_api import to_web_response
from app.design.graphs.subgraphs import DESIGN_STAGES, DESIGN_SUBGRAPHS
from app.design.nodes.gates import make_gate, route_gate
from app.design.nodes.persist import ORIGIN_KEY, make_persist
from app.design.schemas.architecture_state import ArchitectureState
from app.design.session_store import SqlCheckpointSaver
from app.metrics import langsmith as langsmith_metrics


class StageNotReached(Exception):
    """아직 만들지 않은 스테이지로 되감으려 했다 — 그건 되감기가 아니라 전진이다."""


def _stage_runner(subgraph, origin: str):
    """서브그래프를 돌리고 "무엇이 이 상태를 만들었는지"를 한 줄 남긴다.

    persist 노드가 origin(생성이냐 피드백 반영이냐)을 알아야 버전 이력이 의미를 갖는다.
    서브그래프 자체는 서빙·저장을 모르는 순수 스테이지로 두고 싶으므로, 마커는 여기
    바깥에서 붙인다.
    """

    def run(state: ArchitectureState) -> dict:
        return {**dict(subgraph.invoke(state)), ORIGIN_KEY: origin}

    return run


def build_design_graph(saver=None):
    """5개 스테이지를 게이트로 이어 붙인 상위 그래프를 컴파일해 돌려준다.

    saver를 주지 않으면 MySQL 체크포인터를 쓴다. 게이트가 있는 그래프는 체크포인터가
    없으면 재개할 수 없다 — 멈춘 지점이 어디에도 남지 않기 때문이다.
    """
    subs = DESIGN_SUBGRAPHS
    builder = StateGraph(ArchitectureState)

    for stage in DESIGN_STAGES:
        builder.add_node(f"gen_{stage}", _stage_runner(subs[stage]["generate"], "generated"))
        builder.add_node(f"fb_{stage}", _stage_runner(subs[stage]["feedback"], "feedback"))
        builder.add_node(f"persist_{stage}", make_persist(stage))
        builder.add_node(f"gate_{stage}", make_gate(stage))

    builder.add_edge(START, f"gen_{DESIGN_STAGES[0]}")

    for index, stage in enumerate(DESIGN_STAGES):
        # 생성도 피드백도 같은 persist로 모인다 — 저장 자리가 하나뿐이어야 "저장된 것"과
        # "게이트에서 보여준 것"이 갈라지지 않는다.
        builder.add_edge(f"gen_{stage}", f"persist_{stage}")
        builder.add_edge(f"fb_{stage}", f"persist_{stage}")
        builder.add_edge(f"persist_{stage}", f"gate_{stage}")

        is_last = index == len(DESIGN_STAGES) - 1
        builder.add_conditional_edges(
            f"gate_{stage}",
            route_gate,
            {
                "advance": END if is_last else f"gen_{DESIGN_STAGES[index + 1]}",
                "loop": f"fb_{stage}",
            },
        )

    return builder.compile(checkpointer=saver if saver is not None else SqlCheckpointSaver())


# 앱 전역에서 재사용할 컴파일된 그래프 (모듈 로드 시 1회 생성).
graph = build_design_graph()


# ----------------------------------------------------------------------------
# Workspace application service에서 사용하는 실행 헬퍼
# ----------------------------------------------------------------------------
def _result_payload(result: dict, app_id: str) -> dict[str, Any]:
    """그래프 실행 결과를 API 응답 형태(dict)로 변환한다.

    산출물 직렬화는 공용 저장소 레이어의 to_web_response가 맡는다 — 설계뿐 아니라
    요구사항 산출물까지 같은 모양으로 싣는 일이라 거기 있다. 여기서는 흐름 상태
    (어느 게이트에서 멈췄나)만 얹는다.
    """
    payload: dict[str, Any] = {"app_id": app_id, **to_web_response(result)}

    interrupts = result.get("__interrupt__")
    if interrupts:
        value = interrupts[0].value
        payload.update(
            {
                "status": "need_feedback",
                "stage": value.get("stage"),
                "feedback_prompt": value.get("prompt"),
            }
        )
        return payload

    payload["status"] = "completed"
    payload["stage"] = None
    return payload


def _invoke_traced_design_graph(
    operation: str,
    app_id: str,
    invocation,
) -> dict[str, Any]:
    """Run a web-design graph operation as a privacy-safe root trace."""

    with langsmith_metrics.trace_scope(
        f"easydep.design.{operation}",
        metadata={"agent": "design", "operation": operation, "app_id": app_id},
    ):
        return _result_payload(dict(invocation()), app_id)


def start_design(app_id: str, state: ArchitectureState) -> dict[str, Any]:
    """설계 세션을 시작한다. 첫 스테이지(클래스 다이어그램)를 만들고 게이트에서 멈춘다.

    thread_id는 app_id다 — 앱 하나당 설계 세션 하나. 같은 app_id로 다시 start하면
    LangGraph가 같은 스레드에 이어 쓰므로, 처음부터 다시 돌리려면 먼저 스레드를 비워야 한다.
    """
    graph_input: ArchitectureState = {**state, "app_id": app_id}
    config: RunnableConfig = {"configurable": {"thread_id": app_id}}
    return _invoke_traced_design_graph(
        "start", app_id, lambda: graph.invoke(graph_input, config)
    )


def resume_design(app_id: str, feedback: str) -> dict[str, Any]:
    """멈춰 있는 게이트에 답해 세션을 재개한다.

    feedback이 비어 있으면 다음 스테이지로 진행하고, 있으면 현재 스테이지를 그 피드백으로
    재생성한 뒤 같은 게이트에서 다시 묻는다.
    """
    config: RunnableConfig = {"configurable": {"thread_id": app_id}}
    return _invoke_traced_design_graph(
        "resume", app_id, lambda: graph.invoke(Command(resume=feedback), config)
    )


def retry_design(
    app_id: str, *, repair_guidance: str | None = None,
    binding_source_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """실패한 설계 노드부터 다시 실행한다.

    게이트의 사용자 입력을 재개하는 ``resume_design``과 달리, 실패 체크포인트에는
    전달할 interrupt 응답이 없다. ``None`` 입력으로 남아 있는 다음 노드만 실행해
    이미 완료·저장된 상위 설계 산출물을 보존한다.
    """
    status = session_status(app_id)
    if not status["retryable"]:
        raise ValueError("The design session has no failed stage to retry.")
    config: RunnableConfig = {"configurable": {"thread_id": app_id}}
    if repair_guidance is not None:
        guidance = repair_guidance.strip()
        if not guidance:
            raise ValueError("Repair guidance must not be empty.")
        if status.get("stage") != "class_diagram":
            raise ValueError("Repair guidance is only supported for class-diagram retries.")
        graph.update_state(config, {"class_binding_repair_guidance": guidance})
    if binding_source_decision is not None:
        if status.get("stage") != "class_diagram":
            raise ValueError("Binding source decisions are only supported for class-diagram retries.")
        graph.update_state(config, {"class_binding_source_decision": binding_source_decision})
    return _invoke_traced_design_graph("retry", app_id, lambda: graph.invoke(None, config))


def rewind_design(app_id: str, stage: str) -> dict[str, Any]:
    """실행 위치를 stage 앞으로 되감고, 거기서부터 다시 만든다.

    "ERD만 다시 만들어줘"에 해당하는 조작이다. 다만 **그 스테이지만** 다시 만들지 않는다 —
    되감은 뒤 그래프는 앞으로 흐르므로, 이어서 진행하면 뒤쪽 산출물도 새 재료로 다시
    만들어진다. 그게 요점이다: API 명세를 바꿨는데 그것을 재료로 만든 배포 다이어그램이
    그대로 남으면 두 산출물이 어긋난다.

    되감기 지점은 **이전 스테이지의 게이트**다. 그 게이트의 advance 엣지가 gen_{stage}로
    이어지기 때문이다. gate_route를 "advance"로 못박아 넘긴다 — 마지막 값이 "loop"이면
    피드백 쪽으로 잘못 흐른다.
    """
    index = DESIGN_STAGES.index(stage)
    if index == 0:
        # 첫 스테이지 앞에는 되감을 게이트가 없다. 그건 start_design이 하는 일이다.
        raise ValueError(f"{stage} is the first stage; use start_design instead.")

    # 아직 만들지 않은 스테이지로 "되감으면" 실제로는 한 걸음 **전진**한다. 조용히
    # 다른 일을 하느니 거절한다 — 부르는 쪽은 되감는다고 믿고 있다.
    here = session_status(app_id)["stage"]
    if here in DESIGN_STAGES and index > DESIGN_STAGES.index(here):
        raise StageNotReached(
            f"{stage} has not been produced yet (the run is at {here})."
        )

    config: RunnableConfig = {"configurable": {"thread_id": app_id}}
    graph.update_state(
        config,
        {"gate_route": "advance"},
        as_node=f"gate_{DESIGN_STAGES[index - 1]}",
    )
    # resume이 아니라 그냥 이어서 실행한다 — 지금 걸려 있는 interrupt가 없다.
    return _result_payload(dict(graph.invoke(None, config)), app_id)


def revise_design_stage(app_id: str, stage: str, feedback: str) -> dict[str, Any]:
    """Re-enter an already produced stage gate and apply one revision.

    Marking ``persist_{stage}`` complete makes the gate the next node without
    running the generator or writing a preliminary artifact version.  Only the
    resumed feedback branch can then produce and persist revised output.
    """

    if stage not in DESIGN_STAGES:
        raise ValueError(f"Unknown design stage: {stage}")
    status = session_status(app_id)
    if not status["exists"]:
        raise ValueError("No saved design checkpoint is available for revision.")
    here = status.get("stage")
    if here in DESIGN_STAGES and DESIGN_STAGES.index(stage) > DESIGN_STAGES.index(here):
        raise StageNotReached(
            f"{stage} has not been produced yet (the run is at {here})."
        )

    config: RunnableConfig = {"configurable": {"thread_id": app_id}}
    snapshot = graph.get_state(config)
    if not snapshot.config:
        raise ValueError("No saved design checkpoint is available for revision.")
    if snapshot.next:
        next_nodes = [str(node) for node in snapshot.next]
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
                "The design run is not at a safe feedback checkpoint; retry it first."
            )
    checkpoint_config = cast(RunnableConfig, dict(snapshot.config))
    checkpoint_next = tuple(str(node) for node in snapshot.next)

    def restore_checkpoint() -> None:
        if not checkpoint_next:
            graph.update_state(
                checkpoint_config,
                {},
                as_node=f"gate_{DESIGN_STAGES[-1]}",
            )
            return
        prior_gate = checkpoint_next[0]
        prior_stage = prior_gate.removeprefix("gate_")
        restored_config = graph.update_state(
            checkpoint_config,
            {},
            as_node=f"persist_{prior_stage}",
        )
        waiting = dict(graph.invoke(None, restored_config))
        interrupts = waiting.get("__interrupt__") or []
        value = interrupts[0].value if interrupts else {}
        if not isinstance(value, dict) or value.get("stage") != prior_stage:
            raise RuntimeError(
                f"Design checkpoint restoration did not re-enter {prior_gate}."
            )

    def invocation():
        try:
            graph.update_state(config, {}, as_node=f"persist_{stage}")
            waiting = dict(graph.invoke(None, config))
            interrupts = waiting.get("__interrupt__") or []
            value = interrupts[0].value if interrupts else {}
            if not isinstance(value, dict) or value.get("stage") != stage:
                raise RuntimeError(
                    f"Design revision could not enter the {stage} feedback gate."
                )
            return graph.invoke(Command(resume=feedback), config)
        except Exception:
            restore_checkpoint()
            raise

    return _invoke_traced_design_graph("revise_stage", app_id, invocation)


def session_status(app_id: str) -> dict[str, Any]:
    """이 앱의 설계 실행이 지금 어떤 상태인가.

    반환 {"exists": bool, "active": bool, "retryable": bool,
          "stage": str|None, "node": str|None}
      exists — 체크포인트가 있다(한 번이라도 돌렸다). 되감을 수 있다는 뜻.
      active — 게이트에서 멈춰 있다. 재개할 수 있다는 뜻.
      retryable — 실패한 생성·피드백·저장 노드부터 재시도할 수 있다는 뜻.
      stage  — 멈춰 있는 스테이지(멈춰 있지 않으면 None).
      node   — 체크포인트가 가리키는 정확한 다음 노드(완료했으면 None).

    **두 상태를 구별해야 한다.** 파이프라인이 END까지 가면 active는 False가 되지만
    exists는 True다. 되감기가 가장 필요한 시점이 바로 그때(다 만들고 나서 "API 명세가
    잘못됐네")이므로, 되감기를 active로 막으면 안 된다.

    화면은 새로고침 뒤에 이걸 물어본다 — 상태는 서버(체크포인트)에만 있다.
    """
    config: RunnableConfig = {"configurable": {"thread_id": app_id}}
    snapshot = graph.get_state(config)
    status: dict[str, Any] = {
        "exists": bool(snapshot.values),
        "active": False,
        "retryable": False,
        "stage": None,
        "node": None,
    }
    if not snapshot.next:
        return status

    # 멈춰 있는 노드 이름에서 스테이지를 읽는다: gate_api_spec → api_spec.
    node = snapshot.next[0]
    status["node"] = node
    status["active"] = node.startswith("gate_")
    status["retryable"] = node.startswith(("gen_", "fb_", "persist_"))
    for prefix in ("gate_", "gen_", "fb_", "persist_"):
        if node.startswith(prefix):
            status["stage"] = node[len(prefix) :]
            break
    return status


def has_active_session(app_id: str) -> bool:
    """게이트에서 멈춰 있어서 resume할 수 있는가.

    없는데 resume하면 LangGraph는 예외를 내지 않고 **빈 입력으로 처음부터** 돌아버린다.
    유스케이스 명세도 없이 도니까 빈 산출물이 만들어지고 그게 저장까지 된다. 그래서
    부르는 쪽이 먼저 확인해야 한다.
    """
    return session_status(app_id)["active"]


def has_design_run(app_id: str) -> bool:
    """되감을 실행이 있는가 — 완료된 실행도 포함한다."""
    return session_status(app_id)["exists"]


def sync_design_state(app_id: str, values: dict[str, Any]) -> None:
    """파이프라인 밖에서 고친 산출물을 체크포인트에도 반영한다.

    지목 수정(`app/design/cascade.py`)은 그래프를 돌리지 않고 모델을 직접 고친다.
    그러면 저장소와 체크포인트가 갈라진다 — 나중에 재개하면 **고치기 전 상태**로
    돌아가 버린다. 여기서 맞춰둔다.

    세션이 없으면(한 번도 안 돌렸거나 리셋됨) 맞출 것도 없으므로 그냥 넘어간다.
    """
    if not has_design_run(app_id):
        return
    config: RunnableConfig = {"configurable": {"thread_id": app_id}}
    graph.update_state(config, values)


def reset_design(app_id: str) -> None:
    """세션 체크포인트를 지운다. 설계를 처음부터 다시 돌리고 싶을 때.

    산출물(artifacts 테이블)은 건드리지 않는다 — 그건 이력이고, 여기서 지우는 것은
    "어디까지 왔는지"뿐이다.
    """
    graph.checkpointer.delete_thread(app_id)
