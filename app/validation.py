"""Shared contracts for deterministic validation and repair audit history."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Generic, Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, field_validator

ArtifactT = TypeVar("ArtifactT")
ContextT = TypeVar("ContextT")

FindingOrigin = Literal["schema", "deterministic", "semantic"]
ValidationStatus = Literal["clean", "findings", "needs_input", "disabled", "error"]
RepairStatus = Literal[
    "ACTIVE",
    "WAITING_EXTERNAL",
    "STALLED",
    "NEEDS_INPUT",
    "COMPLETED",
    "CANCELLED",
]
RepairOutcome = Literal[
    "improved",
    "clean",
    "repeated_candidate",
    "no_improvement",
    "regressed",
    "waiting_external",
    "error",
]


class Finding(BaseModel):
    """One validation finding, independent of its domain presentation."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    message: str
    location: str | None = None
    requires_user_input: bool = False
    origin: FindingOrigin = "deterministic"

    def __init__(
        self,
        rule_id: str,
        message: str,
        location: str | None = None,
        requires_user_input: bool = False,
        origin: FindingOrigin = "deterministic",
        **data: Any,
    ) -> None:
        """Retain the compact positional construction used by existing checks."""
        super().__init__(
            rule_id=rule_id,
            message=message,
            location=location,
            requires_user_input=requires_user_input,
            origin=origin,
            **data,
        )


class ValidationReport(BaseModel):
    """Typed outcome from one validation lane."""

    model_config = ConfigDict(frozen=True)

    status: ValidationStatus
    findings: tuple[Finding, ...] = ()
    checked_rule_ids: tuple[str, ...] = ()
    unexamined_rule_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class RepairAttempt(BaseModel):
    """한 번의 의미 수리와 수용 여부를 재현하는 감사 레코드."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str = ""
    stage: str
    target_ids: tuple[str, ...] = ()
    strategy_key: str
    input_digest: str
    candidate_digest: str = ""
    finding_keys_before: tuple[str, ...] = ()
    finding_keys_after: tuple[str, ...] = ()
    outcome: RepairOutcome
    detail: str = ""
    created_at: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    elapsed_ms: float | None = None

    def model_post_init(self, __context: Any) -> None:
        if not self.attempt_id:
            object.__setattr__(self, "attempt_id", str(uuid4()))
        if not self.created_at:
            object.__setattr__(self, "created_at", datetime.now(UTC).isoformat())


class RedoRepairAttempt(BaseModel):
    """상위 요구사항 단계로 올린 수리 한 번의 공개 요약이다."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    round: int = 0
    target: str = ""
    source: str = ""
    reason: str = ""
    escalated: bool = False
    rule_ids: tuple[str, ...] = ()
    strategy_key: str = ""
    input_digest: str = ""


class BlockingFinding(BaseModel):
    """UI와 자동 실행기가 공유하는 수리 가능 차단 사유다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    stage: str
    target_ids: tuple[str, ...] = ()
    message: str
    severity: Literal["error", "warning"] = "error"
    repairable: bool


class RepairStateSummary(BaseModel):
    """외부 응답에 노출하는 수리 에피소드의 안정된 요약 계약이다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RepairStatus
    attempt_count: int = 0
    accepted_count: int = 0
    recent_attempts: tuple[RepairAttempt | RedoRepairAttempt, ...] = ()
    tried_strategies: tuple[str, ...] = ()
    rejected_candidate_digests: tuple[str, ...] = ()
    finding_digest: str = ""
    stall_reason: str = ""


