"""Deterministic context builders for the EasyDep Arazzo planning boundary."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping
from typing import Any

import jsonschema

from app.testing.utils.functional_executor import resolve_schema

_METHODS = ("delete", "get", "head", "options", "patch", "post", "put", "trace")
_IDENTIFIER = re.compile(r"[^A-Za-z0-9_-]+")
_NATURAL_PARTS = re.compile(r"(\d+)")
_SCENARIO_ORDER = re.compile(r":(\d+)$")


class ArazzoPlanningError(ValueError):
    """Frozen planning inputs do not form a deterministic Arazzo context."""


def _natural_identifier_key(value: str) -> tuple[tuple[int, object], ...]:
    """Sort identifiers such as UC1, UC2, UC10 in numeric order."""

    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in _NATURAL_PARTS.split(value)
        if part
    )


def _records(value: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        for key in keys:
            nested = value.get(key)
            if isinstance(nested, list):
                value = nested
                break
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ArazzoPlanningError("Planning records must be a list of objects.")
    return [copy.deepcopy(dict(item)) for item in value]


def _id(record: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ArazzoPlanningError(f"Planning record is missing an identifier ({', '.join(keys)}).")


def _unique(records: Iterable[dict[str, Any]], *keys: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        identifier = _id(record, *keys)
        if identifier in result:
            raise ArazzoPlanningError(f"Duplicate frozen identifier: {identifier}")
        result[identifier] = record
    return result


def _strings(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item.strip() for item in value if isinstance(item, str) and item.strip()}


def _record_links(record: Mapping[str, Any], *keys: str) -> set[str]:
    links: set[str] = set()
    for key in keys:
        links.update(_strings(record.get(key)))
    return links


def _nested_requirement_ids(value: Any) -> set[str]:
    """Collect only explicitly named requirement-link fields from a use-case spec."""
    if isinstance(value, Mapping):
        result = _record_links(value, "requirementIds", "requirement_ids", "covered_req_ids")
        for child in value.values():
            result.update(_nested_requirement_ids(child))
        return result
    if isinstance(value, list):
        return set().union(*(_nested_requirement_ids(item) for item in value), set())
    return set()


def _workflow_id(use_case_id: str) -> str:
    normalized = _IDENTIFIER.sub("-", use_case_id).strip("-")
    if not normalized:
        raise ArazzoPlanningError(f"Use case cannot produce a workflowId: {use_case_id}")
    return "workflow-" + normalized


def use_case_id_for_candidate(candidate: Mapping[str, Any]) -> str:
    """Return the frozen use-case identifier behind a workflow candidate.

    A workflow ID is an execution identifier, not a useful public label.  Keep
    the two concepts separate so a missing display name never leaks a value
    such as ``workflow-UC2`` into the Testing result.
    """

    use_case = candidate.get("useCase")
    if isinstance(use_case, Mapping):
        for key in ("use_case_id", "useCaseId", "id"):
            value = use_case.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    trace = candidate.get("trace")
    if isinstance(trace, Mapping):
        values = trace.get("useCaseIds")
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str) and value.strip():
                    return value.strip()
    workflow_id = candidate.get("workflowId")
    if isinstance(workflow_id, str) and workflow_id.startswith("workflow-"):
        return workflow_id.removeprefix("workflow-")
    return ""


def use_case_display_name(candidate: Mapping[str, Any]) -> str:
    """Resolve the public workflow label from frozen use-case evidence only."""

    use_case = candidate.get("useCase")
    if isinstance(use_case, Mapping):
        for key in ("name", "useCaseName", "use_case_name", "title"):
            value = use_case.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    use_case_id = use_case_id_for_candidate(candidate)
    return f"Use case {use_case_id}" if use_case_id else "Functional use case"


def _is_functional(use_case: Mapping[str, Any]) -> bool:
    """Respect only an explicit classification; unclassified use cases are functional."""
    for key in ("functional", "isFunctional", "is_functional"):
        if key in use_case:
            return use_case[key] is True
    for key in ("type", "kind", "category"):
        value = use_case.get(key)
        if isinstance(value, str) and value.strip().lower() in {"functional", "nonfunctional"}:
            return value.strip().lower() == "functional"
    return True


def _is_functional_requirement(requirement: Mapping[str, Any]) -> bool:
    for key in ("type", "requirementType", "requirement_type"):
        value = requirement.get(key)
        if isinstance(value, str):
            return value.strip().lower() in {"fr", "functional"}
    return False


def _traceability_links(use_cases: Any) -> dict[str, set[str]]:
    if not isinstance(use_cases, Mapping):
        return {}
    traceability = use_cases.get("traceability")
    requirements = traceability.get("requirements") if isinstance(traceability, Mapping) else None
    if not isinstance(requirements, Mapping):
        return {}
    result: dict[str, set[str]] = {}
    for requirement_id, detail in requirements.items():
        if isinstance(requirement_id, str) and isinstance(detail, Mapping):
            result[requirement_id] = _record_links(
                detail,
                "use_cases",
                "useCaseIds",
                "use_case_ids",
                "realized_by_use_cases",
                "constrains_use_cases",
            )
    return result


def _effective_parameters(
    path_item: Mapping[str, Any], operation: Mapping[str, Any]
) -> list[dict[str, Any]]:
    values: dict[tuple[str, str], dict[str, Any]] = {}
    for owner in (path_item, operation):
        raw = owner.get("parameters")
        if raw is None:
            continue
        if not isinstance(raw, list):
            raise ArazzoPlanningError("Frozen OpenAPI parameters must be a list.")
        for parameter in raw:
            if not isinstance(parameter, Mapping):
                raise ArazzoPlanningError("Frozen OpenAPI parameter must be an object.")
            name, location = parameter.get("name"), parameter.get("in")
            if (
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(location, str)
                or not location.strip()
            ):
                raise ArazzoPlanningError("Frozen OpenAPI parameter lacks name or in.")
            values[(location, name)] = copy.deepcopy(dict(parameter))
    return [values[key] for key in sorted(values, key=lambda item: (item[0], item[1]))]


def _request_contract(operation: Mapping[str, Any]) -> dict[str, Any] | None:
    request_body = operation.get("requestBody")
    if request_body is None:
        return None
    if not isinstance(request_body, Mapping):
        raise ArazzoPlanningError("Frozen OpenAPI requestBody must be an object.")
    content = request_body.get("content")
    if content is not None and not isinstance(content, Mapping):
        raise ArazzoPlanningError("Frozen OpenAPI requestBody content must be an object.")
    json_content = content.get("application/json") if isinstance(content, Mapping) else None
    return {
        "required": bool(request_body.get("required")),
        "contentType": "application/json",
        "schema": copy.deepcopy(json_content.get("schema"))
        if isinstance(json_content, Mapping)
        else None,
    }


def _response_contracts(operation: Mapping[str, Any]) -> list[dict[str, Any]]:
    responses = operation.get("responses")
    if not isinstance(responses, Mapping):
        raise ArazzoPlanningError("Frozen OpenAPI operation responses must be an object.")
    result: list[dict[str, Any]] = []
    for status, response in sorted(responses.items(), key=lambda item: str(item[0])):
        if not isinstance(response, Mapping):
            raise ArazzoPlanningError("Frozen OpenAPI response must be an object.")
        content = response.get("content")
        if content is not None and not isinstance(content, Mapping):
            raise ArazzoPlanningError("Frozen OpenAPI response content must be an object.")
        json_content = content.get("application/json") if isinstance(content, Mapping) else None
        result.append(
            {
                "status": str(status),
                "description": str(response.get("description") or ""),
                "schema": copy.deepcopy(json_content.get("schema"))
                if isinstance(json_content, Mapping)
                else None,
            }
        )
    return result


def _is_empty_object(schema: Mapping[str, Any] | None) -> bool:
    return (
        isinstance(schema, Mapping)
        and schema.get("type") == "object"
        and not schema.get("properties")
    )


def _schema_slots(
    openapi: Mapping[str, Any],
    schema: Any,
    slot: str,
    issues: list[str],
    *,
    require_concrete: bool = True,
    pointer_parts: tuple[str, ...] = (),
    include_pointer: bool = False,
) -> list[dict[str, Any]]:
    """Project OpenAPI leaves into typed, connectable input/output slots."""

    try:
        resolved = resolve_schema(dict(openapi), schema)
    except ValueError as error:
        issues.append(f"{slot}: {error}")
        return []
    schema_type = resolved.get("type")
    if isinstance(schema_type, list):
        non_null = [item for item in schema_type if item != "null"]
        if len(non_null) == 1:
            resolved = {**resolved, "type": non_null[0]}
            schema_type = non_null[0]
    if schema_type is None:
        for key in ("anyOf", "oneOf"):
            branches = resolved.get(key)
            if not isinstance(branches, list):
                continue
            resolved_branches = [
                resolve_schema(dict(openapi), branch)
                for branch in branches
                if isinstance(branch, Mapping)
            ]
            non_null = [branch for branch in resolved_branches if branch.get("type") != "null"]
            if len(non_null) == 1 and len(non_null) < len(resolved_branches):
                resolved = non_null[0]
                schema_type = resolved.get("type")
                break
    if schema_type == "object":
        properties = resolved.get("properties")
        if not isinstance(properties, Mapping) or not properties:
            if require_concrete:
                issues.append(f"{slot}: object schema has no properties")
            return []
        names = sorted(properties)
        if require_concrete:
            required = resolved.get("required")
            required_names = set(required) if isinstance(required, list) else set()
            names = [name for name in names if name in required_names]
            if not names:
                value: dict[str, Any] = {
                    "slot": slot,
                    "type": "object",
                    "format": "",
                    "cardinality": "one",
                }
                if include_pointer:
                    value["pointerParts"] = pointer_parts
                return [value]
        return [
            value
            for name in names
            for child in [properties[name]]
            for value in _schema_slots(
                openapi,
                child,
                f"{slot}.{name}",
                issues,
                require_concrete=require_concrete,
                pointer_parts=pointer_parts + (str(name),),
                include_pointer=include_pointer,
            )
        ]
    if schema_type == "array":
        values = _schema_slots(
            openapi,
            resolved.get("items"),
            f"{slot}[]",
            issues,
            require_concrete=require_concrete,
            pointer_parts=pointer_parts + ("0",),
            include_pointer=include_pointer,
        )
        return values if include_pointer else [{**value, "cardinality": "many"} for value in values]
    if not isinstance(schema_type, str):
        issues.append(f"{slot}: schema has no concrete type")
        return []
    value = {
        "slot": slot,
        "type": schema_type,
        "format": str(resolved.get("format") or ""),
        "cardinality": "one",
    }
    if isinstance(resolved.get("description"), str) and resolved["description"].strip():
        value["description"] = resolved["description"].strip()
    if include_pointer:
        value["pointerParts"] = pointer_parts
    return [value]


def _json_pointer(parts: tuple[str, ...]) -> str:
    return "#" if not parts else "#/" + "/".join(
        part.replace("~", "~0").replace("/", "~1") for part in parts
    )


def _output_name(parts: tuple[str, ...], used: set[str]) -> str:
    words = [word for part in parts for word in re.findall(r"[A-Za-z0-9]+", part)]
    stem = "body" + "".join(word[:1].upper() + word[1:] for word in words)
    stem = stem if stem != "body" else "bodyValue"
    name, suffix = stem, 2
    while name in used:
        name = f"{stem}{suffix}"
        suffix += 1
    used.add(name)
    return name


def _step_id(operation: Mapping[str, Any]) -> str:
    value = _IDENTIFIER.sub("-", _id(operation, "operationId")).strip("-")
    if not value:
        raise ArazzoPlanningError("Operation cannot produce a deterministic stepId.")
    return value


def _connection_types_compatible(
    input_slot: Mapping[str, Any], output: Mapping[str, Any]
) -> bool:
    """Keep only format assignments that can be justified by the source type.

    A specifically formatted value can flow to an unformatted target of the
    same base type. An unformatted source cannot prove a target's narrower
    format, and two different explicit formats are incompatible.
    """
    if input_slot.get("type") != output.get("type") or input_slot.get("cardinality") != output.get("cardinality"):
        return False
    source_format = output.get("format")
    target_format = input_slot.get("format")
    if source_format == target_format:
        return True
    source_specified = isinstance(source_format, str) and bool(source_format.strip())
    target_specified = isinstance(target_format, str) and bool(target_format.strip())
    if source_specified and not target_specified:
        return True
    # A response schema can omit a string format even though a concrete JSON
    # Pointer leaf is later checked against the consumer's OpenAPI schema at
    # runtime.  Do not make the same claim for a whole response body.
    return (
        not source_specified
        and target_specified
        and target_format in jsonschema.FormatChecker.checkers
        and str(output.get("outputExpression") or "").startswith("$response.body#/")
    )


def _parameter_input_slots(
    openapi: Mapping[str, Any], parameter: Mapping[str, Any], slot: str,
    issues: list[str],
) -> list[dict[str, Any]]:
    """Project one parameter without changing its declared identity.

    Parameter objects are values of one OpenAPI parameter, unlike JSON bodies
    whose leaves can be independently connected. Query object serialization is
    supported by the functional executor for form and deepObject styles; other
    object parameter locations/styles are rejected before an invalid workflow
    can be authored.
    """
    schema = parameter.get("schema")
    try:
        resolved = resolve_schema(dict(openapi), schema)
    except ValueError as error:
        issues.append(f"{slot}: {error}")
        return []
    schema_type = resolved.get("type")
    if isinstance(schema_type, list):
        non_null = [item for item in schema_type if item != "null"]
        if len(non_null) == 1:
            resolved = {**resolved, "type": non_null[0]}
    if resolved.get("type") != "object":
        return _schema_slots(openapi, schema, slot, issues)

    location, name = parameter.get("in"), parameter.get("name")
    if location != "query":
        issues.append(
            f"{slot}: object-valued {location or 'unknown'} parameters cannot be serialized safely"
        )
        return []

    style = parameter.get("style", "form")
    explode = parameter.get("explode", style == "form")
    if not (
        (style == "form" and isinstance(explode, bool))
        or (style == "deepObject" and explode is True)
    ):
        issues.append(
            f"{slot}: object query parameter style {style!r} with explode={explode!r} "
            "is unsupported; supported styles are form and deepObject with explode=true"
        )
        return []

    properties = resolved.get("properties")
    if not isinstance(properties, Mapping) or not properties:
        issues.append(f"{slot}: object schema has no properties")
        return []

    def contains_object(value: Any) -> bool:
        try:
            child = resolve_schema(dict(openapi), value)
        except ValueError as error:
            issues.append(f"{slot}: {error}")
            return False
        if child.get("type") == "object":
            return True
        if child.get("type") == "array":
            return contains_object(child.get("items"))
        return any(
            contains_object(branch)
            for key in ("oneOf", "anyOf", "allOf")
            for branches in [child.get(key)]
            if isinstance(branches, list)
            for branch in branches
        )

    if any(contains_object(child) for child in properties.values()):
        issues.append(f"{slot}: nested object query properties are unsupported")
        return []

    value = {
        "slot": slot,
        "type": "object",
        "format": "",
        "cardinality": "one",
        "parameterName": name,
        "parameterStyle": style,
        "parameterExplode": explode,
        "valueSchema": copy.deepcopy(resolved),
    }
    if isinstance(resolved.get("description"), str) and resolved["description"].strip():
        value["description"] = resolved["description"].strip()
    return [value]


def _operation_order(operation: Mapping[str, Any]) -> tuple[int, str]:
    hints = operation.get("traceHints")
    refs = hints.get("scenarioRefs") if isinstance(hints, Mapping) else []
    positions = [
        int(match.group(1))
        for reference in refs if isinstance(refs, list) and isinstance(reference, str)
        if (match := _SCENARIO_ORDER.search(reference))
    ]
    return min(positions, default=10**9), _id(operation, "operationId")


def _current_operation_contract(
    openapi: Mapping[str, Any], operation_id: str, fallback: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Read schemas from the supplied frozen OpenAPI, never a stale projection copy."""

    paths = openapi.get("paths")
    if not isinstance(paths, Mapping):
        return fallback
    for path_item in paths.values():
        if not isinstance(path_item, Mapping):
            continue
        for method in _METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, Mapping) or operation.get("operationId") != operation_id:
                continue
            return {
                **fallback,
                "parameters": _effective_parameters(path_item, operation),
                "requestBody": _request_contract(operation),
                "responses": _response_contracts(operation),
            }
    return fallback


