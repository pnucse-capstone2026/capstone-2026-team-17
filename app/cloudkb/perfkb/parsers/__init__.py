"""perfkb 파서 — `spec_infos.details`(CSP 원본 응답)에서 성능 신호를 뽑는다.

- `details.py` — Go `%v` 포맷 문자열 읽기 (여기가 유일하게 취약한 부분)
- `project.py` — 행 → 성능 레코드 (순수 함수)
- `build.py`   — 행 묶음 → 데이터셋 + 감사 통계
"""

from __future__ import annotations
