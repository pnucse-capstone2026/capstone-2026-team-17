"""A narrow, whole-source replacement tool for editor-only implementation owners."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Self

from openhands.sdk.tool import Action, Observation, ToolAnnotations, ToolDefinition, ToolExecutor
from pydantic import BaseModel, Field

from .harness import render_harness_error
from .workspace import grant_owner_file_access

SOURCE_REPLACE_TOOL_NAME = "replace_source"
SOURCE_EDIT_TOOL_NAME = "edit_source"
_REGISTERED = False
_EDIT_REGISTERED = False
_REGISTRATION_LOCK = threading.Lock()


class SourceReplaceAction(Action):
    """Replace one supplied writable source file with a complete UTF-8 body."""

    path: str = Field(min_length=1)
    source: str = Field(min_length=1)


class ExactSourceEdit(BaseModel):
    old_text: str = Field(min_length=1, max_length=65536)
    new_text: str = Field(max_length=65536)


class SourceEditAction(Action):
    """Apply exact unique text replacements to one existing writable source."""

    path: str = Field(min_length=1)
    edits: list[ExactSourceEdit] = Field(min_length=1, max_length=16)


class SourceReplaceObservation(Observation):
    """Result of a bounded source replacement."""

    failure_code: Literal[
        "EMPTY_SOURCE",
        "SOURCE_TOO_LARGE",
        "INVALID_PATH_ARGUMENT",
        "PATH_OUTSIDE_WORKSPACE",
        "WRITE_OUTSIDE_OWNER_SCOPE",
        "SOURCE_WRITE_FAILED",
        "EDIT_TARGET_MISSING",
        "EDIT_CONTEXT_STALE",
        "EDIT_CONTEXT_AMBIGUOUS",
        "EDIT_CONTEXT_OVERLAP",
    ] | None = None
    rejected_path: str | None = None
    allowed_paths: list[str] = Field(default_factory=list)
    source_sha256: str | None = None
    failure_detail: str | None = None


def source_replace_allowed_paths(workspace: Path, allowed_files: list[str]) -> list[str]:
    """Return the exact allowed targets in the editor's workspace-relative form."""

    root = workspace.resolve()
    paths = [Path(value).resolve().relative_to(root).as_posix() for value in allowed_files]
    return sorted(set(paths))


