"""Deterministic contracts around the OpenHands implementation agent."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.llm_connection import LlmConnection

HARNESS_POLICY_VERSION = "easydep-openhands-harness/v8"
OWNER_PROMPT_VERSION = "easydep-owner-prompt/v9"
WORKSPACE_PATH_VERSION = "easydep-owner-workspace/v3"
OWNER_TOOL_MODES = frozenset({"editor", "restricted", "terminal"})
PROTOCOL_TOKENS = ("<|channel|>", "<|recipient|>", "<|start|>", "<|end|>")
HARNESS_ERROR_PREFIX = "EASYDEP_HARNESS_ERROR "
CANARY_MARKER = "EASYDEP_CANARY_READY"
CANARY_READ_TOOL = "easydep_canary_read"
CANARY_CHECK_TOOL = "easydep_canary_check"

HarnessErrorCode = Literal[
    "TOOL_NOT_AVAILABLE",
    "TOOL_PROTOCOL_TOKEN_LEAK",
    "TOOL_SCHEMA_INVALID",
    "PATH_OUTSIDE_WORKSPACE",
    "READ_OUTSIDE_TASK_EVIDENCE",
    "WRITE_OUTSIDE_OWNER_SCOPE",
    "ENV_WORKSPACE_PERMISSION",
    "COMMAND_FAILED",
    "NO_PROGRESS_REPEAT",
]


class HarnessCompatibilityError(RuntimeError):
    """The persisted or live model/tool contract cannot safely be used."""


class OwnerEnvironmentPreflightError(RuntimeError):
    """The deterministic owner workspace checks failed before an LLM call."""


def is_provider_output_parse_failure(error: BaseException) -> bool:
    """Recognize the narrow provider failure that is safe to replay.

    Cloudflare returns this after it failed to turn the model output into an
    OpenAI-compatible response. No ActionEvent exists for that physical request,
    so the client-side tool dispatcher has not executed a tool yet. Do not widen
    this predicate to arbitrary HTTP 400 responses.
    """

    message = str(error).casefold()
    return "provider_output_parse_transient" in message or (
        "parsing failed" in message
        and "model generated output" in message
        and "could not be parsed" in message
    )


@dataclass(frozen=True, slots=True)
class HarnessError:
    code: HarnessErrorCode
    retryable: bool
    detail: str


@dataclass(slots=True)
class EndpointRetryRecorder:
    """Collect retry metadata without storing prompts or provider responses."""

    events: list[dict[str, object]] = field(default_factory=list)

    def __call__(
        self,
        attempt_number: int,
        max_attempts: int,
        error: BaseException | None,
    ) -> None:
        if error is None:
            reason = "UNKNOWN_RETRY"
            error_type = None
        else:
            reason = (
                "PROVIDER_OUTPUT_PARSE_TRANSIENT"
                if is_provider_output_parse_failure(error)
                else error.__class__.__name__
            )
            error_type = error.__class__.__name__
        self.events.append(
            {
                "attemptNumber": attempt_number,
                "maxAttempts": max_attempts,
                "reason": reason,
                "errorType": error_type,
            }
        )

    def snapshot(self) -> dict[str, object]:
        reasons: dict[str, int] = {}
        for event in self.events:
            reason = str(event["reason"])
            reasons[reason] = reasons.get(reason, 0) + 1
        return {
            "retryCount": len(self.events),
            "reasons": reasons,
            "events": list(self.events),
        }


def render_harness_error(
    code: HarnessErrorCode,
    detail: str,
    *,
    retryable: bool,
    **context: object,
) -> str:
    """Return a concise human message with a stable machine-readable first line."""

    next_actions = {
        "TOOL_NOT_AVAILABLE": "Use one of the tools supplied in this request.",
        "TOOL_PROTOCOL_TOKEN_LEAK": "Stop this model/tool transport.",
        "TOOL_SCHEMA_INVALID": "Retry once with the declared tool schema.",
        "PATH_OUTSIDE_WORKSPACE": "Use an absolute path rooted at the logical workspace.",
        "READ_OUTSIDE_TASK_EVIDENCE": "Report the missing implementation context without broadening discovery.",
        "WRITE_OUTSIDE_OWNER_SCOPE": "Edit only an assigned implementation root.",
        "ENV_WORKSPACE_PERMISSION": "Repair the runner environment without an LLM retry.",
        "COMMAND_FAILED": "Fix the representative command failure before retrying.",
        "NO_PROGRESS_REPEAT": "Change strategy or preserve the candidate and stop.",
    }
    payload = {
        "errorCode": code,
        "retryable": retryable,
        "nextAction": next_actions[code],
        **{key: value for key, value in context.items() if value is not None},
    }
    return (
        HARNESS_ERROR_PREFIX
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        + "\n"
        + detail.strip()
    )


def classify_harness_error_text(text: str) -> HarnessError | None:
    """Classify known model/tool failures without treating arbitrary prose as state."""

    tool_error_context = bool(
        re.search(
            r"attempted to call tool .+request\.tools|tool(?: call| name| arguments?)",
            text,
            flags=re.IGNORECASE,
        )
    )
    if tool_error_context and any(token in text for token in PROTOCOL_TOKENS):
        return HarnessError(
            "TOOL_PROTOCOL_TOKEN_LEAK",
            False,
            "A model protocol control token appeared in a tool call.",
        )
    marker = re.search(
        r"attempted to call tool ['\"](?P<name>[^'\"]+)['\"] which was not in request\.tools",
        text,
        flags=re.IGNORECASE,
    )
    if marker:
        return HarnessError(
            "TOOL_NOT_AVAILABLE",
            True,
            f"The model called an unavailable tool: {marker.group('name')}",
        )
    if re.search(r"parameters for tool .+ did not match schema", text, flags=re.IGNORECASE):
        return HarnessError(
            "TOOL_SCHEMA_INVALID",
            True,
            "The model supplied arguments that did not match the tool schema.",
        )
    if "PATH_OUTSIDE_WORKSPACE" in text or "outside the assigned workspace" in text.casefold():
        return HarnessError(
            "PATH_OUTSIDE_WORKSPACE",
            True,
            "The resolved path was outside the owner workspace.",
        )
    if "READ_OUTSIDE_TASK_EVIDENCE" in text:
        return HarnessError(
            "READ_OUTSIDE_TASK_EVIDENCE",
            True,
            "The requested read was outside the bounded implementation evidence.",
        )
    if "WRITE_OUTSIDE_OWNER_SCOPE" in text or "outside the assigned implementation roots" in text.casefold():
        return HarnessError(
            "WRITE_OUTSIDE_OWNER_SCOPE",
            False,
            "The requested edit was outside the assigned implementation roots.",
        )
    if "ENV_WORKSPACE_PERMISSION" in text:
        return HarnessError(
            "ENV_WORKSPACE_PERMISSION",
            False,
            "The owner cannot access a path that the harness assigned to it.",
        )
    if "COMMAND_FAILED" in text:
        return HarnessError(
            "COMMAND_FAILED",
            True,
            "The canonical verification command failed.",
        )
    if "NO_PROGRESS_REPEAT" in text:
        return HarnessError(
            "NO_PROGRESS_REPEAT",
            False,
            "The same read was repeated without a source change.",
        )
    return None


@dataclass(slots=True)
class HarnessErrorGuard:
    """Stop deterministic harness failures before they consume an entire agent run."""

    counts: dict[str, int] = field(default_factory=dict)
    terminal_code: str | None = None
    max_recoverable_repeats: int = 2
    _conversation: object | None = field(default=None, init=False, repr=False)

    def bind(self, conversation: object) -> None:
        self._conversation = conversation

    def __call__(self, event: object) -> None:
        try:
            payload = event.model_dump(mode="json")
        except (AttributeError, TypeError, ValueError):
            return
        event_type = event.__class__.__name__
        if event_type == "MessageEvent" and getattr(event, "source", None) == "agent":
            encoded = json.dumps(payload, ensure_ascii=False)
            if any(token in encoded for token in PROTOCOL_TOKENS):
                error = HarnessError(
                    "TOOL_PROTOCOL_TOKEN_LEAK",
                    False,
                    "A model protocol control token appeared in an assistant response.",
                )
                self._record(error)
                return
        if event_type == "ActionEvent":
            action = payload.get("action") if isinstance(payload, dict) else None
            if (
                getattr(event, "tool_name", None) == "file_editor"
                and isinstance(action, dict)
                and action.get("command") != "view"
            ):
                self.counts.pop("COMMAND_FAILED", None)
                self.counts.pop("NO_PROGRESS_REPEAT", None)
        error = classify_harness_error_text(json.dumps(payload, ensure_ascii=False))
        if error is None:
            return
        self._record(error)

    def _record(self, error: HarnessError) -> None:
        self.counts[error.code] = self.counts.get(error.code, 0) + 1
        if error.retryable and self.counts[error.code] < self.max_recoverable_repeats:
            return
        self.terminal_code = error.code
        if self._conversation is None:
            return
        from openhands.sdk.conversation.state import ConversationExecutionStatus

        self._conversation.state.execution_status = ConversationExecutionStatus.ERROR


def _public_payload(value: object) -> object:
    """Remove volatile or private event fields before producing a fingerprint."""

    if isinstance(value, dict):
        return {
            str(key): _public_payload(item)
            for key, item in value.items()
            if key
            not in {
                "id",
                "timestamp",
                "reasoning_content",
                "thinking_blocks",
            }
        }
    if isinstance(value, list):
        return [_public_payload(item) for item in value]
    return value


def _source_fingerprint(root: Path) -> tuple[str, dict[str, str], int]:
    file_hashes: dict[str, str] = {}
    marker_count = 0
    source_root = root / "application"
    if not source_root.is_dir():
        source_root = root
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or any(
            part in {"build", ".gradle", "node_modules", "dist"}
            for part in path.parts
        ):
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        relative = path.relative_to(root).as_posix()
        file_hashes[relative] = hashlib.sha256(content).hexdigest()
        marker_count += content.count(b"TODO") + content.count(b"NotImplemented")
    digest = hashlib.sha256(
        json.dumps(file_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return digest, file_hashes, marker_count


@dataclass(slots=True)
class HarnessProgressTracker:
    """Measure source progress and stop exact read-only loops deterministically."""

    workspace: Path
    max_same_read: int = 4
    max_reads_without_change: int = 16
    initial_source_hash: str = field(init=False)
    source_hash: str = field(init=False)
    initial_files: dict[str, str] = field(init=False, repr=False)
    source_files: dict[str, str] = field(init=False, repr=False)
    marker_count: int = field(init=False)
    valid_source_changes: int = 0
    repeated_read_counts: dict[str, int] = field(default_factory=dict)
    max_repeated_read: int = 0
    reads_without_change: int = 0
    max_reads_without_change_observed: int = 0
    last_failure_fingerprint: str | None = None
    terminal_code: str | None = None
    _conversation: object | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        source_hash, files, markers = _source_fingerprint(self.workspace)
        self.initial_source_hash = source_hash
        self.source_hash = source_hash
        self.initial_files = files
        self.source_files = files
        self.marker_count = markers

    def bind(self, conversation: object) -> None:
        self._conversation = conversation

    def __call__(self, event: object) -> None:
        event_type = event.__class__.__name__
        try:
            payload = event.model_dump(mode="json")
        except (AttributeError, TypeError, ValueError):
            return
        public = _public_payload(payload)
        if event_type == "ObservationEvent":
            observation = payload.get("observation") if isinstance(payload, dict) else None
            if isinstance(observation, dict) and observation.get("is_error") is True:
                encoded = json.dumps(public, ensure_ascii=False, sort_keys=True)
                self.last_failure_fingerprint = hashlib.sha256(
                    encoded.encode("utf-8")
                ).hexdigest()[:16]
            return
        if event_type != "ActionEvent":
            return

        current_hash, current_files, markers = _source_fingerprint(self.workspace)
        if current_hash != self.source_hash:
            self.valid_source_changes += 1
            self.repeated_read_counts.clear()
            self.reads_without_change = 0
        self.source_hash = current_hash
        self.source_files = current_files
        self.marker_count = markers

        tool_name = getattr(event, "tool_name", None)
        encoded = json.dumps(public, ensure_ascii=False, sort_keys=True)
        is_read = tool_name == "grep" or (
            tool_name == "file_editor" and '"command": "view"' in encoded
        )
        if not is_read:
            return
        self.reads_without_change += 1
        self.max_reads_without_change_observed = max(
            self.max_reads_without_change_observed,
            self.reads_without_change,
        )
        key = hashlib.sha256(
            f"{current_hash}\0{encoded}".encode()
        ).hexdigest()[:16]
        count = self.repeated_read_counts.get(key, 0) + 1
        self.repeated_read_counts[key] = count
        self.max_repeated_read = max(self.max_repeated_read, count)
        if count < self.max_same_read or self._conversation is None:
            if (
                self.reads_without_change < self.max_reads_without_change
                or self._conversation is None
            ):
                return
            self.terminal_code = "NO_PROGRESS_READ_BUDGET"
            from openhands.sdk.conversation.state import ConversationExecutionStatus

            self._conversation.state.execution_status = ConversationExecutionStatus.STUCK
            return
        self.terminal_code = "NO_PROGRESS_REPEAT"
        from openhands.sdk.conversation.state import ConversationExecutionStatus

        self._conversation.state.execution_status = ConversationExecutionStatus.ERROR

    def snapshot(self) -> dict[str, object]:
        changed = sorted(
            path
            for path in set(self.initial_files) | set(self.source_files)
            if self.initial_files.get(path) != self.source_files.get(path)
        )
        return {
            "schemaVersion": "easydep-harness-progress/v1",
            "initialSourceHash": self.initial_source_hash,
            "sourceHash": self.source_hash,
            "modifiedImplementationFiles": changed,
            "unimplementedMarkerCount": self.marker_count,
            "validSourceChanges": self.valid_source_changes,
            "maxRepeatedIdenticalRead": self.max_repeated_read,
            "maxReadsWithoutSourceChange": self.max_reads_without_change_observed,
            "lastFailureFingerprint": self.last_failure_fingerprint,
        }


def owner_tool_names(
    mode: str,
    *,
    include_upstream_gap: bool = False,
) -> tuple[str, ...]:
    if mode not in OWNER_TOOL_MODES:
        raise ValueError(f"Unsupported OpenHands owner tool mode: {mode}")
    if mode == "terminal":
        return ("file_editor", "terminal", "finish")
    if mode == "editor":
        names = ("replace_source",)
        if include_upstream_gap:
            names += ("report_upstream_gap",)
        return (*names, "finish")
    names = ("file_editor", "grep", "run_task_check")
    if include_upstream_gap:
        names += ("report_upstream_gap",)
    return (*names, "finish")


def _installed_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def tool_schema_hash(mode: str, *, include_upstream_gap: bool = False) -> str:
    """Hash the named tool contract without embedding task-specific absolute paths."""

    from openhands.sdk.tool.builtins.finish import FinishAction
    from openhands.tools.file_editor import FileEditorAction
    from openhands.tools.grep import GrepAction
    from openhands.tools.terminal import TerminalAction

    from .source_replace_tool import SourceReplaceAction
    from .task_check_tool import TaskCheckAction
    from .upstream_gap_tool import UpstreamGapAction

    action_types = {
        "file_editor": FileEditorAction,
        "grep": GrepAction,
        "run_task_check": TaskCheckAction,
        "terminal": TerminalAction,
        "finish": FinishAction,
        "report_upstream_gap": UpstreamGapAction,
        "replace_source": SourceReplaceAction,
    }
    payload = {
        "tools": {
            name: action_types[name].model_json_schema()
            for name in owner_tool_names(mode, include_upstream_gap=include_upstream_gap)
        },
        "openhandsSdk": _installed_version("openhands-sdk"),
        "openhandsTools": _installed_version("openhands-tools"),
        "contractVersion": HARNESS_POLICY_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_harness_manifest(
    connection: LlmConnection,
    *,
    owner_tool_mode: str,
    reasoning_effort: str,
    canary_result_id: str | None = None,
    include_upstream_gap: bool = False,
) -> dict[str, object]:
    return {
        "schemaVersion": "easydep-openhands-harness-manifest/v1",
        "harnessPolicyVersion": HARNESS_POLICY_VERSION,
        "promptVersion": OWNER_PROMPT_VERSION,
        "workspacePathVersion": WORKSPACE_PATH_VERSION,
        "toolSchemaHash": tool_schema_hash(
            owner_tool_mode,
            include_upstream_gap=include_upstream_gap,
        ),
        "toolNames": list(
            owner_tool_names(owner_tool_mode, include_upstream_gap=include_upstream_gap)
        ),
        "ownerToolMode": owner_tool_mode,
        "provider": connection.provider,
        "model": connection.model,
        "baseUrl": getattr(connection, "base_url", None),
        "reasoningEffort": reasoning_effort,
        "openhandsSdkVersion": _installed_version("openhands-sdk"),
        "openhandsToolsVersion": _installed_version("openhands-tools"),
        "canaryResultId": canary_result_id,
    }


def verify_or_store_harness_manifest(path: Path, expected: dict[str, object]) -> None:
    """Reject incompatible persisted conversations before constructing OpenHands."""

    compatibility_keys = (
        "harnessPolicyVersion",
        "promptVersion",
        "workspacePathVersion",
        "toolSchemaHash",
        "toolNames",
        "ownerToolMode",
        "canaryResultId",
    )
    if path.is_file():
        try:
            persisted = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HarnessCompatibilityError(
                f"Cannot read persisted OpenHands harness manifest: {path}"
            ) from error
        mismatches = [
            key for key in compatibility_keys if persisted.get(key) != expected.get(key)
        ]
        if mismatches:
            details = ", ".join(
                f"{key}={persisted.get(key)!r}->{expected.get(key)!r}"
                for key in mismatches
            )
            raise HarnessCompatibilityError(
                "Cannot resume conversation with a different harness contract: " + details
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8")


def owner_prompt_path() -> Path:
    return Path(__file__).resolve().parent / "prompts" / "easydep_owner_system_prompt.j2"


def manifest_id(manifest: dict[str, object]) -> str:
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def safe_event_text(event: object) -> str:
    """Render public event fields for classifiers without relying on model reasoning."""

    try:
        payload: Any = event.model_dump(mode="json")
    except (AttributeError, TypeError, ValueError):
        return ""
    if isinstance(payload, dict):
        payload.pop("reasoning_content", None)
        payload.pop("thinking_blocks", None)
    return json.dumps(payload, ensure_ascii=False)
