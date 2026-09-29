"""추적성 색인 — **누가 무엇을 커버한다고 주장하는가**를 한 곳에서 센다.

집계는 여기서만 한다. 파이프라인 커버리지와 저장되는 추적 스냅샷도 여기서 파생한다.
표나 문서를 렌더링하지 않고, 요구사항 ID가 어느 산출물에 연결되는지만 구조화해 제공한다.

**링크 종류를 뭉개지 않는다.** `requirement_ids`(실현 주장)와 UC 제약 링크는 뜻이
달라 따로 센다. 기존 `nfr_ids`와 요구사항 분석기가 만든 `constraint_applicability`는 모두
제약 링크이지만, FR/NFR 분류와 관계 의미를 동일시하지 않는다. 기능 요구사항도 불변조건이나
정책으로 기존 UC를 제약할 수 있기 때문이다. 환각 판정만 모든 링크를 합쳐서 본다 —
"이 id가 존재하는가"는 어느 산출물에 적혔든 같은 질문이라서다.

## 왜 `app/requirements`에 있나

추적성의 기준과 식별자는 요구사항 단계가 소유한다. 다른 단계는 이 공개 투영을 읽어
자기 산출물이 요구사항에 어떻게 닿는지 기록하되 요구사항 내부 상태를 import하지 않는다.

**구현 엔진의 `traceability-matrix.csv`와는 다른 것이다**(2026-07-28 정정). 그쪽은
`source_artifact → generated_file` 출처 기록이고 한 실행에 고정되며, 여기는 요구 id로
색인된 커버리지다. 이름만 닮았고 칸은 하나도 안 겹친다 — **합치면 양쪽이 망가진다.**

입력이 `dict`인 것도 그래서다. 특정 에이전트의 `AgentState`를 알면 그 에이전트의 것이
되므로, **읽는 키만 알고 상태 타입은 모른다**(`classified` · `use_cases` ·
`use_case_specs`).
"""
from __future__ import annotations

from dataclasses import dataclass, field


def constraints_for_use_case(requirement_trace: dict, use_case_id: str) -> list[dict]:
    """Project one RTM's constraint edges into one downstream design scope."""
    result: list[dict] = []
    for requirement_id, item in (requirement_trace.get("requirements") or {}).items():
        if use_case_id not in (item.get("constrains_use_cases") or []):
            continue
        result.append({
            "id": requirement_id,
            "type": item.get("type"),
            "text": item.get("text", ""),
        })
    return result


def modeled_global_constraints(requirement_trace: dict) -> list[dict]:
    """Return explicitly modeled constraints that have no use-case edge.

    These are context for downstream reasoning, not RTM links: callers must not
    treat them as requirements this particular use case necessarily satisfies.
    """
    result: list[dict] = []
    for requirement_id, item in (requirement_trace.get("requirements") or {}).items():
        if not item.get("modeled_as_constraint"):
            continue
        if item.get("constrains_use_cases") != []:
            continue
        result.append({
            "id": requirement_id,
            "type": item.get("type"),
            "text": item.get("text", ""),
        })
    return result


