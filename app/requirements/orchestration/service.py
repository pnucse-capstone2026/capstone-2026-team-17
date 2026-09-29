"""요구사항 분석 실행과 산출물 저장을 연결하는 application service다.

Workspace는 이 모듈을 직접 호출한다. HTTP 요청과 응답을 다루지 않으므로 FastAPI에
의존하지 않으며, 그래프가 만든 검증된 결과 ``dict``를 그대로 반환한다.
그래프 자체(app.requirements.orchestration.graph)는 호출 방식과 무관하게 재사용된다.

산출물은 단계가 끝날 때마다 설계 에이전트와 같은 MySQL 저장소에 저장된다.
요청에 app_id가 있을 때만 저장하므로, 저장소 없이 단독으로 돌려보는 것도 그대로 된다.
"""

import uuid
from typing import cast

from app.metrics import langsmith as langsmith_metrics
from app.repositories import artifact_repository
from app.requirements.config import settings
from app.requirements.contracts.request import (
    AnalyzeRequest,
    DeploymentPreferences,
    FeedbackEdit,
    IdentitySourceAnswer,
    ResourceAnswer,
)
from app.requirements.modeling.specifications import (
    apply_identity_source_overrides,
    check_specs,
    generate_specification,
    identity_source_question,
)
from app.requirements.orchestration.graph import (
    capture_analysis_checkpoint,
    restore_analysis_checkpoint,
    resume_analysis,
    retry_analysis,
    revise_analysis,
    start_analysis,
)
from app.requirements.runtime import telemetry

# Workspace 프로세스에서 이 서비스를 처음 불러올 때 한 번 로깅을 설정한다.
# 여러 번 불러도 핸들러가 겹치지 않도록 telemetry 쪽에서 보호한다.
telemetry.configure_logging()


def persist_analysis(app_id: str, payload: dict[str, object]) -> list[str]:
    """응답에 실린 산출물 중 달라진 것을 새 버전으로 남기고, 저장한 stage를 돌려준다.

    단계가 끝날 때마다(피드백 게이트 응답 포함) 호출되므로, 4단계까지 가지 않고
    중간에 그만둬도 그때까지의 산출물은 남는다.

    액터·유스케이스와 상세 명세는 같은 usecase_spec 산출물의 순차 버전으로 저장한다.
    2단계 게이트에서는 현재까지 완성된 유스케이스 모델을 보여주고, 3단계에서 상세
    명세가 추가되면 같은 산출물의 새 버전으로 갱신한다. 설계 파이프라인은 요구사항
    분석 전체가 끝난 뒤 시작하므로 중간 버전이 설계 입력으로 소비되지는 않는다.

    STAGE_ARTIFACTS(app/repositories/artifact_repository.py)에 이미 자리가 있어
    스키마 변경은 필요 없다. resource_spec은 **2026-07-28부터 이 에이전트가 만든다**
    (`resources/service.py`) — 계약을 만족한 실행에서만 온다.
    """
    if payload.get("status") not in ("need_feedback", "completed"):
        return []  # clarify 질문 응답에는 아직 산출물이 없다

    stored = cast(dict[str, object], artifact_repository.load_state(app_id))
    saved: list[str] = []
    updates: dict[str, object] = {}

    def collect(stage: str, state_key: str, content: object) -> None:
        # 내용이 그대로면 건너뛴다. 피드백 없이 다음 단계로 넘어갈 때마다 같은
        # 산출물이 새 버전으로 쌓이는 것을 막는다(응답은 누적 산출물을 매번 싣는다).
        if not content or stored.get(state_key) == content:
            return
        updates[state_key] = content
        saved.append(stage)

    collect("refined_requirements", "refined_requirements", payload.get("requirements"))
    collect("capability_contract", "capability_contract", payload.get("capability_contract"))
    collect("resource_intake", "resource_intake", payload.get("resource_intake"))

    actors = payload.get("actors") or []
    use_cases = payload.get("use_cases") or []
    use_case_specs = payload.get("use_case_specs") or []
    traceability = payload.get("traceability") or {}
    if actors or use_cases or use_case_specs:
        # 이 객체는 유스케이스 분석과 상세 명세의 누적 산출물이다. 먼저 분석 결과를
        # 리뷰하고, 다음 게이트에서는 상세 명세가 더해진 같은 객체를 리뷰한다.
        usecase_artifact = {
            "actors": actors,
            "use_cases": use_cases,
            "use_case_specs": use_case_specs,
        }
        if traceability:
            usecase_artifact["traceability"] = traceability
        collect("usecase_spec", "usecase_spec", usecase_artifact)

    collect("usecase_diagram", "usecase_diagram_puml", payload.get("diagram"))
    # `RESOURCE_SPEC`. **계약을 만족한 것만 온다** — `build_resource_spec`이 통과하지
    # 못한 초안은 `resource_intake`에만 남기고 이 키를 아예 내지 않는다. 그래서 여기서
    # 다시 검사하지 않는다(같은 판정을 두 곳에 두면 한쪽만 고쳐진다).
    collect("resource_spec", "resource_spec", payload.get("resource_spec"))
    if saved:
        artifact_repository.save_stages(
            app_id,
            saved,
            cast(dict, {**stored, **updates}),
        )
    return saved


