"""Provider-safe JSON Schema helpers for LLM structured outputs."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel


def remove_non_ascii_descriptions(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a JSON Schema while removing descriptions outside the English-only contract.

    Schema descriptions become model context, unlike source-code comments. Keep ASCII
    field descriptions, but remove non-ASCII descriptions such as Korean Pydantic
    class docstrings without changing validation keywords or values.
    """

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: clean(item)
                for key, item in value.items()
                if key != "description" or not isinstance(item, str) or item.isascii()
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return clean(deepcopy(dict(schema)))


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Build an OpenAI-compatible strict schema with English-only descriptions."""

    schema = remove_non_ascii_descriptions(to_strict_json_schema(model))
    _validate_closed_objects(schema)
    return schema


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Build a non-strict schema while applying the same English-only descriptions."""

    return remove_non_ascii_descriptions(model.model_json_schema())


def _validate_closed_objects(value: Any, path: str = "$") -> None:
    """Reject open maps that the provider cannot enforce as strict output."""

    if isinstance(value, dict):
        if value.get("type") == "object":
            properties = value.get("properties")
            required = value.get("required")
            if value.get("additionalProperties") is not False:
                raise ValueError(
                    f"Strict LLM schema contains an open-ended object at {path}; "
                    "use an explicit non-strict contract for free-form maps."
                )
            if isinstance(properties, dict) and set(properties) != set(required or []):
                raise ValueError(
                    f"Strict LLM schema does not require every property at {path}."
                )
        for key, item in value.items():
            _validate_closed_objects(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_closed_objects(item, f"{path}[{index}]")
