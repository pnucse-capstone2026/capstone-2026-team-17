"""요구사항 runtime의 LLM 접근과 구조화 출력 adapter.

선택한 OpenAI 호환 엔드포인트를 langchain-openai의 ChatOpenAI로 감싼다.

구조화 출력(structured output) 처리:
  - 1차: `ChatOpenAI.with_structured_output(schema, method="json_schema")`.
    이는 OpenAI 네이티브 Structured Outputs(`response_format={"type":"json_schema",
    strict:true}`)를 호출하는 경로로, raw SDK의 `client.chat.completions.parse(...)`
    와 동일한 메커니즘을 langchain이 감싼 것이다.
  - 폴백: 일부 GPT-OSS 배포는 간헐적으로 빈 `parsed`(content='')를 반환한다.
    이 경우 스키마(JSON Schema)를 프롬프트로 주고 원문 JSON을 직접 파싱한다.

`include_raw=True`로 부르는 이유는 둘이다. 하나는 원본 메시지에 실린 토큰 사용량과
백엔드 지문을 읽기 위해서고(그게 없으면 이 파이프라인의 비용도 재현성도 잴 방법이
없다), 다른 하나는 파싱 실패가 예외가 아니라 `parsing_error` 값으로 돌아와 폴백
판단이 명시적이 되기 때문이다. 호출 1건은 telemetry가 지연·토큰·지문·폴백 여부와
함께 기록한다.

## 재현성 — 여기서 얻을 수 없는 것

낮은 `temperature` + `seed` 고정은 같은 표본을 **요청**하는 것이고, **이 모델에서는 보장이
되지 않는다.** 이유가 우연이 아니라 구조적이다:

  - GPT-OSS 계열은 MoE다. 어느 전문가로 라우팅되는지가 **함께 배치된 다른 요청들에
    영향을 받는다.** 우리가 보내는 입력이 같아도 서버의 배치 구성은 매번 다르다.
  - 배치가 달라지면 부동소수 리덕션 순서도 달라져, 같은 가중치·같은 입력에서도 로짓이
    미세하게 갈린다. 그 차이가 argmax를 뒤집는 토큰이 하나만 있어도 출력이 갈라진다.

그래서 seed는 분산을 **줄이는** 장치이지 없애는 장치가 아니다. `system_fingerprint`를
telemetry에 모으는 이유도 "지문이 같으면 재현된다"가 아니라 그 반대다 — **지문이 다르면
출력 차이를 코드 변경 탓으로 돌릴 수 없다**(비교의 필요조건이지 충분조건이 아니다).

실무적 귀결: LLM 출력에 대한 주장은 **한 번 돌려서 하지 않는다.** 결정론이 필요한 판단은
결정론 층(`knowledge/detectors.py`)에 두고, LLM 판정에 대한 주장은 표본을 반복해
비율로 낸다(`evaluation/semantic.py`).
"""

from __future__ import annotations

import json
from typing import Any, Literal, TypeVar

from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, SecretStr, ValidationError

from app.config import settings as llm_settings
from app.llm_connection import build_llm_connection
from app.llm_profiles import profile_for
from app.llm_schema import json_schema, strict_json_schema
from app.requirements.config import settings
from app.requirements.runtime import telemetry

T = TypeVar("T", bound=BaseModel)

_log = telemetry.get_logger("llm")

# with_structured_output에 쓸 method. "json_schema"=네이티브 parse 경로(권장),
# "function_calling"=tool 호출 경로. NIM 모델 특성에 따라 교체 가능.
_STRUCTURED_METHOD: Literal["json_schema"] = "json_schema"
_NON_STRICT_SCHEMA_IDENTITIES = frozenset(
    {"app.requirements.schemas.DeploymentNeedsResult"}
)


# 프로세스 전역 클라이언트 캐시 — 연결·세션 재사용 목적이다.
#
# ⚠ 2026-07-26 정정: 예전 주석은 "첫 호출이 모델 콜드 스타트로 수 분"이라고 적고 있었다.
# 사실이 아니다 — 이 무료 엔드포인트는 첫 호출도 1~2초다. 그 잘못된 전제 위에 세워진 것이
# 아래 `warmup_llm()`(호출부가 없다)과 `timeout=600`이다.
_llm: ChatOpenAI | None = None