def _candidate_operations(candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the target operations followed by optional catalog setup operations."""
    target = candidate.get("operations")
    setup = candidate.get("setupOperations")
    if not isinstance(target, list):
        raise ArazzoPlanningError("Selected workflow candidate has no operations.")
    if setup is not None and not isinstance(setup, list):
        raise ArazzoPlanningError("Selected workflow setup catalog must be a list.")
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    for operation in [*target, *(setup or [])]:
        if not isinstance(operation, Mapping):
            raise ArazzoPlanningError("Selected candidate operation must be an object.")
        operation_id = _id(operation, "operationId")
        if operation_id in seen:
            continue
        seen.add(operation_id)
        values.append(dict(operation))
    return values


def build_execution_candidates(
    candidates: Iterable[Mapping[str, Any]], openapi: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Deterministically expose executable steps, input slots, and legal connections."""

    result: list[dict[str, Any]] = []
    issues: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise ArazzoPlanningError("Selected workflow candidate must be an object.")
        target_operations = candidate.get("operations")
        if not isinstance(target_operations, list):
            raise ArazzoPlanningError("Selected workflow candidate has no operations.")
        target_operation_ids = {
            _id(operation, "operationId")
            for operation in target_operations
            if isinstance(operation, Mapping)
        }
        workflow_steps: list[dict[str, Any]] = []
        for operation in _candidate_operations(candidate):
            operation_id, step_id = _id(operation, "operationId"), _step_id(operation)
            operation = _current_operation_contract(openapi, operation_id, operation)

            def slots(
                schema: Any, slot: str, location: str, *, require_concrete: bool = True,
                include_pointer: bool = False,
            ) -> list[dict[str, Any]]:
                local_issues: list[str] = []
                values = _schema_slots(
                    openapi, schema, slot, local_issues,
                    require_concrete=require_concrete, include_pointer=include_pointer,
                )
                issues.extend(
                    f"{operation_id} {location}: " + issue.removeprefix(f"{slot}: ")
                    for issue in local_issues
                )
                return values

            inputs: list[dict[str, Any]] = []
            for parameter in operation.get("parameters", []):
                if not isinstance(parameter, Mapping):
                    raise ArazzoPlanningError("Selected operation parameter must be an object.")
                if parameter.get("required") is True:
                    slot = f"{parameter.get('in')}:{parameter.get('name')}"
                    local_issues: list[str] = []
                    inputs.extend(
                        _parameter_input_slots(
                            openapi, parameter, slot, local_issues,
                        )
                    )
                    issues.extend(
                        f"{operation_id} parameter {slot}: "
                        + issue.removeprefix(f"{slot}: ")
                        for issue in local_issues
                    )
            request_body = operation.get("requestBody")
            if isinstance(request_body, Mapping) and request_body.get("required") is True:
                inputs.extend(slots(request_body.get("schema"), "body", "requestBody", include_pointer=True))
            responses = operation.get("responses")
            if not isinstance(responses, list):
                raise ArazzoPlanningError("Selected operation responses must be a list.")
            outputs: list[dict[str, Any]] = []
            for response in responses:
                if not isinstance(response, Mapping):
                    raise ArazzoPlanningError("Selected operation response must be an object.")
                if not str(response.get("status", "")).startswith("2"):
                    continue
                schema = response.get("schema")
                if schema is None:
                    continue
                resolved = resolve_schema(dict(openapi), schema)
                if _is_empty_object(resolved) and resolved.get("additionalProperties") is False:
                    issues.append(f"{operation_id} has a closed empty JSON response at response {response.get('status')}")
                    continue
                response_outputs = slots(
                    schema,
                    "body",
                    f"response {response.get('status')}",
                    require_concrete=False,
                    include_pointer=True,
                )
                description = response.get("description")
                if isinstance(description, str) and description.strip():
                    for output in response_outputs:
                        output["responseDescription"] = description.strip()
                outputs.extend(response_outputs)
            used_output_names: set[str] = set()
            for output in outputs:
                pointer_parts = tuple(output.pop("pointerParts", ()))
                output["outputName"] = _output_name(pointer_parts, used_output_names)
                output["outputExpression"] = "$response.body" + _json_pointer(pointer_parts)
                array_index = next((index for index, part in enumerate(pointer_parts) if part.isdigit()), None)
                if array_index is not None and array_index + 1 < len(pointer_parts):
                    output["collectionItemRef"] = {
                        "arrayRootPointer": _json_pointer(pointer_parts[:array_index]),
                        "itemPointerParts": list(pointer_parts[array_index + 1:]),
                    }
            collection_candidates = []
            item_outputs = [output for output in outputs if isinstance(output.get("collectionItemRef"), dict)]
            for match in item_outputs:
                for selected in item_outputs:
                    if (
                        match is selected
                        or match["collectionItemRef"]["arrayRootPointer"]
                        != selected["collectionItemRef"]["arrayRootPointer"]
                    ):
                        continue
                    collection_candidates.append({
                        "selectionId": f"{operation_id}:{match['outputName']}=>{selected['outputName']}",
                        "arrayRootPointer": match["collectionItemRef"]["arrayRootPointer"],
                        "matchOutputName": match["outputName"],
                        "matchOutputExpression": match.get("outputExpression"),
                        "matchType": match.get("type"),
                        "matchFormat": match.get("format"),
                        "matchItemPointerParts": copy.deepcopy(match["collectionItemRef"]["itemPointerParts"]),
                        "selectedOutputName": selected["outputName"],
                        "selectedOutputExpression": selected.get("outputExpression"),
                        "selectedType": selected.get("type"),
                        "selectedFormat": selected.get("format"),
                        "selectedItemPointerParts": copy.deepcopy(selected["collectionItemRef"]["itemPointerParts"]),
                    })
            workflow_steps.append(
                {
                    "workflowId": _id(candidate, "workflowId"),
                    "stepId": step_id,
                    "operationId": operation_id,
                    "method": str(operation.get("method") or "").upper(),
                    "summary": str(operation.get("summary") or ""),
                    "description": str(operation.get("description") or ""),
                    "successStatuses": [
                        response["status"] for response in responses
                        if isinstance(response, Mapping) and str(response.get("status", "")).startswith("2")
                    ],
                    "inputs": inputs,
                    "outputs": outputs,
                    "collectionSelectionCandidates": collection_candidates,
                }
            )
        all_outputs = [
            {
                **output,
                "stepId": step["stepId"],
                "isSetupOperation": step["operationId"] not in target_operation_ids,
                "method": step["method"],
            }
            for step in workflow_steps
            for output in step["outputs"]
        ]
        for step in workflow_steps:
            for input_slot in step["inputs"]:
                input_slot["inputSlot"] = input_slot["slot"]
                input_slot["connections"] = [
                    {
                        "connectionId": f"{output['stepId']}.{output['outputName']}->{step['stepId']}.{input_slot['inputSlot']}",
                        "sourceStepId": output["stepId"],
                        "sourceSlot": output["slot"],
                        "outputName": output["outputName"],
                        "outputExpression": output["outputExpression"],
                        "targetStepId": step["stepId"],
                        "targetInputSlot": input_slot["inputSlot"],
                        "value": f"$steps.{output['stepId']}.outputs.{output['outputName']}",
                    }
                    for output in all_outputs
                    if output["stepId"] != step["stepId"]
                    and _connection_types_compatible(input_slot, output)
                ]
        result.extend(workflow_steps)
    if issues:
        raise ArazzoPlanningError("Selected frozen OpenAPI contracts are not executable: " + "; ".join(issues))
    return result


def _operation_projection(
    operation_id: str,
    method: str,
    path: str,
    operation: Mapping[str, Any],
    path_item: Mapping[str, Any],
    *,
    use_case_id: str,
    requirement_ids: set[str],
) -> dict[str, Any]:
    use_case_links = _record_links(
        operation, "x-easydep-use-case-ids", "useCaseIds", "use_case_ids"
    )
    scenario_links = _record_links(
        operation,
        "x-easydep-scenario-refs",
        "x-easydep-step-refs",
        "x-easydep-scenario-step-refs",
        "scenarioRefs",
        "scenario_refs",
    )
    requirement_links = _record_links(
        operation, "x-easydep-requirement-ids", "requirementIds", "requirement_ids"
    )
    relevance = (
        use_case_id in use_case_links
        or any(
            reference == use_case_id or reference.startswith(use_case_id + ":")
            for reference in scenario_links
        )
        or bool(requirement_ids.intersection(requirement_links))
    )
    return {
        "operationId": operation_id,
        "method": method.upper(),
        "path": path,
        "summary": str(operation.get("summary") or ""),
        "description": str(operation.get("description") or ""),
        "parameters": _effective_parameters(path_item, operation),
        "requestBody": _request_contract(operation),
        "responses": _response_contracts(operation),
        "traceHints": {
            "useCaseIds": sorted(use_case_links),
            "scenarioRefs": sorted(scenario_links),
            "requirementIds": sorted(requirement_links),
            "relevant": relevance,
        },
    }


def _operation_catalog(
    openapi: Mapping[str, Any], use_case_id: str, requirement_ids: set[str]
) -> list[dict[str, Any]]:
    paths = openapi.get("paths")
    if not isinstance(paths, Mapping) or not paths:
        raise ArazzoPlanningError("Frozen OpenAPI document has no paths object.")
    by_id: dict[str, dict[str, Any]] = {}
    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, Mapping):
            raise ArazzoPlanningError("Frozen OpenAPI path item is invalid.")
        for method in _METHODS:
            operation = path_item.get(method)
            if operation is None:
                continue
            if not isinstance(operation, Mapping):
                raise ArazzoPlanningError("Frozen OpenAPI operation is invalid.")
            operation_id = _id(operation, "operationId")
            if operation_id in by_id:
                raise ArazzoPlanningError(f"Duplicate frozen OpenAPI operationId: {operation_id}")
            by_id[operation_id] = _operation_projection(
                operation_id,
                method,
                path,
                operation,
                path_item,
                use_case_id=use_case_id,
                requirement_ids=requirement_ids,
            )
    if not by_id:
        raise ArazzoPlanningError("Frozen OpenAPI document has no operationIds.")
    return sorted(by_id.values(), key=lambda item: item["operationId"])


