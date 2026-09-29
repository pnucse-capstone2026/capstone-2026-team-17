"""Deterministic executor for EasyDep's deliberately small Arazzo profile."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import jsonschema

from app.testing.progress import emit_dynamic_workflow_terminal, emit_testing_progress
from app.testing.schemas.arazzo import ArazzoValidationError, validate_arazzo_document
from app.testing.utils.arazzo_expression import (
    ArazzoExpressionError,
    interpolate,
    resolve,
    select_jsonpath,
)
from app.testing.utils.functional_executor import (
    InputValueProposer,
    InputValueRequest,
    Operation,
    UpstreamAmbiguity,
    operation_for_id,
    operation_url,
    resolve_schema,
    response_summary,
    schema_errors,
    send_operation_request,
    validate_operation_response,
)

_MAX_CALL_DEPTH = 8
class _ExecutionError(ValueError):
    def __init__(self, code: str, message: str, *, defect_class: str = "TEST_DEFECT") -> None:
        super().__init__(message)
        self.code = code
        self.defect_class = defect_class


def _json_pointer(value: Any, pointer: str) -> Any:
    if pointer in {"", "#"}:
        return value
    pointer = pointer.removeprefix("#")
    if not pointer.startswith("/"):
        raise _ExecutionError("UNSUPPORTED_SELECTOR", f"Unsupported JSON Pointer: {pointer}")
    current = value
    for raw in pointer[1:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise _ExecutionError(
                "RUNTIME_EXPRESSION_UNRESOLVED", f"JSON Pointer does not resolve: {pointer}"
            )
    return current


def _set_pointer(value: Any, pointer: str, replacement: Any) -> None:
    pointer = pointer.removeprefix("#")
    if not pointer.startswith("/"):
        raise _ExecutionError(
            "UNSUPPORTED_REPLACEMENT", f"Only JSON Pointer replacements are supported: {pointer}"
        )
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
    current = value
    for part in parts[:-1]:
        if isinstance(current, dict):
            current = current.setdefault(part, {})
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise _ExecutionError(
                "UNSUPPORTED_REPLACEMENT", f"Replacement target does not resolve: {pointer}"
            )
    last = parts[-1]
    if isinstance(current, dict):
        current[last] = replacement
    elif isinstance(current, list) and last.isdigit() and int(last) < len(current):
        current[int(last)] = replacement
    else:
        raise _ExecutionError(
            "UNSUPPORTED_REPLACEMENT", f"Replacement target does not resolve: {pointer}"
        )


def _resolve_value(value: Any, context: Mapping[str, Any]) -> Any:
    if isinstance(value, dict) and {"context", "selector", "type"}.issubset(value):
        selected_context = resolve(str(value["context"]), context)
        selector_type = value["type"]
        if isinstance(selector_type, dict):
            selector_type = selector_type.get("type")
        if selector_type == "jsonpath":
            selected = select_jsonpath(selected_context, str(value["selector"]))
            return selected[0] if len(selected) == 1 else selected
        if selector_type == "jsonpointer":
            return _json_pointer(selected_context, str(value["selector"]))
        raise _ExecutionError("UNSUPPORTED_SELECTOR", f"Unsupported selector type: {selector_type}")
    if isinstance(value, dict):
        return {key: _resolve_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_value(item, context) for item in value]
    try:
        return interpolate(value, context)
    except ArazzoExpressionError as exc:
        raise _ExecutionError("RUNTIME_EXPRESSION_UNRESOLVED", str(exc)) from exc


def _literal_or_expression(value: str, context: Mapping[str, Any]) -> Any:
    if value.startswith("$"):
        return _resolve_value(value, context)
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.lower() == "null":
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value.strip("'\"")


def _criterion(criterion: dict[str, Any], context: Mapping[str, Any]) -> bool:
    kind = criterion.get("type", "simple")
    if isinstance(kind, dict):
        kind = kind.get("type")
    condition = criterion.get("condition")
    if not isinstance(condition, str):
        raise _ExecutionError(
            "INVALID_CRITERION", "A success criterion requires a string condition."
        )
    if kind == "jsonpath":
        if not isinstance(criterion.get("context"), str):
            raise _ExecutionError(
                "INVALID_CRITERION", "A JSONPath criterion requires a runtime-expression context."
            )
        try:
            selected_context = resolve(criterion["context"], context)
            resolved_condition = interpolate(condition, context) if "{" in condition else condition
            if not isinstance(resolved_condition, str):
                raise _ExecutionError(
                    "INVALID_CRITERION",
                    "A JSONPath condition must resolve to a string.",
                )
            values = select_jsonpath(selected_context, resolved_condition)
        except ArazzoExpressionError as exc:
            raise _ExecutionError("INVALID_CRITERION", str(exc)) from exc
        # JSONPath criteria assert that the selector matched; false/zero are
        # legitimate JSON nodes and must not be confused with no match.
        return bool(values)
    if kind != "simple":
        raise _ExecutionError("UNSUPPORTED_CRITERION", f"Unsupported criterion type: {kind}")
    return _simple_criterion(condition, context)


def _criteria(criteria: Any, context: Mapping[str, Any]) -> bool:
    return all(item["passed"] for item in _criterion_results(criteria, context))


def _criterion_results(criteria: Any, context: Mapping[str, Any]) -> list[dict[str, Any]]:
    if criteria is None:
        return []
    if not isinstance(criteria, list):
        raise _ExecutionError("INVALID_CRITERION", "Criteria must be a list.")
    if any(not isinstance(item, dict) for item in criteria):
        raise _ExecutionError("INVALID_CRITERION", "Each criterion must be an object.")
    return [
        {"criterion": copy.deepcopy(item), "passed": _criterion(item, context)} for item in criteria
    ]


def _explicit_non_success_status_expected(
    criterion_results: list[dict[str, Any]], status_code: int
) -> bool:
    """Allow a documented negative test only when it explicitly asserts the status.

    Ordinary workflow success always requires a 2xx response.  A negative test
    may instead assert an expected non-2xx response (for example, ``$statusCode
    == 400``).  Other criteria, including output comparisons, must never turn a
    server error into a successful workflow by themselves.
    """

    if 200 <= status_code < 300:
        return False
    return any(
        item["passed"]
        and isinstance(item.get("criterion"), dict)
        and "$statusCode" in str(item["criterion"].get("condition") or "")
        for item in criterion_results
    )


def _split_top_level(value: str, token: str) -> list[str]:
    """Split a simple condition at top-level operators, without parsing code."""
    parts: list[str] = []
    start = depth = 0
    quote = ""
    escaped = False
    index = 0
    while index < len(value):
        char = value[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise _ExecutionError(
                    "INVALID_CRITERION", "Simple criterion has unbalanced parentheses."
                )
        elif depth == 0 and value.startswith(token, index):
            parts.append(value[start:index].strip())
            start = index + len(token)
            index += len(token) - 1
        index += 1
    if quote or depth != 0:
        raise _ExecutionError(
            "INVALID_CRITERION", "Simple criterion has unbalanced quotes or parentheses."
        )
    parts.append(value[start:].strip())
    return parts


def _simple_criterion(condition: str, context: Mapping[str, Any]) -> bool:
    value = condition.strip()
    while value.startswith("(") and value.endswith(")"):
        depth = 0
        closing = -1
        quote = ""
        escaped = False
        for index, char in enumerate(value):
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
            elif char in {"'", '"'}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    closing = index
                    break
        if closing != len(value) - 1:
            break
        value = value[1:-1].strip()
    alternatives = _split_top_level(value, "||")
    if len(alternatives) > 1:
        return any(_simple_criterion(item, context) for item in alternatives)
    conjunctions = _split_top_level(value, "&&")
    if len(conjunctions) > 1:
        return all(_simple_criterion(item, context) for item in conjunctions)
    if value.startswith("!") and not value.startswith("!="):
        return not _simple_criterion(value[1:].strip(), context)
    comparison: tuple[str, str, str] | None = None
    for operator in ("==", "!=", ">=", "<=", ">", "<"):
        operands = _split_top_level(value, operator)
        if len(operands) == 2:
            comparison = (operands[0], operator, operands[1])
            break
        if len(operands) > 2:
            raise _ExecutionError(
                "INVALID_CRITERION", "Simple criterion contains a chained comparison."
            )
    if comparison is None:
        return bool(_literal_or_expression(value, context))
    left, operator, right = comparison
    left_value = _literal_or_expression(left.strip(), context)
    right_value = _literal_or_expression(right.strip(), context)
    if isinstance(left_value, str) and isinstance(right_value, str):
        left_value, right_value = left_value.casefold(), right_value.casefold()
    elif operator in {">", ">=", "<", "<="}:
        numeric_values: list[Decimal] = []
        for operand in (left_value, right_value):
            if isinstance(operand, bool) or not isinstance(operand, (int, float, str)):
                break
            try:
                numeric_values.append(Decimal(str(operand)))
            except InvalidOperation:
                break
        if len(numeric_values) == 2:
            left_value, right_value = numeric_values
    try:
        return {
            "==": left_value == right_value,
            "!=": left_value != right_value,
            ">": left_value > right_value,
            ">=": left_value >= right_value,
            "<": left_value < right_value,
            "<=": left_value <= right_value,
        }[operator]
    except TypeError as exc:
        raise _ExecutionError(
            "INVALID_CRITERION", f"Criterion operands cannot be compared: {condition}"
        ) from exc


def _first_action(
    step: dict[str, Any], workflow: dict[str, Any], key: str, context: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Select the first eligible step action, falling back to workflow defaults."""
    candidates = step.get(key)
    if candidates is None:
        candidates = workflow.get("successActions" if key == "onSuccess" else "failureActions", [])
    if not isinstance(candidates, list):
        raise _ExecutionError("INVALID_ACTION", f"{key} must be an action list.")
    for action in candidates:
        if isinstance(action, dict) and _criteria(action.get("criteria"), context):
            return action
    return None


