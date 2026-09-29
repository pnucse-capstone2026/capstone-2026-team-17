"""규칙 지식베이스 — 이 에이전트가 무엇을 결함이라 부르고, 그 근거가 무엇인지.

  - `rules.py`      규칙 레코드(우리 표현의 규범 문장 + 인용 좌표). 단일 소스.
  - `basis.py`      근거의 성격(책이 적었나 / 우리가 정했나)과 유보 판단.
  - `detectors.py`  결정론 검출기. 규칙 하나에 검출기 하나.

**책 본문은 없다.** 인용은 자기 사본을 가진 사람이 확인하는 좌표다(`rules.py` docstring).

이 패키지는 `app.requirements` 안의 다른 무엇도 import하지 않는다 — 프롬프트도, 단계도,
설정도. 지식은 파이프라인 모양을 몰라야 파이프라인을 갈아엎을 때 따라온다.
"""
from app.requirements.knowledge import basis, detectors, rules

__all__ = ["basis", "detectors", "rules"]
