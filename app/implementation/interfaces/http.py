"""구현 단계가 생성한 파일을 프론트엔드에 제공하는 읽기 전용 HTTP API다.

구현 작업의 시작·수정·취소는 Workspace 명령이 application worker에 직접 전달한다.
이 모듈은 생성된 파일의 목록·내용·버전과 ZIP 다운로드만 제공하며, 서버의 실제 작업
디렉터리 경로는 응답에 포함하지 않는다.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.db.models import (
    TYPE_DEPLOYMENT_FILE,
    TYPE_FRONTEND_SOURCE_CODE,
    TYPE_IAC_CODE,
    TYPE_SOURCE_CODE,
    TYPE_TEST_CODE,
)
from app.repositories import artifact_repository
from app.repositories.artifact_repository import AppNotFound

from ..application.jobs import InvalidJobState, JobNotFound, worker
from ..domain.artifact_layout import application_artifact_path
from ..domain.artifact_release import artifact_release_id

router = APIRouter(prefix="/api/implementation", tags=["implementation"])
FILE_ARTIFACT_TYPES = {
    TYPE_SOURCE_CODE,
    TYPE_FRONTEND_SOURCE_CODE,
    TYPE_TEST_CODE,
    TYPE_DEPLOYMENT_FILE,
    TYPE_IAC_CODE,
}


@router.get("/apps/{app_id}/jobs/{job_id}/live")
def get_live_implementation_sources(app_id: str, job_id: str) -> dict[str, Any]:
    """진행 중인 구현 폴더의 안전한 text 파일 목록을 반환한다."""

    try:
        return worker.live_sources(job_id, app_id)
    except JobNotFound as error:
        raise HTTPException(
            status_code=404, detail="Active implementation sources are unavailable."
        ) from error


@router.get("/apps/{app_id}/jobs/{job_id}/live/files/{file_path:path}")
def get_live_implementation_file(
    app_id: str, job_id: str, file_path: str
) -> dict[str, Any]:
    """진행 중인 구현 폴더에서 검사가 끝난 UTF-8 text 파일 하나를 반환한다."""

    try:
        return worker.live_source_file(job_id, app_id, file_path)
    except (JobNotFound, FileNotFoundError) as error:
        raise HTTPException(status_code=404, detail="Live source file not found.") from error


@router.post("/apps/{app_id}/jobs/{job_id}/delivery/refresh")
def refresh_delivery_artifacts(app_id: str, job_id: str) -> dict[str, Any]:
    """LLM 구현을 반복하지 않고 현재 renderer로 배포 파일만 다시 저장한다."""

    try:
        return worker.refresh_delivery(job_id, app_id)
    except JobNotFound as error:
        raise HTTPException(status_code=404, detail="Implementation job not found.") from error
    except (InvalidJobState, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/apps/{app_id}/download")
def download_implementation_artifacts(app_id: str) -> StreamingResponse:
    """최신 구현 파일 snapshot들을 하나의 ZIP으로 묶어 다운로드한다."""
    try:
        snapshot_map = artifact_repository.load_file_snapshots(
            app_id, FILE_ARTIFACT_TYPES
        )
    except AppNotFound as error:
        raise HTTPException(status_code=404, detail="Unknown app id.") from error
    snapshots = [
        snapshot_map[artifact_type]
        for artifact_type in sorted(snapshot_map)
        if snapshot_map[artifact_type].get("files")
    ]
    if not snapshots:
        raise HTTPException(status_code=404, detail="Implementation artifacts are unavailable.")

    deployment_types = {TYPE_DEPLOYMENT_FILE, TYPE_IAC_CODE}
    present_deployment_types = deployment_types.intersection(snapshot_map)
    if present_deployment_types and present_deployment_types != deployment_types:
        raise HTTPException(
            status_code=409,
            detail="Deployment script and IaC must be published as one release.",
        )
    version_ids = {
        str(snapshot["artifact_type"]): int(snapshot["version_id"])
        for snapshot in snapshots
    }
    delivery_version_ids = {
        artifact_type: version_ids[artifact_type]
        for artifact_type in sorted(deployment_types)
        if artifact_type in version_ids
    }

    # 파일을 서버 디스크에 임시로 쓰지 않고 메모리에서 ZIP으로 조립한다. manifest에는
    # 어떤 산출물 버전이 들어갔는지 기록해 다운로드한 파일의 출처를 확인할 수 있게 한다.
    archive = io.BytesIO()
    manifest: dict[str, Any] = {
        "app_id": app_id,
        "release_id": artifact_release_id(version_ids),
        "delivery_release_id": (
            artifact_release_id(delivery_version_ids)
            if delivery_version_ids
            else None
        ),
        "implementation_job_ids": {
            str(snapshot["artifact_type"]): str(
                (snapshot.get("metadata") or {}).get("implementation_job_id") or ""
            )
            for snapshot in snapshots
        },
        "artifacts": [],
    }
    occupied_paths: dict[str, str] = {}
    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for snapshot in snapshots:
            artifact_type = str(snapshot["artifact_type"])
            files = snapshot.get("files") or {}
            artifact_entry = {
                "artifact_type": artifact_type,
                "version_id": snapshot.get("version_id"),
                "version_no": snapshot.get("version_no"),
                "snapshot_digest": snapshot.get("snapshot_digest"),
                "file_count": len(files),
                "files": [],
            }
            manifest["artifacts"].append(artifact_entry)
            for path, item in sorted(files.items()):
                try:
                    relative = application_artifact_path(
                        artifact_type, str(path)
                    ).as_posix()
                except ValueError:
                    # 저장소에서도 검사하지만 ZIP을 만들 때 한 번 더 막는다. 손상된 경로
                    # 하나 때문에 압축을 푼 위치 밖에 파일이 만들어져서는 안 된다.
                    continue
                previous_type = occupied_paths.get(relative)
                if previous_type is not None:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Implementation artifacts use the same application path: "
                            f"{relative} ({previous_type}, {artifact_type})."
                        ),
                    )
                content = item.get("content", "") if isinstance(item, dict) else str(item)
                encoded = content.encode("utf-8")
                actual_sha256 = hashlib.sha256(encoded).hexdigest()
                stored_sha256 = str(item.get("sha256") or "") if isinstance(item, dict) else ""
                if stored_sha256 and stored_sha256 != actual_sha256:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Artifact content hash does not match its snapshot: {relative}",
                    )
                bundle.writestr(relative, content)
                occupied_paths[relative] = artifact_type
                artifact_entry["files"].append(
                    {
                        "path": relative,
                        "size": len(encoded),
                        "sha256": actual_sha256,
                    }
                )
        bundle.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
    archive.seek(0)
    return StreamingResponse(
        archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="easydep-{app_id}-implementation.zip"'
            )
        },
    )


@router.get("/apps/{app_id}/artifacts/{artifact_type}")
def get_file_artifact(app_id: str, artifact_type: str) -> dict:
    """파일 내용은 제외하고 현재 snapshot의 파일 경로와 SHA-256을 반환한다."""
    if artifact_type not in FILE_ARTIFACT_TYPES:
        raise HTTPException(status_code=404, detail="Unknown implementation artifact type.")
    try:
        snapshot = artifact_repository.load_file_snapshot(app_id, artifact_type)
    except AppNotFound as error:
        raise HTTPException(status_code=404, detail="Unknown app id.") from error
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Artifact has not been generated.")
    snapshot["files"] = [{"path": path, "sha256": value["sha256"]} for path, value in snapshot["files"].items()]
    return snapshot


@router.get("/apps/{app_id}/artifacts/{artifact_type}/versions")
def list_file_artifact_versions(app_id: str, artifact_type: str) -> dict:
    """지정한 파일 산출물의 저장 버전 목록을 반환한다."""
    if artifact_type not in FILE_ARTIFACT_TYPES:
        raise HTTPException(status_code=404, detail="Unknown implementation artifact type.")
    try:
        versions = artifact_repository.list_file_artifact_versions(app_id, artifact_type)
    except AppNotFound as error:
        raise HTTPException(status_code=404, detail="Unknown app id.") from error
    return {"artifact_type": artifact_type, "versions": versions}


@router.get("/apps/{app_id}/artifacts/{artifact_type}/files/{file_path:path}")
def get_file(app_id: str, artifact_type: str, file_path: str) -> dict:
    """현재 snapshot에서 파일 하나의 UTF-8 내용과 SHA-256을 반환한다."""
    if artifact_type not in FILE_ARTIFACT_TYPES:
        raise HTTPException(status_code=404, detail="Unknown implementation artifact type.")
    try:
        snapshot = artifact_repository.load_file_snapshot(app_id, artifact_type)
    except AppNotFound as error:
        raise HTTPException(status_code=404, detail="Unknown app id.") from error
    item = (snapshot or {}).get("files", {}).get(file_path)
    if item is None:
        raise HTTPException(status_code=404, detail="File not found.")
    return {"path": file_path, **item}
