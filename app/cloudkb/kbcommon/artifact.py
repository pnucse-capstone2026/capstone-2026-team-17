"""빌드 산출물 쓰기·읽기 (지식베이스 패키지 공유).

**왜 공용인가**: costkb와 perfkb가 같은 실수를 같은 모양으로 하고 있었다 —
`build_dataset()` 결과를 검증 없이 바로 `write_text`하고, 로드 측은 `exists()`만
확인한 뒤 `json.loads` + `validate`를 무방비로 불렀다. 결과:

- 상위 스키마가 드리프트하면(새 프로바이더 `oracle` 등) 빌드가 ⚠️ 한 줄만 찍고
  **exit 0**으로 끝나며 깨진 파일을 남긴다. 이후 모든 로드가 `ValidationError`를
  던져 **`output/`를 손으로 지우기 전까지 복구 불가**였다(결함 C2).
- 빌드가 쓰기 도중 끊기면 잘린 JSON이 남아, 성능 도구가 아니라 **비용 추천 전체가
  죽었다**(결함 (마)).

그래서 두 가지를 여기서 강제한다:

1. **쓰기 전에 검증하고, 원자적으로 바꿔친다.** 검증에 실패하면 파일을 아예 만들지
   않는다 — 나쁜 산출물이 존재하지 않는 것이 존재하는 것보다 낫다. 기존 산출물이
   있으면 그대로 살아남는다.
2. **읽기는 실패를 값으로 돌려준다.** 예외를 던지면 호출자가 폴백할 기회를 잃는다.
   "파일 없음"과 "파일이 깨짐"은 다른 사건이라 구분해서 알려준다.

`fetch.py`의 `.part` + `os.replace` 패턴을 그대로 따른다.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import TypeVar

#: 병합 가능한 KB 모델(`merge`를 가진 것). `kbcommon`은 위 층을 모르므로 타입만 받는다.
_T = TypeVar("_T")

import fastjsonschema
import jsonschema
import orjson

from app.cloudkb.kbcommon.invariants import Invariant, Result, run


class ArtifactInvalid(Exception):
    """산출물이 스키마를 위반해 쓰기를 거부했다."""


def write_dataset(
    path: Path,
    dataset: dict,
    schema: dict,
    invariants: Sequence[Invariant] = (),
) -> Result:
    """검증에 통과한 데이터셋만 원자적으로 쓴다.

    두 단계로 본다. **스키마**는 레코드 하나의 형태를, **데이터 일관성 규칙**은 레코드 사이의
    정합성을 본다(`kbcommon/invariants.py`). 후자가 없어서 `default=0, min=10` 같은
    모순이 그대로 산출물이 됐다.

    Returns:
        데이터 일관성 검사 결과. `report` 등급 위반이 담겨 있으니 **호출자가 반드시 알려야 한다.**
        조용히 버리면 침묵이 "문제 없음"으로 읽힌다.

    Raises:
        ArtifactInvalid: 스키마 위반 또는 `error` 등급 데이터 일관성 규칙 위반.
            **이때 파일은 만들어지지 않는다.**
    """
    try:
        jsonschema.validate(dataset, schema)
    except jsonschema.ValidationError as exc:
        # 어디가 틀렸는지 알려주지 않으면 빌드 로그만 보고는 고칠 수 없다.
        location = "/".join(str(p) for p in exc.absolute_path) or "(최상위)"
        raise ArtifactInvalid(f"{location}: {exc.message}") from exc

    result = run(dataset, invariants)
    if not result.ok:
        raise ArtifactInvalid("레코드 간 모순으로 쓰기를 거부했습니다\n" + result.summary())

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(
        json.dumps(dataset, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)
    return result


@lru_cache(maxsize=8)
def _compiled(schema_json: str):
    return fastjsonschema.compile(json.loads(schema_json))


def _validate_fast(data: dict, schema: dict) -> str | None:
    """읽기 경로 전용 검증. 통과하면 None, 아니면 사람이 읽을 설명.

    **왜 쓰기와 다른 검증기를 쓰나.** 읽기는 세션 첫 도구 호출마다 일어나는데,
    `jsonschema`는 순수 파이썬이라 여기서 **거의 전부의 시간**을 먹었다(실측:
    costkb 로드 30.5초 중 28.8초, perfkb 50.5초 중 48.6초 — 94~96%).
    `fastjsonschema`는 스키마를 파이썬 코드로 컴파일해서 **18배 빠르다**
    (28.8초 → 1.6초).

    **바꾸기 전에 같은 것을 거부하는지 확인했다.** 일부러 망가뜨린 입력 10가지
    (숫자 자리에 문자열·필수 칸 삭제·enum 밖의 값·목록 자체 없음)에서 두 검증기의
    판정이 **전부 일치**했다. 빠른데 조용히 통과시키면 느린 것보다 나쁘다.

    쓰기 경로는 `jsonschema`를 그대로 쓴다 — 거기서는 "어디가 틀렸는지"(경로)를
    알려주는 게 중요하고, 빌드당 한 번뿐이라 느려도 된다.
    """
    try:
        _compiled(json.dumps(schema, sort_keys=True))(data)
    except fastjsonschema.JsonSchemaException as exc:
        return str(exc)
    return None


def load_json(path: Path) -> dict:
    """`.gz`든 아니든 읽는다.

    **바이트로 읽어 orjson에 그대로 넘긴다.** 표준 `json`은 순수 파이썬 파서라
    28MB 파일 하나에 1.9초가 든다(실측). orjson은 같은 파일을 0.58초에 읽는다
    (3.3배). 텍스트로 디코드한 뒤 넘기면 그 이득이 깎이므로 바이트를 준다.

    검증기와 달리 여기는 **판정이 갈릴 여지가 없다** — 같은 JSON을 같은 파이썬
    값으로 읽을 뿐이다. 그래도 결과가 같은지는 테스트로 고정했다.
    """
    if path.suffix == ".gz":
        import gzip

        with gzip.open(path, "rb") as handle:
            return orjson.loads(handle.read())
    return orjson.loads(path.read_bytes())


def read_dataset(path: Path, schema: dict) -> tuple[dict | None, str | None]:
    """산출물을 읽어 검증한다. **예외를 던지지 않는다.**

    Returns:
        (데이터, 오류설명). 셋 중 하나다:
        - `(data, None)`  — 정상.
        - `(None, None)`  — 파일 없음. 아직 빌드 안 한 것이지 오류가 아니다.
        - `(None, "…")`   — 파일이 깨졌다. 호출자는 폴백하되 **이 사실을 밝혀야 한다.**
    """
    if not path.exists():
        return None, None
    try:
        # `.gz`(저장소에 커밋한 산출물)도 그대로 읽는다. 여기서 안 받으면 폴백이
        # 조용히 "빌드 안 됨"으로 떨어진다 — 실제로 그렇게 한 번 새어 나갔다.
        data = load_json(path)
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"cannot read {path}: {exc}"
    except json.JSONDecodeError as exc:
        return None, f"{path} is not intact JSON (it may be a half-written file): {exc}"
    error = _validate_fast(data, schema)
    if error:
        return None, f"{path} violates the schema — {error}"
    return data, None


REBUILD_HINT = "rebuild it or delete the file"


# --- 저장소에 커밋된 산출물 ------------------------------------------------
#
# 클론 직후 빌드 없이 동작하게 하려고 gzip한 산출물을 `data/`에 커밋한다.
# 기본 실행은 재현 가능한 `data/`를 먼저 읽는다. `output/`은 로컬 빌드 작업공간이며
# 번들되지 않은 산출물에만 폴백으로 사용한다. 과거 KB가 남긴 같은 이름의 평문 파일이
# 커밋된 현행 데이터를 조용히 덮어쓰지 못하게 하는 경계다.
#
# **폴백은 기본 위치에서만 한다.** 호출자가 다른 디렉터리를 명시하면 그 말을
# 그대로 지킨다 — 테스트가 빈 tmp 디렉터리를 묶어 "미빌드 상태"나 "번들 36건
# 폴백"을 검사하는데, 거기서 커밋된 데이터가 슬쩍 끼어들면 그 테스트들이
# 검사하려던 상황 자체가 사라진다.
#
# 커밋한 파일에는 `_source` 블록(핀·sha256)이 그대로 들어 있어 어느 소스 어느
# 커밋에서 나왔는지가 파일 안에 남는다. 핀 규약이 약해지는 게 아니라 검증
# 가능해지는 쪽이다.

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLED_DIR = REPO_ROOT / "data"
DEFAULT_OUTPUT = REPO_ROOT / "output"


def load_records(output_dir: Path | str, name: str) -> list[dict]:
    """산출물 하나의 `records` 목록. 파일이 없거나 못 읽으면 **빈 목록**.

    "없으면 빈 것"이 예외가 아니라 정상인 자리들이 있다 — 재배포 허가가 없어 커밋하지
    않는 산출물(Azure Retail), 로컬 빌드 전용(AWS Price List), 보강이 아직 안 된 축.
    그 자리마다 같은 다섯 줄(resolve → None이면 빈 것 → load_json → 실패면 빈 것)이
    적혀 있었다. **키를 어떻게 만들지는 부르는 쪽이 정한다** — 소문자로 색인하는 축이
    따로 있어서(대소문자로 조인이 세 번 깨졌다) 그건 공유할 수 있는 것이 아니다.

    `.gz`(배포 번들)와 평문을 둘 다 읽는다 — `read_text`로 직접 읽으면 gz 바이트를
    UTF-8로 디코드하려다 죽는다(빌드 안 한 사용자 경로).
    """
    found = resolve(output_dir, name)
    if found is None:
        return []
    try:
        data = load_json(found)
    except (OSError, ValueError):
        return []
    return list(data.get("records") or [])


def load_merged(
    output_dir: Path | str, names: Sequence[str], empty: Callable[[], _T],
    from_dict: Callable[[dict], _T],
) -> _T | None:
    """여러 산출물을 하나로 병합해 돌려준다. 하나도 못 찾으면 `None`.

    **KB마다 같은 열 줄을 다시 적고 있었다** — `capacitykb`와 `graphkb`의 사본은 주석까지
    같았고(`# output/ 이 먼저, …`), 클래스 이름과 파일 목록만 달랐다. 사본이 둘이면
    한쪽만 고쳐진다: 실제로 `CapacitySet.load`는 `artifact.load_json`을 거쳐 커밋된
    `data/*.gz`를 읽는데 `Graph.load`는 raw `read_text`라 못 읽는 격차가 있었고, 지금은
    graphkb가 평문 `output/*.json`만 먹어서 드러나지 않고 있을 뿐이다.

    `empty`/`from_dict`를 **인자로 받는다** — `kbcommon`은 위 층(KB)을 알지 않는다는 것이
    이 패키지의 규약이라(`tests/test_architecture.py`), 모델을 import할 수 없다.
    """
    merged = empty()
    found = False
    for name in names:
        # 기본 위치에서는 저장소에 커밋된 data/*.gz가 우선한다.
        path = resolve(output_dir, name)
        if path is not None:
            merged.merge(from_dict(load_json(path)))  # type: ignore[attr-defined]
            found = True
    return merged if found else None


def resolve(output_dir: Path | str, name: str) -> Path | None:
    """산출물 하나의 실제 경로. 없으면 None.

    `output/<name>` → `data/<name>.gz` 순으로 본다. `output_dir`가 기본 위치가
    아니면 폴백하지 않는다(위 주석 참고).

    **상대 경로는 CWD가 아니라 저장소 기준이다.** KB들의 기본값이 상대
    `Path("output")`인데, easydep처럼 CWD가 다른 프로세스에 임포트되면 CWD
    해석은 일부 KB만 조용히 미빌드 상태로 만들었다 — 배포 구성에서 svcmap
    대응과 네트워크 계층이 통째로 빠진 채 "미결"로 답했다(실측 2026-07-24).
    같은 저장소의 데이터를 어디서 부르든 같게 찾는 것이 옳다.
    """
    base = Path(output_dir)
    if not base.is_absolute():
        base = REPO_ROOT / base
    try:
        is_default = base.resolve() == DEFAULT_OUTPUT
    except OSError:
        is_default = False
    if not is_default:
        fresh = base / name
        return fresh if fresh.exists() else None
    packed = BUNDLED_DIR / f"{name}.gz"
    if packed.exists():
        return packed
    fresh = base / name
    return fresh if fresh.exists() else None


