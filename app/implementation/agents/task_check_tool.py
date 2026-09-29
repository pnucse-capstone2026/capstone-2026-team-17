"""Serializable OpenHands definition for the legacy focused-check tool."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Self

from openhands.sdk.tool import (
    Action,
    DeclaredResources,
    Observation,
    ToolAnnotations,
    ToolDefinition,
    ToolExecutor,
)

from .task_check import TASK_CHECK_TOOL_NAME, TaskCheckSession
from .harness import render_harness_error


class TaskCheckAction(Action):
    """A focused verification request with no model-controlled arguments."""


class TaskCheckObservation(Observation):
    """Text returned by the assigned compile or test check."""


class TaskCheckExecutor(ToolExecutor):
    def __init__(
        self,
        sandbox: Path,
        task_type: str,
        allowed_write_paths: list[str],
        verification_profile: dict[str, object] | None = None,
        frontend_unit_report_path: Path | None = None,
    ) -> None:
        self.session = TaskCheckSession(
            sandbox,
            task_type,
            list(allowed_write_paths),
            dict(verification_profile) if verification_profile else None,
            frontend_unit_report_path,
        )

    def __call__(self, _action, conversation=None):  # noqa: ANN001, ARG002
        passed, output = self.session.run()
        if not passed:
            no_progress = output.startswith("TASK CHECK NOT RUN")
            output = render_harness_error(
                "NO_PROGRESS_REPEAT" if no_progress else "COMMAND_FAILED",
                output,
                retryable=not no_progress,
                workspace=str(self.session.sandbox),
            )
        return TaskCheckObservation.from_text(text=output, is_error=not passed)


class TaskCheckTool(ToolDefinition[TaskCheckAction, TaskCheckObservation]):
    name = TASK_CHECK_TOOL_NAME

    def declared_resources(self, _action: Action) -> DeclaredResources:
        workspace = str((self.meta or {}).get("workspace", "unknown"))
        return DeclaredResources(
            keys=(f"implementation-check:{workspace}",),
            declared=True,
        )

    @classmethod
    def create(
        cls,
        conv_state,
        *,
        task_type: str,
        allowed_write_paths: list[str],
        verification_profile: dict[str, object] | None = None,
        frontend_unit_report_path: str | None = None,
    ) -> Sequence[Self]:
        sandbox = Path(conv_state.workspace.working_dir)
        return [
            cls(
                description=(
                    "Run the focused compile or test already assigned to this "
                    "implementation task. This tool takes no arguments and cannot "
                    "run arbitrary shell commands. Read a failed result, edit the "
                    "source, and run this check again. Call finish only after it passes."
                ),
                action_type=TaskCheckAction,
                observation_type=TaskCheckObservation,
                executor=TaskCheckExecutor(
                    sandbox,
                    task_type,
                    allowed_write_paths,
                    verification_profile,
                    Path(frontend_unit_report_path) if frontend_unit_report_path else None,
                ),
                annotations=ToolAnnotations(
                    title=TASK_CHECK_TOOL_NAME,
                    readOnlyHint=False,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                meta={"workspace": str(sandbox)},
            )
        ]
