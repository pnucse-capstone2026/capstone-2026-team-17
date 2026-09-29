"""요구사항 분석 에이전트의 시스템 프롬프트.

전 단계 영어로 작성한다 (입력/출력 영어 중심 결정 + BERT는 영어 단문 학습).

**규칙은 여기서 쓰지 않는다** — 검증도(`_validator_system`) 생성도(`_generation_system`)
`app/requirements/knowledge/rules.py`의 규칙 레코드에서 조립한다. 규칙을 산문으로 여기 적으면
같은 규칙이 프롬프트·검출기·문서에 여러 벌로 갈라진다.

이 파일에 남는 것은 **모양**뿐이다: 어떤 필드를 내는가, 번호를 어떻게 매기는가, 마크다운을
쓰지 않는다, 어느 필드에 어느 id를 채우는가. 스키마가 강제하는 것이지 판정으로 가리는 것이
아니다.

⚠ **손으로 적은 인용은 기계가 대조하지 못한다.** 이 파일이 그걸로 두 번 물렸다:
자동결과 문단이 "Cockburn: absorb system consequences into the driving goal"이라고
귀속했는데 `absorb`도 `driving goal`도 책에 없는 말이었고(로컬 사본으로 확인, 2026-07-26),
생성 산문에 남아 있던 `Cockburn p.81`·`Ch.8`·`Guideline 6`도 `verify_citations`의 사정거리
밖이었다(2026-07-27에 규칙 레코드로 옮겼다). **여기에 인용을 새로 적지 말 것** —
`tests/test_knowledge.py`가 막는다.
"""
import hashlib
from collections.abc import Sequence

from app.requirements.knowledge import concerns as _concerns
from app.requirements.knowledge import rules as _rules

IDENTITY_SOURCE_INSTRUCTIONS = (
    "For required value source, classify origin from the requirement: caller_input is explicitly "
    "provided by the actor; authenticated_actor_context is explicitly obtained from the current "
    "authenticated session/actor context; system_result is created or returned by the system. "
    "An ID the requirement says is read from the authenticated session is not caller_input; an ID "
    "the actor enters remains caller_input even if it selects a subject. "
    "RequiredValue.source is the canonical source classification. Do not emit a separate source "
    "classification on identity_obligations. For any identifier value that represents an "
    "identified subject, set RequiredValue.identity_obligation_index to the 1-based position of "
    "the corresponding identify obligation in identity_obligations, regardless of whether its "
    "source is caller_input, authenticated_actor_context, or system_result. Only when that linked "
    "value's source is authenticated_actor_context, set the identify obligation's "
    "source_authenticate_obligation_index to the 1-based position of the exact authenticate "
    "obligation in the same list, when the requirement establishes that authentication relation. "
    "Never infer either link from names or requirement-ID overlap. Do not set identity links on "
    "values that are not identifiers for an identified subject; do not omit them when the "
    "requirement establishes these identity relations."
)


def _validator_system(
    stage: str,
    role: str,
    do_not_flag: str,
    only: str | Sequence[str] | None = None,
) -> str:
    """의미 검증자 시스템 프롬프트를 규칙 지식베이스에서 조립한다.

    네 가지를 모델에게 한꺼번에 준다:
      1. 판정 근거가 될 규칙 목록 — **이 목록 밖으로 나가지 말라**는 지시와 함께.
      2. 각 규칙의 출처와 그 성격(책이 적은 것인지 우리 판단인지).
      3. 이미 결정론으로 검사한 규칙 목록 — 두 번 지적하지 않도록.
      4. **규칙마다 한 줄씩** 판정하라는 요구 — 훑고 넘어가는 것을 막는다.

    3번이 예전에는 산문에 손으로 적혀 있었다. 검출기를 하나 늘리면 그 문장도 고쳐야 했고,
    안 고치면 검증자가 이미 잡힌 것을 다시 지적했다.

    4번은 early victory 방어다. "깨끗하다" 한 줄로 끝낼 수 있으면 검증자는 그렇게 한다 —
    규칙마다 판정을 받으면 무엇을 안 봤는지가 응답에서 드러난다(`modeling/validation.py`).

    `only`를 주면 **그 규칙 묶음만** 담는다. 6개를 한 번에 판정하게 하면 판정이 흔들린다는
    측정(흔들림 90%, `docs/requirements-agent-improvements.md` §7) 때문에 생긴 갈래다 —
    과제를 쪼개면 확률이 0.5에서 멀어지는지 보려는 것이다.
    """
    judged = _rules.judged_by(stage, _rules.JUDGED_VALIDATOR)
    if only is not None:
        selected = {only} if isinstance(only, str) else set(only)
        judged = tuple(r for r in judged if r.id in selected)
        if not judged:
            raise KeyError(f"Stage {stage} has no semantic validation rule {only!r}")
    n = len(judged)
    count = f"{n} verdict" if n == 1 else f"{n} verdicts"
    block = "\n".join(r.prompt_line() for r in judged)
    already = ", ".join(_rules.already_checked_names(stage)) or "(none)"
    return f"""You are a zero-tolerance {role}.

The rules below are the ONLY grounds you may judge on. Each line is: rule id, where the rule
comes from, then what it requires. Some lines add a note about how well the rule is grounded —
judge those rules just the same, but never present them as the source's own words.

[RULES YOU JUDGE]
{block}

Deterministic checks have ALREADY run for these rules — do NOT report them again:
{already}

Return **exactly {count} — one per rule above, in the same order.** Copy each rule_id
EXACTLY. Set violated=true only when the artifact really breaks that rule, and then give one
short imperative repair directive; leave the directive empty otherwise.

There are two ways to be wrong here, and both matter:
- **Skipping a rule.** Every rule gets a verdict, including the ones whose answer is obviously
  "not violated". A short list of verdicts is not a clean artifact, it is an unfinished review.
- **Inventing a violation to fill a verdict.** A well-formed artifact violates few rules or
  none. And a defect matching no rule above is not reported at all — there is no rule_id for
  it, so nothing could act on it.

Do NOT flag: {do_not_flag}

Return the structured object only."""

