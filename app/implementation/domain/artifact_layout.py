"""구현 산출물의 저장 경로를 실행 가능한 애플리케이션 경로로 바꾼다.

파일 산출물은 DB에서 종류별로 나뉘어 저장된다. 하지만 Testing과 사용자가 받는 ZIP은
모두 같은 애플리케이션 폴더 구조를 사용해야 한다. 이 모듈은 그 경로 규칙만 한 곳에서
관리하여 두 경로가 다시 달라지지 않게 한다.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from app.db.models import (
    TYPE_DEPLOYMENT_FILE,
    TYPE_FRONTEND_SOURCE_CODE,
    TYPE_IAC_CODE,
    TYPE_SOURCE_CODE,
    TYPE_TEST_CODE,
)

# 프론트엔드 파일은 저장할 때 ``frontend/`` 접두사를 떼어 별도 산출물로 보관한다.
# 실행 가능한 앱으로 복원할 때에는 접두사를 다시 붙이고, 나머지는 앱 루트에 합친다.
ARTIFACT_APPLICATION_PREFIXES: dict[str, str] = {
    TYPE_SOURCE_CODE: "",
    TYPE_FRONTEND_SOURCE_CODE: "frontend",
    TYPE_TEST_CODE: "",
    TYPE_DEPLOYMENT_FILE: "",
    TYPE_IAC_CODE: "",
}


def application_artifact_path(artifact_type: str, file_path: str) -> PurePosixPath:
    """저장된 파일 경로를 애플리케이션 루트 기준의 안전한 경로로 반환한다."""

    try:
        prefix = ARTIFACT_APPLICATION_PREFIXES[artifact_type]
    except KeyError as error:
        raise ValueError(f"지원하지 않는 파일 산출물 종류입니다: {artifact_type}") from error

    candidate = PurePosixPath(file_path.replace("\\", "/"))
    if candidate.as_posix() == "." or candidate.is_absolute() or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        raise ValueError(f"안전하지 않은 산출물 파일 경로입니다: {file_path}")
    return PurePosixPath(prefix) / candidate if prefix else candidate


__all__ = ["ARTIFACT_APPLICATION_PREFIXES", "application_artifact_path"]
