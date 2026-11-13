from dataclasses import replace
from types import SimpleNamespace
from datetime import timedelta
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.models import PreparedEnvironment, SkillVersion, Skill, User, SkillType, utcnow
from app import environment_preparation as prep
from app import environment_worker as worker
from app.environment_builder import run_container

BASE = 'sha256:' + 'a'*64


def test_wheel_cache_preserves_verified_downloads_after_partial_failure(tmp_path, monkeypatch):
    import base64
    import hashlib
    import json
    from app import environment_builder as builder

    data = b'verified wheel contents'
    wheel = {'filename': 'package.whl', 'sha256': hashlib.sha256(data).hexdigest()}
    other = {'filename': 'other.whl', 'sha256': 'a' * 64}
    output = json.dumps([wheel['filename'], base64.b64encode(data).decode()]).encode()
    fetch = SimpleNamespace(start=lambda: None, logs=lambda **kwargs: output)
    monkeypatch.setattr(builder, 'run_container', lambda *args, **kwargs: fetch)

    def fail(*args):
        raise TimeoutError('download timeout')

    monkeypatch.setattr(builder, 'wait_container', fail)
    with pytest.raises(TimeoutError, match='download timeout'):
        builder.fetch_missing_wheels(None, SimpleNamespace(id='base'), [wheel, other], tmp_path,
                                     {}, 'attempt', 1, [], True)
    path = builder.wheel_cache_path(tmp_path, wheel)
    assert builder.read_cached_wheel(path, wheel) == data
    assert builder.read_cached_wheel(builder.wheel_cache_path(tmp_path, other), other) is None
    path.write_bytes(b'corrupted wheel')
    assert builder.read_cached_wheel(path, wheel) is None
    assert not path.exists()


def test_numpy_alone_does_not_require_pandas_capability():
    analysis = prep.analyze_capabilities('import numpy as np', {})
    assert 'data.tabular' not in analysis['inferred_capabilities']
    assert 'data.tabular' in prep.analyze_capabilities('import pandas as pd', {})['inferred_capabilities']

@pytest.mark.parametrize('text', [
    '严禁遮挡原文、数字、编号、签章、表格线、尺寸、二维码。',
    'Preserve existing QR codes. Do not generate QR codes.',
    '无需生成二维码', 'Do not import qrcode', '识别二维码',
])
def test_mentions_do_not_install_qr_generator(text):
    analysis = prep.analyze_capabilities(text, {})
    assert 'image.qr' not in analysis['inferred_capabilities']
    assert not prep.environment_spec(analysis['inferred_capabilities'], BASE)['wheels']

@pytest.mark.parametrize('text', ['生成二维码', 'Create a QR code', 'import qrcode\nqrcode.make("test")'])
def test_qr_generation_has_traceable_preparation(text):
    analysis = prep.analyze_capabilities(text, {})
    assert 'image.qr' in analysis['inferred_capabilities']
    assert any(e['capability'] == 'image.qr' for e in analysis['evidence'])
    assert prep.environment_spec(analysis['inferred_capabilities'], BASE)['wheels'][0]['name'] == 'qrcode'

def test_inferred_extension_build_is_reused_for_another_skill(db, monkeypatch):
    first, second = version(db), version(db, 'other')
    first.skill_md += '\nCreate a QR code'
    second.skill_md += '\n生成二维码'
    env = prep.bind_version_environment(db, first)
    db.commit()
    calls = []
    def build(client, spec, attempt, on_probing):
        calls.append(spec)
        on_probing()
        return BASE, {'capabilities': ['image.qr', 'pdf.read']}
    monkeypatch.setattr(worker, 'build_environment', build)
    assert worker.process_one(None)
    db.expire_all()
    reused = prep.bind_version_environment(db, second)
    assert reused.digest == env.digest and reused.status == 'ready'
    db.commit()
    assert not worker.process_one(None)
    assert len(calls) == 1

@pytest.fixture
def db(monkeypatch):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(worker, 'SessionLocal', factory)
    monkeypatch.setattr(prep, 'settings', replace(prep.settings, environment_preparation_enabled=True, environment_base_image=BASE))
    with factory() as session:
        yield session
    engine.dispose()


def version(db, name='first', capabilities='pdf.read'):
    owner = User(email=name+'@test.local', display_name='test', password_hash='test')
    db.add(owner); db.flush()
    skill = Skill(owner_id=owner.id, slug=name, name=name, summary='test', description='')
    db.add(skill); db.flush()
    v = SkillVersion(skill_id=skill.id, skill=skill, created_by_id=owner.id, version='1.0.0', skill_type=SkillType.INSTRUCTION,
        package_sha256='0'*64, package_path='test.zip', manifest={},
        skill_md=f'---\nname: test\ndescription: testing\ncapabilities: [{capabilities}]\n---\nRead input',
        input_schema={}, output_schema={}, requested_permissions={})
    db.add(v); db.flush()
    return v


