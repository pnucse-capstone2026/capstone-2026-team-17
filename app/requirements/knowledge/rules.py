"""규칙 지식베이스 — 이 에이전트가 무엇을 결함이라 부르는지, 그 근거는 무엇인지.

## 무엇이 없어서 이게 필요했나

규칙이 세 군데에 흩어져 있었다.

  - `prompts.py`의 시스템 프롬프트 산문 — LLM만 읽는다
  - `step3_specifications.py`의 정규식·상수 — 코드만 읽는다
  - `docs/ARCHITECTURE.md` §4.2의 페이지 인용 — 사람만 읽는다

같은 규칙이 세 곳에 다른 형태로 적혀 있으면 갈라진다. 실제로 갈라졌다: UI 단어 목록이
"예시일 뿐 완전목록이 아니다"라는 사실은 코드 주석에만 있었고, **검증 지적에는 실리지
않았다.** 사용자는 `p.81`(책이 그렇게 적었다)과 UI 단어 8개(우리가 예시에서 일반화했다)를
같은 무게로 읽는다.

그래서 규칙을 데이터로 한 곳에 모은다. 프롬프트도 검출기도 리포트도 여기서 파생된다.

## 책 본문은 여기 없다

Cockburn의 *Writing Effective Use Cases*는 저작물이고 저장소에서 지워졌다(`d1a7ec5`,
filter-branch로 히스토리 전체에서). 그래서 이 파일에 담는 것은 **우리 표현의 규범 문장 +
인용 좌표 + 좌표를 대조할 짧은 열쇠 단어**이고, 본문은 담지 않는다.

인용은 손으로 옮겨 적는 것이라 틀린다. 그래서 `pages`·`probe`로 **기계가 대조할 수 있게**
해 두고, 로컬 사본을 가진 사람이 `verify_citations`로 확인한다. 2026-07-26에 그 검사가
틀린 인용 둘을 잡았다(`p.64`·`p.207` — 그 모듈 docstring 참고).

## 심각도가 셋인 이유

`NON_RULE`이 있는 것이 핵심이다. "스텝 3~9개"와 include 힌트 상한은 **규칙이 아니라는
사실 자체가 지켜야 할 지식**이다. 적어 두지 않으면 다음 사람이 관찰을 규칙으로
승격시킨다 — `docs/ARCHITECTURE.md`가 "임의 사전 금지(오버피팅 방지)"로 경계한 것이
정확히 그것이고, 그 경계가 지금까지 주석에만 있었다.

## 아직 없는 것

  - `refine_requirements` 단계의 규칙이 하나도 없다. 구체성 rubric 조사가 저장소 밖으로
    나갔고(`e10c527`), 근거 없는 규칙을 지어 넣지는 않는다.
  - `refine_requirements` 단계 말고도, **`GUIDANCE` 규칙 6개는 여전히 아무도 판정하지
    않는다.** 생성 프롬프트에 실리기는 하지만(2026-07-27), 지켜졌는지는 재고 있지 않다.

## 생성 프롬프트도 여기서 조립한다 (2026-07-27)

한동안 검증 프롬프트만 이 지식베이스에서 조립했고, 생성 프롬프트는 산문 그대로였다. 미룬
이유는 "회귀를 잡을 평가 세트가 먼저 있어야 한다"였는데 §5에서 생겼으므로 옮겼다.

옮기면서 드러난 것이 미룬 값을 넘었다 — **둘은 이미 갈라져 있었다.** 생성 산문은
`no protocols (HTTP/SQL)`을 금지했는데 그런 규칙이 없었고(오히려
`spec.black-box-no-internal-components`의 경계 (d)가 위반이 아니라고 적어 둔다),
`GUIDANCE` 규칙은 어느 프롬프트에도 실리지 않고 있었다. `docs/requirements-agent-improvements.md`
§11 참고.

나눈 선: **모양은 산문(`prompts.py`), 규칙은 여기.** 어떤 필드를 내는가·번호를 어떻게
매기는가는 스키마가 강제하는 것이라 규칙이 아니다.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.requirements.knowledge import basis

# --- 심각도 -----------------------------------------------------------------
#: 위반이면 검증이 지적한다.
DEFECT = "defect"
#: 생성 쪽 지침. 위반을 지적하지는 않는다(판정 기준이 되기에는 무르다).
GUIDANCE = "guidance"
#: **규칙이 아니라는 기록.** 어디에서도 강제하지 않는다. 관찰이나 공학적 가드를
#: 규칙으로 승격시키지 않기 위해 둔다.
NON_RULE = "non_rule"

SEVERITIES = (DEFECT, GUIDANCE, NON_RULE)

# --- 누가 판정하는가 ---------------------------------------------------------
# `DEFECT`인데 판정하는 곳이 없는 규칙이 실제로 있다. 그 사실을 `None` 하나로 뭉개면
# "LLM이 판정한다"와 "아무도 판정하지 않는다"가 같은 값이 된다 — 검증 프롬프트를 이
# 목록에서 조립하는 순간 후자가 전자로 조용히 승격한다.
#: `knowledge/detectors.py`의 결정론 검출기.
JUDGED_DETECTOR = "detector"
#: LLM 의미 검증자(검증 프롬프트에 규칙 문장이 들어간다).
JUDGED_VALIDATOR = "validator"
#: 단계 노드가 직접 결정론으로 구현한다(커버리지·참조 드롭).
JUDGED_STAGE = "stage_code"
#: **판정하는 곳이 없다.** 규칙은 적혀 있고 검증은 없다는 사실을 드러낸다.
JUDGED_NOWHERE = "nowhere"

JUDGES = (JUDGED_DETECTOR, JUDGED_VALIDATOR, JUDGED_STAGE, JUDGED_NOWHERE)

# --- 단계 -------------------------------------------------------------------
# 이름은 `stage_registry.py`의 `group`과 같다. import하지 않는 이유는 순환이다
# (stages → steps → knowledge). 두 목록이 맞는지는 테스트가 확인한다.
MODEL_USE_CASES = "model_use_cases"
WRITE_SPECIFICATIONS = "write_specifications"
DRAW_DIAGRAM = "draw_diagram"

_BOOK = "Writing Effective Use Cases"


@dataclass(frozen=True)
class Rule:
    """규칙 하나. **규범 문장은 우리 표현이고, 인용은 좌표다**(본문 아님)."""

    id: str
    stage: str
    severity: str
    #: 규범 문장. 검증 프롬프트에 그대로 들어가므로 영어로 쓴다.
    statement: str
    #: 확인 좌표. `Writing Effective Use Cases, p.64` 처럼.
    citation: str
    #: 근거 라벨(`basis.BASIS_OF_EVIDENCE`에 등록돼야 한다).
    evidence: str
    #: 짐작인 규칙은 반드시 있어야 한다 — **출처의 한계**를 적는다(위반의 의심이 아니다).
    caveat: str | None = None
    #: 이 규칙을 판정하는 곳. `DEFECT`는 반드시 밝힌다(없으면 `JUDGED_NOWHERE`).
    judged_by: str = JUDGED_NOWHERE
    #: 결정론 검출기 이름(`detectors.py`에 등록). `judged_by`가 검출기일 때만 있다.
    detector: str | None = None
    #: 이 결함을 **낸 단계**의 논리 이름(`stage_registry.py`의 `key`). 되돌릴 대상이다.
    #:
    #: `stage`(그룹)와 다른 사실이다. `model_use_cases` 그룹에는 되돌릴 수 있는 단계가
    #: 둘(actors·use_cases) 있고, 어느 쪽이 낸 결함인지는 규칙마다 다르다. 규칙 id 접두사로
    #: 짐작할 수도 있지만, 라우팅 표를 접두사 규약에 걸어 두면 이름을 바꾸는 순간 조용히
    #: 어긋난다. `DEFECT`만 갖는다 — 나머지는 되돌릴 대상이 아니다.
    owner: str | None = None
    #: 인용을 **기계로 확인할** 인쇄 페이지 번호. 책 근거인 규칙에만 있다.
    pages: tuple[int, ...] = ()
    #: 그 페이지에 있어야 하는 짧은 단어들(소문자). 좌표가 맞는지 보는 열쇠일 뿐,
    #: 본문을 옮겨 담는 자리가 아니다 — 저작물을 저장소에 넣지 않기 위한 경계다.
    probe: tuple[str, ...] = ()
    #: **생성 쪽에만** 주는 보조 문구 — 예시, 쓰는 법. 규범이 아니다.
    #:
    #: 규범은 `statement` 하나뿐이다. 여기에 새 제약을 적으면 아무도 판정하지 않는 규칙이
    #: 조용히 생긴다(그 사실을 드러내려고 `JUDGED_NOWHERE`를 둔 것인데, 이 자리는 그
    #: 표시를 우회한다). 그래서 여기 적을 수 있는 것은 `statement`가 이미 말한 것을
    #: 다시 보여 주는 것뿐이다.
    #:
    #: **검증 프롬프트에는 넣지 않는다.** 두 가지 이유다: 예시를 판정자에게 주면 규범이
    #: 아니라 예시에 맞는지를 보게 되고, 판정 프롬프트가 길어지면 그 규칙만이 아니라
    #: 응답 전체가 흔들린다(§9 — 규칙 6개를 한 프롬프트에 넣으면 안정 판정이 0이 됐다).
    #: 쓰는 쪽과 판정하는 쪽은 같은 **규범**을 받아야 하지만 같은 **재료**를 받을 필요는 없다.
    generation_note: str | None = None

    @property
    def hedged(self) -> bool:
        """지적할 때 출처의 한계를 함께 밝혀야 하는가."""
        return basis.needs_hedge(self.evidence)

    @property
    def from_book(self) -> bool:
        """인용이 도서 좌표인가(→ 로컬 사본으로 대조할 수 있어야 한다)."""
        return self.evidence.startswith("cockburn-")

    @property
    def short_citation(self) -> str:
        """책 제목을 뗀 좌표(`p.64`). 지적 문구에 붙이기 위한 것."""
        return self.citation.removeprefix(f"{_BOOK}, ")

    @property
    def tag(self) -> str:
        """지적 문구 꼬리표. 짐작인 규칙은 그 사실이 함께 붙는다."""
        parts = [self.id, self.short_citation]
        if self.hedged:
            parts.append("project inference")
        return f"[{' · '.join(parts)}]"

    def prompt_line(self) -> str:
        """검증 프롬프트 한 줄. 근거의 성격까지 모델에게 알린다.

        고지 문구는 라벨마다 다르다(`basis.prompt_note`) — "책이 말한 적 없다"와 "책의
        원칙이지만 페이지를 못 댄다"를 한 문구로 뭉개면 둘 중 하나는 거짓이 된다.
        """
        note = basis.prompt_note(self.evidence)
        source = f"{self.citation} — {note}" if note else self.citation
        return f"- {self.id} ({source}): {self.statement}"


# ---------------------------------------------------------------------------
# 규칙 목록
# ---------------------------------------------------------------------------
RULES: tuple[Rule, ...] = (
    # --- 2단계: 액터 / 유스케이스 ---------------------------------------
    Rule(
        id="actors.sud-is-not-an-actor",
        stage=MODEL_USE_CASES,
        severity=DEFECT,
        statement=(
            "The system under design is never an actor. Actors are external to it — "
            "primary actors initiate, supporting actors are called upon."
        ),
        citation=f"{_BOOK}, p.59 (Ch. 4, Stakeholders and Actors)",
        evidence="cockburn-page",
        owner="actors",
        judged_by=JUDGED_VALIDATOR,
        pages=(59,),
        probe=("actor", "system under design"),
    ),
    Rule(
        id="actors.source-grounded-external-roles",
        stage=MODEL_USE_CASES,
        severity=GUIDANCE,
        statement=(
            "Derive an external actor only from an accepted actor-goal statement or an "
            "explicit role/domain fact, regardless of its FR/NFR classifier label. Ordinary "
            "quality and deployment constraints do not create actors."
        ),
        citation="easydep convention (source-grounded actor projection)",
        evidence="project-convention",
        caveat="우리 규약이다 — 책이 액터의 출처를 요구사항 유형으로 제한한 것은 아니다.",
    ),
    Rule(
        id="usecases.user-goal-level",
        stage=MODEL_USE_CASES,
        severity=GUIDANCE,
        statement=(
            "Cluster use cases at the user-goal (elementary business process) level; keep "
            "lifecycle operations with the same actor, business object, and responsibility "
            "together unless the requirements establish distinct triggers or outcomes, and "
            "absorb subfunction requirements into the use case that covers them."
        ),
        citation=f"{_BOOK}, p.62 (Ch. 5, Three Named Goal Levels)",
        evidence="cockburn-page",
        pages=(62,),
        probe=("user goal",),
    ),
    Rule(
        id="usecases.goal-source-grounded",
        stage=MODEL_USE_CASES,
        severity=DEFECT,
        statement=(
            "Every use-case name, goal qualifier, and actor-goal boundary must be supported "
            "by its linked functional requirements and accepted actor facts. Do not add a "
            "lifecycle state, prerequisite, outcome, or domain convention merely because it "
            "would be plausible for that operation. A linked requirement may legitimately be "
            "an absorbed subfunction, consequence, or postcondition of the same goal; do not "
            "demand its removal when that trace path is explicit."
        ),
        citation="easydep convention (source-grounded use-case projection)",
        evidence="project-convention",
        caveat="요구사항에서 유스케이스 경계를 투영할 때 환각을 막기 위한 우리 규약이다.",
        owner="use_cases",
        judged_by=JUDGED_VALIDATOR,
    ),
    Rule(
        id="usecases.constraint-is-not-a-goal",
        stage=MODEL_USE_CASES,
        severity=GUIDANCE,
        statement=(
            "A policy, invariant, precondition, postcondition, or quality constraint is not "
            "an independently initiated actor goal, regardless of its FR/NFR classifier label. "
            "Record it as a constrains edge only for explicitly identified existing operations; "
            "a system-wide or ambiguous constraint may remain global."
        ),
        citation="easydep convention",
        evidence="project-convention",
        caveat="우리 규약이다. 제약의 적용 범위를 FR/NFR 분류로 정하지 않는다.",
    ),
    Rule(
        id="usecases.every-functional-requirement-covered",
        stage=MODEL_USE_CASES,
        severity=DEFECT,
        statement=(
            "Every functional requirement that explicitly states an independently initiated "
            "actor goal must be covered by a use case, and every requirement id referenced "
            "must exist. A requirement that only states a policy, invariant, or execution "
            "constraint must remain traceable without becoming a source or derived use case, "
            "regardless of its classifier label."
        ),
        citation="easydep convention (traceability)",
        evidence="project-convention",
        caveat="추적성을 위해 우리가 정한 규칙이다. 실제 actor goal 여부는 모델 검토가 판단한다.",
        owner="use_cases",
        judged_by=JUDGED_VALIDATOR,
    ),
    Rule(
        id="deployment-needs.grounded-without-design-inference",
        stage=MODEL_USE_CASES,
        severity=GUIDANCE,
        statement=(
            "Every deployment need, role, and metadata value must be directly supported "
            "by its referenced requirement text. An allowed simplification is not a "
            "required topology choice: for example, 'high availability is not required' "
            "does not imply one instance, no replication, or no failover."
        ),
        citation="easydep convention (requirements-to-deployment boundary)",
        evidence="project-convention",
        caveat="우리 시스템의 요구사항-설계 경계 규약이다.",
        judged_by=JUDGED_VALIDATOR,
    ),
    Rule(
        id="deployment-needs.generic-capability-not-resource-selection",
        stage=MODEL_USE_CASES,
        severity=GUIDANCE,
        statement=(
            "Deployment-need identifiers and roles describe generic capabilities or "
            "constraints. They must not select CSP products, instance types, VM counts, "
            "disks, load balancers, or other concrete infrastructure designs."
        ),
        citation="easydep project scope (requirements/design separation)",
        evidence="project-convention",
        caveat="우리 시스템의 요구사항-설계 경계 규약이다.",
        judged_by=JUDGED_VALIDATOR,
    ),
    # --- 3단계: 명세 ------------------------------------------------------
    Rule(
        id="spec.black-box-no-ui-mechanics",
        stage=WRITE_SPECIFICATIONS,
        severity=DEFECT,
        statement=(
            "Steps must not name user-interface mechanics (screens, fields, buttons, "
            "clicks, tabs). Say what the system does for the actor."
        ),
        citation=f'{_BOOK}, p.209 (Reminder 7: "Keep the GUI Out")',
        evidence="cockburn-example",
        caveat=(
            "단어 목록은 그가 **예로 든** UI 용어일 뿐이다. 책은 금지 단어목록을 "
            "명문화하지 않았으므로 이 목록은 완전하지 않다 — 없는 단어로도 위반할 수 있다."
        ),
        owner="specs",
        judged_by=JUDGED_DETECTOR,
        detector="ui_terms",
        pages=(209,),
        probe=("reminder 7", "gui"),
    ),
    Rule(
        id="spec.black-box-no-internal-components",
        stage=WRITE_SPECIFICATIONS,
        severity=DEFECT,
        statement=(
            "Say what the system does for the actor, not which internal part does it. It is a "
            "violation to name a specific internal part as the actor of a step or as the place "
            "data goes: a named service ('the login service'), engine, cache, queue, or store "
            "('records it in an audit log', 'in an immutable store').\n"
            "  It is NOT a violation when: (a) the given requirements themselves name that "
            "component — repeating it is requirement coverage, not leakage; (b) the reference is "
            "to the system as a whole, unqualified ('the service is unavailable'); (c) the step "
            "only says that something is logged, recorded or stored, without naming where "
            "('System records the order'); (d) the sentence names a technology or protocol "
            "mechanism rather than a part (tokens, cipher suites, licences) — that is a "
            "different concern."
        ),
        citation=f"{_BOOK}, p.41 (Ch. 3, Scope — black box)",
        evidence="cockburn-extrapolated",
        caveat=(
            "black-box 원칙과 페이지는 확인했다. 다만 **어디까지가 '내부 부품'인지는 우리가 "
            "정했다** — 위 (a)~(d) 경계 넷은 책에 없다.\n"
            "그 넷은 2026-07-27에 라벨을 붙이다 정해졌다. 독립 평가자(fable-5)와 30건을 각자 "
            "판정했더니 28/30이 일치했고, 갈린 2건이 모두 '요구사항이 그 부품 이름을 이미 "
            "부르는' 경우였다 → (a). 나머지 셋은 그 평가자가 '규칙이 답을 안 정해 준다'고 "
            "지적한 자리다: 맨 'the service'(→b), `logs/records/stores` 어법(→c, 표본의 약 "
            "1/3에 나온다), JWT·RSA-256·DRM 같은 기전(→d). "
            "판정이 흔들린 원인의 일부가 모델이 아니라 **이 문장이 회색지대를 남긴 것**이었다."
        ),
        owner="specs",
        judged_by=JUDGED_VALIDATOR,
        pages=(41,),
        probe=("black box",),
        generation_note=(
            "Write \"System records the order\" (not \"saves the order to the order store\"), "
            "\"System retrieves the toy list\" (not \"queries the catalog service\"), "
            "\"System confirms the member's credentials\" (not \"checks the credential store\")."
        ),
    ),
    Rule(
        # 생성 프롬프트가 산문으로 "no protocols (HTTP/SQL)"라고 금지하고 있었는데, **그 금지에
        # 대응하는 규칙이 없었다.** 게다가 바로 위 규칙의 경계 (d)는 프로토콜·기전을 부르는 것이
        # black-box 위반이 *아니라고* 적어 둔다 — 생성과 검증이 이미 갈라져 있었던 자리다.
        #
        # 갈라짐을 어느 쪽으로든 조용히 정리할 수 있었다(산문을 지우거나, DEFECT로 올리거나).
        # 둘 다 안 한다: 지우면 지금 동작이 근거 없이 바뀌고, 올리면 판정할 수 없는 결함이
        # 하나 늘어난다. 그래서 **있는 그대로** 적는다 — 생성 쪽 선호이고, 아무도 판정하지 않고,
        # 근거는 책이 아니라 우리다.
        id="spec.no-protocol-mechanics",
        stage=WRITE_SPECIFICATIONS,
        severity=GUIDANCE,
        statement=(
            "Prefer business-level wording over transport and query mechanics (HTTP, REST "
            "calls, SQL) when naming what happens in a step."
        ),
        citation="easydep 규약",
        evidence="project-convention",
        caveat=(
            "책 근거가 없다. 위 `spec.black-box-no-internal-components`의 경계 (d)는 "
            "프로토콜 언급을 black-box 위반으로 **보지 않는다** — 그래서 이것은 결함이 아니라 "
            "생성 쪽 선호로만 둔다. 지적하지 않으므로 이 문장이 지켜지는지는 재고 있지 않다."
        ),
        judged_by=JUDGED_NOWHERE,
    ),
    Rule(
        id="spec.no-branching-in-a-step",
        stage=WRITE_SPECIFICATIONS,
        severity=DEFECT,
        statement=(
            "A step states one thing that happens. Branching words (if/else) do not "
            "belong in a step — split the branch into an extension."
        ),
        citation=f"{_BOOK}, Ch. 7 (Scenarios and Steps), p.88~",
        evidence="cockburn-chapter",
        owner="specs",
        judged_by=JUDGED_DETECTOR,
        detector="branch_words",
        pages=(88,),
        probe=("scenarios and steps",),
    ),
    # 이 규칙은 하루 안에 강등됐다가 복구됐다. 그 경위가 측정 방법에 대한 교훈이다:
    #   1. `toystore` 한 데이터셋에서 "안정된 판정 0건"이 나와 GUIDANCE로 내렸다.
    #   2. 도메인 5종에서 **단독 프로브**로 다시 재니 안정 4 / 흔들림 15 / 없음 6이었다.
    # 조합 프롬프트(규칙 6개를 한 번에)에서는 신호가 묻히고, 단독으로 물으면 나온다.
    # **"신호가 없다"는 주장은 측정 조건을 함께 말해야 한다** — 안 그러면 프롬프트 구성을
    # 규칙의 성질로 착각한다. (`docs/requirements-agent-improvements.md` §9)
    Rule(
        id="spec.no-hidden-branching",
        stage=WRITE_SPECIFICATIONS,
        severity=DEFECT,
        statement=(
            "A step whose behaviour depends on an unstated outcome is still a branch, "
            "even without the word 'if'. Split it into a separate extension."
        ),
        citation=f"{_BOOK}, Ch. 7 (Scenarios and Steps), p.88~",
        evidence="cockburn-chapter",
        owner="specs",
        judged_by=JUDGED_VALIDATOR,
        pages=(88,),
        probe=("scenarios and steps",),
    ),
    Rule(
        id="spec.no-control-tokens-in-prose",
        stage=WRITE_SPECIFICATIONS,
        severity=DEFECT,
        statement=(
            "Scenario-ending tokens ('Success!', 'Fail!') are not prose. Express the "
            "ending in the outcome field."
        ),
        citation=f"{_BOOK}, p.47-49 (scenario endings)",
        evidence="cockburn-page",
        owner="specs",
        judged_by=JUDGED_DETECTOR,
        detector="control_tokens",
        pages=(47, 48, 49),
        probe=("fail!",),
    ),
    Rule(
        id="spec.one-subgoal-per-step",
        stage=WRITE_SPECIFICATIONS,
        severity=GUIDANCE,
        statement="Each step carries exactly one sub-goal — one transaction.",
        citation=f'{_BOOK}, Guideline 6 ("Include a Reasonable Set of Actions"), p.93',
        evidence="cockburn-guideline",
        pages=(93,),
        probe=("guideline 6", "transaction"),
    ),
    Rule(
        id="spec.step-count-is-not-a-target",
        stage=WRITE_SPECIFICATIONS,
        severity=NON_RULE,
        statement=(
            "The 3-to-9 step range is an observation about readable use cases, not a "
            "limit. Never pad or trim steps to hit a count; judge each step's level."
        ),
        citation=f'{_BOOK}, p.208 (Reminder 6: "Get the Goal Level Right")',
        evidence="cockburn-observation",
        caveat=(
            "책이 관찰로 적은 범위다 — 규칙이 아니다. 개수로 게이트하면 개수를 맞추려고 "
            "내용을 늘리거나 자른다. **어디에서도 강제하지 않는다.**"
        ),
        pages=(208,),
        probe=("three to nine",),
    ),
    Rule(
        id="spec.consequence-is-a-guarantee",
        stage=WRITE_SPECIFICATIONS,
        # 강등 → 복구. 경위는 위 `spec.no-hidden-branching` 주석과 같다(단독 프로브
        # 4도메인에서 안정 3 / 흔들림 6 / 없음 11).
        severity=DEFECT,
        statement=(
            "Automated system consequences and cross-cutting quality concerns (logging, "
            "auditing, encrypting stored data, sending a receipt) are internal success "
            "guarantees, never main-scenario steps."
        ),
        # ⚠ 2026-07-26 정정: 예전 인용 `p.64`는 틀렸다. 그 페이지는 Ch. 5(Three Named
        # Goal Levels)이고 guarantee를 다루지 않는다. 보증은 Ch. 6이고 "Minimal
        # Guarantees" 절이 p.83이다(로컬 사본으로 확인).
        citation=f"{_BOOK}, Ch. 6 (Preconditions, Triggers, and Guarantees), p.83",
        evidence="cockburn-extrapolated",
        caveat=(
            "보증이 사후조건의 자리라는 것은 Ch. 6에서 확인했다. 다만 '자동결과(로깅·감사·"
            "암호화·확인 발송)는 스텝이 아니라 보증'이라는 구체적 적용은 우리가 끌어낸 것이다."
        ),
        owner="specs",
        judged_by=JUDGED_VALIDATOR,
        pages=(83,),
        probe=("minimal guarantee",),
    ),
    Rule(
        id="spec.no-precondition-recheck",
        stage=WRITE_SPECIFICATIONS,
        severity=DEFECT,
        statement=(
            "A precondition is a state the use case may assume. A main-scenario step "
            "must not re-verify what a precondition already guarantees."
        ),
        citation=f"{_BOOK}, p.81 (preconditions need not be checked)",
        evidence="cockburn-page",
        owner="specs",
        judged_by=JUDGED_VALIDATOR,
        pages=(81,),
        probe=("precondition", "not be checked"),
    ),
    Rule(
        id="spec.no-scope-creep",
        stage=WRITE_SPECIFICATIONS,
        severity=DEFECT,
        statement=(
            "Every step, extension condition, handling action, and guarantee must be directly "
            "supported by the current named actor goal and its requirements, or be a necessary "
            "consequence of them. A requirement shared by several use cases is evidence for each "
            "named goal, not permission to implement a neighbouring goal inside the current one. "
            "Do not infer ordering or a lifecycle precondition between neighbouring goals unless "
            "the supplied requirements state it. "
            "Merely plausible domain convention or defensive behaviour is not a necessary "
            "consequence. Do not invent auditing, logging, security, persistence, uniqueness, "
            "technical failure, retry, fallback, recovery, or error handling just because similar "
            "systems commonly have it. A failure branch for an explicitly required validation "
            "is directly supported, but it must not add an unstated retry or recovery policy."
        ),
        citation="easydep convention (hallucination guard)",
        evidence="project-convention",
        caveat="환각을 막기 위해 우리가 정한 규칙이다. 책의 규칙이 아니다.",
        owner="specs",
        judged_by=JUDGED_VALIDATOR,
    ),
    Rule(
        id="spec.causal-flow-consistency",
        stage=WRITE_SPECIFICATIONS,
        severity=DEFECT,
        statement=(
            "The trigger is the event that starts this actor goal, not an earlier enabling state. "
            "When a conditional follow-on goal becomes available after another goal's result, "
            "keep that prior operation and result in the trigger or preconditions; do not replay "
            "them as steps of the follow-on goal. "
            "The main scenario and its extensions must form causally coherent paths: an extension "
            "branches at the first main step where its condition can be known, and an alternate "
            "operation branches before a mutually incompatible main-path choice."
        ),
        citation=(
            f"{_BOOK}, Ch. 6 (Preconditions, Triggers, and Guarantees) and "
            "Ch. 8 (Extensions)"
        ),
        evidence="cockburn-extrapolated",
        caveat=(
            "The source distinguishes preconditions, triggers, main success scenarios, and "
            "extensions. The explicit causal-consistency test is EasyDep's operationalization "
            "of those distinctions."
        ),
        owner="specs",
        judged_by=JUDGED_VALIDATOR,
        pages=(80, 81, 106),
        probe=("trigger", "extension"),
    ),
    Rule(
        id="spec.remerge-re-establishes-state",
        stage=WRITE_SPECIFICATIONS,
        severity=DEFECT,
        statement=(
            "An extension that resumes the main scenario must actually re-establish the "
            "state the resume step assumes."
        ),
        citation=f"{_BOOK}, Ch. 8 (Extensions), rejoin at p.106",
        evidence="cockburn-extrapolated",
        caveat=(
            "확장이 주 시나리오로 복귀한다(rejoin)는 개념과 페이지는 확인했다. "
            "'복귀 지점이 가정하는 상태를 실제로 회복해야 한다'는 요구는 우리가 세운 것이다."
        ),
        owner="specs",
        judged_by=JUDGED_VALIDATOR,
        pages=(106,),
        probe=("rejoin",),
    ),
    Rule(
        id="spec.extension-reference-integrity",
        stage=WRITE_SPECIFICATIONS,
        severity=DEFECT,
        statement=(
            "Every extension must have a concrete branch_step naming a step that exists in "
            "the main scenario; global/unanchored extensions are unsupported and must be "
            "represented at a supported concrete branch point. resume_at_step must name an "
            "existing main-scenario step and is set only when the outcome is 'resume'."
        ),
        citation="easydep convention (schema integrity)",
        evidence="project-convention",
        caveat="우리 스키마의 무결성 규칙이다 — 위반은 결정론적으로 참이지만, 규범을 정한 것은 우리다.",
        owner="specs",
        judged_by=JUDGED_DETECTOR,
        detector="extension_refs",
    ),
    Rule(
        id="spec.contract-completeness",
        stage=WRITE_SPECIFICATIONS,
        severity=DEFECT,
        statement=(
            "A specification states its success guarantee. Preconditions describe only "
            "states actually established before the use case; an empty precondition list "
            "is valid when the supplied goal and requirements establish none."
        ),
        citation="easydep convention (fully-dressed template)",
        evidence="project-convention",
        caveat=(
            "풀 템플릿의 어느 칸을 필수로 볼지는 우리가 정했다. 빈 전제조건을 결함으로 "
            "취급하면 생성기가 인증·권한·수명주기 상태를 지어내는 것이 실험에서 확인되어, "
            "명시할 전제조건이 없는 것과 계약 누락을 구분한다."
        ),
        owner="specs",
        judged_by=JUDGED_DETECTOR,
        detector="contract_fields",
    ),
    Rule(
        id="spec.public-behavior-completeness",
        stage=WRITE_SPECIFICATIONS,
        severity=DEFECT,
        statement=(
            "Review the specification against its use-case goal, linked requirements, and "
            "explicitly applicable constraints for "
            "complete, non-contradictory public behavior: the responsible actor, any relevant "
            "authenticated subject or delegation, required information, and observable outcome. "
            "For every declared required value, verify that its source (caller_input, "
            "authenticated_actor_context, or system_result) matches the explicit origin stated "
            "in the linked requirements or explicitly applicable constraints. If that origin is "
            "misclassified, direct correction of "
            "the `RequiredValue.source` field to the evidenced origin. Also verify that "
            "`RequiredValue.usage` matches the natural-language scenario and observable outcome: "
            "control means the system consumes the value to perform behavior; result means the "
            "system produces the value as an observable outcome; both requires evidence of both. "
            "Source is provenance, not direction: authenticated_actor_context alone does not make "
            "a value a result, and a context value consumed by an operation is control usage. If "
            "usage is misclassified, direct correction of `RequiredValue.usage` and cite the "
            "specific flow or outcome that establishes the correction. Do not infer usage from "
            "the value name or source alone. For an identifier that "
            "represents an identified subject, direct correction of the proposal's "
            "`RequiredValue.identity_obligation_index`; for an authenticated-context identity "
            "relation, direct correction of `IdentityObligation.source_authenticate_obligation_index`. "
            "These are proposal fields. Do not direct repair of the derived accepted "
            "`identity_source_kind` or `source_authenticate_obligation_ref`. Report an omitted "
            "identity relation only when grounded in the supplied linked requirements or explicitly "
            "applicable constraints. When reporting "
            "an OMITTED identify relation, direct the complete proposal repair: add the identify "
            "obligation together with a linked identifier `RequiredValue` whose "
            "`identity_obligation_index` points to it and whose `source` is the evidenced origin; "
            "when authenticated_actor_context is evidenced, include the evidenced authenticate "
            "obligation in the same proposal if it is absent, then set the identify obligation's "
            "`source_authenticate_obligation_index` to it. "
            "Do not direct adding an orphan identify obligation alone. "
            "Report an omission or contradiction only when that behavior is grounded in the "
            "supplied goal or requirements. Do not invent authentication, authorization, required "
            "preconditions, or other behavior when the sources establish none. Authentication "
            "validates a principal or session; identification distinguishes which subject or record "
            "the behavior targets. An actor role or label alone, or public or published-data browsing "
            "alone, does not establish an identify obligation. For behavior that acts on the requester's "
            "own subject or record, report an omitted identify relation when a linked functional "
            "requirement establishes that behavior and an explicitly applicable constraint establishes "
            "authenticated identity; require the linked identifier RequiredValue to use "
            "authenticated_actor_context and link identify to authenticate. Applicable global "
            "constraints may establish source or applicability in review context but do not "
            "belong in an entry's requirement_ids unless themselves linked."
        ),
        citation="easydep convention (source-grounded public behavior review)",
        evidence="project-convention",
        owner="specs",
        judged_by=JUDGED_VALIDATOR,
    ),
    Rule(
        id="spec.scenario-requirement-reference-integrity",
        stage=WRITE_SPECIFICATIONS,
        severity=DEFECT,
        statement=(
            "Every accepted functional requirement must be realized by at least one main-scenario "
            "covered_req_id, and each covered_req_id must belong to that same use case. "
            "Non-functional constraints are never scenario coverage."
        ),
        citation="easydep convention (use-case-local traceability)",
        evidence="project-convention",
        owner="specs",
        judged_by=JUDGED_DETECTOR,
        detector="scenario_requirement_refs",
    ),
    # --- 4단계: 관계 / 다이어그램 -----------------------------------------
    Rule(
        id="rel.shared-authentication-is-a-precondition",
        stage=DRAW_DIAGRAM,
        severity=DEFECT,
        statement=(
            "A shared login/authentication/authorization sub-goal is a precondition set "
            "up by a prior use case, not an <<include>> drawn from every use case."
        ),
        citation=f"{_BOOK}, p.81",
        evidence="cockburn-page",
        owner="relationships",
        judged_by=JUDGED_VALIDATOR,
        pages=(81,),
        probe=("precondition", "log"),
    ),
    Rule(
        id="rel.consequence-is-not-an-include",
        stage=DRAW_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Cross-cutting internal consequences (logging, auditing, encrypting, sending "
            "confirmations) are success guarantees, never included sub-goals."
        ),
        # ⚠ 2026-07-26 정정: 예전 인용 `p.64`는 틀렸다(위 spec 쌍둥이 규칙 참고).
        citation=f"{_BOOK}, Ch. 6 (Preconditions, Triggers, and Guarantees), p.83",
        evidence="cockburn-extrapolated",
        caveat=(
            "보증이 사후조건의 자리라는 것은 Ch. 6에서 확인했다. 그것이 곧 "
            "'include로 뽑지 말라'는 뜻이라는 적용은 우리가 끌어낸 것이다."
        ),
        owner="relationships",
        judged_by=JUDGED_VALIDATOR,
        pages=(83,),
        probe=("minimal guarantee",),
    ),
    Rule(
        id="rel.include-is-the-default-relationship",
        stage=DRAW_DIAGRAM,
        severity=GUIDANCE,
        statement=(
            "Use <<include>> only for a genuine shared, reusable mandatory interaction with "
            "its own observable result; never factor a rule, invariant, or postcondition. "
            "Use extend and generalization sparingly."
        ),
        # ⚠ 2026-07-26 정정: 예전 인용 `p.207`은 틀렸다. 그 페이지는 Reminder 5
        # ("Who Has the Ball?")이고, 거기 있는 "rule of thumb"은 다른 이야기다 —
        # 아마 그 단어로 찾다가 잘못 붙었다. 관계를 다루는 곳은 Ch. 10이다.
        citation=f"{_BOOK}, Ch. 10 (Linking Use Cases), p.114-117",
        evidence="cockburn-extrapolated",
        caveat=(
            "관계를 다루는 장과 페이지는 확인했다. 다만 'include가 기본이고 나머지는 "
            "아껴 쓴다'는 우선순위는 우리가 정리한 것이다."
        ),
        pages=(117,),
        probe=("include", "extend"),
    ),
    Rule(
        id="rel.failures-stay-inline-extensions",
        stage=DRAW_DIAGRAM,
        severity=DEFECT,
        statement=(
            "A scenario branch whose sole purpose is handling a failure, error, or cancellation "
            "and which has no independent actor goal and observable result stays an inline "
            "extension of its use case. Do not promote a mere failure branch to <<extend>> or "
            "to a derived use case."
        ),
        citation=f"{_BOOK}, p.109 (Ch. 8, Extensions)",
        evidence="cockburn-page",
        owner="relationships",
        judged_by=JUDGED_VALIDATOR,
        pages=(109,),
        probe=("extension", "fail"),
    ),
    Rule(
        id="rel.extend-adds-conditional-behavior",
        stage=DRAW_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Use <<extend>> when an extending use case adds behavior at an explicitly "
            "referenced extension point of a base use case that remains meaningful on its "
            "own. A condition may govern insertion, including an actor's optional choice. "
            "Do not use <<extend>> for mandatory ordinary continuation or mere temporal order."
        ),
        citation="OMG UML 2.5.1, §18.1.3 (Extend semantics)",
        evidence="uml-spec",
        owner="relationships",
        judged_by=JUDGED_VALIDATOR,
    ),
    Rule(
        id="rel.generalization-keeps-meaning",
        stage=DRAW_DIAGRAM,
        severity=DEFECT,
        statement="A generalization must not invert or confuse the parent/child meaning.",
        citation="OMG UML, generalization semantics",
        evidence="uml-spec",
        owner="relationships",
        judged_by=JUDGED_VALIDATOR,
    ),
    Rule(
        id="rel.supporting-actors-on-the-right",
        stage=DRAW_DIAGRAM,
        severity=GUIDANCE,
        statement=(
            "Draw primary actors to the left of the system boundary and supporting "
            "actors to the right."
        ),
        citation=f'{_BOOK}, Guideline 18 ("Supporting Actors on the Right"), p.243',
        evidence="cockburn-guideline",
        pages=(243,),
        probe=("guideline 18", "supporting actors on the right"),
    ),
    Rule(
        id="rel.reference-integrity",
        stage=DRAW_DIAGRAM,
        severity=DEFECT,
        statement=(
            "Every relationship must reference an actor or use case that exists. A "
            "relationship naming something that does not exist is dropped, not drawn."
        ),
        citation="easydep convention (schema integrity)",
        evidence="project-convention",
        caveat="우리 규약이다. 판정은 `step4_diagram.identify_relationships`가 결정론으로 한다.",
        owner="relationships",
        judged_by=JUDGED_STAGE,
    ),
    Rule(
        id="rel.include-hint-cap",
        stage=DRAW_DIAGRAM,
        severity=NON_RULE,
        statement=(
            "Capping include hints at six is a prompt-size guard, not a limit on how "
            "many includes the model may produce."
        ),
        citation="app/requirements/modeling/relationships.py (_MAX_INCLUDE_HINTS)",
        evidence="engineering-guard",
        caveat=(
            "순수 공학적 가드다 — 규칙이 아니고 판정 근거로 쓰면 안 된다. 출력 개수를 "
            "제한하지 않는다."
        ),
    ),
)


_BY_ID: dict[str, Rule] = {r.id: r for r in RULES}


def rule(rule_id: str) -> Rule:
    """id로 규칙 하나. 없으면 `KeyError` — 없는 규칙을 인용하는 것은 오류다."""
    return _BY_ID[rule_id]


def known_ids() -> frozenset[str]:
    """존재하는 규칙 id 전부. LLM이 댄 인용을 대조하는 데 쓴다."""
    return frozenset(_BY_ID)


def rules_for(stage: str, severity: str | None = None) -> tuple[Rule, ...]:
    """단계(+심각도)로 규칙을 고른다. 선언 순서를 유지한다."""
    return tuple(
        r for r in RULES
        if r.stage == stage and (severity is None or r.severity == severity)
    )


def rule_of(issue: str) -> str | None:
    """지적 문구가 인용한 규칙 id. 못 찾으면 None.

    꼬리표는 우리가 만든다(`Rule.tag` → `[<id> · <좌표> …]`)므로 정확히 맞춰 찾는다.
    문구를 파싱하는 대신 **아는 id로 조회**하는 방향이라, 새 규칙이 생겨도 이 함수는 그대로다.

    지식베이스에 두는 이유: 채점(`evaluation/scorecard.py`)과 되돌리기 라우팅
    (`agent/supervisor.py`)이 같은 되읽기를 필요로 한다. 두 벌이면 갈라진다.
    """
    for rule_id in _BY_ID:
        if f"[{rule_id} ·" in issue:
            return rule_id
    return None


def owner_of(issue: str) -> str | None:
    """이 지적을 낸 단계의 논리 이름. 규칙을 못 찾거나 책임 단계가 없으면 None."""
    rule_id = rule_of(issue)
    if rule_id is None:
        return None
    return _BY_ID[rule_id].owner


def tag_of(rule_id: str) -> str:
    """지적 문구에 붙일 꼬리표. 모르는 id는 그 사실을 드러낸다.

    조용히 빈 문자열을 돌려주면 **없는 규칙을 인용한 지적이 근거 있는 지적처럼**
    보인다. 그건 이 지식베이스를 두는 이유와 반대다.
    """
    found = _BY_ID.get(rule_id)
    return found.tag if found else f"[{rule_id} · 알 수 없는 규칙]"


def judged_by(stage: str, judge: str) -> tuple[Rule, ...]:
    """이 단계에서 그 판정자가 보는 결함 규칙들."""
    return tuple(r for r in rules_for(stage, DEFECT) if r.judged_by == judge)


def generation_prompt_block(stage: str) -> str:
    """**생성** 프롬프트가 지켜야 할 규칙 목록.

    검증 프롬프트(`validator_prompt_block`)와 나뉘는 자리다. 두 목록이 다른 것을 담는 것은
    의도한 것이다:

      - 검증은 `DEFECT`이면서 **의미 검증자가 보는** 것만 담는다. 검출기가 이미 잡은 것을
        다시 지적하지 않기 위해서다.
      - 생성은 **강제되는 것 전부**(`DEFECT` + `GUIDANCE`)를 담는다. 쓰는 쪽에서는
        "누가 잡느냐"가 상관없다 — 검출기가 잡을 결함도 애초에 안 쓰는 편이 낫다.

    `GUIDANCE`가 여기서 처음으로 일을 한다. 지금까지 그 심각도는 "지적하지 않는 지침"이라고
    적혀 있었지만 **아무 데도 실리지 않아서 사실상 죽은 표시**였다. 생성 프롬프트가 산문으로
    같은 말을 따로 적고 있었기 때문이다.

    인용은 싣지 않는다. 좌표는 지적 문구가 달고 나가는 것이고(`Rule.tag`), 쓰는 쪽에
    페이지 번호를 주는 것은 프롬프트를 늘릴 뿐 쓰기를 돕지 않는다.
    """
    lines = []
    for r in rules_for(stage):
        if r.severity not in (DEFECT, GUIDANCE):
            continue
        lines.append(f"- ({r.id}) {r.statement}")
        if r.generation_note:
            lines.append(f"  {r.generation_note}")
    return "\n".join(lines)


def non_rules_block(stage: str) -> str:
    """**규칙이 아니라고** 적어 둔 것들 — 생성 쪽에 그 사실 그대로 준다.

    `NON_RULE`이 실제로 쓰이는 자리다. 과적합은 판정할 때가 아니라 **쓸 때** 일어난다:
    "스텝 3~9개"를 목표로 알아들은 모델은 아홉 번째 스텝을 지어내거나 열 번째를 지운다.
    그러니 이 사실을 받아야 하는 쪽은 판정자가 아니라 생성자다.

    비어 있으면 빈 문자열을 돌려준다 — 부르는 쪽이 절 자체를 빼도록.
    """
    lines = [f"- ({r.id}) {r.statement}" for r in rules_for(stage, NON_RULE)]
    return "\n".join(lines)


def already_checked_names(stage: str) -> tuple[str, ...]:
    """검증자에게 "이건 이미 봤다"고 알려줄 규칙 id들.

    예전에는 이 목록이 프롬프트 산문에 손으로 적혀 있었다("branching words, control
    tokens, UI terms, broken step references, missing contract"). 검출기를 하나 늘리면
    그 문장도 같이 고쳐야 하고, 안 고치면 검증자가 이미 잡힌 것을 다시 지적한다.
    """
    return tuple(
        r.id for r in rules_for(stage, DEFECT)
        if r.judged_by in (JUDGED_DETECTOR, JUDGED_STAGE)
    )


def unjudged_defects() -> tuple[Rule, ...]:
    """결함이라고 적어 놓고 **아무도 판정하지 않는** 규칙들.

    비어 있는 것이 목표가 아니다 — 지금 비어 있지 않고, 그게 사실이다. 테스트가 이
    목록을 고정해 두므로 조용히 늘어나지는 않는다.
    """
    return tuple(r for r in RULES if r.severity == DEFECT and r.judged_by == JUDGED_NOWHERE)
