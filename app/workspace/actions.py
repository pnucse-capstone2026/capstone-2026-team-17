"""Workspace action registry와 공개 다음-action 정책이다.

repository를 호출하지 않는 순수 모듈이다. command snapshot만으로 client가 다음에 보낼 수 있는
action을 결정한다. service의 payload 검증, 단계 routing과 dispatch도 같은 registry를 사용한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .contracts import ActionOffer, AwaitingOutcome, WaitReason, WorkspaceAction


class StagePolicy(StrEnum):
    CURRENT = "current"
    REFERENCE = "reference"
    REQUIREMENTS = "requirements"
    DESIGN = "design"
    IMPLEMENTATION = "implementation"
    TESTING = "testing"


@dataclass(frozen=True, slots=True)
class ActionSpec:
    action: WorkspaceAction
    handler: str
    stage_policy: StagePolicy
    required_payload: tuple[str, ...] = ()


_SPECS = (
    ActionSpec(WorkspaceAction.MESSAGE, "stage_message", StagePolicy.CURRENT),
    ActionSpec(
        WorkspaceAction.ADVANCE,
        "stage_message",
        StagePolicy.REFERENCE,
        ("action_id",),
    ),
    ActionSpec(
        WorkspaceAction.PLAN_DOWNSTREAM_REVISION,
        "plan_downstream_revision",
        StagePolicy.DESIGN,
        ("action_id",),
    ),
    ActionSpec(
        WorkspaceAction.CONFIRM_CHANGE,
        "confirm_change",
        StagePolicy.REFERENCE,
        ("action_id",),
    ),
    ActionSpec(
        WorkspaceAction.DISMISS_CHANGE,
        "dismiss_change",
        StagePolicy.REFERENCE,
        ("action_id",),
    ),
    ActionSpec(
        WorkspaceAction.START_DESIGN,
        "stage_message",
        StagePolicy.DESIGN,
        ("action_id",),
    ),
    ActionSpec(
        WorkspaceAction.RETRY_REQUIREMENTS,
        "retry_requirements",
        StagePolicy.REQUIREMENTS,
        ("action_id",),
    ),
    ActionSpec(
        WorkspaceAction.RETRY_DESIGN,
        "retry_design",
        StagePolicy.DESIGN,
        ("action_id",),
    ),
    ActionSpec(
        WorkspaceAction.START_IMPLEMENTATION,
        "start_implementation",
        StagePolicy.IMPLEMENTATION,
        ("action_id",),
    ),
    ActionSpec(
        WorkspaceAction.RETRY_IMPLEMENTATION,
        "retry_implementation",
        StagePolicy.IMPLEMENTATION,
        ("action_id", "job_id"),
    ),
    ActionSpec(
        WorkspaceAction.RERUN_IMPLEMENTATION,
        "start_implementation",
        StagePolicy.IMPLEMENTATION,
        ("action_id",),
    ),
    ActionSpec(
        WorkspaceAction.START_TESTING,
        "start_testing",
        StagePolicy.TESTING,
        ("action_id", "implementation_job_id"),
    ),
    ActionSpec(
        WorkspaceAction.APPLY_DEPLOYMENT_PREFERENCES,
        "stage_message",
        StagePolicy.REQUIREMENTS,
        ("action_id", "deployment_preferences"),
    ),
    ActionSpec(
        WorkspaceAction.BRANCH_CHECKPOINT,
        "branch_checkpoint",
        StagePolicy.CURRENT,
        ("checkpoint_stage",),
    ),
    ActionSpec(
        WorkspaceAction.RERUN_FROM_STAGE,
        "rerun_from_stage",
        StagePolicy.CURRENT,
        ("restart_stage",),
    ),
)

ACTION_REGISTRY = {spec.action.value: spec for spec in _SPECS}

# HTTP request model이 채우는 수동 실행 기본값이다. 공개 offer에 없는 값이라도 이 값과
# 같으면 실행 의미를 바꾸지 않으므로 허용한다. 반대로 retry처럼 기본값에서 벗어난
# 옵션은 offer가 명시한 경우에만 받을 수 있다.
_PASSIVE_REQUEST_DEFAULTS: dict[str, Any] = {
    "text": "",
    "base_package": "com.easydep.app",
    "allow_assumptions": True,
    "retry_failed": False,
    "auto_approve_method_proposals": False,
}
_INTERNAL_CONVERSATION_FIELDS = {
    "_resource_answer_context",
    "_conversation_actions",
    "_conversation_outcome",
    "conversation_intent",
    "revision_execution",
    "revision_instructions",
    "revision_interpretation",
    "revision_origin_stage",
    "revision_plan",
    "_timeline_text",
    "validated_impact",
    "validated_target_feedbacks",
    "validated_targets",
    "feedback_decision",
    # Added only by Workspace after validating a saved identity-source option.
    "identity_source_answer",
}


def action_spec(action: str | WorkspaceAction) -> ActionSpec:
    try:
        return ACTION_REGISTRY[str(action)]
    except KeyError as error:
        raise ValueError(f"Unknown workspace action: {action}") from error


def validate_payload(action: str | WorkspaceAction, payload: dict[str, Any]) -> None:
    spec = action_spec(action)
    missing = [name for name in spec.required_payload if not payload.get(name)]
    if missing:
        raise ValueError(
            f"Missing values for the {spec.action.value} command: {', '.join(missing)}"
        )


def _offer(
    action: WorkspaceAction,
    label: str,
    payload: dict[str, Any],
    *,
    auto: bool = False,
    description: str | None = None,
) -> ActionOffer:
    return ActionOffer(
        action=action,
        label=label,
        payload=payload,
        auto_selectable=auto,
        description=description,
    )


def _answer_offers(command_id: str, result: dict[str, Any]) -> list[ActionOffer]:
    question = result.get("resource_question") or {}
    choices = question.get("choices") or [] if isinstance(question, dict) else []
    context = question.get("context") if isinstance(question, dict) else None
    pinned_context = (
        dict(context)
        if isinstance(context, dict)
        and context.get("element_ref")
        and isinstance(context.get("validated_target"), dict)
        else None
    )
    if result.get("current_stage") == "class_diagram" and pinned_context is None:
        raise ValueError("A class design question must pin one validated target.")
    offers = [
        _offer(
            WorkspaceAction.MESSAGE,
            str(choice.get("label") or choice.get("value") or "Select"),
            {
                "action_id": command_id,
                "text": str(choice.get("value") or ""),
                **({"context": pinned_context} if pinned_context is not None else {}),
            },
            description=str(choice.get("description") or "") or None,
        )
        for choice in choices
        if isinstance(choice, dict) and choice.get("value") is not None
    ]
    if offers:
        if question.get("allowFreeText") is True:
            offers.append(
                _offer(
                    WorkspaceAction.MESSAGE,
                    "Provide another answer",
                    {
                        "action_id": command_id,
                        **(
                            {"context": pinned_context}
                            if pinned_context is not None
                            else {}
                        ),
                    },
                )
            )
        return offers
    return [
        _offer(
            WorkspaceAction.MESSAGE,
            "Send answer",
            {
                "action_id": command_id,
                **({"context": pinned_context} if pinned_context is not None else {}),
            },
        )
    ]


def _integration_checkpoint_retry_offer(
    command: dict[str, Any],
    result: dict[str, Any],
    stage: str,
    common: dict[str, str],
) -> ActionOffer | None:
    """Offer an explicit retry beside, never instead of, an integration question."""

    payload = command.get("payload") or {}
    command_stage = str(command.get("stage") or "")
    if command_stage == "testing":
        if result.get("_linked_implementation_checkpoint_retryable") is not True:
            return None
        job_id = str(payload.get("implementation_job_id") or "")
    elif stage == "implementation":
        if result.get("checkpoint_retryable") is not True:
            return None
        job_id = str(result.get("job_id") or payload.get("job_id") or "")
    else:
        return None
    if not job_id:
        return None
    return _offer(
        WorkspaceAction.RETRY_IMPLEMENTATION,
        "Retry implementation checkpoint",
        {**common, "job_id": job_id},
    )


def blocking_findings_route(blockers: list[dict[str, Any]]) -> str:
    """Return a user-action route from explicit Testing classification metadata."""

    owner_routes = {
        "environment": "environment",
        "platform": "platform",
        # A plan/schema defect already exhausted the bounded regeneration inside
        # Testing. It belongs to EasyDep, not to the generated application or user.
        "testing": "platform",
        "design": "design",
        "requirements-or-design": "design",
        "platform-or-design": "platform-or-design",
    }
    defect_routes = {
        "ENVIRONMENT_DEFECT": "environment",
        "PLATFORM_DEFECT": "platform",
        "TEST_DEFECT": "platform",
        "UPSTREAM_AMBIGUITY": "design",
        "PLATFORM_OR_DESIGN_DEFECT": "platform-or-design",
    }
    routes: set[str] = set()
    for blocker in blockers:
        owner = str(blocker.get("repair_owner") or "")
        defect_class = str(blocker.get("defect_class") or "")
        route = owner_routes.get(owner) or defect_routes.get(defect_class)
        if not route:
            return ""
        routes.add(route)
    if routes == {"environment"}:
        return "environment"
    if routes == {"platform"}:
        return "platform"
    if routes == {"design"}:
        return "design"
    if routes and routes <= {"platform", "design", "platform-or-design"}:
        return "platform-or-design"
    return ""


def awaiting_outcome(command: dict[str, Any]) -> AwaitingOutcome:
    """대기 중인 명령의 필수 상호작용 계약을 만든다."""

    command_id = str(command.get("command_id") or "")
    result = dict(command.get("result") or {})
    stage = str(result.get("routing_stage") or command.get("stage") or "requirements")
    common = {"action_id": command_id}

    if result.get("action") == WorkspaceAction.CONFIRM_CHANGE.value:
        return AwaitingOutcome(
            wait_reason=WaitReason.REVIEW,
            actions=[
                _offer(WorkspaceAction.CONFIRM_CHANGE, "Apply change", common),
                _offer(WorkspaceAction.DISMISS_CHANGE, "Dismiss change", common),
            ],
        )

    if result.get("deployment_configuration_required"):
        return AwaitingOutcome(
            wait_reason=WaitReason.REVIEW,
            actions=[
                _offer(
                    WorkspaceAction.MESSAGE,
                    "Ask about deployment options",
                    common,
                )
            ],
        )

    if result.get("resource_question") or result.get("resource_questions"):
        question = result.get("resource_question") or {}
        retry_action = _integration_checkpoint_retry_offer(command, result, stage, common)
        if isinstance(question, dict) and question.get("kind") == "suggested":
            return AwaitingOutcome(
                wait_reason=WaitReason.REVIEW,
                actions=[
                    *_answer_offers(command_id, result),
                    _offer(
                        WorkspaceAction.ADVANCE,
                        "Skip suggestion and continue",
                        common,
                        auto=True,
                    ),
                    *([retry_action] if retry_action is not None else []),
                ],
            )
        return AwaitingOutcome(
            wait_reason=WaitReason.QUESTION,
            actions=[
                *_answer_offers(command_id, result),
                *([retry_action] if retry_action is not None else []),
            ],
        )

    raw_feedback_question = result.get("feedback_question")
    if raw_feedback_question is None and isinstance(result.get("validation"), dict):
        raw_feedback_question = result["validation"].get("feedback_question")
    if isinstance(raw_feedback_question, dict):
        try:
            from .conversation.feedback_envelope import Question

            question = Question.model_validate(raw_feedback_question)
        except (TypeError, ValueError):
            question = None
        if question is not None:
            actions = [
                _offer(
                    WorkspaceAction.MESSAGE,
                    option.label,
                    {
                        **common,
                        "feedback_option_id": option.option_id,
                        "text": "\n".join(
                            [
                                option.decision_payload.normalized_meaning.requested_effect,
                                *(
                                    f"Preserve constraint: {constraint}"
                                    for constraint in option.decision_payload.preserved_constraints
                                ),
                            ]
                        ),
                    },
                    description=option.description or None,
                )
                for option in question.options
            ]
            if question.allow_free_text:
                target_ref = question.authority_candidates[0].ref
                actions.append(
                    _offer(
                        WorkspaceAction.MESSAGE,
                        "Provide another answer",
                        {
                            **common,
                            "feedback_free_text": True,
                            "context": {"element_ref": target_ref},
                        },
                    )
                )
            retry_action = _integration_checkpoint_retry_offer(command, result, stage, common)
            if retry_action is not None:
                actions.append(retry_action)
            return AwaitingOutcome(wait_reason=WaitReason.QUESTION, actions=actions)

    if isinstance(result.get("downstream_revision_handoff"), dict):
        return AwaitingOutcome(
            wait_reason=WaitReason.REVIEW,
            actions=[
                _offer(WorkspaceAction.MESSAGE, "Send revision feedback", common),
                _offer(
                    WorkspaceAction.PLAN_DOWNSTREAM_REVISION,
                    "Plan affected design changes",
                    common,
                ),
            ],
        )

    blockers = [item for item in result.get("blocking_findings") or [] if isinstance(item, dict)]
    blocking_route = str(result.get("blocking_route") or blocking_findings_route(blockers))
    repair_job_id = str(
        result.get("job_id") or (command.get("payload") or {}).get("job_id") or ""
    )
    testing_job = result.get("job")
    testing_implementation_job_id = (
        str(testing_job.get("implementation_job_id") or "")
        if isinstance(testing_job, dict)
        else ""
    )
    if blocking_route == "environment" and stage == "testing" and testing_implementation_job_id:
        return AwaitingOutcome(
            wait_reason=WaitReason.EXTERNAL_WAIT,
            actions=[],
        )
    if blocking_route == "environment" and repair_job_id:
        return AwaitingOutcome(
            wait_reason=WaitReason.EXTERNAL_WAIT,
            actions=[],
        )
    if blocking_route == "platform":
        return AwaitingOutcome(
            wait_reason=WaitReason.EXTERNAL_WAIT,
            actions=[],
        )
    if blocking_route in {"design", "platform-or-design"}:
        return AwaitingOutcome(
            wait_reason=WaitReason.REPAIR,
            actions=[],
        )

    if result.get("requires_revision"):
        # An incomplete artifact is owned by the bounded repair loop.  Do not
        # turn a technical finding into a generic user-edit request (or allow
        # approval to skip it); a real user decision is represented above by a
        # typed feedback_question with grounded authority and options.
        findings = [
            item
            for item in result.get("blocking_findings") or []
            if isinstance(item, dict)
        ]
        finding_details = [
            item
            for item in result.get("finding_details") or []
            if isinstance(item, dict)
        ]
        technical_repair = (
            any(item.get("repairable") is not False for item in findings)
            and not any(
                item.get("requires_user_input", item.get("requiresUserInput")) is True
                for item in [*findings, *finding_details]
            )
            and result.get("feedback_question") is None
            and result.get("resource_question") is None
            and not result.get("resource_questions")
        )
        if technical_repair:
            return AwaitingOutcome(
                wait_reason=WaitReason.REPAIR,
                actions=[],
            )
        # A bare revision flag is neither an answer contract nor permission for
        # free-form edits.  Typed questions were returned above; all remaining
        # technical pauses are consumed by the shared checkpoint loop.
        return AwaitingOutcome(wait_reason=WaitReason.REPAIR, actions=[])

    if result.get("kind") == "question" or result.get("questions"):
        actions = _answer_offers(command_id, result)
        preserved = (command.get("payload") or {}).get("_conversation_actions")
        if isinstance(preserved, list):
            for raw in preserved:
                offer = ActionOffer.model_validate(raw)
                if offer.action == WorkspaceAction.MESSAGE:
                    continue
                if any(existing.action == offer.action for existing in actions):
                    continue
                actions.append(offer)
        return AwaitingOutcome(
            wait_reason=WaitReason.QUESTION,
            actions=actions,
        )

    if stage == "design" and result.get("resume_implementation") is True:
        next_action = WorkspaceAction.START_IMPLEMENTATION
        next_label = "Resume implementation"
    else:
        next_action = (
            WorkspaceAction.ADVANCE
            if stage in {"requirements", "design"}
            else WorkspaceAction.MESSAGE
        )
        next_label = (
            "Continue to next stage"
            if next_action == WorkspaceAction.ADVANCE
            else "Send feedback"
        )
    next_payload: dict[str, Any] = dict(common)
    if stage == "design" and result.get("method_proposals"):
        next_label = "Approve method proposals and continue"
        next_payload["auto_approve_method_proposals"] = True
    return AwaitingOutcome(
        wait_reason=WaitReason.REVIEW,
        actions=[
            _offer(WorkspaceAction.MESSAGE, "Send revision feedback", common),
            *(
                [_offer(next_action, next_label, next_payload, auto=True)]
                if next_action != WorkspaceAction.MESSAGE
                else []
            ),
        ],
    )


def terminal_actions(command: dict[str, Any]) -> list[ActionOffer]:
    """종료된 명령에서 가능한 단계 전환 또는 재시도를 반환한다."""

    status = str(command.get("status") or "")
    stage = str(command.get("stage") or "")
    command_id = str(command.get("command_id") or "")
    result = dict(command.get("result") or {})
    common = {"action_id": command_id}
    if result.get("feedback_question_answered_by"):
        return [_offer(WorkspaceAction.MESSAGE, "Continue conversation", common)]
    if (
        status in {"FAILED", "INTERRUPTED"}
        and command.get("action") == "confirm_change"
    ):
        source_action_id = str((command.get("payload") or {}).get("action_id") or "")
        return [
            _offer(WorkspaceAction.MESSAGE, "Ask about this error", common),
            _offer(
                WorkspaceAction.CONFIRM_CHANGE,
                "Retry approved change",
                {"action_id": source_action_id},
                auto=True,
            ),
        ]
    # The service adds this marker only after it has re-read the linked job and
    # confirmed that its current terminal checkpoint is still safe to resume.
    if status in {"FAILED", "CANCELLED"}:
        job = result.get("job")
        job = job if isinstance(job, dict) else {}
        job_id = str(
            (command.get("payload") or {}).get("job_id")
            or result.get("job_id")
            or job.get("job_id")
            or ""
        )
        if (
            stage == "implementation"
            and job_id
            and result.get("_current_implementation_checkpoint_retryable") is True
        ):
            return [
                _offer(
                    WorkspaceAction.RETRY_IMPLEMENTATION,
                    "Retry implementation checkpoint",
                    {**common, "job_id": job_id},
                )
            ]
        return []
    if status in {"FAILED", "INTERRUPTED"}:
        # Non-question technical failures are resumed by the common durable
        # loop; exposing another retry/rerun action can create duplicate work.
        return []
    if status != "COMPLETED":
        return []
    discuss = _offer(WorkspaceAction.MESSAGE, "Continue conversation", common)
    if stage == "requirements":
        return [
            discuss,
            _offer(WorkspaceAction.START_DESIGN, "Start design", common, auto=True),
        ]
    if stage == "design":
        transition = _design_transition_offer(command)
        return [discuss, *([transition] if transition is not None else [])]
    if stage == "implementation":
        job_id = str(result.get("job_id") or "")
        actions = [
            discuss,
            _offer(WorkspaceAction.RERUN_IMPLEMENTATION, "Rerun implementation", common),
        ]
        if job_id:
            actions.append(
                _offer(
                    WorkspaceAction.START_TESTING,
                    "Start testing",
                    {**common, "implementation_job_id": job_id},
                    auto=True,
                )
            )
        return actions
    return [discuss]


def offered_actions(command: dict[str, Any]) -> list[ActionOffer]:
    if command.get("status") == "AWAITING_INPUT":
        return awaiting_outcome(command).actions
    preserved = (command.get("payload") or {}).get("_conversation_actions")
    # A terminal implementation command must expose the retry or fresh rerun
    # derived from its current checkpoint state. A copied conversation action
    # from before the terminal result can otherwise hide that recovery path.
    if (
        command.get("status") in {"FAILED", "INTERRUPTED", "CANCELLED"}
        and command.get("stage") == "implementation"
    ):
        return terminal_actions(command)
    if isinstance(preserved, list):
        actions = [ActionOffer.model_validate(action) for action in preserved]
        if command.get("status") == "COMPLETED" and command.get("stage") == "design":
            # A completed clarification can carry a transition offer copied from
            # the earlier gate. Recompute that offer from current readiness hints
            # so stale "Start implementation" actions cannot bypass unfinished
            # design artifacts.
            actions = [
                offer
                for offer in actions
                if offer.action
                not in {
                    WorkspaceAction.ADVANCE,
                    WorkspaceAction.START_IMPLEMENTATION,
                }
            ]
            transition = _design_transition_offer(command)
            if transition is not None:
                actions.append(transition)
        return actions
    return terminal_actions(command)


def _design_transition_offer(command: dict[str, Any]) -> ActionOffer | None:
    """Choose the next design transition from service-provided readiness hints."""

    result = command.get("result") or {}
    if not isinstance(result, dict):
        return None
    common = {"action_id": str(command.get("command_id") or "")}
    # A design checkpoint branch contains the complete, validated design artifact
    # set, but intentionally has no copied LangGraph execution checkpoint. Treat
    # that branch-entry record as the design completion boundary it represents.
    if (
        command.get("action") == WorkspaceAction.BRANCH_CHECKPOINT.value
        and command.get("status") == "COMPLETED"
        and result.get("checkpoint_stage") == "design"
    ):
        return _offer(
            WorkspaceAction.START_IMPLEMENTATION,
            "Start implementation",
            common,
            auto=True,
        )
    if result.get("design_complete") is True:
        return _offer(
            WorkspaceAction.START_IMPLEMENTATION,
            "Start implementation",
            common,
            auto=True,
        )
    if result.get("design_can_advance") is True:
        return _offer(
            WorkspaceAction.ADVANCE,
            "Continue design",
            common,
            auto=True,
        )
    return None


def result_with_contract(command: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """입력을 바꾸지 않고 status에서 계산한 계약을 붙인다."""

    enriched = dict(result)
    enriched.pop("awaiting_input", None)
    snapshot = {**command, "result": enriched}
    if snapshot.get("status") == "AWAITING_INPUT":
        outcome = awaiting_outcome(snapshot)
        enriched.update(outcome.model_dump(mode="json", exclude_none=True))
    else:
        enriched["actions"] = [
            action.model_dump(mode="json", exclude_none=True)
            for action in offered_actions(snapshot)
        ]
        enriched.pop("wait_reason", None)
    return enriched


def action_is_offered(action: str, payload: dict[str, Any], prior: dict[str, Any]) -> bool:
    """제출 action과 payload 전체가 공개 offer의 실행 범위 안인지 확인한다."""

    for offer in offered_actions(prior):
        if offer.action != action:
            continue
        expected = offer.payload
        if not all(payload.get(key) == value for key, value in expected.items()):
            continue
        extras_are_passive = True
        for key, value in payload.items():
            if key in expected or key in _INTERNAL_CONVERSATION_FIELDS:
                continue
            if action == WorkspaceAction.MESSAGE.value and key in {"text", "context"}:
                # 자유 답변과 UI에서 명시적으로 고른 artifact target은 message offer의
                # 입력 영역이다. 고정 선택 text는 위 expected 비교에서 이미 고정됐다.
                continue
            if key in _PASSIVE_REQUEST_DEFAULTS and value == _PASSIVE_REQUEST_DEFAULTS[key]:
                continue
            extras_are_passive = False
            break
        if extras_are_passive:
            return True
    return False
