"""클라우드 네이티브 관심사 — **사용자가 쓰지 않은 요구사항**을 드러내는 축.

## 규칙과 무엇이 다른가 (왜 `rules.py`가 아닌가)

판정 방향이 반대다.

  - `rules.py`  — 우리가 **낸 산출물**을 보고 위반을 찾는다. 위반이면 결함이다.
  - 여기        — 요구사항에 **없는 것**을 찾는다. 없는 것은 결함이 아니라 **뒤 단계가
    정해야 할 일**이다.

한 파일에 두면 심각도 어휘가 곧 갈린다. "위반했다"와 "안 적혔다"를 같은 목록에 두면
검증 프롬프트가 후자를 지적으로 바꾼다 — 그러면 사용자가 안 쓴 것이 전부 결함이 되고
오탐이 대부분이 된다.

형식은 checklist-based reading(CBR)이다 — 질문 목록을 들고 요구사항을 읽어 **누락**을
찾는다. 그래서 항목이 규범문이 아니라 질문문이고, 미충족이 결함이 아니라 인계다.

## 2026-08-06 VM 범위 재정제

앞 판의 Kubernetes 관심사는 보관 문서와 커밋 이력에만 남긴다. 현 목록은 Docker-on-VM
범위의 `depkb/claims.json` 21좌표와 `perfkb` 실측만 사용한다. 관심사가 인용하는 좌표의
실재성과 유일성은 `verify_concerns`가 검사한다.

**메타 특성은 그대로다**(`META_CHARACTERISTIC`) — 바뀐 것은 입장 관문이다:

    구  판   문헌 좌표(doc_id·probe)가 실재하면 후보 → E1~E3 게이트
    신  판   **컨트롤 플레인 실측이 "침묵의 답"을 보였을 때만** 후보 → E1 게이트

"침묵의 답"이란: 요구사항이 그 결정을 안 정했을 때 **환경이 실제로 무엇을 하는지를
우리가 관측한 것**이다. claims.json의 부류가 그 관측이다 —

    server-default   명시 안 하면 CSP가 기본 VPC·네트워크·방화벽을 선택한다
    optional-link    identity·공인 주소·추가 디스크 등을 생략해도 VM은 생성된다
    function         연결을 바꾸면 VM은 남아도 도달성·API 접근 기능이 사라진다

거부(required) 계열은 관심사가 아니다 — 거부는 "환경이 대신 정함"이 아니라 이후 설계가
처리할 제약이며, DepKB의 `closure()`가 의존관계와 선택 조건을 제공한다.

**군집은 우리 구성이다.** 관측 21좌표를 결정 4개로 묶은 것은 우리 판단이고, 그렇다고
표시한다. 관측 자체는 실측이다(전 좌표가 `claims.json`에 실재해야 하며
`verify_concerns`가 CI에서 대조한다).

**좌표 유일성 규칙(구 판 doc_id 유일성의 계승)**: 한 claim 좌표는 한 관심사에만
속한다. 두 관심사가 같은 좌표를 인용하면 미분화 신호다 — 기계가 막는다.

**신호(열쇠말) 승계 규칙**: 결정론 층의 열쇠말은 구 판에서 코퍼스로 채굴·측정한
것이다. 새 관심사의 질문이 구 관심사와 **같은 결정**을 물을 때만 승계한다
(`cn.load-shape` ← `cn.traffic-shape` · `cn.reachability` ← `cn.network-exposure`).
나머지는 비워 둔다 — 억지 열쇠말은 오탐이 결정론의 이름을 달고 나오는 길이다.

## 수요측은 근거로 인정하지 않는다 (사용자 결정)

코퍼스의 사용자 진술(다중화 24건·규모 4건 등)은 이 축의 입장 근거가 아니다 —
실증이 제한된 범위·워크로드·환경에서 이뤄질 것이기 때문이다. 수요측 실물이 있는
결정은 **계약**(`request.json`)이 직접 묻는다(scale 등 — 계보 감사 판정표 참조).
가용성은 선호도를 사전 설문으로 받지 않는다. 요구사항에 관측 가능한 필수 장애 허용 목표가
있으면 가용성 결정으로 넘기고, 근거가 없으면 최소 단일 인스턴스 후보를 사용한다. CSP
관리형 그룹은 가용성이 필요하다고 결정된 뒤 선택하는 구현 수단이다.

## 심각도 축을 두지 않는다

값이 하나뿐이기 때문이다 — 관심사는 전부 "안 정했으면 인계"다. 값 하나짜리 축은
분류가 아니라 장식이고, 나중에 두 번째 값이 생기면 그때 만든다.

## 명세에는 들어가지 않는다

`spec.black-box-no-internal-components`·`spec.no-protocol-mechanics`가 명세에 내부
컴포넌트·프로토콜을 금지한다. 관심사는 NFR 층과 나란한 별도 산출물에 살고
유스케이스 명세는 손대지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.requirements.knowledge import advisory, basis

#: **메타 특성** — 무엇이 관심사가 될 수 있는지를 정하는 단일 기준(Nickerson 외 2013).
#:
#: 2026-08-02 재도출에서도 이 문장은 그대로다. 바뀐 것은 "환경이 대신 정한다"의
#: 근거가 문헌 서술에서 **컨트롤 플레인 관측**으로 올라간 것이다.
META_CHARACTERISTIC = (
    "요구사항 단계에서 확정할 수 있고, 확정하지 않으면 클라우드 실행 환경이 그 답을 "
    "대신 정해 버리는 결정."
)

#: ISO/IEC 25010:2023 제품 품질 특성 9종. **CSP와 무관한 품질 대조 축**이다.
#:
#: 2023 개정판을 쓴다 — `usability`가 `interaction capability`로, `portability`가
#: `flexibility`로 바뀌었고 `safety`가 최상위로 신설됐다. 2011판 이름을 쓰면 매핑이
#: 조용히 옛 모델을 가리킨다.
ISO25010: tuple[str, ...] = (
    "functional suitability",
    "performance efficiency",
    "compatibility",
    "interaction capability",
    "reliability",
    "security",
    "maintainability",
    "flexibility",
    "safety",
)

#: 이 축의 근거 라벨과, 관심사를 실은 모든 출력에 붙는 고지.
#: **어떤 출력 경로에서도 고지를 떼면 안 된다.**
#:
#: 관측(claims)은 실측이지만 **"이것이 요구사항 단계의 결정이다"라는 승격과 군집은
#: 우리 구성**이다 — 그래서 basis는 여전히 `inferred`다.
EVIDENCE = advisory.EVIDENCE
ADVISORY_NOTICE = advisory.ADVISORY_NOTICE


@dataclass(frozen=True)
class Concern:
    """관심사 하나. **질문은 우리 표현이고, 근거는 실측 좌표다.**"""

    id: str
    #: 요구사항에 이 관심사가 다뤄졌는지 묻는 한 문장. 프롬프트에 그대로 들어가므로
    #: 영어로 쓴다. **질문이지 규범이 아니다** — "그래야 한다"가 아니라 "정해졌는가"다.
    #:
    #: **하나의 결정만 묻는다**(입도 규칙). 둘을 묻고 있으면 관심사를 쪼개야 한다.
    question: str
    #: **침묵의 답** — 이 결정을 안 정하면 환경이 실제로 무엇을 하는지, 관측 그대로.
    #: 구 판의 `cloud_specific`(E2의 답) 자리이고 뜻이 좁아졌다: 서술이 아니라 관측이다.
    cloud_specific: str
    #: **실측 좌표** — `claims.json`의 주장 키(`csp/subject->object/relationFamily`).
    #: 전 좌표가 실재해야 하고(`verify_concerns`), 한 좌표는 한 관심사에만 속한다.
    claims: tuple[str, ...] = ()
    #: claims 밖의 실측 KB를 근거로 쓸 때 그 패키지 이름(예: "perfkb").
    #: claims가 비어 있으면 이것이 있어야 한다 — 근거 없는 관심사는 없다.
    kb_ref: str = ""
    #: 사람이 읽을 근거 요약.
    citation: str = ""
    #: 이 관심사가 걸리는 ISO/IEC 25010:2023 품질 특성(`ISO25010`의 값들).
    #: 매핑은 우리 구성이고, 빈 튜플은 누락이 아니라 사실이다.
    iso25010: tuple[str, ...] = ()
    #: **오늘 실재하는 기계 소비자** — 이 답이 실제로 흘러 들어가는 `RESOURCE_SPEC` 칸.
    #: `None`은 배제가 아니라 범위 표시다(받아 줄 기계가 아직 없다 — `noted`).
    consumer: str | None = None
    #: **배포 계획이 소비할 수 없는 관심사라면 그 이유**(어느 단계의 것인가).
    #: `consumer=None`(예정)과 다르다 — 이쪽은 "안 하기로 한 것"(경계)이다.
    out_of_scope: str = ""
    #: 결정론 층의 열쇠말(소문자). **승계 규칙**: 구 판에서 코퍼스로 채굴·측정한
    #: 열쇠말을, 질문이 같은 결정일 때만 물려받는다. 비어 있으면 LLM 층만 판정한다.
    signals: tuple[str, ...] = ()
    evidence: str = EVIDENCE

    def __post_init__(self) -> None:
        if not self.claims and not self.kb_ref:
            raise ValueError(f"{self.id}: 실측 좌표가 없다 — 근거 없는 관심사는 없다")

    @property
    def hedged(self) -> bool:
        """이 근거로 말할 때 한계를 밝혀야 하는가. 이 축은 **항상 참**이다 —
        관측은 실측이어도 승격·군집이 우리 구성이라서다."""
        return basis.needs_hedge(self.evidence)

    def prompt_line(self) -> str:
        """판정 프롬프트 한 줄."""
        return f"- {self.id}: {self.question}"


# ---------------------------------------------------------------------------
# 관심사 목록 — Docker-on-VM 범위 재정제 (5건 · depkb 21좌표 + perfkb 1)
#
# 새 관심사를 넣으려면: 컨트롤 플레인 실측(claims 또는 실측 KB)이 "침묵의 답"을
# 보여야 하고, `python -m app.requirements.knowledge.verify_concerns`를 돌린다.
# ---------------------------------------------------------------------------

CONCERNS: tuple[Concern, ...] = (
    Concern(
        id="cn.network-isolation",
        question=(
            "Do the requirements say whether the application needs a dedicated, "
            "isolated network, or whether the provider's default network is "
            "acceptable?"
        ),
        cloud_specific=(
            "정하지 않으면 CSP 기본값이나 기존 네트워크가 배치 위치를 대신 정할 수 "
            "있다. AWS는 VPC·서브넷 생략을 허용하는 경로가 있고, GCP의 방화벽·LB·NIC도 "
            "네트워크 또는 서브넷을 생략할 수 있다. Azure에서는 네트워크가 서브넷을 "
            "선택적으로 포함한다. 따라서 전용망 여부는 요구사항에서 명시할 결정이다."
        ),
        claims=(
            "aws/firewall->network/provisioning",
            "aws/vm->subnet/provisioning",
            "azure/network->subnet/provisioning",
            "gcp/firewall->network/provisioning",
            "gcp/loadBalancer->network/provisioning",
            "gcp/nic->network/provisioning",
            "gcp/nic->subnet/provisioning",
        ),
        citation="depkb Docker-on-VM 실측 7좌표 — 네트워크·서브넷 생략 가능 경로",
        iso25010=("security",),
    ),
    Concern(
        id="cn.reachability",
        question=(
            "Do the requirements say what must be reachable from the public internet "
            "and what must not?"
        ),
        cloud_specific=(
            "정하지 않으면 기본 방화벽이나 방화벽 규칙이 접근성을 대신 정한다. AWS는 "
            "NIC·VM의 보안 그룹 생략을 허용하고, Azure는 NIC·서브넷 방화벽 연결이 "
            "선택적이다. GCP는 VM을 유지한 채 방화벽 규칙을 제거할 수 있어 외부 TCP "
            "도달성만 사라진다."
        ),
        claims=(
            "aws/nic->firewall/provisioning",
            "aws/vm->firewall/provisioning",
            "aws/vm->firewall/runtime",
            "azure/nic->firewall/provisioning",
            "azure/subnet->firewall/provisioning",
            "azure/subnet->firewall/runtime",
            "gcp/vm->firewall/runtime",
        ),
        citation="depkb Docker-on-VM 실측 7좌표 — 방화벽 기본값·연결·도달성",
        iso25010=("security",),
        # 승계: cn.network-exposure(구 판, 같은 결정) — 코퍼스 채굴·측정 이력 유효.
        signals=("public", "private network", "firewall", "내부망", "방화벽"),
    ),
    Concern(
        id="cn.address-stability",
        question=(
            "Do the requirements say whether externally published endpoints need a "
            "stable public address?"
        ),
        cloud_specific=(
            "정하지 않으면 주소의 지속성을 CSP의 기본 자원형이 정한다 — gcp 임시 "
            "IP(accessConfig)는 재부여 시 새 주소가 오고(34.64.142.22→34.22.74.114 "
            "실측), aws EIP·azure PIP는 소유 자원이라 같은 주소로 회복된다. 주소가 "
            "바뀌어도 경고는 없다. (클린룸 2/2가 독립 재도출한 관심사 — 본 도출은 "
            "이 좌표들을 무방비 축에만 묶어 이 결정을 못 봤다.)"
        ),
        claims=(
            "aws/vm->publicIp/runtime",
            "azure/nic->publicIp/runtime",
            "gcp/vm->publicIp/runtime",
        ),
        citation="depkb 실측 3좌표 — 회복 주소 3사 3색(임시 vs 소유)",
        iso25010=("reliability",),
    ),
    Concern(
        id="cn.cloud-api-access",
        question=(
            "Do the requirements say whether the application itself calls cloud "
            "APIs and therefore needs a cloud identity?"
        ),
        cloud_specific=(
            "세 CSP 모두 클라우드 identity 없이 VM을 만들 수 있다. AWS에서는 VM에 "
            "역할이 없어도 생성은 성공하지만 클라우드 API 호출 기능은 제공되지 않는다. "
            "따라서 애플리케이션이 CSP API를 호출하는지는 요구사항에서 따로 확인해야 한다."
        ),
        claims=(
            "aws/vm->workloadIdentity/provisioning",
            "azure/vm->workloadIdentity/provisioning",
            "gcp/vm->workloadIdentity/provisioning",
            "aws/vm->workloadIdentity/runtime",
        ),
        citation="depkb Docker-on-VM 실측 4좌표 — VM identity 선택성 + AWS 기능 결속",
        iso25010=("security",),
    ),
    Concern(
        id="cn.load-shape",
        question=(
            "Do the requirements describe the shape of the load over time — steady, "
            "bursty, or scheduled peaks — and not only its size?"
        ),
        cloud_specific=(
            "정하지 않으면 카탈로그 경제가 답을 정한다 — 하한만으로 고르면 최저가 "
            "상위가 버스트 인스턴스다(perfkb 실측: 최저가 100 중 37이 버스트·구세대· "
            "공유CPU). 상시 부하에 버스트를 주면 크레딧 소진 시점에 성능이 꺼진다."
        ),
        kb_ref="perfkb",
        citation="perfkb 실측 — 최저가 100 중 37 함정(버스트·구세대·공유CPU)",
        consumer="RESOURCE_SPEC.trafficPattern",
        iso25010=("performance efficiency",),
        # 승계: cn.traffic-shape(구 판, 같은 결정) — 코퍼스 채굴·측정 이력 유효.
        signals=("peak", "burst", "spike", "seasonal", "피크", "급증", "성수기"),
    ),
)

BY_ID: dict[str, Concern] = {c.id: c for c in CONCERNS}


def unmapped_characteristics() -> tuple[str, ...]:
    """어떤 관심사도 걸리지 않은 ISO/IEC 25010 특성.

    실측 재도출 뒤 이 목록은 커졌다 — 실행 환경이 대신 정하는 것을 관측으로만
    입장시키면, 환경이 대신 정하지 않는 특성(interaction capability 등)은 당연히
    비고, **우리가 아직 안 잰 특성**도 빈다. 두 뜻을 이 함수는 못 가른다 —
    해석은 사람이 한다.
    """
    covered = {c for concern in CONCERNS for c in concern.iso25010}
    return tuple(c for c in ISO25010 if c not in covered)


def all_ids() -> tuple[str, ...]:
    """관심사 id 전부(선언 순서)."""
    return tuple(c.id for c in CONCERNS)


def chunks(size: int) -> tuple[tuple[str, ...], ...]:
    """관심사 id를 `size`개씩 묶는다. `size<=0`이면 한 덩어리(전부).

    **선언 순서 그대로 자른다.** 주제별로 묶으면 "나눠 물으면 나아지는가"를 재는 실험에
    주제 응집이라는 두 번째 변수가 섞인다 — 그러면 무엇이 효과를 냈는지 못 가린다.
    """
    ids = all_ids()
    if size <= 0:
        return (ids,)
    return tuple(ids[i:i + size] for i in range(0, len(ids), size))


def prompt_block(only: tuple[str, ...] | None = None) -> str:
    """판정 프롬프트에 실을 관심사 목록.

    고지(`ADVISORY_NOTICE`)를 함께 싣는다 — 관측은 실측이어도 "요구사항이 정할
    결정"이라는 승격은 우리 구성이라고 판정자에게도 말해야 한다.
    """
    picked = CONCERNS if only is None else tuple(BY_ID[i] for i in only)
    lines = [c.prompt_line() for c in picked]
    return "\n".join([*lines, "", ADVISORY_NOTICE])
