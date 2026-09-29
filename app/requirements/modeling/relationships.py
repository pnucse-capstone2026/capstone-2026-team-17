"""Canonical use-case 관계를 선택·검증하고 history-aware repair하는 modeling stage다.

수락된 Step 3 RTM coverage로 ``include``와 ``extend`` 후보를 제한하며, 모델은 그 유한
후보 안에서만 의미 결정을 수행한다. Actor 관계는 upstream canonical fact로 취급한다.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from typing import Any, cast

from langchain_core.messages import HumanMessage, SystemMessage

from app.requirements import prompts
from app.requirements.common.state_contract import contract
from app.requirements.contracts.state import AgentState
from app.requirements.knowledge import rules
from app.requirements.modeling import validation as validator
from app.requirements.modeling.contracts import (
    ModelingStagePatch,
    SemanticReviewCall,
    StructuredProposalCall,
)
from app.requirements.modeling.feedback import feedback_for
from app.requirements.runtime.structured_llm import invoke_structured
from app.requirements.schemas import ExistingIncludeModel, ExtendSelection, RelationshipModel
from app.validation import (
    RepairAttempt,
    RepairLedger,
    repair_makes_progress,
    stable_digest,
    transient_llm_error,
)

_EXTEND_CUE = re.compile(r"\b(?:may|optionally|when|while|after|once|unless)\b", re.IGNORECASE)
_LEXICAL_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9'-]*")

RelationshipRecord = dict[str, object]


def _clean_text(value: object) -> str:
    visible = "".join(
        character
        for character in str(value or "")
        if unicodedata.category(character) != "Cf"
    )
    return " ".join(visible.split())


def _humanize_name(value: object) -> str:
    words = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])",
        " ",
        str(value or ""),
    )
    return _clean_text(words)


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def _prompt_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _requirements_with_source_evidence(state: AgentState) -> dict[str, dict]:
    raw = list(state.get("raw_requirements") or [])
    result: dict[str, dict] = {}
    for item in state.get("classified") or []:
        requirement_id = str(item.get("id") or "")
        if not requirement_id:
            continue
        source_evidence: list[str] = []
        for source_ref in item.get("source_refs") or []:
            match = re.fullmatch(r"RAW(\d+)", str(source_ref))
            if match and 0 < int(match.group(1)) <= len(raw):
                source_evidence.append(str(raw[int(match.group(1)) - 1]))
        result[requirement_id] = {
            "text": _clean_text(item.get("text")),
            "source_evidence": list(dict.fromkeys(source_evidence)),
        }
    return result


def _extend_candidate_ids(
    state: AgentState,
    use_cases: list[dict],
    specs_by_id: dict[str, dict],
) -> list[str]:
    """Bound semantic extend work to goals with explicit conditional/optional evidence."""
    evidence = _requirements_with_source_evidence(state)
    candidates: list[str] = []
    for use_case in use_cases:
        use_case_id = str(use_case["id"])
        if use_case_id not in specs_by_id:
            continue
        parts = [str(use_case.get("goal") or "")]
        for requirement_id in use_case.get("requirement_ids") or []:
            item = evidence.get(str(requirement_id), {})
            parts.append(str(item.get("text") or ""))
            parts.extend(str(value) for value in item.get("source_evidence") or [])
        if _EXTEND_CUE.search(" ".join(parts)):
            candidates.append(use_case_id)
    return candidates


def _accepted_specs_by_id(
    state: AgentState, accepted_ids: set[str]
) -> tuple[dict[str, dict], list[dict]]:
    """Index accepted specifications by stable use-case ID."""
    indexed: dict[str, dict] = {}
    rejections: list[dict] = []
    for raw_spec in state.get("use_case_specs") or []:
        spec = dict(raw_spec)
        use_case_id = str(spec.get("use_case_id") or "")
        if use_case_id not in accepted_ids:
            continue
        if (
            spec.get("generated") is False
            or spec.get("issues")
            or spec.get("semantic_status") != validator.OK
        ):
            rejections.append(
                {
                    "kind": "specification",
                    "use_case_id": use_case_id,
                    "step_ref": "specification",
                    "reason": "unaccepted use-case specification",
                }
            )
            continue
        indexed.setdefault(use_case_id, spec)
    return indexed, rejections


def _include_candidates(
    state: AgentState,
    use_cases: list[dict],
    specs_by_id: dict[str, dict],
) -> list[dict]:
    """Bound include discovery by RTM-backed shared behavior; semantics choose reuse."""
    accepted_fr_ids = {
        str(item["id"])
        for item in state.get("classified") or []
        if item.get("id") and item.get("type") == "FR"
    }
    refs_by_requirement: dict[str, list[dict]] = defaultdict(list)
    for use_case in use_cases:
        if use_case.get("level") == "subfunction":
            continue
        use_case_id = str(use_case["id"])
        spec = specs_by_id.get(use_case_id)
        if spec is None:
            continue
        mapped_requirements = {
            str(value) for value in use_case.get("requirement_ids") or []
        }
        for step in spec.get("main_scenario") or []:
            number = step.get("step_number")
            if not isinstance(number, int):
                continue
            for requirement_id in step.get("covered_req_ids") or []:
                requirement_id = str(requirement_id)
                if (
                    requirement_id not in accepted_fr_ids
                    or requirement_id not in mapped_requirements
                ):
                    continue
                refs_by_requirement[requirement_id].append(
                    {
                        "use_case_id": use_case_id,
                        "step_ref": f"main:{number}",
                        "sentence": _clean_text(step.get("sentence")),
                    }
                )

    candidates: list[dict] = []
    for requirement_id, refs in refs_by_requirement.items():
        participant_ids = list(dict.fromkeys(str(ref["use_case_id"]) for ref in refs))
        if len(participant_ids) < 2:
            continue
        identity = {
            "kind": "include",
            "requirement_id": requirement_id,
            "step_refs": [
                {"use_case_id": ref["use_case_id"], "step_ref": ref["step_ref"]}
                for ref in refs
            ],
        }
        candidate_id = _stable_id("rel-include", identity)
        candidates.append(
            {
                "candidate_id": candidate_id,
                "requirement_id": requirement_id,
                "participating_use_case_ids": participant_ids,
                "step_refs": refs,
                "derived_use_case_id": (
                    "UC_INC_" + candidate_id.rsplit("-", 1)[1].upper()
                ),
            }
        )
    return candidates


def _existing_include_options(
    state: AgentState,
    use_cases: list[dict],
    specs_by_id: dict[str, dict],
) -> list[dict]:
    """Shortlist reusable existing UCs from action/object evidence in accepted specs."""
    accepted_fr_ids = {
        str(item["id"])
        for item in state.get("classified") or []
        if item.get("id") and item.get("type") == "FR"
    }

    def lexical_terms(value: object) -> list[str]:
        return [match.group(0).lower() for match in _LEXICAL_TOKEN.finditer(str(value or ""))]

    def verb_stem(token: str) -> str:
        if token.endswith("ies") and len(token) > 3:
            return f"{token[:-3]}y"
        if token.endswith(("ches", "shes", "sses", "xes", "zes", "oes")):
            return token[:-2]
        if token.endswith("s") and not token.endswith("ss"):
            return token[:-1]
        return token

    accepted: list[tuple[dict, list[dict]]] = []
    for use_case in use_cases:
        if use_case.get("level") == "subfunction":
            continue
        use_case_id = str(use_case["id"])
        spec = specs_by_id.get(use_case_id)
        if spec is None:
            continue
        numbered_steps = [
            step
            for step in spec.get("main_scenario") or []
            if isinstance(step.get("step_number"), int)
        ]
        if not numbered_steps:
            continue
        mapped_requirements = {
            str(value) for value in use_case.get("requirement_ids") or []
        }
        refs: list[dict] = []
        for step in numbered_steps:
            requirement_ids = list(
                dict.fromkeys(
                    str(requirement_id)
                    for requirement_id in step.get("covered_req_ids") or []
                    if str(requirement_id) in accepted_fr_ids
                    and str(requirement_id) in mapped_requirements
                )
            )
            if requirement_ids:
                refs.append(
                    {
                        "use_case_id": use_case_id,
                        "step_ref": f"main:{step['step_number']}",
                        "sentence": _clean_text(step.get("sentence")),
                        "requirement_ids": requirement_ids,
                    }
                )
        accepted.append((use_case, refs))

    options: list[dict] = []
    for target, _target_refs in accepted:
        name_terms = lexical_terms(target.get("name"))
        if not name_terms:
            continue
        action = verb_stem(name_terms[0])
        object_terms = set(name_terms[1:])
        object_terms.update(
            term
            for term in lexical_terms(target.get("goal"))
            if verb_stem(term) != action
        )
        if not object_terms:
            continue
        target_id = str(target["id"])
        base_step_options = []
        for base, refs in accepted:
            if str(base["id"]) == target_id:
                continue
            for ref in refs:
                step_terms = lexical_terms(ref["sentence"])
                if (
                    action in {verb_stem(term) for term in step_terms}
                    and object_terms.intersection(step_terms)
                ):
                    base_step_options.append(ref)
        if len({str(ref["use_case_id"]) for ref in base_step_options}) >= 2:
            options.append(
                {
                    "included_use_case_id": target_id,
                    "name": str(target.get("name") or ""),
                    "base_step_options": base_step_options,
                }
            )
    return options


def _ancestor_names(actor: str, parent_by_child: dict[str, str]) -> set[str]:
    ancestors: set[str] = set()
    current = parent_by_child.get(actor)
    while current and current not in ancestors:
        ancestors.add(current)
        current = parent_by_child.get(current)
    return ancestors


def _actor_projection(
    use_cases: list[dict], actors: list[dict]
) -> tuple[list[dict], list[dict], list[str], list[str]]:
    """Project canonical actor participation and specialization facts."""
    actor_refs = {str(actor.get("actor_ref") or "") for actor in actors}
    parent_by_child = {
        str(actor["actor_ref"]): str(actor["parent_actor_ref"])
        for actor in actors
        if actor.get("actor_ref") and actor.get("parent_actor_ref") in actor_refs
    }
    generalizations = [
        {"parent_actor_ref": parent, "child_actor_ref": child, "kind": "actor"}
        for child, parent in parent_by_child.items()
    ]

    declared: list[tuple[str, str]] = []
    dropped: list[str] = []
    for use_case in use_cases:
        use_case_id = str(use_case["id"])
        participants = [
            use_case.get("primary_actor_ref"),
            *(use_case.get("supporting_actor_refs") or []),
        ]
        for actor_value in participants:
            actor_ref = str(actor_value or "")
            if not actor_ref:
                continue
            if actor_ref not in actor_refs:
                dropped.append(f"unknown actor {actor_ref} for use case {use_case_id}")
                continue
            pair = (actor_ref, use_case_id)
            if pair not in declared:
                declared.append(pair)

    declared_set = set(declared)
    associations = [
        {"actor_ref": actor_ref, "use_case_id": use_case_id}
        for actor_ref, use_case_id in declared
        if not any(
            (ancestor, use_case_id) in declared_set
            for ancestor in _ancestor_names(actor_ref, parent_by_child)
        )
    ]
    associated = {actor_ref for actor_ref, _ in declared}
    for actor_ref, _ in declared:
        associated.update(
            child
            for child in parent_by_child
            if actor_ref in _ancestor_names(child, parent_by_child)
        )
    orphan_actors = [
        str(actor["name"])
        for actor in actors
        if str(actor["actor_ref"]) not in associated
    ]
    return associations, generalizations, dropped, orphan_actors


def _relationship_prompt(
    state: AgentState,
    use_cases: list[dict],
    specs_by_id: dict[str, dict],
    include_candidates: list[dict],
    existing_include_options: list[dict],
    feedback: str,
) -> str:
    """Serialize compact evidence; relationship semantics live in the system prompt."""
    requirement_text = _requirements_with_source_evidence(state)
    bounded_artifact = []
    for use_case in use_cases:
        use_case_id = str(use_case["id"])
        spec = specs_by_id.get(use_case_id)
        if spec is None:
            continue
        bounded_artifact.append(
            {
                "use_case_id": use_case_id,
                "name": str(use_case.get("name") or ""),
                "goal": str(use_case.get("goal") or ""),
                "primary_actor": str(use_case.get("primary_actor") or ""),
                "requirement_ids": [
                    str(requirement_id)
                    for requirement_id in use_case.get("requirement_ids") or []
                ],
                "preconditions": spec.get("preconditions") or [],
                "trigger": spec.get("trigger") or "",
                "success_guarantee": spec.get("success_guarantee") or [],
                "main_scenario_steps": [
                    {
                        "step_ref": f"main:{step.get('step_number')}",
                        "sentence": _clean_text(step.get("sentence")),
                    }
                    for step in spec.get("main_scenario") or []
                    if isinstance(step.get("step_number"), int)
                ],
            }
        )
    prompt_candidates = [
        {
            "candidate_id": candidate["candidate_id"],
            "requirement_id": candidate["requirement_id"],
            "step_refs": candidate["step_refs"],
        }
        for candidate in include_candidates
    ]
    prompt = (
        f"Requirements by ID:\n{_prompt_json(requirement_text)}\n\n"
        f"Include candidates:\n{_prompt_json(prompt_candidates)}\n\n"
        f"Existing include options:\n{_prompt_json(existing_include_options)}\n\n"
        f"Use cases:\n{_prompt_json(bounded_artifact)}"
    )
    return prompts.apply_user_feedback(prompt, feedback)


def _relationship_review_payload(
    requirements: list[dict],
    actors: list[dict],
    use_cases: list[dict],
    specs_by_id: dict[str, dict],
    relationships: dict[str, Any],
) -> dict[str, Any]:
    """Expose only the accepted evidence and the materialized relationship result."""
    return {
        "requirements": [
            {
                "id": str(requirement.get("id") or ""),
                "text": str(requirement.get("text") or ""),
                "type": str(requirement.get("type") or ""),
            }
            for requirement in requirements
            if requirement.get("id")
        ],
        "actors": [
            {
                "name": str(actor.get("name") or ""),
                "description": str(actor.get("description") or ""),
                "parent_actor": actor.get("parent_actor"),
            }
            for actor in actors
            if actor.get("name")
        ],
        "use_cases": [
            {
                "id": str(use_case["id"]),
                "name": str(use_case.get("name") or ""),
                "goal": str(use_case.get("goal") or ""),
                "primary_actor": str(use_case.get("primary_actor") or ""),
                "supporting_actors": list(use_case.get("supporting_actors") or []),
                "requirement_ids": list(use_case.get("requirement_ids") or []),
                "preconditions": list(
                    (specs_by_id.get(str(use_case["id"])) or {}).get("preconditions") or []
                ),
                "trigger": str(
                    (specs_by_id.get(str(use_case["id"])) or {}).get("trigger") or ""
                ),
                "main_scenario": list(
                    (specs_by_id.get(str(use_case["id"])) or {}).get("main_scenario") or []
                ),
                "extensions": list(
                    (specs_by_id.get(str(use_case["id"])) or {}).get("extensions") or []
                ),
                "success_guarantee": list(
                    (specs_by_id.get(str(use_case["id"])) or {}).get("success_guarantee")
                    or []
                ),
            }
            for use_case in use_cases
            if str(use_case["id"]) in specs_by_id
        ],
        "relationships": {
            key: relationships.get(key, [])
            for key in (
                "associations",
                "includes",
                "extends",
                "generalizations",
                "derived_use_cases",
            )
        },
    }


def _normalize_step_ref(value: object) -> str | None:
    match = re.fullmatch(r"(?:main:|step\s*)?(\d+)", _clean_text(value), re.IGNORECASE)
    if not match:
        return None
    return f"main:{int(match.group(1))}"


def _materialize_includes(
    model: RelationshipModel,
    candidates: list[dict],
    use_cases: list[dict],
) -> tuple[list[dict], list[dict], list[str]]:
    """Materialize only one unambiguous model decision per bounded candidate."""
    candidates_by_id = {str(item["candidate_id"]): item for item in candidates}
    names_by_id = {str(item["id"]): str(item.get("name") or "") for item in use_cases}
    decisions_by_id: dict[str, list] = defaultdict(list)
    dropped: list[str] = []
    for selection in model.includes:
        if selection.candidate_id not in candidates_by_id:
            dropped.append(f"unknown relationship candidate {selection.candidate_id}")
            continue
        decisions_by_id[selection.candidate_id].append(selection)

    includes: list[dict] = []
    derived: list[dict] = []
    for candidate in candidates:
        candidate_id = str(candidate["candidate_id"])
        selections = decisions_by_id.get(candidate_id, [])
        if len(selections) != 1:
            if len(selections) > 1:
                dropped.append(f"ambiguous relationship candidate decision {candidate_id}")
            continue
        selection = selections[0]
        name = _humanize_name(selection.included_use_case_name)
        if selection.decision != "approve":
            continue
        if not name:
            dropped.append(f"approved include candidate {candidate_id} has no use-case name")
            continue
        derived_id = str(candidate["derived_use_case_id"])
        derived.append(
            {
                "use_case_id": derived_id,
                "name": name,
                "origin": "factored_include",
                "step_refs": candidate["step_refs"],
                "requirement_ids": [candidate["requirement_id"]],
            }
        )
        for base_id in candidate["participating_use_case_ids"]:
            includes.append(
                {
                    "base_use_case_id": base_id,
                    "included_use_case_id": derived_id,
                    "base_use_case": names_by_id[base_id],
                    "included_use_case": name,
                    "step_refs": candidate["step_refs"],
                    "requirement_ids": [candidate["requirement_id"]],
                    "requirement_refs": [
                        {
                            "use_case_id": ref["use_case_id"],
                            "step_ref": ref["step_ref"],
                            "requirement_id": candidate["requirement_id"],
                        }
                        for ref in candidate["step_refs"]
                    ],
                }
            )
    return includes, derived, dropped


def _materialize_existing_includes(
    model: ExistingIncludeModel,
    options: list[dict],
    use_cases: list[dict],
) -> tuple[list[dict], list[str]]:
    """Materialize only model-selected existing targets and supplied base steps."""
    names_by_id = {str(item["id"]): str(item.get("name") or "") for item in use_cases}
    options_by_target = {
        str(item["included_use_case_id"]): item for item in options
    }
    selections_by_target: dict[str, list] = defaultdict(list)
    for selection in model.existing_includes:
        selections_by_target[selection.included_use_case_id].append(selection)

    includes: list[dict] = []
    dropped: list[str] = []
    for target_id, selections in selections_by_target.items():
        option = options_by_target.get(target_id)
        if option is None or target_id not in names_by_id:
            dropped.append(f"existing include {target_id}: target is not supplied")
            continue
        if len(selections) != 1:
            dropped.append(f"existing include {target_id}: ambiguous selection")
            continue
        selected_steps: list[dict] = []
        allowed_steps = {
            (str(item["use_case_id"]), str(item["step_ref"])): item
            for item in option["base_step_options"]
        }
        seen: set[tuple[str, str]] = set()
        for ref in selections[0].base_step_refs:
            key = (ref.use_case_id, ref.step_ref)
            if key not in allowed_steps:
                dropped.append(f"existing include {target_id}: base step {key} is not supplied")
                continue
            if ref.use_case_id == target_id:
                dropped.append(f"existing include {target_id}: target cannot be its own base")
                continue
            if key not in seen:
                selected_steps.append(allowed_steps[key])
                seen.add(key)
        base_ids = list(dict.fromkeys(str(item["use_case_id"]) for item in selected_steps))
        if len(base_ids) < 2:
            dropped.append(f"existing include {target_id}: needs steps from two bases")
            continue
        requirement_ids = list(
            dict.fromkeys(
                requirement_id
                for item in selected_steps
                for requirement_id in item["requirement_ids"]
            )
        )
        requirement_refs = [
            {
                "use_case_id": item["use_case_id"],
                "step_ref": item["step_ref"],
                "requirement_id": requirement_id,
            }
            for item in selected_steps
            for requirement_id in item["requirement_ids"]
        ]
        for base_id in base_ids:
            includes.append(
                {
                    "base_use_case_id": base_id,
                    "included_use_case_id": target_id,
                    "base_use_case": names_by_id[base_id],
                    "included_use_case": names_by_id[target_id],
                    "step_refs": selected_steps,
                    "requirement_ids": requirement_ids,
                    "requirement_refs": requirement_refs,
                }
            )
    return includes, dropped


def _materialize_extends(
    model: RelationshipModel,
    use_cases: list[dict],
    specs_by_id: dict[str, dict],
) -> tuple[list[dict], list[str]]:
    names_by_id = {str(item["id"]): str(item.get("name") or "") for item in use_cases}
    requirements_by_id = {
        str(item["id"]): [str(value) for value in item.get("requirement_ids") or []]
        for item in use_cases
    }
    valid_steps = {
        use_case_id: {
            f"main:{step.get('step_number')}": _clean_text(step.get("sentence"))
            for step in spec.get("main_scenario") or []
            if isinstance(step.get("step_number"), int)
        }
        for use_case_id, spec in specs_by_id.items()
    }
    relations: list[dict] = []
    dropped: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for selection in model.extends:
        base_id = selection.base_use_case_id
        extending_id = selection.extending_use_case_id
        step_ref = _normalize_step_ref(selection.base_step_ref)
        label = f"extend {base_id} / {extending_id}"
        if base_id not in names_by_id or extending_id not in names_by_id:
            dropped.append(f"{label}: relationship stage cannot create use cases")
            continue
        if base_id == extending_id:
            dropped.append(f"{label}: endpoints must be distinct")
            continue
        if step_ref is None or step_ref not in valid_steps.get(base_id, {}):
            dropped.append(
                f"{label}: base step ref {selection.base_step_ref!r} is not one of "
                f"{sorted(valid_steps.get(base_id, {}))}"
            )
            continue
        identity = (base_id, extending_id, step_ref)
        if identity in seen:
            continue
        seen.add(identity)
        requirement_ids = requirements_by_id.get(extending_id, [])
        relations.append(
            {
                "base_use_case_id": base_id,
                "extending_use_case_id": extending_id,
                "base_use_case": names_by_id[base_id],
                "extending_use_case": names_by_id[extending_id],
                "condition": _clean_text(selection.condition),
                "extension_point": step_ref,
                "extension_point_name": _clean_text(selection.extension_point_name),
                "step_refs": [
                    {
                        "use_case_id": base_id,
                        "step_ref": step_ref,
                        "sentence": valid_steps[base_id][step_ref],
                    }
                ],
                "requirement_ids": requirement_ids,
                "requirement_refs": [
                    {"use_case_id": extending_id, "requirement_id": requirement_id}
                    for requirement_id in requirement_ids
                ],
            }
        )
    return relations, dropped


def _suppress_redundant_associations(
    associations: list[dict], relations: dict[str, list[dict]], actors: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Hide a relation-owned duplicate entry point for the same actor lineage."""
    actor_refs = {str(actor.get("actor_ref") or "") for actor in actors}
    parent_by_child = {
        str(actor["actor_ref"]): str(actor["parent_actor_ref"])
        for actor in actors
        if actor.get("actor_ref") and actor.get("parent_actor_ref") in actor_refs
    }
    pairs = {(str(item["actor_ref"]), str(item["use_case_id"])) for item in associations}
    bases_by_target: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for relation in relations["includes"]:
        bases_by_target[str(relation["included_use_case_id"])].append(
            (str(relation["base_use_case_id"]), "include")
        )
    for relation in relations["extends"]:
        bases_by_target[str(relation["extending_use_case_id"])].append(
            (str(relation["base_use_case_id"]), "extend")
        )

    kept: list[dict] = []
    suppressed: list[dict] = []
    for association in associations:
        actor = str(association["actor_ref"])
        target = str(association["use_case_id"])
        lineage = {actor, *_ancestor_names(actor, parent_by_child)}
        inherited = next(
            (
                (base_id, kind)
                for base_id, kind in bases_by_target.get(target, [])
                if any((candidate_actor, base_id) in pairs for candidate_actor in lineage)
            ),
            None,
        )
        if inherited is None:
            kept.append(association)
            continue
        base_id, kind = inherited
        suppressed.append(
            {
                "actor_ref": actor,
                "use_case_id": target,
                "via_use_case_id": base_id,
                "relation_kind": kind,
            }
        )
    return kept, suppressed


