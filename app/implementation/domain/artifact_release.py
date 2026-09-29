"""파일 산출물 버전 집합을 변경 불가능한 release 식별자로 묶는다."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping


def artifact_release_id(version_ids: Mapping[str, int]) -> str:
    """Return a stable ID for one exact artifact-version combination."""

    normalized = {
        str(artifact_type): int(version_id)
        for artifact_type, version_id in version_ids.items()
        if str(artifact_type) and int(version_id) > 0
    }
    if not normalized:
        raise ValueError("An artifact release requires at least one version")
    source = "\n".join(
        f"{artifact_type}:{normalized[artifact_type]}"
        for artifact_type in sorted(normalized)
    )
    return "easydep-release-" + hashlib.sha256(source.encode("utf-8")).hexdigest()


__all__ = ["artifact_release_id"]
