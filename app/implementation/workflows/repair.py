"""Route implementation failures back to the owning implementation agent.

Repair ownership comes from structured evidence, the failed task, or declared write roots.
Diagnostic wording is evidence for the owner, never a heuristic routing API.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from app.config import settings
from app.llm_connection import build_llm_connection
from openai import APIError, OpenAI
from pydantic import BaseModel, ValidationError

REPAIR_SCHEMA = "implementation-repair-plan/v4"
REPAIR_PLAN = Path("reports/repair-plan.json")
REPAIR_PROMPT_DIR = Path("reports/implementation-tasks")
REPAIR_PROMPT_HEADING = "## Automatic repair task"
REPAIR_PROMPT_START = "<!-- easydep:repair-directives:start -->"
REPAIR_PROMPT_END = "<!-- easydep:repair-directives:end -->"


class ReviewerProviderError(RuntimeError):
    """A reviewer provider request failed and may be handled by the outer retry."""

    def __init__(self, details: dict[str, object]) -> None:
        super().__init__("unit failure reviewer provider request failed")
        self.details = details
        self.status_code = details.get("status")
        self.failure_kind = (
            "provider_request_validation"
            if self.status_code == 400
            else "provider_request_error"
        )


_REVIEWER_ERROR_TEXT_LIMIT = 12_000
_REVIEWER_SECRET_PATTERNS = re.compile(
    r"(?i)(api[_ -]?key|authorization|bearer|credential|secret|access[_ -]?token|refresh[_ -]?token)"
    r"\s*[:=]?\s*[^\s,;]+"
)


def _reviewer_error_value(error: APIError) -> dict[str, object]:
    """Project only safe, bounded provider error fields for logs and retry signals."""

    candidates: list[object] = [getattr(error, "body", None)]
    response = getattr(error, "response", None)
    if response is not None:
        try:
            candidates.append(response.json())
        except Exception:
            pass

    fields: dict[str, object] = {}
    for candidate in candidates:
        current = candidate
        # SDK bodies and provider gateways may wrap the same error several times.
        for _ in range(5):
            if isinstance(current, str):
                try:
                    current = json.loads(current)
                except (ValueError, TypeError):
                    break
            if not isinstance(current, dict):
                break
            for key in ("message", "failed_generation"):
                value = current.get(key)
                if isinstance(value, str) and key not in fields:
                    fields[key] = value
            nested = current.get("error")
            if isinstance(nested, dict) or isinstance(nested, str):
                current = nested
                continue
            break

    def safe_text(value: object, limit: int) -> str | None:
        if not isinstance(value, str):
            return None
        value = _REVIEWER_SECRET_PATTERNS.sub(r"\1=[REDACTED]", value)
        if len(value) <= limit:
            return value
        edge = limit // 2
        return (
            value[:edge]
            + f"\n...[truncated chars={len(value)} sha256={hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()}]...\n"
            + value[-edge:]
        )

    output: dict[str, object] = {
        "status": getattr(error, "status_code", None),
        "errorType": type(error).__name__,
    }
    message = safe_text(fields.get("message"), 1600)
    generation = safe_text(fields.get("failed_generation"), _REVIEWER_ERROR_TEXT_LIMIT)
    if message:
        output["message"] = message
    if generation:
        output["failed_generation"] = generation
    return output
logger = logging.getLogger(__name__)


class RepairRoutingError(ValueError):
    """Structured repair evidence cannot identify one implementation task."""


class UnitFailureReview(BaseModel):
    """One bounded OSS decision for an executed focused-test failure."""

    classification: Literal["implementation", "test_oracle", "undetermined"]
    rationale: str
    evidence: list[str]
    preserve_assertions: list[str]
    correction_instruction: str


_UNIT_FAILURE_REVIEW_PROMPT = """Assess whether the generated focused test oracle is valid or the implementation violates its contract. Use only the supplied explicit operation/behavior contract and source/test evidence. Return exactly JSON with fields: classification (implementation|test_oracle|undetermined), rationale (string), evidence (array of strings), preserve_assertions (array of strings naming every existing passing or contract-backed assertion to retain), correction_instruction (string). A failure alone or source/test disagreement alone does not prove a bad oracle. Mark test_oracle only if the failed assertion is invalid or contradicts authoritative contract; mark implementation only if a valid contract-backed assertion is violated by the SUT; otherwise undetermined. Never weaken/delete passing or contract-backed assertions. For test_oracle, the SUT is read-only and correction_instruction must change only the assigned existing test file, retaining the behavior assertion; do not suggest adding attributes/markers to the SUT. For implementation, keep the supplied test byte-for-byte unchanged and restrict correction_instruction to the assigned subject source. Do not edit code."""


def _run_file(run_root: Path, relative: object) -> Path | None:
    """Resolve one declared run-relative evidence path without discovery."""

    if not isinstance(relative, str) or not relative:
        return None
    candidate = (run_root / relative).resolve()
    try:
        candidate.relative_to(run_root.resolve())
    except ValueError:
        return None
    return candidate


def _read_declared_text(run_root: Path, relative: object) -> str | None:
    path = _run_file(run_root, relative)
    if path is None or not path.is_file():
        return None
    try:
        if path.stat().st_size > 65536:
            return None
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _frontend_test_runtime_note(
    run_root: Path, candidate_source: str, test_source: str
) -> dict[str, object] | None:
    """Return small, declared frontend-runtime facts for one unit-review prompt."""

    frontend_prefix = "application/frontend/"
    if not candidate_source.startswith(frontend_prefix):
        return None
    vite_config = _read_declared_text(run_root, "application/frontend/vite.config.ts")
    if vite_config is None:
        return None
    environment = "jsdom" if re.search(r"environment\s*:\s*['\"]jsdom['\"]", vite_config) else None
    response_blob_text = bool(
        re.search(r"new\s+Response\s*\(\s*blob\s*\)\s*\.text\s*\(", test_source)
    )
    note: dict[str, object] = {
        "testEnvironment": environment,
        "configPath": "application/frontend/vite.config.ts",
    }
    if response_blob_text:
        note["currentTestAlreadyUsesNodeResponseForBlob"] = True
    if environment == "jsdom":
        note["blobInspectionGuidance"] = (
            "jsdom browser Blob values and Node Response values can be different runtime brands. "
            "Do not replace one unsupported Blob reader with another unverified fallback. "
            "For a decoded-content assertion, use FileReader in the test DOM and retain exact content "
            "and MIME-type assertions; do not use Node Response(blob).text() or Blob.text() as alternatives."
        )
    return note


def _unit_failure_review(
    run_root: Path,
    failed: dict[str, object],
    evidence: dict[str, object],
) -> dict[str, object] | None:
    """Ask the configured reviewer about one executed, SHA-pinned unit failure.

    Missing declared contract or raw check evidence is deliberately undetermined:
    this path must never infer that a test is wrong merely from source disagreement.
    """

    task_id = str(failed.get("task_id", ""))
    logger.info("unit failure review entered task_id=%s", task_id)

    def skip(reason: str) -> None:
        logger.info("unit failure review skipped task_id=%s reason=%s", task_id, reason)

    results = evidence.get("unitTestResults")
    candidate = evidence.get("frozenTestCandidate")
    profile = failed.get("verification_profile")
    if not isinstance(results, dict) or not isinstance(candidate, dict) or not isinstance(profile, dict):
        skip("missing_results_candidate_or_profile")
        return None
    if not (
        isinstance(results.get("total"), int)
        and isinstance(results.get("failed"), int)
        and isinstance(results.get("skipped", 0), int)
        and results["failed"] > 0
        and results["total"] - results.get("skipped", 0) > 0
    ):
        skip("no_executed_assertion_failure")
        return None
    candidate_path = candidate.get("path")
    candidate_sha = candidate.get("sha256")
    candidate_source = candidate.get("sourcePath")
    test_source = _read_declared_text(run_root, candidate_path)
    if not (
        isinstance(candidate_sha, str)
        and isinstance(candidate_source, str)
        and test_source is not None
        and hashlib.sha256(test_source.encode("utf-8")).hexdigest() == candidate_sha
    ):
        skip("frozen_candidate_missing_or_sha_mismatch")
        return None
    subject_paths = profile.get("unitTestSubjectPaths")
    if not isinstance(subject_paths, list) or not subject_paths or not all(
        isinstance(path, str) and path for path in subject_paths
    ):
        skip("subject_paths_missing")
        return None
    subject_sources = {
        path: _read_declared_text(run_root, path)
        for path in subject_paths
    }
    if any(body is None for body in subject_sources.values()):
        skip("subject_source_missing_or_unreadable")
        return None
    context_path = _run_file(run_root, failed.get("context_file", failed.get("contextFile")))
    if context_path is None or not context_path.is_file():
        skip("task_context_missing_or_unreadable")
        return None
    try:
        context = _read_json(context_path)
    except (OSError, json.JSONDecodeError):
        skip("subject_context_missing_or_unreadable")
        return None
    subject_context_path = context.get("subjectContextPath")
    subject_context_file = _run_file(run_root, subject_context_path)
    if subject_context_file is None or not subject_context_file.is_file():
        skip("operation_context_paths_missing")
        return None
    try:
        subject_context = _read_json(subject_context_file)
    except (OSError, json.JSONDecodeError):
        skip("operation_contract_missing_or_unreadable")
        return None
    operation_paths = subject_context.get("operationContextPaths")
    if not isinstance(operation_paths, list) or not operation_paths:
        generated_contract = subject_context.get("generatedOperationContractsPath")
        operation_paths = [generated_contract] if isinstance(generated_contract, str) else []
    if not isinstance(operation_paths, list) or not operation_paths or not all(
        isinstance(path, str) and path for path in operation_paths
    ):
        skip("task_prompt_missing_or_unreadable")
        return None
    operation_contracts = {
        path: _read_declared_text(run_root, path)
        for path in operation_paths
    }
    if any(body is None for body in operation_contracts.values()):
        return None
    prompt = _read_declared_text(run_root, failed.get("prompt_file", failed.get("promptFile")))
    if prompt is None:
        return None
    task_type = str(failed.get("task_type", ""))
    raw_report: object
    if task_type == "frontend-unit-test":
        report_path = run_root / "reports" / "agent-executions" / f"{failed['task_id']}.vitest.json"
        if not report_path.is_file() or report_path.stat().st_size > 65536:
            skip("frontend_raw_report_missing_or_oversized")
            return None
        try:
            raw_report = _read_json(report_path)
        except (OSError, json.JSONDecodeError):
            skip("frontend_raw_report_invalid")
            return None
    else:
        # Backend has no stable run-root XML artifact. Its canonical verifier already
        # projects selected-class XML failures into this evidence when available.
        raw_report = {
            key: evidence.get(key)
            for key in ("testResults", "stdout", "stderr", "diagnosticPaths")
            if key in evidence
        }
        if not raw_report.get("testResults"):
            skip("backend_test_results_missing")
            return None
    frontend_runtime = _frontend_test_runtime_note(run_root, candidate_source, test_source)
    payload = {
        "taskPrompt": prompt,
        "taskSpec": {
            key: failed.get(key)
            for key in (
                "task_id", "task_type", "allowed_write_paths", "required_output_paths",
                "required_test_paths", "verification_profile", "depends_on",
            )
        },
        "materializedContext": {
            key: context.get(key)
            for key in ("readSourcePaths", "availableReadPaths", "subjectContextPath", "designInputs")
            if key in context
        },
        "subjectContext": {
            key: subject_context.get(key)
            for key in (
                "operationContextPaths", "generatedOperationContractsPath",
                "readSourcePaths", "sourceRefs",
            )
            if key in subject_context
        },
        "operationContracts": operation_contracts,
        "subjectSources": subject_sources,
        "frozenTestSource": test_source,
        "observedCheckEvidence": {
            "unitTestResults": results,
            "rawReport": raw_report,
        },
    }
    if frontend_runtime is not None:
        payload["frontendTestRuntime"] = frontend_runtime
    try:
        connection = build_llm_connection()
        logger.info(
            "unit failure review request started task_id=%s model=%s",
            task_id,
            connection.model,
        )
        if not connection.api_key:
            logger.warning(
                "unit failure review unavailable task_id=%s model=%s reason=no_api_key",
                task_id,
                connection.model,
            )
            return None
        client = OpenAI(
            api_key=connection.api_key,
            base_url=connection.base_url,
            default_headers=connection.default_headers(),
            timeout=settings.llm_timeout_seconds,
            max_retries=0,
        )
        messages = [
            {"role": "system", "content": _UNIT_FAILURE_REVIEW_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

        def request_review(current_messages: list[dict[str, str]]) -> object:
            return client.chat.completions.create(
                model=connection.model,
                messages=current_messages,
                temperature=0.1,
                max_completion_tokens=8192,
                reasoning_effort=settings.design_reasoning_effort,
                response_format={"type": "json_object"},
            )

        latest_invalid_content = ""
        latest_issues: list[dict[str, object]] = []
        correction_attempt = 0
        while True:
            request_messages = messages
            if correction_attempt:
                schema_json = json.dumps(
                    UnitFailureReview.model_json_schema(), ensure_ascii=False
                )
                correction = (
                    "Reassess the original evidence under the original system instructions. "
                    "Your latest response failed schema validation. Return one complete JSON "
                    "object conforming to this exact schema; do not omit required fields, "
                    "default a verdict, weaken or remove contract-backed assertions, or broaden "
                    "the permitted edit target. This is a format/schema correction only; a "
                    "valid undetermined classification is acceptable when evidence is uncertain. "
                    f"Schema: {schema_json}\nValidation issues: "
                    f"{json.dumps(latest_issues, ensure_ascii=False)}"
                )
                request_messages = [
                    *messages,
                    {"role": "assistant", "content": latest_invalid_content},
                    {"role": "user", "content": correction},
                ]
            response = request_review(request_messages)
            choices = response.choices or []
            content = choices[0].message.content if choices else None
            if isinstance(content, str):
                try:
                    review = UnitFailureReview.model_validate_json(content).model_dump()
                    break
                except ValidationError as validation_error:
                    latest_invalid_content = content
                    latest_issues = [
                        {"loc": list(item.get("loc", ())), "type": item.get("type")}
                        for item in validation_error.errors(include_input=False)
                    ]
            else:
                latest_invalid_content = ""
                latest_issues = [
                    {"loc": ["message", "content"], "type": "missing_content"}
                ]
            correction_attempt += 1
            logger.warning(
                "unit failure review response failed schema task_id=%s model=%s attempt=%s issues=%s",
                task_id,
                connection.model,
                correction_attempt,
                latest_issues,
            )
            # The Workspace stop path terminates this registered runner process.
            # Capped backoff avoids a tight loop while leaving retries uncapped.
            time.sleep(min(30.0, float(2 ** min(correction_attempt - 1, 5))))
        logger.info(
            "unit failure review completed task_id=%s model=%s classification=%s",
            task_id,
            connection.model,
            review["classification"],
        )
        return review
    except ValidationError as error:
        issues = [
            {"loc": list(item.get("loc", ())), "type": item.get("type")}
            for item in error.errors(include_input=False)
        ]
        logger.warning(
            "unit failure review invalid schema task_id=%s model=%s issues=%s",
            task_id,
            getattr(locals().get("connection"), "model", "unknown"),
            issues,
        )
        return None
    except APIError as error:
        details = _reviewer_error_value(error)
        logger.warning(
            "unit failure review API error task_id=%s model=%s details=%s",
            task_id,
            getattr(locals().get("connection"), "model", "unknown"),
            json.dumps(details, ensure_ascii=False),
        )
        raise ReviewerProviderError(details) from error
    except (OSError, ValueError) as error:
        logger.warning(
            "unit failure review failed task_id=%s model=%s error_type=%s errno=%s",
            task_id,
            getattr(locals().get("connection"), "model", "unknown"),
            type(error).__name__,
            getattr(error, "errno", None),
        )
        return None


def select_repair_task(
    run_root: Path,
    *,
    owner: str,
    failed_task_id: str,
    evidence: dict[str, object],
) -> dict[str, object]:
    """Choose one task from exact IDs, trace refs, or explicit source paths."""

    manifest = _read_json(run_root / "reports" / "run-manifest.json")
    tasks = [
        task
        for task in manifest.get("implementation_tasks", [])
        if isinstance(task, dict) and isinstance(task.get("task_id"), str)
    ]
    failed = next((task for task in tasks if task["task_id"] == failed_task_id), None)
    if failed is not None:
        unit_subject = _unit_subject_repair_task(failed, tasks, evidence)
        if unit_subject is not None:
            return unit_subject
        failed_owner = str(failed.get("owner", ""))
        if owner and failed_owner and owner != failed_owner:
            raise RepairRoutingError(
                f"Failed task {failed_task_id} belongs to {failed_owner}, not {owner}"
            )
        return failed
    evidence_refs, task_ids = _structured_repair_evidence(evidence)
    task_ids |= {
        value.removeprefix("task:")
        for value in evidence_refs
        if value.startswith("task:")
    }
    exact = [task for task in tasks if task["task_id"] in task_ids]
    if len(exact) == 1:
        exact_owner = str(exact[0].get("owner", ""))
        if owner and exact_owner and owner != exact_owner:
            raise RepairRoutingError(
                f"Repair task {exact[0]['task_id']} belongs to {exact_owner}, not {owner}"
            )
        return exact[0]
    if len(exact) > 1:
        raise RepairRoutingError("Repair evidence names multiple implementation tasks")
    owner_tasks = [task for task in tasks if str(task.get("owner", "")) == owner]
    if len(owner_tasks) == 1:
        return owner_tasks[0]
    if not owner_tasks:
        raise RepairRoutingError(f"No implementation tasks for repair owner {owner}")

    evidence_paths = referenced_source_paths(evidence)
    candidate_sets: list[set[str]] = []
    if evidence_refs:
        matched = {
            str(task["task_id"])
            for task in owner_tasks
            if evidence_refs.intersection(_task_source_refs(task))
        }
        if matched:
            candidate_sets.append(matched)
    if evidence_paths:
        matched = {
            str(task["task_id"])
            for task in owner_tasks
            if any(path in _task_paths(run_root, task) for path in evidence_paths)
        }
        if matched:
            candidate_sets.append(matched)
    if not candidate_sets:
        raise RepairRoutingError(
            f"Repair routing for owner {owner} needs an exact task, source ref, or path"
        )
    candidates = set.intersection(*candidate_sets)
    if len(candidates) != 1:
        detail = ", ".join(sorted(candidates)) or "conflicting evidence"
        raise RepairRoutingError(f"Repair routing is ambiguous for owner {owner}: {detail}")
    task_id = next(iter(candidates))
    return next(task for task in owner_tasks if task["task_id"] == task_id)


def schedule_cross_phase_repair(
    run_root: Path,
    failed_task_id: str,
    evidence: dict[str, object],
    *,
    batch_id: str | None = None,
) -> dict[str, object] | None:
    """Schedule repair with the explicitly declared implementation owner."""
    batch_id = batch_id or uuid.uuid4().hex
    manifest_path = run_root / "reports" / "run-manifest.json"
    manifest = _read_json(manifest_path)
    tasks = [
        task
        for task in manifest.get("implementation_tasks", [])
        if isinstance(task, dict) and task.get("task_id")
    ]
    if not tasks:
        return None
    paths = referenced_source_paths(evidence)

    failed = next(
        (task for task in tasks if str(task.get("task_id")) == failed_task_id),
        None,
    )
    plan_path = run_root / REPAIR_PLAN
    current_plan = _read_json(plan_path) if plan_path.is_file() else {"entries": []}
    current_entries = [
        entry
        for entry in current_plan.get("entries", [])
        if isinstance(entry, dict) and entry.get("failedTaskId") == failed_task_id
    ]
    repair_revision = len(current_entries) + 1

    unit_recheck_ids: set[str] = set()
    selected = _unit_subject_repair_task(failed, tasks, evidence)
    frozen_candidate: dict[str, object] | None = None
    unit_failure_review: dict[str, object] | None = None
    if selected is not None:
        unit_failure_review = _unit_failure_review(run_root, failed, evidence)
        if unit_failure_review is None or unit_failure_review["classification"] == "undetermined":
            return None
        frozen_candidate = dict(evidence["frozenTestCandidate"])
        source_path = frozen_candidate.get("sourcePath")
        if isinstance(source_path, str) and source_path not in paths:
            paths.append(source_path)
        if unit_failure_review["classification"] == "test_oracle":
            selected = failed
        else:
            unit_recheck_ids.add(str(failed["task_id"]))
        owner = str(selected.get("owner", "")).strip()
    else:
        attributed = _attributed_source_repair_task(failed, tasks, evidence)
        if attributed is not None:
            selected = attributed
            owner = str(selected.get("owner", "")).strip()
            target = str(evidence.get("attributedTargetFile", "")).replace("\\", "/")
            if target and target not in paths:
                paths.append(target)
            linked_unit = _freeze_linked_unit_test(
                run_root, tasks, selected, failed_task_id, repair_revision
            )
            if linked_unit is not None:
                unit_task_id, frozen_candidate = linked_unit
                unit_recheck_ids.add(unit_task_id)
        else:
            explicit_owner = evidence.get("owner")
            owner = (
                explicit_owner.strip()
                if isinstance(explicit_owner, str) and explicit_owner.strip()
                else ""
            )
            if not owner and failed is not None:
                owner = str(failed.get("owner", "")).strip()
            if not owner:
                owner = _owner_for_paths(tasks, paths)
            if not owner:
                return None
            selected = select_repair_task(
                run_root,
                owner=owner,
                failed_task_id=failed_task_id,
                evidence=evidence,
            )
            owner = str(selected.get("owner", owner))
    owner_ids = {str(selected["task_id"])}

    current_text = _evidence_text(evidence)
    plan_path = run_root / REPAIR_PLAN
    plan = (
        _read_json(plan_path)
        if plan_path.is_file()
        else {"schemaVersion": REPAIR_SCHEMA, "entries": []}
    )
    entries = current_entries
    repair_paths = _repair_paths(tasks, owner_ids, paths)
    source_digest = _source_digest(
        run_root,
        repair_paths or _owner_digest_paths(tasks, owner_ids),
    )
    failure_digest = hashlib.sha256(current_text.encode("utf-8")).hexdigest()
    same_failure_count = sum(
        1
        for item in entries
        if item.get("failureDigest") == failure_digest
        and item.get("acceptedSourceDigest") == source_digest
    )
    strategy = _repair_strategy(same_failure_count)
    now = datetime.now(UTC).isoformat()
    entry = {
        "batchId": batch_id,
        "failedTaskId": failed_task_id,
        "owner": owner,
        "ownerTaskIds": sorted(owner_ids),
        "recheckTaskIds": sorted(unit_recheck_ids),
        "frozenTestCandidate": frozen_candidate,
        "unitFailureReview": unit_failure_review,
        "outcome": "scheduled",
        "evidence": _bounded_evidence(current_text),
        "relatedPaths": paths,
        "repairPaths": repair_paths,
        "failureDigest": failure_digest,
        "acceptedSourceDigest": source_digest,
        "acceptedSourceRoot": "application",
        "strategy": strategy,
        "revision": len(entries) + 1,
        "createdAt": str(entries[0].get("createdAt")) if entries else now,
        "updatedAt": now,
    }
    all_entries = [item for item in plan.get("entries", []) if isinstance(item, dict)]
    plan.update(
        {
            "schemaVersion": REPAIR_SCHEMA,
            "status": "ACTIVE",
            "activeBatchId": batch_id,
            "entries": [*all_entries, entry],
            "updatedAt": now,
        }
    )
    plan.pop("stallReason", None)
    _write_json(plan_path, plan)
    return entry


def schedule_cross_phase_repair_batch(
    run_root: Path,
    repairs: list[tuple[str, dict[str, object]]],
) -> list[dict[str, object]] | None:
    """Append declared repairs and activate them as one coordinator batch."""
    if not repairs:
        return None
    plan_path = run_root / REPAIR_PLAN
    original_plan = plan_path.read_bytes() if plan_path.is_file() else None
    original_candidate_paths: set[str] | None = set()
    if original_plan is not None:
        try:
            original_candidate_paths = {
                str(candidate.get("path"))
                for entry in json.loads(original_plan).get("entries", [])
                if isinstance(entry, dict)
                and isinstance((candidate := entry.get("frozenTestCandidate")), dict)
            }
        except (json.JSONDecodeError, OSError):
            original_candidate_paths = None
    batch_id = uuid.uuid4().hex
    scheduled: list[dict[str, object]] = []
    failed = False
    try:
        for failed_task_id, evidence in repairs:
            entry = schedule_cross_phase_repair(
                run_root, failed_task_id, evidence, batch_id=batch_id
            )
            if entry is None:
                failed = True
                break
            scheduled.append(entry)
    except Exception:
        _rollback_repair_batch(run_root, plan_path, original_plan, original_candidate_paths)
        raise
    if failed:
        _rollback_repair_batch(run_root, plan_path, original_plan, original_candidate_paths)
        return None
    return scheduled


def _rollback_repair_batch(
    run_root: Path,
    plan_path: Path,
    original_plan: bytes | None,
    original_candidate_paths: set[str] | None,
) -> None:
    if original_candidate_paths is not None and plan_path.is_file():
        try:
            current = _read_json(plan_path)
            for entry in current.get("entries", []):
                candidate = entry.get("frozenTestCandidate") if isinstance(entry, dict) else None
                relative = candidate.get("path") if isinstance(candidate, dict) else None
                if (
                    isinstance(relative, str)
                    and relative not in original_candidate_paths
                    and relative.startswith("reports/agent-executions/")
                ):
                    candidate_path = _run_file(run_root, relative)
                    if candidate_path is not None and candidate_path.is_file():
                        candidate_path.unlink()
        except (OSError, ValueError, TypeError):
            logger.exception("Could not clean incomplete repair-batch candidates")
    if original_plan is None:
        plan_path.unlink(missing_ok=True)
    else:
        plan_path.write_bytes(original_plan)


def _active_repair_entries(plan: dict[str, object]) -> list[dict[str, object]]:
    """Return only entries queued by the current repair submission."""
    entries = [entry for entry in plan.get("entries", []) if isinstance(entry, dict)]
    batch_id = plan.get("activeBatchId")
    if isinstance(batch_id, str) and batch_id:
        return [entry for entry in entries if entry.get("batchId") == batch_id]
    return entries[-1:]


def schedule_source_conformance_repair(
    run_root: Path, report: dict[str, object]
) -> dict[str, object] | None:
    """공개 계약 또는 ERD 검사 결과를 일반 자동 수리 흐름에 넣는다."""
    violations = [
        item for item in report.get("violations", []) if isinstance(item, dict)
    ]
    if not violations:
        return None
    evidence = {
        "owner": "backend",
        "command": ["source-design-conformance"],
        "stderr": json.dumps(violations, ensure_ascii=False, indent=2),
    }
    return schedule_cross_phase_repair(
        run_root,
        "source-design-conformance",
        evidence,
    )


def apply_repair_directives(run_root: Path) -> None:
    """초기 구현 설명과 분리된 짧은 수리 prompt를 만든다.

    초기 prompt는 기능 전체를 처음 만드는 데 유용하지만, 작은 compile 또는 HTTP 오류를
    고칠 때 다시 보내면 모델이 이미 정상인 코드를 재검토하게 된다. 작업 정의에는 별도
    ``repair_prompt_file``만 연결하고 원본 prompt는 그대로 보존한다.
    """
    plan_path = run_root / REPAIR_PLAN
    if not plan_path.is_file():
        return
    plan = _read_json(plan_path)
    entries = [item for item in plan.get("entries", []) if isinstance(item, dict)]
    active_entries = _active_repair_entries(plan)
    if not active_entries:
        return

    active_ids = {
        str(value)
        for active in active_entries
        for value in active.get("ownerTaskIds", [])
    }
    manifest_path = run_root / "reports" / "run-manifest.json"
    manifest = _read_json(manifest_path)
    if _prepare_linked_unit_recheck(run_root, plan, manifest):
        _write_json(plan_path, plan)
        entries = [item for item in plan.get("entries", []) if isinstance(item, dict)]
        active_entries = _active_repair_entries(plan)
    task_files = _task_files(run_root)

    for task in manifest.get("implementation_tasks", []):
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id", ""))
        prompt_path = run_root / str(task.get("prompt_file", ""))
        if not task_id or not prompt_path.is_file():
            continue
        original = prompt_path.read_text(encoding="utf-8")
        base_prompt = _without_repair_directives(original)
        if base_prompt != original:
            prompt_path.write_text(base_prompt, encoding="utf-8")
        base_digest = hashlib.sha256(base_prompt.encode("utf-8")).hexdigest()
        relevant = [
            entry for entry in entries if task_id in entry.get("ownerTaskIds", [])
        ]
        active_relevant = [
            entry for entry in active_entries if task_id in entry.get("ownerTaskIds", [])
        ]
        repair_prompt_path = run_root / REPAIR_PROMPT_DIR / f"{task_id}.repair.md"
        repair_prompt = ""
        if task_id in active_ids and active_relevant:
            current = relevant[-1]
            previous = relevant[-4:-1]
            plan_history = "\n".join(
                f"- {entry.get('strategy', 'previous approach')}: "
                + _first_evidence_line(str(entry.get("evidence", "")))
                for entry in previous
            ) or "- No previous failures"
            execution_history = _recent_execution_history(run_root, task_id)
            history = plan_history
            if execution_history:
                history += "\n\n### Previous changes and verification results\n\n" + execution_history
            source_hints = "\n".join(
                f"- `{path}`" for path in current.get("relatedPaths", [])
            ) or "- Start with the source paths from the task definition"
            frozen_context = ""
            review = current.get("unitFailureReview")
            test_oracle_repair = (
                isinstance(review, dict)
                and review.get("classification") == "test_oracle"
            )
            frozen_oracle = current.get("frozenTestCandidate")
            if isinstance(frozen_oracle, dict):
                candidate_path = frozen_oracle.get("path")
                source_path = frozen_oracle.get("sourcePath")
                candidate = run_root / candidate_path if isinstance(candidate_path, str) else None
                if candidate is not None and candidate.is_file() and isinstance(source_path, str):
                    source = candidate.read_text(encoding="utf-8")
                    if len(source.encode("utf-8")) <= 65536:
                        if test_oracle_repair:
                            preserve = review.get("preserve_assertions", [])
                            preserve_text = "\n".join(
                                f"- {item}" for item in preserve if isinstance(item, str)
                            ) or "- Preserve every passing and contract-backed assertion."
                            instruction = review.get("correction_instruction", "")
                            frozen_context = (
                                "## Editable focused test evidence\n\n"
                                f"`{source_path}` is the SHA-pinned executed test candidate. Correct only this "
                                "assigned test file. The supplied implementation subject is read-only; do not "
                                "weaken or delete valid assertions.\n\n"
                                f"### Assertions to preserve\n\n{preserve_text}\n\n"
                                f"### Reviewer correction boundary\n\n{instruction}\n\n"
                                f"```\n{source}\n```\n\n"
                            )
                        else:
                            frozen_context = (
                                "## Frozen focused test (read-only)\n\n"
                                f"`{source_path}` is the executed failing oracle. Do not edit it; repair only "
                                "your declared implementation source and let the system recheck it.\n\n"
                                f"```\n{source}\n```\n\n"
                            )
            immutable = "\n".join(
                f"- `{path}`" for path in task.get("immutable_paths", [])
            ) or "- None"
            restricted_tools = (
                str(
                    task.get("owner_tool_mode")
                    or settings.implementation_owner_tool_mode
                )
                != "terminal"
            )
            if restricted_tools:
                reproduce_instruction = (
                    "Inspect the current source with the file editor, apply one focused edit "
                    "batch, then use `run_task_check` to reproduce the assigned verification.\n\n"
                )
                verification_instruction = (
                    "After editing, call `run_task_check`. Inspect its concrete failure and "
                    "continue repairing in this conversation. "
                )
            else:
                reproduce_instruction = (
                    "Use the terminal to reproduce the assigned verification against the "
                    "current source before editing.\n\n"
                )
                verification_instruction = (
                    "After editing, rerun the relevant build or test in the terminal. Inspect "
                    "its concrete failure and continue repairing in this conversation. "
                )
            hint_scope = (
                "These paths come from failure evidence and traceability. They are "
                "investigation hints, not an exhaustive list of relevant source.\n\n"
            )
            completion_instruction = (
                verification_instruction
                + "When it passes, call FinishTool immediately; do not end with a plain-text "
                "summary.\n"
            )
            repair_goal = (
                "Correct only the assigned focused test oracle. The supplied implementation source is "
                "read-only. Retain every listed valid assertion and do not alter unrelated behavior."
                if test_oracle_repair
                else "Resolve the technical failure below. Choose the implementation, tests, and edit order "
                "autonomously. Do not change unrelated features or generated public contracts. Read needed "
                "source with the file editor."
            )
            repair_prompt = (
                f"# {REPAIR_PROMPT_HEADING.removeprefix('## ')}\n\n"
                f"{repair_goal}\n\n"
                f"{reproduce_instruction}"
                f"## Current approach\n\n{current.get('strategy', 'focused-fix')}\n\n"
                "## Starting source hints\n\n"
                f"{source_hints}\n\n"
                f"{hint_scope}"
                f"## Read-only public contracts\n\n{immutable}\n\n"
                f"## Previous failed approaches\n\n{history}\n\n"
                "## Current failure\n\n```text\n"
                f"{current.get('evidence', '')}\n```\n\n"
                f"{frozen_context}"
                "Resolve every item in the current failure evidence before running verification; "
                "a passing build alone does not clear implementation markers or controller stubs. "
                f"{completion_instruction}"
            )
            repair_prompt_path.write_text(repair_prompt, encoding="utf-8")
            task["repair_prompt_file"] = str(
                repair_prompt_path.relative_to(run_root)
            ).replace("\\", "/")
        else:
            task.pop("repair_prompt_file", None)
            if repair_prompt_path.is_file():
                repair_prompt_path.unlink()
        digest_material = (
            base_prompt if not repair_prompt else base_prompt + "\0" + repair_prompt
        )
        digest = hashlib.sha256(digest_material.encode("utf-8")).hexdigest()
        task["initial_prompt_sha256"] = base_digest
        task["prompt_sha256"] = digest
        sources = dict(task.get("source_artifacts", {}))
        if task_id in active_ids:
            sources["repairEvidence"] = str(plan_path)
        else:
            sources.pop("repairEvidence", None)
        task["source_artifacts"] = sources
        if task_id in task_files:
            path, definition = task_files[task_id]
            definition["prompt_sha256"] = digest
            definition["initial_prompt_sha256"] = base_digest
            definition["source_artifacts"] = sources
            if task_id in active_ids and repair_prompt:
                definition["repair_prompt_file"] = task["repair_prompt_file"]
            else:
                definition.pop("repair_prompt_file", None)
            _write_json(path, definition)
    _write_json(manifest_path, manifest)


def _prepare_linked_unit_recheck(
    run_root: Path, plan: dict[str, object], manifest: dict[str, object]
) -> bool:
    """Ensure an active source repair rechecks its unique declared dependent unit."""

    entries = _active_repair_entries(plan)
    if not entries:
        return False
    tasks = [
        task
        for task in manifest.get("implementation_tasks", [])
        if isinstance(task, dict) and task.get("task_id")
    ]
    changed = False
    for active in entries:
        if active.get("frozenTestCandidate") or active.get("recheckTaskIds"):
            continue
        if active.get("unitFailureReview") is not None:
            continue
        owner_ids = active.get("ownerTaskIds", [])
        if not isinstance(owner_ids, list) or len(owner_ids) != 1:
            continue
        owner_task = next(
            (task for task in tasks if str(task.get("task_id")) == str(owner_ids[0])),
            None,
        )
        if owner_task is None or str(owner_task.get("task_type", "")) not in {
            "backend-implementation",
            "frontend-implementation",
        }:
            continue
        linked = _freeze_linked_unit_test(
            run_root,
            tasks,
            owner_task,
            str(active.get("failedTaskId", "")),
            int(active.get("revision", 1)),
        )
        if linked is None:
            continue
        unit_task_id, candidate = linked
        active["frozenTestCandidate"] = candidate
        active["recheckTaskIds"] = [unit_task_id]
        changed = True
    return changed


def repair_task_ids(run_root: Path) -> set[str]:
    """현재 자동 수리를 수행할 기능 작업 ID를 반환한다."""
    plan_path = run_root / REPAIR_PLAN
    if not plan_path.is_file():
        return set()
    return {
        str(value)
        for entry in _active_repair_entries(_read_json(plan_path))
        for value in entry.get("ownerTaskIds", [])
    }


def repair_recheck_task_ids(run_root: Path) -> set[str]:
    """Return frozen unit-test tasks that must rerun after a subject repair."""

    plan_path = run_root / REPAIR_PLAN
    if not plan_path.is_file():
        return set()
    return {
        str(value)
        for entry in _active_repair_entries(_read_json(plan_path))
        for value in entry.get("recheckTaskIds", [])
    }


def repair_recheck_for_task(
    run_root: Path, task_id: str
) -> dict[str, object] | None:
    """Return the active frozen unit-test recheck contract for one task."""

    plan_path = run_root / REPAIR_PLAN
    if not plan_path.is_file():
        return None
    entries = _active_repair_entries(_read_json(plan_path))
    matching = [
        entry
        for entry in entries
        if task_id in entry.get("recheckTaskIds", [])
        and isinstance(entry.get("frozenTestCandidate"), dict)
    ]
    return matching[-1] if matching else None


def _unit_subject_repair_task(
    failed: dict[str, object] | None,
    tasks: list[dict[str, object]],
    evidence: dict[str, object],
) -> dict[str, object] | None:
    """Map a real focused assertion failure to its declared implementation subject."""

    if failed is None or str(failed.get("task_type", "")) not in {
        "backend-unit-test",
        "frontend-unit-test",
    }:
        return None
    results = evidence.get("unitTestResults")
    if not isinstance(results, dict):
        return None
    total = results.get("total")
    failed_count = results.get("failed")
    skipped = results.get("skipped", 0)
    if not all(isinstance(value, int) for value in (total, failed_count, skipped)):
        return None
    if total - skipped <= 0 or failed_count <= 0:
        return None
    candidate = evidence.get("frozenTestCandidate")
    if not isinstance(candidate, dict) or not all(
        isinstance(candidate.get(key), str) and candidate[key]
        for key in ("path", "sha256", "sourcePath")
    ):
        return None
    profile = failed.get("verification_profile")
    subjects = profile.get("unitTestSubjectPaths") if isinstance(profile, dict) else None
    if not isinstance(subjects, list) or not subjects or not all(
        isinstance(path, str) and path for path in subjects
    ):
        return None
    parent_ids = {
        str(task_id)
        for task_id in failed.get("depends_on", failed.get("dependsOn", []))
        if isinstance(task_id, str)
    }
    candidates = [
        task
        for task in tasks
        if str(task.get("task_id")) in parent_ids
        and str(task.get("task_type")) in {
            "backend-implementation",
            "frontend-implementation",
        }
        and set(subjects).issubset(
            {
                str(path)
                for path in task.get("allowed_write_paths", task.get("allowedWritePaths", []))
                if isinstance(path, str)
            }
        )
    ]
    return candidates[0] if len(candidates) == 1 else None


def _attributed_source_repair_task(
    failed: dict[str, object] | None,
    tasks: list[dict[str, object]],
    evidence: dict[str, object],
) -> dict[str, object] | None:
    """Honor a unique RTM-attributed source task only inside its declared scope."""

    if failed is None or str(failed.get("task_type", "")) != "integration-implementation":
        return None
    task_id = evidence.get("repairTaskId")
    target = evidence.get("attributedTargetFile")
    if not isinstance(task_id, str) or not task_id or not isinstance(target, str):
        return None
    matches = [task for task in tasks if str(task.get("task_id")) == task_id]
    if len(matches) != 1 or task_id == str(failed.get("task_id", "")):
        return None
    selected = matches[0]
    if str(selected.get("task_type", "")) not in {
        "backend-implementation",
        "frontend-implementation",
    } or str(selected.get("owner", "")) == str(failed.get("owner", "")):
        return None
    declared_paths = {
        str(path).replace("\\", "/")
        for key in ("allowed_write_paths", "required_output_paths")
        for path in selected.get(key, [])
        if isinstance(path, str)
    }
    return selected if target.replace("\\", "/") in declared_paths else None


def _freeze_linked_unit_test(
    run_root: Path,
    tasks: list[dict[str, object]],
    owner_task: dict[str, object],
    failed_task_id: str,
    repair_revision: int,
) -> tuple[str, dict[str, object]] | None:
    """Freeze the uniquely declared unit test dependent on a repaired source task."""

    owner_id = str(owner_task.get("task_id", ""))
    owner_paths = {
        str(path).replace("\\", "/")
        for path in owner_task.get("allowed_write_paths", owner_task.get("allowedWritePaths", []))
        if isinstance(path, str)
    }
    linked: list[tuple[dict[str, object], str]] = []
    for task in tasks:
        task_type = str(task.get("task_type", task.get("taskType", "")))
        dependencies = task.get("depends_on", task.get("dependsOn", []))
        profile = task.get("verification_profile", task.get("verificationProfile", {}))
        subjects = profile.get("unitTestSubjectPaths") if isinstance(profile, dict) else None
        required_tests = task.get("required_test_paths", task.get("requiredTestPaths", []))
        if (
            task_type not in {"backend-unit-test", "frontend-unit-test"}
            or owner_id not in dependencies
            or not isinstance(subjects, list)
            or not subjects
            or not all(isinstance(path, str) and path for path in subjects)
            or not set(subjects).issubset(owner_paths)
            or not isinstance(required_tests, list)
            or len(required_tests) != 1
            or not isinstance(required_tests[0], str)
        ):
            continue
        test_path = required_tests[0].replace("\\", "/")
        if _run_file(run_root, test_path) is not None and _run_file(run_root, test_path).is_file():
            linked.append((task, test_path))

    # The repair plan has one frozen candidate slot. Do not assign one test's
    # bytes to multiple unit tasks or guess when the manifest is ambiguous.
    if len(linked) != 1:
        return None
    task, source_path = linked[0]
    source = _run_file(run_root, source_path)
    if source is None:
        return None
    target_dir = run_root / "reports" / "agent-executions"
    target_dir.mkdir(parents=True, exist_ok=True)
    task_id = str(task.get("task_id", ""))
    identity = hashlib.sha256(
        f"{failed_task_id}:{task_id}:{repair_revision}".encode("utf-8")
    ).hexdigest()[:12]
    target = target_dir / f"{identity}.frozen-test{source.suffix}"
    shutil.copyfile(source, target)
    return task_id, {
        "path": target.relative_to(run_root).as_posix(),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "sourcePath": source_path,
    }


def active_repair_for_task(
    run_root: Path, task_id: str
) -> dict[str, object] | None:
    """현재 작업에 배정된 최신 수리 항목을 반환한다."""
    plan_path = run_root / REPAIR_PLAN
    if not plan_path.is_file():
        return None
    entries = _active_repair_entries(_read_json(plan_path))
    matching = [
        entry for entry in entries if task_id in entry.get("ownerTaskIds", [])
    ]
    return matching[-1] if matching else None


def referenced_source_paths(evidence: dict[str, object]) -> list[str]:
    """compiler, test와 JSON 보고서에서 application 상대 경로를 읽는다."""
    text = _evidence_text(evidence).replace("\\", "/")
    paths = re.findall(
        r"(application/(?:src|frontend|terraform)/[A-Za-z0-9_./@+-]+"
        r"\.(?:java|kt|tsx|ts|jsx|js|svelte|sql|ya?ml|json|tf))",
        text,
        flags=re.IGNORECASE,
    )
    return list(dict.fromkeys(path.rstrip(".,;:)") for path in paths))


def _structured_repair_evidence(
    evidence: dict[str, object],
) -> tuple[set[str], set[str]]:
    """Read the exact refs already emitted by the Workspace Testing handoff."""

    documents = [evidence]
    test_results = evidence.get("testResults")
    if isinstance(test_results, dict):
        documents.append(test_results)
    elif isinstance(test_results, str) and test_results.lstrip().startswith("{"):
        try:
            parsed = json.loads(test_results)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            documents.append(parsed)

    refs: set[str] = set()
    task_ids: set[str] = set()
    for document in documents:
        for key in (
            "sourceRefs",
            "source_refs",
            "confirmedTargetRefs",
            "confirmed_target_refs",
        ):
            value = document.get(key, [])
            values = value if isinstance(value, list) else [value]
            refs.update(item for item in values if isinstance(item, str) and item)
        for key in ("taskId", "task_id", "failedTaskId", "failed_task_id"):
            value = document.get(key)
            if isinstance(value, str) and value:
                task_ids.add(value)
    return refs, task_ids


def _task_source_refs(task: dict[str, object]) -> set[str]:
    return {
        str(value)
        for value in task.get("source_refs", task.get("sourceRefs", []))
        if isinstance(value, str)
    }


def _task_paths(run_root: Path, task: dict[str, object]) -> set[str]:
    paths = {
        str(value).replace("\\", "/")
        for key in ("required_test_paths", "requiredTestPaths", "allowed_write_paths", "allowedWritePaths")
        for value in task.get(key, [])
        if isinstance(value, str)
    }
    context_file = task.get("context_file", task.get("contextFile"))
    context_path = run_root / context_file if isinstance(context_file, str) else None
    if context_path is not None and context_path.is_file():
        context = _read_json(context_path)
        paths.update(
            str(value).replace("\\", "/")
            for value in context.get("readSourcePaths", [])
            if isinstance(value, str)
        )
    return paths


def repair_rounds(plan: dict[str, object]) -> int:
    """한 실패가 자동 수리된 최대 횟수를 반환한다."""
    revisions = [
        int(entry.get("revision", 0))
        for entry in plan.get("entries", [])
        if isinstance(entry, dict)
    ]
    return max(revisions, default=0)


def _owner_for_paths(
    tasks: list[dict[str, object]], paths: list[str]
) -> str:
    """Return one unambiguous owner whose declared scope contains all paths."""
    if not paths:
        return ""
    owners: set[str] = set()
    for task in tasks:
        exact_paths = {
            str(path).replace("\\", "/")
            for path in task.get("allowed_write_paths", [])
        }
        roots = [
            str(path).replace("\\", "/").rstrip("/")
            for path in task.get("allowed_write_roots", [])
        ]
        if all(
            path in exact_paths
            or any(path == root or path.startswith(root + "/") for root in roots)
            for path in paths
        ):
            owner = str(task.get("owner", ""))
            if owner:
                owners.add(owner)
    return next(iter(owners)) if len(owners) == 1 else ""


def _repair_paths(
    tasks: list[dict[str, object]], owner_ids: set[str], evidence_paths: list[str]
) -> list[str]:
    """Return evidence hints already inside the owner's immutable-safe base scope.

    ``repairPaths`` is consumed by the runtime, so it must never grant a permission the
    original task did not have. ``relatedPaths`` retains the complete evidence for navigation.
    """
    owner_tasks = [
        task for task in tasks if str(task.get("task_id")) in owner_ids
    ]
    return list(
        dict.fromkeys(
            path
            for path in evidence_paths
            if any(_path_in_base_write_scope(task, path) for task in owner_tasks)
        )
    )


def _owner_digest_paths(
    tasks: list[dict[str, object]], owner_ids: set[str]
) -> list[str]:
    """Use existing owner files to notice progress when evidence names no source file."""
    return sorted(
        {
            str(path).replace("\\", "/")
            for task in tasks
            if str(task.get("task_id")) in owner_ids
            for path in task.get("allowed_write_paths", [])
            if _path_in_base_write_scope(task, str(path).replace("\\", "/"))
        }
    )


def _path_in_base_write_scope(task: dict[str, object], path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    immutable = {
        str(value).replace("\\", "/").strip("/")
        for value in task.get("immutable_paths", [])
    }
    if any(
        normalized == root or normalized.startswith(root + "/")
        for root in immutable
    ):
        return False
    exact = {
        str(value).replace("\\", "/").strip("/")
        for value in task.get("allowed_write_paths", [])
    }
    roots = {
        str(value).replace("\\", "/").strip("/")
        for value in task.get("allowed_write_roots", [])
    }
    return normalized in exact or any(
        normalized == root or normalized.startswith(root + "/")
        for root in roots
    )


def _source_digest(run_root: Path, paths: list[str]) -> str:
    """마지막으로 승인된 run source 중 수리 대상의 내용을 식별한다."""
    content: list[tuple[str, str | None]] = []
    for relative in sorted(set(paths)):
        path = run_root / relative
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        content.append((relative, digest))
    return hashlib.sha256(
        json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _repair_strategy(repeated_count: int) -> str:
    """같은 실패가 반복되면 이전 증거와 다른 진단 관점을 제안한다."""
    strategies = (
        "Edit the files named by the failure using the verification result",
        "Reproduce the failure, trace the call path, diagnose the cause, and then edit",
        "Reimplement the smallest failing part while preserving public contracts",
        "Reread the assigned feature and consistently rebuild only the failing part",
    )
    if repeated_count < len(strategies):
        strategy = strategies[repeated_count]
    else:
        # 네 문구를 다시 순환하면 요청만 달라 보일 뿐 실제 전략은 반복된다. 이전 실행의
        # 변경 파일과 검사 결과가 prompt에 함께 들어가므로, 이후에는 아직 시험하지 않은
        # 가설을 먼저 세우고 그 가설을 확인하는 새 접근을 선택하게 한다.
        strategy = (
            f"State new diagnostic hypothesis {repeated_count - len(strategies) + 1}, "
            "verify evidence not covered by prior changes, and then edit"
        )
    return strategy


def _recent_execution_history(run_root: Path, task_id: str) -> str:
    """최근 OpenHands 수리 결과를 다음 대화에 짧게 전달한다.

    repair plan만 보면 전략 이름은 알 수 있지만 실제로 어느 파일을 바꿨고 어떤 검사가 다시
    실패했는지는 알 수 없다. 다만 과거 compiler 출력을 그대로 반복하면 이미 바뀐 source의
    class나 test 이름을 현재 오류로 오해할 수 있다. 최신 세 시도의 결과와 대표 진단 한 줄만
    전달하고, 원문은 JSON 실행 기록에 보존한다.
    """
    result_path = (
        run_root / "reports" / "agent-executions" / f"{task_id}.result.json"
    )
    if not result_path.is_file():
        return ""
    try:
        result = _read_json(result_path)
    except (OSError, json.JSONDecodeError):
        return ""
    repair_history = result.get("repairHistory")
    if not isinstance(repair_history, dict):
        return ""
    attempts = [
        item for item in repair_history.get("attempts", []) if isinstance(item, dict)
    ][-3:]
    lines: list[str] = []
    for index, attempt in enumerate(attempts, 1):
        detail = _representative_diagnostic(str(attempt.get("detail", "")))
        lines.append(
            f"- Run {index}: strategy={attempt.get('strategy_key', 'unknown')}, "
            f"outcome={attempt.get('outcome', 'unknown')}, "
            f"candidate={str(attempt.get('candidate_digest', ''))[:12] or 'none'}, "
            f"evidence={detail or 'not recorded'}"
        )
    return "\n".join(lines)


def _representative_diagnostic(value: str, limit: int = 320) -> str:
    """긴 build 출력에서 다음 대화가 구분할 수 있는 대표 실패 한 줄만 고른다."""
    lines = [" ".join(line.split()) for line in value.splitlines() if line.strip()]
    markers = ("error:", "failed", "failure", "expected:", "violation", "missing")
    selected = next(
        (line for line in lines if any(marker in line.lower() for marker in markers)),
        lines[0] if lines else "",
    )
    return selected[:limit]


def _bounded_evidence(value: str, limit: int = 8000) -> str:
    if len(value) <= limit:
        return value
    half = limit // 2
    return value[:half] + "\n... middle of log omitted ...\n" + value[-half:]


def _first_evidence_line(value: str) -> str:
    """이전 실패 목록에는 첫 번째 읽을 수 있는 한 줄만 사용한다."""
    return next((line.strip() for line in value.splitlines() if line.strip()), "Failure recorded")


def _evidence_text(evidence: dict[str, object]) -> str:
    return "\n".join(
        str(evidence.get(key, ""))
        for key in ("command", "stderr", "stdout", "testResults")
        if evidence.get(key)
    ).strip()


def _task_files(run_root: Path) -> dict[str, tuple[Path, dict[str, object]]]:
    result: dict[str, tuple[Path, dict[str, object]]] = {}
    for path in (run_root / "reports" / "implementation-tasks").glob("*.task.json"):
        task = _read_json(path)
        if task.get("task_id"):
            result[str(task["task_id"])] = (path, task)
    return result


def _without_repair_directives(prompt: str) -> str:
    if REPAIR_PROMPT_START in prompt:
        return prompt.split(REPAIR_PROMPT_START, 1)[0].rstrip()
    legacy = "\n\n## Orchestrated repair and revalidation directives"
    if legacy in prompt:
        return prompt.split(legacy, 1)[0].rstrip()
    return prompt


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
