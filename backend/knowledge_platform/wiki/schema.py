"""Admit byte-bound Wiki schema inputs without reading or executing host content.

Expected commitments must come from the owning installation/migration authority,
not a model request. This module verifies consistency; it does not establish that
external authority or authorize publication by itself.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import re
import yaml
from pydantic import ValidationError
from .lint import WikiLintContract, _StrictLoader, LlmWikiError, _validate_inputs
from .schema_rules import (
    BrainSchemaDocument, BrainSchemaError, SchemaPackManifest, SchemaResolver,
    _dump_yaml, _model_dict, _sha256_text, workspace_page_prefixes,
)


@dataclass(frozen=True)
class AdmittedSchemaBundle:
    bundle_hash: str
    closure_sha256: str
    resolved_yaml: str
    bundle_version: str
    allowed_page_types: tuple[str, ...]
    required_frontmatter: tuple[str, ...]
    page_prefixes: tuple[tuple[str, tuple[str, ...]], ...]
    pack_digests: tuple[tuple[str, str], ...]

    def lint_contract(self) -> WikiLintContract:
        return WikiLintContract(self.bundle_version, self.bundle_hash,
            self.allowed_page_types, self.required_frontmatter, dict(self.page_prefixes))


def _mapping(raw: str, name: str, budget: list[int]) -> dict:
    try:
        value = yaml.load(raw, Loader=_StrictLoader)
    except (yaml.YAMLError, LlmWikiError, RecursionError) as error:
        raise BrainSchemaError(f'Invalid schema YAML: {name}') from error
    if not isinstance(value, dict):
        raise BrainSchemaError(f'Schema must be a mapping: {name}')
    # Bound model and recursive alias-graph work before model parsing.
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop(); count += 1; budget[0] -= 1
        if depth > 32 or count > 20000 or budget[0] < 0:
            raise BrainSchemaError('Schema structure budget exceeded')
        if isinstance(item, dict): pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list): pending.extend((v, depth + 1) for v in item)
    return value


def schema_closure_sha256(*, custom_yaml: str, brain_yaml: str,
                          agents_markdown: str, catalog_yaml: dict[str, str]) -> str:
    """Content commitment only; computing it does not admit or trust a bundle."""
    if not isinstance(catalog_yaml, dict) or len(catalog_yaml) > 64:
        raise BrainSchemaError('Schema catalog budget exceeded')
    if any(not isinstance(k, str) or len(k) > 160 or not re.fullmatch(r'[a-z0-9._-]+', k) for k in catalog_yaml):
        raise BrainSchemaError('Invalid catalog identity')
    texts = [custom_yaml, brain_yaml, agents_markdown, *catalog_yaml.values()]
    total = 0
    for raw in texts:
        if not isinstance(raw, str): raise BrainSchemaError('Schema input must be text')
        size = len(raw.encode('utf-8')); total += size
        if size > 1024 * 1024 or total > 8 * 1024 * 1024:
            raise BrainSchemaError('Schema byte budget exceeded')
    facts = {'format': 'puddingknowledge-schema-closure/v1',
             'custom': _sha256_text(custom_yaml), 'brain': _sha256_text(brain_yaml),
             'agents': _sha256_text(agents_markdown),
             'catalog': {k: _sha256_text(v) for k, v in catalog_yaml.items()}}
    return hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def admit_schema_bundle(*, custom_yaml: str, brain_yaml: str, agents_markdown: str,
                        catalog_yaml: dict[str, str], expected_bundle_hash: str,
                        expected_closure_sha256: str) -> AdmittedSchemaBundle:
    for digest in (expected_bundle_hash, expected_closure_sha256):
        if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
            raise BrainSchemaError('Invalid expected schema commitment')
    closure = schema_closure_sha256(custom_yaml=custom_yaml, brain_yaml=brain_yaml,
        agents_markdown=agents_markdown, catalog_yaml=catalog_yaml)
    if closure != expected_closure_sha256:
        raise BrainSchemaError('Schema closure commitment mismatch')
    try:
        budget = [100000]
        custom = SchemaPackManifest.model_validate(_mapping(custom_yaml, 'custom', budget))
        brain = BrainSchemaDocument.model_validate(_mapping(brain_yaml, 'brain', budget))
        manifests = {name: SchemaPackManifest.model_validate(_mapping(raw, name, budget))
                     for name, raw in catalog_yaml.items()}
        if custom.name in manifests or any(name != item.name for name, item in manifests.items()):
            raise BrainSchemaError('Catalog identity mismatch or custom shadowing')
        if any(len(p.page_types) > 256 or len(p.link_types) > 1000 for p in [custom, *manifests.values()]):
            raise BrainSchemaError('Schema declaration budget exceeded')
        # These names enter generated Markdown and downstream typed lookups.
        names = [*brain.wiki.allowed_page_types, *brain.wiki.allowed_link_types,
                 *brain.wiki.required_frontmatter]
        for pack in [custom, *manifests.values()]:
            names.extend(p.name for p in pack.page_types)
            names.extend(l.name for l in pack.link_types)
            for page in pack.page_types:
                if any(not workspace_page_prefixes({'path_prefixes': [raw]}) for raw in page.path_prefixes):
                    raise BrainSchemaError('Invalid empty Wiki schema path prefix')
                for prefix in workspace_page_prefixes(page):
                    if not re.fullmatch(r'[a-z0-9_-]+(?:/[a-z0-9_-]+)*/?', prefix):
                        raise BrainSchemaError('Invalid Wiki schema path prefix')
        if any(not re.fullmatch(r'[a-z][a-z0-9_-]{0,159}', name) for name in names):
            raise BrainSchemaError('Invalid Wiki schema declaration name')
        if any(any(c in value for c in '\n\r`') or len(value) > 160
               for value in (brain.schema_id, brain.bundle_version)):
            raise BrainSchemaError('Invalid Wiki schema identity')
        if len(set(brain.wiki.allowed_link_types)) != len(brain.wiki.allowed_link_types):
            raise BrainSchemaError('Duplicate allowed link types')
        resolver = SchemaResolver(manifests)
        resolved = resolver.resolve_manifest(custom)
        # Exact closure: ancestors and explicitly borrowed packs, no unrelated
        # installed defaults and no silently missing borrowed selections.
        required = set()
        parent = custom.extends
        while parent:
            required.add(parent)
            parent = manifests[parent].extends
        for spec in custom.borrow_from:
            required.add(spec.pack)
            target = manifests[spec.pack]
            if set(spec.types or []) - {p.name for p in target.page_types} or set(spec.link_types or []) - {l.name for l in target.link_types}:
                raise BrainSchemaError('Unknown borrowed schema selection')
        if required != set(manifests):
            raise BrainSchemaError('Schema catalog must be the exact resolution closure')
        custom_hash = _sha256_text(custom_yaml)
        ref = brain.gbrain_pack
        if (ref.name, ref.version, ref.manifest_sha256) != (custom.name, custom.version, custom_hash):
            raise BrainSchemaError('Brain custom pack identity or hash mismatch')
        if agents_markdown != resolver._render_agents(brain, resolved):
            raise BrainSchemaError('AGENTS projection mismatch')
        if set(brain.wiki.allowed_page_types) - {p.name for p in resolved.page_types} or set(brain.wiki.allowed_link_types) - {l.name for l in resolved.link_types}:
            raise BrainSchemaError('Brain contract references unknown resolved declarations')
        resolved_yaml = _dump_yaml(_model_dict(resolved))
        parent_hash = _sha256_text(catalog_yaml[custom.extends]) if custom.extends else 'none'
        bundle_hash = _sha256_text(f'{_sha256_text(brain_yaml)}:{parent_hash}:{custom_hash}:{_sha256_text(resolved_yaml)}:{_sha256_text(agents_markdown)}')
        if bundle_hash != expected_bundle_hash:
            raise BrainSchemaError('Legacy schema bundle commitment mismatch')
        result = AdmittedSchemaBundle(bundle_hash, closure, resolved_yaml, brain.bundle_version,
            tuple(brain.wiki.allowed_page_types), tuple(brain.wiki.required_frontmatter),
            tuple((p.name, tuple(workspace_page_prefixes(p))) for p in resolved.page_types),
            tuple(sorted((k, _sha256_text(v)) for k, v in catalog_yaml.items())))
        _validate_inputs(result.lint_contract(), {}, None, True, {}, None)
        return result
    except (ValidationError, ValueError, KeyError, RecursionError) as error:
        raise BrainSchemaError('Invalid schema bundle declarations') from error
