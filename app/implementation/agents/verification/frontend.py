from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from app.implementation.config import npm_command_environment

MUTATING_HTTP_METHODS = {"post", "put", "patch", "delete"}
FRONTEND_BUILD_REPORT = Path("reports/frontend-build.json")

RESPONSIVE_TABLE_STYLES = """

/* EasyDep accessibility repair: keep data tables usable on narrow screens. */
@media (max-width: 40rem) {
  table {
    display: block;
    max-width: 100%;
    overflow-x: auto;
  }
}
"""


def has_mutating_operations(openapi: object) -> bool:
    if not isinstance(openapi, dict) or not isinstance(openapi.get("paths"), dict):
        return False
    return any(
        str(method).lower() in MUTATING_HTTP_METHODS
        for path_item in openapi["paths"].values()
        if isinstance(path_item, dict)
        for method in path_item
    )


def frontend_contract_violations(
    sandbox: Path,
    relative_paths: list[str],
    *,
    requires_success_feedback: bool = False,
) -> list[str]:
    sources: list[str] = []
    styles: list[str] = []
    violations: list[str] = []
    for relative in relative_paths:
        path = sandbox / relative
        if not path.is_file():
            continue
        if path.suffix == ".css":
            styles.append(path.read_text(encoding="utf-8"))
            continue
        if path.suffix not in {".ts", ".tsx"}:
            continue
        source = path.read_text(encoding="utf-8")
        sources.append(source)
        for number, line in enumerate(source.splitlines(), 1):
            # ``placeholder=`` is a valid JSX input attribute, not an
            # unfinished implementation marker.
            if re.search(
                r"(?<![\w-])(?:TODO|FIXME|PLACEHOLDER)\b(?!\s*=)",
                line,
                re.IGNORECASE,
            ):
                violations.append(
                    f"{relative}:{number}: unresolved implementation marker; "
                    "remove the marker and implement the described behavior"
                )
        if re.search(
            r"(?:(?:window|globalThis)\s*\.\s*)?\b(?:fetch|XMLHttpRequest)\s*\(",
            source,
        ) or re.search(r"\baxios\b", source):
            violations.append(
                f"{relative}: direct HTTP calls are forbidden; use src/generated"
            )
    combined = "\n".join(sources)
    if not re.search(r"from\s+['\"][^'\"]*generated", combined):
        violations.append(
            "Frontend implementation does not import the OpenAPI Generator client/models"
        )
    if requires_success_feedback and not re.search(
        r"(?:role\s*=\s*['\"]status['\"]|aria-live\s*=\s*['\"](?:polite|assertive)['\"])",
        combined,
    ):
        violations.append(
            "Mutating API operations require an accessible success status announcement"
        )
    declared_ids = {
        value
        for attribute in _jsx_attribute_values(combined, "id")
        for value in attribute.split()
    }
    for attribute in _jsx_attribute_values(combined, "aria-describedby"):
        for described_id in attribute.split():
            if described_id not in declared_ids:
                violations.append(
                    f"aria-describedby references missing element id: {described_id}"
                )
    combined_styles = "\n".join(styles)
    if "<table" in combined and not re.search(
        r"overflow-x\s*:\s*(?:auto|scroll)", combined_styles
    ):
        violations.append(
            "Data tables require responsive narrow-screen handling in styles.css"
        )
    return violations


def repair_responsive_table_styles(
    sandbox: Path, relative_paths: list[str]
) -> list[str]:
    """Add the narrow-screen table rule when the generated UI declares a table.

    The rule is a mechanical accessibility safeguard, not a domain decision. It
    is safe to apply before the frontend contract gate because it only touches the
    declared ``styles.css`` output and is idempotent.
    """
    paths = [sandbox / relative for relative in relative_paths]
    source_paths = [path for path in paths if path.suffix in {".ts", ".tsx"} and path.is_file()]
    if not source_paths:
        return []
    if not any("<table" in path.read_text(encoding="utf-8") for path in source_paths):
        return []
    style_path = next(
        (path for path in paths if path.name == "styles.css" and path.is_file()),
        None,
    )
    if style_path is None:
        return []
    styles = style_path.read_text(encoding="utf-8")
    if re.search(r"overflow-x\s*:\s*(?:auto|scroll)", styles):
        return []
    separator = "\n" if styles.endswith("\n") else "\n\n"
    style_path.write_text(styles + separator + RESPONSIVE_TABLE_STYLES.lstrip("\n"), encoding="utf-8")
    return [str(style_path.relative_to(sandbox)).replace("\\", "/")]


