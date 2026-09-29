"""**사용자에게 받아야 할 것** 한 곳 — 질문·근거·소비자·선행조건.

## 왜 생겼나

"무엇을 받아야 하는가"를 말하는 곳이 셋이었고 **서로를 몰랐다**(실측 2026-08-01):

| 출처 | 개수 | 묻는 것 | 실제로 이어진 것 |
|---|---|---|---|
| `appkb/request.json` + `contract.REQUIRED_WHY` | 15칸 | **값** | 조인 축 + 판정 5개 |
| `requirements/knowledge/concerns.py` | 29건 | **요구사항에 적혔나** | 7건만 계약 칸으로 |
| `depkb`의 `closure().decisions` | csp별 1~3 | **앵커 정한 뒤의 선택** | **아무도 안 물었다** |

그리고 계약 15칸에는 축이 하나뿐이었다 — 전부 *"어떻게 고르나"*이고 *"무엇을
놓나"*가 없었다. 앵커 후보가 CSP당 13~14종인데 받을 칸이 없어서 **의존 주장
118건이 사슬에 진입하지 못했다.**

## 새 규칙이 아니라 승격이다

*"모든 칸에는 소비자(어느 조인·어느 판정이 읽는가)가 있어야 한다"* 는 이미
`request.json`의 설명에 적혀 있었고(과거 사용자 입력을 받아 놓고 안 읽던 결함의 일반화),
*"필수 칸을 지우면 이름 붙은 판정이 실제로 사라져야 한다"* 는
`tests/test_required_fields.py`가 이미 검사하고 있었다. 둘 다 **산문과 테스트에
흩어진 채로** 있었을 뿐이다. 여기서는 그것을 항목의 **칸**으로 만든다:

    Ask(id, question, opens, basis, tier, …)
                      └ 소비자   └ 근거

`opens`나 `basis`가 비면 항목을 만들 수 없다(`__post_init__`이 죽는다). 그래서
**근거 없는 질문이 생길 수 없다** — 이 저장소가 세 번 물렸던 자리
(`cloudkb/CLAUDE.md` §5: 지어낸 뒤 다음 턴에 근거처럼 인용)에 대한 구조적 방어다.

## 근거의 세 갈래

    concern:<id>   요구사항 축이 이 질문을 이미 정의했다.
                   코퍼스 좌표(probe·citation)가 딸려 있고 CI가 대조한다.
    claim:<좌표>   3사 실측이 이 선택을 열었다(depkb).
    code:<파일>#<이름>
                   우리 코드가 이 값 없이는 판정을 못 낸다. **우리 사정이지
                   외부 사실이 아니다** — 그래서 갈래를 갈라 적는다.

셋 다 실재 검사가 붙는다(`app/cloudkb/tests/test_input_registry.py`,
`concern:`은 층이 갈려 `tests/test_input_registry_concerns.py`).

## 정적인 것과 파생되는 것

값 질문(`ASKS`)은 여기 손으로 적는다. **결정 질문은 적지 않는다** — depkb의
`closure().decisions`가 이미 내고 있고, 손으로 옮기면 다음 실측에서 어긋난다
(이 저장소가 반복해서 물린 사본 문제). `asks_for()`가 그때그때 뽑는다.

## 이 모듈이 하지 않는 것

값의 **모양**(타입·enum·범위)은 여기 없다. 그건 `request.json`이 진실이고,
`cloud_contract.field_type()`이 읽는다. 같은 사실을 두 곳에 적지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from app.cloudkb.depkb.closure import closure
from app.cloudkb.depkb.scope import VM_ANCHOR_TYPES
from app.requirements.resources import resource_contract as _contract

#: 계층 — **없으면 못 재는가(필수) · 판정 하나가 닫히는가(권고) · 계획에 실릴
#: 뿐인가(맥락)**. 셋을 같은 얼굴로 물으면 사용자가 전부 필수로 읽고, 안 물으면
#: 계획을 다 만든 뒤에야 "그걸 줬으면 판정이 섰다"를 알게 된다.
REQUIRED = "required"
SUGGESTED = "suggested"
CONTEXT = "context"
DECISION = "decision"  # depkb에서 파생 — 앵커가 정해진 뒤에만 열린다

_TIERS = (REQUIRED, SUGGESTED, CONTEXT, DECISION)

#: 근거의 갈래. 접두사가 곧 검사 방법이라 문자열로 두지 않고 여기서 못 박는다.
CONCERN = "concern"  # requirements/knowledge/concerns.py의 id
CLAIM = "claim"  # depkb claims.json의 좌표
CODE = "code"  # 우리 코드 — 파일#이름

_BASIS_KINDS = (CONCERN, CLAIM, CODE)


@dataclass(frozen=True)
class Basis:
    """근거 하나. **좌표이지 문장이 아니다** — 문장은 인용한 쪽이 쓴다."""

    kind: str
    ref: str

    def __post_init__(self) -> None:
        if self.kind not in _BASIS_KINDS:
            raise ValueError(f"Unknown basis kind: {self.kind!r}")
        if not self.ref.strip():
            raise ValueError("A basis reference is required")

    def __str__(self) -> str:
        return f"{self.kind}:{self.ref}"


@dataclass(frozen=True)
class Ask:
    """받아야 할 것 하나.

    `question`은 사용자에게 그대로 나가는 말이고, `opens`는 **이 값이 없으면
    무엇이 안 되는가**다. 되묻기에 둘 다 실린다 — 왜 없이 물으면 사용자가 아무
    값이나 채운다는 것이 이 구조의 출발점이다(`REQUIRED_WHY`의 원래 취지).
    """

    id: str
    question: str
    #: 소비자 — 이 값이 여는 판정·조인. **비면 항목을 만들 수 없다.**
    opens: str
    basis: tuple[Basis, ...]
    tier: str
    #: `RESOURCE_SPEC`의 어느 칸으로 가는가. 결정 질문은 칸이 없다.
    spec_field: str = ""
    #: 이 CSP에서만 열린다. 빈 문자열이면 3사 공통.
    csp: str = ""
    #: 연쇄 의존 자원 집합에 이 자원이 들어올 때만 열린다(결정 질문). 빈 문자열이면 항상.
    needs_resource: str = ""
    #: 사람이 고를 수 있는 값들 — 계약이 enum으로 못 박지 못하는 것(앵커 목록은
    #: CSP마다 다르고 claims에서 나온다). 비어 있으면 스키마가 모양을 말한다.
    choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("An ask ID is required")
        if not self.question.strip():
            raise ValueError(f"{self.id}: a question is required")
        if not self.opens.strip():
            raise ValueError(
                f"{self.id}: an `opens` consumer is required; do not collect a field "
                "that enables no named decision"
            )
        if not self.basis:
            raise ValueError(
                f"{self.id}: at least one basis reference is required"
            )
        if self.tier not in _TIERS:
            raise ValueError(f"{self.id}: unknown tier {self.tier!r}")


#: 값 질문. **결정 질문은 여기 없다** — depkb에서 파생된다(`_decision_asks`).
#:
#: 순서가 되묻기 순서다. 조인 축을 먼저 받는 이유는 그 뒤의 모든 것이 CSP로
#: 갈리기 때문이다 — provider 없이는 앵커 목록조차 못 낸다.
ASKS: tuple[Ask, ...] = (
    Ask(
        id="join.provider",
        spec_field="provider",
        tier=REQUIRED,
        question="Which cloud provider will host the deployment? (aws, azure, or gcp)",
        opens="This selects the provider-specific cost, performance, capacity, resource "
        "type, and dependency data required to produce a deployment plan.",
        basis=(
            Basis(CODE, "app/core/regions.py#providers"),
            Basis(CODE, "app/cloudkb/depkb/closure.py#closure"),
        ),
    ),
    Ask(
        id="join.region",
        spec_field="region",
        tier=REQUIRED,
        question="Which region should host the deployment? A place name is acceptable.",
        opens="Pricing and capacity data are indexed by provider region code; the place "
        "name is resolved before those datasets are queried.",
        basis=(Basis(CODE, "app/core/regions.py#resolve"),),
    ),
    # **existingResources는 2026-08-02에 계약에서 빠졌다 — 범위 결정(사용자).**
    # 이 시스템의 대상은 **신규 앱 개발**이라 인계받을 기존 자원이 없다. 근거
    # 부재(탄소)와 다른 사유다: "의존성 계산은 이미 있는 자원을 원리적으로 모른다"는 논리는
    # 유효하되, 그 질문이 서는 전제(브라운필드)가 범위 밖이다. 되살리려면 범위
    # 확장이 먼저다 — 그때는 클린룸 계약 재도출의 두 논거(필수 승격으로 없음/모름
    # 구분 · {종류, 식별자} 쌍)도 함께 볼 것(archive/cleanroom-battery-2026-08-02.md).
    Ask(
        id="budget.monthly",
        spec_field="monthlyBudgetUSD",
        tier=CONTEXT,
        question="What is the monthly budget? Other currencies can be converted to USD.",
        opens="When supplied, the monthly budget enables a cost-ceiling check. Without it, "
        "the system leaves budget compliance unmeasured rather than inventing a limit.",
        # 2026-08-02 관심사 실측 재도출로 문헌 유래 관심사(cn.cost-ceiling)가
        # 죽었다 — 이 칸의 근거는 과제 원문(비용 기준 추천)과 판정 코드다.
        basis=(Basis(CODE, "app/cloudkb/costkb/agent_api.py#estimate_monthly_cost"),),
    ),
    # ── 선택축 — 채우면 판정이 하나 열린다 ───────────────────────────────────
    Ask(
        id="spec.min_vcpu",
        spec_field="minVCpu",
        tier=SUGGESTED,
        question=(
            "If known, provide either the minimum vCPU or minimum memory required by this workload."
        ),
        opens="A sizing floor enables instance-spec selection. Without one, the system "
        "does not assume that the cheapest or smallest instance is sufficient.",
        basis=(Basis(CODE, "app/cloudkb/costkb/agent_api.py#recommend_specs"),),
    ),
    Ask(
        id="spec.min_memory",
        spec_field="minMemoryGiB",
        tier=SUGGESTED,
        question="What is the minimum memory requirement in GiB? This may be supplied instead of vCPU.",
        opens="Memory provides an alternative sizing floor; either memory or vCPU is "
        "sufficient to enable instance-spec selection.",
        basis=(Basis(CODE, "app/cloudkb/costkb/agent_api.py#recommend_specs"),),
    ),
    Ask(
        id="load.traffic_pattern",
        spec_field="trafficPattern",
        tier=SUGGESTED,
        question="Is the workload steady or does it have intermittent spikes?",
        opens="The traffic pattern determines whether burst-performance warnings conflict "
        "with this workload.",
        basis=(
            Basis(CONCERN, "cn.load-shape"),
            Basis(CODE, "app/cloudkb/perfkb/agent_api.py#recommend_warning"),
        ),
    ),
    # ── 맥락축 — 판정을 열진 않지만 계획에 실린다 ────────────────────────────
    Ask(
        id="scale.expected",
        spec_field="scale",
        tier=CONTEXT,
        question="What load is expected? Provide either concurrent users or requests per second.",
        opens="This records the intended scale for the plan but does not infer a VM size.",
        basis=(Basis(CODE, "app/core/resource_spec.schema.json#scale"),),
    ),
    Ask(
        id="data.residency",
        spec_field="dataResidency",
        tier=CONTEXT,
        question="Must data remain within a specific country or geographic area?",
        opens="The deployment plan exposes the provider's region display name for a manual "
        "residency check; the system does not infer legal compliance.",
        basis=(Basis(CODE, "app/core/region_catalog.py#catalog"),),
    ),
)

# **lowCarbonPreferred는 2026-08-02에 계약에서 빠졌다.** 계보 감사의 결과다 —
# 근거 사슬이 GCP 프레임워크 문헌 → 우리 관심사 축 → 우리 배선(2026-07-28)뿐이고,
# 사용자가 그 제약을 말한 실물이 코퍼스에 없다(내부 0건 · PURE 13건 전부 오탐).
# 탄소 데이터와 질의응답 축(envkb·cap_region_carbon)은 유지된다. 되살리려면
# 사용자 진술 실물(코퍼스 또는 실제 사용례)이 먼저다.

#: 사용자에게 묻지 **않는** 칸과 그 이유. 스키마에 있는데 여기 없는 칸이 생기면
#: 테스트가 실패한다 — "빠뜨린 것"과 "안 묻기로 한 것"을 구별하기 위해서다.
NOT_ASKED: dict[str, str] = {
    "schemaVersion": "The producer copies the constant contract version from the schema.",
    "workloads": "The Docker-on-VM deployment scope fixes this value to ['vm'].",
    "regionAsWritten": "The producer preserves the user's region text from join.region.",
}

#: **요구사항 단계에서 안 받고 인계로 넘기는 것**과 그 이유(2026-08-01).
#:
#: 계약이 받는 것은 "계획과 판정에 필요한 값"이다. 아래는 **렌더 시점에만**
#: 필요하고, 요구사항을 쓰는 사람이 그 시점에 알 이유가 없다. 받으면 어색하고
#: 안 받으면 사라지므로, **인계 항목으로 명시해서 낸다**(`cloud_artifact`의
#: `_handoff`) — 침묵과 인계는 다르다.
HANDOFF: dict[str, str] = {
    "containerRegistry": "The deployment operator supplies the container registry; CI "
    "selects the image tag, and the manifest keeps a placeholder until then.",
    "tlsCertificate": "The deployment operator supplies the TLS certificate or secret.",
}


@lru_cache(maxsize=1)
def by_id() -> dict[str, Ask]:
    """질문 ID로 cloud input registry를 조회하는 불변 index를 만든다."""

    return {a.id: a for a in ASKS}


@lru_cache(maxsize=1)
def by_field() -> dict[str, Ask]:
    """resource spec field로 registry 질문을 조회하는 불변 index를 만든다."""

    return {a.spec_field: a for a in ASKS if a.spec_field}


def tier_of(spec_field: str) -> str:
    """그 칸이 어느 계층인가. 모르는 칸이면 빈 문자열."""
    ask = by_field().get(spec_field)
    return ask.tier if ask else ""


@lru_cache(maxsize=8)
def anchors_for(csp: str) -> tuple[str, ...]:
    """이 CSP에서 앵커가 될 수 있는 자원 — `topology.workloads`가 받는 값.

    **claims에서 뽑는다**(주체 집합 중 연쇄 의존 자원 계산이 가능한 것). 손으로 적으면 다음
    실측에서 어긋나고, 그건 이 저장소가 반복해서 물린 자리다.
    """
    out = []
    # closure가 claims.json의 읽기와 검증을 한 곳에서 담당한다. 여기서 같은 파일을 다시
    # 찾아 읽으면 패키지 이동 때 경로가 어긋나고 검증 규칙도 두 벌이 된다.
    for subject in sorted(VM_ANCHOR_TYPES):
        try:
            closure(subject, csp)
        except KeyError:
            continue
        out.append(subject)
    return tuple(out)


def _decision_asks(csp: str, workloads: tuple[str, ...]) -> tuple[Ask, ...]:
    """앵커가 정해진 뒤 열리는 결정 — **depkb가 낸 것을 그대로 옮긴다.**

    손으로 목록을 적지 않는 이유는 하나다: 새 실측이 결정을 하나 더 열면 여기가
    조용히 낡는다. `closure().decisions`가 진실이고 이 함수는 사영이다.
    """
    seen: dict[str, Ask] = {}
    for anchor in workloads:
        try:
            plan = closure(anchor, csp)
        except KeyError:
            continue  # 이 CSP에서 미측정인 앵커 — 계획 쪽이 따로 말한다
        for decision in plan.decisions:
            subject, _, obj = decision.about.partition("→")
            ask = Ask(
                id=f"decision.{csp}.{subject}.{obj.replace('|', '-or-')}",
                spec_field="",
                tier=DECISION,
                csp=csp,
                needs_resource=subject,
                # DepKB 설명은 내부 조사 기록이며 일부 자료는 한글이다. 사용자에게는
                # 선택 대상과 이유만 영어로 보여 주고, 원문은 basis 좌표에서 조회한다.
                question=f"How should {obj} be selected for {subject}?",
                opens=f"The {subject} creation flow requires this control-plane decision "
                f"({decision.kind}).",
                basis=(Basis(CLAIM, f"{csp}/{decision.about}/existence"),),
                choices=tuple(obj.split("|")) if "|" in obj else (),
            )
            seen[ask.id] = ask
    return tuple(seen[k] for k in sorted(seen))


def asks_for(csp: str = "", workloads: tuple[str, ...] = ()) -> tuple[Ask, ...]:
    """지금 **물을 수 있는** 것 전부 — 선행조건이 채워진 순서대로.

    선행조건이 실재하는 이유: `decision.*`은 앵커를 모르면 물을 수 없고, 앵커
    질문은 CSP를 모르면 목록조차 못 낸다. 그래서 계약은 평면이지만 **묻는 순서는
    평면이 아니다** — 그 사실이 지금까지 어디에도 없었다.

    Args:
        csp: 정해졌으면 그 값. 없으면 CSP 의존 질문은 안 나온다.
        workloads: 사용자가 고른 앵커들. 없으면 결정 질문은 안 나온다.
    """
    out = list(ASKS)
    if csp:
        out = [a for a in out if not a.csp or a.csp == csp]
        out.extend(_decision_asks(csp, tuple(workloads)))
    return tuple(out)


def choices_for(ask: Ask, csp: str = "") -> tuple[str, ...]:
    """그 질문이 받는 값들 — 앵커 목록처럼 CSP에 매인 것을 여기서 푼다."""
    if ask.id == "topology.workloads":
        return anchors_for(csp) if csp else ()
    if ask.id == "join.provider":
        from app.cloudkb import regions

        return tuple(regions.providers())
    if ask.choices:
        return ask.choices
    field_name = ask.spec_field
    if field_name:
        spec = _contract.request_schema().get("properties", {}).get(field_name, {})
        return tuple(spec.get("enum", ()))
    return ()


#: **한쪽만 있으면 더 안 묻는 칸들.** vCPU와 메모리는 *둘 다 필터를 잡는 진짜 두
#: 축*이지만(`sizing_floor.resolve`가 각각 max로 거른다) 한쪽만 있어도 스펙 선택이
#: 열리므로 같은 것을 두 번 묻지 않는다.
#:
#: **2026-08-01에 쌍이 둘에서 하나로 줄었다.** 규모 신호(동시 사용자·RPS)가 다른
#: 한 쌍이었는데, 그 둘은 같은 양의 두 *단위*였을 뿐이라 `scale{value,unit}` 한
#: 칸이 됐다 — 계약을 제로베이스에서 다시 짜면서 나온 값이 이것이다. 모양이 맞으면
#: 특수 처리가 필요 없다.
PAIRS: tuple[tuple[str, ...], ...] = (("minVCpu", "minMemoryGiB"),)


def missing(spec: dict, tier: str = REQUIRED) -> tuple[Ask, ...]:
    """그 계층에서 아직 답이 없는 질문들. 쌍 처리는 `PAIRS`가 정한다."""
    satisfied: set[str] = set()
    for pair in PAIRS:
        if any(name in spec for name in pair):
            satisfied |= set(pair)
    missing_asks = [
        a
        for a in ASKS
        if a.tier == tier
        and a.spec_field
        and a.spec_field not in spec
        and a.spec_field not in satisfied
    ]
    # Alternative sizing fields open the same decision, so ask only once. The answer is
    # natural language and may contain either value; extraction decides which field it fills.
    for pair in PAIRS:
        members = [a for a in missing_asks if a.spec_field in pair]
        if len(members) > 1:
            missing_asks = [a for a in missing_asks if a.spec_field not in pair]
            missing_asks.append(members[0])
    order = {ask.id: index for index, ask in enumerate(ASKS)}
    return tuple(sorted(missing_asks, key=lambda ask: order[ask.id]))


@dataclass(frozen=True)
class Gap:
    """못 받은 것 하나 — 화면·되묻기가 그대로 쓰는 꼴."""

    ask: Ask
    tier: str
    question: str
    why: str
    choices: tuple[str, ...] = field(default_factory=tuple)


def gaps(spec: dict, csp: str = "") -> tuple[Gap, ...]:
    """지금 사용자에게 낼 것 — 필수 → 권고 → 맥락 순.

    결정 질문(`DECISION`)은 여기 없다. 그건 앵커가 정해진 **뒤** 계획을 낼 때
    열리고, 그 자리에서 `asks_for(csp, workloads)`가 낸다.
    """
    out: list[Gap] = []
    for tier in (REQUIRED, SUGGESTED, CONTEXT):
        for ask in missing(spec, tier):
            out.append(
                Gap(
                    ask=ask,
                    tier=tier,
                    question=ask.question,
                    why=ask.opens,
                    choices=choices_for(ask, csp),
                )
            )
    return tuple(out)
