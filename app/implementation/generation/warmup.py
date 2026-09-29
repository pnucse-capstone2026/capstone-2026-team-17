"""Web 서버 시작 시 구현 runtime을 가능한 범위에서 미리 준비한다.

Docker가 없거나 image registry에 연결할 수 없어도 Web 서버 시작을 실패시키지 않는다.
오류는 로컬 진단 파일에 기록하며, 실제 사용자 작업은 정식 job 경로에서 같은 준비 명령을
다시 실행할 수 있다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..runtime.linux_runner_transport import RUNNER_GRADLE_CACHE_VOLUME
from ..runtime.linux_runner_transport import configured_runner_image
from ..config import npm_command_environment
from .frontend_scaffold import react_scaffold_files

WARMUP_SCHEMA = "easydep-implementation-warmup/v1alpha1"
WARMUP_GRADLE_BUILD = """plugins {
    id 'org.springframework.boot' version '3.3.13'
    id 'io.spring.dependency-management' version '1.1.6'
    id 'java'
}

java {
    toolchain { languageVersion = JavaLanguageVersion.of(21) }
}

repositories { mavenCentral() }

dependencies {
    implementation 'org.springframework.boot:spring-boot-starter-web'
    implementation 'org.springframework.boot:spring-boot-starter-actuator'
    implementation 'org.springframework.boot:spring-boot-starter-validation'
    implementation 'org.springframework.boot:spring-boot-starter-data-jpa'
    implementation 'org.flywaydb:flyway-core'
    implementation 'org.springdoc:springdoc-openapi-starter-webmvc-ui:2.6.0'
    implementation 'com.google.code.findbugs:jsr305:3.0.2'
    implementation 'com.fasterxml.jackson.datatype:jackson-datatype-jsr310'
    implementation 'org.openapitools:jackson-databind-nullable:0.2.10'
    runtimeOnly 'com.h2database:h2'
    testImplementation 'org.springframework.boot:spring-boot-starter-test'
}
"""


def warmup_implementation_runtime(
    repository_root: Path, command_timeout_seconds: int
) -> dict[str, Any]:
    """서버 시작을 막지 않으면서 재사용할 image와 dependency cache를 준비한다."""
    repository_root = repository_root.resolve()
    root = repository_root / ".easydep" / "implementation-warmup"
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / "report.json"
    timeout = max(60, min(command_timeout_seconds, 900))
    steps: list[dict[str, Any]] = []

    def run(
        name: str, command: list[str], cwd: Path, *, env: dict[str, str] | None = None
    ) -> bool:
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=timeout,
                check=False,
            )
            ok = result.returncode == 0
            detail = "ready" if ok else (result.stderr or result.stdout)[-500:]
            exit_code: int | None = result.returncode
        except (OSError, subprocess.TimeoutExpired) as error:
            ok = False
            detail = str(error)[-500:]
            exit_code = None
        steps.append(
            {
                "name": name,
                "status": "SUCCEEDED" if ok else "FAILED",
                "exitCode": exit_code,
                "durationMs": int((time.monotonic() - started) * 1000),
                "detail": detail,
            }
        )
        return ok

    # API·구현·Testing이 같은 이미지를 사용하므로 별도 OpenAPI/Gradle 이미지를 받지 않는다.
    toolchain_image = configured_runner_image()
    if toolchain_image:
        available = run(
            "inspect-toolchain",
            ["docker", "image", "inspect", toolchain_image],
            repository_root,
        )
        if not available:
            run("pull-toolchain", ["docker", "pull", toolchain_image], repository_root)

    gradle_project = root / "gradle"
    gradle_project.mkdir(parents=True, exist_ok=True)
    (gradle_project / "settings.gradle").write_text(
        "rootProject.name = 'easydep-implementation-warmup'\n", encoding="utf-8"
    )
    (gradle_project / "build.gradle").write_text(WARMUP_GRADLE_BUILD, encoding="utf-8")
    # `gradle dependencies` is a report task: it resolves the dependency graph
    # from POM metadata but never materialises the classpath, so the artifact
    # jars stay unfetched and the first real job still downloads the whole
    # Spring stack.  Compiling one throwaway source forces the compile
    # classpath to resolve, which is what actually fills the shared cache.
    warmup_source = gradle_project / "src" / "main" / "java" / "easydep"
    warmup_source.mkdir(parents=True, exist_ok=True)
    (warmup_source / "Warmup.java").write_text(
        "package easydep;\n\n"
        "/** Throwaway source that forces the compile classpath to resolve. */\n"
        "public final class Warmup {\n"
        "  private Warmup() {}\n"
        "}\n",
        encoding="utf-8",
    )
    local_gradle = shutil.which("gradle")
    if local_gradle:
        gradle_command = [
            local_gradle,
            "compileJava",
            "--no-daemon",
            "-Dorg.gradle.vfs.watch=false",
            "--build-cache",
        ]
    elif toolchain_image:
        gradle_command = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{gradle_project.resolve()}:/workspace",
            # 임시 컨테이너의 쓰기 계층에 cache를 두면 --rm과 함께 사라진다. 명시적으로
            # warmup을 켠 경우에만 실제 구현 runner와 같은 named volume을 채운다.
            "-v",
            f"{RUNNER_GRADLE_CACHE_VOLUME}:/tmp/easydep-gradle-home",
            "-e",
            "GRADLE_USER_HOME=/tmp/easydep-gradle-home",
            "-w",
            "/workspace",
            "--entrypoint",
            "gradle",
            toolchain_image,
            "compileJava",
            "--no-daemon",
            "-Dorg.gradle.vfs.watch=false",
            "--build-cache",
        ]
    else:
        gradle_command = ["gradle", "--version"]
    run(
        "warm-gradle-dependencies",
        gradle_command,
        gradle_project,
    )

    npm_project = root / "npm"
    npm_project.mkdir(parents=True, exist_ok=True)
    (npm_project / "package.json").write_text(
        react_scaffold_files("EasyDep warm-up", "")["package.json"],
        encoding="utf-8",
    )
    npm = "npm.cmd" if os.name == "nt" else "npm"
    run(
        "warm-npm-cache",
        [npm, "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund"],
        npm_project,
        env=npm_command_environment(),
    )

    report = {
        "schemaVersion": WARMUP_SCHEMA,
        "completedAt": datetime.now(UTC).isoformat(),
        "status": "SUCCEEDED" if all(step["status"] == "SUCCEEDED" for step in steps) else "PARTIAL",
        "steps": steps,
    }
    temporary = report_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(report_path)
    return report