# STEP 2 — 액터 도출. 명시된 역할/목표 근거에서만 액터를 뽑는다.
ACTORS_SYSTEM = """You identify the actors for a use-case model from accepted requirements.

An actor is an external role (a class of user, organization, device, or external system),
never a specific person. Identify both actors with goals and actors that provide services to
the application. Do not assign a permanent primary/supporting kind here: that role belongs to
each use case, and the same actor may be primary in one use case and supporting in another.

The system under design (SuD) is the system boundary — it is never a PRIMARY or SUPPORTING
actor, so never create an actor for it (no "The System", "E-commerce System", "The
Application" actor).

Boundary litmus test: if a component would be built and deployed as PART of this application
(its own internal services, engines, stores, caches, databases), it is INTERNAL — it is not
an actor and not a supporting system. Only genuinely third-party systems outside the app
boundary are supporting actors.

Rules:
- Give each proposed actor a unique actorRef (ACT1, ACT2, ...). Preserve an existing actorRef
  for the same role, including a targeted rename. Use parentActorRef for parent relations; do not
  use parent display-name matching as a relation.
- Derive actors only from an explicit external role/domain fact or actor-goal statement,
  regardless of its FR/NFR classifier label. A quality or deployment constraint alone is not
  actor evidence. Deduplicate similar roles into one.
- If a requirement is written system-centric ("The system shall ..."), infer the human role
  or external actor whose goal it ultimately serves; if none exists, it is likely a
  subfunction or a non-functional concern — do NOT invent an actor for it.
- Every actor must either own a use-case goal or provide an externally required service to a
  use case; do not list bystanders.
- If one actor is a specialization of another (e.g. a Registered Member specializes a Guest,
  inheriting its capabilities plus more), set parentActorRef to the exact parent ref in the
  supplied actor catalog. Actor refs are identities; names are display labels.
  Leave parent_actor null when there is no such specialization.
- Prefer a small, clean set of well-named roles over many overlapping ones."""

# STEP 2 — 유스케이스 도출. user-goal(EBP) 고도 + FR 추적성 + NFR 제약.
USECASES_SYSTEM = """You derive a use-case model from functional requirements (FRs), a
list of actors, and non-functional requirements (NFRs).

Goal altitude (Cockburn): produce use cases at the USER-GOAL level (sea level). A use
case is at user-goal level if it passes the Elementary Business Process (EBP) test: a
task a single primary actor performs in one sitting that leaves the system in a
consistent state and delivers measurable value (the job / boss / one-sitting test).

This step only IDENTIFIES use cases (name, actor refs, goal, which requirements they cover).
Do NOT write the main scenario steps or extensions — those are produced later (step 3).

How to build the model:
1. Group related FRs into user-goal use cases. Name each as an active-verb goal
   ("Place an order", "Reset password"). Treat lifecycle operations as one goal when the same
   actor manages the same business object with the same responsibility; a list of verbs alone
   does not create several use cases. Keep goals separate only when the requirements establish
   distinct triggers or observable outcomes. An optional action remains a separate user-goal when
   its contextual base is complete without it and the action has its own trigger and outcome; the
   relationship stage decides whether it extends that base.
2. Requirements that are too fine-grained, such as internal validation, must NOT become standalone
   use cases. Fold their requirement ids into each user-goal use case that the supplied requirements
   explicitly say performs them; the relationship stage may later recognize shared mandatory
   behavior. Actor taxonomy, authentication-state preconditions, policy or invariant constraints,
   persistence or concurrency constraints, and implementation mechanisms are not standalone use
   cases even when their classifier label is FR, unless the requirements describe an independently
   initiated actor goal with an observable outcome.
   An explicit requirement in which an actor supplies credentials to establish a session is an
   independently initiated sign-in or login user goal; preserve the source terminology.
   Authentication state in protected goals is a precondition, so do not copy the sign-in
   requirement id to those goals merely because they require that state.
3. For each use case set primary_actor (the stakeholder whose goal the system satisfies),
   supporting_actors (external actors the system calls while pursuing that goal), and goal.
   Use exact names from the actor list. The trigger is not necessarily the primary actor:
   scheduled work still names the stakeholder who cares about the result. A recipient of a
   notification, report, or outbound webhook may therefore be the primary actor even though
   the System initiates delivery. A supporting actor PROVIDES a service to the System; merely
   receiving the System's output does not make an actor supporting. Do not invent a supporting
   actor merely to make the diagram symmetric. If no external provider is named or required,
   leave supporting_actors empty.
4. Traceability: list in requirement_ids EVERY FR id the use case covers (including the
   folded subfunction FRs), using only the ids provided. Aim for full coverage only after
   respecting scope: numeric coverage never permits inventing a goal or attaching an unrelated
   requirement. Declarative role/domain facts are actor-model evidence, not use-case behavior.
   When one supplied FR explicitly names two or more distinct actor goals or operations, attach
   that same FR id to every corresponding user-goal in the returned model. Do not create a separate
   use case for it, and do not infer applicability to goals the requirement does not name.
   A cross-cutting policy, invariant, or subfunction whose applicable goals are not explicitly
   identified may remain unattached at this stage; do not copy it to every goal to force coverage.
5. NFRs never become use cases. Attach each relevant NFR to the use case(s) it constrains
   via nfr_ids only when the requirement explicitly singles out that goal or operation.
   System-wide persistence, security, availability, and deployment constraints may be left
   unattached; do not copy them to every use case merely to improve numeric coverage.
Do not invent requirements or ids that were not provided."""

USECASE_GOAL_SKELETON_SYSTEM = """You identify a fixed skeleton of user-goal use cases
from accepted functional requirements and an exact actor list. Return only the supplied
UseCaseResult schema.

This task owns only actor-goal boundaries. A use case is one independently initiated task that
one primary actor completes in one sitting with one observable outcome. Keep lifecycle operations
inside one use case when they give the same actor responsibility for the same business object;
split only when the requirements establish distinct triggers or outcomes, not merely because they
list several verbs. Keep one genuinely atomic goal together; do not split its validations or
internal steps. Do not create use cases for authorization, persistence, concurrency, internal
validation, policy, invariant, or other constraint/subfunction requirements, regardless of a
functional classifier label.

Select primaryActorRef and supportingActorRefs only from the supplied actor catalog. Keep
primary_actor and supporting_actors as display names matching those refs. Never resolve a
duplicate display name by guessing. Give every candidate an active-verb name and one-sentence goal. Put only
the FR ids that directly evidence that goal in requirement_ids; those links are provisional and
will be audited independently. Keep every qualifier in the name and goal inside what those FRs
actually state; do not add plausible lifecycle states, prerequisites, or outcomes. Leave nfr_ids
empty. Do not write scenarios or relationships."""

