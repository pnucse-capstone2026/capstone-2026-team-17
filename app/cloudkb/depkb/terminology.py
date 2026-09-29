"""Controlled terminology for empirical cloud-resource dependency claims.

Only terms in this module may drive DepKB reasoning.  Human-readable notes are
descriptions, never executable predicates.  The research lineage and permitted
interpretations are documented in ``document/terminology-ledger.md``.
"""

from __future__ import annotations

RELATION_FAMILIES = frozenset({"provisioning", "runtime"})
CONDITION_KINDS = frozenset(
    {"always", "conditional", "placement", "exclusiveChoice", "compatibility"}
)

FINDINGS_BY_FAMILY = {
    "provisioning": frozenset(
        {
            "mandatoryForProvisioning",
            "conditionalForProvisioning",
            "notMandatoryForProvisioning",
        }
    ),
    "runtime": frozenset(
        {"runtimeRequiredForSignal", "noRuntimeEffectObserved"}
    ),
}

REALIZATION_BEHAVIORS = frozenset(
    {"providerDefaulted", "providerCreated", "explicitlyAttachable"}
)
EVIDENCE_STATUSES = frozenset({"confirmed", "inconclusive", "conflicting"})
STUDY_DISPOSITIONS = frozenset({"included", "excludedByScope"})
ACQUISITION_METHODS = frozenset(
    {
        "schemaDeclaration",
        "controlPlaneValidation",
        "provisioningExecution",
        "runtimeProbe",
    }
)
REPLICATION_STATUSES = frozenset({"pending", "replicated", "failed"})

# Scientific claim values that belonged to the retired universal-verdict model.
FORBIDDEN_CLAIM_VALUES = frozenset(
    {"required", "optional", "holds", "unknown", "outOfScope"}
)


def validate_claim(claim: dict) -> None:
    """Reject ambiguous or unregistered scientific claim terms."""
    family = claim.get("relationFamily")
    finding = claim.get("finding")
    if family not in RELATION_FAMILIES:
        raise ValueError(f"unregistered relation family: {family!r}")
    if finding not in FINDINGS_BY_FAMILY[family]:
        raise ValueError(f"finding {finding!r} is invalid for {family!r}")
    if claim.get("evidenceStatus") not in EVIDENCE_STATUSES:
        raise ValueError(f"invalid evidenceStatus: {claim.get('evidenceStatus')!r}")
    if claim.get("studyDisposition") not in STUDY_DISPOSITIONS:
        raise ValueError(f"invalid studyDisposition: {claim.get('studyDisposition')!r}")
    if claim.get("replicationStatus") not in REPLICATION_STATUSES:
        raise ValueError(f"invalid replicationStatus: {claim.get('replicationStatus')!r}")
    behavior = claim.get("realizationBehavior")
    if behavior is not None and behavior not in REALIZATION_BEHAVIORS:
        raise ValueError(f"invalid realizationBehavior: {behavior!r}")
    if not isinstance(claim.get("condition"), dict):
        raise TypeError("condition must be a structured object")
    condition = claim["condition"]
    if condition.get("kind") not in CONDITION_KINDS:
        raise ValueError(f"unregistered condition kind: {condition.get('kind')!r}")
    machine = condition.get("machine")
    if machine is not None and not isinstance(machine, dict):
        raise ValueError("condition.machine must be an object")
    if condition.get("kind") == "exclusiveChoice":
        cardinality = (machine or {}).get("cardinality")
        if cardinality != {"min": 1, "max": 1}:
            raise ValueError("exclusiveChoice must declare cardinality 1..1")
    if not claim.get("decisionRule"):
        raise ValueError("decisionRule is required")
    observations = claim.get("observations") or []
    if not observations:
        raise ValueError("at least one observation is required")
    for observation in observations:
        method = observation.get("acquisitionMethod")
        if method not in ACQUISITION_METHODS:
            raise ValueError(f"invalid acquisitionMethod: {method!r}")
        if not observation.get("expectedOutcome") or not observation.get("actualOutcome"):
            raise ValueError("every observation needs expectedOutcome and actualOutcome")
        if method == "schemaDeclaration" and not observation.get("cite"):
            raise ValueError("schemaDeclaration observations need a source locator")
        if method != "schemaDeclaration":
            required = {"experiment", "step", "resultFile", "definition"}
            missing = required - set(observation)
            if missing:
                raise ValueError(f"dynamic observation is missing coordinates: {sorted(missing)}")
    provenance = claim.get("provenance")
    if not isinstance(provenance, dict) or not provenance.get("migrationSource"):
        raise ValueError("claim provenance is required")
