from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..domain.implementation_ir import remove_readonly
from ..runtime.linux_runner_transport import (
    OWNER_CONTROL_ROOT_ENV,
    OWNER_GRADLE_CACHE,
    OWNER_NPM_CACHE,
    OWNER_TERMINAL_HOME,
    OWNER_TERMINAL_SHELL_ENV,
    OWNER_TERMINAL_USER_ENV,
    OWNER_WORKSPACE_ALIAS,
)

_IGNORED_WORKSPACE_PARTS = {
    "build",
    ".gradle",
    "node_modules",
    "dist",
}
_SHARED_OWNER_TASK_MARKER = ".easydep-shared-owner-task"


def _is_ignored_workspace_path(relative: Path) -> bool:
    """Return whether a generated/cache path must survive a source refresh."""

    return relative.name.endswith(".tsbuildinfo") or any(
        part in _IGNORED_WORKSPACE_PARTS for part in relative.parts
    )


def _iter_workspace_source_files(root: Path):
    """Yield source files without descending into retained build/cache trees."""

    for directory, children, files in os.walk(root, followlinks=False):
        children[:] = [
            child for child in children if child not in _IGNORED_WORKSPACE_PARTS
        ]
        current = Path(directory)
        for name in files:
            path = current / name
            if path.is_file():
                yield path


def _shared_owner_task_marker(sandbox: Path) -> Path:
    """Return coordinator metadata deliberately kept outside the owner tree."""

    return sandbox.parent / f".{sandbox.name}{_SHARED_OWNER_TASK_MARKER}"


def missing_required_outputs(sandbox: Path, relative_paths: list[str]) -> list[str]:
    """Return contracted task outputs that the agent has not created as files."""
    return [relative for relative in relative_paths if not (sandbox / relative).is_file()]


def load_task(run_root: Path, task_id: str) -> dict[str, object]:
    task_dir = run_root / "reports" / "implementation-tasks"
    for candidate in task_dir.glob("*.task.json"):
        task = json.loads(candidate.read_text(encoding="utf-8"))
        if task["task_id"] == task_id:
            return task
    raise ValueError(f"Unknown task: {task_id}")


def prompt_file_sha256(path: Path) -> str:
    """Hash a prompt with the same UTF-8/LF semantics used by ``TaskSpec``."""

    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def load_strict_task(
    run_root: Path,
    task_id: str,
    *,
    allowed_task_types: frozenset[str] | None = None,
) -> dict[str, object]:
    """Load one planned task only when its sidecar and manifest agree exactly.

    The member runner receives paths from a diagnostic boundary.  Requiring a
    single sidecar and a single matching manifest record prevents an arbitrary
    or stale task file from being submitted to the owner runtime.
    """

    root = run_root.resolve()
    if not task_id.strip():
        raise ValueError("Task ID is required")
    task_dir = root / "reports" / "implementation-tasks"
    matches: list[dict[str, object]] = []
    for candidate in sorted(task_dir.glob("*.task.json")):
        try:
            task = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid task file: {candidate.name}") from error
        if isinstance(task, dict) and task.get("task_id") == task_id:
            matches.append(task)
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one task file for {task_id!r}")
    task = matches[0]

    manifest_path = root / "reports" / "run-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        records = manifest["implementation_tasks"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Run manifest has no valid implementation tasks") from error
    manifest_matches = [
        record
        for record in records
        if isinstance(record, dict) and record.get("task_id") == task_id
    ]
    if len(manifest_matches) != 1:
        raise ValueError(f"Expected exactly one manifest task for {task_id!r}")
    manifest_task = manifest_matches[0]
    required_fields = ("task_id", "task_type", "prompt_file", "context_file", "prompt_sha256")
    if any(not isinstance(task.get(field), str) or not task[field] for field in required_fields):
        raise ValueError(f"Task sidecar and manifest disagree for {task_id!r}")
    if json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":")) != json.dumps(
        manifest_task, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ):
        raise ValueError(f"Task sidecar and manifest disagree for {task_id!r}")
    task_type = str(task["task_type"])
    if allowed_task_types is not None and task_type not in allowed_task_types:
        raise ValueError(f"Task {task_id!r} is not an owner task")
    prompt_path = (root / str(task["prompt_file"])).resolve()
    try:
        prompt_path.relative_to(root)
        prompt_text = prompt_path.read_text(encoding="utf-8")
        repair_prompt = task.get("repair_prompt_file")
        if isinstance(repair_prompt, str) and repair_prompt:
            repair_path = (root / repair_prompt).resolve()
            repair_path.relative_to(root)
            prompt_text += "\0" + repair_path.read_text(encoding="utf-8")
        prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    except (OSError, ValueError) as error:
        raise ValueError(f"Task prompt is unavailable for {task_id!r}") from error
    if prompt_hash != task["prompt_sha256"]:
        raise ValueError(f"Task prompt hash is inconsistent for {task_id!r}")
    context_path = (root / str(task["context_file"])).resolve()
    try:
        context_path.relative_to(root)
        json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"Task context is unavailable for {task_id!r}") from error
    return task


