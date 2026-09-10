"""Authorized Package images through HTTP models and independent local/Milvus runtime."""
import base64
import hashlib
import json
import sqlite3
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from knowledge_platform.package.builder import KnowledgePackageBuilder, export_package_zip
from test_knowledge_platform_package_runtime import Runtime
from test_knowledge_platform_milvus_runtime import milvus

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7XcAAAAASUVORK5CYII=")


def package(root):
    space = "space_kb_default"
    assets = []; files = {}
    for aid, kind, mime, body in [("mm_doc", "document", "text/plain", b"Text archive note"), ("mm_img", "image", "image/png", PNG)]:
        path = root / aid; path.write_bytes(body); files[aid] = path
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        assets.append(dict(id=aid, space_id=space, kind=kind, title=aid, description="",
            mime_type=mime, source_type="package", source_uri=f"knowledge://spaces/{space}/assets/{aid}",
            revision=digest, content_digest=digest))
    caps = ["knowledge_read", "knowledge_search", "document_rag_query"]
    KnowledgePackageBuilder().build(output_dir=root / "package", package_id="mm", version="1",
        spaces=[dict(id=space, name="Space", description="")],
        collections=[dict(id="mm_collection", space_id=space, name="Images and text", version="1", kind="documents",
            asset_ids=list(files), semantic_asset_ids=[], capabilities=caps)],
        assets=assets, asset_files=files, capabilities=caps, catalog_revision="sha256:" + "0" * 64)
    archive = root / "package.zip"; export_package_zip(root / "package", archive)
    return archive


class Models:
    def __init__(self):
        self.calls = []; self.bad = False; self.revoke = None; owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls.append(payload)
                rows = []
                for i, item in enumerate(payload["input"]["contents"]):
                    if "image" in item:
                        assert base64.b64decode(item["image"].split(",", 1)[1]) == PNG
                    vector = [1., 0.] if "image" in item or item.get("text") == "find diagram" else [0., 1.]
                    rows.append(dict(index=i, embedding=vector))
                if owner.bad: rows = []
                if owner.revoke: owner.revoke()
                body = json.dumps(dict(output=dict(embeddings=rows))).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
    def image_calls(self):
        return sum("image" in item for call in self.calls for item in call["input"]["contents"])
    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()


@pytest.mark.parametrize("provider", ["local", "milvus"])
def test_multimodal_package_restart_evidence_and_revocation(tmp_path, request, provider):
    endpoint = request.getfixturevalue("milvus")[0] if provider == "milvus" else None
    archive = package(tmp_path)
    runtime = Runtime(tmp_path / "runtime", imports=[dict(id="mm", path=str(archive), digest="sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest())])
    models = Models()
    config = dict(version=1, provider_id="knowledge_" + ("milvus" if endpoint else "local") + "_vector",
        space_ids=["space_kb_default"], batch_size=16, max_chars=1200,
        embedding=dict(protocol="dashscope_multimodal", endpoint=f"http://127.0.0.1:{models.server.server_port}/embeddings",
            model="qwen3-vl-embedding", dimension=2, api_key_env=None))
    if endpoint: config["milvus"] = dict(endpoint=endpoint, api_key_env=None)
    config_path = runtime.root / "index.json"; config_path.write_text(json.dumps(config))
    runtime.extra_args = ["--index-config", str(config_path)]
    query = dict(query="find diagram", space_id="space_kb_default", collection_id="mm_collection", limit=1)
    rebuild = dict(space_id="space_kb_default", collection_id="mm_collection", collection_version="1",
        capability="document_rag_query", provider_id=config["provider_id"], idempotency_key="mm-once")
    try:
        runtime.start()
        imported = runtime.call("/v1/packages:import", dict(package_ref="mm", idempotency_key="import"))
        assert imported["status"] == "ok", imported
        indexed = runtime.call("/v1/indexes:rebuild", rebuild)
        assert indexed["status"] == "ok", indexed
        assert models.image_calls() == 1
        assert all(call["parameters"]["enable_fusion"] is False for call in models.calls)
        def evidence(result):
            assert result["status"] == "ok" and result["evidence"], result
            row = result["evidence"][0]
            assert row["resource_uri"].endswith("/mm_img") and row["quote"] == "", result
        evidence(runtime.call("/v1/knowledge/query", query))
        evidence(runtime.call("/v1/document-rag/query", {k:v for k,v in query.items() if k != "collection_id"}))
        result = runtime.call("/mcp", dict(jsonrpc="2.0", id=1, method="tools/call",
            params=dict(name="knowledge_query", arguments=query)))
        evidence(result["result"]["structuredContent"])
        exported = runtime.call("/v1/packages:export", dict(output_ref="output", package_id="mm-roundtrip", version="1",
            collections=[dict(id="mm_collection", version="1")]))
        assert exported["status"] == "ok", exported
        with zipfile.ZipFile(runtime.archive) as z:
            assert PNG in [z.read(name) for name in z.namelist() if not name.endswith("/")]
        runtime.stop(); archive.unlink(); runtime.start()
        replay = runtime.call("/v1/indexes:rebuild", rebuild)
        assert replay["status"] == "ok" and replay["data"]["index"]["idempotent"], replay
        evidence(runtime.call("/v1/knowledge/query", query)); assert models.image_calls() == 1
        db_path = runtime.root / "state/catalog.sqlite3"
        def active():
            with sqlite3.connect(db_path) as db:
                return db.execute("SELECT generation FROM knowledge_local_vector_indexes WHERE status='active'").fetchall()
        original = active(); models.bad = True
        failed = runtime.call("/v1/indexes:rebuild", {**rebuild, "idempotency_key":"bad"})
        assert failed["status"] == "error" and active() == original, failed
        models.bad = False; evidence(runtime.call("/v1/knowledge/query", query))
        def revoke():
            with sqlite3.connect(db_path) as db: db.execute("UPDATE knowledge_package_imports SET status='revoked'")
        models.revoke = revoke
        failed = runtime.call("/v1/indexes:rebuild", {**rebuild, "idempotency_key":"revoked"})
        assert failed["status"] == "error" and active() == original, failed
        models.revoke = None
        assert runtime.call("/v1/knowledge/query", query)["status"] == "error"
    finally:
        runtime.stop(); models.close()
