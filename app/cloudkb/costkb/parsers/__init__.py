"""costkb 투영 로직.

덤프에서 spec_infos 행을 읽는 리더는 `kbcommon/tumblebug_dump.py`에 있다(perfkb와 공유).
여기(`tumblebug.py`)는 그 행을 costkb 레코드로 투영하는 것만 담당한다.
"""

from __future__ import annotations
