"""루트 설정을 모든 개발 단계가 공유하는 LLM 연결 정보로 바꾼다.

직접 OpenAI 호환 SDK를 쓰는 단계와 LiteLLM을 쓰는 OpenHands는 모델 이름의 모양이
다르다. 공급자를 URL로 추측하거나 호출부마다 접두사를 붙이지 않고, 이 모듈이 두 경로에
필요한 값을 한 번만 계산한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.config import Settings, settings

LlmProvider = Literal[
    "openrouter", "nvidia_nim", "cloudflare", "openai_compatible"
]


@dataclass(frozen=True, slots=True)
class LlmConnection:
    """한 OpenAI 호환 클라이언트를 만드는 데 필요한 값이다."""

    provider: LlmProvider
    api_key: str
    base_url: str
    model: str
    litellm_provider: str
    headers: tuple[tuple[str, str], ...] = ()

    def default_headers(self) -> dict[str, str]:
        """SDK에 넘길 새 dict를 반환해 공유 설정이 변경되지 않게 한다."""

        return dict(self.headers)

    def litellm_model(self) -> str:
        """직접 SDK용 모델 ID에 LiteLLM adapter를 정확히 한 번 붙인다."""

        return f"{self.litellm_provider}/{self.model}"

    def display_name(self) -> str:
        """사용자에게 보여 줄 제공자 이름을 실제 연결과 맞춘다."""

        return {
            "openrouter": "OpenRouter",
            "nvidia_nim": "NVIDIA NIM",
            "cloudflare": "Cloudflare AI Gateway",
            "openai_compatible": "OpenAI-compatible LLM provider",
        }[self.provider]

    def openhands_options(self) -> dict[str, object]:
        """선택한 공급자에서 OpenHands가 추가로 필요로 하는 옵션만 반환한다."""

        if self.provider == "cloudflare":
            return {"force_string_serializer": True}
        return {}

    @property
    def requires_openhands_message_normalization(self) -> bool:
        """Cloudflare tool-call 왕복에 빈 문자열 보정이 필요한지 알려 준다."""

        return self.provider == "cloudflare"

    def format_openhands_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """공급자가 요구하는 OpenHands assistant 메시지 형식을 적용한다.

        OpenHands는 설명 없이 도구만 호출한 assistant 메시지의 ``content``를 없앨 수
        있다. Cloudflare Workers AI는 뒤따르는 tool 결과를 받을 때 빈 문자열이라도 이
        key가 있어야 한다. 또한 응답에서 받은 ``reasoning_content``를 다음 요청에
        재전송하면 Cloudflare의 OpenAI 호환 endpoint가 지원하지 않는 필드로 거부한다.
        입력과 다른 공급자의 메시지는 그대로 보존한다.
        """

        if not self.requires_openhands_message_normalization:
            return messages
        normalized: list[dict[str, Any]] = []
        for message in messages:
            if message.get("role") != "assistant":
                normalized.append(message)
                continue
            assistant = dict(message)
            assistant.pop("reasoning_content", None)
            if assistant.get("tool_calls") and assistant.get("content") is None:
                assistant["content"] = ""
            normalized.append(assistant)
        return normalized


def _direct_model_id(model: str) -> str:
    """루트 MODEL에 LiteLLM 전용 접두사가 섞이지 않았는지 확인한다."""

    value = model.strip()
    forbidden = ("openrouter/", "nvidia_nim/")
    if value.startswith(forbidden):
        raise ValueError(
            "MODEL must be the provider's direct model ID without a LiteLLM prefix"
        )
    return value


def build_llm_connection(config: Settings = settings) -> LlmConnection:
    """명시된 공급자와 공통 설정으로 하나의 연결을 만든다."""

    return _build_llm_connection(config, config.model)


def build_openhands_llm_connection(config: Settings = settings) -> LlmConnection:
    """Build the OpenHands connection, optionally overriding only MODEL."""

    return _build_llm_connection(config, config.openhands_model or config.model)


def build_admission_llm_connection(config: Settings = settings) -> LlmConnection:
    """Build the semantic-admission connection without changing Design MODEL."""

    return _build_llm_connection(config, config.admission_model or config.model)


def build_arazzo_llm_connection(config: Settings = settings) -> LlmConnection:
    """Build the Testing workflow-planning connection without changing MODEL."""

    return _build_llm_connection(config, config.arazzo_model or config.model)


def _build_llm_connection(config: Settings, model_id: str) -> LlmConnection:
    """Assemble one provider connection while the caller chooses its model."""

    provider = config.llm_provider
    model = _direct_model_id(model_id)
    account_id = (config.cloudflare_account_id or "").strip()
    api_token = (config.cloudflare_api_token or "").strip()
    gateway_id = (config.cloudflare_ai_gateway_id or "").strip()

    if provider == "cloudflare" and (account_id or api_token):
        missing = [
            name
            for name, value in (
                ("CLOUDFLARE_ACCOUNT_ID", account_id),
                ("CLOUDFLARE_API_TOKEN", api_token),
            )
            if not value
        ]
        if missing:
            names = ", ".join(missing)
            raise ValueError(f"Cloudflare LLM configuration is incomplete: {names}")
        api_key = api_token
        base_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
    else:
        # 다른 공급자를 선택하면 오래된 Cloudflare 값이 남아 있어도 읽지 않는다.
        api_key = config.api_key
        base_url = config.base_url

    litellm_provider = {
        "openrouter": "openrouter",
        "nvidia_nim": "nvidia_nim",
        "cloudflare": "openai",
        "openai_compatible": "openai",
    }[provider]
    headers = (
        (("cf-aig-gateway-id", gateway_id),)
        if provider == "cloudflare" and gateway_id
        else ()
    )
    return LlmConnection(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        litellm_provider=litellm_provider,
        headers=headers,
    )


def llm_subprocess_environment(config: Settings = settings) -> dict[str, str]:
    """하위 Python/Docker 프로세스가 같은 중앙 설정을 다시 읽게 한다.

    반환된 dict의 key가 곧 전체 전달 목록이다. runner가 별도의 LLM 변수 목록을 갖지
    않으므로 여기에 provider 설정을 추가해도 구현 단계만 빠뜨리는 일이 생기지 않는다.
    """

    design_connection = build_llm_connection(config)
    openhands_connection = build_openhands_llm_connection(config)
    admission_connection = build_admission_llm_connection(config)
    arazzo_connection = build_arazzo_llm_connection(config)
    environment = {
        "LLM_PROVIDER": design_connection.provider,
        "API_KEY": design_connection.api_key,
        "BASE_URL": design_connection.base_url,
        "MODEL": design_connection.model,
        "OPENHANDS_MODEL": openhands_connection.model,
        "ADMISSION_MODEL": admission_connection.model,
        "ARAZZO_MODEL": arazzo_connection.model,
        "LLM_TIMEOUT_SECONDS": str(config.llm_timeout_seconds),
        "LLM_WALL_TIMEOUT_SECONDS": str(config.llm_wall_timeout_seconds),
        "IMPLEMENTATION_REASONING_EFFORT": config.implementation_reasoning_effort,
        "IMPLEMENTATION_OWNER_TOOL_MODE": config.implementation_owner_tool_mode,
        "IMPLEMENTATION_OPENHANDS_CANARY": str(
            config.implementation_openhands_canary
        ).lower(),
        "IMPLEMENTATION_OPENHANDS_CANARY_REPETITIONS": str(
            config.implementation_openhands_canary_repetitions
        ),
        "IMPLEMENTATION_OPENHANDS_REQUEST_ATTEMPTS": str(
            config.implementation_openhands_request_attempts
        ),
        "IMPLEMENTATION_OPENHANDS_RETRY_MIN_WAIT_SECONDS": str(
            config.implementation_openhands_retry_min_wait_seconds
        ),
        "IMPLEMENTATION_OPENHANDS_RETRY_MAX_WAIT_SECONDS": str(
            config.implementation_openhands_retry_max_wait_seconds
        ),
        "IMPLEMENTATION_OPENHANDS_RETRY_MULTIPLIER": str(
            config.implementation_openhands_retry_multiplier
        ),
        "IMPLEMENTATION_OPENHANDS_CANARY_MAX_ATTEMPTS": str(
            config.implementation_openhands_canary_max_attempts
        ),
        "IMPLEMENTATION_OPENHANDS_CANARY_TRANSIENT_TTL_SECONDS": str(
            config.implementation_openhands_canary_transient_ttl_seconds
        ),
    }
    # URL과 key는 위에서 최종값으로 바꿨으므로 account/token을 중복 전달하지 않는다.
    # Gateway 선택 header에 실제로 쓰이는 ID만 Cloudflare 하위 프로세스에 보낸다.
    if design_connection.provider == "cloudflare" and config.cloudflare_ai_gateway_id:
        environment["CLOUDFLARE_AI_GATEWAY_ID"] = config.cloudflare_ai_gateway_id.strip()
    return environment
