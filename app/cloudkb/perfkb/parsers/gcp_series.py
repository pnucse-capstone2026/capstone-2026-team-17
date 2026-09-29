"""GCP 머신 시리즈 특성 — Cyclenerd 카탈로그 (조사 2026-07-24 채택 2).

## 왜 필요한가

perfkb의 gcp 11,622건은 **sustainedCpu 하나뿐**이었다 — 세 프로바이더 중 최악의
공백. 기계 판독 가능한 무인증 공식 카탈로그가 없어서다(Billing Catalog API는
인증 필요, 문서는 사이트 렌더링뿐). Cyclenerd가 Google 문서를 옮겨 둔 두 파일이
그 구멍을 메운다 — 이미 핀 박은 pricing.yml(GPU 수·로컬 SSD GB)과 새로 핀 박은
시리즈 SQL(cpuPlatform·계열·크기별 네트워크 대역폭).

## 커뮤니티 큐레이션이다 — 그 사실이 라벨에 실린다

원문이 Google 문서 URL을 달아 두었지만 옮긴 것은 커뮤니티다. mingrammer와 같은
등급으로 basis=inferred(`cyclenerd-gcp-catalog`)이고, azure-sizes-doc(문서 표
직접 파싱 — stated)과 등급이 다르다는 것이 두 라벨을 가른 이유다.

## SQL 파싱이지만 문법이 핀돼 있다

시리즈 파일은 `UPDATE instances SET k='v', … WHERE name LIKE '패턴';` 문장의
나열이다(실측 — 생성기가 아니라 사람이 쓰지만 형식이 일정하다). 문장 단위
정규식으로 읽고, LIKE 패턴은 fnmatch로 옮긴다(`%` → `*`). tier1(Tier_1 네트워킹
활성 시 대역폭)은 **담지 않는다** — 기본 구성의 값이 아니라서, 담으면 기본
대역폭처럼 읽힌다.
"""

from __future__ import annotations

import re
from collections import Counter
from fnmatch import fnmatch
from pathlib import Path

import yaml

from app.cloudkb.kbcommon.fetch import fetch_cached, list_github_tree
from app.cloudkb.kbcommon.sources import SOURCES

EVIDENCE = "cyclenerd-gcp-catalog"

_API = "https://api.github.com/repos/Cyclenerd/google-cloud-compute-machine-types"

_STATEMENT = re.compile(
    r"UPDATE\s+instances\s+SET\s+(?P<sets>.*?)\s+WHERE\s+name\s+LIKE\s+'(?P<pattern>[^']+)'\s*;",
    re.DOTALL | re.IGNORECASE,
)
_PAIR = re.compile(r"(\w+)\s*=\s*'([^']*)'")

#: pricing.yml 인스턴스 항목에서 GPU 모델로 취급하는 키. `cost·cpu·ram·local-ssd·
#: type`은 GPU가 아니다 — 키 목록은 실측(2026-07-24)이고, 모르는 키가 나타나면
#: 조용히 GPU가 되지 않도록 여기 없는 것은 세기만 한다.
_GPU_KEYS = {
    "a100": "A100", "a100-80gb": "A100 80GB", "h100-80gb": "H100 80GB",
    "h100-80gb-mega": "H100 80GB Mega", "l4": "L4", "rtx6000": "RTX 6000",
}
_NON_GPU_KEYS = {"cost", "cpu", "ram", "local-ssd", "type"}


class Report:
    def __init__(self) -> None:
        self.series_files = 0
        self.series_matched = 0
        self.instance_matched = 0
        self.unknown_keys: Counter = Counter()


def parse_series_sql(text: str) -> list[tuple[str, dict]]:
    """한 시리즈 SQL → `[(fnmatch 패턴, 필드)]`. 문장 순서를 지킨다 —
    뒤 문장이 앞 문장을 좁혀 덮는 구조라 순서가 곧 의미다."""
    rules: list[tuple[str, dict]] = []
    for match in _STATEMENT.finditer(text):
        raw = dict(_PAIR.findall(match.group("sets")))
        fields: dict = {}
        if raw.get("cpuPlatform"):
            fields["cpuFamily"] = raw["cpuPlatform"]
        if raw.get("family"):
            fields["family"] = raw["family"]
        if raw.get("bandwidth"):
            try:
                fields["networkBandwidthMbps"] = float(raw["bandwidth"]) * 1000
            except ValueError:
                pass
        if fields:
            rules.append((match.group("pattern").replace("%", "*"), fields))
    return rules