def build_llm(*, seed_override: int | None = None) -> ChatOpenAI:
    """선택한 OpenAI 호환 채팅 모델을 반환한다(프로세스당 1회 생성)."""
    global _llm
    if _llm is None or seed_override is not None:
        connection = build_llm_connection()
        profile = profile_for(
            connection.model,
            fallback_temperature=settings.temperature,
            fallback_max_tokens=settings.requirements_max_completion_tokens,
        )
        options: dict[str, Any] = {
            "model": connection.model,
            "base_url": connection.base_url,
            "api_key": SecretStr(connection.api_key),
            "default_headers": connection.default_headers(),
            "temperature": profile.temperature,
            # 같은 입력에 같은 표본을 **요청**한다. 보장은 아니다 — 서버가 seed를
            # 무시할 수도 있고, 백엔드 구성이 바뀌면(system_fingerprint) 같은 seed라도
            # 결과가 달라진다. 그래서 지문을 telemetry에 남겨 사후에 확인할 수 있게 한다.
            # None이면 파라미터 자체를 안 보낸다.
            "seed": settings.seed if seed_override is None else seed_override,
            "max_completion_tokens": profile.completion_limit(
                settings.requirements_max_completion_tokens
            ),
            # 진짜 멈춘 호출이 무한 대기하지 않도록 두는 상한.
            #
            # **600에서 90으로 내렸다(2026-07-27). 근거는 실측 분포다** — 위 주석이
            # "줄이려면 배치 실행의 실측 분포를 보고 정하는 게 맞다"고 미뤄 둔 그 값이다.
            #
            # 프로브 캠페인에서 요청 지연이 두 갈래(bimodal)로 갈렸다: 대부분 1~15초인데
            # 일부가 **9분 22초·9분 24초·9분 29초**로 멈춘다(세 번 다 같은 값대). 그동안
            # 상한이 600초라 멈춘 요청 하나가 재시도도 못 하고 10분을 그대로 먹었다 —
            # 45분이면 끝날 측정이 14시간짜리가 된 원인이 이것이다.
            #
            # 90초는 정상 호출(1~15초)의 6배 위, 멈춘 갈래(560초+)의 한참 아래다. 두 갈래
            # 사이가 넓어서 값을 고르기 쉬웠다. 멈춘 요청은 이제 90초에 끊고 재시도한다.
            #
            # ⚠ 이걸 내리기 전에 **실패를 세는 쪽을 먼저 고쳐야 했다.** 측정 경로가 실패를
            # "위반 없음" 한 표로 세고 있었기 때문에, 상한을 내릴수록 규칙이 깨끗해 보이는
            # 상태였다(`evaluation/semantic.py`의 `ask`). 순서를 바꾸면 이 변경이 곧
            # 측정 편향이 된다.
            "timeout": 90,
            # 후보 모델의 endpoint 오류와 모델 출력 실패를 섞지 않도록 SDK가 같은 요청을
            # 몰래 반복하지 않는다. 재개 여부는 호출 결과를 기록한 상위 실행기가 정한다.
            "max_retries": llm_settings.llm_max_retries,
        }
        if profile.top_p is not None:
            options["top_p"] = profile.top_p
        if reasoning_effort := profile.resolve_reasoning(
            settings.requirements_reasoning_effort
        ):
            options["reasoning_effort"] = reasoning_effort
        if extra_body := profile.extra_body(connection.provider):
            options["extra_body"] = extra_body
        instance = ChatOpenAI(**options)
        if seed_override is not None:
            return instance
        _llm = instance
    return _llm


def reset_llm() -> None:
    """캐시된 클라이언트를 버린다.

    `build_llm()`이 프로세스당 1회만 만들기 때문에, settings의 모델·온도·seed를
    바꾼 뒤에는 이걸 불러야 반영된다. 안 부르면 설정은 바뀌었는데 호출은 옛 값으로
    나가고, 그건 로그만 봐서는 알 수 없는 종류의 어긋남이다.
    """
    global _llm
    _llm = None


def warmup_llm() -> float:
    """1회 더미 호출로 연결을 미리 만든다. 소요 초를 반환.

    ⚠ **호출부가 없다.** 원래 목적("콜드 스타트를 미리 지불한다")이 잘못된 전제였고
    (위 캐시 주석), 첫 호출이 1~2초인 엔드포인트에서는 벌어 주는 것이 거의 없다.
    지우는 게 맞을 수 있는데, 배치 실행에서 첫 호출 지연을 재는 용도로는 아직 쓸모가 있다.
    """
    import time

    from langchain_core.messages import HumanMessage

    t = time.time()
    try:
        build_llm().invoke([HumanMessage(content="ping")])
    except Exception as exc:  # noqa: BLE001 - 워밍업 실패는 치명적이지 않음
        # 워밍업이 실패해도 진행은 하지만, 그러면 첫 실제 호출이 콜드 스타트를 물게
        # 되므로 뒤따르는 지연은 모델이 느린 게 아니라 이것 때문이다.
        telemetry.record_degradation("llm.warmup", f"{type(exc).__name__}: {exc}")
    return time.time() - t


def message_text(content: str | list) -> str:
    """AIMessage content를 평문 문자열로 정규화한다.

    Args:
        content: 문자열 또는 LangChain message part 목록이다.

    Returns:
        part 순서를 유지해 결합한 평문 문자열이다.

    Notes:
        text가 없는 dict part는 기존처럼 빈 문자열로 처리한다.
    """
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            parts.append(str(part.get("text", "")))
    return "".join(parts)