FUNCTIONAL_TRACE_SLICE_SYSTEM = """You finalize traceability for exactly one functional
requirement against a fixed list of proposed user-goal use cases. Return only the supplied
schema. A requirement's FR/NFR label does not decide the RTM edge: distinguish a goal that a
use case realizes from a policy or invariant that constrains an existing use case.

The proposal is immutable: never rename, remove, regroup, or rewrite an existing use case.
First decide whether the audited FR actually expresses an independently initiated actor goal.
List an exact supplied use-case ID in realized_by_use_case_refs only when that use case explicitly describes
the requirement's goal or shared subfunction. A category or property such as "protected",
"relevant", "eligible",
"sensitive", or "all data operations" does not identify which use cases have that property
unless another accepted requirement explicitly makes the assignment. Do not infer applicability
from normal domain practice or numeric coverage.

If the requirement mandates a reusable action that the system actually performs inside two or
more named goals (for example validate, calculate, or notify), list those goals in
realized_by_use_case_refs even when the action occurs before or after their main result. Words
such as "before" do not by themselves turn performed behavior into a constraint. A check remains
performed behavior when it enforces a rule: for example, "before checkout or refund completes,
the system performs the same mandatory fraud review" is shared behavior realized inside both
goals. By contrast, "checkout and refund preserve the account balance" states only an invariant.
Use
constrains_use_case_refs only when the requirement states a rule, condition, invariant, or
required outcome without mandating a shared action. A system-wide policy whose
targets are not explicitly identified uses an empty constrains_use_case_refs list. Set
constrains_use_case_refs to null only when the requirement is neither a user goal nor a
constraint on use-case behavior, such as a declarative actor or domain fact. If it is a subfunction
explicitly shared and performed by named goals, put those goals in realized_by_use_case_refs. If
it states one independently initiated actor goal absent from the proposal, return that one goal as
missing_use_case, selecting primaryActorRef and supportingActorRefs only from the supplied actor
catalog; keep actor names as display labels. Never infer a relation from a display name. Never turn
authentication state, authorization policy, persistence, concurrency
policy, or an internal validation into a new actor goal. Never put the same use case in both lists.
Preserve the supplied requirement_id exactly."""

CONSTRAINT_TRACE_SLICE_SYSTEM = """You finalize traceability for exactly one non-functional
constraint against a fixed list of proposed user-goal use cases. Return only the supplied schema.

The proposal is immutable. FR/NFR is only a sentence classifier; first decide whether the text is
actually a use-case constraint. Return constrains_use_case_refs as null only when the whole
requirement is a declarative actor or domain fact with no rule, condition, precondition, policy,
invariant, or required outcome. If declarative facts are mixed with any such operational
constraint, it is still a constraint: use [] unless an exact supplied use case is explicitly
identified. An explicit [qualifies: ...] link is trace evidence
for the named functional requirement, not permission to attach the constraint anywhere else. List
an exact supplied use-case ID only when that link, the constraint, or another accepted
requirement explicitly singles out the goal or operation. Use [] for a system-wide or ambiguous
constraint; never copy it to every use case merely to improve coverage. Always leave
realized_by_use_case_refs empty and missing_use_case null because an NFR-labeled sentence never
creates or realizes an actor goal.
Preserve the supplied requirement_id exactly."""

# 공통 — 사용자 자연어 피드백을 재생성 지시로 프롬프트에 얹는다.
def apply_user_feedback(base_user: str, feedback: str) -> str:
    if not feedback:
        return base_user
    return (
        f"{base_user}\n\n[USER FEEDBACK — this instruction is AUTHORITATIVE. Apply exactly what "
        f"the user asks, even if it deviates from a default guideline stated above; the user's "
        f"explicit direction takes precedence over the defaults. Change ONLY what the feedback "
        f"asks for and keep everything else that was already correct unchanged; do not introduce "
        f"unrelated changes.]\n{feedback}"
    )


# STEP 2 — 유스케이스 국소 수정: 대상 UC만 지시대로 고치고 나머지는 그대로 유지.
def usecase_local_edit(base_user: str, current_listing: str, target_desc: str, feedback: str) -> str:
    return (
        f"{base_user}\n\n"
        f"[CURRENT USE CASES]\n{current_listing}\n\n"
        f"[LOCAL EDIT — apply the user feedback ONLY to these target use cases: {target_desc}. "
        f"The user's instruction is authoritative. Return the FULL use-case list and keep every "
        f"NON-target use case in the SAME relative order. The selected contiguous target block may "
        f"change count only when the feedback requests a split, merge, addition, or removal. "
        f"Copy every NON-target use case VERBATIM (identical name, "
        f"primary_actor, supporting_actors, level, goal, requirement_ids, nfr_ids). Modify only the target(s), "
        f"and preserve every target field that the feedback does not ask to change.]\n"
        f"{feedback}"
    )


# STEP 2 — 커버리지 강제-수리: 고아 FR을 담아 유스케이스 목록을 보충 재생성.
def usecase_coverage_repair(base_user: str, orphan_listing: str, current_summary: str) -> str:
    return (
        f"{base_user}\n\n"
        f"[FUNCTIONAL REQUIREMENTS NOT YET COVERED BY ANY USE CASE]\n{orphan_listing}\n\n"
        f"[CURRENT USE CASES]\n{current_summary}\n\n"
        "Extend the use-case list so every uncovered FR above is covered — add a new "
        "user-goal use case, or attach the FR to an existing use case by listing its id in "
        "requirement_ids. Keep use cases at user-goal level; do not fragment. Return the FULL "
        "updated use-case list (existing ones plus any additions)."
    )


