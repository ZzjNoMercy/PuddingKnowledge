"""Stage immutable media and parser outputs before the parent's fenced publication."""
from dataclasses import dataclass, replace
import hashlib
import json
from urllib.parse import quote

from knowledge_platform.catalog.models import KnowledgeAsset


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class StagedAsset:
    id: str
    uri: str
    digest: str
    kind: str
    mime: str
    title: str
    metadata: dict


def _stage(objects, space, item, revision, key, content, kind, mime, title, **metadata):
    digest = objects.put(content)
    identity = 'feishu_media_' + _hash('\0'.join((item, revision, key, digest)))[:48]
    return StagedAsset(identity, f'knowledge://spaces/{space}/assets/{identity}', digest,
        kind, mime, title, {'source_item_id': item, 'remote_revision': revision, **metadata})


async def stage_document(objects, *, space, item, document, parser_config=None):
    """No Catalog writes or provider URLs survive this operation."""
    staged = []
    markdown = document.markdown.decode('utf-8')
    media = getattr(document, 'media', ())
    if len(getattr(document,'attachments',())) > len(media):
        raise ValueError('Document attachments have not been materialized')
    if len(media) > 128 or sum(len(m.content) for m in media) > 32 * 1024 * 1024:
        raise ValueError('Feishu media exceeds document budget')
    paths = set()
    derived_bytes=0;derived_files=0
    for attachment in media:
        path = attachment.relative_path
        if path in paths:
            raise ValueError('Duplicate attachment path')
        paths.add(path)
        binding_key=path+'\0'+_hash(json.dumps(parser_config,sort_keys=True))
        original = _stage(objects, space, item, document.revision, binding_key, attachment.content,
            'attachment', attachment.mime_type, attachment.filename)
        if attachment.mime_type.split(';')[0] == 'application/pdf' or attachment.filename.lower().endswith('.pdf'):
            if parser_config:
                from knowledge_platform.parsers.mineru import MinerUClient, ParseLimits
                parsed = await MinerUClient(parser_config['endpoint'], timeout=parser_config.get('timeout', 60),
                    limits=ParseLimits(max_file_bytes=8*1024*1024)).parse_pdf(attachment.content, attachment.filename)
                derived_bytes+=len(parsed.markdown)+sum(len(x.content) for x in parsed.assets)
                derived_files+=1+len(parsed.assets)
                if derived_bytes>64*1024*1024 or derived_files>512:
                    raise ValueError('Document derived media exceeds publication budget')
                # A provider upgrade may change output under the same configured endpoint.
                # Bind this publication to the actual output, not only its input/config.
                output_fingerprint=_hash(json.dumps([parsed.parser_id,parsed.version,
                    hashlib.sha256(parsed.markdown).hexdigest(),
                    sorted((x.relative_path,x.mime_type,hashlib.sha256(x.content).hexdigest()) for x in parsed.assets)],sort_keys=True))
                binding_key+='\0'+output_fingerprint
                original=_stage(objects,space,item,document.revision,binding_key,attachment.content,
                    'attachment',attachment.mime_type,attachment.filename)
                images = []
                replacements={}
                for image in parsed.assets:
                    child = _stage(objects, space, item, document.revision, binding_key + '/' + image.relative_path,
                        image.content, 'derived_media', image.mime_type, image.relative_path, original_asset_id=original.id)
                    images.append(child)
                    replacements[image.relative_path]=child.uri
                from knowledge_platform.parsers.mineru import rewrite_media
                parsed_markdown = rewrite_media(parsed.markdown, replacements).decode('utf-8')
                derivative = _stage(objects, space, item, document.revision, binding_key + '/normalized_markdown',
                    parsed_markdown.encode(), 'parsed_document', 'text/markdown', attachment.filename,
                    original_asset_id=original.id, parser_id=parsed.parser_id, parser_version=parsed.version)
                original = replace(original, metadata={**original.metadata, 'derivatives': {'normalized_markdown': derivative.id}})
                staged.extend(images)
                staged.append(derivative)
        staged.append(original)
        markdown = markdown.replace('(./' + path + ')', '(' + original.uri + ')')
        markdown = markdown.replace('(./' + quote(path, safe='/') + ')', '(' + original.uri + ')')
        markdown = markdown.replace('(' + path + ')', '(' + original.uri + ')')
        markdown = markdown.replace('(' + quote(path, safe='/') + ')', '(' + original.uri + ')')
    return replace(document, markdown=markdown.encode()), tuple(staged)


def publish_media(session, staged):
    for row in staged:
        old = session.get(KnowledgeAsset, row.id)
        if old is None:
            session.add(KnowledgeAsset(id=row.id, space_id=row.uri.split('/')[3], kind=row.kind, title=row.title,
                source_type='feishu', source_uri=row.uri, mime_type=row.mime, revision=row.digest,
                content_digest=row.digest, metadata_json=row.metadata))
        elif (old.source_uri, old.content_digest, old.metadata_json, old.kind, old.mime_type) != (row.uri, row.digest, row.metadata, row.kind, row.mime):
            raise ValueError('Media Asset identity collision')


async def stage_drive(objects, *, space, item, document, parser_config=None):
    from knowledge_platform.connector_sync.feishu_source import FeishuDocument, FeishuMedia
    revision=objects.put(document.raw)
    document=replace(document,revision=revision)
    pdf=document.title.lower().endswith('.pdf')
    mime='application/pdf' if pdf else 'text/plain' if document.title.lower().endswith('.txt') else 'text/markdown'
    media=FeishuMedia('drive', 'drive', document.title, document.raw, mime, 'assets/original')
    envelope=FeishuDocument(revision,document.title,document.raw,b'[Original](assets/original)',(),(),(media,))
    _, staged=await stage_document(objects,space=space,item=item,document=envelope,parser_config=parser_config)
    original=next(a for a in staged if a.kind=='attachment')
    original=replace(original,kind='original_file')
    staged=[a for a in staged if a.id!=original.id]
    if not pdf:
        derivative=_stage(objects,space,item,revision,original.id+'/normalized_markdown',document.markdown,
            'parsed_document','text/markdown',document.title,original_asset_id=original.id,
            parser_id='utf8',parser_version='1')
        staged.append(derivative)
        original=replace(original,metadata={**original.metadata,'derivatives':{'normalized_markdown':derivative.id}})
    staged.append(original)
    return document, original, tuple(staged)
