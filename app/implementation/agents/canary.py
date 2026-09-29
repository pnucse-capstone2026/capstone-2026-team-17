"""Live compatibility canary for the exact OpenHands model/tool transport."""

from __future__ import annotations

import json
import random
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.llm_connection import LlmConnection

from .harness import (
    CANARY_CHECK_TOOL,
    CANARY_READ_TOOL,
    EndpointRetryRecorder,
    HarnessCompatibilityError,
    HarnessErrorGuard,
    build_harness_manifest,
    is_provider_output_parse_failure,
    manifest_id,
)

CANARY_SCHEMA_VERSION = "easydep-openhands-tool-canary/v5"
CANARY_MODES = frozenset({"openhands", "direct-editor"})
CANARY_SYSTEM_PROMPT = """You are running an EasyDep tool-protocol canary.
Use only the supplied tools. First call easydep_canary_read with no arguments. Then pass
the exact returned marker to easydep_canary_check using its marker argument. If it returns
CANARY_PASSED, call the finish tool immediately. Do not answer with prose and do not invent
or rename tools."""
CANARY_USER_MESSAGE = "Run the tool-protocol canary now."
TRANSIENT_CANARY_FAILURES = frozenset(
    {
        "PROVIDER_RATE_LIMIT",
        "PROVIDER_TIMEOUT",
        "PROVIDER_STREAM_INCOMPLETE",
        "NETWORK_CONNECTION_ERROR",
        "PROVIDER_OUTPUT_PARSE_TRANSIENT",
    }
)
DETERMINISTIC_CANARY_FAILURES = frozenset(
    {
        "MODEL_NOT_FOUND",
        "MODEL_TOOL_PROTOCOL_INCOMPATIBLE",
        "TOOL_PROTOCOL_TOKEN_LEAK",
        "TOOL_NOT_AVAILABLE",
        "TOOL_SCHEMA_INVALID",
        "CANARY_SEQUENCE_FAILED",
    }
)


def classify_canary_exception(error: Exception) -> str:
    """Separate transport/provider failures from model-tool incompatibility."""

    name = error.__class__.__name__.casefold()
    message = str(error).casefold()
    status_code = getattr(error, "status_code", None)
    if "model not found" in message or (
        (status_code == 404 or "notfounderror" in name or "notfounderror" in message)
        and ("model" in message or "7003" in message)
    ):
        return "MODEL_NOT_FOUND"
    if "provider_output_parse_transient" in message:
        return "PROVIDER_OUTPUT_PARSE_TRANSIENT"
    if is_provider_output_parse_failure(error):
        return "PROVIDER_OUTPUT_PARSE_TRANSIENT"
    if status_code == 429 or "ratelimit" in name or "rate limit" in message:
        return "PROVIDER_RATE_LIMIT"
    if "provider_timeout" in message or "timeout" in name or "timed out" in message:
        return "PROVIDER_TIMEOUT"
    if "stream" in name and any(
        marker in message for marker in ("closed", "ended", "incomplete", "terminated")
    ):
        return "PROVIDER_STREAM_INCOMPLETE"
    if any(marker in name for marker in ("connection", "network", "dns")):
        return "NETWORK_CONNECTION_ERROR"
    if "tool" in message and any(
        marker in message for marker in ("validation", "request.tools", "protocol")
    ):
        return "MODEL_TOOL_PROTOCOL_INCOMPATIBLE"
    return "CANARY_EXECUTION_ERROR"


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _cache_is_live(result: dict[str, object], now: datetime) -> bool:
    expires_at = _parse_timestamp(result.get("cacheExpiresAt"))
    return expires_at is None or expires_at > now


def _failure_reasons(attempts: object) -> set[str]:
    if not isinstance(attempts, list):
        return set()
    return {
        str(attempt["terminationReason"])
        for attempt in attempts
        if isinstance(attempt, dict) and attempt.get("terminationReason")
    }


