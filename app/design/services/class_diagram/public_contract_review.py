"""Independent semantic closure review for public use-case contracts.

The normal class validator deliberately proves only finite structural facts.  It
cannot decide whether a DTO called ``Request`` is the requirement's "request
details".  This module keeps that open-world judgement in one bounded,
structured reviewer call and then verifies every reference the reviewer cites.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.design.services.class_diagram.cache import (
    AcceptedUnitCache,
    accepted_unit_key,
    configured_provider_identity,
    record_cache_outcome,
)
from app.design.services.class_diagram.scenario import ScenarioIndex, UseCase, text
from app.design.services.class_diagram.trusted_context import (
    required_value_catalog,
    required_value_evidence,
)
from app.design.contracts.type_system import DesignTypeError, parse_type_expression
from app.design.services.class_diagram.type_system import referenced_type_names
from app.design.services.class_diagram.validation.model import operation_catalog
from app.design.services.common.structured import parse_structured
from app.llm_connection import build_llm_connection
from app.llm_profiles import effective_temperature
from app.validation import Finding, stable_digest

_EVIDENCE_VERSION = "class-public-contract-review/v8"
_PROMPT = """You independently review whether an accepted class-model use-case
slice closes every public-contract obligation owned by the class stage.  The
requirements/API/implementation stages own the authentication policy expressed
by identity obligations of kind authenticate; do not require a class operation
or collaboration mapping for that policy precondition.  Do not accept a claim merely
because a DTO, principal, or method has a similar name. For every obligation,
cite operationRef (the operation's stableId), its operationId, and callRef (the
call's stableId); cite parameterRef (the parameter's stableRef) and DataType
fieldRef when those declarations are evidence. A fieldRef must belong to the
concrete return or input parameter type cited by that mapping.
For required_values with usage control or both, cite the Control call that receives
the value, its exact parameter, and that call's argument binding. For usage result
or both, cite a Control operation with a concrete non-void return type. For a
required_value whose source is system_result, that Control return is sufficient
evidence: do not require an argument binding or a prior call result. When a
required system_result identifier is returned through a declared structured
type, cite its concrete return DTO fieldRef; the field must not be Optional<T>.
For identify identity obligations, cite a Control parameter and its exact argument
binding. An authenticated identity source must link through that parameter's exact
requiredValueRef to an accepted required-value catalog entry whose identityObligationRef
is this identify obligation. caller_input must bind a canonical actor input;
system_result must bind a prior accepted call result. Never infer provenance from names.
For a required value with usage control or both, the cited Control parameter's
requiredValueRef must equal the exact accepted valueRef. A result-only value does
not require a parameter citation: cite the concrete Control return instead. A
system_result may additionally be cited on a downstream parameter only when the
supplied call flow shows that exact value produced by an earlier call and the
parameter receives it. Catalog entries are evidence, not arbitrary runtime values:
direct bindings are offered only for eligible server-context entries; caller_input
and system_result must use their actual actor-input/prior-result dataflow.
If the supplied model does not make the connection clear, return fail or
ambiguous with one precise reason.  Never invent IDs, fields, operations, or
calls.  Return only the response schema."""


class _ReviewMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    obligation_id: str = Field(alias="obligationId", min_length=1)
    operation_ref: str = Field(alias="operationRef", min_length=1)
    operation_id: str = Field(alias="operationId", min_length=1)
    call_ref: str = Field(alias="callRef", min_length=1)
    parameter_ref: str | None = Field(default=None, alias="parameterRef")
    field_ref: str | None = Field(default=None, alias="fieldRef")
    rationale: str = Field(min_length=1)


class _ReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pass", "fail", "ambiguous"]
    mappings: list[_ReviewMapping] = Field(default_factory=list)
    finding: str = ""


def _contract(use_case: UseCase) -> dict[str, Any]:
    candidate = use_case.specification.get("public_contract")
    return dict(candidate) if isinstance(candidate, Mapping) else {}


def has_obligations(index: ScenarioIndex) -> bool:
    return any(
        _obligations(use_case)
        for use_case in index.use_cases
    )


def _obligations(use_case: UseCase) -> list[dict[str, Any]]:
    contract = _contract(use_case)
    result: list[dict[str, Any]] = []
    for ordinal, item in enumerate(contract.get("identity_obligations") or [], start=1):
        if isinstance(item, Mapping) and text(item.get("obligation")).casefold() != "authenticate":
            result.append({"obligationId": f"identity:{ordinal}", "kind": "identity", **dict(item)})
    for ordinal, item in enumerate(contract.get("required_values") or [], start=1):
        if isinstance(item, Mapping):
            result.append({"obligationId": f"value:{ordinal}", "kind": "required_value", **dict(item)})
    return result


def _fields(model: dict[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for owner in [*(model.get("Classes") or []), *(model.get("DataTypes") or [])]:
        if not isinstance(owner, Mapping):
            continue
        name = text(owner.get("className") or owner.get("name"))
        declared = {text(ref): text(field).partition(":")[2].strip()
                    for field, ref in zip(owner.get("fields") or [], owner.get("fieldRefs") or []) if text(ref)}
        if name and declared:
            result[name] = declared
    return result


def _is_optional_type(type_expression: str) -> bool:
    """Whether a declared field has the design-level optional container."""
    try:
        expression = parse_type_expression(type_expression)
    except DesignTypeError:
        return False
    return expression.kind == "container" and expression.name == "optional"


def _declared_structured_type_names(model: dict[str, Any]) -> set[str]:
    return {
        text(item.get("className") or item.get("name"))
        for item in [*(model.get("Classes") or []), *(model.get("DataTypes") or [])]
        if isinstance(item, Mapping) and text(item.get("className") or item.get("name"))
    }


def _result_identifier_error(
    model: dict[str, Any], obligation: dict[str, Any], mapping: _ReviewMapping,
    operation: dict[str, Any],
    fields: dict[str, dict[str, str]],
) -> str:
    """Prove result identifiers are concrete at the public Control boundary."""
    if (
        text(obligation.get("source")).casefold() != "system_result"
        or text(obligation.get("value_type")).casefold() != "identifier"
        or text(obligation.get("usage")).casefold() not in {"result", "both"}
    ):
        return ""
    return_type = text(operation.get("returnType"))
    if _is_optional_type(return_type):
        return (
            f"Required system-result identifier '{mapping.obligation_id}' must cite a "
            "non-Optional Control return or fieldRef on its concrete return type."
        )
    return_types = referenced_type_names(return_type)
    structured_types = [
        name for name in return_types if name in _declared_structured_type_names(model)
    ]
    if not structured_types:
        return ""
    if not mapping.field_ref:
        return (
            f"Required system-result identifier '{mapping.obligation_id}' must cite a "
            "non-Optional fieldRef on the concrete Control return type."
        )
    field_type = next(
        (fields[type_name][mapping.field_ref] for type_name in structured_types
         if mapping.field_ref in fields[type_name]),
        "",
    )
    if not field_type:
        return (
            f"Required system-result identifier '{mapping.obligation_id}' must cite a "
            "fieldRef on the concrete Control return type."
        )
    if _is_optional_type(field_type):
        return (
            f"Required system-result identifier '{mapping.obligation_id}' must cite a "
            "non-Optional fieldRef on the concrete Control return type."
        )
    return ""


def _slice(model: dict[str, Any], use_case: UseCase) -> dict[str, Any]:
    prefix = f"{use_case.id}:"
    classes: list[dict[str, Any]] = []
    for owner in model.get("Classes") or []:
        if not isinstance(owner, Mapping):
            continue
        operations = [
            dict(operation) for operation in owner.get("operations") or []
            if isinstance(operation, Mapping)
            and any(text(ref).startswith(prefix) for ref in operation.get("stepRefs") or [])
        ]
        if operations:
            classes.append({
                "className": text(owner.get("className")),
                "stereotype": text(owner.get("stereotype")),
                "operations": operations,
            })
    type_expressions = [text(parameter.get("type")) for owner in classes for operation in owner["operations"]
                        for parameter in operation.get("parameters") or [] if isinstance(parameter, Mapping)]
    type_expressions.extend(text(operation.get("returnType")) for owner in classes for operation in owner["operations"])
    used_types = set().union(*(referenced_type_names(item) for item in type_expressions)) if type_expressions else set()
    data_types = [dict(item) for item in model.get("DataTypes") or []
                  if isinstance(item, Mapping) and text(item.get("name")) in used_types]
    operation_classes = tuple(classes)
    for owner in model.get("Classes") or []:
        if (
            not isinstance(owner, Mapping)
            or text(owner.get("stereotype")).casefold() != "entity"
            or text(owner.get("className")) not in used_types
        ):
            continue
        entity_name = text(owner.get("className"))
        return_operations = []
        for class_item in operation_classes:
            for operation in class_item["operations"]:
                if (
                    text(class_item.get("stereotype")).casefold() in {"boundary", "control"}
                    and entity_name in referenced_type_names(text(operation.get("returnType")))
                ):
                    return_operations.append((
                        text(class_item.get("stereotype")), text(operation.get("returnType")),
                    ))
        entity = {
            "className": entity_name,
            "stereotype": text(owner.get("stereotype")),
            "fields": list(owner.get("fields") or []),
            "fieldRefs": list(owner.get("fieldRefs") or []),
        }
        if return_operations:
            roles = list(dict.fromkeys(role for role, _return_type in return_operations))
            return_types = list(dict.fromkeys(
                return_type for _role, return_type in return_operations
            ))
            entity["outputEvidence"] = (
                f"The cited {' and '.join(roles)} operations return "
                f"{' or '.join(return_types)}; these fields are the returned "
                f"{entity_name} details."
            )
        classes.append(entity)
    collaboration = next(
        (dict(item) for item in model.get("Collaborations") or []
         if isinstance(item, Mapping) and text(item.get("collaborationId")) == use_case.id),
        {},
    )
    return {"Classes": classes, "DataTypes": data_types, "Collaboration": collaboration}


def _actor_input_sources(model: dict[str, Any], use_case: UseCase) -> set[str]:
    """Stable input provenance rooted at this UC's accepted Boundary entries."""
    collaboration = next((item for item in model.get("Collaborations") or []
                          if isinstance(item, Mapping) and text(item.get("collaborationId")) == use_case.id), {})
    operations = operation_catalog(model)
    dto_field_refs = {
        text(item.get("name")): {
            text(field_ref) for field_ref in item.get("fieldRefs") or [] if text(field_ref)
        }
        for item in model.get("DataTypes") or []
        if isinstance(item, Mapping)
        and text(item.get("name"))
        and text(item.get("kind")).casefold() in {"valueobject", "datatype"}
    }
    sources: set[str] = set()
    for call in collaboration.get("calls") or []:
        if not isinstance(call, Mapping) or text(call.get("parentCallId")):
            continue
        operation = operations.get(text(call.get("receiverOperationId")))
        if not operation or text(operation.get("stereotype")).casefold() != "boundary":
            continue
        for parameter in operation.get("parameters") or []:
            if not isinstance(parameter, Mapping):
                continue
            ref = text(parameter.get("stableRef"))
            if not ref:
                continue
            field_refs = set().union(*(
                dto_field_refs.get(type_name, set())
                for type_name in referenced_type_names(text(parameter.get("type")))
            ))
            roots = set()
            for step_ref in call.get("stepRefs") or []:
                if text(step_ref).startswith(f"{use_case.id}:"):
                    roots.add(f"{text(step_ref)}#{ref}")
            if text(call.get("stableId")):
                roots.add(f"{text(call.get('stableId'))}#{ref}")
            sources.update(roots)
            sources.update(
                f"{root}.{field_ref}"
                for root in roots for field_ref in field_refs
            )
    return sources


