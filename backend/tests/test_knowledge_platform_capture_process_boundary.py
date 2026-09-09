import asyncio
import base64
import json
import pytest
from knowledge_platform.local import capture_fetch
from knowledge_platform.capture.http import MAX_RESPONSE_BYTES


def test_parent_rejects_oversized_decoded_payload(monkeypatch):
    class Process:
        returncode=0
        async def communicate(self,data):
            return json.dumps({'url':'https://example.com','content_type':'text/plain','body':base64.b64encode(b'x'*(MAX_RESPONSE_BYTES+1)).decode()}).encode(),b''
    async def create(*args,**kwargs):return Process()
    monkeypatch.setattr(capture_fetch.asyncio,'create_subprocess_exec',create)
    with pytest.raises(ValueError,match='exceeds bounds'):
        asyncio.run(capture_fetch.fetch('https://example.com'))
