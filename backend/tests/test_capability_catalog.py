from dataclasses import replace
from types import SimpleNamespace
import asyncio
import pytest
from test_environment_preparation import db, BASE
from app import environment_preparation as prep
from app import runtime_capability as runtime
from app.environment_capabilities import resolve_capabilities


@pytest.mark.parametrize('text,cap', [
    ('把 Markdown 转为 HTML','text.markdown'),
    ('Read XML documents','data.xml'),
    ('生成一维条形码','image.barcode'),
    ('import os, markdown as md','text.markdown'),
    ('from defusedxml.ElementTree import fromstring','data.xml'),
])
def test_catalog_identifies_intent_and_imports(text,cap):
    a=prep.analyze_capabilities(text,{})
    assert cap in a['inferred_capabilities']
    assert any(e['capability']==cap and e.get('line')==1 for e in a['evidence'])
    assert not a['unresolved_imports']
    assert len(prep.environment_spec([cap],BASE)['wheels'])==1


@pytest.mark.parametrize('text', ['不要生成条形码','Do not read XML','无需 Markdown 转为 HTML','原件上有条形码，不得遮挡'])
def test_negated_prose_does_not_trigger_extensions(text):
    assert not (set(prep.analyze_capabilities(text,{})['inferred_capabilities']) & set(prep.EXTENSIONS))


def test_unknown_import_is_visible_but_never_installed():
    a=prep.analyze_capabilities('import os, cv2, local_helper\nfrom .helpers import thing',{})
    assert a['unresolved_imports']==['cv2','local_helper']
    assert a['warnings']
    assert not prep.environment_spec(a['inferred_capabilities'],BASE)['wheels']


def test_new_providers_require_functional_success():
    assert {'text.markdown','image.barcode','data.xml'} <= set(resolve_capabilities({
        'markdown':{'available':True},'python-barcode':{'available':True},'defusedxml':{'available':True}}))
    assert 'data.xml' not in resolve_capabilities({'defusedxml':{'available':False}})


def test_old_catalog_digests_do_not_prevent_additive_upgrade(db,monkeypatch):
    monkeypatch.setattr(runtime,'settings',replace(runtime.settings,environment_preparation_enabled=True,environment_queue_wait_seconds=0))
    previous=prep.enqueue_environment(db,prep.environment_spec(['pdf.read']))
    previous.status='ready';previous.image_id=BASE
    previous.spec={**previous.spec,'probe_digest':'old-probe','extension_digest':'old-catalog'}
    db.commit()
    job=SimpleNamespace(memory=SimpleNamespace(data={'prepared_environment_digest':previous.digest,
        'environment':{'image_id':BASE,'capabilities':['pdf.read']}}))
    result=asyncio.run(runtime.request_capability(db,job,object(),'text.markdown',lambda:False))
    assert result['error_code']=='ENVIRONMENT_BUILD_WAIT_TIMEOUT'
    assert job.memory.data['environment_upgrade_attempts']==1
    assert job.memory.data['prepared_environment_digest']==previous.digest


def test_old_dependency_lock_change_still_rejected(db,monkeypatch):
    monkeypatch.setattr(runtime,'settings',replace(runtime.settings,environment_preparation_enabled=True))
    previous=prep.enqueue_environment(db,prep.environment_spec(['image.qr']))
    previous.status='ready'
    previous.spec={**previous.spec,'wheels':[{**previous.spec['wheels'][0],'version':'0.0.0'}]}
    db.commit()
    job=SimpleNamespace(memory=SimpleNamespace(data={'prepared_environment_digest':previous.digest,'environment':{}}))
    result=asyncio.run(runtime.request_capability(db,job,object(),'data.xml',lambda:False))
    assert result['error_code']=='ENVIRONMENT_CAPABILITY_UNSUPPORTED'
    assert 'environment_upgrade_attempts' not in job.memory.data
