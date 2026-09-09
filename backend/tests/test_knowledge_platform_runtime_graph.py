from __future__ import annotations

import importlib
import sys
import threading
from pathlib import Path

import pytest

from knowledge_platform.baseline import capture_runtime_call_graph


def _probe_package(tmp_path: Path, name: str) -> None:
    package = tmp_path / name
    package.mkdir()
    (package / "__init__.py").write_text("\n", encoding="utf-8")
    (package / "worker.py").write_text(
        "def leaf():\n    return 'observed'\n",
        encoding="utf-8",
    )
    (package / "entry.py").write_text(
        "import threading\nfrom .worker import leaf\n\ndef run():\n    return leaf()\n\ndef _thread_entry():\n    return leaf()\n\ndef run_threaded():\n    thread = threading.Thread(target=_thread_entry)\n    thread.start()\n    thread.join()\n",
        encoding="utf-8",
    )


def test_runtime_graph_records_only_observed_cross_module_edges(tmp_path: Path) -> None:
    package_name = f"runtime_probe_{tmp_path.name.replace('-', '_')}"
    _probe_package(tmp_path, package_name)
    sys.path.insert(0, str(tmp_path))
    try:
        entry = importlib.import_module(f"{package_name}.entry")
        graph = capture_runtime_call_graph(entry.run, module_prefixes=(package_name,))
        replay = capture_runtime_call_graph(entry.run, module_prefixes=(package_name,))
    finally:
        sys.path.remove(str(tmp_path))
        for module_name in tuple(sys.modules):
            if module_name == package_name or module_name.startswith(f"{package_name}."):
                del sys.modules[module_name]

    assert graph.edges
    assert any(
        edge.caller_module == f"{package_name}.entry"
        and edge.callee_module == f"{package_name}.worker"
        and edge.callee_function == "leaf"
        for edge in graph.edges
    )
    assert set(graph.loaded_modules) >= {f"{package_name}.entry", f"{package_name}.worker"}
    assert graph.to_dict()["observation_only"] is True
    assert graph.graph_digest == replay.graph_digest


def test_runtime_graph_requires_an_explicit_scope() -> None:
    with pytest.raises(ValueError, match="module_prefixes"):
        capture_runtime_call_graph(lambda: None, module_prefixes=())


def test_runtime_graph_includes_new_thread_edges_and_restores_thread_profiler(tmp_path: Path) -> None:
    package_name = f"runtime_thread_probe_{tmp_path.name.replace('-', '_')}"
    _probe_package(tmp_path, package_name)
    sys.path.insert(0, str(tmp_path))
    previous = sys.getprofile()
    previous_thread = threading.getprofile()
    try:
        entry = importlib.import_module(f"{package_name}.entry")
        graph = capture_runtime_call_graph(entry.run_threaded, module_prefixes=(package_name,))
    finally:
        sys.path.remove(str(tmp_path))
        for module_name in tuple(sys.modules):
            if module_name == package_name or module_name.startswith(f"{package_name}."):
                del sys.modules[module_name]

    assert any(
        edge.caller_module == f"{package_name}.entry"
        and edge.callee_module == f"{package_name}.worker"
        and edge.callee_function == "leaf"
        for edge in graph.edges
    )
    assert sys.getprofile() is previous
    assert threading.getprofile() is previous_thread


def test_runtime_graph_restores_previous_profiler_after_probe_failure() -> None:
    previous = sys.getprofile()

    def failing_probe() -> None:
        raise RuntimeError("probe failed")

    with pytest.raises(RuntimeError, match="probe failed"):
        capture_runtime_call_graph(failing_probe, module_prefixes=("test_",))
    assert sys.getprofile() is previous
