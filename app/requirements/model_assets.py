"""저장소에 쪼개 넣은 BERT 가중치를 로드 가능한 모델 디렉터리로 되살린다.

GitHub은 파일당 100MiB를 넘기지 못하므로 417MiB짜리 `model.safetensors`를 그대로
커밋할 수 없다. `scripts/shard_bert_model.py`가 이를 두 단계로 쪼개 커밋해 두고,
여기서 되돌린다.

  materials/BERT_FR_NFR_Classifier/bert_model/
    config.json, tokenizer.json, tokenizer_config.json   ← 그대로 커밋
    weights/                                             ← 쪼갠 가중치 (커밋)
      manifest.json
      model.safetensors.index.json
      model-00002-of-00009.safetensors ...               ← 한도 이하, 그대로 사용
      model-00001-of-00009.safetensors.part000 ...       ← 한도 초과 샤드만 분할

되살린 결과는 커밋된 디렉터리를 건드리지 않고 `.easydep/models/bert_fr_nfr/`에 만든다.
저장소에 들어간 것은 읽기 전용 입력, 되살린 것은 언제든 다시 만들 수 있는 산출물이라
서로 섞지 않는 편이 낫다(읽기 전용 파일시스템 배포에서도 대상만 바꾸면 된다).

되살릴 때 드는 비용을 줄이려고 두 가지를 한다.

  - **한도 이하 샤드는 복사하지 않고 하드링크**한다. 417MiB 중 실제로 쓰는 건
    분할됐던 샤드(89MiB)뿐이라, 전체를 합쳤다 푸는 방식보다 쓰기량이 1/5 이하다.
  - **manifest 지문을 stamp로 남겨** 두 번째 기동부터는 존재·크기 확인만 하고 건너뛴다.

Docker 이미지는 빌드 단계에서 이 모듈을 직접 실행해 미리 되살려 두므로(Dockerfile 참고)
런타임에는 stamp 확인만 하고 넘어간다. 즉 파드 기동에 재조립 비용이 없다.

torch/transformers에 의존하지 않는 표준 라이브러리 전용 모듈이라 이미지 빌드 단계에서
`python -m app.requirements.model_assets` 로 단독 실행할 수 있다.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

# app/requirements/model_assets.py → app/requirements → app → 저장소 루트
REPO_ROOT = Path(__file__).resolve().parents[2]

# 커밋된 조각이 들어 있는 하위 디렉터리와 그 명세 파일.
WEIGHTS_SUBDIR = "weights"
MANIFEST_NAME = "manifest.json"
INDEX_NAME = "model.safetensors.index.json"

# 되살린 디렉터리에 남기는 지문. 내용이 현재 manifest 지문과 같으면 재조립을 건너뛴다.
STAMP_NAME = ".shard-stamp"

# 되살릴 위치를 옮기고 싶을 때 쓰는 환경변수(읽기 전용 루트 파일시스템 등).
CACHE_ENV = "BERT_MODEL_CACHE_DIR"

# 다른 워커가 재조립 중일 때 기다리는 최대 시간과, 죽은 워커의 잠금을 무시하는 기준.
_LOCK_WAIT_SECONDS = 180.0
_LOCK_STALE_SECONDS = 600.0


class ModelAssetsError(RuntimeError):
    """가중치를 되살릴 수 없을 때. 호출부가 BERT 검증만 끄고 계속 갈 수 있게 한다."""


# --------------------------------------------------------------------------
# 보조
# --------------------------------------------------------------------------
def _sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        while block := fp.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def _default_dest(model_dir: Path) -> Path:
    override = os.environ.get(CACHE_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return REPO_ROOT / ".easydep" / "models" / "bert_fr_nfr"


def _link_or_copy(source: Path, dest: Path) -> None:
    """같은 볼륨이면 하드링크로 즉시 끝내고, 안 되면 복사한다."""
    if dest.exists():
        dest.unlink()
    try:
        os.link(source, dest)
    except OSError:
        shutil.copy2(source, dest)


def _is_materialized(dest: Path, stamp: str | None, manifest: dict | None) -> bool:
    """dest가 이미 쓸 수 있는 상태인지 확인한다.

    stamp가 None이면(= 원본 manifest가 없는 이미지 런타임) 인덱스와 stamp 존재만 본다.
    """
    stamp_path = dest / STAMP_NAME
    if not stamp_path.is_file() or not (dest / INDEX_NAME).is_file():
        return False
    if stamp is None:
        return True
    try:
        if stamp_path.read_text(encoding="utf-8").strip() != stamp:
            return False
    except OSError:
        return False

    # stamp가 맞아도 파일이 지워졌을 수 있으므로 이름·크기까지는 확인한다.
    # (sha256 재검사는 최초 재조립 때만 하고 기동 경로에서는 생략한다.)
    for entry in (manifest or {}).get("files", []):
        target = dest / entry["name"]
        if not target.is_file() or target.stat().st_size != entry["size"]:
            return False
    for name in (manifest or {}).get("companions", []):
        if not (dest / name).is_file():
            return False
    return True


class _BuildLock:
    """디렉터리 생성의 원자성을 이용한 프로세스 간 잠금.

    uvicorn을 멀티 워커로 띄우면 워커들이 동시에 기동하면서 같은 대상을 재조립하려 든다.
    먼저 잡은 쪽만 만들고 나머지는 끝날 때까지 기다린다.
    """

    def __init__(self, dest: Path) -> None:
        self.path = dest.with_name(dest.name + ".lock")
        self.acquired = False

    def __enter__(self) -> _BuildLock:
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        while True:
            try:
                self.path.mkdir(parents=True)
                self.acquired = True
                return self
            except FileExistsError:
                # 재조립 도중 죽은 프로세스가 남긴 잠금은 일정 시간 뒤 무시한다.
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    continue
                if age > _LOCK_STALE_SECONDS:
                    shutil.rmtree(self.path, ignore_errors=True)
                    continue
                if time.monotonic() > deadline:
                    # 잠금은 못 잡았지만 상대가 끝냈을 수 있으니 호출부가 다시 확인한다.
                    return self
                time.sleep(0.5)

    def __exit__(self, *exc_info: object) -> None:
        if self.acquired:
            shutil.rmtree(self.path, ignore_errors=True)


# --------------------------------------------------------------------------
# 재조립
# --------------------------------------------------------------------------
def _merge_parts(weights_dir: Path, entry: dict, dest: Path) -> None:
    """분할된 샤드를 조각 순서대로 이어 붙인다. 조각마다 sha256을 대조한다."""
    target = dest / entry["name"]
    tmp = target.with_name(f"{target.name}.tmp{os.getpid()}")
    try:
        with tmp.open("wb") as out:
            for part in entry["parts"]:
                source = weights_dir / part["name"]
                if not source.is_file():
                    raise ModelAssetsError(f"조각이 없다: {source}")
                if source.stat().st_size != part["size"]:
                    raise ModelAssetsError(f"조각 크기가 다르다: {source}")
                digest = hashlib.sha256()
                with source.open("rb") as fp:
                    while block := fp.read(1 << 20):
                        digest.update(block)
                        out.write(block)
                if digest.hexdigest() != part["sha256"]:
                    raise ModelAssetsError(f"조각이 손상됐다: {source}")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _materialize(model_dir: Path, weights_dir: Path, manifest: dict, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / STAMP_NAME).unlink(missing_ok=True)

    for entry in manifest["files"]:
        if entry.get("parts"):
            _merge_parts(weights_dir, entry, dest)
        else:
            source = weights_dir / entry["name"]
            if not source.is_file():
                raise ModelAssetsError(f"샤드가 없다: {source}")
            _link_or_copy(source, dest / entry["name"])

        target = dest / entry["name"]
        if target.stat().st_size != entry["size"]:
            raise ModelAssetsError(f"재조립 크기가 manifest와 다르다: {target}")
        if _sha256_of(target) != entry["sha256"]:
            raise ModelAssetsError(f"재조립 결과가 manifest와 다르다: {target}")

    for name in manifest.get("companions", []):
        source = model_dir / name
        if not source.is_file():
            raise ModelAssetsError(f"모델 부속 파일이 없다: {source}")
        shutil.copy2(source, dest / name)


def ensure_model_dir(
    model_dir: str | os.PathLike[str],
    dest: str | os.PathLike[str] | None = None,
    force: bool = False,
) -> Path:
    """transformers `from_pretrained`에 바로 넘길 수 있는 디렉터리를 돌려준다.

    - 모델 디렉터리에 온전한 `model.safetensors`가 있으면(예전처럼 Releases에서 받은
      경우) 아무것도 하지 않고 그 디렉터리를 그대로 쓴다.
    - 아니면 `weights/`의 조각을 되살린 디렉터리 경로를 돌려준다. 이미 되살려 뒀으면
      stamp만 확인하고 즉시 반환한다.

    되살릴 수 없으면 `ModelAssetsError`를 올린다.
    """
    model_dir = Path(model_dir)
    if not force and (model_dir / "model.safetensors").is_file():
        return model_dir

    weights_dir = model_dir / WEIGHTS_SUBDIR
    manifest_path = weights_dir / MANIFEST_NAME
    dest = Path(dest).expanduser().resolve() if dest else _default_dest(model_dir)

    if not manifest_path.is_file():
        # 이미지 빌드 단계에서 이미 되살려 두고 조각은 이미지에 넣지 않은 경우.
        if _is_materialized(dest, None, None):
            return dest
        raise ModelAssetsError(
            f"가중치를 찾을 수 없다: {manifest_path} 도 {model_dir / 'model.safetensors'} 도 없다. "
            "저장소를 통째로 clone 했는지 확인하고, 없다면 "
            "`python scripts/shard_bert_model.py <model.safetensors>` 로 조각을 만든다."
        )

    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    stamp = hashlib.sha256(manifest_raw).hexdigest()

    if not force and _is_materialized(dest, stamp, manifest):
        return dest

    with _BuildLock(dest) as lock:
        # 기다리는 사이 다른 워커가 끝냈을 수 있다.
        if not force and _is_materialized(dest, stamp, manifest):
            return dest
        if not lock.acquired:
            raise ModelAssetsError(
                f"다른 프로세스의 가중치 재조립을 {_LOCK_WAIT_SECONDS:.0f}초 기다렸지만 끝나지 않았다: {dest}"
            )
        try:
            _materialize(model_dir, weights_dir, manifest, dest)
        except OSError as exc:
            raise ModelAssetsError(
                f"{dest} 에 가중치를 되살리지 못했다: {exc}. "
                f"쓰기 가능한 경로를 {CACHE_ENV} 환경변수로 지정한다."
            ) from exc
        (dest / STAMP_NAME).write_text(stamp, encoding="utf-8")

    return dest


def main() -> int:
    """이미지 빌드 단계에서 가중치를 미리 되살려 두기 위한 진입점."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-dir",
        default=str(REPO_ROOT / "materials" / "BERT_FR_NFR_Classifier" / "bert_model"),
        help="쪼갠 조각이 들어 있는 모델 디렉터리",
    )
    parser.add_argument("--dest", default=None, help="되살릴 위치 (기본: .easydep/models/bert_fr_nfr)")
    parser.add_argument("--force", action="store_true", help="stamp를 무시하고 다시 만든다")
    args = parser.parse_args()

    resolved = ensure_model_dir(args.model_dir, dest=args.dest, force=args.force)
    print(f"[model_assets] 가중치 준비 완료: {resolved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
