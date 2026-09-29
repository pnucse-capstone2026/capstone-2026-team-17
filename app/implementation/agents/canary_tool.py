"""Small deterministic tools for the live model/tool protocol canary."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Self

from openhands.sdk.tool import (
    Action,
    Observation,
    ToolAnnotations,
    ToolDefinition,
    ToolExecutor,
    register_tool,
)

from .harness import CANARY_CHECK_TOOL, CANARY_MARKER, CANARY_READ_TOOL
_REGISTERED = False
_LOCK = threading.Lock()


class CanaryReadAction(Action):
    """Read the fixed canary marker. This action takes no arguments."""


class CanaryCheckAction(Action):
    """Return the exact marker received from the read tool."""

    marker: str


class CanaryObservation(Observation):
    """A deterministic canary tool result."""


class CanaryReadExecutor(ToolExecutor):
    def __call__(self, _action, conversation=None):  # noqa: ANN001, ARG002
        return CanaryObservation.from_text(text=CANARY_MARKER)


class CanaryCheckExecutor(ToolExecutor):
    def __call__(self, action, conversation=None):  # noqa: ANN001, ARG002
        if action.marker != CANARY_MARKER:
            return CanaryObservation.from_text(
                text="CANARY_FAILED: marker did not match the preceding tool result",
                is_error=True,
            )
        return CanaryObservation.from_text(text="CANARY_PASSED")


class CanaryReadTool(ToolDefinition[CanaryReadAction, CanaryObservation]):
    name = CANARY_READ_TOOL

    @classmethod
    def create(cls, conv_state) -> Sequence[Self]:  # noqa: ARG003
        return [
            cls(
                description="Read the fixed EasyDep canary marker. Takes no arguments.",
                action_type=CanaryReadAction,
                observation_type=CanaryObservation,
                executor=CanaryReadExecutor(),
                annotations=ToolAnnotations(
                    title=CANARY_READ_TOOL,
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            )
        ]


class CanaryCheckTool(ToolDefinition[CanaryCheckAction, CanaryObservation]):
    name = CANARY_CHECK_TOOL

    @classmethod
    def create(cls, conv_state) -> Sequence[Self]:  # noqa: ARG003
        return [
            cls(
                description=(
                    "Pass the exact marker returned by easydep_canary_read. "
                    "The required argument is named marker."
                ),
                action_type=CanaryCheckAction,
                observation_type=CanaryObservation,
                executor=CanaryCheckExecutor(),
                annotations=ToolAnnotations(
                    title=CANARY_CHECK_TOOL,
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            )
        ]


def register_canary_tools() -> tuple[str, str]:
    global _REGISTERED
    if _REGISTERED:
        return CANARY_READ_TOOL, CANARY_CHECK_TOOL
    with _LOCK:
        if not _REGISTERED:
            register_tool(CANARY_READ_TOOL, CanaryReadTool)
            register_tool(CANARY_CHECK_TOOL, CanaryCheckTool)
            _REGISTERED = True
    return CANARY_READ_TOOL, CANARY_CHECK_TOOL
