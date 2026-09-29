"""PIPELINE 결함을 어느 stage로 되돌릴지 결정하는 canonical supervisor 경계다.

## 무엇이 없어서 이게 필요했나

지금까지 반성 루프는 **단계 안에 갇혀 있었다.** `step3`은 명세를 다시 쓰고, `step4`는 관계를
다시 뽑는다. 그런데 결함의 원인이 위에 있으면 그 자리에서 아무리 다시 써도 안 고쳐진다 —
유스케이스가 잘못 쪼개져서 생긴 복귀 결함은 명세를 다시 써서 없앨 수 없다.

그 상황에는 이름이 이미 있었다. `repair_stopped="no_improvement"` — 재생성이 결함을 줄이지
못했다는 기록이다. **그 신호를 받아 위로 올려보낼 통로가 구조에 없었다.**

그리고 `review_model`(2단계 의미 검증)은 결함을 찾아 놓고 **아무도 고치지 않았다.** 원인이
`identify_actors`에 있는데 그 단계를 다시 부를 권한이 어디에도 없었기 때문이다.

## 어떻게 정하는가 — 라우팅 표는 지식베이스다

지적 문구는 규칙 id를 들고 다니고(`Rule.tag`), 규칙은 자기를 **낸 단계**를 안다(`Rule.owner`).
그래서 "이 결함을 누구에게 돌려보낼지"는 **찾아보면 되는 사실**이고 LLM에 물을 일이 아니다.
여기에 LLM 라우터를 두면 유도할 수 있는 판단에 비결정성을 더하는 셈이 된다
(GPT-OSS 계열은 결정론적이지 않다 — `runtime/structured_llm.py` 참고).

LLM 감독자로 바꾸고 싶으면 갈아끼울 자리는 `decide()` 하나다. 그때 필요한 것은 라우팅
품질을 재는 눈금인데, 그건 아직 없다(`evaluation/`은 산출물 품질을 잰다).

## 정책

1. 남아 있는 지적을 모아 `owner`로 묶는다.
2. 그 단계가 **자기 루프에서 이미 포기했으면**(`repair_stopped`가 clean이 아니면) 같은 단계로
   되돌리지 않고 **상위 단계로 올린다**. 같은 자리에서 또 실패할 것이 뻔한 재생성에 예산을
   쓰지 않는다 — 그게 이 층이 생긴 이유다.
3. 여러 단계가 걸리면 **가장 위쪽 하나만** 고른다. 위를 고치면 아래는 cascade로 다시 돈다.
4. 같은 입력·finding에는 같은 전략을 다시 쓰지 않는다. 미사용 상위 전략이 없으면
   ``stalled``로 드러내므로 숫자 예산 없이도 같은 실패를 반복하지 않는다.

**이 층은 판단만 한다.** 실제 재실행은 그래프(엣지)나 cascade가 한다 — 판단을 순수하게
두면 LLM 없이 시험할 수 있다.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

from app.requirements.contracts.state import AgentState
from app.requirements.knowledge import rules
from app.requirements.modeling.feedback import feedback_for  # noqa: F401 - public facade
from app.requirements.resources import cloud_contract
from app.requirements.runtime import telemetry
from app.validation import stable_digest

ADVANCE = "advance"
REDO = "redo"


def blocking_issues(state: AgentState, through: str = "relationships") -> list[str]:
    """요구사항 handoff를 막는 unresolved finding을 현재 report에서 읽는다.

    ``through``는 마지막으로 완료된 검토 경계를 뜻한다. ``model``은 모델 검토와 coverage를,
    ``specs``는 여기에 명세 report를, ``relationships``는 완성된 요구사항 산출물까지 검사한다.
    별도 readiness 결과를 만들지 않고 기존 report를 읽으므로, 저장된 요구사항 state를 바꾸지
    않으면서 모든 호출자가 같은 handoff 판단을 내릴 수 있다.

    current-schema stage가 생성한 report만 판정 근거로 삼는다. 과거 checkpoint에서 빠진
    report를 원시 산출물로 재구성하지 않으며, design 구조 계약은 adapter가 별도로 검사한다.
    """
    scopes = ("model", "specs", "relationships")
    if through not in scopes:
        raise ValueError(f"Unknown requirements handoff scope: {through!r}")

    issues: list[str] = []

    source_issues = state.get("requirement_source_issues") or []
    if source_issues:
        findings = sorted({str(issue) for issue in source_issues})
        issues.append(
            f"requirement source mapping has unresolved issues: {', '.join(findings)}"
        )

    resource_intake = state.get("resource_intake") or {}
    if isinstance(resource_intake, dict):
        resource_errors = sorted(
            {str(error) for error in resource_intake.get("errors") or []}
        )
        current_resource_values = (
            resource_intake.get("draft")
            or state.get("resource_spec")
            or {}
        )
        missing_required_fields = set(
            cloud_contract.missing_fields(current_resource_values)
        )
        required_questions = sorted(
            {
                str(question.get("field") or "unknown field")
                for question in (resource_intake.get("questions") or [])
                if isinstance(question, dict)
                and question.get("kind") != "suggested"
                and question.get("field") in missing_required_fields
            }
        )
        if resource_errors or required_questions:
            detail = []
            if resource_errors:
                detail.append(f"errors: {', '.join(resource_errors)}")
            if required_questions:
                detail.append(f"required questions: {', '.join(required_questions)}")
            status = "missing" if not state.get("resource_spec") else "invalid"
            issues.append(f"resource contract is {status}: {'; '.join(detail)}")

    capability_contract = state.get("capability_contract") or {}
    if isinstance(capability_contract, dict):
        unresolved_capabilities = sorted(
            {
                str(capability.get("id") or f"item {index}")
                for index, capability in enumerate(
                    capability_contract.get("capabilities") or [], start=1
                )
                if isinstance(capability, dict)
                and capability.get("decision") == "needsQuestion"
            }
        )
        if unresolved_capabilities:
            issues.append(
                "capability contract needs answers for: "
                f"{', '.join(unresolved_capabilities)}"
            )

    review = state.get("model_review") or {}
    if isinstance(review, dict):
        issues.extend(f"model review: {issue}" for issue in review.get("issues") or [])
        unexamined = review.get("unexamined_rules") or []
        if unexamined:
            issues.append(f"model review left rules unexamined: {', '.join(map(str, unexamined))}")
        if review.get("semantic_status") in {"failed", "ungrounded"}:
            issues.append(f"model review was not validated: {review['semantic_status']}")

    coverage = state.get("coverage") or {}
    if isinstance(coverage, dict):
        # An FR label does not prove that the sentence is an independently initiated user goal.
        # Cross-cutting policy and integrity statements remain intentionally unattached unless
        # model review identifies a genuinely missing goal. Unknown references, in contrast,
        # can never be joined safely downstream.
        unknown_refs = coverage.get("unknown_requirement_refs") or []
        if unknown_refs:
            issues.append(
                f"coverage has unknown requirement references: {', '.join(map(str, unknown_refs))}"
            )

    if through == "model":
        return list(dict.fromkeys(issues))

    specs = state.get("spec_report") or {}
    if isinstance(specs, dict):
        total = specs.get("total_issues", 0) or 0
        if total:
            issues.append(f"specification report has {total} unresolved issue(s)")
        failed = specs.get("failed_ucs") or []
        if failed:
            issues.append(f"specification generation failed for: {', '.join(map(str, failed))}")
        unvalidated = specs.get("unvalidated_ucs") or []
        if unvalidated:
            issues.append(f"specifications were not validated for: {', '.join(map(str, unvalidated))}")

    if through == "specs":
        return list(dict.fromkeys(issues))

    relationships = {
        **(state.get("relationships") or {}),
        **(state.get("relationship_report") or {}),
    }
    if isinstance(relationships, dict):
        for field, label in (
            ("relationship_issues", "relationship report"),
            ("missing_supporting_associations", "relationship report is missing supporting associations"),
            ("orphan_actors", "relationship report has orphan actors"),
            ("dropped_refs", "relationship report dropped references"),
        ):
            findings = relationships.get(field) or []
            if findings:
                issues.append(f"{label}: {', '.join(map(str, findings))}")
        unexamined = relationships.get("unexamined_rules") or []
        if unexamined:
            issues.append(
                "relationship review left rules unexamined: "
                f"{', '.join(map(str, unexamined))}"
            )
        if relationships.get("semantic_status") in {"failed", "ungrounded"}:
            issues.append(
                "relationship review was not validated: "
                f"{relationships['semantic_status']}"
            )
    return list(dict.fromkeys(issues))


def blocking_findings(
    state: AgentState, through: str = "relationships"
) -> list[dict[str, object]]:
    """정규 handoff 차단 사유를 공개용 구조로 투영한다."""
    spec_targets = sorted(
        str(use_case_id)
        for use_case_id in (state.get("spec_report") or {}).get("issues_by_uc", {})
    )
    findings: list[dict[str, object]] = []
    for message in blocking_issues(state, through=through):
        if message.startswith("requirement source mapping"):
            code, stage, repairable = "requirements.source", "requirements", False
        elif message.startswith("resource contract"):
            code, stage, repairable = "requirements.resource-contract", "resources", False
        elif message.startswith("capability contract"):
            code, stage, repairable = "requirements.capability-contract", "resources", False
        elif message.startswith("model review"):
            code, stage, repairable = "requirements.model-review", "use_cases", True
        elif message.startswith("coverage"):
            code, stage, repairable = "requirements.coverage", "use_cases", True
        elif message.startswith("specification"):
            code, stage, repairable = "requirements.specification", "specs", True
        else:
            code, stage, repairable = "requirements.relationship", "relationships", True
        findings.append(
            {
                "code": code,
                "stage": stage,
                "target_ids": spec_targets if stage == "specs" else [],
                "message": message,
                "severity": "error",
                "repairable": repairable,
            }
        )
    return findings


def repair_state(state: AgentState, through: str = "relationships") -> dict[str, object]:
    """저장된 단계별 수리 이력을 공개용 상태 요약으로 만든다."""
    blockers = blocking_findings(state, through=through)
    histories = [
        spec.get("repair_history")
        for spec in state.get("use_case_specs") or []
        if isinstance(spec.get("repair_history"), dict)
    ]
    relationships_history = (state.get("relationships") or {}).get("repair_history")
    if isinstance(relationships_history, dict):
        histories.append(relationships_history)
    attempts = [
        attempt
        for history in histories
        for attempt in history.get("attempts", [])
        if isinstance(attempt, dict)
    ]
    redo = [entry for entry in state.get("redo_history") or [] if isinstance(entry, dict)]
    statuses = {str(history.get("status") or "") for history in histories}
    if not blockers:
        status = "COMPLETED"
    elif "WAITING_EXTERNAL" in statuses:
        status = "WAITING_EXTERNAL"
    elif blockers and not any(bool(blocker["repairable"]) for blocker in blockers):
        status = "NEEDS_INPUT"
    else:
        # A local stage may be stalled while an upstream/cascade strategy is still available.
        # Workspace owns the automatic escalation, so the handoff remains actionable.
        status = "ACTIVE"
    recent = [*attempts, *redo][-5:]
    rejected_candidates = sorted(
        {
            str(attempt.get("candidate_digest") or "")
            for attempt in attempts
            if attempt.get("outcome") not in {"improved", "clean"}
            and attempt.get("candidate_digest")
        }
    )
    return {
        "status": status,
        "attempt_count": len(attempts) + len(redo),
        "accepted_count": sum(
            attempt.get("outcome") in {"improved", "clean"} for attempt in attempts
        ),
        "recent_attempts": recent,
        "tried_strategies": sorted(
            {
                str(entry.get("strategy_key") or "")
                for entry in [*attempts, *redo]
                if entry.get("strategy_key")
            }
        ),
        "rejected_candidate_digests": rejected_candidates,
        "finding_digest": stable_digest(
            [
                {
                    "code": blocker["code"],
                    "stage": blocker["stage"],
                    "targets": blocker["target_ids"],
                    "message": blocker["message"],
                }
                for blocker in blockers
            ]
        ),
        "stall_reason": next(
            (
                str(history.get("stall_reason") or "")
                for history in reversed(histories)
                if history.get("stall_reason")
            ),
            "",
        ),
    }

# `stages`는 **함수 안에서** import한다. 단계 함수들이 이 모듈의 `feedback_for`를 쓰는데
# (steps → supervisor), `stages`는 그 단계 함수들을 import한다(stages → steps). 모듈 상단에서
# 끌어오면 순환이 된다 — `feedback_gates.py`가 `feedback`을 지연 import하는 것과 같은 이유다.
def _editable_in_order() -> tuple[str, ...]:
    """되돌릴 수 있는 단계를 cascade 순서로. `stages.py`에서 파생한다(손으로 적지 않는다)."""
    from app.requirements import stage_registry as stages

    return tuple(k for k in stages.cascade_order() if k in stages.editable_keys())


def group_of(owner: str) -> str | None:
    """단계의 논리 이름 → 그 단계가 속한 그래프 그룹. `stages.py`에서 파생한다."""
    from app.requirements import stage_registry as stages

    return next((s.group for s in stages.PIPELINE if s.key == owner), None)


def upstream_of(owner: str) -> str | None:
    """이 단계 바로 위의 되돌릴 수 있는 단계. 맨 위면 None(더 올릴 곳이 없다)."""
    editable = _editable_in_order()
    if owner not in editable:
        return None
    index = editable.index(owner)
    return editable[index - 1] if index > 0 else None


@dataclass(frozen=True)
class Decision:
    """되돌릴지, 어디로, 무엇을 지시로 들려 보낼지."""

    action: str = ADVANCE
    #: 되돌릴 단계의 논리 이름(`stages.py`의 key). ADVANCE면 None.
    owner: str | None = None
    #: 그 단계에 들려 보낼 지시. 되돌리기가 값어치를 내려면 **결함을 함께 보내야** 한다 —
    #: 같은 프롬프트로 다시 부르면 같은 답이 온다.
    instruction: str = ""
    #: 왜 이렇게 정했는지(계측·리포트용). 판단이 조용하면 디버깅할 수 없다.
    reason: str = ""
    #: 자기 단계가 포기해서 위로 올린 것인가.
    escalated: bool = False
    #: 이 판단의 근거가 된 규칙 id들.
    rule_ids: tuple[str, ...] = field(default_factory=tuple)
    strategy_key: str = ""
    input_digest: str = ""


def _issues_by_owner(state: AgentState) -> dict[str, list[str]]:
    """남아 있는 지적을 낸 단계별로 묶는다. 규칙을 못 찾은 지적은 되돌릴 곳이 없다."""
    grouped: dict[str, list[str]] = {}
    sources = [
        *( (spec.get("issues") or []) for spec in (state.get("use_case_specs") or []) ),
        (state.get("relationships") or {}).get("relationship_issues") or [],
        (state.get("model_review") or {}).get("issues") or [],
    ]
    for issues in sources:
        for issue in issues:
            owner = rules.owner_of(issue)
            if owner:
                grouped.setdefault(owner, []).append(issue)
    return grouped


def _gave_up(state: AgentState, owner: str) -> bool:
    """그 단계의 자기 반성 루프가 이미 포기했는가.

    `clean`이 아닌 모든 값이 포기다 — `stalled`(새 전략 없음),
    `waiting_external`, `error`, `not_generated`(최초 생성 실패).
    """
    if owner == "specs":
        stopped = {
            spec.get("repair_stopped", "unknown")
            for spec in (state.get("use_case_specs") or [])
            if spec.get("issues")
        }
        return bool(stopped) and stopped != {"clean"}
    if owner == "relationships":
        return (state.get("relationships") or {}).get("repair_stopped", "clean") != "clean"
    # actors·use_cases에는 자기 반성 루프가 없다 — 포기할 것도 없으므로 직접 되돌린다.
    return False


def _instruction(issues: list[str], *, escalated: bool) -> str:
    """되돌릴 단계에 들려 보낼 지시.

    올려보낼 때는 **아래 단계가 못 고쳤다는 사실까지** 적는다. 그게 없으면 위 단계는
    같은 산출물을 다시 만들고, 아래 단계는 또 같은 결함을 낸다.
    """
    head = (
        "The downstream stage could not repair these defects on its own, so the cause is "
        "likely here. Regenerate so that they cannot arise:"
        if escalated else
        "These defects were found in the output. Regenerate to remove them:"
    )
    return head + "\n" + "\n".join(f"- {issue}" for issue in issues)


def decide(state: AgentState) -> Decision:
    """남은 결함을 보고 되돌릴지 정한다(LLM 없음, 순수 함수)."""
    grouped = _issues_by_owner(state)
    if not grouped:
        return Decision(reason="남은 결함이 없다")

    attempted = {
        (str(entry.get("input_digest") or ""), str(entry.get("strategy_key") or ""))
        for entry in state.get("redo_history") or []
        if isinstance(entry, dict)
    }
    # 가장 위쪽의 아직 쓰지 않은 전략 하나만 고른다. 위를 고치면 아래는 cascade로 다시 돈다.
    targets = []
    for owner, issues in grouped.items():
        escalated = _gave_up(state, owner)
        destination = upstream_of(owner) if escalated else owner
        input_digest = stable_digest({"owner": owner, "issues": sorted(set(issues))})
        while destination is not None:
            strategy_key = f"repair:{owner}->{destination}"
            if (input_digest, strategy_key) not in attempted:
                targets.append(
                    (
                        destination,
                        owner,
                        issues,
                        destination != owner,
                        strategy_key,
                        input_digest,
                    )
                )
                break
            destination = upstream_of(destination)
    if not targets:
        return Decision(reason="stalled: 같은 finding에 사용할 새 수리 전략이 없다")

    order = list(_editable_in_order())
    destination, source, issues, escalated, strategy_key, input_digest = min(
        targets, key=lambda target: order.index(target[0])
    )
    rule_ids = tuple(
        dict.fromkeys(r for r in (rules.rule_of(i) for i in issues) if r)
    )
    reason = (
        f"{source}가 스스로 고치지 못해 {destination}로 올린다"
        if escalated else
        f"{destination}가 낸 결함 {len(issues)}건을 그 단계로 되돌린다"
    )
    return Decision(
        action=REDO,
        owner=destination,
        instruction=_instruction(issues, escalated=escalated),
        reason=reason,
        escalated=escalated,
        rule_ids=rule_ids,
        strategy_key=strategy_key,
        input_digest=input_digest,
    )


# ---------------------------------------------------------------------------
# 그래프 노드 — 판단을 상태 델타 + 라우팅 마커로 바꾼다
# ---------------------------------------------------------------------------
def route_redo(state: AgentState) -> str:
    """감독자가 남긴 마커를 읽어 분기 키를 돌려준다(조건부 엣지용).

    게이트의 `route_gate`와 같은 방식이다 — 노드는 마커만 쓰고, 갈 수 있는 곳은
    컴파일 타임에 엣지가 선언한다(런타임 `Command(goto)` 없음).
    """
    return state.get("redo_route", "advance")


def supervise_for(
    *allowed_groups: str,
) -> Callable[[AgentState], AgentState]:
    """그 자리에서 되돌릴 수 있는 그룹만 허용하는 감독 노드를 만든다.

    자리마다 갈 수 있는 곳이 다르다 — `supervise_model`에서 `draw_diagram`으로 "되돌리는"
    것은 아직 돌지 않은 그룹으로 뛰는 것이고, 그러면 산출물 없이 아래가 돈다. 허용 목록을
    노드에 박아 두면 엣지 맵과 어긋난 대상을 골랐을 때 **죽지 않고 드러난다.**
    """
    allowed = frozenset(allowed_groups)

    def supervise(state: AgentState) -> AgentState:
        decision = decide(state)
        if decision.action == ADVANCE:
            # 지시를 비운다 — 남겨 두면 다음에 그 단계가 돌 때 낡은 지시를 다시 먹는다.
            return cast(AgentState, {"redo_route": "advance", "stage_feedback": {}})

        group = group_of(decision.owner or "")
        if group not in allowed:
            # 라우팅 표와 판단이 어긋났다. 크게 실패시키지 않는 대신 저하로 남긴다 —
            # 여기서 죽으면 산출물이 통째로 사라지고, 조용히 넘기면 되돌리기가 없었던
            # 것처럼 보인다.
            telemetry.record_degradation(
                "supervisor.unreachable_target",
                f"{decision.owner}(→{group})로 되돌릴 수 없는 자리다. 허용: {sorted(allowed)}",
            )
            return cast(AgentState, {"redo_route": "advance", "stage_feedback": {}})

        rounds = int(state.get("redo_rounds", 0) or 0) + 1
        history = list(state.get("redo_history") or [])
        history.append({
            "owner": decision.owner,
            "group": group,
            "reason": decision.reason,
            "escalated": decision.escalated,
            "rule_ids": list(decision.rule_ids),
            "strategy_key": decision.strategy_key,
            "input_digest": decision.input_digest,
        })
        return cast(AgentState, {
            "redo_route": group,
            "stage_feedback": {decision.owner: decision.instruction},
            "redo_rounds": rounds,
            "redo_history": history,
        })

    return supervise
