"""Workspace adapter가 공유하는 공개 대화 계약이다.

command status가 수명주기의 단일 기준이다. result는 대기 이유와 다음 명령을 설명하지만
flag 조합으로 두 번째 상태 머신을 만들지 않는다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceAction(StrEnum):
    MESSAGE = "message"
    ADVANCE = "advance"
    PLAN_DOWNSTREAM_REVISION = "plan_downstream_revision"
    CONFIRM_CHANGE = "confirm_change"
    DISMISS_CHANGE = "dismiss_change"
    START_DESIGN = "start_design"
    RETRY_REQUIREMENTS = "retry_requirements"
    RETRY_DESIGN = "retry_design"
    START_IMPLEMENTATION = "start_implementation"
    RETRY_IMPLEMENTATION = "retry_implementation"
    RERUN_IMPLEMENTATION = "rerun_implementation"
    START_TESTING = "start_testing"
    APPLY_DEPLOYMENT_PREFERENCES = "apply_deployment_preferences"
    BRANCH_CHECKPOINT = "branch_checkpoint"
    RERUN_FROM_STAGE = "rerun_from_stage"


class CheckpointStage(StrEnum):
    """새 앱으로 보관할 수 있는 완료 시점."""

    REQUIREMENTS = "requirements"
    DESIGN = "design"
    IMPLEMENTATION = "implementation"


class RestartStage(StrEnum):
    """새 분기에서 다시 시작할 수 있는 작업."""

    REQUIREMENTS = "requirements"
    DESIGN = "design"
    IMPLEMENTATION = "implementation"
    TESTING = "testing"


class WaitReason(StrEnum):
    REVIEW = "review"
    QUESTION = "question"
    REPAIR = "repair"
    EXTERNAL_WAIT = "external_wait"


class ActionOffer(BaseModel):
    """모든 client가 같은 방식으로 표시할 서버 제공 명령 한 건이다."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    action: WorkspaceAction
    label: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    auto_selectable: bool = False
    description: str | None = None


class AwaitingOutcome(BaseModel):
    """`AWAITING_INPUT` 명령의 사용자 상호작용 계약이다.

    기술 수리는 실행 중 기존 체크포인트를 자동 재개하므로 사용자 action을
    요구하지 않는다. 실제 질문일 때만 하나 이상의 action을 제공한다.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    wait_reason: WaitReason
    actions: list[ActionOffer] = Field(default_factory=list)
