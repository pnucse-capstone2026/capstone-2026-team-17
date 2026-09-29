"""구현 작업의 생성, 실행, 결과 저장을 관리한다.

설계 단계가 완료한 산출물의 입력 형태를 확인한 뒤 별도 프로세스에서 코드 생성과 구현 작업을
실행한다. 설계 의미 검사는 설계 단계가 소유하며 여기서 반복하지 않는다. 각 작업의 상태는
``easydep-job-state.json``에 저장하므로 서버가
재시작되어도 workflow checkpoint가 남아 있는 작업은 이어서 실행할 수 있다. 여러 요청이 동시에
들어올 수 있어 실제 실행은 크기가 제한된 thread pool에 맡긴다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.db.models import (
    TYPE_API_SPEC,
    TYPE_CLASS,
    TYPE_DEPLOYMENT,
    TYPE_DEPLOYMENT_FILE,
    TYPE_ERD,
    TYPE_FRONTEND_SOURCE_CODE,
    TYPE_IAC_CODE,
    TYPE_REFINE_REQ,
    TYPE_SEQUENCE,
    TYPE_SOURCE_CODE,
    TYPE_TEST_CODE,
    TYPE_USECASE_SPEC,
)
from app.demo_validation import demo_skip_validation_enabled
from app.metrics import langsmith as langsmith_metrics
from app.repositories import artifact_repository

from ..config import ImplementationSettings
from ..delivery.refresh import refresh_delivery_artifacts
from .feedback import resolve_feedback_targets
from .prototype import PrototypeClient
from .source_files import (
    classify_source_path,
    is_visible_source_path,
    iter_application_sources,
    read_application_source,
)

_INCOMPLETE_LEASE_GRACE_SECONDS = 30.0


def _now() -> str:
    """작업 기록에 사용할 현재 UTC 시각을 ISO 8601 문자열로 반환한다."""
    return datetime.now(UTC).isoformat()


def _pid_is_alive(pid: int) -> bool:
    """추가 dependency 없이 lease 소유 서버가 실행 중인지 확인한다."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows의 ``os.kill(pid, 0)``은 POSIX의 존재 확인과 다르다. Python은 0을
        # console signal로 해석하지 못해 ``TerminateProcess``로 보낼 수 있으므로, 실행 중인
        # EasyDep 서버를 확인하다가 종료시키는 문제가 생긴다. 읽기 전용 process handle로
        # 종료 코드만 확인한다.
        import _winapi  # type: ignore[import-not-found]

        process_query_limited_information = 0x1000
        try:
            handle = _winapi.OpenProcess(
                process_query_limited_information,
                False,
                pid,
            )
        except OSError as error:
            # 다른 계정의 process라 조회 권한이 없다는 응답은 process가 존재한다는 뜻이다.
            return error.winerror == _winapi.ERROR_ACCESS_DENIED
        try:
            return _winapi.GetExitCodeProcess(handle) == _winapi.STILL_ACTIVE
        finally:
            _winapi.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def build_testing_contracts(design: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """구현이 실제로 사용한 요구사항·설계 입력을 Testing용으로 고정한다.

    DB ID는 원본 산출물과의 연결을 증명하고, content는 구현 시작 시점에 이미 수화된 표현을
    그대로 보존한다. 특히 OpenAPI는 저장된 proposal model에서 결정론적으로 만든 문서이므로
    Testing이 최신 class/API model을 섞어 다시 만들지 않는다.
    """

    sources = {
        "requirements": (TYPE_REFINE_REQ, design.get("refined_requirements")),
        "use_cases": (TYPE_USECASE_SPEC, design.get("usecase_spec")),
        "openapi": (TYPE_API_SPEC, design.get("api_spec")),
        "deployment": (TYPE_DEPLOYMENT, design.get("deployment_diagram_bundle")),
    }
    refs = design.get("artifact_versions") or {}
    contracts: dict[str, dict[str, Any]] = {}
    for name, (artifact_type, content) in sources.items():
        ref = refs.get(artifact_type)
        if content in (None, "", {}, []):
            # 저장소를 거친 정상 실행에는 네 입력이 모두 있다. 다만 오래된 구현 작업이나
            # 작은 단위 테스트가 API만 넘긴 경우까지 구현 시작을 막지는 않는다. Testing은
            # 받은 입력만 고정하고, 필수 계약이 빠졌다면 자신의 입력 검사에서 정확히 알린다.
            continue
        encoded = json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        contract = {
            "digest": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "content": content,
        }
        # 정상 HTTP 흐름에서는 load_state가 DB version_id를 제공한다. 단위 테스트처럼 저장소를
        # 거치지 않은 입력도 content와 digest로 고정할 수 있어야 구현 코드가 DB 조회에 묶이지
        # 않는다.
        if isinstance(ref, dict) and ref.get("version_id") is not None:
            contract["version_id"] = int(ref["version_id"])
        contracts[name] = contract
    return contracts


def _trace_artifact_versions(design: dict[str, Any]) -> dict[str, int]:
    """구현이 읽은 설계 버전 중 trace projection에 필요한 ID만 고정한다."""
    versions = design.get("artifact_versions")
    if not isinstance(versions, dict):
        return {}
    wanted = {
        TYPE_REFINE_REQ,
        TYPE_USECASE_SPEC,
        TYPE_CLASS,
        TYPE_SEQUENCE,
        TYPE_API_SPEC,
        TYPE_ERD,
        TYPE_DEPLOYMENT,
    }
    return {
        artifact_type: int(ref["version_id"])
        for artifact_type, ref in versions.items()
        if artifact_type in wanted
        and isinstance(ref, dict)
        and isinstance(ref.get("version_id"), int)
        and not isinstance(ref.get("version_id"), bool)
    }


def _preserve_feedback_traceability(
    traceability: dict[str, Any] | None,
    previous_snapshot: dict[str, Any] | None,
    confirmed_refs: list[str],
) -> dict[str, Any] | None:
    """Feedback가 같은 파일을 다시 쓸 때만 이전 RTM 출처를 보존한다."""
    if not isinstance(traceability, dict):
        return traceability
    mappings = traceability.get("mappings")
    if not isinstance(mappings, list):
        return traceability
    metadata = previous_snapshot.get("metadata") if isinstance(previous_snapshot, dict) else None
    previous = (
        metadata.get("implementation_traceability")
        if isinstance(metadata, dict)
        else None
    )
    prior_by_file: dict[str, dict[str, list[str]]] = {}
    for item in (previous.get("mappings") if isinstance(previous, dict) else []) or []:
        if not isinstance(item, dict) or not isinstance(item.get("target_file"), str):
            continue
        bucket = prior_by_file.setdefault(item["target_file"], {})
        for key in ("requirementIds", "useCaseIds", "sourceRefs"):
            values = item.get(key)
            if isinstance(values, list):
                bucket[key] = sorted(
                    {*bucket.get(key, []), *(str(value) for value in values if value)}
                )
    safe_confirmed = sorted({item for item in confirmed_refs if isinstance(item, str) and item})
    merged: list[Any] = []
    for item in mappings:
        if not isinstance(item, dict):
            merged.append(item)
            continue
        updated = dict(item)
        target_file = updated.get("target_file")
        prior = prior_by_file.get(target_file) if isinstance(target_file, str) else None
        if isinstance(prior, dict):
            # 새 행에 없는 필드만 같은 파일의 이전 확정 값을 옮긴다.
            for key in ("requirementIds", "useCaseIds", "sourceRefs"):
                if not updated.get(key) and prior.get(key):
                    updated[key] = prior[key]
        elif not updated.get("sourceRefs") and safe_confirmed:
            # 새 파일은 파일명이나 내용으로 연결하지 않는다. Testing이 확정한 ref만 쓴다.
            updated["sourceRefs"] = safe_confirmed
        merged.append(updated)
    return {**traceability, "mappings": merged}


def _compact_implementation_verification(
    run_root: Path,
    job_id: str,
    traceability: dict[str, Any] | None,
    confirmed_refs: list[str],
) -> list[dict[str, Any]]:
    """큰 검증 보고서에서 trace에 필요한 실행 근거만 고른다."""
    confirmed = {item for item in confirmed_refs if isinstance(item, str) and item}
    trace_tasks = {
        str(mapping.get("taskId"))
        for mapping in (traceability.get("mappings") if isinstance(traceability, dict) else [])
        or []
        if isinstance(mapping, dict) and isinstance(mapping.get("taskId"), str)
    }
    fallback_tasks = {
        str(mapping["taskId"])
        for mapping in (traceability.get("mappings") if isinstance(traceability, dict) else []) or []
        if isinstance(mapping, dict)
        and isinstance(mapping.get("taskId"), str)
        and confirmed.intersection(
            item for item in mapping.get("sourceRefs", []) if isinstance(item, str)
        )
    }
    compact: list[dict[str, Any]] = []
    for name in ("feedback-regression.json", "final-verification.json"):
        path = run_root / "reports" / name
        if not path.is_file():
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(report, dict):
            continue
        verification = report.get("verification")
        verification = verification if isinstance(verification, dict) else {}
        item: dict[str, Any] = {
            "jobId": job_id,
            "status": report.get("status"),
            "report": f"reports/{name}",
        }
        command = verification.get("command")
        if isinstance(command, str):
            item["command"] = command[:1000]
        elif isinstance(command, list):
            item["command"] = [
                str(part)[:200]
                for part in command[:20]
                if isinstance(part, (str, int, float)) and not isinstance(part, bool)
            ]
        for key in ("exitCode", "durationMs"):
            value = verification.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                item[key] = value
        scenario = report.get("scenarioVerification")
        verified_tasks = {
            str(task.get("taskId"))
            for task in (scenario.get("tasks") if isinstance(scenario, dict) else []) or []
            if isinstance(task, dict)
            and task.get("status") in {"PASSED", "SUCCEEDED"}
            and isinstance(task.get("taskId"), str)
        }
        # scenario가 task별 결과를 주지 않는 전체 build 보고서도 그 job의 RTM task를
        # 검증한 근거다. 요구사항 통과로 바꾸지 않고 task의 evidence로만 연결한다.
        task_ids = sorted(verified_tasks or fallback_tasks or trace_tasks)
        if task_ids:
            item["taskIds"] = task_ids
        compact.append(item)
    return compact


class JobNotFound(KeyError):
    """요청한 구현 작업 ID의 상태 파일이 없을 때 발생한다."""


class InvalidJobState(RuntimeError):
    """현재 작업 상태에서는 요청한 동작을 수행할 수 없을 때 발생한다."""


class ImplementationWorker:
    """구현 작업 상태 파일과 크기가 제한된 로컬 실행 queue를 관리한다."""

    def __init__(self, settings: ImplementationSettings | None = None):
        """실행 경로와 worker pool을 준비하고 중단된 작업을 복구한다."""
        self.settings = settings or ImplementationSettings.from_env()
        self.settings.work_root.mkdir(parents=True, exist_ok=True)
        self.client = PrototypeClient(self.settings)
        self.executor = ThreadPoolExecutor(max_workers=self.settings.max_workers, thread_name_prefix="easydep-implementation")
        self.warmup_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="easydep-warmup")
        self.lock = threading.RLock()
        self._active_jobs: set[str] = set()
        self._warmup_lock = threading.Lock()
        self._warmup_started = False
        self.client.reconcile_orphaned_containers(self._live_lease_job_ids())
        self._recover_pending_jobs()

    def create_job(self, app_id: str, design: dict[str, Any], base_package: str, allow_assumptions: bool) -> dict[str, Any]:
        """필수 설계 snapshot의 존재를 확인하고 비동기 구현 planning을 시작한다."""
        missing = [key for key in ("class_diagram_puml", "api_spec") if design.get(key) in (None, "", {})]
        if missing:
            raise InvalidJobState("Missing required design artifacts: " + ", ".join(missing))
        missing_models = [
            key for key in (
                "extracted_bce_classes", "sequence_diagram_model", "api_spec_model",
            )
            if not isinstance(design.get(key), dict) or not design[key]
        ]
        if missing_models:
            return self._create_input_blocked_job(app_id, base_package, missing_models)
        job_id = uuid.uuid4().hex
        job_path = self.client.prepare_job(job_id, app_id, design, base_package, allow_assumptions)
        record = {
            "job_id": job_id, "app_id": app_id, "status": "QUEUED", "base_package": base_package,
            "job_path": str(job_path), "run_root": None, "workflow": None,
            "testing_contracts": build_testing_contracts(design),
            "trace_artifact_versions": _trace_artifact_versions(design),
            "error": None, "created_at": _now(), "updated_at": _now(),
        }
        self._write(record)
        self.executor.submit(langsmith_metrics.bind_context(self._plan), job_id)
        return self.public_record(record)

    def refresh_delivery(self, job_id: str, app_id: str) -> dict[str, Any]:
        """완료된 구현의 코드와 workflow를 유지한 채 배포 산출물만 갱신한다."""

        record = self._read(job_id)
        if record.get("app_id") != app_id:
            raise JobNotFound(job_id)
        if record.get("status") != "COMPLETED":
            raise InvalidJobState("Delivery files can be refreshed only after implementation.")

        refreshed = refresh_delivery_artifacts(
            app_id,
            implementation_job_id=job_id,
        )
        with self.lock:
            # renderer 실행 중 다른 요청이 작업을 바꾸었을 수 있으므로 저장 직전에 다시 읽는다.
            record = self._read(job_id)
            if record.get("app_id") != app_id or record.get("status") != "COMPLETED":
                raise InvalidJobState("Implementation changed while refreshing delivery files.")
            version_ids = dict(record.get("artifact_version_ids") or {})
            version_ids.update(refreshed["artifact_version_ids"])
            record["artifact_version_ids"] = version_ids
            record["delivery_refresh"] = {
                "updated_at": _now(),
                "provider": refreshed.get("provider"),
                "verification": refreshed.get("verification"),
            }
            record["updated_at"] = _now()
            self._write(record)

        return {
            **refreshed,
            "status": "COMPLETED",
            "artifact_version_ids": version_ids,
        }

    def _create_input_blocked_job(
        self, app_id: str, base_package: str, missing_models: list[str]
    ) -> dict[str, Any]:
        """필수 입력이 빠진 경우 코드 생성기를 시작하지 않는다."""
        job_id = uuid.uuid4().hex
        summary = ", ".join(missing_models)
        record = {
            "job_id": job_id,
            "app_id": app_id,
            "status": "NEEDS_INPUT",
            "base_package": base_package,
            "run_root": None,
            "workflow": {
                "schemaVersion": "implementation-workflow/v1alpha1",
                "status": "NEEDS_INPUT",
                "currentPhase": "input-validation",
                "updatedAt": _now(),
                "phases": [],
                "tasks": [],
                "nextRunnableTasks": [],
                "blockingReason": "Required design snapshot files are missing: " + summary,
            },
            "error": (
                "The implementation job did not start because required design snapshot "
                "files are missing: " + summary
            ),
            "created_at": _now(),
            "updated_at": _now(),
        }
        self._write(record)
        return self.public_record(record)

    def create_feedback_job(
        self,
        app_id: str,
        design: dict[str, Any],
        feedback: str,
        base_package: str,
        allow_assumptions: bool,
        *,
        confirmed_target_refs: list[str] | None = None,
        repair_task_type: str = "control",
        repair_file_hints: list[str] | None = None,
        verification_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """저장된 구현 파일에 사용자 피드백을 적용하는 새 작업을 만든다.

        Workspace가 owner·version을 검증한 RTM ref를 조사 힌트로 받는다. 상류 산출물 변경은
        대화형 router가 해당 전문 단계로 보내므로 여기서 자연어를 다시 분류하지 않는다.
        """
        source_snapshot = artifact_repository.load_file_snapshot(
            app_id, TYPE_SOURCE_CODE
        )
        if not source_snapshot:
            raise InvalidJobState(
                "No generated source snapshot is available for feedback"
            )

        metadata = source_snapshot.get("metadata", {})
        implementation_rtm = (
            metadata.get("implementation_traceability")
            if isinstance(metadata, dict)
            else None
        )
        if confirmed_target_refs is None:
            raise ValueError(
                "Implementation feedback requires Workspace-validated target refs."
            )
        # 대화형 Workspace 또는 Testing이 실제 RTM ref를 확인했다. 이 값은 구현 에이전트가
        # 조사할 시작점이며, 원인이 연결된 공통 코드에 있을 때 수정 범위를 막는 allowlist가 아니다.
        eligibility = {
            "status": "ELIGIBLE",
            "source": "confirmed_workspace_targets",
            "rtmValidated": True,
        }
        job_id = uuid.uuid4().hex
        targeting = resolve_feedback_targets(
            implementation_rtm if isinstance(implementation_rtm, dict) else None,
            confirmed_refs=confirmed_target_refs,
        )
        raw_confirmed_refs = targeting.get("confirmedTargetRefs")
        raw_related_files = targeting.get("relatedFiles")
        confirmed_refs = (
            [str(item) for item in raw_confirmed_refs]
            if isinstance(raw_confirmed_refs, list)
            else []
        )
        related_files = (
            [str(item) for item in raw_related_files]
            if isinstance(raw_related_files, list)
            else []
        )
        effective_file_hints = list(repair_file_hints or [])
        if confirmed_target_refs:
            if set(confirmed_refs) != set(confirmed_target_refs):
                raise ValueError(
                    "Implementation feedback targets no longer match the current RTM."
                )
            if not related_files:
                raise ValueError(
                    "Implementation feedback targets have no trace-linked writable files."
                )
            if repair_file_hints is None:
                effective_file_hints = related_files
        execution_feedback = feedback
        if confirmed_refs or related_files:
            execution_feedback += (
                "\n\n## RTM trace hints\n"
                "Confirmed refs:\n"
                + "\n".join(f"- {item}" for item in confirmed_refs)
                + "\nSuggested starting files:\n"
                + "\n".join(f"- {item}" for item in related_files)
            )

        snapshots = {
            path: item["content"]
            for path, item in source_snapshot.get("files", {}).items()
        }
        base_versions = {
            TYPE_SOURCE_CODE: source_snapshot["version_no"],
        }
        for artifact_type in (
            TYPE_FRONTEND_SOURCE_CODE,
            TYPE_TEST_CODE,
            TYPE_DEPLOYMENT_FILE,
            TYPE_IAC_CODE,
        ):
            snapshot = artifact_repository.load_file_snapshot(app_id, artifact_type)
            if snapshot:
                for path, item in snapshot.get("files", {}).items():
                    # Frontend snapshot은 저장할 때 ``frontend/`` 접두사를 제거한다. 피드백
                    # workspace를 만들 때 이를 되살리지 않으면 package.json과 src가 백엔드
                    # 루트에 섞여 원래 애플리케이션과 다른 프로젝트가 된다.
                    restored_path = (
                        f"frontend/{path}"
                        if artifact_type == TYPE_FRONTEND_SOURCE_CODE
                        else path
                    )
                    snapshots[restored_path] = item["content"]
                base_versions[artifact_type] = snapshot["version_no"]

        job_path = self.client.prepare_feedback_job(
            job_id,
            app_id,
            design,
            snapshots,
            execution_feedback,
            base_package,
            allow_assumptions,
            repair_task_type=repair_task_type,
            repair_file_hints=effective_file_hints,
            verification_profile=verification_profile,
        )
        record = {
            "job_id": job_id,
            "job_type": "FEEDBACK_REVISION",
            "parent_job_id": metadata.get("implementation_job_id"),
            "app_id": app_id,
            "status": "QUEUED",
            "base_package": base_package,
            "base_versions": base_versions,
            "feedback": feedback,
            "feedback_eligibility": eligibility,
            "feedback_targeting": targeting,
            "repair_task_type": repair_task_type,
            "repair_file_hints": effective_file_hints,
            "testing_contracts": build_testing_contracts(design),
            "trace_artifact_versions": _trace_artifact_versions(design),
            "job_path": str(job_path),
            "run_root": None,
            "workflow": None,
            "error": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self._write(record)
        self.executor.submit(langsmith_metrics.bind_context(self._plan), job_id)
        return self.public_record(record)

    def get(self, job_id: str) -> dict[str, Any]:
        """호스트 내부 경로를 제외한 구현 작업 상태를 반환한다."""
        record = self._with_live_generation_progress(self._read(job_id))
        record["checkpoint_retryable"] = self._checkpoint_retryable(record)
        return self.public_record(record)

    def live_sources(self, job_id: str, app_id: str) -> dict[str, Any]:
        """실행 중 폴더에서 화면에 보여 줄 소스 목록을 만든다.

        HTTP 계층에는 ``run_root``를 넘기지 않는다. 작업 ID와 앱 ID가 모두 맞는지 확인한 뒤
        해당 job 폴더 안의 ``application``만 읽으며, 현재 OpenHands 작업의 출력 예정 경로도
        합쳐 아직 생성되지 않은 파일을 ``writing`` 상태로 보여 준다.
        """

        record, run_root, application = self._live_application(job_id, app_id)
        writing_paths = self._running_write_paths(run_root)
        files = {
            item.workspace_path: {
                "path": item.workspace_path,
                "artifact_type": item.artifact_type,
                "artifact_path": item.artifact_path,
                "sha256": item.sha256,
                "size": item.size,
                "exists": True,
                "status": (
                    "writing" if item.workspace_path in writing_paths else "available"
                ),
            }
            for item in iter_application_sources(application)
        }
        for workspace_path in writing_paths:
            if workspace_path in files or not is_visible_source_path(workspace_path):
                continue
            artifact_type, artifact_path = classify_source_path(workspace_path)
            files[workspace_path] = {
                "path": workspace_path,
                "artifact_type": artifact_type,
                "artifact_path": artifact_path,
                "sha256": "",
                "size": 0,
                "exists": False,
                "status": "writing",
            }
        ordered = [files[path] for path in sorted(files)]
        revision_input = "\n".join(
            f"{item['path']}:{item['status']}:{item['sha256']}" for item in ordered
        )
        return {
            "job_id": job_id,
            "run_id": run_root.name,
            "status": str(record.get("status") or ""),
            "revision": hashlib.sha256(revision_input.encode("utf-8")).hexdigest(),
            "files": ordered,
        }

    def live_source_file(
        self, job_id: str, app_id: str, workspace_path: str
    ) -> dict[str, Any]:
        """실행 중 애플리케이션의 UTF-8 text 파일 하나를 반환한다."""

        _record, _run_root, application = self._live_application(job_id, app_id)
        item = read_application_source(application, workspace_path)
        return {
            "path": item.workspace_path,
            "artifact_type": item.artifact_type,
            "artifact_path": item.artifact_path,
            "content": item.content,
            "sha256": item.sha256,
            "size": item.size,
        }

    def _live_application(
        self, job_id: str, app_id: str
    ) -> tuple[dict[str, Any], Path, Path]:
        """job 기록을 확인하고 허용된 실제 application 폴더를 찾는다."""

        if re.fullmatch(r"[0-9a-f]{32}", job_id) is None:
            raise JobNotFound(job_id)
        record = self._read(job_id)
        if str(record.get("app_id")) != app_id:
            raise JobNotFound(job_id)
        run_root_value = record.get("run_root")
        if not isinstance(run_root_value, str):
            raise JobNotFound(job_id)
        job_root = (self.settings.work_root / job_id).resolve()
        run_root = Path(run_root_value).resolve()
        try:
            run_root.relative_to(job_root)
        except ValueError as error:
            raise JobNotFound(job_id) from error
        application = run_root / "application"
        if not application.is_dir():
            raise JobNotFound(job_id)
        return record, run_root, application

    @staticmethod
    def _running_write_paths(run_root: Path) -> set[str]:
        """현재 RUNNING task가 작성할 application 상대 경로를 읽는다."""

        try:
            state = json.loads(
                (run_root / "reports" / "workflow-state.json").read_text(
                    encoding="utf-8"
                )
            )
            manifest = json.loads(
                (run_root / "reports" / "run-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, json.JSONDecodeError):
            return set()
        running_ids = {
            str(task.get("task_id"))
            for task in state.get("tasks", [])
            if isinstance(task, dict) and task.get("status") == "RUNNING"
        }
        paths = {
            str(path).replace("\\", "/").lstrip("/")
            for task in manifest.get("implementation_tasks", [])
            if isinstance(task, dict) and str(task.get("task_id")) in running_ids
            for path in task.get("allowed_write_paths", [])
        }
        return {
            path.removeprefix("application/")
            for path in paths
            if path.startswith("application/")
        }

    def retry_failed(self, job_id: str) -> dict[str, Any]:
        """저장된 checkpoint에서 실패했거나 감사에 멈춘 단계만 다시 시작한다."""
        record = self._read(job_id)
        integration_retry = self._integration_checkpoint_retryable(record)
        if (
            record.get("status")
            not in {"FAILED", "INTERRUPTED", "NEEDS_PLANNER", "CANCELLED"}
            and not integration_retry
        ):
            raise InvalidJobState(
                "Only a failed, interrupted, cancelled, or audit-blocked implementation job can be retried: "
                f"{record.get('status')}"
            )
        if not self._checkpoint_retryable(record):
            raise InvalidJobState(
                "The failed implementation job has no reusable execution checkpoint; "
                "start a fresh implementation run instead."
            )

        if integration_retry:
            self._reset_integration_checkpoint(record)
        record["status"] = "QUEUED"
        record.pop("stopRequested", None)
        record.pop("_interrupted_by_user", None)
        record["checkpoint_retry_count"] = int(
            record.get("checkpoint_retry_count", 0)
        ) + 1
        record["updated_at"] = _now()
        record.pop("error", None)
        record.pop("blocking_details", None)
        self._write(record)
        self.executor.submit(
            langsmith_metrics.bind_context(self._run),
            job_id,
            True,
        )
        record["checkpoint_retryable"] = False
        return self.public_record(record)

    def request_owner_repair(
        self,
        job_id: str,
        *,
        owner: str,
        evidence: dict[str, object],
    ) -> dict[str, Any]:
        """Schedule one owner repair from a completed implementation checkpoint.

        Testing has already classified the failing gate and declares its implementation
        owner. The repair reuses the preserved run, task definition, source, and repair
        evidence; an SDK conversation file is not a delivery prerequisite.
        """
        if owner not in {"backend", "frontend"}:
            raise ValueError(f"Unknown implementation repair owner: {owner}")
        record = self._read(job_id)
        if record.get("status") != "COMPLETED":
            raise InvalidJobState(
                "Only a completed implementation can accept a Testing repair: "
                f"{record.get('status')}"
            )
        if not self._execution_checkpoint_exists(record):
            raise InvalidJobState(
                "The implementation has no reusable owner checkpoint."
            )

        run_root = Path(str(record["run_root"]))
        from ..workflows.repair import RepairRoutingError, select_repair_task

        failed_task_id = str(
            evidence.get("failed_task_id") or evidence.get("failedTaskId") or ""
        )
        try:
            owner_task = select_repair_task(
                run_root,
                owner=owner,
                failed_task_id=failed_task_id,
                evidence=evidence,
            )
        except RepairRoutingError as error:
            raise InvalidJobState(str(error)) from error

        owner_task_id = str(owner_task["task_id"])

        from ..workflows.repair import schedule_cross_phase_repair

        repair_evidence = dict(evidence)
        repair_evidence["owner"] = owner
        repair = schedule_cross_phase_repair(
            run_root,
            failed_task_id or owner_task_id,
            repair_evidence,
        )
        if repair is None:
            raise InvalidJobState(
                f"Could not schedule a repair for implementation owner {owner}."
            )

        record["status"] = "QUEUED"
        record["updated_at"] = _now()
        record["owner_repair"] = {
            "owner": owner,
            "task_id": owner_task_id,
            "repair_plan": "reports/repair-plan.json",
            "requested_at": record["updated_at"],
        }
        record.pop("error", None)
        record.pop("blocking_details", None)
        self._write(record)
        self.executor.submit(
            langsmith_metrics.bind_context(self._run),
            job_id,
            True,
        )
        return self.public_record(record)

    def request_owner_repair_batch(
        self,
        job_id: str,
        *,
        repairs: list[dict[str, object]],
    ) -> dict[str, Any]:
        """Queue declared Testing repairs as one checkpoint workflow cycle.

        Each item keeps the normal owner-routing evidence, but the workflow is
        submitted once so its final integration gate runs after all owners and
        their linked frozen unit rechecks.
        """
        if not repairs:
            raise ValueError("At least one owner repair is required.")
        record = self._read(job_id)
        if record.get("status") != "COMPLETED":
            raise InvalidJobState(
                "Only a completed implementation can accept a Testing repair: "
                f"{record.get('status')}"
            )
        if not self._execution_checkpoint_exists(record):
            raise InvalidJobState("The implementation has no reusable owner checkpoint.")

        run_root = Path(str(record["run_root"]))
        from ..workflows.repair import (
            RepairRoutingError,
            schedule_cross_phase_repair_batch,
            select_repair_task,
        )

        scheduled_repairs: list[tuple[str, dict[str, object]]] = []
        owner_task_ids: list[str] = []
        for repair in repairs:
            owner = str(repair.get("owner") or "")
            if owner not in {"backend", "frontend"}:
                raise ValueError(f"Unknown implementation repair owner: {owner}")
            raw_evidence = repair.get("evidence")
            if not isinstance(raw_evidence, dict):
                raise ValueError("Owner repair evidence must be an object.")
            evidence = dict(raw_evidence)
            failed_task_id = str(
                evidence.get("failed_task_id") or evidence.get("failedTaskId") or ""
            )
            try:
                owner_task = select_repair_task(
                    run_root,
                    owner=owner,
                    failed_task_id=failed_task_id,
                    evidence=evidence,
                )
            except RepairRoutingError as error:
                raise InvalidJobState(str(error)) from error
            owner_task_ids.append(str(owner_task["task_id"]))
            evidence["owner"] = owner
            scheduled_repairs.append((failed_task_id or str(owner_task["task_id"]), evidence))

        try:
            scheduled = schedule_cross_phase_repair_batch(run_root, scheduled_repairs)
        except RepairRoutingError as error:
            raise InvalidJobState(str(error)) from error
        if scheduled is None:
            raise InvalidJobState("Could not schedule the declared implementation repairs.")

        record["status"] = "QUEUED"
        record["updated_at"] = _now()
        record["owner_repair"] = {
            "owner": "batch",
            "task_id": owner_task_ids[0],
            "task_ids": owner_task_ids,
            "repair_plan": "reports/repair-plan.json",
            "requested_at": record["updated_at"],
        }
        record.pop("error", None)
        record.pop("blocking_details", None)
        self._write(record)
        self.executor.submit(
            langsmith_metrics.bind_context(self._run),
            job_id,
            True,
        )
        return self.public_record(record)

    @staticmethod
    def _execution_checkpoint_exists(record: dict[str, Any]) -> bool:
        job_path_value = record.get("job_path")
        run_root_value = record.get("run_root")
        if not isinstance(job_path_value, str) or not isinstance(run_root_value, str):
            return False
        run_root = Path(run_root_value)
        return (
            Path(job_path_value).is_file()
            and (run_root / "reports" / "run-manifest.json").is_file()
            and (run_root / "reports" / "workflow-state.json").is_file()
        )

    @staticmethod
    def _checkpoint_retryable(record: dict[str, Any]) -> bool:
        """실행 checkpoint를 같은 Job에서 안전하게 재사용할 수 있는지 확인한다."""
        failure = record.get("failure_classification")
        if (
            isinstance(failure, dict)
            and failure.get("kind") == "provider_request_validation"
            and failure.get("status_code") == 400
        ):
            # Replaying the preserved checkpoint would only repeat a request
            # that the provider already rejected as invalid.
            return False
        if record.get("status") not in {
            "FAILED",
            "INTERRUPTED",
            "CANCELLED",
            "NEEDS_PLANNER",
            "NEEDS_INPUT",
        }:
            return False
        if not ImplementationWorker._execution_checkpoint_exists(record):
            return False
        return record.get("status") != "NEEDS_INPUT" or ImplementationWorker._integration_checkpoint_retryable(record)

    @staticmethod
    def _integration_checkpoint_retryable(record: dict[str, Any]) -> bool:
        """Return whether the sole paused integration task can be explicitly retried.

        This does not resolve or discard an upstream question.  Workspace keeps that
        question visible and offers this retry separately; only an explicit retry
        resets the existing integration checkpoint.
        """
        if record.get("status") != "NEEDS_INPUT":
            return False
        workflow = record.get("workflow")
        if not isinstance(workflow, dict) or workflow.get("currentPhase") != "integration":
            return False
        pending = [
            task for task in workflow.get("tasks", [])
            if isinstance(task, dict) and task.get("status") != "SUCCEEDED"
        ]
        if len(pending) != 1:
            return False
        task = pending[0]
        if (
            task.get("taskType") != "integration-implementation"
            or task.get("status") != "NEEDS_INPUT"
            or not isinstance(task.get("resultFile"), str)
        ):
            return False
        run_root_value = record.get("run_root")
        if not isinstance(run_root_value, str):
            return False
        run_root = Path(run_root_value)
        result_path = run_root / str(task["resultFile"])
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            result.get("status") == "NEEDS_INPUT"
            and result.get("terminationReason") == "UPSTREAM_GAP"
        )

    @staticmethod
    def _reset_integration_checkpoint(record: dict[str, Any]) -> None:
        """Archive a paused integration result before an explicit checkpoint retry."""
        workflow = record["workflow"]
        assert isinstance(workflow, dict)
        task = next(
            item for item in workflow["tasks"]
            if isinstance(item, dict) and item.get("status") == "NEEDS_INPUT"
        )
        run_root = Path(str(record["run_root"]))
        result_path = run_root / str(task["resultFile"])
        archived = result_path.with_name(
            f"{task['task_id']}.attempt-{int(task.get('attempts', 0)):03d}.admission.result.json"
        )
        if archived.exists():
            raise InvalidJobState("The integration admission evidence is already archived.")
        result_path.replace(archived)
        task["status"] = "PENDING"
        task["resultFile"] = None
        task["upstreamGap"] = None
        task["candidateEvidence"] = None
        workflow["status"] = "READY"
        workflow["blockingReason"] = None
        workflow["blockingDetails"] = []
        record["workflow"] = workflow

    def get_testing_input(self, job_id: str) -> dict[str, Any]:
        """Testing API가 버전이 고정된 입력을 만들 때 필요한 정보를 반환한다.

        Testing API는 공개 작업 응답을 다시 해석하지 않고, 구현 작업이 저장한 snapshot의
        DB 식별자를 직접 받는다. 이 식별자로 파일을 한 번 복원하면 같은 앱에 새 구현이
        저장되어도 Testing 작업의 입력이 바뀌지 않는다.
        """
        record = self._read(job_id)
        return {
            "job_id": record["job_id"],
            "app_id": record["app_id"],
            "status": record["status"],
            "artifact_version_ids": dict(record.get("artifact_version_ids") or {}),
            # 내부 작업 파일의 이름은 기존 ``testing_contracts``를 유지하지만, TestingInput이
            # 사용하는 공개 이름으로 넘긴다. 양쪽 이름을 동시에 노출해 소비자가 임의로
            # fallback하는 구조를 만들지 않는다.
            "contract_artifacts": dict(record.get("testing_contracts") or {}),
            # 같은 파일 snapshot을 재사용해도 현재 Job이 만든 RTM을 Testing에
            # 전달한다. 파일 내용 version과 실행별 추적 정보의 수명은 다르다.
            "implementation_traceability": record.get(
                "implementation_traceability"
            ),
        }

    def discard_feedback_candidate(
        self,
        job_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """검사 결과를 개선하지 못한 feedback 산출물만 폐기한다.

        구현 Job 기록에는 새로 만든 버전과 내용이 같아 재사용한 이전
        버전 ID가 함께 들어 있을 수 있다. Repository가 실제 버전의 소유 Job을
        다시 확인하므로 이 메서드는 이전 성공 결과를 지우지 않는다. 실행 중인
        Job이 나중에 snapshot을 다시 저장하지 않도록 완료된 feedback Job만 받는다.
        """

        record = self._read(job_id)
        if record.get("job_type") != "FEEDBACK_REVISION":
            raise InvalidJobState(
                "Only a feedback revision candidate can discard generated artifacts"
            )
        if record.get("status") != "COMPLETED":
            raise InvalidJobState(
                "Only a completed feedback revision can discard generated artifacts"
            )
        if record.get("artifact_status") == "DISCARDED":
            return self.public_record(record)

        version_ids = record.get("artifact_version_ids")
        owned = artifact_repository.delete_file_snapshots_owned_by_job(
            str(record["app_id"]),
            version_ids if isinstance(version_ids, dict) else {},
            implementation_job_id=str(record["job_id"]),
        )
        record["artifact_version_ids"] = {}
        record["artifact_status"] = "DISCARDED"
        record["discarded_artifact_types"] = sorted(owned)
        record["artifacts_discarded_at"] = _now()
        record["artifacts_discarded_reason"] = reason[-2000:]
        record["updated_at"] = _now()
        self._write(record)
        return self.public_record(record)

    def register_snapshot(
        self,
        app_id: str,
        artifact_version_ids: dict[str, int],
        testing_contracts: dict[str, dict[str, Any]],
        *,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """복사된 구현 산출물을 Testing이 읽을 수 있는 완료 작업으로 등록한다.

        분기는 OpenHands 실행 폴더를 복사하지 않는다. Testing에는 실제 파일이 들어 있는
        ``ArtifactVersion`` ID와 구현 때 고정한 설계 계약만 필요하므로, 이 작은 작업 기록을
        새로 만든다. 실행 중 체크포인트는 만들지 않아 이 작업을 재개 대상으로
        잘못 취급하지 않는다.
        """

        snapshot_job_id = job_id or uuid.uuid4().hex
        if self._record_path(snapshot_job_id).exists():
            raise InvalidJobState(f"Implementation snapshot job already exists: {snapshot_job_id}")
        timestamp = _now()
        record = {
            "job_id": snapshot_job_id,
            "job_type": "CHECKPOINT_BRANCH",
            "app_id": app_id,
            "status": "COMPLETED",
            "artifact_version_ids": dict(artifact_version_ids),
            "testing_contracts": dict(testing_contracts),
            "workflow": None,
            "error": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        self._write(record)
        return self.public_record(record)

    def cancel(self, job_id: str) -> dict[str, Any]:
        """종료되지 않은 작업을 취소하고 실행 중인 하위 프로세스도 중지한다."""
        record = self._read(job_id)
        if record["status"] in {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}:
            raise InvalidJobState(f"Job is already in a terminal state: {record['status']}")
        record["status"] = "CANCELLED"
        record["stopRequested"] = True
        record["error"] = "Job execution was cancelled by user request."
        record["updated_at"] = _now()
        self._write(record)
        self.client.cancel(job_id)
        return self.public_record(record)

    def _plan(self, job_id: str) -> None:
        """하위 프로세스에서 코드를 생성한 뒤 실행할 task와 phase를 계획한다."""
        lease_token = self._claim_job_execution(job_id)
        if lease_token is None:
            return
        record = self._read(job_id)
        run_after_plan = False
        try:
            self._set_status(record, "GENERATING")
            run_root = self.client.generate(Path(record["job_path"]))
            # 생성은 별도 프로세스에서 실행된다. 프로세스가 끝나기 직전에 사용자가 취소했을
            # 수 있으므로 결과를 반영하기 전에 최신 상태를 다시 읽는다.
            if self._read(job_id).get("status") == "CANCELLED":
                return
            record["run_root"] = str(run_root)
            manifest_path = run_root / "reports" / "run-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("status") == "NEEDS_INPUT":
                # 입력 계약 오류는 사용자가 설계를 고쳐 해결할 수 있는 정상적인 중단이다.
                # 오류 보고서를 보존하고, 생성된 코드가 필요한 planner는 실행하지 않는다.
                diagnostics = [
                    str(item.get("message") or "")
                    for item in manifest.get("diagnostics") or []
                    if isinstance(item, dict) and item.get("severity") == "ERROR"
                ]
                workflow = {
                    "schemaVersion": "implementation-workflow/v1alpha1",
                    "status": "NEEDS_INPUT",
                    "currentPhase": "input-validation",
                    "updatedAt": _now(),
                    "phases": [],
                    "tasks": [],
                    "nextRunnableTasks": [],
                    "blockingReason": (
                        "; ".join(diagnostics)
                        or "Implementation input validation requires design changes."
                    ),
                }
                self._apply_workflow(record, workflow)
                self._complete_demo_fallback(record, original_status="NEEDS_INPUT")
                return
            self._set_status(record, "PLANNING")
            workflow = self.client.plan_workflow(run_root, Path(record["job_path"]))
            if self._read(job_id).get("status") == "CANCELLED":
                return
            self._apply_workflow(record, workflow)
            run_after_plan = record["status"] == "QUEUED"
            if not run_after_plan:
                self._complete_demo_fallback(
                    record,
                    original_status=str(workflow.get("status") or "FAILED"),
                )
        except Exception as error:
            self._fail(record, error)
        finally:
            self._release_job_execution(job_id, lease_token)
        if run_after_plan:
            self.executor.submit(
                langsmith_metrics.bind_context(self._run),
                job_id,
                False,
            )

    def _run(self, job_id: str, retry_failed: bool) -> None:
        """실행 가능한 workflow 작업을 수행하고 완료된 파일을 보관한다."""
        lease_token = self._claim_job_execution(job_id)
        if lease_token is None:
            return
        record = self._read(job_id)
        requeue = False
        try:
            self._set_status(record, "RUNNING")
            workflow = self.client.run_phase(
                Path(record["run_root"]),
                Path(record["job_path"]),
                retry_failed,
            )
            current = self._read(job_id)
            if current.get("status") == "CANCELLED":
                return
            if current.get("stopRequested") is True or current.get("_interrupted_by_user") is True:
                record["stopRequested"] = True
                record["_interrupted_by_user"] = True
            self._apply_workflow(record, workflow, write=False)
            requeue = record["status"] == "QUEUED"
            if record["status"] == "COMPLETED":
                self._persist_outputs(record)
            elif record["status"] in {"FAILED", "INTERRUPTED", "NEEDS_INPUT", "NEEDS_PLANNER"} and self._complete_demo_fallback(
                record,
                original_status=str(workflow.get("status") or "FAILED"),
            ):
                requeue = False
            else:
                self._write(record)
        except Exception as error:
            self._fail(record, error)
        finally:
            self._release_job_execution(job_id, lease_token)
        # 같은 Job의 다음 수리 cycle은 현재 실행권을 반납한 뒤 시작한다. 먼저 submit하면
        # thread pool이 즉시 새 함수를 실행해 자기 자신의 lease와 충돌할 수 있다.
        if requeue:
            self.executor.submit(
                langsmith_metrics.bind_context(self._run),
                job_id,
                True,
            )

    def _apply_workflow(
        self,
        record: dict[str, Any],
        workflow: dict[str, Any],
        *,
        write: bool = True,
    ) -> None:
        """외부 실행기의 workflow 상태를 EasyDep 구현 작업 상태로 변환한다."""
        record["workflow"] = workflow
        status = str(workflow.get("status", "FAILED"))
        if status == "COMPLETE" or (
            status == "READY" and self._workflow_is_complete(workflow)
        ):
            record["status"] = "COMPLETED"
        elif status in {"READY", "READY_TO_FINALIZE"}:
            record["status"] = "QUEUED"
        elif status in {"NEEDS_INPUT", "NEEDS_PLANNER", "INTERRUPTED", "FAILED"}:
            record["status"] = status
            record["error"] = str(
                workflow.get("blockingReason")
                or workflow.get("error")
                or f"Implementation job {status}"
            )
            details = workflow.get("blockingDetails")
            record["blocking_details"] = details if isinstance(details, list) else []
        else:
            record["status"] = status
        if status == "COMPLETE":
            record.pop("error", None)
            record.pop("blocking_details", None)
        record["updated_at"] = _now()
        if write:
            self._write(record)

    @staticmethod
    def _workflow_is_complete(workflow: dict[str, Any]) -> bool:
        """남은 task가 없는 ``READY`` workflow를 완료로 판단한다.

        일부 workflow runner는 모든 task가 성공한 뒤에도 ``COMPLETE``가 아니라 ``READY``를
        반환한다. 새로 계획만 끝난 때도 같은 값을 사용하므로, 실제 task와 phase가 있고
        실행 가능하거나 차단된 일이 하나도 없을 때만 ``COMPLETED``로 바꾼다.
        """
        tasks = workflow.get("tasks")
        phases = workflow.get("phases")
        if not isinstance(tasks, list) or not tasks:
            return False
        if not isinstance(phases, list) or not phases:
            return False
        if workflow.get("nextRunnableTasks"):
            return False
        if workflow.get("blockingReason") or workflow.get("blockingDetails"):
            return False
        return all(
            isinstance(task, dict) and task.get("status") == "SUCCEEDED"
            for task in tasks
        ) and all(
            isinstance(phase, dict)
            and phase.get("status") in {"SUCCEEDED", "UNPLANNED"}
            for phase in phases
        )

    def _persist_outputs(self, record: dict[str, Any]) -> None:
        """생성된 파일을 종류별 snapshot으로 나누어 산출물 저장소에 저장한다."""
        application = Path(record["run_root"]) / "application"
        groups: dict[str, dict[str, str]] = {
            kind: {}
            for kind in (
                TYPE_SOURCE_CODE,
                TYPE_FRONTEND_SOURCE_CODE,
                TYPE_TEST_CODE,
                TYPE_DEPLOYMENT_FILE,
                TYPE_IAC_CODE,
            )
        }
        for source in iter_application_sources(application):
            groups[source.artifact_type][source.artifact_path] = source.content
        metadata = {
            "implementation_job_id": record["job_id"],
            "run_id": Path(record["run_root"]).name,
            "job_type": record.get("job_type", "INITIAL_IMPLEMENTATION"),
            "parent_job_id": record.get("parent_job_id"),
            "base_versions": record.get("base_versions", {}),
            "feedback": record.get("feedback"),
        }
        trace_path = Path(record["run_root"]) / "reports" / "rtm-traceability-map.json"
        implementation_trace = (
            json.loads(trace_path.read_text(encoding="utf-8"))
            if trace_path.is_file()
            else None
        )
        confirmed_refs: list[str] = []
        if record.get("job_type") == "FEEDBACK_REVISION":
            base_versions = record.get("base_versions")
            base_version = (
                base_versions.get(TYPE_SOURCE_CODE)
                if isinstance(base_versions, dict)
                else None
            )
            previous_snapshot = (
                artifact_repository.load_file_snapshot(
                    record["app_id"], TYPE_SOURCE_CODE, version_no=base_version
                )
                if isinstance(base_version, int)
                else None
            )
            targeting = record.get("feedback_targeting")
            raw_confirmed_refs = (
                targeting.get("confirmedTargetRefs")
                if isinstance(targeting, dict)
                else []
            )
            confirmed_refs = (
                raw_confirmed_refs if isinstance(raw_confirmed_refs, list) else []
            )
            implementation_trace = _preserve_feedback_traceability(
                implementation_trace if isinstance(implementation_trace, dict) else None,
                previous_snapshot,
                confirmed_refs,
            )
        implementation_verification = _compact_implementation_verification(
            Path(record["run_root"]),
            str(record["job_id"]),
            implementation_trace if isinstance(implementation_trace, dict) else None,
            confirmed_refs,
        )
        record["implementation_traceability"] = (
            implementation_trace if isinstance(implementation_trace, dict) else None
        )
        snapshots_to_save: dict[
            str,
            tuple[dict[str, str], dict[str, Any]],
        ] = {}
        for artifact_type, files in groups.items():
            if files:
                snapshot_metadata = dict(metadata)
                # 추적표는 여러 파일 snapshot에 반복 저장하지 않는다. 모든 구현 실행에
                # 존재하는 backend source snapshot 한 곳에 두고, Testing과 산출물 API가
                # 해당 버전을 기준으로 task→파일 연결을 재사용한다.
                if artifact_type == TYPE_SOURCE_CODE and isinstance(
                    implementation_trace, dict
                ):
                    snapshot_metadata["implementation_traceability"] = (
                        implementation_trace
                    )
                if artifact_type == TYPE_SOURCE_CODE and isinstance(
                    record.get("testing_contracts"), dict
                ):
                    # 이 source가 구현될 때 실제로 읽은 계약이다. 나중 Testing 결과를
                    # 다른 최신 설계와 섞지 않고 trace로 되살릴 수 있게 함께 고정한다.
                    snapshot_metadata["testing_contracts"] = dict(
                        record["testing_contracts"]
                    )
                if artifact_type == TYPE_SOURCE_CODE and isinstance(
                    record.get("trace_artifact_versions"), dict
                ):
                    snapshot_metadata["trace_artifact_versions"] = dict(
                        record["trace_artifact_versions"]
                    )
                if artifact_type == TYPE_SOURCE_CODE and implementation_verification:
                    snapshot_metadata["implementation_verification"] = (
                        implementation_verification
                    )
                snapshots_to_save[artifact_type] = (files, snapshot_metadata)
        version_ids = artifact_repository.save_file_snapshots(
            record["app_id"], snapshots_to_save
        )
        # ``save_file_snapshot``이 반환한 DB 식별자만 Testing API에 넘긴다. Testing은
        # 이 ID가 가리키는 파일 묶음을 한 번 복원하고 모든 검사를 같은 폴더에서 실행한다.
        record["artifact_version_ids"] = version_ids
        record["updated_at"] = _now()
        self._write(record)

    def _set_status(self, record: dict[str, Any], status: str) -> None:
        """상태와 수정 시각을 함께 바꾸고 즉시 디스크에 저장한다."""
        record["status"] = status
        record["updated_at"] = _now()
        self._write(record)

    def _fail(self, record: dict[str, Any], error: Exception) -> None:
        """취소된 작업은 되살리지 않고 나머지 오류를 ``FAILED``로 기록한다."""
        try:
            if self._read(str(record["job_id"])).get("status") == "CANCELLED":
                return
        except JobNotFound:
            pass
        if self._complete_demo_fallback(record, original_status="EXCEPTION"):
            return
        record["status"] = "FAILED"
        record["error"] = str(error)[-4000:]
        failure_kind = getattr(error, "failure_kind", None)
        status_code = getattr(error, "status_code", None)
        if failure_kind == "provider_request_validation" and status_code == 400:
            # Workspace reads persisted job state, rather than this exception.
            record["failure_classification"] = {
                "kind": failure_kind,
                "status_code": status_code,
            }
        else:
            record.pop("failure_classification", None)
        record["updated_at"] = _now()
        self._write(record)

    @staticmethod
    def _explicit_user_interrupt(record: dict[str, Any]) -> bool:
        return bool(
            record.get("stopRequested") is True
            or record.get("_interrupted_by_user") is True
            or record.get("status") == "CANCELLED"
        )

    @staticmethod
    def _demo_completion_workflow(workflow: object) -> dict[str, Any]:
        source = workflow if isinstance(workflow, dict) else {}
        phases = [
            {
                "phaseId": str(phase.get("phaseId") or ""),
                "status": "SUCCEEDED",
            }
            for phase in source.get("phases", [])
            if isinstance(phase, dict)
        ]
        tasks = [
            {
                key: task[key]
                for key in ("task_id", "taskType", "owner", "phase")
                if key in task
            }
            | {"status": "SUCCEEDED"}
            for task in source.get("tasks", [])
            if isinstance(task, dict)
        ]
        return {
            "schemaVersion": str(source.get("schemaVersion") or "implementation-workflow/v1alpha1"),
            "status": "COMPLETE",
            "currentPhase": None,
            "phases": phases,
            "tasks": tasks,
            "nextRunnableTasks": [],
            "blockingReason": None,
            "blockingDetails": [],
            "demoFallback": True,
        }

    def _complete_demo_fallback(
        self,
        record: dict[str, Any],
        *,
        original_status: str,
    ) -> bool:
        """Collapse non-user implementation failures only for the demo workflow."""
        if not demo_skip_validation_enabled() or self._explicit_user_interrupt(record):
            return False
        if record.get("status") == "COMPLETED":
            return False
        delivery_finalized = self._finalize_demo_delivery(record)
        record["workflow"] = self._demo_completion_workflow(record.get("workflow"))
        record["status"] = "COMPLETED"
        record["demoFallback"] = True
        record["demoFallbackOriginalStatus"] = original_status
        record["demoFallbackDeliveryFinalized"] = delivery_finalized
        record.pop("error", None)
        record.pop("blocking_details", None)
        record["updated_at"] = _now()
        try:
            self._persist_outputs(record)
        except Exception:  # noqa: BLE001 - demo completion retains any existing workspace.
            record["updated_at"] = _now()
            self._write(record)
        return True

    @staticmethod
    def _finalize_demo_delivery(record: dict[str, Any]) -> bool:
        """Reuse normal delivery rendering before persisting a demo fallback."""
        run_root = record.get("run_root")
        job_path = record.get("job_path")
        if not isinstance(run_root, str) or not isinstance(job_path, str):
            return False
        try:
            from ..generation.orchestrator import load_job
            from ..workflows.coordinator import _complete_implementation

            workflow = record.get("workflow")
            state = workflow if isinstance(workflow, dict) else {}
            _complete_implementation(
                Path(run_root),
                load_job(Path(job_path)),
                state,
                {"status": "SKIPPED"},
            )
        except Exception:  # noqa: BLE001 - public demo completion still retains sources.
            return False
        return True

    def _record_path(self, job_id: str) -> Path:
        return self.settings.work_root / job_id / "easydep-job-state.json"

    def _lease_path(self, job_id: str) -> Path:
        return self.settings.work_root / job_id / "execution-lease.json"

    def _claim_job_execution(self, job_id: str) -> str | None:
        """서버와 thread가 달라도 한 Job 실행권은 하나만 발급한다."""
        token = uuid.uuid4().hex
        path = self._lease_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            if job_id in self._active_jobs:
                return None
            for _attempt in range(2):
                try:
                    descriptor = os.open(
                        path,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    )
                except FileExistsError:
                    try:
                        lease = json.loads(path.read_text(encoding="utf-8"))
                        owner_pid = int(lease.get("ownerPid", -1))
                    except (OSError, ValueError, json.JSONDecodeError):
                        # O_EXCL로 파일을 만든 직후 JSON을 쓰는 아주 짧은 동안 다른 process가
                        # 빈 파일을 볼 수 있다. 새 파일을 죽은 lease로 판단해 지우면 두 실행이
                        # 동시에 같은 Job을 차지한다. 최근 파일은 작성 중인 것으로 보고 양보한다.
                        try:
                            age = time.time() - path.stat().st_mtime
                        except OSError:
                            continue
                        if age < _INCOMPLETE_LEASE_GRACE_SECONDS:
                            return None
                        owner_pid = -1
                    if _pid_is_alive(owner_pid):
                        return None
                    # 이전 서버가 죽은 뒤에도 CLI가 남아 있으면 정확한 Job marker의
                    # process tree만 종료하고 마지막 checkpoint에서 다시 시작한다.
                    self.client.terminate_orphaned_process(job_id)
                    path.unlink(missing_ok=True)
                    continue
                try:
                    payload = json.dumps(
                        {
                            "jobId": job_id,
                            "token": token,
                            "ownerPid": os.getpid(),
                            "claimedAt": _now(),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ).encode("utf-8")
                    os.write(descriptor, payload)
                finally:
                    os.close(descriptor)
                self._active_jobs.add(job_id)
                return token
        return None

    def _release_job_execution(self, job_id: str, token: str) -> None:
        """자신이 발급받은 실행권만 제거한다."""
        path = self._lease_path(job_id)
        with self.lock:
            self._active_jobs.discard(job_id)
            try:
                lease = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            if lease.get("token") == token:
                path.unlink(missing_ok=True)

    def start_warmup(self) -> bool:
        """사용자 작업용 worker를 차지하지 않는 별도 thread에서 warm-up을 시작한다."""
        if not self.settings.startup_warmup:
            return False
        with self._warmup_lock:
            if self._warmup_started:
                return False
            self._warmup_started = True
            self.warmup_executor.submit(langsmith_metrics.bind_context(self._warmup))
            return True

    def _warmup(self) -> None:
        try:
            report = self.client.warmup_runtime()
            print(f"[startup] 구현 런타임 워밍업: {report['status']}")
        except Exception as error:
            # warm-up은 첫 요청의 지연을 줄이는 보조 작업이다. 실패해도 서버 시작이나 이후
            # 사용자 요청을 막지 않고 실제 요청이 들어왔을 때 다시 준비하도록 둔다.
            print(f"[startup] 구현 런타임 워밍업 실패(요청 시 재시도): {error}")

    def _recover_pending_jobs(self) -> None:
        """서버 재시작 뒤 디스크 checkpoint에서 중단된 작업을 재개한다."""
        for path in self.settings.work_root.glob("*/easydep-job-state.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            status = record.get("status")
            if status not in {"QUEUED", "GENERATING", "PLANNING", "RUNNING"}:
                continue
            if record.get("run_root"):
                self.executor.submit(
                    langsmith_metrics.bind_context(self._run),
                    record["job_id"],
                    True,
                )
            else:
                self.executor.submit(
                    langsmith_metrics.bind_context(self._plan), record["job_id"]
                )

    def _live_lease_job_ids(self) -> set[str]:
        """현재 살아 있는 서버 lease가 가리키는 job ID만 반환한다."""
        live: set[str] = set()
        for path in self.settings.work_root.glob("*/execution-lease.json"):
            try:
                lease = json.loads(path.read_text(encoding="utf-8"))
                owner_pid = int(lease.get("ownerPid", -1))
                token = lease.get("token")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                # An incomplete lease may be mid-write during restart. Keep its
                # container until the normal claim path can resolve it safely.
                live.add(path.parent.name)
                continue
            if isinstance(token, str) and token and _pid_is_alive(owner_pid):
                live.add(path.parent.name)
        return live

    def _read(self, job_id: str) -> dict[str, Any]:
        """작업 상태 JSON을 읽으며 파일이 없으면 :class:`JobNotFound`를 발생시킨다."""
        path = self._record_path(job_id)
        if not path.is_file():
            raise JobNotFound(job_id)
        with self.lock:
            return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _with_live_generation_progress(record: dict[str, Any]) -> dict[str, Any]:
        """호스트 경로를 숨긴 채 하위 프로세스의 세부 진행 상태를 응답에 합친다."""
        if record.get("status") not in {"GENERATING", "PLANNING"}:
            return record
        job_path = record.get("job_path")
        if not isinstance(job_path, str):
            return record
        progress_path = Path(job_path).parent / "generation-progress.json"
        try:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return record
        status = progress.get("status")
        if not isinstance(status, str):
            return record
        result = dict(record)
        result["progress"] = {
            key: progress[key]
            for key in ("status", "message", "updatedAt")
            if isinstance(progress.get(key), str)
        }
        # 생성 함수가 반환하면 저장 상태가 PLANNING으로 바뀐다. 그전에는 하위 프로세스가
        # 기록한 자세한 phase를 보여 주어 긴 생성 구간을 한 상태로만 표시하지 않게 한다.
        if record.get("status") == "GENERATING" and status in {
            "VALIDATING_INPUT",
            "REUSING_GENERATED_RUN",
            "PREPARING_FEEDBACK",
            "GENERATING_SOURCES",
            "PREPARING_BUILD",
            "VERIFYING",
            "PLANNING",
        }:
            result["status"] = status
            result["updated_at"] = progress.get("updatedAt", record.get("updated_at"))
        return result

    def _write(self, record: dict[str, Any]) -> None:
        """작업 상태를 UTF-8 JSON으로 쓰고 가능한 경우 원자적으로 교체한다."""
        path = self._record_path(record["job_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            payload = json.dumps(record, ensure_ascii=False, indent=2)
            # 고정된 .tmp 이름은 다른 서버 프로세스나 백신이 파일을 여는 순간 충돌할 수 있다.
            # 매번 고유한 임시 파일을 만들고 os.replace를 재시도한다. Windows가 기존 파일의
            # 교체만 잠시 거부하면 마지막 수단으로 대상 파일에 직접 덮어쓴다.
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(payload, encoding="utf-8")
                last_error: PermissionError | None = None
                for attempt in range(3):
                    try:
                        os.replace(temporary, path)
                        last_error = None
                        break
                    except PermissionError as error:
                        last_error = error
                        if attempt < 2:
                            time.sleep(0.05 * (attempt + 1))
                if last_error is not None:
                    # 일부 Windows 파일 공유 설정은 기존 파일을 열어 쓰는 것은 허용하면서
                    # 교체는 거부한다. 작업 전체를 실패시키지 않고 상태 기록을 남기는 쪽을 택한다.
                    path.write_text(payload, encoding="utf-8")
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def public_record(record: dict[str, Any]) -> dict[str, Any]:
        """내부 경로와 전체 source 내용을 제거한 HTTP 응답용 작업 정보를 만든다."""
        result = {key: value for key, value in record.items() if key not in {"job_path", "run_root"}}
        workflow = result.get("workflow")
        if isinstance(workflow, dict):
            # 실행 checkpoint는 Python 이름인 ``task_id``를 그대로 저장한다. HTTP 응답은
            # 기존 frontend 계약인 ``taskId``만 이 경계에서 만든다. 내부에서 두 이름을
            # 번갈아 쓰지 않으므로 재개와 병렬 실행 코드가 단순해진다.
            public_tasks: list[dict[str, Any]] = []
            for task in workflow.get("tasks", []):
                if not isinstance(task, dict):
                    continue
                public_task = dict(task)
                task_id = public_task.pop("task_id", None)
                if task_id is not None:
                    public_task["taskId"] = task_id
                public_tasks.append(public_task)
            result["workflow"] = {**workflow, "tasks": public_tasks}
        return result

    def shutdown(self) -> None:
        """새 작업 접수를 멈추고 실행 중인 하위 프로세스와 future를 정리한다."""
        self.client.cancel_all()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.warmup_executor.shutdown(wait=False, cancel_futures=True)


worker = ImplementationWorker()
