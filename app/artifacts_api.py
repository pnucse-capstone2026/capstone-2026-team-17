"""앱 컨테이너와 산출물 저장소의 서빙 레이어 — **어느 에이전트의 것도 아니다.**

여기 있는 것은 세 에이전트가 함께 쓰는 것들이다:

  GET  /api/apps/{app_id}                     저장된 산출물 전부
  GET  /api/apps/{app_id}/trace?ref=kind:id   산출물 직접·상하류 관계
  GET  /api/apps/{app_id}/stages/{s}/versions 버전 이력
  GET  /api/apps/{app_id}/stages/{s}/versions/{no}
  GET  /api/apps/{app_id}/stages/{s}/image.{ext}   PlantUML → 이미지

**왜 따로 뺐나.** 원래 이것들은 설계 API에 있었다. 그런데 다루는 대상은
설계 산출물 5개가 아니라 **9개 전부**다 — 요구사항 분석이 만든
refined_requirements·usecase_spec·usecase_diagram·resource_spec 도 이 주소로 저장되고
조회되고 그려진다. `app_id` 발급도 마찬가지로 요구사항 화면이 부르는 것이었다.

즉 설계 폴더에 있었지만 설계 것이 아니었다. 그 상태에서는 설계 코드를 고치다가 다른
에이전트의 화면이 조용히 깨질 수 있다(실제로 한 번 그랬다). 소유를 이름으로 드러낸다.

산출물 생성과 수정은 Workspace가 각 단계의 application service를 호출해 처리한다.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from app.artifact_images import (
    MAIN_VIEW,
    PROVISIONING_VIEW,
    RUNTIME_VIEW,
    artifact_image_cache,
    deployment_target_view,
    sequence_diagrams_from_state,
    sequence_view,
    warm_artifact_images,
)
from app.artifact_trace_service import UnknownTraceRef, artifact_trace_response
from app.db.models import FORMAT_JSON
from app.design.schemas.architecture_state import ArchitectureState
from app.repositories import artifact_repository
from app.repositories.artifact_repository import STAGE_ARTIFACTS, AppNotFound

# URL에 version이 없으므로 브라우저가 그림을 영구 보관하면 피드백 전 이미지가 남을 수 있다.
# process 안에서는 산출물 저장 직후 같은 route의 cache를 교체하므로 HTTP 응답만 no-store로 둔다.
_NO_STORE_IMAGE_HEADERS = {"Cache-Control": "no-store, max-age=0"}

router = APIRouter(tags=["apps"])

#: 저장 가능한 모든 산출물 — 요구사항 4개 + 설계 5개.
STAGES = list(STAGE_ARTIFACTS)

#: 이미지로 렌더할 수 있는 산출물과, 그 PlantUML 이 상태의 어느 칸에 있는지.
#: usecase_diagram(요구사항 것)이 여기 들어 있는 것이 이 모듈이 공용인 이유를 보여준다.
PUML_FIELDS = {
    "usecase_diagram": {
        "code": "usecase_diagram_puml",
        "valid": "usecase_diagram_syntax_valid",
        "errors": "usecase_diagram_syntax_errors",
        "label": "use case diagram",
    },
    "class_diagram": {
        "code": "class_diagram_puml",
        "valid": "class_diagram_syntax_valid",
        "errors": "class_diagram_syntax_errors",
        "label": "class diagram",
    },
    "sequence_diagram": {
        "code": "sequence_diagram_puml",
        "valid": "sequence_diagram_syntax_valid",
        "errors": "sequence_diagram_syntax_errors",
        "label": "sequence diagram",
    },
    "erd": {
        "code": "erd_puml",
        "valid": "erd_syntax_valid",
        "errors": "erd_syntax_errors",
        "label": "ERD",
    },
    "deployment_diagram": {
        "code": "deployment_diagram_puml",
        "valid": "deployment_diagram_syntax_valid",
        "errors": "deployment_diagram_syntax_errors",
        "label": "deployment diagram",
    },
}


# ---------------------------------------------------------------------------
# 공용 헬퍼 — 에이전트 라우터들도 이걸 쓴다.
# ---------------------------------------------------------------------------
def validate_app_id(app_id: str) -> None:
    try:
        uuid.UUID(app_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid app id.") from error


def require_app(app_id: str) -> ArchitectureState:
    """앱의 상태 전체를 복원하거나 404. 존재 확인만이면 require_app_exists 를 쓴다."""
    try:
        return artifact_repository.load_state(app_id)
    except AppNotFound as error:
        raise HTTPException(status_code=404, detail="Unknown app id.") from error


def require_app_exists(app_id: str) -> None:
    """앱이 없으면 404. 상태는 만들지 않는다.

    존재 확인만 하는 곳은 이걸 써야 한다 — load_state 는 모든 산출물을 읽고 다이어그램을
    전부 다시 렌더하므로, 결과를 버릴 거면 순전한 낭비다.
    """
    try:
        artifact_repository.ensure_app_exists(app_id)
    except AppNotFound as error:
        raise HTTPException(status_code=404, detail="Unknown app id.") from error


def validate_stage_name(stage: str) -> None:
    if stage not in STAGES:
        raise HTTPException(status_code=404, detail=f"Unknown stage: {stage}")


def to_web_response(result: Mapping[str, Any]) -> dict[str, Any]:
    """상태 → 화면이 읽는 형태 {artifacts, validation, artifact_status}.

    `validation`은 **두 가지 다른 질문**을 함께 싣는다. `valid`/`errors`는 렌더된 산출물이
    문법에 맞는지를 묻고, 이건 렌더러가 보장하므로 거의 언제나 참이다. `findings`는
    **LLM이 낸 모델이 규칙을 지켰는지**를 묻고, 그건 보장된 것이 없다. 화면이 후자를
    전자와 같은 자리에서 읽어야 "문법은 통과했지만 내용이 틀렸다"를 보여줄 수 있다.

    `check_key`가 없는 스테이지는 `findings`가 빈 목록이고 `check_status`가 None이다.
    **그건 "깨끗하다"가 아니라 "검사하지 않았다"이다** — 화면은 둘을 구별해야 한다.
    """
    artifacts: dict[str, Any] = {}
    validation: dict[str, Any] = {}
    artifact_status = dict(result.get("artifact_status", {}))

    for stage, config in STAGE_ARTIFACTS.items():
        empty: Any = {} if config["format"] == FORMAT_JSON else ""
        check = result.get(config.get("check_key") or "") or {}
        findings = list(check.get("findings", []))
        artifact = result.get(config["state_key"], empty)
        # Findings make an artifact a draft, not an absent artifact. Keep it
        # visible as a review warning; a generated artifact may still feed the
        # next design stage.
        artifacts[stage] = artifact
        if findings and artifact:
            artifact_status[stage] = "needs_review"
        validation[stage] = {
            "valid": result.get(config["valid_key"]) if config["valid_key"] else None,
            "errors": (result.get(config["errors_key"], []) if config["errors_key"] else []),
            "findings": findings,
            # Targeted design checks retain typed evidence alongside their
            # legacy display strings.  Workspace uses this to distinguish a
            # genuine user decision from a technical repair finding.
            "finding_details": list(check.get("finding_details") or []),
            "check_status": check.get("stopped"),
            "repair_iters": check.get("repair_iters", 0),
            # Repair state is stage-agnostic evidence consumed by Workspace
            # when it decides whether a repair is active or stalled.
            "repair_history": check.get("repair_history"),
            # Sequence reconciliation may need a new class operation.  The
            # proposal is review data, not an automatic class-diagram change.
            "method_proposals": list(check.get("method_proposals") or []),
        }

    deployment_bundle = result.get("deployment_diagram_bundle") or {}
    artifact_metadata: dict[str, Any] = {}
    if deployment_bundle:
        projections = [
            item
            for item in deployment_bundle.get("projections") or []
            if isinstance(item, dict) and isinstance(item.get("target"), dict)
        ]
        artifact_metadata["deployment_diagram"] = {
            "schemaVersion": deployment_bundle.get("schemaVersion"),
            "readOnly": deployment_bundle.get("readOnly") is True,
            "regeneration": deployment_bundle.get("regeneration"),
            "status": deployment_bundle.get("status"),
            "selection": deployment_bundle.get("selection"),
            "selectedTarget": deployment_bundle.get("selectedTarget"),
            "sizing": deployment_bundle.get("sizing"),
            "targets": [
                {
                    **dict(item["target"]),
                    "status": item.get("status"),
                    "issueCount": len(item.get("issues") or []),
                }
                for item in projections
            ],
        }
    return {
        "artifacts": artifacts,
        "validation": validation,
        "artifact_status": artifact_status,
        "artifact_metadata": artifact_metadata,
    }


@router.get("/api/apps/{app_id}")
def get_app(app_id: str) -> JSONResponse:
    """이 앱에 저장된 산출물 전부.

    브라우저는 상태를 안 들고 있으므로, 새로고침한 화면이 자기를 복원하는 통로다.
    진행 상황(어느 게이트에서 멈췄나)은 각 에이전트가 자기 주소로 따로 알려준다 —
    그건 저장소가 아는 것이 아니라 실행이 아는 것이다.
    """
    validate_app_id(app_id)
    return JSONResponse(content={"app_id": app_id, **to_web_response(require_app(app_id))})


@router.get("/api/apps/{app_id}/trace")
def get_artifact_trace(app_id: str, ref: str | None = None) -> JSONResponse:
    """선택 산출물 주소의 직접·상하류 관계를 읽기 전용으로 반환한다."""

    validate_app_id(app_id)
    try:
        return JSONResponse(content=artifact_trace_response(app_id, ref))
    except AppNotFound as error:
        raise HTTPException(status_code=404, detail="Unknown app id.") from error
    except UnknownTraceRef as error:
        raise HTTPException(status_code=404, detail=f"Unknown artifact trace ref: {error}") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=f"Invalid artifact trace ref: {error}") from error


@router.get("/api/apps/{app_id}/stages/{stage}/versions")
def list_stage_versions(app_id: str, stage: str) -> JSONResponse:
    validate_app_id(app_id)
    validate_stage_name(stage)
    require_app_exists(app_id)
    return JSONResponse(content={"versions": artifact_repository.list_versions(app_id, stage)})


@router.get("/api/apps/{app_id}/stages/{stage}/versions/{version_no}")
def get_stage_version(app_id: str, stage: str, version_no: int) -> JSONResponse:
    validate_app_id(app_id)
    validate_stage_name(stage)
    require_app_exists(app_id)
    content = artifact_repository.get_version_content(app_id, stage, version_no)
    if content is None:
        raise HTTPException(status_code=404, detail="Version not found.")
    return JSONResponse(content={"version_no": version_no, "content": content})


@router.get("/api/apps/{app_id}/stages/{stage}/image.{extension}")
def get_stage_image(app_id: str, stage: str, extension: str) -> Response:
    """산출물 저장 직후 미리 렌더링한 이미지를 돌려준다.

    정상 흐름에서는 메모리 cache만 읽는다. 서버 재시작 뒤 처음 보는 기존 산출물처럼 cache가
    비어 있을 때만 현재 stage를 복원해 cache를 다시 채우며, 그 다음 요청부터는 DB와 renderer를
    거치지 않는다.
    """
    validate_app_id(app_id)
    validate_stage_name(stage)
    if extension not in ("png", "svg"):
        raise HTTPException(status_code=404, detail="Unsupported image format.")
    if stage not in PUML_FIELDS:
        raise HTTPException(status_code=404, detail="Stage has no diagram image.")

    image = artifact_image_cache.get(app_id, stage, MAIN_VIEW, extension)
    if image is None:
        state = require_app(app_id)
        try:
            warm_artifact_images(app_id, stage, state)
        except Exception as error:
            raise HTTPException(status_code=500, detail="Diagram rendering failed.") from error
        image = artifact_image_cache.get(app_id, stage, MAIN_VIEW, extension)
    if not image:
        raise HTTPException(status_code=404, detail="Artifact has not been generated.")

    return Response(
        content=image,
        media_type="image/svg+xml" if extension == "svg" else "image/png",
        headers=_NO_STORE_IMAGE_HEADERS,
    )


@router.get("/api/apps/{app_id}/stages/deployment_diagram/views/{view}/image.{extension}")
def get_deployment_diagram_view_image(
    app_id: str,
    view: str,
    extension: str,
    target: str | None = None,
) -> Response:
    """미리 렌더링한 deployment runtime 또는 provisioning 그림을 반환한다."""
    validate_app_id(app_id)
    if extension not in ("png", "svg"):
        raise HTTPException(status_code=404, detail="Unsupported image format.")
    views = {
        "runtime": RUNTIME_VIEW,
        "provisioning": PROVISIONING_VIEW,
    }
    base_view = views.get(view)
    if base_view is None:
        raise HTTPException(status_code=404, detail="Unknown deployment diagram view.")
    cache_view = deployment_target_view(base_view, target) if target else base_view
    image = artifact_image_cache.get(
        app_id, "deployment_diagram", cache_view, extension,
    )
    if image is None:
        state = require_app(app_id)
        try:
            warm_artifact_images(app_id, "deployment_diagram", state)
        except Exception as error:
            raise HTTPException(status_code=500, detail="Diagram rendering failed.") from error
        image = artifact_image_cache.get(
            app_id, "deployment_diagram", cache_view, extension,
        )
    if not image:
        raise HTTPException(status_code=404, detail="Artifact has not been generated.")
    return Response(
        content=image,
        media_type="image/svg+xml" if extension == "svg" else "image/png",
        headers=_NO_STORE_IMAGE_HEADERS,
    )


@router.get("/api/apps/{app_id}/stages/sequence_diagram/diagrams")
def list_sequence_diagrams(app_id: str) -> JSONResponse:
    """프론트엔드 갤러리용 유스케이스별 시퀀스 다이어그램 목록."""
    validate_app_id(app_id)
    diagrams = sequence_diagrams_from_state(require_app(app_id))
    return JSONResponse(
        content={
            "diagrams": [
                {
                    "use_case_id": str(diagram.get("use_case_id") or ""),
                    "use_case_name": str(diagram.get("use_case_name") or ""),
                }
                for diagram in diagrams
            ]
        }
    )


@router.get("/api/apps/{app_id}/stages/sequence_diagram/diagrams/{use_case_id}/image.{extension}")
def get_sequence_diagram_image(app_id: str, use_case_id: str, extension: str) -> Response:
    """선택한 유스케이스에 대해 미리 렌더링한 시퀀스 이미지를 반환한다."""
    validate_app_id(app_id)
    if extension not in ("png", "svg"):
        raise HTTPException(status_code=404, detail="Unsupported image format.")
    cache_view = sequence_view(use_case_id)
    image = artifact_image_cache.get(
        app_id, "sequence_diagram", cache_view, extension,
    )
    if image is None:
        state = require_app(app_id)
        try:
            warm_artifact_images(app_id, "sequence_diagram", state)
        except Exception as error:
            raise HTTPException(status_code=500, detail="Diagram rendering failed.") from error
        image = artifact_image_cache.get(
            app_id, "sequence_diagram", cache_view, extension,
        )
    if not image:
        raise HTTPException(status_code=404, detail="Sequence diagram not found.")
    return Response(
        content=image,
        media_type="image/svg+xml" if extension == "svg" else "image/png",
        headers=_NO_STORE_IMAGE_HEADERS,
    )
