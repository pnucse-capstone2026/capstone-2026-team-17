"""Azure VM **크기 문서의 표** — NIC 수·네트워크 대역폭 (조사 2026-07-24 채택 1).

## 왜 필요한가

perfkb의 azure 34,846건은 미러에서 온 4필드(sustainedCpu·diskIops·acu·
acceleratedNetworking)뿐이었다 — aws는 거의 전 필드가 차 있는데 azure는 네트워크
대역폭도 NIC 수도 없었다. MicrosoftDocs/azure-compute-docs의 크기 문서에는 크기마다

    | Size Name | Max NICs (Qty.) | Max Network Bandwidth (Mbps) |

표가 있다(실측). **산문이 아니라 표를 파싱한다** — azure-limits-doc과 같은 방법,
같은 라이선스(CC-BY-4.0), basis=stated.

## 무엇을 담고 무엇을 안 담나

`maxNics`·`networkBandwidthMbps` **둘만** 담는다 — 스키마에 이미 있고(IBM이 쓰던
칸) azure가 비어 있던 칸이다. 표에 함께 있는 vCPU·메모리·디스크 IOPS는 미러가
이미 갖고 있어(다른 소스·다른 스냅샷) 담지 않는다 — 두 소스의 값이 섞이면
어느 스냅샷의 값인지 알 수 없게 된다.

각주 표기(`<sup>1</sup>`)와 'Not Supported' 칸은 실측에서 확인한 형식이다 —
숫자가 아닌 칸은 담지 않고 센다.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from app.cloudkb.kbcommon.fetch import fetch_cached, list_github_tree
from app.cloudkb.kbcommon.sources import SOURCES

EVIDENCE = "azure-sizes-doc"

_API = "https://api.github.com/repos/MicrosoftDocs/azure-compute-docs"

#: 크기 문서가 사는 곳. 문서 재편(경로 이동)은 매칭 급감으로 잡는다.
_SIZES_PREFIX = "virtual-machines/sizes/"

_SUP = re.compile(r"<sup>.*?</sup>")
_SIZE_NAME = re.compile(r"^Standard_[A-Za-z0-9_-]+$")


#: 구세대·은퇴 목록 문서. 행이 **산문 시리즈 라벨**("Dv1 and Dsv1-series")이라
#: 구체 크기로의 매핑은 아래 손 표가 한다 — 오지정하면 구세대 **오경보**가 되는
#: 자리라, 문서 라벨 집합과 손 표를 상호 대조해 낡음을 빌드가 잡는다(svcmap
#: 바인딩 검증 선례).
_LIFECYCLE_FILES = (
    "virtual-machines/sizes/lifecycle/previous-gen-sizes-list.md",
    "virtual-machines/sizes/lifecycle/retired-sizes-list.md",
)

#: 라벨 → 크기 이름 정규식(소문자). **문서의 라벨 전수와 1:1이어야 빌드가 돈다.**
#: 패턴은 전부 앵커드 — 현행 세대(_v5·bsv2류)는 접미로 구조적으로 배제된다.
#: 확인처: 각 라벨이 곧 확인처다(위 두 문서의 해당 행).
_GENERATION_TABLE: dict[str, tuple[str, ...]] = {
    # --- previous-gen-sizes-list.md ---
    "B-series (V1)": (r"^standard_b\d+l?m?s?$",),          # B1ls·B1s·B2ms… (bsv2류는 _v2 접미라 제외)
    "Standard D-series": (r"^standard_d\d+$",),
    "Preview DC-series": (r"^standard_dc\d+s$",),           # DC2s·DC4s (dcsv2는 _v2)
    "DS-series": (r"^standard_ds\d+$",),
    "Dv1 and Dsv1-series": (r"^standard_ds?\d+$",),         # v1은 무접미 — D/DS 표와 같은 크기들
    "Dv2 and Dsv2-series": (r"^standard_ds?\d+_v2(_promo)?$",),
    "Dv3 and Dsv3-series": (r"^standard_d\d+s?_v3$",),
    "Av2 and Amv2-series": (r"^standard_a\d+m?_v2$",),
    "F-series": (r"^standard_f\d+$",),
    "Fs-series": (r"^standard_f\d+s$",),
    "Fsv2-series": (r"^standard_f\d+s_v2$",),
    "Ev3 and Esv3-series": (r"^standard_e\d+i?s?_v3$",),
    "Ev4 and Esv4-series": (r"^standard_e\d+s?_v4$",),
    "Eav4 and Easv4-series": (r"^standard_e\d+as?_v4$",),
    "Edv4 and Edsv4-series": (r"^standard_e\d+ds?_v4$",),
    "GS-series": (r"^standard_gs\d+$",),
    "G-series": (r"^standard_g\d+$",),
    "Memory-optimized D-series": (r"^standard_d1[1-4]$",),   # D 패턴의 부분집합 — 겹침 무해
    "Memory-optimized DS-series": (r"^standard_ds1[1-4]$",),
    "Lsv1-series": (r"^standard_l\d+s$",),
    "Lsv2-series": (r"^standard_l\d+s_v2$",),
    # --- retired-sizes-list.md (구세대 목록과 겹치는 라벨은 같은 크기를 가리킨다) ---
    "D-series": (r"^standard_d\d+$",),
    "Ds-series": (r"^standard_ds\d+$",),
    "Dv2-series": (r"^standard_d\d+_v2(_promo)?$",),
    "Dsv2-series": (r"^standard_ds\d+_v2(_promo)?$",),
    "Av2/Amv2-series": (r"^standard_a\d+m?_v2$",),
    "Gs-series": (r"^standard_gs\d+$",),
    "Standard_M192idms_v2": (r"^standard_m192idms_v2$",),
    "Standard_M192ids_v2": (r"^standard_m192ids_v2$",),
    "Standard_M192ims_v2": (r"^standard_m192ims_v2$",),
    "Standard_M192is_v2": (r"^standard_m192is_v2$",),
    "Ls-series": (r"^standard_l\d+s$",),
    "NCv3-NC24rs Series": (r"^standard_nc24rs_v3$",),
    "NCv3-Series": (r"^standard_nc\d+r?s_v3$",),
    "NVv3-series": (r"^standard_nv\d+s_v3$",),
    "NVv4-series": (r"^standard_nv\d+as_v4$",),
    "NP-series": (r"^standard_np\d+s?$",),
}

_SERIES_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|")


def _doc_labels(markdown: str) -> set[str]:
    """목록 문서의 첫 칸(시리즈 라벨) 전수 — 헤더·구분선 제외."""
    labels: set[str] = set()
    for line in markdown.splitlines():
        match = _SERIES_ROW.match(line)
        if not match:
            continue
        label = _SUP.sub("", match.group(1)).strip()
        if not label or label.lower() == "series name" or set(label) <= {"-", ":"}:
            continue
        labels.add(label)
    return labels


def fetch_generation(refresh: bool = False) -> tuple[list[re.Pattern], list[Path]]:
    """구세대·은퇴 크기 패턴(검증됨). 손 표가 문서와 어긋나면 빌드가 죽는다."""
    source = SOURCES["azure-compute-docs"]
    found: set[str] = set()
    fetched: list[Path] = []
    for rel in _LIFECYCLE_FILES:
        cache_name = f"azure-compute-docs-{rel.replace('/', '-')}"
        path = fetch_cached(f"{source.url}/articles/{rel}", cache_name, refresh=refresh)
        fetched.append(path)
        found.update(_doc_labels(path.read_text(encoding="utf-8")))
    missing = found - set(_GENERATION_TABLE)
    stale = set(_GENERATION_TABLE) - found
    if missing or stale:
        raise RuntimeError(
            "구세대 손 표가 문서와 어긋난다 — "
            f"문서에만 있음: {sorted(missing)} / 표에만 있음: {sorted(stale)}. "
            "오지정은 구세대 오경보가 되므로 표를 사람이 고친 뒤 빌드할 것."
        )
    return [re.compile(p) for pats in _GENERATION_TABLE.values() for p in pats], fetched


class Report:
    def __init__(self) -> None:
        self.files = 0
        self.matched = 0
        self.unmatched: Counter = Counter()
        self.non_numeric = 0
        self.generation_flagged = 0


def _cells(line: str) -> list[str]:
    return [_SUP.sub("", c).strip() for c in line.strip().strip("|").split("|")]


def _number(text: str) -> float | None:
    cleaned = text.replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None  # 'Not Supported' · '-' · 빈 칸


def parse_tables(markdown: str) -> dict[str, dict]:
    """한 문서의 표들에서 `크기 이름(소문자) → {maxNics, networkBandwidthMbps}`."""
    out: dict[str, dict] = {}
    lines = markdown.splitlines()
    header: list[str] | None = None
    nic_idx = bw_idx = name_idx = None
    for line in lines:
        if not line.lstrip().startswith("|"):
            header = None
            continue
        cells = _cells(line)
        if header is None:
            header = cells
            name_idx = nic_idx = bw_idx = None
            for i, cell in enumerate(cells):
                low = cell.lower()
                if "size name" in low:
                    name_idx = i
                elif "max nics" in low:
                    nic_idx = i
                elif "network bandwidth" in low:
                    bw_idx = i
            continue
        if set("".join(cells)) <= {"-", ":", " ", ""}:
            continue  # 구분선
        if name_idx is None or (nic_idx is None and bw_idx is None):
            continue
        if len(cells) <= max(i for i in (name_idx, nic_idx, bw_idx) if i is not None):
            continue
        name = cells[name_idx]
        if not _SIZE_NAME.match(name):
            continue
        fields = out.setdefault(name.lower(), {})
        if nic_idx is not None and (nics := _number(cells[nic_idx])) is not None:
            fields["maxNics"] = int(nics)
        if bw_idx is not None and (bw := _number(cells[bw_idx])) is not None:
            fields["networkBandwidthMbps"] = bw
    return {k: v for k, v in out.items() if v}


def fetch(refresh: bool = False) -> tuple[dict[str, dict], list[Path]]:
    """크기 문서 전부를 받아 표를 파싱한다. `(크기별 필드, 캐시 경로들)`."""
    source = SOURCES["azure-compute-docs"]
    paths = list_github_tree(
        _API, source.pin, "articles", cache_prefix="azcompute-tree", refresh=refresh
    )
    wanted = sorted(
        p for p in paths
        if p.startswith(_SIZES_PREFIX) and p.endswith("-series.md")
    )
    table: dict[str, dict] = {}
    fetched: list[Path] = []
    for rel in wanted:
        cache_name = f"azure-compute-docs-{rel.replace('/', '-')}"
        path = fetch_cached(f"{source.url}/articles/{rel}", cache_name, refresh=refresh)
        fetched.append(path)
        for name, fields in parse_tables(path.read_text(encoding="utf-8")).items():
            # 같은 크기가 여러 문서에 나오면 값이 같아야 정상 — 다르면 나중 것을
            # 덮지 않고 첫 값을 지킨다(어느 쪽이 맞는지 우리가 모른다).
            table.setdefault(name, fields)
    if len(wanted) < 100:
        raise RuntimeError(
            f"크기 문서가 {len(wanted)}개뿐이다(실측 기준 200+) — 문서가 재편됐는지 "
            "확인할 것(경로 접두가 낡았을 수 있다)."
        )
    return table, fetched


def enrich(specs: list[dict], table: dict[str, dict],
           generation: list[re.Pattern] = ()) -> Report:
    """perfkb azure 레코드에 크기 표 사실을 덧붙인다(제자리 수정).

    **빈 칸만 채운다** — 이미 값이 있으면(다른 소스) 덮지 않는다.
    구세대 판정은 **False만 표시한다**: 목록에 있으면 문서가 그렇게 말한 것
    (stated)이고, 없다고 최신이라 주장하지 않는다 — 부재를 True로 읽는 것이
    침묵 오독이다(perfkb의 aws-non-burstable 계보).
    """
    report = Report()
    by_name: dict[str, list[dict]] = {}
    for spec in specs:
        if spec.get("provider") == "azure" and spec.get("specName"):
            by_name.setdefault(spec["specName"].lower(), []).append(spec)

    if generation:
        for name, targets in by_name.items():
            if not any(p.match(name) for p in generation):
                continue
            for spec in targets:
                if spec.get("currentGeneration") is None:
                    spec["currentGeneration"] = False
                    spec["azureSizesEvidence"] = EVIDENCE
            report.generation_flagged += 1

    for name, fields in table.items():
        targets = by_name.get(name)
        if not targets:
            # 미러의 azure 표기(소문자 standard_d2s_v5)와 문서 표기(Standard_D2s_v5)
            # 는 소문자 정규화로 만난다 — 그래도 안 만나는 것은 미러에 없는 크기다.
            report.unmatched[name.split("_")[1][:1] if "_" in name else name] += 1
            continue
        applied = {k: v for k, v in fields.items()}
        if not applied:
            continue
        for spec in targets:
            for key, value in applied.items():
                if spec.get(key) is None:
                    spec[key] = value
            spec["azureSizesEvidence"] = EVIDENCE
        report.matched += 1
    report.files = len(table)
    return report


def reconcile_max_nics(specs: list[dict]) -> dict[str, int]:
    """**두 소스가 같을 때만 `maxNics`를 담는다**(결정 D3 · `aws-limits` 선례).

    미러 `details.MaxNetworkInterfaces`는 azure 34,846행을 **100%** 채운다. 우리는
    그걸 모르고 문서표(72.8%)만 쓰고 있었다 — 계획서 §1-(3)이 뿌리라고 짚은 결함이다.
    이제 둘을 갖게 됐으니 고를 수 있는데, **다르게 말하는 값을 하나 골라 담으면 그건
    우리 짐작**이다. 그래서 고르지 않는다.

      일치      담는다
      불일치    **둘 다 버린다** 그리고 센다 (해소하지 않는다 — 위협 T3: 어느 쪽이
                최신인지 알 방법이 없다)
      미러만    담지 않는다. 단일 소스이므로 `_coverage`에 사실로 남는다

    커버리지가 **줄어드는 방향의 결정**이라는 것을 계획서가 미리 적어 뒀다. 그 대가로
    규율이 하나가 된다.

    Returns:
        건수 집계. 그대로 `_coverage`에 실린다.
    """
    from app.cloudkb.perfkb.parsers.project import MIRROR_MAX_NICS

    tally = {"agree": 0, "mismatch": 0, "mirrorOnly": 0, "docOnly": 0}
    for spec in specs:
        if spec.get("provider") != "azure":
            continue
        mirror = spec.pop(MIRROR_MAX_NICS, None)
        doc = spec.get("maxNics")
        if mirror is None and doc is None:
            continue
        if mirror is None:
            tally["docOnly"] += 1
            continue
        if doc is None:
            tally["mirrorOnly"] += 1
            continue
        if float(mirror) == float(doc):
            tally["agree"] += 1
        else:
            tally["mismatch"] += 1
            # **칸을 지운다.** `None`을 넣으면 스키마가 정수를 요구해 산출물이 죽는데,
            # 그보다 뜻이 중요하다 — 이 스펙에 대해 우리는 값을 **모른다**. 없는 칸이
            # 곧 모름이라는 것이 이 데이터셋의 규약이다.
            spec.pop("maxNics", None)
    return tally


def format_reconcile(tally: dict[str, int]) -> str:
    return (
        f"azure maxNics 두 소스 대조: 일치 {tally['agree']:,} · "
        f"불일치 {tally['mismatch']:,}(**둘 다 버림**) · "
        f"미러에만 {tally['mirrorOnly']:,}(단일 소스라 담지 않음) · "
        f"문서에만 {tally['docOnly']:,}"
    )


def format_report(report: Report) -> str:
    lines = [
        f"azure 크기 표: 문서의 크기 {report.files:,}종 중 미러와 매칭 "
        f"{report.matched:,}종에 NIC·대역폭을 덧붙임"
    ]
    if report.generation_flagged:
        lines.append(
            f"  구세대·은퇴 목록 매칭 {report.generation_flagged:,}종 → "
            "currentGeneration=False (미기재는 최신 주장 없이 모름 유지)"
        )
    if report.unmatched:
        total = sum(report.unmatched.values())
        lines.append(
            f"  미러에 없는 크기 {total}종은 담지 않음 (계열 첫 글자 분포: "
            f"{dict(report.unmatched.most_common(6))})"
        )
    return "\n".join(lines)
