import json
import subprocess
from typing import Any

from app.testing.runtime.container_runner import run_toolchain_command


class TrivyIssues(list[str]):
    """기존 문자열 목록 계약을 유지하면서 실제 실행 근거도 함께 보존한다.

    Testing의 기존 호출자는 이 값을 평범한 ``list[str]``처럼 사용한다. 상세 수리 흐름은
    ``evidence``를 읽어 어떤 Trivy 명령이 어느 파일에서 어떤 규칙을 발견했는지 전달한다.
    """

    def __init__(self, values: list[str], *, evidence: dict[str, Any]) -> None:
        super().__init__(values)
        self.evidence = evidence


def run_trivy_scan(target_dir: str) -> list[str]:
    """Trivy 구성 검사를 실행하고 발견한 문제를 읽기 쉬운 문자열로 반환한다.

    host에 설치된 다른 버전을 사용하지 않고 구현·Testing과 공유하는
    ``easydep-toolchain``의 Trivy를 사용한다. 이미 그 컨테이너 안이면 바로 실행한다.
    """
    command = [
        "trivy",
        "config",
        ".",
        "--format",
        "json",
        "--severity",
        "HIGH,CRITICAL",
        # Image에 들어 있는 같은 규칙을 사용한다. 검사마다 외부 OCI
        # repository를 조회하지 않아 빠르고, 네트워크 유무에 따라 결과가 바뀌지 않는다.
        "--skip-check-update",
        "--disable-telemetry",
        "--skip-version-check",
        "--quiet",
    ]
    toolchain = "easydep-toolchain"
    try:
        execution = run_toolchain_command(
            command,
            cwd=target_dir,
            timeout=300,
        )
        result = execution.completed
        toolchain = execution.toolchain
        if not result.stdout.strip():
            message = f"Trivy returned no result: {result.stderr[-2000:]}"
            environment_error = execution.environment_error or result.returncode == 0
            return TrivyIssues(
                [message],
                evidence={
                    "name": "trivy config",
                    "tool": "trivy",
                    "toolchain": toolchain,
                    "status": "INCONCLUSIVE" if environment_error else "FAIL",
                    "command": command,
                    "exitCode": result.returncode,
                    "stderr": result.stderr[-4000:],
                    "targets": [],
                    "environmentError": environment_error,
                },
            )

        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            message = f"The Trivy JSON result could not be parsed: {result.stdout[:500]}"
            environment_error = execution.environment_error or result.returncode == 0
            return TrivyIssues(
                [message],
                evidence={
                    "name": "trivy config",
                    "tool": "trivy",
                    "toolchain": toolchain,
                    "status": "INCONCLUSIVE" if environment_error else "FAIL",
                    "command": command,
                    "exitCode": result.returncode,
                    "stderr": result.stderr[-4000:],
                    "targets": [],
                    "environmentError": environment_error,
                },
            )

        issues: list[str] = []
        findings: list[dict[str, Any]] = []
        targets: set[str] = set()
        results = parsed.get("Results", [])
        for result_item in results:
            target = result_item.get("Target", "Unknown File")
            if isinstance(target, str) and target and target != "Unknown File":
                targets.add(target.replace("\\", "/").lstrip("/"))
            misconfigs = result_item.get("Misconfigurations", [])
            for misconf in misconfigs:
                rule_id = str(misconf.get("ID") or "UNKNOWN_RULE")
                issue = (
                    f"[{target}] {rule_id}: {misconf.get('Title', 'Unknown Issue')} "
                    f"({misconf.get('Severity', 'UNKNOWN')}): "
                    f"{misconf.get('Message', '')}"
                )
                issues.append(issue)
                cause = misconf.get("CauseMetadata")
                cause = cause if isinstance(cause, dict) else {}
                findings.append(
                    {
                        "ruleId": rule_id,
                        "target": str(target).replace("\\", "/").lstrip("/"),
                        "resource": str(cause.get("Resource") or ""),
                        "startLine": cause.get("StartLine"),
                        "endLine": cause.get("EndLine"),
                        "finding": issue,
                    }
                )
        return TrivyIssues(
            issues,
            evidence={
                "name": "trivy config",
                "tool": "trivy",
                "toolchain": toolchain,
                "status": "FAIL" if issues else "PASS",
                "command": command,
                "exitCode": result.returncode,
                "stderr": result.stderr[-4000:],
                "targets": sorted(targets),
                "findings": findings,
                "environmentError": False,
            },
        )

    except (OSError, subprocess.SubprocessError) as error:
        message = f"Trivy execution failed: {error}"
        return TrivyIssues(
            [message],
            evidence={
                "name": "trivy config",
                "tool": "trivy",
                "toolchain": toolchain,
                "status": "INCONCLUSIVE",
                "command": command,
                "exitCode": None,
                "stderr": str(error),
                "targets": [],
                "environmentError": True,
            },
        )