@dataclass(frozen=True)
class Traceability:
    """한 실행 상태의 추적 링크를 되읽기 좋게 뒤집어 둔 것."""

    #: 요구 id → 분류된 요구 레코드.
    by_id: dict[str, dict] = field(default_factory=dict)
    fr_ids: frozenset[str] = frozenset()
    nfr_ids: frozenset[str] = frozenset()
    use_case_ids: frozenset[str] = frozenset()
    #: FR/NFR 라벨과 무관하게 분석기가 정책·불변조건으로 판정한 요구 id.
    constraint_ids: frozenset[str] = frozenset()
    #: 요구 id → 그것을 **실현한다고 주장하는** UC id들(`requirement_ids`).
    ucs_claiming: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 요구 id → 그것을 **제약으로 붙인** UC id들(`nfr_ids`).
    ucs_constrained_by: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 요구 id → 그것에서 파생된 배포 필요사항 id들.
    deployment_needs_of: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 요구 id → 그것을 근거로 도출된 액터 역할들.
    actors_of: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 요구 id → 그것에서 파생된 capability id들.
    capabilities_of: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 요구 id → 그것을 커버한다고 적힌 명세 스텝들(`"UC1.3"`). UC보다 정밀한 추적.
    steps_of: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 요구 id → 그것을 실현한다고 적힌 보장(`"UC1.success.1"`). 실행 스텝과 분리한다.
    guarantees_of: dict[str, tuple[str, ...]] = field(default_factory=dict)

    # -- 되읽기 ------------------------------------------------------------
    def ucs_of(self, req_id: str) -> tuple[str, ...]:
        """어느 칸으로든 이 요구를 건 UC들."""
        merged = list(self.ucs_claiming.get(req_id, ()))
        merged += [u for u in self.ucs_constrained_by.get(req_id, ()) if u not in merged]
        return tuple(merged)

    @property
    def referenced_ids(self) -> frozenset[str]:
        """요구사항 산출물이 어느 관계로든 참조한 id 전부."""
        return frozenset().union(
            self.ucs_claiming,
            self.ucs_constrained_by,
            self.deployment_needs_of,
            self.actors_of,
            self.capabilities_of,
            self.constraint_ids,
        )

    @property
    def unknown_refs(self) -> tuple[str, ...]:
        """분류 목록에 **없는** id를 참조한 것 — 환각.

        `requirement_ids`·`nfr_ids`를 합쳐서 보고, 대조 대상은 FR 목록이 아니라 **알려진
        요구 전체**다. 둘 중 하나라도 빠뜨리면 오탐이나 미탐이 생긴다(모듈 docstring).
        """
        return tuple(sorted(self.referenced_ids - frozenset(self.by_id)))

    @property
    def unknown_use_case_refs(self) -> tuple[str, ...]:
        """RTM 간선이 가리키지만 현재 UC 집합에는 없는 id."""
        referenced = {
            use_case_id
            for mapping in (self.ucs_claiming, self.ucs_constrained_by)
            for use_case_ids in mapping.values()
            for use_case_id in use_case_ids
        }
        return tuple(sorted(referenced - self.use_case_ids))

    @property
    def covered_fr_ids(self) -> tuple[str, ...]:
        """어떤 UC가 실현을 주장한 FR."""
        return tuple(sorted(self.fr_ids & frozenset(self.ucs_claiming)))

    @property
    def orphan_fr_ids(self) -> tuple[str, ...]:
        """아무 UC도 주장하지 않은 FR — 누락 위험."""
        return tuple(sorted(self.fr_ids - frozenset(self.ucs_claiming)))

    @property
    def attached_nfr_ids(self) -> tuple[str, ...]:
        """제약 또는 다른 요구사항 모델 요소로 근거 있게 반영된 NFR."""
        attached = (
            frozenset(self.ucs_constrained_by)
            | frozenset(self.deployment_needs_of)
            | self.constraint_ids
            | frozenset(self.actors_of)
            | frozenset(self.capabilities_of)
        )
        return tuple(sorted(self.nfr_ids & attached))

    @property
    def unattached_nfr_ids(self) -> tuple[str, ...]:
        """Use Case와 배포 필요사항 어디에도 연결되지 않은 NFR."""
        return tuple(sorted(self.nfr_ids - frozenset(self.attached_nfr_ids)))

    @property
    def coverage_ratio(self) -> float:
        """UC가 실현한 FR 비율(호환 필드). FR이 없으면 1.0이다."""
        if not self.fr_ids:
            return 1.0
        return round(len(self.covered_fr_ids) / len(self.fr_ids), 4)

    @property
    def goal_ids(self) -> tuple[str, ...]:
        """FR 중 정책·불변조건이 아니라 사용자 목표 후보인 id."""
        return tuple(sorted(self.fr_ids - self.constraint_ids))

    @property
    def covered_goal_ids(self) -> tuple[str, ...]:
        return tuple(sorted(frozenset(self.goal_ids) & frozenset(self.ucs_claiming)))

    @property
    def missing_goal_ids(self) -> tuple[str, ...]:
        return tuple(sorted(frozenset(self.goal_ids) - frozenset(self.ucs_claiming)))

    @property
    def goal_coverage_ratio(self) -> float:
        if not self.goal_ids:
            return 1.0
        return round(len(self.covered_goal_ids) / len(self.goal_ids), 4)

    @property
    def accounted_ids(self) -> tuple[str, ...]:
        """액터·UC·capability 중 하나에 근거 있게 반영된 요구사항."""
        return tuple(sorted(frozenset(self.by_id) & self.referenced_ids))

    @property
    def unaccounted_ids(self) -> tuple[str, ...]:
        """어느 요구사항 산출물에도 연결되지 않은 진짜 추적 공백."""
        return tuple(sorted(frozenset(self.by_id) - self.referenced_ids))

    @property
    def accounted_ratio(self) -> float:
        """전체 요구사항 중 어느 산출물에든 반영된 비율."""
        if not self.by_id:
            return 1.0
        return round(len(self.accounted_ids) / len(self.by_id), 4)