def _prior_result_source(source_ref: str, calls: dict[str, Any], target_ref: str, operations: dict[str, dict[str, Any]]) -> bool:
    source_call_ref, separator, suffix = source_ref.partition("#")
    if not separator or suffix not in {"result", "result.unwrap"}:
        return False
    ordered = list(calls)
    if source_call_ref not in ordered or target_ref not in ordered or ordered.index(source_call_ref) >= ordered.index(target_ref):
        return False
    operation = operations.get(text(calls[source_call_ref].get("receiverOperationId")))
    return bool(operation and text(operation.get("returnType")) and text(operation.get("returnType")).casefold() != "void")


def _review_key(index: ScenarioIndex, model: dict[str, Any], use_case: UseCase) -> str:
    return accepted_unit_key(
        "class-public-contract-review",
        unit_slice={"useCase": use_case.id, "obligations": _obligations(use_case), "slice": _slice(model, use_case)},
        inventory={}, feedback={}, prompt=_PROMPT, schema=_ReviewResponse,
        provider=configured_provider_identity(build_llm_connection().base_url),
        model=settings.model, seed=settings.seed,
        temperature=effective_temperature(settings.model, settings.temperature),
        reasoning_effort=None, max_completion_tokens=None,
        extra={"version": _EVIDENCE_VERSION, "scenario": index.raw},
    )


