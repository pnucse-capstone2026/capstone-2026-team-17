"""분류된 소프트웨어 요구사항에서 일반 배포 capability를 도출한다.

자연어 해석은 구조화 LLM 호출이 맡고, 결정론 코드는 결과 envelope과 요구사항 근거를
검증한다. 이 단계는 배포가 제공해야 할 능력만 기술하며 구체 리소스 선택은 설계에 맡긴다.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable

from langchain_core.messages import HumanMessage, SystemMessage

from app.requirements.common.state_contract import contract
from app.requirements.config import settings
from app.requirements.contracts.state import AgentState
from app.requirements.resources.capability_contract import (
    OUT_OF_SCOPE_DEPENDENCY_CAPABILITY_IDS,
    decide,
    link_dependency_capability,
    load_policy,
)
from app.requirements.runtime import telemetry
from app.requirements.runtime.structured_llm import invoke_structured
from app.requirements.schemas import CapabilityContract, DeploymentNeed, DeploymentNeedsResult

_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_DEPLOYMENT_BOUNDARY = re.compile(
    r"\b(?:deploy(?:ment)?|infrastructure|server|virtual machine|vm|instance|"
    r"region|zone|availability|failover|replica|restart|ingress|egress|network|"
    r"https|tls|certificate|domain|container|runtime|environment variable|"
    r"secret distribution|credential distribution|isolation|encrypt(?:ed|ion)? at rest|"
    r"encrypt(?:ed|ion)? in transit|latency|response time|throughput|requests? per second|"
    r"concurrent (?:users|sessions|requests)|cpu|memory|storage|disk|persist(?:ent|ence)?|"
    r"durable|backup|observability|logging|metrics?|tracing)\b",
    re.IGNORECASE,
)
_SYSTEM = """Derive deployment needs from the supplied software requirements.
Return a generic deploymentNeeds dictionary. Each dictionary key is a concise,
snake_case capability identifier chosen for this application. Keep product, language,
framework, protocol, version, and endpoint names out of the key; put grounded details in
metadata instead (for example, use `container_runtime`, not `java21_container_runtime`).
Each value contains:
- role: what capability the deployment must provide and why
- required: true when the requirements mandate it; false for a supported preference
- requirementIds: one or more exact requirement IDs from the input
- metadata: free-form structured details, including an `unresolved` string array when useful
- evidenceSpans: one or more exact substrings from the referenced requirement text
- origin: explicit when the capability is directly stated, otherwise inferred
- dependencyCapabilityIds: zero or more stable IDs selected only from this registry:
  `persistent-block-storage` for application data that must survive VM replacement;
  `load-balanced-ingress` for explicitly requested load-balanced ingress;
  `https-ingress` for explicitly requested HTTPS or TLS ingress, which is recognized so
  the system can report that it is outside the current generation scope;
  `https-load-balanced-ingress` only when both load balancing and HTTPS termination are
  explicitly requested and is likewise outside the current generation scope. Use an empty
  list when none matches. General HTTPS does not imply a load balancer, and an HTTPS need
  must never be lowered to HTTP.

Do not select or name concrete cloud resources, CSP products, VM counts, instance types,
load balancers, disks, Kubernetes objects, or implementation products. For example,
derive `durable storage across restarts`, not `AWS EBS`; derive `survive one instance
failure`, not `two VMs and a load balancer`. Do not invent needs without supporting
requirement IDs. Do not repeat application behavior such as CRUD, business functions,
API endpoints, programming languages, or frameworks as deployment needs. Include only
properties that change the deployment boundary, such as ingress, persistence, availability,
capacity, performance, security, runtime configuration, or observability. Metadata must
contain only constraints explicitly stated in the referenced requirements. Never introduce
defaults such as protocol versions, consistency levels, backup policies, replication, domain
names, or certificate authorities. The `unresolved` list is only for a missing value clearly
required to interpret an expressed constraint; it is not a generic deployment checklist.
Authentication, authorization, validation, atomic transactions, concurrency handling, and
business invariants are application behavior by default, not deployment capabilities. Include
one only when the requirement explicitly changes the deployment boundary by naming an external
dependency or a deploy-time concern such as credential or secret distribution, network access,
runtime isolation, or infrastructure-managed protection. A security or reliability implication
alone is not enough.
When a requirement explicitly constrains application state, metadata may contain an
`applicationState` object. Its optional keys are `durability`, `accessScope`, and `accessPath`.
Use `accessScope: node-filesystem` only when the text explicitly says local, node, or VM
filesystem state; use `shared-service` only when it explicitly requires shared or external
state. Omit every unknown key rather than inferring it. This object describes application
intent and does not select a storage product.
When a requirement explicitly names deployment locations, metadata may contain
`placementScope: singleZone | multiZone | multiRegion`. Preserve the stated placement
without translating it into a high-availability or failover claim. Multi-region is outside
the current deployment scope, so preserve it as `placementScope: multiRegion` rather than
silently lowering it to multi-zone.
Use an empty metadata object when there are no grounded details. Merge equivalent needs and
keep distinct roles separate.

