"""장기 LLM 호출과 독립된 endpoint 상태 probe를 예약한다."""

from __future__ import annotations

import json
import os
import threading
from time import perf_counter

from app.config import settings
from app.llm_connection import build_llm_connection
from app.llm_profiles import profile_for


def _emit(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def _probe(operation: str, timeout_seconds: float) -> None:
    from openai import OpenAI

    from app.config import settings

    started = perf_counter()
    first_event: float | None = None
    status = "completed"
    error_type: str | None = None
    status_code: int | None = None
    _emit("llmStallProbeStarted", stalledOperation=operation)
    try:
        connection = build_llm_connection()
        client = OpenAI(
            base_url=connection.base_url,
            api_key=connection.api_key,
            default_headers=connection.default_headers(),
            max_retries=0,
            timeout=timeout_seconds,
        )
        profile = profile_for(
            connection.model,
            fallback_temperature=settings.temperature,
            fallback_max_tokens=32,
        )
        stream = client.chat.completions.create(
            model=connection.model,
            messages=[{"role": "user", "content": "Reply with exactly: pong"}],
            # 진단 호출도 실제 제품과 같은 sampling 하한을 지킨다. 0을 쓰면 본 호출과
            # 다른 조건을 측정하게 되어 endpoint 응답성 비교가 왜곡될 수 있다.
            temperature=profile.temperature,
            seed=42,
            stream=True,
            # Probe는 첫 event만 확인한다. 본 작업의 큰 reasoning budget까지 복제하면
            # 진단 호출 자체가 느려지므로 작은 출력 상한 외의 추론 설정은 보내지 않는다.
            max_tokens=32,
        )
        for _chunk in stream:
            if first_event is None:
                first_event = perf_counter() - started
    except BaseException as error:  # 관찰 실패를 주 호출로 전파하지 않는다.
        status = "failed"
        error_type = type(error).__name__
        status_code = getattr(error, "status_code", None)
    elapsed = perf_counter() - started
    _emit(
        "llmStallProbeFinished",
        stalledOperation=operation,
        status=status,
        errorType=error_type,
        statusCode=status_code,
        rateLimited=status_code == 429 or error_type == "RateLimitError",
        endpointResponsive=status == "completed",
        firstEventSeconds=round(first_event, 6) if first_event is not None else None,
        elapsedSeconds=round(elapsed, 6),
    )


def start_stall_probe(operation: str) -> threading.Event:
    """설정된 지연을 넘기면 독립적인 짧은 probe를 한 번 실행한다."""
    completed = threading.Event()
    threshold_override = os.getenv("EASYDEP_LLM_STALL_PROBE_AFTER_SECONDS")
    threshold_value: str | float | None = (
        threshold_override
        if threshold_override is not None
        else settings.easydep_llm_stall_probe_after_seconds
    )
    if not threshold_value:
        return completed

    threshold = float(threshold_value)
    raw_timeout = os.getenv("EASYDEP_LLM_STALL_PROBE_TIMEOUT_SECONDS")
    timeout_seconds = (
        float(raw_timeout)
        if raw_timeout is not None
        else settings.easydep_llm_stall_probe_timeout_seconds
    )

    def watch() -> None:
        if not completed.wait(threshold):
            _probe(operation, timeout_seconds)

    threading.Thread(target=watch, daemon=True, name="easydep-llm-stall-probe").start()
    return completed
