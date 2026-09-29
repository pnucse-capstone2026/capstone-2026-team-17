"""speckb 전용 다운로드·보존 계층.

## 왜 kbcommon/fetch.py를 쓰지 않는가

speckb는 저장소의 어떤 모듈도 import하지 않는다. 디렉터리만 떼어내도 그대로
동작해야 한다는 제약이 이 패키지의 존재 이유에 붙어 있다 — 저장소가 이미
가공해 둔 값(리전명, tumblebug 덤프 파생 사양)이 원본 수집 경로에 섞여 들어가는
것을 구조적으로 막기 위해서다. 그래서 `fetch_cached()`와 겹치는 원자적 교체·
provenance 기록 로직을 여기에 다시 둔다. 중복은 실수가 아니라 의도다.

표준 라이브러리만 쓴다. httpx도 requests도 끌어오지 않는다.

## 저장 형식

응답 본문은 gzip으로 압축해 저장하지만 **내용은 한 바이트도 바꾸지 않는다**.
`.provenance.json` 사이드카의 sha256은 압축 결과가 아니라 **압축 전 원본 응답
바이트**에 대해 계산한다. 그래야 나중에 같은 URL을 다시 받아 바이트 단위로
대조할 수 있다. gzip 헤더의 mtime은 0으로 고정해 같은 입력이 같은 파일을
만들도록 했다.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

USER_AGENT = "easydep-speckb/1.0 (raw cloud VM catalog collector)"
TIMEOUT_SECONDS = 120.0
MAX_ATTEMPTS = 5

# 4xx는 보통 재시도해도 그대로다(404를 세 번 더 받아봐야 소용없다). 429만
# 예외다 — Azure Retail API가 전 리전을 훑는 중에 실제로 이걸 돌려줬고,
# 처음엔 4xx라는 이유로 즉시 포기해 리전 하나를 통째로 놓쳤다.
RETRYABLE_CLIENT_ERRORS = frozenset({429})


def enable_utf8_stdout() -> None:
    """Windows 콘솔에서 한국어 로그가 깨지지 않게 한다."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: dict[str, str]

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    byte_range: tuple[int, int] | None = None,
) -> Response:
    """GET 한 번. 본문을 bytes 그대로 돌려준다.

    5xx·429·네트워크 오류는 지수 백오프로 재시도한다. 나머지 4xx는 즉시
    돌려준다 — 404는 호출부가 "이 리전에는 이 피드가 없다"고 판단해야 하는
    정상 신호라서 실패로 만들면 안 된다.

    429는 서버가 `Retry-After`를 주면 그 값을 따르고, 없으면 지수 백오프를 쓴다.
    """
    request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if headers:
        request_headers.update(headers)
    if byte_range is not None:
        request_headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(url, headers=request_headers)
        backoff = 2**attempt
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read()
                meta = {k.lower(): v for k, v in response.headers.items()}
                return Response(response.status, _decode(raw, meta), meta)
        except urllib.error.HTTPError as error:
            meta = {k.lower(): v for k, v in error.headers.items()} if error.headers else {}
            body = _decode(error.read(), meta)
            if error.code < 500 and error.code not in RETRYABLE_CLIENT_ERRORS:
                return Response(error.code, body, meta)
            if error.code in RETRYABLE_CLIENT_ERRORS:
                backoff = _retry_after(meta, backoff)
            last_error = error
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            last_error = error

        if attempt < MAX_ATTEMPTS:
            time.sleep(backoff)

    raise RuntimeError(f"{url} 요청이 {MAX_ATTEMPTS}회 모두 실패했다: {last_error}")


def _retry_after(headers: dict[str, str], fallback: float) -> float:
    """`Retry-After` 초 단위 값. 없거나 날짜 형식이면 fallback을 쓴다."""
    value = headers.get("retry-after")
    if not value:
        return fallback
    try:
        return max(float(value), fallback)
    except ValueError:
        return fallback


def _decode(raw: bytes, headers: dict[str, str]) -> bytes:
    """전송 압축만 푼다. 내용은 건드리지 않는다."""
    if headers.get("content-encoding", "").lower() == "gzip":
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError):
            return raw
    return raw


# provenance에 남기면 안 되는 값. Azure Resource SKUs는 요청 URL 경로에 구독 ID가
# 들어가는데, 이 파일들은 공개 저장소에 커밋된다. 자격증명은 아니지만 개인 식별자라
# 올릴 값이 아니다. GCP도 프로젝트 ID가 경로에 들어간다.
_REDACTIONS = (
    (re.compile(r"/subscriptions/[0-9a-fA-F-]{36}"), "/subscriptions/{subscriptionId}"),
    (re.compile(r"/projects/[^/]+"), "/projects/{project}"),
)


def redact(url: str) -> str:
    """URL에서 계정 식별자를 지운다. 나머지는 그대로 둔다."""
    for pattern, replacement in _REDACTIONS:
        url = pattern.sub(replacement, url)
    return url


def already_have(path: Path) -> bool:
    """provenance 사이드카까지 갖춘 파일만 '받았다'고 본다.

    중간에 끊긴 실행을 이어받을 때, 본문만 있고 사이드카가 없는 파일은
    다시 받아야 하므로 False를 돌려준다.
    """
    return path.exists() and _sidecar(path).exists()


def save_gz(path: Path, body: bytes, url: str, *, headers: dict[str, str] | None = None) -> None:
    """응답 본문을 gzip으로 저장하고 provenance 사이드카를 남긴다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with (
        open(temporary, "wb") as raw_file,
        gzip.GzipFile(
            filename=path.name, fileobj=raw_file, mode="wb", mtime=0
        ) as gzip_file,
    ):
        # `filename`을 반드시 넘긴다. 생략하면 파이썬 gzip이 `fileobj.name`을 헤더에
        # 적는데, 여기서는 그게 `<이름>.json.gz.part`라서 압축을 풀면 `.part` 파일이
        # 나온다(`.gz`로 끝날 때만 잘라내는 로직이라 `.part`는 안 걸린다).
        # 최종 이름을 넘기면 gzip이 `.gz`를 떼어 `<이름>.json`으로 적는다.
        gzip_file.write(body)
    os.replace(temporary, path)

    source_headers = headers or {}
    record = {
        "url": redact(url),
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "etag": source_headers.get("etag"),
        "last_modified": source_headers.get("last-modified"),
        "note": "sha256과 bytes는 압축 전 원본 응답 본문 기준이다.",
    }
    _write_json(_sidecar(path), record)


def read_gz(path: Path) -> bytes:
    with gzip.open(path, "rb") as handle:
        return handle.read()


def load_gz_json(path: Path):
    return json.loads(read_gz(path).decode("utf-8"))


def write_manifest(path: Path, manifest: dict) -> None:
    manifest = dict(manifest)
    manifest["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    _write_json(path, manifest)


def _sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".provenance.json")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def raw_dir() -> Path:
    return Path(__file__).resolve().parent / "raw"
