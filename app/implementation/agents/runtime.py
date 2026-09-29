from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config import settings
from app.demo_validation import demo_skip_validation_enabled
from app.llm_connection import LlmConnection
from app.llm_profiles import profile_for
from app.metrics import langsmith as langsmith_metrics
from app.implementation.config import npm_command_environment

from ..runtime.linux_runner_transport import (
    LLM_CREDENTIAL_ENVIRONMENT,
    OWNER_NPM_CACHE,
    OWNER_TERMINAL_HOME,
    OWNER_TERMINAL_SHELL_ENV,
)
from ..workflows.repair import active_repair_for_task, repair_recheck_for_task
from ..workflows.traceability import declared_java_source_matches
from .admission import (
    integration_evidence_paths,
)
from .canary import (
    TRANSIENT_CANARY_FAILURES,
    classify_canary_exception,
    ensure_model_tool_canary,
    model_tool_canary_id,
    open_endpoint_circuit,
)
from .harness import (
    EndpointRetryRecorder,
    HarnessCompatibilityError,
    HarnessErrorGuard,
    HarnessProgressTracker,
    build_harness_manifest,
    classify_harness_error_text,
    is_provider_output_parse_failure,
    owner_prompt_path,
    render_harness_error,
    verify_or_store_harness_manifest,
)
from .provider import (
    openhands_compatibility,
    openhands_connection,
)
from .source_replace_tool import (
    SOURCE_EDIT_TOOL_NAME,
    SOURCE_REPLACE_TOOL_NAME,
    SourceEditAction,
    SourceEditExecutor,
    SourceReplaceAction,
    SourceReplaceExecutor,
    SourceReplaceObservation,
    register_source_edit_tool,
    register_source_replace_tool,
    source_replace_allowed_paths,
)
from .task_check import (
    TaskCheckSession,
    consume_successful_task_check,
    has_successful_task_check,
    is_infrastructure_task_check_failure,
    register_task_check_tool,
    run_task_check,
)
from .upstream_gap_tool import (
    UpstreamGap,
    register_upstream_gap_tool,
    reported_upstream_gap,
)
from .verification.build import (
    WorkspaceVerificationError,
    verify_agent_workspace,
)
from .verification.frontend import store_frontend_build
from .workspace import (
    changed_files,
    cleanup_agent_workspace,
    grant_owner_file_access,
    load_task,
    missing_required_outputs,
    path_is_editable,
    preflight_owner_workspace,
    prepare_agent_workspace,
    prepare_owner_workspace_alias,
    release_owner_workspace_alias,
    snapshot_files,
)

# OpenHands owns the tool/action loop. This only bounds one task conversation.
MAX_AGENT_TURN_ITERATIONS = 32
# The failed real-app baseline spent 238 tool calls without completing after the
# useful first draft was already present around call 28.  A clean real-app run
# reached its first complete implementation at call 61, so 96 leaves one local
# build-and-repair pass without inheriting OpenHands' 500 iteration default.  A
# retry resumes the same persisted conversation.
OWNER_TURN_ITERATIONS = 96
OWNER_TASK_TYPES = frozenset(
    {
        "backend-implementation",
        "frontend-implementation",
        "integration-implementation",
        "backend-unit-test",
        "frontend-unit-test",
    }
)