def select_relationship_parts(
    state: AgentState,
    use_cases: list[RelationshipRecord],
    specs_by_id: dict[str, RelationshipRecord],
    candidates: list[RelationshipRecord],
    existing_include_options: list[RelationshipRecord],
    feedback: str,
    *,
    proposal_call: StructuredProposalCall | None = None,
) -> tuple[
    list[RelationshipRecord],
    list[RelationshipRecord],
    list[RelationshipRecord],
    list[str],
]:
    """수락된 근거의 유한 후보 안에서 include/extend 관계를 선택한다."""

    propose = proposal_call or invoke_structured
    include_model = RelationshipModel()
    existing_include_model = ExistingIncludeModel()
    extend_model = RelationshipModel()
    if specs_by_id:
        derived_include_prompt = _relationship_prompt(
            state,
            use_cases,
            specs_by_id,
            candidates,
            [],
            feedback,
        )
        system = prompts.generation_system_for(rules.DRAW_DIAGRAM)
        if candidates:
            include_model = propose(
                RelationshipModel,
                [
                    SystemMessage(content=system),
                    HumanMessage(
                        content=(
                            f"{derived_include_prompt}\n\n"
                            "Task focus: decide derived include candidates only. Return extends "
                            "as an empty list."
                        )
                    ),
                ],
            )
        if existing_include_options:
            existing_include_prompt = _relationship_prompt(
                state,
                use_cases,
                specs_by_id,
                [],
                existing_include_options,
                feedback,
            )
            existing_include_model = propose(
                ExistingIncludeModel,
                [
                    SystemMessage(content=system),
                    HumanMessage(
                        content=(
                            f"{existing_include_prompt}\n\n"
                            "Task focus: decide existing include selections only. Return one "
                            "existing target ID with all matching supplied base steps, or an "
                            "empty list."
                        )
                    ),
                ],
            )
        extend_prompt = _relationship_prompt(
            state, use_cases, specs_by_id, [], [], feedback
        )
        extend_selections: list[ExtendSelection] = []
        for extending_use_case_id in _extend_candidate_ids(
            state, use_cases, specs_by_id
        ):
            focused_model = propose(
                RelationshipModel,
                [
                    SystemMessage(content=system),
                    HumanMessage(
                        content=(
                            f"{extend_prompt}\n\n"
                            "Task focus: decide extend relationships only for "
                            f"extending_use_case_id={extending_use_case_id}. Return includes as "
                            "an empty list. Every selected base must be directly named or "
                            "unambiguously described by this use case's own requirement evidence; "
                            "sharing a validation, actor, or business object is insufficient."
                        )
                    ),
                ],
            )
            extend_selections.extend(
                selection
                for selection in focused_model.extends
                if selection.extending_use_case_id == extending_use_case_id
            )
        extend_model = RelationshipModel(extends=extend_selections)
    derived_includes, derived, include_drops = _materialize_includes(
        include_model, candidates, use_cases
    )
    existing_includes, existing_include_drops = _materialize_existing_includes(
        existing_include_model, existing_include_options, use_cases
    )
    includes = [*derived_includes, *existing_includes]
    extends, extend_drops = _materialize_extends(extend_model, use_cases, specs_by_id)
    return (
        includes,
        extends,
        derived,
        [*include_drops, *existing_include_drops, *extend_drops],
    )


