from __future__ import annotations

import os
from pathlib import Path


def test_local_notification_shadow_is_space_bound_and_does_not_modify_canonical_catalog(tmp_path: Path) -> None:
    from scripts.phase9_local_notification_shadow import run_shadow

    legacy_source = Path(os.environ["PUDDINGKNOWLEDGE_LEGACY_SOURCE"]).expanduser().resolve()
    report = run_shadow(
        catalog=legacy_source / "artifacts/phase0b-local-catalog/knowledge-platform.sqlite3",
        output_dir=tmp_path,
    )
    assert report["status"] == "PHASE9_LOCAL_NOTIFICATION_SHADOW_PASS_NOT_ACTIVATABLE"
    assert report["activation"] == "not-activated"
    assert report["legacy_unbound_event_count"] > 0
    assert report["bound_event_count"] == 1
    assert report["canonical_catalog_unchanged"] is True
    assert report["http"] == {"status_code": 200, "status": "ok", "count": 1}
    assert "/Users/" not in str(report)
