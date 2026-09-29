from __future__ import annotations

import importlib.util
import sys

from app.config import settings
from app.llm_connection import LlmConnection, build_openhands_llm_connection


def openhands_connection() -> LlmConnection:
    """OpenHands도 다른 단계와 같은 중앙 연결 정보를 한 번만 읽는다.

    이 모듈은 OpenHands SDK가 요구하는 실행 보조 기능만 가진다. provider별 URL,
    모델 접두사, header 규칙은 ``app.llm_connection`` 밖으로 복사하지 않는다.
    """

    return build_openhands_llm_connection()


def configured_max_output_tokens(default: int) -> int:
    raw = settings.openhands_max_output_tokens
    return int(raw) if raw else default


def openhands_compatibility(
    connection: LlmConnection | None = None,
) -> dict[str, object]:
    """실행 전 SDK와 공통 LLM 연결의 최소 준비 상태를 보고한다."""

    effective_connection = connection or openhands_connection()
    return {
        "python": ".".join(map(str, sys.version_info[:3])),
        "pythonCompatible": sys.version_info >= (3, 12),
        "sdkInstalled": module_available("openhands.sdk"),
        "toolsInstalled": module_available("openhands.tools"),
        "apiKeyConfigured": bool(effective_connection.api_key),
    }


def module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False
