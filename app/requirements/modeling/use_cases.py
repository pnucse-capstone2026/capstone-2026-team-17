"""요구사항 근거가 있는 actor와 user-goal use case modeling stage다."""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypedDict, cast

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from app.requirements import prompts, traceability
from app.requirements.common.state_contract import contract
from app.requirements.config import settings  # re-exported test/configuration seam
from app.requirements.contracts.state import ActorItem, AgentState, RequirementItem, UseCaseItem
from app.requirements.knowledge import rules
from app.requirements.modeling import validation as validator
from app.requirements.modeling.contracts import (
    ModelingStagePatch,
    SemanticReviewCall,
    StructuredProposalCall,
)
from app.requirements.modeling.feedback import feedback_for
from app.requirements.runtime import telemetry
from app.requirements.runtime.structured_llm import invoke_structured
from app.requirements.schemas import Actor, ActorResult, UseCase, UseCaseResult


class _MissingUseCaseCandidate(BaseModel):
    """One independently initiated goal absent from the fixed proposal."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    primary_actor_ref: str = Field(min_length=1)
    supporting_actor_refs: list[str] = Field(default_factory=list)
    primary_actor: str = Field(min_length=1)
    supporting_actors: list[str] = Field(default_factory=list)
    goal: str = Field(min_length=1)


class _RequirementTraceSlice(BaseModel):
    """The complete RTM decision for exactly one accepted requirement."""

    model_config = ConfigDict(extra="forbid")

    requirement_id: str = Field(min_length=1)
    realized_by_use_case_refs: list[str] = Field(default_factory=list)
    # None means this requirement has no UC relationship (for example an actor/domain fact).
    # [] means it is explicitly a system-wide constraint with no justified UC-local target.
    constrains_use_case_refs: list[str] | None = None
    missing_use_case: _MissingUseCaseCandidate | None = None


MissingUseCaseCandidate = _MissingUseCaseCandidate
RequirementTraceSlice = _RequirementTraceSlice


class _ModelReviewResult(TypedDict):
    """모델 검토 단계가 저장하는 고정된 결과 구조다."""

    issues: list[str]
    semantic_status: str
    unexamined_rules: list[str]


def _split_fr_nfr(
    classified: list[RequirementItem],
) -> tuple[list[RequirementItem], list[RequirementItem]]:
    fr = [requirement for requirement in classified if requirement.get("type") == "FR"]
    nfr = [requirement for requirement in classified if requirement.get("type") == "NFR"]
    return fr, nfr


def _listing(items: list[RequirementItem]) -> str:
    return "\n".join(_requirement_line(item) for item in items)


def _requirement_line(item: RequirementItem) -> str:
    """Keep the existing constraint-to-behavior RTM edge visible to later decisions."""
    qualified = [str(value) for value in item.get("qualifies") or [] if str(value)]
    qualifier = f" [qualifies: {', '.join(qualified)}]" if qualified else ""
    return f"- {item['id']}: {item['text']}{qualifier}"


def _accepted_source_refs(values: list[str], accepted_ids: set[str]) -> list[str]:
    """Normalize actor provenance without allowing an unknown requirement reference."""
    return sorted({str(value).strip() for value in values if str(value).strip() in accepted_ids})


def _actor_key(value: str | None) -> str:
    return " ".join(str(value or "").split()).casefold()


def normalize_actors(
    raw_actors: list[Actor], accepted_ids: set[str], existing_actors: list[ActorItem] | None = None
) -> tuple[list[ActorItem], list[str]]:
    """Actor proposal의 identity·parent·source reference를 canonicalize한다."""
    records: dict[str, dict] = {}
    dangling: list[str] = []
    seen_refs: set[str] = set()
    for actor in raw_actors:
        name = " ".join(actor.name.split())
        proposal_ref = str(actor.actor_ref or "").strip()
        if not name:
            dangling.append("blank actor name")
            continue
        if not proposal_ref:
            dangling.append(f"missing actorRef for {name}")
            continue
        if proposal_ref in seen_refs:
            dangling.append(f"duplicate actorRef {proposal_ref}")
            continue
        seen_refs.add(proposal_ref)
        source_refs = _accepted_source_refs(actor.source_refs, accepted_ids)
        records[proposal_ref] = {
            "name": name,
            "description": actor.description,
            "parent_actor_ref": actor.parent_actor_ref,
            "parent_actor": actor.parent_actor,
            "source_refs": source_refs,
        }
        if not source_refs:
            dangling.append(name)

    actors: list[ActorItem] = []
    existing_refs = {str(actor.get("actor_ref")) for actor in (existing_actors or []) if actor.get("actor_ref")}
    refs_by_key: dict[str, str] = {}
    next_id = 1
    for proposal_ref in records:
        if proposal_ref and proposal_ref in existing_refs:
            refs_by_key[proposal_ref] = proposal_ref
            continue
        while f"ACT{next_id}" in existing_refs or f"ACT{next_id}" in refs_by_key.values():
            next_id += 1
        refs_by_key[proposal_ref] = f"ACT{next_id}"
        next_id += 1
    for proposal_ref, record in records.items():
        if not record["source_refs"]:
            continue
        parent_ref = str(record["parent_actor_ref"] or "").strip()
        if record["parent_actor"] and not parent_ref:
            dangling.append(f"{record['name']} parent actor requires parentActorRef")
            parent = None
        elif parent_ref and parent_ref not in records:
            dangling.append(f"{record['name']} parentActorRef {parent_ref}")
            parent = None
        elif parent_ref == proposal_ref:
            dangling.append(record["name"])
            parent = None
        else:
            parent = records[parent_ref]["name"] if parent_ref else None
        actors.append({
            "actor_ref": refs_by_key[proposal_ref],
            "name": record["name"],
            "description": record["description"],
            "parent_actor": parent,
            "parent_actor_ref": refs_by_key.get(parent_ref) if parent_ref else None,
            "source_refs": record["source_refs"],
        })
    return actors, dangling


def normalize_use_cases(
    raw_use_cases: list[UseCase],
    actors: list[ActorItem],
    functional_ids: set[str],
    constraint_ids: set[str],
) -> tuple[list[UseCase], list[str]]:
    """새 actor를 만들지 않고 use-case의 actor 참조를 canonical 이름으로 해소한다."""
    by_ref = {
        str(actor["actor_ref"]): actor
        for actor in actors
        if actor.get("actor_ref")
    }
    use_cases = []
    dangling: list[str] = []
    if len(by_ref) != len(actors):
        return [], ["accepted actor catalog contains a missing actor_ref"]
    for use_case in raw_use_cases:
        primary_actor = by_ref.get(use_case.primary_actor_ref) if use_case.primary_actor_ref else None
        if primary_actor is None:
            dangling.append(use_case.primary_actor_ref or f"missing primaryActorRef for {use_case.primary_actor}")
            continue
        support_refs: list[str] = []
        proposed_support = list(zip(use_case.supporting_actor_refs, use_case.supporting_actors))
        if len(use_case.supporting_actor_refs) != len(use_case.supporting_actors):
            dangling.extend(
                f"missing supportingActorRef for {name}"
                for name in use_case.supporting_actors[len(use_case.supporting_actor_refs):]
            )
        supporting: list[str] = []
        for ref, actor_name in proposed_support:
            actor_item = by_ref.get(ref) if ref else None
            if actor_item is None:
                dangling.append(actor_name)
            elif actor_item["actor_ref"] not in support_refs:
                support_refs.append(actor_item["actor_ref"])
                supporting.append(actor_item["name"])
        use_cases.append(use_case.model_copy(update={
            "primary_actor": primary_actor["name"],
            "primary_actor_ref": primary_actor["actor_ref"],
            "supporting_actor_refs": support_refs,
            "supporting_actors": supporting,
            "requirement_ids": [
                ref for ref in dict.fromkeys(use_case.requirement_ids) if ref in functional_ids
            ],
            "nfr_ids": [ref for ref in dict.fromkeys(use_case.nfr_ids) if ref in constraint_ids],
        }))
    return use_cases, dangling


def _actors_referenced_by_use_cases(
    actors: list[ActorItem], use_cases: list[UseCase | UseCaseItem]
) -> list[ActorItem]:
    """Keep participating actors and the generalization context they inherit from."""
    by_ref = {
        str(actor.get("actor_ref")): actor
        for actor in actors
        if actor.get("actor_ref")
    }
    retained: set[str] = set()
    for use_case in use_cases:
        if isinstance(use_case, dict):
            references = (
                str(use_case.get("primary_actor_ref") or ""),
                *[str(value) for value in use_case.get("supporting_actor_refs") or []],
            )
        else:
            references = (use_case.primary_actor_ref or "", *use_case.supporting_actor_refs)
        retained.update(
            reference
            for reference in references
            if reference
        )
    pending = list(retained)
    while pending:
        actor = by_ref.get(pending.pop())
        parent = str(actor.get("parent_actor_ref") or "") if actor else ""
        if parent and parent not in retained:
            retained.add(parent)
            pending.append(parent)
    return [
        actor for actor in actors
        if str(actor.get("actor_ref") or "") in retained
    ]


def _retry_dangling_actor_refs(
    *,
    schema,
    raw_items,
    canonicalize: Callable,
    repair_prompt: Callable[[list[str]], str],
    extract: Callable,
    proposal_call: StructuredProposalCall,
) -> list:
    """Allow exactly one identity-only correction before discarding invalid links."""
    canonical, dangling = canonicalize(raw_items)
    if not dangling:
        return canonical
    repaired = proposal_call(
        schema,
        [
            SystemMessage(content=prompts.ACTORS_SYSTEM if schema is ActorResult else prompts.USECASES_SYSTEM),
            HumanMessage(content=repair_prompt(sorted(set(dangling)))),
        ],
    )
    return canonicalize(extract(repaired))[0]


def _actor_repair_prompt(base_human: str, raw_actors) -> Callable[[list[str]], str]:
    proposed = "\n".join(
        f"- {actor.actor_ref}: {actor.name} [parentActorRef: {actor.parent_actor_ref or 'none'}; sourceRefs: {actor.source_refs}]"
        for actor in raw_actors
    ) or "- (none)"

    def build(dangling: list[str]) -> str:
        return (
            f"{base_human}\n\n[ACTOR IDENTITY REPAIR]\n"
            "Return the same actor proposal, correcting only blank, duplicate, or dangling "
            "actor identities and parentActorRef references. Keep distinct actor refs separate, "
            "even when display names match. Do not derive additional roles. "
            "Every sourceRefs entry must be an accepted requirement ID.\n\n"
            f"[CURRENT ACTORS]\n{proposed}\n\n[IDENTITIES TO CORRECT]\n{', '.join(dangling)}"
        )

    return build


def _use_case_repair_prompt(base_human: str, raw_use_cases, actors: list[ActorItem]) -> Callable[[list[str]], str]:
    proposed = "\n".join(
        f"- {use_case.name} [primary: {use_case.primary_actor}; "
        f"supporting: {', '.join(use_case.supporting_actors) or 'none'}]"
        for use_case in raw_use_cases
    ) or "- (none)"
    actor_names = ", ".join(actor["name"] for actor in actors) or "(none)"

    def build(dangling: list[str]) -> str:
        return (
            f"{base_human}\n\n[USE-CASE ACTOR IDENTITY REPAIR]\n"
            "Return the same use-case proposal, correcting only primaryActor and "
            "supportingActors references that do not name a listed actor. Do not derive, "
            "remove, or regroup use cases.\n\n"
            f"[CANONICAL ACTORS]\n{actor_names}\n\n[CURRENT USE CASES]\n{proposed}\n\n"
            f"[IDENTITIES TO CORRECT]\n{', '.join(dangling)}"
        )

    return build


def _trace_slice(
    requirement: RequirementItem,
    accepted_requirements: list[RequirementItem],
    use_case_catalog: list[UseCaseItem],
    actor_catalog: list[ActorItem],
    proposal_call: StructuredProposalCall,
) -> _RequirementTraceSlice:
    proposed = "\n".join(
        f"- {use_case['id']}: {use_case['name']} "
        f"[primary: {use_case['primary_actor_ref']} ({use_case['primary_actor']}); goal: {use_case['goal']}]"
        for use_case in use_case_catalog
    ) or "- (none)"
    actor_refs = "\n".join(
        f"- {actor['actor_ref']}: {actor['name']}" for actor in actor_catalog
    ) or "- (none)"
    context = "\n".join(
        _requirement_line(item)
        for item in accepted_requirements
        if item["id"] != requirement["id"]
    ) or "- (none)"
    functional = requirement.get("type") == "FR"
    result = proposal_call(
        _RequirementTraceSlice,
        [
            SystemMessage(content=(
                prompts.FUNCTIONAL_TRACE_SLICE_SYSTEM
                if functional
                else prompts.CONSTRAINT_TRACE_SLICE_SYSTEM
            )),
            HumanMessage(content=(
                f"[ACTOR CATALOG]\n{actor_refs}\n\n[FIXED PROPOSED USE CASES]\n{proposed}\n\n"
                f"[{'FUNCTIONAL REQUIREMENT' if functional else 'NON-FUNCTIONAL CONSTRAINT'} "
                f"UNDER AUDIT]\n"
                f"{_requirement_line(requirement)}\n\n"
                f"[OTHER ACCEPTED REQUIREMENTS — CONTEXT ONLY]\n{context}"
            )),
        ],
    )
    if not isinstance(result, _RequirementTraceSlice):
        raise TypeError(f"unexpected trace slice result {type(result).__name__}")
    if result.requirement_id != requirement["id"]:
        raise ValueError(
            f"trace slice returned {result.requirement_id!r} for {requirement['id']!r}"
        )
    return result


def _audit_requirement_traceability(
    functional_requirements: list[RequirementItem],
    constraints: list[RequirementItem],
    raw_use_cases: list[UseCase],
    use_case_refs: list[str],
    actor_catalog: list[ActorItem],
    functional_audit_ids: list[str],
    proposal_call: StructuredProposalCall,
) -> tuple[list[UseCase], dict[str, set[str]]]:
    """Review ambiguous requirements and keep realization and constraint edges separate."""
    if len(raw_use_cases) != len(use_case_refs):
        raise ValueError("every proposed use case must have one provisional UC id")
    use_case_catalog = [
        normalize_use_case(use_case, use_case_ref)
        for use_case, use_case_ref in zip(raw_use_cases, use_case_refs, strict=True)
    ]
    accepted_requirements = functional_requirements + constraints
    by_id = {requirement["id"]: requirement for requirement in accepted_requirements}
    constraint_ids = [constraint["id"] for constraint in constraints]
    task_ids = functional_audit_ids + constraint_ids
    workers = max(1, min(settings.spec_concurrency, len(task_ids)))
    decisions: dict[str, _RequirementTraceSlice] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                telemetry.bind_context(_trace_slice),
                by_id[requirement_id],
                accepted_requirements,
                use_case_catalog,
                actor_catalog,
                proposal_call,
            ): requirement_id
            for requirement_id in task_ids
        }
        for future in as_completed(futures):
            requirement_id = futures[future]
            try:
                decisions[requirement_id] = future.result()
            except Exception as exc:  # noqa: BLE001 - preserve only this requirement's mapping
                telemetry.record_degradation(
                    "use_cases.traceability_slice",
                    f"{type(exc).__name__}: {exc}",
                    subject=requirement_id,
                )

    known_refs = set(use_case_refs)
    accepted: dict[str, _RequirementTraceSlice] = {}
    for requirement_id, decision in decisions.items():
        if requirement_id in constraint_ids and decision.missing_use_case is not None:
            telemetry.record_degradation(
                "use_cases.traceability_slice",
                "a non-functional constraint proposed a missing use case",
                subject=requirement_id,
            )
            continue
        realized_refs = set(decision.realized_by_use_case_refs)
        constrained_refs = set(decision.constrains_use_case_refs or [])
        unknown = sorted((realized_refs | constrained_refs) - known_refs)
        if unknown:
            telemetry.record_degradation(
                "use_cases.traceability_slice",
                f"unknown use-case refs: {unknown}",
                subject=requirement_id,
            )
            continue
        if realized_refs and constrained_refs:
            telemetry.record_degradation(
                "use_cases.traceability_slice",
                "one requirement returned both realization and constraint edges",
                subject=requirement_id,
            )
            continue
        if requirement_id in constraint_ids and realized_refs:
            telemetry.record_degradation(
                "use_cases.traceability_slice",
                "a non-functional constraint claimed realization by a use case",
                subject=requirement_id,
            )
            continue
        accepted[requirement_id] = decision

    functional_ids = set(functional_audit_ids)
    nfr_ids_to_audit = set(constraint_ids)
    realization_targets = {
        requirement_id: set(decision.realized_by_use_case_refs)
        for requirement_id, decision in accepted.items()
    }
    constraint_targets = {
        requirement_id: set(decision.constrains_use_case_refs or [])
        for requirement_id, decision in accepted.items()
        if (
            decision.constrains_use_case_refs is not None
            and not decision.realized_by_use_case_refs
            and decision.missing_use_case is None
        )
    }
    updated: list[UseCase] = []
    for use_case, use_case_ref in zip(raw_use_cases, use_case_refs, strict=True):
        requirement_ids = [
            requirement_id
            for requirement_id in use_case.requirement_ids
            if requirement_id not in accepted or requirement_id not in functional_ids
        ]
        nfr_ids = [
            requirement_id
            for requirement_id in use_case.nfr_ids
            if requirement_id not in accepted or requirement_id not in nfr_ids_to_audit
        ]
        for requirement_id in functional_audit_ids:
            if use_case_ref in realization_targets.get(requirement_id, set()):
                requirement_ids.append(requirement_id)
        for requirement_id in constraint_ids:
            if use_case_ref in constraint_targets.get(requirement_id, set()):
                nfr_ids.append(requirement_id)
        updated.append(use_case.model_copy(update={
            "requirement_ids": list(dict.fromkeys(requirement_ids)),
            "nfr_ids": list(dict.fromkeys(nfr_ids)),
        }))

    # A functional decision with no realization is not an actor goal. Remove a skeleton-only
    # use case once it has no realized FR left; the requirement itself stays in ``classified``
    # and therefore remains traceable as a constraint or other model evidence.
    updated = [use_case for use_case in updated if use_case.requirement_ids]
    existing_names = {_actor_key(use_case.name) for use_case in updated}
    for requirement_id in functional_audit_ids:
        accepted_decision = accepted.get(requirement_id)
        candidate = accepted_decision.missing_use_case if accepted_decision else None
        if candidate is None or _actor_key(candidate.name) in existing_names:
            continue
        updated.append(UseCase(
            name=candidate.name,
            primary_actor_ref=candidate.primary_actor_ref,
            supporting_actor_refs=candidate.supporting_actor_refs,
            primary_actor=candidate.primary_actor,
            supporting_actors=candidate.supporting_actors,
            goal=candidate.goal,
            requirement_ids=[requirement_id],
            nfr_ids=[],
        ))
        use_case_refs.append(f"UC{len(use_case_refs) + 1}")
        existing_names.add(_actor_key(candidate.name))
    return updated, constraint_targets


@contract("identify_actors", requires=("classified",), produces=("actors",))
def identify_actors(
    state: AgentState,
    feedback: str = "",
    *,
    target_ref: str | None = None,
    proposal_call: StructuredProposalCall | None = None,
) -> ModelingStagePatch:
    """수락된 role/domain 사실과 actor 목표에서 외부 역할을 도출한다."""
    feedback = feedback_for(dict(state), "actors", feedback)
    classified = state.get("classified") or []
    if not classified:
        return {"actors": [], "phase": "actors"}

    normalized_target_ref = str(target_ref or "").strip() or None
    existing_actors = state.get("actors") or []
    if normalized_target_ref and normalized_target_ref not in {
        str(actor.get("actor_ref") or "").strip() for actor in existing_actors
    }:
        raise ValueError(f"Targeted actor feedback references unknown actorRef: {normalized_target_ref}")
    target_constraint = (
        "\n\n[TARGETED ACTOR IDENTITY]\n"
        f"The selected actorRef is {normalized_target_ref}. Return that exact actorRef for "
        "the selected actor, including when its display name changes. Do not substitute a "
        "different actorRef based on matching names or feedback text. If the requested change "
        "would delete this actor, state that deletion cannot be represented by this targeted "
        "identity-preserving edit rather than silently assigning another actorRef."
        if normalized_target_ref
        else ""
    )

    human = prompts.apply_user_feedback(
        "Accepted requirements:\n"
        f"{_listing(classified)}\n\n"
        "Existing actor catalog (preserve each actorRef for the same role, including a targeted rename):\n"
        + "\n".join(
            f"- {actor.get('actor_ref')}: {actor['name']} — {actor.get('description', '')}"
            for actor in existing_actors
        )
        + "\n\n"
        "Use a requirement as actor evidence only when it states an external role, a role "
        "specialization/domain fact, or an actor goal. Quality and deployment constraints alone "
        "do not create actors. Return sourceRefs containing only accepted requirement IDs."
        + target_constraint,
        feedback,
    )
    system = (
        f"{prompts.ACTORS_SYSTEM}\n\n"
        "Actor discovery may also use accepted structural role/domain statements, regardless "
        "of their FR/NFR label. Do not infer actors from ordinary quality or deployment constraints."
    )
    propose = proposal_call or invoke_structured
    result: ActorResult = propose(
        ActorResult, [SystemMessage(content=system), HumanMessage(content=human)]
    )
    accepted_ids = {requirement["id"] for requirement in classified}
    actors = _retry_dangling_actor_refs(
        schema=ActorResult,
        raw_items=result.actors,
        canonicalize=lambda items: normalize_actors(items, accepted_ids, existing_actors),
        repair_prompt=_actor_repair_prompt(human, result.actors),
        extract=lambda repaired: repaired.actors,
        proposal_call=propose,
    )
    return {"actors": actors, "phase": "actors"}


def normalize_use_case(use_case: UseCase, uid: str) -> UseCaseItem:
    """구조화 use-case proposal을 안정 ID가 있는 state 항목으로 정규화한다."""
    return {
        "id": uid,
        "name": use_case.name,
        "primary_actor_ref": use_case.primary_actor_ref or "",
        "supporting_actor_refs": use_case.supporting_actor_refs,
        "primary_actor": use_case.primary_actor,
        "supporting_actors": use_case.supporting_actors,
        "level": use_case.level,
        "goal": use_case.goal,
        "requirement_ids": use_case.requirement_ids,
        "nfr_ids": use_case.nfr_ids,
    }


def _local_edit_use_cases(
    existing: list[UseCaseItem],
    base_human: str,
    target_ids: list[str],
    feedback: str,
    actors: list[ActorItem],
    functional_ids: set[str],
    constraint_ids: set[str],
    constraint_applicability: dict[str, list[str]],
    proposal_call: StructuredProposalCall,
) -> ModelingStagePatch:
    target_set = {target.strip() for target in target_ids if target and target.strip()}
    current_listing = "\n".join(
        f"- {json.dumps(use_case, ensure_ascii=False, sort_keys=True)}"
        for use_case in existing
    )
    target_desc = ", ".join(
        f"{use_case['id']} ({use_case['name']})"
        for use_case in existing if use_case["id"] in target_set
    ) or ", ".join(sorted(target_set))
    human = prompts.usecase_local_edit(base_human, current_listing, target_desc, feedback)
    result: UseCaseResult = proposal_call(
        UseCaseResult,
        [SystemMessage(content=prompts.USECASES_SYSTEM), HumanMessage(content=human)],
    )
    use_cases = _retry_dangling_actor_refs(
        schema=UseCaseResult,
        raw_items=result.use_cases,
        canonicalize=lambda items: normalize_use_cases(
            items, actors, functional_ids, constraint_ids
        ),
        repair_prompt=_use_case_repair_prompt(human, result.use_cases, actors),
        extract=lambda repaired: repaired.use_cases,
        proposal_call=proposal_call,
    )
    # The model returns a full list for a local edit, but its copy of a sibling
    # is not an authority.  When the cardinality is unchanged, merge by the
    # explicit list-position contract and copy every non-target record from the
    # persisted baseline.  This keeps sibling identity and all fields intact,
    # even when the model edits a different domain's goal in its response.
    if len(use_cases) == len(existing):
        use_case_items = [
            existing[index]
            if existing[index]["id"] not in target_set
            else normalize_use_case(use_case, existing[index]["id"])
            for index, use_case in enumerate(use_cases)
        ]
    elif target_positions := [
        index for index, item in enumerate(existing) if item["id"] in target_set
    ]:
        first_target = target_positions[0]
        last_target = target_positions[-1]
        if target_positions != list(range(first_target, last_target + 1)):
            raise ValueError(
                "A cardinality-changing local edit requires one contiguous target block."
            )
        replacement_count = len(use_cases) - len(existing) + len(target_positions)
        if replacement_count < 0:
            raise ValueError("The local edit removed elements outside its target block.")
        proposed_targets = use_cases[
            first_target : first_target + replacement_count
        ]
        if len(proposed_targets) != replacement_count:
            raise ValueError("The local edit did not preserve non-target list positions.")

        reusable_ids = [existing[index]["id"] for index in target_positions]
        used_ids = {
            item["id"] for item in existing if item["id"] not in target_set
        }
        next_id = 1
        target_items: list[UseCaseItem] = []
        for index, use_case in enumerate(proposed_targets):
            if index < len(reusable_ids):
                use_case_id = reusable_ids[index]
            else:
                while f"UC{next_id}" in used_ids:
                    next_id += 1
                use_case_id = f"UC{next_id}"
                next_id += 1
            used_ids.add(use_case_id)
            target_items.append(normalize_use_case(use_case, use_case_id))
        use_case_items = (
            existing[:first_target] + target_items + existing[last_target + 1:]
        )
    else:
        raise ValueError("The local edit does not identify an existing target use case.")
    preserved_ids = {item["id"] for item in use_case_items}
    return {
        "actors": _actors_referenced_by_use_cases(actors, use_case_items),
        "use_cases": use_case_items,
        "constraint_applicability": {
            requirement_id: [
                use_case_id for use_case_id in use_case_ids if use_case_id in preserved_ids
            ]
            for requirement_id, use_case_ids in constraint_applicability.items()
            if not use_case_ids or any(use_case_id in preserved_ids for use_case_id in use_case_ids)
        },
        "phase": "use_cases",
    }


@contract("identify_use_cases", requires=("classified", "actors"), produces=("use_cases",))
def identify_use_cases(
    state: AgentState,
    feedback: str = "",
    target_ids: list[str] | None = None,
    *,
    proposal_call: StructuredProposalCall | None = None,
) -> ModelingStagePatch:
    """FR 근거의 user-goal use-case를 도출하고 NFR 제약을 별도로 연결한다."""
    feedback = feedback_for(dict(state), "use_cases", feedback)
    classified = state.get("classified") or []
    fr, nfr = _split_fr_nfr(classified)
    actors = state.get("actors") or []
    propose = proposal_call or invoke_structured
    if not fr:
        return {
            "actors": [],
            "use_cases": [],
            "constraint_applicability": {},
            "phase": "use_cases",
        }

    actor_listing = "\n".join(
        f"- {actor.get('actor_ref') or f'ACT{index}'} | {actor['name']}: {actor['description']}"
        for index, actor in enumerate(actors, 1)
    ) or "- (none identified)"
    human = (
        f"Actors:\n{actor_listing}\n\n"
        f"Functional requirements (candidates for use cases):\n{_listing(fr)}\n\n"
        f"Non-functional requirements (attach as constraints only):\n{_listing(nfr) or '- (none)'}"
    )
    existing = state.get("use_cases") or []
    if target_ids and existing:
        return _local_edit_use_cases(
            existing, human, target_ids, feedback, actors,
            {requirement["id"] for requirement in fr},
            {requirement["id"] for requirement in nfr},
            dict(state.get("constraint_applicability") or {}),
            propose,
        )

    result: UseCaseResult = propose(
        UseCaseResult,
        [
            SystemMessage(content=prompts.USECASE_GOAL_SKELETON_SYSTEM),
            HumanMessage(content=prompts.apply_user_feedback(human, feedback)),
        ],
    )
    # Resolve actor identities before issuing the UC ids consumed by the RTM audit.
    # The repair is allowed to remove invalid proposals, so issuing ids before it would
    # make a later positional pairing able to move a trace edge to another use case.
    raw_use_cases = _retry_dangling_actor_refs(
        schema=UseCaseResult,
        raw_items=result.use_cases,
        canonicalize=lambda items: normalize_use_cases(
            items,
            actors,
            {requirement["id"] for requirement in fr},
            {requirement["id"] for requirement in nfr},
        ),
        repair_prompt=_use_case_repair_prompt(human, result.use_cases, actors),
        extract=lambda repaired: repaired.use_cases,
        proposal_call=propose,
    )
    # Assign canonical UC ids before RTM review; trace responses may select only these ids.
    provisional_use_case_refs = [f"UC{index}" for index in range(1, len(raw_use_cases) + 1)]
    claim_counts = Counter(
        requirement_id
        for use_case in raw_use_cases
        for requirement_id in set(use_case.requirement_ids)
    )
    audit_ids = sorted(
        requirement["id"]
        for requirement in fr
        if claim_counts[requirement["id"]] != 1
    )
    if audit_ids or nfr:
        raw_use_cases, constraint_targets = _audit_requirement_traceability(
            fr, nfr, raw_use_cases, provisional_use_case_refs, actors, audit_ids, propose
        )
    else:
        constraint_targets = {}
    use_case_items = [
        normalize_use_case(use_case, provisional_use_case_refs[index])
        for index, use_case in enumerate(raw_use_cases)
    ]
    constraint_applicability = {
        requirement_id: [
            item["id"] for item in use_case_items if item["id"] in targets
        ]
        for requirement_id, targets in constraint_targets.items()
        if not targets or any(target in {item["id"] for item in use_case_items} for target in targets)
    }
    return {
        "actors": _actors_referenced_by_use_cases(actors, raw_use_cases),
        "use_cases": use_case_items,
        "constraint_applicability": constraint_applicability,
        "phase": "use_cases",
    }


def _model_review(
    state: AgentState,
    review_call: SemanticReviewCall,
) -> _ModelReviewResult:
    """Review one fixed actor/use-case proposal without triggering a repair."""
    payload = {
        "requirements": [
            {key: requirement.get(key) for key in ("id", "text", "type", "qualifies")}
            for requirement in (state.get("classified") or [])
        ],
        "deployment_needs": state.get("deployment_needs") or {},
        "actors": [
            {key: actor.get(key) for key in ("name", "description", "parent_actor", "source_refs")}
            for actor in (state.get("actors") or [])
        ],
        "use_cases": [
            {key: use_case.get(key) for key in (
                "name", "primary_actor", "supporting_actors", "level", "goal",
                "requirement_ids", "nfr_ids",
            )}
            for use_case in (state.get("use_cases") or [])
        ],
        "constraint_applicability": state.get("constraint_applicability") or {},
    }
    result = review_call(
        rules.MODEL_USE_CASES,
        payload,
        prefix="model",
        source="use_cases.semantic_validator",
        confirm_violations=True,
    )
    return {
        "issues": result.findings,
        "semantic_status": result.status,
        "unexamined_rules": list(result.unexamined),
    }


def _actor_reference_defects(state: AgentState) -> set[tuple[str, str, str]]:
    """Return use-case actor links that do not resolve to a preserved actor."""
    known = {
        str(actor.get("actor_ref") or "")
        for actor in (state.get("actors") or [])
        if actor.get("actor_ref")
    }
    defects: set[tuple[str, str, str]] = set()
    for use_case in state.get("use_cases") or []:
        # Use-case names are presentation text and may legitimately repeat.
        # Review repair must key a newly introduced actor defect to the
        # accepted UC identity that existed before the review.
        use_case_key = str(use_case.get("id") or "")
        primary = str(use_case.get("primary_actor_ref") or "")
        if not primary or primary not in known:
            defects.add((use_case_key, "primary_actor", primary))
        for supporting_actor in use_case.get("supporting_actor_refs") or []:
            supporting = str(supporting_actor)
            if not supporting or supporting not in known:
                defects.add((use_case_key, "supporting_actor", supporting))
    return defects


def _review_feedback(issues: list[str]) -> str:
    return (
        "Regenerate only the use-case proposal to address the independent model review below. "
        "Keep the accepted requirements and canonical actor list fixed. Do not add actors or "
        "change requirement text or classification:\n"
        + "\n".join(f"- {issue}" for issue in issues)
    )


def _review_issue_keys(issues: list[str]) -> set[str]:
    """Compare semantic defects by stable rule identity, not fluctuating prose."""
    return {
        rules.rule_of(issue) or issue
        for issue in issues
    }


@contract("review_model", requires=("actors", "use_cases"), produces=("model_review",))
def review_model(
    state: AgentState,
    *,
    proposal_call: StructuredProposalCall | None = None,
    review_call: SemanticReviewCall | None = None,
) -> ModelingStagePatch:
    """고정 model proposal을 검증하고 use-case만 최대 한 번 guarded repair한다."""
    _ = settings.enable_semantic_validator
    reviewer = review_call or validator.review
    initial_review = _model_review(state, reviewer)
    issues = initial_review["issues"]
    # ``classified`` is not part of this node's historical contract because review itself needs
    # only the artifact. A repair, however, must be source-grounded and run at the ordinary
    # identify-use-cases -> review boundary. Legacy/direct review calls (including a completed
    # handoff audit with downstream artifacts) therefore cannot mutate use cases behind those
    # artifacts and keep the original result.
    if not issues or not state.get("classified") or state.get("phase") != "use_cases":
        return {"model_review": initial_review, "phase": "model_review"}

    try:
        candidate_update = identify_use_cases(
            state,
            feedback=_review_feedback(issues),
            proposal_call=proposal_call,
        )
        candidate_state = cast(AgentState, dict(state))
        candidate_state.update(cast(AgentState, candidate_update))
        candidate_review = _model_review(candidate_state, reviewer)
        initial_coverage = check_coverage(state)["coverage"]
        candidate_coverage = check_coverage(candidate_state)["coverage"]
    except Exception as exc:  # noqa: BLE001 - preserve the reviewed original for the handoff gate
        telemetry.record_degradation(
            "use_cases.model_repair",
            f"{type(exc).__name__}: {exc}",
        )
        return {"model_review": initial_review, "phase": "model_review"}

    new_actor_defects = _actor_reference_defects(candidate_state) - _actor_reference_defects(state)
    initial_issue_keys = _review_issue_keys(issues)
    candidate_issue_keys = _review_issue_keys(candidate_review["issues"])
    new_unexamined = set(candidate_review["unexamined_rules"]) - set(
        initial_review["unexamined_rules"]
    )
    coverage_regressed = any(
        set(candidate_coverage[field]) - set(initial_coverage[field])
        for field in ("orphan_fr_ids", "unattached_nfr_ids")
    )
    improved = (
        candidate_review["semantic_status"] == validator.OK
        and candidate_issue_keys < initial_issue_keys
        and not new_unexamined
        and not candidate_coverage["unknown_requirement_refs"]
        and not coverage_regressed
        and not new_actor_defects
    )
    if not improved:
        telemetry.record_degradation(
            "use_cases.model_repair",
            "candidate rejected: model findings did not strictly improve or references regressed",
        )
        return {"model_review": initial_review, "phase": "model_review"}

    return {
        "actors": candidate_update.get("actors", state.get("actors") or []),
        "use_cases": candidate_update["use_cases"],
        "constraint_applicability": candidate_update.get("constraint_applicability") or {},
        "model_review": candidate_review,
        "phase": "model_review",
    }


@contract("check_coverage", requires=("classified", "use_cases"), produces=("coverage",))
def check_coverage(state: AgentState) -> ModelingStagePatch:
    """user-goal coverage와 전체 모델 accounting을 섞지 않고 계산한다."""
    trace = traceability.index(cast(dict[object, object], state))
    requirement_trace = traceability.build_requirement_trace(
        cast(dict[object, object], state)
    )
    coverage = {
        "fr_total": len(trace.fr_ids),
        "covered_fr_ids": list(trace.covered_fr_ids),
        "unrealized_fr_ids": list(trace.orphan_fr_ids),
        "orphan_fr_ids": list(trace.missing_goal_ids),
        "unattached_nfr_ids": list(trace.unattached_nfr_ids),
        "unknown_requirement_refs": list(trace.unknown_refs),
        "unknown_use_case_refs": list(trace.unknown_use_case_refs),
        "unaccounted_requirement_ids": list(trace.unaccounted_ids),
        "goal_requirement_ids": list(trace.goal_ids),
        "covered_goal_requirement_ids": list(trace.covered_goal_ids),
        "missing_goal_requirement_ids": list(trace.missing_goal_ids),
        "goal_coverage_ratio": trace.goal_coverage_ratio,
        "accounted_coverage_ratio": trace.accounted_ratio,
        "fr_realization_ratio": trace.coverage_ratio,
        "coverage_ratio": trace.goal_coverage_ratio,
    }
    return {
        "coverage": coverage,
        "traceability": requirement_trace,
        "phase": "coverage",
    }
