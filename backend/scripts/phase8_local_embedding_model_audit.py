"""Audit an explicitly supplied local embedding model without network access.

This is an inventory and offline-tokenizer probe only.  It never loads model
weights, emits source paths or model data, creates a collection, or authorizes
activation.  A complete artifact set is not sufficient for a provider: the
required local runtime must also be present.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_REQUIRED_FILES = (
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
)
_WEIGHT_FILES = ("model.safetensors", "model.safetensors.index.json")


def _regular_file(path: Path) -> bool:
    return not path.is_symlink() and path.is_file()


def _has_symlink_component(path: Path) -> bool:
    current = path
    while current != current.parent:
        if current.is_symlink():
            return True
        current = current.parent
    return False


def _prepare_output_dir(path: Path) -> Path:
    path = path.expanduser().absolute()
    if _has_symlink_component(path):
        raise ValueError("embedding model audit output directory must not be a symlink")
    if path.exists() and not path.is_dir():
        raise ValueError("embedding model audit output directory must be a directory")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _probe_tokenizer(model_dir: Path) -> tuple[bool, str | None]:
    try:
        from transformers import AutoTokenizer

        AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
    except Exception as error:  # noqa: BLE001 - report only the stable error type
        return False, type(error).__name__
    return True, None


def run_audit(
    *,
    model_dir: Path,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    probe_tokenizer: bool = False,
    required_runtime: str | None = None,
) -> dict[str, Any]:
    output_dir = _prepare_output_dir(output_dir)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-embedding-model-audit/v1",
        "status": "PHASE8_LOCAL_EMBEDDING_MODEL_NOT_RUNNABLE",
        "activation_allowed": False,
        "network_contacted": False,
        "source_paths_emitted": False,
        "model_weights_loaded": False,
        "model_output_emitted": False,
        "probe_tokenizer_requested": probe_tokenizer,
        "required_runtime": required_runtime,
        "provider_runtime": {
            "transformers": _module_available("transformers"),
            "torch": _module_available("torch"),
            "vllm": _module_available("vllm"),
        },
    }
    try:
        model_dir = model_dir.expanduser().absolute()
        if _has_symlink_component(model_dir) or not model_dir.is_dir():
            raise ValueError("embedding model directory must be a non-symlink directory")

        missing_files = [name for name in _REQUIRED_FILES if not _regular_file(model_dir / name)]
        if not any(_regular_file(model_dir / name) for name in _WEIGHT_FILES):
            missing_files.append("model weights")
        config: dict[str, Any] = {}
        config_path = model_dir / "config.json"
        if _regular_file(config_path):
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(raw_config, dict):
                config = raw_config
        architecture = config.get("architectures")
        architecture_names = [item for item in architecture if isinstance(item, str)] if isinstance(architecture, list) else []
        blockers: list[str] = []
        if missing_files:
            blockers.append("missing_local_model_artifacts")
        # Inventory is useful without loading optional provider runtimes. Only
        # an explicitly requested runtime is a readiness blocker; tokenizer
        # probing separately reports a missing transformers provider.
        if required_runtime and not result["provider_runtime"].get(required_runtime, False):
            blockers.append(f"{required_runtime}_runtime_required_but_unavailable")
        tokenizer_loadable: bool | None = None
        tokenizer_error_type: str | None = None
        if probe_tokenizer and not missing_files:
            tokenizer_loadable, tokenizer_error_type = _probe_tokenizer(model_dir)
            if not tokenizer_loadable:
                blockers.append("offline_tokenizer_probe_failed")
        result.update(
            {
                "artifact_file_count": sum(
                    _regular_file(model_dir / name) for name in (*_REQUIRED_FILES, *_WEIGHT_FILES)
                ),
                "missing_artifacts": missing_files,
                "architecture": architecture_names,
                "hidden_size": config.get("hidden_size") if isinstance(config.get("hidden_size"), int) else None,
                "tokenizer_loadable": tokenizer_loadable,
                "tokenizer_error_type": tokenizer_error_type,
                "provider_blockers": blockers,
                "runnable": not blockers,
            }
        )
        if not blockers:
            result["status"] = "PHASE8_LOCAL_EMBEDDING_MODEL_OFFLINE_PROVIDER_READY_NOT_ACTIVATABLE"
    except Exception as error:  # noqa: BLE001 - keep report bounded and path-free
        result["error_type"] = type(error).__name__

    report_path = output_dir / "phase8-local-embedding-model-audit-report.json"
    if report_path.is_symlink():
        raise ValueError("embedding model audit report must not be a symlink")
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--probe-tokenizer", action="store_true")
    parser.add_argument("--required-runtime", choices=("transformers", "vllm"))
    args = parser.parse_args()
    result = run_audit(
        model_dir=args.model_dir,
        output_dir=args.output_dir,
        probe_tokenizer=args.probe_tokenizer,
        required_runtime=args.required_runtime,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if "error_type" not in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
