"""인용 좌표를 로컬 도서 사본과 대조한다 — 로컬 전용 명령.

    python -m app.requirements.knowledge.verify_citations

## 왜 필요했나

인용을 손으로 옮겨 적으면 틀린다. 실제로 틀렸다 — 2026-07-26에 이 검사를 처음 돌려
두 건을 잡았다.

  - `p.64`("자동결과는 guarantee"의 근거, 규칙 **둘**이 `stated`로 인용) → 그 페이지는
    Ch. 5(Three Named Goal Levels)이고 보증을 다루지 않는다. 보증은 Ch. 6, p.83이다.
  - `p.207`("include가 기본 관계") → 그 페이지는 Reminder 5("Who Has the Ball?")다.
    거기 "rule of thumb"이라는 말이 있어서 그 단어로 찾다가 잘못 붙은 것으로 보인다.
    관계를 다루는 곳은 Ch. 10(Linking Use Cases)이다.

둘 다 **틀린 페이지를 근거로 "책이 그렇게 말했다"고 단언하던 상태**였다. 근거를 대는
시스템에서 이건 근거가 없는 것보다 나쁘다.

## 저작권 경계

읽는 것은 로컬 사본(`materials/Usecase_Knowledge/`, gitignore됨)이고, **책에서 나온
글자는 아무것도 저장소에 들어가지 않는다.** 규칙이 담는 것은 우리 표현의 규범 문장,
인용 좌표, 그리고 그 페이지에 있어야 하는 짧은 열쇠 단어(`Rule.probe`)뿐이다. 이 명령이
내는 것도 통과/실패 판정이다 — 발췌를 출력하지 않는다.

## 인쇄 페이지 ≠ 물리 페이지

PDF의 첫 페이지가 인쇄된 1페이지가 아니다(앞머리). 오프셋은 **측정한다** — 각 물리
페이지에서 홀로 있는 숫자 줄을 인쇄 번호로 보고, `물리 - 인쇄`의 최빈값을 쓴다. 상수로
박으면 판(印刷)이 다른 사본에서 조용히 어긋난다.

`pypdf`는 런타임 의존성이 아니다(`requirements.txt`가 일부러 뺀 목록에 있다). 없으면
안내하고 실패한다 — `app/cloudkb/patternkb/parsers/aws_waf.py`와 같은 규약이다.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.requirements.knowledge import rules

#: 로컬 사본이 놓이는 자리. gitignore돼 있다(`.gitignore`의 materials/Usecase_Knowledge/).
DEFAULT_BOOK = Path("materials/Usecase_Knowledge/Writing Effective Use Cases.pdf")

_PAGE_NUMBER = re.compile(r"^(\d{1,3})$")


@dataclass(frozen=True)
class Verdict:
    """규칙 하나의 인용 대조 결과."""

    rule_id: str
    citation: str
    #: 페이지에서 찾지 못한 열쇠 단어들. 비어 있으면 통과.
    missing: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing


def _pages_text(pdf_path: Path) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 로컬 전용 경로
        raise SystemExit(
            "pypdf가 필요하다 (로컬 전용 명령이라 requirements.txt에 없다): "
            "pip install pypdf"
        ) from exc
    reader = PdfReader(str(pdf_path))
    return [(page.extract_text() or "").lower() for page in reader.pages]


def measure_offset(pages: list[str]) -> int:
    """`물리 인덱스 - 인쇄 페이지 번호`의 최빈값.

    페이지마다 인쇄 번호가 홀로 있는 줄로 나타나는 것을 이용한다. 표·목차처럼 숫자만
    있는 줄도 걸리지만, 최빈값을 쓰므로 잡음에 흔들리지 않는다.
    """
    votes: Counter[int] = Counter()
    for index, text in enumerate(pages):
        for line in text.splitlines():
            match = _PAGE_NUMBER.match(line.strip())
            if match:
                votes[index - int(match.group(1))] += 1
    if not votes:
        raise SystemExit("인쇄 페이지 번호를 하나도 찾지 못했다 — 이 사본으로는 대조할 수 없다.")
    return votes.most_common(1)[0][0]


def load_pages(pdf_path: Path = DEFAULT_BOOK) -> list[str]:
    """사본에서 페이지 텍스트를 뽑는다(소문자). 301쪽 파싱이라 값싸지 않다 — 재사용하라."""
    if not pdf_path.exists():
        raise SystemExit(
            f"로컬 사본이 없다: {pdf_path}\n"
            "저작물이라 저장소에 없다 — 각자 사본을 그 경로에 두면 된다(gitignore됨)."
        )
    return _pages_text(pdf_path)


def verify(pdf_path: Path = DEFAULT_BOOK) -> list[Verdict]:
    """`probe`가 있는 규칙 전부를 대조한다. 없는 규칙은 대상이 아니다."""
    return verify_pages(load_pages(pdf_path))


def verify_pages(pages: list[str]) -> list[Verdict]:
    """이미 뽑아 둔 페이지 텍스트로 대조한다(파싱을 한 번만 하고 싶을 때)."""
    offset = measure_offset(pages)

    verdicts: list[Verdict] = []
    for rule in rules.RULES:
        if not rule.probe:
            continue
        haystack = ""
        for printed in rule.pages:
            index = printed + offset
            if 0 <= index < len(pages):
                haystack += pages[index]
        missing = tuple(key for key in rule.probe if key not in haystack)
        verdicts.append(Verdict(rule.id, rule.citation, missing))
    return verdicts


def main() -> int:
    verdicts = verify()
    failed = [v for v in verdicts if not v.ok]
    for verdict in verdicts:
        mark = "OK  " if verdict.ok else "FAIL"
        line = f"{mark} {verdict.rule_id}  ({verdict.citation})"
        if verdict.missing:
            line += f"  <- 그 페이지에 없다: {list(verdict.missing)}"
        print(line)

    unverifiable = [r.id for r in rules.RULES if r.from_book and not r.probe]
    if unverifiable:
        print(f"\n대조할 좌표가 없는 도서 인용: {unverifiable}")
    print(f"\n대조 {len(verdicts)}건 · 실패 {len(failed)}건")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
