"""cb-tumblebug `assets.dump.gz`에서 `spec_infos` 행을 읽는다 (KB 패키지 공유).

## 왜 kbcommon에 있나

이 파일은 어떤 KB에도 속하지 않는 **인프라**다 — 전부 "tumblebug 덤프에서 spec_infos 행을
읽는 법"이지 그 행으로 무엇을 하는가(가격/성능)가 아니다. `costkb`(가격 컬럼)와
`perfkb`(details 컬럼)가 **같은 테이블의 다른 컬럼**을 보므로 둘 다 이 리더가 필요하다.
`fetch_cached`가 여기 있는 것과 같은 이유다 — 여러 KB가 공유하는 건 kbcommon에 둔다.
(원래 `costkb/parsers/dump.py`에 있었으나 perfkb가 두 번째 소비자가 되면서 승격했다.)

덤프는 **PostgreSQL custom format**(v1.15-0, gzip, PG 16.14)이라 `psql`이 아니라
`pg_restore` 계열 파서가 필요하다. 여기서는 `pgdumplib`(BSD-3)를 쓴다.

**선택적 의존성**: 빌드할 때만 필요하다. `uv sync --extra costkb`(또는 `--extra perfkb`) —
둘 다 `pgdumplib`를 끌어온다. graphkb의 neo4j extra와 같은 관례.

**탈출구**: pgdumplib이 미래 덤프 버전에서 깨지면 `--rows-file`로 우회한다 —
    docker run --rm -v "$PWD:/d" postgres:16-alpine \
      pg_restore --data-only -t spec_infos -f - /d/assets.dump > rows.copy
"""

from __future__ import annotations

import gzip
import re
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

from app.cloudkb.kbcommon.fetch import fetch_cached

DEFAULT_TAG = "v0.12.25"
_RAW_URL = (
    "https://raw.githubusercontent.com/cloud-barista/cb-tumblebug/{tag}/assets/assets.dump.gz"
)
TABLE = "spec_infos"
_SCHEMA = "public"

# COPY public.spec_infos (id, uid, ...) FROM stdin;
_COPY_COLUMNS = re.compile(r"\(([^)]*)\)\s+FROM\s+stdin", re.IGNORECASE)

_MISSING_DEP = (
    "pgdumplib이 설치되어 있지 않습니다. cb-tumblebug 덤프는 PostgreSQL custom format이라 "
    "전용 파서가 필요합니다.\n"
    "  uv sync --extra costkb   (또는 --extra perfkb / uv add pgdumplib)\n"
    "costkb는 빌드를 건너뛰어도 번들 36건으로 계속 동작합니다."
)


def dump_url(tag: str = DEFAULT_TAG) -> str:
    return _RAW_URL.format(tag=tag)


def fetch_dump(*, tag: str = DEFAULT_TAG, refresh: bool = False) -> Path:
    """덤프를 받아 gunzip한 경로를 반환한다 (둘 다 캐시)."""
    gz = fetch_cached(dump_url(tag), f"tumblebug-assets-{tag}.dump.gz", refresh=refresh)
    raw = gz.with_suffix("")  # .../tumblebug-assets-v0.12.25.dump
    if refresh or not raw.exists():
        with gzip.open(gz, "rb") as fin, raw.open("wb") as fout:
            shutil.copyfileobj(fin, fout)
    return raw


def _columns_of(entry) -> list[str]:
    """COPY 문에서 컬럼 이름을 뽑는다.

    pgdumplib의 Entry는 컬럼 목록을 따로 노출하지 않고 `copy_stmt`만 준다.
    (실측: spec_infos는 42컬럼)
    """
    match = _COPY_COLUMNS.search(getattr(entry, "copy_stmt", "") or "")
    if not match:
        raise RuntimeError(
            f"{getattr(entry, 'tag', '?')}의 COPY 문에서 컬럼을 읽지 못했습니다. "
            "덤프 형식이 바뀌었을 수 있습니다."
        )
    return [c.strip() for c in match.group(1).split(",")]


def iter_spec_rows(dump_path: Path) -> Iterator[dict]:
    """덤프에서 `spec_infos` 행을 dict로 하나씩 내보낸다.

    Raises:
        RuntimeError: pgdumplib이 없거나, 덤프에 spec_infos가 없거나, 파싱에 실패할 때.
            조용히 빈 결과를 내지 않고 크게 실패한다.
    """
    yield from iter_table_rows(dump_path, TABLE)


def iter_table_rows(dump_path: Path, table: str) -> Iterator[dict]:
    """덤프의 **아무 테이블**이나 dict로 내보낸다.

    처음엔 `spec_infos` 하나만 읽었는데, 같은 덤프에 `image_infos`(174,759행)와
    `latency_infos`(10,890행)가 **안 쓰인 채로** 들어 있었다. 테이블 이름을 상수로
    박아 두면 그런 걸 못 본다.
    """
    try:
        import pgdumplib
    except ImportError as exc:  # pragma: no cover - 환경 의존
        raise RuntimeError(_MISSING_DEP) from exc

    try:
        dump = pgdumplib.load(str(dump_path))
    except Exception as exc:  # noqa: BLE001 - 버전 드리프트를 사용자에게 설명해야 한다
        raise RuntimeError(
            f"덤프를 읽지 못했습니다: {type(exc).__name__}: {exc}\n"
            "pgdumplib이 이 덤프 버전을 지원하지 않을 수 있습니다. 우회 방법:\n"
            '  docker run --rm -v "$PWD:/d" postgres:16-alpine \\\n'
            f"    pg_restore --data-only -t {TABLE} -f - /d/assets.dump > rows.copy\n"
            "  python -m costkb build --rows-file rows.copy   (또는 perfkb)"
        ) from exc

    print(
        f"덤프: PostgreSQL {dump.server_version} / dump {dump.dump_version} "
        f"({dump_path.stat().st_size:,} B)",
        file=sys.stderr,
    )

    entry = next(
        (e for e in dump.entries if e.desc == "TABLE DATA" and e.tag == table), None
    )
    if entry is None:
        available = sorted(
            {e.tag for e in dump.entries if e.desc == "TABLE DATA" and e.tag is not None}
        )
        raise RuntimeError(
            f"덤프에 {table} 테이블이 없습니다. 있는 것: {available}. "
            "업스트림이 스키마를 바꿨을 수 있습니다(94MB→32.8MB 축소 전례 있음)."
        )

    columns = _columns_of(entry)
    for row in dump.table_data(_SCHEMA, table):
        yield dict(zip(columns, row, strict=False))


def iter_rows_from_copy_file(path: Path) -> Iterator[dict]:
    """`pg_restore --data-only -f -` 산출물(COPY 텍스트)에서 행을 읽는다 (탈출구).

    pgdumplib이 못 읽는 덤프를 사용자가 pg_restore로 변환해 넘길 때 쓴다.
    """
    columns: list[str] | None = None
    in_data = False
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not in_data:
                if line.startswith("COPY ") and TABLE in line:
                    columns = _columns_of(
                        type("E", (), {"copy_stmt": line})()  # _columns_of 재사용
                    )
                    in_data = True
                continue
            if line.startswith("\\."):
                in_data = False
                continue
            values = [None if v == "\\N" else v for v in line.rstrip("\n").split("\t")]
            yield dict(zip(columns or [], values, strict=False))
    if columns is None:
        raise RuntimeError(f"{path}에서 {TABLE}의 COPY 블록을 찾지 못했습니다.")
