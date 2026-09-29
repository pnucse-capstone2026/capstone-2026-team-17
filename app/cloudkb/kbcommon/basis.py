"""근거의 성격: 원본이 명시했는가, 우리가 짐작했는가.

## 왜 숫자를 버렸나

예전에는 `confidence`가 0.5~1.0의 연속값이었다. **척도의 정의가 어디에도 없었다** —
0.9와 0.95의 차이가 무엇인지, 두 근거를 합치면 어떻게 되는지, 0.8이 "80% 확률"인지
"꽤 믿는다"인지 아무도 답할 수 없었다. 실제로 쓰이는 방식도 임계값 하나
(`>= 0.8`)뿐이었고, 그 임계선이 실제로 가른 것은 `cfn-description` 158건 중
**115건이 정확히 0.8**이라 상수를 0.81로만 바꿔도 결과가 뒤집히는 상태였다.

중요한 것은 얼마나 믿느냐가 아니라 **사실이냐 아니냐**다. 그래서 둘로만 나눈다.

- **`stated`** — 원본이 그렇게 적어 놓았다. 우리는 옮겼을 뿐이다.
  CFN 스키마의 `minimum`, bicep의 `flags`, KCC ServiceMapping의 `gvk.kind`처럼
  기계가 읽으라고 만든 구조화 필드가 여기 속한다.
- **`inferred`** — 우리가 짐작했다. 이름이 비슷하다거나, 산문에 그렇게 쓰여 있다거나,
  구조가 그렇게 생겼다는 이유로 만든 것이다. **틀릴 수 있다.**

여기에 기존 `reviewed`가 세 번째 축으로 붙는다. 짐작이라도 사람이 눈으로 보고
맞다고 한 것은 사실로 취급한다 — 그게 이 저장소가 검수 파일을 두는 이유다.

    사실로 취급 = (basis == "stated") or reviewed

## 세 번째 등급 — `observed`

번들 축을 열면서 둘로 안 되는 것이 나왔다. "Azure VM 템플릿 330개 중 **100.0%**에
네트워크 인터페이스가 있다"는 사실은

- `stated`가 아니다 — Azure 문서가 그렇게 말한 적 없다. **우리가 센 것**이다.
- `inferred`도 아니다 — 짐작이 아니라 **측정**이고, 세는 방법을 적어 두면 누구나
  같은 숫자를 얻는다.

그래서 `observed`를 둔다. 뜻은 **"이 코퍼스에서 그렇게 나왔다"**이지
**"클라우드가 그렇게 강제한다"가 아니다.** 이 경계를 흐리면 표본 편향이 곧 사실이
된다(Quickstart 템플릿은 데모 쪽으로 기울어, VM과 스토리지 계정이 53.6%로 같이
나오는데 그건 옛 부트 진단 관행의 흔적이다).

판정에는 쓰지 않는다 — 100%가 "없으면 안 된다"를 증명하지 못한다. 고지는 반드시
붙인다. 표본 수도 함께 담아야 한다(17개짜리 앵커의 35.3%는 6건이다).

## 질문이 둘이다 — 판정과 고지

옛 계획(P2a)은 "신뢰도 < 0.7이면 경고"였다. 그 선은 `check`가 쓰던 0.8과도 어긋났고,
신뢰도 자체를 버리면서 **전제가 통째로 사라졌다.** 승계는 숫자를 다른 숫자로 바꾸는
일이 아니라, **섞여 있던 두 질문을 가르는** 일이다.

    is_fact      — 이 근거로 값을 **거부해도** 되는가       검수가 면제된다
    needs_hedge  — 답에 **유보를 붙여야** 하는가            검수는 면제가 아니다

검수의 뜻이 두 방향에서 다르기 때문이다. 사람이 확인한 제약으로 값을 **막는** 것은
정당하다 — 확인했으니까. 하지만 사람이 "가장 가깝다"고 고른 클라우드 간 동치를
**단언**하는 것은 정당하지 않다 — 확인한 것은 *우리의 판단이 그렇다*는 사실이지
*원본이 그렇게 선언했다*는 사실이 아니다.

실측이 이걸 가른다. "AWS ALB는 GCP에서 뭐야?"에 모델이 `ComputeForwardingRule`을
**단언**했다. 그 엣지는 검수된 짐작이었다 — `is_fact`로는 사실이고 `needs_hedge`로는
유보 대상이다. 두 함수가 갈리지 않으면 이 답을 막을 수 없다.

## 라벨이 갈리면 라벨을 쪼갠다

한 evidence 라벨은 성격이 하나여야 한다. 갈린다면 그 라벨이 **서로 다른 두 가지를
뭉뚱그리고 있다**는 뜻이다. 실제로 그런 라벨이 둘 있었고, 신뢰도가 두 값으로 갈리는
것으로 먼저 드러났다(`kbcommon/invariants.py`의 one-basis-per-evidence):

    kcc-ref          1.0 = ServiceMapping의 구조화 필드   ← 명시
                     0.9 = 설명문 정규식                   ← 짐작
    azure-limits-doc 0.9 = 표의 숫자                       ← 명시
                     0.7 = 각주·비수치 표현                ← 짐작

그래서 짐작 쪽을 각각 `kcc-description`, `azure-limits-note`로 떼어 냈다.
"""

