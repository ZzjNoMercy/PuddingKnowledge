"""Small, deterministic lexical and rank-fusion primitives."""
from __future__ import annotations

import math
import re
from collections import Counter

_TOKEN = re.compile(r"[^\W\u4e00-\u9fff]+|[\u4e00-\u9fff]")
_MAX_QUERY = 512
_MAX_TEXTS = 10_000
_MAX_TEXT = 12_000


def _tokens(value: str) -> list[str]:
    if not isinstance(value, str) or len(value) > _MAX_TEXT:
        raise ValueError("text is invalid or too large")
    value = value.casefold()
    base = _TOKEN.findall(value)
    out = list(base)
    # Adjacent Chinese bigrams provide phrase-sensitive matches while retaining
    # individual-character recall.
    run = ""
    for char in value:
        if "\u4e00" <= char <= "\u9fff":
            run += char
        else:
            if len(run) > 1:
                out.extend(run[i : i + 2] for i in range(len(run) - 1))
            run = ""
    if len(run) > 1:
        out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return out


def bm25_rank(query: str, texts: list[str], limit: int) -> list[tuple[int, float]]:
    if not isinstance(query, str) or len(query) > _MAX_QUERY or not isinstance(texts, list) or len(texts) > _MAX_TEXTS:
        raise ValueError("BM25 input is invalid or too large")
    if type(limit) is not int or limit < 0 or limit > _MAX_TEXTS:
        raise ValueError("limit is invalid")
    if any(not isinstance(text,str) or len(text)>_MAX_TEXT for text in texts) or sum(len(text.encode("utf-8")) for text in texts)>32*1024*1024:
        raise ValueError("BM25 corpus exceeds bound")
    query_terms = _tokens(query)
    if not query_terms or limit == 0:
        return []
    docs = [_tokens(text) for text in texts]
    if not docs:
        return []
    lengths = [len(doc) for doc in docs]
    average = sum(lengths) / len(lengths) if lengths else 0.0
    document_frequency = Counter(term for doc in docs for term in set(doc))
    query_frequency = Counter(query_terms)
    scores: list[tuple[int, float]] = []
    for index, doc in enumerate(docs):
        frequency = Counter(doc)
        score = 0.0
        for term, qf in query_frequency.items():
            tf = frequency.get(term, 0)
            if not tf:
                continue
            df = document_frequency.get(term, 0)
            idf = math.log(1.0 + (len(docs) - df + 0.5) / (df + 0.5))
            denominator = tf + 1.5 * (1.0 - 0.75 + 0.75 * (len(doc) / average if average else 0.0))
            score += idf * (tf * (1.5 + 1.0) / denominator) * qf
        if score > 0.0 and math.isfinite(score):
            scores.append((index, score))
    scores.sort(key=lambda item: (-item[1], item[0]))
    return scores[:limit]


def rrf_fuse(rankings: list[list[int]], weights: list[float], k: int, limit: int) -> list[tuple[int, float]]:
    if not isinstance(rankings, list) or not isinstance(weights, list) or len(rankings) != len(weights) or not rankings:
        raise ValueError("rankings and weights are invalid")
    if type(k) is not int or not 1 <= k <= 1000 or type(limit) is not int or not 0 <= limit <= _MAX_TEXTS:
        raise ValueError("k or limit is invalid")
    if len(rankings) > 8:
        raise ValueError("too many rankings")
    clean_weights: list[float] = []
    for weight in weights:
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(float(weight)) or not 0 <= weight <= 100:
            raise ValueError("weight is invalid")
        clean_weights.append(float(weight))
    if not any(weight > 0 for weight in clean_weights):
        raise ValueError("at least one weight must be positive")
    fused: dict[int, float] = {}
    for ranking, weight in zip(rankings, clean_weights):
        if not isinstance(ranking, list) or len(ranking) > _MAX_TEXTS:
            raise ValueError("ranking is invalid")
        if any(isinstance(index, bool) or type(index) is not int or index < 0 for index in ranking) or len(set(ranking)) != len(ranking):
            raise ValueError("ranking indexes must be unique non-negative integers")
        if weight == 0:
            continue
        for position, index in enumerate(ranking, start=1):
            fused[index] = fused.get(index, 0.0) + weight / (k + position)
    maximum = sum(clean_weights) / (k + 1)
    result = [(index, min(1.0, max(0.0, score / maximum))) for index, score in fused.items() if score > 0]
    result.sort(key=lambda item: (-item[1], item[0]))
    return result[:limit]