def analyze_requirements(req: AnalyzeRequest) -> dict[str, object]:
    """요구사항 분석 세션을 시작하거나(구체화 질문에 대한) 답변으로 재개한다.

    - 신규 세션: requirements 를 담아 호출 (thread_id는 서버가 발급).
    - 구체화 답변: answer + 기존 thread_id 로 호출.
    응답이 need_clarification 이면 questions 를 사용자에게 보여주고,
    답변을 다시 이 함수에 담아 보내면 세션이 이어진다.
    app_id를 함께 보내면 단계가 끝날 때마다 그 앱의 저장소에 기록되고,
    이번 호출에서 저장된 stage 목록이 saved_stages로 돌아온다.
    """
    # 재개 값은 **하나**다. 둘 이상 오면 무엇을 따를지가 모호하므로 거절한다 —
    # 골라서 쓰면 화면이 보낸 것과 서버가 쓴 것이 조용히 갈린다.
    given = [
        name
        for name, value in (
            ("answer", req.answer),
            ("edit", req.edit),
            ("resource_answers", req.resource_answers),
            ("resource_answer", req.resource_answer),
            ("identity_source_answer", req.identity_source_answer),
            (
                "deployment_preferences",
                req.deployment_preferences if not req.requirements else None,
            ),
        )
        if value is not None
    ]
    if len(given) > 1:
        raise ValueError(
            f"Send only one resume input, not all of: {' / '.join(given)}."
        )

    # 재개 경로 — 자연어(answer) · 구조화 편집(edit) · 되묻기의 답(resource_answers).
    resume: str | FeedbackEdit | ResourceAnswer | IdentitySourceAnswer | DeploymentPreferences | None = (
        req.answer if req.answer is not None else req.edit
    )
    if resume is None and req.resource_answers is not None:
        resume = ResourceAnswer(answers=req.resource_answers)
    if resume is None and req.resource_answer is not None:
        resume = req.resource_answer
    if resume is None and req.identity_source_answer is not None:
        resume = req.identity_source_answer
    if resume is None and req.deployment_preferences is not None and not req.requirements:
        resume = req.deployment_preferences
    with langsmith_metrics.trace_metadata(
        {"app_id": req.app_id} if req.app_id else None
    ):
        if resume is not None:
            if not req.thread_id:
                raise ValueError(
                    "answer, edit, and resource_answers require a thread_id."
                )
            payload = resume_analysis(
                resume, req.thread_id, persist=settings.enable_session_persistence
            )
        else:
            # 신규 분석 시작 경로
            if not req.requirements:
                raise ValueError(
                    "Provide requirements, or provide an answer with a thread_id."
                )
            thread_id = req.thread_id or str(uuid.uuid4())
            payload = start_analysis(
                req.requirements,
                thread_id,
                req.feedback_gates,
                persist=settings.enable_session_persistence,
                constraints_text=req.resource_constraints_text or "",
                cloud_constraints=(
                    req.deployment_preferences.model_dump(mode="json", exclude_unset=True)
                    if req.deployment_preferences is not None
                    else (
                        req.cloud_constraints.model_dump(mode="json")
                        if req.cloud_constraints is not None
                        else None
                    )
                ),
            )

    if req.app_id:
        try:
            payload["saved_stages"] = persist_analysis(req.app_id, payload)
        except artifact_repository.AppNotFound as error:
            raise ValueError(f"App {req.app_id} was not found.") from error
        payload["app_id"] = req.app_id

    return payload


