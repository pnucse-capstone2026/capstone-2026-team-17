"""저장된 Workspace command에서 앱별 최근 대화 문맥을 재구성한다."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.db.models import WorkspaceCommand
from app.db.session import session_scope
from app.workspace import repository
from app.workspace.actions import offered_actions

_MAX_TURN_CHARS = 8_000
_MAX_TOTAL_TURN_CHARS = 24_000
_MAX_DECISION_CHARS = 4_000


class ConversationTurn(BaseModel):
    """LLM에 제공할 사용자 또는 assistant 발화 한 건."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=8_000)
    command_id: str = Field(min_length=1)


class ConversationContext(BaseModel):
    """한 앱의 대화 해석에 필요한 작은 영속 문맥."""

    model_config = ConfigDict(extra="forbid")

    app_id: str = Field(min_length=1)
    workspace: dict[str, Any]
    turns: list[ConversationTurn] = Field(default_factory=list)
    pending_question: str | None = None
    actions: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    target_remap: dict[str, str] = Field(default_factory=dict)


def _recent_conversation_commands(app_id: str, limit: int) -> list[dict[str, Any]]:
    """Read messages plus revision approvals that carry conversational state."""

    with session_scope() as session:
        rows = session.scalars(
            select(WorkspaceCommand)
            .where(
                WorkspaceCommand.app_id == app_id,
                WorkspaceCommand.action.in_(("message", "confirm_change")),
            )
            .order_by(
                WorkspaceCommand.created_at.desc(),
                WorkspaceCommand.command_id.desc(),
            )
            .limit(limit)
        ).all()
        return [repository.command_dict(row) for row in reversed(rows)]


def _assistant_text(result: Mapping[str, Any]) -> str | None:
    """새 대화 결과의 작은 저장 형태만 읽고 단계 결과 문장은 대화로 꾸미지 않는다."""

    conversation = result.get("conversation")
    if isinstance(conversation, Mapping):
        result = conversation
    reply = result.get("reply")
    if isinstance(reply, str) and reply.strip():
        return reply.strip()
    if isinstance(reply, Mapping):
        text = reply.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    clarification = result.get("clarification")
    if isinstance(clarification, Mapping):
        question = clarification.get("question")
        if isinstance(question, str) and question.strip():
            return question.strip()
    kind = result.get("kind") or result.get("type")
    if kind == "reply":
        text = result.get("text")
        return text.strip() if isinstance(text, str) and text.strip() else None
    if kind == "clarification":
        question = result.get("question")
        return question.strip() if isinstance(question, str) and question.strip() else None
    return None


def _pending_question(command: Mapping[str, Any] | None) -> str | None:
    if not command or command.get("status") != "AWAITING_INPUT":
        return None
    result = command.get("result")
    if not isinstance(result, Mapping):
        return None
    conversation = result.get("conversation")
    if isinstance(conversation, Mapping):
        clarification = conversation.get("clarification")
        if isinstance(clarification, Mapping):
            value = clarification.get("question")
            if isinstance(value, str) and value.strip():
                return value.strip()
        if (conversation.get("kind") or conversation.get("type")) == "clarification":
            value = conversation.get("question")
            if isinstance(value, str) and value.strip():
                return value.strip()
    if (result.get("kind") or result.get("type")) == "clarification":
        value = result.get("question")
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("resource_question", "question"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Mapping):
            text = value.get("question") or value.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    questions = result.get("questions") or result.get("resource_questions")
    if isinstance(questions, Sequence) and not isinstance(questions, (str, bytes)):
        first = next((item for item in questions if isinstance(item, str) and item.strip()), None)
        if first:
            return first.strip()
    # Some stages publish one ordinary question instead of the resource-question
    # shape. It is still current input only while the command is awaiting input.
    if (result.get("kind") or result.get("type")) == "question":
        value = result.get("message") or result.get("text")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _explicit_decision(command: Mapping[str, Any]) -> str | None:
    payload = command.get("payload")
    if not isinstance(payload, Mapping):
        return None
    intent = payload.get("conversation_intent")
    if isinstance(intent, Mapping) and intent.get("intent") == "answer":
        instruction = intent.get("instruction")
        if isinstance(instruction, str) and instruction.strip():
            return instruction.strip()
    return None


def _revision_target_remap(command: Mapping[str, Any]) -> dict[str, str]:
    """Read only the bounded rename map produced by a completed revision."""

    result = command.get("result")
    if not isinstance(result, Mapping):
        return {}
    execution = result.get("revision_execution")
    if not isinstance(execution, Mapping):
        execution = result
    raw = execution.get("target_remap")
    if not isinstance(raw, Mapping):
        return {}
    pairs = [
        (str(source).strip(), str(target).strip())
        for source, target in raw.items()
        if str(source).strip() and str(target).strip()
    ]
    return dict(pairs[:50])


