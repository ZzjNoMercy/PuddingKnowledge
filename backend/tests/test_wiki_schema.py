import json
from pathlib import Path
import pytest
from knowledge_platform.wiki.schema import admit_schema_bundle, schema_closure_sha256
from knowledge_platform.wiki.schema_rules import BrainSchemaError

def fixture():
    raw=json.loads((Path(__file__).parent/'fixtures/wiki-schema-legacy.json').read_text())
    expected=raw.pop('resolved_yaml');raw.pop('legacy_source_sha256')
    return raw,expected

def commit(inputs):
    return schema_closure_sha256(**{k:v for k,v in inputs.items() if k in {'custom_yaml','brain_yaml','agents_markdown','catalog_yaml'}})

def admit(inputs):
    return admit_schema_bundle(**inputs,expected_closure_sha256=commit(inputs))

def test_frozen_legacy_resolution_and_bundle_hash():
    inputs,expected=fixture();result=admit(inputs)
    assert result.resolved_yaml==expected
    assert result.bundle_hash==inputs['expected_bundle_hash']
    assert result.lint_contract().prefixes_by_type=={'source':('sources/',),'concept':('concepts/',)}
    assert len(result.pack_digests)==3
    detached=result.lint_contract();detached.prefixes_by_type.clear()
    assert len(result.lint_contract().prefixes_by_type)==2

@pytest.mark.parametrize('field',['custom_yaml','brain_yaml','agents_markdown'])
def test_unchanged_external_commitment_rejects_byte_changes(field):
    inputs,_=fixture();sha=commit(inputs);inputs[field]+='\n'
    with pytest.raises(BrainSchemaError,match='closure commitment'):
        admit_schema_bundle(**inputs,expected_closure_sha256=sha)

def test_borrowed_bytes_bound_even_when_resolution_identical():
    inputs,_=fixture();sha=commit(inputs);inputs['catalog_yaml']['borrowed']+='\n'
    with pytest.raises(BrainSchemaError,match='closure commitment'):
        admit_schema_bundle(**inputs,expected_closure_sha256=sha)
    assert admit(inputs).closure_sha256!=sha

@pytest.mark.parametrize('attack',['missing_parent','missing_borrow','extra','name_mismatch','custom_shadow','cycle','agents','stale_custom','unknown_borrow','unknown_type','yaml_duplicate','yaml_alias','yaml_tag','bad_expected'])
def test_admission_rejects_adversarial_inputs_even_with_recomputed_closure(attack):
    inputs,_=fixture();catalog=inputs['catalog_yaml']
    if attack=='missing_parent':del catalog['base']
    elif attack=='missing_borrow':del catalog['borrowed']
    elif attack=='extra':catalog['unused']=catalog['borrowed'].replace('name: borrowed','name: unused')
    elif attack=='name_mismatch':catalog['borrowed']=catalog['borrowed'].replace('name: borrowed','name: wrong')
    elif attack=='custom_shadow':catalog['custom']=inputs['custom_yaml']
    elif attack=='cycle':catalog['base']=catalog['base'].replace('name: base','extends: middle\nname: base')
    elif attack=='agents':inputs['agents_markdown']+='Run a command now.\n'
    elif attack=='stale_custom':inputs['custom_yaml']+='\n'
    elif attack=='unknown_borrow':inputs['custom_yaml']=inputs['custom_yaml'].replace('- source','- absent')
    elif attack=='unknown_type':inputs['brain_yaml']=inputs['brain_yaml'].replace('- concept','- absent')
    elif attack=='yaml_duplicate':inputs['custom_yaml']+='name: duplicate\n'
    elif attack=='yaml_alias':inputs['custom_yaml']+='author: &x value\nlicense: *x\n'
    elif attack=='yaml_tag':inputs['custom_yaml']+='author: !!python/object/apply:os.system [echo unsafe]\n'
    elif attack=='bad_expected':inputs['expected_bundle_hash']='0'*64
    with pytest.raises(BrainSchemaError):admit(inputs)

def test_bounded_inputs():
    inputs,_=fixture();inputs['agents_markdown']='x'*(1024*1024+1)
    with pytest.raises(BrainSchemaError,match='budget'):admit(inputs)

@pytest.mark.parametrize('replacement', ['wiki/', "''", '../outside/', 'concepts/`injected`/', 'concepts/\\outside/'])
def test_prefix_escape_rejected(replacement):
    inputs,_=fixture()
    inputs['custom_yaml']=inputs['custom_yaml'].replace('wiki/concepts/',replacement)
    with pytest.raises(BrainSchemaError, match='path prefix'):admit(inputs)


def test_name_markdown_injection_rejected():
    inputs,_=fixture()
    inputs['custom_yaml']=inputs['custom_yaml'].replace('name: concept', 'name: "concept`injected"')
    with pytest.raises(BrainSchemaError,match='declaration name'):admit(inputs)

def test_resolver_preserves_override_priority_and_base_order():
    from knowledge_platform.wiki.schema_rules import SchemaPackManifest, SchemaResolver
    def pack(name,extends,pages):
        return SchemaPackManifest(name=name,version='1.0.0',extends=extends,
            page_types=[{'name':n,'primitive':'concept','path_prefixes':[prefix+'/']} for n,prefix in pages])
    base=pack('base',None,[('a','base-a'),('b','base-b')])
    middle=pack('middle','base',[('a','middle-a'),('c','middle-c')])
    borrowed=pack('borrowed',None,[('a','borrow-a'),('d','borrow-d')])
    custom=pack('custom','middle',[('a','custom-a'),('e','custom-e')])
    from knowledge_platform.wiki.schema_rules import BorrowFrom
    custom.borrow_from=[BorrowFrom(pack='borrowed',types=['a','d'])]
    resolved=SchemaResolver({p.name:p for p in [base,middle,borrowed]}).resolve_manifest(custom)
    assert [(p.name,p.path_prefixes) for p in resolved.page_types]==[
        ('e',['custom-e/']),('d',['borrow-d/']),('c',['middle-c/']),('a',['custom-a/']),('b',['base-b/'])]


def test_extends_depth_and_alias_cycle_rejected():
    from knowledge_platform.wiki.schema_rules import SchemaPackManifest, SchemaResolver
    packs={f'p{i}':SchemaPackManifest(name=f'p{i}',version='1.0.0',extends=f'p{i+1}' if i<9 else None) for i in range(10)}
    with pytest.raises(BrainSchemaError,match='hard cap'):
        SchemaResolver(packs).resolve_manifest(packs['p0'])
    custom=SchemaPackManifest(name='custom',version='1.0.0',extends=None,page_types=[
        {'name':'a','primitive':'concept','aliases':['b']},
        {'name':'b','primitive':'concept','aliases':['a']}])
    with pytest.raises(BrainSchemaError,match='alias cycle'):
        SchemaResolver({}).resolve_manifest(custom)


def test_structural_budget_shared_across_documents():
    from knowledge_platform.wiki.schema import _mapping
    budget=[5]
    assert _mapping('items: [a, b]', 'first', budget)=={'items':['a','b']}
    with pytest.raises(BrainSchemaError,match='structure budget'):
        _mapping('items: [a, b]', 'second', budget)