def effective_task_prompt_sha256(
    task: dict[str, object],
    tasks: list[dict[str, object]],
    run_root: Path | None = None,
) -> str:
    """Bind the integration checkpoint to the admitted owner executions."""

    base = str(task.get("prompt_sha256", ""))
    if task.get("task_type") != "integration-implementation":
        return base
    by_id = {str(item.get("task_id")): item for item in tasks}
    dependencies = [
        {
            "taskId": str(task_id),
            "promptSha256": str(by_id.get(str(task_id), {}).get("prompt_sha256", "")),
            "resultSha256": (
                hashlib.sha256(result_path.read_bytes()).hexdigest()
                if run_root is not None
                and (
                    result_path := run_root
                    / "reports"
                    / "agent-executions"
                    / f"{task_id}.result.json"
                ).is_file()
                else None
            ),
        }
        for task_id in task.get("depends_on", [])
    ]
    evidence_sha256 = None
    if run_root is not None:
        context = json.loads(
            (run_root / str(task["context_file"])).read_text(encoding="utf-8")
        )
        evidence_identity = []
        for path in integration_evidence_paths(run_root, task, context):
            source = run_root / path
            evidence_identity.append(
                {
                    "path": path,
                    "contentSha256": (
                        hashlib.sha256(source.read_bytes()).hexdigest()
                        if source.is_file()
                        else None
                    ),
                    "missing": not source.is_file(),
                }
            )
        evidence_sha256 = hashlib.sha256(
            json.dumps(
                evidence_identity,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    identity = json.dumps(
        {
            'completionMode': str(task.get('completion_mode') or 'agent'),
            "promptSha256": base,
            "dependencies": dependencies,
            "integrationEvidenceSha256": evidence_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


# Operation-marker conversations use the same verified tool harness but do not
# persist the old conversation. A retry starts a fresh, short conversation over
# the preserved candidate source instead of nudging a read-only loop forever.
HARNESS_TASK_TYPES = OWNER_TASK_TYPES
OWNER_CONTINUATION_MESSAGE = (
    "Continue from the current candidate; do not restart repository discovery. Run the "
    "canonical verification command now, inspect only its concrete compiler or test failures, "
    "fix them, rerun verification, and call finish when it passes."
)
OWNER_STUCK_RECOVERY_MESSAGE = (
    "Your last response made no observable progress. Do not restate the task, restart analysis, "
    "or enumerate alternatives. Choose one already inspected writable target and apply its "
    "simplest legal edit now. If no inspected writable target can be edited, report the missing "
    "evidence concisely. For an existing file call file_editor with "
    'command="str_replace", old_str, and new_str; never use command="edit" or old_string/'
    "new_string. Then run canonical verification."
)
OWNER_GAP_RECOVERY_MESSAGE = (
    "Your last response made no observable progress. Do not reread an inspected file, run a "
    "broad grep, restart analysis, or enumerate alternatives. A missing collaborator or wiring "
    "entry alone is not an upstream gap; choose conventional wiring in the writable component "
    "when declared public behavior and existing dependency APIs permit it. Call "
    "report_upstream_gap with one supplied source_ref only when required public input, output, "
    "or externally visible behavior is absent or contradictory, so no legal implementation "
    "exists without inventing product meaning. Otherwise apply the simplest legal edit now "
    'using command="str_replace", old_str, and new_str for an existing file; never use '
    'command="edit" or old_string/new_string. Then run canonical verification.'
)
OWNER_FINISH_RECOVERY_MESSAGE = (
    "The verification command has already been run, but this conversation was not completed. "
    "Do not summarize the work or run another command. Call the FinishTool now to mark this "
    "task complete."
)
OWNER_INITIAL_ACTION_MESSAGE = (
    "Start with one writable source that contains an assigned completion marker. Perform the "
    "first legal file_editor edit from its local declarations and assigned task behavior. If a "
    "concrete implementation need remains, consult only the listed operation contract and declared "
    "dependency sources. Interaction hints are behavioral evidence; use them to understand delegated "
    "behavior, but do not inject dependencies or alter BCE ownership solely because of a hint."
)
OWNER_EDITOR_INITIAL_ACTION_MESSAGE = (
    "For a small change to an existing source, use edit_source with exact unique old_text/new_text "
    "contexts. For a new file or broad rewrite, use replace_source with the complete UTF-8 body. "
    "Use only supplied writable paths; do not search, inspect files, run checks, or use shell commands."
)
EDITOR_REPAIR_MESSAGE = (
    "The editor harness ran the focused check and it failed. Repair only this diagnosis using "
    "exact edit_source contexts for a small change to an existing file, or replace_source for a "
    "broad rewrite. Do not search or run verification; the harness will check after this repair.\n\n"
)
EDITOR_STUCK_RECOVERY_MESSAGE = (
    "Do not explain or analyze. Immediately use edit_source for a small exact change to an "
    "existing supplied source, replace_source for a complete rewrite, or report_upstream_gap only when public behavior is insufficient. "
    "The harness performs verification."
)


EDITOR_READ_EVIDENCE_MAX_BYTES = 64 * 1024
EDITOR_WRITABLE_SOURCE_MAX_BYTES = 64 * 1024
DIRECT_EDITOR_SOURCE_EXTENSIONS = {".java", ".tsx", ".ts", ".js", ".jsx"}
DIRECT_EDITOR_CODE_FENCES = {
    ".java": "java",
    ".tsx": "tsx",
    ".ts": "typescript",
    ".js": "javascript",
    ".jsx": "jsx",
}


def _direct_editor_source_is_eligible(
    task: dict[str, object], task_type: str, writable_paths: list[str], sandbox: Path
) -> bool:
    """Allow the one-call editor only for one bounded code source."""
    if len(writable_paths) != 1:
        return False
    path = Path(writable_paths[0])
    if path.suffix.casefold() not in DIRECT_EDITOR_SOURCE_EXTENSIONS:
        return False
    if task_type == "frontend-implementation":
        markers = task.get("required_completion_markers")
        if not isinstance(markers, list) or not markers or not all(
            isinstance(marker, str) and marker.strip() for marker in markers
        ):
            return False
    source = sandbox / path
    try:
        if source.is_file():
            return source.stat().st_size <= EDITOR_WRITABLE_SOURCE_MAX_BYTES
        return task_type in {"backend-unit-test", "frontend-unit-test"}
    except OSError:
        return False


def _frontend_direct_editor_sdk_evidence(
    sandbox: Path, context: dict[str, object]
) -> list[tuple[str, Path, str]]:
    """Return generated declarations for one frontend operation."""
    operation_ids = context.get("operationIds")
    context_paths = context.get("operationContextPaths")
    if not (
        isinstance(operation_ids, list)
        and len(operation_ids) == 1
        and isinstance(operation_ids[0], str)
        and isinstance(context_paths, list)
        and len(context_paths) == 1
        and isinstance(context_paths[0], str)
    ):
        return []

    sandbox_root = sandbox.resolve()
    operation_path = (sandbox / context_paths[0]).resolve()
    try:
        operation_path.relative_to(sandbox_root)
        if (
            not operation_path.is_file()
            or operation_path.stat().st_size > EDITOR_READ_EVIDENCE_MAX_BYTES
        ):
            return []
        operation_context = json.loads(operation_path.read_text(encoding="utf-8"))
        generated = operation_context["generatedClient"]
        method_path = (sandbox / generated["generatedMethodPath"]).resolve()
        method_path.relative_to(sandbox_root)
        if not method_path.is_file() or generated.get("resolved") is not True:
            return []

        from ..planning.frontend_contracts import (
            GeneratedClientContracts,
            _agent_contract_surface,
            _through_braced_declaration,
        )

        generated_root = method_path.parent.parent.parent
        contracts = GeneratedClientContracts.discover(generated_root)
        operation = contracts.resolve_operations(operation_ids).get(operation_ids[0])
        if operation is None or operation.source_path.resolve() != method_path:
            return []

        declarations: list[tuple[str, Path, str]] = []
        source = method_path.read_text(encoding="utf-8")
        method_signature = next(
            (
                line.strip().removesuffix("{").rstrip()
                for line in source.splitlines()
                if line.lstrip().startswith(f"async {operation.operation_id}(")
                and f"Promise<{operation.response_type}>" in line
                and line.rstrip().endswith("{")
            ),
            "",
        )
        if (
            operation.request_type
            and generated.get("requestType") == operation.request_type
        ):
            marker = f"export interface {operation.request_type}"
            start = source.find(marker)
            if start >= 0:
                declaration = _through_braced_declaration(source[start:], marker)
                if declaration:
                    if method_signature:
                        declaration += f"\n\n{method_signature}"
                    declarations.append(
                        (
                            f"{method_path.relative_to(sandbox_root).as_posix()} :: "
                            f"{operation.request_type}",
                            method_path,
                            declaration,
                        )
                    )
        elif method_signature:
            declarations.append(
                (
                    f"{method_path.relative_to(sandbox_root).as_posix()} :: "
                    f"SDK operation {operation.operation_id}",
                    method_path,
                    method_signature,
                )
            )

        references = operation_context.get("referencedComponents", {})
        schema_names = {
            reference.rsplit("/", 1)[-1]
            for reference in references
            if isinstance(reference, str)
            and reference.startswith("#/components/schemas/")
        } if isinstance(references, dict) else set()
        schema_names.add(operation.response_type)
        model_files = {
            path.stem: path for path in contracts.files if path.parent.name == "models"
        }
        for name in sorted(schema_names):
            model_path = model_files.get(name)
            if model_path is not None:
                surface = _agent_contract_surface(
                    model_path.read_text(encoding="utf-8"),
                    model_path.relative_to(generated_root),
                )
                declarations.append(
                    (
                        f"{model_path.relative_to(sandbox_root).as_posix()} :: SDK model {name}",
                        model_path,
                        surface,
                    )
                )
        return declarations
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        return []


def _editor_read_source_evidence(
    sandbox: Path,
    context: dict[str, object],
    writable_files: list[str],
) -> str:
    """Embed the current source and bounded preselected code dependencies."""

    sandbox_root = sandbox.resolve()
    writable = {Path(path).resolve() for path in writable_files}
    candidates: list[tuple[str, Path, str | None]] = []
    for path in writable_files:
        candidate = Path(path).resolve()
        if candidate.is_file():
            candidates.append(
                (candidate.relative_to(sandbox_root).as_posix(), candidate, None)
            )
    values = context.get("readSourcePaths", [])
    if isinstance(values, list):
        for value in values:
            if not isinstance(value, str) or not value:
                continue
            candidate = (sandbox / value).resolve()
            if (
                not candidate.is_relative_to(sandbox_root)
                or candidate in writable
                or not candidate.is_file()
                or candidate.suffix.casefold()
                not in DIRECT_EDITOR_SOURCE_EXTENSIONS | {".json", ".txt"}
            ):
                continue
            candidates.append((value.replace("\\", "/"), candidate, None))

    sdk_declarations = (
        _frontend_direct_editor_sdk_evidence(sandbox, context)
        if len(writable_files) == 1
        and Path(writable_files[0]).suffix.casefold() == ".tsx"
        else []
    )
    candidates.extend((label, path, body) for label, path, body in sdk_declarations)

    included: list[str] = []
    omitted = 0
    sdk_included = 0
    used = 0
    # Preserve the writable target first, then spend the finite evidence budget
    # on smaller dependencies so large context files cannot crowd out several
    # concise declarations.
    unique_candidates = {path: (candidate, body) for path, candidate, body in candidates}
    ordered_candidates = sorted(
        unique_candidates.items(),
        key=lambda item: (
            item[1][0] not in writable,
            item[1][1] is None,
            len(item[1][1].encode("utf-8"))
            if item[1][1] is not None
            else item[1][0].stat().st_size,
            item[0],
        ),
    )
    for path, (candidate, body_override) in ordered_candidates:
        body = (
            body_override
            if body_override is not None
            else candidate.read_text(encoding="utf-8")
        )
        body_size = len(body.encode("utf-8"))
        if used + body_size > EDITOR_READ_EVIDENCE_MAX_BYTES:
            omitted += 1
            continue
        fence = DIRECT_EDITOR_CODE_FENCES.get(
            ".ts" if body_override is not None else candidate.suffix.casefold(),
            candidate.suffix.lstrip(".") or "text",
        )
        is_sdk_declaration = body_override is not None
        label = (
            "current writable source"
            if candidate in writable
            else "generated TypeScript SDK declaration"
            if is_sdk_declaration
            else "read-only evidence"
        )
        included.append(f"### `{path}` ({label})\n```{fence}\n{body}\n```")
        sdk_included += int(is_sdk_declaration)
        used += body_size

    if not included and not omitted:
        return ""
    sdk_authority = (
        "\nGenerated TypeScript SDK declarations below define application-facing value types. "
        "Use them as the coding authority; operation context describes the wire schema and "
        "may use different types when the SDK converts values.\n"
        if sdk_included
        else ""
    )
    return (
        "\n\n## Supplied writable source and read-only evidence\n\n"
        + sdk_authority
        + "\n\n".join(included)
        + f"\n\nEvidence bodies: {len(included)} included, {omitted} omitted "
        + f"(UTF-8 body cap {EDITOR_READ_EVIDENCE_MAX_BYTES} bytes)."
    )


def _owner_evidence_boundary_message(
    required_test_paths: object, task_type: str = ""
) -> str:
    """Render owner-only prompt text; it does not change execution policy."""

    paths = [
        value
        for value in required_test_paths if isinstance(value, str) and value
    ] if isinstance(required_test_paths, list) else []
    boundary = (
        "The listed writable task files, additional writable roots, and supplied read evidence "
        "are the complete boundary. Do not guess or probe unlisted file or directory paths."
    )
    if task_type in {"backend-unit-test", "frontend-unit-test"}:
        message = boundary
        if paths:
            message += (
                "\nFocused test paths are supplied:\n"
                + "\n".join(f"- `{path}`" for path in paths)
                + "\nUse only these focused test paths for test evidence."
            )
        message += (
            "\nThis is a focused unit-test authoring task. The supplied implementation "
            "source and API evidence are read-only; create the assigned test file and keep "
            "its assertions meaningful."
        )
        if task_type == "frontend-unit-test":
            message += (
                "\nDerive Testing Library queries from the supplied markup's actual accessible "
                "roles and names; an HTML tag alone does not guarantee a role (for example, an "
                "unnamed form is not a form landmark). For pending or disabled behavior, use the "
                "known input and button, or inspect `closest('form')` only when a container "
                "attribute is needed."
            )
        return message
    if paths:
        return (
            boundary
            + "\nFocused test paths are supplied:\n"
            + "\n".join(f"- `{path}`" for path in paths)
            + "\nUse only these focused test paths for test evidence."
        )
    return (
        boundary
        + "\nNo focused test is supplied. Do not search test directories; use only the "
        "provided verification and completion requirements."
    )


_SANDBOX_TOOLS_REGISTERED = False
_SANDBOX_TOOLS_REGISTRATION_LOCK = threading.Lock()


class OwnerConversationIncomplete(WorkspaceVerificationError):
    """An owner stopped at an SDK execution boundary, not a source-code gate."""


class DirectEditorResponseError(RuntimeError):
    """The one-shot editor response did not contain a usable source replacement."""

    def __init__(
        self,
        message: str,
        *,
        failure_code: str = "DIRECT_EDITOR_RESPONSE_INVALID",
        retryable: bool = False,
        rejected_path: str | None = None,
        source_sha256: str | None = None,
        tool_name: str = SOURCE_REPLACE_TOOL_NAME,
        field_issues: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_code = failure_code
        self.retryable = retryable
        self.rejected_path = rejected_path[:512] if rejected_path else None
        self.source_sha256 = source_sha256
        self.tool_name = tool_name
        self.field_issues = (field_issues or [])[:16]


def _is_infrastructure_verification_failure(error: WorkspaceVerificationError) -> bool:
    return is_infrastructure_task_check_failure(str(error))


def _integration_source_owner_attribution(
    run_root: Path,
    failed_task_id: str,
    evidence: dict[str, object],
    diagnosis: str,
) -> dict[str, str] | None:
    """Resolve one named Java failure to its exact declared implementation task."""

    startup = evidence.get("applicationStartup")
    application_log = (
        startup.get("applicationLog", "") if isinstance(startup, dict) else ""
    )
    if not isinstance(application_log, str):
        application_log = ""
    text = "\n".join(
        value[:65536]
        for value in (diagnosis, str(evidence.get("stderr") or ""), application_log)
        if value
    )
    rtm_path = run_root / "reports" / "rtm-traceability-map.json"
    manifest_path = run_root / "reports" / "run-manifest.json"
    try:
        rtm = json.loads(rtm_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(rtm, dict) or not isinstance(manifest, dict):
        return None
    matches = declared_java_source_matches(rtm, text)
    tasks = manifest.get("implementation_tasks")
    if not isinstance(tasks, list):
        return None
    candidates: dict[tuple[str, str], dict[str, str]] = {}
    for match in matches:
        task_id = match.get("taskId")
        target = str(match.get("target_file") or "").replace("\\", "/")
        if not isinstance(task_id, str) or not task_id or not target:
            continue
        task_matches = [
            task
            for task in tasks
            if isinstance(task, dict) and task.get("task_id") == task_id
        ]
        if len(task_matches) != 1 or task_id == failed_task_id:
            continue
        task = task_matches[0]
        if task.get("task_type") not in {
            "backend-implementation",
            "frontend-implementation",
        }:
            continue
        declared_paths = {
            str(path).replace("\\", "/")
            for key in ("allowed_write_paths", "required_output_paths")
            for path in task.get(key, [])
            if isinstance(path, str)
        }
        if target not in declared_paths:
            continue
        candidates[(task_id, target)] = {
            "taskId": task_id,
            "targetFile": target,
        }
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def run_openhands_conversation(conversation: object) -> None:
    """Use the cancellable SDK loop while retaining older test/SDK compatibility."""

    async_run = getattr(conversation, "arun", None)
    if callable(async_run):
        asyncio.run(async_run())
        return
    conversation.run()  # type: ignore[attr-defined]


def _is_owner_task(task_type: str) -> bool:
    return task_type in OWNER_TASK_TYPES


def _is_harness_task(task_type: str) -> bool:
    return task_type in HARNESS_TASK_TYPES


def _owner_conversation_identity(run_root: Path, task_id: str) -> tuple[Path, uuid.UUID]:
    """Return stable OpenHands persistence coordinates for one implementation owner."""

    try:
        job_id = run_root.parents[2].name
    except IndexError:
        job_id = run_root.parent.name
    conversation_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"easydep://implementation/{job_id}/{run_root.name}/{task_id}",
    )
    return run_root / "reports" / "openhands-conversations", conversation_id


def _owner_message_required(
    *,
    resumed: bool,
    conversation: object,
    prompt: str,
) -> bool:
    """Return whether the current task message is absent from persisted history."""

    if not resumed:
        return True
    from openhands.sdk.event import MessageEvent
    from openhands.sdk.llm import content_to_str

    events = conversation.state.events
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if not isinstance(event, MessageEvent) or event.source != "user":
            continue
        if "".join(content_to_str(event.llm_message.content)) == prompt:
            return False
    return True


def _owner_continuation_required(
    *,
    resumed: bool,
    conversation: object,
    prompt: str,
) -> bool:
    """Continue a completed owner turn whose deterministic verification failed."""

    if not resumed:
        return False
    from openhands.sdk.event import ActionEvent, MessageEvent
    from openhands.sdk.llm import content_to_str

    events = conversation.state.events
    matching_user_index: int | None = None
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if not isinstance(event, MessageEvent) or event.source != "user":
            continue
        if "".join(content_to_str(event.llm_message.content)) == prompt:
            matching_user_index = index
            break
    if matching_user_index is None:
        return False
    for index in range(matching_user_index + 1, len(events)):
        event = events[index]
        if isinstance(event, (ActionEvent, MessageEvent)) and event.source == "agent":
            return True
    return False


def _configure_openhands_profile_store() -> None:
    """Keep OpenHands' implicit profile lock out of the user's home directory.

    OpenHands' built-in vision/switch tools instantiate ``LLMProfileStore()``
    without a directory argument, which defaults to ``~/.openhands/profiles``.
    On Windows that directory can be owned by another server/elevation context,
    causing every agent to fail before it writes any task output.  A shared
    process-local temporary directory is writable and still allows concurrent
    tasks to coordinate through OpenHands' file lock.
    """
    from openhands.sdk.llm import llm_profile_store

    profile_dir = Path(tempfile.gettempdir()) / f"easydep-openhands-profiles-{os.getpid()}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.chmod(0o700)
    llm_profile_store._DEFAULT_PROFILE_DIR = profile_dir


class EventJournal:
    def __init__(self, path: Path):
        self.path = path
        self.event_count = 0
        self.tool_counts: dict[str, int] = {}
        self.latest_agent_message = ""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def __call__(self, event) -> None:
        event_type = event.__class__.__name__
        tool_name = getattr(event, "tool_name", None)
        # SDK는 한 번의 도구 사용을 ActionEvent와 ObservationEvent 두 개로 남긴다.
        # 사용량에는 실제 요청인 ActionEvent만 세어 화면에 두 배로 보이지 않게 한다.
        if tool_name and event_type == "ActionEvent":
            self.tool_counts[tool_name] = self.tool_counts.get(tool_name, 0) + 1
        event_payload = event.model_dump(mode="json")
        payload = {
            "sequence": self.event_count,
            "timestamp": time.time(),
            "type": event_type,
            "source": getattr(event, "source", None),
            "tool": tool_name,
            "event": event_payload,
        }
        # Workspace 화면에는 숨겨진 reasoning이 아니라 모델이 사용자에게 반환한 마지막
        # assistant 텍스트만 보여 준다. 실행 중 한 번 저장해 두므로 진행 조회 때 큰 journal을
        # 매번 다시 읽지 않아도 된다.
        if event_type == "MessageEvent" and event_payload.get("source") == "agent":
            message = event_payload.get("llm_message")
            content = message.get("content") if isinstance(message, dict) else None
            text_parts = [
                str(item.get("text"))
                for item in content or []
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ]
            if text_parts:
                self.latest_agent_message = "\n".join(text_parts)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.event_count += 1

    def record_direct_source_replacement(self, action: SourceReplaceAction) -> None:
        """Persist action evidence without retaining generated source in the journal."""

        payload = {
            "sequence": self.event_count,
            "timestamp": time.time(),
            "type": "DirectEditorAction",
            "source": "agent",
            "tool": "replace_source",
            "event": {
                "path": action.path,
                "sourceSha256": hashlib.sha256(
                    action.source.encode("utf-8")
                ).hexdigest(),
            },
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.event_count += 1
        self.tool_counts["replace_source"] = self.tool_counts.get("replace_source", 0) + 1
        self.tool_counts["replace_source_applied"] = (
            self.tool_counts.get("replace_source_applied", 0) + 1
        )
        self.latest_agent_message = "Direct editor applied replace_source."

    def record_direct_source_edit(self, action: SourceEditAction, observation: SourceReplaceObservation) -> None:
        """Persist bounded edit evidence without retaining source text."""

        payload = {
            "sequence": self.event_count,
            "timestamp": time.time(),
            "type": "DirectEditorAction",
            "source": "agent",
            "tool": SOURCE_EDIT_TOOL_NAME,
            "event": {
                "path": action.path,
                "editCount": len(action.edits),
                "sourceSha256": observation.source_sha256,
            },
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.event_count += 1
        self.tool_counts[SOURCE_EDIT_TOOL_NAME] = self.tool_counts.get(SOURCE_EDIT_TOOL_NAME, 0) + 1
        self.tool_counts[f"{SOURCE_EDIT_TOOL_NAME}_applied"] = self.tool_counts.get(
            f"{SOURCE_EDIT_TOOL_NAME}_applied", 0
        ) + 1
        self.latest_agent_message = "Direct editor applied edit_source."

    def record_direct_source_rejection(
        self,
        *,
        failure_code: str,
        rejected_path: str | None,
        allowed_paths: list[str],
        source_sha256: str | None,
        tool_name: str = SOURCE_REPLACE_TOOL_NAME,
        field_issues: list[dict[str, str]] | None = None,
        failure_detail: str | None = None,
    ) -> None:
        """Persist bounded argument diagnostics without source bodies or raw responses."""

        payload = {
            "sequence": self.event_count,
            "timestamp": time.time(),
            "type": "DirectEditorActionRejected",
            "source": "agent",
            "tool": tool_name,
            "event": {
                "failureCode": failure_code[:80],
                "rejectedPath": rejected_path[:512] if rejected_path else None,
                "allowedPaths": allowed_paths[:16],
                "sourceSha256": source_sha256,
                "fieldIssues": (field_issues or [])[:16],
                "failureDetail": failure_detail[:320] if failure_detail else None,
            },
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.event_count += 1
        self.tool_counts[tool_name] = self.tool_counts.get(tool_name, 0) + 1
        rejected_key = f"{tool_name}_rejected"
        self.tool_counts[rejected_key] = (
            self.tool_counts.get(rejected_key, 0) + 1
        )

class NoActionResponseGuard:
    """Turn a reasoning-only completion into one explicit action-recovery turn.

    EasyDep observes OpenHands' typed response classification only; it does not inspect
    model text or provider error strings. A response with neither visible content nor a
    tool call has made no task progress, so waiting for an arbitrary repeat count only
    multiplies the same expensive defect. The runtime still grants one bounded recovery
    turn with a concrete edit-or-gap instruction.
    """

    def __init__(self) -> None:
        self.threshold = 1
        self.consecutive_count = 0
        self.max_consecutive_count = 0
        self.triggered = False
        self._conversation: object | None = None

    def bind(self, conversation: object) -> None:
        self._conversation = conversation

    def reset(self) -> None:
        self.consecutive_count = 0
        self.triggered = False

    def __call__(self, event: object) -> None:
        from openhands.sdk.agent.response_dispatch import (
            LLMResponseType,
            classify_response,
        )
        from openhands.sdk.conversation.state import ConversationExecutionStatus
        from openhands.sdk.event import ActionEvent, MessageEvent

        if isinstance(event, ActionEvent) and event.source == "agent":
            self.consecutive_count = 0
            return
        if not isinstance(event, MessageEvent) or event.source != "agent":
            return
        response_type = classify_response(event.llm_message)
        if response_type not in {
            LLMResponseType.EMPTY,
            LLMResponseType.REASONING_ONLY,
        }:
            self.consecutive_count = 0
            return
        self.consecutive_count += 1
        self.max_consecutive_count = max(
            self.max_consecutive_count,
            self.consecutive_count,
        )
        if self.consecutive_count < self.threshold or self._conversation is None:
            return
        self.triggered = True
        self._conversation.state.execution_status = ConversationExecutionStatus.STUCK


class SuccessfulTaskCheckGuard:
    """Stop the owner loop after a passing canonical check.

    ``run_task_check`` records its passing evidence before emitting the
    observation. Marking the conversation stuck at that point routes through
    the existing finish-recovery message instead of allowing post-check
    exploration to consume the remaining owner iterations.
    """

    def __init__(
        self,
        sandbox: Path,
        task_type: str,
        allowed_write_paths: list[str],
        verification_profile: dict[str, object] | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.task_type = task_type
        self.allowed_write_paths = allowed_write_paths
        self.verification_profile = verification_profile
        self.triggered = False
        self._conversation: object | None = None

    def bind(self, conversation: object) -> None:
        self._conversation = conversation

    def __call__(self, event: object) -> None:
        if self.triggered or self._conversation is None:
            return
        if getattr(event, "tool_name", None) != "run_task_check":
            return
        observation = getattr(event, "observation", None)
        if observation is None or getattr(observation, "is_error", True):
            return
        if not has_successful_task_check(
            self.sandbox,
            self.task_type,
            self.allowed_write_paths,
            self.verification_profile,
        ):
            return
        from openhands.sdk.conversation.state import ConversationExecutionStatus

        self.triggered = True
        self._conversation.state.execution_status = ConversationExecutionStatus.STUCK


def _owner_workspace_guidance(
    task_type: str,
    workspace: Path | str,
    owner_roots: list[str],
    owner_tool_mode: str = "terminal",
    *,
    owner_files: list[str] | None = None,
    read_files: list[str] | None = None,
    bounded_evidence: bool = False,
) -> str:
    """Return stable runner facts, not implementation instructions."""

    logical_workspace = Path(workspace)
    common = [
        "## EasyDep implementation workspace",
        "",
        "- Agent: Implementation. Requirements, caller-visible APIs, and observable behavior are admitted constraints for this task; broad validation belongs to the Testing agent.",
        "- Current state: EXECUTE. Implement the admitted product behavior in the declared write scope and choose conventional implementation mechanics where generated design hints are incomplete.",
        (
            f"- Complete workspace: `{logical_workspace}`. For file_editor, use absolute paths rooted at this directory."
            if owner_tool_mode != "editor"
            else f"- Complete workspace: `{logical_workspace}`."
        ),
        (
            '- For an existing file, file_editor uses command="str_replace" with old_str and new_str. For a new file, it uses command="create" with file_text. command="edit" and old_string/new_string are invalid.'
            if owner_tool_mode != "editor"
            else ""
        ),
        "- Preserve generated public declarations: never change or delete an existing public signature. Within the assigned write scope (files or roots), adding only the smallest constructor, accessor, or helper declaration needed is permitted.",
        "- Choose one legal conventional implementation and edit it; do not enumerate alternatives or delay the edit for theoretical choices.",
        (
            "- Use build/test results rather than file counts as completion evidence."
            if owner_tool_mode != "editor"
            else ""
        ),
        (
            "- After an edit batch, run the canonical verification once. If it fails, inspect that output and its existing diagnostic files before rerunning; do not rerun only to obtain more detail."
            if owner_tool_mode != "editor"
            else ""
        ),
        (
            "- When canonical verification passes, call the FinishTool immediately. A plain-text summary does not complete the task. Do not disable tests or alter test reporting to hide a failure."
            if owner_tool_mode != "editor"
            else "- Do not disable tests or alter test reporting to hide a failure."
        ),
        "- Prefer the lowest-cost test level that proves the behavior; avoid restarting a full application context for every assertion.",
        "- Use English for source comments and user-visible text.",
    ]
    if bounded_evidence:
        common.extend(
            [
                "- Requirements, caller-visible APIs, and observable behavior are hard constraints. Preserve generated public signatures and compile boundaries when a legal implementation exists.",
                "- Generated class, sequence, RTM, collaborator, and wiring details are implementation hints; they may be incomplete.",
                "- Read the listed contract or declared dependencies only for that concrete need. Do not reread unchanged files; let canonical verification identify remaining mechanics.",
                "- A missing collaborator or wiring entry alone is not an upstream gap. When declared public behavior and existing dependency APIs are sufficient, choose conventional wiring in the writable component.",
                "- Report an upstream gap only when required public input, output, or externally visible behavior is absent or contradictory, leaving no legal implementation without inventing product meaning.",
            ]
        )
        if task_type not in {"backend-unit-test", "frontend-unit-test"}:
            common.extend(
                [
                    "- Start with one writable source containing an assigned completion marker. Make its first legal edit from local declarations and assigned task behavior. If a concrete implementation need remains, consult only the listed operation contract and declared dependency sources. Interaction hints are behavioral evidence; use them to understand delegated behavior, but do not inject dependencies or alter BCE ownership solely because of a hint.",
                    "- Preserve shared work and every generated body or implementation marker not assigned to this task.",
                ]
            )
    else:
        common.extend(
            [
                "- Start from generated skeletons and their local context; use only the listed operation contract and declared dependency sources when a concrete contract gap remains.",
            ]
        )
    if owner_tool_mode == "editor":
        common.extend(
            [
                "- Use edit_source for small exact changes to existing supplied sources; use replace_source for a new file or broad complete rewrite. No file browser, grep, terminal, or model-run verification tool is available.",
                "- The harness runs the canonical verification after each editor attempt. On a non-infrastructure failure it provides one exact diagnosis for one repair attempt.",
                "- Call FinishTool after a replacement; a plain-text summary does not complete the task.",
            ]
        )
    elif owner_tool_mode == "terminal":
        common.extend(
            [
                "- The terminal session preserves `cd` and environment changes between calls.",
                "- Use `file_editor` for source edits and `terminal` for inspection, search, build, and tests.",
            ]
        )
    else:
        common.extend(
            [
                "- Use `file_editor` for source reads and edits, `grep` for search, and `run_task_check` for verification.",
                "- No terminal is available. Do not invent shell or repository-browser tools.",
            ]
        )
    if task_type == "backend-implementation":
        common.extend(
            [
                "- Backend project root: `application`.",
                "- Gradle is installed as `gradle`; this project has no Gradle wrapper.",
                (
                    f"- Canonical backend verification: `cd {logical_workspace / 'application'} && gradle test --build-cache`."
                    if owner_tool_mode == "terminal"
                    else "- The editor harness runs canonical backend verification outside the conversation."
                    if owner_tool_mode == "editor"
                    else "- Canonical backend verification is the argument-free `run_task_check` tool."
                ),
                (
                    "- The terminal exports `SPRING_PROFILES_ACTIVE=test` and Gradle uses the shared `GRADLE_USER_HOME` cache."
                    if owner_tool_mode == "terminal"
                    else "- The harness uses the configured test profile and shared Gradle cache."
                    if owner_tool_mode == "editor"
                    else "- `run_task_check` uses the configured test profile and shared Gradle cache."
                ),
            ]
        )
    elif task_type == "frontend-implementation":
        common.extend(
            [
                "- Frontend project root: `application/frontend`.",
                "- If dependencies are absent, run `npm ci --ignore-scripts --no-audit --no-fund --prefer-offline` once.",
                (
                    f"- Canonical frontend verification: `cd {logical_workspace / 'application' / 'frontend'} && npm exec -- tsc -b`."
                    if owner_tool_mode == "terminal"
                    else "- Canonical frontend verification is the argument-free `run_task_check` tool."
                ),
                f"- npm uses the shared cache at `{OWNER_NPM_CACHE}`.",
            ]
        )
    elif task_type == "backend-unit-test":
        common.extend(
            [
                "- This task authors one focused JUnit test; the implementation subject is read-only evidence.",
                "- The harness runs the selected Gradle test and requires at least one non-skipped passing JUnit case.",
                "- Do not weaken assertions merely to make a failing implementation pass.",
            ]
        )
    elif task_type == "frontend-unit-test":
        common.extend(
            [
                "- This task authors one focused Vitest file; the implementation subject and generated client are read-only evidence.",
                "- The harness runs only the assigned Vitest path and requires at least one non-skipped passing case.",
                "- Do not weaken assertions merely to make a failing implementation pass.",
            ]
        )
    def logical_owner_path(value: str) -> str:
        path = Path(value)
        return str(path if path.is_absolute() else logical_workspace / path)

    logical_owner_files = [logical_owner_path(value) for value in owner_files or []]
    logical_owner_roots = [logical_owner_path(value) for value in owner_roots]
    common.extend(
        [
            "",
            "Writable task files (authoritative exact-file scope):",
            "- Completion markers identify required bodies; they are not the write-scope definition.",
        ]
    )
    common.extend(f"- `{path}`" for path in logical_owner_files)
    if not owner_files:
        common.append("- none")
    common.extend(["", "Additional writable roots:"])
    common.extend(f"- `{root}`" for root in logical_owner_roots)
    if not owner_roots:
        common.append("- none")
    common.append(
        "- Every path not listed above and not contained by an additional writable root is read-only."
    )
    if bounded_evidence:
        common.extend(
            [
                "",
                "Readable evidence files (authoritative exact-file scope):",
                "- `file_editor` view accepts an exact file path from this list; use `grep` when searching a containing directory.",
            ]
        )
        common.extend(f"- `{path}`" for path in read_files or [])
        if not read_files:
            common.append("- none")
    return "\n".join(common)


def _unit_test_subject_evidence(
    sandbox: Path, verification_profile: dict[str, object] | None
) -> str:
    """Embed the planned implementation subject for a bounded test authoring task."""

    profile = verification_profile or {}
    paths = profile.get("unitTestSubjectPaths", [])
    if not isinstance(paths, list):
        return ""
    root = sandbox.resolve()
    included: list[str] = []
    used = 0
    for value in paths:
        if not isinstance(value, str) or not value:
            continue
        candidate = (sandbox / value).resolve()
        if (
            not candidate.is_relative_to(root)
            or not candidate.is_file()
            or candidate.suffix.casefold() not in DIRECT_EDITOR_SOURCE_EXTENSIONS
        ):
            continue
        body = candidate.read_text(encoding="utf-8")
        size = len(body.encode("utf-8"))
        if used + size > EDITOR_READ_EVIDENCE_MAX_BYTES:
            continue
        fence = DIRECT_EDITOR_CODE_FENCES.get(candidate.suffix.casefold(), "text")
        included.append(
            f"### `{candidate.relative_to(root).as_posix()}` (read-only unit-test subject)\n"
            f"```{fence}\n{body}\n```"
        )
        used += size
    if not included:
        return ""
    return "\n\n## Supplied implementation subject for this unit test\n\n" + "\n\n".join(included)


def write_execution_plan(
    run_root: Path,
    tasks: list[dict[str, object]],
    requested_mode: str,
) -> dict[str, object]:
    connection = openhands_connection()
    compatibility = openhands_compatibility(connection)
    plan = {
        "schemaVersion": "openhands-execution-plan/v1alpha1",
        "mode": requested_mode,
        "runnable": all(
            bool(compatibility[key])
            for key in ("pythonCompatible", "sdkInstalled", "toolsInstalled", "apiKeyConfigured")
        ),
        "compatibility": compatibility,
        "llm": {
            "provider": connection.provider,
            "model": connection.model,
            "baseUrl": connection.base_url,
        },
        "taskOrder": [task["task_id"] for task in tasks],
        "isolation": "copy source-only application to an ASCII temp workspace, edit only assigned implementation paths, run focused checks inside OpenHands, protect generated contracts, promote verified files only",
    }
    target = run_root / "reports" / "agent-execution-plan.json"
    target.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan


def execute_openhands_task(run_root: Path, task_id: str) -> dict[str, object]:
    """Execute one implementation agent task and publish only safe task metrics."""

    app_id = _run_app_id(run_root)
    with langsmith_metrics.trace_scope(
        "easydep.implementation.openhands_task",
        metadata={
            "agent": "implementation",
            "operation": "openhands_task",
            "run_id": run_root.name,
            "task_id": task_id,
            "app_id": app_id,
        },
    ):
        return _execute_openhands_task(run_root, task_id)


def _promote_changed_files(sandbox: Path, run_root: Path, changed: set[str]) -> None:
    """검증된 candidate manifest를 run으로 옮긴다.

    run 폴더는 Windows host와 Linux toolchain 사이의 공유 경로일 수 있다. ``copy2``는
    내용 뒤에 Linux 권한과 시간 정보까지 쓰려 하므로 정상적으로 복사한 뒤에도 EPERM을
    낼 수 있다. 생성 source 계약에는 파일 내용만 필요하므로 metadata를 복사하지 않는다.
    """
    for relative in sorted(changed):
        source = sandbox / relative
        target = run_root / relative
        if not source.is_file():
            # Deletion is part of the verified candidate manifest.  Silently
            # retaining the accepted copy would publish a tree different from
            # the one that passed verification.
            if target.is_file():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _candidate_application_changes(sandbox: Path, run_root: Path) -> set[str]:
    """Return every source add, modification, and deletion in the candidate."""

    return {
        f"application/{path}"
        for path in changed_files(
            snapshot_files(run_root / "application"),
            snapshot_files(sandbox / "application"),
        )
    }


def _persist_admission_gap(
    run_root: Path,
    task: dict[str, object],
    task_id: str,
    gap: UpstreamGap,
    attempt: int,
    started: float,
) -> dict[str, object]:
    """Persist the existing NEEDS_INPUT contract without preparing an agent."""

    execution_dir = run_root / "reports" / "agent-executions"
    execution_dir.mkdir(parents=True, exist_ok=True)
    journal = execution_dir / f"{task_id}.attempt-{attempt:03d}.events.jsonl"
    journal.write_text("", encoding="utf-8")
    result = {
        "taskId": task_id,
        "taskType": str(task.get("task_type") or ""),
        "owner": str(task.get("owner") or ""),
        "promptSha256": task.get("prompt_sha256"),
        "effectiveModel": (
            task.get("llm", {}).get("model")
            if isinstance(task.get("llm"), dict)
            else None
        ),
        "status": "NEEDS_INPUT",
        "upstreamGap": gap.as_result(),
        "candidateEvidence": {"changedFiles": []},
        "durationMs": int((time.monotonic() - started) * 1000),
        "eventCount": 0,
        "toolCounts": {},
        "eventJournal": str(journal.relative_to(run_root)).replace("\\", "/"),
        "terminationReason": "UPSTREAM_GAP",
    }
    write_execution_result(execution_dir, task_id, attempt, result)
    shutil.copyfile(journal, execution_dir / f"{task_id}.events.jsonl")
    return result


def _owned_directory_roots(paths: list[str]) -> list[str]:
    """명시된 wiring 파일과 같은 패키지에는 새 구현 파일을 만들 수 있게 한다.

    전체 ``main/java``가 아니라 이미 작업에 배정된 파일의 바로 위 디렉터리만 연다.
    따라서 OpenHands는 같은 ``config`` 패키지에서 Security 설정을 별도 클래스로 만들지,
    기존 설정 클래스에 합칠지 스스로 고를 수 있다.
    """
    return sorted(
        {
            Path(path.replace("\\", "/")).parent.as_posix()
            for path in paths
            if path.startswith("application/")
        }
    )


def _active_repair_scope(
    task: dict[str, object], active_repair: dict[str, object]
) -> tuple[list[str], list[str], list[str]]:
    """통합 수리에 필요한 정확한 파일만 일시적으로 편집 가능하게 만든다.

    wiring 작업은 평소 업무 코드를 건드리지 못한다. 다만 최종 검사에서 서로 다른 기능의
    파일이 함께 실패하면 수리 계획이 그 파일들을 wiring에 배정할 수 있다. 이때 기존
    ``immutable_paths``를 그대로 적용하면 계획에는 파일이 보이지만 편집 도구가 다시 막는
    모순이 생긴다.

    공개 BCE/API 계약과 결정론적으로 만든 persistence 파일은 계속 보호한다. 단일 기능
    수리는 원래 작업의 관련 파일과 전용 디렉터리를 유지하고, 여러 기능을 잇는 wiring 수리는
    오류에서 확인한 ``repairPaths``만 추가한다.
    """
    base_paths = [str(path).replace("\\", "/") for path in task.get("allowed_write_paths", [])]
    base_roots = [
        str(path).replace("\\", "/")
        for path in task.get("allowed_write_roots", [])
    ]
    immutable = {
        str(path).replace("\\", "/") for path in task.get("immutable_paths", [])
    }
    # Owner repair paths are RTM/failure navigation hints. The backend and
    # frontend owners already have broad source roots, so repair evidence must
    # never grant new write authority or unfreeze a generated contract.
    if _is_owner_task(str(task.get("task_type", ""))):
        return base_paths, base_roots, sorted(immutable)
    requested_paths = [
        str(path).replace("\\", "/")
        for path in active_repair.get("repairPaths", [])
        if isinstance(path, str) and path.startswith("application/")
    ]
    protected_parts = ("/api/", "/persistence/")
    protected_prefixes = ("application/src/main/resources/db/migration/",)
    repair_paths = [
        path
        for path in requested_paths
        if not any(part in "/" + path for part in protected_parts)
        and not path.startswith(protected_prefixes)
        # 기능 작업이 원래 소유한 Entity 본문은 고칠 수 있다. 반면 wiring이 공개 BCE
        # 계약을 새로 소유하게 만들지는 않는다.
        and ("/bce/" not in "/" + path or path in base_paths)
    ]
    # 한 기능이 원래 소유한 관련 파일은 함께 열어 두어 test 실패를 Service나 Entity에서
    # 고칠 수 있게 한다. 여러 기능을 합치는 wiring 수리는 repair plan이 실제 오류 파일만
    # 추가하므로 main/java 전체로 넓어지지 않는다.
    editable = list(dict.fromkeys([*base_paths, *repair_paths]))
    # exact repair 파일 위에 놓인 넓은 ownership 경로만 해제한다. 편집기 자체는 editable
    # 파일 목록을 다시 검사하므로 같은 package의 관련 없는 기존 파일까지 열리지 않는다.
    immutable = {
        path
        for path in immutable
        if not any(_path_is_immutable(repair_path, {path}) for repair_path in repair_paths)
    }
    roots = base_roots
    if str(task.get("task_type")) == "wiring":
        roots = _owned_directory_roots(base_paths)
    return editable, roots, sorted(immutable)


def _task_execution_scope(
    task: dict[str, object], active_repair: dict[str, object] | None
) -> tuple[list[str], list[str], list[str]]:
    """사전 점검과 실제 실행이 함께 사용할 편집 범위를 계산한다."""

    if active_repair is not None:
        return _active_repair_scope(task, active_repair)
    return (
        [str(path).replace("\\", "/") for path in task.get("allowed_write_paths", [])],
        [str(path).replace("\\", "/") for path in task.get("allowed_write_roots", [])],
        sorted(
            str(path).replace("\\", "/")
            for path in task.get("immutable_paths", [])
        ),
    )



@dataclass(frozen=True)
class OwnerAccessContract:
    """Resolve the bounded owner workspace access surface once per task."""

    writable_files: list[str]
    writable_roots: list[str]
    immutable_paths: list[str]
    read_hints: list[str]
    readable_files: list[str] | None

    @classmethod
    def build(
        cls,
        *,
        sandbox: Path,
        run_root: Path,
        task: dict[str, object],
        context: dict[str, object],
        task_type: str,
        editable_paths: list[str],
        editable_roots: list[str],
        immutable: list[str],
        bounded_evidence: bool,
    ) -> OwnerAccessContract:
        sandbox_root = sandbox.resolve()

        def resolve_inside(value: str) -> Path:
            candidate = (sandbox / value).resolve()
            if not candidate.is_relative_to(sandbox_root):
                raise RuntimeError("Task access path escapes the sandbox.")
            return candidate

        writable_files = [str(resolve_inside(path)) for path in editable_paths]
        writable_roots = [str(resolve_inside(root)) for root in editable_roots]
        immutable_paths = [str(resolve_inside(path)) for path in immutable]
        immutable_resolved = {Path(path) for path in immutable_paths}
        if any(
            writable == immutable_path or immutable_path in writable.parents
            for writable in map(Path, writable_files)
            for immutable_path in immutable_resolved
        ):
            raise RuntimeError("Task access contract overlaps writable and immutable files.")
        if not bounded_evidence:
            return cls(
                writable_files=writable_files,
                writable_roots=writable_roots,
                immutable_paths=immutable_paths,
                read_hints=[],
                readable_files=None,
            )

        evidence_paths = (
            integration_evidence_paths(run_root, task, context)
            if task_type == "integration-implementation"
            else context.get("readSourcePaths", [])
        )
        if task_type in {"backend-unit-test", "frontend-unit-test"}:
            profile = task.get("verification_profile")
            subjects = (
                profile.get("unitTestSubjectPaths", [])
                if isinstance(profile, dict)
                else []
            )
            evidence_paths = [
                *(evidence_paths if isinstance(evidence_paths, list) else []),
                *(subjects if isinstance(subjects, list) else []),
            ]
        frozen_readonly = task.get("frozen_readonly_paths", [])
        if isinstance(frozen_readonly, list):
            evidence_paths = [
                *(evidence_paths if isinstance(evidence_paths, list) else []),
                *(value for value in frozen_readonly if isinstance(value, str)),
            ]
        read_hints = [
            str(candidate)
            for value in evidence_paths
            if isinstance(value, str)
            and (candidate := resolve_inside(value)).is_file()
        ]
        context_file = task.get("context_file")
        if not isinstance(context_file, str):
            raise TypeError("Bounded task access requires a context file.")
        context_path = resolve_inside(context_file)
        if not context_path.is_file():
            raise RuntimeError("Bounded task context file is missing.")
        readable_files = sorted(
            {
                str(context_path),
                *read_hints,
                *writable_files,
            }
        )
        if not {Path(path) for path in writable_files}.issubset(
            {Path(path) for path in readable_files}
        ):
            raise RuntimeError("Bounded task writable files must be readable.")
        return cls(
            writable_files=writable_files,
            writable_roots=writable_roots,
            immutable_paths=immutable_paths,
            read_hints=read_hints,
            readable_files=readable_files,
        )

def _complete_verified_task_without_agent(
    run_root: Path,
    task: dict[str, object],
    task_id: str,
    task_type: str,
    required_paths: list[str],
    verification: dict[str, object],
    started: float,
) -> dict[str, object]:
    '''Persist successful canonical verification without creating a conversation.'''

    missing_outputs = missing_required_outputs(run_root, required_paths)
    if missing_outputs:
        raise WorkspaceVerificationError(
            {
                'command': ['required-task-outputs'],
                'exitCode': 1,
                'stdout': '',
                'stderr': 'Missing required outputs: ' + ', '.join(missing_outputs),
                'testResults': '',
            }
        )
    execution_dir = run_root / 'reports' / 'agent-executions'
    attempt = execution_attempt(run_root, task_id)
    journal = EventJournal(
        execution_dir / f'{task_id}.attempt-{attempt:03d}.events.jsonl'
    )
    initial_verification = {'status': 'PASSED', 'evidence': verification}
    result: dict[str, object] = {
        'taskId': task_id,
        'taskType': task_type,
        'owner': str(task.get('owner') or ''),
        'promptSha256': task.get('prompt_sha256'),
        'effectiveModel': None,
        'changedFiles': [],
        'outputFiles': required_paths,
        'verification': verification,
        'tools': [],
        'durationMs': int((time.monotonic() - started) * 1000),
        'eventCount': 0,
        'toolCounts': {},
        'eventJournal': str(journal.path.relative_to(run_root)).replace('\\', '/'),
        'rawResponse': '',
        'conversationId': None,
        'conversationCheckpoint': None,
        'resumedConversation': False,
        'executionStatus': 'finished',
        'terminationReason': None,
        'maxConsecutiveNoActionResponses': 0,
        'stuckRecoveryUsed': False,
        'finishRecoveryUsed': False,
        'harnessErrorCounts': {},
        'harnessProgress': None,
        'workspacePreflight': None,
        'harnessManifest': None,
        'canaryResultId': None,
        'endpointRetries': None,
        'conversationStats': None,
        'completionPath': 'verify-only',
        'agentInvoked': False,
        'initialVerification': initial_verification,
        'status': 'SUCCEEDED',
    }
    write_execution_result(execution_dir, task_id, attempt, result)
    shutil.copyfile(journal.path, execution_dir / f'{task_id}.events.jsonl')
    return result


def _frozen_unit_candidate(
    run_root: Path, task: dict[str, object], repair: dict[str, object] | None
) -> dict[str, str] | None:
    """Validate the one retained unit-test candidate referenced by a repair plan."""

    if repair is None:
        return None
    raw = repair.get("frozenTestCandidate")
    if not isinstance(raw, dict):
        return None
    path = raw.get("path")
    digest = raw.get("sha256")
    source_path = raw.get("sourcePath")
    if not all(isinstance(value, str) and value for value in (path, digest, source_path)):
        return None
    candidate = (run_root / path).resolve()
    reports_root = (run_root / "reports" / "agent-executions").resolve()
    if not candidate.is_relative_to(reports_root) or not candidate.is_file():
        return None
    if hashlib.sha256(candidate.read_bytes()).hexdigest() != digest:
        return None
    if str(task.get("task_type", "")) in {"backend-unit-test", "frontend-unit-test"}:
        allowed = {
            str(value).replace("\\", "/")
            for value in task.get("allowed_write_paths", [])
            if isinstance(value, str)
        }
        if source_path not in allowed:
            return None
    return {"path": path, "sha256": digest, "sourcePath": source_path}


def _preserve_failed_unit_candidate(
    sandbox: Path,
    run_root: Path,
    task: dict[str, object],
    task_id: str,
    evidence: dict[str, object],
) -> dict[str, object] | None:
    """Retain a failing, executed unit test before owner-workspace cleanup.

    This is intentionally limited to an executed assertion failure. Compiler,
    runner, zero-test, and all-skipped failures retain their normal unit-author
    repair behavior instead of being attributed to the implementation subject.
    """

    if not _is_executed_unit_assertion_failure(evidence):
        return None
    results = evidence.get("unitTestResults")
    if not isinstance(results, dict):
        return None
    candidates = [
        str(value).replace("\\", "/")
        for value in task.get("required_test_paths", task.get("requiredTestPaths", []))
        if isinstance(value, str)
    ]
    if len(candidates) != 1:
        return None
    source_path = candidates[0]
    source = (sandbox / source_path).resolve()
    if not source.is_relative_to(sandbox.resolve()) or not source.is_file():
        return None
    target_dir = run_root / "reports" / "agent-executions"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{task_id}.frozen-test{source.suffix}"
    shutil.copyfile(source, target)
    return {
        "path": str(target.relative_to(run_root)).replace("\\", "/"),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "sourcePath": source_path,
    }


def _is_executed_unit_assertion_failure(evidence: dict[str, object]) -> bool:
    results = evidence.get("unitTestResults")
    if not isinstance(results, dict):
        return False
    total, failed, skipped = (
        results.get("total"), results.get("failed"), results.get("skipped", 0)
    )
    return (
        all(isinstance(value, int) for value in (total, failed, skipped))
        and total - skipped > 0
        and failed > 0
    )


def _execute_frozen_unit_recheck(
    run_root: Path,
    task: dict[str, object],
    task_id: str,
    candidate: dict[str, str],
) -> dict[str, object]:
    """Run the existing canonical check with a hash-locked test and no LLM."""

    started = time.monotonic()
    task_type = str(task.get("task_type", ""))
    allowed = [str(value) for value in task.get("allowed_write_paths", [])]
    profile = task.get("verification_profile")
    verification_profile = dict(profile) if isinstance(profile, dict) else None
    sandbox = prepare_agent_workspace(
        run_root,
        task,
        preserve_failed_edits=False,
        persistent=True,
        requires_owner_terminal=False,
    )
    try:
        source = (run_root / candidate["path"]).resolve()
        reports_root = (run_root / "reports" / "agent-executions").resolve()
        target = (sandbox / candidate["sourcePath"]).resolve()
        if (
            not source.is_relative_to(reports_root)
            or not source.is_file()
            or not target.is_relative_to(sandbox.resolve())
            or hashlib.sha256(source.read_bytes()).hexdigest() != candidate["sha256"]
        ):
            raise WorkspaceVerificationError({
                "command": ["frozen-unit-recheck"], "exitCode": 1,
                "stdout": "", "stderr": "Frozen unit-test candidate is missing or changed.",
                "testResults": "",
            })
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        report_path = (
            run_root / "reports" / "agent-executions" / f"{task_id}.vitest.json"
            if task_type == "frontend-unit-test"
            else None
        )
        verification = verify_agent_workspace(
            sandbox, task_type, allowed, verification_profile, report_path
        )
        if hashlib.sha256(target.read_bytes()).hexdigest() != candidate["sha256"]:
            raise WorkspaceVerificationError({
                "command": ["frozen-unit-recheck"], "exitCode": 1,
                "stdout": "", "stderr": "Canonical recheck modified the frozen unit test.",
                "testResults": "",
            })
        _promote_changed_files(sandbox, run_root, {candidate["sourcePath"]})
        attempt = execution_attempt(run_root, task_id)
        execution_dir = run_root / "reports" / "agent-executions"
        result: dict[str, object] = {
            "taskId": task_id, "taskType": task_type,
            "owner": str(task.get("owner") or ""),
            "promptSha256": task.get("prompt_sha256"),
            "effectiveModel": None, "changedFiles": [candidate["sourcePath"]],
            "outputFiles": [candidate["sourcePath"]], "verification": verification,
            "tools": [], "durationMs": int((time.monotonic() - started) * 1000),
            "eventCount": 0, "toolCounts": {}, "rawResponse": "",
            "conversationId": None, "conversationCheckpoint": None,
            "resumedConversation": False, "executionStatus": "finished",
            "completionPath": "frozen-unit-recheck", "agentInvoked": False,
            "frozenRecheckCandidate": candidate, "status": "SUCCEEDED",
        }
        write_execution_result(execution_dir, task_id, attempt, result)
        return result
    except WorkspaceVerificationError as error:
        execution_dir = run_root / "reports" / "agent-executions"
        attempt = execution_attempt(run_root, task_id)
        write_execution_result(execution_dir, task_id, attempt, {
            "taskId": task_id, "taskType": task_type,
            "owner": str(task.get("owner") or ""),
            "promptSha256": task.get("prompt_sha256"),
            "verification": error.evidence, "agentInvoked": False,
            "completionPath": "frozen-unit-recheck",
            "frozenRecheckCandidate": candidate,
            "durationMs": int((time.monotonic() - started) * 1000),
            "status": "FAILED",
        })
        raise
    finally:
        cleanup_agent_workspace(sandbox, run_root=run_root)


def _direct_editor_tool_schema(allowed_paths: list[str]) -> dict[str, object]:
    exact_paths = sorted(set(allowed_paths))
    return {
        "type": "function",
        "function": {
            "name": "replace_source",
            "description": (
                "Replace one complete UTF-8 source body. Set path to exactly one listed "
                "workspace-relative writable path; never redirect to another file. Allowed paths: "
                + ", ".join(exact_paths)
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "source"],
                "properties": {
                    "path": {"type": "string", "enum": exact_paths},
                    "source": {"type": "string", "minLength": 1},
                },
            },
        },
}


def _direct_editor_edit_tool_schema(allowed_paths: list[str]) -> dict[str, object]:
    exact_paths = sorted(set(allowed_paths))
    return {
        "type": "function",
        "function": {
            "name": SOURCE_EDIT_TOOL_NAME,
            "description": (
                "Apply exact unique old_text/new_text edits to one existing supplied source. "
                "All contexts are matched against the original source and validated before one write. "
                "Use replace_source for a new file or broad rewrite. Allowed paths: "
                + ", ".join(exact_paths)
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "edits"],
                "properties": {
                    "path": {"type": "string", "enum": exact_paths},
                    "edits": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 16,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["old_text", "new_text"],
                            "properties": {
                                "old_text": {"type": "string", "minLength": 1, "maxLength": 65536},
                                "new_text": {"type": "string", "maxLength": 65536},
                            },
                        },
                    },
                },
            },
        },
    }


def _direct_editor_reasoning_effort(llm_config: dict[str, object]) -> str:
    value = llm_config.get("reasoningEffort")
    return value if isinstance(value, str) and value in {"none", "low", "high", "max"} else "low"


def _request_direct_editor_action(
    connection: LlmConnection,
    prompt: str,
    llm_config: dict[str, object],
    allowed_paths: list[str],
) -> SourceReplaceAction | SourceEditAction:
    """Request one exact source replacement or bounded exact-context edit."""

    if not connection.api_key:
        raise DirectEditorResponseError("Direct editor API key is not configured.")
    from openai import OpenAI

    raw_max_tokens = llm_config.get("maxOutputTokens", 8192)
    if not isinstance(raw_max_tokens, int) or raw_max_tokens < 1:
        raise TypeError("implementation LLM maxOutputTokens must be a positive integer")
    request: dict[str, object] = {
        "model": connection.model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a source editor. Return exactly one edit_source or replace_source tool call. "
                    "Use edit_source for small exact edits to an existing file; use replace_source "
                    "for a new file or broad rewrite. Do not explain, inspect, or call other tools."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "tools": [
            _direct_editor_tool_schema(allowed_paths),
            _direct_editor_edit_tool_schema(allowed_paths),
        ],
        "tool_choice": "required",
        "temperature": 0,
        "max_completion_tokens": raw_max_tokens,
    }
    if connection.provider == "cloudflare":
        request["reasoning_effort"] = _direct_editor_reasoning_effort(llm_config)
    client = OpenAI(
        api_key=connection.api_key,
        base_url=connection.base_url,
        default_headers=connection.default_headers(),
        timeout=max(1, int(settings.llm_timeout_seconds)),
        max_retries=0,
    )
    response = client.chat.completions.create(**request)
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise DirectEditorResponseError(
            "Direct editor returned no completion choice.",
            failure_code="NO_COMPLETION_CHOICE",
            retryable=True,
        )
    tool_calls = getattr(getattr(choices[0], "message", None), "tool_calls", None) or []
    if len(tool_calls) != 1:
        raise DirectEditorResponseError(
            "Direct editor must return exactly one source edit tool call.",
            failure_code="INVALID_TOOL_CALL_COUNT",
            retryable=True,
        )
    function = getattr(tool_calls[0], "function", None)
    tool_name = getattr(function, "name", None)
    if tool_name not in {SOURCE_REPLACE_TOOL_NAME, SOURCE_EDIT_TOOL_NAME}:
        raise DirectEditorResponseError(
            "Direct editor returned an unexpected tool name.",
            failure_code="UNEXPECTED_TOOL_NAME",
            retryable=True,
        )
    arguments = getattr(function, "arguments", None)
    if not isinstance(arguments, str):
        raise DirectEditorResponseError(
            "Direct editor returned non-text tool arguments.",
            failure_code="MISSING_TOOL_ARGUMENTS",
            retryable=True,
        )
    try:
        value = json.loads(arguments)
    except json.JSONDecodeError as error:
        raise DirectEditorResponseError(
            "Direct editor returned invalid tool JSON.",
            failure_code="INVALID_TOOL_JSON",
            retryable=True,
        ) from error
    action_type = SourceEditAction if tool_name == SOURCE_EDIT_TOOL_NAME else SourceReplaceAction
    try:
        return action_type.model_validate(value)
    except ValidationError as error:
        issues: list[dict[str, str]] = []
        for item in error.errors(include_input=False)[:16]:
            location = item.get("loc", ())
            issues.append(
                {
                    "field": ".".join(str(part)[:80] for part in location)[:160],
                    "type": str(item.get("type", "invalid"))[:80],
                }
            )
        rejected_path = value.get("path") if isinstance(value, dict) else None
        source = value.get("source") if isinstance(value, dict) else None
        raise DirectEditorResponseError(
            "Direct editor tool arguments do not match the selected source tool.",
            failure_code="INVALID_TOOL_ARGUMENTS",
            retryable=True,
            rejected_path=rejected_path if isinstance(rejected_path, str) else None,
            tool_name=tool_name,
            source_sha256=(
                hashlib.sha256(source.encode("utf-8")).hexdigest()
                if isinstance(source, str)
                else None
            ),
            field_issues=issues,
        ) from error


def _apply_direct_editor_action(
    sandbox: Path,
    writable_files: list[str],
    action: SourceReplaceAction | SourceEditAction,
    journal: EventJournal | None = None,
) -> SourceReplaceObservation:
    allowed_paths = source_replace_allowed_paths(sandbox, writable_files)
    tool_name = SOURCE_EDIT_TOOL_NAME if isinstance(action, SourceEditAction) else SOURCE_REPLACE_TOOL_NAME
    if action.path not in allowed_paths:
        observation = SourceReplaceObservation.from_text(
            text="WRITE_OUTSIDE_OWNER_SCOPE: path must exactly match one supplied workspace-relative path.",
            is_error=True,
            failure_code="INVALID_PATH_ARGUMENT",
            rejected_path=action.path[:512],
            allowed_paths=allowed_paths,
            source_sha256=(
                hashlib.sha256(action.source.encode("utf-8")).hexdigest()
                if isinstance(action, SourceReplaceAction)
                else None
            ),
        )
    elif isinstance(action, SourceReplaceAction) and len(action.source.encode("utf-8")) > EDITOR_WRITABLE_SOURCE_MAX_BYTES:
        observation = SourceReplaceObservation.from_text(
            text="Replacement source exceeds the 64 KiB direct-editor limit.",
            is_error=True,
            failure_code="SOURCE_TOO_LARGE",
            rejected_path=action.path[:512],
            allowed_paths=source_replace_allowed_paths(sandbox, writable_files),
            source_sha256=hashlib.sha256(action.source.encode("utf-8")).hexdigest(),
        )
    else:
        executor = (
            SourceEditExecutor(sandbox, writable_files)
            if isinstance(action, SourceEditAction)
            else SourceReplaceExecutor(sandbox, writable_files)
        )
        observation = executor(action)
    if journal is not None:
        if observation.is_error:
            journal.record_direct_source_rejection(
                failure_code=observation.failure_code or "SOURCE_WRITE_FAILED",
                rejected_path=observation.rejected_path,
                allowed_paths=observation.allowed_paths,
                source_sha256=observation.source_sha256,
                tool_name=tool_name,
                failure_detail=observation.failure_detail,
            )
        elif isinstance(action, SourceEditAction):
            journal.record_direct_source_edit(action, observation)
        else:
            journal.record_direct_source_replacement(action)
    return observation


def _execute_openhands_task(run_root: Path, task_id: str) -> dict[str, object]:
    """Run one OpenHands conversation and keep EasyDep at the safety boundary."""

    task = load_task(run_root, task_id)
    if task.get("task_type") == "integration-implementation":
        manifest = json.loads(
            (run_root / "reports" / "run-manifest.json").read_text(encoding="utf-8")
        )
        manifest_tasks = [
            item
            for item in manifest.get("implementation_tasks", [])
            if isinstance(item, dict)
        ]
        task = {
            **task,
            "prompt_sha256": effective_task_prompt_sha256(
                task, manifest_tasks, run_root
            ),
        }
    task_type = str(task.get("task_type", ""))
    owner_task = _is_owner_task(task_type)
    harness_task = _is_harness_task(task_type)
    if harness_task and os.environ.get("EASYDEP_FIXED_LINUX_RUNNER") != "1":
        raise RuntimeError(
            "Harnessed OpenHands tasks require the isolated EasyDep Linux runner."
        )
    frozen_recheck = repair_recheck_for_task(run_root, task_id)
    frozen_candidate = _frozen_unit_candidate(run_root, task, frozen_recheck)
    if frozen_recheck is not None:
        if frozen_candidate is None:
            raise WorkspaceVerificationError({
                "command": ["frozen-unit-recheck"], "exitCode": 1,
                "stdout": "", "stderr": "Frozen unit-test candidate is unavailable.",
                "testResults": "",
            })
        return _execute_frozen_unit_recheck(
            run_root, task, task_id, frozen_candidate
        )
    active_repair = active_repair_for_task(run_root, task_id)
    editable_paths, editable_roots, immutable = _task_execution_scope(task, active_repair)
    frozen_repair_candidate = _frozen_unit_candidate(run_root, task, active_repair)
    active_review = (
        active_repair.get("unitFailureReview")
        if isinstance(active_repair, dict)
        else None
    )
    editable_frozen_test = (
        isinstance(active_review, dict)
        and active_review.get("classification") == "test_oracle"
    )
    frozen_readonly_paths: list[str] = []
    if frozen_repair_candidate is not None and not editable_frozen_test:
        frozen_readonly_paths = [frozen_repair_candidate["sourcePath"]]
        immutable = sorted({*immutable, *frozen_readonly_paths})
    required_paths = [str(path) for path in task.get("required_output_paths", editable_paths)]
    task = {
        **task,
        "allowed_write_paths": editable_paths,
        "allowed_write_roots": editable_roots,
        "immutable_paths": immutable,
        "frozen_readonly_paths": frozen_readonly_paths,
    }
    owner_tool_mode = (
        str(task.get("owner_tool_mode") or settings.implementation_owner_tool_mode)
        if owner_task
        else "restricted"
    )
    if owner_tool_mode == "editor" and not _direct_editor_source_is_eligible(
        task, task_type, editable_paths, run_root
    ):
        owner_tool_mode = "restricted"
    context = json.loads((run_root / task["context_file"]).read_text(encoding="utf-8"))
    initial_verification: dict[str, object] | None = None
    completion_path = 'agent'
    precheck_sandbox: Path | None = None
    bounded_evidence = task_type in {
        "backend-implementation",
        "integration-implementation",
        "backend-unit-test",
        "frontend-unit-test",
    }
    if bounded_evidence and task_type not in {
        "backend-implementation",
        "backend-unit-test",
        "frontend-unit-test",
    }:
        owner_tool_mode = "restricted"
    if (
        task.get('completion_mode', 'agent') == 'verify-or-repair'
        and task_type == 'integration-implementation'
        and not demo_skip_validation_enabled()
    ):
        precheck_started = time.monotonic()
        raw_profile = task.get('verification_profile')
        precheck_profile = (
            dict(raw_profile)
            if isinstance(raw_profile, dict) and raw_profile
            else None
        )
        precheck_sandbox = prepare_agent_workspace(
            run_root,
            task,
            preserve_failed_edits=True,
            persistent=True,
            requires_owner_terminal=owner_tool_mode == 'terminal',
        )
        precheck = TaskCheckSession(
            precheck_sandbox,
            task_type,
            editable_paths,
            precheck_profile,
        )
        passed, diagnosis = precheck.run()
        precheck_evidence = precheck.last_evidence
        if passed:
            verification = consume_successful_task_check(
                precheck_sandbox,
                task_type,
                editable_paths,
                precheck_profile,
            )
            if verification is not None:
                cleanup_agent_workspace(precheck_sandbox, run_root=run_root)
                return _complete_verified_task_without_agent(
                    run_root,
                    task,
                    task_id,
                    task_type,
                    required_paths,
                    verification,
                    precheck_started,
                )
            diagnosis = 'TASK CHECK PASSED BUT ITS EVIDENCE COULD NOT BE REUSED'
        startup_evidence = (
            precheck_evidence.get("applicationStartup")
            if isinstance(precheck_evidence, dict)
            else None
        )
        typed_startup_infrastructure_failure = (
            isinstance(startup_evidence, dict)
            and startup_evidence.get("defectClass") == "ENVIRONMENT_DEFECT"
        )
        if (
            is_infrastructure_task_check_failure(diagnosis)
            or typed_startup_infrastructure_failure
        ):
            cleanup_agent_workspace(precheck_sandbox, run_root=run_root)
            raise OwnerConversationIncomplete(
                precheck_evidence
                if isinstance(precheck_evidence, dict)
                else {
                    'command': ['run_task_check'],
                    'exitCode': 1,
                    'stdout': '',
                    'stderr': diagnosis,
                    'testResults': '',
                }
            )
        if isinstance(precheck_evidence, dict):
            attributed = _integration_source_owner_attribution(
                run_root, task_id, precheck_evidence, diagnosis
            )
            if attributed is not None:
                precheck_evidence["repairTaskId"] = attributed["taskId"]
                precheck_evidence["attributedTargetFile"] = attributed["targetFile"]
                cleanup_agent_workspace(precheck_sandbox, run_root=run_root)
                raise WorkspaceVerificationError(precheck_evidence)
        initial_verification = {'status': 'FAILED', 'diagnosis': diagnosis}
        completion_path = 'repair-agent'
    editor_mode = owner_task and owner_tool_mode == "editor"
    connection = openhands_connection()
    if not editor_mode:
        compatibility = openhands_compatibility(connection)
        missing = [
            key
            for key in ("pythonCompatible", "sdkInstalled", "toolsInstalled", "apiKeyConfigured")
            if not compatibility[key]
        ]
        if missing:
            raise RuntimeError("OpenHands live mode prerequisites are missing: " + ", ".join(missing))

    requires_owner_terminal = owner_task and owner_tool_mode == "terminal"
    # A normal owner sequence shares one run-local candidate.  The integration
    # precheck is deliberately excluded: its failed candidate is diagnostic
    # input to that task only and must not become the next owner's workspace.
    # Frozen rechecks return above through their own hash-locked workspace.
    shared_owner_workspace = owner_task and precheck_sandbox is None
    sandbox = precheck_sandbox or prepare_agent_workspace(
        run_root,
        task,
        preserve_failed_edits=True,
        persistent=owner_task,
        shared_owner_workspace=shared_owner_workspace,
        requires_owner_terminal=requires_owner_terminal,
    )
    if frozen_repair_candidate is not None:
        frozen_source = run_root / frozen_repair_candidate["path"]
        frozen_target = sandbox / frozen_repair_candidate["sourcePath"]
        frozen_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(frozen_source, frozen_target)
    logical_workspace = (
        prepare_owner_workspace_alias(sandbox, task_id) if owner_task else sandbox
    )
    before = snapshot_files(sandbox)
    prompt_file = task.get("repair_prompt_file") if active_repair is not None else task.get("prompt_file")
    if not isinstance(prompt_file, str) or not (run_root / prompt_file).is_file():
        prompt_file = str(task["prompt_file"])
    prompt = (run_root / prompt_file).read_text(encoding="utf-8")
    if owner_task:
        prompt += (
            "\n\n## First action\n\n"
            + (
                OWNER_EDITOR_INITIAL_ACTION_MESSAGE
                if editor_mode
                else OWNER_INITIAL_ACTION_MESSAGE
            )
            + "\n\n## Evidence boundary\n\n"
            + _owner_evidence_boundary_message(
                task.get("required_test_paths", task.get("requiredTestPaths", [])), task_type
            )
        )
    if initial_verification is not None:
        prompt += (
            '\n\n## Initial canonical verification\n\n'
            + str(initial_verification['diagnosis'])
            + (
                '\nRepair only the reported failure; the editor harness runs the check.\n'
                if editor_mode
                else '\nRepair only the reported failure, then run run_task_check once.\n'
            )
        )
    upstream_gap_source_refs = (
        [
            value
            for value in task.get("source_refs", task.get("sourceRefs", []))
            if isinstance(value, str) and value
        ]
        if (
            owner_task
            and bounded_evidence
            and owner_tool_mode in {"restricted", "editor"}
            and not demo_skip_validation_enabled()
        )
        else None
    )
    verification_profile = task.get("verification_profile")
    verification_profile = (
        dict(verification_profile)
        if isinstance(verification_profile, dict) and verification_profile
        else None
    )
    if task_type in {"backend-unit-test", "frontend-unit-test"}:
        prompt += _unit_test_subject_evidence(sandbox, verification_profile)
    access_contract = OwnerAccessContract.build(
        sandbox=sandbox,
        run_root=run_root,
        task=task,
        context=context,
        task_type=task_type,
        editable_paths=editable_paths,
        editable_roots=editable_roots,
        immutable=immutable,
        bounded_evidence=bounded_evidence,
    )
    writable_files = access_contract.writable_files
    writable_roots = access_contract.writable_roots
    immutable_absolute = access_contract.immutable_paths
    read_hints = access_contract.read_hints
    readable_files = access_contract.readable_files
    if editor_mode:
        prompt += _editor_read_source_evidence(sandbox, context, writable_files)
    owner_system_context = ""
    if harness_task:
        owner_system_context = _owner_workspace_guidance(
            task_type,
            logical_workspace,
            editable_roots,
            owner_tool_mode,
            owner_files=editable_paths,
            read_files=read_hints,
            bounded_evidence=bounded_evidence,
        )
    else:
        prompt += (
            "\n\n## EasyDep task constraints\n\n"
            "Use the provided source locations as investigation hints, not edit limits. "
            "Keep generated contracts unchanged. Work only inside the sandbox and writable roots. "
            "Run run_task_check until it passes before finish. Use English for source comments "
            "and user-visible text.\n\nWritable task files:\n"
            + ("\n".join(f"- `{path}`" for path in writable_files) or "- none")
            + "\n\nAdditional writable roots:\n"
            + ("\n".join(f"- `{path}`" for path in writable_roots) or "- none")
        )
    if immutable_absolute and not owner_task:
        prompt += (
            "\n\nGenerated contracts are readable but write-protected by the sandbox. "
            "Inspect them on demand instead of copying their contents into the conversation."
        )
    if read_hints and not owner_task:
        prompt += "\n\nSuggested source hints:\n" + "\n".join(
            f"- `{path}`" for path in read_hints
        )

    execution_dir = run_root / "reports" / "agent-executions"
    attempt = execution_attempt(run_root, task_id)
    journal = EventJournal(execution_dir / f"{task_id}.attempt-{attempt:03d}.events.jsonl")
    no_action_guard = NoActionResponseGuard() if harness_task else None
    successful_task_check_guard = (
        SuccessfulTaskCheckGuard(
            sandbox,
            task_type,
            editable_paths,
            verification_profile,
        )
        if owner_task and not editor_mode
        else None
    )
    harness_guard = HarnessErrorGuard() if harness_task else None
    progress_tracker = HarnessProgressTracker(sandbox) if harness_task and not editor_mode else None
    stuck_recovery_used = False
    finish_recovery_used = False
    started = time.monotonic()
    conversation = None
    agent = None
    persistence_dir: Path | None = None
    conversation_id: uuid.UUID | None = None
    resumed_conversation = False
    workspace_preflight: dict[str, object] | None = None
    harness_manifest: dict[str, object] | None = None
    canary_result: dict[str, object] | None = None
    endpoint_retry_recorder = EndpointRetryRecorder() if harness_task else None
    if owner_task:
        persistence_dir, conversation_id = _owner_conversation_identity(run_root, task_id)
        resumed_conversation = (
            persistence_dir / conversation_id.hex / "base_state.json"
        ).is_file()
    try:
        reasoning_effort = os.environ.get(
            "OPENHANDS_REASONING_EFFORT",
            str(task["llm"].get("reasoningEffort", settings.implementation_reasoning_effort)),
        )
        if editor_mode:
            reasoning_effort = _direct_editor_reasoning_effort(task["llm"])
        if harness_task:
            workspace_preflight = preflight_owner_workspace(
                sandbox,
                editable_files=writable_files,
                editable_roots=writable_roots,
                immutable_paths=immutable_absolute,
                logical_workspace=logical_workspace,
                enforce_write_scope=owner_tool_mode != "terminal",
                requires_owner_terminal=requires_owner_terminal,
            )
            expected_canary_id = (
                model_tool_canary_id(
                    connection,
                    owner_tool_mode=owner_tool_mode,
                    reasoning_effort=reasoning_effort,
                )
                if settings.implementation_openhands_canary
                and not editor_mode
                and isinstance(connection, LlmConnection)
                else None
            )
            harness_manifest = build_harness_manifest(
                connection,
                owner_tool_mode=owner_tool_mode,
                reasoning_effort=reasoning_effort,
                canary_result_id=expected_canary_id,
                include_upstream_gap=upstream_gap_source_refs is not None,
            )
            verify_or_store_harness_manifest(
                run_root / "reports" / "openhands-harness" / f"{task_id}.manifest.json",
                harness_manifest,
            )
        conversation, agent = create_openhands_conversation(
            sandbox,
            connection,
            task["llm"],
            task_type=task_type,
            verification_paths=editable_paths,
            verification_profile=verification_profile,
            frontend_unit_report_path=(
                run_root / "reports" / "agent-executions" / f"{task_id}.vitest.json"
                if task_type == "frontend-unit-test"
                else None
            ),
            editable_files=writable_files,
            editable_roots=writable_roots,
            readable_files=readable_files,
            immutable_paths=immutable_absolute,
            callbacks=[
                journal,
                *([no_action_guard] if no_action_guard else []),
                *(
                    [successful_task_check_guard]
                    if successful_task_check_guard
                    else []
                ),
                *([harness_guard] if harness_guard else []),
                *([progress_tracker] if progress_tracker else []),
            ],
            retry_listener=endpoint_retry_recorder,
            max_iterations=(
                4
                if editor_mode
                else MAX_AGENT_TURN_ITERATIONS
                if owner_task and bounded_evidence
                else OWNER_TURN_ITERATIONS
                if owner_task
                else MAX_AGENT_TURN_ITERATIONS
            ),
            reasoning_effort=reasoning_effort,
            native_owner_tools=harness_task,
            enable_native_terminal=harness_task and owner_tool_mode == "terminal",
            owner_tool_mode=owner_tool_mode,
            upstream_gap_source_refs=upstream_gap_source_refs,
            workspace=logical_workspace,
            owner_system_context=owner_system_context,
            persistence_dir=persistence_dir,
            conversation_id=conversation_id,
        )
        if no_action_guard is not None:
            no_action_guard.bind(conversation)
        if successful_task_check_guard is not None:
            successful_task_check_guard.bind(conversation)
        if harness_guard is not None:
            harness_guard.bind(conversation)
        if progress_tracker is not None:
            progress_tracker.bind(conversation)
        # The task conversation construction above instantiates and serializes
        # every real executor without calling its LLM. Only after that
        # deterministic check may the live protocol canary use the endpoint.
        if (
            harness_task
            and not editor_mode
            and settings.implementation_openhands_canary
            and isinstance(
            connection, LlmConnection
            )
        ):
            canary_result = ensure_model_tool_canary(
                run_root,
                connection,
                task["llm"],
                owner_tool_mode=owner_tool_mode,
                reasoning_effort=reasoning_effort,
                repetitions=settings.implementation_openhands_canary_repetitions,
                max_attempts=settings.implementation_openhands_canary_max_attempts,
                transient_failure_ttl_seconds=(
                    settings.implementation_openhands_canary_transient_ttl_seconds
                ),
                retry_min_wait_seconds=(
                    settings.implementation_openhands_retry_min_wait_seconds
                ),
                retry_max_wait_seconds=(
                    settings.implementation_openhands_retry_max_wait_seconds
                ),
                retry_multiplier=settings.implementation_openhands_retry_multiplier,
            )
            if (
                harness_manifest is None
                or canary_result["canaryResultId"]
                != harness_manifest["canaryResultId"]
            ):
                raise HarnessCompatibilityError(
                    "MODEL_TOOL_PROTOCOL_INCOMPATIBLE: canary contract ID changed"
                )
        # The SDK loads persisted events when the stable conversation exists.
        # Compare the exact user message in that public event history: a new
        # repair is appended, while a crash after event persistence resumes
        # without duplicating the potentially large task message.
        message_required = _owner_message_required(
            resumed=resumed_conversation,
            conversation=conversation,
            prompt=prompt,
        )
        if message_required:
            conversation.send_message(prompt)
        elif _owner_continuation_required(
            resumed=resumed_conversation,
            conversation=conversation,
            prompt=prompt,
        ):
            conversation.send_message(OWNER_CONTINUATION_MESSAGE)
        run_openhands_conversation(conversation)
        if (
            editor_mode
            and _conversation_is_stuck(conversation)
            and reported_upstream_gap(agent) is None
            and not any(
                path in editable_paths
                for path in changed_files(before, snapshot_files(sandbox))
            )
        ):
            if no_action_guard is not None:
                no_action_guard.reset()
            stuck_recovery_used = True
            conversation.send_message(EDITOR_STUCK_RECOVERY_MESSAGE)
            run_openhands_conversation(conversation)
        if editor_mode and reported_upstream_gap(agent) is None:
            first_changes = changed_files(before, snapshot_files(sandbox))
            # A persistent owner sandbox may already contain an unpromoted,
            # canonical-different candidate from its prior conversation.  A
            # repeated replace_source body then has no *attempt* delta, but it
            # still needs the normal verifier and promotion-boundary decision.
            candidate_changes = _candidate_application_changes(sandbox, run_root)
            if (
                not any(path in editable_paths for path in first_changes)
                and not candidate_changes
            ):
                raise OwnerConversationIncomplete(
                    {
                        "command": ["replace_source"],
                        "exitCode": 1,
                        "stdout": "",
                        "stderr": "Editor conversation made no source change.",
                        "testResults": "",
                    }
                )
            editor_check = TaskCheckSession(
                sandbox,
                task_type,
                editable_paths,
                verification_profile,
                (
                    run_root / "reports" / "agent-executions" / f"{task_id}.vitest.json"
                    if task_type == "frontend-unit-test"
                    else None
                ),
            )
            passed, diagnosis = editor_check.run()
            if not passed:
                if (
                    task_type in {"backend-unit-test", "frontend-unit-test"}
                    and isinstance(editor_check.last_evidence, dict)
                    and _is_executed_unit_assertion_failure(editor_check.last_evidence)
                ):
                    # The test is now a frozen oracle. Do not send its failure
                    # back to its test-writing editor, which could weaken it.
                    raise WorkspaceVerificationError(editor_check.last_evidence)
                if is_infrastructure_task_check_failure(diagnosis):
                    raise OwnerConversationIncomplete(
                        {
                            "command": ["run_task_check"],
                            "exitCode": 1,
                            "stdout": "",
                            "stderr": diagnosis,
                            "testResults": "",
                        }
                    )
                if stuck_recovery_used:
                    raise WorkspaceVerificationError(
                        {
                            "command": ["run_task_check"],
                            "exitCode": 1,
                            "stdout": "",
                            "stderr": diagnosis,
                            "testResults": "",
                        }
                    )
                repair_evidence = _editor_read_source_evidence(
                    sandbox, context, writable_files
                )
                repair_before = snapshot_files(sandbox)
                from openhands.sdk.conversation.state import ConversationExecutionStatus

                conversation.state.execution_status = ConversationExecutionStatus.IDLE
                conversation.send_message(
                    EDITOR_REPAIR_MESSAGE
                    + repair_evidence
                    + "\n\n## Exact diagnosis\n\n"
                    + diagnosis
                )
                run_openhands_conversation(conversation)
                repair_changes = changed_files(repair_before, snapshot_files(sandbox))
                if not any(path in editable_paths for path in repair_changes):
                    raise OwnerConversationIncomplete(
                        {
                            "command": ["replace_source"],
                            "exitCode": 1,
                            "stdout": "",
                            "stderr": "Editor repair made no source change.",
                            "testResults": "",
                            "initialTaskCheckDiagnosis": diagnosis,
                            "initialTaskCheckEvidence": editor_check.last_evidence,
                        }
                    )
                if reported_upstream_gap(agent) is None:
                    passed, diagnosis = run_task_check(
                        sandbox,
                        task_type,
                        editable_paths,
                        verification_profile,
                        frontend_unit_report_path=(
                            run_root / "reports" / "agent-executions" / f"{task_id}.vitest.json"
                            if task_type == "frontend-unit-test"
                            else None
                        ),
                    )
                    if not passed:
                        if is_infrastructure_task_check_failure(diagnosis):
                            raise OwnerConversationIncomplete(
                                {
                                    "command": ["run_task_check"],
                                    "exitCode": 1,
                                    "stdout": "",
                                    "stderr": diagnosis,
                                    "testResults": "",
                                }
                            )
                        raise WorkspaceVerificationError(
                            {
                                "command": ["run_task_check"],
                                "exitCode": 1,
                                "stdout": "",
                                "stderr": diagnosis,
                                "testResults": "",
                            }
                        )
        if owner_task and not editor_mode and _conversation_is_stuck(conversation):
            if no_action_guard is not None:
                no_action_guard.reset()
            successful_task_check = (
                harness_task
                and has_successful_task_check(
                    sandbox,
                    task_type,
                    editable_paths,
                    verification_profile,
                )
            )
            if not successful_task_check:
                stuck_recovery_used = True
                conversation.send_message(
                    OWNER_GAP_RECOVERY_MESSAGE
                    if upstream_gap_source_refs is not None
                    else OWNER_STUCK_RECOVERY_MESSAGE
                )
                run_openhands_conversation(conversation)
        upstream_gap = (
            reported_upstream_gap(agent) if upstream_gap_source_refs is not None else None
        )
        if upstream_gap is not None:
            candidate_changes = _candidate_application_changes(sandbox, run_root)
            result = {
                "taskId": task_id,
                "taskType": task_type,
                "owner": str(task.get("owner") or ""),
                "promptSha256": task.get("prompt_sha256"),
                "effectiveModel": connection.litellm_model(),
                "status": "NEEDS_INPUT",
                "upstreamGap": upstream_gap.as_result(),
                "candidateEvidence": {"changedFiles": sorted(candidate_changes)},
                "durationMs": int((time.monotonic() - started) * 1000),
                "eventCount": journal.event_count,
                "toolCounts": journal.tool_counts,
                "eventJournal": str(journal.path.relative_to(run_root)).replace("\\", "/"),
                "rawResponse": journal.latest_agent_message,
                "conversationId": str(conversation_id) if conversation_id else None,
                "conversationCheckpoint": (
                    str(persistence_dir.relative_to(run_root)).replace("\\", "/")
                    if persistence_dir is not None
                    else None
                ),
                "resumedConversation": resumed_conversation,
                "executionStatus": _conversation_execution_status(conversation),
                "terminationReason": "UPSTREAM_GAP",
                "workspacePreflight": workspace_preflight,
                "harnessManifest": harness_manifest,
                "conversationStats": _conversation_stats_snapshot(conversation),
            }
            conversation.close()
            write_execution_result(execution_dir, task_id, attempt, result)
            shutil.copyfile(journal.path, execution_dir / f"{task_id}.events.jsonl")
            return result
        if (
            harness_task
            and not finish_recovery_used
            and _conversation_needs_finish_recovery(conversation)
            and has_successful_task_check(
                sandbox,
                task_type,
                editable_paths,
                verification_profile,
            )
        ):
            # OpenHands can stop after a prose response even when FinishTool is
            # available. Give that state one narrow completion-only recovery.
            finish_recovery_used = True
            if no_action_guard is not None:
                no_action_guard.reset()
            conversation.send_message(OWNER_FINISH_RECOVERY_MESSAGE)
            run_openhands_conversation(conversation)
        successful_task_check = (
            (editor_mode or harness_task)
            and has_successful_task_check(
                sandbox,
                task_type,
                editable_paths,
                verification_profile,
            )
        )
        if _conversation_terminal_failure(conversation) and not successful_task_check:
            raise OwnerConversationIncomplete(
                {
                    "command": ["openhands", "conversation"],
                    "exitCode": 1,
                    "stdout": "",
                    "stderr": journal.latest_agent_message or "OpenHands conversation did not finish.",
                    "testResults": "",
                }
            )
        missing_outputs = missing_required_outputs(sandbox, required_paths)
        if missing_outputs:
            raise WorkspaceVerificationError(
                {
                    "command": ["required-task-outputs"],
                    "exitCode": 1,
                    "stdout": "",
                    "stderr": "Missing required outputs: " + ", ".join(missing_outputs),
                    "testResults": "",
                }
            )
        attempt_changes = changed_files(before, snapshot_files(sandbox))
        candidate_changes = _candidate_application_changes(sandbox, run_root)
        unauthorized = sorted(
            {
                path
                for path in candidate_changes
                if not path_is_editable(path, editable_paths, editable_roots, immutable)
            }
            | {
                path
                for path in attempt_changes
                if not path.startswith("application/")
            }
        )
        if unauthorized:
            raise WorkspaceVerificationError(
                {
                    "command": ["implementation-promotion-boundary"],
                    "exitCode": 1,
                    "stdout": "",
                    "stderr": "Candidate changes outside the owner's promotion boundary: "
                    + ", ".join(unauthorized),
                    "testResults": "",
                    "unauthorizedChanges": unauthorized,
                }
            )
        # Restricted owners run the same deterministic check through
        # ``run_task_check``. Reuse that evidence after FinishTool so the outer
        # coordinator does not execute compileJava a second time. Terminal
        # owners have no cached check, so the fallback remains authoritative.
        verification = consume_successful_task_check(
            sandbox, task_type, editable_paths, verification_profile
        ) or verify_agent_workspace(sandbox, task_type, editable_paths, verification_profile)
    except Exception as error:
        if conversation is not None:
            conversation.close()
        provider_failure_reason = classify_canary_exception(error)
        if (
            harness_task
            and isinstance(connection, LlmConnection)
            and provider_failure_reason in TRANSIENT_CANARY_FAILURES
        ):
            open_endpoint_circuit(
                run_root,
                connection,
                owner_tool_mode=owner_tool_mode,
                reasoning_effort=reasoning_effort,
                ttl_seconds=(
                    settings.implementation_openhands_canary_transient_ttl_seconds
                ),
                reason=provider_failure_reason,
            )
        classified_error = classify_harness_error_text(str(error))
        compatibility_prefix = str(error).partition(":")[0]
        compatibility_reasons = {
            "MODEL_NOT_FOUND",
            "MODEL_TOOL_PROTOCOL_INCOMPATIBLE",
            "TOOL_PROTOCOL_TOKEN_LEAK",
            "TOOL_NOT_AVAILABLE",
            "TOOL_SCHEMA_INVALID",
            "PROVIDER_RATE_LIMIT",
            "PROVIDER_TIMEOUT",
            "PROVIDER_STREAM_INCOMPLETE",
            "NETWORK_CONNECTION_ERROR",
            "CANARY_EXECUTION_ERROR",
            "ENDPOINT_DEGRADED",
            "PROVIDER_OUTPUT_PARSE_TRANSIENT",
        }
        deterministic_termination = (
            harness_guard.terminal_code
            if harness_guard is not None and harness_guard.terminal_code
            else (
                classified_error.code
                if classified_error is not None
                else (
                    compatibility_prefix
                    if isinstance(error, HarnessCompatibilityError)
                    and compatibility_prefix in compatibility_reasons
                    else (
                        "HARNESS_CONTRACT_INCOMPATIBLE"
                        if isinstance(error, HarnessCompatibilityError)
                        else None
                    )
                )
            )
        )
        if (
            deterministic_termination is None
            and provider_failure_reason in TRANSIENT_CANARY_FAILURES
        ):
            deterministic_termination = provider_failure_reason
        interrupted = owner_task and (
            isinstance(error, OwnerConversationIncomplete)
            or (
                isinstance(error, WorkspaceVerificationError)
                and _is_infrastructure_verification_failure(error)
            )
            or (
                not isinstance(error, WorkspaceVerificationError)
                and deterministic_termination
                in {*TRANSIENT_CANARY_FAILURES, "ENDPOINT_DEGRADED"}
            )
        )
        failure = {
            "taskId": task_id,
            "taskType": task_type,
            "owner": str(task.get("owner") or ""),
            "promptSha256": task.get("prompt_sha256"),
            "status": "INTERRUPTED" if interrupted else "FAILED",
            "completionPath": completion_path,
            "initialVerification": initial_verification,
            "effectiveModel": connection.litellm_model(),
            "errorType": error.__class__.__name__,
            "error": str(error),
            "durationMs": int((time.monotonic() - started) * 1000),
            "eventCount": journal.event_count,
            "toolCounts": journal.tool_counts,
            "eventJournal": str(journal.path.relative_to(run_root)).replace("\\", "/"),
            "rawResponse": journal.latest_agent_message,
            "conversationId": str(conversation_id) if conversation_id else None,
            "conversationCheckpoint": (
                str(persistence_dir.relative_to(run_root)).replace("\\", "/")
                if persistence_dir is not None
                else None
            ),
            "resumedConversation": resumed_conversation,
            "executionStatus": _conversation_execution_status(conversation),
            "terminationReason": (
                deterministic_termination
                if deterministic_termination is not None
                else (
                    progress_tracker.terminal_code
                    if progress_tracker is not None and progress_tracker.terminal_code
                    else (
                        "consecutive_no_action_responses"
                        if no_action_guard is not None and no_action_guard.triggered
                        else None
                    )
                )
            ),
            "maxConsecutiveNoActionResponses": (
                no_action_guard.max_consecutive_count
                if no_action_guard is not None
                else 0
            ),
            "stuckRecoveryUsed": stuck_recovery_used,
            "finishRecoveryUsed": finish_recovery_used,
            "harnessErrorCounts": harness_guard.counts if harness_guard else {},
            "harnessProgress": progress_tracker.snapshot() if progress_tracker else None,
            "workspacePreflight": workspace_preflight,
            "harnessManifest": harness_manifest,
            "canaryResultId": (
                canary_result.get("canaryResultId") if canary_result else None
            ),
            "endpointRetries": (
                endpoint_retry_recorder.snapshot()
                if endpoint_retry_recorder is not None
                else None
            ),
        }
        if isinstance(error, WorkspaceVerificationError):
            frozen_candidate = _preserve_failed_unit_candidate(
                sandbox, run_root, task, task_id, error.evidence
            )
            if frozen_candidate is not None:
                error.evidence["frozenTestCandidate"] = frozen_candidate
            failure["verificationEvidence"] = error.evidence
        failure["conversationStats"] = _conversation_stats_snapshot(conversation)
        write_execution_result(execution_dir, task_id, attempt, failure)
        shutil.copyfile(journal.path, execution_dir / f"{task_id}.events.jsonl")
        if interrupted and not isinstance(error, OwnerConversationIncomplete):
            raise OwnerConversationIncomplete(
                {
                    "command": ["openhands", "conversation"],
                    "exitCode": 1,
                    "stdout": "",
                    "stderr": str(error),
                    "testResults": "",
                    "terminationReason": deterministic_termination,
                }
            ) from error
        raise
    conversation.close()
    changed = candidate_changes
    promoted_files = changed | {path for path in required_paths if (sandbox / path).is_file()}
    _promote_changed_files(sandbox, run_root, promoted_files)
    if task_type == "integration-implementation":
        frontend_build = verification.get("frontendVerification")
        if isinstance(frontend_build, dict):
            store_frontend_build(run_root, sandbox, frontend_build)
    result = {
        "taskId": task_id,
        "taskType": task_type,
        "owner": str(task.get("owner") or ""),
        "promptSha256": task.get("prompt_sha256"),
        "effectiveModel": connection.litellm_model(),
        "changedFiles": sorted(changed),
        "outputFiles": required_paths,
        "verification": verification,
        "tools": sorted(agent._tools) if agent is not None else [],
        "durationMs": int((time.monotonic() - started) * 1000),
        "eventCount": journal.event_count,
        "toolCounts": journal.tool_counts,
        "eventJournal": str(journal.path.relative_to(run_root)).replace("\\", "/"),
        "rawResponse": journal.latest_agent_message,
        "conversationId": str(conversation_id) if conversation_id else None,
        "conversationCheckpoint": (
            str(persistence_dir.relative_to(run_root)).replace("\\", "/")
            if persistence_dir is not None
            else None
        ),
        "resumedConversation": resumed_conversation,
        "executionStatus": _conversation_execution_status(conversation),
        "terminationReason": None,
        "maxConsecutiveNoActionResponses": (
            no_action_guard.max_consecutive_count if no_action_guard is not None else 0
        ),
        "stuckRecoveryUsed": stuck_recovery_used,
        "finishRecoveryUsed": finish_recovery_used,
        "harnessErrorCounts": harness_guard.counts if harness_guard else {},
        "harnessProgress": progress_tracker.snapshot() if progress_tracker else None,
        "workspacePreflight": workspace_preflight,
        "harnessManifest": harness_manifest,
        "canaryResultId": canary_result.get("canaryResultId") if canary_result else None,
        "endpointRetries": (
            endpoint_retry_recorder.snapshot()
            if endpoint_retry_recorder is not None
            else None
        ),
        "conversationStats": _conversation_stats_snapshot(conversation),
        'completionPath': completion_path,
        'agentInvoked': True,
        'initialVerification': initial_verification,
        "status": "SUCCEEDED",
    }
    write_execution_result(execution_dir, task_id, attempt, result)
    shutil.copyfile(journal.path, execution_dir / f"{task_id}.events.jsonl")
    if owner_task:
        release_owner_workspace_alias(sandbox, logical_workspace)
    # Keep the accepted run-local candidate warm for the next serial owner.
    # Isolated prechecks, frozen rechecks, and non-owner tasks retain their
    # disposable cleanup lifecycle.
    if not shared_owner_workspace:
        cleanup_agent_workspace(sandbox, run_root=run_root if owner_task else None)
    return result


def _conversation_execution_status(conversation: object | None) -> str | None:
    if conversation is None:
        return None
    status = getattr(getattr(conversation, "state", None), "execution_status", None)
    value = getattr(status, "value", None)
    return str(value) if value is not None else None


def _conversation_is_stuck(conversation: object) -> bool:
    from openhands.sdk.conversation.state import ConversationExecutionStatus

    return (
        getattr(getattr(conversation, "state", None), "execution_status", None)
        is ConversationExecutionStatus.STUCK
    )


def _conversation_needs_finish_recovery(conversation: object) -> bool:
    """Return whether an owner stopped without using OpenHands' FinishTool."""

    from openhands.sdk.conversation.state import ConversationExecutionStatus

    status = getattr(getattr(conversation, "state", None), "execution_status", None)
    if not isinstance(status, ConversationExecutionStatus):
        return False
    return status not in {
        ConversationExecutionStatus.FINISHED,
        ConversationExecutionStatus.STUCK,
    }


def _conversation_terminal_failure(conversation: object) -> bool:
    """OpenHands owns recovery; EasyDep only consumes its typed terminal state."""

    from openhands.sdk.conversation.state import ConversationExecutionStatus

    state = getattr(conversation, "state", None)
    if state is None:
        return False
    status = getattr(state, "execution_status", None)
    if status is None:
        return False
    if not isinstance(status, ConversationExecutionStatus):
        raise TypeError("OpenHands conversation returned an untyped execution status")
    return status is not ConversationExecutionStatus.FINISHED


def _tool_validation_message(error_text: str) -> str | None:
    """Extract the provider's structured tool-validation message without its envelope."""

    marker = "Error code: 400 - "
    _, found, encoded_payload = error_text.partition(marker)
    if not found:
        return None
    payload: object
    try:
        payload = json.loads(encoded_payload)
    except json.JSONDecodeError:
        try:
            payload = ast.literal_eval(encoded_payload)
        except (SyntaxError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return None
    for item in errors:
        if not isinstance(item, dict):
            continue
        message = item.get("message")
        if isinstance(message, str) and "tool call validation failed" in message.casefold():
            return message.strip()
    return None


def _conversation_stats_snapshot(conversation: object | None) -> dict[str, object] | None:
    """Persist OpenHands' own metrics without inventing unavailable values."""

    stats = getattr(conversation, "conversation_stats", None)
    if stats is None:
        return None
    try:
        snapshot = stats.model_dump(mode="json", context={"use_snapshot": True})
    except (AttributeError, TypeError, ValueError):
        try:
            snapshot = stats.model_dump()
        except (AttributeError, TypeError, ValueError):
            return None
    return snapshot if isinstance(snapshot, dict) else None


def _register_native_llm_usage(conversation: object, agent: object) -> None:
    """Register EasyDep LLM subclasses with OpenHands' native metrics registry.

    OpenHands SDK 1.36 only discovers objects whose concrete type is its base
    ``LLM`` class. EasyDep's provider-error adapter subclasses that class, so
    register the existing SDK LLMs explicitly instead of duplicating token
    accounting outside OpenHands.
    """

    registry = getattr(conversation, "llm_registry", None)
    stats = getattr(conversation, "conversation_stats", None)
    subscribe = getattr(registry, "subscribe", None)
    add = getattr(registry, "add", None)
    list_usage_ids = getattr(registry, "list_usage_ids", None)
    register_llm = getattr(stats, "register_llm", None)
    if not all(callable(item) for item in (subscribe, add, list_usage_ids, register_llm)):
        return

    subscribe(register_llm)
    registered = set(list_usage_ids())
    condenser = getattr(agent, "condenser", None)
    for llm in (getattr(agent, "llm", None), getattr(condenser, "llm", None)):
        usage_id = getattr(llm, "usage_id", None)
        if not isinstance(usage_id, str) or not usage_id or usage_id in registered:
            continue
        add(llm)
        registered.add(usage_id)


def _run_app_id(run_root: Path) -> str | None:
    """구현 실행에 저장된 변경되지 않는 앱 ID를 읽는다."""

    try:
        manifest = json.loads(
            (run_root / "reports" / "run-manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    app_id = manifest.get("app_id")
    return str(app_id) if app_id else None


def execution_attempt(run_root: Path, task_id: str) -> int:
    state_path = run_root / "reports" / "workflow-state.json"
    if not state_path.is_file():
        return 1
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 1
    return max(
        1,
        next(
            (
                int(task.get("attempts", 1))
                for task in state.get("tasks", [])
                if isinstance(task, dict) and task.get("task_id") == task_id
            ),
            1,
        ),
    )


def write_execution_result(
    execution_dir: Path,
    task_id: str,
    attempt: int,
    result: dict[str, object],
) -> None:
    content = json.dumps(result, ensure_ascii=False, indent=2)
    (execution_dir / f"{task_id}.attempt-{attempt:03d}.result.json").write_text(
        content, encoding="utf-8"
    )
    # Keep the stable path as a latest-result compatibility pointer/copy.
    (execution_dir / f"{task_id}.result.json").write_text(content, encoding="utf-8")


class _DirectEditorConversation:
    """One direct tool call that preserves the existing editor completion path."""

    def __init__(
        self,
        sandbox: Path,
        connection: LlmConnection,
        llm_config: dict[str, object],
        editable_files: list[str],
        callbacks: list[object],
    ) -> None:
        self.sandbox = sandbox
        self.connection = connection
        self.llm_config = llm_config
        self.editable_files = editable_files
        self.callbacks = callbacks
        self.initial_prompt: str | None = None
        self.prompt = ""
        self.state = type("DirectEditorState", (), {"execution_status": None, "events": []})()

    def send_message(self, message: str) -> None:
        if self.initial_prompt is None:
            self.initial_prompt = message
            self.prompt = message
            return
        # Direct editor calls are stateless API requests. Keep the original
        # task contract on a repair request while replacing (rather than
        # accumulating) the latest source evidence and diagnosis.
        self.prompt = (
            self.initial_prompt
            + "\n\n## Latest repair context\n\n"
            + message
        )

    def run(self) -> None:
        from openhands.sdk.conversation.state import ConversationExecutionStatus

        journal = next(
            (
                callback
                for callback in self.callbacks
                if isinstance(callback, EventJournal)
            ),
            None,
        )
        allowed_paths = source_replace_allowed_paths(self.sandbox, self.editable_files)
        base_request_prompt = self.prompt
        retry_delay = 1
        while True:
            try:
                action = _request_direct_editor_action(
                    self.connection,
                    self.prompt,
                    self.llm_config,
                    allowed_paths,
                )
            except DirectEditorResponseError as error:
                if not error.retryable:
                    raise
                failure = {
                    "failureCode": error.failure_code,
                    "rejectedPath": error.rejected_path,
                    "allowedPaths": allowed_paths[:16],
                    "sourceSha256": error.source_sha256,
                    "fieldIssues": error.field_issues,
                }
                if journal is not None:
                    journal.record_direct_source_rejection(
                        failure_code=error.failure_code,
                        rejected_path=error.rejected_path,
                        allowed_paths=allowed_paths,
                        source_sha256=error.source_sha256,
                        tool_name=error.tool_name,
                        field_issues=error.field_issues,
                    )
            else:
                observation = _apply_direct_editor_action(
                    self.sandbox, self.editable_files, action, journal
                )
                if not observation.is_error:
                    break
                if observation.failure_code not in {
                    "EMPTY_SOURCE",
                    "SOURCE_TOO_LARGE",
                    "INVALID_PATH_ARGUMENT",
                    "PATH_OUTSIDE_WORKSPACE",
                    "WRITE_OUTSIDE_OWNER_SCOPE",
                    "EDIT_TARGET_MISSING",
                    "EDIT_CONTEXT_STALE",
                    "EDIT_CONTEXT_AMBIGUOUS",
                    "EDIT_CONTEXT_OVERLAP",
                }:
                    raise WorkspaceVerificationError(
                        {
                            "command": [
                                SOURCE_EDIT_TOOL_NAME
                                if isinstance(action, SourceEditAction)
                                else SOURCE_REPLACE_TOOL_NAME
                            ],
                            "exitCode": 1,
                            "stdout": "",
                            "stderr": observation.text[:1200],
                            "testResults": "",
                        "sourceReplaceFailure": {
                            "failureCode": observation.failure_code,
                            "rejectedPath": observation.rejected_path,
                            "allowedPaths": observation.allowed_paths[:16],
                            "sourceSha256": observation.source_sha256,
                            "failureDetail": observation.failure_detail,
                        },
                        }
                    )
                failure = {
                    "failureCode": observation.failure_code,
                    "rejectedPath": observation.rejected_path,
                    "allowedPaths": observation.allowed_paths[:16],
                    "sourceSha256": observation.source_sha256,
                    "fieldIssues": [],
                }
            self.prompt = (
                base_request_prompt
                + "\n\n## Latest source tool argument correction\n\n"
                + json.dumps(failure, ensure_ascii=False, sort_keys=True)
                + "\nChoose an allowed path exactly as listed. For edit_source provide unique "
                "exact old_text/new_text contexts; otherwise use replace_source with a complete "
                "source body. Do not edit or redirect to any other file."
            )
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)
        self.state.execution_status = ConversationExecutionStatus.FINISHED

    def close(self) -> None:
        return None


def create_openhands_conversation(
    sandbox: Path,
    connection: LlmConnection,
    llm_config: dict[str, object],
    *,
    task_type: str = "",
    verification_paths: list[str] | None = None,
    verification_profile: dict[str, object] | None = None,
    frontend_unit_report_path: Path | None = None,
    editable_files: list[str] | None = None,
    editable_roots: list[str] | None = None,
    readable_files: list[str] | None = None,
    immutable_paths: list[str] | None = None,
    callbacks: list[object] | None = None,
    retry_listener: object | None = None,
    max_iterations: int = MAX_AGENT_TURN_ITERATIONS,
    reasoning_effort: str = "medium",
    native_owner_tools: bool = False,
    enable_native_terminal: bool = False,
    owner_tool_mode: str | None = None,
    upstream_gap_source_refs: list[str] | None = None,
    workspace: Path | None = None,
    owner_system_context: str = "",
    canary_tools: bool = False,
    system_prompt_text: str | None = None,
    persistence_dir: Path | None = None,
    conversation_id: uuid.UUID | None = None,
):
    global _SANDBOX_TOOLS_REGISTERED

    # Preserve the direct-call baseline for older callers. Production owners
    # always pass the configured mode explicitly; omitted mode means the former
    # broad owner editor contract, whether or not its terminal is enabled.
    effective_owner_tool_mode = owner_tool_mode or (
        "terminal" if native_owner_tools else "restricted"
    )
    if effective_owner_tool_mode == "editor":
        direct = _DirectEditorConversation(
            sandbox,
            connection,
            llm_config,
            list(editable_files or []),
            list(callbacks or []),
        )
        return direct, type(
            "DirectEditorAgent",
            (),
            {"_tools": {"finish": None, "replace_source": None, "edit_source": None}},
        )()

    from openhands.sdk import LLM, Agent, AgentContext, Conversation, Tool, register_tool
    from openhands.sdk.context.condenser import default_condenser
    from openhands.sdk.llm.exceptions import (
        FunctionCallValidationError,
        LLMBadRequestError,
        LLMNoResponseError,
    )
    from openhands.tools.file_editor import FileEditorTool
    from openhands.tools.file_editor.definition import FileEditorObservation
    from openhands.tools.file_editor.impl import FileEditorExecutor
    from openhands.tools.grep import GrepObservation, GrepTool
    from openhands.tools.grep.impl import GrepExecutor
    from openhands.tools.terminal import TerminalTool
    from pydantic import SecretStr

    _configure_openhands_profile_store()
    owner_terminal_shell: str | None = None
    if enable_native_terminal:
        owner_terminal_shell = os.environ.get(OWNER_TERMINAL_SHELL_ENV, "").strip()
        if not owner_terminal_shell:
            raise RuntimeError(
                f"Autonomous OpenHands terminal requires {OWNER_TERMINAL_SHELL_ENV}."
            )
        exposed_credentials = [
            name for name in LLM_CREDENTIAL_ENVIRONMENT if os.environ.get(name)
        ]
        if exposed_credentials:
            raise RuntimeError(
                "OpenHands terminal credential environment was not scrubbed: "
                + ", ".join(exposed_credentials)
            )

    def raise_provider_tool_validation(error: LLMBadRequestError) -> None:
        message = _tool_validation_message(str(error))
        if message is not None:
            raise FunctionCallValidationError(message) from error

    def _completion_has_no_assistant_output(response: object) -> bool:
        response_get = getattr(response, "get", None)
        if not callable(response_get):
            return False
        choices = response_get("choices")
        if not isinstance(choices, list) or not choices:
            return False
        choice_get = getattr(choices[0], "get", None)
        if not callable(choice_get):
            return False
        message = choice_get("message")
        message_get = getattr(message, "get", None)
        if not callable(message_get):
            return False
        return not any(
            message_get(field)
            for field in (
                "content",
                "tool_calls",
                "function_call",
                "reasoning_content",
                "reasoning",
                "refusal",
            )
        )

    class ProviderToolValidationLLM(LLM):
        """Route narrow provider 400s into the matching safe SDK recovery."""

        require_native_tool_choice: bool = False

        def _chat_tool_choice(self, kwargs: dict[str, object]) -> dict[str, object]:
            if self.require_native_tool_choice and kwargs.get("tools"):
                return {**kwargs, "tool_choice": "required"}
            return kwargs

        def _validate_chat_response(self, response, **kwargs):
            validated = super()._validate_chat_response(response, **kwargs)
            if _completion_has_no_assistant_output(validated):
                raise LLMNoResponseError(
                    "PROVIDER_EMPTY_RESPONSE_TRANSIENT: provider returned an empty "
                    "assistant completion before dispatching a tool"
                )
            return validated

        async def acompletion(self, *args, **kwargs):
            try:
                return await asyncio.wait_for(
                    super().acompletion(*args, **kwargs),
                    timeout=float(settings.llm_wall_timeout_seconds),
                )
            except TimeoutError as error:
                raise TimeoutError(
                    "PROVIDER_TIMEOUT: OpenHands LLM completion exceeded the "
                    f"{settings.llm_wall_timeout_seconds:g}s wall timeout"
                ) from error

        async def aresponses(self, *args, **kwargs):
            try:
                return await asyncio.wait_for(
                    super().aresponses(*args, **kwargs),
                    timeout=float(settings.llm_wall_timeout_seconds),
                )
            except TimeoutError as error:
                raise TimeoutError(
                    "PROVIDER_TIMEOUT: OpenHands LLM response exceeded the "
                    f"{settings.llm_wall_timeout_seconds:g}s wall timeout"
                ) from error

        def _transport_call(self, **kwargs):
            try:
                return super()._transport_call(**self._chat_tool_choice(kwargs))
            except Exception as error:
                if is_provider_output_parse_failure(error):
                    raise LLMNoResponseError(
                        "PROVIDER_OUTPUT_PARSE_TRANSIENT: provider could not parse "
                        "the generated model output before dispatching a tool"
                    ) from error
                raise

        async def _atransport_call(self, **kwargs):
            try:
                return await super()._atransport_call(**self._chat_tool_choice(kwargs))
            except Exception as error:
                if is_provider_output_parse_failure(error):
                    raise LLMNoResponseError(
                        "PROVIDER_OUTPUT_PARSE_TRANSIENT: provider could not parse "
                        "the generated model output before dispatching a tool"
                    ) from error
                raise

        def _handle_error(self, error, fallback_call_fn):
            try:
                return super()._handle_error(error, fallback_call_fn)
            except LLMBadRequestError as mapped_error:
                raise_provider_tool_validation(mapped_error)
                raise

        async def _ahandle_error(self, error, fallback_call_fn):
            try:
                return await super()._ahandle_error(error, fallback_call_fn)
            except LLMBadRequestError as mapped_error:
                raise_provider_tool_validation(mapped_error)
                raise

    class SandboxFileEditorExecutor(FileEditorExecutor):
        """Apply only EasyDep's filesystem boundary to the canonical editor."""

        def __init__(
            self,
            workspace_root: str,
            writable_files: list[str],
            writable_roots: list[str],
            immutable: list[str],
            enforce_write_scope: bool,
            readable_files: list[str] | None,
        ):
            super().__init__(workspace_root=workspace_root)
            self.logical_workspace = Path(workspace_root)
            self.workspace_root = Path(workspace_root).resolve()
            self.writable_files = {Path(path).resolve() for path in writable_files}
            self.writable_roots = {Path(path).resolve() for path in writable_roots}
            self.immutable = {Path(path).resolve() for path in immutable}
            self.enforce_write_scope = enforce_write_scope
            self.readable_files = (
                {Path(path).resolve() for path in readable_files}
                if readable_files is not None
                else None
            )

        def __call__(self, action, conversation=None):
            supplied = Path(action.path)
            target = (
                supplied.resolve()
                if supplied.is_absolute()
                else (self.workspace_root / supplied).resolve()
            )
            try:
                target.relative_to(self.workspace_root)
            except ValueError:
                return FileEditorObservation.from_text(
                    text=render_harness_error(
                        "PATH_OUTSIDE_WORKSPACE",
                        "The path is outside the assigned workspace. Use an absolute path rooted at the assigned /work task directory.",
                        retryable=True,
                        workspace=str(self.logical_workspace),
                    ),
                    command=action.command,
                    is_error=True,
                )
            if (
                action.command == "view"
                and self.readable_files is not None
                and target not in self.readable_files
            ):
                return FileEditorObservation.from_text(
                    text=render_harness_error(
                        "READ_OUTSIDE_TASK_EVIDENCE",
                        "The path is outside the bounded implementation evidence. Use the supplied contract and edit now, or report the missing context.",
                        retryable=True,
                        workspace=str(self.logical_workspace),
                        requestedPath=str(target),
                    ),
                    command=action.command,
                    is_error=True,
                )
            if action.command != "view" and self.enforce_write_scope:
                if any(target == path or path in target.parents for path in self.immutable):
                    return FileEditorObservation.from_text(
                        text=render_harness_error(
                            "WRITE_OUTSIDE_OWNER_SCOPE",
                            "Generated contracts are read-only.",
                            retryable=False,
                            workspace=str(self.logical_workspace),
                        ),
                        command=action.command,
                        is_error=True,
                    )
                if target not in self.writable_files and not any(
                    target == root or root in target.parents for root in self.writable_roots
                ):
                    return FileEditorObservation.from_text(
                        text=render_harness_error(
                            "WRITE_OUTSIDE_OWNER_SCOPE",
                            "The write is outside the assigned implementation roots.",
                            retryable=False,
                            workspace=str(self.logical_workspace),
                        ),
                        command=action.command,
                        is_error=True,
                    )
            observation = super().__call__(action, conversation)
            if action.command != "view" and not getattr(observation, "is_error", False):
                grant_owner_file_access(target, self.workspace_root)
            return observation

    class SandboxFileEditorTool(FileEditorTool):
        name = "file_editor"

        @classmethod
        def create(
            cls,
            conv_state,
            writable_files,
            writable_roots,
            immutable_paths,
            enforce_write_scope,
            readable_files,
        ):
            return [
                instance.model_copy(
                    update={
                        "description": (
                            "Read or edit plain-text files inside the assigned workspace. "
                            "The canonical FileEditor requires an absolute path rooted at that "
                            "workspace, for example /work/application/src/main/java/example/App.java. "
                            'For an existing file use command="str_replace" with old_str and '
                            'new_str; for a new file use command="create" with file_text. '
                            'command="edit" and old_string/new_string are invalid. Paths resolving '
                            "outside the workspace are rejected. Use view before an edit and "
                            "preserve generated public declarations."
                        ),
                        "executor": SandboxFileEditorExecutor(
                            conv_state.workspace.working_dir,
                            writable_files,
                            writable_roots,
                            immutable_paths,
                            enforce_write_scope,
                            readable_files,
                        )
                    }
                )
                for instance in super().create(conv_state)
            ]

    class SandboxGrepExecutor(GrepExecutor):
        def __init__(self, working_dir: str, readable_files: list[str] | None):
            self.logical_workspace = Path(working_dir)
            super().__init__(working_dir)
            self.readable_files = (
                {Path(path).resolve() for path in readable_files}
                if readable_files is not None
                else None
            )

        def __call__(self, action, conversation=None):
            supplied = Path(action.path) if action.path else None
            target = (
                supplied.resolve()
                if supplied is not None and supplied.is_absolute()
                else (self.working_dir / supplied).resolve()
                if supplied is not None
                else self.working_dir
            )
            try:
                target.relative_to(self.working_dir)
            except ValueError:
                return GrepObservation.from_text(
                    text=render_harness_error(
                        "PATH_OUTSIDE_WORKSPACE",
                        "The search path is outside the assigned workspace. Search a path relative to /work.",
                        retryable=True,
                        workspace=str(self.logical_workspace),
                    ),
                    matches=[],
                    pattern=action.pattern,
                    search_path=str(target),
                    include_pattern=action.include,
                    is_error=True,
                )
            if self.readable_files is not None:
                candidates = (
                    [target]
                    if target in self.readable_files
                    else sorted(
                        path
                        for path in self.readable_files
                        if supplied is not None
                        and target.is_dir()
                        and path.is_file()
                        and path.is_relative_to(target)
                    )
                )
                if not candidates:
                    return GrepObservation.from_text(
                        text=render_harness_error(
                            "READ_OUTSIDE_TASK_EVIDENCE",
                            "Search only the supplied implementation evidence. Report missing context instead of broadening discovery.",
                            retryable=True,
                            workspace=str(self.logical_workspace),
                            requestedPath=str(target),
                        ),
                        matches=[],
                        pattern=action.pattern,
                        search_path=str(target),
                        include_pattern=action.include,
                        is_error=True,
                    )
                try:
                    pattern = re.compile(action.pattern, re.IGNORECASE)
                except re.error as error:
                    return GrepObservation.from_text(
                        text=f"Invalid regex pattern: {error}",
                        matches=[],
                        pattern=action.pattern,
                        search_path=str(target),
                        include_pattern=action.include,
                        is_error=True,
                    )
                matches = []
                try:
                    for candidate in candidates:
                        if pattern.search(
                            candidate.read_text(encoding="utf-8", errors="ignore")
                        ):
                            matches.append(candidate)
                except OSError as error:
                    return GrepObservation.from_text(
                        text=str(error),
                        matches=[],
                        pattern=action.pattern,
                        search_path=str(target),
                        include_pattern=action.include,
                        is_error=True,
                    )
                return self._build_observation(
                    action,
                    target if target.is_dir() else target.parent,
                    matches,
                )
            return super().__call__(action, conversation)

    class SandboxGrepTool(GrepTool):
        name = "grep"

        @classmethod
        def create(cls, conv_state, readable_files):
            return [
                instance.model_copy(
                    update={
                        "description": (
                            "Search text files inside /work. Use a path relative to /work and "
                            "do not search parent directories."
                        ),
                        "executor": SandboxGrepExecutor(
                            conv_state.workspace.working_dir, readable_files
                        ),
                    }
                )
                for instance in super().create(conv_state)
            ]

    editor_registry_name = "easydep_sandbox_file_editor"
    grep_registry_name = "easydep_sandbox_grep"
    if not _SANDBOX_TOOLS_REGISTERED:
        with _SANDBOX_TOOLS_REGISTRATION_LOCK:
            if not _SANDBOX_TOOLS_REGISTERED:
                register_tool(editor_registry_name, SandboxFileEditorTool)
                register_tool(grep_registry_name, SandboxGrepTool)
                _SANDBOX_TOOLS_REGISTERED = True
    model = connection.litellm_model()
    raw_temperature = llm_config["temperature"]
    raw_max_output = llm_config["maxOutputTokens"]
    if not isinstance(raw_temperature, (int, float, str)):
        raise TypeError("implementation LLM temperature must be numeric")
    if not isinstance(raw_max_output, (int, str)):
        raise TypeError("implementation LLM maxOutputTokens must be an integer")
    profile = profile_for(
        connection.model,
        fallback_temperature=float(raw_temperature),
        fallback_max_tokens=int(raw_max_output),
    )
    requested_max_output = (
        int(settings.openhands_max_output_tokens)
        if settings.openhands_max_output_tokens is not None
        else min(int(raw_max_output), profile.default_max_tokens)
    )
    llm_options: dict[str, Any] = {
        "model": model,
        "usage_id": "implementation_agent",
        "api_key": SecretStr(connection.api_key),
        "base_url": connection.base_url,
        "extra_headers": connection.default_headers(),
        "temperature": profile.temperature,
        "max_output_tokens": profile.completion_limit(
            requested_max_output
        ),
        "timeout": max(1, int(settings.llm_timeout_seconds)),
        "num_retries": settings.implementation_openhands_request_attempts,
        "retry_min_wait": settings.implementation_openhands_retry_min_wait_seconds,
        "retry_max_wait": settings.implementation_openhands_retry_max_wait_seconds,
        "retry_multiplier": settings.implementation_openhands_retry_multiplier,
    }
    if retry_listener is not None:
        llm_options["retry_listener"] = retry_listener
    llm_options.update(connection.openhands_options())
    if (
        connection.provider == "cloudflare"
        and native_owner_tools
        and effective_owner_tool_mode == "editor"
    ):
        llm_options["require_native_tool_choice"] = True
    # Cloudflare's OpenAI-compatible endpoint controls thinking with
    # ``reasoning_effort``. OpenHands otherwise keeps its provider-agnostic
    # 200k extended-thinking default on the LLM object even though that is not
    # part of this endpoint's contract.
    if connection.provider == "cloudflare":
        llm_options["extended_thinking_budget"] = None
    if profile.top_p is not None:
        llm_options["top_p"] = profile.top_p
    if resolved_reasoning := profile.resolve_reasoning(reasoning_effort):
        llm_options["reasoning_effort"] = resolved_reasoning
    if extra_body := profile.extra_body(connection.provider):
        llm_options["litellm_extra_body"] = extra_body
    warnings.filterwarnings(
        "ignore",
        message=r"Cost calculation failed:.*",
        module=r"openhands\.sdk\.llm\.utils\.telemetry",
    )
    llm = ProviderToolValidationLLM(**llm_options)
    if canary_tools:
        from .canary_tool import register_canary_tools

        read_tool, check_tool = register_canary_tools()
        tools = [Tool(name=read_tool, params={}), Tool(name=check_tool, params={})]
    elif native_owner_tools:
        if effective_owner_tool_mode == "editor":
            tools = [
                Tool(
                    name=register_source_replace_tool(),
                    params={"allowed_files": editable_files or []},
                ),
                Tool(
                    name=register_source_edit_tool(),
                    params={"allowed_files": editable_files or []},
                ),
            ]
            if upstream_gap_source_refs is not None:
                tools.append(
                    Tool(
                        name=register_upstream_gap_tool(),
                        params={"source_refs": upstream_gap_source_refs},
                    )
                )
        else:
            tools = [
                Tool(
                    name=editor_registry_name,
                    params={
                        "writable_files": editable_files or [],
                        "writable_roots": editable_roots or [],
                        "immutable_paths": immutable_paths or [],
                        "enforce_write_scope": effective_owner_tool_mode != "terminal",
                        "readable_files": readable_files,
                    },
                ),
            ]
        if effective_owner_tool_mode == "restricted":
            task_check_tool_name = register_task_check_tool()
            tools.extend(
                [
                    Tool(
                        name=grep_registry_name,
                        params={"readable_files": readable_files},
                    ),
                    Tool(
                        name=task_check_tool_name,
                        params={
                            "task_type": task_type,
                            "allowed_write_paths": verification_paths or [],
                            "verification_profile": verification_profile or {},
                            "frontend_unit_report_path": str(frontend_unit_report_path)
                            if frontend_unit_report_path
                            else None,
                        },
                    ),
                ]
            )
            if upstream_gap_source_refs is not None:
                upstream_gap_tool_name = register_upstream_gap_tool()
                tools.append(
                    Tool(
                        name=upstream_gap_tool_name,
                        params={"source_refs": upstream_gap_source_refs},
                    )
                )
        elif effective_owner_tool_mode not in {"editor", "terminal"}:
            raise ValueError(
                f"Unsupported OpenHands owner tool mode: {effective_owner_tool_mode}"
            )
        if enable_native_terminal and effective_owner_tool_mode == "terminal":
            npm_environment = npm_command_environment(os.environ)
            tools.append(
                Tool(
                    name=TerminalTool.name,
                    params={
                        "terminal_type": "subprocess",
                        "shell_path": owner_terminal_shell,
                        "env": {
                            "HOME": OWNER_TERMINAL_HOME,
                            "npm_config_cache": OWNER_NPM_CACHE,
                            "npm_config_registry": npm_environment[
                                "npm_config_registry"
                            ],
                            "npm_config_replace_registry_host": npm_environment[
                                "npm_config_replace_registry_host"
                            ],
                        },
                    },
                )
            )
    else:
        task_check_tool_name = register_task_check_tool()
        tools = [
            Tool(
                name=editor_registry_name,
                params={
                    "writable_files": editable_files or [],
                    "writable_roots": editable_roots or [],
                    "immutable_paths": immutable_paths or [],
                    "enforce_write_scope": True,
                    "readable_files": readable_files,
                },
            ),
            Tool(
                name=grep_registry_name,
                params={"readable_files": readable_files},
            ),
            Tool(
                name=task_check_tool_name,
                params={
                    "task_type": task_type,
                    "allowed_write_paths": verification_paths or [],
                    "verification_profile": verification_profile or {},
                    "frontend_unit_report_path": str(frontend_unit_report_path)
                    if frontend_unit_report_path
                    else None,
                },
            ),
        ]
    agent_options: dict[str, object] = {}
    if native_owner_tools or canary_tools:
        # Read the versioned prompt directly. OpenHands 1.36's custom Jinja path
        # writes bytecode below the process home, which is outside EasyDep's
        # controlled temporary state and can itself fail on Windows permissions.
        agent_options["system_prompt"] = system_prompt_text or owner_prompt_path().read_text(
            encoding="utf-8"
        )
        agent_options["agent_context"] = AgentContext(
            system_message_suffix=owner_system_context
        )
    agent = Agent(
        llm=llm,
        tools=tools,
        include_default_tools=["FinishTool"],
        condenser=default_condenser(
            llm=llm.model_copy(update={"usage_id": "implementation_condenser"}),
        ),
        **agent_options,
    )
    conversation = Conversation(
        agent=agent,
        workspace=str(workspace or sandbox),
        callbacks=callbacks,
        max_iteration_per_run=max_iterations,
        stuck_detection=True,
        visualizer=None,
        persistence_dir=persistence_dir,
        conversation_id=conversation_id,
        delete_on_close=False,
    )
    _register_native_llm_usage(conversation, conversation.agent)
    return conversation, conversation.agent


def _path_is_immutable(path: str, immutable_paths: set[str]) -> bool:
    """파일 경로가 생성 계약 파일 또는 그 하위에 있는지 확인한다."""
    normalized = path.replace("\\", "/").rstrip("/")
    return any(
        normalized == root.rstrip("/") or normalized.startswith(root.rstrip("/") + "/")
        for root in immutable_paths
    )
