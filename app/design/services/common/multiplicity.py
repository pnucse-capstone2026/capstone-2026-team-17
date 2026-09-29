"""다중도 어휘 한 벌 — 표기·정규화·크로우풋 기호.

## 왜 한 곳인가

다중도를 아는 곳이 셋이다: 클래스 다이어그램 렌더러(그림에 라벨을 찍는다), ERD 사상
(외래키가 어디 붙는지 정한다), 검출기(모델이 제대로 적었는지 판정한다). 이 셋이 표기
목록을 **각자 적어 두고 있었다.** 그때는 우연히 일치했지만 아무것도 그것을 강제하지
않았고, 이 저장소는 같은 모양의 실패를 이미 겪었다 — 규칙이 프롬프트 산문과 코드에
따로 있어서 갈라진 일(`knowledge/rules.py` docstring).

갈라지면 어떻게 되는지가 특히 고약하다: 검출기는 "제대로 적었다"고 통과시키는데 사상은
"모르는 표기"라며 관계를 안 옮긴다. 그러면 **아무 지적도 없이 선이 사라진다.**

## 정규화는 지어내기가 아니다

`0..*`는 UML에서 `*`와 **정확히 같은 뜻**이고, `1..1`은 `1`과 같다. 이걸 거절하던 때가
있었는데, 그건 "모르면 지어내지 않는다"가 아니라 **같은 것을 같다고 못 읽는 것**이었다.
옳게 쓴 모델이 결함으로 보고되고 재생성 예산이 표기 바꾸기에 쓰였다.

경계는 분명하다. **표준이 같다고 정한 것만 접는다.**

  - 접는다: `0..*` → `*`, `1..1` → `1`, 그리고 공백(`0 .. 1`)
  - 안 접는다: `n` · `N` · `many` · `0..N` · `여러 개`

뒤엣것은 표기가 아니라 **해석**이다. `n`을 `*`로 읽는 것은 이름에서 의도를 읽는 것과
같은 종류의 추측이고, 그 추측을 지운 것이 이 ERD 작업의 출발점이었다.
"""
from __future__ import annotations

import re

#: 사상이 아는 표기. 이 넷이 다중도의 전부다.
CANONICAL: tuple[str, ...] = ("1", "0..1", "*", "1..*")

#: 표준이 같다고 정한 표기 → 정규형. **해석이 아니라 같은 것을 같다고 읽는 것이다.**
EQUIVALENT: dict[str, str] = {"0..*": "*", "1..1": "1"}

#: 정규형 → "여럿인가".
_MANY = {"1": False, "0..1": False, "*": True, "1..*": True}
#: 정규형 → "반드시 있어야 하는가"(하한이 1 이상인가).
_MANDATORY = {"1": True, "0..1": False, "*": False, "1..*": True}

#: 정규형 → 크로우풋 기호. 왼쪽 끝과 오른쪽 끝의 표기가 다르다(거울상이다).
_CROW_LEFT = {"1": "||", "0..1": "|o", "1..*": "}|", "*": "}o"}
_CROW_RIGHT = {"1": "||", "0..1": "o|", "1..*": "|{", "*": "o{"}

_SPACES = re.compile(r"\s+")


def normalize(raw: object) -> str:
    """받은 표기를 정규형으로. **모르는 표기는 빈 문자열이다.**

    빈 문자열은 "다중도가 아니다"라는 뜻이고, 그것이 "명시 안 됨"과 같은 값인 것은
    의도한 것이다 — 둘 다 *사상할 근거가 없다*는 같은 결론으로 간다. 다만 지적 문구는
    둘을 구별한다(`detectors.entity_association_multiplicity`): 안 적은 것과 못 읽는
    것을 같은 말로 지적하면 고치는 쪽이 무엇을 해야 할지 모른다.
    """
    text = _SPACES.sub("", str(raw or ""))
    text = EQUIVALENT.get(text, text)
    return text if text in CANONICAL else ""


def is_known(raw: object) -> bool:
    """정규화해서 아는 표기가 되는가."""
    return bool(normalize(raw))


def is_many(multiplicity: str) -> bool:
    """이 끝이 여럿인가. **정규형만 받는다** — 부르기 전에 `normalize()`를 지난다."""
    return _MANY[multiplicity]


def is_mandatory(multiplicity: str) -> bool:
    """이 끝이 반드시 있어야 하는가(하한이 1 이상인가)."""
    return _MANDATORY[multiplicity]


def crow_left(multiplicity: str) -> str:
    """선의 **왼쪽** 끝 기호."""
    return _CROW_LEFT[multiplicity]


def crow_right(multiplicity: str) -> str:
    """선의 **오른쪽** 끝 기호. 왼쪽과 거울상이라 표가 따로 있다."""
    return _CROW_RIGHT[multiplicity]


def label(raw: object) -> str:
    """클래스 다이어그램의 관계 끝에 붙일 `"1"` 같은 라벨. 모르는 표기면 빈 문자열.

    **정규형을 그린다.** 모델이 `0..*`라고 적었어도 그림에는 `*`가 나간다 — 같은 뜻이고,
    그림과 사상이 다른 글자를 쓰면 읽는 사람이 둘을 다른 것으로 본다.

    모르는 표기를 안 그리는 쪽을 고른 이유: 라벨은 `"..."` 안의 자유 텍스트라 무엇이든
    통과하고, 통과한 오타는 그림에서 사실처럼 보인다. 판정은 검출기가 하고
    (`class.entity-association-multiplicity`), 렌더러는 아는 것만 그린다.
    """
    canonical = normalize(raw)
    return f'"{canonical}"' if canonical else ""
