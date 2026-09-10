from __future__ import annotations

import math

import pytest

from knowledge_platform.retrieval.hybrid import bm25_rank, rrf_fuse


def test_bm25_frequency_and_length_normalization_are_stable() -> None:
    ranked = bm25_rank("alpha", ["alpha", "alpha alpha", "alpha " * 30, "beta"], 10)
    assert [index for index, _score in ranked] == [2, 1, 0]
    assert bm25_rank("missing", ["alpha"], 5) == []


def test_bm25_chinese_and_empty_inputs() -> None:
    assert bm25_rank("知识图谱", ["这是知识图谱系统", "知识库", "完全不同"], 2)[0][0] == 0
    assert bm25_rank("", ["alpha"], 5) == []
    assert bm25_rank("x", [], 5) == []


def test_rrf_consensus_normalization_and_ties() -> None:
    result = rrf_fuse([[2, 1, 0], [1, 2, 3]], [1.0, 1.0], 60, 4)
    assert [index for index, _score in result] == [1, 2, 0, 3]
    assert 0 < result[0][1] < 1
    assert all(0 <= score <= 1 and math.isfinite(score) for _index, score in result)


@pytest.mark.parametrize("rankings,weights,k,limit", [([[1, 1]], [1], 60, 5), ([[-1]], [1], 60, 5), ([[True]], [1], 60, 5), ([[1]], [math.nan], 60, 5), ([[1]], [0], 60, 5), ([], [], 60, 5), ([[1]], [1], 0, 5)])
def test_rrf_rejects_invalid_inputs(rankings, weights, k, limit) -> None:
    with pytest.raises(ValueError):
        rrf_fuse(rankings, weights, k, limit)


def test_bm25_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError):
        bm25_rank("x", ["x"], True)
    with pytest.raises(ValueError):
        bm25_rank("x", ["x"], -1)


def test_unicode_word_matching_and_disabled_route():
    assert bm25_rank('CAFÉ',['café noir','other'],1)[0][0]==0
    assert rrf_fuse([[0],[1]],[0,1],60,2)==[(1,1.0)]