def _action_inputs(action: dict[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        item["name"]: _resolve_value(item.get("value"), context)
        for item in action.get("parameters") or []
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def _schema_value(
    schema: dict[str, Any],
    supplied: Any,
    *,
    has_supplied: bool,
    location: str,
    operation_id: str,
    proposer: InputValueProposer | None,
    schema_document: dict[str, Any] | None = None,
    require_explicit_values: bool = False,
) -> Any:
    if schema_document is not None:
        schema = resolve_schema(schema_document, schema)
    if has_supplied:
        return supplied
    if require_explicit_values:
        raise _ExecutionError(
            "INPUT_VALUE_UNAVAILABLE",
            f"No explicit value was provided for {location}.",
        )
    for key in ("const", "default", "example"):
        if key in schema:
            return copy.deepcopy(schema[key])
    examples = schema.get("examples")
    if isinstance(examples, list) and examples:
        return copy.deepcopy(examples[0])
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return copy.deepcopy(enum[0])
    schema_type = schema.get("type")
    if schema_type == "object" or isinstance(schema.get("properties"), dict):
        properties: dict[str, Any] = (
            schema["properties"] if isinstance(schema.get("properties"), dict) else {}
        )
        required = set(schema.get("required") or [])
        return {
            name: _schema_value(
                child,
                None,
                has_supplied=False,
                location=f"{location}.{name}",
                operation_id=operation_id,
                proposer=proposer,
                schema_document=schema_document,
            )
            for name, child in properties.items()
            if isinstance(child, dict)
            and (
                name in required
                or any(key in child for key in ("const", "default", "example", "enum"))
            )
        }
    if schema_type == "array":
        if schema.get("minItems", 0) == 0:
            return []
    if schema_type == "null":
        return None
    if proposer is None:
        raise _ExecutionError(
            "INPUT_VALUE_UNAVAILABLE", f"No deterministic value is available for {location}."
        )
    try:
        return proposer(
            InputValueRequest(operation_id=operation_id, location=location, schema=schema)
        )
    except (TypeError, ValueError) as exc:
        raise _ExecutionError(
            "INPUT_VALUE_INVALID", f"Input proposer failed for {location}: {exc}"
        ) from exc


def _validated_schema_value(
    schema: dict[str, Any], value: Any, *, location: str, openapi: dict[str, Any]
) -> Any:
    errors = schema_errors(openapi, schema, value)
    if errors:
        raise _ExecutionError("INPUT_VALUE_INVALID", f"Input {location} is invalid: {errors[0]}")
    return value


def _inline_arazzo_schema(
    document: dict[str, Any], schema: dict[str, Any], seen: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Expand only local reusable input schemas before JSON Schema validation."""
    reference = schema.get("$ref")
    if isinstance(reference, str):
        if reference in seen or not reference.startswith("#/"):
            raise _ExecutionError(
                "WORKFLOW_INPUT_INVALID",
                f"Workflow input schema reference cannot be resolved: {reference}",
            )
        target: Any = document
        for raw in reference[2:].split("/"):
            part = raw.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                raise _ExecutionError(
                    "WORKFLOW_INPUT_INVALID",
                    f"Workflow input schema reference cannot be resolved: {reference}",
                )
            target = target[part]
        if not isinstance(target, dict):
            raise _ExecutionError(
                "WORKFLOW_INPUT_INVALID",
                f"Workflow input schema reference is not an object: {reference}",
            )
        return _inline_arazzo_schema(
            document,
            {**target, **{key: value for key, value in schema.items() if key != "$ref"}},
            seen | {reference},
        )
    return {
        key: _inline_arazzo_schema(document, value, seen)
        if isinstance(value, dict)
        else [
            _inline_arazzo_schema(document, item, seen) if isinstance(item, dict) else item
            for item in value
        ]
        if isinstance(value, list)
        else value
        for key, value in schema.items()
    }


def _workflow_inputs(
    workflow: dict[str, Any],
    supplied: Mapping[str, Any] | None,
    proposer: InputValueProposer | None,
    document: dict[str, Any],
    require_explicit_values: bool = False,
) -> dict[str, Any]:
    if supplied is not None and not isinstance(supplied, Mapping):
        raise _ExecutionError("WORKFLOW_INPUT_INVALID", "workflow_inputs must be a mapping.")
    schema = workflow.get("inputs")
    values = dict(supplied or {})
    if schema is None:
        return values
    if not isinstance(schema, dict):
        raise _ExecutionError(
            "WORKFLOW_INPUT_INVALID", "Workflow inputs must be a JSON Schema object."
        )
    schema = _inline_arazzo_schema(document, schema)
    properties: dict[str, Any] = (
        schema["properties"] if isinstance(schema.get("properties"), dict) else {}
    )
    required = set(schema.get("required") or [])
    for name, child in properties.items():
        if not isinstance(child, dict):
            continue
        if name in values:
            continue
        if name in required or (
            not require_explicit_values
            and any(key in child for key in ("const", "default", "example", "enum"))
        ):
            values[name] = _schema_value(
                child,
                None,
                has_supplied=False,
                location=f"inputs.{name}",
                operation_id=f"workflow:{workflow.get('workflowId', '')}",
                proposer=proposer,
                require_explicit_values=require_explicit_values,
            )
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(values), key=str)
    if errors:
        raise _ExecutionError("WORKFLOW_INPUT_INVALID", f"Workflow inputs are invalid: {errors[0]}")
    return values


def _parameters(
    operation: Operation,
    step: dict[str, Any],
    context: Mapping[str, Any],
    openapi: dict[str, Any],
    proposer: InputValueProposer | None,
    require_explicit_values: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Any]:
    declared: dict[tuple[str, str], Any] = {}
    for item in step.get("parameters") or []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        where = item.get("in")
        if isinstance(where, str):
            declared[(where, item["name"])] = _resolve_value(item.get("value"), context)
    paths: dict[str, Any] = {}
    query: dict[str, Any] = {}
    headers: dict[str, Any] = {}
    seen: set[tuple[str, str]] = set()
    for owner in (operation.path_item, operation.value):
        for parameter in owner.get("parameters") or []:
            if not isinstance(parameter, dict):
                continue
            name, where, schema = (
                parameter.get("name"),
                parameter.get("in"),
                parameter.get("schema"),
            )
            if (
                not isinstance(name, str)
                or not isinstance(where, str)
                or not isinstance(schema, dict)
                or (where, name) in seen
            ):
                continue
            seen.add((where, name))
            required = bool(parameter.get("required")) or where == "path"
            present = (where, name) in declared
            if not required and not present:
                continue
            value = _schema_value(
                schema,
                declared.get((where, name)),
                has_supplied=present,
                location=f"{where}.{name}",
                operation_id=operation.operation_id,
                proposer=proposer,
                schema_document=openapi,
                require_explicit_values=require_explicit_values,
            )
            value = _validated_schema_value(
                schema, value, location=f"{where}.{name}", openapi=openapi
            )
            if where == "path":
                paths[name] = value
            elif where == "query":
                query[name] = value
            elif where == "header":
                headers[name] = str(value)
    request_body = step.get("requestBody")
    body_schema: Any = None
    body_meta = operation.value.get("requestBody")
    if isinstance(body_meta, dict):
        body_schema = ((body_meta.get("content") or {}).get("application/json") or {}).get("schema")
    if isinstance(request_body, dict) and "payload" in request_body:
        body = _resolve_value(request_body["payload"], context)
        for replacement in request_body.get("replacements") or []:
            if not isinstance(replacement, dict):
                continue
            selector = replacement.get("targetSelectorType", "jsonpointer")
            if isinstance(selector, dict):
                selector = selector.get("type")
            if selector != "jsonpointer":
                raise _ExecutionError(
                    "UNSUPPORTED_REPLACEMENT",
                    "Only JSON Pointer request-body replacements are supported.",
                )
            _set_pointer(
                body,
                str(replacement.get("target", "")),
                _resolve_value(replacement.get("value"), context),
            )
    elif (
        isinstance(body_schema, dict) and isinstance(body_meta, dict) and body_meta.get("required")
    ):
        body = _schema_value(
            body_schema,
            None,
            has_supplied=False,
            location="body",
            operation_id=operation.operation_id,
            proposer=proposer,
            schema_document=openapi,
            require_explicit_values=require_explicit_values,
        )
    else:
        body = None
    if isinstance(body_schema, dict) and body is not None:
        body = _validated_schema_value(body_schema, body, location="body", openapi=openapi)
    return paths, query, headers, body


def _report_failure(
    workflow_id: str,
    steps: list[dict[str, Any]],
    workflow_inputs: dict[str, Any],
    *,
    code: str,
    message: str,
    defect_class: str = "TEST_DEFECT",
    contract_status: str = "INCONCLUSIVE",
    semantic_status: str = "INCONCLUSIVE",
    outputs: dict[str, Any] | None = None,
    finding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    unavailable = defect_class == "ENVIRONMENT_DEFECT"
    return {
        "workflowId": workflow_id,
        "status": "unavailable" if unavailable else "failed",
        "gateStatus": "INCONCLUSIVE" if unavailable else "FAIL",
        "defectClass": defect_class,
        "steps": steps,
        "workflowInputs": workflow_inputs,
        "outputs": outputs or {},
        "contractStatus": contract_status,
        "semanticStatus": semantic_status,
        "reason": message,
        "finding": finding or {"code": code, "message": message},
    }


def execute_arazzo_workflow(
    document: dict[str, Any],
    workflow_id: str,
    *,
    openapi: dict[str, Any],
    target_url: str,
    workflow_inputs: Mapping[str, Any] | None = None,
    workflow_inputs_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    propose_input: InputValueProposer | None = None,
    require_explicit_values: bool = False,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Execute one validated local Arazzo workflow without dynamic code evaluation."""
    workflow_started = time.perf_counter()
    emit_testing_progress(
        phase="dynamic",
        scope="workflow",
        status="RUNNING",
        label=f"Running workflow {workflow_id}",
        workflow_id=workflow_id,
    )

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        gate_status = str(result.get("gateStatus") or "INCONCLUSIVE").upper()
        emit_dynamic_workflow_terminal(
            status=gate_status
            if gate_status in {"PASS", "FAIL", "INCONCLUSIVE"}
            else "INCONCLUSIVE",
            label=(
                f"Completed workflow {workflow_id}"
                if gate_status == "PASS"
                else f"Workflow {workflow_id} did not complete"
            ),
            workflow_id=workflow_id,
            total_steps=len(result.get("steps") or []),
            elapsed_ms=int((time.perf_counter() - workflow_started) * 1000),
        )
        return result

    try:
        frozen = validate_arazzo_document(document, openapi=openapi)
    except (ArazzoValidationError, TypeError, ValueError) as exc:
        return finish(
            _report_failure(
                workflow_id,
                [],
                dict(workflow_inputs or {}),
                code="ARAZZO_DOCUMENT_INVALID",
                message=str(exc),
            )
        )
    workflows = {item["workflowId"]: item for item in frozen["workflows"] if isinstance(item, dict)}
    if workflow_id not in workflows:
        return finish(
            _report_failure(
                workflow_id,
                [],
                dict(workflow_inputs or {}),
                code="WORKFLOW_NOT_FOUND",
                message=f"Workflow does not exist: {workflow_id}",
            )
        )
    if workflow_inputs_by_id is not None and not isinstance(workflow_inputs_by_id, Mapping):
        return finish(
            _report_failure(
                workflow_id,
                [],
                dict(workflow_inputs or {}),
                code="WORKFLOW_INPUT_INVALID",
                message="workflow_inputs_by_id must map workflow IDs to input objects.",
            )
        )
    preserved_inputs_by_id: dict[str, dict[str, Any]] = {}
    for preserved_workflow_id, values in (workflow_inputs_by_id or {}).items():
        if not isinstance(preserved_workflow_id, str) or not isinstance(values, Mapping):
            return finish(
                _report_failure(
                    workflow_id,
                    [],
                    dict(workflow_inputs or {}),
                    code="WORKFLOW_INPUT_INVALID",
                    message="workflow_inputs_by_id must map workflow IDs to input objects.",
                )
            )
        preserved_inputs_by_id[preserved_workflow_id] = dict(values)
    reports: list[dict[str, Any]] = []
    completed: dict[str, dict[str, Any]] = {}
    completed_steps: dict[tuple[str, str], dict[str, Any]] = {}
    resolved_inputs_by_id: dict[str, dict[str, Any]] = {}
    cleanup_evidence: list[dict[str, Any]] = []
    last_error: _ExecutionError | None = None
    step_started: dict[tuple[str, str], float] = {}

    def append_step_report(report: dict[str, Any]) -> None:
        reports.append(report)
        workflow = str(report.get("workflowId") or workflow_id)
        step = str(report.get("stepId") or "")
        elapsed = step_started.pop((workflow, step), workflow_started)
        failed = (
            report.get("finding") is not None
            or str(report.get("status") or "").lower() == "failed"
            or str(report.get("semanticStatus") or "").upper() == "FAIL"
            or str(report.get("contractStatus") or "").upper() == "FAIL"
        )
        emit_testing_progress(
            phase="dynamic",
            scope="step",
            status="FAIL" if failed else "PASS",
            label=(f"Completed step {step}" if not failed else f"Step {step} failed"),
            workflow_id=workflow,
            step_id=step,
            operation_id=str(report.get("operationId") or report.get("calledWorkflowId") or ""),
            method=str(report.get("method") or ""),
            path=str(report.get("path") or ""),
            status_code=(
                int(report["statusCode"])
                if isinstance(report.get("statusCode"), int)
                else None
            ),
            contract_status=str(report.get("contractStatus") or ""),
            semantic_status=str(report.get("semanticStatus") or ""),
            control=str(report.get("control") or ""),
            attempt=(
                int(report["attempt"])
                if isinstance(report.get("attempt"), int)
                else None
            ),
            elapsed_ms=int((time.perf_counter() - elapsed) * 1000),
        )

    def run_nested(
        child_workflow_id: str,
        supplied: Mapping[str, Any] | None,
        depth: int,
    ) -> tuple[bool, dict[str, Any], dict[str, Any]]:
        child_started = time.perf_counter()
        child = workflows.get(child_workflow_id) or {}
        emit_testing_progress(
            phase="dynamic",
            scope="workflow",
            status="RUNNING",
            label=f"Running child workflow {child_workflow_id}",
            workflow_id=child_workflow_id,
            total_steps=len(child.get("steps") or []),
        )
        try:
            outcome = run(child_workflow_id, supplied, depth)
        except Exception:
            emit_testing_progress(
                phase="dynamic",
                scope="workflow",
                status="FAIL",
                label=f"Child workflow {child_workflow_id} failed",
                workflow_id=child_workflow_id,
                elapsed_ms=int((time.perf_counter() - child_started) * 1000),
            )
            raise
        emit_testing_progress(
            phase="dynamic",
            scope="workflow",
            status="PASS" if outcome[0] else "FAIL",
            label=f"Completed child workflow {child_workflow_id}",
            workflow_id=child_workflow_id,
            total_steps=len(child.get("steps") or []),
            elapsed_ms=int((time.perf_counter() - child_started) * 1000),
        )
        return outcome

    def run(
        current_id: str, supplied: Mapping[str, Any] | None, depth: int
    ) -> tuple[bool, dict[str, Any], dict[str, Any]]:
        nonlocal last_error
        if depth > _MAX_CALL_DEPTH:
            raise _ExecutionError("WORKFLOW_DEPTH_EXCEEDED", "Local workflow call depth exceeded.")
        workflow = workflows[current_id]
        for dependency in workflow.get("dependsOn") or []:
            if not isinstance(dependency, str) or dependency not in workflows:
                raise _ExecutionError(
                    "DEPENDENCY_NOT_COMPLETED",
                    f"Workflow dependency was not completed: {dependency}",
                )
            if dependency not in completed:
                dependency_ok, _dependency_outputs, _dependency_inputs = run(
                    dependency, {}, depth + 1
                )
                if not dependency_ok:
                    return False, {}, {}
        combined_supplied = dict(preserved_inputs_by_id.get(current_id, {}))
        combined_supplied.update(dict(supplied or {}))

        def scoped_propose(request: InputValueRequest) -> Any:
            if propose_input is None:
                raise ValueError("No input proposer is available.")
            return propose_input(
                InputValueRequest(
                    operation_id=request.operation_id,
                    location=request.location,
                    schema=request.schema,
                    operation_context=current_id,
                )
            )

        inputs = _workflow_inputs(
            workflow,
            combined_supplied,
            scoped_propose if propose_input is not None else None,
            frozen,
            require_explicit_values=require_explicit_values,
        )
        resolved_inputs_by_id[current_id] = dict(inputs)
        step_values: dict[str, dict[str, Any]] = {}
        local_outputs: dict[str, Any] = {}
        steps = workflow["steps"]
        index_by_id = {step["stepId"]: index for index, step in enumerate(steps)}
        index = 0
        primary_failure: _ExecutionError | None = None
        cleanup_start: int | None = None
        while index < len(steps):
            step = steps[index]
            step_id = step["stepId"]
            step_started[(current_id, step_id)] = time.perf_counter()
            emit_testing_progress(
                phase="dynamic",
                scope="step",
                status="RUNNING",
                label=f"Running step {step_id}",
                workflow_id=current_id,
                step_id=step_id,
                operation_id=str(step.get("operationId") or step.get("workflowId") or ""),
                total_steps=len(steps),
                attempt=1,
            )
            dependencies = step.get("dependsOn") or []
            for dependency in dependencies:
                if not isinstance(dependency, str):
                    raise _ExecutionError(
                        "DEPENDENCY_NOT_COMPLETED",
                        f"Step dependency was not completed: {dependency}",
                    )
                if dependency.startswith("$workflows."):
                    parts = dependency.split(".")
                    if (
                        len(parts) != 4
                        or parts[2] != "steps"
                        or (parts[1], parts[3]) not in completed_steps
                    ):
                        raise _ExecutionError(
                            "DEPENDENCY_NOT_COMPLETED",
                            f"Step dependency was not completed: {dependency}",
                        )
                elif dependency not in step_values:
                    raise _ExecutionError(
                        "DEPENDENCY_NOT_COMPLETED",
                        f"Step dependency was not completed: {dependency}",
                    )
            context: dict[str, Any] = {
                "inputs": inputs,
                "outputs": local_outputs,
                "steps": step_values,
                "workflows": completed,
                "request": {},
                "response": {},
                "url": target_url,
                "method": "",
                "statusCode": None,
            }
            if isinstance(step.get("workflowId"), str):
                child_inputs = {
                    item["name"]: _resolve_value(item.get("value"), context)
                    for item in step.get("parameters") or []
                    if isinstance(item, dict) and isinstance(item.get("name"), str)
                }
                ok, child_outputs, _child_inputs = run_nested(
                    step["workflowId"], child_inputs, depth + 1
                )
                report = {
                    "stepId": step_id,
                    "workflowId": current_id,
                    "calledWorkflowId": step["workflowId"],
                    "status": "passed" if ok else "failed",
                    "control": "workflow-call",
                    "outputs": child_outputs,
                }
                append_step_report(report)
                step_values[step_id] = {"outputs": child_outputs}
                completed_steps[(current_id, step_id)] = step_values[step_id]
                if not ok:
                    return False, local_outputs, inputs
                index += 1
                continue
            try:
                operation = operation_for_id(openapi, step["operationId"])
                paths, query, headers, body = _parameters(
                    operation,
                    step,
                    context,
                    openapi,
                    scoped_propose if propose_input is not None else None,
                    require_explicit_values=require_explicit_values,
                )
                context["url"] = operation_url(target_url, operation, paths, query)
                context["method"] = operation.method
                context["request"] = {
                    "body": body,
                    "header": headers,
                    "query": query,
                    "path": paths,
                }
                attempts = 0
                while True:
                    try:
                        response, request = send_operation_request(
                            operation,
                            target_url=target_url,
                            paths=paths,
                            query=query,
                            headers=headers,
                            body=body,
                            timeout_seconds=timeout_seconds,
                        )
                    except httpx.RequestError as exc:
                        transport_error = _ExecutionError(
                            "HTTP_TRANSPORT_ERROR",
                            f"HTTP request could not reach {operation.method} {operation.path}: {exc}",
                            defect_class="ENVIRONMENT_DEFECT",
                        )
                        try:
                            transport_action = _first_action(step, workflow, "onFailure", context)
                        except _ExecutionError:
                            transport_action = None
                        if (
                            transport_action is not None
                            and transport_action.get("type") == "retry"
                            and attempts < int(transport_action.get("retryLimit", 1))
                        ):
                            attempts += 1
                            emit_testing_progress(
                                phase="dynamic",
                                scope="step",
                                status="RUNNING",
                                label=f"Retrying step {step_id}",
                                detail="The previous request could not reach the application.",
                                workflow_id=current_id,
                                step_id=step_id,
                                operation_id=operation.operation_id,
                                method=operation.method,
                                path=operation.path,
                                attempt=attempts + 1,
                            )
                            continue
                        raise transport_error from exc
                    contract_status = "PASS"
                    step_error: _ExecutionError | None = None
                    try:
                        payload = validate_operation_response(openapi, operation, response)
                    except (
                        UpstreamAmbiguity,
                        ValueError,
                        json.JSONDecodeError,
                        jsonschema.SchemaError,
                    ) as exc:
                        contract_status = "FAIL"
                        step_error = _ExecutionError(
                            "RESPONSE_SCHEMA_MISMATCH",
                            str(exc),
                            defect_class="SUT_DEFECT",
                        )
                        try:
                            payload = response.json()
                        except (json.JSONDecodeError, ValueError):
                            payload = None
                    context["statusCode"] = response.status_code
                    context["response"] = {"body": payload, "header": dict(response.headers)}
                    criteria_present = step.get("successCriteria") is not None
                    criterion_results = _criterion_results(step.get("successCriteria"), context)
                    criteria_satisfied = all(item["passed"] for item in criterion_results)
                    http_success = 200 <= response.status_code < 300
                    expected_non_success = _explicit_non_success_status_expected(
                        criterion_results, response.status_code
                    )
                    success = (
                        step_error is None
                        and criteria_satisfied
                        and (http_success or expected_non_success)
                    )
                    action = _first_action(
                        step, workflow, "onSuccess" if success else "onFailure", context
                    )
                    if (
                        not success
                        and action is not None
                        and action.get("type") == "retry"
                        and attempts < int(action.get("retryLimit", 1))
                    ):
                        attempts += 1
                        emit_testing_progress(
                            phase="dynamic",
                            scope="step",
                            status="RUNNING",
                            label=f"Retrying step {step_id}",
                            detail="The previous response did not satisfy the step.",
                            workflow_id=current_id,
                            step_id=step_id,
                            operation_id=operation.operation_id,
                            method=operation.method,
                            path=operation.path,
                            attempt=attempts + 1,
                        )
                        continue
                    break
                outputs = (
                    {
                        name: _resolve_value(value, context)
                        for name, value in (step.get("outputs") or {}).items()
                    }
                    if success
                    else {}
                )
                report = {
                    "workflowId": current_id,
                    "stepId": step_id,
                    "operationId": operation.operation_id,
                    "method": operation.method,
                    "path": operation.path,
                    "statusCode": response.status_code,
                    "request": request,
                    "responseBody": response_summary(response.text),
                    "criteria": criterion_results,
                    "outputs": outputs,
                    "contractStatus": contract_status,
                    "semanticStatus": (
                        "PASS"
                        if criteria_present and success
                        else "UNVERIFIED"
                        if success
                        else "FAIL"
                    ),
                    "attempt": attempts + 1,
                }
                append_step_report(report)
                step_values[step_id] = {"outputs": outputs, "response": context["response"]}
                completed_steps[(current_id, step_id)] = step_values[step_id]
                if success:
                    if (
                        action
                        and action.get("type") == "goto"
                        and isinstance(action.get("stepId"), str)
                    ):
                        report["control"] = "goto"
                        index = index_by_id[action["stepId"]]
                        continue
                    if (
                        action
                        and action.get("type") == "goto"
                        and isinstance(action.get("workflowId"), str)
                    ):
                        report["control"] = "goto-workflow"
                        ok, _out, _in = run_nested(
                            action["workflowId"], _action_inputs(action, context), depth + 1
                        )
                        if not ok:
                            return False, local_outputs, inputs
                        break
                    if action and action.get("type") == "end":
                        break
                    index += 1
                    continue
                last_error = step_error or _ExecutionError(
                    "SUCCESS_CRITERIA_FAILED" if criteria_present else "HTTP_STATUS_NOT_SUCCESS",
                    f"Step {step_id} did not meet its success criteria.",
                    defect_class="SUT_DEFECT",
                )
                failed_criterion = next(
                    (item["criterion"] for item in criterion_results if not item["passed"]),
                    None,
                )
                report["finding"] = {
                    "code": last_error.code,
                    "message": str(last_error),
                    "criterion": failed_criterion,
                }
                if (
                    action
                    and action.get("type") == "goto"
                    and isinstance(action.get("stepId"), str)
                ):
                    report["control"] = "cleanup-goto"
                    primary_failure = last_error
                    cleanup_start = len(reports)
                    index = index_by_id[action["stepId"]]
                    continue
                if (
                    action
                    and action.get("type") == "goto"
                    and isinstance(action.get("workflowId"), str)
                ):
                    report["control"] = "cleanup-workflow"
                    primary_error = last_error
                    cleanup_start = len(reports)
                    cleanup_ok, _cleanup_outputs, _cleanup_inputs = run_nested(
                        action["workflowId"], _action_inputs(action, context), depth + 1
                    )
                    cleanup_evidence.append(
                        {
                            "workflowId": action["workflowId"],
                            "gateStatus": "PASS" if cleanup_ok else "FAIL",
                            "steps": reports[cleanup_start:],
                        }
                    )
                    last_error = primary_error
                if primary_failure is not None:
                    cleanup_evidence.append(
                        {
                            "workflowId": current_id,
                            "gateStatus": "FAIL",
                            "steps": reports[cleanup_start or 0 :],
                        }
                    )
                    last_error = primary_failure
                return False, local_outputs, inputs
            except _ExecutionError as exc:
                last_error = exc
                failure_report = {
                    "workflowId": current_id,
                    "stepId": step_id,
                    "status": "failed",
                    "finding": {"code": exc.code, "message": str(exc)},
                }
                if isinstance(context.get("statusCode"), int):
                    failure_report["statusCode"] = context["statusCode"]
                append_step_report(failure_report)
                if primary_failure is not None:
                    cleanup_evidence.append(
                        {
                            "workflowId": current_id,
                            "gateStatus": "FAIL",
                            "steps": reports[cleanup_start or 0 :],
                        }
                    )
                    last_error = primary_failure
                    return False, local_outputs, inputs
                try:
                    action = _first_action(step, workflow, "onFailure", context)
                except _ExecutionError:
                    action = None
                if (
                    action
                    and action.get("type") == "goto"
                    and isinstance(action.get("stepId"), str)
                ):
                    reports[-1]["control"] = "cleanup-goto"
                    primary_failure = exc
                    cleanup_start = len(reports)
                    index = index_by_id[action["stepId"]]
                    continue
                if (
                    action
                    and action.get("type") == "goto"
                    and isinstance(action.get("workflowId"), str)
                ):
                    reports[-1]["control"] = "cleanup-workflow"
                    cleanup_begin = len(reports)
                    cleanup_ok, _cleanup_outputs, _cleanup_inputs = run_nested(
                        action["workflowId"], _action_inputs(action, context), depth + 1
                    )
                    cleanup_evidence.append(
                        {
                            "workflowId": action["workflowId"],
                            "gateStatus": "PASS" if cleanup_ok else "FAIL",
                            "steps": reports[cleanup_begin:],
                        }
                    )
                    last_error = exc
                return False, local_outputs, inputs
        if primary_failure is not None:
            cleanup_evidence.append(
                {
                    "workflowId": current_id,
                    "gateStatus": "PASS",
                    "steps": reports[cleanup_start or 0 :],
                }
            )
            last_error = primary_failure
            return False, local_outputs, inputs
        local_outputs = {
            name: _resolve_value(
                value,
                {
                    "inputs": inputs,
                    "outputs": local_outputs,
                    "steps": step_values,
                    "workflows": completed,
                    "request": {},
                    "response": {},
                    "url": target_url,
                    "method": "",
                    "statusCode": None,
                },
            )
            for name, value in (workflow.get("outputs") or {}).items()
        }
        completed[current_id] = {"inputs": inputs, "outputs": local_outputs}
        return True, local_outputs, inputs

    try:
        ok, outputs, root_inputs = run(workflow_id, workflow_inputs, 0)
    except _ExecutionError as exc:
        for (active_workflow, active_step), started in list(step_started.items()):
            emit_testing_progress(
                phase="dynamic",
                scope="step",
                status="FAIL",
                label=f"Step {active_step} failed",
                detail=str(exc),
                workflow_id=active_workflow,
                step_id=active_step,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
            step_started.pop((active_workflow, active_step), None)
        return finish(
            _report_failure(
                workflow_id,
                reports,
                dict(workflow_inputs or {}),
                code=exc.code,
                message=str(exc),
                defect_class=exc.defect_class,
            )
        )
    if not ok:
        error = last_error or _ExecutionError(
            "WORKFLOW_FAILED", "Workflow execution failed.", defect_class="SUT_DEFECT"
        )
        contract_status = (
            "FAIL"
            if error.code == "RESPONSE_SCHEMA_MISMATCH"
            or any(report.get("contractStatus") == "FAIL" for report in reports)
            else "PASS"
        )
        result = _report_failure(
            workflow_id,
            reports,
            root_inputs,
            code=error.code,
            message=str(error),
            defect_class=error.defect_class,
            contract_status=contract_status,
            semantic_status="FAIL",
            outputs=outputs,
        )
        result["workflowInputsById"] = resolved_inputs_by_id
        failed_report = next(
            (report for report in reports if isinstance(report, dict) and report.get("finding")),
            {},
        )
        failed_workflow_id = str(failed_report.get("workflowId") or workflow_id)
        failed_step_id = str(failed_report.get("stepId") or "")
        result["failedWorkflowId"] = failed_workflow_id
        result["failedStepId"] = failed_step_id
        if isinstance(result.get("finding"), dict):
            result["finding"].update({"workflowId": failed_workflow_id, "stepId": failed_step_id})
        if cleanup_evidence:
            result["cleanupEvidence"] = cleanup_evidence
        return finish(result)
    semantic = (
        "PASS"
        if any(report.get("semanticStatus") == "PASS" for report in reports)
        else "UNVERIFIED"
    )
    return finish(
        {
            "workflowId": workflow_id,
            "status": "passed",
            "gateStatus": "PASS",
            "defectClass": None,
            "steps": reports,
            "workflowInputs": root_inputs,
            "workflowInputsById": resolved_inputs_by_id,
            "outputs": outputs,
            "contractStatus": "PASS",
            "semanticStatus": semantic,
        }
    )


__all__ = ["execute_arazzo_workflow"]