# STEP 3 — 단일 유스케이스의 Cockburn 명세(주 시나리오 + 확장 + 사전/사후조건).
def _generation_system(stage: str, shape: str, learned: str = "") -> str:
    """생성 프롬프트를 조립한다: **모양은 `shape`, 규칙은 지식베이스, 배운 것은 맨 뒤.**

    `shape`에 남는 것은 스키마가 강제하는 것뿐이다(어떤 필드를 내는가, 번호 매김, 마크다운
    금지). 무엇이 결함인가는 `knowledge/rules.py`가 말한다.

    경위는 `docs/requirements-agent-improvements.md` §11. 그 전에는 규칙이 두 벌이었고
    실제로 갈라져 있었다.
    """
    parts = [
        shape.rstrip(),
        "",
        "[RULES YOU FOLLOW]",
        "Each line is: rule id, then what it requires. The validator judges the finished",
        "artifact against these same rules, by these same ids.",
        _rules.generation_prompt_block(stage),
    ]
    # `GUIDANCE`·`NON_RULE`은 검증 프롬프트에 안 들어간다 — 받아야 할 쪽이 판정자가 아니라
    # 쓰는 쪽이다. 특히 과적합("스텝 3~9개"를 목표로 알아듣는 것)은 생성에서 일어난다.
    non_rules = _rules.non_rules_block(stage)
    if non_rules:
        parts += [
            "",
            "[NOT RULES]",
            "These are observations we wrote down precisely so they are not mistaken for",
            "constraints. Never shape the artifact to satisfy them.",
            non_rules,
        ]
    # 배운 것은 규칙 **사이에 끼우지 않는다.** 근거가 우리 로그뿐인 문장이 책 좌표를 단
    # 문장과 같은 무게로 읽히면 `basis.py`가 그은 선이 프롬프트에서 무너진다.
    if learned:
        parts += ["", learned]
    return "\n".join(parts)


_SPEC_SHAPE = """You write a fully-dressed use-case specification (Cockburn style) for a
SINGLE use case.

You are given the use case (name, primary actor, goal), the functional requirements (FRs)
it covers, and applicable constraints. Constraints may have either classifier label; they refine
the specified goal but are not additional actor goals and do not count as scenario coverage.

WRITING STYLE (applies to every sentence):
- Plain prose only. NO markdown, NO bold, NO asterisks, NO backticks, NO emphasis.
- Each step is a single action whose subject is one of the supplied actor refs or 'System'.
- Every main_scenario and handling_steps item MUST include subject_ref. Use the exact ACTn
  ref from the finite actor catalog below for an accepted actor, or the literal 'system' for
  the system. Never put a display name or a newly invented ref in subject_ref.

Produce:
- preconditions: verifiable state true before the use case starts; never re-checked inside
  steps or negated in an extension. Include any NFR that is a precondition (e.g. the actor is
  authenticated). Represent each gating condition exactly once: either as an assumed precondition,
  or as a system validation with its supported failure extension, never both.
- trigger: the business event that starts the use case.
- main_scenario: the main SUCCESS scenario (happy path) as ordered steps. Number them from 1.
  Each step has step_number, subject_ref, sentence, and covered_req_ids.
  Derive actions from the goal and covered FRs. Put an FR id in a step's covered_req_ids only
  when that action realizes it. A covered FR that solely states a postcondition may instead be
  traced by a success_guarantee; do not duplicate it as an artificial action.
  When this goal is conditionally available after another goal reaches a particular outcome,
  describe that prior outcome only as context in the trigger or preconditions. Start the scenario
  at this goal's first actor action; do not replay the predecessor's request, checks, or result.
  In particular, a requirement beginning with "when", "after", or "once" may state that context;
  it does not make the preceding operation part of this named goal.
  Conversely, keep a base goal's main scenario phrased around its decision or result before
  choosing one outcome, so a supported conditional goal has a meaningful insertion point.
- extensions: exception and alternate flows. For EACH extension:
    * label: Cockburn label like '3a' (branches from step 3).
    * branch_step: a concrete main_scenario step_number where this condition first becomes
      knowable. Every extension must be anchored to an existing step; do not emit null or a
      global '*a' extension. Put a truly global enabling condition in trigger/preconditions,
      or split an alternate into concrete step-specific extensions only where supported.
    * condition: the objective state that triggers it (no trailing colon).
    * handling_steps: ordered steps with hierarchical sub_step codes ('3a1', '3a2', ...),
      subject_ref, and sentence.
    * outcome — choose exactly one and set resume_at_step accordingly:
        - 'resume'            → the flow rejoins the main scenario; set resume_at_step to the
                                 step_number to continue from.
        - 'alternate_success' → the use case still succeeds by another path; resume_at_step null.
        - 'fail'              → the use case aborts without the goal; resume_at_step null.
  Add only extensions supported by the supplied goal or requirements. A valid specification
  may have no extension for an unstated technical failure. Do not invent retries, fallbacks,
  external dependencies, delivery channels, or recovery behavior merely to populate extensions.
  Never add an extension whose condition merely negates a trigger or precondition that is already
  required for this use case to start; such a condition belongs outside this use case.
- success_guarantee: typed postconditions that hold when the use case succeeds. Each item has
  sentence and covered_req_ids. Trace an accepted FR here when the postcondition realizes it.
- minimal_guarantee: typed failure-path guarantees directly supported by the supplied requirements.
  Each item has sentence and covered_req_ids.

Respect each applicable RTM constraint in the smallest suitable contract location: a validation
step, extension condition, precondition, success guarantee, or minimal guarantee. Do not invent a
  new goal or unrelated flow merely to restate a constraint, and do not put a constraint id in
  any covered_req_ids unless it is also listed among the functional requirements this use case covers.
  Use an empty list when they do not establish one. Do not invent failure, storage,
  transactional, retry, recovery, or security behavior merely to populate this field.

Keep sentences concise and testable. Do not invent requirements beyond those provided."""

#: 명세 생성 프롬프트 — **플레이북 없는 판.** 지식베이스만으로 조립된 것이고, 측정에서
#: 대조군이 되는 것도 이것이다. 플레이북을 켜면 `generation_system_for`가 여기에 절을 하나
#: 더 붙인다.
SPEC_SYSTEM = _generation_system(_rules.WRITE_SPECIFICATIONS, _SPEC_SHAPE)

