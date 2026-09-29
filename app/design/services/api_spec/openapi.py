"""기존 dict 기반 OpenAPI projection import를 보존하는 호환 facade다."""
from __future__ import annotations

from typing import Any

from app.design.contracts.api_spec import ApiSpecModel
from app.design.services.api_spec.projection import (
    OPENAPI_VERSION,
    build_openapi_from_payload,
    sanitize_path,
    sanitize_schema_name,
)
from app.design.services.api_spec.projection import (
    build_openapi_from_model as _build_typed_openapi,
)


def build_openapi_from_model(model: dict[str, Any] | ApiSpecModel) -> dict[str, Any]:
    """기존 dict 또는 새 typed 모델을 동일한 OpenAPI JSON으로 투영한다."""

    if not model:
        return {}
    if isinstance(model, ApiSpecModel):
        return _build_typed_openapi(model)
    return build_openapi_from_payload(model)


__all__ = [
    "OPENAPI_VERSION",
    "build_openapi_from_model",
    "sanitize_path",
    "sanitize_schema_name",
]
