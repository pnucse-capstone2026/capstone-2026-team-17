"""Workspace 화면에서 사용하는 HTTP API를 제공한다.

Workspace는 사용자의 메시지를 명령으로 등록하고, 백그라운드 파이프라인이 남긴 이벤트와
산출물 상태를 프론트엔드에 전달한다. 이 모듈은 HTTP 요청을 검사하고 적절한 서비스 또는
저장소를 호출하는 얇은 경계다. 요구사항 분석이나 설계 생성 같은 실제 작업은
``workspace_service``가 실행하며, 이 모듈 안에서 직접 LLM을 호출하지 않는다.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.artifacts_api import require_app, to_web_response, validate_app_id
from app.cloudkb import region_catalog
from app.design.service import (
    apply_deployment_sizing_session,
    deployment_sizing_session,
)
from app.design.services.common.plantuml import render_plantuml
from app.repositories import artifact_repository
from app.requirements.schemas import DeploymentPreferences

from . import repository
from .contracts import CheckpointStage, RestartStage, WorkspaceAction
from .live_preview import live_previews
from .service import workspace_service

router = APIRouter(prefix="/api/workspace", tags=["workspace"])


def _testing_report(command: Mapping[str, Any]) -> dict[str, Any] | None:
    """Testing command에서 화면에 보여 줄 가장 최근 보고서를 찾는다.

    완료된 command는 ``result.job.result``에 보고서를 보관한다. 서버가 검사 도중
    재시작됐다면 같은 내용이 command payload의 checkpoint에 먼저 저장될 수 있다.
    어느 쪽이든 Testing 입력 계약 자체는 매우 크고 결과 화면에는 필요하지 않으므로
    제외하고, 실제 판정과 검사 근거만 반환한다.
    """

    result = command.get("result")
    result = result if isinstance(result, Mapping) else {}
    job = result.get("job")
    job_report = job.get("result") if isinstance(job, Mapping) else None
    payload = command.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    checkpoint = payload.get("testing_checkpoint")
    checkpoint_report = (
        checkpoint.get("result") if isinstance(checkpoint, Mapping) else None
    )

    candidates = (job_report, checkpoint_report, result)
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        # 실행 준비 checkpoint에는 보존한 계획만 들어 있을 수 있다. 실제 검사 결과를
        # 뜻하는 키가 하나라도 생긴 뒤부터 결과 보고서로 공개한다.
        if not any(
            key in candidate
            for key in ("verification", "passed", "gateStatus", "blocking_findings")
        ):
            continue
        report = dict(candidate)
        report.pop("testingInput", None)
        return report
    return None


@router.get("/apps/{app_id}/testing-result")
def get_testing_result(app_id: str) -> dict[str, Any]:
    """가장 최근 Testing 실행의 상태와 상세 결과를 반환한다.

    Testing 결과는 별도 테이블이나 산출물로 복사하지 않는다. 이미 저장된 Workspace
    command를 읽으므로 이후 단계로 이동한 뒤에도 마지막 검사 결과를 다시 볼 수 있다.
    """

    validate_app_id(app_id)
    require_app(app_id)
    command = repository.latest_command(app_id, stage="testing")
    if command is None:
        return {
            "app_id": app_id,
            "available": False,
            "command_id": None,
            "command_status": None,
            "implementation_job_id": None,
            "created_at": None,
            "started_at": None,
            "completed_at": None,
            "report": None,
        }

    result = command.get("result")
    result = result if isinstance(result, Mapping) else {}
    job = result.get("job")
    job = job if isinstance(job, Mapping) else {}
    payload = command.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    checkpoint = payload.get("testing_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, Mapping) else {}
    return {
        "app_id": app_id,
        "available": True,
        "command_id": command.get("command_id"),
        "command_status": command.get("status"),
        "implementation_job_id": (
            job.get("implementation_job_id")
            or checkpoint.get("implementation_job_id")
            or payload.get("implementation_job_id")
        ),
        "created_at": command.get("created_at"),
        "started_at": command.get("started_at"),
        "completed_at": command.get("completed_at"),
        "report": _testing_report(command),
    }


def _class_preview(app_id: str, command_id: str):
    """명령에 속한 클래스 다이어그램의 생성 중 미리보기를 찾는다.

    미리보기는 아직 정식 산출물 버전이 아니므로 메모리에만 있다. 다른 앱의 명령 ID를
    넣어 미리보기를 읽지 못하도록 명령과 앱의 관계도 함께 확인한다.
    """
    validate_app_id(app_id)
    command = repository.get_command(command_id)
    if command is None or str(command.get("app_id") or "") != app_id:
        raise HTTPException(status_code=404, detail="Unknown workspace command.")
    preview = live_previews.get(app_id, command_id, "class_diagram")
    if preview is None:
        raise HTTPException(
            status_code=404, detail="Class diagram preview is not available."
        )
    return preview


@router.get("/apps/{app_id}/commands/{command_id}/previews/class_diagram")
def get_class_diagram_preview(app_id: str, command_id: str) -> dict[str, Any]:
    """클래스 다이어그램 생성 진행률과 현재까지의 PlantUML을 반환한다."""
    preview = _class_preview(app_id, command_id)
    return {
        "command_id": preview.command_id,
        "stage": preview.stage,
        "revision": preview.revision,
        "phase": preview.phase,
        "unit": preview.unit,
        "completed": preview.completed,
        "total": preview.total,
        "puml": preview.puml,
    }


@router.get(
    "/apps/{app_id}/commands/{command_id}/previews/class_diagram/image.svg"
)
def get_class_diagram_preview_image(app_id: str, command_id: str) -> Response:
    """생성 중인 클래스 다이어그램을 SVG 이미지로 반환한다."""
    preview = _class_preview(app_id, command_id)
    # 같은 revision을 반복해서 조회할 때마다 PlantUML 서버를 호출하지 않도록 SVG를
    # 미리보기 저장소에 보관한다. revision이 달라지면 cache_svg가 이전 그림을 덮지 않는다.
    image = preview.image_svg or render_plantuml(preview.puml, "svg")
    if not image:
        raise HTTPException(status_code=500, detail="Diagram rendering failed.")
    if preview.image_svg is None:
        live_previews.cache_svg(
            app_id, command_id, preview.stage, preview.revision, image,
        )
    return Response(
        content=image,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


class CreateWorkspaceAppRequest(BaseModel):
    """새 앱을 만들 때 프론트엔드가 보내는 최초 입력."""

    message: str = Field(min_length=1, max_length=30000)
    # provider와 region은 이전 화면에서도 보내던 선택 필드다. 현재 화면은 분석 대화가
    # 진행되는 동안 여러 배포 후보를 모으므로, 새 요청에서는 두 값이 없어도 된다.
    provider: Literal["aws", "azure", "gcp"] | None = None
    region: str = Field(default="", max_length=100)
    monthly_budget_amount: float | None = Field(default=None, gt=0)
    monthly_budget_currency: str = Field(default="USD", min_length=3, max_length=3)
    resource_constraints_text: str = Field(default="", max_length=12000)

    @field_validator("monthly_budget_currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        """통화 코드를 ISO 4217에서 사용하는 세 글자 대문자 형태로 정리한다."""
        value = value.strip().upper()
        if len(value) != 3 or not value.isalpha():
            raise ValueError("monthly_budget_currency must be a three-letter code")
        return value


class WorkspaceCommandRequest(BaseModel):
    """이미 만들어진 앱에서 다음 작업을 요청하는 명령."""

    # action은 프론트엔드가 임의 문자열을 보내지 못하도록 가능한 값을 고정한다. 나머지
    # 필드는 action별 선택 값이며, 실제 조합 검사는 workspace_service가 담당한다.
    action: WorkspaceAction
    text: str = Field(default="", max_length=30000)
    context: dict[str, Any] | None = None
    action_id: str | None = None
    job_id: str | None = None
    implementation_job_id: str | None = None
    base_package: str = "com.easydep.app"
    allow_assumptions: bool = True
    retry_failed: bool = False
    auto_approve_method_proposals: bool = False
    deployment_preferences: dict[str, Any] | None = None
    checkpoint_stage: CheckpointStage | None = None
    restart_stage: RestartStage | None = None
    feedback_option_id: str | None = Field(default=None, min_length=1, max_length=200)
    feedback_free_text: bool | None = None


class ComputeSizingSelectionRequest(BaseModel):
    """한 compute unit의 최종 VM 선택이다."""

    computeUnitId: str = Field(min_length=1, max_length=200)
    sku: str = Field(min_length=1, max_length=200)
    replicaCount: int = Field(ge=1, le=100)
    replicationConfirmed: bool = False


class CapacityOverrideRequest(BaseModel):
    """Optional late capacity input for a provider-specific sizing preview."""

    computeUnitId: str = Field(min_length=1, max_length=200)
    minVCpu: float = Field(gt=0)
    minMemoryGiB: float = Field(gt=0)

    @field_validator("minVCpu", "minMemoryGiB", mode="before")
    @classmethod
    def reject_boolean_capacity(cls, value: object) -> object:
        if isinstance(value, bool):
            raise TypeError("capacity overrides must be numbers")
        return value


class ApplyDeploymentSizingRequest(BaseModel):
    """한 deployment target에 적용할 모든 compute 선택이다."""

    targetId: str = Field(min_length=1, max_length=1000)
    structureDigest: str | None = Field(default=None, min_length=1, max_length=128)
    selections: list[ComputeSizingSelectionRequest] = Field(min_length=1, max_length=50)
    capacityOverrides: list[CapacityOverrideRequest] | None = Field(default=None, max_length=50)


@router.get("/apps")
def list_apps(limit: int = Query(default=50, ge=1, le=100)) -> dict[str, Any]:
    """최근 생성한 앱을 최신순으로 조회한다."""
    return {"apps": repository.list_workspace_apps(limit)}


@router.get("/cloud-options")
def cloud_options() -> dict[str, Any]:
    """배포 후보 입력 화면에 표시할 CSP 지역과 통화 목록을 반환한다."""
    providers: dict[str, list[dict[str, Any]]] = {
        name: [] for name in ("aws", "azure", "gcp")
    }
    for item in region_catalog.catalog():
        if item.provider in providers:
            providers[item.provider].append(
                {
                    "code": item.code,
                    "name": item.name,
                    "latitude": item.latitude,
                    "longitude": item.longitude,
                    "zones": list(item.zones),
                }
            )
    return {"regions": providers, "currencies": ["USD", "KRW", "EUR", "JPY"]}


@router.post("/apps", status_code=202)
def create_workspace_app(request: CreateWorkspaceAppRequest) -> dict[str, Any]:
    """앱을 만든 뒤 최초 요구사항 분석 명령을 비동기로 등록한다."""
    message = request.message.strip()
    region = request.region.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Enter application requirements.")
    # 이전 클라이언트가 사용하는 단일 provider/region 형식은 둘 중 하나만 있으면 의미를
    # 정할 수 없다. 따라서 둘을 함께 보내거나 둘 다 생략하도록 검사한다.
    if bool(request.provider) != bool(region):
        raise HTTPException(
            status_code=422,
            detail="Legacy provider and region values must be supplied together.",
        )
    # 명령보다 앱 행을 먼저 만든다. 이후 명령 실행이 실패해도 사용자가 같은 app_id에서
    # 오류 내용을 확인하고 다시 시도할 수 있다.
    app_id = artifact_repository.create_app(
        requirements_text=message,
        resource_constraints_text=request.resource_constraints_text.strip(),
    )
    try:
        command = workspace_service.submit(
            app_id,
            action="message",
            stage="requirements",
            payload={
                "text": message,
                **({"provider": request.provider, "region": region} if request.provider else {}),
                "monthly_budget_amount": request.monthly_budget_amount,
                "monthly_budget_currency": request.monthly_budget_currency.upper(),
                "resource_constraints_text": request.resource_constraints_text.strip(),
            },
        )
    except Exception as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"app_id": app_id, "command": command}


@router.put("/apps/{app_id}/deployment-preferences")
def save_deployment_preferences(
    app_id: str, request: DeploymentPreferences
) -> dict[str, Any]:
    """분석 명령을 새로 만들지 않고 사용자가 선택한 배포 후보를 저장한다.

    요구사항 분석 중에도 화면에서 후보를 바꿀 수 있으므로, 활성 명령과 충돌하는 별도
    command를 만들지 않는다. 저장 후 대기 중인 분석이 있으면 서비스가 이어서 진행한다.
    """
    validate_app_id(app_id)
    try:
        artifact_repository.ensure_app_exists(app_id)
    except artifact_repository.AppNotFound as error:
        raise HTTPException(status_code=404, detail="Unknown app id.") from error
    catalog = {
        (item.provider, item.code): item for item in region_catalog.catalog()
    }
    for target in request.targets:
        region = catalog.get((target.provider, target.region))
        if region is None:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown {target.provider} region: {target.region}",
            )
        unknown_zones = sorted(set(target.zones) - set(region.zones))
        if unknown_zones:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Unknown zones for {target.provider}/{target.region}: "
                    + ", ".join(unknown_zones)
                ),
            )

    # Pydantic 기본값은 이전 클라이언트의 요청도 받을 수 있게 해 주지만, 사용자가 직접
    # 선택했다는 뜻은 아니다. exclude_unset=True로 실제 전송한 필드만 저장한다.
    selection = request.model_dump(mode="json", exclude_unset=True)
    stored = repository.save_deployment_preferences(app_id, selection)
    if "resource_constraints_text" in selection:
        artifact_repository.update_inputs(
            app_id, resource_constraints_text=request.resource_constraints_text
        )
    resume = workspace_service.apply_saved_deployment_preferences(app_id)
    return {"preferences": stored, "resume_command": resume}


@router.get("/apps/{app_id}/deployment-sizing")
def get_deployment_sizing(
    app_id: str,
    target: str = Query(min_length=1),
    capacity: str | None = Query(default=None),
) -> dict[str, Any]:
    """저장 target의 VM 후보와 전체 배포 비용 견적을 반환한다.

    ``pricing``의 미확정 항목은 0원이 아니라 known monthly floor 밖의 unknown term으로
    반환한다.
    """

    validate_app_id(app_id)
    try:
        parsed_capacity: list[dict[str, Any]] | None = None
        if capacity is not None:
            candidate = json.loads(capacity)
            if not isinstance(candidate, list):
                raise ValueError("capacity must be a JSON array.")
            parsed_capacity = candidate
        return deployment_sizing_session(app_id, target, parsed_capacity)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=422, detail="capacity must be a JSON array.") from error
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.put("/apps/{app_id}/deployment-sizing")
def apply_deployment_sizing(
    app_id: str, request: ApplyDeploymentSizingRequest
) -> dict[str, Any]:
    """검증된 VM 선택을 ResourcePlan과 저장된 배포 산출물에 반영한다."""

    validate_app_id(app_id)
    try:
        kwargs: dict[str, Any] = {}
        if request.capacityOverrides is not None:
            kwargs["capacity_overrides"] = [
                override.model_dump() for override in request.capacityOverrides
            ]
        result = apply_deployment_sizing_session(
            app_id,
            request.targetId,
            [selection.model_dump() for selection in request.selections],
            request.structureDigest,
            **kwargs,
        )
        workspace_service.sync_deployment_configuration(app_id, result)
        return result
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/apps/{app_id}")
def get_workspace(app_id: str) -> dict[str, Any]:
    """Workspace 화면을 다시 그리는 데 필요한 현재 상태를 한 번에 반환한다."""
    validate_app_id(app_id)
    state = require_app(app_id)
    # 구현 작업은 별도 프로세스에서 끝날 수 있다. 화면 snapshot을 만들기 전에 DB의 실제
    # 작업 상태와 Workspace 명령 상태를 맞춰, 완료됐는데 계속 실행 중으로 보이지 않게 한다.
    workspace_service.reconcile_implementation_command(app_id)
    web = to_web_response(state)
    artifacts = {
        name: {
            "available": bool(content),
            "status": web.get("artifact_status", {}).get(name),
            "validation": web.get("validation", {}).get(name),
        }
        for name, content in web.get("artifacts", {}).items()
    }
    command = workspace_service.present_command(
        app_id, repository.latest_command(app_id)
    )
    return {
        "app_id": app_id,
        "current_stage": (
            command["stage"]
            if command is not None
            else repository.get_app_summary(app_id)["current_stage"]
        ),
        "command": command,
        "events": repository.list_timeline_events(app_id, include_llm_timings=False),
        "progress_cursor": repository.progress_cursor(app_id),
        "artifacts": artifacts,
        "deployment_preferences": repository.get_deployment_preferences(app_id),
    }


@router.get("/apps/{app_id}/events/{event_id}/llm-timings")
def get_event_llm_timings(
    app_id: str,
    event_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """큰 설계 LLM 원문 기록을 Workspace에서 펼친 page만 반환한다."""
    validate_app_id(app_id)
    try:
        return repository.get_event_llm_timings(
            app_id, event_id, offset=offset, limit=limit
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown workspace event.") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/apps/{app_id}/commands", status_code=202)
def create_command(app_id: str, request: WorkspaceCommandRequest) -> dict[str, Any]:
    """사용자 메시지나 다음 단계 진행 요청을 Workspace 명령으로 등록한다."""
    validate_app_id(app_id)
    payload = request.model_dump(mode="json", exclude={"action"}, exclude_none=True)
    if request.action == "message" and not request.text.strip():
        raise HTTPException(status_code=422, detail="Enter a message.")
    try:
        command = workspace_service.submit(
            app_id, action=request.action.value, payload=payload
        )
    except artifact_repository.AppNotFound as error:
        raise HTTPException(status_code=404, detail="Unknown app id.") from error
    except (RuntimeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"app_id": app_id, "command": command}


@router.post("/apps/{app_id}/commands/{command_id}/stop")
def stop_command(app_id: str, command_id: str) -> dict[str, Any]:
    """Request cancellation of an active command without creating another one."""

    validate_app_id(app_id)
    try:
        command = repository.request_stop(app_id, command_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown workspace command.") from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    repository.notify_command_changed(
        app_id, command_id=command_id, stage=str(command.get("stage") or "requirements")
    )
    return {"app_id": app_id, "command": command}


@router.get("/apps/{app_id}/events")
async def stream_events(
    app_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    """새 Workspace 이벤트를 SSE(Server-Sent Events) 스트림으로 전달한다.

    브라우저가 연결을 다시 맺으면 ``Last-Event-ID`` 헤더나 ``after`` query parameter를
    사용해 마지막으로 받은 이벤트 다음부터 전송한다. 이 방식으로 네트워크가 잠시 끊겨도
    이미 표시한 메시지는 중복하지 않고, 그동안 생긴 메시지도 빠뜨리지 않는다.
    """
    validate_app_id(app_id)
    try:
        artifact_repository.ensure_app_exists(app_id)
    except artifact_repository.AppNotFound as error:
        raise HTTPException(status_code=404, detail="Unknown app id.") from error
    header = request.headers.get("last-event-id")
    # 헤더와 query parameter가 모두 있으면 더 최근 위치에서 시작한다. 숫자가 아닌 헤더는
    # 잘못된 재연결 정보이므로 무시하고 검증된 after 값을 사용한다.
    cursor = max(after, int(header)) if header and header.isdigit() else after

    async def generate():
        """DB를 짧은 간격으로 확인해 SSE 형식의 문자열을 차례로 내보낸다."""
        nonlocal cursor
        idle = 0
        while not await request.is_disconnected():
            events = repository.list_progress_events(
                app_id, after=cursor, limit=100, include_llm_timings=False
            )
            if events:
                idle = 0
                for event in events:
                    cursor = int(event["event_id"])
                    yield (
                        f"id: {cursor}\n"
                        "event: workspace\n"
                        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    )
            else:
                idle += 1
                if idle >= 15:
                    idle = 0
                    # 이벤트가 없어도 주기적으로 주석 행을 보내면 프록시가 유휴 연결을
                    # 끊는 일을 줄일 수 있다. SSE 클라이언트는 이 행을 이벤트로 표시하지 않는다.
                    yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        # Nginx 같은 프록시가 응답을 모아서 한꺼번에 보내면 실시간 화면이 늦어진다.
        # buffering을 끄고 브라우저 cache도 금지해 이벤트를 생기는 즉시 전달한다.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
