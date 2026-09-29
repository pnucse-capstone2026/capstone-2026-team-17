"""콘솔 출력 인코딩을 맞춘다.

**왜 필요한가.** 이 프로젝트는 안내문·경고를 전부 한국어로 찍는데, Windows 콘솔은
기본이 cp949라 `—`나 `✓` 같은 글자에서 `UnicodeEncodeError`로 **죽는다.**
빌드가 실패하는 것도 아니고 *결과를 보고하다가* 죽으므로 최악이다 —
실제로 `python -m kbcommon verify`가 검사를 다 통과하고 나서 출력에서 터졌다.

사용자에게 `PYTHONIOENCODING=utf-8`을 설정하라고 시키는 방법도 있지만, 그건
"문서에 적어뒀으니 됐다"는 식이고 잊으면 그대로 재발한다. 진입점에서 한 줄로
해결되는 것을 사람에게 미룰 이유가 없다.

`errors="replace"`인 이유: 인코딩을 못 바꾸는 환경에서도 죽지는 않게 하려는 것이다.
글자가 깨져 보이는 것과 도구가 죽는 것은 심각도가 다르다.

**요구사항 쪽에도 같은 것이 있다**(`app/requirements/common/console.py`). 합치지 않는
것이 맞다 — 이 모듈이 있는 `kbcommon`은 프로젝트 내부를 import하지 않는 맨 아래 층이라
상위 단계 패키지를 부를 수 없고, 반대로 상위 단계가 여기를 부르면 실행 도구가 stdout
한 줄 때문에 KB 패키지를 끌어오게 된다. 패키지마다 하나씩이 이 구조의 최소값이다.
"""

from __future__ import annotations

import sys


def use_utf8() -> None:
    """stdout/stderr을 UTF-8로 맞춘다. 진입점에서 한 번 부른다."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass  # 리다이렉트된 파이프 등 — 여기서 죽을 이유는 없다