def task_base_package(task: dict[str, object]) -> str:
    package_markers = {
        "application", "persistence", "adapter", "integration", "config", "bce", "api"
    }
    for output in task["allowed_write_paths"]:
        relative = Path(str(output))
        parts = relative.parts
        if "java" not in parts:
            continue
        java_index = parts.index("java")
        marker_index = next(
            (
                index for index in range(java_index + 1, len(parts))
                if parts[index] in package_markers
            ),
            None,
        )
        if marker_index is not None and marker_index > java_index + 1:
            return ".".join(parts[java_index + 1 : marker_index])
    raise ValueError("Cannot derive base package from task outputs")


def read_persistence_entity_contracts(run_root: Path, base_package: str) -> str:
    root = (
        run_root
        / "application"
        / "src"
        / "main"
        / "java"
        / Path(base_package.replace(".", "/"))
        / "persistence"
        / "entity"
    )
    contracts: list[str] = []
    for path in sorted(root.glob("*Entity.java")):
        contracts.append(
            f"// persistence/entity/{path.name}\n"
            + path.read_text(encoding="utf-8").strip()
        )
    return "\n\n".join(contracts) or "// No persistence entity contracts found"


def prepare_agent_workspace(
    run_root: Path,
    task: dict[str, object],
    *,
    preserve_failed_edits: bool = True,
    persistent: bool = False,
    shared_owner_workspace: bool = False,
    requires_owner_terminal: bool = True,
) -> Path:
    """작업별 임시 공간을 만들고 현재 run source와 맞춘다.

    한 대화 안에서는 OpenHands가 자유롭게 여러 번 수정한다. 실패한 작업을 재개할 때에는
    기본적으로 sandbox의 편집 내용을 보존하고, 정식 run source의 변경 사항과 불변 계약만
    다시 동기화한다. build와 package cache는 복사하지 않는다.
    """
    # Real runs normally have UUID-like names, but tests and imported runs may reuse a
    # short directory name such as ``run``. Include the absolute root in the key so two
    # unrelated runs never inherit one another's failed candidate workspace.
    run_key = hashlib.sha256(str(run_root.resolve()).encode("utf-8")).hexdigest()[:12]
    # Serial owners normally work from one candidate for the whole run.  This
    # keeps toolchain output warm and lets a retry continue its unpromoted
    # candidate, while the per-task refresh below still restores every path
    # outside the current owner scope from the canonical run source.
    #
    # Prechecks and frozen/replay checks intentionally keep the default
    # task-keyed workspace so their candidate cannot leak into an owner run.
    task_key = (
        f"{run_key}-shared-owner"
        if shared_owner_workspace
        else f"{run_key}-{str(task['task_id']).removeprefix('implement-')}"
    )
    # 작업 ID는 보고서에서 읽기 쉬운 전체 이름을 유지한다. 다만 Windows 임시 경로에 같은
    # 이름을 그대로 붙이면 persistence처럼 여러 Entity를 묶은 작업이 260자 제한에 닿는다.
    # 임시 폴더만 앞부분과 해시로 줄이면 충돌을 피하면서 어떤 작업인지도 알아볼 수 있다.
    if persistent:
        control_value = os.environ.get(OWNER_CONTROL_ROOT_ENV, "").strip()
        if control_value:
            control_root = Path(control_value).resolve()
            resolved_run = run_root.resolve()
            if control_root != resolved_run and control_root not in resolved_run.parents:
                raise ValueError("Implementation run is outside the fixed runner control root")
            # Keep the resumable candidate inside the job-only Docker bind, but not
            # below the already deep generated run/report tree.  Windows hosts apply
            # their path limit to that bind even though the owner runs in Linux.
            sandbox_parent = control_root / "w"
        else:
            sandbox_parent = Path(tempfile.gettempdir()) / "easydep-owner-workspaces"
    else:
        sandbox_parent = Path(tempfile.gettempdir()) / "easydep-agent-workspaces"
    longest_output = max(
        (len(str(Path(str(path)))) for path in task["allowed_write_paths"]),
        default=0,
    )
    # ``-2`` 같은 충돌 회피 suffix까지 붙을 수 있도록 네 글자를 남긴다.
    available_task_length = 240 - len(str(sandbox_parent.resolve())) - longest_output - 6
    if available_task_length < 8:
        raise ValueError("Agent workspace root leaves no safe Windows path budget")
    if shared_owner_workspace and len(task_key) > available_task_length:
        # A shared candidate must resolve to the identical directory for every
        # serial task. Never shorten it from task-specific output lengths.
        raise ValueError("Shared owner workspace root leaves no safe Windows path budget")
    if not shared_owner_workspace and len(task_key) > available_task_length:
        digest = hashlib.sha256(task_key.encode("utf-8")).hexdigest()[:10]
        prefix_length = available_task_length - len(digest) - 1
        task_key = (
            f"{task_key[:prefix_length]}-{digest}"
            if prefix_length > 0
            else digest[:available_task_length]
        )
    sandbox_base = sandbox_parent / task_key
    sandbox = sandbox_base
    source_application = run_root / "application"
    sandbox_application = sandbox / "application"
    current_task_id = str(task["task_id"])
    editable = {
        str(path).replace("\\", "/")
        for path in task.get("allowed_write_paths", [])
    }
    editable_roots = {
        str(path).replace("\\", "/").rstrip("/")
        for path in task.get("allowed_write_roots", [])
    }
    immutable = {
        str(path).replace("\\", "/").rstrip("/")
        for path in task.get("immutable_paths", [])
    }
    if sandbox_application.is_dir():
        if requires_owner_terminal:
            _restore_coordinator_access(sandbox)
        # A failed candidate is resumable only by its own task.  Before a
        # different serial owner starts, restore all source from canonical so
        # an unaccepted edit cannot cross the task boundary. Generated caches
        # remain untouched by _refresh_agent_workspace.
        previous_task_id = ""
        # The owner terminal can write its entire candidate tree, so task
        # identity must live beside (not inside) that tree. Fixed-runner
        # permission hardening makes this sidecar coordinator-owned.
        marker = _shared_owner_task_marker(sandbox)
        if shared_owner_workspace and marker.is_file():
            try:
                previous_task_id = marker.read_text(encoding="utf-8").strip()
            except OSError:
                previous_task_id = ""
        _refresh_agent_workspace(
            run_root,
            source_application,
            sandbox,
            sandbox_application,
            editable,
            editable_roots,
            immutable,
            preserve_failed_edits=(
                preserve_failed_edits
                and (not shared_owner_workspace or previous_task_id == current_task_id)
            ),
            verify_content_if_discarding_candidate=(
                shared_owner_workspace
                and previous_task_id != current_task_id
            ),
        )
    else:
        sandbox.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source_application,
            sandbox_application,
            ignore=shutil.ignore_patterns(*_IGNORED_WORKSPACE_PARTS, "*.tsbuildinfo"),
        )
    if shared_owner_workspace:
        _shared_owner_task_marker(sandbox).write_text(
            current_task_id + "\n", encoding="utf-8"
        )
    for relative in task["allowed_write_paths"]:
        target = sandbox / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt" and len(str(target.resolve())) > 240:
            raise ValueError(f"Agent write path exceeds safe Windows path budget: {target}")
    _copy_read_sources(run_root, sandbox, task)
    if requires_owner_terminal:
        _apply_fixed_runner_permissions(run_root, sandbox, task)
    return sandbox


