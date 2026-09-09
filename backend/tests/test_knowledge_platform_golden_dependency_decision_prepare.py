from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.phase0a_golden_dependency_decision_prepare import prepare_candidate


def _inputs(root: Path) -> tuple[Path, Path, str]:
    queue = json.loads((root / "artifacts/phase0a/golden-dependency-review-queue.json").read_text(encoding="utf-8"))
    return (
        root / "artifacts/phase0a/golden-dependency-review-queue.json",
        root / "docs/knowledge-platform/golden-records/connectors_and_capture.json",
        str(queue["items"][0]["review_id"]),
    )


@pytest.mark.parametrize("decision", ["include_observer", "exclude_observer"])
def test_explicit_golden_decision_writes_candidate_only(tmp_path: Path, decision: str) -> None:
    root = Path(__file__).resolve().parents[2]
    queue, canonical, review_id = _inputs(root)
    output = tmp_path / f"{decision}.json"
    result = prepare_candidate(
        queue_path=queue,
        canonical_path=canonical,
        output_path=output,
        review_id=review_id,
        decision=decision,
    )
    assert result["decision"] == decision
    assert result["canonical_unchanged"] is True
    assert "/Users/" not in output.read_text(encoding="utf-8")
    assert "/private/" not in output.read_text(encoding="utf-8")


def test_explicit_golden_decision_rejects_unknown_review_id(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    queue, canonical, _ = _inputs(root)
    with pytest.raises(ValueError, match="review ID"):
        prepare_candidate(
            queue_path=queue,
            canonical_path=canonical,
            output_path=tmp_path / "candidate.json",
            review_id="sha256:" + "0" * 64,
            decision="include_observer",
        )


def test_explicit_golden_decision_rejects_stale_canonical(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    queue, canonical, review_id = _inputs(root)
    stale = tmp_path / "canonical.json"
    shutil.copyfile(canonical, stale)
    stale.write_text(stale.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="canonical record is stale"):
        prepare_candidate(
            queue_path=queue,
            canonical_path=stale,
            output_path=tmp_path / "candidate.json",
            review_id=review_id,
            decision="include_observer",
        )
