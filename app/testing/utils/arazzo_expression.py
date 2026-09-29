"""Safe, small runtime-expression support for EasyDep's Arazzo profile."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from jsonpath_rfc9535 import JSONPathError, find

from app.testing.schemas.arazzo import ArazzoValidationError, parse_arazzo_runtime_expression


class ArazzoExpressionError(ValueError):
    """An Arazzo runtime expression could not be resolved deterministically."""


_EMBEDDED = re.compile(r"\{([^{}]+)\}")


def _pointer(value: Any, pointer: str) -> Any:
    if not pointer.startswith("#/"):
        if pointer == "#":
            return value
        raise ArazzoExpressionError(f"Unsupported JSON Pointer: {pointer}")
    current = value
    for raw_part in pointer[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise ArazzoExpressionError(f"JSON Pointer does not resolve: {pointer}") from exc
        elif isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            raise ArazzoExpressionError(f"JSON Pointer does not resolve: {pointer}")
    return current


def _parts(value: Any, dotted: str) -> Any:
    current = value
    if not dotted:
        return current
    for part in dotted.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise ArazzoExpressionError(f"Runtime expression does not resolve: {dotted}")
    return current


def resolve(expression: str, context: Mapping[str, Any]) -> Any:
    """Resolve one complete EasyDep runtime expression without evaluating code."""
    try:
        root, parts, pointer = parse_arazzo_runtime_expression(expression)
    except ArazzoValidationError as exc:
        raise ArazzoExpressionError(str(exc)) from exc
    if root in {"url", "method", "statusCode"}:
        value = context[root]
    elif root in {"request", "response"}:
        value = _parts(context.get(root, {}), ".".join(parts))
    elif root in {"inputs", "outputs"}:
        value = context.get(root, {}).get(parts[0])
        if parts[0] not in context.get(root, {}):
            raise ArazzoExpressionError(f"Runtime expression does not resolve: {expression}")
    elif root == "steps":
        value = _parts(context.get("steps", {}), f"{parts[0]}.outputs.{parts[1]}")
    elif root == "workflows":
        value = _parts(context.get("workflows", {}), ".".join(parts))
    else:  # The parser is closed, but preserve fail-closed behavior if it expands.
        raise ArazzoExpressionError(f"Unsupported runtime expression: {expression}")
    return _pointer(value, "#" + pointer) if pointer is not None else value


def interpolate(value: Any, context: Mapping[str, Any]) -> Any:
    """Resolve full expressions as values and ``{...}`` expressions inside strings."""
    if not isinstance(value, str):
        return value
    if value.startswith("$") and "{" not in value:
        return resolve(value, context)

    def replacement(match: re.Match[str]) -> str:
        resolved = resolve(match.group(1), context)
        if isinstance(resolved, (dict, list)):
            return json.dumps(resolved, ensure_ascii=False, separators=(",", ":"))
        return str(resolved)

    return _EMBEDDED.sub(replacement, value)


def select_jsonpath(value: Any, expression: str) -> list[Any]:
    """Evaluate RFC9535 JSONPath and return selected JSON values."""
    try:
        return [node.value for node in find(expression, value)]
    except JSONPathError as exc:
        raise ArazzoExpressionError(f"Invalid RFC9535 JSONPath: {expression}") from exc


__all__ = ["ArazzoExpressionError", "interpolate", "resolve", "select_jsonpath"]
