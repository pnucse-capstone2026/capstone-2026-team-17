"""STEP 3 — 유스케이스별 명세 proposal·검증·history-aware repair stage다.

step2의 각 유스케이스에 대해 주 시나리오 + 확장(예외/대안) + 사전/사후조건을 생성한다.
유스케이스마다 LLM 호출 1건이 독립적이라 ThreadPoolExecutor로 동시 실행해 속도를 높인다
(동시 상한은 settings.spec_concurrency). invoke_structured가 호출마다 자체 ChatOpenAI를
만들므로 스레드 안전하다.

LLM 출력을 그대로 믿지 않고 검증·반성한다:
  - _clean: 문장의 마크다운/특수문자 정리(모델이 **굵게** 등을 섞어도 방어).
  - _validate_spec: 정적(결정론) 체크. 판정은 `knowledge/detectors.py`가 하고 여기서는
    지적을 문자열로 바꾼다 — 규칙과 검출기가 지식베이스에 함께 있어야 지적이 인용을 들고 나간다.
  - _semantic_findings: LLM 의미 검증(hidden branching·scope creep 등, 정적이 못 잡는 것).
    검증자가 댄 규칙 id를 지식베이스와 대조해, **없는 규칙을 인용한 지적은 버린다.**
  - _spec_for의 reflection 루프: 검증 실패 시 이력을 붙여 미사용 전략으로 재생성,
    회귀·반복 후보는 버리고 전략이 소진되면 정체 상태로 전환.

규칙의 출처(책이 적었나 / 우리가 정했나)는 `knowledge/rules.py`에 있다. 책 본문은 저장소에
없다 — 저작물이라 지웠고(`d1a7ec5`), 담는 것은 우리 표현의 규범 문장과 인용 좌표다.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import NotRequired, TypedDict, cast

from langchain_core.messages import HumanMessage, SystemMessage

from app.requirements import prompts
from app.requirements.common.state_contract import contract
from app.requirements.config import settings
from app.requirements.contracts.state import (
    ActorItem,
    AgentState,
    RequirementItem,
    UseCaseItem,
    UseCaseSpecItem,
)
from app.requirements.knowledge import detectors, rules
from app.requirements.modeling import validation as validator
from app.requirements.modeling.contracts import (
    ModelingStagePatch,
    SemanticReviewCall,
    StructuredProposalCall,
)
from app.requirements.modeling.feedback import feedback_for
from app.requirements.modeling.cross_uc_values import reconcile_cross_use_case_values
from app.requirements.runtime import telemetry
from app.requirements.runtime.structured_llm import invoke_structured
from app.requirements.schemas import SemanticAmbiguityReview, UseCaseSpec
from app.requirements.traceability import constraints_for_use_case, modeled_global_constraints
from app.validation import (
    RepairAttempt,
    RepairLedger,
    repair_makes_progress,
    stable_digest,
    transient_llm_error,
)

# 마크다운/특수문자 → plain 정규화 매핑.
_REPLACEMENTS = {
    " ": " ", " ": " ",                    # narrow/no-break space
    "‑": "-", "–": "-", "—": "-",       # non-breaking/en/em dash
    "‘": "'", "’": "'", "“": '"', "”": '"',  # smart quotes
    "**": "", "__": "", "`": "",                       # bold/code 마크업
}

_RULE_TAG = re.compile(r"\[([a-z][a-z0-9_.-]*)\s")
_SPEC_REPAIR_STRATEGIES = (
    "targeted_findings",
    "fresh_contract_regeneration",
)


class _NeighbourGoal(TypedDict):
    """같은 요구사항을 공유하지만 현재 명세 범위 밖인 목표다."""

    id: str
    name: str
    goal: str


class _SpecificationInput(UseCaseItem):
    """명세 생성 동안만 붙는 결정론적 문맥을 포함한 유스케이스다."""

    _neighboring_goals: NotRequired[list[_NeighbourGoal]]
    _constraint_requirements: NotRequired[list[dict[str, object]]]
    _global_constraint_context: NotRequired[list[dict[str, object]]]
    _existing_spec: NotRequired[UseCaseSpecItem]


def normalize_text(text: str) -> str:
    """문장에서 마크다운 마크업과 특수 공백/따옴표를 제거해 plain 텍스트로 만든다."""
    for src, dst in _REPLACEMENTS.items():
        text = text.replace(src, dst)
    return text.strip()


def _resolve(ids: list[str], by_id: dict[str, RequirementItem]) -> str:
    """요구 id 목록을 'id: text' 나열로 해석한다(없는 id는 조용히 건너뜀)."""
    lines = [f"- {i}: {by_id[i]['text']}" for i in ids if i in by_id]
    return "\n".join(lines) or "- (none)"


def validate_specification(
    spec: dict[str, object], allowed_subject_refs: set[str] | None = None,
) -> list[str]:
    """명세를 결정론적으로 점검한다(생성은 LLM 휴리스틱, 이 점검은 확정적).

    판정은 `knowledge/detectors.py`가 한다 — 규칙과 검출기가 지식베이스에 함께 있어야
    지적이 근거(규칙 id + 인용)를 들고 나간다. 여기서는 상태·리포트에 실릴 문자열로만 바꾼다.

    예전에는 정규식과 UI 단어 목록이 이 파일 상단에 있었고, "그 목록은 완전목록이 아니다"는
    사실이 **주석에만** 있었다. 그래서 지적을 받는 사람은 그 한계를 알 수 없었다.
    """
    findings = [f.as_issue() for f in detectors.spec_findings(spec)]
    allowed = (allowed_subject_refs or set()) | {"system"}
    step_groups: list[tuple[str, object]] = []
    main = spec.get("main_scenario")
    if isinstance(main, list):
        step_groups.extend((f"main_scenario[{index}]", step) for index, step in enumerate(main))
    extensions = spec.get("extensions")
    if isinstance(extensions, list):
        for ext_index, extension in enumerate(extensions):
            if not isinstance(extension, dict):
                continue
            handling = extension.get("handling_steps")
            if isinstance(handling, list):
                step_groups.extend(
                    (f"extensions[{ext_index}].handling_steps[{step_index}]", step)
                    for step_index, step in enumerate(handling)
                )
    for location, step in step_groups:
        if not isinstance(step, dict):
            continue
        subject_ref = step.get("subject_ref")
        is_actor_ref = isinstance(subject_ref, str) and re.fullmatch(r"ACT\d+", subject_ref)
        if (
            not isinstance(subject_ref, str)
            or (allowed_subject_refs is not None and subject_ref not in allowed)
            or (allowed_subject_refs is None and subject_ref != "system" and not is_actor_ref)
        ):
            findings.append(
                f"[step-subject-ref] {location} must use an accepted actor ref or 'system'."
            )
    findings.extend(_public_contract_findings(spec))
    return findings


def _public_contract_findings(spec: dict[str, object]) -> list[str]:
    """Check source references and declaration consistency in the optional typed projection."""
    contract = spec.get("public_contract")
    if not isinstance(contract, dict):
        return []

    linked_raw = spec.get("requirement_ids")
    linked_ids = {str(value) for value in linked_raw} if isinstance(linked_raw, list) else None
    findings: list[str] = []

    for collection in ("identity_obligations", "required_values"):
        entries = contract.get(collection)
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            refs = entry.get("requirement_ids")
            if linked_ids is None or not isinstance(refs, list):
                continue
            unsupported = sorted({str(ref) for ref in refs} - linked_ids)
            if unsupported:
                findings.append(
                    "[public-contract-integrity] "
                    f"{collection}[{index}] cites requirement IDs not linked as functional "
                    f"requirements to this use case: {', '.join(unsupported)}."
                )

    obligations = contract.get("identity_obligations")
    obligations_by_ref: dict[str, dict[str, object]] = {}
    valid_auth_refs: list[object] = []
    if isinstance(obligations, list):
        valid_auth_refs = [
            entry.get("obligation_ref") for entry in obligations
            if isinstance(entry, dict) and entry.get("obligation") == "authenticate"
            and isinstance(entry.get("obligation_ref"), str)
        ]
        valid_source_kinds = {
            "caller_input", "authenticated_context", "system_result", "unresolved"
        }
        for index, entry in enumerate(obligations):
            if not isinstance(entry, dict) or entry.get("obligation") != "identify":
                continue
            kind = entry.get("identity_source_kind", "unresolved")
            source_ref = entry.get("source_authenticate_obligation_ref")
            if kind not in valid_source_kinds:
                findings.append(
                    f"[public-contract-integrity] identity_obligations[{index}] has an invalid identity_source_kind."
                )
            elif kind == "authenticated_context":
                if not valid_auth_refs or source_ref not in valid_auth_refs:
                    findings.append(
                        f"[public-contract-integrity] identity_obligations[{index}] must reference a same-use-case authenticate obligation."
                    )
            elif source_ref is not None:
                findings.append(
                    f"[public-contract-integrity] identity_obligations[{index}] has an authenticate reference without authenticated_context source."
                )
        obligations_by_ref = {
            entry["obligation_ref"]: entry for entry in obligations
            if isinstance(entry, dict) and isinstance(entry.get("obligation_ref"), str)
        }

    values = contract.get("required_values")
    declarations: dict[str, list[dict[str, object]]] = {}
    linked_identify_refs: set[str] = set()
    if isinstance(values, list):
        value_refs: set[str] = set()
        for entry in values:
            if not isinstance(entry, dict):
                continue
            allowed_values = entry.get("allowed_values")
            if allowed_values is not None and (
                not isinstance(allowed_values, list)
                or not allowed_values
                or any(not isinstance(item, str) or not item.strip() for item in allowed_values)
                or len({item.strip().casefold() for item in allowed_values if isinstance(item, str)})
                != len(allowed_values)
            ):
                findings.append(
                    "[public-contract-integrity] Required value allowed_values must be a nonempty "
                    "list of unique nonempty strings."
                )
            value_ref = entry.get("value_ref")
            if isinstance(value_ref, str) and value_ref in value_refs:
                findings.append(
                    "[public-contract-integrity] Required value value_ref values must be unique within the use case."
                )
            elif isinstance(value_ref, str):
                value_refs.add(value_ref)
            identity_ref = entry.get("identity_obligation_ref")
            if identity_ref is not None and identity_ref not in obligations_by_ref:
                findings.append(
                    "[public-contract-integrity] Required value references an identity obligation outside this use case."
                )
            is_identifier = entry.get("value_type") == "identifier"
            if identity_ref is not None:
                linked = obligations_by_ref.get(identity_ref) if isinstance(identity_ref, str) else None
                if not is_identifier or not isinstance(linked, dict) or linked.get("obligation") != "identify":
                    findings.append(
                        "[public-contract-integrity] An identity obligation link must select a "
                        "same-use-case identify obligation on an identifier value."
                    )
                elif isinstance(identity_ref, str):
                    linked_identify_refs.add(identity_ref)
                if (
                    is_identifier and isinstance(linked, dict)
                    and linked.get("obligation") == "identify"
                    and entry.get("source") == "authenticated_actor_context"
                ) and (
                    linked.get("identity_source_kind") != "authenticated_context"
                    or linked.get("source_authenticate_obligation_ref") not in valid_auth_refs
                ):
                    findings.append(
                        "[public-contract-integrity] An authenticated-context identifier must "
                        "reference its exact same-use-case authenticated identify obligation."
                    )
            elif entry.get("source") == "authenticated_actor_context" and is_identifier:
                findings.append(
                    "[public-contract-integrity] An authenticated-context identifier must "
                    "reference its exact same-use-case authenticated identify obligation."
                )
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            key = " ".join(entry["name"].split()).casefold()
            if key:
                declarations.setdefault(key, []).append(entry)
    if isinstance(obligations, list):
        for index, entry in enumerate(obligations):
            if not isinstance(entry, dict) or entry.get("obligation") != "identify":
                continue
            obligation_ref = entry.get("obligation_ref")
            if not isinstance(obligation_ref, str) or obligation_ref not in linked_identify_refs:
                findings.append(
                    f"[public-contract-integrity] identity_obligations[{index}] identify obligation "
                    "must link to a required identifier value in this use case."
                )
    for name, entries in declarations.items():
        if len(entries) < 2:
            continue
        signatures = {
            (entry.get("source"), entry.get("value_type"), entry.get("usage"))
            for entry in entries
        }
        label = name
        if len(signatures) > 1:
            findings.append(
                f"[public-contract-integrity] Required value '{label}' has conflicting "
                "source, type, or usage declarations."
            )
        else:
            findings.append(
                f"[public-contract-integrity] Required value '{label}' is declared more than once."
            )
    return findings

def _accepted_public_contract_proposal(contract: dict[str, object]) -> dict[str, object]:
    """Project accepted contract refs and derived fields back to proposal-local indexes."""
    obligations = contract.get("identity_obligations") or []
    values = contract.get("required_values") or []
    ref_to_index = {
        item.get("obligation_ref"): index
        for index, item in enumerate(obligations, start=1)
        if isinstance(item, dict) and isinstance(item.get("obligation_ref"), str)
    }
    proposal: dict[str, object] = {"identity_obligations": [], "required_values": []}
    for item in obligations:
        if not isinstance(item, dict):
            continue
        projected = {
            key: item[key]
            for key in ("subject", "obligation", "requirement_ids")
            if key in item
        }
        source_ref = item.get("source_authenticate_obligation_ref")
        if source_ref in ref_to_index:
            projected["source_authenticate_obligation_index"] = ref_to_index[source_ref]
        proposal["identity_obligations"].append(projected)
    for item in values:
        if not isinstance(item, dict):
            continue
        projected = {
            key: item[key]
            for key in ("name", "source", "value_type", "usage", "requirement_ids", "allowed_values")
            if key in item
        }
        identity_ref = item.get("identity_obligation_ref")
        if identity_ref in ref_to_index:
            projected["identity_obligation_index"] = ref_to_index[identity_ref]
        proposal["required_values"].append(projected)
    return proposal


def _spec_human(
    uc: _SpecificationInput,
    by_id: dict[str, RequirementItem],
    actors: list[ActorItem],
    feedback: str = "",
) -> str:
    """명세 생성용 user 프롬프트(유스케이스 + FR/NFR). feedback 시 재생성 지시를 얹는다."""
    primary_actor_ref = uc.get("primary_actor_ref")
    actor = next((a for a in actors if a.get("actor_ref") == primary_actor_ref), None)
    actor_desc = f"{actor['name']} — {actor['description']}" if actor else uc.get("primary_actor", "")
    accepted_refs = list(dict.fromkeys(
        [primary_actor_ref, *(uc.get("supporting_actor_refs") or [])]
    ))
    actor_catalog = [
        f"- {ref}: {next((a.get('name', '') for a in actors if a.get('actor_ref') == ref), '')}"
        for ref in accepted_refs if isinstance(ref, str) and ref
    ]
    actor_catalog_text = "\n".join(actor_catalog) or "- (none)"
    neighbouring_goals = uc.get("_neighboring_goals") or []
    scope = (
        f"Current goal boundary: implement ONLY {uc['name']} — {uc.get('goal', '')}."
    )
    if neighbouring_goals:
        listing = "\n".join(
            f"- {item['id']}: {item['name']} — {item.get('goal', '')}"
            for item in neighbouring_goals
        )
        scope += (
            "\nNeighbouring goals share source requirements but are OUT OF SCOPE. Do not "
            "include them as steps or extensions, and do not infer ordering, lifecycle state, "
            "or preconditions between them unless a requirement states it explicitly:\n"
            f"{listing}"
        )
    applicable_constraints = list(uc.get("_constraint_requirements") or [])
    constraint_listing = "\n".join(
        f"- {item.get('id')}: {item.get('text', '')}"
        for item in applicable_constraints
        if item.get("id")
    ) or "- (none)"
    global_constraint_listing = "\n".join(
        f"- {item.get('id')}: {item.get('text', '')}"
        for item in (uc.get("_global_constraint_context") or [])
        if item.get("id")
    ) or "- (none)"
    base = (
        f"Use case: {uc['name']}\n"
        f"{scope}\n"
        f"Primary actor: {actor_desc}\n"
        f"Finite actor catalog for step subject_ref values (the system sentinel is 'system'):\n"
        f"{actor_catalog_text}\n"
        f"Goal: {uc.get('goal', '')}\n\n"
        f"Functional requirements it covers:\n{_resolve(uc.get('requirement_ids', []), by_id)}\n\n"
        f"Non-functional constraints:\n{_resolve(uc.get('nfr_ids', []), by_id)}\n\n"
        "Applicable RTM constraints (refine this use case; they are not new goals or "
        f"scenario coverage):\n{constraint_listing}\n\n"
        "Explicitly modeled global constraints (source context only; not attached to this "
        "use case):\n"
        f"{global_constraint_listing}\n"
        "Use this context only when this use case's own linked behavior establishes that "
        "the constraint applies. Public or published-data browsing alone, and actor labels "
        "alone, do not establish applicability. Do not assert this use case is protected "
        "merely because a global constraint is listed.\n\n"
        "Public behavior contract: list only identity obligations and values explicitly "
        "established by the covered functional requirements or explicitly applicable constraints. "
        "'authenticate' validates the acting principal or session; 'identify' distinguishes "
        "which subject or record the behavior targets; 'act_on_behalf' means one "
        "subject exercises delegated authority for a different subject, not authentication alone. "
        "When this use case's linked behavior acts on the requester's own subject or record and "
        "an explicitly applicable constraint establishes authenticated identity, include both "
        "authenticate and identify obligations, plus their linked identifier RequiredValue with "
        "source authenticated_actor_context; link that value to identify and identify to "
        "authenticate using the proposal indexes. "
        "Each entry's requirement_ids must cite the linked functional requirement for its "
        "behavior; applicable global constraints justify source or applicability in context, "
        "not entry requirement_ids, unless themselves linked. "
        "For each value, cite requirement IDs and report its source (caller_input, "
        "authenticated_actor_context, or system_result), type, and use (control, result, or both). "
        "Use describes direction relative to this use case's behavior, not origin: control means "
        "the system consumes the value to perform an operation; result means the system produces "
        "the value as an observable outcome; both requires evidence of both directions. Source is "
        "provenance only: a value from authenticated_actor_context may be consumed by a Control "
        "operation and does not become a result merely because it is server-provided. Ground the "
        "use in the scenario and observable outcome; do not classify an unmentioned value as a "
        "result. "
        "For allowed_values, include a list only when the linked requirements explicitly define "
        "a finite set of alternatives, including branch alternatives; copy those alternatives "
        "faithfully. Omit it for open-ended inputs and calculator-style values. "
        "The normalizer assigns each accepted required value its stable value_ref; do not invent "
        "or copy value references. "
        + prompts.IDENTITY_SOURCE_INSTRUCTIONS + " "
        "A caller supplied identifier remains untrusted input and is not proof of identity. "
        "Leave lists empty when the requirements establish no obligation; do not infer login, "
        "authorization, identity checks, or required values."
    )
    existing_spec = uc.get("_existing_spec")
    if existing_spec:
        current = {
            key: existing_spec.get(key)
            for key in (
                "preconditions",
                "trigger",
                "main_scenario",
                "extensions",
                "success_guarantee",
                "minimal_guarantee",
            )
        }
        current["public_contract"] = _accepted_public_contract_proposal(
            existing_spec.get("public_contract") or {}
        )
        base += (
            "\n\n[CURRENT SPECIFICATION — use this as the authoritative baseline. "
            "Return the full specification, but change only what the user feedback asks "
            "for and preserve every other field exactly.]\n"
            + json.dumps(current, ensure_ascii=False, sort_keys=True)
        )
    return prompts.apply_user_feedback(base, feedback)


def normalize_specification(spec: UseCaseSpec, uc: UseCaseItem) -> UseCaseSpecItem:
    """구조화 출력을 정리(_clean)해 상태 dict로 조립한다(issues는 이후 계산)."""
    # Refs are minted from accepted source identity, never from the displayed
    # subject label. requirement_ids remain evidence references and are not
    # copied into either identity field.
    identity_obligations: list[dict[str, object]] = []
    used_obligation_refs: set[str] = set()

    def opaque_ref(prefix: str, parts: list[str]) -> str:
        encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]
        return f"{prefix}_{digest}"

    for position, obligation in enumerate(spec.public_contract.identity_obligations, start=1):
        requirement_ids = list(obligation.requirement_ids)
        position_key = [str(uc["id"]), str(position)]
        stable_subject_ref = opaque_ref("sub", position_key)
        subject_ref = obligation.subject_ref
        if not isinstance(subject_ref, str) or not re.fullmatch(r"sub_[0-9a-f]{20}", subject_ref):
            subject_ref = stable_subject_ref

        obligation_ref = obligation.obligation_ref
        if not isinstance(obligation_ref, str) or not re.fullmatch(r"ob_[0-9a-f]{20}", obligation_ref):
            obligation_ref = opaque_ref("ob", position_key)
        # Guard duplicate proposals and the vanishingly unlikely digest clash.
        collision_index = 1
        while obligation_ref in used_obligation_refs:
            obligation_ref = opaque_ref("ob", [*position_key, str(collision_index)])
            collision_index += 1
        used_obligation_refs.add(obligation_ref)
        normalized_obligation = {
            "obligation_ref": obligation_ref,
            "subject_ref": subject_ref,
            "subject": obligation.subject,
            "obligation": obligation.obligation,
            "identity_source_kind": "unresolved",
            "requirement_ids": requirement_ids,
        }
        identity_obligations.append(normalized_obligation)

    required_values: list[dict[str, object]] = []
    for position, value in enumerate(spec.public_contract.required_values, start=1):
        normalized_value: dict[str, object] = {
            "value_ref": opaque_ref("val", [str(uc["id"]), str(position)]),
            "name": value.name,
            "source": value.source,
            "value_type": value.value_type,
            "usage": value.usage,
            "requirement_ids": list(value.requirement_ids),
        }
        if value.allowed_values is not None:
            normalized_value["allowed_values"] = list(value.allowed_values)
        selected_index = value.identity_obligation_index
        if selected_index is not None and 1 <= selected_index <= len(identity_obligations):
            normalized_value["identity_obligation_ref"] = identity_obligations[selected_index - 1]["obligation_ref"]
        required_values.append(normalized_value)

    # RequiredValue.source is the proposal's sole source of truth. Resolve links
    # only through explicit proposal-local indexes; labels and requirement IDs
    # are evidence, never join keys.
    source_kind = {
        "caller_input": "caller_input",
        "authenticated_actor_context": "authenticated_context",
        "system_result": "system_result",
    }
    for position, obligation in enumerate(spec.public_contract.identity_obligations, start=1):
        accepted = identity_obligations[position - 1]
        if obligation.obligation != "identify":
            continue
        linked_sources = {
            value.source for value in spec.public_contract.required_values
            if value.identity_obligation_index == position
        }
        if len(linked_sources) != 1:
            continue
        source = next(iter(linked_sources))
        accepted["identity_source_kind"] = source_kind[source]
        if source != "authenticated_actor_context":
            continue
        auth_index = obligation.source_authenticate_obligation_index
        if auth_index is None or not 1 <= auth_index <= len(identity_obligations):
            accepted["identity_source_kind"] = "unresolved"
            continue
        auth_obligation = identity_obligations[auth_index - 1]
        if auth_obligation["obligation"] != "authenticate":
            accepted["identity_source_kind"] = "unresolved"
            continue
        accepted["source_authenticate_obligation_ref"] = auth_obligation["obligation_ref"]

    return {
        "use_case_id": uc["id"],
        "name": uc["name"],
        "requirement_ids": list(uc["requirement_ids"]),
        "nfr_ids": list(uc["nfr_ids"]),
        "preconditions": [normalize_text(p) for p in spec.preconditions],
        "trigger": normalize_text(spec.trigger),
        "main_scenario": [
            {"step_number": s.step_number, "subject_ref": s.subject_ref,
             "sentence": normalize_text(s.sentence),
             "covered_req_ids": s.covered_req_ids}
            for s in spec.main_scenario
        ],
        "extensions": [
            {"label": normalize_text(e.label), "branch_step": e.branch_step,
             "condition": normalize_text(e.condition),
             "handling_steps": [{"sub_step": h.sub_step, "subject_ref": h.subject_ref,
                                 "sentence": normalize_text(h.sentence)}
                                for h in e.handling_steps],
             "outcome": e.outcome, "resume_at_step": e.resume_at_step}
            for e in spec.extensions
        ],
        "success_guarantee": [
            {
                "sentence": normalize_text(guarantee.sentence),
                "covered_req_ids": list(guarantee.covered_req_ids),
            }
            for guarantee in spec.success_guarantee
        ],
        "minimal_guarantee": [
            {
                "sentence": normalize_text(guarantee.sentence),
                "covered_req_ids": list(guarantee.covered_req_ids),
            }
            for guarantee in spec.minimal_guarantee
        ],
        "public_contract": {
            **spec.public_contract.model_dump(mode="json", exclude={"identity_obligations", "required_values"}),
            "identity_obligations": identity_obligations,
            "required_values": required_values,
        },
        "issues": [],
        "repair_iters": 0,
        # _check가 곧 덮어쓴다. 조립 시점에는 아직 아무 검증도 안 했다.
        "semantic_status": validator.PENDING,
    }


#: 검증자에게 보여줄 명세의 공개 필드 계약. 여기 없는 것은 검증자가 못 본다.
SPECIFICATION_REVIEW_FIELDS = (
    "trigger",
    "preconditions",
    "main_scenario",
    "extensions",
    "success_guarantee",
    "minimal_guarantee",
    "public_contract",
)

def spec_review_payload(
    item: dict[str, object],
    requirements: list[dict[str, object]] | None = None,
    goal_context: dict[str, object] | None = None,
    constraints: list[dict[str, object]] | None = None,
    global_constraint_context: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """검증자가 받는 모양. **공개 함수인 이유는 평가가 같은 모양을 써야 하기 때문**이다.

    평가(`evaluation/`)가 이 모양을 따로 알고 있으면, 파이프라인이 보여주는 것과 눈금이
    재는 것이 조용히 달라진다 — 그러면 눈금 수치가 파이프라인에 대한 말이 아니게 된다.
    """
    payload: dict[str, object] = {
        key: item[key] for key in SPECIFICATION_REVIEW_FIELDS if key in item
    }
    if item.get("name"):
        payload["use_case_name"] = item["name"]
    if goal_context:
        payload.update(goal_context)
    payload["requirements_it_must_cover"] = requirements or []
    if constraints:
        payload["constraints_it_must_respect"] = constraints
    if global_constraint_context:
        payload["explicit_global_constraint_source_context"] = global_constraint_context
        payload["global_constraint_context_scope"] = (
            "Use only when this use case's linked behavior establishes applicability. "
            "Public or published-data browsing alone and actor labels alone do not establish "
            "applicability; listing a global constraint does not mean this use case is protected."
        )
    return payload


def _semantic_findings(
    item: UseCaseSpecItem,
    requirements: list[dict[str, object]] | None = None,
    goal_context: dict[str, object] | None = None,
    constraints: list[dict[str, object]] | None = None,
    review_call: SemanticReviewCall | None = None,
    global_constraint_context: list[dict[str, object]] | None = None,
) -> tuple[list[str], str]:
    """정적 체크가 못 잡는 의미 결함을 독립 검증자에게 묻는다.

    판정은 `modeling/validation.py`가 한다. 여기서 만드는 payload가 **black-box 경계**다:

      - 넣는다: 산출물(명세)과 그 명세가 다뤄야 할 **요구사항**. 요구사항은 판정의 대상이
        아니라 잣대다 — `spec.no-scope-creep`("주어진 요구에 없는 기능을 만들지 말라")은
        요구사항을 못 보면 **판정 자체가 불가능하다.** 2026-07-26까지 실제로 그랬다:
        규칙 목록에는 있는데 근거가 payload에 없어서, 검증자는 짐작으로 답할 수밖에 없었다.
        평가 세트의 의미 규칙 눈금(`evaluation/seeded.py`)을 만들다 드러났다.
      - 넣지 않는다: 생성 프롬프트·사용자 피드백·재생성 이력. 그걸 보여주면 검증자가
        "규칙을 지켰나" 대신 "지시를 따랐나"를 보게 된다.

    `(결함 목록, 검증 상태)`를 돌려준다. 상태를 함께 내는 이유는 **"결함 없음"과
    "확인하지 못함"이 같은 값이 되면 안 되기 때문**이다. 예전에는 검증기가 예외로
    죽어도 빈 리스트를 돌려줬고, 그러면 NIM이 내려간 동안 생성된 모든 명세가 조용히
    '깨끗함'으로 통과했다.
    """
    payload = spec_review_payload(
        cast(dict[str, object], item), requirements, goal_context, constraints,
        global_constraint_context,
    )
    reviewer = review_call or validator.review
    source_rule_id = "spec.public-behavior-completeness"
    broad_rule_ids = tuple(
        rule.id for rule in rules.judged_by(
            rules.WRITE_SPECIFICATIONS, rules.JUDGED_VALIDATOR
        )
        if rule.id != source_rule_id
    )
    review = reviewer(
        rules.WRITE_SPECIFICATIONS,
        payload,
        prefix="semantic",
        source="spec.semantic_validator",
        subject=item.get("use_case_id"),
        rule_ids=broad_rule_ids,
        confirm_violations=True,
    )
    findings = list(review.findings)
    status = validator.UNGROUNDED if review.unexamined else review.status
    if not findings and status == validator.OK:
        source_review = reviewer(
            rules.WRITE_SPECIFICATIONS,
            payload,
            prefix="semantic",
            source="spec.public_behavior_source_validator",
            subject=item.get("use_case_id"),
            rule_ids=(source_rule_id,),
            confirm_violations=False,
        )
        findings.extend(source_review.findings)
        if source_review.status != validator.OK or source_review.unexamined:
            status = validator.UNGROUNDED
    # A partial verdict is not a clean semantic review. Keep the persisted shape small by
    # representing that existing condition with the existing unvalidated status instead of
    # adding another per-spec audit field.
    return findings, status


def requirement_view(
    uc: UseCaseItem, by_id: dict[str, RequirementItem]
) -> list[dict[str, object]]:
    """이 UC가 다뤄야 할 요구사항(id + 문장). 검증자가 scope creep을 판정할 잣대다."""
    ids = list(uc.get("requirement_ids", [])) + list(uc.get("nfr_ids", []))
    return [{"id": rid, "text": by_id[rid]["text"]} for rid in ids if rid in by_id]


def _accepted_step_subject_refs(uc: UseCaseItem) -> set[str]:
    refs = [uc.get("primary_actor_ref"), *(uc.get("supporting_actor_refs") or [])]
    return {ref for ref in refs if isinstance(ref, str) and ref}


def _check(
    item: UseCaseSpecItem,
    requirements: list[dict[str, object]] | None = None,
    goal_context: dict[str, object] | None = None,
    constraints: list[dict[str, object]] | None = None,
    review_call: SemanticReviewCall | None = None,
    allowed_subject_refs: set[str] | None = None,
    global_constraint_context: list[dict[str, object]] | None = None,
) -> tuple[list[str], str]:
    """정적(결정론) + 의미(LLM) 검증을 병합한 (issues, 의미검증 상태)."""
    static_findings = validate_specification(
        cast(dict[str, object], item), allowed_subject_refs
    )
    if static_findings:
        return static_findings, validator.PENDING
    findings, status = _semantic_findings(
        item, requirements, goal_context, constraints, review_call,
        global_constraint_context,
    )
    return findings, status


def _issue_keys(issues: list[str]) -> set[str]:
    """Return stable keys for deterministic findings and semantic rule verdicts."""
    keys: set[str] = set()
    known_rule_ids = rules.known_ids()
    for issue in issues:
        rule_id = next(
            (
                match.group(1)
                for match in reversed(list(_RULE_TAG.finditer(issue)))
                if match.group(1) in known_rule_ids
            ),
            None,
        )
        if rule_id is None:
            keys.add(f"raw:{issue}")
        else:
            finding = " ".join(issue.rsplit("[", 1)[0].split()).casefold()
            origin = "semantic" if issue.startswith("[semantic]") else "deterministic"
            keys.add(f"{origin}:{rule_id}:{finding}")
    return keys


def generate_specification(
    uc: UseCaseItem,
    by_id: dict[str, RequirementItem],
    actors: list[ActorItem],
    feedback: str = "",
    *,
    proposal_call: StructuredProposalCall | None = None,
    review_call: SemanticReviewCall | None = None,
) -> UseCaseSpecItem:
    """명세를 생성하고, 검증 실패 시 지시를 붙여 재생성하는 반성 루프(스레드에서 호출).

    각 반복은 결정론 static + LLM semantic 검증을 병합해 issues를 만들고, 남으면 그 issues를
    지시로 붙여 재생성한다. feedback이 있으면 최초 생성에
    사용자 지시를 반영한다.

    **채택 규칙은 검증 단계가 전진했거나 결함이 줄었는가다.** 정적 무결성 결함을
    해소하면 그때까지 실행하지 않았던 의미 검증이 처음으로 드러날 수 있다. 두 결함의
    개수가 같더라도 이는 회귀가 아니라 정적 계약을 통과한 진전이므로 다음 수리 기회를
    준다. 같은 검증 단계 안에서 결함 수가 줄지 않으면 직전본을 최선으로 보고 멈춘다.

    멈춘 이유는 `repair_stopped`에 남긴다. 수술적(부분) 수정으로 바꿀 값어치가 있는지는
    이 값의 분포를 봐야 알 수 있고, 지금은 그 근거가 없다.
    숫자 예산은 없다. 같은 입력·finding에는 아직 쓰지 않은 전략만 선택하고, 모든 전략이
    진전을 만들지 못하면 ``stalled``로 명시적으로 멈춘다. 채택된 후보로 입력 digest가
    바뀌면 전략 집합을 다시 사용할 수 있으므로 100회 이상 진전하는 수리도 잘리지 않는다.
    """
    specification_input = cast(_SpecificationInput, uc)
    base_user = _spec_human(specification_input, by_id, actors, feedback)
    # 검증자에게 줄 잣대. 생성 프롬프트와 달리 **요구사항만** 담는다(지시는 담지 않는다).
    requirements = requirement_view(uc, by_id)
    constraints = list(specification_input.get("_constraint_requirements") or [])
    global_constraint_context = list(
        specification_input.get("_global_constraint_context") or []
    )
    goal_context: dict[str, object] = {
        "use_case_goal": uc.get("goal", ""),
        "neighbouring_goals_sharing_requirements": (
            specification_input.get("_neighboring_goals") or []
        ),
    }
    propose = proposal_call or invoke_structured
    reviewer = review_call or validator.review

    def _generate(messages) -> UseCaseSpecItem:
        spec: UseCaseSpec = propose(UseCaseSpec, messages)
        item = normalize_specification(spec, uc)
        item["issues"], item["semantic_status"] = _check(
            item,
            requirements,
            goal_context,
            constraints,
            review_call=reviewer,
            allowed_subject_refs=_accepted_step_subject_refs(uc),
            global_constraint_context=global_constraint_context,
        )
        return item

    # 명세 하나마다 **한 번** 조립한다(재생성에도 같은 것을 쓴다). 프롬프트가 재생성마다
    # 달라지면 반성 루프가 무엇을 고쳤는지 알 수 없다.
    system = prompts.generation_system_for(rules.WRITE_SPECIFICATIONS)

    item = _generate([SystemMessage(content=system), HumanMessage(content=base_user)])

    unresolved_keys = _issue_keys(item["issues"])
    ledger = RepairLedger()
    attempts = 0
    stopped = "stalled"
    while item["issues"]:
        previous_spec = {
            key: item.get(key)
            for key in (
                "preconditions", "trigger", "main_scenario", "extensions",
                "success_guarantee", "minimal_guarantee",
                "public_contract",
            )
        }
        previous_spec["public_contract"] = _accepted_public_contract_proposal(
            item.get("public_contract") or {}
        )
        input_digest = stable_digest(
            {"specification": previous_spec, "findings": sorted(unresolved_keys)}
        )
        finding_keys_before = tuple(sorted(unresolved_keys))
        strategy = next(
            (
                candidate_strategy
                for candidate_strategy in _SPEC_REPAIR_STRATEGIES
                if not ledger.strategy_attempted(
                    input_digest=input_digest,
                    finding_keys=finding_keys_before,
                    strategy_key=candidate_strategy,
                )
            ),
            None,
        )
        if strategy is None:
            ledger.status = "STALLED"
            ledger.stall_reason = "No untried repair strategy remains for this specification state."
            stopped = "stalled"
            break
        repair_user = prompts.spec_repair_user(
            base_user,
            json.dumps(previous_spec, ensure_ascii=False, indent=2),
            item["issues"],
            strategy=strategy,
            repair_history=ledger.prompt_context_for_state(
                input_digest=input_digest,
                finding_keys=finding_keys_before,
            ),
        )
        attempts += 1
        try:
            candidate = _generate(
                [SystemMessage(content=system), HumanMessage(content=repair_user)]
            )
        except Exception as exc:  # noqa: BLE001 - 재생성 실패 시 직전본 유지
            # 수리를 못 했으므로 이 명세에는 검증이 지적한 결함이 그대로 남아 있다.
            telemetry.record_degradation(
                "spec.repair", f"{type(exc).__name__}: {exc}", subject=uc["id"]
            )
            waiting = transient_llm_error(exc)
            ledger.record(
                RepairAttempt(
                    stage="requirements.specifications",
                    target_ids=(str(uc["id"]),),
                    strategy_key=strategy,
                    input_digest=input_digest,
                    finding_keys_before=finding_keys_before,
                    finding_keys_after=finding_keys_before,
                    outcome="waiting_external" if waiting else "error",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            ledger.status = "WAITING_EXTERNAL" if waiting else "STALLED"
            ledger.stall_reason = "External LLM is unavailable." if waiting else str(exc)
            stopped = "waiting_external" if waiting else "error"
            break
        candidate_keys = _issue_keys(candidate["issues"])
        candidate_spec = {
            key: candidate.get(key)
            for key in (
                "preconditions", "trigger", "main_scenario", "extensions",
                "success_guarantee", "minimal_guarantee",
                "public_contract",
            )
        }
        candidate_digest = stable_digest(candidate_spec)
        allowed_subject_refs = _accepted_step_subject_refs(uc)
        current_static = validate_specification(
            cast(dict[str, object], item), allowed_subject_refs
        )
        candidate_static = validate_specification(
            cast(dict[str, object], candidate), allowed_subject_refs
        )
        advanced_to_semantic_review = bool(current_static) and not candidate_static
        repeated = ledger.candidate_seen(
            input_digest=input_digest,
            candidate_digest=candidate_digest,
        )
        improved = not repeated and repair_makes_progress(
            tuple(unresolved_keys),
            tuple(candidate_keys),
            frontier_before=0 if current_static else 1,
            frontier_after=0 if candidate_static else 1,
        )
        outcome = (
            "repeated_candidate"
            if repeated
            else "clean"
            if not candidate_keys
            else "improved"
            if improved
            else "regressed"
            if len(candidate_keys) > len(unresolved_keys)
            else "no_improvement"
        )
        ledger.record(
            RepairAttempt(
                stage="requirements.specifications",
                target_ids=(str(uc["id"]),),
                strategy_key=strategy,
                input_digest=input_digest,
                candidate_digest=candidate_digest,
                finding_keys_before=finding_keys_before,
                finding_keys_after=tuple(sorted(candidate_keys)),
                outcome=outcome,
                detail=(
                    "Static validation cleared and semantic validation became reachable."
                    if advanced_to_semantic_review
                    else ""
                ),
            )
        )
        if not improved:
            continue
        item = candidate
        unresolved_keys = candidate_keys

    if not item["issues"]:
        stopped = "clean"
        ledger.status = "COMPLETED"

    # 채택 횟수가 아니라 **시도 횟수**다. 채택 수를 세면 헛돈 재생성이 기록에서
    # 사라져서, 반성 루프가 비용을 얼마나 쓰는지 알 수 없게 된다.
    item["repair_iters"] = attempts
    item["repair_stopped"] = stopped
    item["repair_history"] = ledger.model_dump(mode="json")
    return item


def _tracked_spec_for(
    uc: _SpecificationInput,
    by_id: dict[str, RequirementItem],
    actors: list[ActorItem],
    feedback: str = "",
    proposal_call: StructuredProposalCall | None = None,
    review_call: SemanticReviewCall | None = None,
) -> UseCaseSpecItem:
    """Generate one specification while exposing only its live task boundary."""
    fields = {"useCaseId": uc["id"], "useCaseName": uc.get("name", "")}
    telemetry.emit_progress("specTaskStarted", **fields)
    status = "completed"
    try:
        return generate_specification(
            uc,
            by_id,
            actors,
            feedback,
            proposal_call=proposal_call,
            review_call=review_call,
        )
    except BaseException:
        status = "failed"
        raise
    finally:
        telemetry.emit_progress("specTaskFinished", status=status, **fields)


def _failed_spec(uc: UseCaseItem, exc: BaseException) -> UseCaseSpecItem:
    """생성이 끝내 실패한 UC 자리를 채우는 빈 명세.

    이 UC를 목록에서 빼 버리면 산출물에서 조용히 사라진다 — 형제 명세가 멀쩡한 실행과
    구별되지 않는다. 자리는 남기되 "만들지 못했다"고 적어, 리포트와 저장된 아티팩트
    양쪽에서 보이게 한다.
    """
    return {
        "use_case_id": uc["id"],
        "name": uc.get("name", ""),
        "requirement_ids": list(uc["requirement_ids"]),
        "nfr_ids": list(uc["nfr_ids"]),
        "preconditions": [],
        "trigger": "",
        "main_scenario": [],
        "extensions": [],
        "success_guarantee": [],
        "minimal_guarantee": [],
        "issues": [f"[generation] Could not generate the specification: {type(exc).__name__}: {exc}"],
        "repair_iters": 0,
        "semantic_status": validator.FAILED,
        "repair_stopped": "not_generated",
        "generated": False,
    }


@contract("generate_specs", requires=("use_cases", "classified"),
          produces=("use_case_specs",))
def generate_specs(
    state: AgentState,
    feedback: str = "",
    target_ids: list[str] | None = None,
    *,
    proposal_call: StructuredProposalCall | None = None,
    review_call: SemanticReviewCall | None = None,
) -> ModelingStagePatch:
    """모든 유스케이스의 명세를 UC별 병렬로 생성한다(입력 순서로 취합).

    feedback: 재생성 지시(대상 UC 생성에 반영).
    target_ids: 주어지면 그 UC만 재생성하고 나머지는 기존 use_case_specs를 그대로 둔다(local 피드백).
    """
    feedback = feedback_for(dict(state), "specs", feedback)
    use_cases = state.get("use_cases") or []
    if not use_cases:
        return {"use_case_specs": [], "phase": "specs"}

    classified = state.get("classified") or []
    by_id: dict[str, RequirementItem] = {r["id"]: r for r in classified}
    actors = state.get("actors") or []

    existing = {s["use_case_id"]: s for s in (state.get("use_case_specs") or [])}
    target_set = set(target_ids) if target_ids else None
    to_gen: list[_SpecificationInput] = []
    for use_case in use_cases:
        if target_set is not None and use_case["id"] not in target_set:
            continue
        requirement_ids = set(use_case.get("requirement_ids") or [])
        neighbouring_goals = [
            {
                "id": other["id"],
                "name": other.get("name", ""),
                "goal": other.get("goal", ""),
            }
            for other in use_cases
            if other["id"] != use_case["id"]
            and requirement_ids.intersection(other.get("requirement_ids") or [])
        ]
        direct_ids = {
            str(value)
            for key in ("requirement_ids", "nfr_ids")
            for value in cast(list[str], use_case.get(key) or [])
        }
        applicable_constraints = [
            item
            for item in constraints_for_use_case(
                state.get("traceability") or {}, str(use_case["id"])
            )
            if str(item.get("id") or "") not in direct_ids
        ]
        spec_input = cast(_SpecificationInput, {
            **use_case,
            "_neighboring_goals": neighbouring_goals,
            "_constraint_requirements": applicable_constraints,
            "_global_constraint_context": modeled_global_constraints(
                state.get("traceability") or {}
            ),
        })
        if target_set is not None and existing.get(use_case["id"]):
            spec_input["_existing_spec"] = existing[use_case["id"]]
        to_gen.append(spec_input)

    workers = max(1, min(len(to_gen), settings.spec_concurrency)) if to_gen else 1
    results: dict[str, UseCaseSpecItem] = {}
    if to_gen:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # bind_context로 감싸야 워커 스레드가 같은 실행에 계측을 집계한다 —
            # ThreadPoolExecutor는 contextvars를 복사해 주지 않는다. submit 마다 새로
            # 감싼다(Context 하나는 한 번만 실행할 수 있다).
            futures = {
                pool.submit(
                    telemetry.bind_context(_tracked_spec_for),
                    uc,
                    by_id,
                    actors,
                    feedback,
                    proposal_call,
                    review_call,
                ): uc["id"]
                for uc in to_gen
            }
            by_id_uc = {uc["id"]: uc for uc in to_gen}
            for fut in as_completed(futures):
                uc_id = futures[fut]
                try:
                    results[uc_id] = fut.result()
                except Exception as exc:  # noqa: BLE001 - 형제를 살리려 여기서 흡수
                    # 예전에는 여기서 예외가 올라가 노드 전체가 실패했다. UC 10개 중
                    # 9개가 이미 끝났어도 그 9개까지 함께 버려졌다.
                    telemetry.record_degradation(
                        "spec.generate", f"{type(exc).__name__}: {exc}", subject=uc_id
                    )
                    results[uc_id] = _failed_spec(by_id_uc[uc_id], exc)

    # use_cases 입력 순서 유지: 재생성분 우선, 아니면 기존 spec 유지(local 피드백 시 형제 보존).
    specs = [results.get(uc["id"]) or existing.get(uc["id"]) for uc in use_cases]
    specs = [s for s in specs if s is not None]
    specs = apply_identity_source_overrides(
        specs, state.get("identity_source_overrides")
    )
    return {"use_case_specs": specs, "phase": "specs"}


@contract("check_specs", requires=("use_case_specs",), produces=("spec_report", "use_case_specs"))
def check_specs(
    state: AgentState, *, review_semantic: bool = True,
    allowed_producer_ids: set[str] | None = None,
) -> ModelingStagePatch:
    """교차 UC 식별자 결과를 정리한 뒤 생성 명세의 검증 결과를 집계한다.

    generate_specs가 UC별 정적+의미 검증·수리를 마친 후, 전체 UC의 생산·소비 관계가
    필요한 경우에만 별도 모델 검토로 결과 식별자를 보완한다. 이후 잔여 issues와 repair
    횟수를 집계해 표면화한다.
    """
    specs = state.get("use_case_specs") or []
    if (
        review_semantic and len(specs) > 1
        and state.get("use_cases") and state.get("classified")
    ):
        specs = reconcile_cross_use_case_values(
            specs, state["use_cases"], state["classified"],
            allowed_producer_ids=allowed_producer_ids,
        )
    reviewed_state = cast(AgentState, {**state, "use_case_specs": specs})
    report = {
        "n_specs": len(specs),
        "total_issues": sum(len(s.get("issues", [])) for s in specs),
        "issues_by_uc": {s["use_case_id"]: s["issues"] for s in specs if s.get("issues")},
        "total_repair_iters": sum(s.get("repair_iters", 0) for s in specs),
        # 의미 검증을 못 거친 명세. issues가 비었다는 것과 "확인했는데 깨끗하다"는 것을
        # 리포트에서 구별할 수 있어야 한다 — 이 목록이 비어 있지 않으면 total_issues는
        # 하한일 뿐이다.
        # 원인이 달라도 결과는 같다 — 이 명세는 의미 검증을 **거치지 못했다.**
        # 어느 상태가 그에 해당하는지는 validator가 정한다(같은 목록을 두 번 적지 않는다).
        "unvalidated_ucs": [
            s["use_case_id"] for s in specs
            if s.get("semantic_status") in validator.UNVALIDATED
        ],
        # 생성 자체가 실패해 빈 자리로 남은 UC. 형제는 살아 있으므로 실행은 계속되지만
        # 이 UC의 명세는 없다 — 있는 척하지 않는다.
        "failed_ucs": [
            s["use_case_id"] for s in specs if s.get("generated") is False
        ],
        # 반성 루프가 왜 멈췄는지의 분포. "no_improvement"가 많으면 재생성이 헛돌고
        # 있다는 뜻이라, 전체 재생성 대신 부분 수정으로 바꿀 근거가 된다.
        "repair_stopped": dict(
            Counter(s.get("repair_stopped", "unknown") for s in specs)
        ),
    }
    # This runs in the stage subgraph before its parent feedback gate.  Persist
    # the selective-review result in graph state so an interrupt resume does not
    # issue the same LLM call again merely to redisplay the same question.
    return {
        "use_case_specs": specs,
        "spec_report": report,
        "semantic_ambiguity_question": (
            find_source_grounded_semantic_ambiguity(reviewed_state)
            if review_semantic else state.get("semantic_ambiguity_question")
        ),
        "identity_source_question": identity_source_question(reviewed_state),
        "phase": "check_specs",
    }


def _identity_source_options(spec: dict[str, object], obligation: dict[str, object]) -> list[dict[str, str]]:
    """Build finite options from this UC's minted authenticate refs, never labels."""
    contract = spec.get("public_contract")
    entries = contract.get("identity_obligations", []) if isinstance(contract, dict) else []
    authenticate_refs = sorted({
        str(item.get("obligation_ref")) for item in entries
        if isinstance(item, dict) and item.get("obligation") == "authenticate"
        and isinstance(item.get("obligation_ref"), str)
    })
    options = [
        {
            "id": "caller_input", "label": "Caller supplied input",
            "description": "The caller supplies the identifier; it is not proof of identity.",
            "identitySourceKind": "caller_input",
        },
        {
            "id": "system_result", "label": "System result",
            "description": "The system determines or returns the identity as part of this use case.",
            "identitySourceKind": "system_result",
        },
    ]
    options.extend({
        "id": f"authenticated_context:{ref}",
        "label": "Authenticated context",
        "description": "Use the subject established by this authenticate obligation.",
        "identitySourceKind": "authenticated_context",
        "sourceAuthenticateObligationRef": ref,
    } for ref in authenticate_refs)
    return options