def prepare_owner_workspace_alias(sandbox: Path, task_key: str = "owner") -> Path:
    """Expose a short per-task path inside the fixed Linux owner container.

    Owners may run concurrently, so changing one process-global ``/work`` symlink
    would race.  ``/work/<task>`` keeps the visible structure stable while giving
    every conversation an independent link.
    """

    resolved = sandbox.resolve()
    if os.name != "posix" or os.environ.get("EASYDEP_FIXED_LINUX_RUNNER") != "1":
        return resolved
    if os.geteuid() != 0:
        raise RuntimeError("Owner workspace alias requires a root coordinator.")
    safe_key = re.sub(r"[^a-zA-Z0-9._-]+", "-", task_key).strip("-.") or "owner"
    if len(safe_key) > 48:
        digest = hashlib.sha256(safe_key.encode("utf-8")).hexdigest()[:10]
        safe_key = f"{safe_key[:37]}-{digest}"
    alias_root = Path(OWNER_WORKSPACE_ALIAS)
    alias_root.mkdir(mode=0o755, parents=True, exist_ok=True)
    alias = alias_root / safe_key
    if alias.is_symlink():
        if alias.resolve() == resolved:
            return alias
        alias.unlink()
    elif alias.exists():
        raise RuntimeError(f"Owner workspace alias is occupied by a non-symlink: {alias}")
    alias.symlink_to(resolved, target_is_directory=True)
    return alias


