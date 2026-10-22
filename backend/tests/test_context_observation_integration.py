import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app import artifact_collection, config, sandbox_agent_loop as loop
from app.agent_policy import AgentExecutionState
from app.database import SessionLocal
from app.durable_checkpoint import state_to_json
from app.model_gateway import ModelGatewayError, ModelResult
from app.models import JobEvent, WorkflowJob
from test_agent_policy import _completed_plan
from test_artifact_collection import Sandbox
from test_workflow_endpoints import deploy_endpoint, publish_sandbox_skill


def test_toggle_keeps_exact_model_inputs_and_records_snapshot_and_provider_metadata(
    client, user_headers, owner_headers, monkeypatch,
):
    monkeypatch.setattr(config, 'settings', replace(config.settings, sandbox_worker_enabled=True))
    monkeypatch.setattr(artifact_collection, 'settings', replace(artifact_collection.settings, artifact_delivery_only=True))
    _, version = publish_sandbox_skill(client, user_headers, owner_headers, slug='context-observation')
    endpoint = deploy_endpoint(client, user_headers, version, slug='context-observation-v1')
    response = client.post(f"/api/v1/workflow-endpoints/{endpoint['slug']}/jobs",
                          headers={'X-SkillGo-Key': endpoint['api_key']}, data={'instruction': 'Create report'})
    assert response.status_code == 202
    context = {'root': '/workspace/skills/test', 'name': 'Test', 'version': '1',
               'skill_md': 'Create report', 'runtime_requirements': {}, 'fixed_execution': None}
    requests = []
    fail = False

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000, agent_options={})
        model_name = 'test-private-model'

        async def agent_step(self, **kwargs):
            requests.append(copy.deepcopy(kwargs['messages']))
            if fail:
                raise ModelGatewayError('MODEL_UNAVAILABLE', 'simulated failure')
            return ModelResult(output={'action': 'finish', 'summary': 'Done',
                                       'artifacts': ['/workspace/work/report.txt']},
                               model_name=self.model_name,
                               token_usage={'prompt_tokens': 30, 'completion_tokens': 5,
                                            'prompt_tokens_details': {'cached_tokens': 20}})

    async def save(db, job, sandbox, state, fence):
        job.memory.data = {**job.memory.data, 'durable_checkpoint': {'size': 123, 'status': 'ready'}}

    monkeypatch.setattr(loop, 'DockerSandbox', Sandbox)
    monkeypatch.setattr(loop, 'save_checkpoint', save)
    with SessionLocal() as db:
        job = db.get(WorkflowJob, response.json()['id'])
        for enabled in [False, True, True]:
            monkeypatch.setattr(loop, 'settings', replace(loop.settings, context_observability_enabled=enabled))
            state = AgentExecutionState(skill_count=1, loaded_skills={1}, plan_status_tracking=False)
            assert state.update_plan(_completed_plan())['ok']
            sandbox = Sandbox({'/workspace/work/report.txt': b'report'})
            sandbox.durable_resume = {
                'versions': [version['id']], 'model_name': job.model_name,
                'messages': [{'role': 'system', 'content': 'fixed rules'}, {'role': 'user', 'content': 'request'}],
                'execution_state': state_to_json(state), 'next_turn': 1, 'repeated_action': '',
                'repeat_count': 0, 'recoverable_errors': 0, 'tool_operation_count': 0,
                'singleton_tool_turns': 0, 'fixed_executed': [False], 'memory': {},
            }
            if fail:
                with pytest.raises(ModelGatewayError):
                    asyncio.run(loop._run_agent_loop(db, job, sandbox, skill_contexts=[context],
                                                     gateway=Gateway(), job_cancelled=lambda: False))
            else:
                asyncio.run(loop._run_agent_loop(db, job, sandbox, skill_contexts=[context],
                                                 gateway=Gateway(), job_cancelled=lambda: False))
            event = db.query(JobEvent).filter(JobEvent.job_id == job.id, JobEvent.event_type == 'reasoning').order_by(JobEvent.sequence.desc()).first()
            if enabled:
                assert event.data['turn_timing']['snapshot_status'] == 'saved'
                assert event.data['turn_timing']['snapshot_bytes'] == 123
                assert event.data['turn_timing']['model_call_ms'] >= 0
                if not fail:
                    assert event.data['provider_usage']['input_tokens'] == 30
                    assert event.data['provider_usage']['cache_read_tokens'] == 20
                else:
                    assert event.status == 'failed'
                    assert 'context_observation' in event.data
            else:
                assert 'context_observation' not in event.data
            if enabled:
                fail = True
        assert requests[0] == requests[1] == requests[2]
