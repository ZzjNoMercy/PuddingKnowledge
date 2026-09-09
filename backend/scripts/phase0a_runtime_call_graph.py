"""Run explicit no-argument probes and emit observed runtime call edges."""

from __future__ import annotations

import argparse
import importlib
import json
from collections.abc import Callable

from knowledge_platform.baseline.runtime_graph import capture_runtime_call_graph


def _resolve_probe(reference: str) -> Callable[[], object]:
    module_name, separator, function_name = reference.partition(":")
    if not separator or not module_name or not function_name:
        raise ValueError(f"probe must use module:function syntax: {reference!r}")
    module = importlib.import_module(module_name)
    probe = getattr(module, function_name, None)
    if not callable(probe):
        raise ValueError(f"probe is not callable: {reference!r}")
    return probe


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="append", required=True, help="No-argument module:function probe; repeatable")
    parser.add_argument("--module-prefix", action="append", required=True, help="Module prefix to include; repeatable")
    args = parser.parse_args()
    graphs = [
        (reference, capture_runtime_call_graph(_resolve_probe(reference), module_prefixes=args.module_prefix))
        for reference in args.probe
    ]
    payload = {
        "format": "agent-native-knowledge-platform-runtime-call-graph-report/v1",
        "observation_only": True,
        "probes": [
            {
                **graph.to_dict(),
                "probe_reference": reference,
                "module_prefixes": sorted(set(args.module_prefix)),
            }
            for reference, graph in graphs
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
