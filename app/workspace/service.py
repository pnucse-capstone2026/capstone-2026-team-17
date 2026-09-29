from __future__ import annotations

import json
import logging
import os
import random
import re
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, cast

from fastapi import HTTPException

from app.db.models import TYPE_CLASS, TYPE_USECASE_SPEC
from app.design import progress as design_progress
from app.design.graphs.design_graph import graph as design_graph
from app.design.graphs.design_graph import has_active_session, session_status
from app.design.graphs.subgraphs import DESIGN_SPECS, DESIGN_STAGES
from app.design.observability import design_timing_context, log_design_timing
from app.design.service import (
    BatchReviseRequest,
    ReviseRequest,
    apply_deployment_endpoint_answer_session,
    apply_deployment_topology_decision_session,
    resume_design_session,
    retry_design_session,
    revise_design_element,
    revise_design_elements,
    revise_design_stage_session,
    start_design_session,
)
from app.design.services.class_diagram.models import ClassBindingStalled
from app.design.services.common.plantuml import render_plantuml
from app.design.services.common.structured import capture_llm_timings
from app.design.services.persistence_scope import erd_disposition
from app.design.validation import design_readiness_report
from app.implementation.application.jobs import (
    worker as implementation_worker,
)
from app.metrics import langsmith as langsmith_metrics
from app.repositories import artifact_repository
from app.requirements.config import settings as requirements_settings
from app.requirements.contracts.request import (
    AnalyzeRequest,
    CloudProvider,
    DeploymentPreferences,
    FeedbackEdit,
    FeedbackStage,
    InitialCloudConstraints,
    IdentitySourceAnswer,
    ResourceAnswer,
)
from app.requirements.orchestration.service import (
    analyze_requirements,
    retry_requirements_analysis,
    revise_requirements_analysis,
)
from app.requirements.resources.capability_contract import capability_resource_questions
from app.requirements.runtime import telemetry as requirements_telemetry
from app.testing.service import run_testing
from app.validation import stable_digest

from . import repository
from .actions import (
    ActionOffer,
    StagePolicy,
    action_is_offered,
    action_spec,
    blocking_findings_route,
    offered_actions,
    result_with_contract,
    validate_payload,
)
from .checkpoints import (
    checkpoint_options,
    create_checkpoint_branch,
    create_restart_branch,
)
from .contracts import RestartStage
from .conversation.agent import conversation_agent
from .conversation.context import build_conversation_context
from .conversation.contracts import (
    Clarification,
    CommandIntent,
    ConversationIntent,
    Reply,
    RevisionExecutionResult,
    RevisionInterpretation,
    RevisionPlan,
    RevisionTarget,
)
from .conversation.delivery import (
    design_revision_payload,
    implementation_revision_payload,
    repair_payload_from_testing_evidence,
    requirements_feedback_edit,
)
from .conversation.feedback_envelope import (
    BaseRevision,
    Decision,
    DecisionMeaning,
    DecisionPayload,
    DecisionPolicy,
    Question,
    QuestionOption,
    answer_option,
    free_text_decision,
    question_is_stale,
)
from .conversation.project_tools import ProjectTools
from .conversation.revision_planner import plan_revision, validate_plan
from .live_preview import live_previews

_log = logging.getLogger(__name__)

TERMINAL_JOB_STATUSES = {
    "COMPLETED",
    "FAILED",
    "INTERRUPTED",
    "CANCELLED",
    "REJECTED",
    "NEEDS_INPUT",
    "NEEDS_PLANNER",
}

# 예전의 길이 제한 표본은 진단용 내부 값으로만 남긴다. 현재 개발 기본 설정은 실제 JSON
# 응답과 reasoning을 별도 ``responseContent``·``reasoningContent`` field로 기록하므로,
# Workspace event에서도 같은 실행의 원문을 확인할 수 있다.
_PRIVATE_DESIGN_TIMING_FIELDS = frozenset({"failureContentPrefix", "failureContentSuffix"})
_DESIGN_DELIVERY_CONTEXT_FIELDS = frozenset(
    {
        "validated_target_feedbacks",
        "approved_authority_targets",
        "approved_downstream_targets",
    }
)
_NUMBERED_REQUIREMENT_LINE = re.compile(
    r"^\s*[-*]\s*\[REQ[-_ ]?\d+\]\s*(?P<text>.+?)\s*$",
    re.IGNORECASE,
)


def _initial_requirement_lines(text: str) -> list[str]:
    """Extract explicitly numbered requirements from a structured task brief.

    A pasted benchmark brief often has an introduction, section headings, and a
    separate cloud-constraint section alongside ``[REQ-01]`` items. Only the
    numbered requirement items belong in the requirements-modeling input; the
    cloud text is supplied through ``resource_constraints_text``. For ordinary
    free-form input, retain the existing non-empty-line behavior.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    numbered = [
        match.group("text").strip()
        for line in lines
        if (match := _NUMBERED_REQUIREMENT_LINE.match(line))
    ]
    return numbered or lines


def _public_design_timing_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """설계 timing 한 건을 Workspace event로 옮기고 예전 중복 표본만 제거한다."""
    return {key: value for key, value in event.items() if key not in _PRIVATE_DESIGN_TIMING_FIELDS}


def _implementation_agent_results(run_path: Path) -> list[dict[str, Any]]:
    """완료된 OpenHands 작업의 답변·수정 파일·검증·수리 이력을 읽는다."""

    execution_dir = run_path / "reports" / "agent-executions"
    results: list[dict[str, Any]] = []
    # ``*.attempt-NNN.result.json``은 이력 보관본이고 ``<task>.result.json``이 최신본이다.
    # 화면에는 작업별 최신본 하나만 보내 중복 표시를 피한다.
    for path in sorted(execution_dir.glob("*.result.json")):
        if ".attempt-" in path.name:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        results.append(
            {
                "task_id": str(payload.get("taskId") or path.name.removesuffix(".result.json")),
                "task_type": str(payload.get("taskType") or ""),
                "owner": str(payload.get("owner") or ""),
                "status": str(payload.get("status") or ""),
                "raw_response": str(payload.get("rawResponse") or ""),
                "changed_files": list(payload.get("changedFiles") or []),
                "verification": payload.get("verification")
                or payload.get("verificationEvidence")
                or {},
                "repair_history": payload.get("repairHistory") or {},
                "event_journal": str(payload.get("eventJournal") or ""),
            }
        )
    return results


def _resource_questions(
    result: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """리소스 질문 목록과 화면에서 먼저 물을 질문을 함께 고른다."""
    questions = list(result.get("resource_questions") or [])
    selected = next(
        (question for question in questions if question.get("kind") != "suggested"),
        questions[0] if questions else None,
    )
    return questions, selected


def _with_capability_handoff_questions(app_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Backfill choice cards for checkpoints saved before capability UI support.

    Workspace command results are persisted snapshots. Merely deploying the new
    presentation code would otherwise leave an already-blocked application with
    the old empty question list forever. Enrich a copy from the canonical
    artifact state; never rewrite command history during a read.
    """

    if (
        result.get("phase") != "requirements_handoff"
        or result.get("resource_question")
        or result.get("resource_questions")
    ):
        return result
    blockers = result.get("blocking_findings") or []
    if not any(
        isinstance(blocker, dict) and blocker.get("code") == "requirements.capability-contract"
        for blocker in blockers
    ):
        return result
    state = artifact_repository.load_state(app_id)
    questions = capability_resource_questions(state.get("capability_contract") or {})
    if not questions:
        return result
    enriched = dict(result)
    enriched["resource_question"] = questions[0]
    enriched["resource_questions"] = questions
    enriched["message"] = (
        f"A deployment decision is required before design can start. {questions[0]['question']}"
    )
    return enriched


# The implementation screen follows the two long-lived OpenHands owners and the
# deterministic verifier. Internal generator checkpoints remain in the job log;
# they are details of backend preparation, not additional user-facing work.
_IMPLEMENTATION_PROGRESS_PHASES = (
    ("backend", "Backend implementation"),
    ("frontend", "Frontend implementation"),
    ("integration", "Integration verification"),
)
_MAX_AUTOMATIC_SEMANTIC_REPAIR_ITERATIONS = 32
_RETRY_BASE_SECONDS = 1.0
_RETRY_MAX_SECONDS = 60.0
_RETRY_SLEEP_SLICE_SECONDS = 1.0


class WorkspaceStopRequested(Exception):
    """Internal control flow for a durable user cancellation request."""


