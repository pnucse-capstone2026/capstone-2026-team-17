"""perfkb 산출물의 레코드 간 데이터 일관성 검사.

perfkb도 cb-tumblebug 덤프에서 나오지만 costkb와 달리 **성능 신호를 골라 담는다.**
그래서 같은 검사라도 뜻이 다르다 — 현재 위반 0건인 것은 우연이 아니라 담는 과정에서
걸러졌다는 뜻이고, 0이 깨지면 그 필터가 뚫렸다는 신호다.

성능 KB에서는 과대 진술이 과소 진술보다 해롭다는 원칙(감사 §5-6)에 따라, 여기서는
가속기 모순을 `error`로 둔다. costkb 쪽은 미러 계약 때문에 `report`다.

얼개는 `kbcommon/invariants.py` 참고.
"""

from __future__ import annotations

import collections
from collections.abc import Iterable

from app.cloudkb.kbcommon.invariants import Invariant, Violation, accelerator_fields_agree

# 리전에 무관하다고 **믿고 있는** 신호. 경고 판단이 여기 걸려 있다.
_ASSUMED_REGION_INVARIANT = ("sustainedCpu", "currentGeneration")


def _region_invariant_signals(dataset: dict) -> Iterable[Violation]:
    """리전 불변이라고 가정한 신호가 실제로 불변인가.

    `dataset.py`의 리전 접기는 이 가정 위에 서 있다 — 어느 리전 레코드를 골라도
    같으니 하나만 써도 된다는 것이다. 실측상 다리전 스펙 3,221종 전부에서 참이지만,
    **가정을 참이라고 적어 두는 것과 매 빌드 확인하는 것은 다르다.** 상류가 리전별로
    다른 값을 주기 시작하면 접기가 조용히 임의의 답을 내게 된다.
    """
    grouped: dict[tuple, list[dict]] = collections.defaultdict(list)
    for rec in dataset.get("specs") or []:
        if isinstance(rec, dict):
            grouped[(rec.get("provider"), str(rec.get("specName", "")).lower())].append(rec)

    for (provider, spec), recs in sorted(grouped.items()):
        if len(recs) < 2:
            continue
        for field in _ASSUMED_REGION_INVARIANT:
            values = {
                r[field].get("value") if isinstance(r.get(field), dict) else None
                for r in recs
                if r.get(field) is not None
            }
            if len(values) > 1:
                yield Violation(
                    where=f"{provider} {spec}",
                    detail=f"{field}이 리전마다 {sorted(map(str, values))}로 갈립니다",
                )


INVARIANTS = (
    Invariant(
        name="accelerator-fields-agree",
        question="가속기 종류가 적힌 스펙에 개수도 적혀 있는가?",
        severity="error",
        check=accelerator_fields_agree("specs"),
    ),
    Invariant(
        name="region-invariant-signals",
        question="리전 불변이라고 가정한 성능 신호가 실제로 불변인가?",
        # error인 이유: 이 가정이 깨지면 리전 접기가 임의의 답을 내기 시작한다.
        # 조용히 틀린 성능 단언을 하느니 빌드가 서는 편이 낫다.
        severity="error",
        check=_region_invariant_signals,
    ),
)
