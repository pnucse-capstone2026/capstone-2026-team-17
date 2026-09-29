"""구조화된 feedback 의도를 stage 재생성·하위 cascade로 조율하는 canonical 경계다.

정책(사용자 결정): 상위 stage를 재생성하면 하위(specs/relationships/diagram)는 '없던 것'으로
보고 fresh 재실행한다. 같은 stage 내 local이면 대상 항목만 재생성(형제 보존)하되, 하위는 그래도
재실행한다. cascade는 선형 의존(actors→use_cases→coverage→specs→relationships→diagram)을 따른다.
"""
from __future__ import annotations

from typing import cast

from app.requirements import stage_registry as stages
from app.requirements.config import settings
from app.requirements.contracts.request import FeedbackEdit
from app.requirements.contracts.state import AgentState
from app.requirements.modeling.diagram import render_diagram  # noqa: F401
from app.requirements.modeling.feedback import resolve_intent
from app.requirements.modeling.relationships import identify_relationships
from app.requirements.modeling.specifications import generate_specs
from app.requirements.modeling.use_cases import (
    check_coverage,  # noqa: F401 — _STAGE_FN_NAME이 globals()로 찾는다
    identify_actors,
    identify_use_cases,
)
from app.requirements.orchestration import playbook
from app.requirements.schemas import FeedbackIntent

# 선형 stage 순서·재실행 함수 이름·편집 가능 stage는 전부 단계 목록에서 파생한다
# (app/requirements/stage_registry.py). 예전엔 여기 손으로 적혀 있어서, 파이프라인을
# 바꾸면 그래프와 여기가 조용히 어긋날 수 있었다.
#
# 함수는 이름으로 찾아 globals()에서 꺼낸다 — 테스트가 이 모듈의 stage 함수를
# monkeypatch할 수 있어야 하기 때문이다. 그래서 정적 분석에는 위 두 임포트가
# 미사용으로 보인다(noqa 참고).
_ORDER = list(stages.cascade_order())
_STAGE_FN_NAME = stages.node_by_key()
_EDITABLE = stages.editable_keys()


def _clamp_editable_stage(up_to: str) -> str:
    """up_to 위치 이하에서 재생성 가능한 최상위 stage를 돌려준다(coverage→use_cases 등)."""
    ui = _ORDER.index(up_to)
    positions = [i for i, s in enumerate(_ORDER) if s in _EDITABLE and i <= ui]
    return _ORDER[max(positions)]


def _regenerate_stage(state: AgentState, intent: FeedbackIntent) -> None:
    """의도에 따라 대상 stage를 재생성해 state에 반영한다(하위는 _cascade가 처리).

    local scope면 target_ids를 넘겨 대상 항목만 고치고 형제는 보존한다(use_cases/specs 지원).
    """
    instr = intent.instruction
    targets = intent.target_ids if intent.scope == "local" else None
    if intent.stage == "actors":
        # Actor identity edits are deliberately singular: the workspace has
        # already validated the catalog ACTn, and modeling must not reconstruct
        # identity from a mutable label or feedback prose.
        if targets is not None and len(targets) != 1:
            raise ValueError("Targeted actor feedback requires exactly one actorRef.")
        state.update(cast(AgentState, identify_actors(
            state,
            feedback=instr,
            target_ref=targets[0] if targets else None,
        )))
    elif intent.stage == "use_cases":
        state.update(cast(AgentState, identify_use_cases(state, feedback=instr, target_ids=targets)))
    elif intent.stage == "specs":
        state.update(cast(AgentState, generate_specs(state, feedback=instr, target_ids=targets)))
    elif intent.stage == "relationships":
        state.update(cast(AgentState, identify_relationships(state, feedback=instr)))


