"""Requirements modeling 단계가 검증된 구조화 피드백을 읽는 경계다."""

from __future__ import annotations

from app.requirements.contracts.request import FeedbackEdit
from app.requirements.schemas import FeedbackIntent


def feedback_for(state: dict[str, object], key: str, given: str = "") -> str:
    """직접 전달된 지시를 우선하고 없으면 supervisor의 단계 지시를 읽는다."""

    if given:
        return given
    feedback = state.get("stage_feedback") or {}
    if not isinstance(feedback, dict):
        return ""
    return str(feedback.get(key) or "")


def resolve_intent(feedback: FeedbackEdit, _state: object = None) -> FeedbackIntent:
    """Workspace가 검증한 edit을 재분류하지 않고 modeling intent로 옮긴다."""

    if not isinstance(feedback, FeedbackEdit):
        raise TypeError("Requirements feedback must be a validated FeedbackEdit.")
    return FeedbackIntent(
        stage=feedback.stage,
        scope=feedback.scope,
        target_ids=feedback.target_ids if feedback.scope == "local" else [],
        instruction=feedback.instruction,
    )


__all__ = ["feedback_for", "resolve_intent"]