@contract("identify_relationships", requires=("use_cases", "actors"), produces=("relationships",))
def identify_relationships(
    state: AgentState,
    feedback: str = "",
    *,
    proposal_call: StructuredProposalCall | None = None,
    review_call: SemanticReviewCall | None = None,
) -> ModelingStagePatch:
    """actor 사실을 투영하고 집중 호출로 유한 include/extend 관계를 선택한다."""
    feedback = feedback_for(cast(dict[str, object], state), "relationships", feedback)
    use_cases = cast(list[dict], state.get("use_cases") or [])
    empty: dict[str, Any] = {
        "associations": [],
        "includes": [],
        "extends": [],
        "generalizations": [],
        "derived_use_cases": [],
        "suppressed_associations": [],
        "orphan_actors": [],
        "dropped_refs": [],
        "relationship_issues": [],
    }
    if not use_cases:
        return {"relationships": empty, "phase": "relationships"}

    actors = cast(list[dict], state.get("actors") or [])
    associations, generalizations, actor_drops, orphan_actors = _actor_projection(
        use_cases, actors
    )
    accepted_ids = {str(use_case["id"]) for use_case in use_cases}
    specs_by_id, spec_rejections = _accepted_specs_by_id(state, accepted_ids)
    candidates = _include_candidates(state, use_cases, specs_by_id)
    existing_include_options = _existing_include_options(state, use_cases, specs_by_id)

    def build(selection_feedback: str) -> dict[str, Any]:
        includes, extends, derived, selection_drops = select_relationship_parts(
            state,
            use_cases,
            specs_by_id,
            candidates,
            existing_include_options,
            selection_feedback,
            proposal_call=proposal_call,
        )
        projected_associations, suppressed = _suppress_redundant_associations(
            associations, {"includes": includes, "extends": extends}, actors
        )
        return {
            "associations": projected_associations,
            "includes": includes,
            "extends": extends,
            "generalizations": generalizations,
            "derived_use_cases": derived,
            "suppressed_associations": suppressed,
            "orphan_actors": orphan_actors,
            "dropped_refs": [
                *actor_drops,
                *(f"{item['use_case_id']}: {item['reason']}" for item in spec_rejections),
                *selection_drops,
            ],
            "relationship_issues": [],
            "semantic_status": "single_pass" if specs_by_id else "not_run",
            "repair_iters": 0,
            "repair_stopped": "single_pass" if specs_by_id else "not_applicable",
        }

    relations = build(feedback)
    if specs_by_id:
        reviewer = review_call or validator.review

        def review_candidate(candidate: dict[str, Any], subject: str) -> validator.Review:
            return reviewer(
                rules.DRAW_DIAGRAM,
                _relationship_review_payload(
                    cast(list[dict], state.get("classified") or []),
                    actors,
                    use_cases,
                    specs_by_id,
                    candidate,
                ),
                prefix="rel",
                source="relationships.semantic_validator",
                subject=subject,
                confirm_violations=True,
            )

        review = review_candidate(relations, "initial")
        ledger = RepairLedger()
        attempts = 0
        strategies = ("targeted_selection", "full_reselection")
        while review.status == validator.OK and review.findings:
            finding_keys = tuple(
                sorted({rules.rule_of(finding) or finding for finding in review.findings})
            )
            input_digest = stable_digest(
                {"relationships": relations, "findings": finding_keys}
            )
            strategy = next(
                (
                    key
                    for key in strategies
                    if not ledger.strategy_attempted(
                        input_digest=input_digest,
                        finding_keys=finding_keys,
                        strategy_key=key,
                    )
                ),
                None,
            )
            if strategy is None:
                ledger.status = "STALLED"
                ledger.stall_reason = "No untried relationship repair strategy remains."
                break
            repair_feedback = "\n".join([
                feedback,
                "Correct only these independently confirmed relationship defects. "
                "Re-evaluate the supplied candidates and return a complete relationship "
                "selection without changing use cases or specifications.",
                f"Strategy: {strategy}.",
                f"Repair history:\n{ledger.prompt_context()}",
                *(f"- {finding}" for finding in review.findings),
            ]).strip()
            attempts += 1
            try:
                candidate = build(repair_feedback)
                candidate_review = review_candidate(candidate, f"repair-{attempts}")
            except Exception as exc:  # noqa: BLE001 - preserve the best accepted projection
                waiting = transient_llm_error(exc)
                ledger.record(
                    RepairAttempt(
                        stage="requirements.relationships",
                        strategy_key=strategy,
                        input_digest=input_digest,
                        finding_keys_before=finding_keys,
                        finding_keys_after=finding_keys,
                        outcome="waiting_external" if waiting else "error",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
                ledger.status = "WAITING_EXTERNAL" if waiting else "STALLED"
                ledger.stall_reason = str(exc)
                break
            candidate_keys = tuple(
                sorted(
                    {
                        rules.rule_of(finding) or finding
                        for finding in candidate_review.findings
                    }
                )
            )
            candidate_digest = stable_digest(candidate)
            repeated = ledger.candidate_seen(
                input_digest=input_digest,
                candidate_digest=candidate_digest,
            )
            improved = (
                candidate_review.status == validator.OK
                and not repeated
                and repair_makes_progress(finding_keys, candidate_keys)
            )
            ledger.record(
                RepairAttempt(
                    stage="requirements.relationships",
                    strategy_key=strategy,
                    input_digest=input_digest,
                    candidate_digest=candidate_digest,
                    finding_keys_before=finding_keys,
                    finding_keys_after=candidate_keys,
                    outcome=(
                        "repeated_candidate"
                        if repeated
                        else "clean"
                        if improved and not candidate_keys
                        else "improved"
                        if improved
                        else "no_improvement"
                    ),
                )
            )
            if not improved:
                continue
            relations = candidate
            review = candidate_review
        if review.status == validator.OK and not review.findings:
            ledger.status = "COMPLETED"
        relations["relationship_issues"] = review.findings
        relations["semantic_status"] = review.status
        relations["unexamined_rules"] = list(review.unexamined)
        relations["repair_iters"] = attempts
        relations["repair_history"] = ledger.model_dump(mode="json")
        relations["repair_stopped"] = (
            "clean"
            if review.status == validator.OK and not review.findings
            else "waiting_external"
            if ledger.status == "WAITING_EXTERNAL"
            else "stalled"
        )
    return {"relationships": relations, "phase": "relationships"}


@contract("check_relationships", requires=("relationships",), produces=("relationship_report",))
def check_relationships(state: AgentState) -> ModelingStagePatch:
    """결정론적 관계 무결성 진단을 기존 report shape로 집계한다."""
    rel = state.get("relationships") or {}
    use_cases = cast(list[dict], state.get("use_cases") or [])
    declared_supporting = {
        (str(actor_ref), str(use_case["id"]))
        for use_case in use_cases
        for actor_ref in use_case.get("supporting_actor_refs", []) or []
    }
    associations = {
        (str(item.get("actor_ref") or ""), str(item.get("use_case_id") or ""))
        for item in rel.get("associations", [])
    }
    actor_refs = {str(actor.get("actor_ref") or "") for actor in state.get("actors") or []}
    parent_by_child = {
        str(actor["actor_ref"]): str(actor["parent_actor_ref"])
        for actor in state.get("actors") or []
        if actor.get("actor_ref") and actor.get("parent_actor_ref") in actor_refs
    }
    bases_by_target: dict[str, set[str]] = defaultdict(set)
    for relation in [*rel.get("includes", []), *rel.get("extends", [])]:
        target_key = (
            "included_use_case_id"
            if "included_use_case_id" in relation
            else "extending_use_case_id"
        )
        bases_by_target[str(relation.get(target_key) or "")].add(
            str(relation.get("base_use_case_id") or "")
        )

    def is_associated(actor: str, use_case_id: str, seen: set[str] | None = None) -> bool:
        if (actor, use_case_id) in associations or any(
            (ancestor, use_case_id) in associations
            for ancestor in _ancestor_names(actor, parent_by_child)
        ):
            return True
        visited = seen or set()
        return use_case_id not in visited and any(
            is_associated(actor, base_id, visited | {use_case_id})
            for base_id in bases_by_target.get(use_case_id, set())
        )

    report = {
        "counts": {
            key: len(rel.get(key, []))
            for key in (
                "associations",
                "includes",
                "extends",
                "generalizations",
                "derived_use_cases",
            )
        },
        "orphan_actors": rel.get("orphan_actors", []),
        "declared_supporting_associations": len(declared_supporting),
        "missing_supporting_associations": sorted(
            f"{actor} -> {use_case_id}"
            for actor, use_case_id in declared_supporting
            if not is_associated(actor, use_case_id)
        ),
        "dropped_refs": rel.get("dropped_refs", []),
        "relationship_issues": rel.get("relationship_issues", []),
        "semantic_status": rel.get("semantic_status", "unknown"),
        "unexamined_rules": rel.get("unexamined_rules", []),
        "repair_iters": rel.get("repair_iters", 0),
        "repair_stopped": rel.get("repair_stopped", "unknown"),
    }
    return {"relationship_report": report, "phase": "check_relationships"}
