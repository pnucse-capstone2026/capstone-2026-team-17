"""구현 중인 애플리케이션에서 화면과 저장소에 보여 줄 text 파일을 고른다.

실행 폴더에는 소스뿐 아니라 Gradle cache, 빌드 결과와 다운로드한 package도 생긴다. 이
모듈은 사람이 작성하거나 검토할 파일만 한 번의 공통 규칙으로 분류한다. 구현 완료 시 DB에
저장하는 snapshot과 실행 중 viewer가 같은 규칙을 사용하므로, 화면에서 보던 파일이 완료 뒤
갑자기 다른 종류로 바뀌지 않는다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from app.db.models import (
    TYPE_DEPLOYMENT_FILE,
    TYPE_FRONTEND_SOURCE_CODE,
    TYPE_IAC_CODE,
    TYPE_SOURCE_CODE,
    TYPE_TEST_CODE,
)

MAX_SOURCE_BYTES = 1_000_000
_IGNORED_DIRECTORIES = frozenset({
    ".git", ".gradle", ".idea", ".svelte-kit", "build", "dist", "node_modules", "target"
})
_SECRET_NAMES = frozenset({
    ".npmrc", "credentials", "credentials.json", "id_dsa", "id_ed25519", "id_rsa"
})
_SECRET_SUFFIXES = frozenset({".jks", ".key", ".keystore", ".p12", ".pfx", ".pem"})


@dataclass(frozen=True)
class ApplicationSourceFile:
    """viewer와 완료 snapshot이 공유하는 파일 한 건이다."""

    workspace_path: str
    artifact_type: str
    artifact_path: str
    content: str
    sha256: str
    size: int


def classify_source_path(workspace_path: str) -> tuple[str, str]:
    """애플리케이션 기준 경로를 저장 산출물 종류와 그 안의 경로로 나눈다."""

    relative = workspace_path.replace("\\", "/").lstrip("/")
    lowered = relative.lower()
    if relative.startswith("frontend/"):
        return TYPE_FRONTEND_SOURCE_CODE, relative.removeprefix("frontend/")
    if relative.startswith("deployment/"):
        # OpenTofu 파일은 기존 IaC gate가 독립적으로 고정할 수 있게 IAC_CODE에 두고,
        # README·Compose·cloud-init·실행 script·환경변수 예시는 배포 패키지 한 묶음으로
        # 저장한다. materialize 시 두 snapshot은 같은 application 경로에 합쳐진다.
        if "/tofu/" in f"/{lowered}" or lowered.endswith((".tf", ".tf.json", ".tftpl")):
            return TYPE_IAC_CODE, relative
        return TYPE_DEPLOYMENT_FILE, relative
    if "/test/" in f"/{lowered}":
        return TYPE_TEST_CODE, relative
    if relative == ".dockerignore" or any(
        token in lowered for token in ("k8s/", "dockerfile", "helm/")
    ):
        return TYPE_DEPLOYMENT_FILE, relative
    return TYPE_SOURCE_CODE, relative


def is_visible_source_path(workspace_path: str) -> bool:
    """경로만 보고 build·secret 파일처럼 viewer에 내보내면 안 되는 항목을 거른다."""

    relative = Path(workspace_path.replace("\\", "/"))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return False
    lowered_parts = {part.lower() for part in relative.parts}
    name = relative.name.lower()
    return (
        not lowered_parts.intersection(_IGNORED_DIRECTORIES)
        # 실제 ``.env``에는 비밀값이 들어갈 수 있어 계속 제외한다. 이름과 설명만 담는
        # ``.env.example``은 사용자가 배포 패키지를 실행하는 데 필요한 문서이므로 저장하고
        # viewer에도 보여 준다.
        and (not name.startswith(".env") or name == ".env.example")
        and name not in _SECRET_NAMES
        and relative.suffix.lower() not in _SECRET_SUFFIXES
    )


def iter_application_sources(
    application_root: Path,
) -> Iterator[ApplicationSourceFile]:
    """안전하게 읽을 수 있는 UTF-8 text 파일을 경로 순서대로 반환한다."""

    root = application_root.resolve()
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        workspace_path = path.relative_to(root).as_posix()
        if not is_visible_source_path(workspace_path):
            continue
        try:
            size = path.stat().st_size
            if size > MAX_SOURCE_BYTES:
                continue
            raw = path.read_bytes()
            if b"\x00" in raw:
                continue
            content = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        artifact_type, artifact_path = classify_source_path(workspace_path)
        yield ApplicationSourceFile(
            workspace_path=workspace_path,
            artifact_type=artifact_type,
            artifact_path=artifact_path,
            content=content,
            sha256=hashlib.sha256(raw).hexdigest(),
            size=size,
        )


def read_application_source(
    application_root: Path, workspace_path: str
) -> ApplicationSourceFile:
    """viewer가 요청한 파일 하나를 검사한 뒤 읽는다.

    ``..``와 절대 경로는 물론, 심볼릭 링크로 애플리케이션 폴더 밖을 가리키는 경우도 막는다.
    binary·secret·build 파일과 1MB를 넘는 파일은 목록과 내용 API 모두에서 같은 방식으로
    거절한다.
    """

    normalized = workspace_path.replace("\\", "/").lstrip("/")
    if not is_visible_source_path(normalized):
        raise FileNotFoundError(normalized)
    root = application_root.resolve()
    target = root / Path(normalized)
    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise FileNotFoundError(normalized) from error
    relative = resolved.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise FileNotFoundError(normalized)
    if not resolved.is_file() or resolved.stat().st_size > MAX_SOURCE_BYTES:
        raise FileNotFoundError(normalized)
    raw = resolved.read_bytes()
    if b"\x00" in raw:
        raise FileNotFoundError(normalized)
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FileNotFoundError(normalized) from error
    artifact_type, artifact_path = classify_source_path(normalized)
    return ApplicationSourceFile(
        workspace_path=normalized,
        artifact_type=artifact_type,
        artifact_path=artifact_path,
        content=content,
        sha256=hashlib.sha256(raw).hexdigest(),
        size=len(raw),
    )