class RepairLedger(BaseModel):
    """숫자 예산 대신 진전과 미사용 전략으로 종료를 보장하는 수리 기록."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["RepairHistory/v1"] = "RepairHistory/v1"
    episode_id: str = ""
    status: RepairStatus = "ACTIVE"
    attempts: list[RepairAttempt] = []
    stall_reason: str = ""
    next_retry_at: str | None = None

    @field_validator("stall_reason", mode="before")
    @classmethod
    def _normalize_empty_stall_reason(cls, value: Any) -> str:
        """진행 중인 이력이 저장한 null을 공개 문자열 형태로 정리한다."""

        return "" if value is None else str(value)

    def model_post_init(self, __context: Any) -> None:
        if not self.episode_id:
            self.episode_id = str(uuid4())

    def strategy_attempted(
        self,
        *,
        input_digest: str,
        finding_keys: Sequence[str],
        strategy_key: str,
    ) -> bool:
        signature = tuple(sorted(set(finding_keys)))
        return any(
            attempt.input_digest == input_digest
            and attempt.finding_keys_before == signature
            and attempt.strategy_key == strategy_key
            for attempt in self.attempts
        )

    def candidate_seen(self, *, input_digest: str, candidate_digest: str) -> bool:
        return bool(candidate_digest) and any(
            attempt.input_digest == input_digest
            and attempt.candidate_digest == candidate_digest
            for attempt in self.attempts
        )

    def failure_seen(
        self,
        *,
        input_digest: str,
        finding_keys: Sequence[str],
    ) -> bool:
        """후보가 달라도 같은 검증 실패가 두 번 누적됐는지 확인한다.

        한 번의 재발만으로 멈추면 서로 다른 수리안을 시도하는 정상 흐름도 막을 수 있다.
        따라서 이전 기록에 같은 실패가 두 번 있을 때, 즉 현재 결과가 세 번째 같은
        실패일 때만 정체로 판단한다. 이는 전체 수리 횟수 제한이 아니라 같은 상태를
        반복하는 경우만 끝내는 조건이다.
        """
        signature = tuple(sorted(set(finding_keys)))
        matching_attempts = sum(
            1
            for attempt in self.attempts
            if (
                attempt.input_digest == input_digest
                and attempt.finding_keys_before == signature
            )
        )
        return bool(signature) and matching_attempts >= 2

    def record(self, attempt: RepairAttempt) -> None:
        self.attempts.append(attempt)

    def prompt_context(self, *, recent: int = 5) -> str:
        """모든 고유 실패는 남기고 오래된 상세만 결정론적으로 압축한다."""
        rejected = [
            attempt
            for attempt in self.attempts
            if attempt.outcome not in {"improved", "clean"}
        ]
        unique_findings = sorted(
            {
                finding
                for attempt in rejected
                for finding in (
                    *attempt.finding_keys_before,
                    *attempt.finding_keys_after,
                )
            }
        )
        tried_strategies = sorted({attempt.strategy_key for attempt in self.attempts})
        rejected_candidates = sorted(
            {attempt.candidate_digest for attempt in rejected if attempt.candidate_digest}
        )
        recent_attempts = [
            {
                "strategy": attempt.strategy_key,
                "outcome": attempt.outcome,
                "findingsBefore": list(attempt.finding_keys_before),
                "findingsAfter": list(attempt.finding_keys_after),
                "candidateDigest": attempt.candidate_digest,
                "detail": attempt.detail,
            }
            for attempt in self.attempts[-max(0, recent):]
        ]
        return json.dumps(
            {
                "instruction": "Do not repeat a tried strategy or rejected candidate.",
                "uniqueFailedFindings": unique_findings,
                "triedStrategies": tried_strategies,
                "rejectedCandidateDigests": rejected_candidates,
                "olderAttemptCount": max(0, len(self.attempts) - len(recent_attempts)),
                "recentAttempts": recent_attempts,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )

    def prompt_context_for_state(
        self,
        *,
        input_digest: str,
        finding_keys: Sequence[str],
    ) -> str:
        """현재 산출물과 같은 실패 상태에서 이미 시도한 내용만 LLM에 보여 준다.

        전체 ledger는 종료 판정과 실행 기록에 계속 사용한다. LLM에는 현재 명세를 고치는
        데 직접 도움이 되는 시도만 보내, 이전에 이미 개선한 상태의 오류와 해시 문자열이
        다음 수리를 방해하지 않게 한다.
        """

        signature = tuple(sorted(set(finding_keys)))
        attempts = [
            {
                "strategy": attempt.strategy_key,
                "outcome": attempt.outcome,
                "findingsAfter": list(attempt.finding_keys_after),
            }
            for attempt in self.attempts
            if (
                attempt.input_digest == input_digest
                and attempt.finding_keys_before == signature
            )
        ]
        if not attempts:
            return ""
        return json.dumps(
            {"previousAttempts": attempts},
            ensure_ascii=False,
            sort_keys=True,
        )


def stable_digest(value: Any) -> str:
    """JSON-compatible 값의 정규화 SHA-256 digest."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def repair_makes_progress(
    before: Sequence[str],
    after: Sequence[str],
    *,
    frontier_before: int = 0,
    frontier_after: int = 0,
) -> bool:
    """결함 집합이 줄거나 검증 의존 단계가 앞으로 간 후보만 수용한다."""
    before_set = set(before)
    after_set = set(after)
    return not after_set or after_set < before_set or frontier_after > frontier_before


