"""Reusable OpenAPI request and response primitives for functional testing."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urljoin

import httpx
import jsonschema

from app.design.contracts.application_runtime import SYNTHETIC_UUID_BASIC_USERNAME

_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_SUMMARY_STRING_LIMIT = 512
_SUMMARY_SIZE_LIMIT = 4000


class UpstreamAmbiguity(ValueError):
    """고정 산출물에 실행을 정할 정보가 없을 때 사용한다."""


@dataclass(frozen=True)
class Operation:
    operation_id: str
    path: str
    method: str
    value: dict[str, Any]
    path_item: dict[str, Any]


@dataclass(frozen=True)
class InputValueRequest:
    """LLM에 전체 요청이 아니라 값 하나만 물어보기 위한 최소 입력이다."""

    operation_id: str
    location: str
    schema: dict[str, Any]
    operation_context: str = ""


InputValueProposer = Callable[[InputValueRequest], Any]


def _ref(
    document: dict[str, Any], schema: Any, seen: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """입력 예시와 leaf 탐색에 필요한 local $ref 하나만 푼다."""
    if not isinstance(schema, dict):
        raise UpstreamAmbiguity("The OpenAPI schema must be an object.")
    pointer = schema.get("$ref")
    if not pointer:
        return schema
    if not isinstance(pointer, str) or not pointer.startswith("#/") or pointer in seen:
        raise UpstreamAmbiguity(f"The OpenAPI schema reference cannot be resolved: {pointer}")
    target: Any = document
    for part in pointer[2:].split("/"):
        if not isinstance(target, dict) or part not in target:
            raise UpstreamAmbiguity(f"The OpenAPI schema reference does not exist: {pointer}")
        target = target[part]
    return {
        **_ref(document, target, seen | {pointer}),
        **{key: value for key, value in schema.items() if key != "$ref"},
    }


def resolve_schema(document: dict[str, Any], schema: Any) -> dict[str, Any]:
    """Resolve one local OpenAPI schema reference for request-value synthesis."""

    return _ref(document, schema)


def _inline_refs(document: dict[str, Any], value: Any, seen: frozenset[str] = frozenset()) -> Any:
    """독립 response schema 안의 local ref를 실제 선언으로 펼친다."""
    if isinstance(value, list):
        return [_inline_refs(document, item, seen) for item in value]
    if not isinstance(value, dict):
        return value
    pointer = value.get("$ref")
    if pointer:
        if not isinstance(pointer, str) or not pointer.startswith("#/") or pointer in seen:
            raise UpstreamAmbiguity(f"The OpenAPI schema reference cannot be expanded: {pointer}")
        target: Any = document
        for part in pointer[2:].split("/"):
            if not isinstance(target, dict) or part not in target:
                raise UpstreamAmbiguity(f"The OpenAPI schema reference does not exist: {pointer}")
            target = target[part]
        merged = {
            **target,
            **{key: child for key, child in value.items() if key != "$ref"},
        }
        return _inline_refs(document, merged, seen | {pointer})
    return {key: _inline_refs(document, child, seen) for key, child in value.items()}


def _type(document: dict[str, Any], schema: Any) -> str:
    value = _ref(document, schema)
    kind = value.get("type")
    if isinstance(kind, str) and kind:
        return kind
    alternatives = value.get("anyOf")
    if isinstance(alternatives, list):
        concrete = [
            item for item in alternatives if isinstance(item, dict) and item.get("type") != "null"
        ]
        has_null = any(
            isinstance(item, dict) and item.get("type") == "null" for item in alternatives
        )
        if has_null and len(concrete) == 1:
            return _type(document, concrete[0])
    if isinstance(value.get("properties"), dict):
        return "object"
    if "items" in value:
        return "array"
    if value.get("enum"):
        return "string"
    raise UpstreamAmbiguity("The OpenAPI schema type is missing.")


def schema_errors(document: dict[str, Any], schema: dict[str, Any], value: Any) -> list[str]:
    """제안값을 실제 OpenAPI leaf schema로 다시 검사한다."""

    expanded = _inline_refs(document, schema)
    try:
        validator = jsonschema.Draft202012Validator(
            expanded,
            format_checker=jsonschema.FormatChecker(),
        )
        return [error.message for error in sorted(validator.iter_errors(value), key=str)]
    except jsonschema.SchemaError as error:
        raise UpstreamAmbiguity(f"The OpenAPI input schema is invalid: {error}") from error


def _index(document: dict[str, Any]) -> dict[str, list[Operation]]:
    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise UpstreamAmbiguity("The frozen OpenAPI document has no paths.")
    result: dict[str, list[Operation]] = {}
    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, dict):
            continue
        for method, value in path_item.items():
            operation_id = (
                value.get("operationId")
                if isinstance(value, dict) and str(method).lower() in _METHODS
                else None
            )
            if isinstance(operation_id, str) and operation_id.strip():
                item = Operation(operation_id.strip(), path, str(method).upper(), value, path_item)
                result.setdefault(item.operation_id, []).append(item)
    return result


def operation_for_id(document: dict[str, Any], operation_id: str) -> Operation:
    """Resolve exactly one operation from the frozen OpenAPI document."""
    matches = _index(document).get(operation_id, [])
    if len(matches) != 1:
        detail = "was not found" if not matches else "is duplicated"
        raise UpstreamAmbiguity(f"OpenAPI operationId {operation_id} {detail}.")
    return matches[0]


def _response_schema(
    document: dict[str, Any], operation: Operation, status: int | None = None
) -> dict[str, Any] | None:
    responses = operation.value.get("responses")
    if not isinstance(responses, dict):
        raise UpstreamAmbiguity(f"OpenAPI responses are empty: {operation.operation_id}")
    response: Any
    if status is None:
        candidates = [
            value
            for key, value in responses.items()
            if str(key).startswith("2") and isinstance(value, dict)
        ]
        if len(candidates) != 1:
            raise UpstreamAmbiguity(
                f"OpenAPI success response schema is ambiguous: {operation.operation_id}"
            )
        response = candidates[0]
    else:
        response = (
            responses.get(str(status))
            or responses.get(f"{str(status)[0]}XX")
            or responses.get("default")
        )
    if not isinstance(response, dict):
        raise UpstreamAmbiguity(f"OpenAPI success response is missing: {operation.operation_id}")
    content = response.get("content")
    # 204처럼 본문이 없는 성공 응답은 schema가 없는 것이 정상이다. ``content``를
    # 선언했는데 JSON schema만 빠진 경우와 구분하여, 후자는 계속 명세 오류로 다룬다.
    if content is None:
        return None
    json_content = content.get("application/json") if isinstance(content, dict) else None
    schema = json_content.get("schema") if isinstance(json_content, dict) else None
    if not isinstance(schema, dict):
        raise UpstreamAmbiguity(f"OpenAPI response schema is missing: {operation.operation_id}")
    _type(document, schema)
    return schema


def _basic_auth() -> tuple[str, str]:
    """로컬 Testing 앱의 공통 Basic 테스트 계정을 모든 호출에 보낸다."""
    return os.environ.get("EASYDEP_TEST_USERNAME", SYNTHETIC_UUID_BASIC_USERNAME), os.environ.get(
        "EASYDEP_TEST_PASSWORD", "easydep-test"
    )


def operation_url(
    target_url: str, operation: Operation, values: dict[str, Any], query: dict[str, Any]
) -> str:
    path = operation.path
    for name, value in values.items():
        path = path.replace("{" + name + "}", quote(str(value), safe=""))
    if "{" in path or "}" in path:
        raise UpstreamAmbiguity(f"A required path parameter cannot be populated: {operation.path}")
    parameters: dict[str, dict[str, Any]] = {}
    for owner in (operation.path_item, operation.value):
        for parameter in owner.get("parameters") or []:
            if (
                isinstance(parameter, dict)
                and parameter.get("in") == "query"
                and isinstance(parameter.get("name"), str)
            ):
                parameters[parameter["name"]] = parameter
    query_pairs: list[tuple[str, Any]] = []
    for name, value in query.items():
        parameter = parameters.get(name, {})
        if isinstance(value, dict):
            style = parameter.get("style", "form")
            explode = parameter.get("explode", style == "form")
            if style == "form" and explode:
                query_pairs.extend((str(key), item) for key, item in value.items())
                continue
            if style == "form":
                flattened = [part for pair in value.items() for part in (pair[0], pair[1])]
                query_pairs.append((name, ",".join(str(part) for part in flattened)))
                continue
            if style == "deepObject" and explode:
                query_pairs.extend((f"{name}[{key}]", item) for key, item in value.items())
                continue
        query_pairs.append((name, value))
    return urljoin(target_url.rstrip("/") + "/", path.lstrip("/")) + (
        ("?" + urlencode(query_pairs, doseq=True)) if query_pairs else ""
    )


def _summary_value(value: Any, *, depth: int = 0) -> Any:
    """수리 evidence에 넣을 테스트 값을 작은 JSON 형태로 만든다."""

    if depth >= 6:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(name): _summary_value(item, depth=depth + 1)
            for name, item in list(value.items())[:40]
        }
    if isinstance(value, (list, tuple)):
        values = [_summary_value(item, depth=depth + 1) for item in value[:20]]
        if len(value) > 20:
            values.append("[TRUNCATED]")
        return values
    if isinstance(value, str):
        return value[:_SUMMARY_STRING_LIMIT] + ("…" if len(value) > _SUMMARY_STRING_LIMIT else "")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:_SUMMARY_STRING_LIMIT]


def _bounded_summary(value: Any) -> Any:
    summarized = _summary_value(value)
    rendered = json.dumps(summarized, ensure_ascii=False, default=str)
    if len(rendered) <= _SUMMARY_SIZE_LIMIT:
        return summarized
    return {
        "truncated": True,
        "preview": rendered[: _SUMMARY_SIZE_LIMIT - 64] + "…",
    }


def response_summary(body: str) -> str:
    """오류 응답도 요청 evidence와 같은 크기 제한을 적용한다."""

    try:
        return json.dumps(_bounded_summary(json.loads(body)), ensure_ascii=False)
    except (TypeError, ValueError):
        return body[:2000] + ("…" if len(body) > 2000 else "")


def _response_summary(body: str) -> str:
    """Backward-compatible private alias for the reusable response primitive."""
    return response_summary(body)


def _request_summary(
    operation: Operation,
    paths: dict[str, Any],
    query: dict[str, Any],
    headers: dict[str, Any],
    body: Any,
    *,
    sent: bool = True,
) -> dict[str, Any]:
    path = operation.path
    for name, value in paths.items():
        path = path.replace("{" + name + "}", quote(str(_summary_value(value)), safe=""))
    result: dict[str, Any] = {
        "method": operation.method.upper(),
        "path": path,
        "query": _bounded_summary(query),
        "body": _bounded_summary(body),
    }
    if headers:
        result["headers"] = _bounded_summary(headers)
    if not sent:
        result["sent"] = False
    return result


def send_operation_request(
    operation: Operation,
    *,
    target_url: str,
    paths: dict[str, Any],
    query: dict[str, Any],
    headers: dict[str, Any],
    body: Any,
    timeout_seconds: float,
) -> tuple[httpx.Response, dict[str, Any]]:
    """Send one prepared OpenAPI operation and retain the bounded request evidence."""
    request = _request_summary(operation, paths, query, headers, body)
    response = httpx.request(
        operation.method,
        operation_url(target_url, operation, paths, query),
        headers={"Accept": "application/json", **headers},
        json=body,
        auth=_basic_auth(),
        timeout=timeout_seconds,
        follow_redirects=False,
    )
    return response, request


def validate_operation_response(
    document: dict[str, Any], operation: Operation, response: httpx.Response
) -> Any:
    """Validate a declared response body and return its decoded JSON value, if any."""
    schema = _response_schema(document, operation, response.status_code)
    if schema is None:
        if response.content:
            raise ValueError("The response contains a body that is absent from OpenAPI.")
        return None
    payload = response.json()
    errors = sorted(
        jsonschema.Draft202012Validator(_inline_refs(document, schema)).iter_errors(payload),
        key=str,
    )
    if errors:
        raise ValueError(str(errors[0]))
    return payload


__all__ = [
    "InputValueProposer",
    "InputValueRequest",
    "UpstreamAmbiguity",
    "operation_for_id",
    "operation_url",
    "resolve_schema",
    "response_summary",
    "schema_errors",
    "send_operation_request",
    "validate_operation_response",
]
