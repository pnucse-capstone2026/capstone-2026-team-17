"""Testing이 생성 앱과 배포 파일을 검사할 때 쓰는 공용 툴체인."""

from __future__ import annotations

import os
import subprocess
import time
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread

from app.config import settings
from app.implementation.runtime.process import run_process_tree
from app.testing.progress import emit_testing_progress, testing_progress_enabled

RUNNER_IMAGE_ENV = "EASYDEP_TOOLCHAIN_IMAGE"
DEFAULT_RUNNER_IMAGE = "easydep-toolchain:local"
GRADLE_CACHE_VOLUME = "easydep-member-gradle-cache"
GRADLE_CACHE_PATH = "/tmp/easydep-gradle-cache"
TOFU_CACHE_VOLUME = "easydep-tofu-provider-cache"
TOFU_CACHE_PATH = "/app/.cache/opentofu"
CONTAINER_CHECK_ROOT = "/easydep-check"
_HEARTBEAT_INTERVAL_SECONDS = 30
_TOOLCHAIN_USER_ID = "1000:1000"
_cache_ownership_lock = Lock()
_prepared_tofu_cache_images: set[str] = set()
_prepared_gradle_cache_images: set[str] = set()


@dataclass(frozen=True)
class ToolchainExecution:
    """공용 툴체인에서 한 명령을 실행한 결과.

    ``command``는 사용자 산출물에 적용한 짧은 명령이고, ``toolchain``은
    그 명령을 실제로 실행한 환경이다. Docker가 컨테이너를 시작하지
    못한 경우만 ``environment_error``를 참으로 두어, 파일 검사 실패와
    실행 환경 실패를 혼동하지 않게 한다.
    """

    completed: subprocess.CompletedProcess[str]
    command: tuple[str, ...]
    toolchain: str
    environment_error: bool


def _tool_gate(command: list[str]) -> tuple[str, str]:
    executable = str(command[0] if command else "toolchain").lower()
    if executable == "trivy":
        return "static", "Scanning deployment configuration"
    if executable == "tofu":
        return "iac", "Validating infrastructure code"
    return "package", "Checking deployment package"


def _command_heartbeat(command: list[str]) -> tuple[Event, Thread | None]:
    """Emit periodic progress while one toolchain command is blocked."""

    stopped = Event()
    if not testing_progress_enabled():
        return stopped, None
    context = copy_context()
    gate, label = _tool_gate(command)
    started = time.perf_counter()

    def pulse() -> None:
        attempt = 0
        while True:
            if stopped.wait(_HEARTBEAT_INTERVAL_SECONDS):
                return
            attempt += 1
            context.run(
                emit_testing_progress,
                phase="static",
                scope="gate",
                status="RUNNING",
                label=label,
                detail="Toolchain command is still running.",
                gate=gate,
                attempt=attempt,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )

    worker = Thread(target=pulse, name="easydep-testing-tool-heartbeat", daemon=True)
    worker.start()
    return stopped, worker


def configured_runner_image(environment: dict[str, str] | None = None) -> str:
    """환경변수와 공용 설정에서 로컬 toolchain image 이름을 고른다."""

    source = os.environ if environment is None else environment
    value = source.get(RUNNER_IMAGE_ENV, "").strip()
    return value or (settings.easydep_toolchain_image or "").strip() or DEFAULT_RUNNER_IMAGE


def _prepare_tofu_cache(image: str, timeout: int) -> None:
    """Make a legacy root-owned provider cache writable by the toolchain user.

    Older runner invocations could populate the named volume as root.  The current
    toolchain image runs checks as ``appuser``; normalize ownership once per server
    process before OpenTofu tries to create its provider lock file.
    """

    with _cache_ownership_lock:
        if image in _prepared_tofu_cache_images:
            return
        command = [
            "docker",
            "run",
            "--rm",
            "--user",
            "root",
            "--network",
            "none",
            "--security-opt",
            "no-new-privileges:true",
            "-v",
            f"{TOFU_CACHE_VOLUME}:{TOFU_CACHE_PATH}",
            "--entrypoint",
            "chown",
            image,
            "-R",
            _TOOLCHAIN_USER_ID,
            TOFU_CACHE_PATH,
        ]
        completed = run_process_tree(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=min(timeout, 120),
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-2000:]
            raise RuntimeError(
                "Could not prepare the shared OpenTofu provider cache"
                + (f": {detail}" if detail else ".")
            )
        _prepared_tofu_cache_images.add(image)


