"""규범의 출처: 책이 그렇게 적었는가, 우리가 정했는가.

## 왜 이게 먼저인가

이 에이전트의 검증 지적은 전부 "규칙 위반"이라는 형태로 나온다. 그런데 그 규칙들의
출처가 서로 다르다. `p.81`(공유 인증은 include가 아니다)은 Cockburn이 그렇게 적어 둔
것이고, UI 금지 단어 8개는 **그가 든 예시에서 우리가 일반화한 것**이다. 후자는 실제로
완전목록이 아니고, 그 사실이 `step3_specifications.py` 주석에 적혀 있었지만 **산출물에는
실리지 않았다.** 사용자는 두 지적을 같은 무게로 읽는다.

그래서 규칙마다 출처의 성격을 달아 두고, 짐작인 것은 답에 유보를 붙인다.
`app/cloudkb/kbcommon/basis.py`가 클라우드 사실에 세운 규율과 같다.

## 왜 그 파일을 import하지 않고 어휘를 다시 쓰는가

어휘는 공유하고 코드는 공유하지 않는다. 라벨 표는 각 축이 무엇을 근거로 삼는지에 대한
그 축의 선언이라, 한쪽이 라벨을 늘릴 때 다른 쪽이 따라 늘어나면 안 된다.

**의존 규약은 2026-07-28에 바뀌었다.** 예전에는 *"`app/requirements`는 `app/cloudkb`
없이 돌아야 한다(데이터셋 수백 MB)"*였는데, 좁은 표면에서는 그 근거가 성립하지 않았다 —
`appkb.contract` + `envkb.regions`는 446ms · 모듈 168개이고 import 시점에 데이터셋을
읽지 않는다(실측). 지금 규약은 소유권이 분명한 문을 하나씩 두는 것이다. 요구사항의
자원 계약은 `app.requirements.resources`, 공급자 사실은 `app.cloudkb`의 공개 표면을
통해서만 읽는다. 공용 계측은 `app.metrics`가 소유한다.

## 등급을 둘로만 나눈다

  - **`stated`** — 책·표준이 그렇게 적었다. 우리는 옮겼을 뿐이고 **페이지를 댈 수 있다.**
  - **`inferred`** — 우리가 정했거나, 예시에서 일반화했거나, 책 내용이라고 알지만
    저장소 안에서 페이지를 확인할 수 없다. **틀릴 수 있다.**

`observed`(코퍼스에서 세어 나온 것)는 두지 않는다. 아직 세는 것이 없다 — 실행 코퍼스에서
결함 패턴을 세기 시작하면 그때 만든다. 없는 등급을 미리 만들면 라벨이 등급을 못 받는다.

## `reviewed` 축이 없는 이유

deployment는 "짐작이라도 사람이 검수했으면 사실"이라는 축을 둔다. 여기서는 **그 주장을
저장소 안에서 할 수 없다.** 페이지 인용을 사람이 교차검증한 기록(`cockburn-grounding.md`)이
저작권 정리 때 저장소 밖으로 나갔고(`e10c527`), 책 자체도 나갔다(`d1a7ec5`). 그래서 검수
여부가 아니라 **"페이지를 댈 수 있는가"로 가른다.** 페이지를 못 대는 규칙은 짐작으로
두고, 책을 가진 사람이 페이지를 채우면 등급이 올라간다 — 라벨이 곧 할 일 목록이다.
"""
from __future__ import annotations

STATED = "stated"
INFERRED = "inferred"

VALUES = (STATED, INFERRED)

