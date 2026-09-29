"""구조화 출력 LLM 호출 — 모든 설계 산출물이 공유하는 단 하나의 LLM 배관.

**왜 구조화 출력만 쓰나.** 예전에는 산출물마다 방식이 갈렸다. 클래스·ERD는 LLM에게
Pydantic 스키마를 강제해 구조화된 모델을 받고 그것을 결정론적으로 렌더했지만, 시퀀스·
API·배포는 LLM이 PlantUML/JSON 텍스트를 직접 쓰고 그것을 파싱했다. 후자는 문법 오류가
날 수 있으니 validate→repair 루프가 필요했고, 피드백이 렌더된 텍스트를 편집하므로
"모델과 그림이 어긋나는" 상태가 원천적으로 가능했다.

지금은 다섯 산출물 모두 이 함수 하나를 거친다. LLM은 **언제나 스키마에 맞는 JSON만**
내놓고, 그림/명세는 그 모델에서 결정론적으로 렌더된다. 그래서 수리 루프가 사라지고,
피드백은 항상 모델을 편집하며, 모델과 산출물이 어긋날 수 없다.
"""

from __future__ import annotations

import hashlib
import json
import queue
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from datetime import UTC, datetime
from functools import lru_cache, wraps
from time import perf_counter
from types import SimpleNamespace
from typing import Any, ParamSpec, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import settings
from app.llm_connection import LlmConnection, build_llm_connection
from app.llm_profiles import profile_for
from app.llm_schema import strict_json_schema
from app.metrics import langsmith as langsmith_metrics
from app.metrics.llm_stall_probe import start_stall_probe


class StructuredLlmError(RuntimeError):
    """어느 구조화 산출물 호출이 실패했는지 보존하는 경계 오류."""


class _SchemaValidationFailure(Exception):
    """Carry a local validation error and its parsed JSON object to the one repair."""

    def __init__(self, error: ValidationError, parsed_input: Any | None) -> None:
        super().__init__(str(error))
        self.error = error
        self.parsed_input = parsed_input


class _IncompleteStructuredStream(Exception):
    """The provider closed a structured stream without a completion signal."""


def _failure_category(error: BaseException) -> str:
    """Classify transport failures without mistaking sandbox/network causes for 429s."""

    text = str(error).casefold()
    name = type(error).__name__.casefold()
    if "429" in text or "ratelimit" in name or "rate limit" in text:
        return "rate_limit"
    if "timeout" in name or "timed out" in text:
        return "timeout"
    if "connection" in name or "connection" in text or "dns" in text:
        return "connection"
    if isinstance(error, _IncompleteStructuredStream):
        return "incomplete_stream"
    if isinstance(error, (ValidationError, _SchemaValidationFailure)):
        return "schema_validation"
    return "provider_or_runtime"


_P = ParamSpec("_P")
_R = TypeVar("_R")


class _TimingEventList(list[dict[str, Any]]):
    """Context copies share this collector, so writes must be explicitly synchronized."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.RLock()

    def append(self, item: dict[str, Any]) -> None:
        with self._lock:
            super().append(item)

    def extend(self, values) -> None:
        with self._lock:
            super().extend(values)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(super().__iter__())

    def __iter__(self):
        return iter(self.snapshot())

    def __len__(self) -> int:
        with self._lock:
            return super().__len__()

    def __getitem__(self, index):
        with self._lock:
            return super().__getitem__(index)


_TIMING_EVENTS: ContextVar[_TimingEventList | None] = ContextVar(
    "design_llm_timing_events", default=None
)


@contextmanager
def capture_llm_timings() -> Iterator[list[dict[str, Any]]]:
    """Collect only this invocation's events, including context-bound worker calls."""

    events = _TimingEventList()
    token = _TIMING_EVENTS.set(events)
    try:
        yield events
    finally:
        _TIMING_EVENTS.reset(token)