def identity_source_question(state: AgentState) -> dict[str, object] | None:
    """Return the next unresolved identify source choice, in stable spec order."""
    for spec in state.get("use_case_specs") or []:
        if not isinstance(spec, dict):
            continue
        contract = spec.get("public_contract")
        obligations = contract.get("identity_obligations", []) if isinstance(contract, dict) else []
        for item in obligations:
            if (not isinstance(item, dict) or item.get("obligation") != "identify"
                    or item.get("identity_source_kind") != "unresolved"):
                continue
            options = _identity_source_options(spec, item)
            use_case_id = str(spec.get("use_case_id") or "")
            use_case_name = str(spec.get("name") or "")
            identity_subject = str(item.get("subject") or "")
            return {
                "useCaseId": use_case_id,
                "useCaseName": use_case_name,
                "obligationRef": str(item.get("obligation_ref") or ""),
                "identitySubject": identity_subject,
                "requirementIds": list(item.get("requirement_ids") or []),
                # Names are display context only. The saved opaque obligation refs
                # and finite options remain the sole answer/matching authority.
                "prompt": (
                    f"For {use_case_id} — {use_case_name}, where does the identity "
                    f"for {identity_subject} come from?"
                ),
                "options": options,
            }
    return None


def apply_identity_source_overrides(
    specs: list[UseCaseSpecItem], overrides: dict[str, dict[str, str]] | None,
) -> list[UseCaseSpecItem]:
    """Apply accepted typed choices after normalization, without another model pass."""
    if not overrides:
        return specs
    for spec in specs:
        contract = spec.get("public_contract")
        obligations = contract.get("identity_obligations", []) if isinstance(contract, dict) else []
        authenticate_refs = {
            item.get("obligation_ref") for item in obligations
            if isinstance(item, dict) and item.get("obligation") == "authenticate"
            and isinstance(item.get("obligation_ref"), str)
        }
        for item in obligations:
            if not isinstance(item, dict) or item.get("obligation") != "identify":
                continue
            override = overrides.get(str(item.get("obligation_ref") or ""))
            if not override:
                continue
            item["identity_source_kind"] = override["identity_source_kind"]
            item.pop("source_authenticate_obligation_ref", None)
            if override["identity_source_kind"] == "authenticated_context":
                source_ref = override.get("source_authenticate_obligation_ref")
                if isinstance(source_ref, str) and source_ref in authenticate_refs:
                    item["source_authenticate_obligation_ref"] = source_ref
                else:
                    # Regeneration may remove or replace the authenticate
                    # obligation. Reopen the choice instead of retaining a
                    # stale or cross-use-case authenticated-context answer.
                    item["identity_source_kind"] = "unresolved"
    return specs