def index(state: dict) -> Traceability:
    """상태에서 추적 색인을 만든다(순수 함수, LLM 없음)."""
    classified = state.get("classified") or []
    by_id = {r["id"]: r for r in classified}

    claiming: dict[str, list[str]] = {}
    constrained: dict[str, list[str]] = {}
    for uc in state.get("use_cases") or []:
        uc_id = uc.get("id", "?")
        for rid in uc.get("requirement_ids", []) or []:
            claiming.setdefault(rid, []).append(uc_id)
        for nid in uc.get("nfr_ids", []) or []:
            constrained.setdefault(nid, []).append(uc_id)

    # FR/NFR은 문장 분류이고 realizes/constrains는 RTM 간선 의미다. 기능형 정책이나
    # 불변조건은 requirement_ids에 거짓 실현 주장으로 넣지 않고 이 맵에 보존한다.
    for requirement_id, use_case_ids in (state.get("constraint_applicability") or {}).items():
        for use_case_id in use_case_ids or []:
            values = constrained.setdefault(str(requirement_id), [])
            if str(use_case_id) not in values:
                values.append(str(use_case_id))

    steps: dict[str, list[str]] = {}
    guarantees: dict[str, list[str]] = {}
    for spec in state.get("use_case_specs") or []:
        uc_id = spec.get("use_case_id", "?")
        for step in spec.get("main_scenario", []) or []:
            for rid in step.get("covered_req_ids", []) or []:
                steps.setdefault(rid, []).append(f"{uc_id}.{step['step_number']}")
        for guarantee_kind in ("success", "minimal"):
            field_name = f"{guarantee_kind}_guarantee"
            for position, guarantee in enumerate(spec.get(field_name, []) or [], 1):
                for rid in guarantee.get("covered_req_ids", []) or []:
                    guarantees.setdefault(rid, []).append(
                        f"{uc_id}.{guarantee_kind}.{position}"
                    )

    deployment: dict[str, list[str]] = {}
    for need_id, need in (state.get("deployment_needs") or {}).items():
        for requirement_id in need.get("requirementIds", []) or []:
            deployment.setdefault(requirement_id, []).append(need_id)

    actors: dict[str, list[str]] = {}
    for actor in state.get("actors") or []:
        actor_name = str(actor.get("name") or "").strip()
        for requirement_id in actor.get("source_refs") or []:
            actors.setdefault(str(requirement_id), []).append(actor_name)

    capabilities: dict[str, list[str]] = {}
    for capability in (state.get("capability_contract") or {}).get("capabilities") or []:
        capability_id = str(capability.get("id") or "").strip()
        for requirement_id in capability.get("requirementIds") or []:
            capabilities.setdefault(str(requirement_id), []).append(capability_id)

    return Traceability(
        by_id=by_id,
        fr_ids=frozenset(r["id"] for r in classified if r.get("type") == "FR"),
        nfr_ids=frozenset(r["id"] for r in classified if r.get("type") == "NFR"),
        use_case_ids=frozenset(
            str(use_case.get("id") or "")
            for use_case in state.get("use_cases") or []
            if str(use_case.get("id") or "")
        ),
        constraint_ids=frozenset(
            str(requirement_id)
            for requirement_id in (state.get("constraint_applicability") or {})
        ),
        ucs_claiming={k: tuple(v) for k, v in claiming.items()},
        ucs_constrained_by={k: tuple(v) for k, v in constrained.items()},
        deployment_needs_of={k: tuple(v) for k, v in deployment.items()},
        actors_of={k: tuple(v) for k, v in actors.items()},
        capabilities_of={k: tuple(v) for k, v in capabilities.items()},
        steps_of={k: tuple(v) for k, v in steps.items()},
        guarantees_of={k: tuple(v) for k, v in guarantees.items()},
    )


