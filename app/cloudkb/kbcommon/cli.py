"""KB CLI가 함께 쓰는 배관.

## `build_source` — 파서를 늦게 불러 빌드한다

`_cmd_build`가 KB마다 `if args.source == …: from …parsers import x; x.build(...)`를
늘어놓고 있었다(네 KB 합쳐 스물여덟 갈래). 갈래마다 하는 일은 **"이름으로 파서를 찾아
`build`를 부른다"** 하나인데, import를 정적으로 쓰는 바람에 소스 하나에 세 줄씩 붙었다.

늦게 import하는 것 자체는 의도다 — 파서는 무거운 의존을 끌고 오고, 빌드할 때만 필요하다.
`envkb/__main__.py`가 이미 `importlib`로 그렇게 하고 있었고, 여기는 그 방식을 나머지
KB가 쓸 수 있게 옮긴 것이다.

**`kbcommon`은 여전히 어느 KB도 모른다.** 부르는 쪽이 자기 패키지 이름(`__package__`)을
넘기고, 여기서는 그 패키지 **상대**로 import한다. 절대 경로를 박으면 패키지가 옮겨질 때
AST 재작성이 못 보는 자리가 하나 남는다(easydep 병합에서 실제로 그랬다).

## 표로 접되 다른 것은 다르게 보이게 둔다

소스마다 인자가 같지 않다 — 대부분은 `refresh`뿐이지만 `heuristics`·`prose`·`zip_url`을
받는 것, 아무것도 안 받는 것(`svcmap`), 아예 다른 함수를 부르는 것
(`cfnlint.build_conditions`)이 있다. **표에는 표준 모양만 담고 나머지는 분기로 남긴다** —
전부 표에 넣으려면 소스마다 인자 조립 람다가 필요해지고, 그건 갈래를 없애는 게 아니라
읽기 어려운 곳으로 옮기는 것이다.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any


def build_source(package: str, module: str, output: Path, **kwargs: Any) -> None:
    """`<package>.parsers.<module>.build(output, **kwargs)`.

    `package`는 부르는 쪽의 `__package__`다 — `kbcommon`이 KB 이름을 알지 않게 한다.
    """
    parser = importlib.import_module(f".parsers.{module}", package)
    parser.build(output, **kwargs)
