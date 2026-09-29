"""Typed adapters from a revision plan to delivery inputs.

The planner decides ownership.  This module only translates its already
validated targets to the small input shapes accepted by the delivery services;
it never parses a ref string to guess a stage or follows pipeline order. RTM
targets sent to Implementation are investigation hints, not write boundaries.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.design.service import BatchReviseRequest, ReviseRequest
from app.requirements.contracts.request import FeedbackEdit

from .contracts import RevisionPatchIntent, RevisionPlan, RevisionTarget


class RevisionDeliveryError(ValueError):
    """A plan cannot be represented by a supported bounded delivery input."""


_REQUIREMENTS_STAGE_BY_KIND = {
    # Refined requirements are edited by the actors feedback path.  The
    # requirements orchestrator owns that mapping; this adapter only records
    # the explicit service stage rather than deriving it from ref text.
    "use_case": "use_cases",
    "use_case_spec": "specs",
    # An actor target carries its catalog-issued ACTn as element_id.  The
    # orchestrator passes that exact value to actor modeling; display labels
    # are intentionally not part of this execution boundary.
    "actor": "actors",
}


@dataclass(frozen=True, slots=True)
class DesignRevisionPayload:
    """The bounded design-service input for one approved revision plan."""

    revisions: tuple[ReviseRequest, ...]
    approved_authority_targets: tuple[str, ...]
    approved_downstream_targets: tuple[str, ...]
    patch_intents: tuple[RevisionPatchIntent, ...] = ()

    def batch_request(self) -> BatchReviseRequest:
        """Build the design service request without dropping frozen scope."""
        return BatchReviseRequest(revisions=list(self.revisions))


@dataclass(frozen=True, slots=True)
class ImplementationRevisionPayload:
    """The only target form accepted by implementation feedback execution."""

    confirmed_target_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TestingRepairPayload:
    """Evidence-backed bridge to the existing implementation repair path."""

    confirmed_target_refs: tuple[str, ...]
    repair_file_hints: tuple[str, ...]


def requirements_feedback_edit(
    targets: Iterable[RevisionTarget], instruction: str
) -> FeedbackEdit:
    """Translate requirements targets into one local ``FeedbackEdit``.

    The requirements service accepts one modeling stage per edit.  The adapter
    uses catalog-owned ``kind`` and ``element_id`` fields, never a partitioned
    ``ref`` string.
    """
    selected = _unique_targets(targets)
    _require_instruction(instruction)
    if not selected or any(target.owner != "requirements" for target in selected):
        raise RevisionDeliveryError("Requirements delivery requires requirements-owned targets.")
    if len(selected) == 1 and selected[0].kind == "requirements_stage":
        stage = selected[0].element_id
        if stage not in {"actors", "relationships"}:
            raise RevisionDeliveryError("The requirements stage has no broad revision adapter.")
        return FeedbackEdit(
            stage=stage,  # type: ignore[arg-type]
            scope="broad",
            target_ids=[],
            instruction=instruction.strip(),
        )
    try:
        stages = {_REQUIREMENTS_STAGE_BY_KIND[target.kind] for target in selected}
    except KeyError as error:
        raise RevisionDeliveryError(
            "Requirements delivery does not support the selected target kind."
        ) from error
    if len(stages) != 1:
        raise RevisionDeliveryError("Requirements delivery requires one modeling stage.")
    target_ids = tuple(target.element_id for target in selected)
    if len(set(target_ids)) != len(target_ids):
        raise RevisionDeliveryError("Requirements delivery target IDs must be unique.")
    return FeedbackEdit(
        stage=stages.pop(),  # type: ignore[arg-type]
        scope="local",
        target_ids=list(target_ids),
        instruction=instruction.strip(),
    )


def design_revision_payload(
    plan: RevisionPlan,
    instruction: str,
    *,
    instructions_by_ref: Mapping[str, str] | None = None,
    patch_intents: Iterable[RevisionPatchIntent] | None = None,
) -> DesignRevisionPayload:
    """Create explicit design refs plus frozen authority/downstream boundaries."""
    _require_instruction(instruction)
    authority = tuple(sorted(_execution_targets(plan), key=_design_revision_order))
    if not authority or any(target.owner != "design" for target in authority):
        raise RevisionDeliveryError("Design delivery requires design-owned authority targets.")
    authority_refs = _refs(authority)
    downstream_refs = _refs(plan.downstream_targets)
    patches = tuple(patch_intents or ())
    authority_ref_set = set(authority_refs)
    invalid_patch_targets = sorted(
        {patch.target for patch in patches if patch.target not in authority_ref_set}
    )
    if invalid_patch_targets:
        raise RevisionDeliveryError(
            "Patch intents must target approved design authorities: "
            + ", ".join(invalid_patch_targets)
        )
    revisions = tuple(
        ReviseRequest(
            target=target.ref,
            feedback=str((instructions_by_ref or {}).get(target.ref) or instruction).strip(),
            approved_authority_targets=[target.ref],
            # The batch-level delivery carries the one frozen RTM scope shared by
            # all revisions. Leaving this unset delegates to that exact scope.
            approved_downstream_targets=None,
            patch_intents=[
                patch.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_defaults=True,
                    exclude_none=True,
                )
                for patch in patches
                if patch.target == target.ref
            ],
        )
        for target in authority
    )
    return DesignRevisionPayload(
        revisions=revisions,
        approved_authority_targets=authority_refs,
        approved_downstream_targets=downstream_refs,
        patch_intents=patches,
    )


def _design_revision_order(target: RevisionTarget) -> tuple[int, str]:
    """Apply structural owners before operations and collaboration call plans."""

    order = {
        "class": 0,
        "operation": 1,
        "collaboration": 2,
        "call": 2,
        "api": 3,
        "schema": 3,
    }
    return order.get(target.kind, 4), target.ref


def implementation_revision_payload(
    targets: Iterable[RevisionTarget],
) -> ImplementationRevisionPayload:
    """Return only validated implementation ``file``/``task`` refs.

    A missing or broader target is an error.  Passing ``None`` would make the
    downstream worker decide scope from feedback prose, which this boundary
    intentionally never permits.
    """
    selected = _unique_targets(targets)
    if not selected:
        raise RevisionDeliveryError("Implementation delivery requires at least one target.")
    invalid = [
        target.ref
        for target in selected
        if target.owner != "implementation" or target.kind not in {"file", "task"}
    ]
    if invalid:
        raise RevisionDeliveryError(
            "Implementation delivery supports only validated file or task targets: "
            + ", ".join(invalid)
        )
    return ImplementationRevisionPayload(confirmed_target_refs=_refs(selected))


def repair_payload_from_testing_evidence(
    evidence: Mapping[str, object],
    targets: Iterable[RevisionTarget],
) -> TestingRepairPayload:
    """Bridge an implementation-owned testing finding with optional RTM hints.

    Testing itself has no target-mutation service.  This function therefore
    validates any supplied file/task targets but treats them and file evidence
    as investigation hints.  It does not mark a finding or a test projection
    editable, and lack of a file-level trace does not block evidence-led repair.
    """
    if str(evidence.get("repair_owner") or "") != "implementation":
        raise RevisionDeliveryError("Testing evidence does not assign repair to implementation.")
    selected = _unique_targets(targets)
    invalid = [
        target.ref
        for target in selected
        if target.owner != "implementation" or target.kind not in {"file", "task"}
    ]
    if invalid:
        raise RevisionDeliveryError(
            "Testing repair hints support only implementation file or task targets: "
            + ", ".join(invalid)
        )
    file_hints = _string_values(evidence.get("file_hints"))
    return TestingRepairPayload(
        confirmed_target_refs=_refs(selected),
        repair_file_hints=tuple(sorted(set(file_hints))),
    )


def revision_delivery_payload(
    plan: RevisionPlan,
    instruction: str,
    *,
    instructions_by_ref: Mapping[str, str] | None = None,
    patch_intents: Iterable[RevisionPatchIntent] | None = None,
) -> FeedbackEdit | DesignRevisionPayload | ImplementationRevisionPayload:
    """Dispatch one executable plan to its supported typed delivery payload."""
    targets = _execution_targets(plan)
    owners = {target.owner for target in targets}
    if len(owners) != 1:
        raise RevisionDeliveryError("Revision delivery requires one delivery owner.")
    owner = owners.pop()
    if owner == "requirements":
        return requirements_feedback_edit(targets, instruction)
    if owner == "design":
        return design_revision_payload(
            plan,
            instruction,
            instructions_by_ref=instructions_by_ref,
            patch_intents=patch_intents,
        )
    if owner == "implementation":
        return implementation_revision_payload(targets)
    raise RevisionDeliveryError(
        "Testing revisions have no direct mutation capability; use exact repair evidence instead."
    )


def _execution_targets(plan: RevisionPlan) -> tuple[RevisionTarget, ...]:
    return _unique_targets(plan.authority_targets or plan.requested_targets)


def _unique_targets(targets: Iterable[RevisionTarget]) -> tuple[RevisionTarget, ...]:
    selected = tuple(targets)
    if len({target.ref for target in selected}) != len(selected):
        raise RevisionDeliveryError("Revision targets must be unique.")
    return selected


def _refs(targets: Iterable[RevisionTarget]) -> tuple[str, ...]:
    return tuple(sorted({target.ref for target in targets}))


def _string_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(
        sorted({item.strip() for item in value if isinstance(item, str) and item.strip()})
    )


def _require_instruction(instruction: str) -> None:
    if not isinstance(instruction, str) or not instruction.strip():
        raise RevisionDeliveryError("Revision delivery requires a non-empty instruction.")


__all__ = [
    "DesignRevisionPayload",
    "ImplementationRevisionPayload",
    "RevisionDeliveryError",
    "TestingRepairPayload",
    "design_revision_payload",
    "implementation_revision_payload",
    "repair_payload_from_testing_evidence",
    "requirements_feedback_edit",
    "revision_delivery_payload",
]
