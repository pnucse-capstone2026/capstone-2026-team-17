"""stage 사이 interrupt와 feedback cascade routing을 소유하는 canonical gate 경계다.

사용자가 피드백을 주면 그 스텝을 재생성하고 게이트로 루프백(다시 물음), 빈 값이면 다음 단계로
진행한다. 상위 스텝을 재생성하면 forward 흐름이 하위 스텝을 fresh 재실행하므로 cascade
정책(상위 재생성→하위 재생성)이 자연스럽게 성립한다.

라우팅 방식(정적): 게이트 노드는 상태 업데이트 + 라우팅 마커(gate_route: "advance"|"loop")만
반환하고, 분기 대상은 서브그래프의 add_conditional_edges(route_gate, {...})가 컴파일 타임에
선언한다. (예전 Command(goto=...) 런타임 라우팅을 대체 — 토폴로지가 컴파일 타임에 고정된다.)
"advance"는 서브그래프 END(→ 상위 그래프가 다음 스텝으로), "loop"는 같은 게이트로 재진입.

피드백은 Workspace가 실제 ref로 검증한 `FeedbackEdit`을 받는다. local scope면 대상 항목만
고쳐 형제(및 그 id)를 보존하고, cascade는 게이트가 이미 진행한 단계(up_to)까지만 수행한다.
"""
from __future__ import annotations

from typing import cast

from langgraph.types import interrupt

from app.requirements.contracts.request import (
    DeploymentPreferences,
    FeedbackEdit,
    IdentitySourceAnswer,
    ResourceAnswer,
)
from app.requirements.contracts.state import AgentState
from app.requirements.modeling.refinement import classify
from app.requirements.modeling.relationships import check_relationships
from app.requirements.modeling.specifications import check_specs
from app.requirements.orchestration.feedback import apply_feedback_upto


def _ask(stage: str, summary, *, edit_stage: str | None = None, edit_targets=(),
         questions=(), semantic_ambiguity_question=None, identity_source_question=None) -> object:
    """피드백을 요청하는 interrupt. 재개 값을 그대로 반환한다.

    재개 값은 `FeedbackEdit`·`ResourceAnswer`·`DeploymentPreferences` 중 하나다.
    `edit_stage`/`edit_targets`는 화면이 `FeedbackEdit`을 만들 때 쓰는 재료다 — 어느
    단계를 재생성할 수 있고 어떤 항목을 고를 수 있는지. 화면이 그걸 보내면 의도 분류
    LLM 호출이 생략된다.

    `questions`는 **되묻기**의 재료다(`RESOURCE_SPEC`의 못 채운 칸). 피드백과 같은
    자리에서 물어야 하는 이유는 하나다 — 사용자가 요구사항을 확인하는 그 순간이
    "클라우드 제약도 함께 정하는" 유일한 자리이고, 뒤로 미루면 이미 넘어간 뒤가 된다.
    """
    return interrupt({
        "stage": stage,
        "status": "need_feedback",
        "prompt": f"Enter feedback for [{stage}]. Leave it blank to continue.",
        "summary": summary,
        "edit_stage": edit_stage,
        "edit_targets": list(edit_targets),
        "resource_questions": list(questions),
        "semantic_ambiguity_question": semantic_ambiguity_question,
        "identity_source_question": identity_source_question,
    })


def _empty(answer) -> bool:
    """다음 단계로 진행하라는 신호인지. 구조화 편집은 지시가 비었을 때만 비어 있다."""
    if isinstance(answer, FeedbackEdit):
        return not answer.instruction.strip()
    # 되묻기의 답은 **내용이 있는 칸이 하나라도 있으면** 답한 것이다. 빈 문자열만 온
    # 것은 "이 칸은 모르겠다"이므로 진행 신호로 읽는다 — 아니면 모르는 칸 하나가
    # 세션을 영원히 게이트에 묶어 둔다.
    if isinstance(answer, ResourceAnswer):
        if answer.free_text is not None:
            return not answer.free_text.strip()
        return not any(str(v or "").strip() for v in answer.answers.values())
    if isinstance(answer, DeploymentPreferences):
        return not answer.targets
    return not str(answer or "").strip()


