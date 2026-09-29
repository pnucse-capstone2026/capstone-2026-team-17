"""호스트 오케스트레이터와 고정 Linux 멤버 runner 사이의 전송 경계."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from app.config import settings
from app.demo_validation import DEMO_SKIP_VALIDATION_ENV
from app.implementation.config import NPM_REGISTRY_ENV

CONTAINER_WORKSPACE = PurePosixPath("/easydep-workspace")
RUNNER_IMAGE_ENV = "EASYDEP_TOOLCHAIN_IMAGE"
RUNNER_GRADLE_CACHE_VOLUME = "easydep-member-gradle-cache"
RUNNER_NPM_CACHE_VOLUME = "easydep-member-npm-cache"
RUNNER_TOFU_CACHE_VOLUME = "easydep-tofu-provider-cache"
RUNNER_TOFU_CACHE_PATH = "/app/.cache/opentofu"
OWNER_WORKSPACE_VOLUME_PREFIX = "easydep-owner-ws-"
OWNER_TERMINAL_USER = "appuser"
OWNER_TERMINAL_SHELL = "/usr/local/bin/easydep-owner-shell"
OWNER_TERMINAL_HOME = "/var/lib/easydep-owner/home"
OWNER_NPM_CACHE = "/var/cache/easydep/npm"
OWNER_GRADLE_CACHE = "/tmp/easydep-gradle-cache"  # noqa: S108
OWNER_WORKSPACE_ALIAS = "/work"
OWNER_CONTROL_ROOT_ENV = "EASYDEP_OWNER_CONTROL_ROOT"
OWNER_TERMINAL_USER_ENV = "EASYDEP_OWNER_TERMINAL_USER"
OWNER_TERMINAL_SHELL_ENV = "EASYDEP_OWNER_TERMINAL_SHELL"
RUNTIME_ENVIRONMENT = (
    "LLM_TIMEOUT_SECONDS",
    "LLM_WALL_TIMEOUT_SECONDS",
    "OPENHANDS_MAX_OUTPUT_TOKENS",
    "OPENHANDS_REASONING_EFFORT",
    "IMPLEMENTATION_OWNER_TOOL_MODE",
    "IMPLEMENTATION_OPENHANDS_CANARY",
    "IMPLEMENTATION_OPENHANDS_CANARY_REPETITIONS",
    "IMPLEMENTATION_OPENHANDS_REQUEST_ATTEMPTS",
    "IMPLEMENTATION_OPENHANDS_RETRY_MIN_WAIT_SECONDS",
    "IMPLEMENTATION_OPENHANDS_RETRY_MAX_WAIT_SECONDS",
    "IMPLEMENTATION_OPENHANDS_RETRY_MULTIPLIER",
    "IMPLEMENTATION_OPENHANDS_CANARY_MAX_ATTEMPTS",
    "IMPLEMENTATION_OPENHANDS_CANARY_TRANSIENT_TTL_SECONDS",
    "IMPLEMENTATION_COMMAND_TIMEOUT_SECONDS",
    "IMPLEMENTATION_VERIFICATION_TIMEOUT_SECONDS",
    "IMPLEMENTATION_MAX_TASK_ATTEMPTS",
    "EASYDEP_MEMBER_CHECKPOINT_RUN",
    DEMO_SKIP_VALIDATION_ENV,
)
# ``llm_subprocess_environment`` publishes the selected provider credential
# under this canonical name.  Keep the list next to the Docker transport so
# both the runner entrypoint and autonomous tool adapter use the same boundary.
LLM_CREDENTIAL_ENVIRONMENT = ("API_KEY",)
RUNNER_OWNER_LABEL = "easydep.owner=member-runner"
RUNNER_JOB_LABEL = "easydep.job-id"
RUNNER_RUN_LABEL = "easydep.run-id"
_DOCKER_LABEL_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _docker_label_value(value: object) -> str | None:
    candidate = str(value or "")
    return candidate if _DOCKER_LABEL_VALUE.fullmatch(candidate) else None


def _docker_run(arguments: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["docker", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _owned_container_ids(*, job_id: str | None = None, run_id: str | None = None) -> list[str]:
    filters = ["--filter", f"label={RUNNER_OWNER_LABEL}"]
    for key, value in ((RUNNER_JOB_LABEL, job_id), (RUNNER_RUN_LABEL, run_id)):
        safe_value = _docker_label_value(value) if value is not None else None
        if value is not None and safe_value is None:
            return []
        if safe_value:
            filters.extend(["--filter", f"label={key}={safe_value}"])
    result = _docker_run(["ps", "-aq", *filters])
    if result is None or result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def cleanup_runner_containers(
    *,
    job_id: str | None = None,
    run_id: str | None = None,
    container_ids: Iterable[str] | None = None,
) -> None:
    """Stop and remove only owner-labelled member runner containers."""
    if container_ids is None and job_id is None and run_id is None:
        return
    ids = list(container_ids) if container_ids is not None else _owned_container_ids(
        job_id=job_id, run_id=run_id
    )
    for container_id in ids:
        if not _docker_label_value(container_id):
            continue
        _docker_run(["stop", "-t", "10", container_id])
        _docker_run(["rm", container_id])


def reconcile_orphaned_runner_containers(valid_job_ids: set[str]) -> None:
    """Remove owned containers not connected to a live leased job."""
    for container_id in _owned_container_ids():
        result = _docker_run(
            [
                "inspect",
                "--format",
                '{{index .Config.Labels "easydep.job-id"}}',
                container_id,
            ]
        )
        if result is None or result.returncode != 0:
            continue
        job_id = result.stdout.strip()
        if not job_id or job_id in valid_job_ids:
            continue
        cleanup_runner_containers(container_ids=[container_id])


def _job_root_for_arguments(
    arguments: list[str], repository_root: Path
) -> tuple[Path, PurePosixPath] | None:
    """Return the one implementation-job directory needed by this runner.

    The member runner used to bind the whole EasyDep checkout read-write.  An
    autonomous terminal would then be able to read ``.env`` and edit EasyDep
    itself.  Job files are self-contained: all design inputs, generated runs,
    reports and progress files live below the directory containing ``job.json``.
    Mounting only that directory preserves the existing container paths without
    exposing the rest of the checkout.
    """

    root = repository_root.resolve()
    implementation_runs = (root / ".easydep" / "implementation-runs").resolve()
    for value in arguments:
        candidate = Path(to_host_path(value, root)).resolve()
        if candidate.name != "job.json" or not candidate.is_file():
            continue
        try:
            candidate.relative_to(implementation_runs)
        except ValueError as error:
            raise ValueError(
                f"Implementation runner job is outside the work root: {candidate}"
            ) from error
        job_root = candidate.parent
        try:
            job = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Implementation runner job is unreadable: {candidate}") from error
        raw_inputs = job.get("inputs")
        if raw_inputs is not None and not isinstance(raw_inputs, dict):
            raise ValueError(f"Implementation runner job inputs are invalid: {candidate}")
        referenced = [
            *(
                str(path)
                for path in (raw_inputs or {}).values()
                if isinstance(path, str)
            ),
            *(
                str(job[name])
                for name in ("outputRoot", "progressPath")
                if isinstance(job.get(name), str)
            ),
        ]
        for relative in referenced:
            referenced_path = (root / relative).resolve()
            if referenced_path != job_root and job_root not in referenced_path.parents:
                raise ValueError(
                    "Implementation runner input is outside its job directory: "
                    f"{relative}"
                )
        return job_root, to_container_path(job_root, root)
    return None


def configured_runner_image(environment: dict[str, str] | None = None) -> str | None:
    source = os.environ if environment is None else environment
    value = source.get(RUNNER_IMAGE_ENV, "").strip()
    if not value and environment is None:
        value = (settings.easydep_toolchain_image or "").strip()
    return value or None


def to_container_path(path: Path, repository_root: Path) -> PurePosixPath:
    relative = path.resolve().relative_to(repository_root.resolve())
    return CONTAINER_WORKSPACE / relative.as_posix()


def to_host_path(value: str, repository_root: Path) -> str:
    normalized = value.replace("\\", "/")
    prefix = CONTAINER_WORKSPACE.as_posix()
    if normalized == prefix:
        return str(repository_root.resolve())
    if normalized.startswith(prefix + "/"):
        return str(repository_root.resolve() / normalized[len(prefix) + 1 :])
    return value


def owner_workspace_volume_name(
    run_root: str | Path, job_root: Path, repository_root: Path
) -> str:
    """Return the stable named volume for one run's disposable owner workspaces."""
    root = repository_root.resolve()
    job = job_root.resolve()
    run = Path(to_host_path(str(run_root), root)).resolve()
    job_relative = job.relative_to(root).as_posix()
    run_relative = run.relative_to(job).as_posix()
    identity = f"{job_relative}\0{run_relative}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:24]
    return OWNER_WORKSPACE_VOLUME_PREFIX + digest


