"""Pure legacy schema resolution. No filesystem, model, or executable hooks."""
from __future__ import annotations
from copy import deepcopy
from typing import Annotated, Any, Literal
import hashlib
import re
import yaml
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_API_VERSION = 'gbrain-schema-pack-v1'

DEFAULT_CUSTOM_PACK = 'puddingclaw-wiki'

DEFAULT_PARENT_PACK = 'gbrain-base-v2'

PACK_NAME_RE = re.compile('^[a-z0-9._-]+$')

SEMVER_RE = re.compile('^\\d+\\.\\d+\\.\\d+$')

GBRAIN_VERSION_RE = re.compile('^\\d+\\.\\d+\\.\\d+(?:\\.\\d+)?$')

PRIMITIVES = ('entity', 'media', 'temporal', 'annotation', 'concept')

AGGREGATORS = ('scalar_brier', 'weighted_brier', 'count_based', 'cluster_summary')

class BrainSchemaError(RuntimeError):
    """Raised for catalog, validation, or bundle persistence failures."""

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')

class ExtractableSpec(StrictModel):
    prompt_template: str | None = None
    fixture_corpus: str | None = None
    eval_dimensions: list[str] = Field(default_factory=list)
    benchmark_min_recall: float | None = Field(default=None, ge=0, le=1)
    verifier_path: str | None = None

class SubtypeWhen(StrictModel):
    path_pattern: str | None = None
    frontmatter_field: str | None = None
    frontmatter_value: str | int | float | bool | None = None

class PageSubtype(StrictModel):
    name: str = Field(min_length=1)
    when: SubtypeWhen

class PageType(StrictModel):
    name: str = Field(min_length=1)
    primitive: Literal['entity', 'media', 'temporal', 'annotation', 'concept']
    path_prefixes: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    extractable: bool | ExtractableSpec = False
    expert_routing: bool = False
    subtypes: list[PageSubtype] | None = None

def workspace_page_prefixes(page_type: PageType | dict[str, Any]) -> list[str]:
    """Project gbrain paths into paths relative to PuddingClaw's wiki/ root.

    Some official packs describe paths from a whole-brain import root and
    therefore include ``wiki/``. PuddingClaw imports the contents of its
    ``wiki/`` directory as the gbrain source root, so retaining that segment
    would create ``wiki/wiki/...`` and incorrect page slugs.
    """
    raw_prefixes = page_type.path_prefixes if isinstance(page_type, PageType) else page_type.get('path_prefixes', [])
    normalized: list[str] = []
    for value in raw_prefixes:
        prefix = str(value).strip().lstrip('/')
        if prefix.startswith('wiki/'):
            prefix = prefix.removeprefix('wiki/')
        if prefix and prefix not in normalized:
            normalized.append(prefix)
    return normalized

class LinkInference(StrictModel):
    regex: str | None = None
    page_type: str | None = None
    target_type: str | None = None

class LinkType(StrictModel):
    name: str = Field(min_length=1)
    inverse: str | None = None
    inference: LinkInference | None = None

class FrontmatterLink(StrictModel):
    page_type: str
    fields: list[str] = Field(min_length=1)
    link_type: str

class EnrichableType(StrictModel):
    type: str
    rubric: str | None = None

class FilingRule(StrictModel):
    kind: str
    directory: str
    examples: list[str] = Field(default_factory=list)
    description: str | None = None

class BorrowFrom(StrictModel):
    pack: str
    types: list[str] | None = None
    link_types: list[str] | None = None

class CalibrationDomain(StrictModel):
    name: str = Field(pattern='^[a-z][a-z0-9_]*$')
    aggregator: Literal['scalar_brier', 'weighted_brier', 'count_based', 'cluster_summary']
    page_types: list[str] = Field(min_length=1)

class MigrationFrom(StrictModel):
    pack: str = Field(min_length=1)
    version: str = Field(min_length=1)

class FrontmatterFieldResolver(StrictModel):
    frontmatter_field: str = Field(min_length=1)

ResolverSpec = Literal['frontmatter', 'body_first_link', 'slug', 'body_excerpt'] | FrontmatterFieldResolver