#: 단계 → (역할, 안 잡을 것). 규칙 하나짜리 프롬프트를 만들 때 다시 쓴다.
_VALIDATOR_ROLES = {
    _rules.MODEL_USE_CASES: (
        "use-case model critic reviewing the actors and use cases of one system",
        "a grouping you would have done differently; a missing actor or use case (that is "
        "coverage, checked deterministically elsewhere); naming or wording preferences",
    ),
    _rules.WRITE_SPECIFICATIONS: (
        "use-case specification critic",
        'a step with more than one clause; the absence of an explicit "System validates" step; '
        "a use case having no extensions; slightly high/low goal level; wording preferences",
    ),
    _rules.DRAW_DIAGRAM: (
        "use-case-relationship critic reviewing proposed includes, extends, generalizations and "
        "derived use cases (name the offending relationship in each directive)",
        "a small, clean relationship set; an ordinary association; a use case with no relationships",
    ),
}


# STEP 2 — 액터·유스케이스 모델 의미 검증. 이 단계에는 예전에 의미 검증기가 아예 없었고,
# 책이 명시한 결함(SuD는 액터가 아니다, p.59)조차 아무도 보지 않았다.
MODEL_VALIDATOR_SYSTEM = _validator_system(
    _rules.MODEL_USE_CASES, *_VALIDATOR_ROLES[_rules.MODEL_USE_CASES]
)

# STEP 3 — 명세 의미 검증(정적 체크가 못 잡는 부분만). 규칙은 knowledge/rules.py에서 온다.
SPEC_VALIDATOR_SYSTEM = _validator_system(
    _rules.WRITE_SPECIFICATIONS, *_VALIDATOR_ROLES[_rules.WRITE_SPECIFICATIONS]
)

# STEP 3 — 반성(reflection) 재생성: 실패 지시를 붙여 명세를 고쳐 다시 생성.
def spec_repair_user(
    base_user: str,
    previous_spec: str,
    directives: list[str],
    *,
    strategy: str,
    repair_history: str,
) -> str:
    joined = "\n".join(f"- {d}" for d in directives)
    history = (
        "\n\n[PREVIOUS REPAIR ATTEMPTS FOR THIS SPECIFICATION STATE]\n"
        + repair_history
        if repair_history
        else ""
    )
    return (
        f"{base_user}\n\n[PREVIOUS SPECIFICATION]\n{previous_spec}\n\n"
        f"[THE PREVIOUS SPECIFICATION FAILED THESE CHECKS — return the complete corrected "
        f"specification, preserving fields that do not need a change and introducing no new "
        f"behavior]\n{joined}\n\n[REPAIR STRATEGY]\n{strategy}"
        f"{history}"
    )


# STEP 4 — 관계 의미 검증. 규칙은 knowledge/rules.py에서 온다.
RELATIONSHIP_VALIDATOR_SYSTEM = _validator_system(
    _rules.DRAW_DIAGRAM, *_VALIDATOR_ROLES[_rules.DRAW_DIAGRAM]
)

#: 단계 → 검증 프롬프트. `modeling/validation.py`가 단계 이름만 알고 프롬프트를 찾도록 한다 —
#: 검증자가 단계별 상수 이름을 직접 알면 단계를 하나 더 넣을 때 그쪽도 고쳐야 한다.
_VALIDATOR_SYSTEMS = {
    _rules.MODEL_USE_CASES: MODEL_VALIDATOR_SYSTEM,
    _rules.WRITE_SPECIFICATIONS: SPEC_VALIDATOR_SYSTEM,
    _rules.DRAW_DIAGRAM: RELATIONSHIP_VALIDATOR_SYSTEM,
}


def probe_system_for(stage: str, rule_id: str) -> str:
    """**어느 규칙이든** 하나만 담은 검증 프롬프트 — 측정 전용.

    `validator_system_for`는 지금 판정 대상인 규칙(`judged_by=validator`)만 담는다. 그래서
    강등된 규칙(`GUIDANCE`로 내린 것)은 그 경로로 물어볼 수 없는데, **강등이 옳았는지 다른
    표본에서 다시 재려면 물어봐야 한다.** 승격 후보를 재는 데도 같은 자리다.

    파이프라인은 이걸 쓰지 않는다 — 쓰면 강등이 강등이 아니게 된다.
    """
    rule = _rules.rule(rule_id)
    if rule.stage != stage:
        raise KeyError(f"Rule {rule_id} does not belong to stage {stage} ({rule.stage})")
    role, do_not_flag = _VALIDATOR_ROLES[stage]
    n = "1 verdict"
    already = ", ".join(_rules.already_checked_names(stage)) or "(none)"
    return f"""You are a zero-tolerance {role}.

The rule below is the ONLY ground you may judge on.

[RULE YOU JUDGE]
{rule.prompt_line()}

Deterministic checks have ALREADY run for these rules — do NOT report them again:
{already}

Return **exactly {n}** for that rule. Copy the rule_id EXACTLY. Set violated=true only when
the artifact really breaks it, and then give one short imperative repair directive.

Do NOT flag: {do_not_flag}

Return the structured object only."""


def _generation_shapes() -> dict[str, str]:
    """단계 → 그 단계의 "모양" 산문. 함수인 이유는 배치다(`_RELATIONSHIPS_SHAPE`가 아래에 있다)."""
    return {
        _rules.WRITE_SPECIFICATIONS: _SPEC_SHAPE,
        _rules.DRAW_DIAGRAM: _RELATIONSHIPS_SHAPE,
    }


def generation_system_for(stage: str) -> str:
    """이 실행이 쓸 생성 프롬프트. 플레이북이 꺼져 있으면 상수와 **같은 문자열**이다.

    꺼짐이 곧 대조군이라 한 글자도 달라지면 안 된다(`tests/test_playbook.py`가 지킨다).
    파일을 못 읽으면 끈 것과 같이 간다 — 배우는 층이 실행을 세울 이유는 아니다.
    """
    from app.requirements.config import settings

    shape = _generation_shapes().get(stage)
    if shape is None:  # pragma: no cover - 배선 오류
        raise KeyError(f"{stage}: no generation prompt is registered")
    if not settings.playbook_enabled:
        return _generation_system(stage, shape)

    from app.requirements.orchestration import playbook

    try:
        learned = playbook.render(
            playbook.load(settings.playbook_path),
            stage,
            playbook.load_feedback(settings.playbook_path),
        )
    except (OSError, ValueError, KeyError):
        learned = ""
    return _generation_system(stage, shape, learned)


