"""PlantUML JAR와 대화하는 공유 도구: 실행 명령, 문법 검사, 상시 이미지 렌더러.

산출물별 diagram(클래스·시퀀스·ERD·배포)이 모두 같은 jar로 검사·렌더되므로
특정 산출물에 두지 않고 공유한다. 산출물별 "무엇을 그릴지"(BCE→PlantUML 변환 등)는
각 산출물 서비스에 있고, 여기서는 "어떻게 실행/검사/렌더할지"만 다룬다. 이미지 렌더는
FastAPI와 함께 실행되는 PicoWeb JVM 한 개와 내용별 메모리 cache를 사용한다.
"""
from __future__ import annotations

import atexit
import ctypes
import hashlib
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from collections import OrderedDict
from ctypes import wintypes
from pathlib import Path
from threading import RLock

from dotenv import load_dotenv

from app.design.observability import log_design_timing

load_dotenv()


# Keep checked-in SVG examples and API rendering on the exact same renderer.
# Updating PlantUML is an intentional dependency change: change this digest,
# regenerate the examples, and review the resulting SVG diff together.
PLANTUML_IMAGE = (
    "plantuml/plantuml@sha256:"
    "47870c1f76cfb3747bc7090bfe83013a4e3105b5a0bb1515e2baf5d3e2b3ee9d"
)

# 한 서버 process에서 보관할 SVG/PNG의 최대 개수다. 클래스 생성 중 preview와 여러
# 유스케이스의 시퀀스 그림이 함께 들어오므로 너무 작게 두지는 않되, 개발 서버를 오래
# 켜 두어도 이미지 bytes가 끝없이 쌓이지 않게 제한한다.
IMAGE_CACHE_CAPACITY = 512
RENDER_TIMEOUT_SECONDS = 30.0


