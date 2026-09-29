"""검증된 고정 Provider 버전만 허용하는 전용 OpenTofu 캐시 정책."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

PINNED_PROVIDERS = {
    "aws": {"source": "hashicorp/aws", "version": "5.100.0"},
    "azure": {"source": "hashicorp/azurerm", "version": "5.0.1"},
    "gcp": {"source": "hashicorp/google", "version": "5.45.2"},
}
PLUGIN_CACHE = (
    Path(tempfile.gettempdir()) / "easydep" / "provider-plugin-cache"
)


def audit_provider_cache(path: Path = PLUGIN_CACHE) -> dict[str, Any]:
    allowed = {
        (contract["source"].split("/", 1)[1], contract["version"])
        for contract in PINNED_PROVIDERS.values()
    }
    packages: list[dict[str, str]] = []
    registry = path / "registry.opentofu.org" / "hashicorp"
    if registry.is_dir():
        for provider_dir in sorted(item for item in registry.iterdir() if item.is_dir()):
            for version_dir in sorted(item for item in provider_dir.iterdir() if item.is_dir()):
                packages.append({"provider": provider_dir.name, "version": version_dir.name})
    unexpected = [
        item for item in packages if (item["provider"], item["version"]) not in allowed
    ]
    return {
        "status": "passed" if not unexpected else "failed",
        "packages": packages,
        "unexpected": unexpected,
    }


def provider_cache_environment(path: Path = PLUGIN_CACHE) -> dict[str, str]:
    path.mkdir(parents=True, exist_ok=True)
    audit = audit_provider_cache(path)
    if audit["status"] != "passed":
        raise RuntimeError(f"provider cache contains unapproved packages: {audit['unexpected']}")
    environment = os.environ.copy()
    environment["TF_PLUGIN_CACHE_DIR"] = str(path.resolve())
    return environment


def provider_mirror_configuration(path: Path = PLUGIN_CACHE) -> str:
    """직접 다운로드를 허용하지 않는 로컬 filesystem mirror 설정."""
    mirror = path.resolve().as_posix()
    return (
        "provider_installation {\n"
        "  filesystem_mirror {\n"
        f'    path = "{mirror}"\n'
        "  }\n"
        "}\n"
        "disable_checkpoint = true\n"
    )
