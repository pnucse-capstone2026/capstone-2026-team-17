"""타입이 있는 BCE 클래스 모델 생성과 결정론적 렌더링 공개 API다."""
from __future__ import annotations

from typing import Any

__all__ = [
    "generate_class_model",
    "resume_class_model",
    "revise_class_model",
]


def __getattr__(name: str) -> Any:
    """서비스 공개 API를 필요할 때만 불러 검증 모듈 순환을 피한다."""
    if name in __all__:
        from app.design.services.class_diagram import service

        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
