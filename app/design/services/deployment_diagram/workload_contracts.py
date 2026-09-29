"""Small, evidence-bound workload contract producers.

This module deliberately produces only the contracts already understood by the
WorkloadGraph normalizer.  It does not introduce a database domain object or a
provider service choice.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable
from typing import Any

from app.design.services.deployment_diagram.planning_primitives import refs as _refs

_POSTGRES_RUNTIME_RULE = "explicit-postgresql-runtime-topology"
_POSTGRES_NAME = re.compile(
    r"(?<![a-z0-9])postgres(?:ql)?(?![a-z0-9])", re.IGNORECASE
)
_DATABASE_RUNTIME = re.compile(
    r"(?:\b(?:database|db)(?:\s+(?:runtime|service|server|process|container))?\b|"
    r"데이터베이스|디비)",
    re.IGNORECASE,
)
_SEPARATE_RUNTIME = re.compile(
    r"(?:\b(?:separate|external|self[- ]hosted|standalone|container)\b|"
    r"별도|분리|외부|자체\s*호스팅|컨테이너)",
    re.IGNORECASE,
)
_UNACCEPTED = {"rejected", "pending", "needsquestion", "unknown"}
_MODE_FACT_KIND = "dataExecutionMode"
_EMBEDDED_MODE = "embedded"
_POSTGRESQL_CONTAINER_MODE = "postgresql-container"


def _requirement_records(value: Any) -> Iterable[dict[str, Any]]:
    """Yield only direct refined-requirement records, never ERD or capability data."""

    if isinstance(value, list):
        for index, item in enumerate(value, start=1):
            if isinstance(item, dict):
                yield item
            elif isinstance(item, str) and item.strip():
                # The persisted requirements artifact is currently a list of
                # accepted strings.  Its position is the only stable source
                # address available at this boundary.
                yield {
                    "text": item,
                    "sourceRefs": [f"refinedRequirements:{index}"],
                }
        return
    if not isinstance(value, dict):
        return
    for key in ("requirements", "items", "refinedRequirements"):
        items = value.get(key)
        if isinstance(items, list):
            yield from (item for item in items if isinstance(item, dict))
            return


def _requirement_source_refs(requirement: dict[str, Any]) -> list[str]:
    supplied = _refs(requirement.get("sourceRefs") or requirement.get("source_refs"))
    if supplied:
        return supplied
    identifier = str(requirement.get("id") or "").strip()
    return [f"requirement:{identifier}"] if identifier else []


def _accepted_explicit_postgres_refs(
    refined_requirements: Any,
    capability_contract: dict[str, Any] | None,
) -> list[str]:
    """Return provenance only for accepted, directly sourced Postgres runtime text."""

    refs: list[str] = []
    for requirement in _requirement_records(refined_requirements):
        source_refs = _requirement_source_refs(requirement)
        if not source_refs:
            continue
        status = str(
            requirement.get("status")
            or requirement.get("decision")
            or "accepted"
        ).replace(" ", "").lower()
        authority = str(requirement.get("authority") or "explicit").lower()
        if status in _UNACCEPTED or status != "accepted" or authority != "explicit":
            continue
        text = " ".join(
            str(requirement.get(key) or "")
            for key in ("text", "requirement", "description", "title")
        )
        if not _POSTGRES_NAME.search(text):
            continue
        if not (_DATABASE_RUNTIME.search(text) or _SEPARATE_RUNTIME.search(text)):
            continue
        refs.extend(source_refs)
    for capability in (capability_contract or {}).get("capabilities") or []:
        if not isinstance(capability, dict) or capability.get("decision") != "accepted":
            continue
        explicitly_accepted = (
            capability.get("origin") == "explicit"
            or capability.get("confirmation") == "userConfirmed"
        )
        if not explicitly_accepted:
            continue
        text = " ".join(
            [
                str(capability.get("statement") or ""),
                *(str(item) for item in capability.get("evidenceSpans") or []),
            ]
        )
        if not _POSTGRES_NAME.search(text):
            continue
        if not (_DATABASE_RUNTIME.search(text) or _SEPARATE_RUNTIME.search(text)):
            continue
        refs.extend(
            f"requirement:{item}"
            for item in capability.get("requirementIds") or []
            if str(item)
        )
    return _refs(refs)


def _accepted_explicit_separate_database_refs(refined_requirements: Any) -> list[str]:
    """Find an external DB requirement that deliberately leaves its engine open."""

    refs: list[str] = []
    for requirement in _requirement_records(refined_requirements):
        source_refs = _requirement_source_refs(requirement)
        if not source_refs:
            continue
        status = str(
            requirement.get("status")
            or requirement.get("decision")
            or "accepted"
        ).replace(" ", "").lower()
        authority = str(requirement.get("authority") or "explicit").lower()
        if status in _UNACCEPTED or status != "accepted" or authority != "explicit":
            continue
        text = " ".join(
            str(requirement.get(key) or "")
            for key in ("text", "requirement", "description", "title")
        )
        if _POSTGRES_NAME.search(text):
            continue
        if _DATABASE_RUNTIME.search(text) and _SEPARATE_RUNTIME.search(text):
            refs.extend(source_refs)
    return _refs(refs)


def data_execution_mode_decision(
    refined_requirements: Any,
    *,
    deployment_planning_facts: Iterable[dict[str, Any]] = (),
    capability_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Select an already-approved data runtime or return one UI-ready question.

    ``dataExecutionMode`` is an existing deployment planning fact.  Its
    accepted value has precedence over text-derived evidence.  Logical ERD
    facts intentionally never appear here: an ERD says that data exists, not
    which runtime should execute it.
    """

    accepted_modes: list[tuple[str, list[str]]] = []
    for fact in deployment_planning_facts or ():
        if not isinstance(fact, dict):
            continue
        if fact.get("kind") != _MODE_FACT_KIND or fact.get("status") != "accepted":
            continue
        mode = str(fact.get("value") or "").strip().lower()
        if mode not in {_EMBEDDED_MODE, _POSTGRESQL_CONTAINER_MODE}:
            continue
        accepted_modes.append((mode, _refs(fact.get("sourceRefs"))))
    if accepted_modes:
        modes = {mode for mode, _refs_value in accepted_modes}
        source_refs = _refs(
            reference for _mode, refs in accepted_modes for reference in refs
        )
        if len(modes) == 1:
            return {
                "status": "selected",
                "value": accepted_modes[0][0],
                "sourceRefs": source_refs,
            }
        return {
            "status": "needsInput",
            "sourceRefs": source_refs,
            "question": {
                "field": _MODE_FACT_KIND,
                "reason": "Accepted deployment facts select conflicting data execution modes.",
                "options": [_EMBEDDED_MODE, _POSTGRESQL_CONTAINER_MODE],
            },
        }

    postgres_refs = _accepted_explicit_postgres_refs(
        refined_requirements, capability_contract
    )
    if postgres_refs:
        return {
            "status": "selected",
            "value": _POSTGRESQL_CONTAINER_MODE,
            "sourceRefs": postgres_refs,
        }
    separate_database_refs = _accepted_explicit_separate_database_refs(
        refined_requirements
    )
    if separate_database_refs:
        return {
            "status": "needsInput",
            "sourceRefs": separate_database_refs,
            "question": {
                "field": _MODE_FACT_KIND,
                "reason": "A separate database runtime is required, but its supported execution mode was not selected.",
                "options": [_EMBEDDED_MODE, _POSTGRESQL_CONTAINER_MODE],
            },
        }
    return {"status": "notRequired", "sourceRefs": []}


