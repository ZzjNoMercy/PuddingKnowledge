from __future__ import annotations

import json
from argparse import Namespace

from scripts import phase8_local_processing_authoring_process_shadow as shadow


def test_process_authoring_shadow_path_digest_does_not_echo_path(tmp_path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("secret-looking-local-path", encoding="utf-8")

    digest = shadow._path_digest(source)

    assert digest.startswith("sha256:")
    assert str(source) not in digest


def test_process_authoring_child_failure_is_bounded(monkeypatch, capsys, tmp_path) -> None:
    def fail(**_kwargs):
        raise RuntimeError("raw local path must not cross the child contract")

    monkeypatch.setattr(shadow, "_child_result", fail)
    exit_code = shadow._run_child(
        Namespace(catalog=tmp_path / "catalog.sqlite3", file=tmp_path / "source.csv", phase="replay")
    )

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {"status": "failed"}
