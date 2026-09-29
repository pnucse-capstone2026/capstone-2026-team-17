"""고정 Linux 환경에서 멤버 구현 작업과 최종 테스트를 실행한다."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from app.implementation.runtime.linux_runner_transport import (
    LLM_CREDENTIAL_ENVIRONMENT,
    OWNER_NPM_CACHE,
    OWNER_TERMINAL_HOME,
    OWNER_TERMINAL_SHELL,
    OWNER_TERMINAL_SHELL_ENV,
    OWNER_TERMINAL_USER,
    OWNER_TERMINAL_USER_ENV,
    OWNER_WORKSPACE_ALIAS,
)
from app.implementation.runtime.runner_compat import gradle_command, install

RUNNER_WORKSPACE = Path("/easydep-workspace")
HOST_BOOTSTRAP_GRADLE_CACHE = RUNNER_WORKSPACE / ".easydep/gradle-cache"
# 임시 파일이 아니라 이름 있는 Docker volume이 이 고정 경로에 mount된다.
RUNNER_GRADLE_CACHE = Path("/tmp/easydep-gradle-cache")  # noqa: S108
GRADLE_CACHE_MARKER = RUNNER_GRADLE_CACHE / ".easydep-bootstrap-v1"


def _prepare_owner_terminal_identity() -> None:
    """Create the fixed shell that drops autonomous commands to ``appuser``."""

    if os.environ.get("EASYDEP_FIXED_LINUX_RUNNER") != "1":
        return
    if os.name != "posix" or os.geteuid() != 0:
        raise RuntimeError("The fixed owner runner must initialize as Linux root.")

    import pwd

    account = pwd.getpwnam(OWNER_TERMINAL_USER)
    if account.pw_uid == 0:
        raise RuntimeError("The autonomous terminal user must not be root.")
    setpriv = shutil.which("setpriv")
    bash = shutil.which("bash")
    if not setpriv or not bash:
        raise RuntimeError("The fixed owner runner requires setpriv and bash.")

    for directory in (Path(OWNER_TERMINAL_HOME), Path(OWNER_NPM_CACHE)):
        directory.mkdir(parents=True, exist_ok=True)
        os.chown(directory, account.pw_uid, account.pw_gid)
        directory.chmod(0o700)

    shell = Path(OWNER_TERMINAL_SHELL)
    shell.write_text(
        "#!/bin/sh\n"
        'export SPRING_PROFILES_ACTIVE="${SPRING_PROFILES_ACTIVE:-test}"\n'
        f'exec "{setpriv}" --reuid={account.pw_uid} --regid={account.pw_gid} '
        f'--init-groups --no-new-privs "{bash}" -o pipefail "$@"\n',
        encoding="utf-8",
    )
    os.chown(shell, 0, 0)
    shell.chmod(0o755)
    os.environ[OWNER_TERMINAL_USER_ENV] = OWNER_TERMINAL_USER
    os.environ[OWNER_TERMINAL_SHELL_ENV] = str(shell)


def _clear_llm_credentials_from_environment() -> None:
    """Remove connection credentials after application settings are loaded."""

    for name in LLM_CREDENTIAL_ENVIRONMENT:
        os.environ.pop(name, None)


def _seed_gradle_cache() -> None:
    """호스트에서 이미 받은 Gradle 파일을 비어 있는 Linux cache에 한 번 복사한다.

    개발 환경 준비 스크립트가 만든 cache는 저장소 bind mount 안에서 읽을 수 있다.
    Linux named volume이 처음 만들어진 경우에만 이를 복사하고, 이후 Job은 volume을
    그대로 재사용한다. 호스트 cache가 없으면 wrapper가 직접 내려받아 같은 volume에
    저장하므로 이 준비 단계가 구현 실행의 필수 조건은 아니다.
    """

    if GRADLE_CACHE_MARKER.is_file() or not HOST_BOOTSTRAP_GRADLE_CACHE.is_dir():
        return
    RUNNER_GRADLE_CACHE.mkdir(parents=True, exist_ok=True)
    # 여러 Job이 동시에 시작해도 같은 volume을 함께 복사하지 않도록 Linux 파일 잠금을
    # 사용한다. 이 모듈의 실제 진입점은 고정 Linux runner뿐이므로 여기서만 import한다.
    import fcntl

    lock_path = RUNNER_GRADLE_CACHE / ".easydep-bootstrap.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if GRADLE_CACHE_MARKER.is_file():
            return
        shutil.copytree(HOST_BOOTSTRAP_GRADLE_CACHE, RUNNER_GRADLE_CACHE, dirs_exist_ok=True)
        GRADLE_CACHE_MARKER.write_text("ready\n", encoding="utf-8")


def _configure_runner_tools() -> None:
    if os.environ.get("EASYDEP_FIXED_LINUX_RUNNER") == "1":
        _seed_gradle_cache()
        _prepare_owner_terminal_identity()
    install()
    # ``install`` imports the implementation runtime and therefore constructs
    # app.config.settings before the process environment is scrubbed.  Later
    # LLM calls use that in-memory Settings value, while TerminalTool receives
    # an environment without the credential.
    _clear_llm_credentials_from_environment()


def _runner_job(job_path: Path) -> Path:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    job["workspaceRoot"] = str(RUNNER_WORKSPACE)
    target = job_path.with_name("runner-job.json")
    target.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def _worker(arguments: list[str]) -> int:
    _configure_runner_tools()
    from app.implementation.runtime.scaffold_worker import main as worker_main

    if not arguments:
        raise SystemExit("worker requires a job path")
    return worker_main([str(_runner_job(Path(arguments[0]))), *arguments[1:]])


def _cli(arguments: list[str]) -> int:
    """호스트가 계획한 workflow phase를 현재 Linux 환경에서 그대로 실행한다."""
    _configure_runner_tools()
    from app.implementation.interfaces.cli import main as cli_main

    if not arguments:
        raise SystemExit("cli requires an implementation command")
    runner_arguments = list(arguments)
    if runner_arguments[0] in {"plan-workflow", "run-workflow", "run-owner"}:
        if len(runner_arguments) < 3:
            raise SystemExit(f"{runner_arguments[0]} requires run and job paths")
        # job.json의 workspaceRoot는 호스트 절대 경로다. Linux에서 그 문자열을 그대로
        # Path로 해석하면 존재하지 않는 디렉터리가 되므로, 같은 입력을 가리키는 runner
        # 전용 사본만 만들고 현재 bind mount 경로를 기준으로 읽는다.
        runner_arguments[2] = str(_runner_job(Path(runner_arguments[2])))
    return cli_main(runner_arguments)


def _preflight(arguments: list[str]) -> int:
    if arguments:
        raise SystemExit("preflight accepts no arguments")
    _configure_runner_tools()
    commands = {
        "python": ["python", "--version"],
        "java": ["java", "-version"],
        "node": ["node", "--version"],
        "npm": ["npm", "--version"],
        "ripgrep": ["rg", "--version"],
        "gradle": [
            *gradle_command(),
            "--version",
            "--no-daemon",
        ],
        "opentofu": ["tofu", "version"],
        "trivy": ["trivy", "--version"],
    }
    observed: dict[str, dict[str, object]] = {}
    for name, command in commands.items():
        process_result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        observed[name] = {
            "passed": process_result.returncode == 0,
            "version": (process_result.stdout or process_result.stderr).strip()[:1000],
        }
    jars = {
        "openapiGenerator7.24.0": Path("/opt/easydep/openapi-generator-7.24.0.jar").is_file(),
    }
    owner_isolation = _probe_owner_isolation()
    preflight_result = {
        "schemaVersion": "easydep-member-runner-preflight/v1",
        "workspaceBindPassed": (RUNNER_WORKSPACE / "app" / "__init__.py").is_file(),
        "ownerIsolation": owner_isolation,
        "ownerWorkspaceAlias": OWNER_WORKSPACE_ALIAS,
        "ownerWorkspacePreflight": owner_isolation.get("workspacePreflight"),
        "tools": observed,
        "artifacts": jars,
    }
    print(json.dumps(preflight_result, ensure_ascii=False))
    return (
        0
        if preflight_result["workspaceBindPassed"]
        and owner_isolation["passed"]
        and all(item["passed"] for item in observed.values())
        and all(jars.values())
        else 1
    )


def _probe_owner_isolation() -> dict[str, object]:
    """Exercise the same OS permission boundary used by live owner terminals."""

    from app.implementation.agents.workspace import (
        preflight_owner_workspace,
        prepare_agent_workspace,
        prepare_owner_workspace_alias,
        release_owner_workspace_alias,
    )
    from app.implementation.runtime.linux_runner_transport import OWNER_CONTROL_ROOT_ENV

    previous_control = os.environ.get(OWNER_CONTROL_ROOT_ENV)
    try:
        with tempfile.TemporaryDirectory(prefix="easydep-owner-isolation-") as temporary:
            control = Path(temporary)
            run_root = control / "generated-runs" / "run_probe"
            source_root = (
                run_root
                / "application"
                / "src"
                / "main"
                / "java"
                / "com"
                / "example"
            )
            source_root.mkdir(parents=True)
            allowed = source_root / "Service.java"
            immutable = source_root / "api" / "Contract.java"
            immutable.parent.mkdir(parents=True)
            allowed.write_text("before\n", encoding="utf-8")
            immutable.write_text("contract\n", encoding="utf-8")
            control_file = control / "job.json"
            control_file.write_text("{}\n", encoding="utf-8")
            os.environ[OWNER_CONTROL_ROOT_ENV] = str(control)
            sandbox = prepare_agent_workspace(
                run_root,
                {
                    "task_id": "implement-backend-application",
                    "task_type": "backend-implementation",
                    "allowed_write_paths": [
                        "application/src/main/java/com/example/Service.java"
                    ],
                    "allowed_write_roots": [
                        "application/src/main/java/com/example"
                    ],
                    "immutable_paths": [
                        "application/src/main/java/com/example/api"
                    ],
                },
                persistent=True,
            )
            child_environment = os.environ.copy()
            child_environment.update(
                {
                    "EASYDEP_PROBE_CONTROL": str(control_file),
                    "EASYDEP_PROBE_ALLOWED": str(sandbox / allowed.relative_to(run_root)),
                    "EASYDEP_PROBE_IMMUTABLE": str(
                        sandbox / immutable.relative_to(run_root)
                    ),
                }
            )
            result = subprocess.run(
                [
                    OWNER_TERMINAL_SHELL,
                    "-c",
                    'test ! -r "$EASYDEP_PROBE_CONTROL" '
                    '&& printf "after\\n" > "$EASYDEP_PROBE_ALLOWED" '
                    '&& printf "changed\\n" > "$EASYDEP_PROBE_IMMUTABLE"',
                ],
                cwd=sandbox,
                env=child_environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            passed = (
                result.returncode == 0
                and control_file.read_text(encoding="utf-8") == "{}\n"
                and (sandbox / allowed.relative_to(run_root)).read_text(encoding="utf-8")
                == "after\n"
                and (sandbox / immutable.relative_to(run_root)).read_text(encoding="utf-8")
                == "changed\n"
            )
            logical_workspace = prepare_owner_workspace_alias(sandbox, "preflight")
            workspace_preflight = preflight_owner_workspace(
                sandbox,
                editable_files=[str(sandbox / allowed.relative_to(run_root))],
                editable_roots=[str(sandbox / source_root.relative_to(run_root))],
                immutable_paths=[str(sandbox / immutable.parent.relative_to(run_root))],
                logical_workspace=logical_workspace,
                enforce_write_scope=True,
            )
            release_owner_workspace_alias(sandbox, logical_workspace)
            return {
                "passed": passed,
                "workspacePreflight": workspace_preflight,
                "detail": (
                    "The owner can write its disposable candidate but cannot read job "
                    "control state; immutable changes are rejected only at promotion."
                    if passed
                    else (result.stderr.strip() or result.stdout.strip() or "Isolation probe failed.")
                ),
            }
    except Exception as error:
        return {"passed": False, "detail": str(error)}
    finally:
        if previous_control is None:
            os.environ.pop(OWNER_CONTROL_ROOT_ENV, None)
        else:
            os.environ[OWNER_CONTROL_ROOT_ENV] = previous_control


def main(argv: list[str] | None = None) -> int:
    arguments = argv or sys.argv[1:]
    if not arguments:
        raise SystemExit("usage: member_linux_runner {worker|cli|preflight} ...")
    if arguments[0] == "worker":
        return _worker(arguments[1:])
    if arguments[0] == "cli":
        return _cli(arguments[1:])
    if arguments[0] == "preflight":
        return _preflight(arguments[1:])
    raise SystemExit(f"unsupported runner operation: {arguments[0]}")


if __name__ == "__main__":
    raise SystemExit(main())
