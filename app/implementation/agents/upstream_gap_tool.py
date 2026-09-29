"""A bounded owner escape hatch for design evidence that cannot express a behavior."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Self

from openhands.sdk.tool import (
    Action,
    Observation,
    ToolAnnotations,
    ToolDefinition,
    ToolExecutor,
)
from pydantic import Field

UPSTREAM_GAP_TOOL_NAME = "report_upstream_gap"
_REGISTERED = False
_REGISTRATION_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class UpstreamGapOption:
    """One bounded, mutually exclusive upstream choice exposed to the user."""

    id: str
    label: str
    description: str
    requested_effect: str

    def as_result(self) -> dict[str, str]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "requestedEffect": self.requested_effect,
        }


@dataclass(frozen=True, slots=True)
class UpstreamGap:
    """The single TaskSpec reference that blocks a bounded implementation task."""

    summary: str
    source_ref: str
    options: tuple[UpstreamGapOption, ...] = ()

    def as_result(self) -> dict[str, object]:
        result: dict[str, object] = {
            "summary": self.summary,
            "sourceRef": self.source_ref,
        }
        if self.options:
            result["options"] = [option.as_result() for option in self.options]
        return result


class UpstreamGapAction(Action):
    """Report a concise behavior gap tied to one server-provided source reference."""

    summary: str = Field(min_length=1, max_length=500)
    source_ref: str = Field(min_length=1)


class UpstreamGapObservation(Observation):
    """The deterministic result of an upstream-gap report."""


class UpstreamGapSession:
    """Store one valid report for the runtime that owns this conversation."""

    def __init__(self, source_refs: list[str]) -> None:
        self.source_refs = frozenset(source_refs)
        self.result: UpstreamGap | None = None

    def report(self, action: UpstreamGapAction) -> UpstreamGap | None:
        summary = action.summary.strip()
        if not summary or len(summary) > 500 or action.source_ref not in self.source_refs:
            return None
        self.result = UpstreamGap(summary=summary, source_ref=action.source_ref)
        return self.result


class UpstreamGapExecutor(ToolExecutor):
    def __init__(self, source_refs: list[str]) -> None:
        self.session = UpstreamGapSession(source_refs)

    def __call__(self, action, conversation=None):  # noqa: ANN001
        result = self.session.report(action)
        if result is None:
            return UpstreamGapObservation.from_text(
                text=(
                    "UPSTREAM_GAP_SOURCE_REF_INVALID: source_ref must exactly match one "
                    "source reference supplied for this task."
                ),
                is_error=True,
            )
        if conversation is not None:
            from openhands.sdk.conversation.state import ConversationExecutionStatus

            conversation.state.execution_status = ConversationExecutionStatus.FINISHED
        return UpstreamGapObservation.from_text(
            text="UPSTREAM_GAP_REPORTED: the task is paused for upstream input."
        )


class UpstreamGapTool(ToolDefinition[UpstreamGapAction, UpstreamGapObservation]):
    name = UPSTREAM_GAP_TOOL_NAME

    @classmethod
    def create(cls, conv_state, source_refs: list[str]) -> Sequence[Self]:  # noqa: ARG003
        allowed_refs = "\n".join(f"- {source_ref}" for source_ref in source_refs)
        return [
            cls(
                description=(
                    "Stop this preflighted task when admitted evidence either concretely "
                    "contradicts the frozen contract or omits or ambiguously defines required "
                    "upstream product or runtime meaning, so implementation or validation would "
                    "require guessing. Do not use this for ordinary wiring or framework choices, "
                    "and do not keep searching without a concrete blocker. "
                    "Give a concise summary and exactly one "
                    "source_ref from this exact allowlist:\n"
                    f"{allowed_refs}"
                ),
                action_type=UpstreamGapAction,
                observation_type=UpstreamGapObservation,
                executor=UpstreamGapExecutor(source_refs),
                annotations=ToolAnnotations(
                    title=UPSTREAM_GAP_TOOL_NAME,
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            )
        ]


def register_upstream_gap_tool() -> str:
    """Register the native tool once, only when a bounded task needs it."""

    global _REGISTERED
    if _REGISTERED:
        return UPSTREAM_GAP_TOOL_NAME
    with _REGISTRATION_LOCK:
        if not _REGISTERED:
            from openhands.sdk.tool import register_tool

            register_tool(UPSTREAM_GAP_TOOL_NAME, UpstreamGapTool)
            _REGISTERED = True
    return UPSTREAM_GAP_TOOL_NAME


def reported_upstream_gap(agent: object | None) -> UpstreamGap | None:
    """Read a valid report from the instantiated native executor, if any."""

    tools = getattr(agent, "_tools", None)
    tool = tools.get(UPSTREAM_GAP_TOOL_NAME) if isinstance(tools, dict) else None
    session = getattr(getattr(tool, "executor", None), "session", None)
    result = getattr(session, "result", None)
    return result if isinstance(result, UpstreamGap) else None