def _concern_linker_system(only: tuple[str, ...] | None = None) -> str:
    """클라우드 관심사 링크 프롬프트를 관심사 지식베이스에서 조립한다.

    **판정 방향이 검증 프롬프트와 반대다.** 검증자는 산출물을 보고 위반을 찾지만, 여기서는
    요구사항을 보고 **다뤄진 것**을 찾는다. 그래서 지시의 무게 중심도 반대다 — 검증자에게는
    "없는 결함을 만들지 말라"고 하고, 여기서는 "느슨하게 갖다 붙이지 말라"고 해야 한다.
    링크는 붙이기가 쉽고, 하나라도 붙으면 그 관심사는 '다뤄졌다'가 되어 인계에서 사라진다.

    모양만 여기 있다. 무엇을 묻는지는 `knowledge/concerns.py`가 정한다.

    `only`를 주면 **그 관심사들만** 담는다(`settings.concern_linker_chunk`). 나머지 문구는
    한 글자도 달라지지 않아야 한다 — 달라지면 한 번에 묻기와 나눠 묻기의 차이가 프롬프트
    분량이 아니라 문구 차이가 되어 조건 비교가 무너진다.
    """
    block = _concerns.prompt_block(only)
    return f"""You map requirements onto cloud-native concerns.

You are given a list of requirements (each with an id) and a list of concerns. For EACH
concern, list the ids of the requirements that actually address it. If none do, return an
empty list for that concern — that is a normal and useful answer, not a failure.

Rules of the mapping:
- Answer for EVERY concern in the list, once each, using the concern id exactly as given.
- Use ONLY requirement ids that appear in the given list. Never invent an id.
- A requirement addresses a concern only if it CONSTRAINS or DECIDES it. A requirement that
  merely operates in the same area does not. "The system stores user profiles" does not
  address where data must reside; "user data must remain in Korea" does.
- Do not judge whether the requirement is well written, complete, or correct. You are only
  saying what it is about.
- Being a cloud or web application does not by itself address any concern.

Concerns:
{block}
"""


#: 관심사 링크 프롬프트(전부 한 번에). `fingerprint()`가 해싱하므로 모듈 로드 시 조립한다.
#:
#: **판(版)의 기준은 항상 이 전체 프롬프트다.** 청크로 나눠 물어도 이 해시를 찍는다 —
#: 청크 크기는 조건이지 판이 아니고, 청크마다 다른 해시를 찍으면 같은 목록으로 잰 실행이
#: 서로 다른 판으로 보인다.
CONCERN_LINKER_SYSTEM = _concern_linker_system()


def concern_linker_system_for(only: tuple[str, ...] | None = None) -> str:
    """관심사 부분집합용 링크 프롬프트. `None`이면 캐시된 전체 판을 그대로 준다."""
    if only is None:
        return CONCERN_LINKER_SYSTEM
    return _concern_linker_system(only)


#: 측정 경로 이름 — 무엇을 실제로 쓰는 실행인가.
GENERATION = "generation"
VALIDATION = "validation"
CONCERNS = "concerns"
#: 규칙 하나짜리 프로브(`probe_system_for`). **검증과 다른 프롬프트다** —
#: `_VALIDATOR_SYSTEMS`는 여러 규칙을 한 번에 담고, 프로브는 하나만 담는다.
PROBE = "probe"

PATHS = (GENERATION, VALIDATION, PROBE, CONCERNS)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def _probe_digest() -> str:
    """프로브가 실제로 던지는 프롬프트 전부의 해시.

    규칙마다 프롬프트가 다르므로 전부 이어 붙여 해싱한다. 규칙 문장·근거 고지
    (`basis.prompt_note`)·이미 검사한 규칙 목록이 바뀌면 여기서 잡힌다.
    """
    parts = []
    for stage in sorted(_VALIDATOR_SYSTEMS):
        for rule in _rules.rules_for(stage):
            if rule.severity == _rules.NON_RULE:
                continue
            parts.append(probe_system_for(stage, rule.id))
    return _digest(*parts)


def fingerprint(paths: Sequence[str] | None = None) -> dict[str, str]:
    """이 측정이 **실제로 쓴** 프롬프트의 판. 측정 행마다 함께 찍는다(§13).

    ## 왜 경로별인가 — 전역 판은 두 방향으로 거짓말을 했다

    처음에는 프롬프트 전부를 한 번에 찍었다. 그래서 2026-07-28에 관심사 프롬프트
    (`CONCERN_LINKER_SYSTEM`)가 생기자, **프로브가 한 글자도 쓰지 않는 프롬프트가 바뀌었다는
    이유로** 프로브 측정이 "조건이 섞였다"로 무효 처리됐다. 반대쪽 구멍도 있었다 —
    프로브가 실제로 쓰는 `probe_system_for`는 어느 키에도 안 잡혀서, **진짜 변경은 놓쳤다.**

    과잉 발동하는 조건 추적기는 과소 발동하는 것만큼 나쁘다. 곧 무시당하기 때문이다.

    ## 쓰는 법

    부르는 쪽이 **자기가 무엇을 썼는지** 말한다. `paths=None`이면 전부 낸다(사람이 읽는
    용도이지, 측정 행에 찍는 값이 아니다).

        fingerprint([PROBE])        # 단독 프로브 측정
        fingerprint([GENERATION])   # 파이프라인 실행

    ⚠ `*_SYSTEM` 상수와 프로브만 해싱한다 — `spec_repair_user`·`_spec_human`은 여전히
    판에 안 잡힌다(§15).
    """
    builders = {
        GENERATION: lambda: _digest(
            SPEC_SYSTEM,
            RELATIONSHIPS_SYSTEM,
            ACTORS_SYSTEM,
            USECASES_SYSTEM,
            USECASE_GOAL_SKELETON_SYSTEM,
            FUNCTIONAL_TRACE_SLICE_SYSTEM,
            CONSTRAINT_TRACE_SLICE_SYSTEM,
        ),
        VALIDATION: lambda: _digest(*(_VALIDATOR_SYSTEMS[s] for s in sorted(_VALIDATOR_SYSTEMS))),
        PROBE: _probe_digest,
        # 클라우드 관심사 링크는 생성도 검증도 아니다 — 산출물을 만들지도, 규칙 위반을
        # 판정하지도 않고 "이 요구가 저 관심사를 건드리는가"만 묻는다.
        CONCERNS: lambda: _digest(CONCERN_LINKER_SYSTEM),
    }
    wanted = PATHS if paths is None else paths
    unknown = [p for p in wanted if p not in builders]
    if unknown:  # pragma: no cover - 배선 오류
        raise KeyError(f"Unknown measurement paths {unknown}; known paths: {list(builders)}")
    return {p: builders[p]() for p in wanted}