class RetypeRule(StrictModel):
    kind: Literal['retype']
    from_type: str = Field(min_length=1)
    to_type: str = Field(min_length=1)
    subtype: str | None = None
    subtype_field: Literal['subtype', 'legacy_type', 'origin', 'format', 'kind', 'period', 'domain'] = 'subtype'
    path_filter: str | None = None

class PageToLinkRule(StrictModel):
    kind: Literal['page_to_link']
    from_type: str = Field(min_length=1)
    link_type: str = Field(min_length=1)
    source_slug_from: ResolverSpec
    target_slug_from: ResolverSpec
    inverse: str | None = None
    preserve_notes: bool | None = None

class PageToAliasRule(StrictModel):
    kind: Literal['page_to_alias']
    from_type: str = Field(min_length=1)
    canonical_from: ResolverSpec
    alias_slug_from: ResolverSpec
    notes_from: ResolverSpec | None = None

MappingRule = Annotated[RetypeRule | PageToLinkRule | PageToAliasRule, Field(discriminator='kind')]

class SchemaPackManifest(StrictModel):
    """Python mirror of gbrain's official SchemaPackManifest v1."""
    api_version: Literal['gbrain-schema-pack-v1'] = SCHEMA_API_VERSION
    name: str = Field(min_length=1)
    version: str
    description: str = ''
    author: str | None = None
    license: str | None = None
    homepage: AnyUrl | None = None
    gbrain_min_version: str = '0.38.0'
    extends: str | None = 'gbrain-base'
    borrow_from: list[BorrowFrom] = Field(default_factory=list)
    page_types: list[PageType] = Field(default_factory=list)
    link_types: list[LinkType] = Field(default_factory=list)
    frontmatter_links: list[FrontmatterLink] = Field(default_factory=list)
    takes_kinds: list[str] = Field(default_factory=lambda : ['fact', 'take', 'bet', 'hunch'])
    enrichable_types: list[EnrichableType] = Field(default_factory=list)
    filing_rules: list[FilingRule] = Field(default_factory=list)
    phases: list[str] | None = None
    calibration_domains: list[CalibrationDomain] | None = None
    migration_from: MigrationFrom | None = None
    mapping_rules: list[MappingRule] | None = None

    @field_validator('name')
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not PACK_NAME_RE.fullmatch(value):
            raise ValueError('must be a lowercase slug using a-z, 0-9, dot, underscore, or hyphen')
        return value

    @field_validator('version')
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not SEMVER_RE.fullmatch(value):
            raise ValueError('must be semver M.m.p')
        return value

    @field_validator('gbrain_min_version')
    @classmethod
    def validate_gbrain_version(cls, value: str) -> str:
        if not GBRAIN_VERSION_RE.fullmatch(value):
            raise ValueError('must be M.m.p or M.m.p.b')
        return value

    @model_validator(mode='after')
    def validate_references(self) -> SchemaPackManifest:
        own_types = {item.name for item in self.page_types}
        if len(own_types) != len(self.page_types):
            raise ValueError('page_types contains duplicate names')
        own_links = {item.name for item in self.link_types}
        if len(own_links) != len(self.link_types):
            raise ValueError('link_types contains duplicate names')
        prefixes: dict[str, str] = {}
        for page_type in self.page_types:
            for prefix in page_type.path_prefixes:
                owner = prefixes.get(prefix)
                if owner and owner != page_type.name:
                    raise ValueError(f'path_prefix {prefix!r} is declared by both {owner!r} and {page_type.name!r}')
                prefixes[prefix] = page_type.name
        return self

class WikiContract(StrictModel):
    layout: Literal['flat', 'typed_directories'] = 'typed_directories'
    allowed_page_types: list[str] = Field(default_factory=lambda : ['concept', 'system', 'debate'])
    allowed_link_types: list[str] = Field(default_factory=lambda : ['relates_to', 'supports', 'challenges'])
    required_frontmatter: list[str] = Field(default_factory=lambda : ['title', 'type', 'sources', 'created', 'updated', 'schema_version'])

