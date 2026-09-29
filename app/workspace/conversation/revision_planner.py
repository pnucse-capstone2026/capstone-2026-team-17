"""Read-only, deterministic revision planning.

This module decides *whether* an already-selected target can be revised locally
or needs user confirmation for a bounded revision plan. It never calls a revision
service, creates a workspace command, or writes an artifact.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import ClassVar, Literal

from app.artifact_trace import TraceRef

from .contracts import RevisionInterpretation, RevisionPlan, RevisionTarget
from .project_tools import ProjectTools

PlanStatus = Literal[
    "ready_local", "needs_confirmation", "needs_clarification", "unsupported"
]


@dataclass(frozen=True, slots=True)
class OwnershipRule:
    """A supported fact owner, not a generic traversal permission."""

    local: bool
    upstream_kinds: frozenset[str] = frozenset()
    requires_exact_design_link: bool = False
    requires_exact_trace_link: bool = False
    missing_exact_is_unsupported: bool = False
    confirmation: bool = False


class OwnershipRegistry:
    """Small fail-closed registry of revision paths the application supports."""

    _LOCAL_REQUIREMENTS = OwnershipRule(local=True)
    _LOCAL_DESIGN = OwnershipRule(local=True)
    _LOCAL_IMPLEMENTATION = OwnershipRule(local=True)
    _RULES: ClassVar[dict[tuple[str, str], OwnershipRule]] = {
        # Requirement facts are revised in their requirement artifact. A
        # presentation-only change is not registered because no separate
        # presentation source exists in the current model.
        **{
            (kind, scope): OwnershipRule(local=True)
            for kind in {"actor", "use_case", "use_case_spec", "relationship"}
            for scope in {"contract", "behavior"}
        },
        # An ERD entity is a deterministic class projection.  It is planable
        # so its impact can be reported, but it is never directly editable.
        **{
            ("entity", scope): OwnershipRule(
                local=False,
                upstream_kinds=frozenset({"class"}),
                requires_exact_trace_link=True,
                confirmation=True,
            )
            for scope in {"presentation", "contract", "behavior"}
        },
        **{
            (kind, scope): OwnershipRule(local=True)
            for kind in {"class", "operation", "collaboration", "call"}
            for scope in {"presentation", "contract", "behavior"}
        },
        **{
            (kind, scope): OwnershipRule(local=True)
            for kind in {"api", "schema"}
            for scope in {"presentation", "contract"}
        },
        **{
            ("workload", scope): OwnershipRule(local=True)
            for scope in {"presentation", "contract", "behavior"}
        },
        # A sequence topology is a projection of an explicit class
        # collaboration/operation. Broad trace provenance is intentionally not
        # enough to enter this rule.
        **{
            ("sequence", scope): OwnershipRule(
                local=False,
                upstream_kinds=frozenset({"class", "operation", "collaboration", "call"}),
                requires_exact_design_link=True,
                confirmation=True,
            )
            for scope in {"contract", "behavior"}
        },
        **{
            (kind, "implementation"): OwnershipRule(local=True)
            for kind in {"file", "task"}
        },
        **{
            (kind, "behavior"): OwnershipRule(
                local=False,
                upstream_kinds=frozenset(
                    {
                        "use_case",
                        "use_case_spec",
                        "class",
                        "operation",
                        "collaboration",
                        "call",
                        "api",
                        "schema",
                    }
                ),
                requires_exact_trace_link=True,
                confirmation=True,
            )
            for kind in {"file", "task"}
        },
        # Testing has no direct artifact mutation endpoint.  A future
        # catalog-owned test/finding projection can therefore only nominate
        # one exact API source for a user confirmation; it cannot
        # become an executable testing authority by stage order or keywords.
        **{
            (kind, "test_expectation"): OwnershipRule(
                local=False,
                upstream_kinds=frozenset({"api"}),
                requires_exact_trace_link=True,
                missing_exact_is_unsupported=True,
                confirmation=True,
            )
            for kind in {"test", "finding"}
        },
        ("finding", "implementation"): OwnershipRule(
            local=False,
            upstream_kinds=frozenset({"file", "task"}),
            requires_exact_trace_link=True,
            missing_exact_is_unsupported=True,
            confirmation=True,
        ),
    }

    def lookup(self, kind: str, semantic_scope: str) -> OwnershipRule | None:
        return self._RULES.get((kind, semantic_scope))


class RevisionPlanner:
    """Plan revision authority and impact from one frozen ProjectTools view."""

    def __init__(
        self,
        tools: ProjectTools,
        *,
        registry: OwnershipRegistry | None = None,
        origin_stage: Literal["requirements", "design", "implementation", "testing"] | None = None,
    ):
        self.tools = tools
        self.registry = registry or OwnershipRegistry()
        self.origin_stage = origin_stage

    def plan(self, interpretation: RevisionInterpretation | Mapping[str, object]) -> RevisionPlan:
        intent = (
            interpretation
            if isinstance(interpretation, RevisionInterpretation)
            else RevisionInterpretation.model_validate(interpretation)
        )
        snapshot = self.tools.revision_snapshot()
        if not intent.targets:
            return self._result(
                intent,
                snapshot,
                status="needs_clarification",
                requested=(),
                reasons=("no_target_selected",),
                explanation=intent.clarification or "Select the current artifact to revise.",
            )
        try:
            requested = tuple(self._normalize_targets(intent.targets, require_editable=False))
        except (TypeError, ValueError) as error:
            return self._result(
                intent,
                snapshot,
                status="needs_clarification",
                requested=(),
                reasons=("invalid_or_stale_target",),
                explanation=f"The target is not editable in the current snapshot: {error}",
            )
        owners = {target.owner for target in requested}
        if len(owners) != 1:
            return self._result(
                intent,
                snapshot,
                status="needs_clarification",
                requested=requested,
                reasons=("multiple_delivery_owners",),
                explanation="One revision plan can select only one delivery owner.",
            )
        if intent.semantic_scope == "unknown":
            return self._result(
                intent,
                snapshot,
                status="needs_clarification",
                requested=requested,
                reasons=("unknown_semantic_scope",),
                explanation=intent.clarification or "Specify whether the change affects presentation, contract, or behavior.",
            )
        broad_requirements = {
            "actor": "actors",
            "relationship": "relationships",
        }
        if len(requested) == 1 and requested[0].kind == "requirements_stage":
            stage = requested[0].element_id
            if stage not in {"actors", "relationships"}:
                return self._result(
                    intent,
                    snapshot,
                    status="unsupported",
                    requested=requested,
                    reasons=("unsupported_requirements_stage",),
                    explanation="This requirements section has no bounded broad revision adapter.",
                )
            relations = self.tools.revision_relations(requested)
            return self._result(
                intent,
                snapshot,
                status="needs_confirmation",
                requested=requested,
                authority=requested,
                downstream=self._downstream_targets(relations, requested),
                execution_mode="stage_rewind",
                reasons=("stage_rewind_requires_confirmation",),
                explanation=(
                    "This requirements editor regenerates the complete owning section and "
                    "its downstream model. Confirm the displayed scope before continuing."
                ),
            )
        if len(requested) == 1 and requested[0].kind in broad_requirements:
            marker_ref = str(
                TraceRef("requirements_stage", broad_requirements[requested[0].kind])
            )
            authority = tuple(
                self._normalize_targets([marker_ref], require_editable=False)
            )
            relations = self.tools.revision_relations(authority)
            return self._result(
                intent,
                snapshot,
                status="needs_confirmation",
                requested=requested,
                authority=authority,
                downstream=self._downstream_targets(relations, authority),
                execution_mode="stage_rewind",
                reasons=("targeted_reviser_unavailable", "stage_rewind_requires_confirmation"),
                explanation=(
                    "This requirements editor regenerates the complete owning section and "
                    "its downstream model. Confirm the displayed scope before continuing."
                ),
            )
        if all(target.kind == "design_stage" for target in requested):
            if len(requested) != 1:
                return self._result(
                    intent,
                    snapshot,
                    status="needs_clarification",
                    requested=requested,
                    reasons=("multiple_stage_rewind_targets",),
                    explanation="Select one design stage to regenerate.",
                )
            supported = {"class_diagram", "api_spec", "deployment_diagram"}
            if requested[0].element_id not in supported:
                return self._result(
                    intent,
                    snapshot,
                    status="unsupported",
                    requested=requested,
                    reasons=("unsafe_stage_rewind",),
                    explanation=(
                        "This derived stage cannot accept broad feedback safely. Select an "
                        "exact editable source element instead."
                    ),
                )
            relations = self.tools.revision_relations(requested)
            return self._result(
                intent,
                snapshot,
                status="needs_confirmation",
                requested=requested,
                authority=requested,
                downstream=self._downstream_targets(relations, requested),
                execution_mode="stage_rewind",
                reasons=("targeted_reviser_unavailable", "stage_rewind_requires_confirmation"),
                explanation=(
                    "This broad change requires regenerating the selected design stage and "
                    "its current downstream artifacts. Confirm this scope before continuing."
                ),
            )
        read_only_deployment = all(
            target.owner == "design"
            and (
                target.kind == "resource"
                or (
                    target.kind == "workload"
                    and not target.ref.startswith("deployment_diagram:")
                )
            )
            for target in requested
        )
        if read_only_deployment:
            try:
                authority = tuple(self._normalize_targets(
                    ["design_stage:deployment_diagram"],
                    require_editable=False,
                ))
            except (TypeError, ValueError):
                return self._result(
                    intent,
                    snapshot,
                    status="unsupported",
                    requested=requested,
                    reasons=("targeted_reviser_unavailable",),
                    explanation="The selected deployment projection has no current owning stage.",
                )
            relations = self.tools.revision_relations(authority)
            return self._result(
                intent,
                snapshot,
                status="needs_confirmation",
                requested=requested,
                authority=authority,
                downstream=self._downstream_targets(relations, authority),
                execution_mode="stage_rewind",
                reasons=("targeted_reviser_unavailable", "stage_rewind_requires_confirmation"),
                explanation=(
                    "This deployment projection is not directly editable. Confirm regenerating "
                    "its owning deployment stage with the requested change."
                ),
            )
        rules = [self.registry.lookup(target.kind, intent.semantic_scope) for target in requested]
        if any(rule is None for rule in rules):
            unsupported = sorted(
                target.ref for target, rule in zip(requested, rules, strict=True) if rule is None
            )
            return self._result(
                intent,
                snapshot,
                status="unsupported",
                requested=requested,
                reasons=("unsupported_owner_scope",),
                explanation=(
                    "The current revision path does not support "
                    f"{intent.semantic_scope} changes for: {', '.join(unsupported)}"
                ),
            )
        resolved_rules = tuple(rule for rule in rules if rule is not None)
        if len({rule for rule in resolved_rules}) != 1:
            return self._result(
                intent,
                snapshot,
                status="needs_clarification",
                requested=requested,
                reasons=("mixed_ownership_rules",),
                explanation="The selected targets use different ownership rules.",
            )
        rule = resolved_rules[0]
        relations = self.tools.revision_relations(requested)
        downstream = self._downstream_targets(relations, requested)

        api_path_scope = self._api_path_change_scope(intent, requested)
        if api_path_scope is not None:
            authority = tuple(api_path_scope.get("authority_targets") or ())
            unsupported = ", ".join(api_path_scope.get("unsupported") or ())
            if len(authority) != 1:
                return self._result(
                    intent,
                    snapshot,
                    status="needs_clarification",
                    requested=requested,
                    downstream=downstream,
                    reasons=("api_path_missing_boundary_authority",),
                    explanation=(
                        f"The requested path value ({unsupported}) is not exposed by the "
                        "current Boundary contract, and no exact editable Boundary operation "
                        "could be resolved."
                    ),
                )
            authority_relations = self.tools.revision_relations(authority)
            return self._result(
                intent,
                snapshot,
                status="needs_confirmation",
                requested=requested,
                authority=authority,
                upstream=authority,
                downstream=self._downstream_targets(authority_relations, authority),
                reasons=(
                    "api_path_requires_upstream_contract",
                    "target_outside_request",
                ),
                explanation=(
                    f"The requested path value ({unsupported}) is not exposed by the current "
                    f"Boundary contract. Confirm revising {authority[0].display_label} and "
                    "regenerating its linked downstream artifacts."
                ),
            )

        if not rule.local and intent.change_type == "unknown":
            return self._result(
                intent,
                snapshot,
                status="needs_clarification",
                requested=requested,
                downstream=downstream,
                reasons=("unknown_change_type",),
                explanation="Specify whether the requested change modifies, adds, renames, or removes the contract.",
            )

        if rule.local:
            stage_transition_reason = self._delivery_stage_confirmation_reason(requested)
            identity_change = intent.change_type in {"rename", "remove"}
            reasons = ["free_text_revision_requires_confirmation"]
            if identity_change:
                reasons.append("identity_change_requires_confirmation")
            if stage_transition_reason is not None:
                reasons.append(stage_transition_reason)
            return self._result(
                intent,
                snapshot,
                status="needs_confirmation",
                requested=requested,
                authority=requested,
                downstream=downstream,
                reasons=reasons,
                explanation=(
                    "Review and confirm the requested change and its displayed downstream scope "
                    "before the owning delivery stage revises this editable target."
                ),
            )

        candidates = self._upstream_candidates(rule, requested, relations)
        if len(candidates) == 1:
            authority = candidates
            authority_relations = self.tools.revision_relations(authority)
            cascade = self._downstream_targets(authority_relations, authority)
            return self._result(
                intent,
                snapshot,
                status="needs_confirmation",
                requested=requested,
                authority=authority,
                upstream=candidates,
                downstream=cascade,
                reasons=("upstream_authority", "target_outside_request"),
                explanation="An exactly linked upstream artifact owns the requested fact. Confirm this scope.",
            )
        if len(candidates) > 1:
            return self._result(
                intent,
                snapshot,
                status="needs_clarification",
                requested=requested,
                upstream=candidates,
                downstream=downstream,
                reasons=("ambiguous_upstream_authority",),
                explanation="Exact links exist, but they do not identify one authoritative upstream target.",
            )
        return self._result(
            intent,
            snapshot,
            status=(
                "unsupported"
                if rule.requires_exact_design_link or rule.missing_exact_is_unsupported
                else "needs_clarification"
            ),
            requested=requested,
            downstream=downstream,
            reasons=("missing_exact_contract_link",),
            explanation=(
                "No explicit contract link exists, so an upstream artifact cannot be guessed."
                if rule.requires_exact_design_link or rule.missing_exact_is_unsupported
                else "No linked upstream authority establishes the scope of this behavior change."
            ),
        )

    def _api_path_change_scope(
        self,
        intent: RevisionInterpretation,
        requested: tuple[RevisionTarget, ...],
    ) -> Mapping[str, object] | None:
        if (
            intent.semantic_scope != "contract"
            or len(requested) != 1
            or requested[0].kind != "api"
        ):
            return None
        resolve = getattr(self.tools, "api_path_change_scope", None)
        if not callable(resolve):
            return None
        scope = resolve(requested[0].ref, intent.requested_effect)
        return scope if isinstance(scope, Mapping) else None

    def validate_plan(
        self,
        plan: RevisionPlan,
        interpretation: RevisionInterpretation | Mapping[str, object] | None = None,
    ) -> bool:
        """Re-read persisted artifacts and reject a plan whose frozen inputs moved."""
        fresh = ProjectTools(self.tools.app_id) if isinstance(self.tools, ProjectTools) else self.tools
        current = fresh.revision_snapshot(refresh=True)
        snapshot_matches = (
            current.get("trace_digest") == plan.trace_digest
            and current.get("artifact_versions") == plan.artifact_versions
        )
        if not snapshot_matches:
            return False
        if interpretation is None:
            return True
        return RevisionPlanner(
            fresh,
            registry=self.registry,
            origin_stage=self.origin_stage,
        ).plan(interpretation).plan_digest == plan.plan_digest

    def plan_is_stale(self, plan: RevisionPlan) -> bool:
        return not self.validate_plan(plan)

    def _delivery_stage_confirmation_reason(
        self,
        targets: tuple[RevisionTarget, ...],
    ) -> str | None:
        """Require confirmation whenever a revision moves to another owner stage."""

        current = self.origin_stage
        if current is None:
            try:
                workspace = self.tools.read_workspace()
            except AttributeError:
                return None
            # ProjectTools exposes the public workspace field as ``current_stage``.
            # Keep ``stage`` only as a compatibility fallback for small test doubles.
            current = str(workspace.get("current_stage") or workspace.get("stage") or "")
        order = {"requirements": 0, "design": 1, "implementation": 2, "testing": 3}
        current_index = order.get(current)
        if current_index is None:
            return None
        owner_indexes = {
            order[target.owner]
            for target in targets
            if target.owner in order and target.owner != current
        }
        if not owner_indexes:
            return None
        if any(owner_index < current_index for owner_index in owner_indexes):
            return "earlier_delivery_stage_requires_confirmation"
        return "delivery_stage_transition_requires_confirmation"

    def _upstream_candidates(
        self,
        rule: OwnershipRule,
        requested: tuple[RevisionTarget, ...],
        relations: Mapping[str, object],
    ) -> tuple[RevisionTarget, ...]:
        allowed = rule.upstream_kinds
        refs: set[str] = set()
        if rule.requires_exact_design_link:
            links = relations.get("design_links")
            for link in links if isinstance(links, list) else []:
                if not isinstance(link, Mapping):
                    continue
                source, target = str(link.get("from") or ""), str(link.get("to") or "")
                for requested_target in requested:
                    if source == requested_target.ref:
                        refs.add(target)
                    elif target == requested_target.ref:
                        refs.add(source)
        else:
            relation_map = relations.get("relations")
            if isinstance(relation_map, Mapping):
                for requested_target in requested:
                    item = relation_map.get(requested_target.ref)
                    if isinstance(item, Mapping):
                        # ProjectTools supplies direct sources explicitly.
                        # The fallback preserves compatibility with a small
                        # read-only test double, not runtime trace behavior.
                        source_refs = (
                            item.get("direct_upstream")
                            if rule.requires_exact_trace_link
                            and isinstance(item.get("direct_upstream"), list)
                            else item.get("upstream", [])
                        )
                        refs.update(
                            str(ref) for ref in source_refs if isinstance(ref, str)
                        )
        candidates = self._normalizable_refs(refs, allowed, require_editable=True)
        return tuple(sorted(candidates, key=lambda target: target.ref))

    def _downstream_targets(
        self,
        relations: Mapping[str, object],
        targets: Iterable[RevisionTarget],
    ) -> tuple[RevisionTarget, ...]:
        relation_map = relations.get("relations")
        if not isinstance(relation_map, Mapping):
            return ()
        refs: set[str] = set()
        for target in targets:
            item = relation_map.get(target.ref)
            if isinstance(item, Mapping):
                refs.update(str(ref) for ref in item.get("downstream", []) if isinstance(ref, str))
        downstream = self._normalizable_refs(refs, None, require_editable=False)
        requested_refs = {target.ref for target in targets}
        return tuple(
            sorted(
                (target for target in downstream if target.ref not in requested_refs),
                key=_pipeline_target_key,
            )
        )

    def _normalizable_refs(
        self,
        refs: Iterable[str],
        allowed_kinds: frozenset[str] | None,
        *,
        require_editable: bool,
    ) -> tuple[RevisionTarget, ...]:
        candidates: list[RevisionTarget] = []
        for ref in sorted(set(refs)):
            try:
                target = self._normalize_targets([ref], require_editable=require_editable)[0]
            except (TypeError, ValueError):
                continue
            if allowed_kinds is None or target.kind in allowed_kinds:
                candidates.append(target)
        return tuple(candidates)

    def _normalize_targets(
        self, refs: Iterable[str], *, require_editable: bool
    ) -> list[RevisionTarget]:
        """Use selection-aware tools while retaining read-only test doubles."""
        values = list(refs)
        try:
            return self.tools.normalize_revision_targets(
                values, require_editable=require_editable
            )
        except TypeError as error:
            # Older in-memory tool fixtures model only executable targets.
            # They are safe for selection tests because every target they
            # expose is already editable; production ProjectTools always uses
            # the keyword form above.
            if "require_editable" not in str(error):
                raise
            return self.tools.normalize_revision_targets(values)

    def _result(
        self,
        intent: RevisionInterpretation,
        snapshot: Mapping[str, object],
        *,
        status: PlanStatus,
        requested: Iterable[RevisionTarget],
        authority: Iterable[RevisionTarget] = (),
        upstream: Iterable[RevisionTarget] = (),
        downstream: Iterable[RevisionTarget] = (),
        reasons: Iterable[str],
        explanation: str,
        execution_mode: Literal["targeted_revision", "stage_rewind", "none"] | None = None,
    ) -> RevisionPlan:
        requested_items = tuple(sorted(requested, key=lambda target: target.ref))
        authority_items = tuple(sorted(authority, key=lambda target: target.ref))
        upstream_items = tuple(sorted(upstream, key=lambda target: target.ref))
        downstream_items = tuple(sorted(set(downstream), key=_pipeline_target_key))
        selected_mode: Literal["targeted_revision", "stage_rewind", "none"] = (
            execution_mode
            if execution_mode is not None
            else "targeted_revision"
            if status in {"ready_local", "needs_confirmation"} and authority_items
            else "none"
        )
        digest_payload = {
            "requested_effect": intent.requested_effect,
            "semantic_scope": intent.semantic_scope,
            "change_type": intent.change_type,
            "requested_targets": [
                {"ref": target.ref, "element_id": target.element_id}
                for target in requested_items
            ],
            "authority_targets": [
                {"ref": target.ref, "element_id": target.element_id}
                for target in authority_items
            ],
            "artifact_versions": snapshot.get("artifact_versions", {}),
            "trace_digest": snapshot.get("trace_digest", ""),
            "execution_mode": selected_mode,
            "status": status,
        }
        return RevisionPlan(
            plan_digest=_digest(digest_payload),
            status=status,
            requested_targets=list(requested_items),
            authority_targets=list(authority_items),
            upstream_candidates=list(upstream_items),
            downstream_targets=list(downstream_items),
            execution_mode=selected_mode,
            reason_codes=sorted(set(reasons)),
            explanation=explanation,
            artifact_versions=dict(snapshot.get("artifact_versions", {})),
            trace_digest=str(snapshot.get("trace_digest", "")),
        )


def plan_revision(
    tools: ProjectTools,
    interpretation: RevisionInterpretation | Mapping[str, object],
    *,
    origin_stage: Literal["requirements", "design", "implementation", "testing"] | None = None,
) -> RevisionPlan:
    """Function-form API for callers that do not need to retain a planner."""
    return RevisionPlanner(tools, origin_stage=origin_stage).plan(interpretation)


def validate_plan(
    tools: ProjectTools,
    plan: RevisionPlan,
    interpretation: RevisionInterpretation | Mapping[str, object] | None = None,
    *,
    origin_stage: Literal["requirements", "design", "implementation", "testing"] | None = None,
) -> bool:
    """Validate artifact versions and trace digest from a newly-read snapshot."""
    return RevisionPlanner(tools, origin_stage=origin_stage).validate_plan(plan, interpretation)


def _pipeline_target_key(target: RevisionTarget) -> tuple[int, str]:
    order = {"requirements": 0, "design": 1, "implementation": 2, "testing": 3}
    return (order.get(target.owner, len(order)), target.ref)


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "OwnershipRegistry",
    "OwnershipRule",
    "RevisionPlanner",
    "plan_revision",
    "validate_plan",
]
