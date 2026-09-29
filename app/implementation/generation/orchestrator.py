from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from app.config import settings
from app.design.contracts.api_spec import ApiSpecModel
from app.design.contracts.application_runtime import (
    SYNTHETIC_UUID_BASIC_USERNAME,
    application_security_required,
    authenticated_uuid_context_required,
)
from app.design.schemas.class_model import BCEModel
from app.design.schemas.sequence_model import SequenceCollection
from app.llm_connection import build_openhands_llm_connection
from app.implementation.config import npm_command_environment

from ..agents.runtime import write_execution_plan
from ..domain.implementation_ir import (
    pascal_case,
    remove_readonly,
)
from ..domain.models import CommandEvidence, Diagnostic, JobSpec, RunManifest
from ..planning.design_context import (
    TaskSpec,
    generate_backend_owner_tasks,
    generate_backend_unit_test_tasks,
    generate_frontend_tasks,
    generate_frontend_unit_test_tasks,
    generate_vertical_integration_task,
    llm_config,
)
from ..planning.method_projection import project_method_calls
from ..workflows.conformance import capture_generated_contracts
from .runtime_scaffold import write_jackson_runtime_configuration
from .frontend import generate_frontend_project
from .frontend_scaffold import installed_openapi_generator
from .java_scaffold import (
    JAVA_SCAFFOLDER_VERSION,
    JavaScaffoldInput,
    build_java_scaffold_trace,
    render_java_scaffold,
    render_openapi_controller_scaffold,
)
from .method_skeleton import (
    render_backend_method_skeletons,
)
from .operation_contracts import (
    build_generated_operation_contracts,
    write_generated_operation_contracts,
)
from .persistence_scaffold import (
    PERSISTENCE_SCAFFOLDER_VERSION,
    persistence_repository_fqcns,
    render_persistence_scaffold,
)

OPTIONAL_DESIGN_INPUTS = (
    "erdBceModel",
    "erdLogicalModel",
    "deploymentBundle",
    "cloud",
)
IMPLEMENTATION_PIPELINE_VERSION = "0.7.0-sequence-local-skeleton"
OPENAPI_GENERATOR_IMAGE = "openapitools/openapi-generator-cli:v7.24.0"
GRADLE_GENERATOR_IMAGE = "gradle:8.14.2-jdk21"
_SWAGGER_OPERATION_IMPORT = "import io.swagger.v3.oas.annotations.Operation;"
_SWAGGER_OPERATION_ANNOTATION = re.compile(r"(?m)^(?P<indent>[ \t]*)@Operation\b")
# A Docker bind mount can keep a directory handle open for a short time after
# the container process has exited on Windows.  Retrying only the documented
# sharing/access-denied errors keeps an immutable run promotion safe while
# allowing Docker Desktop to release that transient handle.
PROMOTION_RETRYABLE_WINERRORS = frozenset({5, 32, 33})
PROMOTION_MAX_ATTEMPTS = 12
PROMOTION_INITIAL_DELAY_SECONDS = 0.25
PROMOTION_MAX_DELAY_SECONDS = 2.0
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
GRADLE_COMMAND_TIMEOUT_SECONDS = 900
PROGRESS_SCHEMA = "easydep-implementation-progress/v1alpha1"
# Docker Desktop accepts a Windows host path as the bind-mount source, but all
# paths interpreted by the Linux container must be POSIX paths.  Mount every
# implementation input below one fixed container root so BCE, OpenAPI, and
# Gradle all use the same portable contract.
CONTAINER_WORKSPACE = PurePosixPath("/workspace")


def _normalize_openapi_operation_import_conflicts(application: Path) -> None:
    """Keep generated API sources compilable when a model is named ``Operation``."""

    java_root = application / "src" / "main" / "java"
    for api_path in sorted(java_root.rglob("*Api.java")):
        source = api_path.read_text(encoding="utf-8")
        if _SWAGGER_OPERATION_IMPORT not in source:
            continue
        operation_imports = {
            match.group(1)
            for match in re.finditer(r"(?m)^import\s+([^;]+\.Operation);\s*$", source)
        }
        if len(operation_imports) < 2:
            continue
        normalized = source.replace(_SWAGGER_OPERATION_IMPORT + "\n", "")
        normalized = _SWAGGER_OPERATION_ANNOTATION.sub(
            r"\g<indent>@io.swagger.v3.oas.annotations.Operation",
            normalized,
        )
        api_path.write_text(normalized, encoding="utf-8", newline="\n")


def load_job(path: Path) -> JobSpec:
    job_path = path.resolve()
    data = json.loads(job_path.read_text(encoding="utf-8"))
    root = (job_path.parent / data.get("workspaceRoot", ".")).resolve()

    def resolve(value: str) -> Path:
        candidate = (root / value).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"Path escapes workspaceRoot: {value}")
        return candidate

    inputs = {name: resolve(value) for name, value in data.get("inputs", {}).items()}
    generation = data.get("generation", {})
    agent = data.get("agent", {})
    repair = data.get("repair", {})
    repair = repair if isinstance(repair, dict) else {}
    return JobSpec(
        job_type=str(data.get("jobType", "INITIAL_IMPLEMENTATION")),
        feedback=str(data.get("feedback", "")),
        name=data.get("name", job_path.stem),
        workspace_root=root,
        inputs=inputs,
        required_inputs=list(
            data.get(
                "requiredInputs",
                ["bceModel", "sequenceModel", "apiModel", "openapi"],
            )
        ),
        base_package=generation.get("basePackage", "com.example.generated"),
        allow_assumptions=bool(generation.get("allowAssumptions", False)),
        verify_compile=bool(data.get("verification", {}).get("compile", True)),
        output_root=resolve(data.get("outputRoot", "generated/runs")),
        agent_mode=agent.get("mode", "plan-only"),
        agent_temperature=float(
            agent.get("temperature", settings.implementation_agent_temperature)
        ),
        agent_max_output_tokens=int(
            agent.get(
                "maxOutputTokens", settings.implementation_agent_max_output_tokens
            )
        ),
        progress_path=(
            resolve(data["progressPath"])
            if isinstance(data.get("progressPath"), str)
            else None
        ),
        app_id=str(data["appId"]) if data.get("appId") else None,
        repair_task_type=str(repair.get("taskType") or "control"),
        repair_file_hints=[
            str(item)
            for item in repair.get("fileHints") or []
            if isinstance(item, str) and item
        ],
        repair_verification_profile=(
            dict(repair.get("verificationProfile") or {})
            if isinstance(repair.get("verificationProfile"), dict)
            else {}
        ),
    )