def build_requirement_trace(state: dict, verdicts: list[dict] | None = None) -> dict:
    """요구사항 ID를 중심으로 연결된 산출물을 구조화한다.

    별도 표의 행을 만들지 않는다. 각 요구사항을 키로 사용하므로 호출자는 특정 요구가
    어느 유스케이스·시나리오 스텝·배포 필요사항에 연결되는지 직접 조회할 수 있다.
    """
    trace = index(state)
    real_by_req: dict[str, list[bool]] = {}
    for verdict in verdicts or []:
        requirement_id = verdict.get("requirement_id")
        if isinstance(requirement_id, str):
            real_by_req.setdefault(requirement_id, []).append(bool(verdict.get("realized")))

    needs_by_req: dict[str, list[str]] = {}
    referenced_by_needs: set[str] = set()
    deployment_needs = state.get("deployment_needs") or {}
    for need_id, need in deployment_needs.items():
        for requirement_id in need.get("requirementIds", []):
            referenced_by_needs.add(requirement_id)
            if requirement_id in trace.by_id:
                needs_by_req.setdefault(requirement_id, []).append(need_id)

    requirements: dict[str, dict] = {}
    for requirement_id, requirement in trace.by_id.items():
        requirement_type = requirement.get("type")
        verdict_values = real_by_req.get(requirement_id)
        requirements[requirement_id] = {
            "type": requirement_type,
            "text": requirement.get("text", ""),
            "use_cases": list(trace.ucs_of(requirement_id)),
            "realized_by_use_cases": list(trace.ucs_claiming.get(requirement_id, ())),
            "constrains_use_cases": list(trace.ucs_constrained_by.get(requirement_id, ())),
            "modeled_as_constraint": requirement_id in trace.constraint_ids,
            "scenario_steps": list(trace.steps_of.get(requirement_id, ())),
            "guarantees": list(trace.guarantees_of.get(requirement_id, ())),
            "actor_roles": list(trace.actors_of.get(requirement_id, ())),
            "deployment_needs": sorted(needs_by_req.get(requirement_id, [])),
            "capabilities": list(trace.capabilities_of.get(requirement_id, ())),
            "qualifies": list(requirement.get("qualifies", [])),
            "realized": any(verdict_values) if verdict_values else None,
        }

    use_cases = {}
    for use_case in state.get("use_cases") or []:
        use_case_id = use_case.get("id")
        if not use_case_id:
            continue
        use_cases[use_case_id] = {
            "name": use_case.get("name", ""),
            "requirements": [
                requirement_id
                for requirement_id in trace.by_id
                if use_case_id in trace.ucs_of(requirement_id)
            ],
        }
    unknown_refs = sorted(
        set(trace.unknown_refs) | (referenced_by_needs - frozenset(trace.by_id))
    )
    return {
        "requirements": requirements,
        "use_cases": use_cases,
        "deployment_needs": deployment_needs,
        "unknown_refs": unknown_refs,
        "unknown_use_case_refs": list(trace.unknown_use_case_refs),
        "summary": {
            "requirements": len(requirements),
            "covered_functional_requirements": len(trace.covered_fr_ids),
            "unrealized_functional_requirements": list(trace.orphan_fr_ids),
            "orphan_functional_requirements": list(trace.missing_goal_ids),
            "attached_nonfunctional_requirements": len(trace.attached_nfr_ids),
            "unattached_nonfunctional_requirements": list(trace.unattached_nfr_ids),
            "accounted_requirements": len(trace.accounted_ids),
            "unaccounted_requirements": list(trace.unaccounted_ids),
            "goal_requirements": len(trace.goal_ids),
            "covered_goal_requirements": len(trace.covered_goal_ids),
            "missing_goal_requirements": list(trace.missing_goal_ids),
            "goal_coverage_ratio": trace.goal_coverage_ratio,
            "accounted_coverage_ratio": trace.accounted_ratio,
            "deployment_needs": len(deployment_needs),
        },
    }
