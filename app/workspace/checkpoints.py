"""완료된 개발 단계를 새 앱으로 복사하는 분기 저장소다.

LangGraph의 내부 실행 위치와 과거 명령은 복사하지 않는다. 선택한 단계까지의 최신 산출물만
새 앱에 넣고, 다음 단계는 그 공개 산출물에서 새로 시작한다. 원본 앱은 항상 그대로 남는다.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.db.models import (
    TYPE_API_SPEC,
    TYPE_CAPABILITY_CONTRACT,
    TYPE_CLASS,
    TYPE_DEPLOYMENT,
    TYPE_DEPLOYMENT_FILE,
    TYPE_ERD,
    TYPE_FRONTEND_SOURCE_CODE,
    TYPE_IAC_CODE,
    TYPE_REFINE_REQ,
    TYPE_RESOURCE_INTAKE,
    TYPE_RESOURCE_SPEC,
    TYPE_SEQUENCE,
    TYPE_SOURCE_CODE,
    TYPE_TEST_CODE,
    TYPE_USECASE_DIAGRAM,
    TYPE_USECASE_SPEC,
    App,
    ArtifactFile,
    ArtifactVersion,
    WorkspaceCommand,
)
from app.db.session import session_scope
from app.design.services.persistence_scope import erd_disposition
from app.repositories import artifact_repository

from .contracts import CheckpointStage, RestartStage

_REQUIREMENTS = (
    TYPE_REFINE_REQ,
    TYPE_CAPABILITY_CONTRACT,
    TYPE_RESOURCE_INTAKE,
    TYPE_USECASE_SPEC,
    TYPE_USECASE_DIAGRAM,
    TYPE_RESOURCE_SPEC,
)
_DESIGN = (*_REQUIREMENTS, TYPE_CLASS, TYPE_SEQUENCE, TYPE_API_SPEC, TYPE_ERD, TYPE_DEPLOYMENT)
_IMPLEMENTATION_FILES = (
    TYPE_SOURCE_CODE,
    TYPE_FRONTEND_SOURCE_CODE,
    TYPE_TEST_CODE,
    TYPE_DEPLOYMENT_FILE,
    TYPE_IAC_CODE,
)
_COPIED_TYPES = {
    CheckpointStage.REQUIREMENTS: _REQUIREMENTS,
    CheckpointStage.DESIGN: _DESIGN,
    CheckpointStage.IMPLEMENTATION: (*_DESIGN, *_IMPLEMENTATION_FILES),
}
_REQUIRED_TYPES = {
    **_COPIED_TYPES,
    # 프론트엔드·테스트·IaC는 앱에 따라 없을 수 있다. Testing의 최소 파일 입력만
    # 구현 완료 시점의 필수 항목으로 검사한다.
    CheckpointStage.IMPLEMENTATION: (*_DESIGN, TYPE_SOURCE_CODE, TYPE_DEPLOYMENT_FILE),
}
_CURRENT_STAGE = {
    CheckpointStage.REQUIREMENTS: "resource_spec",
    CheckpointStage.DESIGN: "deployment_diagram",
    CheckpointStage.IMPLEMENTATION: "implementation",
}
_PREVIOUS_STAGE = {
    RestartStage.REQUIREMENTS: None,
    RestartStage.DESIGN: CheckpointStage.REQUIREMENTS,
    RestartStage.IMPLEMENTATION: CheckpointStage.DESIGN,
    RestartStage.TESTING: CheckpointStage.IMPLEMENTATION,
}


def checkpoint_options(app_id: str) -> dict[str, list[dict[str, Any]]]:
    """현재 산출물로 가능한 분기와 재실행 단계를 표시한다."""

    with session_scope() as session:
        if session.get(App, app_id) is None:
            raise artifact_repository.AppNotFound(app_id)
        available = set(_latest_versions(session, app_id))
    state = artifact_repository.load_state(app_id)

    branch = [
        _option(
            stage.value,
            set(_required_types_for_checkpoint(stage, state)).issubset(available),
        )
        for stage, required in _REQUIRED_TYPES.items()
    ]
    rerun = [
        _option(
            stage.value,
            previous is None
            or set(_required_types_for_checkpoint(previous, state)).issubset(available),
        )
        for stage, previous in _PREVIOUS_STAGE.items()
    ]
    return {"branch": branch, "rerun": rerun}


def create_checkpoint_branch(
    source_app_id: str,
    stage: CheckpointStage | str,
) -> dict[str, Any]:
    """요구사항·설계·구현 중 선택한 완료 시점까지 새 앱으로 복사한다."""

    return _clone(source_app_id, CheckpointStage(stage))


def create_restart_branch(
    source_app_id: str,
    stage: RestartStage | str,
) -> dict[str, Any]:
    """선택한 단계를 다시 실행할 새 앱을 그 직전 시점에서 만든다."""

    return _clone(
        source_app_id,
        _PREVIOUS_STAGE[RestartStage(stage)],
        preserve_initial_cloud_inputs=RestartStage(stage) == RestartStage.REQUIREMENTS,
    )


def _clone(
    source_app_id: str,
    completed_stage: CheckpointStage | None,
    *,
    preserve_initial_cloud_inputs: bool = False,
) -> dict[str, Any]:
    """원본을 잠근 동안 앱 입력과 최신 산출물을 한 시점 기준으로 복사한다."""

    target_app_id = str(uuid.uuid4())
    copied_types = _COPIED_TYPES.get(completed_stage, ())
    implementation_job_id = (
        uuid.uuid4().hex if completed_stage == CheckpointStage.IMPLEMENTATION else None
    )
    state = artifact_repository.load_state(source_app_id)
    required_types = _required_types_for_checkpoint(completed_stage, state)
    version_ids: dict[str, int] = {}

    with session_scope() as session:
        # 산출물 저장도 app 행을 잠그므로, 복사 도중 새 버전이 끼어들지 않는다.
        source = session.scalar(
            select(App).where(App.app_id == source_app_id).with_for_update()
        )
        if source is None:
            raise artifact_repository.AppNotFound(source_app_id)
        latest = _latest_versions(session, source_app_id)
        missing = [item for item in required_types if item not in latest]
        if missing:
            raise ValueError("The selected checkpoint is incomplete: " + ", ".join(missing))

        initial_cloud_inputs: dict[str, Any] | None = None
        if preserve_initial_cloud_inputs:
            initial_command = session.scalar(
                select(WorkspaceCommand)
                .where(
                    WorkspaceCommand.app_id == source_app_id,
                    WorkspaceCommand.stage == "requirements",
                    WorkspaceCommand.action == "message",
                )
                .order_by(WorkspaceCommand.created_at.asc(), WorkspaceCommand.command_id.asc())
                .limit(1)
            )
            initial_payload = {}
            if initial_command and isinstance(initial_command.payload, dict):
                initial_payload = initial_command.payload
            if initial_payload.get("provider") and initial_payload.get("region"):
                initial_cloud_inputs = {
                    "provider": str(initial_payload["provider"]),
                    "region": str(initial_payload["region"]),
                    "monthly_budget_amount": initial_payload.get("monthly_budget_amount"),
                    "monthly_budget_currency": str(
                        initial_payload.get("monthly_budget_currency") or "USD"
                    ),
                    "resource_constraints_text": source.resource_constraints_text or "",
                }

        session.add(
            App(
                app_id=target_app_id,
                requirements_text=source.requirements_text,
                resource_constraints_text=source.resource_constraints_text,
                current_stage=_CURRENT_STAGE.get(completed_stage),
                deployment_preferences=copy.deepcopy(source.deployment_preferences),
                requirements_gated=source.requirements_gated,
            )
        )
        session.flush()

        for artifact_type in copied_types:
            original = latest.get(artifact_type)
            if original is None:
                continue
            cloned = ArtifactVersion(
                app_id=target_app_id,
                artifact_type=artifact_type,
                version_no=1,
                content=_copy_content(original, implementation_job_id),
                syntax_valid=original.syntax_valid,
                syntax_errors=copy.deepcopy(original.syntax_errors),
                origin=original.origin,
            )
            session.add(cloned)
            session.flush()
            version_ids[artifact_type] = cloned.id
            session.add_all(
                ArtifactFile(
                    artifact_version_id=cloned.id,
                    file_path=item.file_path,
                    content=item.content,
                    sha256=item.sha256,
                )
                for item in original.files
            )

        entry_command_id = _add_entry_command(
            session,
            target_app_id,
            source_app_id,
            completed_stage,
            implementation_job_id,
        )

    if implementation_job_id:
        # 구현 분기에서만 무거운 OpenHands 작업 관리자를 불러온다. 요구사항·설계 분기는
        # 파일 작업 기록이 필요 없고, 명령줄 복사 도구가 기존 구현 Job을 깨우면 안 된다.
        from app.implementation.application.jobs import build_testing_contracts, worker

        worker.register_snapshot(
            target_app_id,
            {
                artifact_type: version_id
                for artifact_type, version_id in version_ids.items()
                if artifact_type in _IMPLEMENTATION_FILES
            },
            build_testing_contracts(dict(artifact_repository.load_state(target_app_id))),
            job_id=implementation_job_id,
        )

    result = {
        "source_app_id": source_app_id,
        "target_app_id": target_app_id,
        "checkpoint_stage": completed_stage.value if completed_stage else None,
        "entry_command_id": entry_command_id,
        "implementation_job_id": implementation_job_id,
    }
    if initial_cloud_inputs is not None:
        result["initial_cloud_inputs"] = initial_cloud_inputs
    return result


def _add_entry_command(
    session: Any,
    target_app_id: str,
    source_app_id: str,
    stage: CheckpointStage | None,
    implementation_job_id: str | None,
) -> str | None:
    """복사한 완료 시점을 Workspace가 정상 완료 명령처럼 표시하게 한다."""

    if stage is None:
        return None
    command_id = str(uuid.uuid4())
    result: dict[str, Any] = {
        "message": f"Checkpoint branch created after {stage.value}.",
        "source_app_id": source_app_id,
        "checkpoint_stage": stage.value,
    }
    if implementation_job_id:
        result.update(
            {
                "job_id": implementation_job_id,
                "job": {
                    "job_id": implementation_job_id,
                    "app_id": target_app_id,
                    "status": "COMPLETED",
                },
            }
        )
    timestamp = datetime.now(UTC).replace(tzinfo=None)
    session.add(
        WorkspaceCommand(
            command_id=command_id,
            app_id=target_app_id,
            action="branch_checkpoint",
            stage=stage.value,
            status="COMPLETED",
            payload={"source_app_id": source_app_id, "checkpoint_stage": stage.value},
            result=result,
            started_at=timestamp,
            completed_at=timestamp,
        )
    )
    return command_id


def _latest_versions(session: Any, app_id: str) -> dict[str, ArtifactVersion]:
    rows = session.scalars(
        select(ArtifactVersion)
        .where(ArtifactVersion.app_id == app_id)
        .order_by(ArtifactVersion.artifact_type, ArtifactVersion.version_no)
    ).all()
    return {row.artifact_type: row for row in rows}


def _required_types_for_checkpoint(
    stage: CheckpointStage | str | None,
    state: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return required artifacts, omitting ERD only for non-persistent designs."""

    required = tuple(_REQUIRED_TYPES.get(stage, ()))
    if stage in {CheckpointStage.DESIGN, CheckpointStage.IMPLEMENTATION}:
        class_model = state.get("extracted_bce_classes")
        if erd_disposition(
            class_model if isinstance(class_model, Mapping) else {}, state
        ) == "not_applicable":
            return tuple(item for item in required if item != TYPE_ERD)
    return required


def _copy_content(source: ArtifactVersion, job_id: str | None) -> str:
    """구현 파일 metadata가 원본 Job을 가리키지 않게 식별자만 교체한다."""

    if job_id is None or not source.files:
        return source.content
    try:
        metadata = json.loads(source.content or "{}")
    except json.JSONDecodeError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    metadata.update({"implementation_job_id": job_id, "run_id": "checkpoint-branch"})
    return json.dumps(metadata, ensure_ascii=False, sort_keys=True)


def _option(stage: str, available: bool) -> dict[str, Any]:
    return {"stage": stage, "label": stage.title(), "available": available}


__all__ = ["checkpoint_options", "create_checkpoint_branch", "create_restart_branch"]
