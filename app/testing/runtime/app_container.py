"""복원된 애플리케이션 폴더를 Docker로 실행한다."""

from __future__ import annotations

import hashlib
import os
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from app.design.contracts.application_runtime import SYNTHETIC_UUID_BASIC_USERNAME
from app.implementation.runtime.process import (
    _terminate_process_tree,
    run_process_tree,
)
from app.testing.runtime.container_runner import (
    GRADLE_CACHE_PATH,
    GRADLE_CACHE_VOLUME,
    configured_runner_image,
    prepare_gradle_cache,
)

DEFAULT_START_TIMEOUT_SECONDS = 360
_EXPOSE = re.compile(r"(?mi)^\s*EXPOSE\s+(?P<port>\d+)")
_FALLBACK_CONTAINER_PORT = 8080
_ENVIRONMENT_BUILD_FAILURE_MARKERS = (
    "failed to fetch",
    "connection reset",
    "connection refused",
    "network is unreachable",
    "context deadline exceeded",
)
_GRADLE_CACHE_DENIAL_MARKERS = ("permission denied", "accessdeniedexception")
_ACTIVE_TESTING_CONTAINERS: set[str] = set()
_PROCESS_LOG_PREFIX = "easydep-application-process-"
_PROCESS_LOG_TAIL_BYTES = 64 * 1024


class ApplicationLaunchError(Exception):
    """생성된 애플리케이션을 실행할 수 없을 때 원인 소유자도 함께 전달한다."""

    def __init__(
        self,
        message: str,
        *,
        defect_class: str = "SUT_DEFECT",
        application_log: str = "",
    ) -> None:
        super().__init__(message)
        self.defect_class = defect_class
        self.application_log = application_log


