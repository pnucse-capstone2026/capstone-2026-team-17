"""설계 규범의 출처: 누가 그렇게 정했는가.

## 왜 이게 먼저인가

설계 검증의 지적은 전부 "규칙 위반"이라는 형태로 나온다. 그런데 그 규칙들의 출처가 서로
다르다. "Boundary와 Entity를 직접 잇지 않는다"는 Jacobson의 BCE 규율이고, "관계의 양 끝이
클래스 목록에 있어야 한다"는 **우리 파이프라인이 요구하는 것**이며, "PlantUML이 선언되지
않은 이름을 만나면 엔티티를 하나 더 만든다"는 **도구의 동작을 실측한 것**이다. 성격이 다른
셋을 같은 무게로 내보내면 사용자는 구별할 수 없다.

`app/requirements/knowledge/basis.py`가 요구사항 규칙에 세운 규율과 같다. 어휘는 공유하고
코드는 공유하지 않는다 — `app/design`은 `app/requirements`를 import하지 않고(그 격리는
현재 위반 0건이다), 라벨 표는 각 축이 무엇을 근거로 삼는지에 대한 **그 축의 선언**이라
한쪽이 늘릴 때 다른 쪽이 따라 늘어나면 안 된다.

## 등급을 둘로 나눈다

  - **`stated`** — 출처가 그렇게 적었거나, 도구가 실제로 그렇게 동작하는 것을 **이 저장소
    안에서 확인했다.** 좌표나 재현 절차를 댈 수 있다.
  - **`inferred`** — 우리가 정했거나, 원문을 확인하지 못했다. **틀릴 수 있다.**

## Jacobson 인용을 승격시키지 않는다

`services/class_diagram/extractor.py`의 프롬프트는 "Jacobson, 1992"와 "UML 2.0 Robustness
Analysis"를 근거로 내세운다. 그러나 그 책은 이 저장소에 없고 페이지를 대조한 기록도 없다.
규칙을 프롬프트에서 이리로 옮기면서 **그 주장을 검증된 인용으로 올리지 않는다** — 옮기는
일은 확인하는 일이 아니다. 그래서 `jacobson-unpinned`은 `inferred`이고, 지적 문구에 "우리
판단"이 붙는다. 책 사본을 가진 사람이 페이지를 채우면 `jacobson-page`를 만들어 올린다.
라벨이 곧 할 일 목록이다.

커밋 `5c5c96c`("지어낸 것을 표시 없이 섞지 않는다")가 이 저장소의 규약이고, 여기가 그
규약이 설계 쪽에서 지켜지는 자리다.
"""
from __future__ import annotations

STATED = "stated"
INFERRED = "inferred"

VALUES = (STATED, INFERRED)

#: evidence 라벨 → 근거의 성격. **라벨 하나에 성격 하나**가 규칙이다.
#: 성격이 갈리면 라벨을 쪼갠다(뭉치면 고지 문구 중 하나가 반드시 거짓이 된다).
BASIS_OF_EVIDENCE: dict[str, str] = {
    # --- Jacobson, Object-Oriented Software Engineering (1992) ---
    # BCE(Boundary-Control-Entity) / 로버스트니스 분석의 원칙이라고 **알고 있으나**
    # 이 저장소 안에서 페이지를 확인하지 못했다. 책이 저장소에 없다.
    # 페이지가 확인되면 `jacobson-page`(STATED)를 만들어 올린다.
    "jacobson-unpinned": INFERRED,
    # --- 도구의 실측된 동작 ---
    # `plantuml.jar`를 실제로 돌려 확인한 것. `citation`에 재현 절차가 있어야 하고,
    # 확인은 테스트가 다시 한다(`tests/test_design_detectors.py`).
    # **실측하지 않은 도구 동작은 여기 넣지 않는다** — 넣는 순간 등급이 거짓이 된다.
    "plantuml-measured": STATED,
    # --- 우리 파이프라인 ---
    # 저장소 안의 코드가 그렇게 요구한다. `citation`이 그 코드 위치를 가리키므로
    # 확인 가능하다 — 다만 "그래야 옳다"가 아니라 "지금 그렇게 되어 있다"이다.
    "pipeline-invariant": STATED,
    # **우리가 정한 규약.** 출처 근거가 아니다. 이름 유일성·표기법처럼 우리 산출물
    # 형식에서만 뜻이 있는 것들.
    "project-convention": INFERRED,
    # 규칙이 아니라 관찰이나 공학적 가드. **판정에 쓰면 안 된다.**
    "engineering-guard": INFERRED,
}


def basis_of(evidence: str) -> str:
    """근거 라벨의 성격. **모르는 라벨은 짐작으로 본다.**

    등록을 잊은 라벨이 조용히 사실로 취급되면 안 된다 — 안전한 쪽으로 틀리는 편이 낫다.
    """
    return BASIS_OF_EVIDENCE.get(evidence, INFERRED)


def needs_hedge(evidence: str) -> bool:
    """이 규칙을 근거로 지적할 때 **유보를 붙여야 하는가.**

    유보의 대상은 **위반 여부가 아니라 규범의 출처**다. "관계 GhostEntity가 클래스 목록에
    없다"는 결정론적으로 참이고 흔들리지 않는다 — 흔들리는 것은 "그게 결함이라고 누가
    정했는가"다. 그래서 유보 문구는 위반을 의심하라는 말이 아니라 출처의 한계를 밝히는
    말이어야 한다.
    """
    return basis_of(evidence) != STATED


#: 사람이 읽을 한 마디. 숫자 대신 **무엇을 근거로 아는지**를 적는다.
_WORDS = {
    STATED: "확인된 근거",
    INFERRED: "우리 판단",
}


def describe(evidence: str) -> str:
    """근거 라벨을 사람이 읽을 한 마디로."""
    return _WORDS.get(basis_of(evidence), "출처 불명")


#: 생성 프롬프트에 실을 영어 고지. **짐작이라고만 적으면 부정확하다** — 왜 짐작인지가
#: 라벨마다 다르고, 그 차이가 모델의 태도를 바꾼다. "출처를 확인 못 했다"와 "우리가
#: 정했다"는 전혀 다른 말이다.
_PROMPT_NOTES = {
    "jacobson-unpinned": (
        "attributed to the source's Boundary-Control-Entity discipline, but this project "
        "could not verify the page — judge the intent, not the wording"
    ),
    "project-convention": "this project's rule, not the source's",
    "engineering-guard": "an engineering guard, not a rule",
}


def prompt_note(evidence: str) -> str | None:
    """프롬프트에 붙일 출처 고지. 확인된 근거면 붙일 것이 없다.

    등록되지 않은 짐작 라벨은 뭉툭한 문구라도 반드시 붙인다 — 고지가 아예 빠지면 모델이
    우리 규약을 원저자의 말처럼 단언한다.
    """
    if not needs_hedge(evidence):
        return None
    return _PROMPT_NOTES.get(evidence, "this project's reading, not stated by any source")