class SourceEditConflict(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def apply_exact_source_edits(source: str, edits: list[ExactSourceEdit]) -> str:
    """Validate each exact context against the same source snapshot, then edit in memory."""
    spans: list[tuple[int, int, str]] = []
    for edit in edits:
        start = source.find(edit.old_text)
        if start < 0:
            raise SourceEditConflict("EDIT_CONTEXT_STALE", "An edit context no longer matches.")
        if source.find(edit.old_text, start + 1) >= 0:
            raise SourceEditConflict(
                "EDIT_CONTEXT_AMBIGUOUS", "An edit context is not unique in the source."
            )
        spans.append((start, start + len(edit.old_text), edit.new_text))
    ordered = sorted(spans)
    if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
        raise SourceEditConflict("EDIT_CONTEXT_OVERLAP", "Edit contexts overlap.")
    updated = source
    for start, end, replacement in reversed(ordered):
        updated = updated[:start] + replacement + updated[end:]
    return updated


class SourceReplaceExecutor(ToolExecutor):
    def __init__(self, workspace: Path, allowed_files: list[str]) -> None:
        self.workspace = workspace.resolve()
        self.allowed_files = {Path(path).resolve() for path in allowed_files}
        self.allowed_paths = source_replace_allowed_paths(
            self.workspace, [str(path) for path in self.allowed_files]
        )

    def __call__(self, action, conversation=None):  # noqa: ANN001, ARG002
        source_sha256 = hashlib.sha256(action.source.encode("utf-8")).hexdigest()
        rejected_path = action.path[:512]
        if not action.source.strip():
            return SourceReplaceObservation.from_text(
                text="SOURCE_REPLACE_EMPTY: source must contain a complete non-empty file body.",
                is_error=True,
                failure_code="EMPTY_SOURCE",
                rejected_path=rejected_path,
                allowed_paths=self.allowed_paths,
                source_sha256=source_sha256,
            )
        supplied = Path(action.path)
        target = (
            supplied.resolve() if supplied.is_absolute() else (self.workspace / supplied).resolve()
        )
        try:
            target.relative_to(self.workspace)
        except ValueError:
            return SourceReplaceObservation.from_text(
                text=render_harness_error(
                    "PATH_OUTSIDE_WORKSPACE",
                    "The path is outside the assigned workspace.",
                    retryable=True,
                ),
                is_error=True,
                failure_code="PATH_OUTSIDE_WORKSPACE",
                rejected_path=rejected_path,
                allowed_paths=self.allowed_paths,
                source_sha256=source_sha256,
            )
        if target not in self.allowed_files:
            return SourceReplaceObservation.from_text(
                text=render_harness_error(
                    "WRITE_OUTSIDE_OWNER_SCOPE",
                    "replace_source accepts only an exact supplied writable source file.",
                    retryable=True,
                ),
                is_error=True,
                failure_code="WRITE_OUTSIDE_OWNER_SCOPE",
                rejected_path=rejected_path,
                allowed_paths=self.allowed_paths,
                source_sha256=source_sha256,
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(action.source, encoding="utf-8")
            grant_owner_file_access(target, self.workspace)
        except OSError as error:
            diagnostic = f"{type(error).__name__} errno={error.errno}"
            if error.strerror:
                diagnostic += f": {error.strerror[:240]}"
            return SourceReplaceObservation.from_text(
                text="SOURCE_WRITE_FAILED: the assigned file could not be written.",
                is_error=True,
                failure_code="SOURCE_WRITE_FAILED",
                rejected_path=rejected_path,
                allowed_paths=self.allowed_paths,
                source_sha256=source_sha256,
                failure_detail=diagnostic[:320],
            )
        return SourceReplaceObservation.from_text(
            text=f"SOURCE_REPLACED: {target.relative_to(self.workspace).as_posix()}"
        )


class SourceEditExecutor(ToolExecutor):
    def __init__(self, workspace: Path, allowed_files: list[str]) -> None:
        self.workspace = workspace.resolve()
        self.allowed_files = allowed_files
        self.allowed_paths = source_replace_allowed_paths(self.workspace, allowed_files)

    def __call__(self, action, conversation=None):  # noqa: ANN001, ARG002
        rejected_path = action.path[:512]
        if action.path not in self.allowed_paths:
            return SourceReplaceObservation.from_text(
                text="WRITE_OUTSIDE_OWNER_SCOPE: edit_source requires one exact supplied writable path.",
                is_error=True,
                failure_code="INVALID_PATH_ARGUMENT",
                rejected_path=rejected_path,
                allowed_paths=self.allowed_paths,
            )
        target = (self.workspace / action.path).resolve()
        if not target.is_file():
            return SourceReplaceObservation.from_text(
                text="EDIT_TARGET_MISSING: edit_source can only modify an existing source file.",
                is_error=True,
                failure_code="EDIT_TARGET_MISSING",
                rejected_path=rejected_path,
                allowed_paths=self.allowed_paths,
            )
        try:
            original = target.read_text(encoding="utf-8")
        except OSError as error:
            return SourceReplaceObservation.from_text(
                text="SOURCE_WRITE_FAILED: the assigned file could not be read for editing.",
                is_error=True,
                failure_code="SOURCE_WRITE_FAILED",
                rejected_path=rejected_path,
                allowed_paths=self.allowed_paths,
                failure_detail=f"{type(error).__name__} errno={error.errno}"[:320],
            )
        except UnicodeError:
            return SourceReplaceObservation.from_text(
                text="SOURCE_WRITE_FAILED: the assigned file is not valid UTF-8.",
                is_error=True,
                failure_code="SOURCE_WRITE_FAILED",
                rejected_path=rejected_path,
                allowed_paths=self.allowed_paths,
            )
        source_sha256 = hashlib.sha256(original.encode("utf-8")).hexdigest()
        try:
            updated = apply_exact_source_edits(original, action.edits)
        except SourceEditConflict as error:
            return SourceReplaceObservation.from_text(
                text=f"{error.code}: {error}",
                is_error=True,
                failure_code=error.code,
                rejected_path=rejected_path,
                allowed_paths=self.allowed_paths,
                source_sha256=source_sha256,
            )
        if len(updated.encode("utf-8")) > 64 * 1024:
            return SourceReplaceObservation.from_text(
                text="SOURCE_TOO_LARGE: edited source exceeds the 64 KiB limit.",
                is_error=True,
                failure_code="SOURCE_TOO_LARGE",
                rejected_path=rejected_path,
                allowed_paths=self.allowed_paths,
                source_sha256=source_sha256,
            )
        written = SourceReplaceExecutor(self.workspace, self.allowed_files)(
            SourceReplaceAction(path=action.path, source=updated)
        )
        if written.is_error:
            return written
        return SourceReplaceObservation.from_text(
            text=f"SOURCE_EDITED: {action.path}",
            rejected_path=action.path,
            allowed_paths=self.allowed_paths,
            source_sha256=hashlib.sha256(updated.encode("utf-8")).hexdigest(),
        )


class SourceReplaceTool(ToolDefinition[SourceReplaceAction, SourceReplaceObservation]):
    name = SOURCE_REPLACE_TOOL_NAME

    @classmethod
    def create(cls, conv_state, allowed_files: list[str]) -> Sequence[Self]:  # noqa: ARG003
        workspace = Path(conv_state.workspace.working_dir)
        allowed = "\n".join(f"- {path}" for path in source_replace_allowed_paths(workspace, allowed_files))
        return [
            cls(
                description=(
                    "Replace the complete UTF-8 body of exactly one supplied writable source. "
                    "Do not use partial patches, shell commands, or repository search. The complete source body must be non-empty. Allowed paths:\n"
                    + allowed
                ),
                action_type=SourceReplaceAction,
                observation_type=SourceReplaceObservation,
                executor=SourceReplaceExecutor(workspace, allowed_files),
                annotations=ToolAnnotations(
                    title=SOURCE_REPLACE_TOOL_NAME,
                    readOnlyHint=False,
                    destructiveHint=True,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            )
        ]


class SourceEditTool(ToolDefinition[SourceEditAction, SourceReplaceObservation]):
    name = SOURCE_EDIT_TOOL_NAME

    @classmethod
    def create(cls, conv_state, allowed_files: list[str]) -> Sequence[Self]:  # noqa: ARG003
        workspace = Path(conv_state.workspace.working_dir)
        allowed = "\n".join(
            f"- {path}" for path in source_replace_allowed_paths(workspace, allowed_files)
        )
        return [
            cls(
                description=(
                    "Apply one or more unique exact old_text/new_text edits to exactly one existing "
                    "supplied writable source. All old_text contexts must match exactly once in "
                    "the original file and must not overlap; the full edit set is validated before "
                    "a single write. For a new file or a broad rewrite use replace_source. Allowed "
                    "paths:\n" + allowed
                ),
                action_type=SourceEditAction,
                observation_type=SourceReplaceObservation,
                executor=SourceEditExecutor(workspace, allowed_files),
                annotations=ToolAnnotations(
                    title=SOURCE_EDIT_TOOL_NAME,
                    readOnlyHint=False,
                    destructiveHint=True,
                    idempotentHint=False,
                    openWorldHint=False,
                ),
            )
        ]


def register_source_replace_tool() -> str:
    """Register the typed editor-only tool once per process."""

    global _REGISTERED
    if _REGISTERED:
        return SOURCE_REPLACE_TOOL_NAME
    with _REGISTRATION_LOCK:
        if not _REGISTERED:
            from openhands.sdk.tool import register_tool

            register_tool(SOURCE_REPLACE_TOOL_NAME, SourceReplaceTool)
            _REGISTERED = True
    return SOURCE_REPLACE_TOOL_NAME


def register_source_edit_tool() -> str:
    """Register the optional exact-context editor tool once per process."""
    global _EDIT_REGISTERED
    if _EDIT_REGISTERED:
        return SOURCE_EDIT_TOOL_NAME
    with _REGISTRATION_LOCK:
        if not _EDIT_REGISTERED:
            from openhands.sdk.tool import register_tool

            register_tool(SOURCE_EDIT_TOOL_NAME, SourceEditTool)
            _EDIT_REGISTERED = True
    return SOURCE_EDIT_TOOL_NAME
