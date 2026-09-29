"""요구사항 runtime 관측 계층 — 로깅·LLM 호출 계측·실행 단위 집계.

**이 모듈이 막으려는 것은 조용한 실패다.**

LLM 파이프라인의 부가 기능(의미 검증, 재생성, 워밍업)은 실패해도 본 작업을 멈추지
않는 게 맞다. 문제는 지금 그 "멈추지 않음"이 "아무 일도 없었음"과 값이 같다는 것이다.
예를 들어 의미 검증기가 예외로 죽으면 빈 결함 목록을 돌려주는데, 그건 "결함을 찾지
못했다"와 구별되지 않는다. NIM이 내려가 있으면 모든 산출물이 조용히 '깨끗함'으로
통과한다.

그래서 여기서는 두 가지를 나눠 기록한다:
  - **실패(failure)**: 하려던 일이 안 됐다.
  - **저하(degradation)**: 기능이 꺼진 채로 결과가 나왔다 — 결과를 믿는 정도가 다르다.

집계는 `run_scope()` 안에서만 쌓인다. 스코프 밖 호출도 로그는 남으므로, 계측을
붙이지 않은 경로가 있어도 정보가 사라지지는 않는다.

## 스레드

`run_scope`는 contextvars를 쓴다. `ThreadPoolExecutor`는 컨텍스트를 자동으로 넘기지
않으므로, 워커에서도 같은 실행에 집계하려면 `bind_context()`로 감싸 submit 한다.
집계 객체 자체는 락으로 보호되므로 여러 스레드가 동시에 더해도 된다.
"""

from __future__ import annotations

import contextvars
import functools
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.llm_connection import build_llm_connection
from app.metrics import langsmith as langsmith_metrics
from app.metrics.llm_stall_probe import start_stall_probe

LOGGER_NAME = "easydep.agent"

_configured = False
_configure_lock = threading.Lock()

ProgressSink = Callable[[str, dict[str, Any]], None]
_progress_sink: contextvars.ContextVar[ProgressSink | None] = contextvars.ContextVar(
    "easydep_progress_sink", default=None
)

#: LogRecord가 이미 쓰는 속성 이름. 두 곳에서 쓴다 —
#:  1) `extra=`로 이 중 하나를 넘기면 logging이 KeyError를 던진다. 그러면 **계측이
#:     관측 대상을 죽인다.** 실제로 그랬다: RunStats의 "name"이 LogRecord.name과 부딪혔다.
#:  2) 포매터가 "우리가 넘긴 필드"만 골라내려면 기본 속성을 빼야 한다.
_RESERVED_LOG_FIELDS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
}


def _log_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """`extra=`로 안전하게 넘길 수 있는 dict로 바꾼다.

    예약된 이름은 `field_` 를 붙여 피한다. 조용히 버리지 않는 이유는, 계측 필드가
    소리 없이 사라지는 것이 이 모듈이 막으려는 바로 그 실패이기 때문이다.
    """
    return {(f"field_{k}" if k in _RESERVED_LOG_FIELDS else k): v for k, v in fields.items()}


def _progress(event: str, **fields: Any) -> None:
    sink = _progress_sink.get()
    if sink is not None:
        try:
            sink(event, fields)
        except Exception:  # noqa: BLE001 - progress reporting must not fail the analysis
            logging.getLogger(LOGGER_NAME).exception("progress sink failed")
    if settings.easydep_experiment_session:
        print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def emit_progress(event: str, **fields: Any) -> None:
    """민감하지 않은 실행 진행 상황을 현재 observer에 전달한다."""
    _progress(event, **fields)


@contextmanager
def progress_scope(sink: ProgressSink) -> Iterator[None]:
    """현재 실행 context에 진행 상황 observer 하나를 결합한다."""
    token = _progress_sink.set(sink)
    try:
        yield
    finally:
        _progress_sink.reset(token)


