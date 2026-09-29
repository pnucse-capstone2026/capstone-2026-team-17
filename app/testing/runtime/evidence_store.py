"""Persist complete Testing runtime logs without embedding them in LLM prompts."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_LOG_REF = re.compile(r"^\.easydep/testing-evidence/[0-9a-f]{64}\.log$")


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def store_application_log(
    app_id: str,
    run_id: str,
    content: str,
    *,
    repository_root: Path | None = None,
) -> dict[str, object]:
    """Store one immutable full log and return a small, portable reference."""

    encoded = content.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    identity = hashlib.sha256(
        f"{app_id}\0{run_id}\0{digest}".encode()
    ).hexdigest()
    relative = Path(".easydep") / "testing-evidence" / f"{identity}.log"
    target = (repository_root or _repository_root()) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        target.write_bytes(encoded)
    return {
        "ref": relative.as_posix(),
        "sha256": digest,
        "bytes": len(encoded),
        "lineCount": len(content.splitlines()),
    }


def load_application_log(
    reference: str,
    *,
    repository_root: Path | None = None,
) -> str:
    """Read only a reference created by :func:`store_application_log`."""

    normalized = str(reference).replace("\\", "/")
    if _LOG_REF.fullmatch(normalized) is None:
        raise ValueError("Invalid Testing runtime log reference.")
    root = (repository_root or _repository_root()).resolve()
    target = (root / Path(normalized)).resolve()
    if root not in target.parents:
        raise ValueError("Testing runtime log reference escapes the repository.")
    return target.read_text(encoding="utf-8")


__all__ = ["load_application_log", "store_application_log"]
