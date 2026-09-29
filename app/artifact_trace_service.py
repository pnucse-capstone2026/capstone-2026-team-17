"""저장된 산출물에서 화면용 추적 조회 결과를 만든다.

이 모듈은 새로운 추적표를 저장하지 않는다. 현재 설계 state, 최신 source snapshot에
보관된 구현 RTM, 그리고 마지막 Testing 결과를 한 번 읽어 기존 순수 projection에
전달할 뿐이다. 그래서 조회가 과거 산출물의 관계를 바꾸지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.artifact_trace import TraceRef, format_ref, parse_ref
from app.artifact_trace_projection import (
    project_artifact_trace,
    projection_state_from_testing_contracts,
    same_implementation_contracts,
)
from app.db.models import TYPE_SOURCE_CODE
from app.repositories import artifact_repository
from app.testing.schemas.testing_input import TestingInput
from app.workspace import repository as workspace_repository


class UnknownTraceRef(ValueError):
    """요청한 주소가 현재 고정 산출물 어디에도 없을 때 사용한다."""


def artifact_trace_response(app_id: str, ref_text: str | None = None) -> dict[str, Any]:
    """앱의 최신 읽기 전용 추적표와 선택 주소의 관계를 JSON으로 만든다."""

    testing_command = workspace_repository.latest_command(app_id, stage="testing")
    testing_input = _testing_input(testing_command)
    reported_result = _testing_result(testing_command)
    evidence_included: bool | None = None
    if testing_input is not None:
        source_version_id = testing_input.artifact_version_ids.get(TYPE_SOURCE_CODE)
        source_snapshot = artifact_repository.load_file_snapshot(
            app_id, TYPE_SOURCE_CODE, version_id=source_version_id
        )
        state = _projection_state(
            app_id,
            source_snapshot,
            testing_input.contract_artifacts.model_dump(mode="json", exclude_none=True),
        )
        evidence_included = _snapshot_matches_testing_input(
            source_snapshot, testing_input
        )
        testing_result = reported_result if evidence_included else {}
        trace_scope = "testing-input"
    else:
        source_snapshot = artifact_repository.load_file_snapshot(app_id, TYPE_SOURCE_CODE)
        source_contracts = _snapshot_contracts(source_snapshot)
        if source_contracts:
            # 이전 command에 입력이 없을 때도 source 자신이 저장한 계약은 사용할 수 있다.
            # 다만 어느 Testing 실행이 이 버전을 검사했는지는 증명할 수 없어 evidence는 뺀다.
            state = _projection_state(app_id, source_snapshot, source_contracts)
            testing_result = {}
            evidence_included = False
            trace_scope = "source-contracts"
        else:
            # 계약 metadata가 없는 snapshot은 현재 설계의 관계까지만 보여 준다.
            # 어느 source를 검사했는지 모르는 Testing 결과를 최신 설계와 섞지 않는다.
            state = artifact_repository.load_state(app_id)
            testing_result = {}
            evidence_included = False
            trace_scope = "latest-unverified"
    implementation_rtm = _implementation_rtm(source_snapshot)
    trace = project_artifact_trace(
        state,
        implementation_rtm,
        testing_result,
        implementation_verification=_implementation_verification(source_snapshot),
    )

    selected = parse_ref(ref_text) if ref_text else None
    if selected is not None and selected not in trace.refs:
        raise UnknownTraceRef(format_ref(selected))

    return {
        "app_id": app_id,
        "ref": format_ref(selected) if selected else None,
        "refs": _refs(trace.refs),
        "unknown_source_refs": _refs(trace.unknown_source_refs),
        "sources": _refs(trace.sources(selected)) if selected else [],
        "consumers": _refs(trace.consumers(selected)) if selected else [],
        "upstream": _refs(trace.upstream(selected)) if selected else [],
        "downstream": _refs(trace.downstream(selected)) if selected else [],
        "files": _refs(trace.files(selected)),
        "evidence": _refs(trace.evidence(selected)),
        "source_snapshot": _snapshot_summary(source_snapshot),
        "testing": _testing_summary(
            testing_command,
            reported_result,
            evidence_included=evidence_included,
        ),
        "trace_scope": trace_scope,
    }


def _implementation_rtm(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """SOURCE_CODE snapshot metadata에 보관한 구현 RTM만 꺼낸다."""

    metadata = snapshot.get("metadata") if isinstance(snapshot, Mapping) else None
    traceability = (
        metadata.get("implementation_traceability")
        if isinstance(metadata, Mapping)
        else None
    )
    return dict(traceability) if isinstance(traceability, Mapping) else {}


def _snapshot_contracts(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """SOURCE_CODE snapshot이 직접 보관한 고정 계약만 꺼낸다."""
    metadata = snapshot.get("metadata") if isinstance(snapshot, Mapping) else None
    contracts = metadata.get("testing_contracts") if isinstance(metadata, Mapping) else None
    return dict(contracts) if isinstance(contracts, Mapping) else {}


def _projection_state(
    app_id: str,
    snapshot: Mapping[str, Any] | None,
    contracts: Mapping[str, Any],
) -> dict[str, Any]:
    """고정 계약을 기본으로 쓰고, 같은 설계 버전일 때만 전체 설계를 보탠다."""
    frozen = projection_state_from_testing_contracts(contracts)
    metadata = snapshot.get("metadata") if isinstance(snapshot, Mapping) else None
    expected = (
        metadata.get("trace_artifact_versions") if isinstance(metadata, Mapping) else None
    )
    if not isinstance(expected, Mapping) or not expected:
        return frozen
    current = artifact_repository.load_state(app_id)
    current_versions = current.get("artifact_versions")
    if not isinstance(current_versions, Mapping) or any(
        not isinstance(current_versions.get(artifact_type), Mapping)
        or current_versions[artifact_type].get("version_id") != version_id
        for artifact_type, version_id in expected.items()
    ):
        return frozen
    state = dict(current)
    state.update(frozen)
    return state


def _implementation_verification(snapshot: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """SOURCE_CODE snapshot의 작은 구현 검증 근거만 trace에 전달한다."""
    metadata = snapshot.get("metadata") if isinstance(snapshot, Mapping) else None
    reports = (
        metadata.get("implementation_verification")
        if isinstance(metadata, Mapping)
        else None
    )
    return [item for item in reports if isinstance(item, dict)] if isinstance(reports, list) else []


def _testing_input(command: Mapping[str, Any] | None) -> TestingInput | None:
    """명시된 command checkpoint 또는 완료 job에서만 TestingInput을 읽는다."""
    if not isinstance(command, Mapping) or command.get("stage") != "testing":
        return None
    payload = command.get("payload")
    result = command.get("result")
    checkpoint = payload.get("testing_checkpoint") if isinstance(payload, Mapping) else None
    job = result.get("job") if isinstance(result, Mapping) else None
    candidates = (
        checkpoint.get("testing_input") if isinstance(checkpoint, Mapping) else None,
        job.get("testing_input") if isinstance(job, Mapping) else None,
        result.get("testing_input") if isinstance(result, Mapping) else None,
    )
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        try:
            return TestingInput.model_validate(candidate)
        except ValueError:
            continue
    return None


def _snapshot_matches_testing_input(
    snapshot: Mapping[str, Any] | None, testing_input: TestingInput
) -> bool:
    """Testing command가 고정한 source와 계약이 실제 snapshot과 같은지 확인한다."""
    if not isinstance(snapshot, Mapping):
        return False
    expected_version_id = testing_input.artifact_version_ids.get(TYPE_SOURCE_CODE)
    if snapshot.get("version_id") != expected_version_id:
        return False
    metadata = snapshot.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    implementation_job_id = metadata.get("implementation_job_id")
    if implementation_job_id and implementation_job_id != testing_input.implementation_job_id:
        return False
    source_contracts = _snapshot_contracts(snapshot)
    input_contracts = testing_input.contract_artifacts.model_dump(
        mode="json", exclude_none=True
    )
    return not source_contracts or same_implementation_contracts(
        source_contracts, input_contracts
    )


def _testing_result(command: Mapping[str, Any] | None) -> dict[str, Any]:
    """가장 최근 command가 Testing 결과일 때만 작은 projection 입력으로 사용한다."""

    if not isinstance(command, Mapping) or command.get("stage") != "testing":
        return {}
    result = command.get("result")
    if not isinstance(result, Mapping):
        return {}
    job = result.get("job")
    if isinstance(job, Mapping) and isinstance(job.get("result"), Mapping):
        return dict(job["result"])
    return dict(result)


def _snapshot_summary(snapshot: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """파일 내용은 보내지 않고 어떤 source snapshot을 읽었는지만 표시한다."""

    if not isinstance(snapshot, Mapping):
        return None
    return {
        key: snapshot.get(key)
        for key in ("version_id", "version_no", "snapshot_digest", "created_at")
    }


def _testing_summary(
    command: Mapping[str, Any] | None,
    result: Mapping[str, Any],
    *,
    evidence_included: bool | None = None,
) -> dict[str, Any] | None:
    """화면이 최신 Testing 결과인지 알 수 있게 최소 상태만 표시한다."""

    if not isinstance(command, Mapping) or command.get("stage") != "testing":
        return None
    summary = {
        "command_id": command.get("command_id"),
        "status": command.get("status"),
        "passed": result.get("passed"),
        "gate_status": result.get("gateStatus"),
    }
    if evidence_included is not None:
        summary["evidence_included"] = evidence_included
    return summary


def _refs(values: tuple[TraceRef, ...]) -> list[str]:
    """typed 주소를 API가 바로 돌려줄 안정 문자열 목록으로 바꾼다."""

    return [format_ref(value) for value in values]


__all__ = ["UnknownTraceRef", "artifact_trace_response"]