def _has_blocking_resource_question(state: AgentState) -> bool:
    """Return whether a resource question must be answered before advancing.

    ``suggested`` questions improve a later recommendation but are not required to
    complete the deployment contract. Every other question represents a missing or
    ambiguous value whose answer changes the resource plan.
    """
    questions = (state.get("resource_intake") or {}).get("questions", [])
    return any(question.get("kind") != "suggested" for question in questions)


def _as_text(answer) -> str:
    """자연어만 받는 자리(step1 재분류·의도 분류)에 넘길 문자열.

    **`ResourceAnswer`는 여기 오면 안 된다.** 되묻기의 답을 물어보지 않은 게이트로 보내면
    `str(answer)`가 pydantic 표현을 만들어 그것이 자연어 피드백으로 흘러든다 — 사용자가
    쓰지도 않은 문장으로 산출물이 재생성된다. 조용히 넘기느니 여기서 멈춘다.
    """
    if isinstance(answer, (DeploymentPreferences, ResourceAnswer)):
        raise TypeError(
            "Structured deployment input is accepted only at the requirements gate; "
            "it must not be routed as natural-language feedback."
        )
    return answer.instruction if isinstance(answer, FeedbackEdit) else str(answer)


def _pick(state: dict, keys: tuple[str, ...]) -> dict:
    """state에서 존재하는 키만 골라 상태 업데이트 delta로 만든다."""
    return {k: state[k] for k in keys if k in state}


def route_gate(state: AgentState) -> str:
    """게이트가 남긴 마커를 읽어 분기 키("advance"|"loop")를 돌려준다(조건부 엣지용)."""
    return state.get("gate_route", "advance")


# ---------------------------------------------------------------------------
# 게이트 노드 — (상태 업데이트 + gate_route 마커) dict를 반환. Command 라우팅 없음.
# ---------------------------------------------------------------------------
def gate_requirements(state: AgentState) -> dict[str, object]:
    """step1(요구사항 구체화·FR/NFR 분류) 말미 게이트.

    피드백이 있으면 재분류(classify: BERT 단독) 후 루프백, 없으면 다음 단계(step2)로 진행한다.
    (step1은 아직 액터/UC가 없어 의도 분류 대상이 아니므로 여기서는 분류 노드를 그대로 재실행한다.)
    """
    # step1에는 재생성할 stage 선택지가 없다(분류는 BERT 단독 결정론). 그래서
    # edit_stage를 주지 않는다 — 화면이 구조화 편집을 만들 재료가 없다는 뜻이다.
    answer = _ask(
        "requirements",
        [f"{r['id']}:{r['type']} {r['text']}" for r in state.get("classified", [])],
        questions=(state.get("resource_intake") or {}).get("questions", []),
    )
    if _empty(answer):
        # A blank review acknowledgement must not silently discard a missing or
        # ambiguous deployment input. Re-enter only the deterministic contract step;
        # requirement refinement and deployment-capability extraction stay cached.
        return {
            "gate_route": (
                "answers" if _has_blocking_resource_question(state) else "advance"
            )
        }

    # **되묻기의 답은 요구사항 피드백이 아니다.** 재분류를 돌리면 사용자는 질문에 답했을
    # 뿐인데 요구사항이 흔들린다. 답은 상태에 쌓고 루프백만 한다 — 루프가
    # `derive_deployment_needs → structure_constraints`를 다시 지나며 스펙이 새 답으로
    # 다시 조립된다(그 배선이 이미 있어서 여기서 단계를 부르지 않는다).
    if isinstance(answer, DeploymentPreferences):
        preferences = answer.model_dump(mode="json", exclude_unset=True)
        return {
            "initial_cloud_constraints": preferences,
            "resource_constraints_text": answer.resource_constraints_text,
            # 자유문장 제약이 새로 들어오면 해당 추출 분기도 갱신해야 한다. 전용 필드만
            # 들어온 일반 경로는 LLM 분석을 반복하지 않고 계약 조립만 다시 수행한다.
            "gate_route": (
                "loop" if answer.resource_constraints_text.strip() else "answers"
            ),
        }

    if isinstance(answer, ResourceAnswer):
        if answer.free_text is not None:
            contexts = list(state.get("resource_free_text_answers") or [])
            contexts.append(
                {
                    "expected_field": str(answer.expected_field or ""),
                    "text": answer.free_text,
                }
            )
            # The graph's loop edge starts at analyze_cloud_inputs, never classify.
            return {"resource_free_text_answers": contexts, "gate_route": "loop"}
        merged = {**(state.get("resource_answers") or {}), **answer.answers}
        # **`answers` 경로로 돌아간다** — 일반 `loop`는 `derive_deployment_needs`부터 다시
        # 도는데, 이 분기는 `classify`를 안 돌려 `classified`가 그대로다. 배포 필요사항은
        # 그 입력의 순수 함수라 같은 답이 나오고, LLM 층을 켜면 그 재계산이 표당 24초짜리
        # 호출 3벌이 된다(실측 396표: 중앙값 23.6초). 답 한 번에 1~2분을 버리는 셈이다.
        return {"resource_answers": merged, "gate_route": "answers"}

    upd = classify(state, feedback=_as_text(answer))  # BERT 단독 재분류
    return {**upd, "gate_route": "loop"}