def release_owner_workspace_alias(sandbox: Path, logical_workspace: Path | None = None) -> None:
    """Remove only the fixed alias when it still points at this owner's sandbox."""

    if os.name != "posix" or os.environ.get("EASYDEP_FIXED_LINUX_RUNNER") != "1":
        return
    alias = logical_workspace or Path(OWNER_WORKSPACE_ALIAS) / "owner"
    if alias.parent != Path(OWNER_WORKSPACE_ALIAS):
        return
    if alias.is_symlink() and alias.resolve() == sandbox.resolve():
        alias.unlink()


def preflight_owner_workspace(
    sandbox: Path,
    *,
    editable_files: list[str],
    editable_roots: list[str],
    immutable_paths: list[str],
    logical_workspace: Path | None = None,
    enforce_write_scope: bool = True,
    requires_owner_terminal: bool = True,
) -> dict[str, object]:
    """Check the real owner identity, workspace and shell before an LLM call."""

    resolved = sandbox.resolve()
    if not resolved.is_dir():
        raise RuntimeError(f"ENV_WORKSPACE_PERMISSION: workspace is missing: {resolved}")
    resolved_editable_files = [Path(path).resolve() for path in editable_files]
    resolved_editable_roots = [Path(path).resolve() for path in editable_roots]
    resolved_immutable = [Path(path).resolve() for path in immutable_paths]
    assigned_paths = [
        *resolved_editable_files,
        *resolved_editable_roots,
        *resolved_immutable,
    ]
    if any(path != resolved and resolved not in path.parents for path in assigned_paths):
        raise RuntimeError("ENV_WORKSPACE_PERMISSION: assigned path escaped the workspace")
    relative_editable_files = [path.relative_to(resolved).as_posix() for path in resolved_editable_files]
    relative_editable_roots = [path.relative_to(resolved).as_posix() for path in resolved_editable_roots]
    relative_immutable = [path.relative_to(resolved).as_posix() for path in resolved_immutable]
    if path_is_editable(
        "../outside",
        relative_editable_files,
        relative_editable_roots,
        relative_immutable,
    ):
        raise RuntimeError("ENV_WORKSPACE_PERMISSION: outside path passed the write policy")
    if any(
        path_is_editable(
            path,
            relative_editable_files,
            relative_editable_roots,
            relative_immutable,
        )
        for path in relative_immutable
    ):
        raise RuntimeError("ENV_WORKSPACE_PERMISSION: immutable path passed the write policy")
    candidates = list(resolved_editable_roots)
    candidates.extend(path.parent for path in resolved_editable_files)
    probe_root = next(
        (
            path
            for path in candidates
            if path == resolved or resolved in path.parents
        ),
        resolved,
    )
    probe_root.mkdir(parents=True, exist_ok=True)
    logical = logical_workspace or resolved
    if logical_workspace is not None and logical.resolve() != resolved:
        raise RuntimeError("ENV_WORKSPACE_PERMISSION: logical workspace mapping is invalid")

    # Restricted owners have no terminal. Their file editor executes through the
    # coordinator and enforces the assigned paths, so a coordinator write probe
    # is the relevant capability check. Do not recurse over the job tree merely
    # to prepare an identity that cannot be used.
    #
    # On development hosts there is likewise no unprivileged owner shell. The
    # same path containment checks still run, while the Linux image probe covers
    # UID/GID when a terminal owner is actually enabled.
    shell = os.environ.get(OWNER_TERMINAL_SHELL_ENV, "").strip()
    if not requires_owner_terminal or os.name != "posix" or not shell:
        sentinel = probe_root / ".easydep-owner-preflight"
        try:
            sentinel.write_text("probe\n", encoding="utf-8")
            if sentinel.read_text(encoding="utf-8") != "probe\n":
                raise OSError("workspace read-back did not match")
            sentinel.unlink()
        except OSError as error:
            raise RuntimeError(
                f"ENV_WORKSPACE_PERMISSION: cannot write assigned workspace: {probe_root}"
            ) from error
        return {
            "schemaVersion": "easydep-owner-workspace-preflight/v1",
            "passed": True,
            "mode": (
                "coordinator-editor"
                if not requires_owner_terminal
                else "coordinator-host"
            ),
            "workspace": str(resolved),
            "logicalWorkspace": str(logical),
            "pipefailPassed": None,
            "ownerIdentity": None,
            "coordinatorIdentity": None,
            "assignedPathsContained": True,
            "outsideWorkspaceRejected": True,
            "immutableWritesRejectedByExecutor": enforce_write_scope,
        }

    logical = logical_workspace or prepare_owner_workspace_alias(resolved)
    if not logical.is_symlink() or logical.resolve() != resolved:
        raise RuntimeError("ENV_WORKSPACE_PERMISSION: logical workspace mapping is invalid")
    relative_probe = probe_root.relative_to(resolved)
    logical_probe = logical / relative_probe
    command = (
        "set -o pipefail; "
        'test "$(pwd -P)" = "$(readlink -f "' + str(logical) + '")"; '
        'printf "probe\\n" > "' + str(logical_probe / ".easydep-owner-preflight") + '"; '
        'test "$(cat "' + str(logical_probe / ".easydep-owner-preflight") + '")" = probe; '
        'rm -f "' + str(logical_probe / ".easydep-owner-preflight") + '"; '
        "set +e; (exit 7) | cat >/dev/null; status=$?; set -e; test \"$status\" -eq 7; "
        'test -w "' + OWNER_TERMINAL_HOME + '"; '
        'test -w "' + OWNER_NPM_CACHE + '"; '
        'test -w "' + OWNER_GRADLE_CACHE + '"; '
        'test "$(id -u)" -ne 0; printf "%s:%s" "$(id -u)" "$(id -g)"'
    )
    result = subprocess.run(
        [shell, "-c", command],
        cwd=logical,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "owner shell probe failed"
        raise RuntimeError(f"ENV_WORKSPACE_PERMISSION: {detail}")

    return {
        "schemaVersion": "easydep-owner-workspace-preflight/v1",
        "passed": True,
        "mode": "fixed-linux-owner",
        "workspace": str(resolved),
        "logicalWorkspace": str(logical),
        "pipefailPassed": True,
        "ownerIdentity": {
            "name": os.environ.get(OWNER_TERMINAL_USER_ENV),
            "uidGid": result.stdout.strip(),
        },
        "coordinatorIdentity": {"uid": os.geteuid(), "gid": os.getegid()},
        "assignedPathsContained": True,
        "outsideWorkspaceRejected": True,
        "immutableWritesRejectedByExecutor": enforce_write_scope,
    }


def _restore_coordinator_access(sandbox: Path) -> None:
    """Reopen a stopped owner's sandbox before the coordinator refreshes it."""

    user_name = os.environ.get(OWNER_TERMINAL_USER_ENV, "").strip()
    if os.name != "posix" or not user_name or os.geteuid() != 0:
        return
    for directory, children, files in os.walk(sandbox.resolve()):
        children[:] = [name for name in children if name not in _IGNORED_WORKSPACE_PARTS]
        root = Path(directory)
        os.chown(root, 0, 0)
        root.chmod(0o755)
        for name in files:
            target = root / name
            if target.is_symlink():
                continue
            os.chown(target, 0, 0)
            target.chmod(0o644)


def _apply_fixed_runner_permissions(
    run_root: Path,
    sandbox: Path,
    task: dict[str, object],
) -> None:
    """Hand the disposable sandbox to the owner while control state stays root-only.

    The fixed runner starts the coordinator as root and its standard terminal as a
    dedicated unprivileged user.  File selection and contract guards belong to
    promotion/editor logic, not Linux modes inside the disposable candidate tree.
    Linux permissions only separate that whole tree from the original job, workflow
    checkpoint, conversation state, and frozen inputs.
    """

    user_name = os.environ.get(OWNER_TERMINAL_USER_ENV, "").strip()
    control_value = os.environ.get(OWNER_CONTROL_ROOT_ENV, "").strip()
    if os.name != "posix" or not user_name or not control_value:
        return
    if os.geteuid() != 0:
        raise RuntimeError("Owner workspace permissions require a root coordinator.")

    import pwd

    account = pwd.getpwnam(user_name)
    control_root = Path(control_value).resolve()
    resolved_run = run_root.resolve()
    resolved_sandbox = sandbox.resolve()
    if (
        control_root != resolved_run
        and control_root not in resolved_run.parents
    ) or control_root not in resolved_sandbox.parents:
        raise ValueError("Owner workspace is outside the fixed runner control root.")

    # The owner must be able to use ordinary editor and build tooling anywhere in
    # its disposable candidate.  This deliberately includes copied generated
    # contracts, package caches, and build output directories.
    _set_tree_permissions(
        resolved_sandbox,
        account.pw_uid,
        account.pw_gid,
        directory_mode=0o755,
        file_mode=0o644,
        # These directories are created by the owner itself and are not touched
        # by refresh. Walking node_modules/.gradle again on every checkpoint
        # resume is both unnecessary and very expensive on Docker Desktop.
        excluded_directory_names=_IGNORED_WORKSPACE_PARTS,
    )

    _harden_control_tree(control_root, resolved_sandbox)


def grant_owner_file_access(path: Path, sandbox: Path) -> None:
    """Hand a file-editor result back to the unprivileged owner shell."""

    user_name = os.environ.get(OWNER_TERMINAL_USER_ENV, "").strip()
    if os.name != "posix" or not user_name or os.geteuid() != 0 or not path.exists():
        return

    import pwd

    account = pwd.getpwnam(user_name)
    resolved = path.resolve()
    boundary = sandbox.resolve()
    if resolved != boundary and boundary not in resolved.parents:
        return

    current = resolved
    while True:
        if not current.is_symlink():
            os.chown(current, account.pw_uid, account.pw_gid)
            current.chmod(0o755 if current.is_dir() else 0o644)
        if current == boundary:
            break
        current = current.parent


def _set_tree_permissions(
    root: Path,
    uid: int,
    gid: int,
    *,
    directory_mode: int,
    file_mode: int,
    excluded_directory_names: set[str] | None = None,
) -> None:
    excluded = excluded_directory_names or set()
    for directory, children, files in os.walk(root):
        children[:] = [name for name in children if name not in excluded]
        current = Path(directory)
        if not current.is_symlink():
            os.chown(current, uid, gid)
            current.chmod(directory_mode)
        for name in files:
            path = current / name
            if path.is_symlink():
                continue
            os.chown(path, uid, gid)
            path.chmod(file_mode)


def _harden_control_tree(control_root: Path, sandbox: Path) -> None:
    """Make every current-job path outside the owner sandbox root-only."""

    resolved_control = control_root.resolve()
    resolved_sandbox = sandbox.resolve()
    for directory, children, files in os.walk(resolved_control):
        current = Path(directory).resolve()
        if current == resolved_sandbox or resolved_sandbox in current.parents:
            children.clear()
            continue
        children[:] = [
            name
            for name in children
            if (current / name).resolve() != resolved_sandbox
            and resolved_sandbox not in (current / name).resolve().parents
        ]
        if not current.is_symlink():
            os.chown(current, 0, 0)
            current.chmod(0o700)
        for name in files:
            path = current / name
            if path.is_symlink():
                continue
            os.chown(path, 0, 0)
            path.chmod(0o600)

    # The shell needs execute-only traversal through these parents, but cannot
    # list them.  The sandbox itself and its contents retain the modes above.
    ancestor = resolved_sandbox.parent
    while ancestor != resolved_control:
        ancestor.chmod(0o711)
        ancestor = ancestor.parent
    resolved_control.chmod(0o711)


def _copy_read_sources(
    run_root: Path,
    sandbox: Path,
    task: dict[str, object],
) -> None:
    """Copy explicitly named read-only evidence that lives outside application source."""

    context_file = task.get("context_file")
    if not isinstance(context_file, str):
        return
    context_path = (run_root / context_file).resolve()
    run_root = run_root.resolve()
    resolved_sandbox = sandbox.resolve()
    context_target = (resolved_sandbox / context_file).resolve()
    if (
        run_root not in context_path.parents
        or resolved_sandbox not in context_target.parents
        or not context_path.is_file()
    ):
        return
    _copy_file_if_changed(context_path, context_target)
    context = json.loads(context_path.read_text(encoding="utf-8"))
    values = [
        *(context.get("readSourcePaths") or []),
        *(context.get("availableReadPaths") or []),
    ]
    for value in values:
        if not isinstance(value, str):
            continue
        relative = Path(value.replace("\\", "/"))
        if (
            relative.parts
            and relative.parts[0] == "application"
            and not any(part in _IGNORED_WORKSPACE_PARTS for part in relative.parts[1:])
        ):
            # Application source is already synchronized by
            # _refresh_agent_workspace. Recopying it here turns every resume
            # into an unnecessary bind-mounted filesystem write.
            continue
        source = (run_root / value).resolve()
        target = (resolved_sandbox / value).resolve()
        if (
            run_root not in source.parents
            or resolved_sandbox not in target.parents
            or not source.is_file()
        ):
            continue
        _copy_file_if_changed(source, target)


def _copy_file_if_changed(
    source: Path,
    target: Path,
    *,
    verify_content_if_metadata_equal: bool = False,
) -> None:
    """Copy a source file only when metadata (and, when required, bytes) differ."""

    if (
        source.is_file()
        and not source.is_symlink()
        and target.is_file()
        and not target.is_symlink()
    ):
        source_stat = source.stat()
        target_stat = target.stat()
        if (
            source_stat.st_size == target_stat.st_size
            and source_stat.st_mtime_ns == target_stat.st_mtime_ns
        ):
            # A stopped owner can deliberately or accidentally preserve the
            # canonical timestamp after changing equal-sized source. At a
            # different-task handoff, bytes are the trust boundary.
            if (
                not verify_content_if_metadata_equal
                or source.read_bytes() == target.read_bytes()
            ):
                return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _refresh_agent_workspace(
    run_root: Path,
    source_application: Path,
    sandbox: Path,
    sandbox_application: Path,
    editable: set[str],
    editable_roots: set[str],
    immutable: set[str],
    *,
    preserve_failed_edits: bool,
    verify_content_if_discarding_candidate: bool = False,
) -> None:
    """선택에 따라 미완성 편집을 보존하거나 승인된 run source로 되돌린다."""
    source_files: set[str] = set()
    for source in _iter_workspace_source_files(source_application):
        relative_application = source.relative_to(source_application)
        if _is_ignored_workspace_path(relative_application):
            continue
        relative_run = (Path("application") / relative_application).as_posix()
        source_files.add(relative_run)
        target = sandbox / relative_run
        if (
            preserve_failed_edits
            and path_is_editable(
                relative_run,
                editable,
                editable_roots,
                immutable,
            )
            and target.is_file()
        ):
            continue
        _copy_file_if_changed(
            source,
            target,
            verify_content_if_metadata_equal=verify_content_if_discarding_candidate,
        )

    for target in _iter_workspace_source_files(sandbox_application):
        relative_application = target.relative_to(sandbox_application)
        # Gradle/npm 산출물과 package cache는 source 동기화 대상이 아니다. 이전 검증이
        # 만든 파일을 여기서 지우면 증분 build 이점을 잃을 뿐 아니라, Windows에서는
        # 종료 중인 test worker가 output.bin을 잠시 잡고 있어 WinError 32가 발생한다.
        # Disposable workspaces are cleaned after success; the shared owner
        # candidate intentionally retains these outputs for the next task.
        if _is_ignored_workspace_path(relative_application):
            continue
        relative_run = target.relative_to(sandbox).as_posix()
        editable_extra = path_is_editable(
            relative_run,
            editable,
            editable_roots,
            immutable,
        )
        if relative_run not in source_files and (
            not preserve_failed_edits or not editable_extra
        ):
            target.unlink()


def path_is_editable(
    relative_path: str,
    allowed_files: set[str] | list[str],
    allowed_roots: set[str] | list[str],
    immutable_paths: set[str] | list[str],
) -> bool:
    """상대 경로가 쓰기 범위 안이고 읽기 전용 계약 밖인지 확인한다."""
    path = relative_path.replace("\\", "/").strip("/")
    immutable = {
        str(item).replace("\\", "/").strip("/") for item in immutable_paths
    }
    if any(path == root or path.startswith(root + "/") for root in immutable):
        return False
    files = {
        str(item).replace("\\", "/").strip("/") for item in allowed_files
    }
    if path in files:
        return True
    roots = {
        str(item).replace("\\", "/").strip("/") for item in allowed_roots
    }
    return any(path == root or path.startswith(root + "/") for root in roots)


def cleanup_agent_workspace(sandbox: Path, *, run_root: Path | None = None) -> None:
    """성공한 작업의 임시 공간만 안전하게 삭제한다."""
    expected_roots = {
        (Path(tempfile.gettempdir()) / "easydep-agent-workspaces").resolve(),
        (Path(tempfile.gettempdir()) / "easydep-owner-workspaces").resolve(),
    }
    if run_root is not None:
        control_value = os.environ.get(OWNER_CONTROL_ROOT_ENV, "").strip()
        if control_value:
            expected_roots.add((Path(control_value) / "w").resolve())
    resolved = sandbox.resolve()
    if not any(expected_root in resolved.parents for expected_root in expected_roots):
        raise ValueError(f"Refusing to remove a non-agent workspace: {resolved}")
    if resolved.exists():
        try:
            shutil.rmtree(resolved, onerror=remove_readonly)
            _shared_owner_task_marker(resolved).unlink(missing_ok=True)
        except OSError:
            # OpenHands가 닫힌 직후 Windows가 파일 handle을 잠깐 유지할 수 있다. 이 경우
            # 구현 성공을 실패로 바꾸지 않고 다음 정리 때 다시 제거한다.
            return


def snapshot_files(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in _iter_workspace_source_files(root):
        relative = path.relative_to(root)
        if path.name.endswith(".tsbuildinfo"):
            continue
        result[str(relative).replace("\\", "/")] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return result


def changed_files(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    }