from __future__ import annotations

STATED = "stated"
INFERRED = "inferred"
#: 코퍼스에서 **세어 나온** 것. 원본이 말한 것도, 우리가 짐작한 것도 아니다.
#: 자세한 건 위 "세 번째 등급" 절 참고.
OBSERVED = "observed"

VALUES = (STATED, INFERRED, OBSERVED)

# evidence 라벨 → 근거의 성격. **라벨 하나에 성격 하나**가 규칙이다.
BASIS_OF_EVIDENCE: dict[str, str] = {
    # --- graphkb ---
    "relationshipRef": STATED,      # CFN 스키마의 relationshipRef 필드
    "swagger-field": STATED,        # (구) Azure swagger의 arm-id 메타데이터
    "arm-id": STATED,               # (구) 같은 계열
    "arm-hierarchy": STATED,        # ARM 타입명이 곧 계층 (규칙 위반 0/2,223 확인)
    "kcc-ref": STATED,              # KCC ServiceMapping의 gvk.kind
    # projectRef/folderRef/organizationRef/billingAccountRef — GCP 자원 계층.
    # 이름이 KCC의 규약이고 설명문이 "belongs to"라고 말한다(projectRef 273/296).
    "kcc-hierarchy": STATED,
    "cdk-oob": STATED,              # AWS CDK가 손으로 정리해 배포하는 관계표
    # AVM 모듈의 dependsOn. **"모듈이 이 순서로 배포한다"**이지 "API가 강제한다"가
    # 아니다 — tpg-schema의 ForceNew와 같은 구분이다. 모듈 저자가 명시한 것이라 stated다.
    "avm-dependson": STATED,
    "kcc-description": INFERRED,    # CRD 설명문 정규식
    "bicep-ref": INFERRED,          # 대상 객체에 id가 있는지로 판별 (검수표로 확정)
    "heuristic": INFERRED,          # 이름 끝 맞추기
    "cb-spider-driver": INFERRED,   # 드라이버 코드를 사람이 읽고 만든 매핑
    "human-review": STATED,         # 사람이 직접 적어 넣은 것
    # --- svcmap (앱 개념 ↔ 벤더 관리형 서비스) ---
    # 넷 다 INFERRED다. MS 표가 명시하는 것은 **서비스 이름 수준의 대응**이고,
    # 그걸 타입 id에 붙이는 마지막 걸음은 언제나 우리 손 검수다 — 다리를 건너면
    # 등급이 떨어진다. 라벨이 갈리는 것은 **어떤 독립 근거가 뒤에 있는가**다.
    "svcmap-cross-checked": INFERRED,   # MS 비교표 + diagrams 분류가 일치
    "ms-learn-comparison": INFERRED,    # MS 비교표만
    "mingrammer-taxonomy": INFERRED,    # diagrams 분류만
    "svcmap-reviewed": INFERRED,        # 독립 근거 없이 손 검수만 (타입 이름이 용도를 말하는 경우)
    # --- capacitykb ---
    "cfn-schema": STATED,           # 스키마 키워드 그대로
    "bicep-flags": STATED,          # flags 비트
    "bicep-type": STATED,           # 스칼라 타입의 제약 필드
    "azure-limits-doc": STATED,     # 한도 문서 표의 숫자
    "azure-limits-note": INFERRED,  # 각주·"varies"처럼 숫자가 아닌 표현
    "cfn-description": INFERRED,    # 설명문에서 뽑은 숫자
    # KCC(GCP). 셋 다 **원본이 명시한 것**이다.
    "kcc-crd-schema": STATED,       # OpenAPI 스키마 키워드 그대로
    "kcc-cel-immutable": STATED,    # x-kubernetes-validations의 self == oldSelf (기계가 강제)
    # `Immutable.` 접두사는 산문에 있지만 KCC의 **표기 규약**이다. CEL과 대조해보니
    # 접두사가 있는데 CEL이 변경을 허용하는 모순은 0건이고, 누락만 19건 있었다.
    # 즉 과다 보고를 하지 않으므로 짐작이 아니라 명시로 취급한다.
    "kcc-immutable-prefix": STATED,
    # 생성된 terraform-provider-google의 스키마 리터럴 그대로.
    # 주의: ForceNew는 "Terraform이 재생성한다"이지 "API가 거부한다"가 아니다.
    "tpg-schema": STATED,
    # terraform-provider-aws. 같은 Terraform이지만 google과 성격이 다르다 —
    # 저쪽은 생성 코드고 이쪽은 사람이 쓴 것이라 틀리는 방식이 다르다.
    "tpaws-schema": STATED,
    # alicloud·tencent provider. 같은 Terraform이지만 **대조할 짝이 없다** —
    # aws는 CFN, gcp는 KCC라는 독립 소스가 있어 어긋남을 셀 수 있었는데 이 둘은
    # 공개 리소스 스키마가 없어 이게 유일한 소스다. 프로바이더가 적어 둔 것이라
    # stated지만, 교차 검증이 없다는 사실은 산출물 coverage에 적는다.
    "tpcsp-schema": STATED,
    # cfn-lint가 별도 관리하는 리전별 허용값. 손 큐레이션이지만 원본이
    # 그렇게 적어 놓은 것이라 stated다.
    "cfn-lint-region": STATED,
    "cfn-lint-conditional": STATED,
    # Azure 명세가 직접 단 주석(`x-ms-mutability`)이다. 교차 검증할 짝이 없는
    # 단일 소스이지만, 우리가 짐작한 게 아니라 원본이 적어 둔 것이라 stated다.
    "swagger-mutability": STATED,
    # 같은 Azure 명세가 직접 단 주석(`x-ms-secret`). 비밀값이라는 표시를 원본이
    # 붙인 것이라 stated다. swagger-mutability와 같은 소스·같은 성격이다.
    "swagger-secret": STATED,
    # AWS 한도. 두 공식 소스가 **같은 값을 말했을 때만** 이 라벨을 단다 —
    # 어느 한쪽만으로는 담지 않는다. 단일 미검증 소스가 감사에서 나온
    # "확신에 찬 오답"의 원인이었다.
    "aws-cross-checked": STATED,
    # --- perfkb ---
    # 이 두 필드는 **한 방향만** 직접 말한다 — "버스트다"/"공유 코어다".
    "aws-burstable-field": STATED,   # BurstablePerformanceSupported=true
    "gcp-shared-cpu-field": STATED,  # IsSharedCpu=true
    # 반대 방향은 "그렇게 분류되지 않았다"에서 끌어낸 추론이다. t1.micro가 이 추론이
    # 깨지는 실례다 — AWS가 false를 주지만 상시 성능이 보장되지는 않는다.
    "aws-non-burstable-inferred": INFERRED,
    "gcp-dedicated-cpu-inferred": INFERRED,
    "azure-family-name": INFERRED,
    # 인스턴스에 실제로 붙어 있는 하드웨어. 측정이 아니라 사양이다.
    "ec2-hardware-probe": STATED,  # family가 standardB로 시작하는지
    # azure 크기 문서 표의 숫자(NIC 수·네트워크 대역폭). azure-limits-doc과 같은
    # 성격 — 문서가 표로 명시한 것을 옮겼다.
    "azure-sizes-doc": STATED,
    # Cyclenerd의 GCP 카탈로그(시리즈 SQL + pricing.yml 인스턴스 항목).
    # Google 문서를 커뮤니티가 옮긴 것이라 mingrammer와 같은 등급 — 짐작(큐레이션).
    "cyclenerd-gcp-catalog": INFERRED,
    # --- bundlekb ---
    # AVM 모듈이 **무엇을 배포하는지**. `avm-dependson`(배포 순서)과 같은 소스지만
    # 다른 축이다. 모듈 저자가 그렇게 짠 것이라 stated지만, "API가 강제한다"는
    # 뜻이 아니라는 점도 `avm-dependson`과 같다.
    "avm-module": STATED,
    # cb-tumblebug이 VM 하나에 실제로 만드는 것(provisioning.go). **우리 실행 경로**의
    # 사실이지 클라우드의 요구가 아니다.
    "tumblebug-dynamic": STATED,
    # cb-tumblebug의 손 큐레이션 템플릿(`init/templates`).
    "tumblebug-template": STATED,
    # AWS가 공식으로 묶어 둔 패턴 이름(`aws-solutions-constructs`).
    "aws-solutions-construct": STATED,
    # Config Connector 샘플. Google이 예시로 고른 최소 동작 구성이지
    # API가 강제하는 집합은 아니다 — avm-module과 같은 경계.
    "kcc-sample": STATED,
    # 실제 템플릿 코퍼스에서 **센** 동시 출현. 클라우드 사실이 아니다.
    "aqt-corpus": OBSERVED,
    # 같은 방법을 AWS에 적용한 것. 표본이 훨씬 얇다는 사실은 coverage에 적는다.
    "awscfn-corpus": OBSERVED,
    # 명시 소스와 코퍼스가 **같은 답을 냈을 때만** 단다. `aws-cross-checked`와 같은 규율.
    "bundle-cross-checked": STATED,
    # --- patternkb ---
    # 설계 지침 **산문**의 검색 결과다. 영원히 자문(advisory)이다 — 검수해도
    # 클라우드 사실이 되지 않는 성격이라 reviewed를 붙이지 않는 것이 규약이고,
    # 값·한도·판정의 근거로 쓰면 안 된다(모든 답에 그 고지가 붙는다).
    "pattern-advisory": INFERRED,
    # --- sizingkb ---
    # cb-tumblebug이 손으로 큐레이션한 표. 원본 문서 링크까지 달려 있다.
    # **채움이 고르지 않다** — 비어 있는 칸은 "0"이 아니라 "안 적었다"이다.
    "tumblebug-networkinfo": STATED,
    "tumblebug-k8sinfo": STATED,
    # bitnami 공용 차트의 규모 프리셋. 원본이 "프로덕션용 아님"을 스스로 적었다.
    "bitnami-preset": STATED,
}


