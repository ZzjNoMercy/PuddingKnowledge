"""Black-box interoperability between separately installed Knowledge and Harness.

Uses generated local fixtures and two subprocesses, never either source checkout
on PYTHONPATH. Does not activate production or contact non-loopback services.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen

FIXTURE = """
import sqlite3, sys
from pathlib import Path
from sqlalchemy import create_engine
import knowledge_platform
assert 'site-packages' in knowledge_platform.__file__, knowledge_platform.__file__
from knowledge_platform.catalog import migrate_to_latest
root=Path(sys.argv[1]); engine=create_engine(f"sqlite:///{root / 'catalog.sqlite3'}")
with engine.begin() as conn: migrate_to_latest(conn)
engine.dispose()
with sqlite3.connect(root / 'catalog.sqlite3') as conn:
 conn.execute("INSERT INTO knowledge_spaces VALUES ('space_kb_default','Local','','{}','now','now')")
 conn.execute("INSERT INTO knowledge_datasets VALUES ('dataset_kb_default','space_kb_default','Local','v1','document','','[]','[]','[]','{}','{}','','now','now')")
(root/'wiki').mkdir(); (root/'wiki/page.md').write_text('# Black-box fixture\\n\\nBLACKBOX_RESOURCE_EVIDENCE\\n')
"""
CLIENT = """
import asyncio, sys
from pathlib import Path
import config
assert 'site-packages' in config.__file__, config.__file__
from tools.read_resource_tool import create_read_resource_tool, bind_mcp_resource_reader
config.save_config({'mcp':{'enabled':['platform'],'servers':{'platform':{
 'transport':'streamable-http','url':sys.argv[1]+'/mcp'}}}})
tool=bind_mcp_resource_reader(create_read_resource_tool(Path.cwd()),['platform'])
async def run():
 async def call(uri, server='platform'):
  return await tool.ainvoke({'type':'tool_call','id':'resource1','name':'read_resource','args':{'resource':uri,'mcp_server':server}})
 if len(sys.argv)>3 and sys.argv[3]=='disconnected':
  bad=await call(sys.argv[2]); assert bad.status=='error',bad
  print('DISCONNECTED_SERVER_REJECTED'); return
 good=await call(sys.argv[2]); assert good.status=='success', good
 assert 'BLACKBOX_RESOURCE_EVIDENCE' in str(good.content), str(good.content)[:1000]
 for uri,server in [(sys.argv[2],'disabled'),(sys.argv[2].replace('space_kb_default','other-space'),'platform'),('knowledge://spaces/space_kb_default/assets/missing','platform')]:
  bad=await call(uri,server); assert bad.status=='error',bad
 print('RESOURCE_READ_AND_DENIALS_PASSED')
asyncio.run(run())
"""


def _assert_port_closed(port: int) -> None:
    """Prove the owned local server released its listener before the next probe."""

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                pass
        except OSError:
            return
        time.sleep(0.05)
    raise RuntimeError(f"owned local server still listens on port {port}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-python", type=Path, required=True)
    parser.add_argument("--harness-python", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="knowledge-harness-mcp-") as directory:
        root = Path(directory).resolve()
        env = {"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"}
        subprocess.run(
            [str(args.knowledge_python), "-c", FIXTURE, str(root)],
            env=env,
            cwd=root,
            check=True,
        )
        before = hashlib.sha256((root / "catalog.sqlite3").read_bytes()).hexdigest()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        with (root / "server.log").open("w+") as log:
            proc = subprocess.Popen(
                [
                    str(args.knowledge_python),
                    "-m",
                    "knowledge_platform.local",
                    "--catalog",
                    str(root / "catalog.sqlite3"),
                    "--wiki-root",
                    str(root / "wiki"),
                    "--temp-dir",
                    str(root / "workspace"),
                    "--ready-file",
                    str(root / "ready.json"),
                    "--port",
                    str(port),
                ],
                env=env,
                cwd=root,
                stdout=log,
                stderr=log,
            )
            try:
                for _ in range(150):
                    if proc.poll() is not None:
                        log.seek(0)
                        raise RuntimeError(log.read()[-3000:])
                    try:
                        with urlopen(
                            origin + "/v1/assets?space_id=space_kb_default", timeout=0.2
                        ) as response:
                            assets = json.load(response)["data"]["assets"]
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise TimeoutError("local Platform startup")
                uri = f"knowledge://spaces/space_kb_default/assets/{assets[0]['id']}"
                client_env = {
                    **env,
                    "PUDDINGHARNESS_HOME": str(root / "harness"),
                    "PUDDINGCLAW_HOME": str(root / "legacy"),
                }
                result = subprocess.run(
                    [str(args.harness_python), "-c", CLIENT, origin, uri],
                    cwd=root,
                    env=client_env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode:
                    raise RuntimeError((result.stdout + result.stderr)[-4000:])
                proc.terminate()
                proc.wait(timeout=10)
                _assert_port_closed(port)
                disconnected = subprocess.run(
                    [
                        str(args.harness_python),
                        "-c",
                        CLIENT,
                        origin,
                        uri,
                        "disconnected",
                    ],
                    cwd=root,
                    env=client_env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if disconnected.returncode:
                    raise RuntimeError(
                        (disconnected.stdout + disconnected.stderr)[-4000:]
                    )
                assert not (root / "legacy").exists()
                assert (
                    hashlib.sha256((root / "catalog.sqlite3").read_bytes()).hexdigest()
                    == before
                )
                print(
                    json.dumps(
                        {
                            "status": "passed",
                            "separate_installed_processes": True,
                            "generated_fixture": True,
                            "disconnected_server_rejected": True,
                            "source_catalog_unchanged": True,
                            "production_activation_allowed": False,
                        }
                    )
                )
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    main()