def transient_llm_error(error: BaseException) -> bool:
    """LLM transport·과부하 오류를 의미 결함과 구별한다."""
    text = f"{type(error).__name__}: {error}".casefold()
    return any(
        marker in text
        for marker in (
            "429",
            "rate limit",
            "too many requests",
            "timeout",
            "timed out",
            "connection",
            "temporarily unavailable",
            "service unavailable",
            "overloaded",
            "bad gateway",
            "gateway timeout",
            "502",
            "503",
            "504",
        )
    )


def repair_retry_delay(failure_count: int) -> int:
    """외부 장애 대기 간격. 시도 수는 제한하지 않고 간격만 5분으로 제한한다."""
    schedule = (5, 15, 30, 60, 300)
    return schedule[min(max(0, failure_count - 1), len(schedule) - 1)]


CheckFn = Callable[[ArtifactT, ContextT], Sequence[Finding]]


@dataclass(frozen=True)
class CheckSpec(Generic[ArtifactT, ContextT]):
    """One deterministic rule and the pure function which judges it."""

    rule_id: str
    run: CheckFn[ArtifactT, ContextT]
    parallel_safe: bool = True


def _finding_key(finding: Finding) -> tuple[str, str, str, bool, FindingOrigin]:
    return (
        finding.rule_id,
        finding.location or "",
        finding.message,
        finding.requires_user_input,
        finding.origin,
    )


def _run_one(
    spec: CheckSpec[ArtifactT, ContextT], artifact: ArtifactT, context: ContextT
) -> tuple[tuple[Finding, ...], str | None]:
    try:
        findings = tuple(spec.run(artifact, context))
    except Exception as exc:  # noqa: BLE001 - report a broken check without hiding siblings
        return (), f"{spec.rule_id}: {type(exc).__name__}: {exc}"

    unexpected = sorted({finding.rule_id for finding in findings} - {spec.rule_id})
    if unexpected:
        return (), (
            f"{spec.rule_id}: check emitted findings for other rules: "
            f"{', '.join(unexpected)}"
        )
    return findings, None


def run_checks(
    checks: Sequence[CheckSpec[ArtifactT, ContextT]],
    artifact: ArtifactT,
    context: ContextT,
    *,
    parallel: bool = False,
    max_workers: int | None = None,
    deduplicate: bool = True,
) -> ValidationReport:
    """Run checks and merge their findings in registration order.

    Parallel completion order is deliberately not observable in the report.
    """
    rule_ids = tuple(check.rule_id for check in checks)
    if parallel and any(not check.parallel_safe for check in checks):
        unsafe = ", ".join(check.rule_id for check in checks if not check.parallel_safe)
        return ValidationReport(
            status="error",
            checked_rule_ids=rule_ids,
            errors=(f"parallel execution requested for non-parallel checks: {unsafe}",),
        )

    if parallel and len(checks) > 1:
        workers = max(1, min(len(checks), max_workers or len(checks)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_run_one, check, artifact, context) for check in checks]
            results = [future.result() for future in futures]
    else:
        results = [_run_one(check, artifact, context) for check in checks]

    findings: list[Finding] = []
    seen: set[tuple[str, str, str, bool, FindingOrigin]] = set()
    errors: list[str] = []
    for emitted, error in results:
        if error:
            errors.append(error)
            continue
        for finding in emitted:
            key = _finding_key(finding)
            if not deduplicate or key not in seen:
                seen.add(key)
                findings.append(finding)

    if errors:
        status: ValidationStatus = "error"
    elif findings:
        status = "needs_input" if all(finding.requires_user_input for finding in findings) else "findings"
    else:
        status = "clean"
    return ValidationReport(
        status=status,
        findings=tuple(findings),
        checked_rule_ids=rule_ids,
        errors=tuple(errors),
    )
