"""고정 Linux runner의 자식 Python 프로세스에도 적용할 실행환경 호환 계층."""

from __future__ import annotations

import shutil
from pathlib import Path

RUNNER_WORKSPACE = Path("/easydep-workspace")
_INSTALLED = False
GRADLE_SYSTEM_PROPERTIES = (
    "-Dorg.gradle.caching=true",
    "-Dorg.gradle.daemon=false",
    "-Dorg.gradle.parallel=false",
    "-Dorg.gradle.workers.max=1",
    "-Dorg.gradle.vfs.watch=false",
    "-Dorg.gradle.jvmargs=-Xmx384m -Xss256k -XX:MaxMetaspaceSize=256m -XX:+UseSerialGC",
)


def gradle_command() -> list[str]:
    """공용 툴체인에 고정 설치된 Gradle 명령을 반환한다."""
    return [
        "gradle",
        *GRADLE_SYSTEM_PROPERTIES,
    ]


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from app.implementation.agents.verification import build
    from app.implementation.generation.orchestrator import PrototypeOrchestrator

    build.gradle_command = gradle_command
    original_promote = PrototypeOrchestrator._promote

    def promote_with_bind_fallback(
        orchestrator: PrototypeOrchestrator, staging: Path, final: Path
    ) -> None:
        try:
            original_promote(orchestrator, staging, final)
        except PermissionError:
            if final.exists():
                raise
            shutil.copytree(staging, final)
            shutil.rmtree(staging)

    PrototypeOrchestrator._promote = promote_with_bind_fallback
    _INSTALLED = True