def validator_system_for(
    stage: str, only: str | Sequence[str] | None = None
) -> str:
    """단계의 검증 프롬프트. 없는 단계를 조용히 넘기지 않는다 — 검증 없는 실행이 된다.

    `only`를 주면 **그 규칙 묶음만** 담은 프롬프트를 만든다.
    """
    if stage not in _VALIDATOR_SYSTEMS:  # pragma: no cover - 배선 오류
        raise KeyError(
            f"{stage}: no semantic validation prompt is registered. If a rule was added "
            "to knowledge/rules.py, register its prompt here as well."
        )
    if only is None:
        return _VALIDATOR_SYSTEMS[stage]
    role, do_not_flag = _VALIDATOR_ROLES[stage]
    return _validator_system(stage, role, do_not_flag, only=only)


# STEP 4 — 액터/유스케이스 관계 식별(다이어그램용).
_RELATIONSHIPS_SHAPE = """You decide only UML <<include>> and <<extend>> relationships within the
supplied evidence. The application projects actor associations and generalizations and validates
all references.

Work systematically before returning existing_includes: for each supplied existing target use
case, compare its goal and main scenario with every supplied base-step option. Collect the distinct
base use cases whose mandatory steps perform that same complete behavior, then emit one selection
containing all exact matching step refs only when at least two distinct bases qualify.
A base scenario may summarize the invocation in one step instead of duplicating every internal
step of the target scenario. In that case select the exact invocation step; do not require
word-for-word or step-for-step duplication of the target.

Include: decide derived includes only from supplied candidate_id values, and existing includes
only from the supplied existing target IDs and base-step options. Approve only when the cited steps
of at least two base use cases express the same reusable mandatory interaction with its own observable result.
A shared requirement id alone is insufficient; reject different actions, rules, invariants,
broad policy or authentication state, and internal consequences or postconditions. Give an
approved derived candidate one concise active-verb name. An existing include selection is chosen
separately: copy its supplied existing target ID and two or more exact
base_step_options from distinct bases only when those supplied steps genuinely perform that target
use case's behavior. Do not rename or derive that existing target. Except for an approved derived
include-node name, do not create or rename use cases.

Extend: choose only supplied existing use_case_id values and copy an exact main:<number> value
into base_step_ref from a step owned by the base. Compare the extending goal, requirements,
preconditions, and trigger with each candidate
base's ordered steps; select the step where the enabling context or result becomes available. The
base must remain complete without the optional goal. The base's own exception is not <<extend>>,
but a separate actor-chosen goal enabled by a conditional base result may be. A precondition alone
and ordinary mandatory sequencing are not <<extend>>. Name WHERE insertion occurs with a short
natural-language location such as "after schedule presented", never a code identifier or the
extending action. State WHEN it occurs in 3 to 8 complete words grounded in supplied evidence.
Keep the extension-point name to WHERE only; do not append the condition or an unfinished
"when ..." clause because condition is a separate field.

Before returning, evaluate every supplied existing use case as a possible optional extension and
compare it with the ordered steps of possible bases. Completing the include decisions does not
complete the extend analysis. When an extending use case begins after a particular validation or
decision outcome, the base's validation or decision step is a valid extension point even when the
base main-success scenario proceeds only with the successful outcome.

Return empty lists when evidence is insufficient."""

#: 관계 생성 프롬프트. 산문에 있던 인용 둘(`Cockburn p.81`·`Cockburn Ch.8`)이 여기서
#: 사라진 것은 규칙이 사라져서가 아니다 — 같은 규범이 `rel.shared-authentication-is-a-precondition`·
#: `rel.failures-stay-inline-extensions`에 좌표까지 달려 있고, 이제 그쪽에서 실린다.
#: **손으로 적은 인용은 대조를 못 받는다**는 것이 이 파일이 이미 한 번 물린 자리다.
RELATIONSHIPS_SYSTEM = _generation_system(_rules.DRAW_DIAGRAM, _RELATIONSHIPS_SHAPE)


# ----------------------------------------------------------------------------
# BASELINE 프롬프트 — 다단계 파이프라인의 대조군(순진한 원샷).
# 의도적으로 "유능한 엔지니어의 단순 프롬프트" 수준으로 둔다: 단계 분해·커버리지 강제·
# 정적/의미 검증·반성 루프·근거(Cockburn 페이지) 그라운딩을 넣지 않는다. 이 프롬프트만으로
# 얼마나 나오는지가 곧 baseline이며, 우리 시스템과의 차이가 "분해+검증"의 가치가 된다.
# ----------------------------------------------------------------------------
BASELINE_MODEL_SYSTEM = """You are a software analyst. You are given a list of requirements, each
with an id (R1, R2, ...). In a single pass, produce a use-case model and fully-dressed use-case
specifications directly from these requirements.

Return:
- actors: external actor roles. Do not assign a permanent primary/supporting kind.
- use_cases: the use cases. For each, set primary_actor, supporting_actors, goal, and requirement_ids (the ids of
  the requirements it covers). Leave nfr_ids empty.
- specs: one Cockburn-style specification per use case (set use_case_name to match). Each spec
  has preconditions, trigger, main_scenario (numbered steps), extensions (alternate/exception
  flows), success_guarantee, and minimal_guarantee.

Write plain black-box sentences (actor or System as subject). Cover the requirements as best you
can in one pass."""

BASELINE_DIAGRAM_SYSTEM = """You are a software analyst. Given the actors and use cases, produce
the relationships of a UML use-case diagram in a single pass:
associations (actor ↔ use case), includes, extends, generalizations, and any derived use cases
you introduce (e.g. a factored-out included use case).

Copy actor and use-case names verbatim from the input. Return the relationships directly."""