def gate_use_cases(state: AgentState) -> dict[str, object]:
    """step2(액터/유스케이스) 말미 게이트. 의도에 따라 actors/use_cases를 범위대로 재생성."""
    use_cases = state.get("use_cases", [])
    answer = _ask(
        "use_cases",
        [u["name"] for u in use_cases],
        edit_stage="use_cases",
        # id가 없는 항목은 고를 수 없으니 뺀다. 게이트가 사용자에게 물어보는 자리라
        # 여기서 KeyError로 죽으면 세션이 통째로 끝난다.
        edit_targets=[u["id"] for u in use_cases if u.get("id")],
    )
    if _empty(answer):
        return {"gate_route": "advance"}
    st = dict(state)
    if not isinstance(answer, FeedbackEdit):
        raise TypeError("Use-case feedback requires a validated FeedbackEdit.")
    apply_feedback_upto(cast(AgentState, st), answer, up_to="coverage")
    return {
        **_pick(st, (
            "actors", "use_cases", "constraint_applicability", "coverage", "traceability"
        )),
        "gate_route": "loop",
    }


def gate_specs(state: AgentState) -> dict[str, object]:
    """step3(명세) 말미 게이트. specs local 피드백은 대상 UC 명세만 재생성한다."""
    specs = state.get("use_case_specs", [])
    # The stage's check_specs node owns the selective LLM review and persists
    # its result.  Do not recalculate it here: interrupt resume re-enters this
    # node and would otherwise duplicate the review call.
    ambiguity = state.get("semantic_ambiguity_question")
    source_question = state.get("identity_source_question")
    answer = _ask(
        "specs",
        [s["use_case_id"] for s in specs],
        edit_stage="specs",
        edit_targets=[s["use_case_id"] for s in specs if s.get("use_case_id")],
        semantic_ambiguity_question=ambiguity,
        identity_source_question=source_question,
    )
    if _empty(answer):
        if source_question:
            return {
                "gate_route": "loop",
                "semantic_ambiguity_question": ambiguity,
                "identity_source_question": source_question,
            }
        return {"gate_route": "advance", "semantic_ambiguity_question": ambiguity}
    if isinstance(answer, IdentitySourceAnswer):
        if not isinstance(source_question, dict):
            raise ValueError("There is no current identity-source question to answer.")
        if (answer.use_case_id != source_question.get("useCaseId")
                or answer.obligation_ref != source_question.get("obligationRef")):
            raise ValueError("Identity-source answer does not match the current question.")
        expected_id = (
            f"authenticated_context:{answer.source_authenticate_obligation_ref}"
            if answer.identity_source_kind == "authenticated_context"
            else answer.identity_source_kind
        )
        options = source_question.get("options") or []
        if not any(isinstance(option, dict) and option.get("id") == expected_id
                   and option.get("identitySourceKind") == answer.identity_source_kind
                   and (answer.identity_source_kind != "authenticated_context"
                        or option.get("sourceAuthenticateObligationRef") == answer.source_authenticate_obligation_ref)
                   for option in options):
            raise ValueError("Identity-source choice is not one of the current question options.")
        st = dict(state)
        specs = [dict(spec) for spec in st.get("use_case_specs", [])]
        target_spec = next((spec for spec in specs
                            if spec.get("use_case_id") == answer.use_case_id), None)
        if target_spec is None:
            raise ValueError("Identity-source question refers to a missing use case.")
        contract = dict(target_spec.get("public_contract") or {})
        obligations = [dict(item) for item in contract.get("identity_obligations", [])]
        target = next((item for item in obligations
                       if item.get("obligation_ref") == answer.obligation_ref
                       and item.get("obligation") == "identify"), None)
        if target is None or target.get("identity_source_kind") != "unresolved":
            raise ValueError("Identity-source obligation is no longer unresolved.")
        target["identity_source_kind"] = answer.identity_source_kind
        if answer.source_authenticate_obligation_ref:
            target["source_authenticate_obligation_ref"] = answer.source_authenticate_obligation_ref
        else:
            target.pop("source_authenticate_obligation_ref", None)
        contract["identity_obligations"] = obligations
        target_spec["public_contract"] = contract
        st["use_case_specs"] = specs
        overrides = dict(st.get("identity_source_overrides") or {})
        overrides[answer.obligation_ref] = {
            "identity_source_kind": answer.identity_source_kind,
            **({"source_authenticate_obligation_ref": answer.source_authenticate_obligation_ref}
               if answer.source_authenticate_obligation_ref else {}),
        }
        st["identity_source_overrides"] = overrides
        st.update(check_specs(cast(AgentState, st), review_semantic=False))
        return {
            "use_case_specs": st["use_case_specs"],
            "spec_report": st["spec_report"],
            "semantic_ambiguity_question": st.get("semantic_ambiguity_question"),
            "identity_source_question": st.get("identity_source_question"),
            "identity_source_overrides": overrides,
            "gate_route": "loop",
        }
    st = dict(state)
    if not isinstance(answer, FeedbackEdit):
        raise TypeError("Specification feedback requires a validated FeedbackEdit.")
    # A typed question answer has resolved this one product choice.  Mark it
    # before recomputing reports so check_specs does not reopen the same source
    # wording while the local UC revision is being applied.
    resolved_ambiguity = bool(ambiguity)
    if resolved_ambiguity:
        st["semantic_ambiguity_questioned"] = True
    intent, _ = apply_feedback_upto(cast(AgentState, st), answer, up_to="specs")
    st.update(check_specs(
        cast(AgentState, st),
        allowed_producer_ids=(
            set(intent.target_ids)
            if intent.scope == "local" and intent.stage == "specs" else None
        ),
    ))  # spec_report 갱신
    upd = _pick(st, (
        "actors", "use_cases", "constraint_applicability", "coverage", "traceability",
        "use_case_specs", "spec_report",
    ))
    return {
        **upd,
        # A natural-language spec revision can introduce a new unresolved
        # identity source. Return the freshly recomputed question so the next
        # gate iteration and Workspace response see it instead of the old one.
        "identity_source_question": st.get("identity_source_question"),
        "semantic_ambiguity_question": (
            None if resolved_ambiguity else st.get("semantic_ambiguity_question")
        ),
        "semantic_ambiguity_questioned": (
            True if resolved_ambiguity else state.get("semantic_ambiguity_questioned", False)
        ),
        "gate_route": "loop",
    }


def gate_relationships(state: AgentState) -> dict[str, object]:
    """step4(관계/다이어그램) 말미 게이트."""
    # 관계는 항목 단위로 고르기 어렵다(연결의 집합이지 목록이 아니다) → broad만 제공.
    answer = _ask(
        "relationships", state.get("relationships", {}), edit_stage="relationships"
    )
    if _empty(answer):
        return {"gate_route": "advance"}
    st = dict(state)
    if not isinstance(answer, FeedbackEdit):
        raise TypeError("Relationship feedback requires a validated FeedbackEdit.")
    apply_feedback_upto(cast(AgentState, st), answer, up_to="diagram")
    st.update(check_specs(cast(AgentState, st), allowed_producer_ids=set()))
    st.update(check_relationships(cast(AgentState, st)))  # relationship_report 갱신
    upd = _pick(st, (
        "actors", "use_cases", "constraint_applicability", "coverage", "traceability",
        "use_case_specs", "spec_report",
        "relationships", "relationship_report", "diagram",
    ))
    return {**upd, "gate_route": "loop"}