def remove_owner_workspace_volume(
    run_root: str | Path, job_root: Path, repository_root: Path
) -> bool:
    """Remove only the exact owner-workspace volume derived for one run."""
    volume_name = owner_workspace_volume_name(run_root, job_root, repository_root)
    result = _docker_run(["volume", "rm", volume_name])
    return result is not None and result.returncode == 0


def runner_command(
    *,
    image: str,
    repository_root: Path,
    operation: str,
    arguments: Iterable[str],
    environment: dict[str, str],
    llm_environment: dict[str, str],
) -> list[str]:
    root = repository_root.resolve()
    runner_arguments = [str(argument) for argument in arguments]
    job_mount = _job_root_for_arguments(runner_arguments, root)
    if operation in {"worker", "cli"} and job_mount is None:
        raise ValueError("Implementation runner requires a job.json below its work root")
    application_source = (root / "app").resolve()
    if not application_source.is_dir():
        raise ValueError(f"EasyDep application source is missing: {application_source}")
    command = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--user",
        "root",
        "--security-opt",
        "no-new-privileges:true",
        "--label",
        RUNNER_OWNER_LABEL,
        "-v",
        f"{application_source}:{CONTAINER_WORKSPACE.as_posix()}/app:ro",
        # 컨테이너가 끝나도 Gradle 배포본과 Maven dependency를 남긴다. 구현 Job마다
        # 130MB가 넘는 배포본을 다시 받거나 Windows bind mount에서 수천 파일을 읽지 않는다.
        "-v",
        f"{RUNNER_GRADLE_CACHE_VOLUME}:/tmp/easydep-gradle-cache",
        "-v",
        f"{RUNNER_NPM_CACHE_VOLUME}:{OWNER_NPM_CACHE}",
        # OpenTofu Provider는 용량이 크므로 작업 컨테이너마다 다시 받지 않는다. 이미지에
        # 넣는 대신 named volume에 한 번 내려받아 구현과 Testing runner가 함께 사용한다.
        "-v",
        f"{RUNNER_TOFU_CACHE_VOLUME}:{RUNNER_TOFU_CACHE_PATH}",
        "-e",
        f"PYTHONPATH={CONTAINER_WORKSPACE}/app/implementation/runtime/runtime_hooks:{CONTAINER_WORKSPACE}",
        "-e",
        "EASYDEP_FIXED_LINUX_RUNNER=1",
        # 위 named volume을 Gradle의 공용 저장소로 사용한다. 오래된 이미지가 Windows
        # bind mount 아래를 cache로 선택하더라도 이 값으로 덮어쓴다.
        "-e",
        "GRADLE_USER_HOME=/tmp/easydep-gradle-cache",
        "-e",
        f"EASYDEP_TOFU_PLUGIN_CACHE={RUNNER_TOFU_CACHE_PATH}",
        "-e",
        f"TF_PLUGIN_CACHE_DIR={RUNNER_TOFU_CACHE_PATH}",
        "-e",
        f"{OWNER_TERMINAL_USER_ENV}={OWNER_TERMINAL_USER}",
        "-e",
        f"{OWNER_TERMINAL_SHELL_ENV}={OWNER_TERMINAL_SHELL}",
        "-e",
        f"npm_config_cache={OWNER_NPM_CACHE}",
        "-e",
        f"npm_config_registry={environment.get('npm_config_registry') or environment.get('NPM_CONFIG_REGISTRY') or environment.get(NPM_REGISTRY_ENV) or settings.easydep_npm_registry}",
        "-e",
        "npm_config_replace_registry_host=always",
    ]
    if job_mount is not None:
        job_root, container_job_root = job_mount
        cache_mount_index = command.index("-v", command.index("-v") + 1)
        job_mount_args = ["-v", f"{job_root}:{container_job_root.as_posix()}"]
        if (
            len(runner_arguments) >= 3
            and runner_arguments[0] in {"run-owner", "run-workflow"}
        ):
            volume_name = owner_workspace_volume_name(
                runner_arguments[1], job_root, root
            )
            job_mount_args.extend(
                [
                    "-v",
                    f"{volume_name}:{(container_job_root / 'w').as_posix()}:nocopy",
                ]
            )
        command[cache_mount_index:cache_mount_index] = job_mount_args
        command.extend(
            ["-e", f"{OWNER_CONTROL_ROOT_ENV}={container_job_root.as_posix()}"]
        )
        job_label = _docker_label_value(job_root.name)
        if job_label:
            command[command.index("-v") : command.index("-v")] = [
                "--label",
                f"{RUNNER_JOB_LABEL}={job_label}",
            ]
    run_argument = (
        runner_arguments[1]
        if len(runner_arguments) > 1
        and runner_arguments[0] in {"run-workflow", "run-owner"}
        else None
    )
    if run_argument:
        run_candidate = Path(to_host_path(run_argument, root))
        run_label = _docker_label_value(run_candidate.name)
        if run_label and run_candidate.name.startswith("run_"):
            command[command.index("-v") : command.index("-v")] = [
                "--label",
                f"{RUNNER_RUN_LABEL}={run_label}",
            ]
    experiment_session = environment.get("EASYDEP_EXPERIMENT_SESSION", "").strip()
    if experiment_session:
        volume_index = command.index("-v")
        command[volume_index:volume_index] = [
            "--label",
            f"easydep.experiment-session={experiment_session}",
        ]
    # 일반 실행 설정은 이 모듈이 관리하지만 LLM 설정 이름은 app.llm_connection이 만든
    # 묶음을 그대로 사용한다. provider별 환경변수를 여기에 다시 나열하면 둘이 쉽게
    # 어긋나므로 별도 목록을 두지 않는다.
    transmitted_names = [*RUNTIME_ENVIRONMENT, *llm_environment]
    for name in dict.fromkeys(transmitted_names):
        if environment.get(name):
            command.extend(["-e", name])
    # 이미지 태그가 이전 코드로 만들어졌더라도 ENTRYPOINT에 저장된 Python 모듈명은
    # 사용하지 않는다. bind mount한 현재 저장소의 고정 진입점을 항상 명시한다.
    command.extend(
        [
            "--entrypoint",
            "python",
            image,
            "-B",
            "-m",
            "app.implementation.runtime.member_linux_runner",
            operation,
            *runner_arguments,
        ]
    )
    return command
