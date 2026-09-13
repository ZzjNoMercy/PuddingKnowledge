from dataclasses import replace
import pytest
from knowledge_platform.wiki.lint import WikiLintContract, lint_workspace

CONTRACT=WikiLintContract('0.1.0','a'*64,('concept','source','media'),('title','type','sources','created','updated','schema_version'),{'concept':('concepts/',),'source':('sources/',),'media':('media/',)})

def page(kind='concept',body='# Page\n',sources='[a.md]',version='0.1.0'):
    return f'---\ntitle: Page\ntype: {kind}\nsources: {sources}\ncreated: 2026-09-13\nupdated: 2026-09-13\nschema_version: {version}\n---\n'+body

def lint(pages=None,index='[[concepts/page]]',**kw):
    return lint_workspace(contract=kw.pop('contract',CONTRACT),pages={'concepts/page':page()} if pages is None else pages,index=index,log_present=kw.pop('log_present',True),raw_hashes=kw.pop('raw_hashes',{'a.md':'b'*64}),raw_manifest_sha256='c'*64,**kw)

def codes(result):return {x['code'] for x in result['errors']}

def test_valid_contract_page_and_digest_provenance():
    result=lint();assert result['ok'] and result['bundle_hash']=='a'*64 and result['raw_manifest_sha256']=='c'*64

@pytest.mark.parametrize('pages,index,expected',[
    ({'concepts/page':'no metadata'},'[[concepts/page]]','invalid_frontmatter'),
    ({'concepts/page':page(version='wrong')},'[[concepts/page]]','schema_drift'),
    ({'concepts/page':page(kind='other')},'[[concepts/page]]','unknown_page_type'),
    ({'wrong/page':page()},'[[wrong/page]]','page_path_type_mismatch'),
    ({'concepts/page':page(sources='[missing.md]')},'[[concepts/page]]','unknown_source'),
    ({'concepts/page':page(sources='a.md')},'[[concepts/page]]','invalid_sources'),
    ({'concepts/page':page(body='[[missing/page]]')},'[[concepts/page]]','broken_wikilink'),
    ({'concepts/page':page(body='[[page]]')},'[[concepts/page]]','invalid_wikilink'),
    ({'wiki/concepts/page':page()},'[[wiki/concepts/page]]','duplicate_wiki_root'),
    ({'concepts/page':page()},'','index_omission'),
    ({'concepts/page':page()},'[[concepts/page]] [[other/missing]]','index_broken_link'),
])
def test_rule_failures(pages,index,expected):assert expected in codes(lint(pages,index))

def test_legacy_raw_prefix_is_warning():
    result=lint({'concepts/page':page(sources='[raw/a.md]')})
    assert result['ok'] and 'legacy_source_prefix' in {x['code'] for x in result['warnings']}

def test_media_source_relations():
    pages={'sources/source':page('source','## 已收录内容\n[[media/article]]\n'),'media/article':page('media','sourced_from [[sources/source]]\n')}
    index='[[sources/source]] [[media/article]]'
    assert lint(pages,index)['ok']
    pages['media/article']=page('media','# Missing source\n')
    assert 'missing_media_source_relation' in codes(lint(pages,index))
    pages['media/article']=page('media','sourced_from [[sources/source]]\n')
    pages['sources/source']=page('source','# No backlink\n')
    assert 'missing_source_backlink' in codes(lint(pages,index))

def test_raw_integrity_and_required_generated_files():
    result=lint(index=None,log_present=False,raw_hashes={'a.md':'missing'})
    assert {'missing_index','missing_log','raw_hash_mismatch'}<=codes(result)

@pytest.mark.parametrize('header',['title: One\ntitle: Two','title: &a test\ntype: *a','title: !!python/object/apply:os.system [echo unsafe]'])
def test_yaml_duplicate_alias_and_tags_rejected(header):
    assert 'invalid_frontmatter' in codes(lint({'concepts/page':'---\n'+header+'\n---\n# Page'}))

def test_input_and_finding_budgets():
    with pytest.raises(ValueError):lint({'concepts/page':'x'*(8*1024*1024+1)})
    with pytest.raises(ValueError):lint(contract=replace(CONTRACT,bundle_hash='not-a-digest'))
    with pytest.raises(ValueError,match='finding budget'):lint({'concepts/page':page(body='[[missing]] '*10001)})

def test_nested_index_is_not_generated_root_index():
    assert lint({'concepts/index':page()},'[[concepts/index]]')['ok']


def test_source_relation_markers_are_line_bounded_and_linear():
    from knowledge_platform.wiki.lint import _sourced_from_targets
    assert _sourced_from_targets('sourced_from sourced_from [[sources/a]] [[sources/b]]\nsourced_from\n[[sources/c]]') == {'sources/a'}
    assert _sourced_from_targets('sourced_from ' * 20000) == set()

    assert _sourced_from_targets('[[sources/sourced_from]] [[sources/target]]') == {'sources/target'}

    assert _sourced_from_targets('sourced_from [[sources/a|Display name]]') == {'sources/a'}
