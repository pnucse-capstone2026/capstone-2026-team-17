"""공급자와 무관한 모델별 요청 차이를 한곳에서 설명한다.

EasyDep은 전 단계에서 하나의 ``MODEL``을 사용한다. 하지만 모델마다 허용하는 sampling,
reasoning, 출력 상한이 다르므로 같은 요청 dict를 그대로 보낼 수는 없다. 이 모듈은 모델을
고르는 기능이 아니라, 선택된 모델에 맞는 요청 모양만 제공한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

MIN_TEMPERATURE = 0.2

ReasoningMode = Literal["none", "low", "medium", "high", "max"]


@dataclass(frozen=True, slots=True)
class LlmModelProfile:
    """한 모델이 받는 공통 생성 파라미터와 확인된 선택 기능이다."""

    model_id: str
    temperature: float
    top_p: float | None
    reasoning_effort: ReasoningMode | None
    supported_reasoning: tuple[ReasoningMode, ...]
    reasoning_budget: int | None
    default_max_tokens: int
    max_tokens: int
    thinking_via_chat_template: bool = False
    preserve_reasoning_on_tool_turn: bool = False

    def __post_init__(self) -> None:
        if self.temperature < MIN_TEMPERATURE:
            raise ValueError(
                f"LLM temperature must be at least {MIN_TEMPERATURE}: {self.temperature}"
            )
        if self.default_max_tokens < 1 or self.max_tokens < self.default_max_tokens:
            raise ValueError("LLM token limits must be positive and ordered")

    def completion_limit(self, requested: int | None) -> int:
        """호출자가 원하는 상한을 provider의 실제 허용 범위 안으로 제한한다."""

        wanted = requested if requested is not None else self.default_max_tokens
        return max(1, min(int(wanted), self.max_tokens))

    def resolve_reasoning(self, requested: str | None = None) -> ReasoningMode | None:
        """단계 설정을 모델이 이해하는 가장 가까운 reasoning 값으로 바꾼다."""

        if not self.supported_reasoning:
            return None
        candidate = str(requested or self.reasoning_effort or "").strip().lower()
        if candidate in self.supported_reasoning:
            return cast(ReasoningMode, candidate)
        # 기존 EasyDep 기본값인 medium을 지원하지 않는 모델은 품질 우선값 high를 쓴다.
        if candidate == "medium" and "high" in self.supported_reasoning:
            return "high"
        if requested is not None:
            raise ValueError(f"unsupported reasoning effort: {candidate}")
        return self.reasoning_effort

    def extra_body(self, provider: str) -> dict[str, object] | None:
        """NVIDIA NIM을 직접 선택했을 때만 전용 요청값을 반환한다."""

        if provider != "nvidia_nim":
            return None
        body: dict[str, object] = {}
        if self.thinking_via_chat_template:
            body["chat_template_kwargs"] = {"enable_thinking": True}
        if self.reasoning_budget is not None:
            body["reasoning_budget"] = self.reasoning_budget
        return body or None

    def reported_reasoning_budget(self, provider: str) -> int | None:
        """실제 요청에 포함된 경우에만 계측용 reasoning budget을 돌려준다."""

        return self.reasoning_budget if provider == "nvidia_nim" else None


_PROFILES: dict[str, LlmModelProfile] = {
    "zai-org/glm-5.3-flash": LlmModelProfile(
        model_id="zai-org/glm-5.3-flash",
        temperature=0.2,
        top_p=None,
        reasoning_effort="medium",
        supported_reasoning=("low", "medium", "high"),
        reasoning_budget=None,
        default_max_tokens=8192,
        max_tokens=16384,
    ),
    "openai/gpt-oss-20b": LlmModelProfile(
        model_id="openai/gpt-oss-20b",
        temperature=0.6,
        top_p=None,
        reasoning_effort="medium",
        supported_reasoning=("low", "medium", "high"),
        reasoning_budget=None,
        default_max_tokens=4096,
        max_tokens=4096,
        preserve_reasoning_on_tool_turn=True,
    ),
    "nvidia/nemotron-3-super-120b-a12b": LlmModelProfile(
        model_id="nvidia/nemotron-3-super-120b-a12b",
        temperature=1.0,
        top_p=0.95,
        reasoning_effort="high",
        supported_reasoning=("none", "low", "high"),
        reasoning_budget=16384,
        default_max_tokens=16384,
        max_tokens=32768,
    ),
    "nvidia/nemotron-3.5-lightning-30b-a3b": LlmModelProfile(
        model_id="nvidia/nemotron-3.5-lightning-30b-a3b",
        temperature=1.0,
        top_p=0.95,
        reasoning_effort=None,
        supported_reasoning=(),
        reasoning_budget=16384,
        default_max_tokens=16384,
        max_tokens=32768,
        thinking_via_chat_template=True,
    ),
    "moonshotai/kimi-k3": LlmModelProfile(
        model_id="moonshotai/kimi-k3",
        temperature=1.0,
        top_p=None,
        reasoning_effort="max",
        supported_reasoning=("low", "high", "max"),
        reasoning_budget=None,
        default_max_tokens=16384,
        max_tokens=65536,
        preserve_reasoning_on_tool_turn=True,
    ),
    "deepseek-ai/deepseek-v4-pro-0813": LlmModelProfile(
        model_id="deepseek-ai/deepseek-v4-pro-0813",
        temperature=1.0,
        top_p=0.95,
        reasoning_effort="high",
        supported_reasoning=("none", "high", "max"),
        reasoning_budget=None,
        default_max_tokens=8192,
        max_tokens=16384,
    ),
    "poolside/laguna-xs-2.1": LlmModelProfile(
        model_id="poolside/laguna-xs-2.1",
        temperature=1.0,
        top_p=0.95,
        reasoning_effort=None,
        supported_reasoning=(),
        reasoning_budget=None,
        default_max_tokens=8192,
        max_tokens=16384,
        preserve_reasoning_on_tool_turn=True,
    ),
}


def canonical_model_id(model: str) -> str:
    """전송 경로가 붙인 접두사를 제거하고 실제 모델 ID를 반환한다."""

    value = model.strip()
    return (
        value.removeprefix("openrouter/")
        .removeprefix("nvidia_nim/")
        .removeprefix("@cf/")
    )


def profile_for(
    model: str,
    *,
    fallback_temperature: float = MIN_TEMPERATURE,
    fallback_max_tokens: int = 16384,
) -> LlmModelProfile:
    """알려진 모델은 공식 profile을, 그 밖의 모델은 안전한 공통 profile을 반환한다."""

    model_id = canonical_model_id(model)
    known = _PROFILES.get(model_id)
    if known is not None:
        return known
    # 등록 전의 GPT-OSS 배포 이름도 기존 reasoning 요청 형식을 유지한다. 실제 스크리닝
    # 후보의 sampling 값은 위의 정확한 ID profile에서만 정한다.
    is_gpt_oss = "gpt-oss" in model_id.lower()
    return LlmModelProfile(
        model_id=model_id,
        temperature=max(MIN_TEMPERATURE, float(fallback_temperature)),
        top_p=None,
        reasoning_effort="medium" if is_gpt_oss else None,
        supported_reasoning=("low", "medium", "high") if is_gpt_oss else (),
        reasoning_budget=None,
        default_max_tokens=max(1, int(fallback_max_tokens)),
        max_tokens=max(1, int(fallback_max_tokens)),
        preserve_reasoning_on_tool_turn=is_gpt_oss,
    )


def candidate_model_ids() -> tuple[str, ...]:
    """동결된 첫 스크리닝 후보를 문서와 같은 순서로 반환한다."""

    return tuple(_PROFILES)


def effective_temperature(model: str, fallback: float) -> float:
    """실제 요청과 cache key가 같은 temperature를 사용하게 한다."""

    return profile_for(model, fallback_temperature=fallback).temperature
