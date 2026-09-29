"""앱 입력과 단계별 산출물을 MySQL에 저장하고 다시 읽는다.

요구사항부터 테스트까지 각 단계의 결과는 ``ArtifactVersion``의 불변 버전으로
보관한다. 설계 단계는 편집 가능한 JSON 모델을 저장하고, 조회할 때 그
모델로 PlantUML 또는 OpenAPI를 다시 만든다. 따라서 그림과 JSON을 따로 수정해서 서로
내용이 달라지는 일을 막을 수 있다.

이 모듈은 저장 형식과 SQLAlchemy transaction만 책임진다. HTTP 상태 코드를 결정하거나
LLM을 호출하는 일은 상위 계층에서 처리한다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.artifact_images import warm_artifact_images
from app.db.models import (
    FORMAT_JSON,
    FORMAT_PUML,
    ORIGIN_GENERATED,
    TYPE_API_SPEC,
    TYPE_CAPABILITY_CONTRACT,
    TYPE_CLASS,
    TYPE_DEPLOYMENT,
    TYPE_ERD,
    TYPE_REFINE_REQ,
    TYPE_RESOURCE_INTAKE,
    TYPE_RESOURCE_SPEC,
    TYPE_SEQUENCE,
    TYPE_USECASE_DIAGRAM,
    TYPE_USECASE_SPEC,
    App,
    ArtifactFile,
    ArtifactVersion,
)
from app.db.session import session_scope
from app.design.schemas.architecture_state import ArchitectureState
from app.design.services.api_spec.openapi import build_openapi_from_model
from app.design.services.class_diagram.plantuml import generate_plantuml_from_bce_json
from app.design.services.deployment_diagram.bundle import (
    hydrate_deployment_diagram_bundle,
)
from app.design.services.deployment_diagram.provider_plantuml import (
    deployment_bundle_provisioning_puml,
    deployment_bundle_runtime_puml,
)
from app.design.services.erd.plantuml import generate_erd_from_bce_json
from app.design.services.sequence_diagram.plantuml import generate_sequence_from_model
from app.design.validation import rehydrated_check_state

logger = logging.getLogger(__name__)


class AppNotFound(Exception):
    """요청한 ``app_id``가 ``apps`` 테이블에 없을 때 발생한다."""


# 파이프라인이 저장할 수 있는 stage와 ArchitectureState 필드의 대응표다.
#
# * state_key: 프론트엔드와 다음 stage가 읽는 결과 필드
# * source_key: 실제로 DB에 저장하는 편집 가능한 JSON 모델
# * derive/derive_state: source_key에서 화면용 문서를 다시 만드는 함수
#
# 설계 산출물은 LLM이 만든 구조화 모델만 저장한다. PlantUML과 OpenAPI는 조회할 때
# 같은 모델로 다시 만들기 때문에 사용자의 피드백도 그림 문자열이 아니라 모델에
# 적용된다. 요구사항 분석 산출물은 별도 변환 모델이 없으므로 받은 JSON을 그대로 저장한다.
STAGE_ARTIFACTS: dict[str, dict[str, Any]] = {
    "refined_requirements": {
        "artifact_type": TYPE_REFINE_REQ,
        "format": FORMAT_JSON,
        "state_key": "refined_requirements",
        "valid_key": None,
        "errors_key": None,
    },
    "capability_contract": {
        "artifact_type": TYPE_CAPABILITY_CONTRACT,
        "format": FORMAT_JSON,
        "state_key": "capability_contract",
        "valid_key": None,
        "errors_key": None,
    },
    "resource_intake": {
        "artifact_type": TYPE_RESOURCE_INTAKE,
        "format": FORMAT_JSON,
        "state_key": "resource_intake",
        "valid_key": None,
        "errors_key": None,
    },
    "usecase_spec": {
        "artifact_type": TYPE_USECASE_SPEC,
        "format": FORMAT_JSON,
        "state_key": "usecase_spec",
        "valid_key": None,
        "errors_key": None,
    },
    "usecase_diagram": {
        "artifact_type": TYPE_USECASE_DIAGRAM,
        "format": FORMAT_PUML,
        "state_key": "usecase_diagram_puml",
        "valid_key": "usecase_diagram_syntax_valid",
        "errors_key": "usecase_diagram_syntax_errors",
    },
    "resource_spec": {
        "artifact_type": TYPE_RESOURCE_SPEC,
        "format": FORMAT_JSON,
        "state_key": "resource_spec",
        "valid_key": None,
        "errors_key": None,
    },
    "class_diagram": {
        "artifact_type": TYPE_CLASS,
        "format": FORMAT_PUML,
        "state_key": "class_diagram_puml",
        "valid_key": "class_diagram_syntax_valid",
        "errors_key": "class_diagram_syntax_errors",
        # 설계 규칙 검사 결과(`app/design/knowledge/`)다. PlantUML 문법 검사와는 확인하는
        # 내용이 다르므로 별도 필드에 둔다. 이 키가 없는 stage는 규칙 검사를 하지 않는다.
        "check_key": "class_diagram_check",
        # BCE 모델을 저장하고 state_key의 PlantUML은 조회할 때 다시 만든다.
        "source_key": "extracted_bce_classes",
        "source_format": FORMAT_JSON,
        "derive": generate_plantuml_from_bce_json,
    },
    "sequence_diagram": {
        "artifact_type": TYPE_SEQUENCE,
        "format": FORMAT_PUML,
        "state_key": "sequence_diagram_puml",
        "valid_key": "sequence_diagram_syntax_valid",
        "errors_key": "sequence_diagram_syntax_errors",
        "check_key": "sequence_diagram_check",
        # 호출 순서를 담은 모델을 저장하고 PlantUML은 조회할 때 다시 만든다.
        "source_key": "sequence_diagram_model",
        "source_format": FORMAT_JSON,
        "derive": generate_sequence_from_model,
    },
    "api_spec": {
        "artifact_type": TYPE_API_SPEC,
        "format": FORMAT_JSON,
        "state_key": "api_spec",
        "valid_key": "api_spec_syntax_valid",
        "errors_key": "api_spec_syntax_errors",
        "check_key": "api_spec_check",
        # endpoint 모델을 저장하고 OpenAPI 문서는 조회할 때 다시 조립한다.
        "source_key": "api_spec_model",
        "source_format": FORMAT_JSON,
        "derive": build_openapi_from_model,
    },
    "erd": {
        "artifact_type": TYPE_ERD,
        "format": FORMAT_PUML,
        "state_key": "erd_puml",
        "valid_key": "erd_syntax_valid",
        "errors_key": "erd_syntax_errors",
        # BCE 모델과 여기서 변환한 데이터 모델의 대응 관계를 검사한 결과다.
        "check_key": "erd_check",
        # ERD 전용 BCE Entity 사본을 저장한다. 피드백은 PlantUML 문자열이 아니라 이
        # 모델을 수정하며, 수정된 모델에서 새 PlantUML을 만든다.
        "source_key": "erd_bce_classes",
        "source_format": FORMAT_JSON,
        "derive": generate_erd_from_bce_json,
    },
    "deployment_diagram": {
        "artifact_type": TYPE_DEPLOYMENT,
        "format": FORMAT_PUML,
        "state_key": "deployment_diagram_puml",
        "valid_key": "deployment_diagram_syntax_valid",
        "errors_key": "deployment_diagram_syntax_errors",
        "check_key": "deployment_diagram_check",
        # 편집 가능한 논리 모델과 CSP별 변환 결과를 한 bundle로 저장한다. 실행 구조도와
        # provisioning 구조도를 같은 bundle에서 만들므로 새로고침 후에도 두 그림이 같은
        # 배포 모델을 사용한다.
        "source_key": "deployment_diagram_bundle",
        "source_format": FORMAT_JSON,
        "derive_state": lambda bundle: {
            "deployment_diagram_puml": deployment_bundle_runtime_puml(bundle),
            "deployment_diagram_provisioning_puml": (
                deployment_bundle_provisioning_puml(bundle)
            ),
        },
        "hydrate": hydrate_deployment_diagram_bundle,
    },
}

STAGE_BY_ARTIFACT_TYPE = {
    config["artifact_type"]: stage for stage, config in STAGE_ARTIFACTS.items()
}


def create_app(requirements_text: str = "", resource_constraints_text: str = "") -> str:
    """새 앱 ID를 만들고 파이프라인이 시작할 입력을 저장한다."""
    app_id = str(uuid.uuid4())
    with session_scope() as session:
        session.add(
            App(
                app_id=app_id,
                requirements_text=requirements_text,
                resource_constraints_text=resource_constraints_text,
            )
        )
    return app_id


def list_apps(limit: int = 50) -> list[dict[str, Any]]:
    """최근 생성한 앱을 최신순으로 최대 ``limit``개 반환한다."""
    with session_scope() as session:
        rows = session.scalars(
            select(App).order_by(App.created_at.desc()).limit(limit)
        ).all()
        return [
            {
                "app_id": row.app_id,
                "current_stage": row.current_stage,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]


def update_inputs(
    app_id: str,
    requirements_text: str | None = None,
    resource_constraints_text: str | None = None,
) -> None:
    """값이 전달된 입력 필드만 수정한다.

    ``None``은 변경하지 않는다는 뜻이고 빈 문자열은 값을 비우라는 뜻이다. 두 경우를
    구분해야 배포 조건만 수정할 때 원래 요구사항이 사라지지 않는다.
    """
    with session_scope() as session:
        app = _require_app(session, app_id)
        if requirements_text is not None:
            app.requirements_text = requirements_text
        if resource_constraints_text is not None:
            app.resource_constraints_text = resource_constraints_text


def ensure_app_exists(app_id: str) -> None:
    """앱 행의 존재만 확인하고, 없으면 :class:`AppNotFound`를 발생시킨다.

    404 응답 여부만 판단할 호출자가 사용한다. ``load_state()``로도 확인할 수 있지만,
    그 함수는 모든 산출물을 읽고 그림까지 다시 만들므로 단순 존재 확인에는 비용이 크다.
    """
    with session_scope() as session:
        _require_app(session, app_id)


def load_state(app_id: str) -> ArchitectureState:
    """저장된 최신 산출물로 앱의 ``ArchitectureState``를 다시 구성한다."""
    with session_scope() as session:
        app = _require_app(session, app_id)

        state: ArchitectureState = {
            "app_id": app_id,
            "requirements_text": app.requirements_text or "",
            "resource_constraints_text": app.resource_constraints_text or "",
        }
        artifact_status: dict[str, str] = {}

        latest_by_type = (
            select(
                ArtifactVersion.artifact_type.label("artifact_type"),
                func.max(ArtifactVersion.version_no).label("version_no"),
            )
            .where(ArtifactVersion.app_id == app_id)
            .group_by(ArtifactVersion.artifact_type)
            .subquery()
        )
        artifact_rows = session.scalars(
            select(ArtifactVersion)
            .join(
                latest_by_type,
                (ArtifactVersion.artifact_type == latest_by_type.c.artifact_type)
                & (ArtifactVersion.version_no == latest_by_type.c.version_no),
            )
            .where(ArtifactVersion.app_id == app_id)
        ).all()

        # 구현과 Testing에는 실제로 읽은 산출물 버전을 함께 넘긴다. 구현이 시작된 뒤
        # 설계가 수정되더라도 Testing이 새 버전을 섞어 읽지 않게 하기 위한 정보다.
        state["artifact_versions"] = {
            version.artifact_type: {
                "version_id": version.id,
                "version_no": version.version_no,
                "stored_digest": hashlib.sha256(
                    version.content.encode("utf-8")
                ).hexdigest(),
            }
            for version in artifact_rows
        }

        for version in artifact_rows:
            stage = STAGE_BY_ARTIFACT_TYPE.get(version.artifact_type)
            if stage is None:
                continue

            config = STAGE_ARTIFACTS[stage]
            source_key = config.get("source_key")
            if source_key:
                # 설계 산출물은 JSON 모델을 먼저 복원한 다음 사람이 보는 문서를 만든다.
                # DB에 PlantUML/OpenAPI 사본을 따로 저장하지 않아 두 표현이 어긋나지 않는다.
                source_value = _decode_content(version.content, config["source_format"])
                if config.get("hydrate"):
                    state.update(config["hydrate"](source_value))
                else:
                    state[source_key] = source_value
                if config.get("derive_state"):
                    state.update(config["derive_state"](source_value))
                else:
                    # 같은 저장 모델에서 항상 같은 PlantUML/OpenAPI를 만든다.
                    state[config["state_key"]] = config["derive"](source_value)
            else:
                state[config["state_key"]] = _decode_content(
                    version.content,
                    config["format"],
                )
            if config["valid_key"]:
                state[config["valid_key"]] = version.syntax_valid
            if config["errors_key"]:
                state[config["errors_key"]] = version.syntax_errors or []

            artifact_status[stage] = "implemented"

        state["artifact_status"] = artifact_status
        # Stored structured models are authoritative. Loading must not reinterpret an
        # earlier sequence/API artifact against a newer class artifact. If the latest
        # persisted stage is still under design review, rebuild only that stage's
        # visible findings; all earlier stages keep the decision made at their gate.
        state.update(rehydrated_check_state(state, stages=(str(app.current_stage),)))
        return state


def save_stage(
    app_id: str,
    stage: str,
    state: ArchitectureState,
    origin: str = ORIGIN_GENERATED,
) -> int | None:
    """stage 결과를 새 산출물 버전으로 저장하고 표시용 이미지를 미리 만든다.

    저장할 내용이 있으면 새 version ID를 반환한다. stage가 비어 있으면 버전을 만들지
    않고 ``None``을 반환하지만, 앱의 현재 stage 표시는 요청받은 값으로 갱신한다. DB commit
    뒤에 PlantUML을 렌더링하므로 느린 이미지 생성 중 transaction을 붙잡지 않는다.
    """
    with session_scope() as session:
        app = _require_app(session, app_id)
        version_id = _write_version(session, app_id, stage, state, origin)

        app.current_stage = stage

    if version_id is not None:
        try:
            warm_artifact_images(app_id, stage, state)
        except Exception:
            # 그림 renderer가 잠시 실패해도 기준 데이터인 typed model 저장까지 취소하지 않는다.
            # 이미지 API의 cache-miss 경로가 같은 stage만 다시 준비할 수 있도록 원인을 남긴다.
            logger.exception(
                "PlantUML image warmup failed after saving app=%s stage=%s",
                app_id,
                stage,
            )
    return version_id


def save_stages(
    app_id: str,
    stages: list[str] | tuple[str, ...],
    state: ArchitectureState,
    origin: str = ORIGIN_GENERATED,
) -> dict[str, int]:
    """Persist a validated multi-stage revision in one database transaction.

    Targeted cascade computes and validates every patch before calling this function.
    Keeping all version inserts under one app-row lock prevents a later-stage write
    failure from leaving an earlier artifact committed on its own. Image warming stays
    post-commit because images are derived caches rather than source artifacts.
    """

    ordered = list(dict.fromkeys(str(stage).strip() for stage in stages if str(stage).strip()))
    unknown = [stage for stage in ordered if stage not in STAGE_ARTIFACTS]
    if unknown:
        raise ValueError(f"Unknown artifact stages: {', '.join(unknown)}")
    if not ordered:
        return {}

    version_ids: dict[str, int] = {}
    with session_scope() as session:
        app = _lock_app(session, app_id)
        for stage in ordered:
            version_id = _write_version(session, app_id, stage, state, origin)
            if version_id is not None:
                version_ids[stage] = version_id
        app.current_stage = ordered[-1]

    for stage in version_ids:
        try:
            warm_artifact_images(app_id, stage, state)
        except Exception:
            logger.exception(
                "PlantUML image warmup failed after saving app=%s stage=%s",
                app_id,
                stage,
            )
    return version_ids


def list_versions(app_id: str, stage: str) -> list[dict[str, Any]]:
    """한 산출물의 변경 이력을 오래된 버전부터 반환한다."""
    config = STAGE_ARTIFACTS[stage]
    with session_scope() as session:
        _require_app(session, app_id)
        versions = session.scalars(
            select(ArtifactVersion)
            .where(
                ArtifactVersion.app_id == app_id,
                ArtifactVersion.artifact_type == config["artifact_type"],
            )
            .order_by(ArtifactVersion.version_no)
        ).all()
        if not versions:
            return []
        current_version_no = versions[-1].version_no
        return [
            {
                "version_no": version.version_no,
                "origin": version.origin,
                "syntax_valid": version.syntax_valid,
                "syntax_errors": version.syntax_errors or [],
                "is_current": version.version_no == current_version_no,
                "created_at": version.created_at.isoformat(),
            }
            for version in versions
        ]


def get_version_content(app_id: str, stage: str, version_no: int) -> Any:
    """지정한 버전의 저장 내용을 반환하며, 없으면 ``None``을 반환한다."""
    config = STAGE_ARTIFACTS[stage]
    with session_scope() as session:
        _require_app(session, app_id)
        version = session.scalars(
            select(ArtifactVersion).where(
                ArtifactVersion.app_id == app_id,
                ArtifactVersion.artifact_type == config["artifact_type"],
                ArtifactVersion.version_no == version_no,
            )
        ).first()
        if version is None:
            return None
        return _decode_content(
            version.content,
            config.get("source_format") or config["format"],
        )


def save_file_snapshot(
    app_id: str,
    artifact_type: str,
    files: dict[str, str],
    *,
    origin: str = ORIGIN_GENERATED,
    metadata: dict[str, Any] | None = None,
) -> int:
    """여러 파일을 한 묶음으로 저장하고 수정하지 않는 버전을 만든다.

    구현 코드처럼 파일이 여러 개인 산출물은 일부만 최신 버전으로 섞이면 실행할 수 없다.
    따라서 전체 파일 tree를 한 ``ArtifactVersion`` 아래에 저장하고 한 번에 교체한다.

    단, 경로와 내용이 최신 snapshot과 모두 같으면 기존 ID를 반환한다.
    수리 Job ID와 같은 metadata는 파일 결과의 내용이 아니므로 비교하지 않는다.
    이렇게 해야 내용을 바꾸지 못한 수리가 DB 버전만 늘리지 않는다.
    """
    if not files:
        raise ValueError("A file artifact snapshot cannot be empty")
    normalized = {_normalize_file_path(path): content for path, content in files.items()}
    with session_scope() as session:
        _lock_app(session, app_id)
        return _write_file_snapshot(
            session,
            app_id,
            artifact_type,
            normalized,
            origin=origin,
            metadata=metadata,
        )


def save_file_snapshots(
    app_id: str,
    snapshots: Mapping[
        str,
        tuple[Mapping[str, str], Mapping[str, Any] | None],
    ],
    *,
    origin: str = ORIGIN_GENERATED,
) -> dict[str, int]:
    """Persist multiple file-artifact snapshots in one database transaction."""

    normalized: dict[str, tuple[dict[str, str], Mapping[str, Any] | None]] = {}
    for artifact_type, (files, metadata) in snapshots.items():
        if not files:
            raise ValueError(f"A file artifact snapshot cannot be empty: {artifact_type}")
        normalized[artifact_type] = (
            {_normalize_file_path(path): content for path, content in files.items()},
            metadata,
        )
    if not normalized:
        return {}

    version_ids: dict[str, int] = {}
    with session_scope() as session:
        _lock_app(session, app_id)
        for artifact_type in sorted(normalized):
            files, metadata = normalized[artifact_type]
            version_ids[artifact_type] = _write_file_snapshot(
                session,
                app_id,
                artifact_type,
                files,
                origin=origin,
                metadata=metadata,
            )
    return version_ids


def _write_file_snapshot(
    session: Session,
    app_id: str,
    artifact_type: str,
    normalized: Mapping[str, str],
    *,
    origin: str,
    metadata: Mapping[str, Any] | None,
) -> int:
    """Write one normalized file tree inside the caller-owned transaction."""

    latest = _latest_version(session, app_id, artifact_type)
    if latest is not None and _same_file_snapshot(latest, normalized):
        return latest.id
    latest_version_no = latest.version_no if latest is not None else 0

    version = ArtifactVersion(
        app_id=app_id,
        artifact_type=artifact_type,
        version_no=latest_version_no + 1,
        content=json.dumps(dict(metadata or {}), ensure_ascii=False, sort_keys=True),
        syntax_valid=True,
        origin=origin,
    )
    session.add(version)
    session.flush()
    for path, content in sorted(normalized.items()):
        session.add(
            ArtifactFile(
                artifact_version_id=version.id,
                file_path=path,
                content=content,
                sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
        )
    return version.id


def delete_file_snapshots_owned_by_job(
    app_id: str,
    artifact_version_ids: Mapping[str, int],
    *,
    implementation_job_id: str,
) -> dict[str, int]:
    """지정한 구현 Job이 새로 만든 파일 snapshot만 삭제한다.

    수리 Job이 기존 snapshot과 같은 내용을 내면 기존 ID를 공유한다.
    그 ID를 삭제하면 이전에 완료된 구현까지 손상된다. 따라서 Job 기록에
    있는 ID라도 실제 버전 metadata의 ``implementation_job_id``가 지정한
    Job과 같은 경우에만 삭제한다. 버전 ID와 산출물 종류, 앱 ID도 함께
    확인하므로 잘못된 작업 기록이 다른 산출물을 지울 수 없다.
    """

    deleted: dict[str, int] = {}
    with session_scope() as session:
        _lock_app(session, app_id)
        for artifact_type, raw_version_id in sorted(artifact_version_ids.items()):
            if (
                not isinstance(raw_version_id, int)
                or isinstance(raw_version_id, bool)
                or raw_version_id < 1
            ):
                continue
            version = session.get(ArtifactVersion, raw_version_id)
            if version is None or (
                version.app_id != app_id
                or version.artifact_type != artifact_type
                or not version.files
                or _safe_json_object(version.content).get("implementation_job_id")
                != implementation_job_id
            ):
                continue
            session.delete(version)
            deleted[artifact_type] = raw_version_id
    return deleted


def load_file_snapshot(
    app_id: str,
    artifact_type: str,
    version_no: int | None = None,
    *,
    version_id: int | None = None,
) -> dict[str, Any] | None:
    """파일 snapshot 한 버전을 설계 상태와 섞지 않고 반환한다.

    selector를 생략하면 기존과 같이 현재 버전을 읽는다. ``version_no`` 또는 내부 DB
    식별자인 ``version_id``를 전달하면 그 버전만 조회한다. Testing job은 시작할 때
    확인한 번호를 계속 전달하므로, 같은 앱의 새 구현이 나중에 저장되더라도 실행 중인
    검사가 최신 파일로 바뀌지 않는다.
    """
    if version_no is not None and version_id is not None:
        raise ValueError("Choose either version_no or version_id, not both")
    with session_scope() as session:
        _require_app(session, app_id)
        if version_id is not None:
            version = session.get(ArtifactVersion, version_id)
            if version is not None and (
                version.app_id != app_id or version.artifact_type != artifact_type
            ):
                version = None
        elif version_no is None:
            version = _latest_version(session, app_id, artifact_type)
        else:
            version = session.scalars(
                select(ArtifactVersion).where(
                    ArtifactVersion.app_id == app_id,
                    ArtifactVersion.artifact_type == artifact_type,
                    ArtifactVersion.version_no == version_no,
                )
            ).first()
        if version is None:
            return None
        return _file_snapshot_payload(version)


def load_file_snapshots(
    app_id: str, artifact_types: list[str] | tuple[str, ...] | set[str]
) -> dict[str, dict[str, Any]]:
    """Load the latest requested file snapshots from one database statement.

    Delivery refresh writes deployment and IaC snapshots in one transaction.
    Selecting all latest versions in one statement prevents a concurrent refresh
    from mixing an old deployment script with new HCL, or the reverse.
    """

    requested = sorted({str(item) for item in artifact_types if str(item)})
    if not requested:
        return {}
    with session_scope() as session:
        _require_app(session, app_id)
        latest = (
            select(
                ArtifactVersion.artifact_type.label("artifact_type"),
                func.max(ArtifactVersion.version_no).label("version_no"),
            )
            .where(
                ArtifactVersion.app_id == app_id,
                ArtifactVersion.artifact_type.in_(requested),
            )
            .group_by(ArtifactVersion.artifact_type)
            .subquery()
        )
        versions = session.scalars(
            select(ArtifactVersion).join(
                latest,
                (ArtifactVersion.artifact_type == latest.c.artifact_type)
                & (ArtifactVersion.version_no == latest.c.version_no),
            )
            .where(ArtifactVersion.app_id == app_id)
            .order_by(ArtifactVersion.artifact_type)
        ).all()
        return {
            version.artifact_type: _file_snapshot_payload(version)
            for version in versions
        }


def list_file_artifact_versions(app_id: str, artifact_type: str) -> list[dict[str, Any]]:
    """파일 산출물의 버전, 파일 수와 metadata를 오래된 순서로 반환한다."""
    with session_scope() as session:
        _require_app(session, app_id)
        file_count = (
            select(func.count(ArtifactFile.file_path))
            .where(ArtifactFile.artifact_version_id == ArtifactVersion.id)
            .correlate(ArtifactVersion)
            .scalar_subquery()
        )
        versions = session.execute(
            select(ArtifactVersion, file_count)
            .where(
                ArtifactVersion.app_id == app_id,
                ArtifactVersion.artifact_type == artifact_type,
            )
            .order_by(ArtifactVersion.version_no)
        ).all()
        if not versions:
            return []
        current_version_no = versions[-1][0].version_no
        return [
            {
                "version_no": version.version_no,
                "file_count": count,
                "is_current": version.version_no == current_version_no,
                "metadata": _safe_json_object(version.content),
                "created_at": version.created_at.isoformat(),
            }
            for version, count in versions
        ]


def _write_version(
    session: Session,
    app_id: str,
    stage: str,
    state: ArchitectureState,
    origin: str,
) -> int | None:
    config = STAGE_ARTIFACTS[stage]
    source_key = config.get("source_key")
    if source_key:
        # 구조화 모델을 저장하고 state_key의 표시용 문서는 조회할 때 다시 만든다.
        content = _encode_content(state.get(source_key), config["source_format"])
    else:
        content = _encode_content(state.get(config["state_key"]), config["format"])
    if not content.strip():
        return None

    # 기존 artifact가 아직 없는 첫 저장도 포함해 앱 단위로 직렬화한다. artifact 행만
    # 잠그면 "행이 없음"은 잠글 수 없어 두 writer가 같은 version_no=1을 만들 수 있다.
    _lock_app(session, app_id)
    latest_version_no = _latest_version_no(session, app_id, config["artifact_type"])

    version = ArtifactVersion(
        app_id=app_id,
        artifact_type=config["artifact_type"],
        version_no=latest_version_no + 1,
        content=content,
        syntax_valid=(
            state.get(config["valid_key"]) if config["valid_key"] else None
        ),
        syntax_errors=(
            list(state.get(config["errors_key"], []) or [])
            if config["errors_key"]
            else None
        ),
        origin=origin,
    )
    session.add(version)
    session.flush()

    return version.id


def _latest_version(
    session: Session,
    app_id: str,
    artifact_type: str,
) -> ArtifactVersion | None:
    return session.scalar(
        select(ArtifactVersion)
        .where(
            ArtifactVersion.app_id == app_id,
            ArtifactVersion.artifact_type == artifact_type,
        )
        .order_by(ArtifactVersion.version_no.desc())
        .limit(1)
    )


def _latest_version_no(session: Session, app_id: str, artifact_type: str) -> int:
    latest = session.scalar(
        select(func.max(ArtifactVersion.version_no)).where(
            ArtifactVersion.app_id == app_id,
            ArtifactVersion.artifact_type == artifact_type,
        )
    )
    return int(latest or 0)


def _require_app(session: Session, app_id: str) -> App:
    app = session.get(App, app_id)
    if app is None:
        raise AppNotFound(app_id)
    return app


def _lock_app(session: Session, app_id: str) -> App:
    """Return an app while holding its row lock until the current transaction ends."""
    app = session.scalar(select(App).where(App.app_id == app_id).with_for_update())
    if app is None:
        raise AppNotFound(app_id)
    return app


def _encode_content(value: Any, content_format: str) -> str:
    if value is None:
        return ""
    if content_format == FORMAT_JSON:
        if not value:
            return ""
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def _decode_content(content: str, content_format: str) -> Any:
    if content_format != FORMAT_JSON:
        return content
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {}


def _file_snapshot_payload(version: ArtifactVersion) -> dict[str, Any]:
    files = {
        item.file_path: {"content": item.content, "sha256": item.sha256}
        for item in version.files
    }
    # 파일 경로와 각 파일의 SHA-256을 정렬된 순서로 합쳐 snapshot 전체 digest를
    # 만든다. DB row ID나 저장 시각은 넣지 않으므로 같은 파일 tree는 같은 digest다.
    digest_source = "".join(
        f"{path}\0{item['sha256']}\n" for path, item in sorted(files.items())
    )
    return {
        "artifact_type": version.artifact_type,
        "version_id": version.id,
        "version_no": version.version_no,
        "snapshot_digest": hashlib.sha256(digest_source.encode("utf-8")).hexdigest(),
        "metadata": _safe_json_object(version.content),
        "files": files,
        "created_at": version.created_at.isoformat(),
    }


def _normalize_file_path(value: str) -> str:
    path = value.replace("\\", "/").strip("/")
    if not path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError(f"Invalid artifact file path: {value}")
    return path


def _same_file_snapshot(
    version: ArtifactVersion,
    expected: Mapping[str, str],
) -> bool:
    """경로와 UTF-8 text 내용이 모두 같은지 비교한다.

    hash만 비교하지 않고 내용도 직접 비교한다. 파일 수가 같고 모든 경로가
    일치해야 하므로 파일이 삭제되거나 추가된 변경도 놓치지 않는다.
    """

    if len(version.files) != len(expected):
        return False
    return all(
        item.file_path in expected and expected[item.file_path] == item.content
        for item in version.files
    )


def _safe_json_object(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    except json.JSONDecodeError:
        return {}