def _review_payload(index: ScenarioIndex, model: dict[str, Any], use_case: UseCase) -> dict[str, Any]:
    """The finite reviewer input, shared unchanged by its one correction pass."""
    del index  # The use-case slice is already fully determined by model and use_case.
    return {
        "useCaseId": use_case.id,
        "publicContractObligations": _obligations(use_case),
        "requiredValueCatalog": required_value_evidence(use_case),
        "acceptedFragmentAndCollaboration": _slice(model, use_case),
    }


def _review_one(index: ScenarioIndex, model: dict[str, Any], use_case: UseCase) -> _ReviewResponse:
    payload = _review_payload(index, model, use_case)
    parsed = parse_structured(
        [{"role": "system", "content": _PROMPT}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        _ReviewResponse, operation="ClassPublicContractReview",
        metadata={"useCaseId": use_case.id, "executionSlice": use_case.id},
    )
    return _ReviewResponse.model_validate(parsed)


def _correct_review_one(
    index: ScenarioIndex, model: dict[str, Any], use_case: UseCase,
    previous: _ReviewResponse, diagnostic: str,
) -> _ReviewResponse:
    """Make one bounded evidence correction; the verifier remains authoritative."""
    payload = _review_payload(index, model, use_case) | {
        "previousResponse": previous.model_dump(by_alias=True),
        "validatorDiagnostic": diagnostic,
    }
    parsed = parse_structured(
        [
            {"role": "system", "content": _PROMPT + "\nYour previous response failed the deterministic validator. Correct only its evidence against the same supplied finite slice and return the response schema."},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        _ReviewResponse, operation="ClassPublicContractReview",
        metadata={"useCaseId": use_case.id, "executionSlice": use_case.id, "correction": True},
    )
    return _ReviewResponse.model_validate(parsed)


def _verify_response(model: dict[str, Any], use_case: UseCase, response: _ReviewResponse) -> str:
    expected = {item["obligationId"] for item in _obligations(use_case)}
    mapped = [item.obligation_id for item in response.mappings]
    if response.status != "pass":
        return text(response.finding) or "The public-contract closure is missing or ambiguous."
    if set(mapped) != expected or len(mapped) != len(set(mapped)):
        return "The reviewer did not map every public-contract obligation exactly once."
    operations = operation_catalog(model)
    operations_by_ref = {
        text(operation.get("stableId")): operation
        for owner in model.get("Classes") or [] if isinstance(owner, Mapping)
        for operation in owner.get("operations") or [] if isinstance(operation, Mapping)
        if text(operation.get("stableId"))
    }
    collaboration = next((item for item in model.get("Collaborations") or [] if isinstance(item, Mapping) and text(item.get("collaborationId")) == use_case.id), {})
    calls = {text(item.get("stableId")): item for item in collaboration.get("calls") or [] if isinstance(item, Mapping)}
    fields = _fields(model)
    obligations = {item["obligationId"]: item for item in _obligations(use_case)}
    for mapping in response.mappings:
        stable_operation = operations_by_ref.get(mapping.operation_ref)
        operation = operations.get(mapping.operation_id)
        if (stable_operation is None or operation is None
                or text(stable_operation.get("operationId")) != mapping.operation_id
                or text(operation.get("stableId")) != mapping.operation_ref):
            return f"Reviewer operationRef '{mapping.operation_ref}' does not identify operationId '{mapping.operation_id}'."
        call = calls.get(mapping.call_ref)
        if call is None or text(call.get("receiverOperationId")) != mapping.operation_id:
            return f"Reviewer cited callRef '{mapping.call_ref}' that does not invoke '{mapping.operation_id}'."
        parameters = [item for item in operation.get("parameters") or [] if isinstance(item, Mapping)]
        cited_parameter = next((item for item in parameters if text(item.get("stableRef")) == mapping.parameter_ref), None) if mapping.parameter_ref else None
        if mapping.parameter_ref and cited_parameter is None:
            return f"Reviewer cited unknown parameterRef '{mapping.parameter_ref}' on '{mapping.operation_id}'."
        obligation = obligations[mapping.obligation_id]
        identity = obligation.get("kind") == "identity" and text(obligation.get("obligation")).casefold() == "identify"
        if identity:
            if not mapping.parameter_ref or cited_parameter is None:
                return f"Identity obligation '{mapping.obligation_id}' must cite its exact parameterRef."
            if text(operation.get("stereotype")).casefold() != "control":
                return f"Identity obligation '{mapping.obligation_id}' must map to a Control call."
            parameter_name = text(cited_parameter.get("name"))
            binding = next((item for item in call.get("argumentBindings") or []
                            if isinstance(item, Mapping) and text(item.get("parameter")) == parameter_name), None)
            if binding is None:
                return f"Identity obligation '{mapping.obligation_id}' has no exact argument binding for its cited parameter."
            source_kind = text(obligation.get("identity_source_kind")).casefold()
            source_ref = text(binding.get("sourceRef"))
            if source_kind == "unresolved" or source_kind not in {"caller_input", "authenticated_context", "system_result"}:
                return f"Identity obligation '{mapping.obligation_id}' has unresolved or unsupported identity source; clarification is required."
            if source_kind == "authenticated_context":
                identify_ref = text(obligation.get("obligation_ref"))
                required_ref = text(cited_parameter.get("requiredValueRef"))
                linked_value = next((item for item in required_value_catalog(use_case)
                                     if item["valueRef"] == required_ref
                                     and item["identityObligationRef"] == identify_ref), None)
                direct_valid = bool(linked_value and source_ref == linked_value["sourceRef"])
                prior_valid = bool(linked_value and mapping.field_ref
                                   and _prior_result_source(source_ref, calls, mapping.call_ref, operations))
                if not (direct_valid or prior_valid):
                    return f"Identity obligation '{mapping.obligation_id}' must bind a required value linked to this identity obligation, directly or through a prior result."
            elif source_kind == "caller_input":
                root_inputs = _actor_input_sources(model, use_case)
                if source_ref not in root_inputs:
                    return f"Identity obligation '{mapping.obligation_id}' must bind a canonical actor input source."
            else:
                if not _prior_result_source(source_ref, calls, mapping.call_ref, operations):
                    return f"Identity obligation '{mapping.obligation_id}' must bind a prior accepted call result."
        if mapping.field_ref:
            type_expression = text(cited_parameter.get("type")) if cited_parameter else text(operation.get("returnType"))
            possible_types = referenced_type_names(type_expression)
            target_field = any(
                mapping.field_ref in fields.get(owner, {}) for owner in possible_types
            )
            bound_source = text(next(
                (
                    item.get("sourceRef") for item in call.get("argumentBindings") or []
                    if isinstance(item, Mapping)
                    and cited_parameter is not None
                    and text(item.get("parameter")) == text(cited_parameter.get("name"))
                ),
                "",
            ))
            root_input_field = bool(
                cited_parameter
                and bound_source.endswith(f".{mapping.field_ref}")
                and bound_source in _actor_input_sources(model, use_case)
            )
            if not target_field and not root_input_field:
                return f"Reviewer fieldRef '{mapping.field_ref}' does not belong to the cited concrete type."
        usage = text(obligation.get("usage")).casefold() if obligation.get("kind") == "required_value" else ""
        if usage in {"control", "both"}:
            if text(operation.get("stereotype")).casefold() != "control":
                return f"Required value '{mapping.obligation_id}' must be mapped to a Control call."
            if cited_parameter is None:
                return f"Required value '{mapping.obligation_id}' must cite its Control parameter."
            if text(cited_parameter.get("requiredValueRef")) != text(obligation.get("value_ref")):
                return f"Required value '{mapping.obligation_id}' must cite its exact accepted valueRef on the parameter."
            parameter_name = text(cited_parameter.get("name"))
            bound_parameters = {
                text(binding.get("parameter")) for binding in call.get("argumentBindings") or []
                if isinstance(binding, Mapping)
            }
            if parameter_name not in bound_parameters:
                return f"Required value '{mapping.obligation_id}' is not bound on callRef '{mapping.call_ref}'."
        if usage in {"result", "both"} and (
            text(operation.get("stereotype")).casefold() != "control"
            or not text(operation.get("returnType"))
            or text(operation.get("returnType")).casefold() == "void"
        ):
            return f"Required value '{mapping.obligation_id}' must cite a Control operation with a concrete return."
        result_identifier_error = _result_identifier_error(
            model, obligation, mapping, operation, fields,
        )
        if result_identifier_error:
            return result_identifier_error
    return ""


def _evidence_matches(evidence: object, model: dict[str, Any], index: ScenarioIndex) -> bool:
    return isinstance(evidence, Mapping) and evidence.get("version") == _EVIDENCE_VERSION and evidence.get("modelDigest") == stable_digest(model) and evidence.get("contractDigest") == stable_digest([_obligations(item) for item in index.use_cases])


def review_public_contract_closure(
    model: dict[str, Any], index: ScenarioIndex, *, cache: AcceptedUnitCache | None = None,
    evidence: object = None,
) -> tuple[list[Finding], dict[str, Any]]:
    """Review only obligation-bearing UCs; matching persisted evidence is authoritative."""
    if not has_obligations(index):
        return [], {"version": _EVIDENCE_VERSION, "modelDigest": stable_digest(model), "contractDigest": stable_digest([_obligations(item) for item in index.use_cases]), "status": "not_required", "verdicts": []}
    if _evidence_matches(evidence, model, index):
        stored = dict(evidence)
        findings = [Finding("class.public-contract-semantic", text(item.get("finding")), text(item.get("useCaseId")), origin="semantic") for item in stored.get("verdicts") or [] if isinstance(item, Mapping) and item.get("status") != "pass"]
        return findings, stored
    findings: list[Finding] = []
    verdicts: list[dict[str, Any]] = []
    for use_case in index.use_cases:
        if not _obligations(use_case):
            continue
        key = _review_key(index, model, use_case)
        def compute() -> dict[str, Any]:
            # Keep an invalid first attempt out of the accepted-unit cache.  At
            # most one correction is made, and only its final response is kept.
            response = _review_one(index, model, use_case)
            diagnostic = _verify_response(model, use_case, response)
            if diagnostic:
                response = _correct_review_one(index, model, use_case, response, diagnostic)
            return response.model_dump(by_alias=True)
        try:
            if cache is None:
                record_cache_outcome(None, operation="ClassPublicContractReview", unit=use_case.id)
                raw = compute()
            else:
                cached = cache.get_or_compute(key, compute)
                record_cache_outcome(cached, operation="ClassPublicContractReview", unit=use_case.id)
                raw = cached.value
        except Exception as error:  # a missing verdict must block approval, never pass implicitly
            problem = f"Public-contract semantic reviewer unavailable: {type(error).__name__}: {error}"
            verdicts.append({"useCaseId": use_case.id, "status": "review_error", "mappings": [], "finding": problem, "reviewKey": key})
            findings.append(Finding("class.public-contract-semantic", problem, use_case.id, origin="semantic"))
            continue
        response = _ReviewResponse.model_validate(raw)
        problem = _verify_response(model, use_case, response)
        status = "pass" if not problem else response.status if response.status != "pass" else "fail"
        verdict = {"useCaseId": use_case.id, "status": status, "mappings": response.model_dump(by_alias=True).get("mappings", []), "finding": problem, "reviewKey": key}
        verdicts.append(verdict)
        if problem:
            findings.append(Finding("class.public-contract-semantic", problem, use_case.id, origin="semantic"))
    return findings, {"version": _EVIDENCE_VERSION, "modelDigest": stable_digest(model), "contractDigest": stable_digest([_obligations(item) for item in index.use_cases]), "status": "pass" if not findings else "needs_input", "verdicts": verdicts}


def semantic_evidence_for_readiness(model: dict[str, Any], state: Mapping[str, Any], index: ScenarioIndex) -> list[Finding]:
    """Pure readiness check: never invokes a model or trusts stale evidence."""
    if not has_obligations(index):
        return []
    check = state.get("class_diagram_check")
    evidence = check.get("semanticEvidence") if isinstance(check, Mapping) else None
    if not _evidence_matches(evidence, model, index):
        return [Finding("class.public-contract-semantic", "Public-contract semantic review evidence is missing or stale; rerun the class-stage check.", "class_diagram", origin="semantic")]
    return [Finding("class.public-contract-semantic", text(item.get("finding")), text(item.get("useCaseId")), origin="semantic") for item in evidence.get("verdicts") or [] if isinstance(item, Mapping) and item.get("status") != "pass"]
