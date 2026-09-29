"""EasyDep 백엔드 애플리케이션을 조립하고 시작·종료 순서를 관리한다.

이 파일은 요구사항 분석이나 설계 생성 규칙을 구현하지 않는다. FastAPI에 각 기능의
router를 연결하고, 서버가 요청을 받기 전에 데이터베이스와 background worker를 준비하며,
종료할 때 남은 worker를 정리하는 진입점이다.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.artifacts_api import router as artifacts_router
from app.db.session import init_db
from app.design.services.common.plantuml import plantuml_renderer
from app.implementation.application.jobs import worker as implementation_worker
from app.implementation.interfaces.http import router as implementation_router
from app.requirements.classifier import warmup_or_raise
from app.workspace.api import router as workspace_router
from app.workspace.service import workspace_service

BASE_DIR = Path(__file__).resolve().parent
# SvelteKit 빌드 결과가 있으면 FastAPI가 같은 포트에서 UI도 제공한다. 개발자가 프론트엔드를
# 아직 빌드하지 않은 경우에는 아래에서 503 안내 페이지를 대신 등록한다.
FRONTEND_BUILD_DIR = BASE_DIR / "frontend" / "build"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """요청 처리 전 준비 작업과 서버 종료 시 정리 작업을 한곳에서 실행한다.

    데이터베이스 표가 준비되기 전에 workspace worker가 저장소를 읽으면 시작과 동시에
    실패할 수 있다. 그래서 runtime warmup은 비동기로 시작하되, DB 초기화는 workspace
    복구보다 먼저 끝낸다. `yield` 뒤의 `finally`는 정상 종료와 예외 종료 모두에서 실행된다.
    """
    from dotenv import load_dotenv
    from openai import OpenAI

    _app.state.ready = False
    load_dotenv()
    # 시작 시점에 OpenAI SDK import가 가능한지 확인한다. 실제 LLM client 생성은 각 runtime이
    # 담당하므로 여기서는 환경을 바꾸거나 네트워크 요청을 보내지 않는다.
    _ = OpenAI
    # 설계 이미지를 요청할 때마다 Docker/JVM을 새로 띄우지 않도록 PlantUML PicoWeb을 서버와
    # 함께 시작한다. JAR가 없는 일부 단위 테스트 환경만 기존 단발 Docker 실행으로 물러난다.
    renderer_started = plantuml_renderer.start()
    print(
        "[startup] PlantUML persistent renderer: "
        f"{'ready' if renderer_started else 'JAR unavailable; compatibility fallback'}"
    )
    try:
        # Docker image와 dependency cache 준비는 별도 executor에서 수행한다. 준비가 오래 걸려도
        # health check와 workspace command 처리를 막지 않기 위해 startup thread에서 기다리지 않는다.
        if implementation_worker.start_warmup():
            print("[startup] 구현 런타임 워밍업 시작")

        # workspace_service.startup()은 저장된 command를 읽으므로 필요한 table을 먼저 만든다.
        init_db()
        interrupted = workspace_service.startup()

        # BERT classifier는 첫 요구사항 요청의 지연을 줄이기 위해 미리 읽는다. 모델을 사용할 수
        # 없는 환경에서는 warmup_or_raise()의 설정 정책에 따라 건너뛸 수 있다.
        loaded = warmup_or_raise()
        print(
            "[startup] BERT classifier preloaded: "
            f"{'yes' if loaded else 'skipped or unavailable'}; "
            f"interrupted workspace commands: {interrupted}"
        )
        _app.state.ready = True
        yield
    finally:
        # 새 command 접수를 중지한 뒤 구현 worker를 종료한다. 역순으로 정리해야 실행 중인
        # 구현 결과를 workspace가 이미 닫힌 저장소에 기록하려는 경쟁 상태를 줄일 수 있다.
        _app.state.ready = False
        workspace_service.shutdown()
        implementation_worker.shutdown()
        plantuml_renderer.stop()


app = FastAPI(title="EasyDep Agents", lifespan=lifespan)
# router 등록 순서는 URL 우선순위를 바꾸지 않지만, 파이프라인 순서대로 두면 새 개발자가
# 어떤 기능이 연결되어 있는지 빠르게 확인할 수 있다.
app.include_router(artifacts_router)
app.include_router(implementation_router)
app.include_router(workspace_router)


@app.get("/api/health")
def health(response: Response) -> dict[str, bool]:
    """startup을 마쳤고 shutdown을 시작하지 않은 API만 준비 상태로 보고한다."""
    ready = bool(getattr(app.state, "ready", False))
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ok": ready}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Docker와 Kubernetes가 사용할 수 있는 최소 health check를 반환한다."""
    return {"status": "ok"}


@app.get("/requirements", include_in_schema=False)
@app.get("/design", include_in_schema=False)
@app.get("/implementation", include_in_schema=False)
@app.get("/testing", include_in_schema=False)
def legacy_ui_redirect() -> RedirectResponse:
    """이전 화면 주소로 들어온 사용자를 통합 workspace로 이동시킨다."""
    return RedirectResponse("/workspace/")


if FRONTEND_BUILD_DIR.is_dir():
    # API router를 먼저 등록했으므로 `/api/...`는 정적 파일보다 앞서 처리된다. 나머지 경로는
    # SvelteKit의 SPA fallback을 포함한 build 디렉터리가 담당한다.
    app.mount(
        "/",
        StaticFiles(directory=FRONTEND_BUILD_DIR, html=True),
        name="workbench-ui",
    )
else:

    @app.get("/", include_in_schema=False)
    def frontend_not_built() -> HTMLResponse:
        """프론트엔드 빌드가 없을 때 원인과 해결 명령을 알리는 페이지를 반환한다."""
        return HTMLResponse(
            "<h1>EasyDep</h1><p>Run <code>npm run build</code> in "
            "<code>frontend/</code> to build the workbench.</p>",
            status_code=503,
        )