def basis_of(evidence: str) -> str:
    """근거 라벨의 성격. **모르는 라벨은 짐작으로 본다.**

    새 라벨이 조용히 사실로 취급되면 안 된다 — 등록을 잊었을 때 안전한 쪽으로
    틀리는 편이 낫다.
    """
    return BASIS_OF_EVIDENCE.get(evidence, INFERRED)


def is_fact(basis: str, reviewed: bool = False) -> bool:
    """사실로 취급할 수 있는가.

    짐작이라도 사람이 확인했으면 사실이다. 소스에 핀이 박혀 있으므로
    (`kbcommon/sources.py`) 그 확인은 다음 빌드에서도 유효하다.

    **`observed`는 사실이 아니다.** 코퍼스에서 100%가 나와도 그건 표본이 그랬다는
    뜻이지 클라우드가 강제한다는 뜻이 아니다 — 그걸로 값을 거부하면 안 된다.
    """
    return basis == STATED or (reviewed and basis != OBSERVED)


def needs_hedge(basis: str, reviewed: bool = False) -> bool:
    """답에 유보를 붙여야 하는가. **검수는 여기서 면제가 아니다.**

    `reviewed`를 받고도 쓰지 않는 것이 핵심이다 — 호출부가 "검수 인자를 빼먹었나"
    하고 되묻지 않도록 시그니처를 `is_fact`와 맞춰 두고, 안 쓴다는 사실을 여기
    한 곳에 적는다. 검수된 짐작은 **판정에는 쓰되 단언하지는 않는다.**

    예전엔 이 판단이 이름 없이 `basis != "stated"` 리터럴로 graphkb·perfkb에
    따로 박혀 있었다. 같은 규칙이 두 벌이면 갈라진다 — perfkb의 필드 목록 세 벌이
    실제로 갈라졌던 것과 같은 모양이다.
    """
    return basis != STATED


# 화면에 보여줄 말. 숫자 대신 **무엇을 근거로 아는지**를 그대로 적는다.
_WORDS = {
    STATED: "stated by the source",
    INFERRED: "a guess",
    # "실측"이라고만 쓰면 클라우드를 잰 것처럼 읽힌다. 무엇을 셌는지 밝힌다.
    OBSERVED: "counted in real templates",
}


def describe(basis: str, reviewed: bool = False) -> str:
    """사람이 읽을 한 마디. `0.8` 같은 숫자보다 이쪽이 실제 정보를 준다."""
    if basis == INFERRED and reviewed:
        return "a guess (reviewed)"
    return _WORDS.get(basis, "source unknown")
