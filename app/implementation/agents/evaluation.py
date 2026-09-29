"""Metrics for controlled OpenHands harness comparisons."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .harness import classify_harness_error_text

_PERMISSION_FAILURE = re.compile(
    r"permission denied|operation not permitted|access is denied",
    flags=re.IGNORECASE,
)


def _attempt_results(run_root: Path) -> list[tuple[Path, dict[str, object]]]:
    execution_dir = run_root / "reports" / "agent-executions"
    results: list[tuple[Path, dict[str, object]]] = []
    for path in sorted(execution_dir.glob("*.attempt-*.result.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            results.append((path, value))
    return results


def _token_usage(result: dict[str, object]) -> int:
    stats = result.get("conversationStats")
    if not isinstance(stats, dict):
        return 0
    usage_map = stats.get("usage_to_metrics")
    if not isinstance(usage_map, dict):
        return 0
    total = 0
    for metrics in usage_map.values():
        if not isinstance(metrics, dict):
            continue
        usage = metrics.get("accumulated_token_usage")
        if not isinstance(usage, dict):
            continue
        total += int(usage.get("prompt_tokens") or 0)
        total += int(usage.get("completion_tokens") or 0)
    return total


def _journal_metrics(path: Path) -> dict[str, object]:
    model_response_ids: set[str] = set()
    tool_calls = 0
    error_counts: dict[str, int] = {}
    false_successes = 0
    first_timestamp: float | None = None
    first_edit_timestamp: float | None = None
    first_error_timestamp: float | None = None
    recovery_timestamp: float | None = None
    last_timestamp: float | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        timestamp = record.get("timestamp")
        if isinstance(timestamp, (int, float)) and first_timestamp is None:
            first_timestamp = float(timestamp)
        if isinstance(timestamp, (int, float)):
            last_timestamp = float(timestamp)
        event = record.get("event")
        if not isinstance(event, dict):
            continue
        response_id = event.get("llm_response_id")
        if isinstance(response_id, str) and response_id:
            model_response_ids.add(response_id)
        direct_edit = (
            record.get("type") == "DirectEditorAction"
            and isinstance(event.get("path"), str)
            and bool(event["path"].strip())
            and isinstance(event.get("sourceSha256"), str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", event["sourceSha256"]) is not None
        )
        if direct_edit:
            tool_calls += 1
            if first_edit_timestamp is None and isinstance(timestamp, (int, float)):
                first_edit_timestamp = float(timestamp)
            if (
                first_error_timestamp is not None
                and recovery_timestamp is None
                and isinstance(timestamp, (int, float))
            ):
                recovery_timestamp = float(timestamp)
        if record.get("type") == "ActionEvent":
            tool_calls += 1
            action = event.get("action")
            if (
                first_edit_timestamp is None
                and record.get("tool") == "file_editor"
                and isinstance(action, dict)
                and action.get("command") != "view"
                and isinstance(timestamp, (int, float))
            ):
                first_edit_timestamp = float(timestamp)
            if (
                first_error_timestamp is not None
                and recovery_timestamp is None
                and record.get("tool") == "file_editor"
                and isinstance(action, dict)
                and action.get("command") != "view"
                and isinstance(timestamp, (int, float))
            ):
                recovery_timestamp = float(timestamp)
        encoded = json.dumps(event, ensure_ascii=False)
        classified = classify_harness_error_text(encoded)
        if classified is not None:
            error_counts[classified.code] = error_counts.get(classified.code, 0) + 1
            if first_error_timestamp is None and isinstance(timestamp, (int, float)):
                first_error_timestamp = float(timestamp)
        if record.get("type") != "ObservationEvent":
            continue
        observation = event.get("observation")
        if not isinstance(observation, dict):
            continue
        content = json.dumps(observation.get("content"), ensure_ascii=False)
        if (
            observation.get("exit_code") == 0
            and observation.get("is_error") is not True
            and _PERMISSION_FAILURE.search(content)
        ):
            false_successes += 1
    return {
        "modelResponses": len(model_response_ids),
        "toolCalls": tool_calls,
        "errorCounts": error_counts,
        "falseSuccessStates": false_successes,
        "firstValidEditMs": (
            int((first_edit_timestamp - first_timestamp) * 1000)
            if first_edit_timestamp is not None and first_timestamp is not None
            else None
        ),
        "errorRecoveryMs": (
            int(
                (
                    (
                        recovery_timestamp
                        if recovery_timestamp is not None
                        else last_timestamp
                    )
                    - first_error_timestamp
                )
                * 1000
            )
            if first_error_timestamp is not None and last_timestamp is not None
            else None
        ),
    }


def _persistence_error_counts(run_root: Path) -> dict[str, int]:
    """Read SDK error events that may not have reached the public callback journal."""

    counts: dict[str, int] = {}
    persistence = run_root / "reports" / "openhands-conversations"
    for path in persistence.glob("*/events/event-*.json"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        classified = classify_harness_error_text(text)
        if classified is not None:
            counts[classified.code] = counts.get(classified.code, 0) + 1
    return counts


def evaluate_harness_run(run_root: Path) -> dict[str, object]:
    """Aggregate task results and public event journals for one implementation run."""

    attempts = _attempt_results(run_root)
    totals = {
        "modelResponses": 0,
        "toolCalls": 0,
        "falseSuccessStates": 0,
        "durationMs": 0,
        "tokens": 0,
    }
    errors: dict[str, int] = {}
    first_edit_values: list[int] = []
    recovery_values: list[int] = []
    task_final: dict[str, bool] = {}
    contracts: list[dict[str, object]] = []
    for result_path, result in attempts:
        task_id = str(result.get("taskId") or result_path.name.split(".attempt-")[0])
        task_final[task_id] = result.get("status") == "SUCCEEDED"
        totals["durationMs"] += int(result.get("durationMs") or 0)
        totals["tokens"] += _token_usage(result)
        contracts.append(
            {
                "taskId": task_id,
                "promptSha256": result.get("promptSha256"),
                "effectiveModel": result.get("effectiveModel"),
                "harnessManifest": result.get("harnessManifest"),
            }
        )
        journal_value = result.get("eventJournal")
        if not isinstance(journal_value, str):
            continue
        journal = run_root / journal_value
        if not journal.is_file():
            continue
        metrics = _journal_metrics(journal)
        for key in ("modelResponses", "toolCalls", "falseSuccessStates"):
            totals[key] += int(metrics[key])
        first_edit = metrics["firstValidEditMs"]
        if isinstance(first_edit, int):
            first_edit_values.append(first_edit)
        recovery = metrics["errorRecoveryMs"]
        if isinstance(recovery, int):
            recovery_values.append(recovery)
        for code, count in metrics["errorCounts"].items():
            errors[code] = errors.get(code, 0) + int(count)

    responses = totals["modelResponses"]
    calls = totals["toolCalls"]
    task_count = len(task_final)
    for code, count in _persistence_error_counts(run_root).items():
        # The SDK persistence log and EasyDep callback journal overlap for
        # ordinary events. Use the larger observation rather than double-count.
        errors[code] = max(errors.get(code, 0), count)
    return {
        "schemaVersion": "easydep-openhands-harness-evaluation/v1",
        "runRoot": str(run_root.resolve()),
        "attemptCount": len(attempts),
        "taskCount": task_count,
        "metrics": {
            **totals,
            "invalidToolCallRate": (
                errors.get("TOOL_NOT_AVAILABLE", 0) / responses if responses else 0.0
            ),
            "schemaErrorRate": (
                errors.get("TOOL_SCHEMA_INVALID", 0) / calls if calls else 0.0
            ),
            "protocolLeaks": errors.get("TOOL_PROTOCOL_TOKEN_LEAK", 0),
            "workspaceViolations": errors.get("PATH_OUTSIDE_WORKSPACE", 0)
            + errors.get("WRITE_OUTSIDE_OWNER_SCOPE", 0),
            "firstValidEditMs": min(first_edit_values) if first_edit_values else None,
            "errorRecoveryMs": sum(recovery_values) if recovery_values else None,
            "finalTaskSuccessRate": (
                sum(task_final.values()) / task_count if task_count else 0.0
            ),
            "harnessErrorCounts": errors,
        },
        "contracts": contracts,
    }


def compare_harness_runs(baseline: Path, candidate: Path) -> dict[str, object]:
    """Compare runs and explicitly report whether conditions are compatible."""

    baseline_report = evaluate_harness_run(baseline)
    candidate_report = evaluate_harness_run(candidate)
    baseline_contracts = {
        (item.get("taskId"), item.get("promptSha256"), item.get("effectiveModel"))
        for item in baseline_report["contracts"]
    }
    candidate_contracts = {
        (item.get("taskId"), item.get("promptSha256"), item.get("effectiveModel"))
        for item in candidate_report["contracts"]
    }
    missing_conditions = []
    if baseline_report["attemptCount"] == 0:
        missing_conditions.append("baseline has no recorded attempts")
    if candidate_report["attemptCount"] == 0:
        missing_conditions.append("candidate has no recorded attempts")
    if baseline_contracts != candidate_contracts:
        missing_conditions.append("task/prompt/model contracts differ")
    return {
        "schemaVersion": "easydep-openhands-harness-comparison/v1",
        "comparable": not missing_conditions,
        "conditionErrors": missing_conditions,
        "baseline": baseline_report,
        "candidate": candidate_report,
    }