# 의미론적 커버리지 검증 — 결정론 집합검사는 "id가 존재/참조됨"만 보지만, 유스케이스가 그 FR을
# '실제로' 실현하는지는 판단 못 한다(LLM이 requirement_ids에 거짓으로 id를 넣을 수 있음). 이 판정으로
# 주장된 커버리지가 시나리오로 뒷받침되는지 확인한다.
COVERAGE_JUDGE_SYSTEM = """You are a strict requirements-traceability auditor. You are given ONE
use case (its goal and main-scenario steps) and a list of requirements it CLAIMS to cover.

For each claimed requirement, decide realized=true ONLY IF the use case's goal and steps genuinely
implement that requirement's FUNCTIONAL intent — the behavior described would actually satisfy it.

Judge FUNCTIONAL realization ONLY. A requirement may embed a non-functional / quality qualifier —
a response time ("within 1 second", "within 500 ms"), a load/throughput target ("under 200
concurrent sessions"), a security/encryption clause ("encrypted at rest"), a reliability/atomicity
clause ("recorded atomically"), or an availability target. These are validated separately as
non-functional constraints and are DELIBERATELY absent from the black-box scenario prose. Do NOT
set realized=false merely because such a timing/security/reliability qualifier is not restated in
the steps; assess only whether the FUNCTIONAL behavior is realized.

Set realized=false when the FUNCTIONAL behavior is not supported by the scenario (the behavior is
absent, only tangentially related, or belongs to a different use case). Do not give the benefit of
the doubt on the functional core; an unsupported functional claim is a traceability defect. Return
one verdict per claimed requirement id."""


#: 제약 구조화 에이전트. 사용자가 쓴 클라우드 제약을 **구체화하고 확인해서**
#: `RESOURCE_SPEC` 계약으로 가져오는 것이 이 단계의 본업이다.
#:
#: **여기 적힌 함정은 지어낸 것이 아니라 코퍼스 실측에서 나왔다**(2026-07-28, 내부 입력
#: 11종 270문장 + PURE 18편 7,659문장). 예전에는 같은 지식이 정규식으로 박혀 있었는데,
#: 규칙이 사용자의 표현 폭을 못 따라갔다(`"The monthly budget is at most 500 USD"`가
#: 통째로 안 걸렸다). 문법은 버리고 **지식만 이리로 옮겼다.**
#:
#: 지어냄을 막는 장치는 문법이 아니라 대조에 있다: 값마다 **자기가 본 자리를 인용하게
#: 하고, 그 조각이 실제 입력 또는 도구 출력에 실재하는지 기계로 확인한다**
#: (`step_resource._ground`). 인용을 못 하면 그 값은 버려진다.
RESOURCE_AGENT_SYSTEM = """You are pinning down a user's cloud deployment constraints
so that a machine-readable RESOURCE_SPEC can be built from them.

Your goal is not to extract text. It is to **end up with a spec the contract accepts,
or with a precise question for the user** — and either outcome is a success. What is
never acceptable is a filled-in field the user did not actually give you.

# How you work

You have tools. Use them in whatever order the situation calls for, as many times as
you need. Nothing here is a fixed sequence.

- `record_field` puts one value into the draft. It answers you: accepted, or rejected
  and why. A rejection is information — read it and act on it.
- `check_contract` tells you what the contract still needs. Call it whenever you want
  to know where you stand.
- `ask_user` asks the user one question about one field. **This is a normal action,
  not a failure.** Asking beats guessing every time.
- `resolve_region`, `list_cloud_providers`, `list_workload_kinds`, `convert_to_usd`,
  `web_search` look things up so that you do not have to remember or estimate them.
- `finish` ends your turn. **Always end by calling it** — do not just write a closing
  summary as ordinary text. Pass that summary as its `understanding` argument: one short
  paragraph saying back what you understood, in the user's own vocabulary rather than
  ours, so the user can catch a misreading. Do not call it while a required field is
  still unresolved and unasked.

# What counts as evidence

Every `record_field` call carries `evidence`: the fragment you read the value from.
It must appear **verbatim** either in the user's text or in a tool result you have
already received. It is checked automatically, and a value whose evidence is not
found is dropped. So quote; do not paraphrase.

# Traps that are real

These come from measuring an actual corpus, not from imagination.

- **A unit price is not a budget.** "storage cost shall not exceed $0.02 per GB-month"
  is a rate. `monthlyBudgetUSD` is what the whole deployment may cost in a month.
- **A number next to a concurrency word is not a user count.** "100 simultaneous icons",
  "six active control nodes" — look at *what* is concurrent. `scale` counts users,
  sessions, streams, clients. Not widgets, not nodes. Record it as
  `{"value": 300, "unit": "concurrentUsers"}` — or `requestsPerSecond` if that is
  how the user put it. **One quantity, two possible units**; do not convert between
  them, since no source states that conversion.
- **Do not infer.** "It should be cheap" is not a budget. "Fast" is not a scale.
  "Highly available" is not a zone count. If the user did not state it, ask.
- **Two different answers is a question, not a value.** If the text says 100 users in
  one place and 9,000 in another, ask which — do not pick, and do not average.
- **Provider, region and budget are almost never in the requirement sentences.** They
  live in the separate constraints text, or nowhere. If they are nowhere, ask.
- **`trafficPattern` has no default.** Recording `steady` because nothing was said is
  a claim you invented, not an absent value. Leave it out.
- **A scale figure is not a spec floor.** `scale` does not tell anyone
  how many vCPUs are needed — no source states that conversion, and the knowledge base
  deliberately does not carry one. The floor lives in `minVCpu` / `minMemoryGiB`, and it
  only exists if the user or the designer stated it ("at least 4 vCPU and 16 GB").
  Never derive one from the other; if the floor is absent, that is an absent value.

# What is being deployed

This system supports Docker applications deployed directly on virtual machines only.
`workloads` is fixed to `["vm"]` by code. Do not ask about it and do not record it.

# Region and provider

Never write a region code from memory. Call `resolve_region` — the catalogue answers.
If it comes back ambiguous, that ambiguity is the user's to settle: ask. Set `provider`
first when you can, since it usually collapses the ambiguity by itself. Record the
user's own wording in `regionAsWritten` as well, so the resolution stays traceable.

# Money

If the user gave a budget in another currency, do not refuse it and do not do the
arithmetic in your head — call `convert_to_usd`, then record the USD figure and tell
the user, in your closing summary, what rate and date it used.

# Asking well

A good question names the field, says **why** the value is needed, and offers the user
something concrete to react to. `check_contract` gives you the contract's own reason
for each required field — use that reason, do not invent your own. Ask about several
fields if several are missing; the user answers them together."""