#: evidence 라벨 → 근거의 성격. **라벨 하나에 성격 하나**가 규칙이다.
BASIS_OF_EVIDENCE: dict[str, str] = {
    # --- Cockburn, Writing Effective Use Cases (Addison-Wesley, 2001) ---
    # 책이 그 페이지에서 그렇게 말한다. citation에 `p.N`이 있어야 한다.
    "cockburn-page": STATED,
    # 번호 붙은 Guideline. 판(印刷)에 따라 페이지가 밀려도 번호는 안 밀린다.
    "cockburn-guideline": STATED,
    # 장(章) 수준 근거. 페이지보다 거칠지만 확인 가능한 좌표다.
    "cockburn-chapter": STATED,
    # 책이 든 **예시**에서 우리가 일반화한 것. UI 금지 단어 목록이 이것이고,
    # 그는 금지 목록을 명문화한 적이 없다 → **완전목록이 아니다.**
    "cockburn-example": INFERRED,
    # 책이 **관찰로** 적은 것(예: 스텝 3~9개). 관찰은 규범이 아니다 —
    # 이걸 규칙으로 쓰면 개수를 맞추려고 내용을 늘리거나 자른다.
    "cockburn-observation": INFERRED,
    # 책 내용이라고 알고 있으나 **페이지를 확인하지 못했다.** 페이지가 확인되면
    # `cockburn-page`로 올라간다. 지금은 하나도 없다 — 로컬 사본으로 전부 확인했다.
    "cockburn-unpinned": INFERRED,
    # **페이지는 확인했지만 규범 문장이 우리 것이다.** 책이 개념(예: 보증이 사후조건의
    # 자리다)을 말한 지점을 확인했고, 거기서 우리가 구체적 적용(예: 로깅·감사는 스텝이
    # 아니라 보증이다)을 끌어냈다.
    #
    # 이 라벨은 `cockburn-unpinned`과 갈라야 했다. 둘을 한 라벨로 두면 고지가 거짓이
    # 된다 — "페이지를 못 댄다"와 "페이지는 댔는데 결론이 우리 것"은 반대 방향의 한계다.
    # (`basis.py`의 규율: 라벨 하나에 성격 하나. 갈리면 라벨을 쪼갠다.)
    "cockburn-extrapolated": INFERRED,
    # --- 그 밖 ---
    # OMG UML 명세가 정한 표기(관계 화살표 방향 등).
    "uml-spec": STATED,
    # **우리가 정한 규약.** 책 근거가 아니다. 스키마 무결성·필수 필드처럼
    # 우리 산출물 형식에서만 뜻이 있는 것들.
    "project-convention": INFERRED,
    # 규칙이 아니라 공학적 가드(프롬프트 크기 상한 등). 판정에 쓰면 안 된다.
    "engineering-guard": INFERRED,
    # --- 클라우드 네이티브 관심사(`knowledge/concerns.py`) ---
    # 설계 산문(12-factor · Azure 패턴 · Well-Architected)에서 나온 것. **영원히
    # `inferred`다** — 다른 라벨은 페이지를 확인하면 `stated`로 오를 길이 있지만 이건
    # 없다. 산문 지침은 사람이 검수해도 클라우드 사실이 되지 않는다
    # (`app/cloudkb/patternkb/model.py`가 자기 축에 세운 규율과 같다).
    "pattern-advisory": INFERRED,
}


def basis_of(evidence: str) -> str:
    """근거 라벨의 성격. **모르는 라벨은 짐작으로 본다.**

    등록을 잊은 라벨이 조용히 사실로 취급되면 안 된다 — 안전한 쪽으로 틀리는 편이 낫다.
    """
    return BASIS_OF_EVIDENCE.get(evidence, INFERRED)


def needs_hedge(evidence: str) -> bool:
    """이 규칙을 근거로 지적할 때 **유보를 붙여야 하는가.**

    유보의 대상은 **위반 여부가 아니라 규범의 출처**다. "resume_at_step 5가 주 시나리오에
    없다"는 결정론적으로 참이고 흔들리지 않는다 — 흔들리는 것은 "그게 결함이라고 누가
    정했는가"다. 그래서 유보 문구(`Rule.caveat`)는 위반을 의심하라는 말이 아니라
    출처의 한계를 밝히는 말이어야 한다.
    """
    return basis_of(evidence) != STATED


# 사람이 읽을 한 마디. 숫자 대신 **무엇을 근거로 아는지**를 적는다.
_WORDS = {
    STATED: "stated by the source",
    INFERRED: "our reading",
}


def describe(evidence: str) -> str:
    """근거 라벨을 사람이 읽을 한 마디로."""
    return _WORDS.get(basis_of(evidence), "source unknown")


# 검증 프롬프트에 실을 영어 고지. **짐작이라고만 적으면 부정확하다** — 왜 짐작인지가
# 라벨마다 다르고, 그 차이가 모델의 판정 태도를 바꾼다. "책이 말한 적 없다"와 "책의
# 원칙이지만 페이지를 못 댄다"는 전혀 다른 말이다.
_PROMPT_NOTES = {
    "cockburn-unpinned": "the source's principle, but this project could not verify the page",
    "cockburn-extrapolated": (
        "the cited page states the concept; this specific application is this project's reading"
    ),
    "cockburn-example": (
        "generalised from the source's examples — the list is not exhaustive, so judge the "
        "intent rather than matching words"
    ),
    "cockburn-observation": "the source states this as an observation, not as a rule",
    "project-convention": "this project's rule, not the source's",
    "engineering-guard": "an engineering guard, not a rule",
    "pattern-advisory": (
        "design guidance from cloud-native literature, not a cloud fact — it says what "
        "is worth deciding, never what the answer is"
    ),
}


def prompt_note(evidence: str) -> str | None:
    """프롬프트에 붙일 출처 고지. 명시된 근거면 붙일 것이 없다.

    등록되지 않은 짐작 라벨은 뭉툭한 문구라도 반드시 붙인다 — 고지가 아예 빠지면
    모델이 우리 규약을 원저자의 말처럼 단언한다.
    """
    if not needs_hedge(evidence):
        return None
    return _PROMPT_NOTES.get(evidence, "this project's reading, not stated by the source")
