"""Workspace가 사용하는 설계 애플리케이션 서비스다.

이 모듈은 설계 실행을 시작하거나 재개하고, 이미 만든 설계의 일부를 수정한다.
웹 응답 객체를 만들지 않고 일반 ``dict``를 반환하므로 Workspace뿐 아니라 다른
Python 호출자도 같은 흐름을 그대로 사용할 수 있다.

실제 클래스·시퀀스·API·ERD·배포 설계 생성은 ``app.design.graphs``가 담당한다.
여기서는 앱과 실행 상태를 확인하고, 그래프 호출과 수정 결과 저장을 한곳에서 조정한다.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import replace
from typing import Any, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from app.artifacts_api import to_web_response
from app.db.models import ORIGIN_FEEDBACK_REVISED
from app.design.cascade import (
    UnapprovedScopeExpansion,
    UnknownTarget,
    _design_target,
    persist_cascade,
    revise_and_cascade,
)
from app.design.graphs.design_graph import (
    StageNotReached,
    graph as design_graph,
    has_active_session,
    has_design_run,
    reset_design,
    resume_design,
    retry_design,
    revise_design_stage,
    rewind_design,
    session_status,
    start_design,
    sync_design_state,
)
from app.design.graphs.subgraphs import DESIGN_SPECS, DESIGN_STAGES
from app.design.nodes.artifact import (
    _repair_is_improvement,
    _repairable_findings,
    assert_untargeted_elements_preserved,
    check_node,
    merge_model,
    render_and_validate,
)
from app.design.schemas.architecture_state import ArchitectureState
from app.design.services.class_diagram.models import GenerationStalled
from app.design.services.deployment_diagram.bundle import (
    build_deployment_diagram_bundle,
    hydrate_deployment_diagram_bundle,
    select_deployment_target,
)
from app.design.services.deployment_diagram.digest import workload_graph_structure_digest
from app.design.services.deployment_diagram.provider_plantuml import (
    deployment_bundle_provisioning_puml,
    deployment_bundle_runtime_puml,
)
from app.design.services.deployment_diagram.sizing import (
    apply_capacity_overrides,
    apply_compute_selections,
    compute_sizing_guidance,
    estimate_selected_deployment_cost,
)
from app.design.services.deployment_diagram.workload_contracts import (
    data_execution_mode_decision,
)
from app.design.validation import design_readiness_report
from app.repositories import artifact_repository
from app.repositories.artifact_repository import AppNotFound
from app.validation import Finding, RepairAttempt, RepairLedger, stable_digest


class ReviseRequest(BaseModel):
    """추적표에 표시된 설계 요소 하나를 수정하는 명령."""

    # ``{stage}:{element}`` 형식이며, 화면이 선택한 대상을 그대로 전달한다.
    target: str
    feedback: str = ""
    # Planner execution supplies these frozen sets.  ``None`` is meaningful:
    # it is not the old implicit permission to discover an upstream authority.
    approved_authority_targets: list[str] | None = None
    approved_downstream_targets: list[str] | None = None
    # The conversation model may resolve a natural-language request into a
    # finite, non-executable patch vocabulary.  These values remain part of the
    # existing command JSON; they do not require a persistence schema change.
    patch_intents: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=40,
        exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def patches_stay_within_target(self) -> ReviseRequest:
        """Reject a patch attached to a different authority revision."""
        target = self.target.strip()
        for patch in self.patch_intents:
            patch_target = str(patch.get("target") or "").strip()
            if not patch_target or patch_target != target:
                raise ValueError("Every structured patch must match its revision target.")
        return self


class BatchReviseRequest(BaseModel):
    """여러 설계 요소를 모두 성공했을 때만 저장하는 수정 명령."""

    revisions: list[ReviseRequest] = Field(min_length=1, max_length=20)

    @field_validator("revisions")
    @classmethod
    def targets_are_unique(cls, revisions: list[ReviseRequest]) -> list[ReviseRequest]:
        """대상이 겹치거나 설명이 비어 있는 수정 묶음을 실행 전에 거절한다."""
        targets = [revision.target.strip() for revision in revisions]
        if any(not target for target in targets):
            raise ValueError("Every targeted revision needs an element reference.")
        if len(targets) != len(set(targets)):
            raise ValueError("A target can appear only once in a revision batch.")
        if any(not revision.feedback.strip() for revision in revisions):
            raise ValueError("Every targeted revision needs feedback text.")
        return revisions


def _persisted_stage_changed(
    original: ArchitectureState,
    working: ArchitectureState,
    stage: str,
) -> bool:
    """Compare the values that the artifact repository actually versions."""

    config = artifact_repository.STAGE_ARTIFACTS.get(stage)
    if config is None:
        # Unknown cascade stages must not be discarded by a deduplication check.
        return True
    source_key = config.get("source_key") or config.get("state_key")
    keys = [
        key
        for key in (
            source_key,
            config.get("valid_key"),
            config.get("errors_key"),
        )
        if key
    ]
    return any(original.get(key) != working.get(key) for key in keys)


def start_design_session(app_id: str) -> dict[str, Any]:
    """첫 설계 단계부터 실행하고 클래스 다이어그램 검토 지점에서 멈춘다."""
    _validate_app_id(app_id)
    state = _load_app(app_id)
    # 모든 설계 산출물은 유스케이스 명세를 입력으로 사용한다. 나머지 순서는 설계
    # 그래프가 보장하므로 시작할 때에는 이 입력이 있는지만 확인하면 된다.
    if not state.get("usecase_spec"):
        raise ValueError(
            "The use case specification must exist first. "
            "Complete requirements analysis in the Workspace first."
        )

    # 새 시작은 이전 체크포인트만 비운다. 이미 저장한 산출물과 버전 이력은 유지한다.
    reset_design(app_id)
    try:
        return start_design(app_id, state)
    except GenerationStalled:
        raise
    except Exception as error:
        raise RuntimeError(f"Design pipeline failed: {error}") from error


def _repair_stale_sequence_projection(
    app_id: str,
    state: ArchitectureState,
    readiness: dict[str, Any],
) -> dict[str, Any] | None:
    """Reproject a sequence whose only defect is stale class provenance."""

    records = list(readiness.get("findingRecords") or [])
    if not records or any(
        str(record.get("stage") or "") != "sequence_diagram"
        or str(record.get("ruleId") or "") != "sequence.class-diagram-version"
        for record in records
    ):
        return None
    class_readiness = design_readiness_report(state, stages=["class_diagram"])
    if class_readiness.get("findings"):
        return None
    # Sequence is a deterministic projection of the accepted class model. A
    # version mismatch has no user decision to make, so regenerate it and stop
    # at the sequence review gate instead of presenting an unrecoverable error.
    return rewind_design(app_id, "sequence_diagram")


def _readiness_state_at_active_class_gate(
    app_id: str,
    state: ArchitectureState,
    active_stage: str,
) -> ArchitectureState:
    """Restore checkpoint-owned class semantic evidence for an approval read.

    Artifact hydration deliberately rebuilds deterministic checks, while the
    completed v3 public-contract review is durable graph state.  At the active
    class gate only, overlay that one check so readiness can validate its
    model/contract digests without repeating the semantic review.
    """

    if active_stage != "class_diagram":
        return state
    snapshot = design_graph.get_state({"configurable": {"thread_id": app_id}})
    checkpoint_state = snapshot.values or {}
    checkpoint_check = checkpoint_state.get("class_diagram_check")
    if not isinstance(checkpoint_check, dict):
        return state
    return cast(ArchitectureState, {**state, "class_diagram_check": checkpoint_check})


def _retry_stalled_class_gate_after_reconcile(
    app_id: str,
    state: ArchitectureState,
    readiness: dict[str, Any],
) -> dict[str, Any] | None:
    """Re-enter a stalled class artifact after reconciliation or before any repair attempt.

    This is limited to an active, stalled class gate with technical findings.  The
    existing class-only start path resets only the graph checkpoint, then resumes
    from the persisted class model and stops at the class gate again.
    """

    status = session_status(app_id)
    if not status.get("active") or status.get("stage") != "class_diagram":
        return None
    check = state.get("class_diagram_check") or {}
    if str(check.get("stopped") or "").casefold() != "stalled":
        return None
    records = [
        record
        for record in readiness.get("findingRecords") or []
        if isinstance(record, dict)
        and str(record.get("stage") or "class_diagram") == "class_diagram"
    ]
    if not records or any(record.get("requiresUserInput") is not False for record in records):
        return None
    repair_history = check.get("repair_history")
    attempts = repair_history.get("attempts") if isinstance(repair_history, dict) else None
    zero_attempts = (
        isinstance(attempts, list)
        and not attempts
        and check.get("repair_iters", 0) == 0
    )
    reconcile = DESIGN_SPECS["class_diagram"].reconcile
    patch = reconcile(state) if reconcile is not None else None
    model_key = DESIGN_SPECS["class_diagram"].model_key
    changed = (
        isinstance(patch, dict)
        and model_key in patch
        and patch.get(model_key) != state.get(model_key)
    )
    if not changed and not zero_attempts:
        return None
    # start_design_session clears the checkpoint, not artifact history. Its
    # existing extractor resumes this saved model; no requirements/design stages
    # before or after the class gate are rerun.
    return start_design_session(app_id)


def _deployment_endpoint_question(
    app_id: str, state: ArchitectureState
) -> dict[str, Any] | None:
    """Expose a grounded missing endpoint as one pinned, free-text question."""

    bundle = state.get("deployment_diagram_bundle") or {}
    graph = bundle.get("workloadGraph") or state.get("deployment_diagram_model") or {}
    issues = [
        issue
        for issue in graph.get("issues") or []
        if isinstance(issue, dict)
        and issue.get("classification") == "needsInput"
        and str(issue.get("field") or "").startswith("connections.")
        and str(issue.get("field") or "").endswith(".endpoint")
    ]
    if not issues:
        return None
    issue = issues[0]
    field = str(issue.get("field") or "")
    connection_id = field[len("connections.") : -len(".endpoint")]
    connections = [
        item
        for item in graph.get("connections") or []
        if isinstance(item, dict) and str(item.get("id") or "") == connection_id
    ]
    if len(connections) != 1:
        return None
    connection = connections[0]
    source_ref = str(connection.get("sourceRef") or "")
    source_refs = sorted({str(ref) for ref in connection.get("sourceRefs") or [] if ref})
    if not source_ref or not source_refs:
        return None
    question = {
        "field": f"connectionEndpoint:{connection_id}",
        "kind": "text",
        "question": (
            f"What non-secret endpoint should workload {source_ref} use for "
            f"external dependency {connection.get('targetRef')}? Enter a URL or host:port."
        ),
        "reason": str(issue.get("reason") or "The external endpoint is required."),
        "sourceRefs": source_refs,
        "context": {
            "connectionId": connection_id,
            "sourceRef": source_ref,
            "workloadGraphStructureDigest": workload_graph_structure_digest(graph),
        },
    }
    return {
        "app_id": app_id,
        **to_web_response(state),
        "status": "need_feedback",
        "stage": "deployment_diagram",
        "resource_question": question,
    }


def _parse_public_endpoint(value: str) -> tuple[str, str]:
    """Accept a plain HTTP(S) URL or host:port without embedded credentials."""

    text = value.strip()
    if not text or any(ord(char) < 32 or char.isspace() for char in text):
        raise ValueError("Enter a non-secret URL or host:port endpoint.")
    parsed = urlsplit(text if "://" in text else f"//{text}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Endpoint credentials are not accepted; enter a non-secret endpoint.")
    if parsed.query or parsed.fragment:
        raise ValueError("Endpoint query strings and fragments are not accepted.")
    if parsed.scheme:
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Enter an HTTP(S) URL or host:port endpoint.")
        try:
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError
        except ValueError as error:
            raise ValueError("Endpoint port must be between 1 and 65535.") from error
        return "url", text
    if parsed.path or not parsed.hostname:
        raise ValueError("Enter a host:port endpoint.")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Endpoint port must be between 1 and 65535.") from error
    if port is None or not 1 <= port <= 65535:
        raise ValueError("Enter a host:port endpoint with a valid port.")
    # Preserve bracketed IPv6, which urlsplit validates through hostname/port.
    host = text.rsplit(":", 1)[0]
    return "hostport", host


def _unique_environment_name(base: str, reserved: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in reserved:
        candidate = f"{base}_{suffix}"
        suffix += 1
    reserved.add(candidate)
    return candidate


def _unique_configuration_id(base: str, reserved: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in reserved:
        candidate = f"{base}-{suffix}"
        suffix += 1
    reserved.add(candidate)
    return candidate


def _blocking_graph_issue_keys(graph: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (
            str(issue.get("field") or ""),
            str(issue.get("classification") or ""),
            str(issue.get("reason") or ""),
        )
        for issue in graph.get("issues") or []
        if isinstance(issue, dict)
        and issue.get("classification") in {"invalid", "unsupported"}
    }


def apply_deployment_endpoint_answer_session(
    app_id: str, pinned_question: dict[str, Any], text: str
) -> dict[str, Any]:
    """Apply a pinned endpoint answer only while the same deployment issue remains."""

    _validate_app_id(app_id)
    _require_app_exists(app_id)
    _require_active_session(app_id)
    if session_status(app_id).get("stage") != "deployment_diagram":
        raise ValueError("An endpoint can only be supplied at the deployment design gate.")
    state = _load_app(app_id)
    current_result = _deployment_endpoint_question(app_id, state)
    current = (current_result or {}).get("resource_question") or {}
    if not current or any(
        pinned_question.get(key) != current.get(key)
        for key in ("field", "sourceRefs", "context")
    ):
        raise ValueError("The endpoint question is stale. Reload the current deployment question.")
    endpoint_kind, endpoint_value = _parse_public_endpoint(text)
    bundle = state.get("deployment_diagram_bundle") or {}
    graph = bundle.get("workloadGraph") or {}
    context = current.get("context") or {}
    connection_id = str(context.get("connectionId") or "")
    source_ref = str(context.get("sourceRef") or "")
    updated_graph = dict(graph)
    baseline_issue_keys = _blocking_graph_issue_keys(graph)
    updated_workloads = []
    matched = False
    for workload in graph.get("workloads") or []:
        item = dict(workload)
        if str(item.get("id") or "") == source_ref:
            configurations = [
                dict(configuration)
                for configuration in item.get("configuration") or []
                if not (
                    configuration.get("kind") == "endpointBinding"
                    and str(configuration.get("connectionRef") or "") == connection_id
                )
            ]
            existing = [
                dict(configuration)
                for configuration in item.get("configuration") or []
                if configuration.get("kind") == "endpointBinding"
                and str(configuration.get("connectionRef") or "") == connection_id
            ]
            matched = bool(existing)
            source_refs = list(context.get("sourceRefs") or [])
            reserved_names = {
                str(configuration.get("name") or "")
                for configuration in configurations
            }
            reserved_ids = {
                str(configuration.get("id") or "")
                for configuration in configurations
            }
            target = next(
                (
                    connection.get("targetRef")
                    for connection in graph.get("connections") or []
                    if isinstance(connection, dict)
                    and str(connection.get("id") or "") == connection_id
                ),
                connection_id,
            )
            name_stem = re.sub(r"[^A-Z0-9]+", "_", str(target).upper()).strip("_") or "ENDPOINT"
            existing_names = {
                str(configuration.get("projection") or ""): str(configuration.get("name") or "")
                for configuration in existing
            }
            if endpoint_kind == "url":
                url_name = existing_names.get("url")
                if not url_name or url_name in reserved_names:
                    url_name = _unique_environment_name(
                        f"{name_stem}_ENDPOINT", reserved_names
                    )
                else:
                    reserved_names.add(url_name)
                configurations.append({
                    "id": _unique_configuration_id(
                        f"{connection_id}-endpoint-url", reserved_ids
                    ),
                    "name": url_name,
                    "kind": "endpointBinding",
                    "value": endpoint_value,
                    "connectionRef": connection_id,
                    "projection": "url",
                    "sensitive": False,
                    "sourceRefs": source_refs,
                })
            else:
                host = endpoint_value
                port = int(text.strip().rsplit(":", 1)[1])
                for projection, value in (("host", host), ("port", port)):
                    config_name = existing_names.get(projection)
                    if not config_name or config_name in reserved_names:
                        config_name = _unique_environment_name(
                            f"{name_stem}_{projection.upper()}", reserved_names
                        )
                    else:
                        reserved_names.add(config_name)
                    configurations.append({
                        "id": _unique_configuration_id(
                            f"{connection_id}-endpoint-{projection}", reserved_ids
                        ),
                        "name": config_name,
                        "kind": "endpointBinding",
                        "value": value,
                        "connectionRef": connection_id,
                        "projection": projection,
                        "sensitive": False,
                        "sourceRefs": source_refs,
                    })
            item["configuration"] = configurations
        updated_workloads.append(item)
    if not matched:
        raise ValueError("The pinned endpoint binding no longer exists.")
    updated_graph["workloads"] = updated_workloads
    rebuilt = build_deployment_diagram_bundle(
        updated_graph,
        dict(state.get("resource_spec") or {}),
        planning_facts=dict(bundle.get("planningFacts") or {}),
    )
    remaining = [
        issue
        for issue in (rebuilt.get("workloadGraph") or {}).get("issues") or []
        if issue.get("field") == f"connections.{connection_id}.endpoint"
        and issue.get("classification") == "needsInput"
    ]
    if remaining:
        raise ValueError("The endpoint value did not resolve the current deployment finding.")
    introduced_issues = (
        _blocking_graph_issue_keys(rebuilt.get("workloadGraph") or {})
        - baseline_issue_keys
    )
    if introduced_issues:
        raise ValueError("The endpoint answer introduced a deployment graph validation issue.")
    hydrated = hydrate_deployment_diagram_bundle(rebuilt)
    state.update(hydrated)
    state["deployment_diagram_puml"] = deployment_bundle_runtime_puml(rebuilt)
    state["deployment_diagram_provisioning_puml"] = deployment_bundle_provisioning_puml(rebuilt)
    artifact_repository.save_stage(
        app_id, "deployment_diagram", state, origin=ORIGIN_FEEDBACK_REVISED
    )
    current_state = _load_app(app_id)
    sync_design_state(app_id, dict(current_state))
    status = session_status(app_id)
    if status.get("active") and status.get("stage") == "deployment_diagram":
        return resume_design_session(app_id)
    return {"app_id": app_id, **to_web_response(current_state), "status": "completed"}


def resume_design_session(app_id: str, feedback: str = "") -> dict[str, Any]:
    """검토 중인 설계에 피드백을 적용하거나 다음 설계 단계로 진행한다."""
    _validate_app_id(app_id)
    _require_app_exists(app_id)
    _require_active_session(app_id)

    # 빈 피드백은 현재 결과를 승인하고 다음 단계로 진행한다는 뜻이다. 결정론적 검사가
    # 문제를 찾은 초안은 다음 단계의 입력으로 쓰지 않고, 먼저 수정하도록 안내한다.
    if not feedback.strip():
        active_stage = session_status(app_id).get("stage")
        if active_stage:
            state = _load_app(app_id)
            readiness_state = _readiness_state_at_active_class_gate(
                app_id, state, str(active_stage)
            )
            if active_stage == "deployment_diagram":
                endpoint_question = _deployment_endpoint_question(app_id, state)
                if endpoint_question is not None:
                    return endpoint_question
            readiness = design_readiness_report(
                readiness_state, stages=[str(active_stage)]
            )
            findings = list(readiness.get("findings") or [])
            if findings:
                repaired = _repair_stale_sequence_projection(
                    app_id, state, readiness
                )
                if repaired is not None:
                    return repaired
                reconciled = _retry_stalled_class_gate_after_reconcile(
                    app_id, readiness_state, readiness
                )
                if reconciled is not None:
                    return reconciled
                raise ValueError(
                    "Resolve the active design findings before advancing. "
                    f"Stage: {active_stage}. Findings: {findings}"
                )
            # ERD는 논리 데이터 모델일 뿐 실행 DB 엔진을 뜻하지 않는다. 별도 DB가
            # 명시됐지만 엔진이 빠진 경우에만 배포 산출물을 만들기 직전에 물어본다.
            if active_stage == "erd":
                decision = data_execution_mode_decision(
                    state.get("refined_requirements") or [],
                    capability_contract=dict(state.get("capability_contract") or {}),
                    deployment_planning_facts=(
                        state.get("deployment_planning_facts") or []
                    ),
                )
                if decision.get("status") == "needsInput":
                    question = {
                        "field": "dataExecutionMode",
                        "kind": "choice",
                        "question": "How should the application run its database?",
                        "reason": (
                            "A separate database runtime is required, but no supported "
                            "engine was specified."
                        ),
                        "sourceRefs": list(decision.get("sourceRefs") or []),
                        "choices": [
                            {
                                "value": "postgresql-container",
                                "label": "PostgreSQL container",
                                "description": (
                                    "Run the application and PostgreSQL as separate "
                                    "containers on the selected VM."
                                ),
                            },
                            {
                                "value": "embedded",
                                "label": "Embedded database",
                                "description": (
                                    "Keep the database inside the application runtime."
                                ),
                            },
                        ],
                    }
                    return {
                        "app_id": app_id,
                        **to_web_response(state),
                        "status": "need_feedback",
                        "stage": "deployment_diagram",
                        "resource_question": question,
                    }
    try:
        result = resume_design(app_id, feedback)
        # When ERD advances into the deployment gate, this graph result is the
        # first Workspace-visible response for that gate. Attach any grounded
        # endpoint value question before it is normalized into a checkpoint.
        if (
            result.get("status") == "need_feedback"
            and result.get("stage") == "deployment_diagram"
            and not result.get("resource_question")
        ):
            endpoint_question = _deployment_endpoint_question(app_id, _load_app(app_id))
            if endpoint_question is not None:
                result = {**result, "resource_question": endpoint_question["resource_question"]}
        return result
    except GenerationStalled:
        raise
    except Exception as error:
        raise RuntimeError(f"Design pipeline failed: {error}") from error


def apply_deployment_topology_decision_session(
    app_id: str, data_execution_mode: str
) -> dict[str, Any]:
    """배포 직전 DB 실행 방식 답을 기존 planning fact로 기록하고 계속한다."""

    _validate_app_id(app_id)
    _require_app_exists(app_id)
    _require_active_session(app_id)
    if session_status(app_id).get("stage") != "erd":
        raise ValueError("A database runtime choice is only accepted before deployment design.")
    mode = data_execution_mode.strip()
    if mode not in {"embedded", "postgresql-container"}:
        raise ValueError(f"Unsupported database runtime choice: {mode}")

    state = _load_app(app_id)
    decision = data_execution_mode_decision(
        state.get("refined_requirements") or [],
        capability_contract=dict(state.get("capability_contract") or {}),
        deployment_planning_facts=state.get("deployment_planning_facts") or [],
    )
    if decision.get("status") != "needsInput":
        raise ValueError("The current design does not need a database runtime choice.")
    fact = {
        "id": "user-data-execution-mode",
        "kind": "dataExecutionMode",
        "value": mode,
        "sourceRefs": list(decision.get("sourceRefs") or []),
        "derivationRule": "user-approved-data-execution-mode",
        "authority": "explicit",
        "status": "accepted",
    }
    facts = [
        dict(item)
        for item in state.get("deployment_planning_facts") or []
        if isinstance(item, dict) and item.get("kind") != "dataExecutionMode"
    ]
    sync_design_state(app_id, {"deployment_planning_facts": [*facts, fact]})
    try:
        # 실제 graph는 아직 ERD gate에 멈춰 있다. 답을 일반 피드백으로 보내지 않고
        # 승인으로 재개하면 deployment stage 하나만 정상 경로에서 생성·저장된다.
        return resume_design(app_id, "")
    except Exception as error:
        raise RuntimeError(f"Design pipeline failed: {error}") from error


def select_deployment_target_session(app_id: str, target_id: str) -> dict[str, Any]:
    """복수 배포 후보 중 사용자가 고른 하나를 LLM 호출 없이 확정한다.

    후보 ID는 저장된 bundle이 만든 값만 받는다. 선택 결과를 DB와 LangGraph 체크포인트에
    함께 반영한 뒤, 마지막 deployment gate를 통과시켜 설계 세션을 완료한다.
    """

    _validate_app_id(app_id)
    state = _load_app(app_id)
    bundle = state.get("deployment_diagram_bundle")
    if not isinstance(bundle, dict) or not bundle:
        raise ValueError("A deployment bundle must exist before selecting a target.")
    selected = select_deployment_target(bundle, target_id)
    hydrated = hydrate_deployment_diagram_bundle(selected)
    state.update(hydrated)
    state["deployment_diagram_puml"] = deployment_bundle_runtime_puml(selected)
    state["deployment_diagram_provisioning_puml"] = (
        deployment_bundle_provisioning_puml(selected)
    )
    artifact_repository.save_stage(
        app_id,
        "deployment_diagram",
        state,
        origin=ORIGIN_FEEDBACK_REVISED,
    )

    # load_state가 현재 검증 결과를 다시 계산한다. 저장 모델과 checkpoint를 이 값으로
    # 맞춰야 다음 resume이 선택 전 needsInput 상태로 되돌아가지 않는다.
    current = _load_app(app_id)
    sync_design_state(app_id, dict(current))
    status = session_status(app_id)
    if status.get("active") and status.get("stage") == "deployment_diagram":
        return resume_design_session(app_id)
    return {"app_id": app_id, **to_web_response(current), "status": "completed"}


def deployment_sizing_session(
    app_id: str,
    target_id: str,
    capacity_overrides: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """VM 후보와 선택 구성의 전체 배포 비용 견적을 함께 계산한다.

    VM 후보의 월 가격은 SKU 비교를 위한 compute detail이다. ``pricing``은 선택된
    ResourcePlan 전체를 local retail-rate snapshot으로 견적 낸 결과이며, usage나
    rate가 없는 항목은 0으로 바꾸지 않고 known monthly floor에 남긴다.
    """

    _validate_app_id(app_id)
    state = _load_app(app_id)
    bundle = state.get("deployment_diagram_bundle")
    if not isinstance(bundle, dict) or not bundle:
        raise ValueError("A deployment bundle must exist before requesting VM guidance.")
    selected = select_deployment_target(bundle, target_id)
    projection = next(
        item
        for item in selected.get("projections") or []
        if isinstance(item, dict) and item.get("target") == selected.get("selectedTarget")
    )
    stored_capacity_overrides = list(
        (projection.get("sizing") or {}).get("capacityOverrides") or []
    )
    effective_capacity = (
        stored_capacity_overrides if capacity_overrides is None else capacity_overrides
    )
    preview_plan, normalized_capacity = apply_capacity_overrides(
        dict(projection.get("deploymentPlan") or {}), effective_capacity
    )
    guidance = compute_sizing_guidance(
        preview_plan,
        provider=str(projection.get("provider") or ""),
        region=str(projection.get("region") or ""),
        workload_graph=dict(selected.get("workloadGraph") or {}),
    )
    stored = list((projection.get("sizing") or {}).get("selected") or [])
    stored_by_compute = {
        str(item.get("computeUnitId") or ""): item
        for item in stored
        if isinstance(item, dict)
    }
    selections: list[dict[str, Any]] = []
    for unit in guidance.get("computeUnits") or []:
        if not isinstance(unit, dict):
            continue
        candidates = [item for item in unit.get("candidates") or [] if isinstance(item, dict)]
        previous = stored_by_compute.get(str(unit.get("computeUnitId") or ""), {})
        sku = str(previous.get("sku") or "")
        candidate = next((item for item in candidates if item.get("sku") == sku), candidates[0] if candidates else None)
        if candidate is None:
            selections = []
            break
        selections.append({
            "computeUnitId": unit.get("computeUnitId"),
            "sku": candidate.get("sku"),
            "replicaCount": previous.get("replicaCount") or unit.get("minimumReplicaCount") or 1,
            "replicationConfirmed": bool(previous.get("replicationConfirmed") or False),
        })
    pricing: dict[str, Any] | None = None
    if selections:
        _preview, pricing = estimate_selected_deployment_cost(
            bundle, selections, target=selected.get("selectedTarget") or target_id,
            capacity_overrides=effective_capacity,
        )
    return {
        "target": selected.get("selectedTarget"),
        "structureDigest": projection.get("deploymentPlanStructureDigest", ""),
        "guidance": guidance,
        # Sizing choices belong to a provider/region/zone projection.  The
        # compatibility summary at bundle level describes only the last
        # selection, so never use it to prefill a different target.
        "selected": list((projection.get("sizing") or {}).get("selected") or []),
        "capacityOverrides": [item.model_dump(by_alias=True) for item in normalized_capacity],
        "pricing": pricing,
    }


def apply_deployment_sizing_session(
    app_id: str,
    target_id: str,
    selections: list[dict[str, Any]],
    expected_structure_digest: str | None = None,
    capacity_overrides: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """VM 선택을 저장하고 같은 ResourcePlan에서 두 그림을 다시 만든다."""

    _validate_app_id(app_id)
    state = _load_app(app_id)
    bundle = state.get("deployment_diagram_bundle")
    if not isinstance(bundle, dict) or not bundle:
        raise ValueError("A deployment bundle must exist before applying VM choices.")
    if expected_structure_digest:
        selected = select_deployment_target(bundle, target_id)
        projection = next(
            item
            for item in selected.get("projections") or []
            if isinstance(item, dict)
            and item.get("target") == selected.get("selectedTarget")
        )
        if projection.get("deploymentPlanStructureDigest") != expected_structure_digest:
            raise ValueError(
                "The deployment preview changed. Reload VM choices before applying."
            )
    updated = apply_compute_selections(
        bundle,
        selections,
        selected_target=target_id,
        capacity_overrides=capacity_overrides,
    )
    if updated.get("status") != "completed":
        return {"app_id": app_id, "status": "needs_input", "sizing": updated.get("sizing")}
    _priced, pricing = estimate_selected_deployment_cost(
        bundle, selections, target=target_id, capacity_overrides=capacity_overrides,
    )
    if pricing is not None:
        selected_projection = next(
            item for item in updated.get("projections") or []
            if isinstance(item, dict) and item.get("target") == updated.get("selectedTarget")
        )
        selected_projection.setdefault("sizing", {})["pricing"] = pricing
        updated.setdefault("sizing", {})["pricing"] = pricing
    hydrated = hydrate_deployment_diagram_bundle(updated)
    state.update(hydrated)
    state["deployment_diagram_puml"] = deployment_bundle_runtime_puml(updated)
    state["deployment_diagram_provisioning_puml"] = (
        deployment_bundle_provisioning_puml(updated)
    )
    artifact_repository.save_stage(
        app_id,
        "deployment_diagram",
        state,
        origin=ORIGIN_FEEDBACK_REVISED,
    )
    current = _load_app(app_id)
    sync_design_state(app_id, dict(current))
    status = session_status(app_id)
    if status.get("active") and status.get("stage") == "deployment_diagram":
        return resume_design_session(app_id)
    return {
        "app_id": app_id,
        **to_web_response(current),
        "status": "completed",
        "sizing": updated.get("sizing"),
    }


def retry_design_session(
    app_id: str, *, repair_guidance: str | None = None,
    binding_source_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """실패한 설계 노드부터 재시도하거나 현재 검토 결과를 복원한다."""
    _validate_app_id(app_id)
    _require_app_exists(app_id)
    status = session_status(app_id)
    if status.get("active") and status.get("stage") == "sequence_diagram":
        state = _load_app(app_id)
        readiness = design_readiness_report(state, stages=["sequence_diagram"])
        repaired = _repair_stale_sequence_projection(app_id, state, readiness)
        if repaired is not None:
            return repaired
    if status.get("active") and status.get("stage") == "class_diagram":
        state = _load_app(app_id)
        readiness_state = _readiness_state_at_active_class_gate(
            app_id, state, "class_diagram"
        )
        readiness = design_readiness_report(readiness_state, stages=["class_diagram"])
        reconciled = _retry_stalled_class_gate_after_reconcile(
            app_id, readiness_state, readiness
        )
        if reconciled is not None:
            return reconciled
    if not status.get("retryable"):
        # 검토 지점은 실패 상태가 아니다. 이때에는 LLM을 다시 호출하지 않고 저장된
        # 결과를 반환하여 새로고침한 Workspace와 실행 상태만 다시 맞춘다.
        if status.get("active") and status.get("stage"):
            state = _load_app(app_id)
            return {
                "app_id": app_id,
                **to_web_response(state),
                "status": "need_feedback",
                "stage": status["stage"],
            }
        raise ValueError(f"No failed design stage is available to retry. Session: {status}")
    try:
        return retry_design(
            app_id,
            repair_guidance=repair_guidance,
            binding_source_decision=binding_source_decision,
        )
    except GenerationStalled:
        raise
    except Exception as error:
        raise RuntimeError(f"Design pipeline failed: {error}") from error


def rewind_design_session(app_id: str, stage: str) -> dict[str, Any]:
    """지정한 단계로 돌아가 해당 산출물부터 다시 만든다."""
    _validate_app_id(app_id)
    _require_app_exists(app_id)
    _require_design_run(app_id)

    if stage not in DESIGN_STAGES:
        raise ValueError(f"Unknown design stage: {stage}")
    if stage == DESIGN_STAGES[0]:
        raise ValueError(f"Rewinding to the first stage is the same as starting again: {stage}")

    try:
        return rewind_design(app_id, stage)
    except StageNotReached as error:
        raise ValueError(str(error)) from error
    except Exception as error:
        raise RuntimeError(f"Design pipeline failed: {error}") from error


def revise_design_stage_session(
    app_id: str,
    stage: str,
    feedback: str,
) -> dict[str, Any]:
    """Apply feedback at an already produced stage without pre-regeneration."""

    _validate_app_id(app_id)
    _require_app_exists(app_id)
    _require_design_run(app_id)
    if not feedback.strip():
        raise ValueError("Design stage revision feedback cannot be empty.")
    try:
        return revise_design_stage(app_id, stage, feedback)
    except StageNotReached as error:
        raise ValueError(str(error)) from error
    except Exception as error:
        raise RuntimeError(f"Design pipeline failed: {error}") from error


def revise_design_element(
    app_id: str,
    request: ReviseRequest,
    *,
    approved_authority_targets: set[str] | None = None,
    approved_downstream_targets: set[str] | None = None,
) -> dict[str, Any]:
    """선택한 설계 요소와 추적 관계로 연결된 부분만 수정한다."""
    return revise_design_elements(
        app_id,
        BatchReviseRequest(revisions=[request]),
        approved_authority_targets=approved_authority_targets,
        approved_downstream_targets=approved_downstream_targets,
    )


def revise_design_elements(
    app_id: str,
    request: BatchReviseRequest,
    *,
    approved_authority_targets: set[str] | None = None,
    approved_downstream_targets: set[str] | None = None,
) -> dict[str, Any]:
    """여러 설계 요소를 메모리에서 차례로 수정하고 모두 성공하면 저장한다."""
    _validate_app_id(app_id)
    original = _load_app(app_id)
    working = original
    changed: list[str] = []
    touched: dict[str, set[str]] = {}
    related: dict[str, list[str]] = {}
    regenerated: dict[str, set[str]] = {}
    batch_targets = {revision.target for revision in request.revisions}

    def class_inventory_target(state: ArchitectureState, target: str) -> bool:
        parsed = _design_target(target)
        if parsed is None or parsed.kind != "class_diagram":
            return False
        model = state.get(DESIGN_SPECS["class_diagram"].model_key) or {}
        return any(
            isinstance(item, dict)
            and str(item.get("className") or "").strip() == parsed.id
            for item in model.get("Classes") or []
        ) if isinstance(model, dict) else False

    try:
        for revision in request.revisions:
            result = revise_and_cascade(
                working,
                revision.target,
                revision.feedback,
                approved_authority_targets=(
                    set(revision.approved_authority_targets)
                    if revision.approved_authority_targets is not None
                    else approved_authority_targets
                ),
                approved_downstream_targets=(
                    set(revision.approved_downstream_targets)
                    if revision.approved_downstream_targets is not None
                    else approved_downstream_targets
                ),
                revision_context_targets=(
                    batch_targets - {revision.target}
                    if class_inventory_target(working, revision.target)
                    else None
                ),
                patch_intents=revision.patch_intents,
            )
            working = result["state"]
            for stage in result["changed"]:
                if stage not in changed:
                    changed.append(stage)
            for stage, elements in result["touched"].items():
                touched.setdefault(stage, set()).update(elements)
            related[revision.target] = result.get("related", [])
            for regenerated_stage, elements in result.get("regenerated", {}).items():
                regenerated.setdefault(regenerated_stage, set()).update(elements)
    except UnknownTarget as error:
        raise ValueError(str(error)) from error
    except UnapprovedScopeExpansion as error:
        raise ValueError(f"Revision requires an approved frozen scope: {error}") from error
    except Exception as error:
        raise RuntimeError(f"Revision failed; no batch changes were saved: {error}") from error

    # A valid LLM response may still reproduce the selected artifact exactly. Use
    # the repository's persisted payload rather than a graph model key: deployment,
    # for example, versions its hydrated bundle and that can change while the
    # workload graph itself remains identical.
    changed = [
        stage
        for stage in changed
        if _persisted_stage_changed(original, working, stage)
    ]
    touched = {stage: elements for stage, elements in touched.items() if stage in changed}
    regenerated = {
        stage: elements for stage, elements in regenerated.items() if stage in changed
    }
    if not changed:
        working = original
    else:
        # Targeted edits invalidate the old stage verdict. Run the ordinary
        # stage check against the final batch state, with automatic whole-model
        # repair disabled so the targeted edit boundary remains intact.
        for stage in changed:
            spec = DESIGN_SPECS.get(stage)
            if spec is None or not spec.check_key:
                continue
            # Class checks have an explicit owner mapper, so they can safely
            # reuse the ordinary bounded checker repair ledger. Other targeted
            # revisions keep their existing no-whole-artifact check behavior.
            checker_spec = spec if spec.repair_target_mapper else replace(spec, repair=None)
            verdict = check_node(checker_spec)(working)
            working = {**working, **verdict}
            if spec.repair_target_mapper:
                working.update(render_and_validate(
                    spec, working.get(spec.model_key) or {}, working
                ))
                continue
            report = dict(working.get(spec.check_key) or {})
            findings = [
                Finding.model_validate(item)
                for item in report.get("finding_details") or []
                if isinstance(item, dict)
            ]
            # Follow up once, and only when the finding names an element that
            # this revision already owns. This uses the existing repair
            # callback/ledger but never invokes check_node's whole-model loop.
            repair = spec.repair
            if not repair or not spec.elements:
                continue
            eligible: list[Finding] = []
            repair_targets: set[str] = set()
            merge_targets: set[str] = set()
            for finding in _repairable_findings(findings):
                location = str(finding.location or "").strip()
                if not location:
                    continue
                owners = {location}
                if owners & touched.get(stage, set()):
                    eligible.append(finding)
                    repair_targets.add(location)
                    merge_targets.update(owners)
            if not eligible:
                continue
            batch = eligible
            input_digest = stable_digest({
                "model": working.get(spec.model_key) or {},
                "findings": [finding.model_dump(mode="json") for finding in batch],
                "targets": sorted(repair_targets),
                "merge_targets": sorted(merge_targets),
            })
            directive = (
                "[TARGETED TECHNICAL REPAIR]\n"
                "Correct only the listed findings on the listed targets. Preserve "
                "every other model element exactly.\n"
                + "\n".join(
                    f"- {finding.rule_id} at {finding.location}: {finding.message}"
                    for finding in batch
                )
            )
            ledger = RepairLedger()
            try:
                current_model = working.get(spec.model_key) or {}
                revised_model = repair(current_model, directive, working, repair_targets)
                candidate = merge_model(spec, current_model, revised_model, merge_targets)
                assert_untargeted_elements_preserved(
                    spec, current_model, candidate, merge_targets
                )
                candidate_state: ArchitectureState = {**working, spec.model_key: candidate}
                if spec.finalize:
                    finalized = spec.finalize(candidate_state)
                    candidate_state = {**candidate_state, **finalized}
                    candidate = candidate_state.get(spec.model_key) or candidate
                candidate_state.update(render_and_validate(spec, candidate, candidate_state))
                candidate_verdict = check_node(replace(spec, repair=None))(candidate_state)
                candidate_state = {**candidate_state, **candidate_verdict}
                candidate_findings = [
                    Finding.model_validate(item)
                    for item in (candidate_verdict.get(spec.check_key) or {}).get("finding_details") or []
                    if isinstance(item, dict)
                ]
                improved = _repair_is_improvement(spec, findings, candidate_findings)
                ledger.record(RepairAttempt(
                    stage=f"design.{stage}",
                    target_ids=tuple(sorted(repair_targets)),
                    strategy_key=f"targeted:{','.join(sorted({f.rule_id for f in batch}))}",
                    input_digest=input_digest,
                    candidate_digest=stable_digest(candidate),
                    finding_keys_before=tuple(sorted(
                        f"{f.rule_id}|{f.location or ''}|{f.message}" for f in findings
                    )),
                    finding_keys_after=tuple(sorted(
                        f"{f.rule_id}|{f.location or ''}|{f.message}" for f in candidate_findings
                    )),
                    outcome=("clean" if improved and not candidate_findings else "improved")
                    if improved else "no_improvement",
                ))
                ledger.status = "COMPLETED" if improved and not candidate_findings else "STALLED"
                if improved:
                    working = candidate_state
                    report = dict(working.get(spec.check_key) or {})
                    report["repair_iters"] = 1
                    report["stopped"] = "clean" if not candidate_findings else "stalled"
                    report["repair_history"] = ledger.model_dump(mode="json")
                    working = {**working, spec.check_key: report}
                else:
                    report["repair_iters"] = 1
                    report["stopped"] = "stalled"
                    report["repair_history"] = ledger.model_dump(mode="json")
                    working = {**working, spec.check_key: report}
            except Exception as error:
                ledger.status = "STALLED"
                ledger.stall_reason = f"{type(error).__name__}: {error}"
                report["stopped"] = "error"
                report["error"] = ledger.stall_reason
                report["repair_iters"] = 1
                report["repair_history"] = ledger.model_dump(mode="json")
                working = {**working, spec.check_key: report}

    # Feedback may only replace an accepted artifact with another accepted
    # artifact. Keep the prior persisted state when bounded repair/checking
    # leaves any changed stage invalid, and return the rejected candidate's
    # validation separately for Workspace review.
    revision_validation: dict[str, Any] = {}
    if changed:
        candidate_response = to_web_response(working)
        candidate_validation = dict(candidate_response.get("validation") or {})
        for stage in changed:
            spec = DESIGN_SPECS.get(stage)
            if spec is None:
                continue
            check = dict(working.get(spec.check_key) or {}) if spec.check_key else {}
            findings = list(check.get("findings") or [])
            errors = list(working.get(spec.errors_key) or [])
            invalid_syntax = bool(spec.valid_key and working.get(spec.valid_key) is False)
            if not findings and not errors and not invalid_syntax:
                continue
            stage_validation = dict(candidate_validation.get(stage) or {})
            stage_validation.setdefault("errors", errors)
            stage_validation.setdefault("findings", findings)
            if check.get("finding_details") is not None:
                stage_validation.setdefault("finding_details", list(check["finding_details"]))
            if check.get("stopped") is not None:
                stage_validation.setdefault("check_status", check["stopped"])
            if check.get("repair_iters") is not None:
                stage_validation.setdefault("repair_iters", check["repair_iters"])
            if check.get("repair_history") is not None:
                stage_validation["repair_history"] = check["repair_history"]
            if invalid_syntax and not stage_validation.get("errors"):
                stage_validation["errors"] = ["Rendered artifact validation failed."]
            revision_validation[stage] = stage_validation

    if revision_validation:
        return {
            "app_id": app_id,
            **to_web_response(original),
            "status": "stalled",
            "revision_status": "stalled",
            "revision_validation": revision_validation,
            "changed": [],
            "touched": {},
            "related": related,
            "regenerated": {},
        }

    combined = {
        "state": working,
        "changed": changed,
        "touched": {stage: sorted(elements) for stage, elements in touched.items()},
    }
    # Update the checkpoint first. If artifact persistence then fails, restore
    # the prior checkpoint so a partial database batch cannot become visible.
    # Artifact versions themselves are committed by one repository transaction.
    if changed:
        sync_design_state(app_id, cast(dict[str, Any], working))
        try:
            persist_cascade(app_id, combined)
        except Exception:
            sync_design_state(app_id, cast(dict[str, Any], original))
            raise

    return {
        "app_id": app_id,
        **to_web_response(working),
        "changed": changed,
        "touched": combined["touched"],
        "related": related,
        "regenerated": {stage: sorted(elements) for stage, elements in regenerated.items()},
    }


def _validate_app_id(app_id: str) -> None:
    """저장소를 조회하기 전에 앱 ID가 UUID 형식인지 확인한다."""
    try:
        uuid.UUID(app_id)
    except ValueError as error:
        raise ValueError("Invalid app id.") from error


def _load_app(app_id: str) -> ArchitectureState:
    """저장된 앱 전체를 읽고, 존재하지 않으면 이해하기 쉬운 오류를 낸다."""
    try:
        return artifact_repository.load_state(app_id)
    except AppNotFound as error:
        raise LookupError("Unknown app id.") from error


def _require_app_exists(app_id: str) -> None:
    """산출물 전체를 읽지 않고 앱이 존재하는지만 확인한다."""
    try:
        artifact_repository.ensure_app_exists(app_id)
    except AppNotFound as error:
        raise LookupError("Unknown app id.") from error


def _require_active_session(app_id: str) -> None:
    """설계가 검토 지점에 멈춰 있는지 확인한다."""
    if not has_active_session(app_id):
        raise ValueError("No design session is in progress. Start the design first.")


def _require_design_run(app_id: str) -> None:
    """완료 여부와 관계없이 되감을 설계 실행 이력이 있는지 확인한다."""
    if not has_design_run(app_id):
        raise ValueError("This app has no design run to rewind. Start the design first.")
