import base64,csv,hashlib,json
from pathlib import Path
from types import SimpleNamespace
import pytest
from knowledge_platform.distribution.installed_identity import inspect_installation


def fixture(root):
    root=root/'site-packages';root.mkdir();meta=root/'puddingknowledge_local-0.1.0.dist-info';meta.mkdir()
    for name,data in {'knowledge_platform/__init__.py':b'','knowledge_contracts/__init__.py':b'','knowledge_platform/read.py':b'print(1)','puddingknowledge_local-0.1.0.dist-info/METADATA':b'Name: puddingknowledge-local\nVersion: 0.1.0\n','puddingknowledge_local-0.1.0.dist-info/WHEEL':b'Wheel-Version: 1.0\n'}.items():
        p=root/name;p.parent.mkdir(exist_ok=True);p.write_bytes(data)
    def write_record():
        rows=[]
        for p in sorted(root.rglob('*')):
            if p.is_file() and p.name not in ('RECORD','direct_url.json'):
                data=p.read_bytes();rows.append([p.relative_to(root).as_posix(),'sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('='),str(len(data))])
        rows.append([meta.name+'/RECORD','',''])
        with (meta/'RECORD').open('w') as f:csv.writer(f).writerows(rows)
    write_record()
    dist=SimpleNamespace(version='0.1.0',metadata={'Name':'puddingknowledge-local'},files=[Path(meta.name)/'RECORD'],locate_file=lambda name:root/name)
    return root,meta,dist,write_record


def test_identity_is_stable_and_binds_same_version_new_bytes(tmp_path):
    root,meta,dist,rewrite=fixture(tmp_path);first=inspect_installation(dist)
    assert inspect_installation(dist)==first and first['authenticated'] is False
    (root/'knowledge_platform/read.py').write_bytes(b'print(2)');rewrite()
    assert inspect_installation(dist)['inventory_sha256']!=first['inventory_sha256']
    assert str(root) not in json.dumps(first)


@pytest.mark.parametrize('mode',['changed','missing','extra','link','hardlink','editable','duplicate','nohash','outside_module','missing_package','special'])
def test_drift_and_unsupported_installations_reject(tmp_path,mode):
    root,meta,dist,_=fixture(tmp_path);path=root/'knowledge_platform/read.py';kwargs={}
    if mode=='changed':path.write_bytes(b'print(2)')
    if mode=='missing':path.unlink()
    if mode=='extra':(root/'knowledge_platform/extra.py').write_text('extra')
    if mode=='link':path.unlink();path.symlink_to(root/'knowledge_platform/__init__.py')
    if mode=='hardlink':(tmp_path/'linked').hardlink_to(path)
    if mode=='editable':(meta/'direct_url.json').write_text(json.dumps({'dir_info':{'editable':True}}))
    if mode=='duplicate':
        record=meta/'RECORD';record.write_text(record.read_text()+record.read_text().splitlines()[0]+'\n')
    if mode=='nohash':
        record=meta/'RECORD';record.write_text(record.read_text().replace('sha256=','md5='))
    if mode=='outside_module':kwargs['module_file']=tmp_path/'elsewhere.py'
    if mode=='missing_package':(root/'knowledge_contracts/__init__.py').unlink()
    if mode=='special':
        import os
        os.mkfifo(root/'knowledge_platform/fifo')
    with pytest.raises(ValueError):inspect_installation(dist,**kwargs)


def test_generated_bytecode_does_not_change_source_fingerprint(tmp_path):
    root,meta,dist,_=fixture(tmp_path);before=inspect_installation(dist)
    cache=root/'knowledge_platform/__pycache__';cache.mkdir();(cache/'read.cpython-312.pyc').write_bytes(b'generated')
    assert inspect_installation(dist)==before


def test_cache_special_entry_rejects(tmp_path):
    import os
    root,_,dist,_=fixture(tmp_path)
    cache=root/'knowledge_platform/__pycache__';cache.mkdir();os.mkfifo(cache/'fake.pyc')
    with pytest.raises(ValueError):inspect_installation(dist)