class WorkspaceService:
    """요구사항부터 테스트까지 한 대화형 명령 흐름으로 실행하는 서비스다."""

    def __init__(self) -> None:
        workers = max(1, min(4, int(os.getenv("EASYDEP_WORKSPACE_WORKERS", "2"))))
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="easydep-workspace"
        )
        self._submission_lock = Lock()

    def startup(self) -> int:
        interrupted = repository.interrupt_unfinished()
        # Testing command payload에 고정 입력과 마지막 검사 경계를 저장하므로 같은 command를
        # 다시 실행하면 해당 경계에서 이어 간다.
        for command in repository.interrupted_testing_commands():
            self._executor.submit(self._execute, str(command["command_id"]))
        # A typed question remains durable user work.  A non-interactive
        # technical pause is different: it already has a persisted stage and
        # must resume its existing checkpoint after process recovery.
        for command in repository.interrupted_technical_retry_commands():
            result = dict(command.get("result") or {})
            if self._technical_retry_stage(command, result) is not None:
                self._executor.submit(self._execute, str(command["command_id"]))
        return interrupted

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def reconcile_implementation_command(self, app_id: str) -> dict[str, Any] | None:
        """구현 작업은 끝났지만 Workspace 명령만 남은 경우 완료 상태를 맞춘다."""
        command = repository.latest_command(app_id)
        if not command or command.get("status") not in {
            "RUNNING",
            "INTERRUPTED",
            "FAILED",
            "AWAITING_INPUT",
        }:
            return command
        if command.get("stage") != "implementation":
            return command
        if command.get("action") not in {
            "start_implementation",
            "retry_implementation",
            "rerun_implementation",
        }:
            return command
        payload = command.get("payload") or {}
        job_id = str(payload.get("job_id") or "")
        if not job_id:
            return command
        try:
            job = implementation_worker.get(job_id)
        except Exception:
            return command
        job_status = str(job.get("status") or "")
        repair_requested = (job.get("owner_repair") or {}).get("requested_at")
        started_at = command.get("started_at")
        if (
            payload.get("action_id")
            and job_status == "COMPLETED"
            and isinstance(repair_requested, str)
            and isinstance(started_at, str)
        ):
            try:
                requested = datetime.fromisoformat(repair_requested)
                started = datetime.fromisoformat(started_at)
                if requested.tzinfo is None:
                    requested = requested.replace(tzinfo=UTC)
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                if requested < started:
                    if command.get("status") == "RUNNING":
                        return command
                    stale_result = result_with_contract(
                        {**command, "status": "FAILED"},
                        {"message": "The implementation repair did not start before this command stopped."},
                    )
                    return self._finish_terminal_command(
                        str(command["command_id"]),
                        command,
                        status="FAILED",
                        result=stale_result,
                        error="The implementation repair was not scheduled.",
                    )
            except ValueError:
                pass
        # READY workflow의 완료 여부는 구현 작업 서비스가 판정하여 공개 상태를
        # COMPLETED로 바꾼다. Workspace가 그 내부 규칙을 다시 구현하지 않는다.
        if job_status != "COMPLETED":
            self._sync_implementation_progress(app_id, str(command["command_id"]), job)
            if job_status in TERMINAL_JOB_STATUSES:
                if job_status == "NEEDS_INPUT":
                    pending = self._implementation_needs_input_result(job, job_id)
                    pending.pop("awaiting_input", None)
                    pending["checkpoint_retryable"] = bool(job.get("checkpoint_retryable"))
                    result = result_with_contract(
                        {**command, "status": "AWAITING_INPUT"}, pending
                    )
                    return repository.update_command(
                        command["command_id"],
                        status="AWAITING_INPUT",
                        result=result,
                        error=None,
                    )
                result = {
                    **dict(command.get("result") or {}),
                    "job_id": job_id,
                    "job": job,
                    "checkpoint_retryable": bool(job.get("checkpoint_retryable")),
                }
                command_status = (
                    "INTERRUPTED" if job_status == "INTERRUPTED" else "FAILED"
                )
                result = result_with_contract(
                    {**command, "status": command_status}, result
                )
                return self._finish_terminal_command(
                    str(command["command_id"]),
                    command,
                    status=command_status,
                    result=result,
                    error=str(job.get("error") or "Implementation needs checkpoint repair."),
                )
            if job_status in {"QUEUED", "RUNNING"} and command.get("status") != "RUNNING":
                return repository.update_command(
                    str(command["command_id"]),
                    status="RUNNING",
                    error=None,
                    completed_at=None,
                )
            return command
        result = {
            "message": "Review the generated implementation artifacts below.",
            "job_id": job_id,
            "job": job,
            "review_artifacts": True,
        }
        result = result_with_contract(
            {**command, "status": "COMPLETED"}, result
        )
        updated = self._finish_terminal_command(
            str(command["command_id"]),
            command,
            status="COMPLETED",
            result=result,
            error=None,
        )
        repository.notify_command_changed(
            app_id,
            command_id=command["command_id"],
            stage="implementation",
        )
        # Rebuild the final three-phase progress view from the durable job state.
        # The completed card itself is projected from the updated command above.
        self._sync_implementation_progress(app_id, str(command["command_id"]), job)
        return updated

    def _sync_implementation_progress(
        self, app_id: str, command_id: str, job: dict[str, Any]
    ) -> None:
        """재시작 뒤에도 저장된 job 상태를 Workspace 진행 이벤트로 복원한다."""
        previous_updates: dict[str, str] = {}
        for event in repository.list_progress_events(app_id):
            if (
                event.get("command_id") != command_id
                or event.get("stage") != "implementation"
                or event.get("kind") != "progress"
            ):
                continue
            metadata = event.get("metadata") or {}
            step = str(metadata.get("step") or "")
            if step:
                previous_updates[step] = "|".join(
                    str(metadata.get(field) or "")
                    for field in (
                        "progress_status",
                        "progress_step_label",
                        "progress_detail",
                        "current_file",
                        "current_class",
                        "recent_command",
                        "verification_status",
                        "repairing",
                    )
                )
        progress = self._implementation_progress_snapshot(job)
        for update in progress.get("updates", []) if progress else []:
            if not isinstance(update, dict):
                continue
            step = str(update.get("step") or "")
            if not step:
                continue
            label = str(update.get("label") or step)
            detail = str(update.get("detail") or "")
            status = str(update.get("status") or "running")
            key = "|".join(
                (
                    status,
                    label,
                    detail,
                    *(str(update.get(field) or "") for field in (
                        "current_file",
                        "current_class",
                        "recent_command",
                        "verification_status",
                        "repairing",
                    )),
                )
            )
            if previous_updates.get(step) == key:
                continue
            repository.append_progress_event(
                app_id,
                command_id=command_id,
                stage="implementation",
                text=detail or label,
                metadata={
                    "progress_event": "implementationStepUpdated",
                    "step": step,
                    "progress_step_label": label,
                    "progress_card_label": str(
                        progress.get("progress_card_label") or "Implementation progress"
                    ),
                    "progress_detail": detail,
                    "progress_status": status,
                    **{
                        key: update[key]
                        for key in (
                            "implementation_owner",
                            "current_file",
                            "current_class",
                            "recent_command",
                            "verification_status",
                            "repairing",
                        )
                        if key in update
                    },
                },
            )

    @staticmethod
    def _sequence_target_feedbacks(context: dict[str, Any]) -> list[ReviseRequest]:
        """Parse UI-provided, per-UC feedback without inferring any target."""
        raw_entries = context.get("target_feedbacks")
        if raw_entries is None:
            return []
        if not isinstance(raw_entries, list) or not raw_entries:
            raise ValueError("Select at least one sequence-diagram feedback target.")
        revisions: list[ReviseRequest] = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise TypeError("Each sequence feedback entry must name a target and feedback.")
            target = str(raw.get("target") or "").strip()
            feedback = str(raw.get("feedback") or "").strip()
            if not target.startswith("sequence_diagram:") or target == "sequence_diagram:":
                raise ValueError("Sequence feedback targets must be selected use-case diagrams.")
            if not feedback:
                raise ValueError(f"Feedback for {target} cannot be empty.")
            revisions.append(ReviseRequest(target=target, feedback=feedback))
        # Pydantic applies duplicate-target validation at the command boundary
        # before any individual revision can run.
        return BatchReviseRequest(revisions=revisions).revisions

    def _fixed_class_resource_choice(
        self,
        app_id: str,
        payload: dict[str, Any],
        latest: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Validate one server-pinned class choice without interpreting it again.

        A resource question normally represents ordinary prose input.  The narrow
        exception is a Design class-gate question whose server-provided context
        pins one catalog target and version.  Its fixed choices are already typed
        UI input; only free text continues to ConversationAgent.
        """

        action_id = str(payload.get("action_id") or "")
        context = payload.get("context")
        if not action_id or not isinstance(context, dict):
            return None
        offered_context = {
            key: value
            for key, value in context.items()
            if key not in _DESIGN_DELIVERY_CONTEXT_FIELDS
        }
        raw_target = offered_context.get("validated_target")
        if raw_target is None:
            return None
        pending = repository.get_command(action_id)
        if (
            pending is None
            or pending.get("app_id") != app_id
            or pending.get("command_id") != latest.get("command_id")
            or pending.get("status") != "AWAITING_INPUT"
            or pending.get("stage") != "design"
        ):
            raise ValueError("This class question is no longer the current design question.")
        status = session_status(app_id)
        if not status.get("active") or status.get("stage") != "class_diagram":
            raise ValueError("The pinned class question is no longer at the class review gate.")
        text = str(payload.get("text") or "").strip()
        offers = offered_actions(pending)
        fixed_choice = any(
            str(offer.action) == "message"
            and "text" in offer.payload
            and offer.payload.get("text") == text
            and offer.payload.get("context") == offered_context
            for offer in offers
        )
        try:
            target = RevisionTarget.model_validate(raw_target)
        except (TypeError, ValueError) as error:
            raise ValueError("The class question target is invalid.") from error
        if (
            target.owner != "design"
            or target.artifact_type != TYPE_CLASS
            or target.kind not in {"class", "operation"}
            or target.ref != str(offered_context.get("element_ref") or "").strip()
            or target.artifact_version_id is None
        ):
            raise ValueError("The question must pin one editable design class target.")
        current = ProjectTools(app_id).current_revision_target(target)
        if (
            current is None
            or current.ref != target.ref
            or current.artifact_version_id != target.artifact_version_id
            or current.owner != "design"
            or current.artifact_type != TYPE_CLASS
            or current.kind not in {"class", "operation"}
        ):
            raise ValueError("The pinned class target version is stale.")
        if not fixed_choice:
            # A matching pinned context with different prose is the explicitly
            # offered free-text path, so leave it for normal interpretation.
            if any(
                str(offer.action) == "message"
                and "text" not in offer.payload
                and offer.payload.get("context") == offered_context
                for offer in offers
            ):
                return None
            raise ValueError("The submitted class answer does not match a fixed choice.")
        return {
            "validated_target_feedbacks": [
                ReviseRequest(
                    target=target.ref,
                    feedback=text,
                    approved_authority_targets=[target.ref],
                    approved_downstream_targets=None,
                ).model_dump(mode="json")
            ],
            "approved_authority_targets": [target.ref],
            "approved_downstream_targets": [],
        }

    def submit(
        self,
        app_id: str,
        *,
        action: str,
        payload: dict[str, Any],
        stage: str | None = None,
    ) -> dict[str, Any]:
        artifact_repository.ensure_app_exists(app_id)
        action, payload, stage = self._prepare_conversational_message(
            app_id,
            action=action,
            payload=payload,
            stage=stage,
        )
        # 분기와 재실행은 원본 앱의 개발 상태를 바꾸지 않는다. 관리 명령이 최신 명령으로
        # 보이더라도 그전에 제공되던 진행 버튼을 그대로 유지한다.
        if action in {"branch_checkpoint", "rerun_from_stage"}:
            previous = repository.latest_command(app_id)
            if previous is not None:
                payload = {
                    **payload,
                    "_conversation_actions": [
                        item.model_dump(mode="json", exclude_none=True)
                        for item in offered_actions(previous)
                    ],
                }
                stage = stage or str(previous.get("stage") or "requirements")
        resolved_stage = stage or self.infer_stage(app_id, action, payload)
        self._validate_payload(action, payload)
        self._validate_action_reference(app_id, action, payload)
        command_id = str(uuid.uuid4())
        with self._submission_lock:
            command = repository.create_command(command_id, app_id, action, resolved_stage, payload)

        self._executor.submit(self._execute, command_id)
        return command

    def _prepare_conversational_message(
        self,
        app_id: str,
        *,
        action: str,
        payload: dict[str, Any],
        stage: str | None,
    ) -> tuple[str, dict[str, Any], str | None]:
        """실행 command를 만들기 전에 자연어 발화를 해석한다.

        명시적 버튼과 리소스 답변은 LLM을 거치지 않는다. UI가 대상을 고른 수정은 target을
        다시 추측하지 않고 의미 범위만 structured output으로 해석한다. 자연어 command는
        backend가 이미 공개한 action과 payload로 바꾸고 답변과 clarification은 전문 단계를
        실행하지 않는 message command로 남긴다.
        """

        if action != "message":
            return action, payload, stage
        # This marker is created only below, after matching a stored server offer.
        # Never accept a client-provided copy as evidence that an action was offered.
        payload = dict(payload)
        for field in (
            "_resource_answer_context",
            "_conversation_actions",
            "_conversation_outcome",
            "conversation_intent",
            "revision_interpretation",
            "revision_plan",
            "validated_impact",
            "validated_targets",
            "identity_source_answer",
        ):
            payload.pop(field, None)
        text = str(payload.get("text") or "").strip()
        latest = repository.latest_command(app_id)
        if latest is None:
            return action, payload, stage
        selected = payload.get("context") or {}
        if not text and selected.get("target_feedbacks") is None:
            return action, payload, stage
        if latest.get("status") in repository.ACTIVE_STATUSES:
            raise RuntimeError(
                f"An active workspace command already exists: {latest['command_id']}"
            )
        fixed_class_context = self._fixed_class_resource_choice(app_id, payload, latest)
        if fixed_class_context is not None:
            offered_context = {
                key: value
                for key, value in selected.items()
                if key not in _DESIGN_DELIVERY_CONTEXT_FIELDS
            }
            return (
                "message",
                {
                    **payload,
                    "_resource_answer_context": {
                        **offered_context,
                        "server_pinned_answer": True,
                    },
                    "context": {**selected, **fixed_class_context},
                },
                "design",
            )
        pending = repository.get_command(str(payload.get("action_id") or ""))
        if pending is not None and pending.get("status") == "AWAITING_INPUT":
            pending_result = pending.get("result") or {}
            raw_identity_question = pending_result.get("identity_source_question")
            if isinstance(raw_identity_question, dict):
                return self._route_identity_source_answer(
                    app_id, payload, stage, latest, pending, raw_identity_question
                )
            pending_question = pending_result.get("feedback_question")
            if pending_question is None and isinstance(pending_result.get("validation"), dict):
                pending_question = pending_result["validation"].get("feedback_question")
            if isinstance(pending_question, dict):
                return self._route_feedback_question_answer(
                    app_id, payload, stage, latest, pending, pending_question
                )
            resource_question = pending_result.get("resource_question")
            resource_field = (
                str(resource_question.get("field") or "")
                if isinstance(resource_question, dict)
                else ""
            )
            if (
                pending.get("app_id") == app_id
                and pending.get("stage") == "design"
                and resource_field.startswith("connectionEndpoint:")
            ):
                # This is a server-pinned value intake, not revision prose.
                # Preserve the complete question envelope for the Design
                # handler, which verifies its connection/source/digest again.
                return (
                    "message",
                    {
                        **payload,
                        "_resource_answer_context": {
                            "connection_endpoint_question": dict(resource_question),
                            "server_pinned_answer": True,
                        },
                    },
                    "design",
                )
            resource_choice_values = {
                str(choice.get("value") or "").strip()
                for choice in (
                    resource_question.get("choices") or []
                    if isinstance(resource_question, dict)
                    else []
                )
                if isinstance(choice, dict) and str(choice.get("value") or "").strip()
            }
            if (
                pending.get("app_id") == app_id
                and isinstance(resource_question, dict)
                and (
                    text in resource_choice_values
                    or resource_question.get("allowFreeText") is True
                )
            ):
                # Resource questions are server-pinned input contracts.  Keep
                # them separate from ordinary feedback, which must be planned.
                return (
                    "message",
                    {
                        **payload,
                        "_resource_answer_context": {"server_pinned_answer": True},
                    },
                    str(pending.get("stage") or stage or "requirements"),
                )
        explicit_instructions: dict[str, str] = {}
        element_ref = str(selected.get("element_ref") or "").strip()
        if element_ref and text:
            explicit_instructions[element_ref] = text
        if selected.get("target_feedbacks") is not None:
            for revision in self._sequence_target_feedbacks(dict(selected)):
                explicit_instructions[revision.target] = revision.feedback
        if explicit_instructions:
            combined_instruction = (
                next(iter(explicit_instructions.values()))
                if len(explicit_instructions) == 1
                else "\n".join(
                    f"{target}: {instruction}"
                    for target, instruction in explicit_instructions.items()
                )
            )
            try:
                conversation_context = build_conversation_context(app_id)
                conversation_context.workspace["selection"] = dict(selected)
                outcome = conversation_agent.interpret_revision(
                    combined_instruction,
                    list(explicit_instructions),
                    tools=ProjectTools(app_id),
                    context=conversation_context,
                    sealed_targets=True,
                )
            except Exception:
                _log.exception("Failed to interpret selected revision feedback")
                outcome = Clarification(
                    question=(
                        "I could not interpret that revision right now. "
                        "Please retry with the same selected target."
                    )
                )
            explicit_payload = {
                **payload,
                "text": combined_instruction,
                "revision_instructions": explicit_instructions,
            }
            if isinstance(outcome, Clarification):
                return self._clarification_message(
                    explicit_payload, outcome, stage, latest
                )
            if not isinstance(outcome, CommandIntent):
                raise ValueError("Selected revision feedback did not produce a command intent.")
            if len(explicit_instructions) == 1:
                target = next(iter(explicit_instructions))
                explicit_payload["revision_instructions"] = {target: outcome.instruction}
                pinned_revision = (
                    outcome.revision.model_copy(update={"targets": [target]})
                    if outcome.revision is not None
                    else None
                )
                outcome = outcome.model_copy(
                    update={"targets": [target], "revision": pinned_revision}
                )
            return self._route_conversation_intent(
                app_id, explicit_payload, outcome, latest
            )

        action_id = str(payload.get("action_id") or "")
        prior = repository.get_command(action_id) if action_id else latest
        repair_anchor = (
            prior
            if prior is not None
            and prior.get("command_id") == action_id
            and prior.get("app_id") == app_id
            and prior.get("status") == "AWAITING_INPUT"
            and prior.get("stage")
            and isinstance((prior.get("result") or {}).get("repair_state"), dict)
            else None
        )
        actionable = self._conversation_action_anchor(app_id, repair_anchor or latest)
        try:
            conversation_context = build_conversation_context(app_id)
            conversation_context.workspace["selection"] = dict(selected)
            # A message tied to the still-open repair command inherits that
            # command's stage as conversational context. This is deliberately
            # keyed by the server record, never by client supplied stage text.
            if repair_anchor is not None:
                conversation_context.workspace["stage"] = str(repair_anchor["stage"])
            outcome = conversation_agent.respond(
                app_id,
                text,
                conversation_context,
                tools=ProjectTools(app_id),
            )
        except Exception:
            _log.exception("Failed to interpret a workspace conversation message")
            outcome = Clarification(
                question=(
                    "I could not interpret that message right now. "
                    "Please retry or use one of the available actions."
                )
            )
        if isinstance(outcome, Reply):
            referenced_stage = (
                str(prior.get("stage") or "requirements")
                if action_id and prior is not None and prior.get("app_id") == app_id
                else ""
            )
            return (
                "message",
                {
                    **payload,
                    "_conversation_actions": [
                        item.model_dump(mode="json", exclude_none=True)
                        for item in self._resolved_offered_actions(app_id, actionable)
                    ],
                    "_conversation_outcome": {
                        "kind": "reply",
                        **outcome.model_dump(mode="json"),
                    },
                },
                stage or referenced_stage or str(latest.get("stage") or "requirements"),
            )
        if isinstance(outcome, Clarification):
            return self._clarification_message(payload, outcome, stage, actionable)
        return self._route_conversation_intent(app_id, payload, outcome, actionable)

    def _conversation_action_anchor(
        self, app_id: str, command: dict[str, Any]
    ) -> dict[str, Any]:
        """Follow completed conversation records back to their workflow gate."""

        current = command
        visited: set[str] = set()
        while len(visited) < 12:
            command_id = str(current.get("command_id") or "")
            if not command_id or command_id in visited:
                break
            visited.add(command_id)
            conversation = (current.get("result") or {}).get("conversation")
            if not (
                isinstance(conversation, dict)
                and (conversation.get("clarification") or conversation.get("reply"))
            ):
                break
            referenced_id = str((current.get("payload") or {}).get("action_id") or "")
            referenced = repository.get_command(referenced_id) if referenced_id else None
            if (
                referenced is None
                or referenced_id == command_id
                or referenced.get("app_id") != app_id
            ):
                break
            current = referenced
        return current

    def _action_snapshot(self, app_id: str, command: dict[str, Any]) -> dict[str, Any]:
        """Return the one readiness-refreshed workflow snapshot for an action."""

        anchor = self._conversation_action_anchor(app_id, command)
        result = anchor.get("result")
        shaped_result = dict(result) if isinstance(result, dict) else {}
        shaped_result.pop("_current_implementation_checkpoint_retryable", None)
        if str(anchor.get("stage") or "") == "design":
            shaped_result = self._with_design_progress_hints(app_id, shaped_result)
        if (
            str(anchor.get("stage") or "") == "implementation"
            and str(anchor.get("status") or "") in {"FAILED", "CANCELLED"}
        ):
            result_job = shaped_result.get("job")
            result_job = result_job if isinstance(result_job, dict) else {}
            job_id = str(
                (anchor.get("payload") or {}).get("job_id")
                or shaped_result.get("job_id")
                or result_job.get("job_id")
                or ""
            )
            if job_id:
                try:
                    job = implementation_worker.get(job_id)
                except Exception:
                    job = {}
                if (
                    str(job.get("app_id") or "") == app_id
                    and str(job.get("job_id") or job_id) == job_id
                    and str(job.get("status") or "")
                    in {"FAILED", "INTERRUPTED", "NEEDS_PLANNER", "NEEDS_INPUT", "CANCELLED"}
                    and bool(job.get("checkpoint_retryable"))
                ):
                    shaped_result[
                        "_current_implementation_checkpoint_retryable"
                    ] = True
        if (
            str(anchor.get("stage") or "") == "testing"
            and str(anchor.get("status") or "")
            in {"FAILED", "INTERRUPTED", "AWAITING_INPUT"}
        ):
            implementation_job_id = str(
                (anchor.get("payload") or {}).get("implementation_job_id") or ""
            )
            if implementation_job_id:
                try:
                    job = implementation_worker.get(implementation_job_id)
                except Exception:
                    job = {}
                if str(job.get("app_id") or "") == app_id:
                    shaped_result["_linked_implementation_checkpoint_retryable"] = bool(
                        job.get("checkpoint_retryable")
                    )
        return {**anchor, "result": shaped_result}

    def _resolved_offered_actions(
        self, app_id: str, command: dict[str, Any]
    ) -> list[ActionOffer]:
        return offered_actions(self._action_snapshot(app_id, command))

    def _clarification_message(
        self,
        payload: dict[str, Any],
        outcome: Clarification,
        stage: str | None,
        latest: dict[str, Any],
    ) -> tuple[str, dict[str, Any], str | None]:
        resolved_actions = self._resolved_offered_actions(
            str(latest.get("app_id") or ""), latest
        )
        offered_message_id = next(
            (
                str(offer.payload.get("action_id") or "")
                for offer in resolved_actions
                if str(offer.action) == "message"
                and str(offer.payload.get("action_id") or "")
            ),
            "",
        )
        preserved_actions = [
            item.model_dump(mode="json", exclude_none=True)
            for item in resolved_actions
        ]
        return (
            "message",
            {
                **payload,
                "_conversation_actions": preserved_actions,
                "action_id": (
                    offered_message_id
                    or payload.get("action_id")
                    or latest["command_id"]
                ),
                "_conversation_outcome": {
                    "kind": "clarification",
                    **outcome.model_dump(mode="json"),
                },
            },
            stage or str(latest.get("stage") or "requirements"),
        )

    @staticmethod
    def _revision_action_anchor(
        app_id: str,
        owner: str,
        command: dict[str, Any],
    ) -> dict[str, Any]:
        """Follow preserved conversation actions back to the editable stage gate."""

        current = command
        visited: set[str] = set()
        while len(visited) < 12:
            command_id = str(current.get("command_id") or "")
            if not command_id or command_id in visited:
                break
            visited.add(command_id)
            conversation = (current.get("result") or {}).get("conversation")
            if isinstance(conversation, dict) and conversation.get("clarification"):
                referenced_id = str(
                    (current.get("payload") or {}).get("action_id") or ""
                )
                referenced = repository.get_command(referenced_id)
                if (
                    referenced_id
                    and referenced_id != command_id
                    and referenced is not None
                    and referenced.get("app_id") == app_id
                    and referenced.get("stage") == owner
                ):
                    current = referenced
                    continue
            message_offer = next(
                (
                    offer
                    for offer in offered_actions(current)
                    if str(offer.action) == "message"
                    and str(offer.payload.get("action_id") or "")
                ),
                None,
            )
            if message_offer is None:
                break
            referenced_id = str(message_offer.payload.get("action_id") or "")
            if referenced_id == command_id:
                break
            referenced = repository.get_command(referenced_id)
            if (
                referenced is None
                or referenced.get("app_id") != app_id
                or referenced.get("stage") != owner
            ):
                break
            current = referenced
        return current

    def _route_conversation_intent(
        self,
        app_id: str,
        payload: dict[str, Any],
        intent: CommandIntent,
        latest: dict[str, Any],
    ) -> tuple[str, dict[str, Any], str | None]:
        """자연어 의도를 공개 offer와 검증된 프로젝트 ref로 연결한다."""

        offered = self._resolved_offered_actions(app_id, latest)
        intent_name = str(intent.intent)
        if intent_name in {
            ConversationIntent.BRANCH.value,
            ConversationIntent.RERUN.value,
        }:
            option_kind = "branch" if intent_name == ConversationIntent.BRANCH.value else "rerun"
            choices = checkpoint_options(app_id)[option_kind]
            available = {
                str(item["stage"])
                for item in choices
                if item.get("available") is True
            }
            if intent.stage not in available:
                return self._clarification_message(
                    payload,
                    Clarification(
                        question="Choose a stage that has all required checkpoint artifacts.",
                        candidates=[
                            str(item["label"])
                            for item in choices
                            if item.get("available") is True
                        ],
                    ),
                    None,
                    latest,
                )
            field = "checkpoint_stage" if option_kind == "branch" else "restart_stage"
            action = "branch_checkpoint" if option_kind == "branch" else "rerun_from_stage"
            return (
                action,
                {
                    **payload,
                    field: intent.stage,
                    "conversation_intent": intent.model_dump(mode="json"),
                },
                None,
            )
        if intent_name == ConversationIntent.REVISE.value:
            tools = ProjectTools(app_id)
            interpretation = intent.revision
            if interpretation is None:
                return self._clarification_message(
                    payload,
                    Clarification(
                        question=(
                            "Please clarify whether this changes presentation, a contract, "
                            "behavior, implementation, or a test expectation."
                        ),
                        candidates=[],
                    ),
                    None,
                    latest,
                )
            plan = plan_revision(tools, interpretation)
            if plan.status in {"needs_clarification", "unsupported"}:
                return self._clarification_message(
                    {
                        **payload,
                        "conversation_intent": intent.model_dump(mode="json"),
                        "revision_interpretation": interpretation.model_dump(mode="json"),
                        "revision_plan": plan.model_dump(mode="json"),
                    },
                    Clarification(
                        question=plan.explanation,
                        candidates=[
                            target.display_label
                            for target in plan.upstream_candidates
                        ],
                    ),
                    None,
                    latest,
                )
            if plan.status != "needs_confirmation":
                return self._clarification_message(
                    payload,
                    Clarification(
                        question="The revision plan is not awaiting explicit confirmation.",
                        candidates=[],
                    ),
                    None,
                    latest,
                )
            execution_targets = plan.authority_targets or plan.requested_targets
            owners = {target.owner for target in execution_targets}
            if len(owners) != 1:
                return self._clarification_message(
                    payload,
                    Clarification(
                        question="Select targets owned by one delivery stage.",
                        candidates=[target.display_label for target in execution_targets],
                    ),
                    None,
                    latest,
                )
            owner = owners.pop()
            repair_linked = (
                latest.get("app_id") == app_id
                and latest.get("status") == "AWAITING_INPUT"
                and latest.get("command_id") == str(payload.get("action_id") or "")
                and isinstance((latest.get("result") or {}).get("repair_state"), dict)
            )
            if repair_linked and owner != str(latest.get("stage") or ""):
                source_text = str(payload.get("text") or "").casefold()
                explicit_cross_stage = any(
                    re.search(
                        rf"(?<![\w:]){re.escape(target.ref.casefold())}(?!\w)",
                        source_text,
                    )
                    is not None
                    for target in execution_targets
                )
                if not explicit_cross_stage:
                    return self._clarification_message(
                        payload,
                        Clarification(
                            question=(
                                "This feedback appears to target another stage. "
                                "Name that stage or target explicitly, or restate the "
                                "change for the open repair."
                            ),
                            candidates=[target.display_label for target in execution_targets],
                        ),
                        None,
                        latest,
                    )
            owner_command = repository.latest_command(app_id, stage=owner)
            if owner_command is not None:
                owner_command = self._revision_action_anchor(
                    app_id, owner, owner_command
                )
                if not any(
                    str(offer.action) == "message"
                    and str(offer.payload.get("action_id") or "")
                    for offer in offered_actions(owner_command)
                ):
                    owner_command = None
            valid_refs = [target.ref for target in execution_targets]
            targets = [target.model_dump(mode="json") for target in execution_targets]
            routed_payload = {
                **payload,
                "text": intent.instruction,
                "conversation_intent": intent.model_dump(mode="json"),
                "revision_interpretation": interpretation.model_dump(mode="json"),
                "revision_plan": plan.model_dump(mode="json"),
                "validated_targets": targets,
                "validated_impact": tools.trace_impact(valid_refs, view="editing"),
            }
            if owner_command is not None:
                routed_payload["action_id"] = str(owner_command.get("command_id") or "")
            else:
                routed_payload.pop("action_id", None)
            decomposed_instructions = {
                item.target: item.instruction
                for item in interpretation.target_instructions
                if item.target in valid_refs
            }
            if decomposed_instructions:
                routed_payload["revision_instructions"] = decomposed_instructions
            if owner == "implementation" and any(
                target.kind == "finding" for target in plan.requested_targets
            ):
                finding = next(
                    target for target in plan.requested_targets if target.kind == "finding"
                )
                evidence = tools.read_element(finding.ref).get("content") or {}
                repair_payload_from_testing_evidence(evidence, execution_targets)
            routed_payload["_conversation_outcome"] = {"kind": "revision_plan"}
            return "message", routed_payload, owner

        if intent_name == ConversationIntent.ANSWER.value:
            return self._clarification_message(
                payload,
                Clarification(
                    question=(
                        "This free-text response is not a server-pinned question answer. "
                        "Describe the requested change so it can be reviewed before applying."
                    ),
                    candidates=[],
                ),
                None,
                latest,
            )

        action_candidates = {
            ConversationIntent.ADVANCE.value: {
                "advance",
                "start_design",
                "start_implementation",
                "start_testing",
            },
            ConversationIntent.CONFIRM_REVISION.value: {"confirm_change"},
            ConversationIntent.DISMISS_REVISION.value: {"dismiss_change"},
        }.get(intent_name, set())
        offer = next(
            (item for item in offered if str(item.action) in action_candidates),
            None,
        )
        if offer is None:
            return self._clarification_message(
                payload,
                Clarification(
                    question=(
                        "That action is not available in the current state. "
                        "Choose one of the available actions."
                    ),
                    candidates=[str(item.label) for item in offered],
                ),
                None,
                latest,
            )
        routed_payload = {
            **payload,
            **dict(offer.payload),
            "conversation_intent": intent.model_dump(mode="json"),
        }
        if str(offer.action) == "message":
            routed_payload["text"] = intent.instruction
        return str(offer.action), routed_payload, None

    def apply_saved_deployment_preferences(self, app_id: str) -> dict[str, Any] | None:
        """Resume only a requirements gate that is waiting for cloud coordinates."""
        preferences = repository.get_deployment_preferences(app_id)
        latest = repository.latest_command(app_id)
        if not preferences or latest is None:
            return None
        result = latest.get("result") or {}
        questions = list(result.get("resource_questions") or [])
        fields = {str(question.get("field") or "") for question in questions}
        if (
            latest.get("stage") != "requirements"
            or latest.get("status") != "AWAITING_INPUT"
            or not fields.intersection({"provider", "region"})
        ):
            return None
        try:
            target_labels = [
                str(target.get("provider") or "").upper()
                + " "
                + str(target.get("region") or "")
                for target in preferences.get("targets") or []
                if isinstance(target, dict)
            ]
            return self.submit(
                app_id,
                action="apply_deployment_preferences",
                payload={
                    "action_id": latest["command_id"],
                    "deployment_preferences": preferences,
                    "_timeline_text": (
                        "Deployment alternatives selected: " + ", ".join(target_labels)
                        if target_labels
                        else "Deployment preferences updated."
                    ),
                },
                stage="requirements",
            )
        except RuntimeError:
            # A concurrent caller may already have queued the same resume operation.
            return None

    def sync_deployment_configuration(
        self, app_id: str, design_result: dict[str, Any]
    ) -> None:
        """Sync the Workspace command whose deployment gate was resumed externally."""

        if design_result.get("status") not in {"completed", "need_feedback"}:
            return
        latest = repository.latest_command(
            app_id,
            stage="design",
            status="AWAITING_INPUT",
        )
        if (
            latest is None
            or latest.get("stage") != "design"
            or latest.get("status") != "AWAITING_INPUT"
            or not (latest.get("result") or {}).get(
                "deployment_configuration_required"
            )
        ):
            return
        visible = self._design_result(design_result)
        awaiting_input = visible.pop("awaiting_input", False) is True
        status = "AWAITING_INPUT" if awaiting_input else "COMPLETED"
        visible = result_with_contract(
            {**latest, "status": status}, visible
        )
        repository.update_command(
            latest["command_id"],
            status=status,
            result=visible,
            completed_at=None if awaiting_input else repository.now(),
            error=None,
        )
        repository.notify_command_changed(
            app_id,
            command_id=latest["command_id"],
            stage="design",
        )

    def present_command(self, app_id: str, command: dict[str, Any] | None) -> dict[str, Any] | None:
        """Return a display-ready command without mutating its stored snapshot."""

        if command is None:
            return None
        presented = dict(command)
        payload = command.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        result = command.get("result")
        shaped_result = dict(result) if isinstance(result, dict) else {}
        if str(command.get("stage") or "") == "design":
            shaped_result = self._with_design_progress_hints(app_id, shaped_result)
        shaped_result = _with_capability_handoff_questions(app_id, shaped_result)
        presented["result"] = result_with_contract(presented, shaped_result)
        presented["result"]["actions"] = [
            item.model_dump(mode="json", exclude_none=True)
            for item in self._resolved_offered_actions(app_id, command)
        ]
        return presented

    @staticmethod
    def _validate_payload(action: str, payload: dict[str, Any]) -> None:
        validate_payload(action, payload)

    def _validate_action_reference(self, app_id: str, action: str, payload: dict[str, Any]) -> None:
        action_id = str(payload.get("action_id") or "")
        if not action_id:
            return
        prior = repository.get_command(action_id)
        if prior is None or prior["app_id"] != app_id:
            raise ValueError("The command to answer could not be found.")
        offered_context = payload.get("_resource_answer_context")
        if action == "message" and isinstance(offered_context, dict):
            offered_payload = {
                **payload,
                "context": {
                    key: value
                    for key, value in offered_context.items()
                    if key != "server_pinned_answer"
                },
            }
            if action_is_offered(
                action, offered_payload, self._action_snapshot(app_id, prior)
            ):
                return
        # 저장된 배포 선택은 내부 재개 trigger다. 같은 질문에 답하지만 choice text 대신
        # 구조화된 값을 전달한다.
        if action == "apply_deployment_preferences":
            result = prior.get("result") or {}
            fields = {
                str(question.get("field") or "")
                for question in result.get("resource_questions") or []
                if isinstance(question, dict)
            }
            if prior["status"] != "AWAITING_INPUT" or not fields.intersection(
                {"provider", "region"}
            ):
                raise ValueError("Deployment preferences do not answer this command.")
            return
        if not action_is_offered(action, payload, self._action_snapshot(app_id, prior)):
            raise ValueError("This action is not currently offered for the referenced command.")

    def infer_stage(self, app_id: str, action: str, payload: dict[str, Any]) -> str:
        policy = action_spec(action).stage_policy
        if policy in {
            StagePolicy.REQUIREMENTS,
            StagePolicy.DESIGN,
            StagePolicy.IMPLEMENTATION,
            StagePolicy.TESTING,
        }:
            return policy.value
        if policy == StagePolicy.REFERENCE:
            prior = repository.get_command(str(payload.get("action_id") or ""))
            if prior is not None:
                return str(
                    (prior.get("result") or {}).get("routing_stage")
                    or prior.get("stage")
                    or "requirements"
                )
        latest = repository.latest_command(app_id)
        if action == "message" and payload.get("action_id"):
            prior = repository.get_command(str(payload["action_id"]))
            if prior is not None:
                return str(
                    (prior.get("result") or {}).get("routing_stage")
                    or prior.get("stage")
                    or "requirements"
                )
        state = artifact_repository.load_state(app_id)
        if not state.get("refined_requirements") or (
            latest is not None
            and latest["stage"] == "requirements"
            and latest["status"] == "AWAITING_INPUT"
        ):
            return "requirements"
        if not state.get("deployment_diagram_puml") or has_active_session(app_id):
            return "design"
        return "implementation"

    def _execute(self, command_id: str) -> None:
        command = repository.get_command(command_id)
        if command is None:
            return
        app_id = str(command["app_id"])
        stage = str(command["stage"])
        action = str(command["action"])
        try:
            with langsmith_metrics.trace_scope(
                f"easydep.workspace.{stage}",
                metadata={
                    "thread_id": app_id,
                    "app_id": app_id,
                    "command_id": command_id,
                    "stage": stage,
                    "action": action,
                    "agent": stage,
                    "operation": "workspace_command",
                },
            ):
                self._execute_command(command_id, command)
        except Exception:
            # ``_execute_command`` has already stored the failure for the UI.
            # Letting the exception leave the trace scope marks the LangSmith
            # root run as failed; the background executor must not re-raise it.
            return

    def _execute_command(self, command_id: str, command: dict[str, Any]) -> None:
        """Execute a persisted command inside its Workspace LangSmith trace."""

        app_id = str(command["app_id"])
        stage = str(command["stage"])
        if self._stop_requested(command_id):
            self._cancel_command(command_id, command, stage)
            return
        repository.update_command(
            command_id,
            status="RUNNING",
            started_at=repository.now(),
            completed_at=None,
            error=None,
        )
        repository.notify_command_changed(
            app_id,
            command_id=command_id,
            stage=stage,
        )
        try:
            try:
                result = self._dispatch_with_transient_retry(command)
            except ClassBindingStalled as error:
                result = self._class_binding_stall_result(command, error)
            if self._stop_requested(command_id):
                self._cancel_command(command_id, command, stage)
                return
            result = self._auto_repair_semantic_result(command, result)
            if self._stop_requested(command_id):
                self._cancel_command(command_id, command, stage)
                return
            if stage == "design":
                result = self._with_design_progress_hints(app_id, result)
            feedback_command = self._feedback_question_command(command)
            if feedback_command is not None:
                if not result.get("stale_revision_plan"):
                    self._complete_feedback_question_source(feedback_command)
            elif self._identity_source_answer_command(command):
                self._complete_identity_source_question_source(command)
            else:
                self._complete_referenced_action(command)
            awaiting_input = result.pop("awaiting_input", False) is True
            if awaiting_input:
                result = result_with_contract(
                    {**command, "status": "AWAITING_INPUT"}, result
                )
                routed_stage = str(result.get("routing_stage") or "")
                changes: dict[str, Any] = {
                    "status": "AWAITING_INPUT",
                    "result": result,
                }
                if routed_stage in {"requirements", "design", "implementation", "testing"}:
                    changes["stage"] = routed_stage
                    command["stage"] = routed_stage
                    stage = routed_stage
                repository.update_command(command_id, **changes)
                repository.notify_command_changed(
                    app_id,
                    command_id=command_id,
                    stage=stage,
                )
                if stage == "requirements":
                    self.apply_saved_deployment_preferences(app_id)
                return
            result = result_with_contract({**command, "status": "COMPLETED"}, result)
            terminal = self._finish_terminal_command(
                command_id, command, status="COMPLETED", result=result, error=None
            )
            repository.notify_command_changed(
                app_id,
                command_id=command_id,
                stage=str(terminal.get("stage") or stage),
            )
        except WorkspaceStopRequested:
            self._cancel_command(command_id, command, stage)
            return
        except Exception as error:
            detail = self._error_text(error)
            latest = repository.get_command(command_id) or command
            failure_result = result_with_contract(
                {**latest, "status": "FAILED"},
                dict(latest.get("result") or {}),
            )
            self._finish_terminal_command(
                command_id,
                command,
                status="FAILED",
                result=failure_result,
                error=detail,
            )
            repository.notify_command_changed(
                app_id,
                command_id=command_id,
                stage=stage,
            )
            raise

    @staticmethod
    def _stop_requested(command_id: str) -> bool:
        current = repository.get_command(command_id)
        return bool(current and (current.get("payload") or {}).get("_stop_requested"))

    def _cancel_command(
        self, command_id: str, command: dict[str, Any], stage: str
    ) -> None:
        """Finish a command only after its durable stop request is observed."""

        latest = repository.get_command(command_id) or command
        if stage == "implementation":
            job_id = str((latest.get("payload") or {}).get("job_id") or "")
            if job_id:
                try:
                    implementation_worker.cancel(job_id)
                except Exception:
                    # The worker may have completed between the stop request
                    # and cancellation. The durable Workspace stop still wins.
                    _log.debug("Could not cancel implementation job %s", job_id, exc_info=True)
        result = result_with_contract(
            {**latest, "status": "CANCELLED"},
            {
                **dict(latest.get("result") or {}),
                "kind": "cancelled",
                "message": "The workspace command was stopped.",
            },
        )
        self._finish_terminal_command(
            command_id, command, status="CANCELLED", result=result, error=None
        )
        repository.notify_command_changed(
            str(command["app_id"]), command_id=command_id, stage=stage
        )

    @staticmethod
    def _cancelled_result(command: dict[str, Any]) -> dict[str, Any]:
        return result_with_contract(
            {**command, "status": "CANCELLED"},
            {
                **dict(command.get("result") or {}),
                "kind": "cancelled",
                "message": "The workspace command was stopped.",
            },
        )

    def _finish_terminal_command(
        self,
        command_id: str,
        command: dict[str, Any],
        *,
        status: str,
        result: dict[str, Any],
        error: str | None,
    ) -> dict[str, Any]:
        """Atomically choose a terminal state after checking the stop bit."""

        latest = repository.get_command(command_id) or command
        return repository.finish_command_honoring_stop(
            command_id,
            status=status,
            result=result,
            error=error,
            cancelled_result=self._cancelled_result(latest),
        )

    @staticmethod
    def _is_transient_execution_error(error: Exception) -> bool:
        """Classify provider/transport failures without treating validation as retryable."""

        if isinstance(error, (ConnectionError, TimeoutError, OSError)):
            return True
        status_code = getattr(error, "status_code", None)
        if isinstance(status_code, int) and (status_code == 429 or status_code >= 500):
            return True
        name = type(error).__name__.lower()
        return any(
            token in name
            for token in (
                "apiconnection",
                "apitimeout",
                "ratelimit",
                "serviceunavailable",
                "gatewaytimeout",
            )
        )

    def _dispatch_with_transient_retry(self, command: dict[str, Any]) -> dict[str, Any]:
        """Retry only checkpoint-backed stages, retaining this command identity."""

        def operation() -> dict[str, Any]:
            return self._dispatch(command)
        return self._run_with_transient_retry(
            command, operation, retry_operation=lambda: self._transient_retry_operation(command)
        )

    def _run_with_transient_retry(
        self,
        command: dict[str, Any],
        operation: Callable[[], dict[str, Any]],
        *,
        retry_operation: Callable[[], Callable[[], dict[str, Any]] | None],
    ) -> dict[str, Any]:
        """Run an idempotent checkpoint operation with cancellable backoff."""

        command_id = str(command["command_id"])
        attempt = 0
        while True:
            if self._stop_requested(command_id):
                raise WorkspaceStopRequested()
            try:
                return operation()
            except Exception as error:
                next_operation = retry_operation()
                if next_operation is None:
                    # Never create a replacement job or replay an uncheckpointed
                    # operation merely because it raised.  Checkpoint-backed
                    # technical stages below are safe to retry regardless of
                    # whether the provider classified the exception as transient.
                    raise
                attempt += 1
                self._record_transient_retry(command_id, attempt, error)
                self._sleep_for_retry(command_id, attempt)
                operation = next_operation

    def _transient_retry_operation(self, command: dict[str, Any]):
        app_id = str(command["app_id"])
        command_id = str(command["command_id"])
        stage = str(command.get("stage") or "")
        if stage == "requirements":
            def retry_requirements() -> dict[str, Any]:
                progress = self._requirements_progress_reporter(app_id, command_id)
                with requirements_telemetry.progress_scope(progress):
                    return self._requirements_result(
                        retry_requirements_analysis(app_id, app_id=app_id)
                    )
            return retry_requirements
        if stage == "design":
            def retry_design() -> dict[str, Any]:
                status = session_status(app_id)
                design_stage = str(status.get("stage") or "design")
                response = self._run_design_operation(
                    command,
                    stage=design_stage,
                    label=self._design_stage_label(design_stage, "Retrying"),
                    operation=lambda: retry_design_session(app_id),
                )
                return self._design_result(response)
            return retry_design
        if stage == "testing":
            # A persisted testing input is immutable, so its run can safely resume.
            latest = repository.get_command(command_id) or command
            checkpoint = (latest.get("payload") or {}).get("testing_checkpoint")
            implementation_job_id = str(
                (checkpoint.get("implementation_job_id") if isinstance(checkpoint, dict) else "")
                or (latest.get("payload") or {}).get("implementation_job_id")
                or ""
            )
            if implementation_job_id:
                return lambda: self._run_testing_command(
                    latest, implementation_job_id
                )
        if stage == "implementation":
            latest = repository.get_command(command_id) or command
            job_id = str((latest.get("payload") or {}).get("job_id") or "")
            if not job_id:
                return lambda: self._dispatch(
                    {**latest, "action": "rerun_implementation"}
                )
            try:
                job = implementation_worker.get(job_id)
            except Exception:
                return lambda: self._retry_implementation_checkpoint(latest, job_id)
            if (
                str(job.get("status") or "") in {"FAILED", "INTERRUPTED", "NEEDS_PLANNER", "NEEDS_INPUT", "CANCELLED"}
                and bool(job.get("checkpoint_retryable"))
            ):
                return lambda: self._retry_implementation_checkpoint(latest, job_id)
            if str(job.get("status") or "") in TERMINAL_JOB_STATUSES:
                failure = job.get("failure_classification")
                if not (
                    isinstance(failure, dict)
                    and failure.get("kind") == "provider_request_validation"
                    and failure.get("status_code") == 400
                ):
                    return lambda: self._dispatch(
                        {**latest, "action": "rerun_implementation"}
                    )
        return None

    def _retry_implementation_checkpoint(
        self, command: dict[str, Any], job_id: str
    ) -> dict[str, Any]:
        """Retry only a confirmed terminal job; never create a replacement job."""

        current = implementation_worker.get(job_id)
        if (
            str(current.get("status") or "") not in {"FAILED", "INTERRUPTED", "NEEDS_PLANNER", "NEEDS_INPUT", "CANCELLED"}
            or not bool(current.get("checkpoint_retryable"))
        ):
            raise RuntimeError("The implementation checkpoint is no longer safe to retry.")
        job = implementation_worker.retry_failed(job_id)
        return self._monitor_implementation(job, command_id=str(command["command_id"]))

    def _record_transient_retry(
        self, command_id: str, attempt: int, error: Exception
    ) -> None:
        latest = repository.get_command(command_id)
        if latest is None:
            raise WorkspaceStopRequested()
        payload = dict(latest.get("payload") or {})
        payload["_transient_retry"] = {
            "attempt": attempt,
            "error_type": type(error).__name__,
        }
        repository.update_command(command_id, payload=payload, error=None)
        repository.notify_command_changed(
            str(latest["app_id"]), command_id=command_id, stage=str(latest["stage"])
        )

    def _sleep_for_retry(self, command_id: str, attempt: int) -> None:
        delay = min(_RETRY_MAX_SECONDS, _RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
        delay *= random.uniform(0.8, 1.2)  # noqa: S311 - scheduling jitter, not security.
        remaining = delay
        while remaining > 0:
            if self._stop_requested(command_id):
                raise WorkspaceStopRequested()
            pause = min(_RETRY_SLEEP_SLICE_SECONDS, remaining)
            time.sleep(pause)
            remaining -= pause

    def _complete_referenced_action(self, command: dict[str, Any]) -> None:
        if command["payload"].get("_conversation_outcome"):
            return
        action_id = str(command["payload"].get("action_id") or "")
        if not action_id:
            return
        prior = repository.get_command(action_id)
        if prior is not None and prior["status"] == "AWAITING_INPUT":
            repository.update_command(action_id, status="COMPLETED", completed_at=repository.now())

    @staticmethod
    def _identity_source_answer_command(command: dict[str, Any]) -> bool:
        outcome = (command.get("payload") or {}).get("_conversation_outcome")
        return isinstance(outcome, dict) and outcome.get("kind") == "identity_source_retry"

    @staticmethod
    def _refresh_identity_source_downstream_handoff(
        command: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        """Refresh an existing Requirements-to-Design handoff after a typed source answer."""

        payload = command.get("payload") or {}
        app_id = str(command.get("app_id") or "")
        source_id = str(payload.get("action_id") or "")
        if not app_id or not source_id or result.get("awaiting_input") is not True:
            return result
        source = repository.get_command(source_id)
        handoff = (source.get("result") or {}).get("downstream_revision_handoff") if source else None
        if (
            source is None
            or source.get("status") != "AWAITING_INPUT"
            or str(source.get("app_id") or "") != app_id
            or not isinstance(handoff, dict)
        ):
            return result
        try:
            answer = IdentitySourceAnswer.model_validate(payload.get("identity_source_answer") or {})
            source_targets = [
                item for item in handoff.get("source_targets") or []
                if isinstance(item, dict) and isinstance(item.get("ref"), str)
            ]
            answered_ref = f"use_case_spec:{answer.use_case_id}"
            refs = [str(item["ref"]) for item in source_targets]
            if not refs or answered_ref not in refs:
                return result
            current_targets = ProjectTools(app_id).normalize_revision_targets(
                refs, require_editable=False
            )
            if len(current_targets) != len(refs) or {
                target.ref for target in current_targets
            } != set(refs):
                return result
            if any(
                target.kind != "use_case_spec"
                or target.owner != "requirements"
                or target.artifact_type != TYPE_USECASE_SPEC
                or not isinstance(target.artifact_version_id, int)
                for target in current_targets
            ):
                return result
        except (TypeError, ValueError):
            return result

        refreshed_handoff = {
            **handoff,
            "source_targets": [
                {"ref": target.ref, "artifact_version_id": target.artifact_version_id}
                for target in current_targets
            ],
        }
        return {**result, "downstream_revision_handoff": refreshed_handoff}

    @staticmethod
    def _complete_identity_source_question_source(command: dict[str, Any]) -> None:
        """Close the exact saved identity question only after its typed resume succeeds."""
        payload = command.get("payload") or {}
        answer = IdentitySourceAnswer.model_validate(payload.get("identity_source_answer") or {})
        source_id = str(payload.get("action_id") or "")
        source = repository.get_command(source_id) if source_id else None
        raw = (source.get("result") or {}).get("identity_source_question") if source else None
        if (
            source is None
            or source.get("status") != "AWAITING_INPUT"
            or source.get("app_id") != command.get("app_id")
            or source.get("stage") != "requirements"
            or not isinstance(raw, dict)
            or str(raw.get("useCaseId") or "") != answer.use_case_id
            or str(raw.get("obligationRef") or "") != answer.obligation_ref
        ):
            raise ValueError("The identity-source answer does not match the open question.")
        if not any(
            isinstance(option, dict)
            and str(option.get("identitySourceKind") or "") == answer.identity_source_kind
            and (option.get("sourceAuthenticateObligationRef") or None)
            == answer.source_authenticate_obligation_ref
            for option in raw.get("options") or []
        ):
            raise ValueError("The identity-source answer is not a saved option.")
        repository.update_command(source_id, status="COMPLETED", completed_at=repository.now())

    def _route_identity_source_answer(
        self,
        app_id: str,
        payload: dict[str, Any],
        stage: str | None,
        latest: dict[str, Any],
        prior: dict[str, Any],
        raw_question: dict[str, Any],
    ) -> tuple[str, dict[str, Any], str | None]:
        """Resume a saved identity-source gate without planning a revision."""
        try:
            if prior.get("app_id") != app_id or prior.get("stage") != "requirements":
                raise ValueError("The identity-source question belongs to another workspace.")
            question = self._identity_source_question(app_id, raw_question)
            displayed = Question.model_validate((prior.get("result") or {}).get("feedback_question"))
            if question is None or displayed.question_id != question.question_id:
                raise ValueError("The identity-source question is stale.")
            authority = ProjectTools(app_id).current_revision_target(
                question.authority_candidates[0]
            )
            if authority is None or question_is_stale(
                question,
                [BaseRevision(artifact_type=authority.artifact_type, version_id=authority.artifact_version_id)],
            ) or question_is_stale(
                displayed,
                [BaseRevision(artifact_type=authority.artifact_type, version_id=authority.artifact_version_id)],
            ):
                raise ValueError("The identity-source question is stale.")

            offered = raw_question.get("options")
            if not isinstance(offered, list):
                raise ValueError("The identity-source question has no saved options.")
            option_id = str(payload.get("feedback_option_id") or "").strip()
            raw_text = str(payload.get("text") or "").strip()
            if option_id:
                matches = [item for item in offered if isinstance(item, dict) and str(item.get("id") or "").strip() == option_id]
            else:
                # Free text is deliberately narrow: accept only an exact,
                # unambiguous rendering of a server-offered option.
                matches = [
                    item for item in offered
                    if isinstance(item, dict) and raw_text in {
                        str(item.get("id") or "").strip(),
                        str(item.get("label") or "").strip(),
                    }
                ]
            if len(matches) != 1:
                raise ValueError("Choose one of the offered identity-source options.")
            chosen = matches[0]
            answer = IdentitySourceAnswer(
                use_case_id=str(raw_question.get("useCaseId") or ""),
                obligation_ref=str(raw_question.get("obligationRef") or ""),
                identity_source_kind=str(chosen.get("identitySourceKind") or ""),
                source_authenticate_obligation_ref=chosen.get("sourceAuthenticateObligationRef"),
            )
            # Ensure an option cannot be swapped between the display envelope
            # and the gate snapshot, even if a persisted record is corrupted.
            if answer.identity_source_kind not in {"caller_input", "authenticated_context", "system_result"}:
                raise ValueError("The selected identity source is invalid.")
        except (TypeError, ValueError) as error:
            return self._feedback_question_clarification(
                payload, Clarification(question=str(error), candidates=[]), stage, latest, prior
            )
        return (
            "message",
            {
                **payload,
                "action_id": str(prior["command_id"]),
                "identity_source_answer": answer.model_dump(mode="json"),
                "_conversation_outcome": {"kind": "identity_source_retry"},
            },
            "requirements",
        )

    def _route_feedback_question_answer(
        self,
        app_id: str,
        payload: dict[str, Any],
        stage: str | None,
        latest: dict[str, Any],
        prior: dict[str, Any],
        raw_question: dict[str, Any],
    ) -> tuple[str, dict[str, Any], str | None]:
        try:
            question = Question.model_validate(raw_question)
            design_gap = (
                prior.get("stage") == "design"
                and question.detected_at.stage == "design"
                and question.trigger.category == "specification_gap"
            )
            class_binding_stall = (
                prior.get("stage") == "design"
                and question.detected_at.stage == "design"
                and question.trigger.category == "class_binding_source"
            )
            design_validation_input = (
                prior.get("stage") == "design"
                and question.detected_at.stage == "design"
                and question.trigger.category == "design_validation_input"
            )
            implementation_gap = (
                prior.get("stage") == "implementation"
                and question.detected_at.stage == "implementation"
                and question.trigger.category == "upstream_contract_gap"
            )
            requirements_ambiguity = (
                prior.get("stage") == "requirements"
                and question.detected_at.stage == "requirements"
                and question.trigger.category == "semantic_ambiguity"
            )
            if question.app_id != app_id or not (
                design_gap
                or class_binding_stall
                or design_validation_input
                or implementation_gap
                or requirements_ambiguity
            ):
                raise ValueError("This feedback question is not an executable workspace gap.")
            targets = question.authority_candidates
            if len(targets) != 1:
                raise ValueError("The feedback question must name one authority target.")
            if (design_gap or class_binding_stall or requirements_ambiguity) and (
                targets[0].owner != "requirements"
                or targets[0].kind != "use_case_spec"
                or targets[0].artifact_type != TYPE_USECASE_SPEC
            ):
                raise ValueError("The feedback question must name one requirements use-case specification.")
            if implementation_gap and targets[0].owner not in {"requirements", "design"}:
                raise ValueError("The implementation gap must name a requirements or design authority.")
            option_id = str(payload.get("feedback_option_id") or "")
            if option_id:
                decision = answer_option(
                    question,
                    option_id=option_id,
                    decision_id=str(uuid.uuid4()),
                    source_user_message_id=str(prior["command_id"]),
                )
            elif class_binding_stall:
                raw_answer = str(payload.get("text") or "").strip()
                decision = free_text_decision(
                    question,
                    raw_answer=raw_answer,
                    decision_id=str(uuid.uuid4()),
                    source_user_message_id=str(prior["command_id"]),
                    normalization={
                        "normalized_meaning": {
                            "semantic_scope": "contract",
                            "requested_effect": raw_answer,
                            "change_type": "modify",
                        },
                        "authoritative_target_refs": [targets[0].ref],
                        "preserved_constraints": (
                            question.decision_policy.required_preserved_constraints
                        ),
                    },
                )
            else:
                interpretation = conversation_agent.interpret_revision(
                    str(payload.get("text") or ""),
                    [target.ref for target in targets],
                    tools=ProjectTools(app_id),
                    context=build_conversation_context(app_id),
                    sealed_targets=True,
                )
                if isinstance(interpretation, Clarification):
                    return self._feedback_question_clarification(
                        payload, interpretation, stage, latest, prior
                    )
                if (
                    not isinstance(interpretation, CommandIntent)
                    or str(interpretation.intent) != ConversationIntent.REVISE.value
                ):
                    raise ValueError("Free-text answer needs clarification.")
                revision = interpretation.revision
                if revision is None:
                    raise ValueError("Free-text answer needs a revision meaning.")
                decision = free_text_decision(
                    question,
                    raw_answer=str(payload.get("text") or ""),
                    decision_id=str(uuid.uuid4()),
                    source_user_message_id=str(prior["command_id"]),
                    normalization={
                        "normalized_meaning": {
                            "semantic_scope": revision.semantic_scope,
                            "requested_effect": revision.requested_effect,
                            "change_type": revision.change_type,
                        },
                        "authoritative_target_refs": revision.targets,
                        "preserved_constraints": (
                            question.decision_policy.required_preserved_constraints
                        ),
                    },
                )
            if decision.status != "NORMALIZED" or decision.normalized_meaning is None:
                return self._feedback_question_clarification(
                    payload,
                    Clarification(
                        question="The answer is outside the question authority or policy."
                    ),
                    stage,
                    latest,
                    prior,
                )
            interpretation = RevisionInterpretation(
                targets=[target.ref for target in decision.authoritative_targets],
                semantic_scope=decision.normalized_meaning.semantic_scope,
                requested_effect=decision.normalized_meaning.requested_effect,
                change_type=decision.normalized_meaning.change_type,
            )
            tools = ProjectTools(app_id)
            selected = tools.validate_revision_selections(
                [target.model_dump(mode="json") for target in decision.authoritative_targets]
            )
            if not selected.get("valid"):
                raise ValueError("The feedback question is stale.")
            binding_source_decision = None
            if class_binding_stall:
                # The structural directive is derived from the saved Question
                # and selected option; never trust a client-supplied copy.
                payload.pop("binding_source_decision", None)
                guidance = decision.normalized_meaning.requested_effect
                if decision.preserved_constraints:
                    guidance += "\nPreserve constraint: " + "; ".join(
                        decision.preserved_constraints
                    )
                if option_id and question.trigger.binding_slot is not None:
                    binding_source_decision = {
                        **question.trigger.binding_slot.model_dump(by_alias=True),
                        "sourceKind": option_id,
                    }
                return (
                    "message",
                    {
                        **payload,
                        "action_id": str(prior["command_id"]),
                        "text": guidance,
                        "feedback_decision": decision.model_dump(mode="json"),
                        "_conversation_outcome": {
                            "kind": "class_binding_retry",
                            **(
                                {"binding_source_decision": binding_source_decision}
                                if binding_source_decision is not None
                                else {}
                            ),
                        },
                    },
                    "design",
                )
            if implementation_gap:
                plan = plan_revision(
                    tools,
                    interpretation,
                    origin_stage="implementation",
                )
            else:
                plan = plan_revision(tools, interpretation)
            decision_refs = [target.ref for target in decision.authoritative_targets]
            plan_is_valid = (
                validate_plan(
                    tools,
                    plan,
                    interpretation,
                    origin_stage="implementation",
                )
                if implementation_gap
                else validate_plan(tools, plan, interpretation)
            )
            if (
                plan.status != "needs_confirmation"
                or [target.ref for target in plan.requested_targets] != decision_refs
                or [target.ref for target in plan.authority_targets] != decision_refs
                or not plan_is_valid
            ):
                raise ValueError("The feedback question is stale.")
        except (TypeError, ValueError) as error:
            return self._clarification_message(
                payload,
                Clarification(question=str(error), candidates=[]),
                stage,
                latest,
            )
        routed_owner = (
            "requirements"
            if design_gap or requirements_ambiguity
            else plan.authority_targets[0].owner
            if plan.authority_targets
            else targets[0].owner
        )
        return (
            "message",
            {
                **payload,
                "text": "\n".join(
                    [
                        interpretation.requested_effect,
                        *(
                            f"Preserve constraint: {constraint}"
                            for constraint in decision.preserved_constraints
                        ),
                    ]
                ),
                "action_id": str(prior["command_id"]),
                "feedback_decision": decision.model_dump(mode="json"),
                "revision_interpretation": interpretation.model_dump(mode="json"),
                "revision_plan": plan.model_dump(mode="json"),
                **(
                    {"revision_origin_stage": "implementation"}
                    if implementation_gap
                    else {}
                ),
                "conversation_intent": {
                    "intent": "revise",
                    "targets": interpretation.targets,
                    "instruction": interpretation.requested_effect,
                },
                "validated_targets": [target.model_dump(mode="json") for target in decision.authoritative_targets],
                "_conversation_outcome": {"kind": "revision_plan"},
            },
            routed_owner,
        )

    def _feedback_question_clarification(
        self,
        payload: dict[str, Any],
        outcome: Clarification,
        stage: str | None,
        latest: dict[str, Any],
        source: dict[str, Any],
    ) -> tuple[str, dict[str, Any], str | None]:
        action, routed_payload, routed_stage = self._clarification_message(
            payload, outcome, stage, latest
        )
        routed_payload["_conversation_actions"] = [
            offer.model_dump(mode="json", exclude_none=True)
            for offer in self._resolved_offered_actions(
                str(latest.get("app_id") or ""), source
            )
        ]
        return action, routed_payload, routed_stage

    @staticmethod
    def _feedback_question_command(command: dict[str, Any]) -> dict[str, Any] | None:
        current = command
        visited: set[str] = set()
        while len(visited) < 12:
            payload = current.get("payload") or {}
            if payload.get("feedback_decision") is not None:
                return current
            if current.get("action") != "retry_requirements":
                return None
            previous_id = str(payload.get("action_id") or "")
            if not previous_id:
                return None
            if previous_id in visited:
                raise ValueError("The Requirements retry chain contains a cycle.")
            visited.add(previous_id)
            previous = repository.get_command(previous_id)
            if (
                previous is None
                or previous.get("app_id") != command.get("app_id")
                or previous.get("stage") != "requirements"
                or previous.get("status") not in {"FAILED", "INTERRUPTED"}
            ):
                return None
            current = previous
        raise ValueError("The Requirements feedback retry chain is too deep.")

    @staticmethod
    def _complete_feedback_question_source(command: dict[str, Any]) -> None:
        """Close only the exact typed question represented by this Decision."""

        payload = command.get("payload") or {}
        try:
            decision = Decision.model_validate(payload.get("feedback_decision"))
        except (TypeError, ValueError) as error:
            raise ValueError("The feedback Decision is invalid.") from error
        if (
            command.get("action") != "message"
            or command.get("stage") not in {"requirements", "design", "implementation"}
            or decision.status != "NORMALIZED"
            or decision.app_id != command.get("app_id")
        ):
            raise ValueError("The feedback Decision does not belong to this command.")
        source_id = str(payload.get("action_id") or "")
        source = repository.get_command(source_id) if source_id else None
        raw_question = (
            (source.get("result") or {}).get("feedback_question")
            if source is not None
            else None
        )
        try:
            question = Question.model_validate(raw_question)
        except (TypeError, ValueError) as error:
            raise ValueError("The source feedback Question is missing or invalid.") from error
        answered_by = (source.get("result") or {}).get("feedback_question_answered_by")
        if source.get("status") == "COMPLETED" and answered_by == command.get("command_id"):
            return
        if (
            source.get("status") != "AWAITING_INPUT"
            or source.get("app_id") != command.get("app_id")
            or decision.source_user_message_id != source_id
            or question.question_id != decision.question_id
            or question.question_version != decision.question_version
            or {target.ref for target in decision.authoritative_targets}
            - {target.ref for target in question.authority_candidates}
        ):
            raise ValueError("The feedback Decision does not match the open Question.")
        repository.update_command(
            source_id,
            status="COMPLETED",
            result={
                **dict(source.get("result") or {}),
                "feedback_question_answered_by": str(command["command_id"]),
            },
            completed_at=repository.now(),
        )

    @staticmethod
    def _source_testing_command(
        command: dict[str, Any], implementation_job_id: str
    ) -> dict[str, Any] | None:
        """Return the Testing command referenced by a retry action, if any."""

        app_id = str(command["app_id"])
        command_id = str((command.get("payload") or {}).get("action_id") or "")
        visited: set[str] = set()
        while command_id and command_id not in visited:
            visited.add(command_id)
            referenced = repository.get_command(command_id)
            if referenced is None:
                return None
            if str(referenced.get("app_id") or "") != app_id:
                raise ValueError("The Testing repair chain belongs to another app.")
            if referenced.get("stage") == "testing":
                payload = referenced.get("payload") or {}
                checkpoint = payload.get("testing_checkpoint")
                if bool(payload.get("_stop_requested")):
                    return None
                if isinstance(checkpoint, dict) and (
                    str(checkpoint.get("implementation_job_id") or "")
                    != implementation_job_id
                ):
                    return None
                return referenced
            command_id = str((referenced.get("payload") or {}).get("action_id") or "")
        prior = repository.latest_command(
            app_id,
            exclude_command_id=str(command["command_id"]),
            stage="testing",
        )
        if (
            not isinstance(prior, dict)
            or str(prior.get("status") or "") not in {"FAILED", "INTERRUPTED"}
            or bool((prior.get("payload") or {}).get("_stop_requested"))
        ):
            return None
        payload = prior.get("payload") or {}
        checkpoint = payload.get("testing_checkpoint")
        if (
            not isinstance(checkpoint, dict)
            or str(payload.get("implementation_job_id") or "") != implementation_job_id
            or str(checkpoint.get("implementation_job_id") or "") != implementation_job_id
        ):
            return None
        return prior

    def _dispatch(self, command: dict[str, Any]) -> dict[str, Any]:
        action = str(command["action"])
        handler = action_spec(action).handler
        conversation_outcome = command["payload"].get("_conversation_outcome")
        if action == "message" and isinstance(conversation_outcome, dict):
            kind = str(conversation_outcome.get("kind") or "")
            if kind == "reply":
                reply = Reply.model_validate(
                    {key: value for key, value in conversation_outcome.items() if key != "kind"}
                )
                return {
                    "kind": "reply",
                    "message": reply.text,
                    "conversation": {"reply": reply.model_dump(mode="json")},
                }
            if kind == "clarification":
                clarification = Clarification.model_validate(
                    {key: value for key, value in conversation_outcome.items() if key != "kind"}
                )
                result = {
                    "kind": "question",
                    "message": clarification.question,
                    "conversation": {
                        "clarification": clarification.model_dump(mode="json")
                    },
                }
                if not command["payload"].get("_conversation_actions"):
                    result["awaiting_input"] = True
                return result
            if kind == "identity_source_retry":
                app_id = str(command["app_id"])
                answer = IdentitySourceAnswer.model_validate(
                    command["payload"].get("identity_source_answer") or {}
                )
                progress = self._requirements_progress_reporter(
                    app_id, str(command["command_id"])
                )
                with requirements_telemetry.progress_scope(progress):
                    result = analyze_requirements(
                        AnalyzeRequest(
                            identity_source_answer=answer,
                            thread_id=app_id,
                            app_id=app_id,
                        )
                    )
                presented = self._requirements_result(result)
                return self._refresh_identity_source_downstream_handoff(command, presented)
            if kind == "revision_plan":
                plan = RevisionPlan.model_validate(
                    command["payload"].get("revision_plan") or {}
                )
                if plan.status != "needs_confirmation":
                    raise ValueError("Only a confirmation plan can wait for approval.")
                return self._revision_plan_result(str(command["command_id"]), plan)
            if kind == "class_binding_retry":
                app_id = str(command["app_id"])
                guidance = str(command["payload"].get("text") or "").strip()
                if not guidance:
                    raise ValueError("Class binding retry guidance cannot be empty.")
                response = self._run_design_operation(
                    command,
                    stage="class_diagram",
                    label=self._design_stage_label("class_diagram", "Retrying"),
                    operation=lambda: retry_design_session(
                        app_id,
                        repair_guidance=guidance,
                        binding_source_decision=conversation_outcome.get(
                            "binding_source_decision"
                        ),
                    ),
                )
                return self._design_result(response)
            raise ValueError("Unknown conversation outcome.")
        # 파일 복원이나 검사 도중 서버가 재시작되었다면 구현 수리부터 반복하지 않는다.
        # 현재 command에 저장한 Testing 체크포인트를 그대로 실행 서비스에 돌려준다.
        checkpoint = command["payload"].get("testing_checkpoint")
        if command.get("stage") == "testing" and isinstance(checkpoint, dict):
            implementation_job_id = str(checkpoint.get("implementation_job_id") or "")
            if not implementation_job_id:
                raise ValueError("The Testing checkpoint has no implementation job ID.")
            return self._run_testing_command(command, implementation_job_id)
        if handler == "stage_message":
            raw_plan = command["payload"].get("revision_plan")
            plan = RevisionPlan.model_validate(raw_plan) if isinstance(raw_plan, dict) else None
            if plan is not None:
                # Planned revisions are displayed by the ``revision_plan``
                # conversation outcome and executed only by ``confirm_change``.
                # In particular, a caller cannot inject a plan into a message
                # command and turn it into authority to mutate artifacts.
                raise ValueError("Revision plans require explicit confirmation.")
            resource_answer_context = command["payload"].get("_resource_answer_context")
            server_pinned_answer = (
                isinstance(resource_answer_context, dict)
                and resource_answer_context.get("server_pinned_answer") is True
            )
            if (
                action == "message"
                and str(command["payload"].get("action_id") or "")
                and str(command["payload"].get("text") or "").strip()
                and not server_pinned_answer
            ):
                # ``action_is_offered`` intentionally permits message text for
                # UI input.  It is not confirmation authority for a revision.
                raise ValueError(
                    "Free-text feedback requires a revision plan and explicit confirmation."
                )
            return self._stage_message(
                command, advance=action in {"advance", "start_design"}
            )
        if handler == "plan_downstream_revision":
            return self._plan_downstream_revision(command)
        if handler == "retry_requirements":
            app_id = str(command["app_id"])
            feedback_command = self._feedback_question_command(command)
            if feedback_command is not None:
                replay = {
                    **command,
                    "payload": dict(feedback_command.get("payload") or {}),
                }
                return self._stage_message(replay, advance=False)
            progress = self._requirements_progress_reporter(app_id, str(command["command_id"]))
            with requirements_telemetry.progress_scope(progress):
                result = retry_requirements_analysis(
                    app_id,
                    app_id=app_id,
                )
            return self._requirements_result(result)
        if handler == "retry_design":
            app_id = str(command["app_id"])
            status = session_status(app_id)
            stage = str(status.get("stage") or "design")
            response = self._run_design_operation(
                command,
                stage=stage,
                label=self._design_stage_label(stage, "Retrying"),
                operation=lambda: retry_design_session(app_id),
            )
            return self._design_result(response)
        if handler == "confirm_change":
            return self._confirm_change(command)
        if handler == "dismiss_change":
            return {"message": "Kept the existing artifacts and dismissed the change request."}
        if handler == "start_implementation":
            if action == "rerun_implementation":
                # The retry starts a new implementation run.  Tell the UI to
                # discard only the previous implementation timeline while
                # preserving requirement and design conversation history.
                repository.append_progress_event(
                    str(command["app_id"]),
                    command_id=str(command["command_id"]),
                    stage="implementation",
                    text="",
                    metadata={"reset_implementation_timeline": True},
                )
            app_id = str(command["app_id"])
            design_state = cast(dict[str, Any], artifact_repository.load_state(app_id))
            missing_artifacts = self._missing_design_artifacts(design_state)
            if missing_artifacts:
                raise ValueError(
                    "Missing required design artifacts: "
                    + ", ".join(missing_artifacts)
                )
            job = implementation_worker.create_job(
                app_id,
                design_state,
                str(command["payload"].get("base_package") or "com.easydep.app"),
                bool(command["payload"].get("allow_assumptions", True)),
            )
            persisted_payload = {
                **dict(command["payload"]),
                "job_id": str(job["job_id"]),
            }
            command["payload"] = persisted_payload
            repository.update_command(
                str(command["command_id"]),
                payload=persisted_payload,
            )
            return self._monitor_implementation(job, command_id=str(command["command_id"]))
        if handler == "retry_implementation":
            payload = command["payload"]
            current_job = implementation_worker.get(str(payload["job_id"]))
            if str(current_job.get("app_id") or "") != str(command["app_id"]):
                raise ValueError("The implementation checkpoint does not belong to this app.")
            repository.append_progress_event(
                str(command["app_id"]),
                command_id=str(command["command_id"]),
                stage="implementation",
                text="Resuming the failed implementation checkpoint.",
                metadata={
                    "progress_event": "implementationCheckpointRetryStarted",
                    "status": "CHECKPOINT_RETRY_STARTED",
                    "job_id": str(payload["job_id"]),
                },
            )
            job = implementation_worker.retry_failed(str(payload["job_id"]))
            return self._monitor_implementation(
                job,
                command_id=str(command["command_id"]),
            )
        if handler == "start_testing":
            implementation_job_id = str(command["payload"]["implementation_job_id"])
            source_testing = self._source_testing_command(command, implementation_job_id)
            if source_testing is not None:
                source_result = source_testing.get("result") or {}
                previous_job = source_result.get("job")
                blockers = source_result.get("blocking_findings") or []
                preserve_test = any(
                    isinstance(blocker, dict)
                    and isinstance(blocker.get("candidate_plan"), dict)
                    and bool(blocker.get("candidate_plan"))
                    for blocker in blockers
                )
                implementation_job = implementation_worker.get(implementation_job_id)
                resumed_repair = False
                if (
                    str(implementation_job.get("status") or "")
                    in {"FAILED", "INTERRUPTED"}
                    and bool(implementation_job.get("checkpoint_retryable"))
                ):
                    resumed = self._retry_implementation_checkpoint(
                        command, implementation_job_id
                    )
                    if (
                        resumed.get("awaiting_input") is True
                        or (resumed.get("job") or {}).get("status") != "COMPLETED"
                    ):
                        return resumed
                    implementation_job = implementation_worker.get(implementation_job_id)
                    resumed_repair = True
                source_checkpoint = (source_testing.get("payload") or {}).get(
                    "testing_checkpoint"
                )
                if isinstance(source_checkpoint, dict) and not resumed_repair:
                    payload = {
                        **dict(command.get("payload") or {}),
                        "testing_checkpoint": dict(source_checkpoint),
                    }
                    command["payload"] = payload
                    repository.update_command(str(command["command_id"]), payload=payload)
                return self._run_testing_command(
                    command,
                    implementation_job_id,
                    previous_job=(
                        previous_job if isinstance(previous_job, dict) else None
                    ),
                    preserve_test=preserve_test,
                    repair_task_type=(
                        str(implementation_job.get("repair_task_type") or "") or None
                    ),
                    reset_checkpoint=resumed_repair,
                )
            return self._run_testing_command(
                command,
                implementation_job_id,
            )
        if handler == "branch_checkpoint":
            branch = create_checkpoint_branch(
                str(command["app_id"]),
                str(command["payload"]["checkpoint_stage"]),
            )
            return {
                **branch,
                "message": (
                    f"Created a new app branch after {branch['checkpoint_stage']}."
                ),
            }
        if handler == "rerun_from_stage":
            return self._rerun_from_stage(command)
        raise ValueError(f"Unsupported workspace command: {action}")

    def _auto_repair_semantic_result(
        self,
        command: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Continue an active stage ledger until it repairs, asks, or stalls.

        The loop has no service-owned retry count.  Each turn must produce a
        new stage diagnostic fingerprint, and the Requirements/Design ledgers
        remain responsible for exhausting strategies and returning ``STALLED``.
        """

        current = result
        retry_attempt = 0
        while True:
            if self._stop_requested(str(command["command_id"])):
                raise WorkspaceStopRequested()
            repair_input = self._active_semantic_repair_input(current)
            if repair_input is None:
                stage = self._technical_retry_stage(command, current)
                if stage is None:
                    return current
                retry_attempt += 1
                fingerprint = self._semantic_repair_fingerprint(current)
                self._record_technical_retry(
                    command, retry_attempt, stage, fingerprint, current
                )
                repaired = self._retry_technical_checkpoint(command, current, stage)
                if repaired is current:
                    return current
                current = repaired
                self._sleep_for_retry(str(command["command_id"]), retry_attempt)
                continue
            stage = str(current.get("routing_stage") or command.get("stage") or "")
            repair_state, repairable = repair_input
            fingerprint = self._semantic_repair_fingerprint(current)
            retry_attempt += 1
            self._record_technical_retry(
                command, retry_attempt, stage, fingerprint, current
            )

            if stage == "testing":
                blockers = [
                    blocker
                    for blocker in current.get("blocking_findings") or []
                    if isinstance(blocker, dict)
                ]
                implementation_blockers = [
                    blocker
                    for blocker in blockers
                    if blocker.get("repairable") is not False
                    and blocker.get("defect_class") == "SUT_DEFECT"
                    and blocker.get("repair_owner") == "implementation"
                ]
                repairable_blockers = [
                    blocker for blocker in blockers if blocker.get("repairable") is not False
                ]
                # Only an unambiguous Implementation-owned SUT failure can cross
                # this boundary. Mixed or separately routed failures remain visible
                # to the existing action/question handling.
                if (
                    not implementation_blockers
                    or len(implementation_blockers) != len(repairable_blockers)
                    or blocking_findings_route(blockers)
                ):
                    return self._retry_technical_checkpoint(command, current, stage)
                previous_job = current.get("job")
                if (
                    not isinstance(previous_job, dict)
                    or previous_job.get("status") != "COMPLETED"
                    or (previous_job.get("result") or {}).get("passed") is not False
                ):
                    return current
                repair_result, implementation_job_id, repair_task_type = (
                    self._repair_testing_with_owner(command, current)
                )
                if (
                    repair_result.get("awaiting_input") is True
                    or (repair_result.get("job") or {}).get("status") != "COMPLETED"
                ):
                    current = repair_result
                    self._sleep_for_retry(str(command["command_id"]), retry_attempt)
                    continue
                current = self._run_with_transient_retry(
                    command,
                    lambda: self._run_testing_command(
                        command,
                        implementation_job_id,
                        previous_job=previous_job,
                        preserve_test=True,
                        repair_task_type=repair_task_type,
                        reset_checkpoint=True,
                    ),
                    retry_operation=lambda: self._transient_retry_operation(command),
                )
                continue
            instruction = self._repair_instruction(stage, repair_state, repairable)
            if stage == "requirements":
                repaired = self._run_with_transient_retry(
                    command,
                    lambda: self._repair_requirements_result(
                        command, current, instruction=instruction
                    ),
                    retry_operation=lambda: (
                        lambda: self._repair_requirements_result(
                            command, current, instruction=instruction
                        )
                    ),
                )
            elif stage == "design":
                design_stage = str(current.get("current_stage") or "design")
                repaired = self._run_with_transient_retry(
                    command,
                    lambda: self._design_result(
                        self._run_design_operation(
                            command,
                            stage=design_stage,
                            label=self._design_stage_label(design_stage, "Repairing"),
                            operation=lambda: retry_design_session(str(command["app_id"])),
                        )
                    ),
                    retry_operation=lambda: self._transient_retry_operation(command),
                )
            else:
                return self._retry_technical_checkpoint(command, current, stage)

            if self._semantic_repair_fingerprint(repaired) == fingerprint:
                current = repaired
                self._sleep_for_retry(str(command["command_id"]), retry_attempt)
                continue
            current = repaired

    @staticmethod
    def _active_semantic_repair_input(
        result: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        if (
            result.get("awaiting_input") is not True
            or result.get("feedback_question") is not None
            or result.get("resource_question") is not None
            or bool(result.get("resource_questions"))
            or result.get("requires_revision") is not True
        ):
            return None
        if any(
            isinstance(item, dict)
            and item.get("requires_user_input", item.get("requiresUserInput")) is True
            for key in ("blocking_findings", "finding_details")
            for item in result.get(key) or []
        ):
            return None
        repair_state = result.get("repair_state")
        if not isinstance(repair_state, dict) or str(
            repair_state.get("status") or ""
        ).upper() not in {"ACTIVE", "STALLED"}:
            return None
        repairable = [
            blocker
            for blocker in result.get("blocking_findings") or []
            if isinstance(blocker, dict) and blocker.get("repairable") is not False
        ]
        return (repair_state, repairable) if repairable else None

    @staticmethod
    def _technical_retry_stage(
        command: dict[str, Any], result: dict[str, Any]
    ) -> str | None:
        """Return a stage only for a non-interactive technical pause.

        Typed questions, resource choices, deployment choices, and revision
        confirmation stay outside this loop.  The stage runners themselves
        retain their existing checkpoint and repair ownership rules.
        """

        if result.get("awaiting_input") is not True:
            return None
        if (
            result.get("feedback_question") is not None
            or result.get("resource_question") is not None
            or bool(result.get("resource_questions"))
            or result.get("deployment_configuration_required") is True
            or result.get("downstream_revision_handoff") is not None
            or result.get("kind") == "question"
            or bool(result.get("questions"))
        ):
            return None
        findings = [
            item
            for key in ("blocking_findings", "finding_details")
            for item in result.get(key) or []
            if isinstance(item, dict)
        ]
        if any(
            item.get("requires_user_input", item.get("requiresUserInput")) is True
            for item in findings
        ):
            return None
        if not (result.get("requires_revision") is True or findings):
            return None
        stage = str(result.get("routing_stage") or command.get("stage") or "")
        return stage if stage in {"requirements", "design", "implementation", "testing"} else None

    def _record_technical_retry(
        self,
        command: dict[str, Any],
        attempt: int,
        stage: str,
        fingerprint: str,
        result: dict[str, Any],
    ) -> None:
        """Persist automatic checkpoint progress without creating a new command."""

        command_id = str(command["command_id"])
        latest = repository.get_command(command_id)
        if latest is None:
            raise WorkspaceStopRequested()
        payload = dict(latest.get("payload") or {})
        payload["_technical_repair_retry"] = {
            "attempt": attempt,
            "stage": stage,
            "finding_digest": fingerprint,
        }
        repository.update_command(command_id, payload=payload, result=dict(result))
        repository.notify_command_changed(
            str(latest["app_id"]), command_id=command_id, stage=stage
        )

    def _retry_technical_checkpoint(
        self,
        command: dict[str, Any],
        result: dict[str, Any],
        stage: str,
    ) -> dict[str, Any]:
        """Reuse an existing checkpoint runner for a non-question technical pause."""

        if stage == "testing":
            job = result.get("job")
            implementation_job_id = (
                str(job.get("implementation_job_id") or "")
                if isinstance(job, dict)
                else ""
            )
            if not implementation_job_id:
                payload = command.get("payload") or {}
                checkpoint = payload.get("testing_checkpoint")
                implementation_job_id = str(
                    (checkpoint.get("implementation_job_id") if isinstance(checkpoint, dict) else "")
                    or payload.get("implementation_job_id")
                    or ""
                )
            if implementation_job_id:
                return self._run_testing_command(command, implementation_job_id)
        if stage == "requirements":
            app_id = str(command["app_id"])
            progress = self._requirements_progress_reporter(app_id, str(command["command_id"]))
            with requirements_telemetry.progress_scope(progress):
                return self._requirements_result(
                    retry_requirements_analysis(app_id, app_id=app_id)
                )
        if stage == "design":
            design_stage = str(result.get("current_stage") or "design")
            return self._design_result(
                self._run_design_operation(
                    command,
                    stage=design_stage,
                    label=self._design_stage_label(design_stage, "Repairing"),
                    operation=lambda: retry_design_session(str(command["app_id"])),
                )
            )
        if stage == "implementation":
            job_id = str(result.get("job_id") or (command.get("payload") or {}).get("job_id") or "")
            if job_id:
                try:
                    return self._retry_implementation_checkpoint(command, job_id)
                except RuntimeError:
                    return result
            # There is no implementation checkpoint to resume. Reuse the
            # existing rerun handler once; it persists its new job id onto this
            # same command, so later loop turns monitor/retry that job instead
            # of allocating another one.
            return self._dispatch({**command, "action": "rerun_implementation"})
        return result

    @staticmethod
    def _semantic_repair_fingerprint(result: dict[str, Any]) -> str:
        state = result.get("repair_state")
        state = state if isinstance(state, dict) else {}
        return stable_digest(
            {
                "stage": result.get("current_stage") or result.get("phase"),
                "repair": {
                    key: state.get(key)
                    for key in (
                        "status",
                        "attempt_count",
                        "accepted_count",
                        "finding_digest",
                        "tried_strategies",
                        "rejected_candidate_digests",
                    )
                },
                "findings": result.get("blocking_findings") or [],
                "changed": result.get("changed") or [],
                "touched": result.get("touched") or {},
            }
        )

    @staticmethod
    def _stalled_semantic_repair_result(result: dict[str, Any]) -> dict[str, Any]:
        repair_state = dict(result.get("repair_state") or {})
        repair_state["status"] = "STALLED"
        repair_state.setdefault(
            "stall_reason", "Automatic repair did not produce new stage progress."
        )
        stage = str(result.get("current_stage") or result.get("phase") or "current")
        return {
            **result,
            "message": (
                f"Automatic repair stalled at {stage.replace('_', ' ')}. "
                "Provide revision feedback for the current artifact to continue."
            ),
            "repair_state": repair_state,
        }

    @staticmethod
    def _repair_instruction(
        stage: str,
        repair_state: dict[str, Any],
        blockers: list[dict[str, Any]],
    ) -> str:
        history = dict(repair_state)
        strategy_key = (
            f"automatic:{stage}:episode-"
            f"{int(history.get('attempt_count') or 0) + 1}"
        )
        messages = [str(blocker.get("message") or "") for blocker in blockers]
        return (
            "Repair the current stage using the accumulated repair history. "
            f"Use this new strategy identity: {strategy_key}. "
            "Do not repeat a rejected strategy or candidate. Resolve these blockers:\n- "
            + "\n- ".join(messages)
            + "\n\nAccumulated repair history:\n"
            + json.dumps(history, ensure_ascii=False, sort_keys=True)
        )

    def _repair_requirements_result(
        self,
        command: dict[str, Any],
        result: dict[str, Any],
        *,
        instruction: str,
    ) -> dict[str, Any]:
        """Apply the existing Requirements repair edit to an in-memory result."""

        repairable = [
            blocker
            for blocker in result.get("blocking_findings") or []
            if isinstance(blocker, dict) and blocker.get("repairable") is not False
        ]
        if not repairable:
            raise ValueError("No LLM-repairable blocker is available.")
        stage_order = {
            "actors": 0,
            "use_cases": 1,
            "specs": 2,
            "relationships": 3,
        }
        owner_value = min(
            (str(item.get("stage") or "relationships") for item in repairable),
            key=lambda value: stage_order.get(value, 99),
            default="relationships",
        )
        owner = cast(FeedbackStage, owner_value)
        targets = sorted(
            {
                str(target)
                for item in repairable
                if str(item.get("stage") or "") == owner
                for target in item.get("target_ids") or []
            }
        )
        app_id = str(command["app_id"])
        request = AnalyzeRequest(
            edit=FeedbackEdit(
                stage=owner,
                scope="local" if targets else "broad",
                target_ids=targets,
                instruction=instruction,
            ),
            thread_id=app_id,
            app_id=app_id,
        )
        progress = self._requirements_progress_reporter(
            app_id, str(command["command_id"])
        )
        with requirements_telemetry.progress_scope(progress):
            repaired = analyze_requirements(request)
        return self._requirements_result(repaired)

    def _repair_testing_with_owner(
        self,
        command: dict[str, Any],
        result: dict[str, Any],
    ) -> tuple[dict[str, Any], str, str]:
        """Reuse the Testing-to-Implementation owner repair hand-off in memory."""

        blockers = result.get("blocking_findings") or []
        implementation_job_id = str(
            (result.get("job") or {}).get("implementation_job_id")
            or command.get("payload", {}).get("implementation_job_id")
            or ""
        )
        if not implementation_job_id:
            raise ValueError("The failing Testing run has no implementation job ID.")
        implementation_blockers = [
            blocker
            for blocker in blockers
            if isinstance(blocker, dict)
            and blocker.get("repairable") is not False
            and (
                blocker.get("repair_owner") == "implementation"
                or blocker.get("defect_class") == "SUT_DEFECT"
            )
        ]
        if not implementation_blockers:
            raise ValueError("The selected Testing finding does not belong to Implementation.")
        (
            selected_blockers,
            _repair_owner,
            _repair_task_type,
            _repair_file_hints,
            _verification_profile,
        ) = self._testing_repair_request(
            str(command["app_id"]),
            result,
            implementation_blockers,
        )
        original_implementation = implementation_worker.get(implementation_job_id)
        previous_repair_results, older_repair_summaries = (
            self._implementation_repair_outcomes(original_implementation)
        )
        repair_payload = {
            **dict(command.get("payload") or {}),
            "job_id": implementation_job_id,
        }
        command["payload"] = repair_payload
        repository.update_command(str(command["command_id"]), payload=repair_payload)
        batch_repairs: list[dict[str, object]] = []
        repair_task_type = "testing-dynamic-functional"
        for owner_blockers in self._testing_repair_owner_groups(
            str(command["app_id"]), result, selected_blockers
        ):
            (
                grouped_blockers,
                repair_owner,
                repair_task_type,
                repair_file_hints,
                verification_profile,
            ) = self._testing_repair_request(
                str(command["app_id"]), result, owner_blockers
            )
            feedback = self._testing_implementation_feedback(
                result,
                grouped_blockers,
                previous_repair_results=previous_repair_results,
                older_repair_summaries=older_repair_summaries,
            )
            confirmed_target_refs, repair_file_hints = (
                self._testing_implementation_repair_targets(
                    str(command["app_id"]), grouped_blockers, repair_file_hints
                )
            )
            batch_repairs.append(
                {
                    "owner": repair_owner,
                    "evidence": {
                        "command": ["testing", repair_task_type],
                        "stderr": feedback,
                        "testResults": json.dumps(
                            {
                                "confirmedTargetRefs": confirmed_target_refs,
                                "fileHints": repair_file_hints,
                                "verificationProfile": verification_profile,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    },
                }
            )
        repair_job = implementation_worker.request_owner_repair_batch(
            implementation_job_id,
            repairs=batch_repairs,
        )
        repair_job_id = str(repair_job.get("job_id") or "")
        if repair_job_id != implementation_job_id:
            raise RuntimeError("Implementation repair returned an unexpected job ID.")
        repair_result = self._monitor_testing_owner_repair(command, repair_job)
        return repair_result, implementation_job_id, repair_task_type

    def _monitor_testing_owner_repair(
        self, command: dict[str, Any], repair_job: dict[str, Any]
    ) -> dict[str, Any]:
        """Monitor one already-scheduled Testing owner repair through checkpoint retry."""

        job_id = str(repair_job.get("job_id") or "")
        if not job_id:
            raise RuntimeError("Implementation repair returned no job ID.")

        def retry_operation() -> Callable[[], dict[str, Any]] | None:
            current = implementation_worker.get(job_id)
            if (
                str(current.get("status") or "")
                in {"FAILED", "INTERRUPTED"}
                and bool(current.get("checkpoint_retryable"))
            ):
                return lambda: self._retry_implementation_checkpoint(command, job_id)
            return None

        return self._run_with_transient_retry(
            command,
            lambda: self._monitor_implementation(
                repair_job, command_id=str(command["command_id"])
            ),
            retry_operation=retry_operation,
        )

    def _rerun_from_stage(self, command: dict[str, Any]) -> dict[str, Any]:
        """선택 단계 직전까지 분기한 새 앱에서 정식 실행 경로를 시작한다."""

        restart_stage = RestartStage(str(command["payload"]["restart_stage"]))
        branch = create_restart_branch(str(command["app_id"]), restart_stage)
        target_app_id = str(branch["target_app_id"])
        entry_command_id = str(branch.get("entry_command_id") or "")

        if restart_stage == RestartStage.REQUIREMENTS:
            state = artifact_repository.load_state(target_app_id)
            saved_preferences = repository.get_deployment_preferences(target_app_id)
            initial_cloud_inputs = branch.get("initial_cloud_inputs") or {}
            payload = {
                "text": str(state.get("requirements_text") or ""),
                "resource_constraints_text": str(
                    state.get("resource_constraints_text") or ""
                ),
            }
            if saved_preferences:
                payload["deployment_preferences"] = saved_preferences
                payload["resource_constraints_text"] = str(
                    saved_preferences.get("resource_constraints_text")
                    or payload["resource_constraints_text"]
                )
            elif initial_cloud_inputs:
                payload.update(initial_cloud_inputs)
            next_command = self.submit(
                target_app_id,
                action="message",
                stage="requirements",
                payload=payload,
            )
        elif restart_stage == RestartStage.DESIGN:
            next_command = self.submit(
                target_app_id,
                action="start_design",
                payload={"action_id": entry_command_id},
            )
        elif restart_stage == RestartStage.IMPLEMENTATION:
            next_command = self.submit(
                target_app_id,
                action="start_implementation",
                payload={"action_id": entry_command_id},
            )
        else:
            next_command = self.submit(
                target_app_id,
                action="start_testing",
                payload={
                    "action_id": entry_command_id,
                    "implementation_job_id": str(branch["implementation_job_id"]),
                },
            )
        return {
            **branch,
            "restart_stage": restart_stage.value,
            "started_command_id": next_command["command_id"],
            "message": f"Created a new branch and started {restart_stage.value} again.",
        }

    def _stage_message(self, command: dict[str, Any], *, advance: bool) -> dict[str, Any]:
        app_id = str(command["app_id"])
        payload = command["payload"]
        text = "" if advance else str(payload.get("text") or "").strip()
        stage = str(command["stage"])
        if advance and stage == "design" and payload.get("auto_approve_method_proposals") is True:
            # Auto mode is an affirmative user choice.  Keep the approval in
            # the same feedback path as a manual decision so reconciliation
            # still applies only the concrete, persisted MethodProposals.
            text = "approve all"
        if stage == "requirements":
            action_id = str(payload.get("action_id") or "")
            previous = repository.get_command(action_id) if action_id else None
            conversation_intent = payload.get("conversation_intent")
            if (
                isinstance(conversation_intent, dict)
                and conversation_intent.get("intent") == "revise"
                and payload.get("validated_targets")
            ):
                targets = [
                    RevisionTarget.model_validate(target)
                    for target in payload.get("validated_targets") or []
                ]
                if payload.get("feedback_decision") is not None:
                    current = ProjectTools(app_id).validate_targets(
                        [target.model_dump(mode="json") for target in targets]
                    )
                    if not current.get("valid"):
                        raise ValueError("The feedback question is stale.")
                edit = requirements_feedback_edit(targets, text)
                progress = self._requirements_progress_reporter(app_id, str(command["command_id"]))
                with requirements_telemetry.progress_scope(progress):
                    result = revise_requirements_analysis(edit, app_id, app_id=app_id)
                return self._requirements_result(result)
            continuation = bool(
                action_id and previous is not None and previous["stage"] == "requirements"
            )
            if continuation:
                assert previous is not None
                previous_result = _with_capability_handoff_questions(
                    app_id, previous.get("result") or {}
                )
                if command.get("action") == "apply_deployment_preferences":
                    preferences = DeploymentPreferences.model_validate(
                        payload.get("deployment_preferences") or {}
                    )
                    request = AnalyzeRequest(
                        deployment_preferences=preferences,
                        thread_id=app_id,
                        app_id=app_id,
                    )
                    progress = self._requirements_progress_reporter(
                        app_id, str(command["command_id"])
                    )
                    with requirements_telemetry.progress_scope(progress):
                        result = analyze_requirements(request)
                    return self._requirements_result(result)
                resource_questions, selected_resource_question = _resource_questions(
                    previous_result
                )
                resource_question = (
                    previous_result.get("resource_question") or selected_resource_question
                )
                resource_field = str((resource_question or {}).get("field") or "")
                conversation_intent = payload.get("conversation_intent")
                if (
                    isinstance(conversation_intent, dict)
                    and conversation_intent.get("intent") == "revise"
                ):
                    targets = [
                        RevisionTarget.model_validate(target)
                        for target in payload.get("validated_targets") or []
                    ]
                    edit = requirements_feedback_edit(targets, text)
                    progress = self._requirements_progress_reporter(
                        app_id, str(command["command_id"])
                    )
                    with requirements_telemetry.progress_scope(progress):
                        result = revise_requirements_analysis(
                            edit,
                            app_id,
                            app_id=app_id,
                        )
                    return self._requirements_result(result)
                elif (
                    text
                    and resource_field
                    and isinstance(conversation_intent, dict)
                    and conversation_intent.get("intent") == "answer"
                ):
                    request = AnalyzeRequest(
                        resource_answer=ResourceAnswer(
                            free_text=text,
                            expected_field=resource_field,
                        ),
                        thread_id=app_id,
                        app_id=app_id,
                    )
                elif text and resource_field:
                    request = AnalyzeRequest(
                        resource_answers={resource_field: text},
                        thread_id=app_id,
                        app_id=app_id,
                    )
                else:
                    request = AnalyzeRequest(answer=text, thread_id=app_id, app_id=app_id)
            else:
                provider = cast(CloudProvider, str(payload.get("provider") or ""))
                region = str(payload.get("region") or "")
                lines = _initial_requirement_lines(text)
                cloud_constraints = (
                    InitialCloudConstraints(
                        provider=provider,
                        region=region,
                        monthly_budget_amount=payload.get("monthly_budget_amount"),
                        monthly_budget_currency=str(
                            payload.get("monthly_budget_currency") or "USD"
                        ),
                    )
                    if provider and region
                    else None
                )
                deployment_preferences = (
                    DeploymentPreferences.model_validate(payload["deployment_preferences"])
                    if payload.get("deployment_preferences")
                    else None
                )
                request = AnalyzeRequest(
                    requirements=lines or [text],
                    thread_id=app_id,
                    app_id=app_id,
                    feedback_gates=True,
                    resource_constraints_text=str(payload.get("resource_constraints_text") or ""),
                    cloud_constraints=cloud_constraints,
                    deployment_preferences=deployment_preferences,
                )
            progress = self._requirements_progress_reporter(app_id, str(command["command_id"]))
            with requirements_telemetry.progress_scope(progress):
                result = analyze_requirements(request)
            return self._requirements_result(result)

        if stage == "design":
            status = session_status(app_id)
            if status.get("retryable") and command.get("action") != "start_design":
                failed_stage = str(status.get("stage") or "design")
                raise ValueError(
                    f"The {failed_stage} step failed. Retry that checkpoint before "
                    "starting or advancing the design pipeline."
                )
            context = payload.get("context") or {}
            validated_feedbacks = context.get("validated_target_feedbacks")
            if validated_feedbacks is not None:
                if not isinstance(validated_feedbacks, list) or not validated_feedbacks:
                    raise ValueError("Validated design feedback requires at least one target.")
                revisions = BatchReviseRequest.model_validate(
                    {"revisions": validated_feedbacks}
                ).revisions
                validated_revision = revise_design_elements(
                    app_id,
                    BatchReviseRequest(revisions=revisions),
                    approved_authority_targets=(
                        {
                            str(ref)
                            for ref in context.get("approved_authority_targets") or []
                            if str(ref)
                        }
                        if "approved_authority_targets" in context
                        else None
                    ),
                    approved_downstream_targets=(
                        {
                            str(ref)
                            for ref in context.get("approved_downstream_targets") or []
                            if str(ref)
                        }
                        if "approved_downstream_targets" in context
                        else None
                    ),
                )
                return self._targeted_design_result(
                    app_id,
                    validated_revision,
                    status,
                    message=(
                        f"Revised {len(revisions)} validated design element(s) and only "
                        "their trace-linked artifacts."
                    ),
                )
            target_feedbacks = self._sequence_target_feedbacks(context)
            revised: dict[str, Any] | None = None
            if target_feedbacks:
                # Every entry has an explicit UC and its own instruction.  The
                # batch service keeps all revisions in memory until all of them
                # succeed, so this command cannot persist a half-applied set.
                revised = revise_design_elements(
                    app_id,
                    BatchReviseRequest(revisions=target_feedbacks),
                )
                revision_message = (
                    f"Revised {len(target_feedbacks)} selected use-case diagrams and "
                    "only their trace-linked artifacts. Review the result or continue."
                )
                related_default: list[Any] | dict[str, Any] = {}
            element_ref = str(context.get("element_ref") or "").strip()
            if revised is None and text and element_ref:
                # A UI-selected element is an explicit local-edit request, not
                # ordinary stage feedback.  In particular, sequence feedback
                # must carry ``sequence_diagram:UCn`` so we never rewind and
                # regenerate every use-case card just to revise one of them.
                revised = revise_design_element(
                    app_id,
                    ReviseRequest(target=element_ref, feedback=text),
                )
                revision_message = (
                    f"Revised the selected {element_ref} and only its "
                    "trace-linked artifacts. Review the result or continue."
                )
                related_default = []
            if revised is not None:
                return self._targeted_design_result(
                    app_id,
                    revised,
                    status,
                    message=revision_message,
                    related_default=related_default,
                )
            current_stage = str(status.get("stage") or "")
            action_id = str(payload.get("action_id") or "")
            previous = repository.get_command(action_id) if action_id else None
            previous_question = (
                (previous.get("result") or {}).get("resource_question")
                if previous is not None
                and str(previous.get("app_id") or "") == app_id
                and previous.get("stage") == "design"
                else None
            )
            answers_data_execution_mode = (
                command.get("action") == "message"
                and isinstance(previous_question, dict)
                and previous_question.get("field") == "dataExecutionMode"
            )
            endpoint_question = (payload.get("_resource_answer_context") or {}).get(
                "connection_endpoint_question"
            )
            answers_connection_endpoint = (
                command.get("action") == "message"
                and isinstance(endpoint_question, dict)
                and isinstance(previous_question, dict)
                and previous.get("status") == "AWAITING_INPUT"
                and endpoint_question == previous_question
                and str(endpoint_question.get("field") or "").startswith(
                    "connectionEndpoint:"
                )
            )
            if text and command.get("action") == "message" and current_stage == "sequence_diagram":
                raise ValueError(
                    "Select one or more use-case targets and provide feedback for each target."
                )
            if answers_connection_endpoint:
                operation_stage = "deployment_diagram"
                verb = "Applying"

                def operation():
                    return apply_deployment_endpoint_answer_session(
                        app_id, dict(endpoint_question), text
                    )

            elif answers_data_execution_mode:
                operation_stage = "deployment_diagram"
                verb = "Generating"

                def operation():
                    return apply_deployment_topology_decision_session(app_id, text)

            elif command.get("action") == "start_design":
                # start_design은 현재 gate의 '다음' 버튼이 아니라 설계를 처음부터 다시
                # 시작하는 공개 action이다. 기존 checkpoint가 남아 있어도 service가
                # reset한 뒤 반드시 class diagram부터 실행해야 한다.
                operation_stage = DESIGN_STAGES[0]
                verb = "Generating"

                def operation():
                    return start_design_session(app_id)
            elif status.get("active"):
                if text:
                    operation_stage = current_stage
                    verb = "Revising"
                else:
                    index = DESIGN_STAGES.index(current_stage)
                    operation_stage = (
                        DESIGN_STAGES[index + 1]
                        if index + 1 < len(DESIGN_STAGES)
                        else "design_complete"
                    )
                    verb = "Generating" if operation_stage != "design_complete" else "Completing"

                def operation():
                    return resume_design_session(app_id, text)
            elif command.get("action") == "advance":
                # A targeted revision can validate and persist design artifacts
                # without opening a full design-generation checkpoint.  The
                # action reference was already checked against a server offer,
                # so advancing from that review finishes Design; it must not
                # restart the whole pipeline merely because no session exists.
                operation_stage = "design_complete"
                verb = "Completing"

                def operation():
                    return {"status": "completed", "app_id": app_id}
            else:
                operation_stage = DESIGN_STAGES[0]
                verb = "Generating"

                def operation():
                    return start_design_session(app_id)

            response = self._run_design_operation(
                command,
                stage=operation_stage,
                label=self._design_stage_label(operation_stage, verb),
                operation=operation,
            )
            return self._design_result(response)

        if stage != "implementation":
            raise ValueError("The current stage cannot process a conversational command.")
        if not text:
            raise ValueError("Enter implementation feedback.")
        conversation_intent = payload.get("conversation_intent")
        confirmed_target_refs: list[str] | None
        if (
            isinstance(conversation_intent, dict)
            and conversation_intent.get("intent") == "revise"
        ):
            targets = [
                RevisionTarget.model_validate(target)
                for target in payload.get("validated_targets") or []
            ]
            delivery = implementation_revision_payload(targets)
            confirmed_target_refs = list(delivery.confirmed_target_refs)
        else:
            confirmed_target_refs = None
        job = implementation_worker.create_feedback_job(
            app_id,
            cast(dict[str, Any], artifact_repository.load_state(app_id)),
            text,
            str(payload.get("base_package") or "com.easydep.app"),
            bool(payload.get("allow_assumptions", True)),
            confirmed_target_refs=confirmed_target_refs,
        )
        latest = repository.get_command(str(command["command_id"])) or command
        persisted_payload = {
            **dict(latest.get("payload") or {}),
            "job_id": str(job["job_id"]),
        }
        repository.update_command(str(command["command_id"]), payload=persisted_payload)
        return self._monitor_implementation(
            job, command_id=str(command["command_id"])
        )

    @staticmethod
    def _design_stage_label(stage: str, verb: str) -> str:
        label = {
            "class_diagram": "class diagram",
            "sequence_diagram": "sequence diagram",
            "api_spec": "API specification",
            "erd": "ERD",
            "deployment_diagram": "deployment diagram",
            "design_complete": "design review",
        }.get(stage, stage.replace("_", " "))
        return f"{verb} the {label}"

    @staticmethod
    def _run_design_operation(
        command: dict[str, Any],
        *,
        stage: str,
        label: str,
        operation,
    ) -> dict[str, Any]:
        app_id = str(command["app_id"])
        command_id = str(command["command_id"])
        started = time.perf_counter()
        llm_timing_events: list[dict[str, Any]] = []

        def record(
            status: str,
            detail: str,
            metadata: dict[str, Any] | None = None,
        ) -> None:
            repository.append_progress_event(
                app_id,
                command_id=command_id,
                stage="design",
                text=label,
                metadata={
                    "progress_event": "designStageProgress",
                    "analysis_step": stage,
                    "current_stage": stage,
                    "design": {"stage": stage},
                    "progress_step_label": label,
                    "progress_detail": detail,
                    "progress_status": status,
                    "progress_card_label": "Design generation",
                }
                | dict(metadata or {}),
            )

        def report(event: str, fields: dict[str, Any]) -> None:
            if event != "classDiagramSnapshotAccepted":
                return
            puml = str(fields.get("puml") or "")
            if not puml.strip():
                return
            snapshot = live_previews.publish(
                app_id=app_id,
                command_id=command_id,
                stage="class_diagram",
                puml=puml,
                phase=str(fields.get("phase") or "generation"),
                unit=str(fields.get("unit") or ""),
                completed=int(fields.get("completed") or 0),
                total=int(fields.get("total") or 0),
            )
            try:
                # preview가 나온 시점에 계속 실행 중인 renderer로 SVG를 준비한다. 브라우저가
                # 처음 그림을 열 때까지 JVM 기동과 렌더링을 미루지 않으며, 같은 내용은 공통
                # SHA cache가 재사용한다.
                image = render_plantuml(snapshot.puml, "svg")
                if image:
                    live_previews.cache_svg(
                        app_id,
                        command_id,
                        snapshot.stage,
                        snapshot.revision,
                        image,
                    )
            except Exception as error:
                # 중간 preview 표시 실패가 클래스 모델 생성 자체를 중단해서는 안 된다. 최종
                # 산출물 저장 때 다시 렌더링하며, 실패 유형만 timing event로 남긴다.
                log_design_timing(
                    "plantuml.preview_warmup.failed",
                    error_type=type(error).__name__,
                    preview_revision=snapshot.revision,
                )
            record(
                "running",
                str(fields.get("detail") or "Updating the class diagram"),
                {
                    "progress_event": "classDiagramPreviewUpdated",
                    "preview_revision": snapshot.revision,
                    "preview_unit": snapshot.unit,
                    "preview_completed": snapshot.completed,
                    "preview_total": snapshot.total,
                },
            )

        try:
            with (
                design_timing_context(
                    app_id=app_id,
                    command_id=command_id,
                    requested_stage=stage,
                ),
                capture_llm_timings() as llm_timing_events,
            ):
                record("running", "Started")
                log_design_timing("workspace.design_operation.started", label=label)
                try:
                    with design_progress.progress_scope(report):
                        response = operation()
                except Exception as error:
                    elapsed = time.perf_counter() - started
                    log_design_timing(
                        "workspace.design_operation.failed",
                        elapsed_ms=round(elapsed * 1000, 1),
                        error_type=type(error).__name__,
                    )
                    record(
                        "failed",
                        f"Failed after {elapsed:.1f}s: {WorkspaceService._error_text(error)}",
                    )
                    raise
            elapsed = time.perf_counter() - started
            payload = response
            validation = payload.get("validation") or {}
            stage_validation = validation.get(stage) or {}
            findings = [
                *list(stage_validation.get("errors") or []),
                *list(stage_validation.get("findings") or []),
            ]
            log_design_timing(
                "workspace.design_operation.completed",
                elapsed_ms=round(elapsed * 1000, 1),
                findings_count=len(findings),
            )
            if findings:
                record(
                    "needs_review",
                    f"Draft generated in {elapsed:.1f}s; {len(findings)} findings require revision",
                )
            else:
                record("completed", f"Completed in {elapsed:.1f}s")
            return response
        finally:
            if llm_timing_events:
                # 상세 timing 수집기는 원래 class 최적화 실험에서도 사용하는 event 모양을
                # 그대로 제공한다. 공개 Workspace event에 같은 dict를 넣어 평가 도구가
                # 내부 설계 함수를 직접 호출하지 않고도 호출·token·repair·cache를 셀 수
                # 있게 한다. prompt나 LLM 응답 원문은 이 목록에 포함되지 않는다.
                repository.append_progress_event(
                    app_id,
                    command_id=command_id,
                    stage="design",
                    text="Design LLM metrics recorded.",
                    metadata={
                        "progress_event": "designLlmMetrics",
                        "analysis_step": stage,
                        "llm_timing_events": [
                            _public_design_timing_event(event) for event in llm_timing_events
                        ],
                    },
                )
            live_previews.mark_terminal(app_id, command_id)

    @staticmethod
    def _requirements_progress_reporter(app_id: str, command_id: str):
        operation_counts: dict[str, int] = {}
        active_spec_tasks: dict[str, dict[str, str]] = {}
        active_analysis_steps: set[str] = set()
        progress_lock = Lock()
        current_step: str | None = None

        step_labels = {
            "intake": "Reading the submitted requirements",
            "clarify": "Refining ambiguous or compound requirements",
            "classify": "Classifying functional and non-functional requirements",
            "analyze_cloud_inputs": "Analyzing deployment and cloud inputs",
            "derive_deployment_needs": "Deriving deployment capabilities from requirement evidence",
            "extract_resource_constraints": "Reading additional cloud constraints",
            "build_resource_spec": "Structuring cloud and resource constraints",
            "identify_actors": "Identifying external actors",
            "identify_use_cases": "Identifying user-goal use cases",
            "review_model": "Reviewing the use-case model",
            "check_coverage": "Checking requirement coverage",
            "generate_specs": "Writing use-case specifications",
            "check_specs": "Checking use-case specifications",
            "identify_relationships": "Identifying use-case relationships",
            "check_relationships": "Checking use-case relationships",
            "render_diagram": "Rendering the use-case diagram",
        }
        operation_labels = {
            "structured:ClarifyOnlyResult": "AI requirement refinement",
            "structured:DeploymentNeedsResult": "AI deployment-capability sample",
            "structured:CloudConstraintExtraction": "AI cloud-constraint extraction",
            "resource_agent": "resource-constraint agent response",
            "structured:ActorResult": "AI actor identification",
            "structured:UseCaseResult": "AI use-case modeling",
            "structured:UseCaseSpec": "AI use-case specification",
            "structured:RelationshipModel": "AI relationship modeling",
        }
        operation_steps = {
            "structured:DeploymentNeedsResult": "derive_deployment_needs",
            "structured:CloudConstraintExtraction": "extract_resource_constraints",
            "resource_agent": "extract_resource_constraints",
        }

        def report(event: str, fields: dict[str, Any]) -> None:
            nonlocal current_step
            with progress_lock:
                event_step = str(fields.get("step") or "")
                if event == "analysisStepStarted" and event_step:
                    active_analysis_steps.add(event_step)
                    current_step = event_step
                elif event == "analysisStepFinished" and event_step:
                    active_analysis_steps.discard(event_step)
                use_case_id = str(fields.get("useCaseId") or "")
                if event == "specTaskStarted" and use_case_id:
                    active_spec_tasks[use_case_id] = {
                        "id": use_case_id,
                        "name": str(fields.get("useCaseName") or use_case_id),
                    }
                elif event == "specTaskFinished" and use_case_id:
                    active_spec_tasks.pop(use_case_id, None)
                active_snapshot = list(active_spec_tasks.values())
                analysis_snapshot = sorted(active_analysis_steps)

            metadata = {
                "progress_event": event,
                **fields,
                "active_spec_tasks": active_snapshot,
                "active_analysis_steps": analysis_snapshot,
            }
            if event == "analysisStepStarted":
                step_label = step_labels.get(str(current_step), "Running an analysis step")
                text = step_label
                metadata.update(
                    {
                        "analysis_step": current_step,
                        "progress_step_label": step_label,
                        "progress_detail": "Started",
                        "progress_status": "running",
                    }
                )
            elif event == "analysisStepFinished":
                finished_step = str(fields.get("step") or current_step or "")
                label = step_labels.get(finished_step, "Analysis step")
                elapsed = float(fields.get("elapsedSeconds") or 0)
                text = f"{label} completed in {elapsed:.1f}s"
                metadata.update(
                    {
                        "analysis_step": finished_step,
                        "progress_step_label": label,
                        "progress_detail": f"Completed in {elapsed:.1f}s",
                        "progress_status": str(fields.get("status") or "completed"),
                    }
                )
            elif event == "llmOperationStarted":
                operation = str(fields.get("operation") or "")
                with progress_lock:
                    operation_counts[operation] = operation_counts.get(operation, 0) + 1
                    operation_count = operation_counts[operation]
                if operation == "structured:DeploymentNeedsResult":
                    total = max(1, int(requirements_settings.capability_samples))
                    suffix = f" (sample {operation_count} of {total})" if total > 1 else ""
                else:
                    suffix = f" (call {operation_count})" if operation_count > 1 else ""
                label = operation_labels.get(operation, "AI model response")
                text = f"Waiting for {label}{suffix}"
                operation_step = operation_steps.get(operation) or current_step
                step_label = step_labels.get(str(operation_step), "Running requirement analysis")
                metadata.update(
                    {
                        "analysis_step": operation_step,
                        "progress_step_label": step_label,
                        "progress_detail": f"Waiting for {label}{suffix}",
                        "progress_status": "running",
                    }
                )
            elif event == "llmOperationFinished":
                operation = str(fields.get("operation") or "")
                elapsed = float(fields.get("elapsedSeconds") or 0)
                status = str(fields.get("status") or "completed")
                label = operation_labels.get(operation, "AI model response")
                text = f"{label} {status} in {elapsed:.1f}s"
                operation_step = operation_steps.get(operation) or current_step
                step_label = step_labels.get(str(operation_step), "Running requirement analysis")
                metadata.update(
                    {
                        "analysis_step": operation_step,
                        "progress_step_label": step_label,
                        "progress_detail": f"{label} {status} in {elapsed:.1f}s",
                        "progress_status": "running" if status == "completed" else status,
                    }
                )
            elif event in {"specTaskStarted", "specTaskFinished"}:
                name = str(fields.get("useCaseName") or fields.get("useCaseId") or "")
                text = f"Writing the use-case specification: {name}"
                metadata.update(
                    {
                        "analysis_step": current_step,
                        "progress_step_label": step_labels["generate_specs"],
                        "progress_detail": "Generating specifications in parallel",
                        "progress_status": "running",
                    }
                )
            else:
                return
            repository.append_progress_event(
                app_id,
                command_id=command_id,
                stage="requirements",
                text=text,
                metadata=metadata,
            )

        return report

    @staticmethod
    def _semantic_ambiguity_question(app_id: str, candidate: object) -> Question | None:
        """Bind a reviewed ambiguity to the current catalog-owned UC spec target."""
        if not isinstance(candidate, dict):
            return None
        try:
            uc_id = str(candidate.get("useCaseId") or "").strip()
            prompt = str(candidate.get("prompt") or "").strip()
            raw_options = candidate.get("options")
            evidence = [str(item) for item in candidate.get("evidenceSpans") or [] if str(item).strip()]
            requirement_ids = [str(item) for item in candidate.get("sourceRequirementIds") or [] if str(item).strip()]
            if not uc_id or not prompt or len(evidence) == 0 or len(requirement_ids) == 0 or not isinstance(raw_options, list):
                return None
            tools = ProjectTools(app_id)
            authority_candidates = tools.normalize_revision_targets(
                [f"use_case_spec:{uc_id}"], require_editable=False
            )
            authority = authority_candidates[0] if len(authority_candidates) == 1 else None
            if (authority is None or authority.owner != "requirements"
                    or authority.kind != "use_case_spec" or authority.artifact_type != TYPE_USECASE_SPEC
                    or authority.artifact_version_id is None):
                return None
            options: list[QuestionOption] = []
            for raw in raw_options:
                if not isinstance(raw, dict):
                    return None
                option_id = str(raw.get("id") or "").strip()
                label = str(raw.get("label") or "").strip()
                description = str(raw.get("description") or "").strip()
                effect = str(raw.get("requestedEffect") or "").strip()
                if not all((option_id, label, description, effect)):
                    return None
                options.append(QuestionOption(
                    option_id=option_id,
                    label=label,
                    description=description,
                    decision_payload=DecisionPayload(
                        normalized_meaning={
                            "semantic_scope": "behavior",
                            "requested_effect": effect,
                            "change_type": "modify",
                        },
                        authoritative_target_refs=(authority.ref,),
                    ),
                ))
            if len(options) != 2 or len({option.option_id for option in options}) != 2:
                return None
            digest = stable_digest(candidate)[:16]
            return Question(
                question_id=f"requirements-semantic-ambiguity:{uc_id}:{digest}",
                question_version=1,
                app_id=app_id,
                draft_id=f"requirements-semantic-ambiguity:{uc_id}:{digest}",
                detected_at={"stage": "requirements", "artifact_ref": authority.ref, "element_ref": authority.ref},
                base_revisions=[BaseRevision(artifact_type=TYPE_USECASE_SPEC, version_id=authority.artifact_version_id)],
                trigger={
                    "category": "semantic_ambiguity",
                    "evidence_refs": [*requirement_ids, *evidence],
                },
                authority_candidates=[authority],
                prompt=prompt,
                options=options,
                allow_free_text=True,
                decision_policy=DecisionPolicy(
                    allowed_semantic_scopes=("behavior",),
                    allowed_change_types=("modify",),
                ),
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _identity_source_question(app_id: str, candidate: object) -> Question | None:
        """Bind a requirements-owned identity-source choice to one live UC spec.

        The requirements gate owns the structural question and its choices.  The
        Workspace only turns that saved snapshot into the common Question card;
        it never reconstructs a choice from user prose.
        """
        if not isinstance(candidate, dict):
            return None
        try:
            use_case_id = str(candidate.get("useCaseId") or "").strip()
            obligation_ref = str(candidate.get("obligationRef") or "").strip()
            prompt = str(candidate.get("prompt") or "").strip()
            requirement_ids = tuple(
                str(item).strip()
                for item in candidate.get("requirementIds") or []
                if str(item).strip()
            )
            raw_options = candidate.get("options")
            if not use_case_id or not obligation_ref or not prompt or not requirement_ids:
                return None
            if not isinstance(raw_options, list) or not 1 <= len(raw_options) <= 12:
                return None
            authority_candidates = ProjectTools(app_id).normalize_revision_targets(
                [f"use_case_spec:{use_case_id}"], require_editable=False
            )
            authority = authority_candidates[0] if len(authority_candidates) == 1 else None
            if (
                authority is None
                or authority.owner != "requirements"
                or authority.kind != "use_case_spec"
                or authority.artifact_type != TYPE_USECASE_SPEC
                or authority.artifact_version_id is None
            ):
                return None
            options: list[QuestionOption] = []
            for raw in raw_options:
                if not isinstance(raw, dict):
                    return None
                option_id = str(raw.get("id") or "").strip()
                label = str(raw.get("label") or "").strip()
                description = str(raw.get("description") or "").strip()
                kind = str(raw.get("identitySourceKind") or "").strip()
                auth_ref = str(raw.get("sourceAuthenticateObligationRef") or "").strip()
                if kind not in {"caller_input", "authenticated_context", "system_result"}:
                    return None
                if not option_id or not label or not description:
                    return None
                if kind == "authenticated_context":
                    if not auth_ref:
                        return None
                elif auth_ref:
                    return None
                # This payload is display-envelope metadata only.  Resume uses
                # the original saved gate option below, never this prose.
                options.append(QuestionOption(
                    option_id=option_id,
                    label=label,
                    description=description,
                    decision_payload=DecisionPayload(
                        normalized_meaning=DecisionMeaning(
                            semantic_scope="contract",
                            requested_effect="Choose the saved identity source for this obligation.",
                            change_type="modify",
                        ),
                        authoritative_target_refs=(authority.ref,),
                    ),
                ))
            digest = stable_digest(candidate)[:16]
            return Question(
                question_id=f"requirements-identity-source:{use_case_id}:{obligation_ref}:{digest}",
                question_version=1,
                app_id=app_id,
                draft_id=f"requirements-identity-source:{use_case_id}:{obligation_ref}:{digest}",
                detected_at={"stage": "requirements", "artifact_ref": authority.ref, "element_ref": authority.ref},
                base_revisions=[BaseRevision(artifact_type=TYPE_USECASE_SPEC, version_id=authority.artifact_version_id)],
                trigger={"category": "identity_source", "evidence_refs": requirement_ids},
                authority_candidates=[authority],
                prompt=prompt,
                options=options,
                allow_free_text=True,
                decision_policy=DecisionPolicy(
                    allowed_semantic_scopes=("contract",),
                    allowed_change_types=("modify",),
                ),
            )
        except (TypeError, ValueError):
            return None

    def _class_binding_stall_result(
        self,
        command: dict[str, Any],
        error: ClassBindingStalled,
    ) -> dict[str, Any]:
        """Turn a bounded class-binding stall into a pinned user source choice."""

        app_id = str(command.get("app_id") or "")
        status = session_status(app_id)
        if not status.get("retryable") or status.get("stage") != "class_diagram":
            raise error

        context = error.repair_context
        if not isinstance(context, dict):
            raise TypeError("A class binding question requires its repair context.") from error
        use_case_id = str(error.unit_id or context.get("useCaseId") or "").strip()
        parameter = context.get("parameter")
        parameter_name = (
            str(parameter.get("name") or "").strip()
            if isinstance(parameter, dict)
            else ""
        )
        if not use_case_id or not parameter_name:
            raise ValueError("A class binding question requires a use case and value name.") from error

        tools = ProjectTools(app_id)
        normalized = tools.normalize_revision_targets(
            [f"use_case_spec:{use_case_id}"], require_editable=False
        )
        if len(normalized) != 1 or normalized[0].kind != "use_case_spec":
            raise ValueError("The stalled class binding has no current use-case authority.") from error
        authority = tools.current_revision_target(normalized[0])
        if (
            authority is None
            or authority.owner != "requirements"
            or authority.artifact_type != TYPE_USECASE_SPEC
            or authority.artifact_version_id is None
        ):
            raise ValueError("The stalled class binding authority is stale.") from error

        choices = (
            (
                "use_case_input",
                "Information supplied when this use case begins",
                "Use an input provided at the start of the use case and pass it through the existing flow.",
                "use an input provided at the start of the use case and pass it through the existing flow.",
            ),
            (
                "authenticated_context",
                "The current user's authenticated context",
                "Use the current user's authenticated context as the source for this value.",
                "use the current user's authenticated context as the source for this value.",
            ),
            (
                "earlier_step_result",
                "A result from an earlier step",
                "Use a value produced by an earlier step in this use case.",
                "use a value produced by an earlier step in this use case.",
            ),
            (
                "derive_from_existing_inputs",
                "Derive it from information already available",
                "Derive the value from other information already available in the use case.",
                "derive the value from other information already available in the use case.",
            ),
        )
        question_id = f"class-binding-source:{use_case_id}:{uuid.uuid4().hex[:12]}"
        question = Question(
            question_id=question_id,
            question_version=1,
            app_id=app_id,
            source_execution_id=str(command.get("command_id") or ""),
            detected_at={
                "stage": "design",
                "artifact_ref": authority.ref,
                "element_ref": authority.ref,
            },
            base_revisions=[
                BaseRevision(
                    artifact_type=authority.artifact_type,
                    version_id=authority.artifact_version_id,
                )
            ],
            trigger={
                "category": "class_binding_source",
                "finding_refs": [str(context.get("code") or "BINDING_SOURCE_UNAVAILABLE")],
                "evidence_refs": [authority.ref, str(context.get("location") or "")],
                "binding_slot": {
                    "useCaseId": use_case_id,
                    "actorEntryIndex": context.get("actorEntryIndex"),
                    "callIndex": context.get("callIndex"),
                    "parameterIndex": context.get("parameterIndex"),
                    "receiverOperationId": context.get("receiverOperationId"),
                    "parameterName": parameter_name,
                },
            },
            authority_candidates=[authority],
            prompt=(
                f"While carrying out {authority.display_label or use_case_id}, "
                f"the system needs a value for '{parameter_name}'. "
                "Where should that value come from?"
            ),
            options=[
                QuestionOption(
                    option_id=option_id,
                    label=label,
                    description=description,
                    decision_payload=DecisionPayload(
                        normalized_meaning=DecisionMeaning(
                            semantic_scope="contract",
                            requested_effect=(
                                f"For the value '{parameter_name}', {requested_effect}"
                            ),
                            change_type="modify",
                        ),
                        authoritative_target_refs=(authority.ref,),
                        preserved_constraints=(
                            "Preserve the existing use-case behavior; clarify only the source of this value.",
                        ),
                    ),
                )
                for option_id, label, description, requested_effect in choices
            ],
            allow_free_text=True,
            decision_policy=DecisionPolicy(
                allowed_semantic_scopes=("contract",),
                allowed_change_types=("modify",),
                required_preserved_constraints=(
                    "Preserve the existing use-case behavior; clarify only the source of this value.",
                ),
            ),
        )
        return {
            "awaiting_input": True,
            "kind": "question",
            "message": question.prompt,
            "feedback_question": question.model_dump(mode="json"),
            "phase": "class_diagram",
            "review_artifacts": ["Class diagram"],
        }

    def _requirements_result(self, result: dict[str, Any]) -> dict[str, Any]:
        status = result.get("status")
        if status == "need_clarification":
            questions = result.get("questions") or []
            return {
                "awaiting_input": True,
                "kind": "question",
                "message": "\n".join(str(item) for item in questions),
                "questions": questions,
                "phase": result.get("phase"),
            }
        if status == "need_feedback":
            phase = str(result.get("phase") or "requirements")
            app_id = str(result.get("app_id") or "")
            identity_question = self._identity_source_question(
                app_id, result.get("identity_source_question")
            ) if app_id else None
            if identity_question is not None:
                # Keep the gate-owned question alongside the presentation
                # envelope.  The resume path re-reads this exact saved option.
                return {
                    "awaiting_input": True,
                    "kind": "question",
                    "message": identity_question.prompt,
                    "phase": phase,
                    "feedback_question": identity_question.model_dump(mode="json"),
                    "identity_source_question": dict(result["identity_source_question"]),
                    "review_artifacts": ["Use-case specifications"],
                }
            question = self._semantic_ambiguity_question(
                app_id, result.get("semantic_ambiguity_question")
            ) if app_id and phase == "specs" else None
            if question is not None:
                return {
                    "awaiting_input": True,
                    "kind": "question",
                    "message": question.prompt,
                    "phase": phase,
                    "feedback_question": question.model_dump(mode="json"),
                    "review_artifacts": ["Use-case specifications"],
                }
            if phase == "requirements_handoff":
                blockers = list(result.get("blocking_findings") or [])
                resource_questions, resource_question = _resource_questions(result)
                repairable = [
                    blocker
                    for blocker in blockers
                    if isinstance(blocker, dict) and blocker.get("repairable") is not False
                ]
                return {
                    "awaiting_input": True,
                    "kind": "action_required",
                    "message": (
                        "A deployment decision is required before design can start. "
                        f"{resource_question.get('question')}"
                        if resource_question
                        else (
                            f"Design handoff is blocked by {len(blockers)} unresolved "
                            "requirements finding(s). Review them and provide feedback."
                        )
                    ),
                    "phase": phase,
                    "requires_revision": True,
                    "blocking_findings": blockers,
                    "repair_state": result.get("repair_state")
                    or {
                        "status": "ACTIVE" if repairable else "NEEDS_INPUT",
                        "attempt_count": 0,
                        "accepted_count": 0,
                        "recent_attempts": [],
                    },
                    "resource_question": resource_question,
                    "resource_questions": resource_questions,
                    "summary": result.get("feedback_summary"),
                    "review_artifacts": [
                        "Refined requirements",
                        "Use cases",
                        "Use-case specifications",
                        "Use-case diagram",
                    ],
                }
            requirements = list(result.get("requirements") or [])
            functional_count = sum(1 for item in requirements if item.get("type") == "FR")
            non_functional_count = sum(1 for item in requirements if item.get("type") == "NFR")
            requirement_label = "requirement" if len(requirements) == 1 else "requirements"
            lead = {
                "requirements": (
                    f"I refined and classified {len(requirements)} {requirement_label} "
                    f"({functional_count} functional and "
                    f"{non_functional_count} non-functional)."
                ),
                "use_cases": (
                    f"I identified {len(result.get('use_cases') or [])} user-goal use cases."
                ),
                "specs": (
                    f"I wrote and checked {len(result.get('use_case_specs') or [])} "
                    "use-case specifications."
                ),
                "relationships": "I completed the use-case relationships and diagram.",
            }.get(phase, "I completed this requirements-analysis step.")
            review_artifacts = [
                label
                for key, label in (
                    ("requirements", "Refined requirements"),
                    ("use_cases", "Use cases"),
                    ("use_case_specs", "Use-case specifications"),
                    ("diagram", "Use-case diagram"),
                )
                if result.get(key)
            ]
            resource_questions, resource_question = _resource_questions(result)
            if resource_question:
                field = str(resource_question.get("field") or "")
                question = str(
                    resource_question.get("question")
                    or "Please provide the missing deployment information."
                )
                if field == "provider":
                    message = f"{lead} Waiting for deployment details."
                else:
                    message = f"{lead} {question}"
                return {
                    "awaiting_input": True,
                    "kind": "question",
                    "message": message,
                    "phase": result.get("phase"),
                    "resource_question": resource_question,
                    "resource_questions": resource_questions,
                    "review_artifacts": review_artifacts,
                }
            listed = ", ".join(review_artifacts) or "the available requirements artifacts"
            return {
                "awaiting_input": True,
                "kind": "action_required",
                "message": (
                    f"{lead} Review: {listed}. "
                    "Send revision feedback, "
                    "or continue to the next analysis stage."
                ),
                "phase": result.get("phase"),
                "summary": result.get("feedback_summary"),
                "review_artifacts": review_artifacts,
            }
        return {
            "message": "Requirements analysis completed.",
            "saved_stages": result.get("saved_stages") or [],
            "phase": result.get("phase"),
        }

    def _targeted_design_result(
        self,
        app_id: str,
        revision: dict[str, Any],
        status: Mapping[str, Any],
        *,
        message: str,
        related_default: list[Any] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Project a local revision through the ordinary design gate contract.

        Targeted edits return the same artifact response as a graph checkpoint,
        but do not have a graph response of their own.  Rebuild a small response
        envelope from that checked artifact result so findings, repairs, and a
        possible typed question cannot be bypassed by a generic review card.
        """

        revision_stalled = str(revision.get("revision_status") or "").lower() == "stalled"
        validation = revision.get("validation")
        validation = dict(validation) if isinstance(validation, Mapping) else {}
        changed = [str(item) for item in revision.get("changed") or [] if str(item)]
        checked_stages = [
            stage
            for stage in changed
            if isinstance(validation.get(stage), Mapping)
        ]
        if not checked_stages:
            checked_stages = [
                stage for stage, check in validation.items() if isinstance(check, Mapping)
            ]
        stage = str(status.get("stage") or (checked_stages[0] if checked_stages else "design"))
        # A local cascade can check several changed artifacts while the saved
        # graph gate is at another stage.  Aggregate their unresolved reports
        # at this response boundary; no string-only legacy finding may acquire
        # an advance offer merely because it belongs to a different key.
        if checked_stages:
            findings: list[Any] = []
            details: list[Any] = []
            for checked_stage in checked_stages:
                check = validation.get(checked_stage) or {}
                findings.extend(list(check.get("errors") or []))
                findings.extend(list(check.get("findings") or []))
                details.extend(list(check.get("finding_details") or []))
            validation[stage] = {
                **dict(validation.get(stage) or {}),
                "findings": findings,
                "finding_details": details,
            }
        normalized = self._design_result(
            {
                **revision,
                "app_id": app_id,
                "stage": stage,
                "current_stage": stage,
                "validation": validation,
            }
        )
        # The normalizer owns kind, wait state, findings, repair state, and
        # questions.  Revision provenance is supplementary UI data only.
        return {
            **normalized,
            "message": (
                "Requested revision could not be validated; the previous artifact is unchanged."
                if revision_stalled
                else message
                if normalized.get("kind") == "action_required"
                and not normalized.get("requires_revision")
                else normalized.get("message")
            ),
            "changed": revision.get("changed") or [],
            "touched": revision.get("touched") or {},
            "related": revision.get("related") or related_default or [],
            # Candidate validation belongs to the rejected, unpersisted
            # revision. Keep it diagnostic-only; readiness and action offers
            # above are calculated solely from the accepted artifact state.
            **(
                {"revision_validation": revision.get("revision_validation")}
                if revision_stalled and revision.get("revision_validation") is not None
                else {}
            ),
        }

    def _design_finding_question(
        self,
        app_id: str,
        stage: str,
        finding_details: list[Any],
    ) -> Question | None:
        """Build a free-text design question only from typed user-input evidence.

        Check reports deliberately do not turn ordinary technical findings into
        product choices.  A report can optionally name a catalog authority (and
        may carry a fully evidenced option envelope); absent that authority the
        finding remains on the repair/stalled path instead of guessing a target.
        """

        for raw in finding_details:
            if not isinstance(raw, Mapping) or not bool(
                raw.get("requires_user_input", raw.get("requiresUserInput", False))
            ):
                continue
            authority_ref = str(
                raw.get("authority_ref")
                or raw.get("authorityRef")
                or ""
            ).strip()
            if not authority_ref:
                continue
            try:
                authorities = ProjectTools(app_id).normalize_revision_targets([authority_ref])
            except (TypeError, ValueError):
                continue
            if len(authorities) != 1 or authorities[0].artifact_version_id is None:
                continue
            authority = authorities[0]
            message = str(raw.get("message") or raw.get("finding") or "").strip()
            if not message:
                continue
            # Options must be supplied with their complete decision payload.
            # Do not manufacture alternatives, effects, or target references.
            envelope = (
                raw.get("resolution_envelope")
                or raw.get("resolutionEnvelope")
                or raw.get("resolution")
                or raw
            )
            raw_options = envelope.get("options") if isinstance(envelope, Mapping) else None
            options: list[QuestionOption] = []
            if isinstance(raw_options, list):
                try:
                    options = [QuestionOption.model_validate(item) for item in raw_options]
                except (TypeError, ValueError):
                    options = []
            digest = stable_digest({"stage": stage, "finding": dict(raw), "authority": authority.ref})[:16]
            finding_ref = str(raw.get("rule_id") or raw.get("ruleId") or "").strip()
            location = str(raw.get("location") or "").strip()
            try:
                return Question(
                    question_id=f"design-finding:{stage}:{digest}",
                    question_version=1,
                    app_id=app_id,
                    draft_id=f"design-finding:{stage}:{digest}",
                    detected_at={
                        "stage": "design",
                        "artifact_ref": authority.ref,
                        "element_ref": authority.ref,
                    },
                    base_revisions=[
                        BaseRevision(
                            artifact_type=authority.artifact_type,
                            version_id=authority.artifact_version_id,
                        )
                    ],
                    trigger={
                        "category": "design_validation_input",
                        "finding_refs": [finding_ref] if finding_ref else [],
                        "evidence_refs": [authority.ref, *([location] if location else [])],
                    },
                    authority_candidates=[authority],
                    prompt=message,
                    options=options,
                    allow_free_text=True,
                    decision_policy=DecisionPolicy(
                        allowed_semantic_scopes=(
                            "presentation", "contract", "behavior", "implementation", "test_expectation"
                        ),
                        allowed_change_types=("modify", "add", "rename", "remove"),
                    ),
                )
            except (TypeError, ValueError):
                # An invalid optional envelope is not a license to invent one.
                continue
        return None

    def _design_result(self, result: dict[str, Any]) -> dict[str, Any]:
        app_id = str(result.get("app_id") or "")
        progress_hints = self._design_progress_hints(app_id, result)
        session = result.get("session") or {}
        stage_hint = (
            session.get("current_stage")
            or result.get("current_stage")
            or result.get("stage")
        )
        raw_question = result.get("feedback_question")
        stage_validation = (result.get("validation") or {}).get(stage_hint)
        if raw_question is None and isinstance(stage_validation, dict):
            raw_question = stage_validation.get("feedback_question")
        if raw_question is not None:
            try:
                question = Question.model_validate(raw_question)
                target = (
                    question.authority_candidates[0]
                    if len(question.authority_candidates) == 1
                    else None
                )
                if (
                    question.app_id != result.get("app_id")
                    or question.detected_at.stage != "design"
                    or target is None
                ):
                    raise ValueError("unsupported feedback question")
                if question.trigger.category == "specification_gap":
                    if target.owner != "requirements" or target.artifact_type != TYPE_USECASE_SPEC:
                        raise ValueError("unsupported feedback question")
                elif question.trigger.category not in {
                    "class_binding_source",
                    "design_validation_input",
                }:
                    raise ValueError("unsupported feedback question")
            except (TypeError, ValueError) as error:
                raise ValueError("Design produced an unsupported feedback question.") from error
            return {
                "awaiting_input": True,
                "kind": "question",
                "message": question.prompt,
                "feedback_question": question.model_dump(mode="json"),
                "design": result,
                # A pending typed decision is a hard readiness boundary even
                # if a stale checkpoint snapshot still reports advancement.
                "design_can_advance": False,
                "design_complete": False,
            }
        # The design service reports completion as ``status: completed``.
        # Older stored command results can still contain the two flags below.
        finished = bool(
            result.get("status") == "completed" or result.get("finished") or session.get("finished")
        )
        if finished:
            return {
                "message": "Design artifact generation completed.",
                "design": result,
                **progress_hints,
            }
        stage = (
            session.get("current_stage")
            or session.get("stage")
            or result.get("current_stage")
            or result.get("stage")
        )
        resource_question = result.get("resource_question")
        if isinstance(resource_question, dict):
            return {
                "awaiting_input": True,
                "kind": "action_required",
                "message": str(
                    resource_question.get("question")
                    or "Choose the missing deployment option."
                ),
                "current_stage": stage,
                "resource_question": resource_question,
                "resource_questions": [resource_question],
                "design": result,
                **progress_hints,
            }
        stage_validation = (result.get("validation") or {}).get(stage) or {}
        findings = [
            *list(stage_validation.get("errors") or []),
            *list(stage_validation.get("findings") or []),
        ]
        finding_details = list(stage_validation.get("finding_details") or [])
        deployment_meta = (result.get("artifact_metadata") or {}).get("deployment_diagram") or {}
        has_completed_target = any(
            isinstance(target, dict)
            and target.get("status") == "completed"
            and target.get("id")
            for target in deployment_meta.get("targets") or []
        )
        selection = deployment_meta.get("selection") or {}
        selected_target = deployment_meta.get("selectedTarget") or {}
        sizing = deployment_meta.get("sizing") or {}
        sizing_target = sizing.get("target") or {}
        sizing_matches_target = not sizing_target or (
            sizing_target.get("id") == selected_target.get("id")
        )
        deployment_configuration_complete = (
            selection.get("status") == "selected"
            and sizing.get("status") == "completed"
            and sizing_matches_target
        )
        if (
            stage == "deployment_diagram"
            and not findings
            and has_completed_target
            and not deployment_configuration_complete
        ):
            return {
                "awaiting_input": True,
                "kind": "action_required",
                "message": (
                    "Compare the deployment targets in the artifact panel, then confirm the "
                    "target, VM size, and replicas together."
                ),
                "current_stage": stage,
                "deployment_configuration_required": True,
                "design": result,
                **progress_hints,
            }
        method_proposals = list(stage_validation.get("method_proposals") or [])
        finding_question = self._design_finding_question(app_id, str(stage or "design"), finding_details)
        if finding_question is not None:
            return {
                "awaiting_input": True,
                "kind": "question",
                "message": finding_question.prompt,
                "current_stage": stage,
                "feedback_question": finding_question.model_dump(mode="json"),
                "findings": findings,
                "finding_details": finding_details,
                "design": result,
                # An unanswered decision is itself a readiness blocker.
                "design_can_advance": False,
                "design_complete": False,
            }
        requires_revision = bool(findings)
        repair_history = stage_validation.get("repair_history") or {}
        repair_status = str(repair_history.get("status") or "")
        attempts = list(repair_history.get("attempts") or [])
        repair_state = {
            "status": (
                "WAITING_EXTERNAL"
                if repair_status == "WAITING_EXTERNAL"
                else "STALLED"
                if repair_status == "STALLED"
                else "ACTIVE"
                if findings
                else "COMPLETED"
            ),
            "attempt_count": len(attempts),
            "accepted_count": sum(
                attempt.get("outcome") in {"improved", "clean"}
                for attempt in attempts
                if isinstance(attempt, dict)
            ),
            "recent_attempts": attempts[-5:],
            "tried_strategies": sorted(
                {
                    str(attempt.get("strategy_key") or "")
                    for attempt in attempts
                    if isinstance(attempt, dict) and attempt.get("strategy_key")
                }
            ),
            "rejected_candidate_digests": sorted(
                {
                    str(attempt.get("candidate_digest") or "")
                    for attempt in attempts
                    if isinstance(attempt, dict)
                    and attempt.get("candidate_digest")
                    and attempt.get("outcome") not in {"improved", "clean"}
                }
            ),
            "finding_digest": stable_digest(findings),
            "stall_reason": repair_history.get("stall_reason") or "",
        }
        blocking_findings = [
            {
                "code": "design.validation",
                "stage": str(stage or "design"),
                "target_ids": [],
                "message": str(finding),
                "severity": "error",
                "repairable": True,
            }
            for finding in findings
        ]
        return {
            "awaiting_input": True,
            "kind": "action_required",
            "message": (
                f"The {str(stage or 'design').replace('_', ' ')} draft has "
                f"{len(findings)} findings. Review the draft and provide feedback "
                "before continuing."
                if requires_revision
                else "Review the current design artifacts, then send revision feedback "
                "or continue to the next stage."
            ),
            "current_stage": stage,
            "requires_revision": requires_revision,
            "blocking_findings": blocking_findings,
            "repair_state": repair_state,
            "findings": findings,
            "finding_details": finding_details,
            # Keep the pending approval decision on the workspace command as
            # well as in the artifact payload so the UI can offer an explicit
            # approval action instead of requiring a magic text phrase.
            "method_proposals": method_proposals,
            "design": result,
            **progress_hints,
        }

    @staticmethod
    def _missing_design_artifacts(state: Mapping[str, Any]) -> list[str]:
        """Return canonical design stages without their persisted source model."""
        class_model = state.get("extracted_bce_classes")
        erd_not_applicable = (
            isinstance(class_model, Mapping)
            and bool(class_model)
            and erd_disposition(class_model, state) == "not_applicable"
        )
        return [
            stage
            for stage in DESIGN_STAGES
            if not (stage == "erd" and erd_not_applicable)
            if not state.get(artifact_repository.STAGE_ARTIFACTS[stage]["source_key"])
        ]

    def _design_progress_hints(
        self, app_id: str, result: Mapping[str, Any]
    ) -> dict[str, bool]:
        """Derive design transitions from the saved graph checkpoint and models.

        A completed targeted revision is not equivalent to a completed design run.
        The graph checkpoint owns whether another design gate is ready, while the
        persisted source models prove that implementation has all of its inputs.
        """

        hints = {"design_can_advance": False, "design_complete": False}
        if not app_id:
            return hints
        try:
            checkpoint = session_status(app_id)
            state = cast(dict[str, Any], artifact_repository.load_state(app_id))
            result_app_id = str(result.get("app_id") or "")
            if result_app_id and result_app_id != app_id:
                return hints
            if checkpoint.get("exists"):
                snapshot = design_graph.get_state(
                    {"configurable": {"thread_id": app_id}}
                )
                snapshot_config = getattr(snapshot, "config", {}) or {}
                configurable = snapshot_config.get("configurable", {})
                checkpoint_state = getattr(snapshot, "values", {}) or {}
                checkpoint_id = (
                    configurable.get("checkpoint_id")
                    if isinstance(configurable, Mapping)
                    else None
                )
                result_checkpoint_id = result.get("checkpoint_id")
                # The design graph is app-thread scoped. Require both sides of
                # that identity before reusing checkpoint-owned evidence; its
                # digest-bound semantic evidence is still revalidated below.
                if (
                    isinstance(configurable, Mapping)
                    and configurable.get("thread_id") == app_id
                    and bool(checkpoint_id)
                    and (
                        not result_checkpoint_id
                        or result_checkpoint_id == checkpoint_id
                    )
                    and isinstance(checkpoint_state, Mapping)
                    and checkpoint_state.get("app_id") == app_id
                ):
                    stages = (
                        DESIGN_STAGES
                        if not checkpoint.get("active")
                        else (str(checkpoint.get("stage") or ""),)
                    )
                    for stage in stages:
                        if stage not in DESIGN_SPECS:
                            continue
                        check_key = DESIGN_SPECS[stage].check_key
                        checkpoint_check = checkpoint_state.get(check_key) if check_key else None
                        if not check_key or not isinstance(checkpoint_check, Mapping):
                            continue
                        # Deterministic findings are recalculated from current
                        # artifacts. Preserve only graph-owned semantic evidence;
                        # the readiness validator rejects stale model/contract digests.
                        evidence = checkpoint_check.get("semanticEvidence")
                        if stage == "class_diagram" and isinstance(evidence, Mapping):
                            hydrated_check = state.get(check_key)
                            state[check_key] = {
                                **(dict(hydrated_check) if isinstance(hydrated_check, Mapping) else {}),
                                "semanticEvidence": dict(evidence),
                            }
        except Exception:  # A presentation hint must not hide a command result.
            return hints

        missing = self._missing_design_artifacts(state)
        if not missing:
            readiness = design_readiness_report(state)
            hints["design_complete"] = bool(
                checkpoint.get("exists")
                and not checkpoint.get("active")
                and not checkpoint.get("retryable")
                and readiness.get("status") == "READY"
            )

        stage = str(checkpoint.get("stage") or result.get("current_stage") or result.get("stage") or "")
        if checkpoint.get("active") and stage in DESIGN_STAGES:
            stage_readiness = design_readiness_report(state, stages=[stage])
            hints["design_can_advance"] = bool(
                not stage_readiness.get("findings")
                and not checkpoint.get("retryable")
            )
        return hints

    def _with_design_progress_hints(
        self, app_id: str, result: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Merge persisted design-transition hints into a result snapshot."""

        visible = dict(result)
        design = visible.get("design")
        source = design if isinstance(design, Mapping) else visible
        return {**visible, **self._design_progress_hints(app_id, source)}

    def _confirm_change(self, command: dict[str, Any]) -> dict[str, Any]:
        action_id = str(command["payload"].get("action_id") or "")
        original = repository.get_command(action_id)
        if original is None or original["status"] != "AWAITING_INPUT":
            raise ValueError("The change request is missing or was already handled.")
        raw_plan = original["payload"].get("revision_plan")
        if isinstance(raw_plan, dict):
            plan = RevisionPlan.model_validate(raw_plan)
            app_id = str(command["app_id"])
            if plan.status != "needs_confirmation":
                raise ValueError("The stored revision plan is not awaiting confirmation.")
            raw_interpretation = original["payload"].get("revision_interpretation")
            interpretation = (
                RevisionInterpretation.model_validate(raw_interpretation)
                if isinstance(raw_interpretation, dict)
                else None
            )
            origin_stage = original["payload"].get("revision_origin_stage")
            if origin_stage not in {"requirements", "design", "implementation", "testing"}:
                origin_stage = None
            if interpretation is None:
                return self._stale_revision_result(plan)
            tools = ProjectTools(app_id)
            if origin_stage is None:
                plan_is_valid = validate_plan(tools, plan, interpretation)
            else:
                plan_is_valid = validate_plan(
                    tools,
                    plan,
                    interpretation,
                    origin_stage=origin_stage,
                )
            if not plan_is_valid:
                return self._stale_revision_result(plan)
            authority_targets = plan.authority_targets or plan.requested_targets
            owners = {target.owner for target in authority_targets}
            if len(owners) != 1:
                raise ValueError("An approved revision plan must have one delivery owner.")
            owner = owners.pop()
            feedback = str(original["payload"].get("text") or "").strip()
            if not feedback:
                raise ValueError("The approved revision plan has no instruction.")
            if (
                plan.execution_mode == "stage_rewind"
                and len(authority_targets) == 1
                and authority_targets[0].kind == "design_stage"
            ):
                rewind_stage = authority_targets[0].element_id
                revised = revise_design_stage_session(app_id, rewind_stage, feedback)
                result = {
                    "message": (
                        f"Regenerated {rewind_stage.replace('_', ' ')} and applied the "
                        "approved feedback."
                    ),
                    "design": revised,
                }
                return self._attach_revision_execution(app_id, plan, result)
            refs = [target.ref for target in authority_targets]
            delegated_payload = {
                **dict(original["payload"]),
                "text": feedback,
                "action_id": action_id,
                "conversation_intent": {
                    "intent": "revise",
                    "targets": refs,
                    "instruction": feedback,
                },
                "validated_targets": [
                    target.model_dump(mode="json") for target in authority_targets
                ],
            }
            delegated_payload.pop("_conversation_outcome", None)
            if owner == "design":
                revision_instructions = delegated_payload.get("revision_instructions")
                revision_instructions = (
                    revision_instructions
                    if isinstance(revision_instructions, dict)
                    else {}
                )
                delivery = design_revision_payload(
                    plan,
                    feedback,
                    instructions_by_ref=revision_instructions,
                    patch_intents=interpretation.patch_intents,
                )
                delegated_payload["context"] = {
                    "validated_target_feedbacks": [
                        revision.model_dump(mode="json")
                        for revision in delivery.revisions
                    ],
                    "approved_authority_targets": list(
                        delivery.approved_authority_targets
                    ),
                    "approved_downstream_targets": list(
                        delivery.approved_downstream_targets
                    ),
                }
            delegated = {
                **command,
                "action": "message",
                "stage": owner,
                "payload": delegated_payload,
            }
            result = self._stage_message(delegated, advance=False)
            response = self._attach_revision_execution(app_id, plan, result)
            if (
                origin_stage == "implementation"
                and owner == "design"
                and response.get("awaiting_input") is True
            ):
                response["resume_implementation"] = True
            return self._attach_downstream_revision_handoff(
                delegated, plan, interpretation, response
            )
        context = original["payload"].get("context") or {}
        feedback = str(original["payload"].get("text") or "").strip()
        app_id = str(command["app_id"])
        target_feedbacks = self._sequence_target_feedbacks(context)
        if target_feedbacks:
            result = revise_design_elements(app_id, BatchReviseRequest(revisions=target_feedbacks))
            return {
                "message": "Revised the selected use-case diagrams and trace-linked artifacts.",
                "design": result,
            }
        element_ref = context.get("element_ref")
        if element_ref:
            result = revise_design_element(
                app_id, ReviseRequest(target=str(element_ref), feedback=feedback)
            )
            return {
                "message": "Revised the selected design element and affected downstream artifacts.",
                "design": result,
            }
        stage = str(context.get("artifact_stage") or context.get("stage") or "")
        if stage not in {
            "sequence_diagram",
            "api_spec",
            "erd",
            "deployment_diagram",
        }:
            raise ValueError(
                "Only a traceable design element or design stage can currently be rewound."
            )
        result = revise_design_stage_session(app_id, stage, feedback)
        return {
            "message": "Returned to the selected design stage and applied the feedback.",
            "design": result,
        }

    def _plan_downstream_revision(self, command: dict[str, Any]) -> dict[str, Any]:
        """Build a fresh Design plan after a reviewed local UC-spec revision."""

        source_id = str(command["payload"].get("action_id") or "")
        source = repository.get_command(source_id)
        if (
            source is None
            or source.get("status") != "AWAITING_INPUT"
            or str(source.get("app_id") or "") != str(command["app_id"])
        ):
            raise ValueError("The requirements revision review is missing or already handled.")
        handoff = (source.get("result") or {}).get("downstream_revision_handoff")
        if not isinstance(handoff, dict):
            raise TypeError("This review does not offer downstream revision planning.")
        source_targets = [
            dict(target)
            for target in handoff.get("source_targets") or []
            if isinstance(target, dict)
        ]
        if not source_targets or any(
            not isinstance(target.get("artifact_version_id"), int)
            for target in source_targets
        ):
            raise ValueError("The requirements revision has no exact source reference.")

        app_id = str(command["app_id"])
        tools = ProjectTools(app_id)
        entries = tools.design_entry_targets_for_requirements(source_targets)
        if entries is None:
            clarification = Clarification(
                question=(
                    "The revised use-case specification changed after this review. "
                    "Submit the feedback again from the current artifact."
                )
            )
            return {
                "awaiting_input": True,
                "kind": "question",
                "message": clarification.question,
                "conversation": {
                    "clarification": clarification.model_dump(mode="json")
                },
            }
        requested_effect = str(handoff.get("requested_effect") or "").strip()
        if not requested_effect:
            raise ValueError("The requirements revision has no accepted instruction.")
        target_refs = (
            [target.ref for target in entries]
            if entries
            else ["design_stage:class_diagram"]
        )
        scope_label = "RTM-linked design" if entries else "full class design"
        instruction = f"Reflect the accepted requirements revision in the {scope_label}: {requested_effect}"
        interpretation = RevisionInterpretation(
            targets=target_refs,
            semantic_scope=str(handoff.get("semantic_scope") or "unknown"),
            requested_effect=instruction,
            change_type=str(handoff.get("change_type") or "unknown"),
        )
        plan = plan_revision(tools, interpretation)
        if plan.status != "needs_confirmation":
            clarification = Clarification(
                question=(
                    plan.explanation
                    if plan.status in {"needs_clarification", "unsupported"}
                    else "The Design scope could not be held for explicit confirmation."
                ),
                candidates=[target.display_label for target in plan.authority_targets],
            )
            return {
                "awaiting_input": True,
                "kind": "question",
                "message": clarification.question,
                "conversation": {
                    "clarification": clarification.model_dump(mode="json")
                },
            }

        payload = {
            **dict(command["payload"]),
            "text": instruction,
            "revision_interpretation": interpretation.model_dump(mode="json"),
            "revision_plan": plan.model_dump(mode="json"),
            "validated_targets": [
                target.model_dump(mode="json")
                for target in (plan.authority_targets or plan.requested_targets)
            ],
        }
        command["payload"] = payload
        repository.update_command(str(command["command_id"]), payload=payload)
        return self._revision_plan_result(str(command["command_id"]), plan)

    @staticmethod
    def _revision_plan_result(
        command_id: str,
        plan: RevisionPlan,
    ) -> dict[str, Any]:
        return {
            "awaiting_input": True,
            "kind": "action_required",
            "action": "confirm_change",
            "action_id": command_id,
            "message": plan.explanation,
            "revision_plan": plan.model_dump(mode="json"),
            "requested_targets": [
                target.model_dump(mode="json") for target in plan.requested_targets
            ],
            "authority_targets": [
                target.model_dump(mode="json") for target in plan.authority_targets
            ],
            "downstream_targets": [
                target.model_dump(mode="json") for target in plan.downstream_targets
            ],
        }

    @staticmethod
    def _attach_downstream_revision_handoff(
        command: dict[str, Any],
        plan: RevisionPlan,
        interpretation: RevisionInterpretation | None,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Replace generic Requirements advance with a fresh-plan handoff marker."""

        authority = plan.authority_targets or plan.requested_targets
        if (
            command.get("stage") != "requirements"
            or interpretation is None
            or not authority
            or any(target.kind != "use_case_spec" for target in authority)
            or result.get("awaiting_input") is not True
        ):
            return result
        versions = (result.get("revision_execution") or {}).get("artifact_versions") or {}
        return {
            **result,
            "downstream_revision_handoff": {
                "source_targets": [
                    {
                        "ref": target.ref,
                        "artifact_version_id": versions.get(target.artifact_type),
                    }
                    for target in authority
                ],
                "semantic_scope": interpretation.semantic_scope,
                "requested_effect": str(command["payload"].get("text") or "").strip()
                or interpretation.requested_effect,
                "change_type": interpretation.change_type,
            },
        }

    @staticmethod
    def _stale_revision_result(plan: RevisionPlan) -> dict[str, Any]:
        clarification = Clarification(
            question=(
                "The project artifacts changed after this revision plan was created. "
                "Please submit the revision again so it can be planned from the latest version."
            ),
            candidates=[],
        )
        return {
            "awaiting_input": True,
            "kind": "question",
            "message": clarification.question,
            "conversation": {"clarification": clarification.model_dump(mode="json")},
            "stale_revision_plan": plan.plan_digest,
        }

    @staticmethod
    def _attach_revision_execution(
        app_id: str,
        plan: RevisionPlan,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        design = result.get("design")
        design = design if isinstance(design, dict) else {}
        fresh_tools = ProjectTools(app_id)
        snapshot = fresh_tools.revision_snapshot()
        changed = result.get("changed") or design.get("changed") or result.get("saved_stages")
        changed_stages = [str(stage) for stage in changed or [] if str(stage)]
        if not changed_stages:
            changed_stages = [
                artifact_repository.STAGE_BY_ARTIFACT_TYPE[artifact_type]
                for artifact_type, current_version in snapshot.get(
                    "artifact_versions", {}
                ).items()
                if plan.artifact_versions.get(artifact_type) != current_version
                and artifact_type in artifact_repository.STAGE_BY_ARTIFACT_TYPE
            ]
        touched = result.get("touched") or design.get("touched")
        touched_targets = (
            {
                str(stage): [str(ref) for ref in refs or []]
                for stage, refs in touched.items()
            }
            if isinstance(touched, dict)
            else {}
        )
        regenerated: dict[str, list[str]] = {}
        stale: dict[str, list[str]] = {}
        target_remap: dict[str, str] = {}
        for target in [
            *plan.requested_targets,
            *plan.authority_targets,
            *plan.downstream_targets,
        ]:
            current = fresh_tools.current_revision_target(target)
            if current is not None and current.ref != target.ref:
                target_remap[target.ref] = current.ref
        for target in plan.downstream_targets:
            current = fresh_tools.current_revision_target(target)
            if (
                current is not None
                and current.artifact_version_id != target.artifact_version_id
            ):
                regenerated.setdefault(current.owner, []).append(current.ref)
            else:
                stale.setdefault(target.owner, []).append(target.ref)
        execution = RevisionExecutionResult(
            changed_stages=changed_stages,
            touched_targets=touched_targets,
            regenerated_targets=regenerated,
            stale_targets=stale,
            target_remap=target_remap,
            artifact_versions=dict(snapshot.get("artifact_versions") or {}),
        )
        response = {
            **result,
            "revision_plan": plan.model_dump(mode="json"),
            "revision_execution": execution.model_dump(mode="json"),
        }
        if not changed_stages:
            response.update(
                {
                    "revision_no_effect": True,
                    "message": (
                        "The approved feedback did not change the current artifacts. "
                        "It may already be satisfied or the current editor may not support "
                        "that change; refine the feedback or continue without it."
                    ),
                }
            )
        return response

    @staticmethod
    def _implementation_progress_snapshot(job: dict[str, Any]) -> dict[str, Any]:
        """내구성 체크포인트에서 화면에 표시할 구현 진행 단계를 만든다.

        공개 작업 상태는 phase 완료 시점에만 갱신될 수 있다. 실행 디렉터리의
        ``workflow-state.json``과 agent event journal을 함께 읽어 현재 phase와
        실제 편집 중인 파일을 phase 실행 중에도 표시한다.
        """
        job_id = str(job.get("job_id") or "")
        private_job = job
        if job_id:
            try:
                private_job = implementation_worker._read(job_id)
            except Exception:  # Progress reporting must not interrupt a job.
                private_job = job

        run_root = str(private_job.get("run_root") or job.get("run_root") or "").strip()
        workflow = private_job.get("workflow") or job.get("workflow")
        run_path = Path(run_root) if run_root else None
        if run_path is not None:
            state_path = run_path / "reports" / "workflow-state.json"
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = None
            if isinstance(state, dict):
                workflow = state
        agent_results = _implementation_agent_results(run_path) if run_path else []

        updates: list[dict[str, Any]] = []

        def add_update(
            step: str,
            label: str,
            status: str,
            detail: str = "",
            **metadata: object,
        ) -> None:
            updates.append(
                {
                    "step": step,
                    "label": label,
                    "status": status,
                    "detail": detail,
                    **metadata,
                }
            )

        job_status = str(job.get("status") or private_job.get("status") or "")
        terminal_failure = job_status in TERMINAL_JOB_STATUSES - {
            "COMPLETED",
            "NEEDS_INPUT",
        }
        failure_error = str(private_job.get("error") or job.get("error") or "").strip()
        failure_lines = [line.strip() for line in failure_error.splitlines() if line.strip()]
        meaningful_failure_lines = [
            line
            for line in failure_lines
            if re.search(r"\b(error|exception|failed|timeout|denied)\b", line, re.IGNORECASE)
        ]
        failure_detail = (
            (meaningful_failure_lines[-1] if meaningful_failure_lines else failure_lines[-1])[-500:]
            if failure_lines
            else "The implementation job did not complete."
        )
        live_progress = job.get("progress")
        progress_message = (
            str(live_progress.get("message") or "") if isinstance(live_progress, dict) else ""
        )
        phase_state: dict[str, dict[str, Any]] = {
            phase_id: {"status": "pending", "detail": ""}
            for phase_id, _label in _IMPLEMENTATION_PROGRESS_PHASES
        }
        workflow_tasks: list[dict[str, Any]] = []
        workflow_complete = job_status == "COMPLETED"

        def normalized_status(value: object) -> str:
            status = str(value or "").upper()
            if status in {"SUCCEEDED", "COMPLETED", "COMPLETE"}:
                return "completed"
            if status in {
                "FAILED",
                "INTERRUPTED",
                "TIMEOUT",
                "CANCELLED",
                "REJECTED",
                "NEEDS_REVIEW",
            }:
                return "failed"
            if status in {"RUNNING", "FINALIZING", "VERIFYING"}:
                return "running"
            return "pending"

        if isinstance(workflow, dict):
            workflow_status = str(workflow.get("status") or "").upper()
            current_phase = str(workflow.get("currentPhase") or "")
            current_phases = {
                str(value)
                for value in workflow.get("currentPhases", [])
                if isinstance(value, str)
            }
            if current_phase:
                current_phases.add(current_phase)
            tasks = [item for item in workflow.get("tasks", []) if isinstance(item, dict)]
            workflow_tasks = tasks
            phase_statuses = {
                str(phase.get("phaseId") or ""): str(phase.get("status") or "").upper()
                for phase in workflow.get("phases", [])
                if isinstance(phase, dict)
            }
            for phase_id, _label in _IMPLEMENTATION_PROGRESS_PHASES:
                evidence = []
                if phase_id in phase_statuses and phase_statuses[phase_id] != "UNPLANNED":
                    evidence.append(phase_statuses[phase_id])
                for task in tasks:
                    task_owner = str(task.get("owner") or task.get("phase") or "")
                    task_type = str(task.get("taskType") or task.get("task_type") or "")
                    if task_owner == phase_id or task_type == f"{phase_id}-implementation":
                        evidence.append(str(task.get("status") or ""))
                statuses = {normalized_status(value) for value in evidence}
                if "failed" in statuses:
                    phase_state[phase_id]["status"] = "failed"
                elif "running" in statuses or (
                    phase_id in current_phases and workflow_status == "RUNNING"
                ):
                    phase_state[phase_id]["status"] = "running"
                elif evidence and statuses <= {"completed"}:
                    phase_state[phase_id]["status"] = "completed"

            workflow_complete = workflow_status == "COMPLETE" or (
                workflow_status == "READY" and implementation_worker._workflow_is_complete(workflow)
            )
            activity = workflow.get("currentActivity")
            if isinstance(activity, dict) and str(activity.get("id") or ""):
                activity_owner = str(activity.get("owner") or activity.get("phase") or "")
                if activity_owner not in phase_state:
                    # Older checkpoints did not persist activity ownership. Their
                    # canonical currentPhase is a safe fallback; activity IDs are
                    # free-form labels and must not be parsed as routing metadata.
                    activity_owner = current_phase if current_phase in phase_state else ""
                if activity_owner in phase_state:
                    phase_state[activity_owner]["status"] = normalized_status(
                        activity.get("status") or "RUNNING"
                    )
                    phase_state[activity_owner]["detail"] = str(activity.get("detail") or "")

            if workflow_status == "FINALIZING":
                phase_state["integration"]["status"] = "running"
        elif job_status in {
            "RUNNING",
            "PLANNING",
            "VALIDATING_INPUT",
            "GENERATING_SOURCES",
            "PREPARING_BUILD",
            "VERIFYING",
            "REUSING_GENERATED_RUN",
            "PREPARING_FEEDBACK",
        }:
            phase_state["backend"] = {
                "status": "running",
                "detail": progress_message or "Backend implementation is in progress.",
            }

        if workflow_complete:
            for state in phase_state.values():
                state["status"] = "completed"
                state["detail"] = ""

        repair = private_job.get("owner_repair")
        repair_owner = str(repair.get("owner") or "") if isinstance(repair, dict) else ""
        repairing = (
            repair_owner in {"backend", "frontend"}
            and not terminal_failure
            and (
                phase_state[repair_owner]["status"] != "completed"
                or job_status in {"QUEUED", "PLANNING"}
            )
        )
        if repairing:
            workflow_complete = False
            phase_state[repair_owner]["status"] = "running"
            phase_state[repair_owner]["detail"] = (
                f"Repairing with the existing {repair_owner} owner conversation."
            )
            phase_state["integration"].update(status="pending", detail="")

        for result in agent_results:
            result_owner = str(result.get("owner") or "")
            task_type = str(result.get("task_type") or "")
            if result_owner not in {"backend", "frontend"}:
                if task_type == "backend-implementation":
                    result_owner = "backend"
                elif task_type == "frontend-implementation":
                    result_owner = "frontend"
            if result_owner not in {"backend", "frontend"}:
                continue
            verification = result.get("verification")
            verification = verification if isinstance(verification, dict) else {}
            raw_command = verification.get("command")
            if isinstance(raw_command, list):
                recent_command = " ".join(
                    str(part) for part in raw_command[:12] if isinstance(part, (str, int, float))
                )[:200]
            elif isinstance(raw_command, str):
                recent_command = raw_command.strip()[:200]
            else:
                recent_command = ""
            raw_verification_status = str(
                verification.get("status") or verification.get("gateStatus") or ""
            ).upper()
            exit_code = verification.get("exitCode")
            if raw_verification_status in {"PASSED", "SUCCEEDED", "COMPLETED", "PASS"}:
                verification_status = "passed"
            elif raw_verification_status in {"FAILED", "FAIL", "ERROR"}:
                verification_status = "failed"
            elif isinstance(exit_code, int) and not isinstance(exit_code, bool):
                verification_status = "passed" if exit_code == 0 else "failed"
            else:
                verification_status = ""
            if recent_command:
                phase_state[result_owner]["recent_command"] = recent_command
            if verification_status:
                phase_state[result_owner]["verification_status"] = verification_status
                check_detail = f"Last check {verification_status}"
                if recent_command:
                    check_detail += f": {recent_command}"
                check_detail += "."
                existing_detail = str(phase_state[result_owner].get("detail") or "")
                if not existing_detail and phase_state[result_owner]["status"] == "running":
                    owner_label = dict(_IMPLEMENTATION_PROGRESS_PHASES)[result_owner]
                    existing_detail = f"{owner_label} is in progress."
                phase_state[result_owner]["detail"] = " ".join(
                    item for item in (existing_detail, check_detail) if item
                )

        if terminal_failure:
            failed_phase = next(
                (
                    phase_id
                    for phase_id, _label in _IMPLEMENTATION_PROGRESS_PHASES
                    if phase_state[phase_id]["status"] == "running"
                ),
                next(
                    (
                        phase_id
                        for phase_id, _label in _IMPLEMENTATION_PROGRESS_PHASES
                        if phase_state[phase_id]["status"] != "completed"
                    ),
                    "integration",
                ),
            )
            phase_state[failed_phase].update(status="failed", detail=failure_detail)

        for phase_id, label in _IMPLEMENTATION_PROGRESS_PHASES:
            state = phase_state[phase_id]
            detail = state["detail"]
            if state["status"] == "running" and not detail:
                detail = f"{label} is in progress."
            add_update(
                f"phase-{phase_id}",
                label,
                state["status"],
                detail,
                implementation_owner=phase_id,
                repairing=bool(repairing and repair_owner == phase_id),
                **{
                    key: state[key]
                    for key in ("recent_command", "verification_status")
                    if state.get(key)
                },
            )

        current_file: str | None = None
        owner_is_editing = any(
            phase_state[phase_id]["status"] == "running"
            for phase_id in ("backend", "frontend")
        )
        if workflow_complete or job_status in TERMINAL_JOB_STATUSES or not owner_is_editing:
            run_path = None
        if run_path is not None:
            events_dir = run_path / "reports" / "agent-executions"
            latest_path: Path | None = None
            active_owner = next(
                (
                    phase_id
                    for phase_id in ("backend", "frontend")
                    if phase_state[phase_id]["status"] == "running"
                ),
                "",
            )
            active_task_id = next(
                (
                    str(task.get("taskId") or task.get("task_id") or "")
                    for task in workflow_tasks
                    if str(task.get("owner") or task.get("phase") or "") == active_owner
                    and normalized_status(task.get("status")) == "running"
                ),
                "",
            )
            candidates = (
                events_dir.glob(f"{active_task_id}*.events.jsonl")
                if active_task_id
                else events_dir.glob("*.events.jsonl")
            )
            for candidate in sorted(candidates):
                try:
                    if (
                        latest_path is None
                        or candidate.stat().st_mtime >= latest_path.stat().st_mtime
                    ):
                        latest_path = candidate
                except OSError:
                    continue
            if latest_path is not None:
                try:
                    lines = latest_path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    lines = []
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event = payload.get("event") if isinstance(payload, dict) else None
                    tool_name = str(payload.get("tool") or "") if isinstance(payload, dict) else ""
                    if not isinstance(event, dict):
                        continue
                    path_sources = [event]
                    path_sources.extend(
                        value
                        for value in (event.get("action"), event.get("observation"))
                        if isinstance(value, dict)
                    )
                    path_value = next(
                        (
                            value
                            for source in path_sources
                            for value in (
                                source.get("path"),
                                source.get("file_path"),
                                source.get("filePath"),
                            )
                            if isinstance(value, str) and value.strip()
                        ),
                        None,
                    )
                    if not isinstance(path_value, str) or not path_value.strip():
                        continue
                    if "file_editor" not in tool_name:
                        continue
                    current_file = path_value.strip().replace("\\", "/")
                    application_marker = "/application/"
                    if application_marker in current_file:
                        current_file = "application/" + current_file.split(application_marker, 1)[1]

        if current_file:
            current_owner = next(
                (
                    phase_id
                    for phase_id in ("backend", "frontend")
                    if phase_state[phase_id]["status"] == "running"
                ),
                "",
            )
            current_update = next(
                (item for item in updates if item["step"] == f"phase-{current_owner}"),
                None,
            )
            if current_update is not None:
                file_name = Path(current_file).name
                current_update["current_file"] = current_file
                current_update["current_class"] = Path(file_name).stem

        summary = next(
            (item for item in updates if item["status"] == "failed"),
            next(
                (item for item in updates if item["status"] == "running"),
                updates[-1],
            ),
        )
        snapshot: dict[str, Any] = {
            "updates": updates,
            "progress_card_label": "Implementation progress",
            "text": summary["detail"] or summary["label"],
            "progress_detail": summary["detail"] or summary["label"],
            "progress_status": summary["status"],
        }
        if current_file:
            file_name = Path(current_file).name
            snapshot["current_file"] = current_file
            snapshot["current_class"] = Path(file_name).stem
        if agent_results:
            snapshot["agent_results"] = agent_results
        return snapshot

    def _implementation_repair_outcomes(
        self,
        latest_job: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """부모 작업을 따라가며 이전 수리의 변경 범위를 짧게 만든다.

        OpenHands의 마지막 답변은 화면과 실행 기록에 그대로 남는다. 다음 수리 LLM은 최신
        source를 직접 읽을 수 있고 현재 실패 증거도 따로 받으므로, 자유형 자기 설명을 다시
        보내지 않는다. 어떤 작업이 어떤 파일을 바꿨는지만 알려 주면 같은 범위를 살피면서도
        이미 존재하는 코드를 기준으로 다른 해결책을 찾을 수 있다.
        """

        recent: list[dict[str, Any]] = []
        older_by_signature: dict[str, dict[str, Any]] = {}
        current = latest_job
        seen_job_ids: set[str] = set()
        while current.get("job_type") == "FEEDBACK_REVISION":
            job_id = str(current.get("job_id") or "")
            if job_id and job_id in seen_job_ids:
                break
            if job_id:
                seen_job_ids.add(job_id)

            progress = self._implementation_progress_snapshot(current)
            agent_results = [
                item
                for item in progress.get("agent_results") or []
                if isinstance(item, dict)
            ]
            changed_files = sorted(
                {
                    str(path)
                    for item in agent_results
                    for path in item.get("changed_files") or []
                    if isinstance(path, str) and path
                }
            )
            outcome = {
                "job_id": job_id,
                "status": str(current.get("status") or ""),
                "changed_files": changed_files,
            }
            if len(recent) < 2:
                recent.append(outcome)
            else:
                compact = {
                    "changed_files": changed_files,
                    "status": str(current.get("status") or ""),
                }
                signature = stable_digest(compact)
                stored = older_by_signature.setdefault(
                    signature,
                    {"repetitions": 0, **compact},
                )
                stored["repetitions"] = int(stored["repetitions"]) + 1

            parent_job_id = str(current.get("parent_job_id") or "")
            if not parent_job_id:
                break
            try:
                current = implementation_worker.get(parent_job_id)
            except Exception:
                # 오래된 작업 파일이 정리됐더라도 현재 수리까지 막지는 않는다.
                break
        return recent, list(older_by_signature.values())

    @staticmethod
    def _testing_repair_request(
        app_id: str,
        result: dict[str, Any],
        blockers: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str, str, list[str], dict[str, Any]]:
        """같은 원인에 속한 finding을 한 구현 작업과 동일 재검사 입력으로 묶는다."""

        gate_order = {
            "testing.static": 0,
            "testing.package": 1,
            "testing.iac": 2,
            "testing.dynamic-functional": 3,
            "testing.dynamicFunctional": 3,
        }
        primary = min(
            blockers,
            key=lambda item: gate_order.get(
                str(item.get("code") or item.get("stage") or ""), 99
            ),
        )
        code = str(primary.get("code") or primary.get("stage") or "")
        selected = [
            item
            for item in blockers
            if str(item.get("code") or item.get("stage") or "") == code
        ] or [primary]
        owners = {
            str(item.get("implementation_owner") or "").strip()
            for item in selected
            if str(item.get("implementation_owner") or "").strip()
        }
        if len(owners) != 1:
            raise ValueError(
                "Testing repair evidence must declare exactly one implementation owner."
            )
        repair_owner = next(iter(owners))
        task_type = {
            "testing.static": "testing-static",
            "testing.package": "testing-package",
            "testing.iac": "testing-iac",
            "testing.dynamic-functional": "testing-dynamic-functional",
            "testing.dynamicFunctional": "testing-dynamic-functional",
        }.get(code, "testing-dynamic-functional")
        file_hints = list(
            dict.fromkeys(
                str(path)
                for item in selected
                for path in item.get("file_hints") or []
                if isinstance(path, str) and path
            )
        )

        job = result.get("job")
        job = job if isinstance(job, dict) else {}
        testing_result = job.get("result")
        testing_result = testing_result if isinstance(testing_result, dict) else {}
        testing_input = job.get("testing_input") or testing_result.get("testingInput") or {}
        profile: dict[str, Any] = {
            "app_id": app_id,
            "testing_input": testing_input if isinstance(testing_input, dict) else {},
        }
        if task_type == "testing-dynamic-functional":
            candidate_plan = primary.get("candidate_plan")
            if isinstance(candidate_plan, dict):
                profile["candidate_plan"] = candidate_plan
            workflow_inputs = primary.get("workflow_inputs")
            if isinstance(workflow_inputs, dict):
                profile["workflow_inputs"] = workflow_inputs
            input_values = primary.get("input_values")
            if isinstance(input_values, dict):
                profile["input_values"] = input_values
            failed_workflow_id = str(primary.get("failed_workflow_id") or "").strip()
            failed_step_id = str(primary.get("failed_step_id") or "").strip()
            if failed_workflow_id:
                profile["failed_workflow_id"] = failed_workflow_id
            if failed_step_id:
                profile["failed_step_id"] = failed_step_id
            evidence = primary.get("evidence")
            evidence = evidence if isinstance(evidence, dict) else {}
            finding = evidence.get("finding")
            finding = finding if isinstance(finding, dict) else {}
            log_reference = finding.get("applicationLogRef")
            if (
                isinstance(log_reference, dict)
                and isinstance(log_reference.get("ref"), str)
            ):
                profile["application_log_ref"] = log_reference["ref"]
            if "failed_workflow_id" not in profile:
                failed_workflow_id = str(evidence.get("failedWorkflowId") or "").strip()
                if failed_workflow_id:
                    profile["failed_workflow_id"] = failed_workflow_id
            if "failed_step_id" not in profile:
                failed_step_id = str(evidence.get("failedStepId") or "").strip()
                if failed_step_id:
                    profile["failed_step_id"] = failed_step_id
        return selected, repair_owner, task_type, file_hints, profile

    def _testing_repair_owner_groups(
        self,
        app_id: str,
        result: dict[str, Any],
        blockers: list[dict[str, Any]],
    ) -> list[list[dict[str, Any]]]:
        """Partition a same-gate SUT aggregate by its declared source task."""

        grouped: dict[str, list[dict[str, Any]]] = {}
        for blocker in blockers:
            (
                _selected,
                _owner,
                _task_type,
                file_hints,
                _profile,
            ) = self._testing_repair_request(app_id, result, [blocker])
            target_refs, _normalized_hints = self._testing_implementation_repair_targets(
                app_id, [blocker], file_hints
            )
            declared = {
                ref.removeprefix("task:")
                for ref in target_refs
                if isinstance(ref, str) and ref.startswith("task:")
            }
            if len(declared) != 1:
                raise ValueError(
                    "Testing repair evidence must declare exactly one source task per finding."
                )
            grouped.setdefault(next(iter(declared)), []).append(blocker)
        return list(grouped.values())

    @staticmethod
    def _testing_implementation_repair_targets(
        app_id: str,
        blockers: list[dict[str, Any]],
        file_hints: list[str],
    ) -> tuple[list[str], list[str]]:
        """Map testing evidence back to current implementation-owned RTM targets."""

        trace_refs = list(
            dict.fromkeys(
                str(ref)
                for blocker in blockers
                for ref in blocker.get("trace_refs") or []
                if isinstance(ref, str) and ref
            )
        )
        evidence = {
            "repair_owner": (
                "implementation"
                if blockers
                and all(
                    blocker.get("repair_owner") == "implementation"
                    or blocker.get("defect_class") == "SUT_DEFECT"
                    for blocker in blockers
                )
                else "testing"
            ),
            "trace_refs": trace_refs,
            "file_hints": file_hints,
        }
        candidate_refs = list(
            dict.fromkeys(
                [*trace_refs, *(f"file:{path}" for path in file_hints)]
            )
        )
        tools = ProjectTools(app_id)
        validation = tools.validate_targets(candidate_refs)
        implementation_refs = list(
            dict.fromkeys(
                str(item.get("canonical_ref") or "")
                for item in validation.get("targets") or []
                if isinstance(item, dict)
                and item.get("valid")
                and item.get("owner") == "implementation"
                and str(item.get("canonical_ref") or "")
            )
        )
        targets = tools.normalize_revision_targets(implementation_refs)
        payload = repair_payload_from_testing_evidence(evidence, targets)
        return list(payload.confirmed_target_refs), list(payload.repair_file_hints)

    @staticmethod
    def _testing_implementation_feedback(
        result: dict[str, Any],
        blockers: list[dict[str, Any]],
        *,
        previous_repair_results: list[dict[str, Any]] | None = None,
        older_repair_summaries: list[dict[str, Any]] | None = None,
    ) -> str:
        """제품 수리 에이전트에 실패 증거와 고정 테스트 계획을 전달한다."""
        evidence = []
        target_ids: list[str] = []
        file_hints: list[str] = []
        trace_refs: list[str] = []
        execution_evidence: list[dict[str, Any]] = []
        candidate_plan: dict[str, Any] = {}
        for blocker in blockers:
            message = str(blocker.get("message") or "").strip()
            if message:
                evidence.append(message)
            target_ids.extend(
                str(item)
                for item in blocker.get("target_ids") or []
                if isinstance(item, str) and item
            )
            file_hints.extend(
                str(item)
                for item in blocker.get("file_hints") or []
                if isinstance(item, str) and item
            )
            trace_refs.extend(
                str(item)
                for item in blocker.get("trace_refs") or []
                if isinstance(item, str) and item
            )
            if isinstance(blocker.get("evidence"), dict) and blocker["evidence"]:
                copied_evidence = dict(blocker["evidence"])
                copied_finding = copied_evidence.get("finding")
                if isinstance(copied_finding, dict):
                    # 전체 application log는 별도 읽기 전용 파일로 전달한다. prompt에
                    # 일부를 다시 붙이면 에이전트가 그 excerpt만 보고 파일을 조사하지 않는다.
                    copied_evidence["finding"] = {
                        key: value
                        for key, value in copied_finding.items()
                        if key != "applicationLogExcerpt"
                    }
                copied_evidence.pop("applicationLogExcerpt", None)
                execution_evidence.append(copied_evidence)
            if not candidate_plan and isinstance(blocker.get("candidate_plan"), dict):
                candidate_plan = dict(blocker["candidate_plan"])
        raw_history = result.get("repair_state")
        raw_history = raw_history if isinstance(raw_history, dict) else {}
        history = {
            "status": raw_history.get("status"),
            "attempt_count": raw_history.get("attempt_count", 0),
            "accepted_count": raw_history.get("accepted_count", 0),
            "older_attempt_count": raw_history.get("older_attempt_count", 0),
            "recent_attempts": list(raw_history.get("recent_attempts") or [])[-2:],
        }
        needs_fixture = any(
            item.get("code") == "TEST_PROFILE_DATA_UNAVAILABLE"
            for item in execution_evidence
        )
        blocker_codes = {
            str(blocker.get("code") or "")
            for blocker in blockers
            if isinstance(blocker, dict)
        }
        if blocker_codes and blocker_codes <= {"testing.static", "testing.iac"}:
            opening = (
                "The generated deployment infrastructure failed its assigned static or "
                "OpenTofu check. Repair only the trace-linked IaC files. Keep the selected "
                "deployment topology and application contracts unchanged."
            )
        elif blocker_codes == {"testing.package"}:
            opening = (
                "The generated deployment package failed its assigned syntax or package "
                "check. Repair only the trace-linked deployment files and keep application "
                "behavior unchanged."
            )
        elif needs_fixture:
            opening = (
                "The generated application's test profile lacks prerequisite data for a "
                "preserved functional flow. Add the smallest test-profile-only fixture or "
                "startup setup that makes the documented success path executable. Do not "
                "change production behavior, API contracts, or test acceptance conditions."
            )
        else:
            opening = (
                "The generated application failed a preserved functional test. Repair only "
                "the production implementation. Keep the existing contracts and test "
                "acceptance conditions unchanged."
            )
        parts = [
            opening,
            "Failure evidence:\n- " + "\n- ".join(evidence or ["Testing gate failed."]),
        ]
        if target_ids:
            parts.append("Confirmed artifact targets:\n- " + "\n- ".join(dict.fromkeys(target_ids)))
        if trace_refs:
            parts.append("Related artifact references:\n- " + "\n- ".join(dict.fromkeys(trace_refs)))
        if file_hints:
            parts.append("Start investigation with these trace-linked files:\n- " + "\n- ".join(dict.fromkeys(file_hints)))
        if execution_evidence:
            parts.append(
                "Exact failing check evidence:\n"
                + json.dumps(execution_evidence, ensure_ascii=False, sort_keys=True)
            )
        if candidate_plan:
            parts.append(
                "Preserved functional test plan:\n"
                + json.dumps(candidate_plan, ensure_ascii=False, sort_keys=True)
            )
        if raw_history:
            parts.append(
                "Previous repair history:\n"
                + json.dumps(history, ensure_ascii=False, sort_keys=True)
            )
        previous_outcomes = []
        for previous in previous_repair_results or []:
            if not isinstance(previous, dict):
                continue
            previous_outcomes.append(
                {
                    "job_id": str(previous.get("job_id") or ""),
                    "status": str(previous.get("status") or ""),
                    "changed_files": list(previous.get("changed_files") or []),
                }
            )
        if previous_outcomes:
            parts.append(
                "Most recent implementation repair outcomes (newest first):\n"
                + json.dumps(previous_outcomes, ensure_ascii=False, sort_keys=True)
                + "\nThese changes are already present and the failure evidence above still "
                "occurred. Do not repeat the same edit or merely report success. Trace the "
                "actual runtime response path, make a materially different correction, and "
                "verify the exact failing operation before finishing."
            )
        if older_repair_summaries:
            parts.append(
                "Older implementation repair outcomes, grouped by identical result:\n"
                + json.dumps(
                    older_repair_summaries,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return "\n\n".join(parts)

    def _implementation_needs_input_result(
        self, current: dict[str, Any], job_id: str
    ) -> dict[str, Any]:
        """Expose one catalog-backed upstream decision as a typed question."""

        workflow = current.get("workflow")
        workflow = workflow if isinstance(workflow, Mapping) else {}
        raw_details = workflow.get("blockingDetails")
        if not isinstance(raw_details, list):
            raw_details = workflow.get("blocking_details")
        details = [item for item in raw_details or [] if isinstance(item, Mapping)]
        gaps = [
            item
            for item in details
            if str(item.get("kind") or "") == "upstream_contract_gap"
            and str(item.get("taskId") or item.get("task_id") or "").strip()
            and str(item.get("summary") or "").strip()
            and str(item.get("sourceRef") or item.get("source_ref") or "").strip()
        ]
        base_result = {
            "job_id": job_id,
            "job": current,
            "implementation_blocking_details": details,
        }
        if len(gaps) != 1:
            return {
                **base_result,
                "awaiting_input": True,
                "kind": "question",
                "message": (
                    "Implementation needs input before it can continue. "
                    "Provide one exact upstream contract sourceRef from the current RTM."
                ),
            }

        gap = gaps[0]
        task_id = str(gap.get("taskId") or gap.get("task_id") or "").strip()
        summary = str(gap.get("summary") or "").strip()
        source_ref = str(gap.get("sourceRef") or gap.get("source_ref") or "").strip()
        app_id = str(current.get("app_id") or "")
        try:
            tools = ProjectTools(app_id)
            validation = tools.validate_revision_selections([source_ref])
            if not validation.get("valid") or len(validation.get("valid_refs") or []) != 1:
                raise ValueError("sourceRef is not an exact current catalog target")
            source_targets = tools.normalize_revision_targets(
                [source_ref], require_editable=False
            )
            if len(source_targets) != 1:
                raise ValueError("sourceRef did not resolve to one catalog target")
            source_target = source_targets[0]
            planned: list[tuple[str, RevisionPlan]] = []
            preferred_scope = (
                "contract"
                if source_ref.startswith(("api:", "operation:"))
                else "behavior"
            )
            for semantic_scope in (preferred_scope, "behavior", "contract"):
                if any(scope == semantic_scope for scope, _candidate in planned):
                    continue
                candidate = plan_revision(
                    tools,
                    RevisionInterpretation(
                        targets=[source_target.ref],
                        semantic_scope=semantic_scope,
                        requested_effect=summary,
                        change_type="modify",
                    ),
                    origin_stage="implementation",
                )
                if (
                    candidate.status == "needs_confirmation"
                    and len(candidate.requested_targets) == 1
                    and candidate.requested_targets[0].ref == source_target.ref
                    and len(candidate.authority_targets) == 1
                    and candidate.authority_targets[0].owner in {"requirements", "design"}
                ):
                    planned.append((semantic_scope, candidate))
            if not planned:
                raise ValueError("no exact upstream revision path is available")
            semantic_scope, plan = planned[0]
            authority = plan.authority_targets[0]
            allowed_semantic_scopes = tuple(
                semantic_scope
                for semantic_scope, candidate in planned
                if candidate.authority_targets == plan.authority_targets
            )
            snapshot = tools.revision_snapshot()
            versions = snapshot.get("artifact_versions")
            if not isinstance(versions, Mapping):
                raise TypeError("the current artifact versions are unavailable")
            version_id = versions.get(authority.artifact_type)
            if not isinstance(version_id, int) or isinstance(version_id, bool) or version_id < 1:
                version_id = authority.artifact_version_id
            if not isinstance(version_id, int) or version_id < 1:
                raise ValueError(
                    f"no current version is available for {authority.artifact_type}"
                )
            question_options: list[QuestionOption] = []
            raw_options = gap.get("options")
            if isinstance(raw_options, list) and 2 <= len(raw_options) <= 3:
                option_ids: set[str] = set()
                for raw_option in raw_options:
                    if not isinstance(raw_option, Mapping):
                        question_options = []
                        break
                    option_id = str(raw_option.get("id") or "").strip()
                    label = str(raw_option.get("label") or "").strip()
                    description = str(raw_option.get("description") or "").strip()
                    requested_effect = str(
                        raw_option.get("requestedEffect")
                        or raw_option.get("requested_effect")
                        or ""
                    ).strip()
                    if (
                        not option_id
                        or option_id in option_ids
                        or not label
                        or not description
                        or not requested_effect
                    ):
                        question_options = []
                        break
                    option_ids.add(option_id)
                    question_options.append(
                        QuestionOption(
                            option_id=option_id,
                            label=label,
                            description=description,
                            decision_payload=DecisionPayload(
                                normalized_meaning={
                                    "semantic_scope": semantic_scope,
                                    "requested_effect": requested_effect,
                                    "change_type": "modify",
                                },
                                authoritative_target_refs=(authority.ref,),
                            ),
                        )
                    )
            question = Question(
                question_id=f"{job_id}:upstream-contract-gap:{task_id}",
                question_version=1,
                app_id=app_id,
                source_execution_id=task_id,
                detected_at={
                    "stage": "implementation",
                    "artifact_ref": source_target.ref,
                    "element_ref": source_target.ref,
                },
                base_revisions=[
                    BaseRevision(
                        artifact_type=authority.artifact_type,
                        version_id=version_id,
                    )
                ],
                trigger={
                    "category": "upstream_contract_gap",
                    "evidence_refs": [source_target.ref],
                },
                authority_candidates=[authority],
                prompt=(
                    "Implementation needs an upstream requirements or design decision: "
                    f"{summary} Choose an option or provide another answer before retrying "
                    "implementation."
                ),
                options=question_options,
                allow_free_text=True,
                decision_policy=DecisionPolicy(
                    allowed_semantic_scopes=allowed_semantic_scopes,
                    allowed_change_types=("modify", "add"),
                ),
            )
            return {
                **base_result,
                "awaiting_input": True,
                "kind": "question",
                "message": question.prompt,
                "feedback_question": question.model_dump(mode="json"),
            }
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            return {
                **base_result,
                "awaiting_input": True,
                "kind": "question",
                "message": (
                    "Implementation is blocked by an upstream contract gap, but its exact "
                    f"authority could not be planned ({error}). Verify sourceRef "
                    f"{source_ref!r} against the current catalog/RTM and retry."
                ),
            }

    def _monitor_implementation(
        self,
        job: dict[str, Any],
        *,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        job_id = str(job["job_id"])
        app_id = str(job.get("app_id") or "")
        last_status: str | None = None
        last_progress: dict[str, str] = {}
        last_agent_results: dict[str, str] = {}
        transient_attempt = 0
        while True:
            if command_id and self._stop_requested(command_id):
                raise WorkspaceStopRequested()
            try:
                current = implementation_worker.get(job_id)
            except Exception as error:
                if not command_id or not self._is_transient_execution_error(error):
                    raise
                transient_attempt += 1
                self._record_transient_retry(command_id, transient_attempt, error)
                self._sleep_for_retry(command_id, transient_attempt)
                continue
            transient_attempt = 0
            status = str(current.get("status") or "")
            if app_id and command_id:
                if status and status != last_status:
                    repository.append_progress_event(
                        app_id,
                        command_id=command_id,
                        stage="implementation",
                        text=f"Implementation job status: {status}.",
                        metadata={
                            "progress_event": "implementationStatusUpdated",
                            "status": status,
                            "job_id": job_id,
                        },
                    )
                    last_status = status
                progress = self._implementation_progress_snapshot(current)
                for update in progress.get("updates", []) if progress else []:
                    if not isinstance(update, dict):
                        continue
                    step = str(update.get("step") or "")
                    if not step:
                        continue
                    progress_key = "|".join(
                        str(update.get(field) or "")
                        for field in (
                            "status",
                            "label",
                            "detail",
                            "current_file",
                            "current_class",
                            "recent_command",
                            "verification_status",
                            "repairing",
                        )
                    )
                    if last_progress.get(step) == progress_key:
                        continue
                    repository.append_progress_event(
                        app_id,
                        command_id=command_id,
                        stage="implementation",
                        text=str(
                            update.get("detail")
                            or update.get("label")
                            or "Implementation is in progress."
                        ),
                        metadata={
                            "progress_event": "implementationStepUpdated",
                            "step": step,
                            "progress_step_label": str(update.get("label") or step),
                            "progress_card_label": str(
                                progress.get("progress_card_label") or "Implementation progress"
                            ),
                            "progress_detail": str(update.get("detail") or ""),
                            "progress_status": str(update.get("status") or "running"),
                            **{
                                key: update[key]
                                for key in (
                                    "implementation_owner",
                                    "current_file",
                                    "current_class",
                                    "recent_command",
                                    "verification_status",
                                    "repairing",
                                )
                                if key in update
                            },
                        },
                    )
                    last_progress[step] = progress_key
                for result in progress.get("agent_results", []) if progress else []:
                    if not isinstance(result, dict):
                        continue
                    task_id = str(result.get("task_id") or "")
                    if not task_id:
                        continue
                    fingerprint = stable_digest(result)
                    if last_agent_results.get(task_id) == fingerprint:
                        continue
                    raw_response = str(result.get("raw_response") or "").strip()
                    repository.append_progress_event(
                        app_id,
                        command_id=command_id,
                        stage="implementation",
                        text=raw_response or f"The result for {task_id} was recorded.",
                        metadata={
                            "progress_event": "implementationAgentResult",
                            **result,
                        },
                    )
                    last_agent_results[task_id] = fingerprint
            if status in TERMINAL_JOB_STATUSES:
                if status != "COMPLETED":
                    if status == "NEEDS_INPUT":
                        return self._implementation_needs_input_result(current, job_id)
                    raise RuntimeError(str(current.get("error") or f"Implementation job {status}"))
                return {
                    "message": "Review the generated implementation artifacts below.",
                    "job_id": job_id,
                    "job": current,
                    "review_artifacts": True,
                }
            if command_id and self._stop_requested(command_id):
                raise WorkspaceStopRequested()
            time.sleep(1)

    def _run_testing_command(
        self,
        command: dict[str, Any],
        implementation_job_id: str,
        *,
        previous_job: dict[str, Any] | None = None,
        preserve_test: bool = False,
        repair_task_type: str | None = None,
        reset_checkpoint: bool = False,
    ) -> dict[str, Any]:
        """Testing을 실행하고 재시작 checkpoint를 현재 Workspace command에 저장한다."""

        command_id = str(command["command_id"])
        last_progress_fingerprint = ""

        if reset_checkpoint:
            # Implementation repaired the files, so the saved TestingInput points
            # at the old artifact versions. Remove it before capturing the fresh
            # candidate; retaining it would silently verify the unrepaired app.
            payload = {
                key: value
                for key, value in dict(command.get("payload") or {}).items()
                if key != "testing_checkpoint"
            }
            command["payload"] = payload
            repository.update_command(command_id, payload=payload)

        def save_checkpoint(checkpoint: dict[str, Any]) -> None:
            nonlocal last_progress_fingerprint
            if self._stop_requested(command_id):
                raise WorkspaceStopRequested()
            # Testing command와 checkpoint의 수명주기가 같으므로 기존 payload에 함께 저장한다.
            # 다른 command 입력은 그대로 보존한다.
            latest = repository.get_command(command_id)
            if latest is None:
                raise RuntimeError("The Workspace command disappeared during Testing.")
            payload = {
                **dict(latest.get("payload") or {}),
                "testing_checkpoint": checkpoint,
            }
            command["payload"] = payload
            repository.update_command(command_id, payload=payload)
            progress_snapshot = checkpoint.get("testing_progress")
            last_event = (
                progress_snapshot.get("last_event")
                if isinstance(progress_snapshot, dict)
                else None
            )
            if not isinstance(last_event, dict):
                return
            fingerprint = stable_digest(
                {
                    key: last_event.get(key)
                    for key in (
                        "phase",
                        "scope",
                        "status",
                        "workflow_id",
                        "step_id",
                        "gate",
                        "operation_id",
                        "method",
                        "path",
                        "control",
                        "attempt",
                        "progress_step_label",
                        "progress_detail",
                        "status_code",
                        "contract_status",
                        "semantic_status",
                    )
                }
            )
            if fingerprint == last_progress_fingerprint:
                return
            last_progress_fingerprint = fingerprint
            try:
                repository.append_progress_event(
                    str(command["app_id"]),
                    command_id=command_id,
                    stage="testing",
                    text=str(
                        last_event.get("progress_detail")
                        or last_event.get("progress_step_label")
                        or "Testing progress updated."
                    ),
                    metadata=dict(last_event),
                )
            except Exception:  # noqa: BLE001 - the DB checkpoint remains authoritative.
                _log.warning("Could not publish Testing progress event.", exc_info=True)

            node = str(checkpoint.get("current_node") or "")
            updates = {
                "queued": [
                    (
                        "prepare-testing",
                        "Prepare testing snapshot",
                        "running",
                        "Freezing the generated application and its design contracts.",
                    )
                ],
                "verification": [
                    (
                        "prepare-testing",
                        "Prepare testing snapshot",
                        "completed",
                        "Testing inputs are ready.",
                    ),
                    (
                        "run-verification",
                        "Run application verification",
                        "running",
                        "Running functional, static, deployment package, and IaC checks.",
                    ),
                ],
                "verification_complete": [
                    (
                        "run-verification",
                        "Run application verification",
                        "completed",
                        "Verification gates finished.",
                    ),
                    (
                        "finalize-testing",
                        "Finalize testing results",
                        "completed",
                        "Test results are ready.",
                    ),
                ],
            }.get(node, [])
            for step, label, status, detail in updates:
                repository.append_progress_event(
                    str(command["app_id"]),
                    command_id=command_id,
                    stage="testing",
                    text=detail,
                    metadata={
                        "progress_event": "testingStepUpdated",
                        "step": step,
                        "progress_step_label": label,
                        "progress_card_label": "Testing progress",
                        "progress_detail": detail,
                        "progress_status": status,
                    },
                )

        checkpoint = (
            None
            if reset_checkpoint
            else command.get("payload", {}).get("testing_checkpoint")
        )
        if self._stop_requested(command_id):
            raise WorkspaceStopRequested()
        job = run_testing(
            str(command["app_id"]),
            implementation_job_id,
            run_id=command_id,
            previous_job=previous_job,
            preserve_test=preserve_test,
            checkpoint=checkpoint if isinstance(checkpoint, dict) else None,
            repair_task_type=repair_task_type,
            progress=save_checkpoint,
        )
        return self._testing_result(job)

    @staticmethod
    def _testing_result(job: dict[str, Any]) -> dict[str, Any]:
        """동기 실행 결과를 기존 Workspace 응답 모양으로 바꾼다."""

        report = job.get("result") or {}
        job_id = str(job.get("job_id") or "")
        if report.get("passed") is False:
            blockers = list(report.get("blocking_findings") or [])
            # Testing already performs its bounded plan correction/replay.  If
            # only test-plan authoring defects remain, no generated-app repair
            # or user decision exists; do not expose a retry as AWAITING_INPUT.
            test_plan_defect = bool(blockers) and all(
                isinstance(blocker, dict)
                and str(blocker.get("defect_class") or "") == "TEST_DEFECT"
                and str(blocker.get("repair_owner") or "") == "testing"
                for blocker in blockers
            )
            if test_plan_defect:
                repair_state = dict(report.get("repair_state") or {})
                repair_state.update(
                    {
                        "status": "STALLED",
                        "stall_reason": (
                            "Bounded test-plan correction was exhausted; "
                            "an EasyDep test-plan authoring defect remains."
                        ),
                    }
                )
                return {
                    "kind": "platform_diagnostic",
                    "message": (
                        "Testing stopped after bounded test-plan correction. "
                        "EasyDep detected an internal test-plan authoring defect; "
                        "the generated application was not assigned a failure."
                    ),
                    "requires_revision": False,
                    "internal_diagnostic": {
                        "category": "TEST_PLAN_AUTHORING_DEFECT",
                        "owner": "EasyDep",
                    },
                    "blocking_findings": blockers,
                    "repair_state": repair_state,
                    "blocking_route": "platform",
                    "job_id": job_id,
                    "job": job,
                }
            repairable = any(
                blocker.get("repairable") is not False
                for blocker in blockers
                if isinstance(blocker, dict)
            )
            blocking_route = blocking_findings_route(
                [blocker for blocker in blockers if isinstance(blocker, dict)]
            )
            if blocking_route == "environment":
                guidance = (
                    "The runtime environment must be restored before the same checks "
                    "can continue."
                )
            elif blocking_route == "platform":
                guidance = (
                    "The failure is in the EasyDep platform and cannot be repaired from "
                    "the generated application."
                )
            elif blocking_route == "design":
                guidance = "Review the affected design before continuing."
            elif blocking_route == "platform-or-design":
                guidance = (
                    "Review the deployment design and EasyDep platform evidence before "
                    "continuing."
                )
            elif repairable and not blocking_route:
                guidance = (
                    "EasyDep classified the failures and will continue the matching "
                    "automatic repair path."
                )
            else:
                guidance = "Review the blocking findings before continuing."
            return {
                "awaiting_input": True,
                "kind": "action_required",
                "message": (
                    f"Testing found {len(blockers)} blocking failure(s). "
                    + guidance
                ),
                "requires_revision": True,
                "blocking_findings": blockers,
                "repair_state": report.get("repair_state")
                or {
                    "status": "ACTIVE",
                    "attempt_count": 0,
                    "accepted_count": 0,
                    "recent_attempts": [],
                },
                "blocking_route": blocking_route,
                "job_id": job_id,
                "job": job,
            }
        return {
            "message": "Testing completed.",
            "job_id": job_id,
            "job": job,
        }

    @staticmethod
    def _error_text(error: Exception) -> str:
        if isinstance(error, HTTPException):
            detail = error.detail
            if isinstance(detail, dict):
                return str(detail.get("message") or detail)
            return str(detail)
        return str(error) or type(error).__name__

    @staticmethod
    def _stage_label(stage: str) -> str:
        return {
            "requirements": "requirements analysis",
            "design": "system design",
            "implementation": "system implementation",
            "testing": "system testing",
        }.get(stage, stage)


workspace_service = WorkspaceService()
