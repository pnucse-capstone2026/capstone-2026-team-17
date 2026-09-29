"""costkb: 클라우드 인스턴스 스펙·가격 지식베이스.

"무엇을 살 수 있고 얼마인가" — 인스턴스 타입 카탈로그(vCPU/메모리)와 온디맨드 정가.

`depkb`가 리소스 의존성을, 이 패키지가 VM 후보와 가격을 담당한다.
costkb는 **카탈로그**라 출처가
파일 단위로 균일하다 — 레코드별 evidence는 죽은 필드가 되므로 두지 않고, `_note`가 담당한다.

**cb-tumblebug MCP와의 관계 — 여기가 핵심이다.**
MCP의 `recommend_vm_spec`은 **같은 질문에 답하는 다른 소스**다(축이 다른 게 아니다).
그래서 costkb는 그 **오프라인 미러**로 만든다:

    recommend_vm_spec → POST /recommendSpec → FilterSpecsByRange → spec_infos
    costkb build      → assets.dump.gz(같은 spec_infos) → output/tumblebug-cost.json

같은 테이블에서 빌드하므로 오프라인 기준선과 라이브 경로가 **같은 세계**를 본다.
AWS/Azure 공개 API에서 직접 빌드하면 오히려 불일치가 생긴다(자세한 건
`parsers/tumblebug.py`의 메모리 버그 설명 참고).

두 소스:
- `specs.json` — 번들 36건(손 큐레이션). 빌드 안 해도 서버·자격증명 없이 즉시 동작.
- `output/tumblebug-cost.json` — `costkb build`가 만든 3사 VM 미러. 있으면 이쪽을 쓴다.
"""

from __future__ import annotations

__version__ = "0.1.0"
