"""설계 지침 고지의 접근점.

설계 지침에서 나온 답에는 *"설계 지침이지 클라우드 사실이 아니다"*라는 고지가 붙는다.
현재 제품 코드는 이 고지만 사용하며 과거 `patternkb` 코퍼스에는 의존하지 않는다.

## 왜 접근점인가 (사본이 아니라)

과거에는 상수가 `patternkb.model`에 있어 제품 요구사항 코드가 보존 후보 패키지를 import했다.
현재는 활성 코드가 소유하고, legacy 코드는 필요하면 이 접근점을 사용해야 한다.
"""
from __future__ import annotations

#: 이 축의 유일한 근거 라벨. basis는 **영원히 `inferred`**다 — 설계 산문은 사람이
#: 검수해도 클라우드 사실이 되지 않는다.
EVIDENCE: str = "pattern-advisory"

#: 관심사·패턴을 실은 모든 출력에 붙는 고지. **어떤 출력 경로에서도 떼면 안 된다.**
ADVISORY_NOTICE: str = (
    "※ This is design guidance, not a cloud fact — confirm values, limits, and "
    "verdicts with the knowledge-base tools (kb_*/cap_*/cost_*)."
)
