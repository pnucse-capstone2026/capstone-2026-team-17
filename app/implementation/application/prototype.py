"""EasyDep 서버와 독립 실행 가능한 구현 CLI 사이를 연결한다.

서버가 받은 설계 산출물을 작업 디렉터리에 UTF-8 파일로 준비하고, 구현 CLI를 별도
프로세스로 실행한 뒤 표준 출력의 JSON 결과를 읽는다. 프로세스를 분리하면 코드 생성이나
빌드가 오래 걸리거나 실패해도 FastAPI 프로세스의 상태와 분리해서 취소할 수 있다.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from app.config import settings
from app.design.contracts.erd import project_logical_model
from app.design.schemas.class_model import BCEModel
from app.llm_connection import llm_subprocess_environment

from ..generation.frontend_scaffold import write_frontend_typescript_config
from ..generation.orchestrator import load_job
from ..generation.runtime_scaffold import write_jackson_runtime_configuration
from ..config import ImplementationSettings
from ..runtime.linux_runner_transport import (
    cleanup_runner_containers,
    configured_runner_image,
    reconcile_orphaned_runner_containers,
    runner_command,
    to_container_path,
)

_OPENAPI_PATH_PARAMETER = re.compile(r"\{([^{}]+)\}")
_OPENAPI_OPERATIONS = frozenset(
    {"delete", "get", "head", "options", "patch", "post", "put", "trace"}
)


class PrototypeExecutionError(RuntimeError):
    """Implementation CLI preparation or execution failed."""

    def __init__(
        self,
        message: str,
        *,
        failure_kind: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_kind = failure_kind
        self.status_code = status_code


def _prepare_runner_output_directories(run_root: Path) -> None:
    """Windows bind mount에서 runner가 쓸 source 부모 폴더를 미리 만든다.

    Linux runner의 일반 사용자는 Windows 공유 폴더에 이미 있는 파일은 수정할 수 있어도
    새 디렉터리를 만드는 과정에서 권한 오류를 받을 수 있다. 실행 계획이 허용한 경로의
    부모만 호스트에서 준비하면 agent의 편집 범위는 넓히지 않으면서 새 page나 package도
    검증 후 안전하게 반영할 수 있다.
    """

    manifest_path = run_root / "reports" / "run-manifest.json"
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    resolved_root = run_root.resolve()
    for task in manifest.get("implementation_tasks", []):
        if not isinstance(task, dict):
            continue
        directories = [
            (run_root / str(path)).parent
            for path in task.get("allowed_write_paths", [])
            if isinstance(path, str)
        ]
        directories.extend(
            run_root / str(path)
            for path in task.get("allowed_write_roots", [])
            if isinstance(path, str)
        )
        if task.get("task_type") == "frontend-implementation":
            # Vite가 만드는 현재 bundle 구조다. source 출력과 마찬가지로 호스트에서
            # 디렉터리만 준비하고, 검증된 내용은 runner가 아래 파일에 복사한다.
            directories.extend(
                [
                    run_root / "application/frontend/dist",
                    run_root / "application/frontend/dist/assets",
                ]
            )
        for directory in directories:
            target = directory.resolve()
            try:
                target.relative_to(resolved_root)
            except ValueError:
                continue
            target.mkdir(parents=True, exist_ok=True)


def _normalize_openapi_path_parameters(api_spec: Any) -> Any:
    """코드 생성기에 필요한 OpenAPI path parameter 선언을 보완한다.

    설계 산출물에는 ``/courses/{courseId}`` 같은 path가 있지만 각 operation의 parameter
    목록에는 같은 이름이 빠질 수 있다. OpenAPI Generator는 이런 문서를 거부하므로,
    선언이 없는 이름만 필수 문자열 parameter로 추가한다. 원본 dict는 수정하지 않는다.
    """
    if not isinstance(api_spec, dict) or not isinstance(api_spec.get("paths"), dict):
        return api_spec

    # 중첩된 dict/list까지 복사해 호출자가 가진 원본 설계 산출물이 바뀌지 않게 한다.
    normalized = json.loads(json.dumps(api_spec))
    _normalize_empty_object_schemas(normalized)
    for path, path_item in normalized["paths"].items():
        if not isinstance(path, str) or not isinstance(path_item, dict):
            continue
        required_names = set(_OPENAPI_PATH_PARAMETER.findall(path))
        if not required_names:
            continue

        shared_parameters = path_item.get("parameters")
        shared_names = (
            {
                parameter.get("name")
                for parameter in shared_parameters
                if isinstance(parameter, dict)
                and parameter.get("in") == "path"
                and isinstance(parameter.get("name"), str)
            }
            if isinstance(shared_parameters, list)
            else set()
        )

        for operation_name, operation in path_item.items():
            if operation_name.lower() not in _OPENAPI_OPERATIONS or not isinstance(operation, dict):
                continue
            operation_parameters = operation.get("parameters")
            if not isinstance(operation_parameters, list):
                operation_parameters = []
                operation["parameters"] = operation_parameters
            declared_names = shared_names | {
                parameter.get("name")
                for parameter in operation_parameters
                if isinstance(parameter, dict)
                and parameter.get("in") == "path"
                and isinstance(parameter.get("name"), str)
            }
            for name in sorted(required_names - declared_names):
                operation_parameters.append(
                    {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
                )
    return normalized


def _normalize_empty_object_schemas(api_spec: dict[str, Any]) -> None:
    """필드가 없는 이름 있는 DTO가 ``Object``로 바뀌지 않도록 닫힌 schema로 표시한다.

    OpenAPI Generator는 ``properties``와 ``additionalProperties``가 모두 없는 object
    schema를 자유 형식 ``Object``로 처리한다. 그러면 ``CourseFilter`` 같은 설계 DTO의
    이름이 사라져 API adapter와 BCE 타입을 연결할 수 없다. 필드를 임의로 추가하지 않고
    ``additionalProperties: false``만 넣어 이름 있는 빈 DTO라는 의미를 보존한다.
    """
    components = api_spec.get("components")
    schemas = components.get("schemas") if isinstance(components, dict) else None
    if not isinstance(schemas, dict):
        return
    for schema in schemas.values():
        if not isinstance(schema, dict):
            continue
        if (
            schema.get("type") == "object"
            and schema.get("properties", {}) == {}
            and "additionalProperties" not in schema
        ):
            schema["additionalProperties"] = False


class PrototypeClient:
    """독립 실행 가능한 구현 CLI의 입력 준비와 하위 프로세스 실행을 담당한다."""

    def __init__(self, settings: ImplementationSettings):
        """실행 설정을 보관하고 작업 ID별 하위 프로세스 registry를 준비한다."""
        self.settings = settings
        self._process_lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[str]] = {}

    def prepare_job(
        self,
        job_id: str,
        app_id: str,
        design: dict[str, Any],
        base_package: str,
        allow_assumptions: bool,
    ) -> Path:
        """설계 파일과 실행 옵션을 작업 디렉터리에 쓰고 ``job.json`` 경로를 반환한다."""
        if not self.settings.python_executable.is_file():
            raise PrototypeExecutionError(
                f"Current EasyDep Python executable does not exist: {self.settings.python_executable}"
            )
        try:
            self.settings.work_root.relative_to(self.settings.repository_root)
        except ValueError as error:
            raise PrototypeExecutionError(
                "Implementation work root must be inside the EasyDep repository"
            ) from error
        root = self.settings.work_root / job_id
        context = root / "design-context"
        context.mkdir(parents=True, exist_ok=True)
        progress_path = root / "generation-progress.json"
        inputs: dict[str, str] = {}

        def write(name: str, filename: str, value: Any) -> None:
            """값이 있는 설계 산출물만 UTF-8 파일로 쓰고 job input에 등록한다."""
            if value in (None, "", {}):
                return
            path = context / filename
            text = (
                json.dumps(value, ensure_ascii=False, indent=2)
                if isinstance(value, (dict, list))
                else str(value)
            )
            path.write_text(text, encoding="utf-8")
            inputs[name] = path.relative_to(self.settings.repository_root).as_posix()

        bce_puml = re.sub(r"\(\s*\.{3}\s*\)", "()", str(design.get("class_diagram_puml") or ""))
        write("bceClass", "class-diagram.puml", bce_puml)
        # 구현용 Java 계약은 표시용 PlantUML을 다시 parsing하지 않고 설계 단계가 저장한
        # 구조화 모델에서 만든다. 네 파일을 같은 job snapshot에 고정하면 생성 도중 다른
        # 설계 버전이 섞이는 것도 막을 수 있다.
        write("bceModel", "class-model.json", design.get("extracted_bce_classes"))
        write("sequenceModel", "sequence-model.json", design.get("sequence_diagram_model"))
        write("apiModel", "api-model.json", design.get("api_spec_model"))
        erd_bce = design.get("erd_bce_classes")
        write("erdBceModel", "erd-class-model.json", erd_bce)
        # 구현 단계가 ERD의 가공 전 BCE를 다시 해석하면 대리키·외래키·연결 테이블을
        # 놓칠 수 있다. ERD 단계에서 다이어그램을 만들 때 검사한 바로 그 논리 모델을
        # 함께 고정해, 저장소 골격도 같은 테이블과 키를 사용하게 한다.
        if isinstance(erd_bce, dict) and erd_bce:
            write(
                "erdLogicalModel",
                "erd-logical-model.json",
                project_logical_model(BCEModel.model_validate(erd_bce)),
            )
        # 구현 에이전트는 설계 다이어그램만으로 비즈니스 규칙을 추측하면 안 된다.
        # 요구사항 단계가 확정한 문장과 유스케이스의 사전·사후 조건, 기본 흐름, 예외
        # 흐름을 같은 작업 스냅샷에 넣어 테스트와 실제 코드를 같은 근거에서 작성한다.
        write("rawRequirements", "raw-requirements.json", design.get("raw_requirements"))
        write(
            "refinedRequirements",
            "refined-requirements.json",
            design.get("refined_requirements"),
        )
        write("useCaseSpec", "use-case-specifications.json", design.get("usecase_spec"))
        # 하나의 파일에 유스케이스별 @startuml 블록을 모두 보존한다. 구현 계획·정합성
        # 검사는 이 입력 전체를 순회하므로 모든 유스케이스 호출 흐름이 소스 생성에 반영된다.
        write("sequence", "sequence-diagrams.puml", design.get("sequence_diagram_puml"))
        write("openapi", "openapi.json", _normalize_openapi_path_parameters(design.get("api_spec")))
        write("erd", "erd.puml", design.get("erd_puml"))
        write("deployment", "deployment-diagram.puml", design.get("deployment_diagram_puml"))
        # PlantUML 배포 다이어그램은 사람이 보는 표현이다. IaC 생성기가 설계에서 검토한
        # 같은 resource 구성을 사용하도록 구조화된 deployment bundle도 함께 전달한다.
        write(
            "deploymentBundle",
            "deployment-diagram-bundle.json",
            design.get("deployment_diagram_bundle"),
        )
        write("cloud", "resource-spec.json", design.get("resource_spec"))
        required_inputs = [
            "bceModel",
            "sequenceModel",
            "apiModel",
            "openapi",
        ]
        if "erdBceModel" in inputs:
            required_inputs.extend(["erdBceModel", "erdLogicalModel"])
        job = {
            "name": f"easydep-{app_id[:8]}",
            "appId": app_id,
            "workspaceRoot": str(self.settings.repository_root),
            "inputs": inputs,
            "requiredInputs": required_inputs,
            "outputRoot": (root / "generated" / "runs")
            .relative_to(self.settings.repository_root)
            .as_posix(),
            "generation": {"basePackage": base_package, "allowAssumptions": allow_assumptions},
            # 빈 method body가 많은 scaffold를 구현 전에 빌드하는 것은 실제 구현 품질을
            # 확인하지 못하면서 Gradle 시간을 한 번 더 쓴다. 생성기 회귀를 조사할 때만
            # IMPLEMENTATION_VERIFY_INITIAL_COMPILE=true로 명시적으로 켠다.
            "verification": {"compile": settings.implementation_verify_initial_compile},
            "progressPath": progress_path.relative_to(self.settings.repository_root).as_posix(),
            "agent": {
                "mode": "openhands",
                "temperature": settings.implementation_agent_temperature,
                "maxOutputTokens": settings.implementation_agent_max_output_tokens,
            },
        }
        path = root / "job.json"
        path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def prepare_feedback_job(
        self,
        job_id: str,
        app_id: str,
        design: dict[str, Any],
        files: dict[str, str],
        feedback: str,
        base_package: str,
        allow_assumptions: bool,
        *,
        repair_task_type: str = "control",
        repair_file_hints: list[str] | None = None,
        verification_profile: dict[str, Any] | None = None,
    ) -> Path:
        """기존 파일 snapshot과 피드백을 사용하는 수정 작업의 ``job.json``을 만든다."""
        path = self.prepare_job(job_id, app_id, design, base_package, allow_assumptions)
        job = json.loads(path.read_text(encoding="utf-8"))
        root = path.parent
        snapshot_path = root / "base-application.json"
        snapshot_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "implementation-source-snapshot/v1alpha1",
                    "files": {
                        f"application/{name.strip('/')}": content
                        for name, content in sorted(files.items())
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        job["jobType"] = "FEEDBACK_REVISION"
        job["feedback"] = feedback
        job["inputs"]["baseSnapshot"] = snapshot_path.relative_to(
            self.settings.repository_root
        ).as_posix()
        job["requiredInputs"] = ["baseSnapshot"]
        job["repair"] = {
            "taskType": repair_task_type,
            "fileHints": list(repair_file_hints or []),
            "verificationProfile": dict(verification_profile or {}),
        }
        path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def generate(self, job_path: Path) -> Path:
        """구현 CLI의 기본 생성 명령을 실행하고 새 run 디렉터리를 반환한다."""
        generated = self._call([str(job_path)], job_path.parent.name)
        return Path(str(generated["output"])).resolve()

    def plan_workflow(self, run_root: Path, job_path: Path) -> dict[str, Any]:
        """생성 결과를 실행·검증하기 위한 workflow를 계획한다."""
        return self._call(["plan-workflow", str(run_root), str(job_path)], job_path.parent.name)

    def generate_and_plan(self, job_path: Path) -> tuple[Path, dict[str, Any]]:
        """Web worker 밖의 호출자가 생성과 planning을 연속 실행할 때 사용하는 helper."""
        run_root = self.generate(job_path)
        return run_root, self.plan_workflow(run_root, job_path)

    def run_phase(
        self, run_root: Path, job_path: Path, retry_failed: bool
    ) -> dict[str, Any]:
        """workflow에서 현재 실행 가능한 phase를 수행한다."""
        write_jackson_runtime_configuration(
            run_root / "application", load_job(job_path).base_package
        )
        write_frontend_typescript_config(run_root / "application" / "frontend")
        args = ["run-workflow", str(run_root), str(job_path)]
        if retry_failed:
            args.append("--retry-failed")
        runner_image = configured_runner_image()
        if runner_image:
            _prepare_runner_output_directories(run_root)
            # 설계 snapshot과 실행 상태는 저장소 안에 있으므로 같은 파일을 Linux 경로로만
            # 바꿔 전달한다. 생성·계획은 빠른 호스트 프로세스에서 끝내고, OpenHands와
            # Gradle이 실제로 동작하는 phase만 고정 Linux 환경에서 실행한다.
            container_args = [
                "run-workflow",
                str(to_container_path(run_root, self.settings.repository_root)),
                str(to_container_path(job_path, self.settings.repository_root)),
            ]
            if retry_failed:
                container_args.append("--retry-failed")
            llm_environment = llm_subprocess_environment()
            environment = os.environ.copy()
            environment.update(llm_environment)
            command = runner_command(
                image=runner_image,
                repository_root=self.settings.repository_root,
                operation="cli",
                arguments=container_args,
                environment=environment,
                llm_environment=llm_environment,
            )
            try:
                return self._call_command(command, job_path.parent.name, environment)
            finally:
                cleanup_runner_containers(
                    job_id=job_path.parent.name,
                    run_id=run_root.name,
                )
        return self._call(args, job_path.parent.name)

    def run_owner(
        self, run_root: Path, job_path: Path, task_id: str
    ) -> dict[str, Any]:
        """Run exactly one already-planned owner task through the member runner.

        This is intentionally narrower than :meth:`run_phase`: planning,
        dependency selection, workflow state changes, and retries all remain
        outside this transport seam.  The CLI validates the selected task.
        """

        runner_image = configured_runner_image()
        if not runner_image:
            raise PrototypeExecutionError(
                "run_owner requires the configured fixed Linux implementation runner"
            )
        _prepare_runner_output_directories(run_root)
        container_args = [
            "run-owner",
            str(to_container_path(run_root, self.settings.repository_root)),
            str(to_container_path(job_path, self.settings.repository_root)),
            task_id,
        ]
        llm_environment = llm_subprocess_environment()
        environment = os.environ.copy()
        environment.update(llm_environment)
        command = runner_command(
            image=runner_image,
            repository_root=self.settings.repository_root,
            operation="cli",
            arguments=container_args,
            environment=environment,
            llm_environment=llm_environment,
        )
        try:
            return self._call_command(command, job_path.parent.name, environment)
        finally:
            cleanup_runner_containers(
                job_id=job_path.parent.name,
                run_id=run_root.name,
            )

    def warmup_runtime(self) -> dict[str, Any]:
        """첫 작업 전에 도구와 공용 dependency cache를 미리 준비한다."""
        from ..generation.warmup import warmup_implementation_runtime

        return warmup_implementation_runtime(
            self.settings.repository_root,
            self.settings.command_timeout_seconds,
        )

    def cancel(self, job_id: str) -> bool:
        """작업 ID에 해당하는 실행 중 프로세스 tree를 종료한다."""
        with self._process_lock:
            process = self._processes.get(job_id)
        if process is None or process.poll() is not None:
            cleanup_runner_containers(job_id=job_id)
            return False
        self._terminate_process_tree(process)
        cleanup_runner_containers(job_id=job_id)
        return True

    def cancel_all(self) -> None:
        """서버 종료 시 이 client가 시작한 모든 하위 프로세스를 종료한다."""
        with self._process_lock:
            processes = list(self._processes.items())
        for job_id, process in processes:
            self._terminate_process_tree(process)
            cleanup_runner_containers(job_id=job_id)

    def reconcile_orphaned_containers(self, valid_job_ids: set[str] | None = None) -> None:
        """서버 시작 시 유효 lease가 없는 EasyDep runner만 정리한다."""
        reconcile_orphaned_runner_containers(valid_job_ids or set())

    def terminate_orphaned_process(self, job_id: str) -> bool:
        """이전 서버가 남긴 해당 Job의 하위 프로세스 tree만 종료한다.

        서버가 강제로 끝나면 메모리 registry는 사라지지만 작은 process marker는 남는다.
        새 서버는 같은 Job을 재개하기 전에 이 PID만 정리하므로 다른 구현 Job이나 사용자가
        직접 실행한 Python 프로세스에는 손대지 않는다.
        """
        marker = self._process_marker_path(job_id)
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
            pid = int(value["pid"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            marker.unlink(missing_ok=True)
            cleanup_runner_containers(job_id=job_id)
            return False
        if not _process_is_alive(pid):
            marker.unlink(missing_ok=True)
            cleanup_runner_containers(job_id=job_id)
            return False
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        else:
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                pass
        marker.unlink(missing_ok=True)
        cleanup_runner_containers(job_id=job_id)
        return True

    def _process_marker_path(self, job_id: str) -> Path:
        return self.settings.work_root / job_id / "implementation-process.json"

    def _write_process_marker(self, job_id: str, pid: int) -> None:
        marker = self._process_marker_path(job_id)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "jobId": job_id,
                    "pid": pid,
                    "ownerPid": os.getpid(),
                    "startedAt": time.time(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _remove_process_marker(self, job_id: str, pid: int) -> None:
        marker = self._process_marker_path(job_id)
        try:
            current = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if int(current.get("pid", -1)) == pid:
            marker.unlink(missing_ok=True)

    def _call(self, args: list[str], operation_id: str | None = None) -> dict[str, Any]:
        """구현 CLI를 UTF-8 하위 프로세스로 실행하고 마지막 JSON 응답을 반환한다."""
        env = os.environ.copy()
        env.update(llm_subprocess_environment())
        # Windows 기본 code page와 관계없이 한글 설계 파일과 JSON 로그를 읽도록 강제한다.
        env["PYTHONUTF8"] = "1"
        env.setdefault(
            "GRADLE_USER_HOME",
            str(self.settings.repository_root / ".easydep" / "gradle-cache"),
        )
        return self._call_command(
            [
                str(self.settings.python_executable),
                "-B",
                "-m",
                "app.implementation.interfaces.cli",
                *args,
            ],
            operation_id,
            env,
        )

    def _call_command(
        self,
        command: list[str],
        operation_id: str | None,
        environment: dict[str, str],
    ) -> dict[str, Any]:
        """호스트 CLI와 Docker runner에 같은 timeout·취소·JSON 처리를 적용한다."""
        environment["PYTHONUTF8"] = "1"
        process: subprocess.Popen[str] | None = None
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                command,
                cwd=self.settings.repository_root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
            if operation_id:
                with self._process_lock:
                    self._processes[operation_id] = process
                self._write_process_marker(operation_id, process.pid)
            stdout, stderr = process.communicate(timeout=self.settings.command_timeout_seconds)
        except subprocess.TimeoutExpired as error:
            if process is not None:
                self._terminate_process_tree(process)
            raise PrototypeExecutionError(
                f"Implementation prototype exceeded {self.settings.command_timeout_seconds} seconds"
            ) from error
        finally:
            if operation_id and process is not None:
                with self._process_lock:
                    if self._processes.get(operation_id) is process:
                        self._processes.pop(operation_id, None)
                self._remove_process_marker(operation_id, process.pid)
        if process.returncode != 0:
            # 일반 stderr보다 run manifest의 ERROR 진단이 사용자에게 더 구체적이다. CLI가
            # 출력 디렉터리를 JSON으로 남겼다면 manifest를 찾아 마지막 오류를 우선 사용한다.
            evidence = (stderr or stdout)[-4000:]
            failure_kind: str | None = None
            status_code: int | None = None
            for line in reversed(stdout.splitlines()):
                try:
                    failed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                failure = failed.get("failure") if isinstance(failed, dict) else None
                if (
                    isinstance(failure, dict)
                    and failure.get("kind") == "provider_request_validation"
                    and failure.get("status_code") == 400
                ):
                    failure_kind = "provider_request_validation"
                    status_code = 400
                output = failed.get("output") if isinstance(failed, dict) else None
                manifest = Path(str(output)) / "reports" / "run-manifest.json" if output else None
                if manifest and manifest.is_file():
                    report = json.loads(manifest.read_text(encoding="utf-8"))
                    messages = [
                        str(item.get("message"))
                        for item in report.get("diagnostics", [])
                        if item.get("severity") == "ERROR"
                    ]
                    if messages:
                        evidence = "; ".join(messages)[-4000:]
                break
            raise PrototypeExecutionError(
                f"Implementation prototype exited with {process.returncode}: {evidence}",
                failure_kind=failure_kind,
                status_code=status_code,
            )
        # 빌드 도구의 일반 로그가 앞에 섞일 수 있으므로 뒤에서부터 유효한 JSON 객체를 찾는다.
        for line in reversed(stdout.splitlines()):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        raise PrototypeExecutionError("Implementation prototype returned no JSON result")

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        """부모 프로세스뿐 아니라 그 프로세스가 시작한 빌드·도구 프로세스도 종료한다."""
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            else:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            process.kill()
        finally:
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()


def _process_is_alive(pid: int) -> bool:
    """추가 패키지 없이 PID가 현재 실행 중인지 확인한다."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
