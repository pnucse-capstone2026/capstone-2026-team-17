"""각 설계 스테이지 뒤의 대화형 피드백 게이트(LangGraph interrupt, 정적 라우팅).

산출물이 만들어지면 게이트가 그것을 사용자에게 보여주고 **거기서 멈춘다**. 사용자가
피드백을 주면 그 스테이지를 재생성하고 게이트로 되돌아와 다시 물어보고, 비워두면 다음
스테이지로 넘어간다. 즉 "생성 → 확인 → 피드백 여부 선택 → 다음"이 그래프 구조다.

라우팅 방식(정적): 게이트 노드는 상태 업데이트 + 라우팅 마커(gate_route: "advance"|"loop")만
반환하고, 분기 대상은 상위 그래프의 add_conditional_edges(route_gate, {...})가 컴파일
타임에 선언한다. 토폴로지가 런타임에 달라지지 않으므로 그래프 그림이 곧 실제 흐름이다.

게이트를 서브그래프 안이 아니라 **상위 그래프에** 두는 이유는 요구사항 에이전트와 같다
(app/requirements/orchestration/graph.py 상단 docstring): LangGraph는 서브그래프가 interrupt로
멈추면 그 내부 누적 상태를 부모로 올리지 않으므로, 스테이지가 끝난 뒤 부모 레벨에서
멈춰야 멈춘 시점의 산출물이 응답에 실린다.
"""
from __future__ import annotations

from collections.abc import Callable

from langgraph.types import interrupt

from app.design.graphs.subgraphs import FEEDBACK_KEYS
from app.design.schemas.architecture_state import ArchitectureState
from app.repositories.artifact_repository import STAGE_ARTIFACTS


def route_gate(state: ArchitectureState) -> str:
    """게이트가 남긴 마커를 읽어 분기 키("advance"|"loop")를 돌려준다(조건부 엣지용)."""
    return state.get("gate_route", "advance")


def _api_has_no_http_operation(artifact: object) -> bool:
    """Return whether a rendered OpenAPI artifact has no executable operation.

    An empty schema-only API cannot be handed to either OpenAPI Generator.  It
    is a repairable design defect, rather than feedback the user can silently
    approve by submitting an empty gate response.
    """
    if not isinstance(artifact, dict):
        return True
    paths = artifact.get("paths")
    if not isinstance(paths, dict):
        return True
    methods = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
    return not any(
        isinstance(path_item, dict)
        and any(
            str(method).lower() in methods and isinstance(operation, dict)
            for method, operation in path_item.items()
        )
        for path_item in paths.values()
    )


def make_gate(stage: str) -> Callable[[ArchitectureState], dict]:
    """스테이지 하나의 게이트 노드를 만든다.

    interrupt 페이로드에 산출물과 검증 결과를 함께 싣는다 — 화면이 무엇을 보고 판단해야
    하는지가 곧 이 페이로드다. 별도로 저장소를 조회하지 않아도 되게 한다.
    """
    config = STAGE_ARTIFACTS[stage]
    feedback_key = FEEDBACK_KEYS[stage]

    def gate(state: ArchitectureState) -> dict:
        # 규칙 검사 결과. 검사 노드가 없는 스테이지는 빈 dict이고, 그때 `check_status`는
        # None이다 — **"위반 없음"이 아니라 "검사하지 않았다"**이고, 화면은 그 둘을
        # 구별해야 한다. 남은 위반을 게이트에서 숨기면 사용자는 통과했다고 믿는다.
        check = state.get(config.get("check_key") or "") or {}
        artifact = state.get(config["state_key"])
        findings = list(check.get("findings", []))
        api_operation_blocked = (
            stage == "api_spec" and _api_has_no_http_operation(artifact)
        )
        answer = interrupt(
            {
                "stage": stage,
                "status": "needs_repair" if api_operation_blocked else "need_feedback",
                "prompt": (
                    "[api_spec] 구현 가능한 HTTP operation이 없어 다음 단계로 진행할 수 "
                    "없습니다. 유스케이스·BCE Control·시퀀스 호출에 근거한 endpoint를 "
                    "추가하도록 피드백을 입력하세요. 비워두면 같은 근거로 자동 재수정을 "
                    "한 번 시도합니다."
                    if api_operation_blocked
                    else f"[{stage}] 결과에 대한 피드백을 입력하세요. "
                    "비워두면 다음 단계로 진행합니다."
                ),
                "artifact": artifact,
                "valid": state.get(config["valid_key"]) if config["valid_key"] else None,
                "errors": (
                    state.get(config["errors_key"], []) if config["errors_key"] else []
                ),
                "findings": findings,
                "check_status": check.get("stopped"),
                "repair_iters": check.get("repair_iters", 0),
                "repair_history": check.get("repair_history"),
                "requires_revision": bool(findings) or api_operation_blocked,
                "blocking_findings": [
                    {
                        "code": "design.validation",
                        "stage": stage,
                        "target_ids": [],
                        "message": finding,
                        "severity": "error",
                        "repairable": True,
                    }
                    for finding in findings
                ],
                "method_proposals": check.get("method_proposals", []),
            }
        )
        if api_operation_blocked and not str(answer or "").strip():
            # Do not silently approve an API that cannot be generated.  The
            # repair node receives a concrete directive, so an empty "next"
            # action is a bounded retry and the graph remains resumable.
            return {
                feedback_key: (
                    "Repair the API model: add at least one requirement-grounded "
                    "HTTP endpoint using an exact Boundary-to-Control call from "
                    "the sequence diagram."
                ),
                "gate_route": "loop",
            }
        if findings and not str(answer or "").strip():
            return {"gate_route": "loop"}
        if not str(answer or "").strip():
            return {"gate_route": "advance"}
        return {feedback_key: str(answer), "gate_route": "loop"}

    return gate
