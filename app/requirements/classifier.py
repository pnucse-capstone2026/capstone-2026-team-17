"""BERT 기반 FR/NFR 분류기.

materials/BERT_FR_NFR_Classifier 노트북에서 파인튜닝한 BertForSequenceClassification
체크포인트를 로드해 요구사항의 FR/NFR 유형을 단독으로 판정한다.

라벨 매핑(학습 시 고정): 0 = NFR, 1 = FR
전처리(노트북 추론 셀과 동일): max_length=128, padding/truncation, softmax confidence.

무거운 의존성(torch/transformers)과 417MB 가중치를 쓰므로:
  - 모델은 최초 호출 시 1회 지연 로딩(lazy) 후 프로세스 전역 재사용.
  - settings.enable_bert_verify=False 이면 로드 자체를 건너뛴다.

가중치는 GitHub 파일 한도 때문에 저장소에 쪼개 들어 있다. 로드 직전
`model_assets.ensure_model_dir`로 되살린 디렉터리를 얻어 쓴다(자세한 내용은 그 모듈).
"""
from __future__ import annotations

import threading

from app.requirements.config import settings
from app.requirements.model_assets import ensure_model_dir
from app.requirements.runtime import telemetry

# 0 = NFR, 1 = FR  (학습 시 {'NFR': 0, 'FR': 1} 매핑과 동일)
_ID2LABEL = {0: "NFR", 1: "FR"}
_MAX_LENGTH = 128

# 지연 로딩된 (tokenizer, model, torch) 캐시. 로드 실패 시 False로 표시해 재시도 방지.
_bundle = None
_load_lock = threading.Lock()


def _load_bundle():
    """(tokenizer, model, torch) 튜플을 1회 로드해 반환. 실패하면 None."""
    global _bundle
    if _bundle is not None:
        return _bundle or None  # False -> None

    with _load_lock:
        if _bundle is not None:
            return _bundle or None
        try:
            import torch
            from transformers import BertForSequenceClassification, BertTokenizer

            # 쪼개 커밋한 샤드를 로드 가능한 디렉터리로 되살린다(이미 있으면 즉시 반환).
            model_dir = ensure_model_dir(settings.bert_model_path)
            tokenizer = BertTokenizer.from_pretrained(model_dir)
            model = BertForSequenceClassification.from_pretrained(model_dir)
            model.eval()
            _bundle = (tokenizer, model, torch)
        except Exception as exc:  # noqa: BLE001 - preclassified batch execution can continue
            # Raw analysis fails closed when classification is unavailable.  The process may
            # still serve execution paths whose inputs already carry authoritative labels.
            telemetry.record_degradation(
                "classifier.bert",
                f"Raw FR/NFR classification is unavailable: "
                f"{type(exc).__name__}: {exc}",
            )
            _bundle = False
            return None
    return _bundle


def bert_available() -> bool:
    """BERT 검증을 실제로 수행할 수 있는지."""
    if not settings.enable_bert_verify:
        return False
    return _load_bundle() is not None


def warmup() -> bool:
    """서버 기동 시 1회 호출해 BERT 가중치를 미리 로드(+워밍업 추론 1회)한다.

    이후 모든 요청은 프로세스 전역 캐시(_bundle)를 재사용하므로 재로딩이 없다.
    (멀티 워커로 띄우면 워커 프로세스마다 1회씩 로드된다.)
    enable_bert_verify=False면 로드하지 않고 False를 반환한다.
    """
    if not bert_available():
        return False
    # 첫 추론에서 지연 초기화되는 연산 그래프/커널까지 데워 첫 요청 지연을 없앤다.
    classify_bert("The system shall respond within 2 seconds.")
    return True


def warmup_or_raise() -> bool:
    """Warm up the enabled classifier and fail startup when it cannot load.

    STEP 1 uses BERT as its only FR/NFR classifier, so a server configured to
    classify raw requirements must not advertise itself as healthy without it.
    A process intentionally limited to preclassified batch inputs may set
    ``ENABLE_BERT_VERIFY=false``.
    """
    loaded = warmup()
    if settings.enable_bert_verify and not loaded:
        raise RuntimeError(
            "BERT FR/NFR classifier is enabled but failed to load. "
            "Check the classifier.bert degradation log and model assets, or set "
            "ENABLE_BERT_VERIFY=false only for preclassified batch execution."
        )
    return loaded


def classify_bert(text: str) -> tuple[str, float] | None:
    """단일 영어 요구사항 문장을 BERT로 분류.

    Returns (label, confidence) 예: ("FR", 0.98). 사용 불가 시 None.
    """
    if not settings.enable_bert_verify:
        return None
    bundle = _load_bundle()
    if bundle is None:
        return None

    tokenizer, model, torch = bundle
    inputs = tokenizer(
        text,
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=_MAX_LENGTH,
    )
    with torch.no_grad():
        logits = model(**inputs).logits
        probs = torch.nn.functional.softmax(logits, dim=-1)
        confidence, pred = torch.max(probs, dim=-1)

    return _ID2LABEL[int(pred.item())], float(confidence.item())