def retry_requirements_analysis(
    thread_id: str, *, app_id: str | None = None
) -> dict[str, object]:
    """저장된 요구사항 checkpoint를 재개하고 새로 생긴 산출물만 저장한다."""
    with langsmith_metrics.trace_metadata({"app_id": app_id} if app_id else None):
        payload = retry_analysis(
            thread_id,
            persist=settings.enable_session_persistence,
        )
    if app_id:
        try:
            payload["saved_stages"] = persist_analysis(app_id, payload)
        except artifact_repository.AppNotFound as error:
            raise ValueError(f"App {app_id} was not found.") from error
        payload["app_id"] = app_id
    return payload


def _revise_local_spec_from_artifacts(
    edit: FeedbackEdit,
    thread_id: str,
    *,
    app_id: str,
) -> dict[str, object]:
    """Revise one saved specification when the requirements checkpoint is absent."""

    state = cast(dict[str, object], artifact_repository.load_state(app_id))
    requirements = state.get("refined_requirements")
    artifact = state.get("usecase_spec")
    if not isinstance(requirements, list) or not isinstance(artifact, dict):
        raise ValueError(  # noqa: TRY004
            "A local specification revision requires current refined_requirements and usecase_spec artifacts."
        )

    by_id: dict[str, dict[str, object]] = {}
    for requirement in requirements:
        if not isinstance(requirement, dict) or not requirement.get("id"):
            raise ValueError("The saved refined_requirements artifact is stale or invalid.")
        requirement_id = str(requirement["id"])
        if requirement_id in by_id:
            raise ValueError(f"The saved refined_requirements artifact has an ambiguous id {requirement_id!r}.")
        by_id[requirement_id] = requirement

    use_cases = artifact.get("use_cases")
    specs = artifact.get("use_case_specs")
    actors = artifact.get("actors") or []
    if not isinstance(use_cases, list) or not isinstance(specs, list) or not isinstance(actors, list):
        raise ValueError("The saved usecase_spec artifact is stale or invalid.")  # noqa: TRY004

    target_id = edit.target_ids[0]
    target_use_cases = [
        item for item in use_cases if isinstance(item, dict) and str(item.get("id")) == target_id
    ]
    target_specs = [
        item
        for item in specs
        if isinstance(item, dict) and str(item.get("use_case_id")) == target_id
    ]
    if len(target_use_cases) != 1 or len(target_specs) != 1:
        raise ValueError(f"The local specification target {target_id!r} is stale or ambiguous.")

    target_use_case = dict(target_use_cases[0])
    referenced_ids = [
        str(value)
        for key in ("requirement_ids", "nfr_ids")
        for value in target_use_case.get(key, []) or []
    ]
    missing_ids = sorted(set(referenced_ids) - set(by_id))
    if missing_ids:
        raise ValueError(
            f"The local specification target {target_id!r} references stale requirements: "
            + ", ".join(missing_ids)
        )

    target_use_case["_existing_spec"] = target_specs[0]
    revised_spec = generate_specification(
        target_use_case,
        by_id,
        actors,
        edit.instruction,
    )
    persisted_source_overrides: dict[str, dict[str, str]] = {}
    for prior_spec in specs:
        if not isinstance(prior_spec, dict):
            continue
        prior_contract = prior_spec.get("public_contract")
        prior_obligations = prior_contract.get("identity_obligations", []) if isinstance(prior_contract, dict) else []
        for obligation in prior_obligations:
            if not isinstance(obligation, dict) or obligation.get("obligation") != "identify":
                continue
            kind = obligation.get("identity_source_kind")
            ref = obligation.get("obligation_ref")
            auth_ref = obligation.get("source_authenticate_obligation_ref")
            if kind in ("caller_input", "system_result") and isinstance(ref, str):
                persisted_source_overrides[ref] = {"identity_source_kind": str(kind)}
            elif kind == "authenticated_context" and isinstance(ref, str) and isinstance(auth_ref, str):
                persisted_source_overrides[ref] = {
                    "identity_source_kind": "authenticated_context",
                    "source_authenticate_obligation_ref": auth_ref,
                }
    revised_spec = apply_identity_source_overrides(
        [revised_spec], persisted_source_overrides
    )[0]
    revised_specs = list(specs)
    target_index = next(
        index
        for index, item in enumerate(revised_specs)
        if isinstance(item, dict) and str(item.get("use_case_id")) == target_id
    )
    revised_specs[target_index] = revised_spec
    revised_artifact = {**artifact, "use_case_specs": revised_specs}
    save_state = {**state, "usecase_spec": revised_artifact}
    saved = artifact_repository.save_stages(app_id, ["usecase_spec"], save_state)

    payload: dict[str, object] = {
        "thread_id": thread_id,
        # Workspace uses the app id to bind the open identity-source question
        # to the current saved use-case specification before presenting it.
        "app_id": app_id,
        "phase": "specs",
        "status": "need_feedback",
        "feedback_prompt": "Enter feedback for [specs]. Leave it blank to continue.",
        "feedback_summary": [str(item.get("use_case_id")) for item in revised_specs],
        "edit_stage": "specs",
        "edit_targets": [str(item.get("use_case_id")) for item in revised_specs],
        "resource_questions": None,
        "blocking_findings": None,
        "requires_revision": None,
        "repair_state": None,
        "requirements": requirements,
        "actors": artifact.get("actors", []),
        "use_cases": use_cases,
        "use_case_specs": revised_specs,
        "spec_report": check_specs({"use_case_specs": revised_specs})["spec_report"],
        "identity_source_question": identity_source_question({"use_case_specs": revised_specs}),
        "saved_stages": list(saved),
    }
    if "traceability" in artifact:
        payload["traceability"] = artifact["traceability"]
    return payload


