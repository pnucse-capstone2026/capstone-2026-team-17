"""A small, offline diagnostic for replaying one implementation owner task."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ..agents.runtime import OWNER_TASK_TYPES
from ..agents.workspace import load_strict_task, prompt_file_sha256
from ..application.prototype import PrototypeClient
from ..generation.orchestrator import load_job
from ..runtime.linux_runner_transport import remove_owner_workspace_volume
from ..workflows.coordinator import materialize_owner_tasks


class OwnerReplayError(ValueError):
    """The saved job or requested owner task is not replayable."""


_SENSITIVE = re.compile(
    r"(secret|token|password|api[_-]?key|authorization|credential)", re.IGNORECASE
)


def _hash_prompt(task: dict[str, Any], root: Path) -> str:
    value = task.get("prompt_sha256") or task.get("promptSha256")
    if value:
        return str(value)
    prompt = root / str(task.get("prompt_file") or task.get("promptFile") or "")
    return prompt_file_sha256(prompt) if prompt.is_file() else ""


def _sanitized(value: Any, key: str = "") -> Any:
    if isinstance(value, str) and _SENSITIVE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _sanitized(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitized(item, key) for item in value]
    if isinstance(value, str) and len(value) > 20000:
        return value[:20000] + "...[TRUNCATED]"
    return value


_RUN_PATH_FIELDS = {
    "context_file",
    "conversationcheckpoint",
    "eventjournal",
    "prompt_file",
    "resultfile",
    "source_artifacts",
    "sourceartifacts",
    "taskfile",
}

_DROPPED_PATH_FIELDS = {"conversationcheckpoint", "source_artifacts", "sourceartifacts"}


def _run_path(run_root: Path, relative: str) -> Path | None:
    candidate = (run_root / relative).resolve()
    if candidate != run_root and run_root not in candidate.parents:
        return None
    return candidate


def _copy_evidence(source: Path, destination: Path) -> None:
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return
    if not source.is_dir():
        return
    for child in source.rglob("*"):
        if not child.is_file():
            continue
        resolved = child.resolve()
        if source not in resolved.parents:
            continue
        target = destination / resolved.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolved, target)


def _copy_unit_test_subjects(
    task: dict[str, Any], *, old_run_root: Path, new_run_root: Path
) -> list[dict[str, Any]]:
    """Seed a diagnostic unit replay with its explicitly declared completed subject."""
    if task.get("task_type") not in {"backend-unit-test", "frontend-unit-test"}:
        return []
    profile = task.get("verification_profile")
    subjects = profile.get("unitTestSubjectPaths") if isinstance(profile, dict) else None
    dependencies = task.get("depends_on")
    if not isinstance(subjects, list) or not subjects or not isinstance(dependencies, list) or len(dependencies) != 1:
        raise OwnerReplayError("Unit replay requires declared subject paths and one implementation dependency")

    manifest_path = old_run_root / "reports" / "run-manifest.json"
    state_path = old_run_root / "reports" / "workflow-state.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OwnerReplayError("Unit replay requires the saved implementation manifest and workflow state") from exc
    parent_id = dependencies[0]
    planned = manifest.get("implementation_tasks")
    parent_tasks = [
        item for item in planned if isinstance(item, dict) and item.get("task_id") == parent_id
    ] if isinstance(planned, list) else []
    states = state.get("tasks")
    parent_states = [
        item for item in states if isinstance(item, dict) and item.get("task_id") == parent_id
    ] if isinstance(states, list) else []
    if len(parent_tasks) != 1 or len(parent_states) != 1 or parent_states[0].get("status") != "SUCCEEDED":
        raise OwnerReplayError("Unit replay subject owner is not a uniquely declared completed task")

    allowed = parent_tasks[0].get("allowed_write_paths")
    output_hashes = parent_states[0].get("outputHashes")
    if not isinstance(allowed, list):
        raise OwnerReplayError("Unit replay subject owner has no declared write paths")
    if not isinstance(output_hashes, dict):
        raise OwnerReplayError("Unit replay subject owner has no saved output hashes")
    copied: list[dict[str, Any]] = []
    for raw in subjects:
        if not isinstance(raw, str) or not raw or Path(raw).is_absolute() or ".." in Path(raw).parts:
            raise OwnerReplayError("Unit replay subject path must be a safe run-relative path")
        relative = Path(raw).as_posix()
        if relative not in allowed:
            raise OwnerReplayError("Unit replay subject is outside its declared implementation owner scope")
        source = _run_path(old_run_root, relative)
        destination = _run_path(new_run_root, relative)
        if source is None or destination is None or not source.is_file():
            raise OwnerReplayError(f"Completed unit replay subject is missing: {relative}")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if output_hashes.get(relative) != digest:
            raise OwnerReplayError(f"Completed unit replay subject hash does not match workflow state: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        copied.append({"path": relative, "bytes": source.stat().st_size, "sha256": digest})
    return copied


def _stable_reference(
    value: str,
    *,
    run_root: Path,
    output_root: Path,
    copied_roots: dict[Path, Path],
) -> str | None:
    source = Path(value)
    if not source.is_absolute():
        source = run_root / source
    source = source.resolve()
    for original, copied in copied_roots.items():
        if source == original or original in source.parents:
            return (copied / source.relative_to(original)).relative_to(output_root).as_posix()
    if source == run_root or run_root in source.parents:
        return None
    return value


def _replace_run_paths(
    value: Any,
    *,
    run_root: Path,
    output_root: Path,
    copied_roots: dict[Path, Path],
    key: str = "",
) -> Any:
    if isinstance(value, dict):
        replaced: dict[str, Any] = {}
        for raw_key, item in value.items():
            field = str(raw_key)
            if field.lower() in _DROPPED_PATH_FIELDS:
                continue
            updated = _replace_run_paths(
                item,
                run_root=run_root,
                output_root=output_root,
                copied_roots=copied_roots,
                key=field,
            )
            if updated is not None or field.lower() not in _RUN_PATH_FIELDS:
                replaced[field] = updated
        return replaced
    if isinstance(value, list):
        replaced = [
            _replace_run_paths(
                item,
                run_root=run_root,
                output_root=output_root,
                copied_roots=copied_roots,
                key=key,
            )
            for item in value
        ]
        return [item for item in replaced if item is not None] if key.lower() in _RUN_PATH_FIELDS else replaced
    if not isinstance(value, str):
        return value
    if key.lower() in _RUN_PATH_FIELDS:
        return _stable_reference(
            value,
            run_root=run_root,
            output_root=output_root,
            copied_roots=copied_roots,
        )
    return value.replace(str(run_root), "[REMOVED TEMPORARY RUN]")


def _rewrite_jsonl_evidence(
    path: Path,
    *,
    run_root: Path,
    output_root: Path,
    copied_roots: dict[Path, Path],
) -> None:
    temporary_name: str | None = None
    try:
        with path.open(encoding="utf-8") as source, tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            for line in source:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    temporary.write(line)
                    continue
                temporary.write(
                    json.dumps(
                        _replace_run_paths(
                            _sanitized(event),
                            run_root=run_root,
                            output_root=output_root,
                            copied_roots=copied_roots,
                        ),
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _preserve_replay_evidence(
    run_root: Path,
    task: dict[str, Any],
    *,
    output_root: Path,
    evidence_root: Path,
) -> tuple[dict[str, Any], dict[Path, Path]]:
    """Copy the selected task evidence before the disposable run is removed."""

    copied_roots: dict[Path, Path] = {}
    files: dict[str, Any] = {}

    def copy_file(relative: str, destination: Path, label: str) -> None:
        source = _run_path(run_root, relative)
        if source is None or not source.is_file():
            return
        _copy_evidence(source, destination)
        copied_roots[source] = destination
        files[label] = destination.relative_to(output_root).as_posix()

    task_id = str(task["task_id"])
    sidecar = _run_path(run_root, f"reports/implementation-tasks/{task_id}.task.json")
    if sidecar is not None and sidecar.is_file():
        copy_file(
            sidecar.relative_to(run_root).as_posix(),
            evidence_root / "task" / "task.json",
            "task",
        )
    else:
        snapshot = evidence_root / "task" / "task.json"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        files["task"] = snapshot.relative_to(output_root).as_posix()
    for field, name in (("prompt_file", "prompt"), ("context_file", "context")):
        relative = task.get(field)
        if isinstance(relative, str):
            copy_file(relative, evidence_root / "task" / f"{name}{Path(relative).suffix}", name)

    if task.get("task_type") in {"backend-unit-test", "frontend-unit-test"}:
        profile = task.get("verification_profile")
        for field, label in (
            ((profile or {}).get("unitTestSubjectPaths", []) if isinstance(profile, dict) else [], "unitSubjects"),
            (task.get("required_test_paths", []), "unitTests"),
        ):
            if not isinstance(field, list):
                continue
            for index, relative in enumerate(field, start=1):
                if isinstance(relative, str):
                    source = _run_path(run_root, relative)
                    if source is not None and source.is_file():
                        copy_file(
                            relative,
                            evidence_root / "task-output" / label / f"{index:02d}-{source.name}",
                            f"{label}{index}",
                        )

    execution_dir = run_root / "reports" / "agent-executions"
    copied_execution: list[str] = []

    def copy_execution(source: Path) -> None:
        destination = evidence_root / "agent-executions" / source.name
        _copy_evidence(source, destination)
        copied_roots[source.resolve()] = destination
        copied_execution.append(destination.relative_to(output_root).as_posix())

    latest_result: dict[str, Any] = {}
    if execution_dir.is_dir():
        latest = execution_dir / f"{task_id}.result.json"
        if latest.is_file():
            copy_execution(latest)
            try:
                value = json.loads(latest.read_text(encoding="utf-8"))
                latest_result = value if isinstance(value, dict) else {}
            except (OSError, json.JSONDecodeError):
                pass
        for source in sorted(execution_dir.iterdir()):
            if source.is_file() and re.fullmatch(
                rf"{re.escape(task_id)}\.attempt-\d+\.(?:result\.json|events\.jsonl)",
                source.name,
            ):
                copy_execution(source)
        if task.get("task_type") == "frontend-unit-test":
            vitest_report = execution_dir / f"{task_id}.vitest.json"
            if vitest_report.is_file():
                copy_execution(vitest_report)
        verification_evidence = latest_result.get("verificationEvidence")
        frozen_candidate = latest_result.get("frozenTestCandidate")
        if not isinstance(frozen_candidate, dict) and isinstance(verification_evidence, dict):
            frozen_candidate = verification_evidence.get("frozenTestCandidate")
        if isinstance(frozen_candidate, dict):
            candidate_path = frozen_candidate.get("path")
            if isinstance(candidate_path, str):
                candidate = _run_path(run_root, candidate_path)
                if candidate is not None and candidate.is_file():
                    copy_execution(candidate)
        journal = latest_result.get("eventJournal")
        if isinstance(journal, str):
            source = _run_path(run_root, journal)
            if source is not None and source.is_file():
                copy_execution(source)
    if copied_execution:
        files["agentExecutions"] = copied_execution

    for result_path in evidence_root.rglob("*"):
        if not result_path.is_file() or result_path.suffix not in {".json", ".jsonl"}:
            continue
        if result_path.suffix == ".jsonl":
            _rewrite_jsonl_evidence(
                result_path,
                run_root=run_root,
                output_root=output_root,
                copied_roots=copied_roots,
            )
            continue
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rewritten = _replace_run_paths(
            _sanitized(result),
            run_root=run_root,
            output_root=output_root,
            copied_roots=copied_roots,
        )
        result_path.write_text(
            json.dumps(rewritten, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return files, copied_roots


def _validate_saved_identity(job_path: Path, old_run_root: Path, task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not job_path.is_file() or not old_run_root.is_dir() or not task_id.strip():
        raise OwnerReplayError("Saved job, run root, and task ID are required")
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OwnerReplayError("Saved job.json is invalid") from exc
    if not isinstance(job, dict) or not job.get("appId"):
        raise OwnerReplayError("Saved job identity is incomplete")
    manifest_path = old_run_root / "reports" / "run-manifest.json"
    if not manifest_path.is_file():
        raise OwnerReplayError("Saved run manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if str(manifest.get("app_id") or manifest.get("appId") or "") != str(job["appId"]):
        raise OwnerReplayError("Saved job and run app identity do not match")
    try:
        old_task = load_strict_task(old_run_root, task_id, allowed_task_types=OWNER_TASK_TYPES)
    except ValueError as exc:
        raise OwnerReplayError(str(exc)) from exc
    return job, old_task


def _clone_job(
    job: dict[str, Any],
    job_path: Path,
    work_root: Path,
    repository_root: Path,
) -> Path:
    old_workspace = Path(str(job.get("workspaceRoot") or job_path.parent)).resolve()
    context = work_root / "design-context"
    context.mkdir(parents=True)
    inputs: dict[str, str] = {}
    for name, raw in dict(job.get("inputs") or {}).items():
        source = Path(str(raw))
        if not source.is_absolute():
            source = old_workspace / source
        source = source.resolve()
        try:
            source.relative_to(old_workspace)
        except ValueError as exc:
            raise OwnerReplayError(f"Saved design input escapes workspaceRoot: {name}") from exc
        if not source.is_file():
            raise OwnerReplayError(f"Saved design input is missing: {name}")
        target = context / f"{len(inputs):03d}-{source.name}"
        shutil.copyfile(source, target)
        inputs[str(name)] = target.relative_to(repository_root).as_posix()
    cloned = {
        "name": f"{job.get('name', 'easydep')}-owner-replay-{work_root.name[-8:]}",
        "appId": job["appId"],
        "workspaceRoot": str(repository_root),
        "inputs": inputs,
        "requiredInputs": [str(item) for item in job.get("requiredInputs", [])],
        "outputRoot": (work_root / "generated" / "runs").relative_to(repository_root).as_posix(),
        "generation": dict(job.get("generation") or {}),
        "verification": dict(job.get("verification") or {}),
        "agent": dict(job.get("agent") or {}),
        "progressPath": (work_root / "generation-progress.json").relative_to(repository_root).as_posix(),
    }
    path = work_root / "job.json"
    path.write_text(json.dumps(cloned, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def replay_owner_task(
    client: PrototypeClient,
    *,
    job_path: Path,
    old_run_root: Path,
    task_id: str,
    output_root: Path,
    fresh_task_id: str | None = None,
) -> Path:
    """Freshly generate and plan a single owner task, then execute it once.

    The returned result is outside the temporary diagnostic directory, which is removed in
    all cases. No workflow execution, repair, persistence, or public action is involved.
    """
    job, old_task = _validate_saved_identity(job_path.resolve(), old_run_root.resolve(), task_id)
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    diagnostic_root: Path | None = None
    result_path: Path | None = None
    new_run_root: Path | None = None
    new_job_path: Path | None = None
    repository_root: Path | None = None
    owner_execution_started = False
    selected: dict[str, Any] | None = None
    evidence_root: Path | None = None
    copied_roots: dict[Path, Path] = {}
    primary_error: Exception | None = None
    interrupted = False
    selected_task_id = fresh_task_id or task_id
    payload: dict[str, Any] = {
        "schemaVersion": "owner-replay-result/v1alpha1",
        "taskId": selected_task_id,
        **({"baselineTaskId": task_id} if selected_task_id != task_id else {}),
        "oldPromptSha256": _hash_prompt(old_task, old_run_root),
        "status": "FAILED",
    }
    try:
        settings_root = client.settings.work_root.resolve()
        repository_root = client.settings.repository_root.resolve()
        diagnostic_root = settings_root / f"owner-replay-{uuid.uuid4().hex}"
        try:
            diagnostic_root.relative_to(settings_root)
            diagnostic_root.relative_to(repository_root)
        except ValueError as exc:
            raise OwnerReplayError(
                "Diagnostic work root must be inside the implementation work and repository roots"
            ) from exc
        result_path = output_root / f"owner-replay-result-{diagnostic_root.name.rsplit('-', 1)[-1]}.json"
        try:
            result_path.relative_to(diagnostic_root)
        except ValueError:
            pass
        else:
            raise OwnerReplayError("Diagnostic result must be outside the work root")
        new_job_path = _clone_job(job, job_path, diagnostic_root, repository_root)
        new_run_root = client.generate(new_job_path).resolve()
        if diagnostic_root not in new_run_root.parents:
            raise OwnerReplayError("Generated diagnostic run escaped work root")
        unit_subject_sources = _copy_unit_test_subjects(
            old_task, old_run_root=old_run_root.resolve(), new_run_root=new_run_root
        )
        spec = load_job(new_job_path)
        fresh = materialize_owner_tasks(new_run_root, spec)
        fresh = [task for task in fresh if task.get("task_id") == selected_task_id]
        if len(fresh) != 1:
            raise OwnerReplayError(
                f"Fresh plan must contain exactly one task {selected_task_id!r}"
            )
        selected = fresh[0]
        if selected.get("task_type") not in OWNER_TASK_TYPES:
            raise OwnerReplayError(
                f"Fresh task {selected_task_id!r} is not an owner task"
            )
        if selected.get("task_type") in {"backend-unit-test", "frontend-unit-test"}:
            original_profile = old_task.get("verification_profile")
            selected_profile = selected.get("verification_profile")
            if (
                old_task.get("task_type") != selected.get("task_type")
                or not isinstance(original_profile, dict)
                or not isinstance(selected_profile, dict)
                or original_profile.get("unitTestSubjectPaths") != selected_profile.get("unitTestSubjectPaths")
                or old_task.get("depends_on") != selected.get("depends_on")
                or not unit_subject_sources
            ):
                raise OwnerReplayError("Fresh unit task does not match the saved subject-owner contract")
        payload.update({
            "newPromptSha256": _hash_prompt(selected, new_run_root),
            "task": _sanitized(selected),
            "planning": {"materialized": True},
            **({"unitTestSubjectSources": unit_subject_sources} if unit_subject_sources else {}),
        })
        owner_execution_started = True
        execution = client.run_owner(new_run_root, new_job_path, selected_task_id)
        payload.update({"status": "SUCCEEDED", "execution": _sanitized(execution)})
        return result_path
    except KeyboardInterrupt:
        interrupted = True
        payload.update({"status": "INTERRUPTED", "error": "Interrupted"})
        raise
    except Exception as exc:
        primary_error = exc
        payload["error"] = str(exc)
        raise
    finally:
        preservation_error: Exception | None = None
        if new_run_root is not None and selected is not None and result_path is not None:
            evidence_root = output_root / f"owner-replay-evidence-{result_path.stem.rsplit('-', 1)[-1]}"
            try:
                evidence, copied_roots = _preserve_replay_evidence(
                    new_run_root,
                    selected,
                    output_root=output_root,
                    evidence_root=evidence_root,
                )
                payload["evidence"] = {
                    "root": evidence_root.relative_to(output_root).as_posix(),
                    **evidence,
                }
            except Exception as exc:
                preservation_error = exc
                if primary_error is None and not interrupted:
                    payload.update({"status": "FAILED", "error": str(exc)})
        cleanup_error: Exception | None = None
        if (
            owner_execution_started
            and new_run_root is not None
            and new_job_path is not None
            and repository_root is not None
        ):
            try:
                if not remove_owner_workspace_volume(
                    new_run_root, new_job_path.parent, repository_root
                ):
                    raise OwnerReplayError("Owner workspace volume cleanup failed")
            except Exception as exc:
                cleanup_error = exc
                payload["cleanupError"] = str(exc)
        try:
            if diagnostic_root is not None and diagnostic_root.exists():
                shutil.rmtree(diagnostic_root)
        except Exception as exc:
            cleanup_error = cleanup_error or exc
            payload["cleanupError"] = str(exc)
        result_error: Exception | None = None
        try:
            if result_path is not None:
                result_path.write_text(
                    json.dumps(
                        _replace_run_paths(
                            _sanitized(payload),
                            run_root=new_run_root,
                            output_root=output_root,
                            copied_roots=copied_roots,
                        )
                        if new_run_root is not None
                        else _sanitized(payload),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
        except Exception as exc:
            result_error = exc
        if primary_error is None and not interrupted:
            if preservation_error is not None:
                raise preservation_error
            if result_error is not None:
                raise result_error
            if cleanup_error is not None:
                raise cleanup_error


def _validate_saved_job(job_path: Path, old_run_root: Path) -> dict[str, Any]:
    """Validate replay inputs when fresh operation IDs have no old-task equivalent."""
    if not job_path.is_file() or not old_run_root.is_dir():
        raise OwnerReplayError("Saved job and run root are required")
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OwnerReplayError("Saved job.json is invalid") from exc
    if not isinstance(job, dict) or not job.get("appId"):
        raise OwnerReplayError("Saved job identity is incomplete")
    manifest_path = old_run_root / "reports" / "run-manifest.json"
    if not manifest_path.is_file():
        raise OwnerReplayError("Saved run manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if str(manifest.get("app_id") or manifest.get("appId") or "") != str(job["appId"]):
        raise OwnerReplayError("Saved job and run app identity do not match")
    return job


def _task_output_hashes(run_root: Path, task: dict[str, Any]) -> dict[str, str | None]:
    paths = task.get("required_output_paths", task.get("allowed_write_paths", []))
    if not isinstance(paths, list):
        return {}
    hashes: dict[str, str | None] = {}
    for relative in paths:
        if not isinstance(relative, str):
            continue
        source = _run_path(run_root, relative)
        hashes[relative] = (
            hashlib.sha256(source.read_bytes()).hexdigest()
            if source is not None and source.is_file()
            else None
        )
    return hashes


def _snapshot_task_outputs(
    run_root: Path,
    task: dict[str, Any],
    *,
    output_root: Path,
    evidence_root: Path,
    step: int,
) -> list[str]:
    """Persist source state immediately after one owner, before its successor runs."""
    paths = task.get("required_output_paths", task.get("allowed_write_paths", []))
    if not isinstance(paths, list):
        return []
    snapshots: list[str] = []
    # Operation task IDs can be very long.  Keep persistent evidence paths short
    # enough for Windows while the result payload retains the full task identity.
    destination_root = evidence_root / "steps" / f"{step:02d}"
    for relative in paths:
        if not isinstance(relative, str):
            continue
        source = _run_path(run_root, relative)
        if source is None or not source.is_file():
            continue
        destination = destination_root / f"{len(snapshots):02d}-{source.name}"
        _copy_evidence(source, destination)
        snapshots.append(destination.relative_to(output_root).as_posix())
    return snapshots


def replay_owner_tasks_sequentially(
    client: PrototypeClient,
    *,
    job_path: Path,
    old_run_root: Path,
    task_ids: list[str],
    output_root: Path,
) -> Path:
    """Execute an explicitly ordered fresh-owner sequence in one disposable run.

    This diagnostic intentionally does not call the product workflow: it materializes once,
    validates the requested dependency chain, and uses the narrow owner transport for each
    already-planned task.  Per-step writable-source fingerprints make the handoff auditable.
    """
    if not task_ids or any(not task_id.strip() for task_id in task_ids):
        raise OwnerReplayError("At least one non-empty fresh task ID is required")
    if len(set(task_ids)) != len(task_ids):
        raise OwnerReplayError("Fresh task IDs must be unique")
    job = _validate_saved_job(job_path.resolve(), old_run_root.resolve())
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    diagnostic_root: Path | None = None
    result_path: Path | None = None
    new_run_root: Path | None = None
    new_job_path: Path | None = None
    repository_root: Path | None = None
    selected: list[dict[str, Any]] = []
    evidence_root: Path | None = None
    copied_roots: dict[Path, Path] = {}
    primary_error: Exception | None = None
    interrupted = False
    owner_execution_started = False
    payload: dict[str, Any] = {
        "schemaVersion": "owner-sequence-replay-result/v1alpha1",
        "taskIds": task_ids,
        "status": "FAILED",
        "steps": [],
    }
    try:
        settings_root = client.settings.work_root.resolve()
        repository_root = client.settings.repository_root.resolve()
        diagnostic_root = settings_root / f"owner-sequence-replay-{uuid.uuid4().hex}"
        try:
            diagnostic_root.relative_to(settings_root)
            diagnostic_root.relative_to(repository_root)
        except ValueError as exc:
            raise OwnerReplayError(
                "Diagnostic work root must be inside the implementation work and repository roots"
            ) from exc
        result_path = output_root / f"owner-sequence-replay-result-{diagnostic_root.name.rsplit('-', 1)[-1]}.json"
        new_job_path = _clone_job(job, job_path, diagnostic_root, repository_root)
        new_run_root = client.generate(new_job_path).resolve()
        if diagnostic_root not in new_run_root.parents:
            raise OwnerReplayError("Generated diagnostic run escaped work root")
        fresh_by_id = {
            str(task.get("task_id")): task
            for task in materialize_owner_tasks(new_run_root, load_job(new_job_path))
            if isinstance(task, dict)
        }
        missing = [task_id for task_id in task_ids if task_id not in fresh_by_id]
        if missing:
            raise OwnerReplayError(f"Fresh plan is missing requested task(s): {', '.join(missing)}")
        selected = [fresh_by_id[task_id] for task_id in task_ids]
        for task in selected:
            if task.get("task_type") not in OWNER_TASK_TYPES:
                raise OwnerReplayError(f"Fresh task {task['task_id']!r} is not an owner task")
        selected_ids = set(task_ids)
        completed: set[str] = set()
        for task in selected:
            dependencies = [str(item) for item in task.get("depends_on", task.get("dependsOn", []))]
            unsatisfied = [
                dependency
                for dependency in dependencies
                if dependency in selected_ids and dependency not in completed
            ]
            if unsatisfied:
                raise OwnerReplayError(
                    f"Fresh task {task['task_id']!r} is out of dependency order; "
                    f"run first: {', '.join(unsatisfied)}"
                )
            completed.add(str(task["task_id"]))
        payload["planning"] = {
            "materialized": True,
            "tasks": [_sanitized(task) for task in selected],
        }
        evidence_root = output_root / f"owner-sequence-replay-evidence-{result_path.stem.rsplit('-', 1)[-1]}"
        for index, task in enumerate(selected, start=1):
            task_id = str(task["task_id"])
            owner_execution_started = True
            execution = client.run_owner(new_run_root, new_job_path, task_id)
            payload["steps"].append(
                {
                    "taskId": task_id,
                    "execution": _sanitized(execution),
                    "writableSourceHashes": _task_output_hashes(new_run_root, task),
                    "sourceSnapshots": _snapshot_task_outputs(
                        new_run_root,
                        task,
                        output_root=output_root,
                        evidence_root=evidence_root,
                        step=index,
                    ),
                }
            )
        payload["status"] = "SUCCEEDED"
        return result_path
    except KeyboardInterrupt:
        interrupted = True
        payload.update({"status": "INTERRUPTED", "error": "Interrupted"})
        raise
    except Exception as exc:
        primary_error = exc
        payload["error"] = str(exc)
        raise
    finally:
        preservation_error: Exception | None = None
        if new_run_root is not None and selected and result_path is not None:
            evidence_root = evidence_root or output_root / f"owner-sequence-replay-evidence-{result_path.stem.rsplit('-', 1)[-1]}"
            try:
                task_evidence: dict[str, Any] = {}
                for index, task in enumerate(selected, start=1):
                    evidence, copied = _preserve_replay_evidence(
                        new_run_root,
                        task,
                        output_root=output_root,
                        evidence_root=evidence_root / "tasks" / f"{index:02d}",
                    )
                    copied_roots.update(copied)
                    task_evidence[str(task["task_id"])] = evidence
                payload["evidence"] = {
                    "root": evidence_root.relative_to(output_root).as_posix(),
                    "tasks": task_evidence,
                }
            except Exception as exc:
                preservation_error = exc
                if primary_error is None and not interrupted:
                    payload.update({"status": "FAILED", "error": str(exc)})
        cleanup_error: Exception | None = None
        if (
            owner_execution_started
            and new_run_root is not None
            and new_job_path is not None
            and repository_root is not None
        ):
            try:
                if not remove_owner_workspace_volume(
                    new_run_root, new_job_path.parent, repository_root
                ):
                    raise OwnerReplayError("Owner workspace volume cleanup failed")
            except Exception as exc:
                cleanup_error = exc
                payload["cleanupError"] = str(exc)
        try:
            if diagnostic_root is not None and diagnostic_root.exists():
                shutil.rmtree(diagnostic_root)
        except Exception as exc:
            cleanup_error = cleanup_error or exc
            payload["cleanupError"] = str(exc)
        result_error: Exception | None = None
        try:
            if result_path is not None:
                result_path.write_text(
                    json.dumps(
                        _replace_run_paths(
                            _sanitized(payload),
                            run_root=new_run_root,
                            output_root=output_root,
                            copied_roots=copied_roots,
                        )
                        if new_run_root is not None
                        else _sanitized(payload),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
        except Exception as exc:
            result_error = exc
        if primary_error is None and not interrupted:
            if preservation_error is not None:
                raise preservation_error
            if result_error is not None:
                raise result_error
            if cleanup_error is not None:
                raise cleanup_error


run_owner_replay = replay_owner_task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job")
    parser.add_argument("run")
    parser.add_argument("task")
    parser.add_argument("output")
    parser.add_argument("--fresh-task")
    args = parser.parse_args(argv)
    from ..config import ImplementationSettings
    replay_owner_task(
        PrototypeClient(ImplementationSettings.from_env()),
        job_path=Path(args.job),
        old_run_root=Path(args.run),
        task_id=args.task,
        output_root=Path(args.output),
        fresh_task_id=args.fresh_task,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