def _endpoint_retry_count(attempts: list[dict[str, object]]) -> int:
    count = 0
    for attempt in attempts:
        retry_data = attempt.get("endpointRetries")
        if not isinstance(retry_data, dict):
            continue
        raw_count = retry_data.get("retryCount", 0)
        if isinstance(raw_count, int):
            count += raw_count
    return count


def _safe_exception_detail(error: Exception, connection: LlmConnection) -> str:
    detail = str(error).replace(connection.api_key, "<redacted>")
    return detail[:1000]


@dataclass(slots=True)
class CanaryRecorder:
    actions: list[str] = field(default_factory=list)
    event_count: int = 0

    def __call__(self, event: object) -> None:
        self.event_count += 1
        if event.__class__.__name__ != "ActionEvent":
            return
        tool_name = getattr(event, "tool_name", None)
        if isinstance(tool_name, str):
            self.actions.append(tool_name)


def _ordered_canary_actions(actions: list[str]) -> bool:
    expected = [CANARY_READ_TOOL, CANARY_CHECK_TOOL, "finish"]
    return actions == expected


def _canary_attempt(
    connection: LlmConnection,
    llm_config: dict[str, object],
    reasoning_effort: str,
) -> dict[str, object]:
    from .runtime import create_openhands_conversation, run_openhands_conversation

    recorder = CanaryRecorder()
    endpoint_retries = EndpointRetryRecorder()
    guard = HarnessErrorGuard()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="easydep-openhands-canary-") as temporary:
        conversation, _agent = create_openhands_conversation(
            Path(temporary),
            connection,
            llm_config,
            callbacks=[recorder, guard],
            retry_listener=endpoint_retries,
            max_iterations=8,
            reasoning_effort=reasoning_effort,
            canary_tools=True,
            system_prompt_text=CANARY_SYSTEM_PROMPT,
        )
        guard.bind(conversation)
        try:
            conversation.send_message(CANARY_USER_MESSAGE)
            try:
                run_openhands_conversation(conversation)
            except Exception as error:
                try:
                    setattr(error, "endpoint_retry_snapshot", endpoint_retries.snapshot())
                except (AttributeError, TypeError):
                    pass
                raise
            execution_status = getattr(conversation.state.execution_status, "value", None)
        finally:
            conversation.close()
    passed = (
        execution_status == "finished"
        and guard.terminal_code is None
        and _ordered_canary_actions(recorder.actions)
    )
    return {
        "passed": passed,
        "executionStatus": execution_status,
        "actions": recorder.actions,
        "eventCount": recorder.event_count,
        "harnessErrorCounts": guard.counts,
        "terminationReason": guard.terminal_code,
        "durationMs": int((time.monotonic() - started) * 1000),
        "endpointRetries": endpoint_retries.snapshot(),
    }


def _direct_editor_canary_attempt(
    connection: LlmConnection,
    llm_config: dict[str, object],
    reasoning_effort: str,  # noqa: ARG001 - direct editor deliberately normalizes to low.
) -> dict[str, object]:
    """Exercise the production direct-editor request and typed write boundary once."""

    from .runtime import _request_direct_editor_action
    from .source_replace_tool import (
        SourceEditAction,
        SourceEditExecutor,
        SourceReplaceExecutor,
    )

    started = time.monotonic()
    action_name: str | None = None
    with tempfile.TemporaryDirectory(prefix="easydep-direct-editor-canary-") as temporary:
        workspace = Path(temporary)
        target = workspace / "CanaryToy.java"
        original = "class CanaryToy { int value() { return 0; } }\n"
        target.write_text(original, encoding="utf-8")
        action = _request_direct_editor_action(
            connection,
            """Edit only the supplied synthetic Java source. Change the value method to return 1.
Use one exact edit_source or replace_source tool call and preserve the class.

### `CanaryToy.java`
```java
class CanaryToy { int value() { return 0; } }
```""",
            llm_config,
            ["CanaryToy.java"],
        )
        action_name = "edit_source" if isinstance(action, SourceEditAction) else "replace_source"
        executor = (
            SourceEditExecutor(workspace, [str(target)])
            if isinstance(action, SourceEditAction)
            else SourceReplaceExecutor(workspace, [str(target)])
        )
        observation = executor(action)
        updated = target.read_text(encoding="utf-8")
    passed = not observation.is_error and updated != original and "return 1;" in updated
    return {
        "passed": passed,
        "executionStatus": "finished" if passed else "error",
        "actions": [action_name] if action_name else [],
        "eventCount": 1 if action_name else 0,
        "harnessErrorCounts": {},
        "terminationReason": None if passed else "DIRECT_EDITOR_CANARY_WRITE_FAILED",
        "durationMs": int((time.monotonic() - started) * 1000),
        "endpointRetries": {"retryCount": 0, "reasons": {}, "events": []},
    }


