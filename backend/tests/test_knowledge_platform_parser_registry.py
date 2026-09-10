import pytest, asyncio, httpx, json
from knowledge_platform.parsers.native import NativeParser
from knowledge_platform.parsers.registry import ParserRegistry, ParserRegistryError, ParserSpec, build_registry

def test_registry_priority_and_health_isolation():
    class Broken:
        def health(self): raise RuntimeError("down")
    class Good:
        def parse(self, filename, content): return NativeParser().parse("x.txt", b"ok")
    registry = ParserRegistry([ParserSpec("broken", Broken(), priority=100, extensions=("txt",)), ParserSpec("good", Good(), priority=1, extensions=("txt",))])
    assert asyncio.run(registry.parse("x.txt", b"ignored")).markdown == b"ok"
    assert [item.parser_id for item in registry.list_available()] == ["good"]

def test_registry_explicit_unsupported_and_size_rejected():
    registry = ParserRegistry([ParserSpec("native", NativeParser(), extensions=("md",), max_bytes=2)])
    with pytest.raises(ParserRegistryError): asyncio.run(registry.parse("x.pdf", b"%PDF"))
    with pytest.raises(ParserRegistryError): asyncio.run(registry.parse("x.md", b"123"))
    with pytest.raises(ParserRegistryError): asyncio.run(registry.parse("x.md", b"x", parser_id="missing"))

def test_build_registry_from_explicit_config():
    registry = build_registry([{"id": "native", "enabled": True, "priority": 10}, {"id": "mineru_local", "enabled": False, "priority": 1, "endpoint": "http://127.0.0.1:8000", "timeout": 60}])
    assert [spec.parser_id for spec in registry.list_available()] == ["native"]
    with pytest.raises(ParserRegistryError): build_registry([{"id": "native", "unknown": True}])
    with pytest.raises(ParserRegistryError): build_registry([{"id": "native", "extensions": "pdf"}])
    with pytest.raises(ParserRegistryError): build_registry([{"id": "mineru_local", "endpoint": "http://127.0.0.1:8000", "timeout": float("nan")}])

def test_registry_routes_mineru_adapter_to_async_parsed_document():
    from knowledge_platform.parsers.mineru import MinerUClient
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"markdown": "# parsed"})
    client = MinerUClient("http://127.0.0.1:8000", transport=httpx.MockTransport(handler))
    registry = ParserRegistry([ParserSpec("mineru_local", type("Adapter", (), {"health": lambda self: True, "parse": lambda self, filename, content: client.parse_pdf(content, filename)})(), extensions=("pdf",))])
    result = asyncio.run(registry.parse("x.pdf", b"%PDF"))
    assert result.markdown == b"# parsed" and result.version == "local-http-v1" and seen
