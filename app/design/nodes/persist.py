"""스테이지가 끝나는 자리에서 산출물을 저장소에 남기는 노드.

**왜 그래프 안에서 저장하나.** 게이트가 없다면 저장은 서빙 레이어의 일이다 — invoke가
끝난 뒤 한 번에 쓰면 되고, 노드는 순수 함수로 남아 테스트가 쉽다. 그런데 게이트가 들어오면
한 번의 그래프 실행이 **여러 HTTP 요청에 걸친다.** "실행이 끝났다"는 순간은 마지막
deployment 뒤에만 오므로, 마지막에 몰아서 저장하면 그 전까지 산출물은 체크포인트 안에만
있고 artifacts 테이블에는 없다. 그러면 게이트에서 멈춰 있는 동안 GET /api/apps/{id}가
빈 값을 돌려주고, 버전 이력도 피드백 반복을 기록하지 못한다.

중복 저장은 문제가 되지 않는다 — LangGraph는 재개할 때 이미 완료된 노드를 다시 실행하지
않는다(체크포인트에서 복원한다).

persist 노드는 생성 쪽과 피드백 쪽 **양쪽에서** 들어온다. 그래서 저장소는 항상 사용자가
게이트에서 보고 있는 것과 일치하고, 피드백을 한 번 줄 때마다 새 버전이 쌓인다.
"""
from __future__ import annotations

from collections.abc import Callable

from app.db.models import ORIGIN_FEEDBACK_REVISED, ORIGIN_GENERATED
from app.design.schemas.architecture_state import ArchitectureState
from app.repositories import artifact_repository

#: 서브그래프 래퍼가 "이 상태를 만든 것이 생성이냐 피드백이냐"를 남기는 상태 키.
ORIGIN_KEY = "stage_origin"


def mark_implemented(state: ArchitectureState, stage: str) -> dict[str, str]:
    """이 스테이지를 완료로 표시한 artifact_status 를 돌려준다(원본은 건드리지 않는다)."""
    status = dict(state.get("artifact_status", {}))
    status[stage] = "implemented"
    return status


def make_persist(stage: str) -> Callable[[ArchitectureState], dict]:
    """스테이지 하나의 저장 노드를 만든다.

    `app_id`가 없으면 저장을 건너뛴다 — 저장소 없이 그래프만 돌려보는 경로(테스트·CLI)를
    막지 않기 위해서다. 요구사항 에이전트의 서빙 레이어도 같은 규칙으로 동작한다
    (app/requirements/api.py: app_id가 있을 때만 저장).
    """

    def persist(state: ArchitectureState) -> dict:
        app_id = state.get("app_id")
        if app_id:
            origin = (
                ORIGIN_FEEDBACK_REVISED
                if state.get(ORIGIN_KEY) == "feedback"
                else ORIGIN_GENERATED
            )
            artifact_repository.save_stages(app_id, [stage], state, origin=origin)

        return {"artifact_status": mark_implemented(state, stage)}

    return persist
