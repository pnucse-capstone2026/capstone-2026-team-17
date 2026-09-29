"""Few-shot 예시 샘플링 전략 모듈.

`auto_clarify.extract_examples_from_xlsx` 의 무작위 추출은 baseline 이다. 추상 요구사항
(쿼리)과의 의미 근접성을 무시하므로, 프롬프트에 붙는 [Reference Examples] 가 정작
구체화하려는 요구사항과 동떨어질 수 있다. 이 모듈은 그 baseline 을 코사인 유사도 기반
샘플링과 같은 인터페이스로 비교할 수 있게 한다.

전략(strategy)
  - "random" : 무작위 k개 (baseline)
  - "cosine" : 쿼리와 코사인 유사도 top-k
  - "mmr"    : Maximal Marginal Relevance — 관련성과 다양성을 함께 최적화(top-k 중복 억제)

임베딩 백엔드(backend) — 둘 다 추가 의존성 없이 동작
  - "tfidf" : 순수 numpy TF-IDF. 오프라인·즉시. 어휘(lexical) 유사도.
  - "bert"  : 로컬 파인튜닝 BERT(settings.bert_model_path)의 마지막 은닉층 평균풀링.
              torch/transformers 필요(이미 프로젝트 의존성). 문맥(semantic) 유사도.
              주의: 이 체크포인트는 FR/NFR 분류로 파인튜닝됐으므로 임베딩이 그 태스크에
              다소 편향될 수 있다. 순수 의미 임베딩이 필요하면 model_path 를 범용 BERT로
              바꾸면 된다. 코퍼스 임베딩은 artifacts/embeddings_cache/ 에 캐시된다.

쿼리가 비었으면 유사도 계산이 불가하므로 "random" 으로 자동 강등한다.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from collections.abc import Callable
from functools import lru_cache

import numpy as np
import pandas as pd

# 기본 데이터셋/컬럼. auto_clarify 와 동일한 기본값을 사용한다.
DEFAULT_DATASET = "materials/FR_NFR_Dataset/FR_NFR_Dataset.xlsx"
_TEXT_COLUMN = "Requirement Text"
_CACHE_DIR = "artifacts/embeddings_cache"

_TOKEN_RE = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------------------
# 데이터셋 로딩
# ---------------------------------------------------------------------------
@lru_cache(maxsize=8)
def _load_corpus(dataset_path: str) -> tuple[str, ...]:
    """요구사항 텍스트 리스트를 로드(결측 제거·중복 제거·공백 정리). 프로세스 내 캐시."""
    df = pd.read_excel(dataset_path)
    series = df[_TEXT_COLUMN].dropna().astype(str).map(str.strip)
    series = series[series.str.len() > 0]
    # 중복은 top-k 가 같은 문장으로 채워지는 걸 막기 위해 제거하되 순서는 보존.
    return tuple(dict.fromkeys(series.tolist()))


# ---------------------------------------------------------------------------
# TF-IDF 백엔드 (순수 numpy — 오프라인, 의존성 없음)
# ---------------------------------------------------------------------------
def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _build_tfidf(texts: tuple[str, ...]) -> tuple[list[dict[str, float]], dict[str, float]]:
    """각 문서의 L2 정규화된 sparse TF-IDF 벡터(dict)와 idf 맵을 만든다.

    dense (n_docs x vocab) 행렬은 메모리를 크게 먹으므로 문서별 sparse dict 로 유지한다.
    코사인 유사도는 정규화된 두 dict 의 공통 항 내적으로 계산된다.
    """
    n = len(texts)
    df: Counter = Counter()
    tokenized: list[Counter] = []
    for t in texts:
        counts = Counter(_tokenize(t))
        tokenized.append(counts)
        df.update(counts.keys())

    # 스무딩된 idf: 흔한 단어의 영향을 줄인다.
    idf = {term: math.log((1 + n) / (1 + d)) + 1.0 for term, d in df.items()}

    doc_vecs: list[dict[str, float]] = []
    for counts in tokenized:
        vec = {term: cnt * idf[term] for term, cnt in counts.items()}
        norm = math.sqrt(sum(w * w for w in vec.values())) or 1.0
        doc_vecs.append({term: w / norm for term, w in vec.items()})
    return doc_vecs, idf


def _tfidf_vec(text: str, idf: dict[str, float]) -> dict[str, float]:
    """쿼리/문장 하나를 코퍼스 idf 로 L2 정규화된 sparse 벡터로 변환(어휘 밖 단어는 무시)."""
    counts = Counter(_tokenize(text))
    vec = {term: cnt * idf[term] for term, cnt in counts.items() if term in idf}
    norm = math.sqrt(sum(w * w for w in vec.values())) or 1.0
    return {term: w / norm for term, w in vec.items()}


def _sparse_cos(a: dict[str, float], b: dict[str, float]) -> float:
    """정규화된 두 sparse 벡터의 코사인 = 공통 항 내적."""
    # 더 작은 dict 를 순회.
    if len(a) > len(b):
        a, b = b, a
    return float(sum(w * b.get(term, 0.0) for term, w in a.items()))


# ---------------------------------------------------------------------------
# BERT 백엔드 (로컬 파인튜닝 모델 평균풀링, 디스크 캐시)
# ---------------------------------------------------------------------------
def _corpus_cache_key(dataset_path: str, model_path: str) -> str:
    stat = os.stat(dataset_path)
    raw = f"{os.path.abspath(dataset_path)}|{stat.st_mtime_ns}|{stat.st_size}|bert|{model_path}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _bert_embed_corpus(texts: tuple[str, ...], dataset_path: str, model_path: str) -> np.ndarray:
    """코퍼스 전체를 (n, d) L2 정규화 임베딩 행렬로. artifacts/ 에 캐시."""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(_CACHE_DIR, f"corpus_{_corpus_cache_key(dataset_path, model_path)}.npy")
    if os.path.exists(cache_file):
        cached = np.load(cache_file)
        if cached.shape[0] == len(texts):
            return cached

    mat = _bert_encode(list(texts), model_path)
    np.save(cache_file, mat)
    return mat


@lru_cache(maxsize=2)
def _load_bert(model_path: str):
    """(tokenizer, model, torch) 를 1회 로드. 인코더로만 쓴다(분류 헤드 미사용)."""
    import torch
    from transformers import AutoModel, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_path)
    # BertForSequenceClassification 체크포인트라도 AutoModel 은 base 인코더만 로드한다
    # (분류 헤드 가중치는 무시). 우리는 은닉 상태만 필요.
    model = AutoModel.from_pretrained(model_path)
    model.eval()
    return tokenizer, model, torch


def _bert_encode(texts: list[str], model_path: str, batch_size: int = 32) -> np.ndarray:
    """attention mask 를 반영한 평균풀링으로 문장 임베딩(L2 정규화)을 배치 계산."""
    tokenizer, model, torch = _load_bert(model_path)
    out: list[np.ndarray] = []
    with torch.no_grad():
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            enc = tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=128,
                return_tensors="pt",
            )
            hidden = model(**enc).last_hidden_state  # (B, T, H)
            mask = enc["attention_mask"].unsqueeze(-1).float()  # (B, T, 1)
            summed = (hidden * mask).sum(dim=1)
            counts = mask.sum(dim=1).clamp(min=1e-9)
            mean = summed / counts  # (B, H)
            mean = torch.nn.functional.normalize(mean, p=2, dim=1)
            out.append(mean.cpu().numpy())
    return np.vstack(out).astype(np.float32)


# ---------------------------------------------------------------------------
# NIM 임베딩 백엔드 (Nvidia NIM 무료 임베딩 엔드포인트, OpenAI 호환, 디스크 캐시)
# ---------------------------------------------------------------------------
def _nim_cache_key(dataset_path: str, model: str) -> str:
    stat = os.stat(dataset_path)
    raw = f"{os.path.abspath(dataset_path)}|{stat.st_mtime_ns}|{stat.st_size}|nim|{model}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


@lru_cache(maxsize=1)
def _nim_client():
    """공통 LLM 연결로 OpenAI 호환 임베딩 클라이언트를 한 번 만든다."""
    from openai import OpenAI

    from app.llm_connection import build_llm_connection

    connection = build_llm_connection()
    return OpenAI(
        api_key=connection.api_key,
        base_url=connection.base_url,
        default_headers=connection.default_headers(),
    )


def _nim_encode(texts: list[str], model: str, input_type: str,
                batch_size: int = 32) -> np.ndarray:
    """NIM 임베딩을 배치로 계산해 (n, d) L2 정규화 행렬로 반환.

    nemotron-embed 계열은 쿼리/문서를 구분하는 input_type("query"|"passage")을
    extra_body 로 요구한다. 코사인용으로 임베딩을 L2 정규화한다.
    """
    client = _nim_client()
    out: list[np.ndarray] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        resp = client.embeddings.create(
            model=model,
            input=batch,
            extra_body={"input_type": input_type, "truncate": "END"},
        )
        # API 는 index 순서를 보장하지만 방어적으로 정렬.
        vecs = [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
        out.append(np.asarray(vecs, dtype=np.float32))
    mat = np.vstack(out)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def _nim_embed_corpus(texts: tuple[str, ...], dataset_path: str, model: str) -> np.ndarray:
    """코퍼스를 passage 로 임베딩(디스크 캐시). NIM 호출 비용을 1회로 제한."""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(_CACHE_DIR, f"corpus_{_nim_cache_key(dataset_path, model)}.npy")
    if os.path.exists(cache_file):
        cached = np.load(cache_file)
        if cached.shape[0] == len(texts):
            return cached
    mat = _nim_encode(list(texts), model, input_type="passage")
    np.save(cache_file, mat)
    return mat


# ---------------------------------------------------------------------------
# 통합 랭킹 인터페이스
# ---------------------------------------------------------------------------
class _Ranking:
    """쿼리 대비 코퍼스 유사도 + 문서 간 pairwise 유사도(다양성/ MMR용)를 제공."""

    def __init__(self, scores: np.ndarray, pairwise: Callable[[int, int], float]):
        self.scores = scores  # shape (n,) 쿼리와의 코사인
        self.pairwise = pairwise  # (i, j) -> 코사인


def _rank(query: str, texts: tuple[str, ...], backend: str, model_path: str,
          dataset_path: str, embed_model: str | None = None) -> _Ranking:
    if backend == "tfidf":
        doc_vecs, idf = _build_tfidf(texts)
        q = _tfidf_vec(query, idf)
        scores = np.array([_sparse_cos(q, d) for d in doc_vecs], dtype=np.float32)
        return _Ranking(scores, lambda i, j: _sparse_cos(doc_vecs[i], doc_vecs[j]))

    if backend == "bert":
        mat = _bert_embed_corpus(texts, dataset_path, model_path)  # (n, d) 정규화됨
        qv = _bert_encode([query], model_path)[0]  # (d,) 정규화됨
        scores = (mat @ qv).astype(np.float32)  # 정규화됨 → 내적 = 코사인
        return _Ranking(scores, lambda i, j: float(mat[i] @ mat[j]))

    if backend == "nim":
        if embed_model is None:
            from app.requirements.config import settings
            embed_model = settings.embed_model
        mat = _nim_embed_corpus(texts, dataset_path, embed_model)  # (n, d) 정규화됨
        qv = _nim_encode([query], embed_model, input_type="query")[0]  # (d,) 정규화됨
        scores = (mat @ qv).astype(np.float32)
        return _Ranking(scores, lambda i, j: float(mat[i] @ mat[j]))

    raise ValueError(f"알 수 없는 backend: {backend!r} (tfidf|bert|nim)")


def _select_topk(ranking: _Ranking, k: int) -> list[int]:
    return np.argsort(-ranking.scores)[:k].tolist()


def _select_mmr(ranking: _Ranking, k: int, lambda_: float = 0.7,
                pool: int = 60) -> list[int]:
    """MMR: score = λ·sim(query) − (1−λ)·max sim(선택된 것들). 후보는 상위 pool 개로 제한."""
    candidates = np.argsort(-ranking.scores)[:pool].tolist()
    selected: list[int] = []
    while candidates and len(selected) < k:
        best_idx, best_val = None, -math.inf
        for c in candidates:
            div = max((ranking.pairwise(c, s) for s in selected), default=0.0)
            val = lambda_ * float(ranking.scores[c]) - (1 - lambda_) * div
            if val > best_val:
                best_idx, best_val = c, val
        selected.append(best_idx)  # type: ignore[arg-type]
        candidates.remove(best_idx)  # type: ignore[arg-type]
    return selected


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------
def sample_examples(
    query: str = "",
    dataset_path: str = DEFAULT_DATASET,
    sample_size: int = 5,
    strategy: str = "random",
    backend: str = "tfidf",
    model_path: str | None = None,
    embed_model: str | None = None,
    seed: int | None = None,
    mmr_lambda: float = 0.7,
) -> list[tuple[str, float]]:
    """전략에 따라 예시 (문장, 점수) 리스트를 반환.

    strategy="random" 은 점수 0.0(비교용). cosine/mmr 은 쿼리와의 코사인 점수를 담는다.
    쿼리가 비면 random 으로 강등한다.
    backend="nim" 이면 embed_model(기본 settings.embed_model)로 NIM 임베딩을 쓴다.
    """
    texts = _load_corpus(dataset_path)
    k = min(sample_size, len(texts))
    if k == 0:
        return []

    if model_path is None:
        # settings 를 지연 import (테스트/스크립트에서 config 없이도 tfidf 사용 가능하게).
        from app.requirements.config import settings
        model_path = settings.bert_model_path

    if strategy == "random" or not query.strip():
        rng = np.random.default_rng(seed)
        idx = rng.choice(len(texts), size=k, replace=False).tolist()
        # 무작위 예시의 실제 쿼리 유사도도 함께 보여주면 비교가 풍부해진다(있을 때만).
        if query.strip():
            ranking = _rank(query, texts, backend, model_path, dataset_path, embed_model)
            return [(texts[i], float(ranking.scores[i])) for i in idx]
        return [(texts[i], 0.0) for i in idx]

    ranking = _rank(query, texts, backend, model_path, dataset_path, embed_model)
    if strategy == "cosine":
        idx = _select_topk(ranking, k)
    elif strategy == "mmr":
        idx = _select_mmr(ranking, k, lambda_=mmr_lambda)
    else:
        raise ValueError(f"알 수 없는 strategy: {strategy!r} (random|cosine|mmr)")
    return [(texts[i], float(ranking.scores[i])) for i in idx]


def format_examples(items: list[tuple[str, float]], header: str = "[Reference Examples from Actual Dataset]") -> str:
    """sample_examples 결과를 프롬프트용 텍스트 블록으로."""
    lines = [header]
    for text, _score in items:
        lines.append(f"- {text}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 비교/평가 하니스
# ---------------------------------------------------------------------------
def _diversity(items_idx: list[int], ranking: _Ranking) -> float:
    """선택된 예시들 간 평균 pairwise 코사인(낮을수록 다양)."""
    if len(items_idx) < 2:
        return 0.0
    sims = [ranking.pairwise(a, b)
            for i, a in enumerate(items_idx) for b in items_idx[i + 1:]]
    return float(np.mean(sims))


def compare_strategies(
    query: str,
    dataset_path: str = DEFAULT_DATASET,
    sample_size: int = 5,
    backend: str = "tfidf",
    strategies: tuple[str, ...] = ("random", "cosine", "mmr"),
    seed: int | None = 42,
    model_path: str | None = None,
    embed_model: str | None = None,
) -> dict:
    """여러 전략을 같은 쿼리로 돌려 정량 지표와 함께 반환.

    지표:
      - mean_query_sim : 선택 예시들의 쿼리 평균 코사인(관련성 ↑ 좋음)
      - diversity      : 선택 예시들 간 평균 코사인(↓ 다양함, 중복 적음)
      - examples       : [(text, query_sim)]
    random 은 seed 고정으로 재현 가능.
    """
    if model_path is None:
        from app.requirements.config import settings
        model_path = settings.bert_model_path

    texts = _load_corpus(dataset_path)
    ranking = (_rank(query, texts, backend, model_path, dataset_path, embed_model)
               if query.strip() else None)
    # 텍스트→인덱스 역참조(다양성 계산에 원본 인덱스가 필요).
    index_of = {t: i for i, t in enumerate(texts)}

    report: dict = {"query": query, "backend": backend, "sample_size": sample_size, "results": {}}
    for strat in strategies:
        items = sample_examples(
            query=query, dataset_path=dataset_path, sample_size=sample_size,
            strategy=strat, backend=backend, model_path=model_path,
            embed_model=embed_model, seed=seed,
        )
        idx = [index_of[t] for t, _ in items]
        mean_sim = float(np.mean([s for _, s in items])) if items else 0.0
        div = _diversity(idx, ranking) if ranking is not None else 0.0
        report["results"][strat] = {
            "mean_query_sim": round(mean_sim, 4),
            "diversity": round(div, 4),
            "examples": [(t, round(s, 4)) for t, s in items],
        }
    return report


def _print_report(report: dict) -> None:
    print(f"\n쿼리: {report['query']!r}")
    print(f"백엔드: {report['backend']} | k={report['sample_size']}\n")
    for strat, r in report["results"].items():
        print(f"── {strat.upper():7s}  관련성(mean_query_sim)={r['mean_query_sim']:.4f}  "
              f"다양성(diversity)={r['diversity']:.4f}")
        for text, score in r["examples"]:
            snippet = text if len(text) <= 100 else text[:97] + "..."
            print(f"     [{score:+.3f}] {snippet}")
        print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Few-shot 예시 샘플링 전략 비교(random vs cosine vs mmr)")
    parser.add_argument("query", nargs="?", default="The system should be secure and fast for users.",
                        help="구체화하려는 추상 요구사항(쿼리)")
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--k", type=int, default=5, help="예시 개수")
    parser.add_argument("--backend", choices=["tfidf", "bert", "nim"], default="tfidf",
                        help="tfidf=오프라인·즉시, bert=로컬 모델 의미 임베딩, nim=NIM 임베딩 API")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    _print_report(compare_strategies(
        query=args.query, dataset_path=args.dataset, sample_size=args.k,
        backend=args.backend, seed=args.seed,
    ))
