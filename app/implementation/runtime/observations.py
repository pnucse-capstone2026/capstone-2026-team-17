"""생성된 애플리케이션 파일에서 배포에 필요한 실행값만 관찰한다.

이 모듈은 배포 계획의 값을 정답처럼 복사하지 않는다. Dockerfile, Spring 설정과
``src/main`` 파일에 실제 근거가 있을 때만 기존 ``bind_runtime_contract()``가 읽는
작은 dict 모양으로 값을 돌려준다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

RUNTIME_OBSERVATIONS_REPORT = "deployment-runtime.json"

_TEXT_SUFFIXES = frozenset({
    ".gradle", ".java", ".json", ".kt", ".kts", ".properties", ".xml", ".yaml", ".yml",
})


def _read(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _application_text(application: Path) -> str:
    """배포 산출물을 제외한 실제 애플리케이션 입력 파일만 합친다."""

    paths = [application / "Dockerfile"]
    source_root = application / "src" / "main"
    if source_root.is_dir():
        paths.extend(
            path for path in source_root.rglob("*")
            if path.is_file() and path.suffix.lower() in _TEXT_SUFFIXES
        )
    return "\n".join(_read(path) for path in paths)


def _spring_settings(application: Path) -> dict[str, Any]:
    for name in ("application.yml", "application.yaml"):
        path = application / "src" / "main" / "resources" / name
        source = _read(path)
        if not source:
            continue
        try:
            parsed = yaml.safe_load(source)
        except yaml.YAMLError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _observed_port(application: Path, settings: dict[str, Any]) -> int | None:
    server_value = settings.get("server")
    server: dict[str, Any] = server_value if isinstance(server_value, dict) else {}
    configured = server.get("port")
    configured_port = configured if isinstance(configured, int) and not isinstance(
        configured, bool
    ) else None
    exposed = {
        int(match.group(1))
        for match in re.finditer(
            r"(?im)^\s*EXPOSE\s+(\d+)(?:/tcp)?\s*$",
            _read(application / "Dockerfile"),
        )
    }
    docker_source = _read(application / "Dockerfile")
    env_match = re.search(
        r"(?im)^\s*ENV\s+SERVER_PORT\s*=\s*(\d+)\s*$",
        docker_source,
    )
    env_port = int(env_match.group(1)) if env_match else None
    observed_port = configured_port if configured_port is not None else env_port
    if observed_port is not None and exposed and observed_port not in exposed:
        raise ValueError(
            "Dockerfile EXPOSE and the Spring Boot server port describe different ports"
        )
    # EXPOSE만으로 애플리케이션의 실제 listen port를 안다고 간주하지 않는다.
    # SERVER_PORT는 Spring Boot가 직접 읽는 실행 설정이므로 관찰 근거로 사용할 수 있다.
    return observed_port


def _joined_path(base: str, leaf: str) -> str:
    parts = [item.strip("/") for item in (base, leaf) if item and item != "/"]
    return "/" + "/".join(parts)


def _observed_health_path(application: Path, settings: dict[str, Any]) -> str | None:
    management = settings.get("management")
    management = management if isinstance(management, dict) else {}
    endpoints = management.get("endpoints")
    endpoints = endpoints if isinstance(endpoints, dict) else {}
    web = endpoints.get("web")
    web = web if isinstance(web, dict) else {}
    mapping = web.get("path-mapping")
    mapping = mapping if isinstance(mapping, dict) else {}
    health = mapping.get("health")
    configured = (
        _joined_path(str(web.get("base-path") or "/actuator"), str(health))
        if isinstance(health, str) and health.strip()
        else None
    )
    source_paths = {
        match.group(1)
        for path in (application / "src" / "main").rglob("*")
        if path.is_file() and path.suffix.lower() in {".java", ".kt"}
        for match in re.finditer(
            r"@(?:GetMapping|RequestMapping)\(\s*[\"']([^\"']*health[^\"']*)[\"']",
            _read(path),
            flags=re.IGNORECASE,
        )
        if match.group(1).startswith("/")
    } if (application / "src" / "main").is_dir() else set()
    candidates = {*source_paths, *([configured] if configured else [])}
    if len(candidates) > 1:
        raise ValueError("Application files declare more than one health endpoint")
    return next(iter(candidates), None)


def _uses_environment(source: str, name: str) -> bool:
    escaped = re.escape(name)
    patterns = (
        rf"\$\{{{escaped}(?=[:}}])",
        rf"(?:System\.)?getenv\(\s*[\"']{escaped}[\"']\s*\)",
        rf"environment\.get\(\s*[\"']{escaped}[\"']",
    )
    return any(re.search(pattern, source) for pattern in patterns)


def _required_environment_names(source: str) -> set[str]:
    """기본값 없이 참조한 대문자 환경 변수 이름을 찾는다."""

    return set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", source))


def _uses_path(source: str, path: str) -> bool:
    """문자열이 mount 경로 자체나 그 아래 파일을 사용하는지 확인한다."""

    if not path:
        return False
    filename_characters = frozenset(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./-"
    )
    for match in re.finditer(re.escape(path), source):
        before = source[match.start() - 1] if match.start() else ""
        after = source[match.end()] if match.end() < len(source) else ""
        if before and before in filename_characters:
            continue
        # `/data2`는 다른 경로지만 `/data/app.db`는 계획한 mount를 실제로 쓴다.
        if not after or after == "/" or after not in filename_characters:
            return True
    return False


def observe_runtime_contract(
    bundle: dict[str, Any], application: Path,
) -> dict[str, list[dict[str, Any]]]:
    """WorkloadGraph의 generated application을 실제 생성 파일과 대조한다.

    반환값은 ``{"workloads": [...]}``이며 workload 항목에는 확인된 ``interfaces``,
    ``configuration``과 ``mounts``만 들어간다. 계획만 있고 파일 근거가 없는 값은
    일부러 생략하여 기존 runtime binding 검사가 누락을 발견하게 한다.
    """

    graph = bundle.get("workloadGraph")
    graph = graph if isinstance(graph, dict) else {}
    workloads = [
        item for item in graph.get("workloads") or []
        if isinstance(item, dict)
        and isinstance(item.get("artifact"), dict)
        and item["artifact"].get("kind") == "generatedApplication"
    ]
    source = _application_text(application)
    settings = _spring_settings(application)
    port = _observed_port(application, settings)
    health_path = _observed_health_path(application, settings)
    observations: list[dict[str, Any]] = []
    for workload in workloads:
        observed: dict[str, Any] = {"workloadId": str(workload.get("id") or "")}
        interfaces = list(workload.get("interfaces") or [])
        # 한 프로세스의 포트와 health를 어느 interface가 소유하는지 파일만으로 구분할 수
        # 있을 때만 연결한다. 여러 interface에 계획값을 복사해 관찰값처럼 만들지 않는다.
        if len(interfaces) == 1 and isinstance(interfaces[0], dict):
            interface: dict[str, Any] = {
                "interfaceId": str(interfaces[0].get("id") or "")
            }
            if port is not None:
                interface["port"] = port
            if health_path is not None:
                interface["healthPath"] = health_path
            if len(interface) > 1:
                observed["interfaces"] = [interface]
        configuration_names = [
            str(item.get("name") or "")
            for item in workload.get("configuration") or []
            if isinstance(item, dict)
            and item.get("name")
            and _uses_environment(source, str(item["name"]))
        ]
        configuration_names.extend(
            sorted(_required_environment_names(source) - set(configuration_names))
        )
        if configuration_names:
            observed["configuration"] = [
                {"name": name} for name in configuration_names
            ]
        # 파일 경로가 환경 변수 값으로 전달되는 경우도 일반적인 실행 방식이다. 소스가
        # 그 환경 변수를 실제로 읽고, 설계 값이 mount 아래를 가리킬 때에만 사용 근거로
        # 인정한다. 계획 값을 관찰값처럼 그대로 복사하지 않는다.
        consumed_values = [
            str(item["value"])
            for item in workload.get("configuration") or []
            if isinstance(item, dict)
            and item.get("name")
            and item.get("value") is not None
            and _uses_environment(source, str(item["name"]))
        ]
        mounts = [
            {
                "storageId": str(item.get("id") or ""),
                "mountPath": str(item.get("mountPath") or ""),
            }
            for item in workload.get("storage") or []
            if isinstance(item, dict)
            and isinstance(item.get("mountPath"), str)
            and (
                _uses_path(source, str(item["mountPath"]))
                or any(
                    _uses_path(value, str(item["mountPath"]))
                    for value in consumed_values
                )
            )
        ]
        if mounts:
            observed["mounts"] = mounts
        observations.append(observed)
    return {"workloads": observations}


def health_path_from_observations(value: dict[str, Any]) -> str | None:
    """저장된 관찰 보고서에서 단일 health path만 안전하게 읽는다."""

    paths = {
        str(interface.get("healthPath"))
        for workload in value.get("workloads") or []
        if isinstance(workload, dict)
        for interface in workload.get("interfaces") or []
        if isinstance(interface, dict) and interface.get("healthPath")
    }
    return next(iter(paths)) if len(paths) == 1 else None


__all__ = [
    "RUNTIME_OBSERVATIONS_REPORT",
    "health_path_from_observations",
    "observe_runtime_contract",
]