def _canary_contract(
    connection: LlmConnection,
    *,
    owner_tool_mode: str,
    reasoning_effort: str,
    canary_mode: str,
) -> dict[str, object]:
    contract = build_harness_manifest(
        connection,
        owner_tool_mode=owner_tool_mode,
        reasoning_effort=reasoning_effort,
    )
    if canary_mode == "direct-editor":
        contract["canaryMode"] = canary_mode
        contract["directEditorTools"] = ["replace_source", "edit_source"]
    return contract


def model_tool_canary_id(
    connection: LlmConnection,
    *,
    owner_tool_mode: str,
    reasoning_effort: str,
    canary_mode: str = "openhands",
) -> str:
    contract = _canary_contract(
        connection,
        owner_tool_mode=owner_tool_mode,
        reasoning_effort=reasoning_effort,
        canary_mode=canary_mode,
    )
    return manifest_id({**contract, "canarySchemaVersion": CANARY_SCHEMA_VERSION})


def _endpoint_circuit_path(run_root: Path, canary_id: str) -> Path:
    return (
        run_root
        / "reports"
        / "openhands-harness"
        / f"endpoint-circuit-{canary_id}.json"
    )


def open_endpoint_circuit(
    run_root: Path,
    connection: LlmConnection,
    *,
    owner_tool_mode: str,
    reasoning_effort: str,
    ttl_seconds: int,
    reason: str,
) -> dict[str, object]:
    """Persist a short circuit after a task exhausts transient LLM retries."""

    if ttl_seconds < 0:
        raise ValueError("OpenHands endpoint circuit TTL cannot be negative")
    canary_id = model_tool_canary_id(
        connection,
        owner_tool_mode=owner_tool_mode,
        reasoning_effort=reasoning_effort,
    )
    now = datetime.now(UTC)
    circuit = {
        "schemaVersion": "easydep-openhands-endpoint-circuit/v1",
        "canaryResultId": canary_id,
        "status": "open",
        "reason": reason,
        "createdAt": now.isoformat(),
        "expiresAt": (now + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    path = _endpoint_circuit_path(run_root, canary_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(circuit, ensure_ascii=False, indent=2), encoding="utf-8")
    return circuit


def _load_endpoint_circuit(run_root: Path, canary_id: str) -> dict[str, object] | None:
    try:
        loaded = json.loads(
            _endpoint_circuit_path(run_root, canary_id).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _close_endpoint_circuit(run_root: Path, canary_id: str) -> None:
    path = _endpoint_circuit_path(run_root, canary_id)
    if not path.is_file():
        return
    closed = {
        "schemaVersion": "easydep-openhands-endpoint-circuit/v1",
        "canaryResultId": canary_id,
        "status": "closed",
        "closedAt": datetime.now(UTC).isoformat(),
    }
    path.write_text(json.dumps(closed, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_model_tool_canary(
    run_root: Path,
    connection: LlmConnection,
    llm_config: dict[str, object],
    *,
    owner_tool_mode: str,
    reasoning_effort: str,
    repetitions: int,
    max_attempts: int | None = None,
    transient_failure_ttl_seconds: int = 600,
    retry_min_wait_seconds: float = 1.0,
    retry_max_wait_seconds: float = 8.0,
    retry_multiplier: float = 1.0,
    canary_mode: str = "openhands",
) -> dict[str, object]:
    """Obtain the required successful canaries under a bounded retry policy."""

    if repetitions < 1:
        raise ValueError("OpenHands canary repetitions must be positive")
    if max_attempts is None:
        max_attempts = repetitions + 2
    if max_attempts < repetitions:
        raise ValueError("OpenHands canary max attempts must cover required successes")
    if transient_failure_ttl_seconds < 0:
        raise ValueError("OpenHands canary transient TTL cannot be negative")
    if retry_min_wait_seconds < 0 or retry_max_wait_seconds < retry_min_wait_seconds:
        raise ValueError("OpenHands canary retry waits are invalid")
    if retry_multiplier < 1:
        raise ValueError("OpenHands canary retry multiplier must be at least one")
    if canary_mode not in CANARY_MODES:
        raise ValueError(f"Unsupported OpenHands canary mode: {canary_mode}")
    policy = {
        "requiredSuccesses": repetitions,
        "maxAttempts": max_attempts,
        "transientFailureTtlSeconds": transient_failure_ttl_seconds,
        "retryMinWaitSeconds": retry_min_wait_seconds,
        "retryMaxWaitSeconds": retry_max_wait_seconds,
        "retryMultiplier": retry_multiplier,
    }
    contract = _canary_contract(
        connection,
        owner_tool_mode=owner_tool_mode,
        reasoning_effort=reasoning_effort,
        canary_mode=canary_mode,
    )
    canary_id = model_tool_canary_id(
        connection,
        owner_tool_mode=owner_tool_mode,
        reasoning_effort=reasoning_effort,
        canary_mode=canary_mode,
    )
    now = datetime.now(UTC)
    circuit = _load_endpoint_circuit(run_root, canary_id)
    force_refresh = False
    if isinstance(circuit, dict) and circuit.get("status") == "open":
        expires_at = _parse_timestamp(circuit.get("expiresAt"))
        if expires_at is None or expires_at > now:
            raise HarnessCompatibilityError(
                "ENDPOINT_DEGRADED: task-level endpoint circuit is open"
            )
        force_refresh = True
    result_path = run_root / "reports" / "openhands-harness" / f"canary-{canary_id}.json"
    cached: dict[str, object] | None = None
    if result_path.is_file():
        try:
            loaded = json.loads(result_path.read_text(encoding="utf-8"))
            cached = loaded if isinstance(loaded, dict) else None
        except (OSError, json.JSONDecodeError):
            cached = None
        now = datetime.now(UTC)
        if (
            isinstance(cached, dict)
            and cached.get("policy") == policy
            and cached.get("contract") == contract
        ):
            if cached.get("passed") is True:
                if not force_refresh:
                    return cached
            else:
                cached_reasons = _failure_reasons(cached.get("attempts"))
                deterministic_reasons = (
                    cached_reasons & DETERMINISTIC_CANARY_FAILURES
                )
                if deterministic_reasons:
                    cached_reason = sorted(deterministic_reasons)[0]
                    raise HarnessCompatibilityError(
                        f"{cached_reason}: cached OpenHands canary failure"
                    )
                if _cache_is_live(cached, now):
                    raise HarnessCompatibilityError(
                        "ENDPOINT_DEGRADED: cached transient OpenHands canary failure"
                    )

    attempts: list[dict[str, object]] = []
    successful_attempts = 0
    transient_failures = 0
    deterministic_failure = False
    while len(attempts) < max_attempts and successful_attempts < repetitions:
        try:
            attempt = (
                _direct_editor_canary_attempt(connection, llm_config, reasoning_effort)
                if canary_mode == "direct-editor"
                else _canary_attempt(connection, llm_config, reasoning_effort)
            )
        except Exception as error:
            attempt = {
                "passed": False,
                "executionStatus": "error",
                "actions": [],
                "eventCount": 0,
                "harnessErrorCounts": {},
                "terminationReason": classify_canary_exception(error),
                "exceptionType": error.__class__.__name__,
                "exceptionDetail": _safe_exception_detail(error, connection),
                "durationMs": None,
                "endpointRetries": getattr(
                    error,
                    "endpoint_retry_snapshot",
                    {"retryCount": 0, "reasons": {}, "events": []},
                ),
            }
        if attempt.get("passed") is True:
            successful_attempts += 1
            attempt["backoffMs"] = 0
        else:
            reason = str(attempt.get("terminationReason") or "CANARY_SEQUENCE_FAILED")
            attempt["terminationReason"] = reason
            if reason in DETERMINISTIC_CANARY_FAILURES:
                deterministic_failure = True
                attempt["backoffMs"] = 0
                attempts.append(attempt)
                break
            if reason in TRANSIENT_CANARY_FAILURES:
                transient_failures += 1
                remaining_capacity = max_attempts - len(attempts) - 1
                needed_successes = repetitions - successful_attempts
                if remaining_capacity >= needed_successes:
                    ceiling = max(
                        retry_min_wait_seconds,
                        min(
                            retry_max_wait_seconds,
                            retry_multiplier
                            * (2 ** max(0, transient_failures - 1)),
                        ),
                    )
                    delay = random.uniform(0, ceiling)  # noqa: S311
                    attempt["backoffMs"] = int(delay * 1000)
                    attempts.append(attempt)
                    time.sleep(delay)
                    continue
            attempt["backoffMs"] = 0
            attempts.append(attempt)
            break
        attempts.append(attempt)
    previous_results: list[dict[str, object]] = []
    if isinstance(cached, dict) and cached.get("contract") == contract:
        existing_history = cached.get("previousResults")
        if isinstance(existing_history, list):
            previous_results.extend(
                item for item in existing_history if isinstance(item, dict)
            )
        previous_results.append(
            {
                "createdAt": cached.get("createdAt"),
                "passed": cached.get("passed"),
                "attempts": cached.get("attempts"),
            }
        )
    endpoint_retry_count = _endpoint_retry_count(attempts)
    result = {
        "schemaVersion": CANARY_SCHEMA_VERSION,
        "canaryResultId": canary_id,
        "createdAt": datetime.now(UTC).isoformat(),
        "contract": contract,
        # ``repetitions`` remains for old report readers; policy is authoritative.
        "repetitions": repetitions,
        "policy": policy,
        "passed": successful_attempts >= repetitions and not deterministic_failure,
        "successfulAttempts": successful_attempts,
        "attemptCount": len(attempts),
        "transientFailureCount": transient_failures,
        "endpointRetryCount": endpoint_retry_count,
        "attempts": attempts,
        "previousResults": previous_results,
    }
    if result["passed"] is True:
        result["endpointHealth"] = (
            "recovered"
            if transient_failures or endpoint_retry_count
            else "healthy"
        )
        result["cacheExpiresAt"] = None
    elif deterministic_failure:
        result["endpointHealth"] = (
            "misconfigured"
            if "MODEL_NOT_FOUND" in _failure_reasons(attempts)
            else "incompatible"
        )
        result["cacheExpiresAt"] = None
    else:
        result["endpointHealth"] = "open"
        result["cacheExpiresAt"] = (
            datetime.now(UTC) + timedelta(seconds=transient_failure_ttl_seconds)
        ).isoformat()
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if result["passed"] is not True:
        failed_attempt = next(
            attempt for attempt in attempts if attempt.get("passed") is not True
        )
        reason = (
            failed_attempt.get("terminationReason")
            if deterministic_failure
            else "ENDPOINT_DEGRADED"
        )
        raise HarnessCompatibilityError(f"{reason}: OpenHands canary failed")
    _close_endpoint_circuit(run_root, canary_id)
    return result
