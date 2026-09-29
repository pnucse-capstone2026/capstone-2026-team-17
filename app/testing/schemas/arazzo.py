"""A small, deliberately closed execution profile for Arazzo 1.1 documents.

The Arazzo schema describes a broad interoperability format.  EasyDep only
executes a local, synchronous subset, so this module first applies the
vendored official schema when it is available and then applies the narrower
profile below.  Keeping that second pass here is important: a schema-valid
document is not necessarily safe or deterministic to execute.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Collection, Iterator, Mapping
from pathlib import Path
from typing import Any, NoReturn

import jsonpath_rfc9535
import jsonschema

from app.testing.utils.functional_executor import schema_errors

ARAZZO_VERSION = "1.1.0"

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
_MAX_WORKFLOWS = 50
_MAX_STEPS = 100
_MAX_RETRY_LIMIT = 3
_MAX_WORKFLOW_DEPTH = 8
_TRACE_KEYS = frozenset({"requirementIds", "useCaseIds", "evidenceRefs"})
_EVAL_CALL = re.compile(r"\beval\s*\(", re.IGNORECASE)
_SCHEMA_FILE = "arazzo-1.1-2026-04-15.json"
_SCHEMA_SHA256 = "8c84165221038852b15472de58141c8cfd4af1b3d13b8e2dab02a94406fc72f1"
_STRICT_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]+$")
_HEADER_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_POINTER = re.compile(r"^(?:/(?:[^~/{}]|~[01])*)*$")
_KNOWN_RUNTIME_PREFIXES = (
    "$url",
    "$method",
    "$statusCode",
    "$request.",
    "$response.",
    "$inputs.",
    "$outputs.",
    "$steps.",
    "$workflows.",
    "$sourceDescriptions.",
    "$components.",
    "$message.",
    "$self",
)


class ArazzoValidationError(ValueError):
    """Raised when an Arazzo document cannot be safely executed by EasyDep."""


def _error(message: str) -> NoReturn:
    raise ArazzoValidationError(message)


def _official_schema() -> dict[str, Any]:
    """Load the pinned official schema and fail closed if it was changed or removed."""

    candidate = Path(__file__).with_name("vendor") / _SCHEMA_FILE
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        _error(f"Vendored Arazzo schema is unavailable: {exc}")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _error(f"Vendored Arazzo schema is unreadable: {exc}")
    if not isinstance(value, dict):
        _error("Vendored Arazzo schema must be an object.")
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    if hashlib.sha256(canonical).hexdigest() != _SCHEMA_SHA256:
        _error("Vendored Arazzo schema checksum does not match the pinned source.")
    return value


def _json_copy(value: Any, path: str = "document") -> Any:
    """Copy only finite JSON values, making the successful return canonicalizable."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _error(f"{path} contains a non-finite number.")
        return value
    if isinstance(value, list):
        return [_json_copy(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _error(f"{path} has a non-string object key.")
            copied[key] = _json_copy(item, f"{path}.{key}")
        return copied
    _error(f"{path} contains a non-JSON value.")


def _validate_jsonschema(document: dict[str, Any]) -> None:
    schema = copy.deepcopy(_official_schema())
    # The Arazzo 1.1 Parameter Object describes `value` as Any, but the pinned
    # official JSON Schema's first branch omits objects. EasyDep's executable
    # OpenAPI profile validates literal values against the frozen parameter
    # schema below, so allow ordinary JSON objects here without changing the
    # vendored schema. Keep Selector Objects exclusive to their own branch.
    parameter_value = schema["$defs"]["parameter-object"]["properties"]["value"]["oneOf"][0]
    parameter_value["type"].append("object")
    parameter_value["not"] = {"$ref": "#/$defs/selector-object"}
    try:
        validator = jsonschema.Draft202012Validator(schema)
        error = next(
            iter(
                sorted(
                    validator.iter_errors(document),
                    key=lambda item: tuple(map(str, item.absolute_path)),
                )
            ),
            None,
        )
    except jsonschema.SchemaError as exc:
        _error(f"Arazzo schema is invalid: {exc.message}")
    if error is not None:
        location = "/".join(str(part) for part in error.absolute_path) or "document"
        _error(f"Arazzo schema validation failed at {location}: {error.message}")


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _error(f"{path} must be a non-empty string.")
    return value.strip()


def _string_list(value: Any, path: str) -> None:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        _error(f"{path} must be a list of non-empty strings.")
    if len(set(value)) != len(value):
        _error(f"{path} must not contain duplicates.")


def _walk_values(value: Any) -> Iterator[Any]:
    """Yield nested scalar values from a JSON-compatible value."""

    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_values(child)
    else:
        yield value


def parse_arazzo_runtime_expression(expression: str) -> tuple[str, tuple[str, ...], str | None]:
    """Parse one supported Arazzo Runtime Expression without evaluating it."""

    if not isinstance(expression, str) or not expression.startswith("$"):
        _error(f"Invalid Arazzo Runtime Expression: {expression!r}")
    if expression in {"$url", "$method", "$statusCode"}:
        return expression[1:], (), None

    match = re.fullmatch(r"\$(request|response)\.(body|payload)(?:#(.*))?", expression)
    if match:
        pointer = match.group(3)
        if pointer is not None and not _POINTER.fullmatch(pointer):
            _error(f"Invalid JSON Pointer in Runtime Expression: {expression}")
        return match.group(1), (match.group(2),), pointer

    match = re.fullmatch(r"\$(request|response)\.(header|query|path)\.(.+)", expression)
    if match:
        source, location, name = match.groups()
        valid_name = (
            _HEADER_TOKEN.fullmatch(name) if location == "header" else _IDENTIFIER.fullmatch(name)
        )
        if valid_name is None:
            _error(f"Unsupported request or response reference: {expression}")
        return source, (location, name), None

    match = re.fullmatch(r"\$(inputs|outputs)\.([A-Za-z0-9._-]+)(?:#(.*))?", expression)
    if match:
        pointer = match.group(3)
        if pointer is not None and not _POINTER.fullmatch(pointer):
            _error(f"Invalid JSON Pointer in Runtime Expression: {expression}")
        return match.group(1), (match.group(2),), pointer

    match = re.fullmatch(
        r"\$steps\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9._-]+)(?:#(.*))?",
        expression,
    )
    if match:
        pointer = match.group(3)
        if pointer is not None and not _POINTER.fullmatch(pointer):
            _error(f"Invalid JSON Pointer in Runtime Expression: {expression}")
        return "steps", (match.group(1), match.group(2)), pointer

    match = re.fullmatch(
        r"\$workflows\.([A-Za-z0-9_-]+)\.(inputs|outputs)\.([A-Za-z0-9._-]+)(?:#(.*))?",
        expression,
    )
    if match:
        pointer = match.group(4)
        if pointer is not None and not _POINTER.fullmatch(pointer):
            _error(f"Invalid JSON Pointer in Runtime Expression: {expression}")
        return "workflows", (match.group(1), match.group(2), match.group(3)), pointer

    _error(f"Unsupported or malformed Arazzo Runtime Expression: {expression}")


def _without_string_literals(value: str) -> str:
    """Mask Arazzo single-quoted literals while preserving character offsets."""

    result = list(value)
    index = 0
    while index < len(value):
        if value[index] != "'":
            index += 1
            continue
        start = index
        index += 1
        while index < len(value):
            if value[index] != "'":
                index += 1
                continue
            if index + 1 < len(value) and value[index + 1] == "'":
                index += 2
                continue
            index += 1
            for position in range(start, index):
                result[position] = " "
            break
        else:
            _error("Arazzo simple condition has an unterminated string literal.")
    return "".join(result)


def _runtime_expressions(value: str, *, condition: bool = False) -> list[str]:
    """Return and syntax-check Runtime Expressions in a value or simple condition."""

    if re.fullmatch(r"\{\{[^{}]+\}\}", value.strip()):
        _error(
            "Double-brace template placeholders are not Arazzo Runtime Expressions; "
            "use a $inputs or $steps expression."
        )
    if condition:
        searchable = _without_string_literals(value)
        expressions = re.findall(r"\$[A-Za-z][^\s<>=!&|(),]*", searchable)
        for expression in expressions:
            parse_arazzo_runtime_expression(expression)
        if "$" in re.sub(r"\$[A-Za-z][^\s<>=!&|(),]*", "", searchable):
            _error(f"Malformed Arazzo Runtime Expression in condition: {value}")
        return expressions

    embedded = re.findall(r"\{(\$[^{}]+)\}", value)
    if "{$" in value and len(embedded) != value.count("{$"):
        _error(f"Malformed embedded Arazzo Runtime Expression: {value}")
    for expression in embedded:
        parse_arazzo_runtime_expression(expression)
    if embedded:
        return embedded
    if value.startswith(_KNOWN_RUNTIME_PREFIXES):
        parse_arazzo_runtime_expression(value)
        return [value]
    if value.startswith("$") and re.match(r"^\$[A-Za-z]", value):
        _error(f"Unsupported Arazzo Runtime Expression: {value}")
    return []


def _pointer(document: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        _error(f"External reference is not allowed: {ref}")
    value: Any = document
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or part not in value:
            _error(f"Local reference does not resolve: {ref}")
        value = value[part]
    return value


def _walk_safety(value: Any, document: dict[str, Any], path: str = "document") -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            _walk_safety(item, document, f"{path}[{index}]")
        return
    if not isinstance(value, dict):
        if isinstance(value, str) and _EVAL_CALL.search(value):
            _error(f"{path} uses eval, which is not allowed.")
        return
    for key, item in value.items():
        key_lower = key.lower()
        child = f"{path}.{key}"
        if key.startswith("x-") and key != "x-easydep-trace":
            _error(f"Unsupported extension at {child}; only x-easydep-trace is allowed.")
        if key == "x-easydep-trace" and re.fullmatch(r"document\.workflows\[\d+\]", path) is None:
            _error("x-easydep-trace is only allowed on Workflow Objects.")
        if key_lower in {"eval", "$eval", "javascript"}:
            _error(f"{child} is not allowed in an execution profile.")
        if key == "operationPath":
            _error(f"{child} is not allowed; use a resolved operationId.")
        if key_lower in {"async", "asyncapi"} or (
            key == "type" and isinstance(item, str) and item.lower() == "asyncapi"
        ):
            _error(f"{child} describes asynchronous execution, which is not supported.")
        if key == "$ref":
            if not isinstance(item, str):
                _error(f"{child} must be a local reference string.")
            _pointer(document, item)
        if key == "x-easydep-trace":
            _validate_trace(item, child)
        _walk_safety(item, document, child)


def _validate_trace(value: Any, path: str) -> None:
    if not isinstance(value, dict):
        _error(f"{path} must be an object.")
    if not value:
        _error(f"{path} must contain at least one trace field.")
    unknown = set(value) - _TRACE_KEYS
    if unknown:
        _error(f"{path} has unsupported fields: {', '.join(sorted(unknown))}.")
    for key, item in value.items():
        _string_list(item, f"{path}.{key}")


def _schema_properties(schema: Any) -> set[str]:
    if not isinstance(schema, dict):
        return set()
    properties = schema.get("properties")
    return set(properties) if isinstance(properties, dict) else set()


def _criterion_type(value: Any, path: str) -> str:
    if value is None or value == "simple":
        return "simple"
    if value == "jsonpath":
        return "jsonpath"
    if (
        isinstance(value, dict)
        and value.get("type") == "jsonpath"
        and value.get("version") == "rfc9535"
    ):
        return "jsonpath"
    _error(f"{path} is outside the EasyDep criterion profile; use simple or RFC 9535 JSONPath.")


def _validate_runtime_reference(
    expression: str,
    *,
    path: str,
    workflow_id: str,
    step_index: int | None,
    current_step: dict[str, Any] | None,
    workflow_inputs: dict[str, set[str]],
    workflow_outputs: dict[str, set[str]],
    workflow_steps: dict[str, list[str]],
    step_outputs: dict[str, dict[str, set[str]]],
) -> tuple[str, str] | None:
    namespace, parts, _pointer = parse_arazzo_runtime_expression(expression)
    if namespace == "inputs":
        if parts[0] not in workflow_inputs[workflow_id]:
            _error(f"{path} references an unknown workflow input: {expression}")
        return None
    if namespace == "outputs":
        if parts[0] not in workflow_outputs[workflow_id]:
            _error(f"{path} references an unknown workflow output: {expression}")
        return None
    if namespace == "steps":
        referenced_step, output_name = parts
        if referenced_step not in step_outputs[workflow_id]:
            _error(f"{path} references an unknown local step: {expression}")
        if output_name not in step_outputs[workflow_id][referenced_step]:
            _error(f"{path} references an unknown step output: {expression}")
        referenced_index = workflow_steps[workflow_id].index(referenced_step)
        if step_index is not None and referenced_index >= step_index:
            _error(f"{path} has a forward or self step-output reference: {expression}")
        return workflow_id, referenced_step
    if namespace == "workflows":
        target_workflow, field, name = parts
        if target_workflow not in workflow_steps:
            _error(f"{path} references an unknown workflow: {expression}")
        names = (
            workflow_inputs[target_workflow]
            if field == "inputs"
            else workflow_outputs[target_workflow]
        )
        if name not in names:
            _error(f"{path} references an unknown workflow {field[:-1]}: {expression}")
        return None
    if namespace in {"request", "response", "url", "method", "statusCode"}:
        if step_index is None:
            _error(f"{path} uses an HTTP Runtime Expression outside a step: {expression}")
        return None
    _error(f"{path} uses an unsupported Runtime Expression: {expression}")


def _validate_execution_value(
    value: Any,
    *,
    path: str,
    workflow_id: str,
    step_index: int | None,
    current_step: dict[str, Any] | None,
    workflow_inputs: dict[str, set[str]],
    workflow_outputs: dict[str, set[str]],
    workflow_steps: dict[str, list[str]],
    step_outputs: dict[str, dict[str, set[str]]],
    dependency_edges: dict[tuple[str, str], set[tuple[str, str]]],
) -> None:
    if isinstance(value, str):
        for expression in _runtime_expressions(value):
            source = _validate_runtime_reference(
                expression,
                path=path,
                workflow_id=workflow_id,
                step_index=step_index,
                current_step=current_step,
                workflow_inputs=workflow_inputs,
                workflow_outputs=workflow_outputs,
                workflow_steps=workflow_steps,
                step_outputs=step_outputs,
            )
            if source is not None and step_index is not None:
                dependency_edges.setdefault(source, set()).add(
                    (workflow_id, workflow_steps[workflow_id][step_index])
                )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_execution_value(
                item,
                path=f"{path}[{index}]",
                workflow_id=workflow_id,
                step_index=step_index,
                current_step=current_step,
                workflow_inputs=workflow_inputs,
                workflow_outputs=workflow_outputs,
                workflow_steps=workflow_steps,
                step_outputs=step_outputs,
                dependency_edges=dependency_edges,
            )
        return
    if not isinstance(value, dict):
        return
    if "reference" in value:
        _error(f"{path} uses a reusable component reference, which is outside the EasyDep profile.")
    if {"context", "selector"}.issubset(value):
        context = _nonempty_string(value.get("context"), f"{path}.context")
        expressions = _runtime_expressions(context)
        if expressions != [context]:
            _error(f"{path}.context must be one Runtime Expression.")
        _validate_runtime_reference(
            context,
            path=f"{path}.context",
            workflow_id=workflow_id,
            step_index=step_index,
            current_step=current_step,
            workflow_inputs=workflow_inputs,
            workflow_outputs=workflow_outputs,
            workflow_steps=workflow_steps,
            step_outputs=step_outputs,
        )
        selector_type = value.get("type")
        selector = _nonempty_string(value.get("selector"), f"{path}.selector")
        if selector_type == "jsonpath" or (
            isinstance(selector_type, dict)
            and selector_type.get("type") == "jsonpath"
            and selector_type.get("version") == "rfc9535"
        ):
            try:
                jsonpath_rfc9535.compile(selector)
            except jsonpath_rfc9535.JSONPathError as exc:
                _error(f"{path}.selector is not valid RFC 9535 JSONPath: {exc}")
            return
        if selector_type == "jsonpointer" or (
            isinstance(selector_type, dict)
            and selector_type.get("type") == "jsonpointer"
            and selector_type.get("version") == "rfc6901"
        ):
            if not _POINTER.fullmatch(selector):
                _error(f"{path}.selector is not a valid JSON Pointer.")
            return
        _error(f"{path}.type is outside the EasyDep selector profile.")
    for key, item in value.items():
        _validate_execution_value(
            item,
            path=f"{path}.{key}",
            workflow_id=workflow_id,
            step_index=step_index,
            current_step=current_step,
            workflow_inputs=workflow_inputs,
            workflow_outputs=workflow_outputs,
            workflow_steps=workflow_steps,
            step_outputs=step_outputs,
            dependency_edges=dependency_edges,
        )


def _validate_output_values(
    value: Any,
    *,
    path: str,
    workflow_id: str,
    step_index: int | None,
    current_step: dict[str, Any] | None,
    workflow_inputs: dict[str, set[str]],
    workflow_outputs: dict[str, set[str]],
    workflow_steps: dict[str, list[str]],
    step_outputs: dict[str, dict[str, set[str]]],
    dependency_edges: dict[tuple[str, str], set[tuple[str, str]]],
) -> None:
    """Require Arazzo outputs to be exact Runtime Expressions or Selector Objects."""

    if not isinstance(value, dict):
        _error(f"{path} must be an output map.")
    for name, item in value.items():
        item_path = f"{path}.{name}"
        if isinstance(item, str):
            expressions = _runtime_expressions(item)
            if expressions != [item]:
                _error(f"{item_path} must be one complete Arazzo Runtime Expression.")
        _validate_execution_value(
            item,
            path=item_path,
            workflow_id=workflow_id,
            step_index=step_index,
            current_step=current_step,
            workflow_inputs=workflow_inputs,
            workflow_outputs=workflow_outputs,
            workflow_steps=workflow_steps,
            step_outputs=step_outputs,
            dependency_edges=dependency_edges,
        )


def _validate_criteria(
    criteria: Any,
    *,
    path: str,
    workflow_id: str,
    step_index: int,
    current_step: dict[str, Any],
    workflow_inputs: dict[str, set[str]],
    workflow_outputs: dict[str, set[str]],
    workflow_steps: dict[str, list[str]],
    step_outputs: dict[str, dict[str, set[str]]],
    dependency_edges: dict[tuple[str, str], set[tuple[str, str]]],
) -> None:
    if criteria is None:
        return
    if not isinstance(criteria, list):
        _error(f"{path} must be a list of Criterion Objects.")
    for index, criterion in enumerate(criteria):
        criterion_path = f"{path}[{index}]"
        if not isinstance(criterion, dict):
            _error(f"{criterion_path} must be a Criterion Object.")
        kind = _criterion_type(criterion.get("type"), f"{criterion_path}.type")
        condition = _nonempty_string(criterion.get("condition"), f"{criterion_path}.condition")
        if kind == "simple":
            expressions = _runtime_expressions(condition, condition=True)
            if not expressions and condition not in {"true", "false"}:
                _error(f"{criterion_path}.condition has no verifiable Runtime Expression.")
        else:
            context = _nonempty_string(criterion.get("context"), f"{criterion_path}.context")
            context_expressions = _runtime_expressions(context)
            if context_expressions != [context]:
                _error(f"{criterion_path}.context must be one Runtime Expression.")
            expressions = context_expressions + _runtime_expressions(condition)
            compilable = re.sub(r"\{\$[^{}]+\}", "0", condition)
            try:
                jsonpath_rfc9535.compile(compilable)
            except jsonpath_rfc9535.JSONPathError as exc:
                _error(f"{criterion_path}.condition is not valid RFC 9535 JSONPath: {exc}")
        for expression in expressions:
            source = _validate_runtime_reference(
                expression,
                path=criterion_path,
                workflow_id=workflow_id,
                step_index=step_index,
                current_step=current_step,
                workflow_inputs=workflow_inputs,
                workflow_outputs=workflow_outputs,
                workflow_steps=workflow_steps,
                step_outputs=step_outputs,
            )
            if source is not None:
                dependency_edges.setdefault(source, set()).add(
                    (workflow_id, workflow_steps[workflow_id][step_index])
                )


def _openapi_operation_ids(openapi: dict[str, Any]) -> set[str]:
    if not isinstance(openapi, dict):
        _error("Frozen OpenAPI document must be an object.")
    paths = openapi.get("paths")
    if not isinstance(paths, dict):
        _error("Frozen OpenAPI document must contain a paths object.")
    counts: dict[str, int] = {}
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            if isinstance(operation_id, str) and operation_id.strip():
                counts[operation_id.strip()] = counts.get(operation_id.strip(), 0) + 1
    duplicates = sorted(key for key, count in counts.items() if count != 1)
    if duplicates:
        _error(f"Frozen OpenAPI operationId is ambiguous: {', '.join(duplicates)}")
    return set(counts)


def _resolved_operation(
    openapi: dict[str, Any], operation_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the unique frozen operation and its Path Item for profile checks."""
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    paths = openapi.get("paths")
    if not isinstance(paths, dict):
        _error("Frozen OpenAPI document must contain a paths object.")
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if (
                str(method).lower() in _HTTP_METHODS
                and isinstance(operation, dict)
                and operation.get("operationId") == operation_id
            ):
                matches.append((operation, path_item))
    if len(matches) != 1:
        _error(f"Frozen OpenAPI operationId does not resolve uniquely: {operation_id}")
    return matches[0]


def _validate_openapi_step_profile(
    step: dict[str, Any],
    path: str,
    operation: dict[str, Any],
    path_item: dict[str, Any],
    openapi: dict[str, Any],
) -> None:
    """Reject request shapes the deterministic HTTP primitive cannot execute."""
    declared: dict[tuple[str, str], Any] = {}
    for owner in (path_item, operation):
        for parameter in owner.get("parameters") or []:
            if not isinstance(parameter, dict):
                continue
            name, location = parameter.get("name"), parameter.get("in")
            if bool(parameter.get("required")) and location not in {"path", "query", "header"}:
                _error(f"{path} uses unsupported required OpenAPI parameter location: {location}")
            if isinstance(name, str) and isinstance(location, str):
                declared[(location, name)] = parameter.get("schema")
    for index, parameter in enumerate(step.get("parameters") or []):
        parameter_path = f"{path}.parameters[{index}]"
        if not isinstance(parameter, dict) or "reference" in parameter:
            _error(f"{parameter_path} must be an inline OpenAPI parameter.")
        location = parameter.get("in")
        if not isinstance(location, str):
            _error(f"{parameter_path}.in is required for an OpenAPI step.")
        if location not in {"path", "query", "header"}:
            _error(f"{parameter_path}.in must be path, query, or header.")
        name = _nonempty_string(parameter.get("name"), f"{parameter_path}.name")
        if (location, name) not in declared:
            _error(f"{parameter_path} is absent from the resolved OpenAPI operation.")
        value = parameter.get("value")
        expressions = _runtime_expressions(value) if isinstance(value, str) else []
        is_selector = isinstance(value, dict) and {"context", "selector", "type"}.issubset(value)
        parameter_schema = declared[(location, name)]
        if not expressions and not is_selector and isinstance(parameter_schema, dict):
            errors = schema_errors(openapi, parameter_schema, value)
            if errors:
                _error(
                    f"{parameter_path}.value does not satisfy the frozen OpenAPI schema: "
                    + "; ".join(errors)
                )
    request_contract = operation.get("requestBody")
    content = request_contract.get("content") if isinstance(request_contract, dict) else None
    if (
        isinstance(request_contract, dict)
        and content is not None
        and (not isinstance(content, dict) or "application/json" not in content)
    ):
        _error(f"{path} uses a non-JSON OpenAPI request body, which is outside the profile.")
    request_body = step.get("requestBody")
    if request_body is None:
        return
    if not isinstance(request_body, dict):
        _error(f"{path}.requestBody must be an object.")
    content_type = request_body.get("contentType")
    if content_type is not None and content_type != "application/json":
        _error(f"{path}.requestBody.contentType must be application/json.")
    if not isinstance(content, dict) or "application/json" not in content:
        _error(f"{path}.requestBody requires an application/json OpenAPI request body.")
    replacements = request_body.get("replacements") or []
    if not isinstance(replacements, list):
        _error(f"{path}.requestBody.replacements must be a list.")
    for index, replacement in enumerate(replacements):
        replacement_path = f"{path}.requestBody.replacements[{index}]"
        if not isinstance(replacement, dict):
            _error(f"{replacement_path} must be an object.")
        selector_type = replacement.get("targetSelectorType", "jsonpointer")
        if isinstance(selector_type, dict):
            selector_type = selector_type.get("type")
        if selector_type != "jsonpointer":
            _error(f"{replacement_path}.targetSelectorType must be jsonpointer.")
        target = _nonempty_string(replacement.get("target"), f"{replacement_path}.target")
        if _POINTER.fullmatch(target.removeprefix("#")) is None:
            _error(f"{replacement_path}.target must be a JSON Pointer.")
    payload = request_body.get("payload")
    media = content.get("application/json") if isinstance(content, dict) else None
    body_schema = media.get("schema") if isinstance(media, dict) else None
    # Runtime expressions and replacements are resolved by the executor. A
    # fully literal body can be checked now, while the rejected workflow is
    # still available to the bounded LLM correction pass.
    has_runtime_value = any(
        isinstance(value, str) and value.startswith("$")
        for value in _walk_values(payload)
    )
    if not replacements and not has_runtime_value and isinstance(body_schema, dict):
        errors = schema_errors(openapi, body_schema, payload)
        if errors:
            _error(
                f"{path}.requestBody.payload does not satisfy the frozen OpenAPI schema: "
                + "; ".join(errors)
            )


def _actions(value: Any, path: str) -> list[tuple[dict[str, Any], str]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        _error(f"{path} must be a list of action objects.")
    return [(item, f"{path}[{index}]") for index, item in enumerate(value)]


def _validate_action(
    action: dict[str, Any],
    path: str,
    workflow_id: str,
    workflow_steps: dict[str, set[str]],
    workflow_order: dict[str, list[str]],
    edges: dict[tuple[str, str], set[tuple[str, str]]],
) -> None:
    if "reference" in action:
        _error(f"{path} uses a reusable action, which is outside the EasyDep profile.")
    action_type = _nonempty_string(action.get("type"), f"{path}.type").lower()
    target_workflow = action.get("workflowId", workflow_id)
    if not isinstance(target_workflow, str) or target_workflow not in workflow_steps:
        _error(f"{path}.workflowId does not resolve to a workflow.")
    if action_type == "retry":
        retry_limit = action.get("retryLimit", 1)
        if (
            isinstance(retry_limit, bool)
            or not isinstance(retry_limit, int)
            or not 0 <= retry_limit <= _MAX_RETRY_LIMIT
        ):
            _error(f"{path}.retryLimit must be an integer from 0 to {_MAX_RETRY_LIMIT}.")
        unsupported = {"workflowId", "stepId", "parameters", "retryAfter"}.intersection(action)
        if unsupported:
            _error(
                f"{path} retry only retries its current step; unsupported fields: "
                f"{', '.join(sorted(unsupported))}."
            )
        return
    if action_type == "goto":
        has_workflow = "workflowId" in action
        has_step = "stepId" in action
        if has_workflow == has_step:
            _error(f"{path} goto must reference exactly one workflowId or stepId.")
        if has_workflow:
            target = (target_workflow, workflow_order[target_workflow][0])
        else:
            step_id = _nonempty_string(action.get("stepId"), f"{path}.stepId")
            if step_id not in workflow_steps[workflow_id]:
                _error(f"{path}.stepId does not resolve in workflow {workflow_id}.")
            target = (workflow_id, step_id)
        # The source step is attached by the caller after its own identity is known.
        action["__easydep_target"] = target
        return
    if action_type != "end":
        _error(f"{path}.type {action_type!r} is not supported by the synchronous profile.")
    irrelevant = {"workflowId", "stepId", "parameters", "retryAfter", "retryLimit"}.intersection(
        action
    )
    if irrelevant:
        _error(f"{path} end action has irrelevant fields: {', '.join(sorted(irrelevant))}.")


def _step_dependency(
    value: Any,
    *,
    path: str,
    workflow_id: str,
    workflow_steps: dict[str, set[str]],
) -> tuple[str, str]:
    """Resolve Arazzo's local step dependency syntax to one graph node."""
    if not isinstance(value, str) or not value:
        _error(f"{path} must be a step reference string.")
    if value.startswith("$sourceDescriptions."):
        _error(f"{path} references an external Arazzo source, which is not allowed.")
    if value.startswith("$workflows."):
        parts = value.split(".")
        if len(parts) != 4 or parts[0] != "$workflows" or parts[2] != "steps":
            _error(f"{path} is not a valid local workflow step reference.")
        target_workflow, step_id = parts[1], parts[3]
    else:
        target_workflow, step_id = workflow_id, value
    if target_workflow not in workflow_steps or step_id not in workflow_steps[target_workflow]:
        _error(f"{path} does not resolve to a workflow step.")
    return target_workflow, step_id


def _assert_acyclic(edges: dict[tuple[str, str], set[tuple[str, str]]]) -> None:
    visiting: set[tuple[str, str]] = set()
    visited: set[tuple[str, str]] = set()

    def visit(node: tuple[str, str]) -> None:
        if node in visiting:
            _error("Workflow control flow contains a cycle.")
        if node in visited:
            return
        visiting.add(node)
        for child in edges.get(node, ()):
            visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in edges:
        visit(node)


def _assert_workflow_call_depth(edges: dict[str, set[str]]) -> None:
    visiting: set[str] = set()

    def visit(workflow_id: str, depth: int) -> None:
        if workflow_id in visiting:
            _error("Local workflow calls contain a recursion cycle.")
        if depth > _MAX_WORKFLOW_DEPTH:
            _error(f"Local workflow call depth exceeds {_MAX_WORKFLOW_DEPTH}.")
        visiting.add(workflow_id)
        for child in edges.get(workflow_id, ()):
            visit(child, depth + 1)
        visiting.remove(workflow_id)

    for workflow_id in edges:
        visit(workflow_id, 1)


def validate_arazzo_document(
    document: Any,
    *,
    openapi: dict[str, Any],
    trace_catalog: Mapping[str, Collection[str]] | None = None,
) -> dict[str, Any]:
    """Validate and defensively copy an executable EasyDep Arazzo v1.1 document."""
    copied = _json_copy(document)
    if not isinstance(copied, dict):
        _error("Arazzo document must be an object.")
    _validate_jsonschema(copied)
    _walk_safety(copied, copied)

    if copied.get("arazzo") != ARAZZO_VERSION:
        _error(f"arazzo must be {ARAZZO_VERSION!r}.")
    sources = copied.get("sourceDescriptions")
    if not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict):
        _error("Exactly one local OpenAPI source description is required.")
    source_description = sources[0]
    if (
        source_description.get("name") != "application"
        or source_description.get("url") != "openapi.json"
        or source_description.get("type") != "openapi"
    ):
        _error("The only source must be application / openapi.json / openapi.")

    operation_ids = _openapi_operation_ids(openapi)
    workflows = copied.get("workflows")
    if not isinstance(workflows, list) or not 1 <= len(workflows) <= _MAX_WORKFLOWS:
        _error(f"workflows must contain from 1 to {_MAX_WORKFLOWS} entries.")
    workflow_steps: dict[str, set[str]] = {}
    workflow_order: dict[str, list[str]] = {}
    workflow_inputs: dict[str, set[str]] = {}
    workflow_outputs: dict[str, set[str]] = {}
    step_outputs: dict[str, dict[str, set[str]]] = {}
    workflow_values: list[tuple[str, dict[str, Any], str]] = []
    total_steps = 0
    for index, workflow in enumerate(workflows):
        path = f"workflows[{index}]"
        if not isinstance(workflow, dict):
            _error(f"{path} must be an object.")
        workflow_id = _nonempty_string(workflow.get("workflowId"), f"{path}.workflowId")
        if _STRICT_IDENTIFIER.fullmatch(workflow_id) is None:
            _error(f"{path}.workflowId must match [A-Za-z0-9_-]+ in the executable profile.")
        if workflow_id in workflow_steps:
            _error(f"Duplicate workflowId: {workflow_id}")
        steps = workflow.get("steps")
        if not isinstance(steps, list) or not steps:
            _error(f"{path}.steps must be a non-empty list.")
        ids: list[str] = []
        for step_index, step in enumerate(steps):
            if not isinstance(step, dict):
                _error(f"{path}.steps[{step_index}] must be an object.")
            step_id = _nonempty_string(step.get("stepId"), f"{path}.steps[{step_index}].stepId")
            if _STRICT_IDENTIFIER.fullmatch(step_id) is None:
                _error(
                    f"{path}.steps[{step_index}].stepId must match [A-Za-z0-9_-]+ in the executable profile."
                )
            if step_id in ids:
                _error(f"Duplicate stepId {step_id!r} in workflow {workflow_id}.")
            ids.append(step_id)
        total_steps += len(ids)
        workflow_steps[workflow_id] = set(ids)
        workflow_order[workflow_id] = ids
        workflow_inputs[workflow_id] = _schema_properties(workflow.get("inputs"))
        workflow_outputs[workflow_id] = set(workflow.get("outputs") or {})
        step_outputs[workflow_id] = {
            step_id: set(step.get("outputs") or {})
            for step_id, step in zip(ids, steps, strict=True)
        }
        workflow_values.append((workflow_id, workflow, path))
    if total_steps > _MAX_STEPS:
        _error(f"A document may contain at most {_MAX_STEPS} steps.")

    edges: dict[tuple[str, str], set[tuple[str, str]]] = {}
    workflow_calls: dict[str, set[str]] = {workflow_id: set() for workflow_id in workflow_steps}
    for workflow_id, workflow, path in workflow_values:
        if workflow.get("parameters"):
            _error(
                f"{path}.parameters is outside the initial EasyDep profile; "
                "declare parameters on each operation step."
            )
        trace = workflow.get("x-easydep-trace")
        if isinstance(trace, dict) and trace_catalog is not None:
            for key, values in trace.items():
                allowed = set(trace_catalog.get(key, ()))
                unknown = sorted(set(values) - allowed)
                if unknown:
                    _error(
                        f"{path}.x-easydep-trace.{key} contains unknown IDs: {', '.join(unknown)}"
                    )
        depends_on = workflow.get("dependsOn", [])
        if not isinstance(depends_on, list):
            _error(f"{path}.dependsOn must be a list of workflowIds.")
        for dependency in depends_on:
            if not isinstance(dependency, str) or dependency not in workflow_steps:
                _error(f"{path}.dependsOn contains an unknown workflowId.")
            edges.setdefault((dependency, workflow_order[dependency][-1]), set()).add(
                (workflow_id, workflow_order[workflow_id][0])
            )
        steps = workflow["steps"]
        _validate_execution_value(
            workflow.get("parameters") or [],
            path=f"{path}.parameters",
            workflow_id=workflow_id,
            step_index=None,
            current_step=None,
            workflow_inputs=workflow_inputs,
            workflow_outputs=workflow_outputs,
            workflow_steps=workflow_order,
            step_outputs=step_outputs,
            dependency_edges=edges,
        )
        for step_index, step in enumerate(steps):
            step_path = f"{path}.steps[{step_index}]"
            if "timeout" in step:
                _error(
                    f"{step_path}.timeout is outside the initial EasyDep profile; "
                    "the runner uses its configured request timeout."
                )
            if "operationId" in step:
                operation_id = _nonempty_string(step.get("operationId"), f"{step_path}.operationId")
                if operation_id not in operation_ids:
                    _error(
                        f"{step_path}.operationId does not resolve uniquely in frozen OpenAPI: {operation_id}"
                    )
                operation, path_item = _resolved_operation(openapi, operation_id)
                _validate_openapi_step_profile(step, step_path, operation, path_item, openapi)
            elif "workflowId" in step:
                target_workflow = _nonempty_string(
                    step.get("workflowId"), f"{step_path}.workflowId"
                )
                if target_workflow not in workflow_steps:
                    _error(f"{step_path}.workflowId does not resolve to a local workflow.")
                unsupported = {"successCriteria", "onSuccess", "onFailure"}.intersection(step)
                if unsupported or workflow.get("successActions") or workflow.get("failureActions"):
                    fields = ", ".join(sorted(unsupported)) or "workflow default actions"
                    _error(
                        f"{step_path} applies unsupported control fields to a local workflow "
                        f"call: {fields}."
                    )
                if "requestBody" in step:
                    _error(
                        f"{step_path}.requestBody is not valid for a workflow step in this profile."
                    )
                workflow_calls[workflow_id].add(target_workflow)
                for parameter_index, parameter in enumerate(step.get("parameters") or []):
                    if not isinstance(parameter, dict) or "reference" in parameter:
                        _error(
                            f"{step_path}.parameters[{parameter_index}] must be an inline workflow parameter."
                        )
                    if "in" in parameter:
                        _error(
                            f"{step_path}.parameters[{parameter_index}].in is not valid for a workflow step."
                        )
                    name = _nonempty_string(
                        parameter.get("name"), f"{step_path}.parameters[{parameter_index}].name"
                    )
                    if name not in workflow_inputs[target_workflow]:
                        _error(
                            f"{step_path}.parameters[{parameter_index}] references an unknown input of {target_workflow}."
                        )
            else:
                _error(f"{step_path} is not an executable OpenAPI or local workflow step.")
            source_node = (workflow_id, workflow_order[workflow_id][step_index])
            edges.setdefault(source_node, set())
            if step_index + 1 < len(steps):
                edges[source_node].add((workflow_id, workflow_order[workflow_id][step_index + 1]))
            step_dependencies = step.get("dependsOn", [])
            if not isinstance(step_dependencies, list):
                _error(f"{step_path}.dependsOn must be a list of step references.")
            for dependency_index, dependency in enumerate(step_dependencies):
                prerequisite = _step_dependency(
                    dependency,
                    path=f"{step_path}.dependsOn[{dependency_index}]",
                    workflow_id=workflow_id,
                    workflow_steps=workflow_steps,
                )
                edges.setdefault(prerequisite, set()).add(source_node)
            for key in ("parameters", "requestBody"):
                if key in step:
                    _validate_execution_value(
                        step[key],
                        path=f"{step_path}.{key}",
                        workflow_id=workflow_id,
                        step_index=step_index,
                        current_step=step,
                        workflow_inputs=workflow_inputs,
                        workflow_outputs=workflow_outputs,
                        workflow_steps=workflow_order,
                        step_outputs=step_outputs,
                        dependency_edges=edges,
                    )
            if "outputs" in step:
                _validate_output_values(
                    step["outputs"],
                    path=f"{step_path}.outputs",
                    workflow_id=workflow_id,
                    step_index=step_index,
                    current_step=step,
                    workflow_inputs=workflow_inputs,
                    workflow_outputs=workflow_outputs,
                    workflow_steps=workflow_order,
                    step_outputs=step_outputs,
                    dependency_edges=edges,
                )
            _validate_criteria(
                step.get("successCriteria"),
                path=f"{step_path}.successCriteria",
                workflow_id=workflow_id,
                step_index=step_index,
                current_step=step,
                workflow_inputs=workflow_inputs,
                workflow_outputs=workflow_outputs,
                workflow_steps=workflow_order,
                step_outputs=step_outputs,
                dependency_edges=edges,
            )
            for key in ("onSuccess", "onFailure"):
                for action, action_path in _actions(step.get(key), f"{step_path}.{key}"):
                    _validate_action(
                        action, action_path, workflow_id, workflow_steps, workflow_order, edges
                    )
                    _validate_execution_value(
                        action.get("parameters") or [],
                        path=f"{action_path}.parameters",
                        workflow_id=workflow_id,
                        step_index=step_index,
                        current_step=step,
                        workflow_inputs=workflow_inputs,
                        workflow_outputs=workflow_outputs,
                        workflow_steps=workflow_order,
                        step_outputs=step_outputs,
                        dependency_edges=edges,
                    )
                    _validate_criteria(
                        action.get("criteria"),
                        path=f"{action_path}.criteria",
                        workflow_id=workflow_id,
                        step_index=step_index,
                        current_step=step,
                        workflow_inputs=workflow_inputs,
                        workflow_outputs=workflow_outputs,
                        workflow_steps=workflow_order,
                        step_outputs=step_outputs,
                        dependency_edges=edges,
                    )
                    target = action.pop("__easydep_target", None)
                    if target is not None:
                        edges[source_node].add(target)
        for key in ("successActions", "failureActions"):
            for action, action_path in _actions(workflow.get(key), f"{path}.{key}"):
                _validate_action(
                    action, action_path, workflow_id, workflow_steps, workflow_order, edges
                )
                final_index = len(steps) - 1
                _validate_execution_value(
                    action.get("parameters") or [],
                    path=f"{action_path}.parameters",
                    workflow_id=workflow_id,
                    step_index=final_index,
                    current_step=steps[final_index],
                    workflow_inputs=workflow_inputs,
                    workflow_outputs=workflow_outputs,
                    workflow_steps=workflow_order,
                    step_outputs=step_outputs,
                    dependency_edges=edges,
                )
                _validate_criteria(
                    action.get("criteria"),
                    path=f"{action_path}.criteria",
                    workflow_id=workflow_id,
                    step_index=final_index,
                    current_step=steps[final_index],
                    workflow_inputs=workflow_inputs,
                    workflow_outputs=workflow_outputs,
                    workflow_steps=workflow_order,
                    step_outputs=step_outputs,
                    dependency_edges=edges,
                )
                # Workflow-level goto starts from the final step, so it is also bounded by the DAG check.
                target = action.pop("__easydep_target", None)
                if target is not None:
                    edges[(workflow_id, workflow_order[workflow_id][-1])].add(target)
        _validate_output_values(
            workflow.get("outputs") or {},
            path=f"{path}.outputs",
            workflow_id=workflow_id,
            step_index=None,
            current_step=None,
            workflow_inputs=workflow_inputs,
            workflow_outputs=workflow_outputs,
            workflow_steps=workflow_order,
            step_outputs=step_outputs,
            dependency_edges=edges,
        )
    _assert_acyclic(edges)
    _assert_workflow_call_depth(workflow_calls)
    return copy.deepcopy(copied)


__all__ = ["ARAZZO_VERSION", "ArazzoValidationError", "validate_arazzo_document"]