def _caller_controls_topology(facts: Iterable[dict[str, Any]]) -> bool:
    """Caller contracts win over this narrow requirement-to-topology default."""

    return any(
        item.get("kind") in {"workloadContract", "connectionContract"}
        and item.get("authority") == "explicit"
        and item.get("status") == "accepted"
        and item.get("derivationRule") != _POSTGRES_RUNTIME_RULE
        for item in facts
        if isinstance(item, dict)
    )


def postgresql_runtime_contracts(
    refined_requirements: Any,
    *,
    capability_contract: dict[str, Any] | None = None,
    existing_facts: Iterable[dict[str, Any]] = (),
    resource_spec: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Produce the fixed app/PostgreSQL container topology when evidence permits.

    The caller's accepted explicit topology facts have precedence.  A logical
    data model, generic persistence requirement, or an ungrounded Postgres
    mention cannot enter through this producer.
    """

    facts = [
        copy.deepcopy(item) for item in (existing_facts or ()) if isinstance(item, dict)
    ]
    if _caller_controls_topology(facts):
        return []
    generated_ids = {
        str(item.get("id") or "")
        for item in facts
        if item.get("derivationRule") == _POSTGRES_RUNTIME_RULE
    }
    if {
        "postgresql-runtime-application",
        "postgresql-runtime-database",
        "postgresql-runtime-connection",
    } <= generated_ids:
        return []
    decision = data_execution_mode_decision(
        refined_requirements,
        deployment_planning_facts=facts,
        capability_contract=capability_contract,
    )
    if decision.get("value") != _POSTGRESQL_CONTAINER_MODE:
        return []
    source_refs = list(decision["sourceRefs"])
    requirements = {
        field: value
        for field in ("minVCpu", "minMemoryGiB")
        if isinstance(resource_spec, dict)
        and (value := resource_spec.get(field)) is not None
    }
    # Both containers run on the same deterministic single-VM placement by
    # default.  Carrying the accepted minimum to each workload prevents the
    # topology expansion from turning sizing inputs into unknown values.
    return [
        {
            "id": "postgresql-runtime-application",
            "kind": "workloadContract",
            "value": {
                "workloadId": "application",
                "artifactKind": "generatedApplication",
                "replicaCount": 1,
                "resourceRequirements": requirements,
            },
            "sourceRefs": source_refs,
            "derivationRule": _POSTGRES_RUNTIME_RULE,
            "authority": "explicit",
            "status": "accepted",
        },
        {
            "id": "postgresql-runtime-database",
            "kind": "workloadContract",
            "value": {
                "workloadId": "postgresql",
                "artifactKind": "prebuiltImage",
                "image": "postgres:16",
                "engine": "postgresql",
                "deploymentMode": "container",
                "runtimeCatalogRef": "docker-on-vm/prebuilt-image",
                "interface": {
                    "id": "postgresql-tcp",
                    "name": "PostgreSQL",
                    "protocol": "tcp",
                    "exposure": "internal",
                    "port": 5432,
                },
                "storage": {
                    "id": "postgresql-data",
                    "persistence": "persistent",
                    "capacityGiB": 10,
                    "mountPath": "/var/lib/postgresql/data",
                    "deletionPolicy": "retain",
                    "replicaSemantics": "singleAttachment",
                },
                "replicaCount": 1,
                "resourceRequirements": requirements,
            },
            "sourceRefs": source_refs,
            "derivationRule": _POSTGRES_RUNTIME_RULE,
            "authority": "explicit",
            "status": "accepted",
        },
        {
            "id": "postgresql-runtime-connection",
            "kind": "connectionContract",
            "value": {
                "connectionId": "application-to-postgresql",
                "sourceWorkloadRef": "application",
                "targetWorkloadRef": "postgresql",
                "protocol": "tcp",
                "endpointBindingRequired": True,
                "secretBindingRequired": True,
            },
            "sourceRefs": source_refs,
            "derivationRule": _POSTGRES_RUNTIME_RULE,
            "authority": "explicit",
            "status": "accepted",
        },
    ]
