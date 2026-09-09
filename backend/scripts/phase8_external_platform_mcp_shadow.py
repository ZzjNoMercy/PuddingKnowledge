"""Validate external Platform MCP discovery without network or local Tool activation."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from knowledge_contracts import CapabilityDescriptor, CapabilityInventory, Correlation, Principal
from knowledge_platform.agent import AgentCapabilitySurfaceBuilder
from knowledge_platform.transport.external_mcp import (
    ExternalPlatformMcpCapabilityDiscovery,
    discover_external_platform_mcp,
)

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"


def run_shadow(*, output_dir: Path = _DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    config = discover_external_platform_mcp(
        {
            "enabled": True,
            "endpoint": "https://platform.example.invalid/mcp",
            "auth_ref": "${PUDDINGKNOWLEDGE_MCP_TOKEN}",
        }
    )
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-external-platform-mcp-shadow/v1",
        "status": "PHASE8_EXTERNAL_PLATFORM_MCP_SHADOW_FAILED",
        "activation_allowed": False,
        "network_contacted": False,
        "local_tool_registration": False,
        "server_name": None,
        "tool_count": 0,
        "agent_surface_built": False,
        "agent_invocation": None,
    }
    if config is not None:
        descriptor = config.descriptor()
        async def load_inventory(_client_config, _principal):
            return CapabilityInventory(
                (CapabilityDescriptor("external/read", "platform", "read", True, True, ("knowledge",)),),
                issuer_id="platform-authz",
            )

        async def invoke_surface():
            surface = await AgentCapabilitySurfaceBuilder(
                ExternalPlatformMcpCapabilityDiscovery(
                    config,
                    inventory_loader=load_inventory,
                    resolve_auth=lambda _reference: "shadow-only-opaque-token",
                ),
                trusted_issuer_ids=frozenset({"platform-authz"}),
            ).build(
                principal=Principal("phase8-shadow"),
                correlation=Correlation("phase8-shadow-correlation"),
            )

            class ShadowInvoker:
                async def invoke(self, _capability, _arguments, *, principal, correlation):
                    return {
                        "status": "ok",
                        "principal_bound": principal.subject_id == "phase8-shadow",
                        "correlation_bound": correlation.trace_id == "phase8-shadow-correlation",
                    }

            return await surface.invoke(
                "external/read",
                {"resource_uri": "knowledge://spaces/space_kb_default/assets/asset-1"},
                invoker=ShadowInvoker(),
            )

        invocation = asyncio.run(invoke_surface())
        result.update(
            {
                "status": "PHASE8_EXTERNAL_PLATFORM_MCP_SHADOW_PASS_NOT_ACTIVATABLE",
                "local_tool_registration": descriptor["local_tool_registration"],
                "server_name": descriptor["server_name"],
                "tool_count": len(descriptor["tool_allowlist"]),
                "agent_surface_built": True,
                "agent_invocation": {
                    "status": invocation["status"],
                    "principal_bound": invocation["principal_bound"],
                    "correlation_bound": invocation["correlation_bound"],
                },
            }
        )
    report_path = output_dir / "phase8-external-platform-mcp-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run_shadow(output_dir=args.output_dir)
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