A statement that high availability is not required is a relaxed availability constraint,
not a mandate for one instance or no replication. Represent it generically (for example,
`availability_requirement` with `high_availability: false`) and do not infer topology,
instance count, failover, or replication from it. More generally, an allowed simplification
is not a required implementation choice."""

CapabilityStagePatch = dict[str, object]
CapabilityProposalCall = Callable[
    [list[dict[str, object]], int], DeploymentNeedsResult
]


def deployment_need_messages(
    requirements: list[dict[str, object]],
) -> list[SystemMessage | HumanMessage]:
    """Capability proposal의 기존 메시지 envelope을 조립한다.

    Args:
        requirements: id·text·type만 포함한 분류 요구사항 목록이다.

    Returns:
        기존 system prompt와 JSON human message를 순서대로 담은 목록이다.

    Notes:
        prompt와 operation 계약을 한곳에 두기 위한 adapter이며 LLM을 호출하지 않는다.
    """
    return [
        SystemMessage(content=_SYSTEM),
        HumanMessage(content=json.dumps(requirements, ensure_ascii=False)),
    ]


def propose_deployment_needs(
    requirements: list[dict[str, object]],
    seed: int,
    *,
    proposal_call: CapabilityProposalCall | None = None,
) -> DeploymentNeedsResult:
    """한 표본의 generic deployment capability proposal을 만든다.

    Args:
        requirements: id·text·type만 포함한 분류 요구사항 목록이다.
        seed: 반복 표본마다 고정되는 구조화 호출 seed다.
        proposal_call: 테스트나 상위 adapter가 주입하는 동일 계약의 호출 함수다.

    Returns:
        아직 근거 검증·보정 임계값 판정을 거치지 않은 typed proposal이다.

    Notes:
        주입 함수가 없을 때 기존 ``DeploymentNeedsResult`` operation과 메시지 모양으로
        구조화 LLM을 정확히 한 번 호출한다.
    """
    if proposal_call is not None:
        return proposal_call(requirements, seed)
    return invoke_structured(
        DeploymentNeedsResult,
        deployment_need_messages(requirements),
        seed_override=seed,
        strict=False,
    )


def _normalized_span(value: str) -> str:
    return " ".join(value.casefold().split()).strip(" .")


def _same_evidence(left: DeploymentNeed, right: DeploymentNeed) -> bool:
    """문자열 키가 아니라 요구사항과 원문 근거의 포함 관계로 동의 표본을 묶는다."""
    left_ids = set(left.requirement_ids)
    right_ids = set(right.requirement_ids)
    if left_ids != right_ids:
        return False
    left_spans = [_normalized_span(item) for item in left.evidence_spans if item.strip()]
    right_spans = [_normalized_span(item) for item in right.evidence_spans if item.strip()]
    return bool(left_spans and right_spans) and any(
        first in second or second in first
        for first in left_spans for second in right_spans
    )


def _has_deployment_boundary_evidence(
    evidence_spans: list[str],
    dependency_capability_ids: list[str],
) -> bool:
    """Accept only needs whose source text can affect deploy-time structure or binding."""
    if dependency_capability_ids:
        return True
    return bool(_DEPLOYMENT_BOUNDARY.search(" ".join(evidence_spans)))


def _ground_application_state_metadata(
    metadata: dict, evidence_spans: list[str]
) -> tuple[dict, list[dict[str, str]]]:
    """소비되는 상태 축의 값이 evidence span에 직접 드러난 경우만 남긴다."""
    projected = dict(metadata)
    raw_state = metadata.get("applicationState") or metadata.get("application_state")
    if not isinstance(raw_state, dict):
        return projected, []
    text = " ".join(evidence_spans).casefold()
    state: dict[str, str] = {}
    rejected: list[dict[str, str]] = []

    raw_durability = str(raw_state.get("durability") or "").strip().casefold()
    durability = {
        "durable": "persistent",
        "retained": "persistent",
        "temporary": "ephemeral",
        "volatile": "ephemeral",
    }.get(raw_durability, raw_durability)
    durability_evidence = {
        "persistent": (
            "persist",
            "durable",
            "survive",
            "retain",
            "not be lost",
            "without data loss",
        ),
        "ephemeral": ("ephemeral", "temporary", "volatile"),
    }
    if durability:
        if durability in durability_evidence and any(
            marker in text for marker in durability_evidence[durability]
        ):
            state["durability"] = durability
        else:
            rejected.append({
                "path": "applicationState.durability",
                "value": raw_durability,
                "reason": "value-not-grounded-in-evidence-span",
            })

    access_scope = str(raw_state.get("accessScope") or "")
    scope_evidence = {
        "node-filesystem": (
            "vm filesystem",
            "node filesystem",
            "local filesystem",
            "local disk",
            "on the vm",
        ),
        "shared-service": (
            "shared state",
            "shared storage",
            "shared location",
            "all application instances",
            "external state",
            "external storage",
            "external database",
        ),
    }
    if access_scope:
        if access_scope in scope_evidence and any(
            marker in text for marker in scope_evidence[access_scope]
        ):
            state["accessScope"] = access_scope
        else:
            rejected.append({
                "path": "applicationState.accessScope",
                "value": access_scope,
                "reason": "value-not-grounded-in-evidence-span",
            })

    access_path = str(raw_state.get("accessPath") or "")
    if access_path:
        if access_path.casefold() in text:
            state["accessPath"] = access_path
        else:
            rejected.append({
                "path": "applicationState.accessPath",
                "value": access_path,
                "reason": "value-not-grounded-in-evidence-span",
            })

    projected.pop("application_state", None)
    if state:
        projected["applicationState"] = state
    else:
        projected.pop("applicationState", None)
    return projected, rejected


@contract(
    "derive_deployment_needs",
    requires=("classified",),
    produces=("deployment_needs", "capability_contract"),
)
def derive_deployment_needs(
    state: AgentState,
    *,
    sample_count: int | None = None,
    proposal_call: CapabilityProposalCall | None = None,
) -> CapabilityStagePatch:
    """요구사항 근거로 검증한 generic deployment capability 계약을 만든다.

    Args:
        state: 분류된 요구사항을 포함한 단계 상태다.
        sample_count: 설정값 대신 사용할 반복 proposal 표본 수다.
        proposal_call: 표본 호출을 대체하는 공개 주입 함수다.

    Returns:
        기존 ``deployment_needs``와 alias JSON ``capability_contract`` state patch다.

    Notes:
        표본 수, seed 증가, degradation 기록, 일치도 보정과 선택적 수락 규칙을 그대로
        유지한다. proposal은 근거·경계 검증을 통과하기 전까지 수락하지 않는다.
    """
    classified = list(state.get("classified") or [])
    if not classified:
        return {
            "deployment_needs": {},
            "capability_contract": {
                "schemaVersion": "CapabilityContract/v1",
                "capabilities": [],
                "questions": [],
            },
            "phase": "deployment_needs",
        }
    known = {str(item.get("id")) for item in classified if item.get("id")}
    listing: list[dict[str, object]] = [
        {"id": item.get("id"), "text": item.get("text", ""), "type": item.get("type")}
        for item in classified
    ]
    samples: list[DeploymentNeedsResult] = []
    resolved_sample_count = max(
        1,
        int(settings.capability_samples if sample_count is None else sample_count),
    )
    for _sample in range(resolved_sample_count):
        try:
            samples.append(
                propose_deployment_needs(
                    listing,
                    (settings.seed or 0) + _sample,
                    proposal_call=proposal_call,
                )
            )
        except Exception as exc:  # noqa: BLE001 - absence must be visible, not fabricated
            telemetry.record_degradation(
                "deployment_needs.extraction", f"{type(exc).__name__}: {exc}"
            )
    if not samples:
        return {
            "deployment_needs": {},
            "capability_contract": {
                "schemaVersion": "CapabilityContract/v1",
                "capabilities": [],
                "questions": [],
            },
            "phase": "deployment_needs",
        }

    needs: dict[str, dict] = {}
    appearances: dict[str, int] = {}
    dependency_capability_appearances: dict[str, dict[str, int]] = {}
    representatives: dict[str, DeploymentNeed] = {}
    for result in samples:
        seen_clusters: set[str] = set()
        for key, need in result.deployment_needs.items():
            cluster_key = next(
                (
                    representative_key
                    for representative_key, representative in representatives.items()
                    if _same_evidence(representative, need)
                ),
                key,
            )
            representatives.setdefault(cluster_key, need)
            if cluster_key not in seen_clusters:
                appearances[cluster_key] = appearances.get(cluster_key, 0) + 1
                counts = dependency_capability_appearances.setdefault(cluster_key, {})
                linked_capability_id = link_dependency_capability(
                    key, need.role, need.evidence_spans
                )
                # A structured LLM field is a proposal, not evidence. Stable IDs enter
                # the downstream plan only when the deterministic linker can reproduce
                # the same ID from the key, role, and quoted evidence.
                observed_capability_ids = (
                    {linked_capability_id} if linked_capability_id else set()
                )
                for capability_id in observed_capability_ids:
                    counts[capability_id] = counts.get(capability_id, 0) + 1
                seen_clusters.add(cluster_key)
    policy = load_policy()
    capabilities: list[dict] = []
    questions: list[dict[str, str]] = []
    requirement_text = {
        str(item.get("id")): str(item.get("text") or "") for item in classified
    }
    for key, need in representatives.items():
        if not _KEY.fullmatch(key):
            telemetry.record_degradation("deployment_needs.invalid_key", key)
            continue
        requirement_ids = list(dict.fromkeys(
            requirement_id for requirement_id in need.requirement_ids
            if requirement_id in known
        ))
        if not requirement_ids:
            telemetry.record_degradation("deployment_needs.ungrounded", key)
            continue
        role = need.role.strip()
        if not role:
            telemetry.record_degradation("deployment_needs.empty_role", key)
            continue
        spans = list(dict.fromkeys(span.strip() for span in need.evidence_spans if span.strip()))
        evidence_valid = bool(spans) and all(
            any(span in requirement_text[requirement_id] for requirement_id in requirement_ids)
            for span in spans
        )
        unresolved = [
            str(item) for item in (need.metadata.get("unresolved") or [])
            if str(item).strip()
        ]
        dependency_capability_ids = sorted({
            capability_id
            for capability_id, count in dependency_capability_appearances.get(
                key, {}
            ).items()
            if count == appearances[key]
        })
        raw_confidence = appearances[key] / resolved_sample_count
        decision, reason, calibrated = decide(
            raw_score=raw_confidence,
            origin=need.origin,
            evidence_valid=evidence_valid,
            unresolved_fields=unresolved,
            out_of_scope=bool(
                set(dependency_capability_ids)
                & OUT_OF_SCOPE_DEPENDENCY_CAPABILITY_IDS
            ),
            policy=policy,
        )
        if decision != "abstained" and not _has_deployment_boundary_evidence(
            spans, dependency_capability_ids
        ):
            decision = "abstained"
            reason = "not-deployment-boundary"
        grounded_metadata, rejected_metadata = _ground_application_state_metadata(
            need.metadata, spans
        )
        needs[key] = {
            "role": role,
            "required": need.required,
            "requirementIds": requirement_ids,
            "metadata": grounded_metadata,
            "evidenceSpans": spans,
            "origin": need.origin,
            "dependencyCapabilityIds": dependency_capability_ids,
            "decision": decision,
        }
        if rejected_metadata:
            needs[key]["rejectedMetadata"] = rejected_metadata
        capability = {
            "id": key,
            "statement": role,
            "requirementIds": requirement_ids,
            "evidenceSpans": spans,
            "origin": need.origin,
            "necessity": "required" if need.required else "preferred",
            "decision": decision,
            "decisionReason": reason,
            "rawConfidence": raw_confidence,
            "calibratedConfidence": calibrated,
            "thresholdVersion": str(policy.get("version") or "unfitted"),
            "confirmation": "notRequired" if decision == "accepted" else "pending",
            "alternatives": [],
            "unresolvedFields": unresolved,
            "dependencyCapabilityIds": dependency_capability_ids,
        }
        capabilities.append(capability)
        if decision == "needsQuestion":
            question = f"Should the deployment provide this capability: {role}?"
            if (
                grounded_metadata.get("placementScope") == "multiZone"
                and "availability" in unresolved
            ):
                question = (
                    "Does multi-zone mean only placing independent VM replicas in different "
                    "zones, or must the service continue during a zone failure?"
                )
            questions.append({
                "capabilityId": key,
                "reason": reason,
                "question": question,
            })

    capability_contract = CapabilityContract.model_validate({
        "schemaVersion": "CapabilityContract/v1",
        "capabilities": capabilities,
        "questions": questions,
    }).model_dump(by_alias=True)
    return {
        "deployment_needs": needs,
        "capability_contract": capability_contract,
        "phase": "deployment_needs",
    }