def _docker(
    arguments: list[str], *, timeout: int, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    return run_process_tree(
        ["docker", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def exposed_port(context: Path) -> int:
    """Dockerfile의 EXPOSE 값이 있으면 사용하고, 없으면 8080을 사용한다."""
    dockerfile = context / "Dockerfile"
    if not dockerfile.is_file():
        raise ApplicationLaunchError(
            f"The restored application has no Dockerfile: {context}"
        )
    match = _EXPOSE.search(dockerfile.read_text(encoding="utf-8"))
    return int(match.group("port")) if match else _FALLBACK_CONTAINER_PORT


def free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _responds(url: str) -> bool:
    """health endpoint가 실제 성공 응답을 반환하는지 확인한다."""
    try:
        response = urllib.request.urlopen(url, timeout=5)  # noqa: S310 - localhost only
        return 200 <= response.status < 300
    except urllib.error.HTTPError:
        return False
    except (urllib.error.URLError, OSError):
        return False


def _container_logs(name: str) -> str:
    # 원문은 자르지 않는다. 화면과 LLM prompt의 미리보기만 별도로 제한한다.
    completed = _docker(["logs", name], timeout=30)
    return (completed.stdout or "") + (completed.stderr or "")


def _log_excerpt(logs: str, limit: int = 4000) -> str:
    """긴 로그에서 시작, 마지막 출력과 중간의 근본 예외를 함께 남긴다."""

    if len(logs) <= limit:
        return logs
    omitted = "\n... omitted ...\n"
    anchors = ("\nCaused by:", " with root cause", "Exception:", "\nERROR ")
    anchor = max(logs.rfind(marker) for marker in anchors)
    if anchor < 0:
        half = max(1, (limit - len(omitted)) // 2)
        return (logs[:half] + omitted + logs[-half:])[:limit]

    available = limit - (2 * len(omitted))
    if available < 3:
        return logs[:limit]
    edge_size = max(1, available // 4)
    middle_size = available - (2 * edge_size)
    middle_start = max(edge_size, anchor - (middle_size // 4))
    middle_end = min(len(logs) - edge_size, middle_start + middle_size)
    middle_start = max(edge_size, middle_end - middle_size)
    if middle_start <= edge_size or middle_end >= len(logs) - edge_size:
        half = max(1, (limit - len(omitted)) // 2)
        return (logs[:half] + omitted + logs[-half:])[:limit]
    return (
        logs[:edge_size]
        + omitted
        + logs[middle_start:middle_end]
        + omitted
        + logs[-edge_size:]
    )[:limit]


def application_log(runtime: Mapping[str, Any]) -> str:
    """현재 Testing이 소유한 실행 중인 앱의 전체 로그를 읽는다.

    외부 ``target_url``이나 보고서에서 온 임의 container 이름으로 Docker 로그를
    읽으면 다른 실행의 정보를 유출할 수 있다. 이 프로세스의
    :func:`running_application`이 아직 소유하고 있는 이름만 허용한다.
    """

    if not isinstance(runtime, Mapping):
        return ""
    if runtime.get("source") == "application-process":
        raw_path = runtime.get("logPath")
        if not isinstance(raw_path, str):
            return ""
        try:
            path = Path(raw_path).resolve()
            if (
                path.parent != Path(tempfile.gettempdir()).resolve()
                or not path.name.startswith(_PROCESS_LOG_PREFIX)
            ):
                return ""
            return _process_log_text(path, max_bytes=_PROCESS_LOG_TAIL_BYTES)
        except OSError:
            return ""
    if runtime.get("source") != "application":
        return ""
    name = runtime.get("container")
    if not isinstance(name, str):
        return ""
    if name not in _ACTIVE_TESTING_CONTAINERS:
        return ""
    return _container_logs(name)


def application_log_excerpt(runtime: Mapping[str, Any], *, limit: int = 6000) -> str:
    """전체 원문을 보존한 상태에서 표시용 미리보기만 제한한다."""

    return _log_excerpt(application_log(runtime), limit=limit)


def _build_failure_defect_class(output: str) -> str:
    """외부 환경 때문에 실패했는지, 생성된 애플리케이션 문제인지 구분한다.

    Dockerfile이 존재하지 않는 파일을 ``COPY``하는 경우처럼 build context와
    Dockerfile이 맞지 않는 문제는 생성된 애플리케이션을 고쳐야 한다. 네트워크처럼
    코드를 바꿔도 해결할 수 없는 경우만 실행 환경 문제로 분류한다.
    """

    lowered = output.casefold()
    if (
        GRADLE_CACHE_PATH.casefold() in lowered
        and any(marker in lowered for marker in _GRADLE_CACHE_DENIAL_MARKERS)
    ):
        return "ENVIRONMENT_DEFECT"
    if any(marker in lowered for marker in _ENVIRONMENT_BUILD_FAILURE_MARKERS):
        return "ENVIRONMENT_DEFECT"
    return "SUT_DEFECT"


def runtime_identity(app_id: str, launch_id: str | None = None) -> tuple[str, str]:
    """병렬 Testing 작업마다 겹치지 않는 image와 container 이름을 만든다."""
    unique_launch_id = launch_id or uuid.uuid4().hex
    suffix = hashlib.sha256(
        f"{app_id}\0{unique_launch_id}".encode()
    ).hexdigest()[:20]
    return f"easydep-testing:{suffix}", f"easydep-testing-{suffix}"


def runtime_network_name(container_name: str) -> str:
    """Derive a per-run network name from the already unique container name."""
    return f"{container_name}-net"


def _wait_until_ready(name: str, url: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _responds(url):
            return
        try:
            running = _docker(
                ["inspect", "-f", "{{.State.Running}}", name], timeout=30
            )
        except subprocess.TimeoutExpired as error:
            raise ApplicationLaunchError(
                "Docker timed out while checking the generated application container.",
                defect_class="ENVIRONMENT_DEFECT",
            ) from error
        if running.returncode != 0 or "true" not in (running.stdout or "").lower():
            logs = _container_logs(name)
            excerpt = _log_excerpt(logs)
            raise ApplicationLaunchError(
                "The generated application exited before accepting requests:\n"
                f"{excerpt}",
                defect_class=_build_failure_defect_class(logs),
                application_log=logs,
            )
        time.sleep(2)
    logs = _container_logs(name)
    excerpt = _log_excerpt(logs)
    raise ApplicationLaunchError(
        f"The generated application did not respond at {url} within {timeout} seconds:\n"
        f"{excerpt}",
        # 준비 시간 초과만으로 source 결함을 확정할 수 없다. 첫 Gradle 실행이나 Windows
        # bind mount가 느린 경우 코드를 고쳐도 달라지지 않으므로 환경 문제로 재실행한다.
        defect_class="ENVIRONMENT_DEFECT",
        application_log=logs,
    )


def _process_log_text(log_path: Path, *, max_bytes: int | None = None) -> str:
    """Read an owned process log through an independent handle without sharing offsets."""

    with log_path.open("rb") as log_file:
        if max_bytes is not None:
            log_file.seek(0, os.SEEK_END)
            log_file.seek(max(0, log_file.tell() - max_bytes))
        return log_file.read().decode("utf-8", errors="replace")


def _wait_until_process_ready(
    process: subprocess.Popen,
    url: str,
    timeout: int,
    log_file: Any,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _responds(url):
            return
        if process.poll() is not None:
            logs = _process_log_text(log_file)
            excerpt = _log_excerpt(logs)
            raise ApplicationLaunchError(
                "The generated application exited before accepting requests:\n"
                f"{excerpt}",
                defect_class=_build_failure_defect_class(logs),
                application_log=logs,
            )
        time.sleep(2)
    logs = _process_log_text(log_file)
    raise ApplicationLaunchError(
        f"The generated application did not respond at {url} within {timeout} seconds:\n"
        f"{_log_excerpt(logs)}",
        defect_class="ENVIRONMENT_DEFECT",
        application_log=logs,
    )


@contextmanager
def _running_application_in_fixed_runner(
    application_dir: Path,
    *,
    health_path: str,
    start_timeout_seconds: int,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Run the final integration app in its existing Linux runner (no nested Docker)."""
    host_port = free_port()
    environment = os.environ.copy()
    environment.update(
        {
            "SPRING_PROFILES_ACTIVE": "test",
            "SPRING_DATASOURCE_URL": (
                "jdbc:h2:mem:easydep_testing;MODE=MySQL;DB_CLOSE_DELAY=-1"
            ),
            "SPRING_DATASOURCE_USERNAME": "sa",
            "SPRING_DATASOURCE_PASSWORD": "",
            "SPRING_SECURITY_USER_NAME": SYNTHETIC_UUID_BASIC_USERNAME,
            "SPRING_SECURITY_USER_PASSWORD": "easydep-test",
            "SPRING_SECURITY_USER_ROLES": "USER",
            "SERVER_ADDRESS": "127.0.0.1",
            "SERVER_PORT": str(host_port),
        }
    )
    command = ["gradle", "bootRun", "--no-daemon", "--build-cache"]
    named_log = tempfile.NamedTemporaryFile(prefix=_PROCESS_LOG_PREFIX, delete=False)
    log_path = Path(named_log.name)
    named_log.close()
    process: subprocess.Popen | None = None
    try:
        with log_path.open("ab") as log_file:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=application_dir,
                    env=environment,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=(os.name != "nt"),
                )
            except OSError as error:
                raise ApplicationLaunchError(
                    f"The application process could not start: {type(error).__name__}: {error}",
                    defect_class="ENVIRONMENT_DEFECT",
                ) from error

            normalized_health = (
                health_path if health_path.startswith("/") else f"/{health_path}"
            )
            base_url = f"http://127.0.0.1:{host_port}"
            _wait_until_process_ready(
                process,
                f"{base_url}{normalized_health}",
                start_timeout_seconds,
                log_path,
            )
            runtime = {
                "source": "application-process",
                "processId": process.pid,
                "hostPort": host_port,
                "healthPath": normalized_health,
                "profile": "test",
                "database": "h2-mysql-mode",
                "logPath": str(log_path),
            }
            yield base_url, runtime
    finally:
        if process is not None:
            _terminate_process_tree(process)
        try:
            log_path.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def running_application(
    app_id: str,
    application_dir: str | Path,
    *,
    launch_id: str | None = None,
    health_path: str = "/healthz",
    start_timeout_seconds: int = DEFAULT_START_TIMEOUT_SECONDS,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """공용 툴체인에서 복원된 backend를 실행하고 접속 URL을 반환한다.

    배포용 Dockerfile은 frontend까지 포함한 최종 image를 만드는 산출물이다. API 기능
    테스트마다 그 image를 다시 만들면 npm 설치와 Gradle dependency 다운로드가 반복된다.
    구현 단계가 이미 단위·작은 통합 테스트와 frontend build를 통과시켰으므로 여기서는
    고정 툴체인과 공유 Gradle cache로 Spring Boot backend만 실행한다.
    """
    context = Path(application_dir)
    if os.environ.get("EASYDEP_FIXED_LINUX_RUNNER") == "1":
        with _running_application_in_fixed_runner(
            context,
            health_path=health_path,
            start_timeout_seconds=start_timeout_seconds,
        ) as runtime:
            yield runtime
        return

    container_port = exposed_port(context)
    _, name = runtime_identity(app_id, launch_id)
    network = runtime_network_name(name)
    host_port = free_port()
    runner_image = configured_runner_image()
    try:
        prepare_gradle_cache(runner_image)
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        raise ApplicationLaunchError(
            f"The shared Gradle cache could not be prepared: {error}",
            defect_class="ENVIRONMENT_DEFECT",
        ) from error

    # 같은 실행 ID의 이전 비정상 종료가 남겼을 수 있는 container와 network를 모두
    # 정리한다. container만 제거하면 재개 시 동일한 deterministic network 이름이
    # 충돌하여 애플리케이션을 시작하기도 전에 Testing이 중단된다.
    _docker(["rm", "-f", name], timeout=60)
    _docker(["network", "rm", network], timeout=60)
    created_network = _docker(["network", "create", network], timeout=60)
    if created_network.returncode != 0:
        output = created_network.stderr or created_network.stdout or ""
        raise ApplicationLaunchError(
            "The Docker network for Testing could not be created:\n"
            + _log_excerpt(output, limit=2000),
            defect_class="ENVIRONMENT_DEFECT",
            application_log=output,
        )
    started = _docker(
        [
            "run",
            "-d",
            "--name",
            name,
            "--network",
            network,
            "--label",
            "easydep.owner=testing-application",
            "-v",
            f"{context.resolve()}:/easydep-application:rw",
            "-v",
            f"{GRADLE_CACHE_VOLUME}:{GRADLE_CACHE_PATH}",
            "-w",
            "/easydep-application",
            "-e",
            f"GRADLE_USER_HOME={GRADLE_CACHE_PATH}",
            # Testing은 아직 CSP의 실제 DB를 provision하지 않는다. 생성 애플리케이션이
            # 외부 DB 주소 때문에 실패하지 않도록 함께 생성된 test profile과 임시 H2를 쓴다.
            "-e",
            "SPRING_PROFILES_ACTIVE=test",
            "-e",
            "SPRING_DATASOURCE_URL=jdbc:h2:mem:easydep_testing;MODE=MySQL;DB_CLOSE_DELAY=-1",
            "-e",
            "SPRING_DATASOURCE_USERNAME=sa",
            "-e",
            "SPRING_DATASOURCE_PASSWORD=",
            # 생성기가 인증 요구를 발견하면 이 표준 Spring 변수를 필수로 만든다. 운영
            # 비밀값을 재사용하지 않고 Testing 전용 계정을 주입해 같은 image를 안전하게 띄운다.
            "-e",
            f"SPRING_SECURITY_USER_NAME={SYNTHETIC_UUID_BASIC_USERNAME}",
            "-e",
            "SPRING_SECURITY_USER_PASSWORD=easydep-test",
            "-e",
            "SPRING_SECURITY_USER_ROLES=USER",
            "-p",
            f"127.0.0.1:{host_port}:{container_port}",
            "--entrypoint",
            "gradle",
            runner_image,
            "bootRun",
            "--no-daemon",
            "--build-cache",
        ],
        timeout=120,
    )
    if started.returncode != 0:
        _docker(["network", "rm", network], timeout=120)
        output = started.stderr or started.stdout or ""
        raise ApplicationLaunchError(
            "The generated application could not start in the shared toolchain:\n"
            + _log_excerpt(output, limit=2000),
            defect_class="ENVIRONMENT_DEFECT",
            application_log=output,
        )

    base_url = f"http://localhost:{host_port}"
    try:
        normalized_health = health_path if health_path.startswith("/") else f"/{health_path}"
        _wait_until_ready(name, f"{base_url}{normalized_health}", start_timeout_seconds)
        runtime = {
            "source": "application",
            "image": runner_image,
            "container": name,
            "network": network,
            "containerPort": container_port,
            "hostPort": host_port,
            "healthPath": normalized_health,
            "profile": "test",
            "database": "h2-mysql-mode",
        }
        _ACTIVE_TESTING_CONTAINERS.add(name)
        try:
            yield base_url, runtime
        finally:
            _ACTIVE_TESTING_CONTAINERS.discard(name)
    finally:
        _docker(["rm", "-f", name], timeout=120)
        _docker(["network", "rm", network], timeout=120)