def _cascade(
    state: AgentState, edited_stage: str, up_to: str | None = None
) -> list[str]:
    """편집 stage의 하위 stage를 순서대로 fresh 재실행한다(피드백 없이).

    up_to를 주면 그 stage까지만 재실행한다(게이트가 자기 단계까지만 cascade하도록). 기본은 끝까지.
    """
    end = len(_ORDER) - 1 if up_to is None else _ORDER.index(up_to)
    ran: list[str] = []
    for st in _ORDER[_ORDER.index(edited_stage) + 1: end + 1]:
        fn = _STAGE_FN_NAME.get(st)
        if fn is None:
            continue
        state.update(cast(AgentState, globals()[fn](state)))
        ran.append(st)
    return ran


def apply_feedback_upto(
    state: AgentState, feedback: FeedbackEdit, up_to: str
) -> tuple[FeedbackIntent, list[str]]:
    """게이트용: 피드백에서 의도를 정해 대상 stage를 재생성하고, up_to stage까지만 cascade한다.

    게이트는 파이프라인을 아직 up_to까지만 진행한 상태이므로, 아직 생성되지 않은 하위는 건드리지
    않는다. 의도가 up_to보다 하위 stage를 가리키면(아직 없는 산출물) up_to로 클램프한다.
    **클램프는 구조화 편집에도 똑같이 건다** — 화면이 보낸 값이라고 믿고 아직 없는 산출물을
    재생성하려 들면 거기서 깨진다.
    반환: (적용된 의도, 재실행된 하위 stage 목록).
    """
    intent = resolve_intent(feedback, state)
    if _ORDER.index(intent.stage) > _ORDER.index(up_to):
        # 아직 생성 안 된 하위를 지목 → up_to 이하의 '재생성 가능한' 최상위 stage로 클램프.
        # (coverage/diagram은 결정론 파생 stage라 재생성 대상이 아님 → 각각 use_cases/relationships로.)
        clamp = _clamp_editable_stage(up_to)
        intent = intent.model_copy(update={"stage": clamp, "scope": "broad", "target_ids": []})
    _regenerate_stage(state, intent)
    cascaded = _cascade(state, intent.stage, up_to=up_to)
    return intent, cascaded


def _consistency(state: AgentState) -> dict[str, object]:
    """재생성본의 결정론 정합성 지표."""
    cov = state.get("coverage", {})
    rel = state.get("relationships", {})
    return {
        "coverage_ratio": cov.get("coverage_ratio"),
        "orphan_fr_ids": cov.get("orphan_fr_ids", []),
        "orphan_actors": rel.get("orphan_actors", []),
        "dropped_refs": rel.get("dropped_refs", []),
        "spec_issues_total": sum(len(s.get("issues", [])) for s in state.get("use_case_specs", [])),
    }


def apply_feedback(
    state: AgentState,
    feedback: FeedbackEdit,
    run_id: str = "",
    dataset: str = "",
) -> tuple[AgentState, dict[str, object]]:
    """피드백을 적용해 대상 stage를 재생성하고 하위를 cascade 재실행한다.

    **적용과 동시에 배운다.** 과제 목표 1의 후반부가 "사용자 피드백을 위한 에이전트 갱신"인데,
    지금까지 피드백은 **산출물만 고치고 사라졌다** — 같은 지적을 세 번 받아도 네 번째 실행이
    같은 것을 냈다. 여기서 플레이북에 남겨야 다음 실행이 읽는다
    (`orchestration/playbook.py`).

    쌓는 것과 쓰는 것은 갈라져 있다: 여기서는 항상 쌓고, 생성 프롬프트에 실릴지는
    `settings.playbook_enabled`가 정한다.

    `run_id`는 "서로 다른 실행에서 두 번"을 세는 열쇠다. 안 주면 세지 못하므로 배우지도
    않는다 — 없는 실행 구분을 지어내느니 안 배우는 편이 낫다.

    반환: (갱신된 state, 리포트). state는 in-place로도 갱신된다.
    """
    intent = resolve_intent(feedback, state)
    _regenerate_stage(state, intent)
    cascaded = _cascade(state, intent.stage)
    if run_id:
        playbook.record_feedback(settings.playbook_path, intent, run_id, dataset)
    report: dict[str, object] = {
        "intent": intent.model_dump(),
        "regenerated": intent.stage,
        "cascaded": cascaded,
        "consistency": _consistency(state),
    }
    return state, report
