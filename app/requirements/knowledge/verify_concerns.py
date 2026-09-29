"""관심사의 실측 좌표를 대조한다 — **CI에서 도는 검사**.

    python -m app.requirements.knowledge.verify_concerns

## 2026-08-02 — 대조 대상이 바뀌었다

구 판은 문헌 좌표(`doc_id`·`probe`)를 패턴 코퍼스와 대조했다. 실측 재도출 뒤
관심사의 근거는 **claims.json의 주장 키**이고, 대조도 그쪽으로 옮겼다:

  1. 관심사가 인용한 주장 키가 `claims.json`에 실재하는가.
  2. **좌표 유일성** — 한 주장 키는 한 관심사에만 속한다(미분화 트립와이어,
     구 판 doc_id 유일성의 계승).
  3. claims가 빈 관심사는 `kb_ref`가 실재하는 실측 KB인가.

## 왜 여기서만 `app/cloudkb`를 import하는가

`app/requirements`는 `app/cloudkb` 없이 돌아야 한다(`knowledge/basis.py`). 그
규약이 지키는 것은 **런타임 경로**다. 이 모듈은 파이프라인이 아니라 개발·CI 도구이고,
어디에서도 import되지 않는다(격리 검사가 지킨다).
"""
from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.requirements.knowledge import concerns


@dataclass(frozen=True)
class Verdict:
    """관심사 하나의 좌표 대조 결과."""

    concern_id: str
    #: claims.json에 없는 주장 키들.
    missing: tuple[str, ...] = ()
    #: 다른 관심사도 인용한 주장 키들 (키, 상대 관심사).
    shared: tuple[tuple[str, str], ...] = ()
    #: kb_ref가 필요한데 없거나, 있는데 import되지 않는다.
    kb_problem: str = ""

    @property
    def ok(self) -> bool:
        return not (self.missing or self.shared or self.kb_problem)


def load_claim_keys() -> set[str]:
    """claims.json의 주장 키 전부. 빈 집합은 성공이 아니라 실패다."""
    path = (
        Path(__file__).resolve().parents[2]
        / "cloudkb"
        / "depkb"
        / "claims.json"
    )
    doc = json.loads(path.read_text(encoding="utf-8"))
    keys = {f"{c['csp']}/{c['subject']}->{c['object']}/{c['relationFamily']}"
            for c in doc["claims"]}
    if not keys:
        raise SystemExit("claims.json이 비어 있다 — 대조가 성립하지 않는다.")
    return keys


def verify(claim_keys: set[str] | None = None) -> list[Verdict]:
    """관심사 전부를 대조한다."""
    keys = load_claim_keys() if claim_keys is None else claim_keys

    owner: dict[str, str] = {}
    verdicts: list[Verdict] = []
    for concern in concerns.CONCERNS:
        missing = tuple(k for k in concern.claims if k not in keys)
        shared = tuple((k, owner[k]) for k in concern.claims if k in owner)
        for k in concern.claims:
            owner.setdefault(k, concern.id)
        kb_problem = ""
        if not concern.claims:
            if not concern.kb_ref:
                kb_problem = "claims도 kb_ref도 없다"
            else:
                try:
                    importlib.import_module(f"app.cloudkb.{concern.kb_ref}")
                except ImportError as exc:
                    kb_problem = f"kb_ref {concern.kb_ref!r}: {exc}"
        verdicts.append(Verdict(concern.id, missing=missing, shared=shared,
                                kb_problem=kb_problem))
    return verdicts


def main() -> int:
    verdicts = verify()
    failed = [v for v in verdicts if not v.ok]
    for verdict in verdicts:
        mark = "OK  " if verdict.ok else "FAIL"
        line = f"{mark} {verdict.concern_id}"
        if verdict.missing:
            line += f"  <- claims에 없다: {list(verdict.missing)}"
        if verdict.shared:
            line += f"  <- 좌표 겹침: {list(verdict.shared)}"
        if verdict.kb_problem:
            line += f"  <- {verdict.kb_problem}"
        print(line)

    total = sum(len(c.claims) for c in concerns.CONCERNS)
    print(f"\n대조 {len(verdicts)}건(좌표 {total}개) · 실패 {len(failed)}건")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
