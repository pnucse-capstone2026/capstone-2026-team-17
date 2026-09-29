"""Project relation-specific DepKB findings into a provisioning closure.

The claim ledger is authoritative.  Descriptive prose is never parsed and no
universal required/optional/holds verdict is reconstructed here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.cloudkb.depkb.scope import VM_ANCHOR_TYPES, is_vm_claim
from app.cloudkb.depkb.terminology import validate_claim

_ARTIFACT = Path(__file__).resolve().parent / "claims.json"


@dataclass(frozen=True)
class MandatoryResource:
    id: str
    because: tuple[str, ...]
    condition: dict


@dataclass(frozen=True)
class NonMandatoryResource:
    id: str
    realizationBehavior: str | None
    condition: dict


@dataclass(frozen=True)
class Decision:
    about: str
    kind: str
    condition: dict


@dataclass(frozen=True)
class UnavailableFinding:
    subject: str
    object: str
    relationFamily: str
    finding: str
    evidenceStatus: str
    replicationStatus: str


@dataclass(frozen=True)
class Closure:
    startResource: str
    csp: str
    mandatoryForProvisioning: tuple[MandatoryResource, ...]
    createOrder: tuple[str, ...]
    nonMandatoryForProvisioning: tuple[NonMandatoryResource, ...]
    decisions: tuple[Decision, ...]
    runtimeRequiredForSignal: tuple[tuple[str, str, str], ...]
    unavailableFindings: tuple[UnavailableFinding, ...]


@lru_cache(maxsize=1)
def _claims() -> list[dict]:
    claims = json.loads(_ARTIFACT.read_text(encoding="utf-8"))["claims"]
    for claim in claims:
        validate_claim(claim)
        if not is_vm_claim(claim):
            raise ValueError(
                "DepKB contains a claim outside Docker-on-VM scope: "
                f"{claim.get('subject')}->{claim.get('object')}"
            )
    return claims


def _topological_order(nodes: set[str], edges: set[tuple[str, str]]) -> tuple[str, ...]:
    """Return dependencies before dependants for A->B meaning A depends on B."""
    order: list[str] = []
    remaining = set(nodes)
    while remaining:
        ready = sorted(
            node for node in remaining
            if not any((node, dependency) in edges for dependency in remaining)
        )
        if not ready:
            raise ValueError(f"cycle in mandatory provisioning findings: {sorted(remaining)}")
        order.append(ready[0])
        remaining.remove(ready[0])
    return tuple(order)


def closure(start_resource: str, csp: str) -> Closure:
    if start_resource not in VM_ANCHOR_TYPES:
        raise KeyError(
            f"resource cannot start a Docker-on-VM plan: {start_resource}; "
            f"allowed={sorted(VM_ANCHOR_TYPES)}"
        )
    reviewed_rows = [
        claim for claim in _claims()
        if claim["csp"] == csp
        and claim["studyDisposition"] == "included"
        and claim["evidenceStatus"] == "confirmed"
    ]
    if not reviewed_rows:
        raise KeyError(f"no included, confirmed DepKB findings for CSP: {csp}")
    rows = [
        claim for claim in reviewed_rows if claim["replicationStatus"] == "replicated"
    ]

    provisioning = [r for r in rows if r["relationFamily"] == "provisioning"]
    mandatory: dict[str, dict] = {}
    non_mandatory: dict[str, NonMandatoryResource] = {}
    decisions: list[Decision] = []
    seen, queue = {start_resource}, [start_resource]
    while queue:
        current = queue.pop(0)
        for claim in provisioning:
            if claim["subject"] != current:
                continue
            label = f"{claim['subject']}→{claim['object']}"
            finding = claim["finding"]
            if finding == "conditionalForProvisioning":
                decisions.append(Decision(label, claim["condition"]["kind"], claim["condition"]))
                continue
            if finding == "mandatoryForProvisioning":
                entry = mandatory.setdefault(
                    claim["object"], {"because": set(), "condition": claim["condition"]}
                )
                entry["because"].add(label)
                if claim["object"] not in seen:
                    seen.add(claim["object"])
                    queue.append(claim["object"])
            else:
                non_mandatory.setdefault(
                    claim["object"],
                    NonMandatoryResource(
                        claim["object"], claim.get("realizationBehavior"), claim["condition"]
                    ),
                )

    non_mandatory = {key: value for key, value in non_mandatory.items() if key not in seen}
    mandatory_edges = {
        (claim["subject"], claim["object"])
        for claim in provisioning
        if claim["finding"] == "mandatoryForProvisioning"
        and claim["subject"] in seen and claim["object"] in seen
    }
    scope = seen | set(non_mandatory)

    runtime = tuple(sorted(
        (claim["subject"], claim["object"], str(claim.get("signal") or ""))
        for claim in rows
        if claim["finding"] == "runtimeRequiredForSignal"
        and claim["subject"] in scope
    ))
    unavailable = tuple(
        UnavailableFinding(
            subject=claim["subject"],
            object=claim["object"],
            relationFamily=claim["relationFamily"],
            finding=claim["finding"],
            evidenceStatus=claim["evidenceStatus"],
            replicationStatus=claim["replicationStatus"],
        )
        for claim in sorted(
            reviewed_rows,
            key=lambda item: (
                item["subject"], item["object"], item["relationFamily"], item["finding"]
            ),
        )
        if claim["replicationStatus"] != "replicated"
        and (claim["subject"] in scope or claim["object"] in scope)
    )
    return Closure(
        startResource=start_resource,
        csp=csp,
        mandatoryForProvisioning=tuple(
            MandatoryResource(key, tuple(sorted(value["because"])), value["condition"])
            for key, value in sorted(mandatory.items())
        ),
        createOrder=_topological_order(seen, mandatory_edges),
        nonMandatoryForProvisioning=tuple(non_mandatory[k] for k in sorted(non_mandatory)),
        decisions=tuple(decisions),
        runtimeRequiredForSignal=runtime,
        unavailableFindings=unavailable,
    )


def describe(start_resource: str, csp: str) -> str:
    result = closure(start_resource, csp)
    lines = [f"`{start_resource}` on {csp}:"]
    if result.mandatoryForProvisioning:
        lines.append("  mandatory for provisioning: " + ", ".join(
            item.id for item in result.mandatoryForProvisioning
        ))
        lines.append("  create order: " + " -> ".join(result.createOrder))
    for item in result.nonMandatoryForProvisioning:
        lines.append(f"  not mandatory for provisioning: {item.id} ({item.realizationBehavior})")
    for decision in result.decisions:
        lines.append(f"  unresolved condition: {decision.about} ({decision.kind})")
    return "\n".join(lines)
