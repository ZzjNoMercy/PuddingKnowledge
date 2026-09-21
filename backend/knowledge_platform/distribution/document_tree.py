"""Preserve verified source-relative document dependencies for independent use."""
from pathlib import Path
from . import wiki_archive as files
from .document_dependencies import collect_document_dependencies
from .document_attachment_metadata import collect_attachment_references, _validate_claimed_facts, _replace_metadata


def collect_tree(root, rows, body_bindings, original_bindings, attachment_bindings, virtual_roots=()):
    """Read actual snapshot bytes; metadata declarations alone do not verify files."""
    root=files._path(root)
    references=collect_attachment_references([{'metadata_json':row.get('doc_metadata') or {}} for row in rows])
    if attachment_bindings is not None and not isinstance(attachment_bindings,dict): raise ValueError('Attachment bindings must be an object')
    bindings={} if attachment_bindings is None else dict(attachment_bindings)
    for row in rows:
        if row['id'] in original_bindings:
            ref=row['doc_metadata']['original_path'];relative=original_bindings[row['id']]
            if ref in bindings and bindings[ref]!=relative:raise ValueError('Original and attachment bindings disagree')
            bindings[ref]=relative
    if set(bindings)!=set(references):raise ValueError('Every attachment requires an exact snapshot binding')
    primary={};directories=set();attachment_facts={};verified={}
    for row in rows:
        relative=body_bindings[row['id']];files._relative(relative)
        if Path(relative).as_posix()!=relative or '\\' in relative:raise ValueError('Noncanonical body binding')
        mime=row.get('mime_type')
        if relative in primary and primary[relative]!=mime:raise ValueError('Conflicting body MIME types')
        primary[relative]=mime
    for ref,kind in sorted(references.items()):
        relative=bindings[ref];files._relative(relative)
        if Path(relative).as_posix()!=relative or '\\' in relative:raise ValueError('Noncanonical attachment binding')
        if kind=='file':
            _,fact=files._read(root/relative);attachment_facts[ref]={'relative_path':relative,'kind':kind,**fact}
            verified[ref]={'kind':kind,'output_path':str(root/relative),**fact};primary.setdefault(relative,None)
        else:
            inventory=files._inventory(root/relative)
            attachment_facts[ref]={'relative_path':relative,'kind':kind,'inventory':inventory}
            verified[ref]={'kind':kind,'output_path':str(root/relative)}
            directories.add(relative);directories.update(str(Path(relative)/name) for name in inventory['directories'])
            for name in inventory['files']:primary.setdefault(str(Path(relative)/name),None)
    graph=collect_document_dependencies(root,primary,virtual_roots=virtual_roots)
    for fact in attachment_facts.values():
        relative=fact['relative_path']
        claimed={relative:{key:fact[key] for key in ('sha256','size_bytes')}} if fact['kind']=='file' else {str(Path(relative)/name):value for name,value in fact['inventory']['files'].items()}
        if any(graph['files'].get(name)!=value for name,value in claimed.items()):raise ValueError('Attachment changed during dependency inspection')
    for row in rows:
        metadata=row.get('doc_metadata') or {}
        _validate_claimed_facts(metadata,verified)
        _replace_metadata(metadata,verified,require_all=True)  # Validate directory/file topology without changing source metadata.
    for name in [*graph['files'],*directories]:
        parent=Path(name).parent
        while parent!=Path('.'):directories.add(parent.as_posix());parent=parent.parent
    return {'format':'knowledge-document-tree/v1','files':graph['files'],'directories':sorted(directories),'graph':graph,'attachments':attachment_facts}


def verify_tree_source(root,tree):
    for name,fact in tree['files'].items():
        if files._read(Path(root)/name)[1]!=fact:raise ValueError('Source document dependency changed')
    for fact in tree['attachments'].values():
        if fact['kind']=='directory' and files._inventory(Path(root)/fact['relative_path'])!=fact['inventory']:
            raise ValueError('Source attachment directory changed')


def validate_owned_tree(root,tree,body_paths,blob_bindings):
    """Validate a committed copied tree and bind its primary files to Catalog blobs."""
    if not isinstance(tree,dict) or set(tree)!={'format','files','directories','graph','attachments'} or tree['format']!='knowledge-document-tree/v1':raise ValueError('Invalid document tree')
    if not isinstance(body_paths,dict) or set(body_paths)!=set(blob_bindings):raise ValueError('Invalid document tree identities')
    actual=files._inventory(Path(root)/'resources',private=True)
    if actual!={'files':tree['files'],'directories':tree['directories']}:raise ValueError('Document tree inventory differs')
    for identity,relative in body_paths.items():
        files._relative(relative)
        if relative not in tree['files'] or 'blobs/'+tree['files'][relative]['sha256']!=blob_bindings[identity]:raise ValueError('Document tree body differs from Catalog blob')
    return {identity:Path(root)/'resources'/relative for identity,relative in body_paths.items()}