def extract_json_object(text: str) -> str:
    """모델 출력에서 첫 JSON object 구간을 추출한다.

    Args:
        text: 코드 fence나 설명이 포함될 수 있는 모델 원문이다.

    Returns:
        첫 ``{``부터 마지막 ``}``까지의 기존 문자열 slice다.

    Notes:
        JSON object가 없으면 기존 ``ValueError`` 메시지를 유지한다.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"응답에서 JSON을 찾지 못함: {text[:200]!r}")
    return text[start : end + 1]


def invoke_native_structured(
    llm: ChatOpenAI,
    schema: type[T],
    messages: list,
    call: telemetry.LlmCall,
    *,
    strict: bool = True,
) -> T | None:
    """네이티브 structured output을 한 번 호출한다.

    Args:
        llm: structured output을 지원하는 ChatOpenAI 호환 client다.
        schema: 검증할 Pydantic response model이다.
        messages: 기존 LangChain message 목록이다.
        call: 논리 호출 하나의 telemetry 기록지다.
        strict: 공급자에게 닫힌 strict schema를 요구할지 여부다.

    Returns:
        파싱된 schema instance 또는 JSON fallback을 지시하는 ``None``이다.

    Notes:
        예외와 parsing error는 삼키되 fallback 사유와 raw usage·fingerprint를 기록한다.
    """
    try:
        normalized_schema = strict_json_schema(schema) if strict else json_schema(schema)
        structured = llm.with_structured_output(
            normalized_schema,
            method=_STRUCTURED_METHOD,
            include_raw=True,
            strict=strict,
        )
        result = structured.invoke(messages)
    except Exception as exc:  # noqa: BLE001 - 폴백으로 흡수, 사유는 기록한다
        call.mark_fallback(f"{type(exc).__name__}: {exc}")
        return None

    # include_raw=True 면 {"raw", "parsed", "parsing_error"} 를 돌려준다. 구버전
    # langchain이 파싱된 모델을 그대로 주면 그대로 쓴다.
    if not isinstance(result, dict):
        try:
            return schema.model_validate(result)
        except ValidationError as exc:
            call.mark_fallback(f"{type(exc).__name__}: {exc}")
            return None

    raw = result.get("raw")
    call.observe_usage(getattr(raw, "usage_metadata", None))
    call.observe_metadata(getattr(raw, "response_metadata", None))
    parsed = result.get("parsed")
    if parsed is not None:
        try:
            return schema.model_validate(parsed)
        except ValidationError as exc:
            call.mark_fallback(f"{type(exc).__name__}: {exc}")
            return None

    error = result.get("parsing_error")
    call.mark_fallback(f"parsed 없음: {error!r}" if error else "parsed 없음(빈 응답)")
    return None


def invoke_json_mode(
    llm: ChatOpenAI,
    schema: type[T],
    messages: list,
    call: telemetry.LlmCall,
    *,
    strict: bool = True,
) -> T:
    """JSON Schema prompt로 원문 응답을 직접 검증하는 fallback을 호출한다.

    Args:
        llm: ChatOpenAI 호환 client다.
        schema: 검증할 Pydantic response model이다.
        messages: 네이티브 호출과 동일한 message 목록이다.
        call: 같은 논리 호출의 telemetry 기록지다.
        strict: fallback prompt에도 strict schema를 제시할지 여부다.

    Returns:
        JSON 원문을 검증한 schema instance다.

    Notes:
        네이티브 요청과 fallback 요청의 token usage를 같은 logical call에 합산한다.
    """
    normalized_schema = strict_json_schema(schema) if strict else json_schema(schema)
    schema_json = json.dumps(normalized_schema)
    instr = SystemMessage(
        content=(
            "Respond with ONLY a single JSON object that conforms to this "
            f"JSON Schema. No prose, no markdown fences.\n{schema_json}"
        )
    )
    raw = llm.invoke(list(messages) + [instr])
    call.observe_usage(getattr(raw, "usage_metadata", None))
    call.observe_metadata(getattr(raw, "response_metadata", None))
    return schema.model_validate_json(extract_json_object(message_text(raw.content)))


def invoke_structured(
    schema: type[T],
    messages: list,
    *,
    seed_override: int | None = None,
    strict: bool = True,
) -> T:
    """네이티브 structured output과 JSON fallback을 한 logical call로 실행한다.

    Args:
        schema: 검증할 Pydantic response model이다.
        messages: LangChain message 목록이다.
        seed_override: 평가 호출이 명시할 선택적 seed다.
        strict: 닫힌 공급자 schema를 사용할지 여부다. 자유형 capability map만
            명시적으로 non-strict를 허용한다.

    Returns:
        schema 검증이 끝난 structured response다.

    Notes:
        telemetry operation ``structured:{schema.__name__}``, 호출 순서와 retry 설정을
        기존과 동일하게 유지한다.
    """
    schema_identity = f"{schema.__module__}.{schema.__qualname__}"
    if not strict and schema_identity not in _NON_STRICT_SCHEMA_IDENTITIES:
        raise ValueError(
            "Non-strict structured output is allowed only for the open-ended "
            f"deployment-needs contract, not {schema_identity}."
        )

    llm = build_llm(seed_override=seed_override)
    with telemetry.record_llm_call(f"structured:{schema.__name__}") as call:
        parsed = invoke_native_structured(llm, schema, messages, call, strict=strict)
        if parsed is not None:
            return parsed
        return invoke_json_mode(llm, schema, messages, call, strict=strict)
