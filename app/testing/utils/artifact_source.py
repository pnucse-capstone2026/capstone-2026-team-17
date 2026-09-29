"""구현 작업의 파일 묶음을 임시 애플리케이션 폴더에 한 번 복원한다."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.implementation.domain.artifact_layout import application_artifact_path
from app.repositories.artifact_repository import AppNotFound, load_file_snapshot
from app.testing.schemas.testing_input import TestingContracts, TestingInput


class ArtifactSourceUnavailable(Exception):
    """구현 작업이 가리키는 파일 묶음을 읽을 수 없을 때 발생한다."""


class ArtifactSnapshotMismatch(Exception):
    """파일 묶음의 경로·내용이 손상되었거나 서로 겹칠 때 발생한다."""


def capture_testing_input(
    app_id: str,
    implementation_job_id: str,
    *,
    artifact_version_ids: Mapping[str, int] | None,
    contract_artifacts: Mapping[str, Any] | None = None,
    implementation_traceability: Mapping[str, Any] | None = None,
) -> TestingInput:
    """구현 작업 기록의 파일 묶음 ID를 Testing 입력으로 고정한다."""
    if artifact_version_ids is None:
        raise ArtifactSourceUnavailable(
            "The implementation job has no saved artifact IDs."
        )
    try:
        return TestingInput(
            app_id=app_id,
            implementation_job_id=implementation_job_id,
            artifact_version_ids=dict(artifact_version_ids),
            contract_artifacts=TestingContracts.model_validate(
                dict(contract_artifacts or {})
            ),
            implementation_traceability=(
                dict(implementation_traceability)
                if implementation_traceability is not None
                else None
            ),
        )
    except ValueError as error:
        raise ArtifactSourceUnavailable(str(error)) from error


def _load_snapshot(testing_input: TestingInput, artifact_type: str) -> Mapping[str, Any]:
    version_id = testing_input.artifact_version_ids[artifact_type]
    try:
        snapshot = load_file_snapshot(
            testing_input.app_id,
            artifact_type,
            version_id=version_id,
        )
    except AppNotFound as error:
        raise ArtifactSourceUnavailable(
            f"Unknown application ID: {testing_input.app_id}"
        ) from error
    if not snapshot or not snapshot.get("files"):
        raise ArtifactSourceUnavailable(
            f"Implementation artifact not found: type={artifact_type}, id={version_id}"
        )
    # Testing의 고정 출처는 Job 기록에 저장된 ``artifact_version_ids``다.
    # 수리 Job이 이전과 동일한 파일 묶음을 내면 새 버전을 만들지 않고
    # 기존 ID를 재사용한다. 이때 snapshot metadata에는 원래 Job ID가 남으므로,
    # metadata를 현재 Job ID와 비교하면 정상적인 공유 snapshot을 거부하게 된다.
    return snapshot


@contextmanager
def materialized_testing_application(testing_input: TestingInput) -> Iterator[Path]:
    """모든 파일 묶음을 한 번 복원하고 임시 ``run_root``를 반환한다."""
    with tempfile.TemporaryDirectory(prefix="easydep-testing-run-") as temporary:
        run_root = Path(temporary)
        application = run_root / "application"
        application.mkdir(parents=True)
        occupied_paths: dict[str, str] = {}

        for artifact_type in testing_input.artifact_version_ids:
            snapshot = _load_snapshot(testing_input, artifact_type)
            for raw_path, raw_item in sorted(snapshot["files"].items()):
                application_path = application_artifact_path(
                    artifact_type, str(raw_path)
                )
                restored = application_path.as_posix()
                previous_type = occupied_paths.get(restored)
                if previous_type is not None:
                    raise ArtifactSnapshotMismatch(
                        "Multiple artifacts use the same restored path: "
                        f"path={restored}, first={previous_type}, second={artifact_type}"
                    )

                item = raw_item if isinstance(raw_item, Mapping) else {}
                content = str(item.get("content") or "")
                expected_digest = str(item.get("sha256") or "").casefold()
                actual_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if not expected_digest or expected_digest != actual_digest:
                    raise ArtifactSnapshotMismatch(
                        f"{artifact_type} file digest does not match: path={raw_path}"
                    )

                target = application / Path(*application_path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                # Windows의 write_text 기본값은 LF를 CRLF로 바꾼다. DB에 저장된 POSIX
                # script를 그대로 복원하지 않으면 Linux toolchain의 ``bash -n``이 ``fi\r``나
                # ``done\r``를 닫힘 문법으로 인식하지 못한다. 저장 문자열의 UTF-8 byte를
                # 그대로 써서 구현 산출물과 Testing 입력을 동일하게 유지한다.
                target.write_bytes(content.encode("utf-8"))
                occupied_paths[restored] = artifact_type

        yield run_root