def repair_frontend_accessibility_contract(
    sandbox: Path, relative_paths: list[str]
) -> list[str]:
    """Remove stale comment markers and invalid static aria references."""
    changed: list[str] = []
    for relative in relative_paths:
        path = sandbox / relative
        if not path.is_file() or path.suffix not in {".ts", ".tsx"}:
            continue
        source = path.read_text(encoding="utf-8")
        repaired = re.sub(
            r"(?mi)^\s*(?://|/\*|\{\/\*)[^\n]*(?:TODO|FIXME|PLACEHOLDER)[^\n]*(?:\*/\}|\*/)?\s*\n?",
            "",
            source,
        )
        declared_ids = {
            value
            for attribute in _jsx_attribute_values(repaired, "id")
            for value in attribute.split()
        }

        def replace_reference(match: re.Match[str]) -> str:
            value = match.group(2)
            valid = [token for token in value.split() if token in declared_ids]
            return f"aria-describedby=\"{' '.join(valid)}\"" if valid else ""

        repaired = re.sub(
            r"aria-describedby\s*=\s*([\"'])(.*?)\1",
            replace_reference,
            repaired,
        )
        if repaired != source:
            path.write_text(repaired, encoding="utf-8")
            changed.append(str(path.relative_to(sandbox)).replace("\\", "/"))
    return changed


def _jsx_attribute_values(source: str, attribute: str) -> list[str]:
    pattern = re.compile(
        rf"\b{re.escape(attribute)}\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|\{{([^{{}}]*)\}})"
    )
    values: list[str] = []
    for match in pattern.finditer(source):
        static_value = match.group(1) if match.group(1) is not None else match.group(2)
        if static_value is not None:
            values.append(static_value)
            continue
        expression = match.group(3) or ""
        values.extend(
            value
            for _, value in re.findall(r"(['\"`])([^'\"`]*)\1", expression)
            if value
        )
    return values