def fetch(refresh: bool = False) -> tuple[list[tuple[str, dict]], dict, list[Path]]:
    """`(시리즈 규칙, 인스턴스별 GPU·SSD, 캐시 경로들)`."""
    source = SOURCES["gcloud-machine-types"]
    paths = list_github_tree(
        _API, source.pin, "instances", cache_prefix="gcloud-mt-tree", refresh=refresh
    )
    sql_files = sorted(p for p in paths if p.startswith("series/") and p.endswith(".sql"))
    if len(sql_files) < 15:
        raise RuntimeError(
            f"시리즈 SQL이 {len(sql_files)}개뿐이다(실측 기준 25±) — 저장소가 "
            "재편됐는지 확인할 것."
        )
    rules: list[tuple[str, dict]] = []
    fetched: list[Path] = []
    for rel in sql_files:
        cache_name = f"gcloud-mt-{rel.replace('/', '-')}"
        path = fetch_cached(f"{source.url}/instances/{rel}", cache_name, refresh=refresh)
        fetched.append(path)
        rules.extend(parse_series_sql(path.read_text(encoding="utf-8")))

    # 인스턴스별 GPU·로컬 SSD — 이미 핀 박은 pricing.yml의 안 쓰던 부분.
    pricing = SOURCES["cyclenerd-gcp-pricing"]
    pricing_path = fetch_cached(
        pricing.url, f"cyclenerd-gcp-pricing-{pricing.pin}.yml", refresh=refresh
    )
    fetched.append(pricing_path)
    doc = yaml.safe_load(pricing_path.read_text(encoding="utf-8"))
    instances: dict[str, dict] = {}
    unknown: Counter = Counter()
    for name, spec in ((doc.get("compute") or {}).get("instance") or {}).items():
        fields: dict = {}
        for key, value in (spec or {}).items():
            if key in _NON_GPU_KEYS:
                if key == "local-ssd" and isinstance(value, (int, float)) and value > 0:
                    fields["localSsdGB"] = float(value)
                continue
            model = _GPU_KEYS.get(key)
            if model is None:
                unknown[key] += 1  # 모르는 키를 GPU로 승격하지 않는다
                continue
            if isinstance(value, (int, float)) and value > 0:
                fields["gpuModel"] = model
                fields["gpuCount"] = float(value)
        if fields:
            instances[name.lower()] = fields
    instances["_unknown_keys"] = unknown  # 보고용 — enrich가 꺼내 쓰고 지운다
    return rules, instances, fetched


def enrich(specs: list[dict], rules: list[tuple[str, dict]],
           instances: dict[str, dict]) -> Report:
    """gcp 레코드에 시리즈·인스턴스 특성을 덧붙인다(제자리 수정, 빈 칸만)."""
    report = Report()
    report.unknown_keys = instances.pop("_unknown_keys", Counter())
    matched_series: set[str] = set()
    matched_instance: set[str] = set()
    for spec in specs:
        if spec.get("provider") != "gcp" or not spec.get("specName"):
            continue
        name = spec["specName"].lower()
        fields: dict = {}
        # 시리즈 규칙 — 순서대로 적용하면 뒤의 좁은 패턴이 넓은 패턴을 덮는다
        # (원본 SQL의 실행 의미 그대로).
        for pattern, rule_fields in rules:
            if fnmatch(name, pattern):
                fields.update(rule_fields)
        if fields:
            matched_series.add(name)
        inst = instances.get(name)
        if inst:
            fields.update(inst)
            matched_instance.add(name)
        if not fields:
            continue
        for key, value in fields.items():
            if spec.get(key) is None:
                spec[key] = value
        spec["gcpSeriesEvidence"] = EVIDENCE
    report.series_matched = len(matched_series)
    report.instance_matched = len(matched_instance)
    report.series_files = len(rules)
    return report


def format_report(report: Report) -> str:
    lines = [
        f"gcp 시리즈 특성: 규칙 {report.series_files}개 → 크기 "
        f"{report.series_matched:,}종 매칭 · GPU/SSD {report.instance_matched:,}종"
    ]
    if report.unknown_keys:
        lines.append(
            f"  모르는 키는 GPU로 승격하지 않고 셈: {dict(report.unknown_keys)}"
        )
    return "\n".join(lines)
