"""지식베이스 패키지들이 공유하는 인프라.

각 KB(depkb·costkb·perfkb)는 서로 독립적인 지식 차원이라 **서로 import하지
않는다**. 다만 어느 KB에도 속하지 않는 공용 인프라는 여기 모아 공유한다:

- `fetch.py`         — 공개 스키마 다운로드 + 로컬 캐시 (CFN zip, bicep types, KCC CRD 등)
- `tumblebug_dump.py` — cb-tumblebug 덤프에서 spec_infos 행 읽기 (costkb·perfkb가 공유)
- `artifact.py`      — 빌드 산출물 검증·원자적 쓰기 / 실패를 값으로 돌려주는 읽기

"KB가 아니라 인프라이고, 둘 이상이 쓰면 여기로" — 이 규칙이 KB 간 단방향 규약을 지켜준다.
"""

from __future__ import annotations