class GbrainPackReference(StrictModel):
    path: str
    name: str
    version: str
    manifest_sha256: str

class BrainSchemaDocument(StrictModel):
    schema_id: str = DEFAULT_CUSTOM_PACK
    bundle_version: str = '0.1.0'
    gbrain_pack: GbrainPackReference
    wiki: WikiContract = Field(default_factory=WikiContract)

def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

class _IndentedSafeDumper(yaml.SafeDumper):
    """Emit sequence items indented below their mapping key.

    gbrain's YAML loader expects the conventional ``page_types:
  - name``
    shape; PyYAML's default indentless sequences are valid YAML but are not
    accepted by that loader.
    """

    def increase_indent(self, flow: bool=False, indentless: bool=False) -> int:
        return super().increase_indent(flow, False)

def _dump_yaml(value: dict[str, Any]) -> str:
    return yaml.dump(value, Dumper=_IndentedSafeDumper, allow_unicode=True, sort_keys=False, width=1000, default_flow_style=False)

def _model_dict(model: BaseModel, *, exclude_none: bool=True) -> dict[str, Any]:
    return model.model_dump(mode='json', exclude_none=exclude_none)

class SchemaResolver:
    def __init__(self, manifests):
        self._manifests = manifests

    def _catalog_manifests(self):
        return dict(self._manifests)

    def _render_agents(self, schema: BrainSchemaDocument, resolved: SchemaPackManifest | None) -> str:
        page_types = ', '.join(schema.wiki.allowed_page_types)
        link_types = ', '.join(schema.wiki.allowed_link_types)
        fields = ', '.join(schema.wiki.required_frontmatter)
        path_rules = [f"- `{page_type.name}`：{'、'.join((f'`{prefix}<slug>.md`' for prefix in prefixes))}。" for page_type in resolved.page_types if page_type.name in schema.wiki.allowed_page_types and (prefixes := workspace_page_prefixes(page_type))]
        path_section = '\n'.join(path_rules)
        return f'# LLM Wiki Agent 操作契约\n\n> schema: {schema.schema_id}@{schema.bundle_version}\n\n## 所有权与操作边界\n\n- `raw/` 只读，严禁修改、重命名或删除其中的内容。\n- Ingest 只能在 `.puddingclaw/staging/` 下写入补丁；由发布流程更新 `wiki/`。\n- Query 和 Lint 均为只读操作。\n- `wiki/log.md` 只允许追加，禁止改写已有记录。\n\n## Schema 约束\n\n- 允许的 page types：{page_types}。\n- 允许的 link types：{link_types}。\n- 必填 frontmatter：{fields}。\n- Wiki 页面按 Page Type 的 `path_prefixes` 分目录存放；文件名 slug 使用小写连字符格式。\n{path_section}\n- 所有 wikilink 必须写出与 Wiki 页面 slug 完全一致的目录前缀：使用 `[[<type-directory>/<slug>]]` 或 `[[<type-directory>/<slug>|显示文本]]`；禁止裸写 `[[<slug>]]`，以保证原生查询与可选图谱投影使用同一标识。\n\n## AI Agent Wiki 分类准则\n\n- `research_paper`：论文、预印本和技术报告；保留作者、年份、URL/DOI/arXiv 标识及核心结论。\n- `software_framework`：LangGraph、AutoGen 等具体软件框架；抽象方法论仍使用 `concept`。\n- `ai_model`：具体模型或模型系列；模型背后的技术方法使用 `concept`。\n- `programming_language`：Python、TypeScript、Rust 等具体编程语言。\n- `system`：具体 Agent 系统、产品或可运行实现；项目过程使用 `project`。\n- `engineering_practice`：从开发记录中提炼的可复用工程经验，必须说明问题、根因、方案、验证、适用范围和失效条件。\n- 普通文章、博客、视频等来源优先使用内置 `media` 或 `source`，不要重复增加 `article`。\n- `media` 页面必须保留 Raw 明确给出的原始署名与发布平台；不得把发布渠道、账号名和自然人身份互换。\n- 对微信公众号、Newsletter、博客、播客频道、视频频道等具有稳定名称且会重复收录内容的发布渠道，应建立或复用一个 `source` 页面；先从 Index 解析已有 slug，再使用 `sourced_from` 将每个 `media` 页面链接到该来源，禁止为同一平台账号重复建页。首次建页时只记录 Raw 直接支持的账号名、平台及已收录内容，不得补写外部事实。\n- `source` 页的“已收录内容”必须使用完整路径 wikilink 指向对应 `media` 页面：`[[media/<slug>|文章标题]]`。禁止使用“详见 media 页面”等纯文本代替链接。对于同一收录关系，`media` 必须以 `sourced_from` 指向 `source`，`source` 也必须在“已收录内容”中反向链接 `media`，确保双向可导航；不得重复收录同一页面。\n- 账号名或频道名本身不等于 `person`。只有 Raw 明确支持具体自然人身份时才建立 `person` 页面和 `authored` 关系；仅有署名时保留原始署名，不创建空洞人物页。\n- Raw 自带的 `id`、`type`、`subtype`、`related` 等 frontmatter 是来源元数据和分类线索，不是目标 Wiki 的分类结论；必须根据正文主题和活动 Schema 重新判断 page type 与关系。\n- 编写页面前先在内部完成“长期实体—稳定主题—关系”清单。对 Raw 明确出现且证据充分的具体框架、系统、模型、语言和工程实践分别建页；一份 raw 可以编译出多个 Wiki 页面，一个页面只描述一个长期稳定主题，不按 raw 文件机械地一对一建页。\n- 具体框架与作用于该框架的工程实践应分别建为 `software_framework` 和 `engineering_practice`；若框架只有名称而缺少足以形成稳定页面的事实，则保留为正文文本并报告知识缺口，不得用模型常识补全。\n- 本次选中的 Raw 正文是事实依据。`source_refs`、文件路径、URL 和其他引用字段只表示来源线索；除非对应内容也作为本次 Raw 被授权读取，否则不得递归读取或据此引入事实。\n- 现有 `index.md` 只用于发现和解析已有页面 slug，不是事实证据；不得根据 Index 摘要推断归属、依赖、实现方或其他关系。\n- 每项事实、实体归属和关系都必须由本次 Raw 直接支持。严格保留专有名词及主客体，不得把不同框架、系统、公司或项目互换；模型既有知识不能作为补充证据。\n- 只在 Raw 明确支持关系时创建 wikilink。不得为了避免孤立页面或满足“互链”而添加关系；暂时没有可信关系的页面可以保持孤立。若关系两端均有充分证据且页面尚不存在，应在同一次 publish 中创建缺失页面。\n- 优先使用 `introduces`、`implements`、`uses`、`depends_on`、`evaluates`、`applies_to`、`supports`、`challenges` 以及内置关系；关系两端只使用相对 `wiki/` 根目录的完整页面 slug，例如 `[[concepts/compiled-rag]]`，不得再次添加 `wiki/`。\n\n## Ingest（摄取）\n\n开始前读取 `AGENTS.md`、当前 Schema Bundle、本次选中的 raw 文件和 Wiki Index。先完成实体、主题、页面类型和有证据关系的规划，再生成页面；发布前逐项核对页面中的事实归属和关系是否可由本次 Raw 直接支持。生成 staging patch、更新索引覆盖情况，并追加一条 Ingest 日志。每个新增或更新的页面必须引用至少一个本次选中的 raw 文件；sources 只能填写 `raw/manifest.jsonl` 中不可变的精确 `snapshot_path`；直接复制 context 返回的路径，例如 `manual-upload-.../document.md`，不要添加 `raw/` 前缀。\n\n## Query（查询）\n\n先读取 `wiki/index.md`，再按需读取相关 Wiki 页面；Query 期间不得读取 raw。回答时引用 Wiki slug 和 sources；若 Wiki 信息不足，明确报告知识缺口。\n\n## Lint（检查）\n\n报告无效 frontmatter、未知类型、断链、孤立页面、索引遗漏、日志改写和过期 raw hash。Lint 不得修改任何文件。\n'

    @staticmethod
    def _merge_by_key(layers: list[list[dict[str, Any]]], key: str) -> list[dict[str, Any]]:
        seen: set[str] = set()
        output: list[dict[str, Any]] = []
        for layer in layers:
            for item in layer:
                value = str(item.get(key, ''))
                if value in seen:
                    continue
                seen.add(value)
                output.append(deepcopy(item))
        return output

    def resolve_manifest(self, custom: SchemaPackManifest) -> SchemaPackManifest:
        manifests = self._catalog_manifests()
        manifests[custom.name] = custom
        ancestors: list[SchemaPackManifest] = []
        current_name = custom.extends
        seen = {custom.name}
        while current_name:
            if current_name in seen:
                raise BrainSchemaError(f'Schema extends cycle detected at {current_name}')
            seen.add(current_name)
            parent = manifests.get(current_name)
            if parent is None:
                raise BrainSchemaError(f'Unknown parent schema pack: {current_name}')
            ancestors.append(parent)
            if len(ancestors) > 8:
                raise BrainSchemaError('Schema extends chain exceeds gbrain hard cap of 8')
            current_name = parent.extends
        ancestors.reverse()
        borrowed_pages: list[dict[str, Any]] = []
        borrowed_links: list[dict[str, Any]] = []
        for spec in custom.borrow_from:
            target = manifests.get(spec.pack)
            if target is None:
                raise BrainSchemaError(f'Unknown borrowed schema pack: {spec.pack}')
            target_dict = _model_dict(target)
            type_filter = set(spec.types or [])
            link_filter = set(spec.link_types or [])
            borrowed_pages.extend((item for item in target_dict['page_types'] if type_filter and item['name'] in type_filter))
            borrowed_links.extend((item for item in target_dict['link_types'] if link_filter and item['name'] in link_filter))
        custom_dict = _model_dict(custom)
        ancestor_dicts = [_model_dict(item) for item in ancestors]
        base_pages = ancestor_dicts[0]['page_types'] if ancestor_dicts else []
        base_by_name = {item['name']: deepcopy(item) for item in base_pages}
        base_order = [item['name'] for item in base_pages]
        middle = ancestor_dicts[1:]
        for layer in [*[item['page_types'] for item in middle], borrowed_pages, custom_dict['page_types']]:
            for item in layer:
                if item['name'] in base_by_name:
                    base_by_name[item['name']] = deepcopy(item)
        new_pages: list[dict[str, Any]] = []
        new_seen: set[str] = set()
        for layer in [custom_dict['page_types'], borrowed_pages, *[item['page_types'] for item in reversed(middle)]]:
            for item in layer:
                name = item['name']
                if name in base_by_name or name in new_seen:
                    continue
                new_seen.add(name)
                new_pages.append(deepcopy(item))
        resolved_pages = new_pages + [base_by_name[name] for name in base_order]
        ancestors_high = list(reversed(ancestor_dicts))
        resolved = deepcopy(custom_dict)
        resolved['page_types'] = resolved_pages
        resolved['link_types'] = self._merge_by_key([custom_dict['link_types'], borrowed_links, *[item['link_types'] for item in ancestors_high]], 'name')
        for (field, key) in (('enrichable_types', 'type'), ('filing_rules', 'kind')):
            resolved[field] = self._merge_by_key([custom_dict[field], *[item[field] for item in ancestors_high]], key)
        front_seen: set[tuple[str, str]] = set()
        resolved_front: list[dict[str, Any]] = []
        for layer in [custom_dict['frontmatter_links'], *[item['frontmatter_links'] for item in ancestors_high]]:
            for item in layer:
                key = (item['page_type'], item['link_type'])
                if key not in front_seen:
                    front_seen.add(key)
                    resolved_front.append(deepcopy(item))
        resolved['frontmatter_links'] = resolved_front
        take_seen: set[str] = set()
        resolved_takes: list[str] = []
        for layer in [custom_dict['takes_kinds'], *[item['takes_kinds'] for item in ancestors_high]]:
            for take in layer:
                if take not in take_seen:
                    take_seen.add(take)
                    resolved_takes.append(take)
        resolved['takes_kinds'] = resolved_takes
        manifest = SchemaPackManifest.model_validate(resolved)
        self._validate_resolved_manifest(manifest)
        return manifest

    @staticmethod
    def _validate_resolved_manifest(manifest: SchemaPackManifest) -> None:
        """Validate the portable Wiki Schema without invoking an external CLI."""
        page_names = {item.name for item in manifest.page_types}
        link_names = {item.name for item in manifest.link_types}
        alias_owner: dict[str, str] = {}
        alias_edges: dict[str, set[str]] = {name: set() for name in page_names}
        prefix_owner: dict[str, str] = {}
        for page_type in manifest.page_types:
            subtype_names = [item.name for item in page_type.subtypes or []]
            if len(set(subtype_names)) != len(subtype_names):
                raise BrainSchemaError(f'Page type {page_type.name!r} contains duplicate subtype names')
            for prefix in workspace_page_prefixes(page_type):
                owner = prefix_owner.get(prefix)
                if owner and owner != page_type.name:
                    raise BrainSchemaError(f'Wiki path prefix {prefix!r} is declared by both {owner!r} and {page_type.name!r}')
                prefix_owner[prefix] = page_type.name
            for alias in page_type.aliases:
                value = str(alias).strip()
                if not value:
                    raise BrainSchemaError(f'Page type {page_type.name!r} contains an empty alias')
                owner = alias_owner.get(value)
                if owner and owner != page_type.name:
                    raise BrainSchemaError(f'Page type alias {value!r} is declared by both {owner!r} and {page_type.name!r}')
                alias_owner[value] = page_type.name
                if value in page_names:
                    alias_edges[page_type.name].add(value)
        visiting: set[str] = set()
        visited: set[str] = set()
    
        def visit(name: str, trail: list[str]) -> None:
            if name in visiting:
                start = trail.index(name) if name in trail else 0
                cycle = ' -> '.join([*trail[start:], name])
                raise BrainSchemaError(f'Wiki Schema page type alias cycle detected: {cycle}')
            if name in visited:
                return
            visiting.add(name)
            for target in sorted(alias_edges.get(name, set())):
                visit(target, [*trail, name])
            visiting.remove(name)
            visited.add(name)
        for page_name in sorted(page_names):
            visit(page_name, [])
        for mapping in manifest.frontmatter_links:
            if mapping.page_type not in page_names:
                raise BrainSchemaError(f'frontmatter_links references unknown page type {mapping.page_type!r}')
            if mapping.link_type not in link_names:
                raise BrainSchemaError(f'frontmatter_links references unknown link type {mapping.link_type!r}')
        for link_type in manifest.link_types:
            inference = link_type.inference
            if inference is None:
                continue
            referenced = {value for value in (inference.page_type, inference.target_type) if value}
            unknown = sorted(referenced - page_names)
            if unknown:
                raise BrainSchemaError(f"link type {link_type.name!r} inference references unknown page types: {', '.join(unknown)}")
        for enrichable in manifest.enrichable_types:
            if enrichable.type not in page_names:
                raise BrainSchemaError(f'enrichable_types references unknown page type {enrichable.type!r}')
        for domain in manifest.calibration_domains or []:
            unknown = sorted(set(domain.page_types) - page_names)
            if unknown:
                raise BrainSchemaError(f"calibration domain {domain.name!r} references unknown page types: {', '.join(unknown)}")
        for rule in manifest.mapping_rules or []:
            if isinstance(rule, RetypeRule):
                referenced_types = {rule.to_type}
                referenced_links: set[str] = set()
            elif isinstance(rule, PageToLinkRule):
                referenced_types = set()
                referenced_links = {rule.link_type}
            else:
                referenced_types = set()
                referenced_links = set()
            unknown_types = sorted(referenced_types - page_names)
            unknown_links = sorted(referenced_links - link_names)
            if unknown_types:
                raise BrainSchemaError(f"mapping rule references unknown page types: {', '.join(unknown_types)}")
            if unknown_links:
                raise BrainSchemaError(f"mapping rule references unknown link types: {', '.join(unknown_links)}")