def _actionable_ancestor(
    command: Mapping[str, Any] | None, app_id: str
) -> Mapping[str, Any] | None:
    """Follow conversational clarification links to the still-actionable command."""

    current = command
    seen: set[str] = set()
    for _ in range(12):
        if not isinstance(current, Mapping):
            return None
        command_id = str(current.get("command_id") or "").strip()
        if not command_id or command_id in seen:
            return None
        seen.add(command_id)
        result = current.get("result")
        conversation = result.get("conversation") if isinstance(result, Mapping) else None
        if not (isinstance(conversation, Mapping) and conversation.get("clarification")):
            return current
        payload = current.get("payload")
        action_id = str(payload.get("action_id") or "").strip() if isinstance(payload, Mapping) else ""
        if not action_id:
            return current
        parent = repository.get_command(action_id)
        if not isinstance(parent, Mapping) or str(parent.get("app_id") or "") != app_id:
            return current
        current = parent
    return current


def _bounded_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n...[content truncated]...\n"
    available = limit - len(marker)
    if available <= 1:
        return text[:limit]
    tail = max(1, available // 3)
    return f"{text[: available - tail]}{marker}{text[-tail:]}"


def _bounded_turns(turns: list[ConversationTurn]) -> list[ConversationTurn]:
    remaining = _MAX_TOTAL_TURN_CHARS
    selected: list[ConversationTurn] = []
    for turn in reversed(turns):
        if remaining <= 0:
            break
        text = _bounded_text(turn.text, min(_MAX_TURN_CHARS, remaining))
        selected.append(turn.model_copy(update={"text": text}))
        remaining -= len(text)
    return list(reversed(selected))


def build_conversation_context(
    app_id: str,
    *,
    limit: int = 12,
) -> ConversationContext:
    """최근 message command와 최신 command 상태로 앱별 문맥을 만든다.

    화면 진행용 event는 읽지 않는다. Message command와 revision approval 결과만 대화
    문맥으로 읽으며, 과거 result의 ``awaiting_input`` flag 대신 command ``status``만 현재
    대기 여부의 기준으로 사용한다.
    """

    if limit < 1 or limit > 50:
        raise ValueError("conversation context limit must be between 1 and 50")
    commands = _recent_conversation_commands(app_id, limit)
    turns: list[ConversationTurn] = []
    decisions: list[str] = []
    target_remap: dict[str, str] = {}
    for command in commands:
        payload = command.get("payload")
        text = payload.get("text") if isinstance(payload, Mapping) else None
        if command.get("action") == "message" and isinstance(text, str) and text.strip():
            turns.append(
                ConversationTurn(
                    role="user",
                    text=_bounded_text(text.strip(), _MAX_TURN_CHARS),
                    command_id=str(command["command_id"]),
                )
            )
        result = command.get("result")
        assistant_text = _assistant_text(result) if isinstance(result, Mapping) else None
        if assistant_text:
            turns.append(
                ConversationTurn(
                    role="assistant",
                    text=_bounded_text(assistant_text, _MAX_TURN_CHARS),
                    command_id=str(command["command_id"]),
                )
            )
        decision = _explicit_decision(command)
        if decision:
            decisions.append(_bounded_text(decision, _MAX_DECISION_CHARS))
        for source, target in _revision_target_remap(command).items():
            # A later rename supersedes an earlier display ref while retaining old aliases.
            target_remap[source] = target
            for older, current in list(target_remap.items()):
                if current == source:
                    target_remap[older] = target

    turns = _bounded_turns(turns)

    latest = repository.latest_command(app_id)
    actionable = _actionable_ancestor(latest, app_id) or latest
    actions = (
        [
            offer.model_dump(mode="json", exclude_none=True)
            for offer in offered_actions(actionable)
        ]
        if actionable
        else []
    )
    workspace = {
        "command_id": latest.get("command_id") if latest else None,
        "stage": latest.get("stage") if latest else None,
        "status": latest.get("status") if latest else None,
        "pending_command_id": actionable.get("command_id") if actionable else None,
        "pending_stage": actionable.get("stage") if actionable else None,
    }
    return ConversationContext(
        app_id=app_id,
        workspace=workspace,
        turns=turns,
        pending_question=(
            _bounded_text(question, 2_000)
            if (question := (_pending_question(actionable) or _pending_question(latest)))
            else None
        ),
        actions=actions,
        decisions=decisions,
        target_remap=target_remap,
    )


__all__ = [
    "ConversationContext",
    "ConversationTurn",
    "build_conversation_context",
]