def test_binding_deduplicates_and_gates_publish(db):
    first, second = version(db), version(db, 'second')
    a = prep.bind_version_environment(db, first)
    b = prep.bind_version_environment(db, second)
    assert a.digest == b.digest
    assert 'test.local' not in str(a.spec) and 'test.zip' not in str(a.spec)
    with pytest.raises(Exception) as caught:
        prep.require_ready_version(first)
    assert caught.value.status_code == 409
    a.status='ready'; a.image_id=BASE
    prep.require_ready_version(first)
    assert first.environment_preparation['status']=='ready'


def test_unknown_capability_never_installs(db):
    v=version(db, capabilities='evil.install')
    env=prep.bind_version_environment(db,v)
    assert env.status=='failed'
    assert 'catalog' in env.error_message
    assert not env.spec.get('wheels')


def test_union_digest_stable_and_image_base_conflicts(db):
    a,b=version(db),version(db,'qr','image.qr')
    first=prep.prepare_job_environment(db,[a,b])
    second=prep.prepare_job_environment(db,[b,a])
    assert first.digest == second.digest
    assert len(first.spec['wheels']) == 1
    b.environment_binding.environment.spec={**b.environment_binding.environment.spec,'base_image':'sha256:'+'b'*64}
    with pytest.raises(Exception) as caught:
        prep.prepare_job_environment(db,[a,b])
    assert caught.value.status_code == 409


def test_worker_probes_then_binds_and_fences_stale_completion(db, monkeypatch):
    env=prep.enqueue_environment(db,prep.environment_spec(['pdf.read']))
    db.commit()
    def build(client,spec,attempt,on_probing):
        on_probing()
        with worker.SessionLocal() as session:
            assert session.get(PreparedEnvironment, env.digest).status=='probing'
        return BASE, {'capabilities':['pdf.read']}
    monkeypatch.setattr(worker,'build_environment',build)
    assert worker.process_one(None)
    db.expire_all()
    assert db.get(PreparedEnvironment,env.digest).status=='ready'
    assert not worker.process_one(None)
    with pytest.raises(RuntimeError):
        worker.finish_attempt(env.digest,'wrong',status='failed')


def test_failed_build_cannot_be_ready(db,monkeypatch):
    env=prep.enqueue_environment(db,prep.environment_spec(['image.qr']))
    db.commit()
    def fail(*args,**kwargs): raise ValueError('digest mismatch')
    monkeypatch.setattr(worker,'build_environment',fail)
    worker.process_one(None)
    db.expire_all()
    assert env.status=='failed' and env.image_id is None
    assert env.error_message=='digest mismatch'


def test_expired_attempt_is_terminal(db):
    env=prep.enqueue_environment(db,prep.environment_spec([]))
    env.status='building'; env.attempt='old'; env.lease_until=utcnow()-timedelta(seconds=1)
    db.commit()
    assert worker.claim_environment() is None
    db.expire_all()
    assert env.status=='failed'


def test_containers_have_no_host_mounts_or_credentials():
    calls=[]
    client=SimpleNamespace(containers=SimpleNamespace(create=lambda **kw: calls.append(kw)))
    run_container(client,image=BASE,argv=['python3','-I','-c','pass'],attempt='test',network=True)
    run_container(client,image=BASE,argv=['python3','-I','-c','pass'],attempt='test',writable=True,user='0:0')
    for c in calls:
        assert 'volumes' not in c and 'mounts' not in c
        assert c['cap_drop']==['ALL'] and c['runtime']
        assert not any('KEY' in k or 'PASSWORD' in k or 'DATABASE' in k for k in c['environment'])
    assert calls[0]['network_mode']=='bridge' and calls[0]['read_only']
    assert calls[1]['network_mode']=='none'


def test_mutable_base_and_arbitrary_package_rejected():
    with pytest.raises(ValueError): prep.environment_spec([], 'image:latest')
    with pytest.raises(ValueError): prep.environment_spec(['pip:whatever'], BASE)


def test_generic_python_requirements_bind_and_build_with_generic_flag(db, monkeypatch):
    v = version(db, name='generic', capabilities='')
    v.skill_md = '---\nname: generic\n---\nimport scipy\n'
    seen = []
    env = prep.bind_version_environment(db, v)
    db.commit()
    assert env.status == 'queued'
    assert env.spec.get('python_requirements') == ['scipy']
    assert env.spec.get('python_imports') == ['scipy']

    def build(client, spec, attempt, on_probing):
        seen.append(spec)
        on_probing()
        return BASE, {'capabilities': [], 'python_imports_verified': True}
    monkeypatch.setattr(worker, 'build_environment', build)
    assert worker.process_one(None)
    db.expire_all()
    ready = db.get(PreparedEnvironment, env.digest)
    assert ready.status == 'ready' and ready.image_id == BASE
    assert seen[0]['python_requirements'] == ['scipy']

