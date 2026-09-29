"""Pairwise, source-grounded reconciliation of identifier outputs across use cases."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from app.design.services.class_diagram.scenario import build_scenario_index
from app.requirements.runtime.structured_llm import invoke_structured


class CandidatePair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    producer_use_case_id: str
    consumer_use_case_id: str
    consumer_value_ref: str


class ConsumerCandidates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consumer_use_case_id: str
    consumer_value_ref: str
    candidate_producer_ids: list[str] = Field(default_factory=list)


class CandidateReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consumers: list[ConsumerCandidates] = Field(default_factory=list)


class PairReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    add_output: bool
    consumer_value_ref: str
    output_name: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    existing_output_value_ref: str | None = None


ModelCall = Callable[[type[BaseModel], list], BaseModel]


def _candidate_pairs(review: CandidateReview) -> list[CandidatePair]:
    return [
        CandidatePair(
            producer_use_case_id=producer_id,
            consumer_use_case_id=consumer.consumer_use_case_id,
            consumer_value_ref=consumer.consumer_value_ref,
        )
        for consumer in review.consumers
        for producer_id in consumer.candidate_producer_ids
    ]


def _call_model(schema: type[BaseModel], messages: list) -> BaseModel:
    return invoke_structured(schema, messages)


def _contract(spec: dict) -> dict:
    value = spec.get("public_contract")
    return value if isinstance(value, dict) else {}


def _identifier_inputs(spec: dict) -> list[dict]:
    contract = _contract(spec)
    values = contract.get("required_values")
    return [
        value for value in values or []
        if isinstance(value, dict)
        and value.get("source") == "caller_input"
        and value.get("value_type") == "identifier"
        and isinstance(value.get("value_ref"), str)
    ]


def _step_evidence(spec: dict, canonical_ids: set[str]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    uc_id = str(spec.get("use_case_id") or "")
    main_requirements: dict[str, set[str]] = {}
    for step in spec.get("main_scenario") or []:
        if not isinstance(step, dict) or not isinstance(step.get("step_number"), int):
            continue
        ref = f"{uc_id}:main:{step['step_number']}"
        linked = {str(item) for item in step.get("covered_req_ids") or []}
        main_requirements[str(step["step_number"])] = linked
        if ref in canonical_ids:
            result[ref] = linked
    ordinals: dict[str, int] = {}
    for extension in spec.get("extensions") or []:
        if not isinstance(extension, dict):
            continue
        anchor = str(extension.get("branch_step") or "")
        ordinals[anchor] = ordinals.get(anchor, 0) + 1
        extension_ref = f"{uc_id}:extension:{anchor}:{ordinals[anchor]}"
        linked = set(main_requirements.get(anchor, set()))
        for step in extension.get("handling_steps") or []:
            sub_step = str(step.get("sub_step") or "")
            ref = f"{extension_ref}:{sub_step}"
            if ref in canonical_ids:
                result[ref] = linked | {
                    str(item) for item in step.get("covered_req_ids") or []
                }
    return result


def _stable_value_ref(use_case_id: str, position: int) -> str:
    encoded = json.dumps(
        [use_case_id, str(position)], ensure_ascii=False, separators=(",", ":")
    )
    return f"val_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20]}"


def reconcile_cross_use_case_values(
    specs: list[dict],
    use_cases: list[dict],
    requirements: list[dict],
    *,
    model_call: ModelCall | None = None,
    allowed_producer_ids: set[str] | None = None,
) -> list[dict]:
    """Use a compact candidate pass, then review each proposed pair with full evidence."""
    if not specs or (allowed_producer_ids is not None and not allowed_producer_ids):
        return specs
    call = model_call or _call_model
    by_id = {str(spec.get("use_case_id")): spec for spec in specs}
    uc_by_id = {str(item.get("id")): item for item in use_cases}
    req_by_id = {str(item.get("id")): item for item in requirements}
    candidates = []
    all_use_cases = []
    for spec in specs:
        uc_id = str(spec.get("use_case_id") or "")
        uc = uc_by_id.get(uc_id, {})
        all_use_cases.append({
            "id": uc_id,
            "name": uc.get("name", spec.get("name", "")),
            "goal": uc.get("goal", ""),
            "requirement_ids": uc.get("requirement_ids", []),
        })
        inputs = _identifier_inputs(spec)
        if inputs:
            candidates.extend(
                {"useCaseId": uc_id, "valueRef": v["value_ref"], "name": v["name"]}
                for v in inputs
            )
    if not candidates:
        return specs
    prompt = (
        "For each consumer identifier, return 0-3 possible creator candidates from the supplied "
        "use cases; detailed review will decide whether each relation is supported. Use names, "
        "goals, requirements, and flow clues to retrieve candidates broadly. Names alone are "
        "not final proof. Return an empty candidate list only when no plausible producer exists.\n"
        f"Use cases: {json.dumps(all_use_cases, ensure_ascii=False)}\n"
        f"Consumer identifiers: {json.dumps(candidates, ensure_ascii=False)}\n"
        f"Eligible producer IDs: {json.dumps(sorted(allowed_producer_ids) if allowed_producer_ids is not None else list(by_id))}"
    )
    proposed = call(CandidateReview, [
        SystemMessage(content="Select candidate cross-use-case identifier flows."),
        HumanMessage(content=prompt),
    ])
    result = deepcopy(specs)
    canonical_ids = set(build_scenario_index(
        {"use_cases": use_cases, "use_case_specs": result}
    ).step_ids)
    current = {str(item.get("use_case_id")): item for item in result}
    seen: set[tuple[str, str, str]] = set()
    for pair in _candidate_pairs(proposed):
        producer_id, consumer_id, consumer_ref = (
            pair.producer_use_case_id, pair.consumer_use_case_id, pair.consumer_value_ref
        )
        key = (producer_id, consumer_id, consumer_ref)
        producer, consumer = current.get(producer_id), current.get(consumer_id)
        if producer is None or consumer is None or producer_id == consumer_id or key in seen:
            continue
        if allowed_producer_ids is not None and producer_id not in allowed_producer_ids:
            continue
        consumer_value = next(
            (v for v in _identifier_inputs(consumer) if v["value_ref"] == consumer_ref), None
        )
        if consumer_value is None:
            continue
        if not isinstance(_contract(producer).get("required_values"), list):
            continue
        p_steps = _step_evidence(producer, canonical_ids)
        detailed_prompt = (
            "Assess only this candidate relation using the supplied complete pair of specs. "
            "Add an output only when the producer creates a durable resource evidenced by a "
            "producer scenario step, the consumer caller-input identifier refers to that "
            "resource, the producer does not already return its identifier, and the producer "
            "does not already take that same resource identifier as caller input. Never repeat "
            "the producer's existing identifier input as its result. Cite exact producer step "
            "refs and producer-linked requirement IDs. Select an existing result value_ref if "
            "the identifier is already returned; otherwise set add_output=false when unsupported.\n"
            f"Producer spec: {json.dumps(producer, ensure_ascii=False)}\n"
            f"Consumer spec: {json.dumps(consumer, ensure_ascii=False)}\n"
            f"Valid producer scenario evidence refs and covered requirements: "
            f"{json.dumps({ref: sorted(reqs) for ref, reqs in p_steps.items()}, ensure_ascii=False)}\n"
            f"Producer linked requirement IDs: {json.dumps(producer.get('requirement_ids') or [])}\n"
            f"Requirement text: {json.dumps({rid: req_by_id[rid].get('text', '') for rid in producer.get('requirement_ids', []) if rid in req_by_id}, ensure_ascii=False)}"
        )
        review = call(PairReview, [
            SystemMessage(content="Review one cross-use-case identifier relation."),
            HumanMessage(content=detailed_prompt),
        ])
        seen.add(key)
        if (
            not review.add_output or review.consumer_value_ref != consumer_ref
            or not review.output_name or not review.output_name.strip()
            or review.existing_output_value_ref is not None
        ):
            continue
        producer_requirements = {str(v) for v in producer.get("requirement_ids") or []}
        cited_requirements = set(review.requirement_ids)
        valid_step_refs = {ref for ref in p_steps}
        cited_steps = set(review.evidence_refs)
        if not cited_steps or not cited_steps.issubset(valid_step_refs):
            continue
        supported_requirements = (
            cited_requirements
            & producer_requirements
            & set().union(*(p_steps[step_ref] for step_ref in cited_steps))
        )
        if not supported_requirements:
            continue
        contract = producer.setdefault("public_contract", {
            "schema_version": "PublicBehaviorContract/v1",
            "identity_obligations": [],
            "required_values": [],
        })
        values = contract.setdefault("required_values", [])
        output_name = " ".join(review.output_name.split())
        if any(
            isinstance(value, dict)
            and " ".join(str(value.get("name") or "").split()).casefold()
            == output_name.casefold()
            for value in values
        ):
            continue
        values.append({
            "value_ref": _stable_value_ref(producer_id, len(values) + 1),
            "name": output_name,
            "source": "system_result",
            "value_type": "identifier",
            "usage": "result",
            "requirement_ids": sorted(supported_requirements),
        })
    return result
