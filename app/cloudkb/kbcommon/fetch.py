"""공개 스키마 다운로드 + 로컬 캐시 (지식베이스 패키지 공유).

캐시 위치는 CLOUDKB_CACHE_DIR 환경변수, 없으면 GRAPHKB_CACHE_DIR(하위 호환),
그것도 없으면 프로젝트 로컬 `.cache/<namespace>`.

여러 KB 패키지가 같은 소스(CFN zip 2.8MB, bicep types 수십 MB 등)를 쓰므로
기본 네임스페이스를 공유해 중복 다운로드를 피한다.

부분 다운로드가 캐시를 오염시키지 않도록 `.part` 임시 파일에 쓴 뒤
os.replace로 원자적으로 교체한다.

## 프로버넌스 — 무엇을 받았는지 남긴다

받은 파일마다 `<파일명>.provenance.json`에 URL·sha256·크기·시각·`Last-Modified`/`ETag`를
남긴다. 목적은 **산출물을 원본에 묶는 것**이다. 감사 중에 캐시된 AWS zip(2,783,390 B)이
같은 URL의 라이브(2,794,161 B)와 이미 달라진 것을 발견했다 — 그 순간 산출물이 어느 입력에서
나왔는지 알 방법이 없어졌다. 고정할 수 없는 소스라도 **무엇을 썼는지는 기록할 수 있다.**

이 기록은 `kbcommon/sources.py`의 고정 ref와 짝을 이룬다: ref는 "무엇을 받기로 했나",
프로버넌스는 "실제로 무엇을 받았나"다.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx

DEFAULT_NAMESPACE = "cloudkb"
PROVENANCE_SUFFIX = ".provenance.json"


def cache_dir(namespace: str = DEFAULT_NAMESPACE) -> Path:
    """캐시 디렉터리 경로를 반환한다 (생성은 fetch_cached에서).

    Args:
        namespace: 캐시를 나눌 이름. 기본값은 모든 KB가 공유하는 "cloudkb".
    """
    from app.config import settings
    env = settings.cloudkb_cache_dir or settings.graphkb_cache_dir
    if env:
        return Path(env)
    # 저장소 기준이다. CWD 기준이면 easydep처럼 다른 데서 부르는 프로세스마다
    # 캐시가 흩어진다 — artifact.resolve()가 산출물에 대해 이미 내린 결론과 같다.
    return Path(__file__).resolve().parent.parent / ".cache" / namespace


def fetch_cached(
    url: str,
    filename: str,
    *,
    refresh: bool = False,
    namespace: str = DEFAULT_NAMESPACE,
) -> Path:
    """URL을 내려받아 캐시된 파일 경로를 반환한다.

    Args:
        url: 다운로드할 URL. 기존 로컬 파일 경로면 다운로드 없이 그대로 사용
            (테스트/오프라인 경로).
        filename: 캐시 디렉터리 안에 저장할 파일 이름.
        refresh: True면 캐시가 있어도 다시 내려받는다.
        namespace: 캐시 네임스페이스.
    """
    local = Path(url)
    if local.exists():
        return local

    dest = cache_dir(namespace) / filename
    if dest.exists() and not refresh and _cache_matches(dest, url):
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    size = 0
    with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as response:
        response.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in response.iter_bytes():
                fh.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        headers = response.headers
    os.replace(tmp, dest)
    _write_provenance(
        dest,
        {
            "url": url,
            "sha256": digest.hexdigest(),
            "bytes": size,
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
            # 고정 불가 소스(AWS zip)에서 유일하게 남는 버전 단서다.
            "last_modified": headers.get("Last-Modified"),
            "etag": headers.get("ETag"),
        },
    )
    return dest


def fetch_relative(
    base: str, rel_path: str, *, cache_prefix: str, refresh: bool = False
) -> Path:
    """base가 로컬 디렉터리면 그 안에서 찾고, 아니면 URL로 조립해 받는다.

    capacitykb의 Azure 파서가 만들었고 graphkb(AVM)가 **비공개인 채로 빌려
    쓰던** 헬퍼다 — KB끼리 관통하던 자리라 여기로 올렸다(아키텍처 예외표 해소).

    Args:
        cache_prefix: 캐시 파일명 접두("azure-" 등). 기존 캐시 이름을 그대로
            유지하기 위한 것이다 — 이름이 바뀌면 캐시 전체가 재다운로드된다.
    """
    local = Path(base) / rel_path
    if local.exists():
        return local
    url = f"{base.rstrip('/')}/{rel_path}"
    return fetch_cached(url, cache_prefix + rel_path.replace("/", "_"), refresh=refresh)


def list_github_tree(
    api_base: str, tag: str, subdir: str, *, cache_prefix: str, refresh: bool = False
) -> list[str]:
    """GitHub git trees API로 태그의 subdir 아래 blob 경로 목록 (캐시됨).

    graphkb의 KCC 파서가 만들었고 capacitykb(GCP)가 빌려 쓰던 헬퍼 — 위와 같은
    이유로 올렸다. 캐시 파일명은 원래 이름(`kcc-tree-root-…`)이 나오도록
    cache_prefix로 맞춘다.
    """
    import sys

    root_path = fetch_cached(
        f"{api_base}/git/trees/{tag}", f"{cache_prefix}-root-{tag}.json", refresh=refresh
    )
    root = json.loads(root_path.read_text(encoding="utf-8"))
    sub_sha = next(
        (e["sha"] for e in root.get("tree", []) if e.get("path") == subdir), None
    )
    if sub_sha is None:
        raise FileNotFoundError(f"{subdir}/ 디렉터리를 저장소 트리에서 찾지 못했습니다.")
    sub_path = fetch_cached(
        f"{api_base}/git/trees/{sub_sha}?recursive=1",
        f"{cache_prefix}-{subdir}-{tag}.json",
        refresh=refresh,
    )
    sub = json.loads(sub_path.read_text(encoding="utf-8"))
    if sub.get("truncated"):
        print(
            f"경고: {subdir}/ 트리 목록이 잘렸습니다 — 일부 파일이 누락될 수 있음",
            file=sys.stderr,
        )
    return [e["path"] for e in sub.get("tree", []) if e.get("type") == "blob"]


def load_yaml_lenient(path: Path) -> dict | None:
    """YAML을 읽되 파싱 실패는 경고 후 None.

    수천 파일 배치에서 한 파일의 오류가 전체 빌드를 세우지 않게 한다 —
    건너뛴 사실은 stderr에 남는다.
    """
    import sys

    import yaml

    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        print(f"경고: YAML 파싱 실패, 건너뜀 — {path.name}: {exc}", file=sys.stderr)
        return None


def _cache_matches(dest: Path, url: str) -> bool:
    """캐시된 파일이 **이 URL에서** 온 것인지.

    소스 고정 ref를 바꾸면 URL은 달라지는데 캐시 **파일명은 그대로**다. URL을 안 보고
    캐시를 재사용하면, 빌드는 새 커밋을 가리킨다고 기록하면서 실제로는 옛 데이터를
    읽는다 — 고정의 목적이 정확히 무너지는 경로다.

    프로버넌스가 없는 캐시(고정 도입 이전에 받은 것)도 "모른다"이므로 다시 받는다.
    한 번은 전부 재다운로드되지만, 그 대가로 캐시의 정체가 확실해진다.
    """
    record = provenance(dest)
    return record is not None and record.get("url") == url


def _write_provenance(dest: Path, record: dict) -> None:
    dest.with_name(dest.name + PROVENANCE_SUFFIX).write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def provenance(path: Path) -> dict | None:
    """캐시 파일의 프로버넌스. 없으면 None (예전 캐시이거나 로컬 파일 경로)."""
    side = path.with_name(path.name + PROVENANCE_SUFFIX)
    if not side.exists():
        return None
    try:
        return json.loads(side.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def describe_source(path: Path, source_key: str | None = None) -> dict:
    """산출물에 실을 프로버넌스 블록.

    프로버넌스가 없으면(로컬 파일을 직접 넘긴 경우 등) 그 자리에서 해시를 계산한다 —
    "무엇을 썼는지 모른다"는 답이 산출물에 남지 않게 한다.
    """
    from app.cloudkb.kbcommon.sources import SOURCES

    record = provenance(path) or {}
    if "sha256" not in record and path.exists():
        record = {
            "url": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
            "fetched_at": None,
            "local_file": True,
        }
    if source_key and (source := SOURCES.get(source_key)):
        record = {
            "source": source.key,
            "pin_kind": source.pin_kind,
            "pin": source.pin,
            **record,
        }
        if source.pin_kind == "bundled":
            # 절대경로는 머신마다 달라 산출물에 들어가면 안 된다 — 레포 기준 경로를 쓴다.
            record["url"] = source.url
    return record


def describe_source_set(paths: list[Path], source_key: str | None = None) -> dict:
    """여러 파일을 받는 소스(Azure 타입 정의 수백 개, GCP CRD 등)의 프로버넌스.

    파일마다 한 항목을 남기면 산출물이 프로버넌스로 뒤덮인다. 대신 **묶음 다이제스트**를
    쓴다 — 파일명과 각 sha256을 정렬해 이어붙인 뒤 다시 해시한다. 한 파일이라도
    달라지면 값이 바뀌므로, "같은 입력이었나"에는 답할 수 있다.

    커밋 SHA로 고정된 소스라면 사실 이 값이 없어도 재현되지만, `pin`은 *의도*이고
    이 다이제스트는 *실제로 읽은 것*이라 둘 다 남긴다.
    """
    parts = []
    total = 0
    for p in sorted(paths, key=lambda x: x.name):
        if not p.exists():
            continue
        data = p.read_bytes()
        total += len(data)
        parts.append(f"{p.name}:{hashlib.sha256(data).hexdigest()}")
    record = {
        "sha256": hashlib.sha256("\n".join(parts).encode()).hexdigest(),
        "bytes": total,
        "files": len(parts),
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    from app.cloudkb.kbcommon.sources import SOURCES as _S

    if source_key and (source := _S.get(source_key)):
        record = {
            "source": source.key,
            "pin_kind": source.pin_kind,
            "pin": source.pin,
            "url": source.url,
            **record,
        }
    return record
