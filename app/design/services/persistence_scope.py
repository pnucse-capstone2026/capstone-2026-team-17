"""Decide whether the accepted design requires a persistent domain model."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from app.requirements.capability_contract import requires_persistent_storage

ErdDisposition = Literal["required", "class_revision_required", "not_applicable"]


def _deployment_needs(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return accepted requirement evidence with current capability decisions applied."""

    direct = state.get("deployment_needs")
    if isinstance(direct, Mapping):
        needs = {
            str(key): dict(value)
            for key, value in direct.items()
            if isinstance(value, Mapping)
        }
    else:
        usecase_spec = state.get("usecase_spec")
        traceability = (
            usecase_spec.get("traceability")
            if isinstance(usecase_spec, Mapping)
            else None
        )
        traced = (
            traceability.get("deployment_needs")
            if isinstance(traceability, Mapping)
            else None
        )
        needs = {
            str(key): dict(value)
            for key, value in (traced.items() if isinstance(traced, Mapping) else ())
            if isinstance(value, Mapping)
        }

    contract = state.get("capability_contract")
    capabilities = (
        contract.get("capabilities") if isinstance(contract, Mapping) else None
    )
    for capability in capabilities if isinstance(capabilities, list) else ():
        if not isinstance(capability, Mapping):
            continue
        capability_id = str(capability.get("id") or "").strip()
        if not capability_id:
            continue
        need = needs.setdefault(capability_id, {})
        need["decision"] = capability.get("decision")
        need["required"] = capability.get("necessity") == "required"
        need["dependencyCapabilityIds"] = list(
            capability.get("dependencyCapabilityIds") or []
        )
    return needs


def persistence_required_by_contract(state: Mapping[str, Any]) -> bool:
    """Use only accepted structured requirement evidence; never infer from prose."""

    return requires_persistent_storage(_deployment_needs(state))


def entity_names(model: Mapping[str, Any] | None) -> frozenset[str]:
    """Return domain Entity names from one accepted BCE model."""

    classes = model.get("Classes") if isinstance(model, Mapping) else None
    return frozenset(
        str(item.get("className") or "").strip()
        for item in (classes if isinstance(classes, list) else [])
        if isinstance(item, Mapping)
        if str(item.get("stereotype") or "").strip().casefold() == "entity"
        and str(item.get("className") or "").strip()
    )


def erd_disposition(
    class_model: Mapping[str, Any] | None,
    state: Mapping[str, Any],
) -> ErdDisposition:
    """Apply the persistence-evidence × BCE-Entity decision matrix."""

    if entity_names(class_model):
        return "required"
    if persistence_required_by_contract(state):
        return "class_revision_required"
    return "not_applicable"


__all__ = [
    "ErdDisposition",
    "entity_names",
    "erd_disposition",
    "persistence_required_by_contract",
]