def run_frontend_command(
    command: list[str],
    *,
    cwd: Path,
    capture_output: bool,
    text: bool,
    encoding: str,
    errors: str,
    timeout: int,
    check: bool,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run npm and terminate the complete command tree if it times out."""
    if os.name != "nt":
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=capture_output,
            text=text,
            encoding=encoding,
            errors=errors,
            timeout=timeout,
            check=check,
            env=env,
        )

    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        encoding=encoding,
        errors=errors,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        env=env,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        try:
            terminated = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
            if terminated.returncode != 0:
                process.kill()
        except (OSError, subprocess.SubprocessError):
            process.kill()
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=stdout or error.output,
            stderr=stderr or error.stderr,
        ) from error
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _timeout_output(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def _frontend_command_environment() -> dict[str, str]:
    """Reuse a system cache instead of re-downloading each clean sandbox."""
    environment = npm_command_environment(os.environ)
    # npm configuration keys are case-sensitive in a Linux process.  The
    # fixed runner injects the lowercase key for its named shared cache; do
    # not add an uppercase fallback alongside it because npm may prefer that
    # second value and bypass the volume.
    if not environment.get("npm_config_cache") and not environment.get(
        "NPM_CONFIG_CACHE"
    ):
        environment["NPM_CONFIG_CACHE"] = str(
            Path(tempfile.gettempdir()) / "easydep-npm-cache"
        )
    return environment


def _frontend_dependency_fingerprint(frontend: Path, executable: str) -> str | None:
    """Identify the manifests and local toolchain that produced node_modules."""
    package = frontend / "package.json"
    lock = frontend / "package-lock.json"
    if not package.is_file() or not lock.is_file():
        return None
    digest = hashlib.sha256()
    for path in (package, lock):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    environment = _frontend_command_environment()
    identity = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "node": shutil.which("node", path=environment.get("PATH")),
        "npm": shutil.which(executable, path=environment.get("PATH")) or executable,
        "path": environment.get("PATH", ""),
        "nodeOptions": environment.get("NODE_OPTIONS", ""),
        "npmPlatform": environment.get("npm_config_platform", ""),
        "npmArch": environment.get("npm_config_arch", ""),
    }
    digest.update(json.dumps(identity, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()


def _frontend_install_marker(frontend: Path) -> Path:
    return frontend / "node_modules" / ".easydep-install.json"


def _record_frontend_install(frontend: Path, executable: str, success: bool) -> None:
    marker = _frontend_install_marker(frontend)
    if not success:
        marker.unlink(missing_ok=True)
        return
    fingerprint = _frontend_dependency_fingerprint(frontend, executable)
    if fingerprint:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"fingerprint": fingerprint}), encoding="utf-8")


def _frontend_dependency_commands(frontend: Path, executable: str) -> list[list[str]]:
    """Reuse only dependencies installed successfully for these exact inputs."""

    marker = _frontend_install_marker(frontend)
    expected = _frontend_dependency_fingerprint(frontend, executable)
    if expected and (frontend / "node_modules" / ".package-lock.json").is_file():
        try:
            recorded = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            recorded = None
        if isinstance(recorded, dict) and recorded.get("fingerprint") == expected:
            return []
    marker.unlink(missing_ok=True)
    return [
        [
            executable,
            "ci",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--prefer-offline",
        ]
    ]


def frontend_source_digest(workspace_root: Path) -> str:
    """production bundle을 결정하는 frontend 입력만 안정적으로 식별한다."""
    frontend = workspace_root / "application" / "frontend"
    digest = hashlib.sha256()
    root_inputs = {
        "index.html",
        "package.json",
        "package-lock.json",
        "tsconfig.json",
        "vite.config.ts",
    }
    candidates = [
        path
        for path in frontend.rglob("*")
        if path.is_file()
        and not any(part in {"dist", "node_modules"} for part in path.relative_to(frontend).parts)
        and (
            (path.parent == frontend and path.name in root_inputs)
            or "src" in path.relative_to(frontend).parts
        )
    ]
    for path in sorted(candidates):
        relative = path.relative_to(frontend).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def store_frontend_build(
    run_root: Path,
    build_workspace: Path,
    evidence: dict[str, object],
) -> dict[str, object] | None:
    """검증된 dist와 source 지문을 run에 보존해 같은 build를 다시 하지 않는다."""
    if evidence.get("verificationKind") != "production-build":
        return None
    source_dist = build_workspace / "application" / "frontend" / "dist"
    if evidence.get("exitCode") != 0 or not (source_dist / "index.html").is_file():
        return None
    target_dist = run_root / "application" / "frontend" / "dist"
    target_dist.resolve().relative_to((run_root / "application" / "frontend").resolve())
    # Windows bind mount에서는 Linux runner가 새 디렉터리의 metadata를 만들지 못할 수
    # 있다. 실행 전 호스트가 준비한 폴더에 검증된 파일 내용만 복사한다. 해시가 붙은 예전
    # asset이 남더라도 index.html은 항상 현재 bundle만 참조하며 source digest가 재사용
    # 가능 여부를 결정한다.
    target_dist.mkdir(parents=True, exist_ok=True)
    for source in source_dist.rglob("*"):
        if not source.is_file():
            continue
        target = target_dist / source.relative_to(source_dist)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    report = {
        "schemaVersion": "easydep-frontend-build/v1alpha1",
        "sourceDigest": frontend_source_digest(run_root),
        "distPath": "application/frontend/dist",
        "verification": evidence,
    }
    report_path = run_root / FRONTEND_BUILD_REPORT
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def reuse_frontend_build(run_root: Path) -> dict[str, object] | None:
    """현재 source와 정확히 맞는 성공 build 증거가 있으면 반환한다."""
    report_path = run_root / FRONTEND_BUILD_REPORT
    dist = run_root / "application" / "frontend" / "dist" / "index.html"
    if not report_path.is_file() or not dist.is_file():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    evidence = report.get("verification")
    if (
        report.get("sourceDigest") != frontend_source_digest(run_root)
        or not isinstance(evidence, dict)
        or evidence.get("verificationKind") != "production-build"
        or evidence.get("exitCode") != 0
    ):
        return None
    return {**evidence, "reusedFromFrontendTask": True}


def run_frontend_verification(
    sandbox: Path,
    run_command: Callable[..., subprocess.CompletedProcess[str]],
    *,
    verification_kind: Literal["typecheck", "production-build"] = "production-build",
    timeout_seconds: int = 300,
) -> dict[str, object]:
    frontend = sandbox / "application" / "frontend"
    package = frontend / "package.json"
    lock = frontend / "package-lock.json"
    executable = "npm.cmd" if os.name == "nt" else "npm"
    final_command = (
        [executable, "exec", "--", "tsc", "-b"]
        if verification_kind == "typecheck"
        else [executable, "run", "build"]
    )
    if not package.is_file() or not lock.is_file():
        missing = "package.json" if not package.is_file() else "package-lock.json"
        return {
            "verificationKind": verification_kind,
            "command": final_command,
            "commands": [],
            "exitCode": 1,
            "durationMs": 0,
            "stdout": "",
            "stderr": f"Frontend {missing} was not found",
            "testResults": "",
        }
    main = frontend / "src" / "main.tsx"
    main_source = main.read_text(encoding="utf-8") if main.is_file() else ""
    has_hash_router = bool(
        re.search(
            r"import\s*\{[^}]*\bHashRouter\b[^}]*\}\s*from\s*['\"]react-router-dom['\"]",
            main_source,
        )
        and re.search(r"<HashRouter(?:\s|>)", main_source)
    )
    if not has_hash_router:
        return {
            "verificationKind": verification_kind,
            "command": final_command,
            "commands": [],
            "exitCode": 1,
            "durationMs": 0,
            "stdout": "",
            "stderr": "Frontend static deployment requires HashRouter in src/main.tsx",
            "testResults": "",
        }
    commands = _frontend_dependency_commands(frontend, executable)
    # 같은 task의 repair는 같은 sandbox를 쓴다. 성공한 ``npm ci``가 남긴 lock record가
    # 있으면 dependency를 다시 지우고 설치하지 않고 TypeScript build만 반복한다.
    commands.append(final_command)
    started = time.monotonic()
    outputs: list[str] = []
    errors: list[str] = []
    exit_code = 0
    executed_command = commands[0]
    environment = _frontend_command_environment()
    for command in commands:
        executed_command = command
        try:
            result = run_command(
                command,
                cwd=frontend,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as error:
            exit_code = 1
            stdout = _timeout_output(error.stdout or error.output)
            stderr = _timeout_output(error.stderr)
            errors.append(
                "Frontend command timed out after "
                f"{timeout_seconds} seconds: {' '.join(command)}"
            )
            if stdout:
                outputs.append(stdout[-12000:])
            if stderr:
                errors.append(stderr[-12000:])
            break
        except OSError as error:
            exit_code = 1
            errors.append(str(error))
            break
        outputs.append(result.stdout[-12000:])
        errors.append(result.stderr[-12000:])
        exit_code = result.returncode
        if command[1:2] == ["ci"]:
            _record_frontend_install(frontend, executable, exit_code == 0)
        if exit_code != 0:
            break
    return {
        "verificationKind": verification_kind,
        "command": executed_command,
        "commands": commands,
        "exitCode": exit_code,
        "durationMs": int((time.monotonic() - started) * 1000),
        "stdout": "\n".join(outputs)[-16000:],
        "stderr": "\n".join(errors)[-16000:],
        "testResults": "",
    }


def _frontend_unit_test_path(allowed_write_paths: list[str]) -> str:
    """Return the single test target a focused frontend owner may create."""

    candidates = [
        path.replace("\\", "/")
        for path in allowed_write_paths
        if isinstance(path, str)
        and path.replace("\\", "/").startswith("application/frontend/")
        and re.search(r"\.(?:test|spec)\.[cm]?[jt]sx?$", path, re.IGNORECASE)
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            "Frontend unit-test task requires exactly one allowed .test/.spec TypeScript path"
        )
    return candidates[0]


def _vitest_execution_summary(report_path: Path) -> dict[str, int]:
    """Read only the execution counters needed to reject empty Vitest runs."""

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Vitest JSON report was not produced") from error
    if not isinstance(report, dict):
        raise RuntimeError("Vitest JSON report has an invalid root")

    def count(name: str) -> int:
        value = report.get(name, 0)
        return value if isinstance(value, int) and value >= 0 else 0

    total = count("numTotalTests")
    failed = count("numFailedTests")
    skipped = count("numPendingTests") + count("numTodoTests")
    # Vitest's older JSON reporter nests Jest-compatible assertion results.
    if total == 0 and isinstance(report.get("testResults"), list):
        assertions = [
            assertion
            for suite in report["testResults"]
            if isinstance(suite, dict)
            for assertion in suite.get("assertionResults", [])
            if isinstance(assertion, dict)
        ]
        total = len(assertions)
        failed = sum(
            1 for assertion in assertions if assertion.get("status") == "failed"
        )
        skipped = sum(
            1
            for assertion in assertions
            if assertion.get("status") in {"pending", "todo", "skipped"}
        )
    return {"total": total, "failed": failed, "skipped": skipped}


def run_frontend_unit_test_verification(
    sandbox: Path,
    allowed_write_paths: list[str],
    run_command: Callable[..., subprocess.CompletedProcess[str]],
    *,
    report_path: Path | None = None,
    timeout_seconds: int = 300,
) -> dict[str, object]:
    """Run exactly the focused Vitest file and require a non-empty result."""

    target = _frontend_unit_test_path(allowed_write_paths)
    frontend = sandbox / "application" / "frontend"
    target_from_frontend = Path(target).relative_to("application/frontend").as_posix()
    package = frontend / "package.json"
    lock = frontend / "package-lock.json"
    executable = "npm.cmd" if os.name == "nt" else "npm"
    if not package.is_file() or not lock.is_file():
        missing = "package.json" if not package.is_file() else "package-lock.json"
        return {
            "command": [executable, "run", "test:unit", "--", target_from_frontend],
            "commands": [],
            "exitCode": 1,
            "durationMs": 0,
            "stdout": "",
            "stderr": f"Frontend {missing} was not found",
            "testResults": "",
        }

    try:
        package_data = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        package_data = {}
    scripts = package_data.get("scripts") if isinstance(package_data, dict) else None
    if not isinstance(scripts, dict) or not isinstance(scripts.get("test:unit"), str):
        return {
            "command": [executable, "run", "test:unit", "--", target_from_frontend],
            "commands": [],
            "exitCode": 1,
            "durationMs": 0,
            "stdout": "",
            "stderr": "Frontend package.json does not provide the test:unit Vitest script",
            "testResults": "",
        }

    report = report_path or frontend / "reports" / "easydep-vitest-unit.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    commands = _frontend_dependency_commands(frontend, executable)
    commands.append(
        [
            executable,
            "run",
            "test:unit",
            "--",
            "--reporter=json",
            f"--outputFile={report.as_posix()}",
            target_from_frontend,
        ]
    )
    started = time.monotonic()
    outputs: list[str] = []
    errors: list[str] = []
    executed_command = commands[0]
    exit_code = 0
    environment = _frontend_command_environment()
    for command in commands:
        executed_command = command
        try:
            result = run_command(
                command,
                cwd=frontend,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as error:
            exit_code = 1
            outputs.append(_timeout_output(error.stdout or error.output)[-12000:])
            errors.append(
                f"Frontend unit-test command timed out after {timeout_seconds} seconds: {' '.join(command)}"
            )
            errors.append(_timeout_output(error.stderr)[-12000:])
            break
        except OSError as error:
            exit_code = 1
            errors.append(str(error))
            break
        outputs.append(result.stdout[-12000:])
        errors.append(result.stderr[-12000:])
        exit_code = result.returncode
        if command[1:2] == ["ci"]:
            _record_frontend_install(frontend, executable, exit_code == 0)
        if exit_code != 0:
            break

    summary: dict[str, int] | None = None
    # The report was unlinked before this command, so an existing JSON result
    # is fresh evidence even when Vitest returns nonzero for an assertion.
    if report.is_file():
        try:
            summary = _vitest_execution_summary(report)
            if exit_code == 0 and (
                summary["total"] - summary["skipped"] <= 0 or summary["failed"]
            ):
                exit_code = 1
                errors.append(
                    "Vitest focused result must contain an executed passing test: "
                    f"total={summary['total']}, failed={summary['failed']}, skipped={summary['skipped']}"
                )
        except RuntimeError as error:
            if exit_code == 0:
                exit_code = 1
                errors.append(str(error))
    return {
        "command": executed_command,
        "commands": commands,
        "exitCode": exit_code,
        "durationMs": int((time.monotonic() - started) * 1000),
        "stdout": "\n".join(outputs)[-16000:],
        "stderr": "\n".join(item for item in errors if item)[-16000:],
        "testResults": json.dumps(summary, ensure_ascii=False) if summary else "",
        "unitTestResults": summary or {},
    }