def _operations(
    openapi: Mapping[str, Any], use_case_id: str, requirement_ids: set[str]
) -> list[dict[str, Any]]:
    # The target workflow remains trace-closed.  The complete catalog is kept
    # separately for optional setup choices during executable planning.
    return [
        item
        for item in _operation_catalog(openapi, use_case_id, requirement_ids)
        if item["traceHints"]["relevant"]
    ]


def _evidence_refs(*records: Mapping[str, Any]) -> list[str]:
    values: set[str] = set()
    for record in records:
        values.update(_record_links(record, "evidenceRefs", "evidence_refs"))
    return sorted(values)


def _setup_use_case_evidence(
    operation: Mapping[str, Any], specs: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Project exact trace-linked use-case details useful for setup literals."""
    hints = operation.get("traceHints")
    linked_ids = _strings(hints.get("useCaseIds")) if isinstance(hints, Mapping) else set()
    evidence: list[dict[str, Any]] = []
    for use_case_id in sorted(linked_ids, key=_natural_identifier_key):
        spec = specs.get(use_case_id)
        if not isinstance(spec, Mapping):
            continue
        item: dict[str, Any] = {"useCaseId": use_case_id}
        name = next(
            (spec.get(key) for key in ("name", "useCaseName", "use_case_name", "title")
             if isinstance(spec.get(key), str) and spec.get(key).strip()),
            None,
        )
        if name:
            item["name"] = name.strip()
        public_contract = spec.get("public_contract")
        if isinstance(public_contract, Mapping) and "required_values" in public_contract:
            item["required_values"] = copy.deepcopy(public_contract["required_values"])
        flow_fields = {
            "preconditions": ("preconditions",),
            "trigger": ("trigger",),
            "main_scenario": ("main_scenario", "mainScenario"),
            "alternative_scenarios": (
                "alternative_scenarios", "alternativeScenarios", "alternative_flows", "alternativeFlows",
            ),
            "success_guarantee": ("success_guarantee", "successGuarantee"),
            "minimal_guarantee": ("minimal_guarantee", "minimalGuarantee"),
            "acceptance_criteria": ("acceptance_criteria", "acceptanceCriteria"),
        }
        for target, aliases in flow_fields.items():
            value = next(
                (spec[key] for key in aliases if key in spec and spec[key] not in (None, "", [], {})),
                None,
            )
            if value is not None:
                item[target] = copy.deepcopy(value)
        for key in ("extensions", "branches"):
            if key in spec:
                item[key] = copy.deepcopy(spec[key])
        evidence.append(item)
    return evidence


def build_workflow_candidates(
    requirements: Any, use_cases: Any, openapi: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Build one complete, non-filtering planning context per functional use case."""
    requirement_records = _records(requirements, "requirements", "functional_requirements")
    requirement_index = _unique(requirement_records, "id", "requirement_id", "requirementId")
    specs_source = (
        use_cases.get("use_case_specs", []) if isinstance(use_cases, Mapping) else use_cases
    )
    specs = _records(specs_source, "use_case_specs")
    if not specs:
        raise ArazzoPlanningError("Frozen use-case specifications are required.")
    spec_index = _unique(specs, "use_case_id", "useCaseId")
    use_case_records = _records(
        use_cases.get("use_cases", []) if isinstance(use_cases, Mapping) else [], "use_cases"
    )
    use_case_index = (
        _unique(use_case_records, "id", "use_case_id", "useCaseId") if use_case_records else {}
    )
    trace_links = _traceability_links(use_cases)
    setup_catalog = _operation_catalog(openapi, "", set())
    for setup_operation in setup_catalog:
        setup_operation["linkedUseCaseEvidence"] = _setup_use_case_evidence(
            setup_operation, spec_index
        )
    functional_requirements = {
        identifier: record
        for identifier, record in requirement_index.items()
        if _is_functional_requirement(record)
    }
    result: list[dict[str, Any]] = []
    covered_functional_requirements: set[str] = set()
    for use_case_id, spec in spec_index.items():
        use_case = use_case_index.get(use_case_id, {})
        merged = {**copy.deepcopy(use_case), **copy.deepcopy(spec)}
        if not _is_functional(merged):
            continue
        use_case_spec = merged
        linked_ids = _nested_requirement_ids(use_case_spec)
        linked_ids.update(_record_links(use_case_spec, "requirements"))
        linked_ids.update(
            requirement_id
            for requirement_id, linked in trace_links.items()
            if use_case_id in linked
        )
        linked_ids.intersection_update(functional_requirements)
        operations = _operations(openapi, use_case_id, linked_ids)
        if not operations:
            raise ArazzoPlanningError(
                "Functional use case has no OpenAPI operation with an exact use-case, "
                f"scenario-step, or requirement link: {use_case_id}"
            )
        selected = [functional_requirements[identifier] for identifier in linked_ids]
        selected.sort(key=lambda record: _id(record, "id", "requirement_id", "requirementId"))
        selected_ids = {_id(record, "id", "requirement_id", "requirementId") for record in selected}
        covered_functional_requirements.update(selected_ids)
        result.append(
            {
                "workflowId": _workflow_id(use_case_id),
                "requirements": selected,
                "useCase": use_case_spec,
                "operations": operations,
                "setupOperations": [
                    item for item in setup_catalog
                    if item["operationId"] not in {operation["operationId"] for operation in operations}
                ],
                "trace": {
                    "requirementIds": sorted(selected_ids),
                    "useCaseIds": [use_case_id],
                    "evidenceRefs": sorted(
                        {
                            *(f"requirement:{identifier}" for identifier in selected_ids),
                            f"use_case:{use_case_id}",
                            *_evidence_refs(*selected, use_case_spec),
                        }
                    ),
                },
            }
        )
    for requirement_id, requirement in functional_requirements.items():
        scoped = bool(
            _record_links(
                requirement, "useCaseIds", "use_case_ids", "use_cases", "use_case_id", "useCaseId"
            )
            or trace_links.get(requirement_id)
        )
        is_constraint = (
            requirement.get("modeled_as_constraint") is True
            or requirement.get("modeledAsConstraint") is True
        )
        if scoped and requirement_id not in covered_functional_requirements and not is_constraint:
            raise ArazzoPlanningError(
                f"Scoped functional requirement is uncovered: {requirement_id}"
            )
    return sorted(result, key=lambda item: _natural_identifier_key(item["workflowId"]))


def attach_workflow_trace(
    workflow: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    """Replace model traceability with the deterministic frozen candidate trace."""
    if not isinstance(workflow, Mapping):
        raise ArazzoPlanningError("Workflow must be an object.")
    workflow_id = _id(candidate, "workflowId")
    trace = candidate.get("trace")
    if not isinstance(trace, Mapping):
        raise ArazzoPlanningError("Candidate has no deterministic trace.")
    value = copy.deepcopy(dict(workflow))
    value.pop("x-easydep-trace", None)
    value["workflowId"] = workflow_id
    frozen_trace = {
        key: sorted(_strings(trace.get(key)))
        for key in ("requirementIds", "useCaseIds", "evidenceRefs")
        if _strings(trace.get(key))
    }
    value["x-easydep-trace"] = frozen_trace
    return value


def build_deterministic_workflow(candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    """Compile a contract-only workflow when no cross-operation decision is required.

    A single traced OpenAPI operation has no ordering or response-to-request binding
    decision. Request values remain absent so the executor can populate required
    values from the frozen OpenAPI schema. Multi-operation candidates deliberately
    remain outside this boundary until their data flow is explicit in an artifact.
    """

    operations = candidate.get("operations")
    if not isinstance(operations, list) or len(operations) != 1:
        return None
    operation = operations[0]
    if not isinstance(operation, Mapping):
        raise ArazzoPlanningError("Candidate operation must be an object.")
    responses = operation.get("responses")
    if not isinstance(responses, list) or not any(
        isinstance(response, Mapping)
        and str(response.get("status") or "").startswith("2")
        for response in responses
    ):
        return None
    operation_id = _id(operation, "operationId")
    step_id = _IDENTIFIER.sub("-", operation_id).strip("-")
    if not step_id:
        raise ArazzoPlanningError(
            f"Operation cannot produce a deterministic stepId: {operation_id}"
        )
    return attach_workflow_trace(
        {
            "workflowId": _id(candidate, "workflowId"),
            "steps": [{"stepId": step_id, "operationId": operation_id}],
        },
        candidate,
    )


def build_arazzo_document(workflows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Wrap frozen workflow objects in EasyDep's canonical local Arazzo envelope."""
    values: list[dict[str, Any]] = []
    for workflow in workflows:
        if not isinstance(workflow, Mapping):
            raise ArazzoPlanningError("Arazzo workflows must be objects.")
        values.append(copy.deepcopy(dict(workflow)))
    return {
        "arazzo": "1.1.0",
        "info": {"title": "EasyDep Functional Workflows", "version": "1.0.0"},
        "sourceDescriptions": [{"name": "application", "url": "openapi.json", "type": "openapi"}],
        "workflows": values,
    }


__all__ = [
    "ArazzoPlanningError",
    "attach_workflow_trace",
    "build_arazzo_document",
    "build_deterministic_workflow",
    "build_execution_candidates",
    "build_workflow_candidates",
    "use_case_display_name",
    "use_case_id_for_candidate",
]
