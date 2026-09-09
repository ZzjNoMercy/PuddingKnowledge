from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.phase0a_golden_dependency_decision_prepare import prepare_candidate


def _inputs(root: Path) -> tuple[Path, Path, str, dict[str, object]]:
    from tests._knowledge_platform_target_fixtures import golden_dependency_inputs

    return golden_dependency_inputs(root)


@pytest.mark.parametrize("decision", ["include_observer", "exclude_observer"])
def test_explicit_golden_decision_writes_candidate_only(tmp_path: Path, decision: str) -> None:
    queue, canonical, review_id, observation = _inputs(tmp_path)
    output = tmp_path / f"{decision}.json"
    result = prepare_candidate(
        queue_path=queue,
        canonical_path=canonical,
        output_path=output,
        review_id=review_id,
        decision=decision,
        observation=observation,
    )
    assert result["decision"] == decision
    assert result["canonical_unchanged"] is True
    assert "/Users/" not in output.read_text(encoding="utf-8")
    assert "/private/" not in output.read_text(encoding="utf-8")


def test_explicit_golden_decision_rejects_unknown_review_id(tmp_path: Path) -> None:
    queue, canonical, _, observation = _inputs(tmp_path)
    with pytest.raises(ValueError, match="review ID"):
        prepare_candidate(
            queue_path=queue,
            canonical_path=canonical,
            output_path=tmp_path / "candidate.json",
            review_id="sha256:" + "0" * 64,
            decision="include_observer",
            observation=observation,
        )


def test_explicit_golden_decision_rejects_stale_canonical(tmp_path: Path) -> None:
    queue, canonical, review_id, observation = _inputs(tmp_path)
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
            observation=observation,
        )
