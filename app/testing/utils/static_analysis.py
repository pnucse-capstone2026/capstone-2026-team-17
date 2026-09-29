"""복원된 애플리케이션 폴더를 Trivy로 검사한다."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.testing.utils.docker_trivy import run_trivy_scan


def _scan(directory: Path, subject: str) -> dict[str, Any]:
    try:
        issues = run_trivy_scan(str(directory.resolve()))
    except Exception as error:
        message = f"Trivy could not start: {error}"
        return {
            "status": "UNAVAILABLE",
            "gateStatus": "INCONCLUSIVE",
            "issues": [message],
            "source": {"source": "application", "directory": str(directory)},
            "message": message,
        }
    if issues is None:
        message = "Trivy returned no result."
        return {
            "status": "UNAVAILABLE",
            "gateStatus": "INCONCLUSIVE",
            "issues": [message],
            "source": {"source": "application", "directory": str(directory)},
            "message": message,
        }
    execution = getattr(issues, "evidence", {})
    execution = execution if isinstance(execution, dict) else {}
    # The legacy helper returns strings. A tool-startup failure is not an application
    # misconfiguration and must not become a FAIL or a pass.
    unavailable = bool(execution.get("environmentError")) or any(
        any(token in str(issue).lower() for token in ("could not", "not found", "no such file", "timed out"))
        for issue in issues
    )
    targets = [
        str(target).replace("\\", "/").lstrip("/")
        for target in execution.get("targets") or []
        if isinstance(target, str) and target.strip()
    ]
    return {
        "status": "UNAVAILABLE" if unavailable else "FAILED" if issues else "PASSED",
        "gateStatus": "INCONCLUSIVE" if unavailable else "FAIL" if issues else "PASS",
        "issues": issues,
        "commands": [execution] if execution else [],
        "tool": str(execution.get("tool") or "trivy"),
        "targets": sorted(set(targets)),
        # rule ID만이 아니라 Trivy가 가리킨 Terraform resource와 line을 보존한다.
        # topology 예외는 이 값이 ResourcePlan의 한 리소스와 정확히 맞을 때만 적용한다.
        "findings": [
            dict(item)
            for item in execution.get("findings") or []
            if isinstance(item, dict)
        ],
        "source": {"source": "application", "directory": str(directory)},
        "message": (
            f"Trivy could not complete the {subject} scan."
            if unavailable
            else f"Found {len(issues)} {subject} misconfigurations via Trivy."
        ),
    }


def scan_stage(
    *,
    node: str,
    directory: str,
    subject: str,
    report_key: str,
) -> dict[str, Any]:
    """복원된 폴더가 있으면 검사하고, 없으면 미실행 사실을 보고한다."""
    target = Path(directory) if directory else None
    if target is None or not target.exists():
        message = f"The {subject} directory is unavailable for scanning: {directory or '(empty)'}"
        return {
            "current_node": node,
            "errors": [message],
            report_key: {
                "status": "UNAVAILABLE",
                "gateStatus": "INCONCLUSIVE",
                "issues": [],
                "source": {"source": "none", "directory": directory},
                "message": message,
            },
        }

    report = _scan(target, subject)
    return {
        "current_node": node,
        "errors": report["issues"],
        report_key: report,
    }