def prepare_gradle_cache(image: str, timeout: int = 120) -> None:
    """Make the implementation runner's shared Gradle cache writable by appuser."""

    with _cache_ownership_lock:
        if image in _prepared_gradle_cache_images:
            return
        command = [
            "docker",
            "run",
            "--rm",
            "--user",
            "root",
            "--network",
            "none",
            "--security-opt",
            "no-new-privileges:true",
            "-v",
            f"{GRADLE_CACHE_VOLUME}:{GRADLE_CACHE_PATH}",
            "--entrypoint",
            "chown",
            image,
            "-R",
            _TOOLCHAIN_USER_ID,
            GRADLE_CACHE_PATH,
        ]
        completed = run_process_tree(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=min(timeout, 120),
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-2000:]
            raise RuntimeError(
                "Could not prepare the shared Gradle cache"
                + (f": {detail}" if detail else ".")
            )
        _prepared_gradle_cache_images.add(image)


def run_toolchain_command(
    command: list[str],
    *,
    cwd: str | Path,
    timeout: int,
    environment: dict[str, str] | None = None,
) -> ToolchainExecution:
    """정적 검사 명령을 현재 소스와 같은 공용 Linux 툴체인에서 실행한다.

    백엔드가 이미 툴체인 컨테이너 안에 있으면 명령을 바로 실행한다.
    Windows 개발 환경에서는 검사 대상 폴더 하나만 mount하여 host의
    우연한 PATH, Git Bash, WSL 설정에 결과가 달라지지 않게 한다.

    이 함수는 외부 network를 끊은 채 실행하며, 호출자도 ``apply`` 명령을
    넘기지 않는다. 따라서 검사 중 실제 cloud resource가 바뀐 일은 없다.
    """

    if not command:
        raise ValueError("toolchain command must not be empty")

    working_directory = Path(cwd).resolve()
    process_environment = {**os.environ, **(environment or {})}
    heartbeat_stop, heartbeat_worker = _command_heartbeat(command)
    try:
        if os.environ.get("EASYDEP_FIXED_LINUX_RUNNER") == "1":
            completed = run_process_tree(
                command,
                cwd=working_directory,
                env=process_environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
            )
            return ToolchainExecution(
                completed=completed,
                command=tuple(command),
                toolchain="fixed-linux-runner",
                environment_error=False,
            )

        image = configured_runner_image()
        if str(command[0]).lower() == "tofu":
            _prepare_tofu_cache(image, timeout)
        docker_command = [
            "docker",
            "run",
            "--rm",
            "--init",
            "--network",
            "none",
            "--security-opt",
            "no-new-privileges:true",
            "--label",
            "easydep.owner=testing-tool",
            "-v",
            f"{working_directory}:{CONTAINER_CHECK_ROOT}",
            "-v",
            f"{TOFU_CACHE_VOLUME}:{TOFU_CACHE_PATH}",
            "-w",
            CONTAINER_CHECK_ROOT,
            "-e",
            "EASYDEP_FIXED_LINUX_RUNNER=1",
            "-e",
            f"TF_PLUGIN_CACHE_DIR={TOFU_CACHE_PATH}",
        ]
        for name, value in (environment or {}).items():
            docker_command.extend(["-e", f"{name}={value}"])
        docker_command.extend(["--entrypoint", command[0], image, *command[1:]])
        completed = run_process_tree(
            docker_command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
        return ToolchainExecution(
            completed=completed,
            command=tuple(command),
            toolchain=image,
            # Docker run의 125~127은 image/entrypoint/container 시작 실패이다.
            # 툴이 실행된 뒤 산출물을 거부한 반환 코드와 구분한다.
            environment_error=completed.returncode in {125, 126, 127},
        )
    finally:
        heartbeat_stop.set()
        if heartbeat_worker is not None:
            heartbeat_worker.join(timeout=0.1)
