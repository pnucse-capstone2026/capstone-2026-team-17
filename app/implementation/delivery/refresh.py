"""완료된 구현 코드는 유지하고 배포 패키지만 최신 renderer로 다시 만든다."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.db.models import TYPE_DEPLOYMENT_FILE, TYPE_IAC_CODE, TYPE_SOURCE_CODE
from app.implementation.application.source_files import iter_application_sources
from app.implementation.domain.artifact_layout import application_artifact_path
from app.implementation.domain.artifact_release import artifact_release_id
from app.implementation.workflows.coordinator import bind_deployment_runtime
from app.repositories import artifact_repository

from .terraform import render_iac


def refresh_delivery_artifacts(
    app_id: str,
    *,
    implementation_job_id: str,
) -> dict[str, Any]:
    """DB에 저장된 구현·설계 결과로 배포 파일 두 묶음만 새로 저장한다.

    Java와 프론트엔드 소스는 읽거나 수정하지 않는다. 기존 배포 스냅샷을 시스템 임시
    폴더에 복원한 다음, 설계 단계가 선택한 ResourcePlan으로 현재 OpenTofu·스크립트
    renderer를 다시 실행한다. 요청이 끝나면 임시 폴더는 자동으로 삭제된다.
    """

    state = artifact_repository.load_state(app_id)
    deployment_bundle = state.get("deployment_diagram_bundle")
    if not isinstance(deployment_bundle, dict) or not deployment_bundle:
        raise ValueError("A completed deployment diagram bundle is required.")

    snapshots: dict[str, dict[str, Any]] = {}
    # SOURCE_CODE의 Spring 설정을 읽어 실제 port와 health 경로를 배포 계획에 결합한다.
    # 이 단계를 생략하면 오래된 설계 기본값(/actuator/health)이 최신 앱(/healthz)과
    # 어긋날 수 있다. 소스 snapshot은 관찰에만 사용하고 새 버전으로 저장하지 않는다.
    for artifact_type in (TYPE_SOURCE_CODE, TYPE_DEPLOYMENT_FILE, TYPE_IAC_CODE):
        snapshot = artifact_repository.load_file_snapshot(app_id, artifact_type)
        if not isinstance(snapshot, dict) or not snapshot.get("files"):
            raise ValueError(f"Implementation artifact is unavailable: {artifact_type}")
        snapshots[artifact_type] = snapshot

    with tempfile.TemporaryDirectory(prefix="easydep-delivery-refresh-") as temporary:
        run_root = Path(temporary)
        application = run_root / "application"
        application.mkdir()

        # Dockerfile처럼 deployment 폴더 밖에 있는 배포 파일도 보존한다. 기존
        # deployment 폴더는 render_deployment_package()가 관리 표식을 확인한 뒤 교체한다.
        for artifact_type, snapshot in snapshots.items():
            for stored_path, raw_item in sorted(snapshot["files"].items()):
                item = raw_item if isinstance(raw_item, dict) else {}
                relative = application_artifact_path(artifact_type, str(stored_path))
                target = application / Path(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(str(item.get("content") or "").encode("utf-8"))

        bundle_path = run_root / "deployment-diagram-bundle.json"
        bundle_path.write_text(
            json.dumps(deployment_bundle, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        bound_bundle = bind_deployment_runtime(run_root, bundle_path)
        if bound_bundle is None:
            raise RuntimeError("Completed deployment bundle could not be runtime-bound.")
        report = render_iac(
            run_root,
            SimpleNamespace(inputs={"deploymentBundle": bound_bundle}),
        )

        groups: dict[str, dict[str, str]] = {
            TYPE_DEPLOYMENT_FILE: {},
            TYPE_IAC_CODE: {},
        }
        for source in iter_application_sources(application):
            if source.artifact_type in groups:
                groups[source.artifact_type][source.artifact_path] = source.content

        snapshots_to_save: dict[
            str, tuple[dict[str, str], dict[str, Any]]
        ] = {}
        source_version_ids = {
            artifact_type: int(snapshot["version_id"])
            for artifact_type, snapshot in snapshots.items()
            if snapshot.get("version_id") is not None
        }
        for artifact_type, files in groups.items():
            if not files:
                raise RuntimeError(f"Refreshed artifact is empty: {artifact_type}")
            snapshots_to_save[artifact_type] = (
                files,
                {
                    "implementation_job_id": implementation_job_id,
                    "operation": "DELIVERY_REFRESH",
                    "renderer": report.get("renderer"),
                    "source_artifact_version_ids": source_version_ids,
                },
            )
        # 배포 script와 HCL은 한 release다. 둘 중 하나만 새 최신 버전으로 보이는
        # 창을 만들지 않도록 repository의 단일 transaction API로 publish한다.
        version_ids = artifact_repository.save_file_snapshots(
            app_id, snapshots_to_save
        )
        delivery_release_id = artifact_release_id(version_ids)

    verification = report.get("verification")
    return {
        "app_id": app_id,
        "implementation_job_id": implementation_job_id,
        "artifact_version_ids": version_ids,
        "delivery_release_id": delivery_release_id,
        "provider": report.get("provider"),
        "verification": verification if isinstance(verification, dict) else {},
    }


__all__ = ["refresh_delivery_artifacts"]
