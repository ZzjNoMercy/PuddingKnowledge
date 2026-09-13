"""Plan an entire Wiki authoring patch before any persistent mutation."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import re
from .lint import lint_workspace, _frontmatter, _validate_inputs
from .schema import AdmittedSchemaBundle


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def canonical(value) -> str:
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


@dataclass(frozen=True)
class PageChange:
    slug: str
    markdown: str | None
    expected_digest: str | None
    replacement: str | None = None


@dataclass(frozen=True)
class WikiPatch:
    expected_revision: str
    changes: tuple[PageChange, ...]
    selected_raw: tuple[str, ...]
    index: str
    log_entry: str


@dataclass(frozen=True)
class PlannedWikiPatch:
    previous_revision: str
    revision: str
    pages: tuple[tuple[str,str], ...]
    index: str
    log: str
    retired: tuple[tuple[str,str,str|None], ...]
    request_digest: str


def workspace_revision(bundle: AdmittedSchemaBundle, pages: dict[str,str], index: str, log: str, raw_hashes: dict[str,str], raw_manifest_sha256: str) -> str:
    _validate_inputs(bundle.lint_contract(),pages,index,True,raw_hashes,raw_manifest_sha256)
    if not isinstance(log,str) or len(log.encode())>8*1024*1024:raise ValueError('Wiki log exceeds budget')
    return digest(canonical({'schema':bundle.bundle_hash,'closure':bundle.closure_sha256,
        'pages':{slug:digest(text) for slug,text in pages.items()},'index':digest(index),'log':digest(log),
        'raw':raw_hashes,'raw_manifest':raw_manifest_sha256}))


def validate_patch_shape(patch):
    if not isinstance(patch,WikiPatch) or not isinstance(patch.changes,tuple) or not 1<=len(patch.changes)<=100:
        raise ValueError('Invalid Wiki patch shape')
    if not isinstance(patch.expected_revision,str) or not re.fullmatch('[a-f0-9]{64}',patch.expected_revision):raise ValueError('Invalid workspace revision')
    if not isinstance(patch.selected_raw,tuple) or len(patch.selected_raw)>100 or any(not isinstance(p,str) or len(p)>1024 for p in patch.selected_raw):raise ValueError('Invalid selected Raw inventory')
    if not isinstance(patch.index,str) or len(patch.index.encode())>8*1024*1024 or not isinstance(patch.log_entry,str) or len(patch.log_entry.encode())>65536:raise ValueError('Wiki patch text budget exceeded')
    size=0
    for change in patch.changes:
        if not isinstance(change,PageChange) or not isinstance(change.slug,str) or len(change.slug)>1024:raise ValueError('Invalid page change')
        if change.expected_digest is not None and (not isinstance(change.expected_digest,str) or not re.fullmatch('[a-f0-9]{64}',change.expected_digest)):raise ValueError('Invalid page precondition')
        if change.replacement is not None and (not isinstance(change.replacement,str) or len(change.replacement)>1024):raise ValueError('Invalid replacement')
        if change.markdown is not None:
            if not isinstance(change.markdown,str):raise ValueError('Invalid page text')
            size+=len(change.markdown.encode())
            if size>16*1024*1024 or len(change.markdown.encode())>8*1024*1024:raise ValueError('Wiki patch byte budget exceeded')


def plan_patch(*, bundle: AdmittedSchemaBundle, pages: dict[str,str], index: str, log: str,
               raw_hashes: dict[str,str], raw_manifest_sha256: str, patch: WikiPatch) -> PlannedWikiPatch:
    validate_patch_shape(patch)
    before=workspace_revision(bundle,pages,index,log,raw_hashes,raw_manifest_sha256)
    if not isinstance(patch,WikiPatch) or patch.expected_revision!=before:raise ValueError('Stale Wiki workspace revision')
    if not isinstance(patch.changes,tuple) or not 1<=len(patch.changes)<=100:raise ValueError('Wiki patch change count invalid')
    if not isinstance(patch.selected_raw,tuple) or len(patch.selected_raw)>100 or len(set(patch.selected_raw))!=len(patch.selected_raw) or any(p not in raw_hashes for p in patch.selected_raw):raise ValueError('Invalid selected Raw inventory')
    if not isinstance(patch.log_entry,str) or not patch.log_entry.strip() or len(patch.log_entry.encode())>65536:raise ValueError('Invalid append-only log entry')
    changed=set();retired=[];after=dict(pages);facts=[];size=0
    for change in patch.changes:
        if not isinstance(change,PageChange) or not isinstance(change.slug,str) or len(change.slug)>1024 or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)+',change.slug) or change.slug.startswith('wiki/') or change.slug in changed:raise ValueError('Invalid or duplicate Wiki patch slug')
        changed.add(change.slug)
        expected=digest(pages[change.slug]) if change.slug in pages else None
        if change.expected_digest!=expected:raise ValueError('Wiki page precondition mismatch')
        if change.markdown is None:
            if expected is None:raise ValueError('Cannot retire a missing Wiki page')
            if change.replacement is not None and (not isinstance(change.replacement,str) or change.replacement==change.slug):raise ValueError('Invalid retirement replacement')
            retired.append((change.slug,pages[change.slug],change.replacement));del after[change.slug]
        else:
            if not isinstance(change.markdown,str) or change.replacement is not None:raise ValueError('Invalid page mutation')
            size+=len(change.markdown.encode())
            if size>16*1024*1024:raise ValueError('Wiki patch byte budget exceeded')
            front,_=_frontmatter(change.markdown,source=change.slug)
            sources=front.get('sources')
            if not isinstance(sources,list) or not set(patch.selected_raw).intersection(str(v) for v in sources):raise ValueError('Every written page must cite selected Raw')
            after[change.slug]=change.markdown
        facts.append({'slug':change.slug,'markdown':change.markdown,'expected_digest':change.expected_digest,'replacement':change.replacement})
    for slug,_,replacement in retired:
        if replacement is not None and replacement not in after:raise ValueError('Retirement replacement must survive in final workspace')
    report=lint_workspace(contract=bundle.lint_contract(),pages=after,index=patch.index,log_present=True,raw_hashes=raw_hashes,raw_manifest_sha256=raw_manifest_sha256)
    if not report['ok']:raise ValueError('Wiki patch lint failed: '+','.join(sorted({e['code'] for e in report['errors']})))
    new_log=log+('' if not log or log.endswith('\n') else '\n')+patch.log_entry+('' if patch.log_entry.endswith('\n') else '\n')
    revision=workspace_revision(bundle,after,patch.index,new_log,raw_hashes,raw_manifest_sha256)
    request_digest=digest(canonical({'revision':before,'changes':facts,'selected_raw':patch.selected_raw,'index':patch.index,'log_entry':patch.log_entry}))
    return PlannedWikiPatch(before,revision,tuple(sorted(after.items())),patch.index,new_log,tuple(retired),request_digest)
