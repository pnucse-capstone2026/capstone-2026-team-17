from __future__ import annotations

import sys
import os
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

NPM_REGISTRY_ENV = "EASYDEP_NPM_REGISTRY"
DEFAULT_NPM_REGISTRY = "https://registry.npmmirror.com"


def npm_command_environment(
    environment: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return npm env with the configured registry and lockfile host rewrite."""
    result = dict(os.environ if environment is None else environment)
    registry = (
        result.get("npm_config_registry")
        or result.get("NPM_CONFIG_REGISTRY")
        or result.get(NPM_REGISTRY_ENV)
        or settings.easydep_npm_registry
        or DEFAULT_NPM_REGISTRY
    ).strip()
    if registry:
        result["npm_config_registry"] = registry
    result["npm_config_replace_registry_host"] = "always"
    return result


@dataclass(frozen=True)
class ImplementationSettings:
    repository_root: Path
    work_root: Path
    python_executable: Path
    max_workers: int
    command_timeout_seconds: int
    startup_warmup: bool = False

    @classmethod
    def from_env(cls) -> ImplementationSettings:
        repository_root = Path(__file__).resolve().parents[2]
        python = Path(sys.executable).resolve()
        work_root = (repository_root / ".easydep" / "implementation-runs").resolve()
        return cls(
            repository_root=repository_root,
            work_root=work_root,
            python_executable=python,
            max_workers=max(1, settings.implementation_max_workers),
            command_timeout_seconds=max(
                60, settings.implementation_command_timeout_seconds
            ),
            startup_warmup=settings.implementation_startup_warmup,
        )


# System & Infrastructure Defaults
DEFAULT_CONTAINER_PORT: int = settings.implementation_default_container_port
DEFAULT_DOCKER_GRADLE_IMAGE: str = settings.implementation_docker_gradle_image
DEFAULT_DOCKER_JRE_IMAGE: str = settings.implementation_docker_jre_image
DEFAULT_AWS_LOG_RETENTION_DAYS: int = settings.implementation_aws_log_retention_days
DEFAULT_AZURE_MYSQL_BACKUP_RETENTION_DAYS: int = settings.implementation_azure_mysql_retention_days

