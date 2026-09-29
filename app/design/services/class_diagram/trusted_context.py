"""Finite accepted required-value source catalog for class design."""
from __future__ import annotations

from typing import Any

from app.design.services.class_diagram.scenario import UseCase, text


_SOURCE_AVAILABILITY = {
    "caller_input": "actor_entry",
    "system_result": "prior_result",
    "authenticated_actor_context": "server_context",
}

_DESIGN_TYPES = {
    "string": "String", "integer": "Integer", "number": "Decimal",
    "boolean": "Boolean", "date": "LocalDate", "datetime": "LocalDateTime",
    "identifier": "UUID", "object": "Object", "array": "List<String>",
}


def required_value_catalog(use_case: UseCase) -> tuple[dict[str, Any], ...]:
    """Expose accepted exact required-value refs and their upstream evidence."""
    contract = use_case.specification.get("public_contract")
    values = contract.get("required_values") if isinstance(contract, dict) else []
    if not isinstance(values, list):
        return ()
    catalog = []
    for item in values:
        if not isinstance(item, dict):
            continue
        value_ref = text(item.get("value_ref"))
        source = text(item.get("source"))
        value_type = text(item.get("value_type"))
        if not value_ref or source not in _SOURCE_AVAILABILITY or not value_type:
            continue
        catalog.append({
            "sourceRef": f"value#{value_ref}",
            "valueRef": value_ref,
            "name": text(item.get("name")),
            "source": source,
            "availability": _SOURCE_AVAILABILITY[source],
            "valueType": value_type,
            "designType": _DESIGN_TYPES.get(value_type.casefold(), value_type),
            "usage": text(item.get("usage")),
            "identityObligationRef": text(item.get("identity_obligation_ref")) or None,
            "evidenceRefs": list(dict.fromkeys(
                text(ref) for ref in item.get("requirement_ids") or [] if text(ref)
            )),
        })
    return tuple(catalog)


def server_context_sources(use_case: UseCase) -> tuple[dict[str, Any], ...]:
    """Server-owned values are the only required values directly available as context."""
    return tuple(item for item in required_value_catalog(use_case)
                 if item["availability"] == "server_context")


def directly_available_sources(use_case: UseCase) -> tuple[dict[str, Any], ...]:
    """Return the catalog's direct-value candidates; other sources need dataflow."""
    return server_context_sources(use_case)


def value_source_allows_binding(
    item: dict[str, Any], candidate_kind: str, *, boundary_handoff: bool,
) -> bool:
    """Map catalog availability to the existing finite collaboration flows."""
    availability = item.get("availability")
    if availability == "actor_entry":
        return candidate_kind == "use_case_input"
    if availability == "prior_result":
        return candidate_kind == "earlier_step_result"
    if availability == "server_context":
        return candidate_kind == "earlier_step_result" or (
            boundary_handoff and candidate_kind == "required_value"
        )
    return False


def required_value_evidence(use_case: UseCase) -> list[dict[str, Any]]:
    return [dict(item) for item in required_value_catalog(use_case)]


__all__ = ["directly_available_sources", "required_value_catalog", "required_value_evidence", "server_context_sources", "value_source_allows_binding"]
