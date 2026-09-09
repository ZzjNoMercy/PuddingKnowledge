"""Capture observed Python runtime call edges for Phase 0A probes.

This is deliberately an observation primitive, not a static-analysis
replacement. A caller must provide a no-argument probe and explicit module
prefixes. Only edges that actually execute during that probe are emitted;
unexecuted branches remain absent and therefore cannot be mistaken for
verified coverage.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass


def _module_in_scope(module_name: str, prefixes: tuple[str, ...]) -> bool:
    return any(module_name == prefix or module_name.startswith(f"{prefix}.") for prefix in prefixes)


@dataclass(frozen=True, slots=True, order=True)
class RuntimeCallEdge:
    caller_module: str
    caller_function: str
    callee_module: str
    callee_function: str


@dataclass(frozen=True, slots=True)
class RuntimeCallGraph:
    """Stable, path-free output of one observed probe execution.

    ``loaded_modules`` is an import-state diagnostic: it lists in-scope
    modules present in ``sys.modules`` after the probe. It is not execution
    coverage. Only ``edges`` are evidence of observed cross-module calls.
    """

    edges: tuple[RuntimeCallEdge, ...]
    loaded_modules: tuple[str, ...]

    @property
    def graph_digest(self) -> str:
        payload = {
            "edges": [
                {
                    "caller_module": edge.caller_module,
                    "caller_function": edge.caller_function,
                    "callee_module": edge.callee_module,
                    "callee_function": edge.callee_function,
                }
                for edge in self.edges
            ],
            "loaded_modules": list(self.loaded_modules),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def to_dict(self) -> dict[str, object]:
        return {
            "format": "agent-knowledge-platform-runtime-call-graph/v1",
            "observation_only": True,
            "graph_digest": self.graph_digest,
            "loaded_modules": list(self.loaded_modules),
            "edges": [
                {
                    "caller_module": edge.caller_module,
                    "caller_function": edge.caller_function,
                    "callee_module": edge.callee_module,
                    "callee_function": edge.callee_function,
                }
                for edge in self.edges
            ],
        }


def capture_runtime_call_graph(
    probe: Callable[[], object],
    *,
    module_prefixes: Iterable[str],
) -> RuntimeCallGraph:
    """Execute ``probe`` once and return its observed cross-module call graph."""

    prefixes = tuple(sorted({prefix.strip() for prefix in module_prefixes if prefix.strip()}))
    if not prefixes:
        raise ValueError("module_prefixes must contain at least one non-empty module prefix")
    observed_edges: set[RuntimeCallEdge] = set()

    def profiler(frame: object, event: str, arg: object) -> Callable[..., object] | None:
        del arg
        if event != "call":
            return profiler
        current = frame
        current_globals = getattr(current, "f_globals", {})
        caller = getattr(current, "f_back", None)
        if not isinstance(current_globals, dict) or caller is None:
            return profiler
        caller_globals = getattr(caller, "f_globals", {})
        caller_module = caller_globals.get("__name__")
        callee_module = current_globals.get("__name__")
        if not isinstance(caller_module, str) or not isinstance(callee_module, str):
            return profiler
        if caller_module == callee_module or not (
            _module_in_scope(caller_module, prefixes) and _module_in_scope(callee_module, prefixes)
        ):
            return profiler
        caller_code = getattr(caller, "f_code", None)
        current_code = getattr(current, "f_code", None)
        caller_name = getattr(caller_code, "co_name", None)
        callee_name = getattr(current_code, "co_name", None)
        if isinstance(caller_name, str) and isinstance(callee_name, str):
            observed_edges.add(RuntimeCallEdge(caller_module, caller_name, callee_module, callee_name))
        return profiler

    previous_profiler = sys.getprofile()
    previous_thread_profiler = threading.getprofile()
    sys.setprofile(profiler)
    threading.setprofile(profiler)
    try:
        probe()
    finally:
        sys.setprofile(previous_profiler)
        threading.setprofile(previous_thread_profiler)

    loaded_modules = tuple(sorted(module_name for module_name in sys.modules if _module_in_scope(module_name, prefixes)))
    return RuntimeCallGraph(tuple(sorted(observed_edges)), loaded_modules)