def test_upload_status_publish_gate_and_retry(client, user_headers, monkeypatch):
    import io, zipfile
    from test_workflow_jobs import create_version
    from app.database import SessionLocal
    monkeypatch.setattr(prep, 'settings', replace(prep.settings, environment_preparation_enabled=True, environment_base_image=BASE))
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('SKILL.md','---\nname: env-test\ndescription: Generate PDF files\ncapabilities: [pdf.read]\n---\nRead PDF files')
    skill,v=create_version(client,user_headers,slug='prepared-test',package=out.getvalue())
    assert v['environment_preparation']['status']=='queued'
    url=f"/api/v1/skills/{skill['id']}/versions/{v['id']}"
    assert client.post(url+'/submit',headers=user_headers).status_code==409
    with SessionLocal() as session:
        env=session.get(PreparedEnvironment,v['environment_preparation']['digest'])
        env.status='failed';env.error_message='test failure';session.commit()
    response=client.post(url+'/environment/retry',headers=user_headers)
    assert response.status_code==200 and response.json()['environment_preparation']['status']=='queued'
    with SessionLocal() as session:
        env=session.get(PreparedEnvironment,v['environment_preparation']['digest'])
        env.status='ready';env.image_id=BASE;session.commit()
    assert client.post(url+'/submit',headers=user_headers).status_code==200


def test_queued_environment_does_not_consume_worker_attempt(client,user_headers,monkeypatch):
    import io,zipfile
    from app import sandbox_worker
    from app.routers import jobs
    from app.database import SessionLocal
    from app.models import WorkflowJob, AgentRun
    from test_workflow_jobs import create_version
    monkeypatch.setattr(prep,'settings',replace(prep.settings,environment_preparation_enabled=True,environment_base_image=BASE))
    from app import config
    monkeypatch.setattr(config,'settings',replace(config.settings,sandbox_worker_enabled=True))
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('SKILL.md','---\nname: queued-test\ndescription: Generate PDF\ncapabilities: [pdf.read]\n---\nRead PDF')
    _,v=create_version(client,user_headers,slug='queued-env',package=out.getvalue())
    response=client.post('/api/v1/jobs',headers=user_headers,data={'version_id':v['id'],'instruction':'Generate PDF'},files={'file':('input.txt',b'test','text/plain')})
    assert response.status_code==201,response.text
    job_id=response.json()['id']
    assert sandbox_worker._claim_job() is None
    with SessionLocal() as session:
        job=session.get(WorkflowJob,job_id)
        assert job.status.value=='queued'
        assert job.memory.data['prepared_environment_digest']==v['environment_preparation']['digest']
        env=session.get(PreparedEnvironment,v['environment_preparation']['digest'])
        env.status='ready';env.image_id=BASE;session.commit()
    lease=sandbox_worker._claim_job()
    assert lease and lease.job_id==job_id

def test_existing_binding_survives_disabling_new_preparation(db, monkeypatch):
    v=version(db)
    env=prep.bind_version_environment(db,v)
    monkeypatch.setattr(prep,'settings',replace(prep.settings,environment_preparation_enabled=False))
    assert prep.bind_version_environment(db,v) is env
    assert prep.prepare_job_environment(db,[v]) is env


def test_source_analysis_does_not_execute_script():
    import io,zipfile
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('scripts/make.py', "import qrcode\nraise RuntimeError('must not execute')")
        z.writestr('requirements.txt','evil-package @ https://internal.example/private')
    assert prep.package_import_hints(out.getvalue()) == ['image.qr']


def test_revoked_environment_cannot_be_reused(db):
    v=version(db)
    env=prep.bind_version_environment(db,v)
    env.status='revoked'
    with pytest.raises(Exception) as caught:
        prep.prepare_job_environment(db,[v])
    assert caught.value.status_code==409


def test_archive_builder_only_copies_exported_files():
    import io,tarfile
    from app.environment_builder import archive_files
    with tarfile.open(fileobj=io.BytesIO(archive_files({'extension/pkg/module.py':b'pass'}))) as t:
        assert t.extractfile('extension/pkg/module.py').read()==b'pass'
        assert not any(m.issym() or m.islnk() for m in t.getmembers())
def test_extension_export_validates_stream_size_and_content(monkeypatch):
    import tarfile
    from app import environment_builder as builder

    data = builder.archive_files({'extension/pkg/module.py': b'pass'})
    def stream(*args, **kwargs):
        yield data[:100]
        yield data[100:]

    api = SimpleNamespace(exec_create=lambda *args, **kwargs: {'Id': 'export'},
                          exec_start=stream, exec_inspect=lambda execution: {'ExitCode': 0})
    installer = SimpleNamespace(id='installer', client=SimpleNamespace(api=api))
    with builder.export_extension(installer, {'Dockerfile': b'FROM test'}) as archive:
        with tarfile.open(fileobj=archive) as tar:
            assert tar.extractfile('extension/pkg/module.py').read() == b'pass'
            assert tar.extractfile('Dockerfile').read() == b'FROM test'
    monkeypatch.setattr(builder, 'MAX_EXTENSION_BYTES', len(data) - 1)
    with pytest.raises(ValueError, match='environment archive limit'):
        with builder.export_extension(installer, {}):
            pytest.fail('Oversized dependency archive was accepted')
