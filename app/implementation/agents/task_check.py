"""OpenHands가 현재 구현 작업의 검사만 직접 실행하게 한다.

코딩 에이전트에 일반 터미널을 주면 계획에 없던 명령이나 전체 빌드를 실행할 수 있다.
이 모듈은 그런 범용 실행 기능 대신, EasyDep이 이미 선택한 focused test 또는 compile 명령만
호출하는 ``run_task_check`` 도구를 제공한다. 실제 명령 선택과 실행은 기존 검증 코드를
그대로 사용하므로 에이전트 내부 검사와 EasyDep의 마지막 검사가 서로 달라지지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .verification.build import (
    WorkspaceVerificationError,
    compact_verification_evidence,
    verify_agent_workspace,
)
from .workspace import snapshot_files

TASK_CHECK_TOOL_NAME = "run_task_check"
_REGISTERED = False
_REGISTRATION_LOCK = threading.Lock()
_SUCCESSFUL_CHECKS_LOCK = threading.Lock()


@dataclass(frozen=True)
class _SuccessfulCheck:
    source_snapshot: dict[str, str]
    task_type: str
    allowed_paths: tuple[str, ...]
    verification_profile_digest: str
    evidence: dict[str, object]


_SUCCESSFUL_CHECKS: dict[str, _SuccessfulCheck] = {}

# Infrastructure diagnostics do not identify a source repair.
def is_infrastructure_task_check_failure(output: str) -> bool:
    normalized = output.casefold()
    return ('npm' in normalized and 'ci' in normalized and 'timed out' in normalized) or 'enomem' in normalized or 'tar_entry_error' in normalized


@dataclass
class TaskCheckSession:
    """한 OpenHands 대화에서 검사 실패와 source 변경 여부를 기억한다."""

    sandbox: Path
    task_type: str
    allowed_write_paths: list[str]
    verification_profile: dict[str, object] | None = None
    frontend_unit_report_path: Path | None = None
    _failed_source_snapshot: dict[str, str] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _last_evidence: dict[str, object] | None = field(default=None, init=False, repr=False)

    @property
    def last_evidence(self) -> dict[str, object] | None:
        """Latest canonical evidence; callers never need to parse display text."""

        return self._last_evidence

    def run(self) -> tuple[bool, str]:
        """source가 바뀐 경우에만 실제 focused 검사를 실행한다."""
        current_snapshot = snapshot_files(self.sandbox)
        if (
            self._failed_source_snapshot is not None
            and current_snapshot == self._failed_source_snapshot
        ):
            return False, (
                "TASK CHECK NOT RUN\n"
                "The source has not changed since the previous failed check. "
                "Read the previous diagnostic and edit the implementation before "
                "running this check again."
            )

        passed, output, evidence = _execute_task_check(
            self.sandbox,
            self.task_type,
            self.allowed_write_paths,
            self.verification_profile,
            self.frontend_unit_report_path,
        )
        self._last_evidence = evidence
        self._failed_source_snapshot = None if passed else current_snapshot
        if passed and evidence is not None:
            key = str(self.sandbox.resolve())
            with _SUCCESSFUL_CHECKS_LOCK:
                _SUCCESSFUL_CHECKS[key] = _SuccessfulCheck(
                    source_snapshot=current_snapshot,
                    task_type=self.task_type,
                    allowed_paths=_normalized_paths(self.allowed_write_paths),
                    verification_profile_digest=_profile_digest(self.verification_profile),
                    evidence=evidence,
                )
        return passed, output


def run_task_check(
    sandbox: Path,
    task_type: str,
    allowed_write_paths: list[str],
    verification_profile: dict[str, object] | None = None,
    frontend_unit_report_path: Path | None = None,
) -> tuple[bool, str]:
    """현재 작업에 정해진 검사를 실행하고 에이전트가 읽을 짧은 결과를 반환한다.

    ``allowed_write_paths``는 명령 인자가 아니라 EasyDep이 작업 계획에서 만든 값이다.
    LLM은 이 함수를 호출할 수만 있고 검사 종류나 Gradle 옵션을 바꿀 수 없다.
    """
    return TaskCheckSession(
        sandbox,
        task_type,
        allowed_write_paths,
        verification_profile,
        frontend_unit_report_path,
    ).run()


def _execute_task_check(
    sandbox: Path,
    task_type: str,
    allowed_write_paths: list[str],
    verification_profile: dict[str, object] | None = None,
    frontend_unit_report_path: Path | None = None,
) -> tuple[bool, str, dict[str, object] | None]:
    try:
        if verification_profile is None:
            # 기존 구현 task의 호출 계약은 그대로 유지한다.
            evidence = verify_agent_workspace(
                sandbox,
                task_type,
                allowed_write_paths,
                frontend_unit_report_path=frontend_unit_report_path,
            )
        else:
            evidence = verify_agent_workspace(
                sandbox,
                task_type,
                allowed_write_paths,
                verification_profile,
                frontend_unit_report_path,
            )
    except WorkspaceVerificationError as error:
        return False, _render_check_result("FAILED", error.evidence), error.evidence
    except Exception as error:  # 도구 실행 자체의 문제도 대화 안에서 확인할 수 있게 한다.
        return False, (
            "TASK CHECK COULD NOT RUN\n"
            f"{error.__class__.__name__}: {error}"
        ), None
    return True, _render_check_result("PASSED", evidence), evidence


def consume_successful_task_check(
    sandbox: Path,
    task_type: str,
    allowed_write_paths: list[str],
    verification_profile: dict[str, object] | None = None,
) -> dict[str, object] | None:
    """같은 source에서 방금 성공한 에이전트 내부 검사 결과를 한 번 재사용한다."""
    key = str(sandbox.resolve())
    with _SUCCESSFUL_CHECKS_LOCK:
        cached = _SUCCESSFUL_CHECKS.pop(key, None)
    if cached is None:
        return None
    if (
        cached.task_type != task_type
        or cached.allowed_paths != _normalized_paths(allowed_write_paths)
        or cached.verification_profile_digest != _profile_digest(verification_profile)
        or cached.source_snapshot != snapshot_files(sandbox)
    ):
        return None
    return {**cached.evidence, "reusedFromTaskCheck": True}


def has_successful_task_check(
    sandbox: Path,
    task_type: str,
    allowed_write_paths: list[str],
    verification_profile: dict[str, object] | None = None,
) -> bool:
    """Return whether the unchanged candidate has already passed its assigned check."""

    key = str(sandbox.resolve())
    with _SUCCESSFUL_CHECKS_LOCK:
        cached = _SUCCESSFUL_CHECKS.get(key)
    return bool(
        cached is not None
        and cached.task_type == task_type
        and cached.allowed_paths == _normalized_paths(allowed_write_paths)
        and cached.verification_profile_digest == _profile_digest(verification_profile)
        and cached.source_snapshot == snapshot_files(sandbox)
    )


def _normalized_paths(paths: list[str]) -> tuple[str, ...]:
    return tuple(sorted(str(path).replace("\\", "/") for path in paths))


def _profile_digest(profile: dict[str, object] | None) -> str:
    encoded = json.dumps(profile or {}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def register_task_check_tool() -> str:
    """OpenHands SDK에 제한된 검사 도구를 한 번만 등록한다.

    OpenHands는 선택 설치 항목이므로 SDK import는 실제 구현 에이전트를 만들 때까지 미룬다.
    계획 조회나 mock 실행에서는 이 모듈을 import해도 OpenHands 설치를 요구하지 않는다.
    """
    global _REGISTERED

    if _REGISTERED:
        return TASK_CHECK_TOOL_NAME

    with _REGISTRATION_LOCK:
        if _REGISTERED:
            return TASK_CHECK_TOOL_NAME

        from openhands.sdk.tool import register_tool

        from .task_check_tool import TaskCheckTool

        register_tool(TASK_CHECK_TOOL_NAME, TaskCheckTool)
        _REGISTERED = True

    return TASK_CHECK_TOOL_NAME


def _render_check_result(status: str, evidence: dict[str, object]) -> str:
    """에이전트가 바로 수정에 사용할 수 있는 짧은 진단만 반환한다.

    원본 Gradle HTML·JUnit XML 경로를 알려 주면 에이전트가 수십만 자 보고서를 반복해서
    읽으며 작업 횟수와 문맥을 소진한다. 원문은 사용자용 실행 증거로 그대로 보존하되,
    코딩 대화에는 검증기가 추출한 대표 실패와 가장 안쪽 원인만 전달한다.
    """
    return f"TASK CHECK {status}\n{compact_verification_evidence(evidence)}"