def revise_requirements_analysis(
    edit: FeedbackEdit,
    thread_id: str,
    *,
    app_id: str,
) -> dict[str, object]:
    """Apply a validated edit by re-entering its persisted feedback gate."""

    try:
        checkpoint = capture_analysis_checkpoint(
            thread_id,
            persist=settings.enable_session_persistence,
        )
    except ValueError as error:
        expected = f"No saved checkpoint was found for requirements run {thread_id!r}."
        if (
            str(error) == expected
            and edit.stage == "specs"
            and edit.scope == "local"
            and len(edit.target_ids) == 1
        ):
            try:
                return _revise_local_spec_from_artifacts(
                    edit,
                    thread_id,
                    app_id=app_id,
                )
            except artifact_repository.AppNotFound as app_error:
                raise ValueError(f"App {app_id} was not found.") from app_error
        raise
    payload = revise_analysis(
        edit,
        thread_id,
        persist=settings.enable_session_persistence,
    )
    try:
        payload["saved_stages"] = persist_analysis(app_id, payload)
    except artifact_repository.AppNotFound as error:
        restore_analysis_checkpoint(
            thread_id,
            checkpoint,
            persist=settings.enable_session_persistence,
        )
        raise ValueError(f"App {app_id} was not found.") from error
    except Exception:
        restore_analysis_checkpoint(
            thread_id,
            checkpoint,
            persist=settings.enable_session_persistence,
        )
        raise
    payload["app_id"] = app_id
    return payload