def find_source_grounded_semantic_ambiguity(
    state: AgentState,
    *,
    proposal_call: StructuredProposalCall | None = None,
) -> dict[str, object] | None:
    """Return one genuine UC-level product choice, or abstain.

    Deterministic defects are already represented by ``issues`` and are repaired
    locally before this review runs.  This selective call therefore receives only
    clean specifications and their own linked requirement text; it cannot choose
    an actor, UC, or source outside that bounded evidence.
    """
    if state.get("semantic_ambiguity_questioned"):
        return None
    specs = state.get("use_case_specs") or []
    requirements = {str(item.get("id")): item for item in state.get("classified") or []
                    if isinstance(item, dict) and item.get("id")}
    candidates: list[dict[str, object]] = []
    for spec in specs:
        if not isinstance(spec, dict) or spec.get("issues") or spec.get("generated") is False:
            continue
        uc_id = str(spec.get("use_case_id") or "").strip()
        linked = [str(value) for value in spec.get("requirement_ids") or []]
        source = [
            {"id": requirement_id, "text": requirements[requirement_id].get("text", "")}
            for requirement_id in linked if requirement_id in requirements
        ]
        if uc_id and source:
            candidates.append({
                "useCaseId": uc_id,
                "requirements": source,
                "specification": spec_review_payload(spec, source),
            })
    if not candidates:
        return None
    prompt = (
        "Review these use-case contracts for exactly one unresolved product-behavior "
        "choice. Abstain unless the supplied requirement wording supports two materially "
        "different public behaviors and neither behavior is selected. Do not report missing "
        "fields, invalid references, implementation details, or a repairable defect. If you "
        "ask, cite exact source substrings, target one supplied useCaseId, and provide exactly "
        "two concise options whose requestedEffect revises only that use-case specification."
    )
    try:
        review = (proposal_call or invoke_structured)(
            SemanticAmbiguityReview,
            [SystemMessage(content=prompt), HumanMessage(content=json.dumps(candidates, ensure_ascii=False))],
        )
    except Exception as error:  # A question is optional; an unavailable reviewer must not block handoff.
        telemetry.record_degradation("spec.semantic_ambiguity", f"{type(error).__name__}: {error}")
        return None
    question = review.question
    if question is None:
        return None
    by_uc = {str(item["useCaseId"]): item for item in candidates}
    candidate = by_uc.get(question.use_case_id)
    if candidate is None:
        return None
    source = candidate["requirements"]
    source_by_id = {str(item["id"]): str(item["text"]) for item in source if isinstance(item, dict)}
    if (set(question.source_requirement_ids) - set(source_by_id)
            or len(set(question.source_requirement_ids)) != len(question.source_requirement_ids)):
        return None
    evidence = "\n".join(source_by_id[item] for item in question.source_requirement_ids)
    if any(span not in evidence for span in question.evidence_spans):
        return None
    if len({option.id for option in question.options}) != 2:
        return None
    return question.model_dump(mode="json", by_alias=True)
