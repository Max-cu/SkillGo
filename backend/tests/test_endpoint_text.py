from dataclasses import replace

from app import config
from app.database import SessionLocal
from app.models import WorkflowJob
from test_workflow_endpoints import publish_sandbox_skill, deploy_endpoint


def test_endpoint_text_without_synthetic_attachment(client, user_headers, owner_headers, monkeypatch):
    monkeypatch.setattr(config, "settings", replace(config.settings, sandbox_worker_enabled=True))
    _, version = publish_sandbox_skill(client, user_headers, owner_headers, slug="text-endpoint")
    endpoint = deploy_endpoint(client, user_headers, version, slug="text-endpoint-v1")
    path = f"/api/v1/workflow-endpoints/{endpoint['slug']}/jobs"
    headers = {"X-SkillGo-Key": endpoint["api_key"], "Idempotency-Key": "text-only-request"}
    assert client.post(path, data={"instruction": "test"}).status_code == 401
    for text in ("", "   "):
        assert client.post(path, headers=headers, data={"instruction": text}).status_code == 422
    assert client.post(path, headers=headers, data={"instruction": "test"},
                       files={"file": ("empty.txt", b"", "text/plain")}).status_code == 422
    created = client.post(path, headers=headers, data={"instruction": "请制作项目汇报 PPT"})
    assert created.status_code == 202, created.text
    job_id = created.json()["id"]
    assert created.json()["status"] == "queued"
    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        assert job.instruction == "请制作项目汇报 PPT"
        assert job.input_files == []
        assert job.prompt_message.content == [{"type": "text", "text": "请制作项目汇报 PPT"}]
    replay = client.post(path, headers=headers, data={"instruction": "请制作项目汇报 PPT"})
    assert replay.status_code == 202
    assert replay.json()["id"] == job_id
    assert replay.headers["x-idempotent-replay"] == "true"
    assert client.get(path + '/' + job_id, headers=headers).status_code == 200
    assert client.post(path + '/' + job_id + '/cancel', headers=headers).status_code == 200
    retry = client.post(f'/api/v1/jobs/{job_id}/retry', headers=user_headers)
    assert retry.status_code == 201, retry.text
    with SessionLocal() as db:
        retried = db.get(WorkflowJob, retry.json()['id'])
        assert retried.input_files == []
        assert retried.instruction == '请制作项目汇报 PPT'