class _ManifestBuffer:
    """Per-thread sink so parallel generators never interleave manifest writes.

    A run directory is an immutable, reproducible checkpoint, so the evidence
    it records must not depend on which generator happened to finish first.
    """

    def __init__(self) -> None:
        self.commands: list[CommandEvidence] = []
        self.diagnostics: list[Diagnostic] = []
        self.tools: dict[str, dict[str, str]] = {}


class PrototypeOrchestrator:
    def __init__(self, spec: JobSpec):
        self.spec = spec
        self.manifest = RunManifest(job_name=spec.name, app_id=spec.app_id)
        self._sinks = threading.local()

    def _sink(self):
        """Return the buffer for this thread, or the manifest when sequential."""
        return getattr(self._sinks, "target", None) or self.manifest

    def _generate_sources(self, application: Path, java_root: Path) -> None:
        """Run the three independent generators concurrently.

        They write to disjoint trees -- BCE to src/main/java/<pkg>/bce, OpenAPI
        to src/main/java/<pkg>/api, and the frontend to application/frontend --
        so only the shared manifest needs isolating.
        """
        lanes = (
            (_ManifestBuffer(), lambda: self._generate_bce(java_root)),
            (_ManifestBuffer(), lambda: self._generate_openapi(application)),
            (_ManifestBuffer(), lambda: self._generate_frontend(application)),
        )

        def lane(buffer: _ManifestBuffer, call) -> None:
            self._sinks.target = buffer
            try:
                call()
            finally:
                self._sinks.target = None

        try:
            with ThreadPoolExecutor(
                max_workers=len(lanes), thread_name_prefix="easydep-generate"
            ) as pool:
                futures = [pool.submit(lane, buffer, call) for buffer, call in lanes]
                for future in futures:
                    future.result()
        finally:
            # Merge in declaration order, never completion order.  This runs even
            # when a lane fails: a failed run still has to report the evidence
            # from the generators that did execute.
            for buffer, _ in lanes:
                self.manifest.commands.extend(buffer.commands)
                self.manifest.diagnostics.extend(buffer.diagnostics)
                self.manifest.tools.update(buffer.tools)

    def run(self) -> Path:
        self._set_status("VALIDATING_INPUT", "Validating input artifacts.")
        self._validate_inputs()
        self.manifest.input_hash = self._combined_input_hash()
        staging, final = self._select_run_paths()
        # A successful run for the exact input and generator fingerprint is an
        # immutable checkpoint. Reusing it is essential when a member runner
        # resumes an interrupted agent workflow; regenerating into the same
        # destination would correctly be rejected by _promote, but only after
        # spending time on every generator again.
        if final.exists():
            self._set_status("REUSING_GENERATED_RUN", "Reusing generated output for the same input.")
            return final
        self._reset_target(staging)
        staging.mkdir(parents=True, exist_ok=True)

        if any(item.severity == "ERROR" for item in self.manifest.diagnostics):
            self._set_status("NEEDS_INPUT", "More input is required before generation.")
            self._write_reports(staging)
            self._promote(staging, final)
            return final

        application = staging / "application"
        java_root = application / "src" / "main" / "java"
        java_root.mkdir(parents=True, exist_ok=True)

        try:
            if self.spec.job_type == "FEEDBACK_REVISION":
                self._set_status("PREPARING_FEEDBACK", "Preparing existing artifacts and feedback.")
                self._prepare_feedback_revision(staging)
                self._set_status("SUCCEEDED", "Feedback preparation completed.")
                self.manifest.generated_files = sorted(
                    str(path.relative_to(staging)).replace("\\", "/")
                    for path in staging.rglob("*")
                    if path.is_file()
                )
                self._write_reports(staging)
                self._promote(staging, final)
                return final
            self._set_status(
                "GENERATING_SOURCES",
                "Generating BCE, OpenAPI, and frontend code.",
            )
            self._generate_sources(application, java_root)
            self._set_status("PREPARING_BUILD", "Preparing the generated application project.")
            self._write_gradle_project(application)
            self._write_application_entrypoint(java_root)
            self._write_runtime_configuration(application)
            # Capture before any OpenHands task runs.  These files are the
            # immutable BCE/OpenAPI source contract for the implementation.
            capture_generated_contracts(staging, self.spec.base_package)

            if self.spec.verify_compile:
                self._set_status("VERIFYING", "Compiling the generated backend.")
                self._compile(application)

            self._generate_openapi_controllers(application)
            self._write_operation_contracts(staging)
            self._set_status("PLANNING", "Planning implementation tasks and dependencies.")
            self.manifest.implementation_tasks = []
            self.manifest.agent_execution = write_execution_plan(
                staging,
                self.manifest.implementation_tasks,
                self.spec.agent_mode,
            )

            self._set_status("SUCCEEDED", "Initial generation and implementation planning completed.")
        except Exception as error:  # evidence is written before returning the failed run
            self._set_status("FAILED", "Initial generation or verification failed.")
            self.manifest.diagnostics.append(
                Diagnostic("GENERATION_FAILED", "ERROR", str(error))
            )

        self.manifest.generated_files = sorted(
            str(path.relative_to(staging)).replace("\\", "/")
            for path in staging.rglob("*")
            if path.is_file()
        )
        self._write_reports(staging)
        self._promote(staging, final)
        return final

    def _select_run_paths(self) -> tuple[Path, Path]:
        """Return an immutable destination, retrying only previously failed runs.

        A failure report is useful evidence and must never be overwritten.  It
        must also not permanently prevent a corrected generator/runtime from
        retrying the same input hash.
        """
        base_name = f"run_{self.manifest.input_hash[:12]}"
        attempt = 0
        while True:
            run_name = base_name if attempt == 0 else f"{base_name}_retry_{attempt}"
            final = self.spec.output_root / run_name
            existing_manifest = final / "reports" / "run-manifest.json"
            if not existing_manifest.exists():
                if final.exists():
                    raise RuntimeError(f"Existing run is missing its manifest: {final}")
                return self.spec.output_root / f".{run_name}.staging", final
            existing = json.loads(existing_manifest.read_text(encoding="utf-8"))
            if existing.get("input_hash") != self.manifest.input_hash:
                raise RuntimeError(f"Existing run has a conflicting input hash: {final}")
            if existing.get("status") != "FAILED":
                return self.spec.output_root / f".{run_name}.staging", final
            attempt += 1

    def _prepare_feedback_revision(self, staging: Path) -> None:
        snapshot_path = self.spec.inputs.get("baseSnapshot")
        if snapshot_path is None or not snapshot_path.is_file():
            raise ValueError("Feedback revision requires baseSnapshot")
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        files = snapshot.get("files", {})
        if not isinstance(files, dict) or not files:
            raise ValueError("Feedback revision baseSnapshot is empty")

        allowed: list[str] = []
        for relative, content in sorted(files.items()):
            relative = str(relative).replace("\\", "/").strip("/")
            parts = Path(relative).parts
            if (
                not relative
                or any(part in {"", ".", ".."} for part in parts)
                or not relative.startswith("application/")
            ):
                raise ValueError(f"Invalid feedback snapshot path: {relative}")
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            # Windows에서 ``write_text``의 기본 줄바꿈 변환을 사용하면 이미 CRLF인 snapshot의
            # LF 앞에 CR이 다시 붙는다. 피드백 작업을 반복할수록 ``CR CR ... LF``가 되어
            # OpenHands 편집기가 동일한 Java 블록을 찾지 못하므로, 복원 경계에서 한 번만 LF로
            # 정리하고 이후 플랫폼 변환을 끈다.
            normalized = re.sub(r"\r+\n?", "\n", str(content))
            target.write_text(normalized, encoding="utf-8", newline="\n")
            allowed.append(relative)

        # A feedback revision starts from an already generated application.
        # Snapshot its BCE/OpenAPI contracts before OpenHands receives any
        # writable paths, just as an initial implementation run does.
        capture_generated_contracts(staging, self.spec.base_package)

        task_dir = staging / "reports" / "implementation-tasks"
        task_dir.mkdir(parents=True, exist_ok=True)
        immutable = [
            relative for relative in allowed
            if "/src/main/java/" in f"/{relative}"
            and any(token in f"/{relative}" for token in ("/bce/", "/api/"))
        ]
        editable_candidates = [relative for relative in allowed if relative not in immutable]
        repair_hints = {
            str(value).replace("\\", "/").strip("/")
            for value in self.spec.repair_file_hints
            if str(value).strip()
        }
        task_type = self.spec.repair_task_type or "control"
        # RTM은 조사 시작점을 고르는 근거이지 수정 권한의 경계가 아니다. 실제 원인이
        # 공통 직렬화 설정이나 build 구성에 있으면 RTM에 직접 연결된 controller/service만
        # 열어서는 고칠 수 없다. 계약 파일은 계속 불변으로 두고, Testing 수리에서는 테스트
        # 자체도 쓰기 범위에서 제외한다.
        editable = editable_candidates
        if task_type.startswith("testing-"):
            editable = [
                relative
                for relative in editable
                if "/src/test/" not in f"/{relative}"
                and not relative.startswith("application/frontend/src/generated/")
            ]
        if task_type == "testing-dynamic-functional":
            # 동적 gate는 backend를 직접 호출하므로 frontend 제품 코드는 관련 범위가 아니다.
            # backend source뿐 아니라 build/runtime 설정은 열어 두어 횡단 원인을 고칠 수 있다.
            editable = [
                relative
                for relative in editable
                if not relative.startswith("application/frontend/")
            ]
        verification_profile = dict(self.spec.repair_verification_profile)
        read_source_paths: list[str] = []
        runtime_log_ref = verification_profile.pop("application_log_ref", None)
        if isinstance(runtime_log_ref, str):
            from app.testing.runtime.evidence_store import load_application_log

            try:
                runtime_log = load_application_log(
                    runtime_log_ref,
                    repository_root=self.spec.workspace_root,
                )
            except (OSError, ValueError):
                runtime_log = ""
            if runtime_log:
                runtime_log_path = staging / "reports" / "testing-runtime.log"
                runtime_log_path.parent.mkdir(parents=True, exist_ok=True)
                runtime_log_path.write_text(runtime_log, encoding="utf-8")
                read_source_paths.append(
                    runtime_log_path.relative_to(staging).as_posix()
                )
        context = {
            "schemaVersion": "implementation-feedback-context/v1alpha1",
            "taskId": "apply-source-feedback",
            "taskType": task_type,
            "feedback": self.spec.feedback,
            "editableFiles": editable,
            "immutableFiles": immutable,
            "suggestedFiles": sorted(repair_hints),
            "readSourcePaths": read_source_paths,
        }
        context_path = task_dir / "source-feedback.context.json"
        context_path.write_text(
            json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if task_type == "testing-dynamic-functional":
            testing_gate_rules = (
                "- Inspect the preserved Testing evidence and the named read-only runtime "
                "log, when present, before editing.\n"
                "- Start with the RTM trace hints. They are investigation hints, not a write "
                "boundary; follow evidence into related production code or configuration.\n"
                "- Do not modify tests or test acceptance conditions.\n"
                "- Use run_task_check to rerun the preserved Arazzo workflow after editing. "
                "The outer Testing stage will then resume the complete gate sequence.\n"
            )
        elif task_type.startswith("testing-"):
            testing_gate_rules = (
                "- Reproduce the assigned Testing gate with run_task_check before editing.\n"
                "- After editing, run the same check again and finish only when it passes.\n"
            )
        else:
            testing_gate_rules = (
                "- Add or strengthen assertions in an existing test file when behavior changes.\n"
                "- Finish only when compileJava and test pass.\n"
            )
        prompt = (
            "Apply the user's natural-language feedback to the existing application.\n\n"
            f"## User feedback\n{self.spec.feedback}\n\n"
            "## Rules\n"
            "- Make a minimal, incremental change; preserve unrelated behavior.\n"
            "- Modify only the editable existing files listed below.\n"
            "- Do not weaken, delete, or disable tests to obtain a passing build.\n"
            "- Generated API and BCE contracts are immutable. Do not edit them.\n"
            + testing_gate_rules
            + "\n"
            "## RTM trace hints\n"
            + (
                "\n".join(f"- `{path}`" for path in sorted(repair_hints))
                if repair_hints
                else "- No file-level RTM hint was available; use the failure evidence."
            )
            + "\n"
            "## Editable files\n"
            + "\n".join(f"- `{path}`" for path in editable)
        )
        prompt_path = task_dir / "source-feedback.prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        task = TaskSpec(
            task_id="apply-source-feedback",
            control="Natural-language source feedback",
            prompt_file=str(prompt_path.relative_to(staging)).replace("\\", "/"),
            context_file=str(context_path.relative_to(staging)).replace("\\", "/"),
            allowed_write_paths=editable,
            immutable_paths=immutable,
            source_artifacts={"baseSnapshot": str(snapshot_path)},
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            # 피드백 작업도 최초 구현과 같은 실행 설정을 사용한다. 일부 값만 복사하면
            # OpenHands가 대화를 시작하기 전에 필수 설정을 찾지 못해 실패할 수 있다.
            llm=llm_config(self.spec),
            owner=(
                "frontend"
                if editable
                and all(path.replace("\\", "/").startswith("application/frontend/") for path in editable)
                else "backend"
            ),
            task_type=task_type,
            verification_profile=verification_profile,
        )
        # OpenHands runtime은 실행 계획뿐 아니라 task별 JSON 계약에서 prompt와 쓰기 범위를
        # 읽는다. 피드백 task도 일반 구현 task와 같은 위치와 이름으로 저장해야 한다.
        (task_dir / f"{task.task_id}.task.json").write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.manifest.implementation_tasks = [task.to_dict()]
        self.manifest.agent_execution = write_execution_plan(
            staging,
            self.manifest.implementation_tasks,
            self.spec.agent_mode,
        )

    def _validate_inputs(self) -> None:
        for name in self.spec.required_inputs:
            path = self.spec.inputs.get(name)
            if path is None or not path.is_file():
                self.manifest.diagnostics.append(
                    Diagnostic("MISSING_REQUIRED_INPUT", "ERROR", f"Missing required input: {name}")
                )

        if self.spec.job_type != "FEEDBACK_REVISION":
            for name in OPTIONAL_DESIGN_INPUTS:
                path = self.spec.inputs.get(name)
                if path is None or not path.is_file():
                    self.manifest.diagnostics.append(
                        Diagnostic(
                            "MISSING_PROTOTYPE_INPUT",
                            "WARNING",
                            f"Prototype continues without optional input: {name}",
                        )
                    )

        for name, path in self.spec.inputs.items():
            if not path.is_file():
                continue
            digest = sha256_file(path)
            self.manifest.inputs[name] = {
                "path": str(path),
                "sha256": digest,
                "size": path.stat().st_size,
            }

    def _combined_input_hash(self) -> str:
        connection = build_openhands_llm_connection()
        digest = hashlib.sha256()
        digest.update(self.spec.name.encode())
        digest.update(self.spec.job_type.encode())
        digest.update(self.spec.feedback.encode())
        digest.update(self.spec.base_package.encode())
        digest.update(str(self.spec.allow_assumptions).encode())
        digest.update(str(self.spec.verify_compile).encode())
        digest.update(connection.model.encode())
        digest.update(connection.base_url.encode())
        digest.update(str(self.spec.agent_temperature).encode())
        digest.update(str(self.spec.agent_max_output_tokens).encode())
        digest.update(JAVA_SCAFFOLDER_VERSION.encode())
        digest.update(OPENAPI_GENERATOR_IMAGE.encode())
        digest.update(IMPLEMENTATION_PIPELINE_VERSION.encode())
        # Bind a run to the actual planner/runtime implementation, not only a
        # manually maintained version label. Prompt or gate edits therefore
        # always receive a new immutable run ID.
        implementation_root = Path(__file__).resolve().parents[1]
        for source in sorted(implementation_root.rglob("*.py")):
            digest.update(source.relative_to(implementation_root).as_posix().encode())
            digest.update(sha256_file(source).encode())
        for name in sorted(self.manifest.inputs):
            digest.update(name.encode())
            digest.update(self.manifest.inputs[name]["sha256"].encode())
        return digest.hexdigest()

    def _container_path(self, path: Path) -> str:
        """Return a workspace-relative path as seen by generation containers."""
        root = self.spec.workspace_root.resolve()
        source = path.resolve()
        try:
            relative = source.relative_to(root)
        except ValueError as error:
            raise ValueError(
                f"Docker input/output must stay inside workspaceRoot: {source}"
            ) from error
        return (CONTAINER_WORKSPACE / relative.as_posix()).as_posix()

    def _workspace_volume(self) -> str:
        return f"{self.spec.workspace_root.resolve()}:{CONTAINER_WORKSPACE.as_posix()}"

    def _set_status(self, status: str, message: str) -> None:
        """Persist fine-grained progress for the parent web worker to poll."""
        self.manifest.status = status
        progress_path = getattr(self.spec, "progress_path", None)
        if progress_path is None:
            return
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schemaVersion": PROGRESS_SCHEMA,
            "status": status,
            "message": message,
            "updatedAt": datetime.now(UTC).isoformat(),
        }
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=progress_path.parent,
                prefix=f".{progress_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                json.dump(payload, handle, ensure_ascii=False)
            temporary = Path(temporary_name)
            for attempt in range(5):
                try:
                    os.replace(temporary, progress_path)
                    temporary_name = None
                    return
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def _generate_bce(self, java_root: Path) -> None:
        """구조화된 BCE와 ERD JSON에서 Java 계약과 저장 골격을 함께 만든다."""
        def read_json(name: str) -> dict[str, object]:
            return json.loads(self.spec.inputs[name].read_text(encoding="utf-8"))

        erd_path = self.spec.inputs.get("erdBceModel")
        scaffold = JavaScaffoldInput.model_validate({
            "bceModel": read_json("bceModel"),
            "erdBceModel": (
                json.loads(erd_path.read_text(encoding="utf-8"))
                if erd_path and erd_path.is_file()
                else None
            ),
            "basePackage": self.spec.base_package,
            "javaVersion": 21,
            "applicationName": self.spec.name,
        })
        files = render_java_scaffold(scaffold)
        for relative, content in files.items():
            target = java_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        sequence_model = SequenceCollection.model_validate_json(
            self.spec.inputs["sequenceModel"].read_text(encoding="utf-8")
        )
        method_projection = project_method_calls(
            bce_model=scaffold.bce_model,
            sequence_model=sequence_model,
        )
        persistence_repositories = (
            persistence_repository_fqcns(
                scaffold.erd_bce_model,
                self.spec.base_package,
                logical_model=read_json("erdLogicalModel"),
            )
            if scaffold.erd_bce_model is not None
            else {}
        )
        method_files = render_backend_method_skeletons(
            scaffold.bce_model,
            method_projection,
            self.spec.base_package,
            persistence_repositories=persistence_repositories,
        )
        for relative, content in method_files.items():
            target = java_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        persistence_files = (
            render_persistence_scaffold(
                scaffold.erd_bce_model,
                self.spec.base_package,
                logical_model=read_json("erdLogicalModel"),
            )
            if scaffold.erd_bce_model is not None
            else {}
        )
        application = java_root.parents[2]
        for relative, content in persistence_files.items():
            target = application / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        trace_path = java_root.parents[3] / "reports" / "java-scaffold-trace.json"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(
            json.dumps(
                build_java_scaffold_trace(scaffold, files),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._sink().tools["typed-java-scaffolder"] = {
            "version": JAVA_SCAFFOLDER_VERSION,
            "input": "BCEModel",
            "javaVersion": "21",
        }
        self._sink().tools["sequence-method-scaffolder"] = {
            "schema": "implementation-method-projection/v1alpha1",
            "files": str(len(method_files) + 1),
        }
        if persistence_files:
            self._sink().tools["typed-persistence-scaffolder"] = {
                "version": PERSISTENCE_SCAFFOLDER_VERSION,
                "input": "erdLogicalModel",
                "files": str(len(persistence_files)),
            }

    def _generate_openapi(self, application: Path) -> None:
        jar = installed_openapi_generator()
        input_path = (
            str(self.spec.inputs["openapi"])
            if jar
            else self._container_path(self.spec.inputs["openapi"])
        )
        output_path = str(application) if jar else self._container_path(application)
        generator_arguments = [
            "generate",
            "-g", "spring",
            "-i", input_path,
            "-o", output_path,
            "--additional-properties=" + ",".join(
                [
                    "library=spring-boot",
                    "interfaceOnly=true",
                    "skipDefaultInterface=true",
                    "useSpringBoot3=true",
                    "useBeanValidation=true",
                    "hideGenerationTimestamp=true",
                    f"basePackage={self.spec.base_package}",
                    f"apiPackage={self.spec.base_package}.api",
                    f"modelPackage={self.spec.base_package}.api.model",
                    f"artifactId={self.spec.name}",
                    "groupId=com.example",
                ]
            ),
            "--global-property", "apis,models",
        ]
        command = (
            ["java", "-jar", str(jar), *generator_arguments]
            if jar
            else [
                "docker",
                "run",
                "--rm",
                "-v",
                self._workspace_volume(),
                "-w",
                CONTAINER_WORKSPACE.as_posix(),
                OPENAPI_GENERATOR_IMAGE,
                *generator_arguments,
            ]
        )
        evidence = self._run_command(
            "openapi-generator", command, self.spec.workspace_root
        )
        _normalize_openapi_operation_import_conflicts(application)
        missing_operation_ids: set[str] = set()
        for line in (evidence.stdout + evidence.stderr).splitlines():
            if "Empty operationId found" in line:
                match = re.search(r"path:\s*([A-Za-z]+\s+\S+)", line)
                if match:
                    method, path = match.group(1).split(maxsplit=1)
                    operation = f"{method.upper()} {path}"
                else:
                    operation = line.strip()
                missing_operation_ids.add(operation)
        for operation in sorted(missing_operation_ids):
            self._sink().diagnostics.append(
                Diagnostic(
                    "OPENAPI_MISSING_OPERATION_ID",
                    "WARNING",
                    f"OpenAPI operation has no operationId: {operation}",
                    str(self.spec.inputs["openapi"]),
                )
            )
        self._sink().tools["openapi-generator"] = {
            "kind": "bundled-jar" if jar else "docker-image",
            "version": "7.24.0",
        }

    def _generate_openapi_controllers(self, application: Path) -> None:
        """OpenAPI interface의 확정 선언을 Controller 골격으로 한 번 옮긴다.

        OpenAPI Generator가 interface를 만든 직후 그 확정 선언을 사용한다. 골격은 앞선
        persistence compile을 방해하지 않으며, 미완성 body 표식은 use-case 작업에서 검사한다.
        """
        package_path = Path(self.spec.base_package.replace(".", "/"))
        api_root = application / "src" / "main" / "java" / package_path / "api"
        controller_root = (
            application / "src" / "main" / "java" / package_path / "adapter" / "in" / "web"
        )
        api_model = ApiSpecModel.model_validate_json(
            self.spec.inputs["apiModel"].read_text(encoding="utf-8")
        )
        bce_model = BCEModel.model_validate_json(
            self.spec.inputs["bceModel"].read_text(encoding="utf-8")
        )
        generated = 0
        for interface_path in sorted(api_root.glob("*Api.java")):
            if interface_path.name == "ApiUtil.java":
                continue
            controller_name, source = render_openapi_controller_scaffold(
                interface_path.read_text(encoding="utf-8"),
                self.spec.base_package,
                api_model=api_model,
                bce_model=bce_model,
            )
            target = controller_root / f"{controller_name}.java"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8", newline="\n")
            generated += 1
        self._sink().tools["openapi-controller-scaffolder"] = {
            "interfaces": generated,
            "source": "generated-openapi-interface",
        }

    def _write_operation_contracts(self, staging: Path) -> None:
        """Persist typed generator facts after BCE/service/controller scaffolds exist."""
        bce_model = BCEModel.model_validate_json(
            self.spec.inputs["bceModel"].read_text(encoding="utf-8")
        )
        sequence_model = SequenceCollection.model_validate_json(
            self.spec.inputs["sequenceModel"].read_text(encoding="utf-8")
        )
        api_model = ApiSpecModel.model_validate_json(
            self.spec.inputs["apiModel"].read_text(encoding="utf-8")
        )
        persistence_repositories = persistence_repository_fqcns(
            BCEModel.model_validate_json(self.spec.inputs["erdBceModel"].read_text(encoding="utf-8")),
            self.spec.base_package,
            logical_model=json.loads(self.spec.inputs["erdLogicalModel"].read_text(encoding="utf-8")),
        ) if self.spec.inputs.get("erdBceModel") else {}
        path = write_generated_operation_contracts(
            staging,
            build_generated_operation_contracts(
                bce_model=bce_model,
                sequence_model=sequence_model,
                api_model=api_model,
                base_package=self.spec.base_package,
                persistence_repositories=persistence_repositories,
            ),
        )
        self._sink().tools["generated-operation-contracts"] = {
            "schema": "easydep-generated-operation-contracts/v1",
            "path": str(path.relative_to(staging)).replace("\\", "/"),
        }

    def _generate_frontend(self, application: Path) -> None:
        openapi = json.loads(self.spec.inputs["openapi"].read_text(encoding="utf-8"))
        frontend = application / "frontend"
        generation = generate_frontend_project(
            workspace_root=self.spec.workspace_root,
            openapi_path=self.spec.inputs["openapi"],
            frontend_root=frontend,
            api_spec=openapi,
            application_name=self.spec.name,
            api_base_url=None,
            run_command=self._run_command,
        )
        self._sink().tools["easydep-frontend-generator"] = generation.tool_metadata()

    def _write_gradle_project(self, application: Path) -> None:
        security_dependencies = ""
        if _requires_application_security(self.spec):
            security_dependencies = (
                "    implementation 'org.springframework.boot:spring-boot-starter-security'\n"
                "    testImplementation 'org.springframework.security:spring-security-test'\n"
            )
        build = f"""plugins {{
    id 'org.springframework.boot' version '3.3.13'
    id 'io.spring.dependency-management' version '1.1.6'
    id 'java'
}}

group = 'com.example'
version = '0.1.0-SNAPSHOT'

java {{
    toolchain {{ languageVersion = JavaLanguageVersion.of(21) }}
}}

repositories {{ mavenCentral() }}

dependencies {{
    implementation 'org.springframework.boot:spring-boot-starter-web'
    implementation 'org.springframework.boot:spring-boot-starter-actuator'
    implementation 'org.springframework.boot:spring-boot-starter-validation'
    implementation 'org.springframework.boot:spring-boot-starter-data-jpa'
    implementation 'org.flywaydb:flyway-core'
    runtimeOnly 'org.flywaydb:flyway-mysql'
    implementation 'org.springdoc:springdoc-openapi-starter-webmvc-ui:2.6.0'
    implementation 'com.google.code.findbugs:jsr305:3.0.2'
    implementation 'com.fasterxml.jackson.datatype:jackson-datatype-jsr310'
    implementation 'org.openapitools:jackson-databind-nullable:0.2.10'
    runtimeOnly 'com.mysql:mysql-connector-j'
    runtimeOnly 'com.h2database:h2'
{security_dependencies.rstrip()}
    testImplementation 'org.springframework.boot:spring-boot-starter-test'
}}

tasks.withType(Test).configureEach {{ useJUnitPlatform() }}
"""
        (application / "build.gradle").write_text(build, encoding="utf-8")
        (application / "settings.gradle").write_text(
            f"rootProject.name = '{self.spec.name}'\n", encoding="utf-8"
        )

    def _write_application_entrypoint(self, java_root: Path) -> None:
        """Create the one Spring Boot entrypoint that the wiring task completes."""
        package = self.spec.base_package
        application_class = f"{pascal_case(self.spec.name)}Application"
        target = (
            java_root
            / Path(package.replace(".", "/"))
            / f"{application_class}.java"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"package {package};\n\n"
            "import org.springframework.boot.SpringApplication;\n"
            "import org.springframework.boot.autoconfigure.SpringBootApplication;\n\n"
            "@SpringBootApplication\n"
            f"public class {application_class} {{\n"
            "    public static void main(String[] args) {\n"
            f"        SpringApplication.run({application_class}.class, args);\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )

    def _write_runtime_configuration(self, application: Path) -> None:
        """운영 DB와 test DB처럼 선택 여지가 없는 Spring 설정을 미리 만든다."""
        datasource_required = _requires_application_datasource(self.spec)
        uuid_actor_required = _requires_uuid_authenticated_actor(self.spec)
        security_required = _requires_application_security(self.spec)
        production_security = (
            "  security:\n"
            "    user:\n"
            "      name: ${SPRING_SECURITY_USER_NAME}\n"
            "      password: ${SPRING_SECURITY_USER_PASSWORD}\n"
            "      roles: ${SPRING_SECURITY_USER_ROLES:USER}\n"
            if security_required
            else ""
        )
        resources = application / "src" / "main" / "resources"
        resources.mkdir(parents=True, exist_ok=True)
        datasource = (
            "  datasource:\n"
            "    url: ${SPRING_DATASOURCE_URL}\n"
            "    username: ${SPRING_DATASOURCE_USERNAME}\n"
            "    password: ${SPRING_DATASOURCE_PASSWORD}\n"
            if datasource_required
            else ""
        )
        application_config = (
            "server:\n"
            "  port: 8000\n"
            "spring:\n"
            + datasource
            + "  jpa:\n"
            "    open-in-view: false\n"
            + production_security
            + "management:\n"
            "  endpoints:\n"
            "    web:\n"
            "      base-path: /\n"
            "      exposure:\n"
            "        include: health\n"
            "      path-mapping:\n"
            "        health: healthz\n"
            "  endpoint:\n"
            "    health:\n"
            "      probes:\n"
            "        enabled: true\n"
        )
        (resources / "application.yml").write_text(
            application_config,
            encoding="utf-8",
        )
        write_jackson_runtime_configuration(application, self.spec.base_package)
        test_resources = application / "src" / "test" / "resources"
        test_resources.mkdir(parents=True, exist_ok=True)
        test_security = (
            "  security:\n"
            "    user:\n"
            f"      name: {SYNTHETIC_UUID_BASIC_USERNAME if uuid_actor_required else 'easydep-test'}\n"
            "      password: easydep-test\n"
            "      roles: USER\n"
            if security_required
            else ""
        )
        (test_resources / "application-test.yml").write_text(
            "spring:\n"
            "  datasource:\n"
            "    url: jdbc:h2:mem:easydep_test;MODE=MySQL;DB_CLOSE_DELAY=-1\n"
            "    username: sa\n"
            "    password: ''\n"
            "    driver-class-name: org.h2.Driver\n"
            "  jpa:\n"
            "    hibernate:\n"
            "      ddl-auto: none\n"
            "  flyway:\n"
            "    enabled: true\n"
            + test_security,
            encoding="utf-8",
        )
        if security_required:
            self._write_security_configuration(application)
            if uuid_actor_required:
                self._write_authenticated_actor_id_provider(application)

    def _write_security_configuration(self, application: Path) -> None:
        """명시적인 인증 요구가 있을 때 Spring의 임의 기본 동작을 대신한다."""
        package = self.spec.base_package
        target = (
            application
            / "src"
            / "main"
            / "java"
            / Path(package.replace(".", "/"))
            / "config"
            / "SecurityConfiguration.java"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"""package {package}.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.web.SecurityFilterChain;

/**
 * Minimal HTTP security configuration used when the design requires authentication.
 * Supply production credentials through SPRING_SECURITY_USER_NAME/PASSWORD/ROLES.
 */
@Configuration
public class SecurityConfiguration {{
    @Bean
    SecurityFilterChain securityFilterChain(HttpSecurity http) throws Exception {{
        return http
            .csrf(csrf -> csrf.disable())
            .sessionManagement(session -> session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
            .authorizeHttpRequests(auth -> auth
                .requestMatchers("/", "/index.html", "/assets/**", "/healthz", "/error").permitAll()
                .anyRequest().authenticated())
            .httpBasic(Customizer.withDefaults())
            .build();
    }}
}}
""",
            encoding="utf-8",
        )

    def _write_authenticated_actor_id_provider(self, application: Path) -> None:
        """Expose the authenticated UUID principal as a typed application dependency."""

        package = self.spec.base_package
        target = (
            application
            / "src"
            / "main"
            / "java"
            / Path(package.replace(".", "/"))
            / "config"
            / "AuthenticatedActorIdProvider.java"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"""package {package}.config;

import java.util.UUID;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Component;

@Component
public class AuthenticatedActorIdProvider {{
    public UUID currentActorId() {{
        Authentication authentication = SecurityContextHolder.getContext().getAuthentication();
        if (authentication == null || !authentication.isAuthenticated()) {{
            throw new IllegalStateException("An authenticated UUID principal is required.");
        }}
        String principal = authentication.getName();
        try {{
            UUID actorId = UUID.fromString(principal);
            if (!actorId.toString().equalsIgnoreCase(principal)) {{
                throw new IllegalArgumentException("Principal is not a canonical UUID.");
            }}
            return actorId;
        }} catch (RuntimeException exception) {{
            throw new IllegalStateException("Authenticated principal is not a valid UUID.", exception);
        }}
    }}
}}
""",
            encoding="utf-8",
        )

    def _compile(self, application: Path) -> None:
        # 공용 툴체인 안에서는 설치된 Gradle을 바로 사용한다. 로컬 개발자가 아직 툴체인
        # 이미지를 쓰지 않고 Gradle도 설치하지 않은 경우에만 기존 Docker 경로를 사용한다.
        local_gradle = shutil.which("gradle")
        if local_gradle:
            command = [
                local_gradle,
                "compileJava",
                "--no-daemon",
                "-Dorg.gradle.vfs.watch=false",
                "--build-cache",
            ]
        else:
            command = [
                "docker", "run", "--rm",
                "-v", self._workspace_volume(),
                "-e", "GRADLE_USER_HOME=/tmp/easydep-gradle-home",
                "-w", self._container_path(application),
                GRADLE_GENERATOR_IMAGE,
                "gradle",
                "compileJava",
                "--no-daemon",
                "-Dorg.gradle.vfs.watch=false",
                "--build-cache",
            ]
        self._run_command(
            "gradle-compile",
            command,
            application,
            timeout_seconds=GRADLE_COMMAND_TIMEOUT_SECONDS,
        )
        # Deliberately `compileJava` only.  This pre-execution gate proves the
        # generated scaffold compiles; nothing consumes a jar yet.  `bootJar`
        # resolves the runtime classpath and copies every dependency into
        # BOOT-INF/lib through the bind mount, and the result is discarded:
        # agents/verification/build.py repackages after implementation, the delivery
        # images build their own jar from source, and _persist_outputs skips
        # every `build/` path.
        local_state = application / ".gradle"
        if local_state.exists():
            shutil.rmtree(local_state)

    def _run_command(
        self,
        name: str,
        command: list[str],
        cwd: Path,
        timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> CommandEvidence:
        started = time.monotonic()
        result = subprocess.run(
            command,
            cwd=cwd,
            env=(
                npm_command_environment()
                if command and Path(command[0]).name.lower() in {"npm", "npm.cmd"}
                else None
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        evidence = CommandEvidence(
            name=name,
            command=command,
            cwd=str(cwd),
            exit_code=result.returncode,
            duration_ms=int((time.monotonic() - started) * 1000),
            stdout=result.stdout[-20000:],
            stderr=result.stderr[-20000:],
        )
        self._sink().commands.append(evidence)
        if result.returncode != 0:
            raise RuntimeError(f"{name} failed with exit code {result.returncode}: {result.stderr[-1000:]}")
        return evidence

    def _write_reports(self, root: Path) -> None:
        reports = root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "run-manifest.json").write_text(
            json.dumps(self.manifest.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (reports / "input-diagnostics.json").write_text(
            json.dumps(
                [diagnostic.__dict__ for diagnostic in self.manifest.diagnostics],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        with (reports / "traceability-matrix.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["source_artifact", "source_sha256", "generated_file"])
            for name, metadata in sorted(self.manifest.inputs.items()):
                for generated in self.manifest.generated_files:
                    if (name == "bceModel" and "/bce/" in generated) or (
                        name == "openapi" and ("/api/" in generated or "/api/model/" in generated)
                    ):
                        writer.writerow([name, metadata["sha256"], generated])
                for task in self.manifest.implementation_tasks:
                    if name in task["source_artifacts"]:
                        writer.writerow([name, metadata["sha256"], task["prompt_file"]])
                        writer.writerow([name, metadata["sha256"], task["context_file"]])

    def _reset_target(self, target: Path) -> None:
        target = target.resolve()
        output_root = self.spec.output_root.resolve()
        if output_root not in target.parents:
            raise ValueError(f"Refusing to reset path outside output root: {target}")
        if target.exists():
            shutil.rmtree(target, onerror=remove_readonly)

    def _promote(self, staging: Path, final: Path) -> None:
        self.spec.output_root.mkdir(parents=True, exist_ok=True)
        for attempt in range(PROMOTION_MAX_ATTEMPTS):
            if final.exists():
                raise RuntimeError(f"Refusing to overwrite immutable run: {final}")
            try:
                os.replace(staging, final)
                return
            except OSError as error:
                winerror = getattr(error, "winerror", None)
                if (
                    winerror not in PROMOTION_RETRYABLE_WINERRORS
                    or attempt == PROMOTION_MAX_ATTEMPTS - 1
                ):
                    raise
                delay = min(
                    PROMOTION_INITIAL_DELAY_SECONDS * (2 ** attempt),
                    PROMOTION_MAX_DELAY_SECONDS,
                )
                time.sleep(delay)


def plan_persistence_tasks(spec: JobSpec, run_root: Path) -> None:
    """결정론적으로 생성된 persistence 파일을 확인하고 LLM 작업은 만들지 않는다."""
    run_root = run_root.resolve()
    erd_path = spec.inputs.get("erdBceModel")
    if erd_path is None or not erd_path.is_file():
        raise ValueError("erdBceModel is required to generate persistence files")
    model = BCEModel.model_validate_json(erd_path.read_text(encoding="utf-8"))
    logical_path = spec.inputs.get("erdLogicalModel")
    if logical_path is None or not logical_path.is_file():
        raise ValueError("erdLogicalModel is required to generate persistence files")
    logical_model = json.loads(logical_path.read_text(encoding="utf-8"))
    for relative, content in render_persistence_scaffold(
        model,
        spec.base_package,
        logical_model=logical_model,
    ).items():
        target = run_root / "application" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")

    persistence_task_types = {
        "persistence",
        "persistence-entities",
        "persistence-repositories",
        "persistence-mapping",
        "persistence-schema",
    }
    _merge_implementation_tasks(run_root, [], replace_types=persistence_task_types)


def plan_backend_owner_task(spec: JobSpec, run_root: Path) -> None:
    """Plan backend behavior once and preserve that frozen task graph on resume."""
    run_root = run_root.resolve()
    manifest_path = run_root / "reports" / "run-manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if any(
            isinstance(task, dict)
            and task.get("task_type")
            == "backend-implementation"
            for task in manifest.get("implementation_tasks", [])
        ):
            # Completed markers disappear from source. Replanning from that
            # mutable tree would change task scope and checkpoint identity.
            # Older owner/marker plans remain valid only inside their own run.
            return
    backend_tasks = generate_backend_owner_tasks(spec, run_root)
    unit_test_tasks = generate_backend_unit_test_tasks(spec, run_root, backend_tasks)
    _merge_implementation_tasks(
        run_root,
        [*backend_tasks, *unit_test_tasks],
        replace_types={
            "use-case",
            "wiring",
            "backend-implementation",
            "backend-unit-test",
        },
    )


def plan_frontend_tasks(spec: JobSpec, run_root: Path) -> None:
    """Add the frontend owner and its bounded final integration pass."""
    run_root = run_root.resolve()
    manifest_path = run_root / "reports" / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    backend_tasks = [
        task
        for task in manifest.get("implementation_tasks", [])
        if isinstance(task, dict)
        and task.get("task_type") == "backend-implementation"
    ]
    if not backend_tasks:
        raise ValueError("Frontend planning requires a persisted backend task plan.")
    frontend_tasks = generate_frontend_tasks(spec, run_root)
    frontend_test_tasks = generate_frontend_unit_test_tasks(spec, run_root, frontend_tasks)
    inherited_llm = next(
        (
            task.get("llm")
            for task in sorted(backend_tasks, key=lambda item: str(item.get("task_id") or ""))
            if isinstance(task.get("llm"), dict)
        ),
        None,
    )
    if isinstance(inherited_llm, dict):
        frontend_tasks = [replace(task, llm=dict(inherited_llm)) for task in frontend_tasks]
        frontend_test_tasks = [
            replace(task, llm=dict(inherited_llm)) for task in frontend_test_tasks
        ]
    integration_task = generate_vertical_integration_task(
        spec,
        run_root,
        [
            *backend_tasks,
            *(
                task
                for task in manifest.get("implementation_tasks", [])
                if isinstance(task, dict) and task.get("task_type") == "backend-unit-test"
            ),
            *(task.to_dict() for task in frontend_tasks),
            *(task.to_dict() for task in frontend_test_tasks),
        ],
    )
    if isinstance(inherited_llm, dict):
        integration_task = replace(integration_task, llm=dict(inherited_llm))
    _merge_implementation_tasks(
        run_root,
        [*frontend_tasks, *frontend_test_tasks, integration_task],
        replace_types={
            "frontend-implementation",
            "frontend-unit-test",
            "integration-implementation",
        },
    )


def _merge_implementation_tasks(
    run_root: Path,
    tasks: list[TaskSpec],
    *,
    replace_types: set[str] | None = None,
) -> None:
    """새 작업을 manifest에 합치고, 같은 종류의 이전 계획은 한 번에 교체한다."""

    manifest_path = run_root / "reports" / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    removed_types = replace_types or set()
    existing = {
        item.get("task_id"): item
        for item in manifest.get("implementation_tasks", [])
        if item.get("task_type") not in removed_types
    }
    for task in tasks:
        existing[task.task_id] = task.to_dict()
    manifest["implementation_tasks"] = list(existing.values())
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _requires_application_security(spec: JobSpec) -> bool:
    """명시적 security evidence 또는 UUID context binding에서만 Security를 켠다.

    현재 API 저장 모델에는 보안 항목이 없으므로 OpenAPI의 표준 ``security``와 승인된
    요구사항 문장을 함께 본다. Actor 이름은 추론에 사용하지 않는다. 승인된 API Control
    binding이 Control의 UUID parameter를 ``$context``에서 채울 때는 인증된 principal이
    실행 계약에 포함된 것으로 처리한다.
    """
    def read_json_input(name: str) -> Any:
        path = spec.inputs.get(name)
        if not path or not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    openapi = read_json_input("openapi")
    requirements = read_json_input("refinedRequirements")
    return application_security_required(
        openapi if isinstance(openapi, dict) else {}, requirements
    ) or _requires_uuid_authenticated_actor(spec)


def _requires_uuid_authenticated_actor(spec: JobSpec) -> bool:
    """Return whether an accepted API binding needs a UUID security principal."""

    def read_model(name: str) -> Any:
        path = spec.inputs.get(name)
        if not path or not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    api_model = read_model("apiModel")
    bce_model = read_model("bceModel")
    return authenticated_uuid_context_required(api_model, bce_model)


def _requires_application_datasource(spec: JobSpec) -> bool:
    """Return whether the admitted design declares an application database.

    The deployment bundle is the normalized storage/runtime contract.  Its
    generated-application workload explicitly carries persistent storage and
    datasource bindings when a database is part of the admitted design.  A
    legacy job without that bundle keeps the existing ERD-based behavior.
    """
    bundle_path = spec.inputs.get("deploymentBundle")
    if bundle_path and bundle_path.is_file():
        try:
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            bundle = None
        graph = bundle.get("workloadGraph") if isinstance(bundle, dict) else None
        workloads = graph.get("workloads") if isinstance(graph, dict) else None
        generated = [
            item
            for item in workloads or []
            if isinstance(item, dict)
            and isinstance(item.get("artifact"), dict)
            and item["artifact"].get("kind") == "generatedApplication"
        ]
        if generated:
            datasource_names = {
                "SPRING_DATASOURCE_URL",
                "SPRING_DATASOURCE_USERNAME",
                "SPRING_DATASOURCE_PASSWORD",
            }
            return any(
                any(
                    isinstance(storage, dict)
                    and str(storage.get("persistence") or "").casefold() == "persistent"
                    for storage in item.get("storage") or []
                )
                or any(
                    isinstance(configuration, dict)
                    and str(configuration.get("name") or "") in datasource_names
                    for configuration in item.get("configuration") or []
                )
                for item in generated
            )

    erd_path = spec.inputs.get("erdBceModel")
    return bool(erd_path and erd_path.is_file())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
