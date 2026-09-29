"""타입 API 제안·정규화·OpenAPI 투영의 public 경계다."""

from app.design.contracts.api_spec import ApiSpecModel
from app.design.services.api_spec.normalization import normalize_api_spec_model
from app.design.services.api_spec.projection import build_openapi_from_model
from app.design.services.api_spec.service import (
    generate_api_spec_model,
    revise_api_spec_model,
)

__all__ = [
    "ApiSpecModel",
    "build_openapi_from_model",
    "generate_api_spec_model",
    "normalize_api_spec_model",
    "revise_api_spec_model",
]