def bind_context(callable_obj: Callable[_P, _R]) -> Callable[_P, _R]:
    """Bind the current ContextVars to one executor submission.

    Call this once per submission. A copied context cannot be entered by two workers at
    the same time, while each copy may safely share the synchronized timing collector.
    """

    context = copy_context()

    @wraps(callable_obj)
    def bound(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        return context.run(callable_obj, *args, **kwargs)

    return bound


def record_llm_timing(
    operation: str,
    *,
    status: str,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Record a zero-duration logical/cache event in the existing timing collection."""

    events = _TIMING_EVENTS.get()
    if events is None:
        return
    observed_at = datetime.now(UTC).isoformat()
    event = {
        "operation": operation,
        "status": status,
        "errorType": None,
        "startedAt": observed_at,
        "finishedAt": observed_at,
        "elapsedSeconds": 0.0,
        "wallTimeoutSeconds": settings.llm_wall_timeout_seconds,
        "clientTimeoutSeconds": settings.llm_timeout_seconds,
        "observationScope": "logicalOnly",
        "ttftSeconds": None,
        "physicalRequest": False,
    }
    if status.startswith("cache_"):
        event["cacheStatus"] = status.removeprefix("cache_")
    event.update(dict(metadata or {}))
    events.append(event)


def run_with_wall_timeout(
    callable_obj,
    *,
    operation: str = "structured-output",
    observation: dict[str, Any] | None = None,
    connection: LlmConnection | None = None,
):
    """Trace one design-model call while retaining the existing timeout contract."""

    recorded_observation = observation if observation is not None else {}
    active_connection = connection or build_llm_connection()
    with langsmith_metrics.trace_scope(
        f"easydep.design.llm.{operation}",
        run_type="llm",
        metadata={
            "agent": "design",
            "operation": operation,
            "ls_provider": active_connection.provider,
            "ls_model_name": active_connection.model,
        },
    ) as trace:
        try:
            return _run_with_wall_timeout(
                callable_obj,
                operation=operation,
                observation=recorded_observation,
            )
        finally:
            if "inputTokens" in recorded_observation or "outputTokens" in recorded_observation:
                trace.set_usage(
                    input_tokens=int(recorded_observation.get("inputTokens") or 0),
                    output_tokens=int(recorded_observation.get("outputTokens") or 0),
                )


def _run_with_wall_timeout(
    callable_obj,
    *,
    operation: str = "structured-output",
    observation: dict[str, Any] | None = None,
):
    """벽시계 타임아웃. 클라이언트 타임아웃이 걸리지 않는 지연(연결 후 무응답 등)을 막는다."""
    recorded_observation = observation if observation is not None else {}
    timeout_seconds = settings.llm_wall_timeout_seconds
    started_at = datetime.now(UTC)
    started = perf_counter()
    status = "failed"
    error_type: str | None = None
    result_queue: queue.Queue = queue.Queue(maxsize=1)
    stall_probe = start_stall_probe(operation)
    if settings.easydep_experiment_session:
        print(json.dumps({
            "event": "llmOperationStarted",
            "operation": operation,
            "startedAt": started_at.isoformat(),
            "wallTimeoutSeconds": timeout_seconds,
        }, ensure_ascii=False), flush=True)

    def target():
        try:
            result_queue.put((True, callable_obj()))
        except Exception as error:  # noqa: BLE001 - 호출 스레드로 그대로 올린다
            result_queue.put((False, error))

    request_context = copy_context()
    thread = threading.Thread(target=lambda: request_context.run(target), daemon=True)
    thread.start()

    try:
        try:
            ok, result = result_queue.get(timeout=timeout_seconds)
        except queue.Empty as error:
            error_type = "WallTimeout"
            raise StructuredLlmError(
                f"{operation}: LLM request timed out after {timeout_seconds:g} seconds."
            ) from error
        if ok:
            status = "completed"
            return result
        error_type = type(result).__name__
        recorded_observation["failureCategory"] = _failure_category(result)
        raise StructuredLlmError(f"{operation}: {result}") from result
    finally:
        stall_probe.set()
        elapsed_seconds = round(perf_counter() - started, 6)
        if settings.easydep_experiment_session:
            print(json.dumps({
                "event": "llmOperationFinished",
                "operation": operation,
                "status": status,
                "errorType": error_type,
                "elapsedSeconds": elapsed_seconds,
            } | dict(observation or {}), ensure_ascii=False), flush=True)
        events = _TIMING_EVENTS.get()
        if events is not None:
            finished_at = datetime.now(UTC)
            events.append(
                {
                    "operation": operation,
                    "status": status,
                    "errorType": error_type,
                    "startedAt": started_at.isoformat(),
                    "finishedAt": finished_at.isoformat(),
                    "elapsedSeconds": elapsed_seconds,
                    "wallTimeoutSeconds": timeout_seconds,
                    "clientTimeoutSeconds": settings.llm_timeout_seconds,
                    "observationScope": "requestCompletionOnly",
                    "ttftSeconds": None,
                }
                | dict(observation or {})
            )


def _response_format(schema: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema.__name__,
            "strict": True,
            "schema": strict_json_schema(schema),
        },
    }


def _reasoning_effort(reasoning_effort: str | None, *, model: str | None = None) -> str | None:
    """단계별 reasoning 설정을 현재 NIM 모델이 받는 값으로 바꾼다."""

    configured = reasoning_effort or settings.design_reasoning_effort
    return profile_for(
        model or settings.model,
        fallback_temperature=settings.temperature,
        fallback_max_tokens=settings.llm_max_completion_tokens or 16384,
    ).resolve_reasoning(configured)


STRUCTURED_STREAM_WHITESPACE_LIMIT = 16_384
INCOMPLETE_STRUCTURED_STREAM_RETRIES = 2


def _json_whitespace_cutoff(
    content: str,
    *,
    in_string: bool,
    escaped: bool,
    whitespace_run: int,
) -> tuple[bool, bool, int, int | None]:
    """JSON 문자열 밖의 비정상적으로 긴 연속 공백이 시작된 지점을 찾는다.

    JSON 문자열 값에는 긴 공백이 합법적으로 들어갈 수 있으므로 따옴표와 escape 상태를
    chunk 사이에서도 이어서 본다. 반환된 마지막 값이 ``None``이면 chunk 전체를 보존하고,
    숫자이면 그 위치까지만 보존한 뒤 스트림을 닫는다.
    """

    for position, character in enumerate(content, start=1):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
            whitespace_run = 0
        elif character in " \t\r\n":
            whitespace_run += 1
            if whitespace_run >= STRUCTURED_STREAM_WHITESPACE_LIMIT:
                return in_string, escaped, whitespace_run, position
        else:
            whitespace_run = 0
    return in_string, escaped, whitespace_run, None


def stream_structured_response(
    client,
    messages: list[dict[str, str]],
    schema: type[BaseModel],
    observation: dict[str, Any],
    *,
    reasoning_effort: str | None = None,
    max_completion_tokens: int | None = None,
    connection: LlmConnection | None = None,
) -> BaseModel:
    """구조화 응답을 스트리밍으로 받아 진행 시간과 최종 스키마를 함께 검증한다."""
    started = perf_counter()
    previous_event: float | None = None
    first_event: float | None = None
    first_output: float | None = None
    first_content: float | None = None
    first_reasoning: float | None = None
    last_reasoning: float | None = None
    max_inter_event = 0.0
    event_count = 0
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    content_characters = 0
    reasoning_characters = 0
    finish_reasons: list[str] = []
    json_string_open = False
    json_escape_pending = False
    outside_whitespace_run = 0
    whitespace_limit_reached = False
    connection = connection or build_llm_connection()
    profile = profile_for(
        connection.model,
        fallback_temperature=settings.temperature,
        fallback_max_tokens=settings.llm_max_completion_tokens or 16384,
    )
    use_stream = not (
        connection.provider == "cloudflare"
        and settings.cloudflare_structured_transport != "stream"
    )
    use_response_format = not (
        connection.provider == "cloudflare"
        and settings.cloudflare_structured_transport != "stream"
    )
    request_messages = messages
    if not use_response_format:
        # Cloudflare documents JSON Mode as non-streaming and its GPT-OSS route
        # can reject a strict response_format outright. Keep the exact schema
        # contract by placing it in the prompt and validate the completed
        # response locally below.
        schema_text = json.dumps(strict_json_schema(schema), ensure_ascii=False)
        request_messages = [
            {
                "role": "system",
                "content": (
                    "Return exactly one JSON object and no Markdown. It must conform "
                    "to this JSON Schema:\n" + schema_text
                ),
            },
            *messages,
        ]
    request: dict[str, Any] = {
        "model": connection.model,
        "messages": request_messages,
        "temperature": profile.temperature,
        "seed": settings.seed,
        "stream": use_stream,
    }
    if use_response_format:
        request["response_format"] = _response_format(schema)
    if use_stream:
        # OpenAI 호환 stream의 마지막 chunk에서 provider가 보고한 사용량을 받는다.
        # 이 값은 로컬 추정치가 아닌 정확한 token/cost total이다.
        request["stream_options"] = {"include_usage": True}
    requested_completion_limit = (
        max_completion_tokens
        if max_completion_tokens is not None
        else settings.llm_max_completion_tokens
    )
    completion_limit = profile.completion_limit(requested_completion_limit)
    request["max_tokens"] = completion_limit
    if profile.top_p is not None:
        request["top_p"] = profile.top_p
    provider_reasoning_effort = _reasoning_effort(reasoning_effort, model=connection.model)
    if provider_reasoning_effort:
        request["reasoning_effort"] = provider_reasoning_effort
    if extra_body := profile.extra_body(connection.provider):
        request["extra_body"] = extra_body
    observation.update(
        schema=schema.__name__,
        provider=connection.provider,
        model=connection.model,
        temperature=profile.temperature,
        topP=profile.top_p,
        reasoningEffort=provider_reasoning_effort,
        reasoningBudget=profile.reported_reasoning_budget(connection.provider),
        maxCompletionTokens=completion_limit,
    )
    response_or_stream = client.chat.completions.create(**request)
    observation["transport"] = (
        "structuredStream" if use_stream else "structuredNonStream"
    )
    observation["responseEstablishedSeconds"] = round(perf_counter() - started, 6)
    if use_stream:
        stream = response_or_stream
    else:
        # Keep the parsing and telemetry path identical by adapting a completed
        # Chat Completion to one synthetic chunk. This is required for
        # Cloudflare JSON Mode, which does not support streaming.
        response = response_or_stream
        choices = []
        for response_choice in getattr(response, "choices", []) or []:
            message = getattr(response_choice, "message", None)
            choices.append(
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=getattr(message, "content", "") or "",
                        reasoning_content=(
                            getattr(message, "reasoning_content", "") or ""
                        ),
                    ),
                    finish_reason=getattr(response_choice, "finish_reason", None),
                )
            )
        stream = [SimpleNamespace(choices=choices, usage=getattr(response, "usage", None))]
    for chunk in stream:
        _observe_stream_usage(observation, getattr(chunk, "usage", None))
        now = perf_counter()
        event_at = datetime.now(UTC).isoformat()
        event_count += 1
        if first_event is None:
            first_event = now - started
            observation["firstEventAt"] = event_at
        if previous_event is not None:
            max_inter_event = max(max_inter_event, now - previous_event)
        previous_event = now
        for choice in chunk.choices:
            content = choice.delta.content or ""
            reasoning = str(getattr(choice.delta, "reasoning_content", "") or "")
            if (content or reasoning) and first_output is None:
                first_output = now - started
            if content and first_content is None:
                first_content = now - started
            if content:
                (
                    json_string_open,
                    json_escape_pending,
                    outside_whitespace_run,
                    whitespace_cutoff,
                ) = _json_whitespace_cutoff(
                    content,
                    in_string=json_string_open,
                    escaped=json_escape_pending,
                    whitespace_run=outside_whitespace_run,
                )
                if whitespace_cutoff is not None:
                    content = content[:whitespace_cutoff]
                    whitespace_limit_reached = True
                content_parts.append(content)
                content_characters += len(content)
            if reasoning:
                if first_reasoning is None:
                    first_reasoning = now - started
                last_reasoning = now - started
                reasoning_parts.append(reasoning)
            reasoning_characters += len(reasoning)
            if choice.finish_reason:
                finish_reasons.append(str(choice.finish_reason))
            if whitespace_limit_reached:
                break
        # Keep only aggregate progress.  This dict is shared with the wall-timeout
        # observer, so a timeout retains evidence from the last received chunk
        # without persisting prompts, reasoning, or response content.
        observation.update(
            firstEventSeconds=(
                round(first_event, 6) if first_event is not None else None
            ),
            lastEventAt=event_at,
            lastEventSeconds=round(now - started, 6),
            maxInterEventSeconds=round(max_inter_event, 6),
            eventCount=event_count,
            contentCharacters=content_characters,
            reasoningCharacters=reasoning_characters,
            finishReasonObserved=bool(finish_reasons),
            finishReasons=list(finish_reasons),
        )
        if whitespace_limit_reached:
            # 정상 JSON이 이미 닫혔다면 아래의 기존 검증이 그대로 수락한다. 아직 미완성이면
            # 같은 검증이 _SchemaValidationFailure를 만들어 기존 schema repair로 이어진다.
            observation.update(
                streamAbortReason="repetitiveJsonWhitespace",
                consecutiveWhitespaceCharacters=outside_whitespace_run,
            )
            close_stream = getattr(stream, "close", None)
            if callable(close_stream):
                close_stream()
            break
    content_text = "".join(content_parts)
    reasoning_text = "".join(reasoning_parts)
    observation.update(
        firstEventSeconds=round(first_event, 6) if first_event is not None else None,
        ttftSeconds=round(first_output, 6) if first_output is not None else None,
        firstContentSeconds=(
            round(first_content, 6) if first_content is not None else None
        ),
        firstReasoningSeconds=(
            round(first_reasoning, 6) if first_reasoning is not None else None
        ),
        lastReasoningSeconds=(
            round(last_reasoning, 6) if last_reasoning is not None else None
        ),
        reasoningLeadSeconds=(
            round(first_content - first_reasoning, 6)
            if first_content is not None and first_reasoning is not None
            else None
        ),
        maxInterEventSeconds=round(max_inter_event, 6),
        eventCount=event_count,
        contentCharacters=len(content_text),
        reasoningCharacters=reasoning_characters,
        finishReasonObserved=bool(finish_reasons),
        finishReasons=finish_reasons,
        responseSha256=hashlib.sha256(content_text.encode("utf-8")).hexdigest(),
    )
    if settings.llm_capture_response_content:
        # 별도 진단 저장소를 만들지 않고 기존 timing event에 원문을 붙인다. 이 event는
        # 실험 결과 JSON과 Workspace event 양쪽에서 그대로 확인할 수 있다.
        observation.update(
            responseContent=content_text,
            reasoningContent=reasoning_text,
        )
    if not finish_reasons and not whitespace_limit_reached:
        observation["streamIncompleteReason"] = "missingFinishReason"
        _record_failure_content(observation, content_text)
        raise _IncompleteStructuredStream(
            "Structured stream ended without a finish reason."
        )
    validation_text, trimmed_closers = _trim_redundant_json_closers(content_text)
    if trimmed_closers:
        observation.update(
            jsonTrailingDelimiterTrimmed=True,
            jsonTrailingDelimiterCount=trimmed_closers,
            normalizedResponseSha256=hashlib.sha256(
                validation_text.encode("utf-8")
            ).hexdigest(),
        )
    try:
        parsed_input = json.loads(validation_text)
    except json.JSONDecodeError:
        parsed_input = None
    try:
        return schema.model_validate_json(validation_text)
    except ValidationError as error:
        observation["schemaValidationErrors"] = [
            dict(item)
            for item in error.errors(
                include_url=False,
                include_input=False,
                include_context=False,
            )
        ]
        _record_failure_content(observation, content_text)
        raise _SchemaValidationFailure(error, parsed_input) from error
    except Exception:
        _record_failure_content(observation, content_text)
        raise


def _record_failure_content(observation: dict[str, Any], content_text: str) -> None:
    """Keep opt-in diagnostics identical for JSON and model validation failures."""
    # 실패 원문 전체와 reasoning은 보존하지 않는다. 실험에서 명시적으로 요청한
    # 제한 길이의 양 끝 표본과 전체 content 지문만 남겨 토큰 절단, 반복 출력,
    # 단순 문법 오류를 구분한다. 특정 schema나 사례에 의존하지 않는 공통 경계다.
    sample_limit = settings.llm_failure_response_sample_chars
    if settings.easydep_experiment_session and sample_limit > 0:
        bounded = min(sample_limit, 4096)
        observation.update(
            failureContentSha256=hashlib.sha256(content_text.encode("utf-8")).hexdigest(),
            failureContentPrefix=content_text[:bounded],
            failureContentSuffix=content_text[-bounded:],
            failureContentSampleCharacters=bounded,
            failureContentSampleTruncated=len(content_text) > bounded * 2,
        )


def _observe_stream_usage(observation: dict[str, Any], usage: Any) -> None:
    """Copy provider-reported usage from the terminal SSE chunk if available."""

    if usage is None:
        return
    if isinstance(usage, dict):
        read = usage.get
    else:
        def read(name: str, default: Any = None) -> Any:
            return getattr(usage, name, default)
    prompt_tokens = read("prompt_tokens", read("input_tokens", None))
    completion_tokens = read("completion_tokens", read("output_tokens", None))
    total_tokens = read("total_tokens", None)
    if prompt_tokens is not None:
        observation["inputTokens"] = int(prompt_tokens)
    if completion_tokens is not None:
        observation["outputTokens"] = int(completion_tokens)
    if total_tokens is not None:
        observation["totalTokens"] = int(total_tokens)


def _trim_redundant_json_closers(content: str) -> tuple[str, int]:
    """Remove only unmatched trailing ``}``/``]`` after one complete JSON value."""
    try:
        _, end = json.JSONDecoder().raw_decode(content.lstrip())
    except json.JSONDecodeError:
        return content, 0
    leading = len(content) - len(content.lstrip())
    absolute_end = leading + end
    suffix = content[absolute_end:]
    non_whitespace = "".join(character for character in suffix if not character.isspace())
    if not non_whitespace or any(character not in "}]" for character in non_whitespace):
        return content, 0
    return content[:absolute_end], len(non_whitespace)


def parse_structured(
    messages: list[dict[str, str]],
    schema: type[BaseModel],
    *,
    reasoning_effort: str | None = None,
    repair_reasoning_effort: str | None = None,
    max_completion_tokens: int | None = None,
    operation: str | None = None,
    metadata: dict[str, Any] | None = None,
    connection: LlmConnection | None = None,
) -> dict[str, Any]:
    """LLM에게 schema를 강제해 구조화 결과를 받고 dict로 돌려준다.

    temperature/seed를 고정하는 것은 같은 입력이 같은 모델을 내도록 하기 위해서다 —
    산출물이 재현되지 않으면 피드백이 무엇을 고쳤는지 알 수 없다.
    """
    connection = connection or build_llm_connection()
    parsed = parse_with_schema_repair(
        _structured_client(
            connection,
            float(settings.llm_timeout_seconds),
            int(settings.llm_max_retries),
        ),
        messages,
        schema,
        reasoning_effort=reasoning_effort,
        repair_reasoning_effort=repair_reasoning_effort,
        max_completion_tokens=max_completion_tokens,
        operation=operation,
        metadata=metadata,
        connection=connection,
    )
    return parsed.model_dump()


@lru_cache(maxsize=4)
def _structured_client(
    connection: LlmConnection,
    timeout_seconds: float,
    max_retries: int,
):
    """Reuse one OpenAI-compatible client per configured provider tuple."""

    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv()
    return OpenAI(
        base_url=connection.base_url,
        api_key=connection.api_key,
        default_headers=connection.default_headers(),
        timeout=timeout_seconds,
        max_retries=max_retries,
    )


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _request_digests(
    messages: list[dict[str, str]], schema: type[BaseModel]
) -> dict[str, str]:
    """Return content fingerprints without retaining prompt or input text."""

    user_content = [
        message.get("content", "")
        for message in messages
        if message.get("role") != "system"
    ]
    system_content = [
        message.get("content", "")
        for message in messages
        if message.get("role") == "system"
    ]
    return {
        "inputDigest": _stable_digest(user_content),
        "promptDigest": _stable_digest(system_content),
        "schemaDigest": _stable_digest(strict_json_schema(schema)),
    }


def parse_with_schema_repair(
    client,
    messages: list[dict[str, str]],
    schema: type[BaseModel],
    *,
    reasoning_effort: str | None = None,
    repair_reasoning_effort: str | None = None,
    max_completion_tokens: int | None = None,
    operation: str | None = None,
    metadata: dict[str, Any] | None = None,
    connection: LlmConnection | None = None,
) -> BaseModel:
    """Retry incomplete streams, then repair one locally invalid response.

    Transport retries repeat the original request. Schema repair inherits the original
    reasoning effort unless the caller explicitly supplies a different repair effort.
    """
    operation_name = operation or schema.__name__
    connection = connection or build_llm_connection()
    semantic_repair = operation_name.casefold().endswith("repair")
    logical_request_digest = _stable_digest(
        {
            "operation": operation_name,
            "messages": messages,
            "schema": strict_json_schema(schema),
        }
    )
    observation: dict[str, Any] = {
        "schemaRepairAttempt": 0,
        "taskKind": operation_name,
        "logicalRequest": operation_name,
        "logicalRequestDigest": logical_request_digest,
        "physicalRequest": True,
        "physicalRequestIndex": 1,
        "repairKind": "semantic" if semantic_repair else None,
        "handoff": "deterministic-validation" if semantic_repair else None,
        **dict(metadata or {}),
        **_request_digests(messages, schema),
    }
    try:
        return run_with_wall_timeout(
            lambda: stream_structured_response(
                client,
                messages,
                schema,
                observation,
                reasoning_effort=reasoning_effort,
                connection=connection,
                **(
                    {"max_completion_tokens": max_completion_tokens}
                    if max_completion_tokens is not None
                    else {}
                ),
            ),
            operation=operation_name,
            observation=observation,
            connection=connection,
        )
    except StructuredLlmError as first_error:
        error = first_error

    physical_request_index = 1
    for retry_attempt in range(1, INCOMPLETE_STRUCTURED_STREAM_RETRIES + 1):
        if not isinstance(error.__cause__, _IncompleteStructuredStream):
            break
        physical_request_index += 1
        retry_observation: dict[str, Any] = {
            "schemaRepairAttempt": 0,
            "taskKind": operation_name,
            "logicalRequest": operation_name,
            "logicalRequestDigest": logical_request_digest,
            "physicalRequest": True,
            "physicalRequestIndex": physical_request_index,
            "repairKind": "transport",
            "handoff": "incomplete-stream-retry",
            "streamRetryAttempt": retry_attempt,
            **dict(metadata or {}),
            **_request_digests(messages, schema),
        }
        try:
            return run_with_wall_timeout(
                lambda: stream_structured_response(
                    client,
                    messages,
                    schema,
                    retry_observation,
                    reasoning_effort=reasoning_effort,
                    connection=connection,
                    **(
                        {"max_completion_tokens": max_completion_tokens}
                        if max_completion_tokens is not None
                        else {}
                    ),
                ),
                operation=f"{operation_name}:stream-retry-{retry_attempt}",
                observation=retry_observation,
                connection=connection,
            )
        except StructuredLlmError as retry_error:
            error = retry_error

    cause = error.__cause__
    parsed_input: Any | None = None
    if isinstance(cause, _SchemaValidationFailure):
        validation_error = cause.error
        parsed_input = cause.parsed_input
    elif isinstance(cause, ValidationError):
        validation_error = cause
    else:
        raise error
    validation_errors = [
        dict(item)
        for item in validation_error.errors(
            include_url=False,
            include_input=False,
            # Pydantic keeps the original exception object in ``ctx.error``
            # for value errors. Repair prompts are JSON, so retaining that
            # object masks the validation failure with a serialization error.
            include_context=False,
        )
    ]

    repair_payload = schema_repair_payload(validation_errors, parsed_input)
    repair_messages = [
        *messages,
        {
            "role": "user",
            "content": (
                "The previous response failed local schema validation. Regenerate the "
                "entire object; do not return a patch and do not omit required values.\n\n"
                "[Schema repair context]\n"
                + json.dumps(repair_payload, ensure_ascii=False)
            ),
        },
    ]
    repair_observation: dict[str, Any] = {
        "schemaRepairAttempt": 1,
        "taskKind": operation_name,
        "logicalRequest": operation_name,
        "logicalRequestDigest": logical_request_digest,
        "physicalRequest": True,
        "physicalRequestIndex": physical_request_index + 1,
        "repairKind": "schema",
        "handoff": "schema-repair",
        **dict(metadata or {}),
        **_request_digests(repair_messages, schema),
    }
    return run_with_wall_timeout(
        lambda: stream_structured_response(
            client,
            repair_messages,
            schema,
            repair_observation,
            reasoning_effort=repair_reasoning_effort or reasoning_effort,
            connection=connection,
            **(
                {"max_completion_tokens": max_completion_tokens}
                if max_completion_tokens is not None
                else {}
            ),
        ),
        operation=f"{operation_name}:schema-repair",
        observation=repair_observation,
        connection=connection,
    )


SCHEMA_REPAIR_PREVIOUS_INPUT_MAX_CHARS = 16_000


def schema_repair_payload(
    validation_errors: list[dict[str, Any]], parsed_input: Any | None,
) -> dict[str, Any]:
    """모델 출력을 저장하지 않고 JSON-safe한 제한 크기 repair 문맥을 만든다."""
    payload: dict[str, Any] = {"validationErrors": validation_errors}
    if parsed_input is None:
        return payload
    encoded = json.dumps(parsed_input, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= SCHEMA_REPAIR_PREVIOUS_INPUT_MAX_CHARS:
        payload["previousParsedInput"] = parsed_input
        return payload
    sample = SCHEMA_REPAIR_PREVIOUS_INPUT_MAX_CHARS // 2
    payload["previousParsedInput"] = {
        "truncated": True,
        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "prefix": encoded[:sample],
        "suffix": encoded[-sample:],
    }
    return payload


def focus_note(targets: set[str] | None) -> str:
    """ "이 항목만 고쳐라"를 프롬프트에 얹는 문구. 대상이 없으면 빈 문자열.

    **이건 지시일 뿐이고 보장이 아니다.** 실제 보장은 코드가 한다 —
    `app/design/nodes/artifact.py`의 merge_model 이 비대상 항목에 대해서는 LLM 출력을
    아예 읽지 않는다. 이 문구는 대상이 잘 고쳐지도록 초점을 좁혀줄 뿐이다.
    """
    if not targets:
        return ""
    listed = ", ".join(sorted(targets))
    return (
        "\n\n[Scope]\n"
        f"Change ONLY these elements: {listed}.\n"
        "Return every other element exactly as given — same names, same fields, same "
        "order. Adding a new element is allowed when the change genuinely requires one."
    )


def revision_messages(
    system_prompt: str,
    context_label: str,
    context_text: str,
    model_label: str,
    current_model: dict[str, Any],
    feedback: str,
    targets: set[str] | None = None,
) -> list[dict[str, str]]:
    """피드백 수정 프롬프트의 공통 뼈대: 맥락 + 현재 모델 + 사용자 피드백 (+ 범위).

    다섯 산출물의 리바이저가 모두 같은 모양이므로 여기 한 번만 적는다.
    """
    import json

    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"[{context_label}]\n{context_text}\n\n"
                f"[{model_label}]\n"
                f"{json.dumps(current_model, ensure_ascii=False, indent=2)}\n\n"
                f"[User Feedback]\n{feedback}" + focus_note(targets)
            ),
        },
    ]