class _WindowsJob:
    """Own a subprocess tree until this process closes its Job Object handle."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects are only available on Windows.")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_job = kernel32.CreateJobObjectW
        create_job.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        create_job.restype = wintypes.HANDLE
        self._kernel32 = kernel32
        self._handle = create_job(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

        class BasicLimit(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit),
                        ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        set_info = kernel32.SetInformationJobObject
        set_info.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        set_info.restype = wintypes.BOOL
        assign = kernel32.AssignProcessToJobObject
        assign.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        assign.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        self._close_handle = close_handle
        if not set_info(self._handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error
        if not assign(self._handle, wintypes.HANDLE(int(process._handle))):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def close(self) -> None:
        if self._handle:
            self._close_handle(self._handle)
            self._handle = None


def plantuml_command(*arguments: str) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "-i",
        PLANTUML_IMAGE,
        "-charset",
        "UTF-8",
        *arguments,
    ]


def check_plantuml_syntax(puml_text: str) -> list[str]:
    """Return syntax errors for a PlantUML source, empty when it is valid.

    Uses `-syntax -pipe`, so the source never touches the filesystem. PlantUML
    reports a valid diagram as its type plus an entity count, and an invalid one
    as ERROR / line number / message.
    """
    if not puml_text.strip():
        log_design_timing(
            "plantuml.syntax_check.skipped",
            reason="empty_source",
            source_chars=0,
        )
        return ["PlantUML code is empty."]

    started = time.perf_counter()
    local = shutil.which("puml")
    if local:
        try:
            with tempfile.TemporaryDirectory(prefix="easydep-puml-check-") as directory:
                source = Path(directory) / "diagram.puml"
                source.write_text(puml_text, encoding="utf-8")
                result = subprocess.run(
                    [local, str(source), "svg"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=30,
                    check=False,
                )
                rendered = list(Path(directory).glob("*.svg"))
                if result.returncode == 0 and rendered:
                    log_design_timing(
                        "plantuml.syntax_check.completed",
                        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                        exit_code=result.returncode,
                        source_chars=len(puml_text),
                        syntax_valid=True,
                        renderer="local",
                    )
                    return []
                detail = "\n".join(
                    value.strip() for value in (result.stdout, result.stderr)
                    if value.strip()
                )
                errors = [detail or "Local PlantUML syntax check failed."]
                log_design_timing(
                    "plantuml.syntax_check.completed",
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                    exit_code=result.returncode,
                    source_chars=len(puml_text),
                    syntax_valid=False,
                    renderer="local",
                )
                return errors
        except subprocess.TimeoutExpired:
            log_design_timing(
                "plantuml.syntax_check.failed",
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                reason="timeout",
                source_chars=len(puml_text),
                renderer="local",
            )
            return ["PlantUML syntax check timed out."]
    # 배포 이미지에는 PlantUML JAR가 이미 들어 있다. Docker를 다시 호출하지 않고 같은
    # JAR를 사용해야 문법 검사와 상시 이미지 렌더러의 버전도 정확히 일치한다.
    jar = _find_plantuml_jar()
    command = (
        ["java", "-jar", str(jar), "-charset", "UTF-8", "-syntax", "-pipe"]
        if jar
        else plantuml_command("-syntax", "-pipe")
    )
    renderer = "jar" if jar else "docker"
    try:
        docker_result = subprocess.run(
            command,
            input=puml_text.encode("utf-8"),
            capture_output=True,
            stdin=None,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        log_design_timing(
            "plantuml.syntax_check.failed",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            reason="renderer_not_available",
            source_chars=len(puml_text),
            renderer=renderer,
        )
        return ["PlantUML JAR 또는 Docker를 실행할 수 없습니다."]
    except subprocess.TimeoutExpired:
        log_design_timing(
            "plantuml.syntax_check.failed",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            reason="timeout",
            source_chars=len(puml_text),
            renderer=renderer,
        )
        return ["PlantUML syntax check timed out."]

    stdout = docker_result.stdout.decode("utf-8", errors="replace")
    stderr = docker_result.stderr.decode("utf-8", errors="replace")
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if lines and lines[0].upper() == "ERROR":
        location = f"line {lines[1]}" if len(lines) > 1 else "unknown line"
        message = " ".join(lines[2:]) or "Syntax error"
        errors = [f"{location}: {message}"]
        log_design_timing(
            "plantuml.syntax_check.completed",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            exit_code=docker_result.returncode,
            source_chars=len(puml_text),
            syntax_valid=False,
            renderer=renderer,
        )
        return errors

    if docker_result.returncode != 0:
        detail = f"{stdout}\n{stderr}".strip()
        errors = [detail or "PlantUML syntax check failed."]
        log_design_timing(
            "plantuml.syntax_check.completed",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            exit_code=docker_result.returncode,
            source_chars=len(puml_text),
            syntax_valid=False,
            renderer=renderer,
        )
        return errors

    log_design_timing(
        "plantuml.syntax_check.completed",
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        exit_code=docker_result.returncode,
        source_chars=len(puml_text),
        syntax_valid=True,
        renderer=renderer,
    )
    return []


def _encode6bit(value: int) -> str:
    """PlantUML URL 형식이 사용하는 64개 문자 중 하나를 반환한다."""
    if value < 10:
        return chr(48 + value)
    value -= 10
    if value < 26:
        return chr(65 + value)
    value -= 26
    if value < 26:
        return chr(97 + value)
    value -= 26
    return "-" if value == 0 else "_" if value == 1 else "?"


def _encode3bytes(first: int, second: int, third: int) -> str:
    """압축된 세 byte를 PlantUML URL 문자 네 개로 바꾼다."""
    return "".join(
        (
            _encode6bit(first >> 2),
            _encode6bit(((first & 0x3) << 4) | (second >> 4)),
            _encode6bit(((second & 0xF) << 2) | (third >> 6)),
            _encode6bit(third & 0x3F),
        )
    )


def _encode_plantuml_source(puml_text: str) -> str:
    """PlantUML HTTP server가 받는 deflate URL 문자열을 만든다.

    PicoWeb은 파일 upload 대신 ``/plantuml/svg/{encoded}`` 형태의 GET 요청을 받는다.
    표준 zlib header를 제외한 deflate bytes와 PlantUML 전용 64진 문자표를 사용한다.
    """
    compressor = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=-15)
    compressed = compressor.compress(puml_text.encode("utf-8")) + compressor.flush()
    encoded: list[str] = []
    for offset in range(0, len(compressed), 3):
        chunk = compressed[offset : offset + 3]
        block = _encode3bytes(
            chunk[0],
            chunk[1] if len(chunk) > 1 else 0,
            chunk[2] if len(chunk) > 2 else 0,
        )
        encoded.append(block[: len(chunk) + 1])
    return "".join(encoded)


def _find_plantuml_jar() -> Path | None:
    """설정값, 개발자 설치 위치, Docker image 위치 순서로 JAR를 찾는다."""
    configured = os.getenv("PLANTUML_JAR", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path.home() / ".local" / "share" / "plantuml" / "plantuml.jar",
        Path("/opt/plantuml/plantuml.jar"),
    ]
    return next(
        (candidate for candidate in candidates if candidate and candidate.is_file()),
        None,
    )


def _available_local_port() -> int:
    """여러 개발 서버를 동시에 띄워도 충돌하지 않는 loopback port를 고른다."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class PlantUmlRenderer:
    """계속 실행되는 PlantUML JVM과 작은 process-local 이미지 cache를 관리한다.

    서버가 시작될 때 PlantUML PicoWeb JVM을 한 번 실행한다. 각 그림은 PlantUML 원문의
    SHA-256과 형식으로 구분해 보관하므로, 같은 그림을 여러 화면이나 앱에서 요청해도 다시
    렌더링하지 않는다. cache miss만 loopback HTTP로 PicoWeb에 전달한다.
    """

    def __init__(self, capacity: int = IMAGE_CACHE_CAPACITY) -> None:
        self._capacity = max(1, capacity)
        self._images: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._process: subprocess.Popen[bytes] | None = None
        self._job: _WindowsJob | None = None
        self._base_url: str | None = None
        self._state_lock = RLock()
        # PicoWeb 호출과 같은 key의 첫 렌더를 직렬화한다. 렌더가 끝난 뒤에는 위 cache에서
        # 바로 반환하므로 일반 이미지 조회에는 lock 대기가 없다.
        self._render_lock = RLock()

    def start(self) -> bool:
        """PicoWeb JVM을 시작한다. 이미 실행 중이면 아무 일도 하지 않는다.

        JAR나 Java가 없는 테스트 환경에서는 ``False``를 반환한다. 정식 Docker image와
        개발 설치에는 JAR를 포함하며, 그 환경에서는 서버 시작 시 항상 ``True``가 된다.
        """
        with self._state_lock:
            if self._process is not None and self._process.poll() is None:
                return True
            if self._process is not None:
                self._process = None
                self._base_url = None
                stale_job = self._job
                self._job = None
                if stale_job is not None:
                    stale_job.close()

            jar = _find_plantuml_jar()
            java = shutil.which("java")
            if jar is None or java is None:
                return False

            port = _available_local_port()
            process = subprocess.Popen(
                [
                    java,
                    "-Dfile.encoding=UTF-8",
                    "-DPLANTUML_LIMIT_SIZE=16384",
                    "-jar",
                    str(jar),
                    f"-picoweb:{port}:127.0.0.1",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            job = None
            if os.name == "nt":
                try:
                    job = _WindowsJob(process)
                except OSError as error:
                    process.kill()
                    process.wait()
                    raise RuntimeError(
                        "Could not attach PlantUML PicoWeb to a Windows Job Object."
                    ) from error
            base_url = f"http://127.0.0.1:{port}/plantuml"
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/", timeout=0.25,
                    ):
                        self._process = process
                        self._job = job
                        self._base_url = base_url
                        return True
                except (OSError, urllib.error.URLError):
                    time.sleep(0.05)

            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            if job is not None:
                job.close()
            raise RuntimeError("PlantUML PicoWeb renderer did not start within 10 seconds.")

    def stop(self) -> None:
        """EasyDep 서버가 종료될 때 이 process가 시작한 JVM만 정리한다."""
        with self._state_lock:
            process = self._process
            job = self._job
            self._process = None
            self._job = None
            self._base_url = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        if job is not None:
            job.close()

    def render(self, puml_text: str, image_format: str = "png") -> bytes:
        """같은 PlantUML은 cache에서, 새 PlantUML만 계속 실행 중인 JVM에서 렌더링한다."""
        if image_format not in {"png", "svg"}:
            raise ValueError(f"Unsupported PlantUML image format: {image_format}")
        if not puml_text.strip():
            return b""

        cache_key = (hashlib.sha256(puml_text.encode("utf-8")).hexdigest(), image_format)
        with self._state_lock:
            cached = self._images.get(cache_key)
            if cached is not None:
                self._images.move_to_end(cache_key)
                return cached

        with self._render_lock:
            with self._state_lock:
                cached = self._images.get(cache_key)
                if cached is not None:
                    self._images.move_to_end(cache_key)
                    return cached

            image = self._render_with_persistent_server(puml_text, image_format)
            if not image:
                return b""
            with self._state_lock:
                self._images[cache_key] = image
                self._images.move_to_end(cache_key)
                while len(self._images) > self._capacity:
                    self._images.popitem(last=False)
            return image

    def _render_with_persistent_server(
        self, puml_text: str, image_format: str,
    ) -> bytes:
        """PicoWeb을 사용하고, JAR가 없는 개발 환경에서만 기존 Docker 실행을 보존한다."""
        if self.start():
            with self._state_lock:
                base_url = self._base_url
            if base_url is None:
                raise RuntimeError("PlantUML renderer started without an address.")
            url = f"{base_url}/{image_format}/{_encode_plantuml_source(puml_text)}"
            try:
                with urllib.request.urlopen(url, timeout=RENDER_TIMEOUT_SECONDS) as response:
                    return response.read()
            except (OSError, urllib.error.URLError) as error:
                # JVM이 예상치 못하게 종료되었다면 다음 호출에서 새 process를 시작할 수 있게
                # 상태를 비운다. 현재 요청은 명확한 실패로 남겨 잘못된 빈 그림을 cache하지 않는다.
                self.stop()
                raise RuntimeError("PlantUML PicoWeb rendering failed.") from error

        result = subprocess.run(
            plantuml_command("-pipe", f"-t{image_format}"),
            input=puml_text.encode("utf-8"),
            capture_output=True,
            timeout=RENDER_TIMEOUT_SECONDS,
            check=False,
        )
        return result.stdout

    def clear_cache(self) -> None:
        """테스트와 명시적인 개발 재시작에서 렌더된 이미지 bytes만 비운다."""
        with self._state_lock:
            self._images.clear()


plantuml_renderer = PlantUmlRenderer()
atexit.register(plantuml_renderer.stop)


def render_plantuml(puml_text: str, image_format: str = "png") -> bytes:
    """공유 PlantUML renderer에서 이미지 bytes를 가져오는 기존 public 함수다."""
    return plantuml_renderer.render(puml_text, image_format)
