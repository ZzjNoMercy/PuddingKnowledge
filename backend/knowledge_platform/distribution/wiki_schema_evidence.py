"""Capture offline schema resources and bind them to verified Wiki archive bytes.

This is an offline preparation operation. It does not fence live installation
writers or grant activation. Catalog paths are explicit operator inputs.
"""
from __future__ import annotations
import argparse
import hashlib
import os
from pathlib import Path
from . import wiki_archive as archive
from ..wiki.schema import admit_schema_bundle, schema_closure_sha256

FORMAT = 'puddingknowledge-wiki-schema-evidence/v1'
CUSTOM = 'schema/gbrain/puddingclaw-wiki/pack.yaml'
BRAIN = 'schema/brain.schema.yaml'
MAX_EVIDENCE = 16 * 1024 * 1024


def _texts(evidence, manifest):
    result = {}
    for key, relative in (('custom_yaml', CUSTOM), ('brain_yaml', BRAIN), ('agents_markdown', 'AGENTS.md')):
        raw, fact = archive._read(evidence / 'archive' / relative, private=True, limit=1024*1024)
        if manifest['files'].get(relative) != fact:
            raise ValueError('Schema file is not bound to archive inventory')
        result[key] = raw.decode('utf-8')
    return result


def verify_schema_evidence(data: bytes, evidence: Path, *, expected_digest: str | None = None):
    if len(data) > MAX_EVIDENCE: raise ValueError('Schema evidence exceeds budget')
    if expected_digest is not None and hashlib.sha256(data).hexdigest() != expected_digest:
        raise ValueError('Owned schema evidence digest mismatch')
    value = archive._json(data)
    required = {'format','archive_manifest_digest','installation_id','source_revision','inputs',
                'bundle_hash','closure_sha256','activation_allowed'}
    if not isinstance(value,dict) or set(value) != required or value['format'] != FORMAT or value['activation_allowed'] is not False:
        raise ValueError('Invalid schema evidence envelope')
    with archive._lock(evidence, shared=True):
        manifest = archive._verify(evidence)
        raw = archive._read(evidence/'manifest.json',private=True,limit=archive.MAX_JSON)[0]
        if value['archive_manifest_digest'] != hashlib.sha256(raw).hexdigest() or any(value[k] != manifest[k] for k in ('installation_id','source_revision')):
            raise ValueError('Schema evidence belongs to another Wiki archive')
        inputs = value['inputs']
        if not isinstance(inputs,dict) or set(inputs) != {'custom_yaml','brain_yaml','agents_markdown','catalog_yaml'}:
            raise ValueError('Invalid schema evidence inputs')
        if any(inputs[k] != v for k,v in _texts(evidence,manifest).items()):
            raise ValueError('Schema evidence disagrees with archived bytes')
        result = admit_schema_bundle(**inputs,expected_bundle_hash=value['bundle_hash'],expected_closure_sha256=value['closure_sha256'])
        if archive._verify(evidence) != manifest: raise ValueError('Archive changed during schema verification')
    return result


def capture_schema_evidence(evidence: Path | str, catalog_files: dict[str, Path | str], *, expected_bundle_hash: str) -> bytes:
    evidence = archive._path(evidence)
    if not isinstance(catalog_files,dict) or len(catalog_files)>64: raise ValueError('Invalid schema catalog')
    # Name checks happen before any model-provided identifier can become a path.
    schema_closure_sha256(custom_yaml='',brain_yaml='',agents_markdown='',catalog_yaml={k:'' for k in catalog_files})
    catalog = {}; facts = {}; paths = {}; total = 0
    for name, path in catalog_files.items():
        path = archive._path(path); raw,fact = archive._read(path,limit=1024*1024)
        total += len(raw)
        if total > 8*1024*1024: raise ValueError('Schema catalog exceeds total byte budget')
        paths[name] = path; facts[name] = fact; catalog[name] = raw.decode('utf-8')
    with archive._lock(evidence, shared=True):
        manifest = archive._verify(evidence)
        manifest_bytes = archive._read(evidence/'manifest.json',private=True,limit=archive.MAX_JSON)[0]
        inputs = _texts(evidence,manifest)
        inputs['catalog_yaml'] = catalog
        closure = schema_closure_sha256(**inputs)
        result = admit_schema_bundle(**inputs,expected_bundle_hash=expected_bundle_hash,expected_closure_sha256=closure)
        value = {'format':FORMAT,'archive_manifest_digest':hashlib.sha256(manifest_bytes).hexdigest(),
            'installation_id':manifest['installation_id'],'source_revision':manifest['source_revision'],
            'inputs':inputs,'bundle_hash':result.bundle_hash,'closure_sha256':closure,'activation_allowed':False}
        data = archive._encode(value)
        if len(data)>MAX_EVIDENCE: raise ValueError('Schema evidence exceeds budget')
        if archive._verify(evidence) != manifest: raise ValueError('Archive changed during schema capture')
    for name,path in paths.items():
        if archive._read(path,limit=1024*1024)[1] != facts[name]:
            raise ValueError('External schema resource changed during capture')
    return data


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wiki-archive',required=True)
    parser.add_argument('--pack',action='append',default=[],metavar='NAME=ABSOLUTE_PATH')
    parser.add_argument('--expected-bundle-hash',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    packs={}
    for item in args.pack:
        name,separator,path=item.partition('=')
        if not separator or name in packs: parser.error('Each pack must be a unique NAME=ABSOLUTE_PATH')
        packs[name]=path
    data=capture_schema_evidence(args.wiki_archive,packs,expected_bundle_hash=args.expected_bundle_hash)
    output=archive._path(args.output)
    # Existing exact output is replayable; different existing bytes reject.
    with archive._directory(output.parent) as parent:
        archive._check(os.fstat(parent),directory=True,private=True)
        archive._publish(output,data)
    verify_schema_evidence(archive._read(output,private=True,limit=MAX_EVIDENCE)[0],Path(args.wiki_archive))


if __name__=='__main__': main()
