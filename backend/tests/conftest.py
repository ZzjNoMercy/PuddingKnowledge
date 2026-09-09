"""Platform tests isolate their Home without importing a Harness runtime."""
import pytest


@pytest.fixture(autouse=True)
def isolate_platform_home(tmp_path, monkeypatch):
    monkeypatch.setenv('PUDDINGKNOWLEDGE_HOME', str(tmp_path / 'knowledge-home'))
    # Retain a sentinel for assertions that legacy Home must never be used.
    monkeypatch.setenv('PUDDINGCLAW_HOME', str(tmp_path / 'legacy-home'))
