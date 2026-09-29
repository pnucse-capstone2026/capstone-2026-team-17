"""Modeling stage가 공유하는 typed proposal 호출과 state patch 계약이다."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from app.requirements.modeling.validation import Review

ModelT = TypeVar("ModelT", bound=BaseModel)
ModelingStagePatch = dict[str, object]
SemanticReviewCall = Callable[..., Review]


class StructuredProposalCall(Protocol):
    """기존 structured runtime과 동일한 typed proposal 호출 경계다."""

    def __call__(
        self,
        schema: type[ModelT],
        messages: list[BaseMessage],
        *,
        seed_override: int | None = None,
    ) -> ModelT:
        """지정 schema와 message로 검증된 proposal 하나를 반환한다."""


__all__ = [
    "ModelingStagePatch",
    "SemanticReviewCall",
    "StructuredProposalCall",
]
