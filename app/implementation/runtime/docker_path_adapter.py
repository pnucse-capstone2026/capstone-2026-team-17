"""Windows Docker Desktop 호출의 컨테이너 경로를 정규화한다."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.config import settings

CONTAINER_WORKSPACE = "/easydep-workspace"


def translate_docker_command(
    command: Sequence[str],
    workspace: str,
    *,
    host_workspace: str | None = None,
    container_workspace: str = CONTAINER_WORKSPACE,
) -> list[str]:
    translated = [str(part) for part in command]
    if not translated or Path(translated[0]).name.lower() not in {"docker", "docker.exe"}:
        return translated

    root = (
        workspace
        if workspace.startswith("/")
        else str(Path(workspace).resolve())
    ).rstrip("\\/")
    host_root = (host_workspace or root).rstrip("\\/")

    def relative_to_root(value: str) -> str | None:
        if value.lower() == root.lower():
            return ""
        for separator in ("\\", "/"):
            prefix = root + separator
            if value.lower().startswith(prefix.lower()):
                return value[len(prefix) :].replace("\\", "/")
        return None

    def container_path(value: str) -> str:
        relative = relative_to_root(value)
        if relative is not None:
            return container_workspace if not relative else f"{container_workspace}/{relative}"
        return value

    def host_path(value: str) -> str:
        relative = relative_to_root(value)
        if relative is None:
            return value
        if not relative:
            return host_root
        separator = "\\" if ":" in host_root[:3] else "/"
        return host_root + separator + relative.replace("/", separator)

    for index in range(1, len(translated)):
        value = translated[index]
        if translated[index - 1] in {"-v", "--volume"}:
            separator = value.find(":", len(root))
            source = value[:separator] if separator >= 0 else value
            target = value[separator + 1 :] if separator >= 0 else ""
            if relative_to_root(source) is not None and target:
                translated[index] = f"{host_path(source)}:{container_path(target)}"
        else:
            translated[index] = container_path(value)
    return translated


def install() -> None:
    workspace = settings.easydep_docker_command_workspace
    host_workspace = settings.easydep_docker_host_workspace
    if not workspace:
        workspace = settings.easydep_docker_windows_workspace
        host_workspace = host_workspace or workspace
    if not workspace or getattr(subprocess.run, "_easydep_docker_adapter", False):
        return
    original_run = subprocess.run

    def adapted_run(command: Any, *args: Any, **kwargs: Any):
        if isinstance(command, (list, tuple)):
            command = translate_docker_command(
                command,
                workspace,
                host_workspace=host_workspace,
            )
        return original_run(command, *args, **kwargs)

    adapted_run._easydep_docker_adapter = True  # type: ignore[attr-defined]
    subprocess.run = adapted_run  # type: ignore[assignment]