class _KeyValueFormatter(logging.Formatter):
    """메시지 뒤에 구조화 필드를 `key=value`로 붙이는 포매터.

    JSON 로거를 붙이는 편이 기계 판독에는 낫지만, 지금 이 로그를 읽는 것은 사람이고
    의존성을 늘리지 않는 편이 낫다. 필드를 `extra=`로 넘기는 호출 규약만 지키면
    나중에 포매터만 갈아끼워 JSON으로 바꿀 수 있다.
    """

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {k: v for k, v in vars(record).items() if k not in _RESERVED_LOG_FIELDS}
        if not extras:
            return base
        rendered = " ".join(f"{k}={v!r}" for k, v in sorted(extras.items()))
        return f"{base} | {rendered}"


def configure_logging(level: str | int | None = None) -> None:
    """이 패키지의 로거를 1회 설정한다(여러 번 불러도 핸들러가 겹치지 않는다).

    서버·CLI·배치 러너가 각자 진입점에서 부르면 되고, 부르지 않아도 라이브러리
    코드는 그냥 조용해질 뿐 죽지 않는다(핸들러 없는 로거의 기본 동작).
    """
    global _configured
    with _configure_lock:
        if _configured:
            return
        resolved = level if level is not None else settings.easydep_log_level
        handler = logging.StreamHandler()
        handler.setFormatter(_KeyValueFormatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
        logger = logging.getLogger(LOGGER_NAME)
        logger.setLevel(resolved)
        logger.addHandler(handler)
        # 루트로 올리지 않는다 — 이 저장소를 라이브러리로 쓰는 쪽의 로깅을 오염시키지 않기 위해.
        logger.propagate = False
        _configured = True


def get_logger(name: str) -> logging.Logger:
    """`easydep.agent.<name>` 로거를 돌려준다."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


_log = get_logger("telemetry")


@dataclass
class Degradation:
    """기능이 꺼진 채 결과가 나온 자리."""

    component: str
    reason: str
    #: 어느 산출물이 영향을 받았는지(유스케이스 id 등). 모르면 None.
    subject: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """저하 기록을 API·artifact에 저장할 JSON shape로 바꾼다."""

        return {"component": self.component, "reason": self.reason, "subject": self.subject}


@dataclass
class RunStats:
    """한 번의 분석 실행에서 일어난 일의 합계.

    여러 스레드가 동시에 더하므로 모든 변경은 `_lock` 안에서 한다.
    """

    name: str = "run"
    llm_calls: int = 0
    llm_failures: int = 0
    #: 네이티브 구조화 출력이 실패해 JSON 모드로 되돌아간 횟수.
    structured_fallbacks: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_seconds: float = 0.0
    wall_seconds: float = 0.0
    degradations: list[Degradation] = field(default_factory=list)
    #: 이 실행에서 응답한 백엔드 구성의 지문(OpenAI 호환 `system_fingerprint`).
    #: seed를 고정해도 지문이 바뀌면 결과가 달라질 수 있다 — **재현성 주장의 근거는
    #: seed가 아니라 이 값이다.** 한 실행에서 둘 이상 보이면 그 실행은 이미 섞여 있다.
    model_fingerprints: set[str] = field(default_factory=set)
    llm_timing_events: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def as_dict(self) -> dict[str, Any]:
        """API 응답·아티팩트에 실을 수 있는 평범한 dict."""
        with self._lock:
            return {
                "name": self.name,
                "llm_calls": self.llm_calls,
                "llm_failures": self.llm_failures,
                "structured_fallbacks": self.structured_fallbacks,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "llm_seconds": round(self.llm_seconds, 3),
                "wall_seconds": round(self.wall_seconds, 3),
                "degradations": [d.as_dict() for d in self.degradations],
                "model_fingerprints": sorted(self.model_fingerprints),
                "llm_timing_events": list(self.llm_timing_events),
            }


_current: contextvars.ContextVar[RunStats | None] = contextvars.ContextVar(
    "easydep_run_stats", default=None
)


def current_run() -> RunStats | None:
    """지금 실행 스코프의 집계 객체(없으면 None)."""
    return _current.get()


@contextmanager
def run_scope(name: str = "run") -> Iterator[RunStats]:
    """집계 스코프를 연다. 스코프가 끝나면 요약을 한 줄 남긴다.

    중첩해도 되지만 안쪽 스코프는 독립된 집계를 갖는다 — 배치 러너가 데이터셋마다
    스코프를 여는 것처럼, "무엇 하나에 대한 합계인가"가 분명한 편이 낫다.
    """
    with langsmith_metrics.trace_scope(
        f"easydep.requirements.{name}",
        metadata={"agent": "requirements", "scope": name},
    ):
        stats = RunStats(name=name)
        token = _current.set(stats)
        started = time.perf_counter()
        try:
            yield stats
        finally:
            # 한 실행 안에서 백엔드 구성이 바뀌면 seed를 고정했어도 앞뒤 산출물의 표본
            # 조건이 다르다. 이건 오류가 아니라 "이 실행의 재현성 주장이 약하다"는 뜻이라
            # 저하로 남긴다 — 스코프를 닫기 전에 해야 아래 요약에 포함된다.
            if len(stats.model_fingerprints) > 1:
                record_degradation(
                    "llm.fingerprint",
                    "한 실행에서 백엔드 구성이 바뀌었다(seed를 고정해도 결과가 갈릴 수 있다)",
                    subject=",".join(sorted(stats.model_fingerprints)),
                )
            stats.wall_seconds = time.perf_counter() - started
            _current.reset(token)
            summary = stats.as_dict()
            _log.info("run finished", extra=_log_fields(summary))


def bind_context(fn: Callable[..., Any]) -> Callable[..., Any]:
    """현재 contextvars 컨텍스트에 묶은 호출자를 만든다(스레드 풀 submit용).

    `ThreadPoolExecutor`는 컨텍스트를 복사해 주지 않아서, 워커 스레드에서
    `current_run()`이 None이 된다. submit 할 때마다 이 함수로 감싸면 워커가 같은
    실행에 집계한다. submit 마다 새로 감싸야 한다 — Context 하나는 한 번만 실행할
    수 있기 때문이다.
    """
    ctx = contextvars.copy_context()

    @functools.wraps(fn)
    def runner(*args: Any, **kwargs: Any) -> Any:
        return ctx.run(fn, *args, **kwargs)

    return runner


def record_degradation(component: str, reason: str, subject: str | None = None) -> None:
    """기능이 꺼진 채 진행했음을 남긴다.

    로그는 항상 남기고, 실행 스코프가 열려 있으면 집계에도 더한다. 부르는 쪽은
    이걸 부른 뒤 "없음"이 아니라 "확인하지 못함"에 해당하는 값을 돌려줘야 한다.
    """
    entry = Degradation(component=component, reason=reason, subject=subject)
    stats = current_run()
    if stats is not None:
        with stats._lock:
            stats.degradations.append(entry)
    _log.warning("degraded", extra=_log_fields(entry.as_dict()))


class LlmCall:
    """`record_llm_call` 이 넘겨주는 호출 1건의 기록지."""

    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.fallback_reason: str | None = None
        self.fingerprints: set[str] = set()

    def mark_fallback(self, reason: str) -> None:
        """네이티브 구조화 출력이 안 돼 JSON 모드로 되돌아갔음을 기록한다."""
        self.fallback_reason = reason

    def observe_metadata(self, metadata: Any) -> None:
        """응답 메타데이터에서 백엔드 지문(`system_fingerprint`)을 읽는다.

        seed를 고정해도 이 값이 바뀌면 결과가 달라질 수 있으므로, 재현성을 주장하려면
        seed가 아니라 이 값을 봐야 한다. 안 주는 게이트웨이도 있어 없으면 넘어간다.
        """
        if not isinstance(metadata, dict):
            return
        fingerprint = metadata.get("system_fingerprint")
        if fingerprint:
            self.fingerprints.add(str(fingerprint))

    def observe_usage(self, usage: Any) -> None:
        """LangChain 메시지의 `usage_metadata`(또는 같은 모양의 dict)에서 토큰을 더한다.

        더하는 이유는 논리적 호출 1건이 실제 요청 2건일 수 있기 때문이다 — 네이티브
        구조화 출력이 실패해 JSON 모드로 다시 물으면 토큰은 양쪽 다 나간다.

        모델·게이트웨이에 따라 사용량을 안 주는 경우가 있어 없으면 조용히 넘어간다 —
        여기서 예외를 내면 계측이 본 작업을 죽이게 된다.
        """
        if not isinstance(usage, dict):
            return
        self.prompt_tokens += int(usage.get("input_tokens") or 0)
        self.completion_tokens += int(usage.get("output_tokens") or 0)


@contextmanager
def record_llm_call(operation: str) -> Iterator[LlmCall]:
    """LLM 호출 1건을 계측한다(지연·토큰·폴백·예외).

    예외는 집계에 남기고 **그대로 다시 올린다** — 삼키는 것은 부르는 쪽의 결정이지
    계측의 결정이 아니다.
    """
    connection = build_llm_connection()
    with langsmith_metrics.trace_scope(
        f"easydep.requirements.llm.{operation}",
        run_type="llm",
        metadata={
            "agent": "requirements",
            "operation": operation,
            "ls_provider": connection.provider,
            "ls_model_name": connection.model,
        },
    ) as trace:
        call = LlmCall(operation)
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        failed: BaseException | None = None
        stall_probe = start_stall_probe(operation)
        _progress(
            "llmOperationStarted",
            operation=operation,
            startedAt=started_at.isoformat(),
        )
        try:
            yield call
        except BaseException as exc:
            failed = exc
            raise
        finally:
            stall_probe.set()
            elapsed = time.perf_counter() - started
            finished_at = datetime.now(UTC)
            _progress(
                "llmOperationFinished",
                operation=operation,
                status="failed" if failed is not None else "completed",
                errorType=type(failed).__name__ if failed is not None else None,
                finishedAt=finished_at.isoformat(),
                elapsedSeconds=round(elapsed, 6),
                promptTokens=call.prompt_tokens,
                completionTokens=call.completion_tokens,
                structuredFallback=call.fallback_reason is not None,
            )
            stats = current_run()
            if stats is not None:
                with stats._lock:
                    stats.llm_calls += 1
                    stats.llm_seconds += elapsed
                    stats.prompt_tokens += call.prompt_tokens
                    stats.completion_tokens += call.completion_tokens
                    if failed is not None:
                        stats.llm_failures += 1
                    if call.fallback_reason is not None:
                        stats.structured_fallbacks += 1
                    stats.model_fingerprints |= call.fingerprints
                    stats.llm_timing_events.append(
                        {
                            "operation": operation,
                            "startedAt": started_at.isoformat(),
                            "finishedAt": finished_at.isoformat(),
                            "elapsedSeconds": round(elapsed, 6),
                            "status": "failed" if failed is not None else "completed",
                            "errorType": type(failed).__name__ if failed is not None else None,
                            "structuredFallback": call.fallback_reason is not None,
                        }
                    )
            trace.set_usage(
                input_tokens=call.prompt_tokens,
                output_tokens=call.completion_tokens,
            )
            record = {
                "operation": operation,
                "seconds": round(elapsed, 3),
                "prompt_tokens": call.prompt_tokens,
                "completion_tokens": call.completion_tokens,
            }
            if failed is not None:
                _log.warning(
                    "llm call failed", extra=_log_fields({**record, "error": repr(failed)})
                )
            elif call.fallback_reason is not None:
                _log.warning(
                    "llm structured output fell back to json mode",
                    extra=_log_fields({**record, "reason": call.fallback_reason}),
                )
            else:
                _log.debug("llm call", extra=_log_fields(record))
