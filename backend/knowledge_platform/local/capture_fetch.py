"""Bounded, cancellable process boundary for untrusted public web reads."""
from __future__ import annotations
import asyncio
import base64
import json
import sys


async def fetch(url, *, allowed_origins=()):
    from knowledge_platform.capture.http import MAX_RESPONSE_BYTES, PublicURLResponse
    process=await asyncio.create_subprocess_exec(sys.executable,'-m',__name__,stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout,_=await asyncio.wait_for(process.communicate(json.dumps({'url':url,'allowed_origins':list(allowed_origins)}).encode()),timeout=60)
        if process.returncode or len(stdout)>8*1024*1024:
            raise ValueError('Capture fetch failed')
        result=json.loads(stdout)
        if not isinstance(result,dict) or set(result)!={'url','content_type','body'} or any(not isinstance(value,str) for value in result.values()):
            raise ValueError('Invalid capture process response')
        body=base64.b64decode(result['body'],validate=True)
        if len(body)>MAX_RESPONSE_BYTES or len(result['url'])>8192:
            raise ValueError('Capture process response exceeds bounds')
        return PublicURLResponse(result['url'],result['content_type'],body)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def main():
    from knowledge_platform.capture.http import fetch_public_url
    try:
        payload=sys.stdin.buffer.read(65537)
        if len(payload)>65536: return 2
        request=json.loads(payload)
        response=fetch_public_url(request['url'],allowed_origins=request['allowed_origins'])
        print(json.dumps({'url':response.url,'content_type':response.content_type,'body':base64.b64encode(response.body).decode()}))
        return 0
    except Exception:
        return 2


if __name__=='__main__':
    raise SystemExit(main())
