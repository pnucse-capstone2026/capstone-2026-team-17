"""성능 데이터셋 로드 및 조회.

**costkb와 달리 번들 폴백이 없다.** costkb는 손 큐레이션 36건이 있어 빌드 없이도 동작하지만,
성능 데이터는 손으로 옮겨 적을 만한 게 아니다(스펙당 10여 개 필드 × 3만 건). 산출물이 없으면
도구가 빌드를 안내한다 — 다른 cloudkb와 같은 방식이다.

산출물이 없어도 **추천은 그대로 동작한다**(fail-open) — 다만 그 사실을 숨기지는 않는다.
경고를 못 붙였으면 못 붙였다고 밝힌다. 자세한 건 `nim_agent/cost_tools.py`의 조인 부분 참고.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.cloudkb.kbcommon import artifact
from app.cloudkb.kbcommon.artifact import read_dataset

_SCHEMA_PATH = Path(__file__).with_name("schema.json")

DEFAULT_OUTPUT_DIR = Path("output")
BUILT_FILENAME = "tumblebug-perf.json"

#: 소스마다 파일을 따로 두고 **읽을 때 합친다**.
#: 소스별 핀·라이선스가 다르므로 한 파일에 섞으면 어느 부분이 언제 것인지 알 수 없다.
#:
#: `ibm-perf.json`은 **재배포 허가를 확인하지 못해 `data/`에 커밋하지 않는다** —
#: 없는 것이 기본이고, 없다고 나머지가 못 도는 일은 없어야 한다.
EXTRA_FILENAMES: tuple[str, ...] = ()


@lru_cache(maxsize=1)
def _schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _resolve(output_dir: Path | str | None) -> str:
    """None이면 그때그때 DEFAULT_OUTPUT_DIR을 읽는다(기본 인자로 박으면 테스트에서 못 바꾼다)."""
    return str(DEFAULT_OUTPUT_DIR if output_dir is None else output_dir)


@lru_cache(maxsize=4)
def _load_result(output_dir: str) -> tuple[dict | None, str | None]:
    """(데이터, 오류). 깨진 산출물에 **예외를 던지지 않는다**.

    perfkb는 번들 폴백이 없어서 예외가 그대로 올라갔고, 그러면 성능 도구가 아니라
    이 데이터를 조인하는 **비용 추천 전체가 죽었다**(결함 (마)). 성능 경고는 부가
    정보이므로, 없으면 없는 대로 추천은 살아야 한다.
    """
    found = artifact.resolve(output_dir, BUILT_FILENAME)
    data, error = read_dataset(
        found if found is not None else Path(output_dir) / BUILT_FILENAME, _schema()
    )
    if data is None:
        return data, error

    # 보조 산출물은 **있으면 더하고 없으면 조용히 넘어간다.** 다만 깨져 있으면
    # 조용히 넘어가지 않는다 — "커버리지가 왜 좁아졌지?"를 미궁으로 만든다.
    merged = dict(data)
    specs = list(data["specs"])
    notes = [data["_note"]]
    for name in EXTRA_FILENAMES:
        extra_path = artifact.resolve(output_dir, name)
        if extra_path is None:
            continue
        extra, extra_error = read_dataset(extra_path, _schema())
        if extra is None:
            error = error or extra_error
            continue
        specs.extend(extra["specs"])
        notes.append(extra["_note"])
    merged["specs"] = specs
    merged["_note"] = "\n".join(notes)
    return merged, error


def _load_cached(output_dir: str) -> dict | None:
    return _load_result(output_dir)[0]


def schema() -> dict:
    """번들된 JSON Schema — 빌드가 쓰기 전 검증에 쓴다."""
    return _schema()


def clear_caches() -> None:
    """로드·인덱스 캐시를 전부 비운다 (테스트가 output_dir을 갈아끼울 때).

    캐시가 셋이라 호출부에서 하나씩 비우면 반드시 빠뜨린다 — 실제로 인덱스를
    추가했을 때 기존 픽스처들이 조용히 낡은 데이터를 봤다. 추가는 여기만 고친다.
    """
    _load_result.cache_clear()
    _by_id.cache_clear()
    _by_provider_name.cache_clear()
    _schema.cache_clear()


def load_warning(output_dir: Path | str | None = None) -> str | None:
    """산출물이 깨져 읽지 못했다면 그 설명. 정상이거나 미빌드면 None."""
    return _load_result(_resolve(output_dir))[1]


def load_perf(output_dir: Path | str | None = None) -> list[dict] | None:
    """성능 레코드 목록. 산출물이 없으면 None."""
    data = _load_cached(_resolve(output_dir))
    return None if data is None else data["specs"]


def dataset_note(output_dir: Path | str | None = None) -> str | None:
    data = _load_cached(_resolve(output_dir))
    return None if data is None else data["_note"]


def is_built(output_dir: Path | str | None = None) -> bool:
    return _load_cached(_resolve(output_dir)) is not None


@lru_cache(maxsize=4)
def _by_id(output_dir: str) -> dict[str, dict]:
    data = _load_cached(output_dir)
    if data is None:
        return {}
    return {rec["id"]: rec for rec in data["specs"]}


def get_by_id(spec_id: str, output_dir: Path | str | None = None) -> dict | None:
    """costkb 레코드의 `id`로 성능 레코드를 찾는다. 조인의 진입점."""
    return _by_id(_resolve(output_dir)).get(spec_id)


# 리전마다 값이 다를 수 있는 수치. 접을 때 **보수적으로(작은 값) 고르고 범위를 함께
# 남긴다** — 성능 KB에서는 과대 진술이 과소 진술보다 해롭기 때문이다. "이 정도는 난다"고
# 했다가 안 나는 것이 "더 날 수도 있다"보다 나쁘다.
_REGION_VARYING = (
    "ebsBaselineIops", "ebsMaxIops", "ebsBaselineMbps", "ebsMaxMbps",
)


def _fold_regions(records: list[dict]) -> dict:
    """한 스펙의 리전별 레코드를 하나로 접는다. **정책이지 우연이 아니다.**

    예전엔 first-wins였는데, 그건 정책이 아니라 **파일에 먼저 나온 리전을 쓰는
    우연**이었다 — 같은 질문에 덤프 순서만 바뀌어도 다른 답이 나온다.

    지금 규칙은 둘이다:

    - 리전 불변인 필드(`sustainedCpu` 등)는 그대로 쓴다. 실측상 다리전 스펙
      3,221종 전부에서 값이 같다 — 다만 그게 **참이라고 가정하지 않고**
      `region-invariant-signals`라는 데이터 일관성 규칙이 매 빌드 확인한다.
    - 리전마다 갈리는 수치는 **가장 작은 값**을 쓰고 `<필드>Range`에 실제 범위를
      남긴다. 감춘 것이 아니라 보수적으로 답하고 폭을 함께 밝히는 것이다.
      (실측: `aws c8gn.48xlarge` 한 종뿐. IOPS가 리전에 따라 240,000 / 480,000)
    """
    folded = dict(records[0])
    if len(records) == 1:
        return folded
    for field in _REGION_VARYING:
        values = sorted(
            {r[field] for r in records
             if isinstance(r.get(field), (int, float)) and not isinstance(r.get(field), bool)}
        )
        if len(values) > 1:
            folded[field] = values[0]
            folded[f"{field}Range"] = [values[0], values[-1]]
    return folded


@lru_cache(maxsize=4)
def _by_provider_name(output_dir: str) -> dict[tuple[str, str], dict]:
    """(provider, specName소문자) → **리전을 접은** 레코드 하나.

    접는 규칙은 `_fold_regions` 참고. 이 인덱스에서 나온 수치는 리전 단위 답이
    아니므로, 특정 리전의 값이 필요하면 `find(region=...)`을 써야 한다.
    """
    data = _load_cached(output_dir)
    if data is None:
        return {}
    grouped: dict[tuple[str, str], list[dict]] = {}
    for rec in data["specs"]:
        grouped.setdefault((rec["provider"], rec["specName"].lower()), []).append(rec)
    return {key: _fold_regions(recs) for key, recs in grouped.items()}


def get_by_spec_name(
    provider: str, spec_name: str, output_dir: Path | str | None = None
) -> dict | None:
    """(provider, specName)으로 성능 레코드를 찾는다 — **id 조인의 폴백**.

    costkb 번들 36건에는 `id`가 없고, 미러 레코드도 그 리전이 perfkb에 없을 수 있다.
    id로만 조인하면 두 경우 모두 조용히 경고가 사라진다(결함 C3).
    """
    if not provider or not spec_name:
        return None
    return _by_provider_name(_resolve(output_dir)).get(
        (provider.lower(), spec_name.lower())
    )


def tracked_providers(output_dir: Path | str | None = None) -> frozenset[str]:
    """성능 신호를 **실제로 수록한** 프로바이더 집합.

    상수로 박지 않고 산출물에서 유도한다 — `parsers/build.py`의 KNOWN_PROVIDERS를
    복제해두면 커버리지가 넓어질 때 조용히 드리프트한다. "레코드가 없다"와
    "이 프로바이더는 애초에 추적 대상이 아니다"를 가르는 데 쓴다(결함 C4).

    별도 캐시를 두지 않는다 — `_by_provider_name`이 이미 캐시돼 있어 키만 훑으면 된다.
    """
    return frozenset(provider for provider, _ in _by_provider_name(_resolve(output_dir)))


def find(
    provider: str | None = None,
    spec_name: str | None = None,
    output_dir: Path | str | None = None,
) -> list[dict]:
    """프로바이더/스펙명으로 찾는다(리전마다 레코드가 따로 있어 여러 건일 수 있다)."""
    records = load_perf(output_dir) or []
    prov = provider.lower() if provider else None
    name = spec_name.lower() if spec_name else None
    return [
        r
        for r in records
        if (prov is None or r["provider"] == prov)
        and (name is None or r["specName"].lower() == name)
    ]


def coverage(output_dir: Path | str | None = None) -> list[dict]:
    """프로바이더별 레코드 수와 주요 신호 채움율 — '모른다'를 드러내는 용도."""
    records = load_perf(output_dir) or []
    by_provider: dict[str, dict] = {}
    for r in records:
        row = by_provider.setdefault(
            r["provider"],
            {"provider": r["provider"], "count": 0, "sustainedCpu": 0, "acu": 0,
             "currentGeneration": 0, "not_sustained": 0},
        )
        row["count"] += 1
        if "sustainedCpu" in r:
            row["sustainedCpu"] += 1
            if r["sustainedCpu"]["value"] is False:
                row["not_sustained"] += 1
        if "acu" in r:
            row["acu"] += 1
        if "currentGeneration" in r:
            row["currentGeneration"] += 1
    return sorted(by_provider.values(), key=lambda x: -x["count"])
