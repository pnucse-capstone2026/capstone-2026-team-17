"""cb-tumblebug 미러 **전수 인벤토리** — 무엇이 들어 있고 우리가 무엇을 안 쓰는가.

    python -m app.cloudkb.tools.tumblebug_inventory --out report/…/inventory.json

## 왜 있나

`perfkb-field-axis-plan-2026-07-29.md`가 진단한 뿌리는 이것이다: **미러가 어디까지
답하는지 세어 보지 않고 외부 소스를 추가했다.** azure `maxNics`를 채우려고 문서를
새로 받아왔는데 미러가 이미 100% 답하고 있었다.

그 병은 세는 도구가 없어서 생겼다. 그래서 세는 도구를 먼저 만든다 — **측정은 코드로
남고, 결론만 문서로 간다.** 문서에 표를 적어 두면 다음 스냅샷에서 그 표가 조용히
거짓이 된다(질의집·분류표에서 이미 두 번 겪었다).

## 무엇을 세나

  1. 덤프의 **테이블 전부**와 행 수 — `spec_infos`만 보던 눈을 넓힌다.
  2. 테이블마다 **컬럼별 채움률·값 종류 수·예시**.
  3. `spec_infos.details`의 **프로바이더별 키**(최상위 + 중첩 1단), 채움률은
     **행 단위와 (프로바이더, 스펙이름) 단위 둘 다** — 리전 편중이 채움률을 왜곡하는
     것이 계획서의 위협 T4다.

`--distinct-cap`에서 값 종류 수집을 멈춘다. `12+`는 "12 이상"이지 정확한 수가 아니다.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

from app.cloudkb.kbcommon.console import use_utf8
from app.cloudkb.kbcommon.tumblebug_dump import fetch_dump, iter_table_rows

#: 중첩 1단을 열어 볼 키들. **여기서 멈추는 이유가 계획서 §4.1의 포화 기준이다** —
#: 중첩 2단은 배열 안의 배열이라 `go_field`가 원리적으로 스칼라를 못 뽑는다.
NESTED = (
    "EbsInfo", "NetworkInfo", "ProcessorInfo", "VCpuInfo", "InstanceStorageInfo",
    "GpuInfo", "BundledLocalSsds", "Accelerators", "EbsOptimizedInfo",
    "InferenceAcceleratorInfo", "FpgaInfo", "PlacementGroupInfo", "MemoryInfo",
)

#: 중첩 1단을 읽을 때 **깊이를 세면서** 자른다.
#:
#: 정규식으로 하면 안 된다는 것을 실측으로 배웠다 — 첫 판은 `Key:{...}`를 정규식으로
#: 찾았는데 중첩 키가 **한 건도 안 잡혔다**(값 안에는 부모 키 이름이 없으므로). 두
#: 번째 함정은 `NetworkInfo`처럼 블록 안에 배열이 또 있는 경우다. 깊이를 세면 둘 다
#: 정확해지고, **못 뽑는 자리를 `blocked`로 남길 수 있다**(포화의 조건).
def _top_level_pairs(content: str) -> tuple[dict[str, str], list[str]]:
    """`a:1,b:{...},c:[...]` → ({스칼라 키: 값}, [더 깊어서 못 읽은 키]).

    중첩 2단에서 멈추는 것이 계획서 §4.1의 포화 기준이고, **멈추는 이유를 남기는 것**이
    그 기준의 조건이다.
    """
    scalars: dict[str, str] = {}
    deeper: list[str] = []
    depth = 0
    part = []
    parts: list[str] = []
    for ch in content:
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(part))
            part = []
            continue
        part.append(ch)
    if part:
        parts.append("".join(part))

    for raw in parts:
        key, _, value = raw.partition(":")
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if value.startswith(("{", "[")):
            deeper.append(key)
        else:
            scalars[key] = value
    return scalars, deeper


class Counter:
    """한 키에 대한 실측 — 채움·값 종류·예시. **값 종류는 상한에서 멈춘다.**"""

    def __init__(self, cap: int) -> None:
        self.filled = 0
        self.values: set[str] = set()
        self.capped = False
        self.cap = cap
        #: 타입 단위 채움 — (프로바이더, 스펙이름)을 한 번만 센다(위협 T4).
        self.types: set[str] = set()

    def see(self, value: object, type_key: str) -> None:
        text = "" if value is None else str(value).strip()
        if text == "" or text == "\\N":
            return
        self.filled += 1
        self.types.add(type_key)
        if len(self.values) < self.cap:
            self.values.add(text[:80])
        else:
            self.capped = True

    def as_dict(self, rows: int, types: int) -> dict:
        return {
            "fillRows": round(self.filled / rows, 4) if rows else 0.0,
            "fillTypes": round(len(self.types) / types, 4) if types else 0.0,
            "distinct": f"{self.cap}+" if self.capped else len(self.values),
            "samples": sorted(self.values)[:3],
        }


def _nested_keys(value: str, parent: str) -> tuple[dict[str, str], list[str]]:
    """최상위 키의 값이 `{...}`일 때 그 **안쪽 한 겹**. (스칼라, 더 깊은 것)."""
    inner = value.strip()
    if not (inner.startswith("{") and inner.endswith("}")):
        return {}, []
    scalars, deeper = _top_level_pairs(inner[1:-1])
    return ({f"{parent}.{k}": v for k, v in scalars.items()},
            [f"{parent}.{k}" for k in deeper])


def inventory_details(rows_iter, *, cap: int) -> dict:
    """`spec_infos.details`의 프로바이더별 키 인벤토리."""
    from app.cloudkb.perfkb.parsers.details import parse_details

    per_provider: dict[str, dict[str, Counter]] = defaultdict(dict)
    rows_seen: dict[str, int] = defaultdict(int)
    types_seen: dict[str, set[str]] = defaultdict(set)
    blocked: dict[str, str] = {}

    for row in rows_iter:
        provider = (row.get("provider_name") or "").strip().lower()
        # **컬럼 이름을 틀리면 T4가 조용히 죽는다.** 첫 실행에서 `cs_p_spec_name`으로
        # 적었다가 전부 `name`(=`provider+region+spec`, 행마다 유일)으로 떨어져
        # "타입 수 = 행 수"가 나왔고, 그러면 타입 단위 채움률이 행 단위와 같아진다.
        name = (row.get("csp_spec_name") or "").strip()
        type_key = f"{provider}+{name}"
        rows_seen[provider] += 1
        types_seen[provider].add(type_key)
        try:
            top = parse_details(row.get("details"))
        except Exception:  # noqa: BLE001 — 인벤토리는 세다가 죽지 않는다
            blocked[provider] = "details가 파서를 통과하지 못했다"
            continue
        bucket = per_provider[provider]
        for key, value in top.items():
            bucket.setdefault(key, Counter(cap)).see(value, type_key)
            nested, deeper = _nested_keys(str(value), key)
            for nested_key, nested_value in nested.items():
                bucket.setdefault(nested_key, Counter(cap)).see(nested_value, type_key)
            for name_ in deeper:
                # **못 읽는 자리를 세어서 남긴다.** 배열 안의 배열이라 `go_field`가
                # 원리적으로 스칼라를 못 뽑는 자리다 — 조용히 빼면 다음 사람이 같은
                # 벽에 다시 부딪힌다.
                blocked[f"{provider}.{name_}"] = "중첩 2단 — 스칼라를 뽑을 수 없다"

    out: dict = {"providers": {}, "blocked": blocked}
    for provider, keys in sorted(per_provider.items()):
        rows = rows_seen[provider]
        types = len(types_seen[provider])
        out["providers"][provider] = {
            "rows": rows,
            "types": types,
            "keys": {k: c.as_dict(rows, types) for k, c in sorted(keys.items())},
        }
    return out


def inventory_columns(rows_iter, *, cap: int) -> dict:
    """테이블 하나의 컬럼별 채움률. `details` 같은 큰 칸도 채움만 센다."""
    counters: dict[str, Counter] = {}
    rows = 0
    for row in rows_iter:
        rows += 1
        key = str(rows)  # 컬럼 인벤토리에는 타입 개념이 없다
        for column, value in row.items():
            counters.setdefault(column, Counter(cap)).see(value, key)
    return {
        "rows": rows,
        "columns": {c: k.as_dict(rows, rows) for c, k in sorted(counters.items())},
    }


def main() -> int:
    use_utf8()
    parser = argparse.ArgumentParser(description="cb-tumblebug 미러 전수 인벤토리")
    parser.add_argument("--out", required=True, help="결과 JSON 경로")
    parser.add_argument("--distinct-cap", type=int, default=12)
    parser.add_argument("--tables", default="spec_infos,image_infos,latency_infos")
    args = parser.parse_args()

    dump = fetch_dump()
    result: dict = {"dump": dump.name, "tables": {}}
    for table in [t.strip() for t in args.tables.split(",") if t.strip()]:
        print(f"[{table}] 컬럼 인벤토리…")
        result["tables"][table] = inventory_columns(
            iter_table_rows(dump, table), cap=args.distinct_cap
        )
        print(f"  행 {result['tables'][table]['rows']:,} · "
              f"컬럼 {len(result['tables'][table]['columns'])}")
    if "spec_infos" in result["tables"]:
        print("[spec_infos.details] 키 인벤토리(최상위 + 중첩 1단)…")
        result["details"] = inventory_details(
            iter_table_rows(dump, "spec_infos"), cap=args.distinct_cap
        )
        for provider, block in result["details"]["providers"].items():
            print(f"  {provider:10} 행 {block['rows']:>7,} · 타입 {block['types']:>6,} "
                  f"· 키 {len(block['keys']):>3}")

    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n저장: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
