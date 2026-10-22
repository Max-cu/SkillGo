import copy
import json

import pytest

from app.agent_context import estimate_tokens, project_context
from app.context_observation import observe_context, provider_usage, safe_observe_context


def tool(name, payload, role='tool'):
    return {'role': role, 'tool_call_id': 'call-1',
            'content': json.dumps({'tool_result': name, 'payload': payload}, ensure_ascii=False)}


def projection(messages, budget=100000):
    return project_context(messages, checkpoint='{}', skill_contexts=[], loaded=set(),
                           completed=set(), max_tokens=budget)


def test_observation_measures_projection_without_changing_native_history_or_exposing_content():
    secret = 'secret source and private reasoning 中文'
    file = {'path': '/workspace/private.md', 'content': secret, 'reference': True, 'complete': True}
    messages = [
        {'role': 'system', 'content': secret}, {'role': 'user', 'content': 'Make a report'},
        {'role': 'assistant', 'reasoning_content': secret, 'tool_calls': [
            {'id': 'call-1', 'function': {'name': 'write_file',
                                        'arguments': json.dumps({'path': '/workspace/a', 'content': secret})}}]},
        tool('write_file', {'ok': True, 'path': '/workspace/a'}),
        {'role': 'assistant', 'tool_calls': [{'id': 'call-2', 'function': {'name': 'read_files', 'arguments': '{}'}}]},
        tool('read_files', {'results': [{'tool': 'read_file', 'payload': file},
                                       {'tool': 'read_file', 'payload': file}]}),
    ]
    original = copy.deepcopy(messages)
    projected = projection(messages)
    projected_copy = copy.deepcopy(projected)
    result = observe_context(messages, projected, skill_contexts=[{'skill_md': secret}])
    assert messages == original and projected == projected_copy
    assert projected == projection(messages)
    assert result['scope'] == 'agent_projection_before_gateway'
    assert result['source']['serialized_utf8_bytes'] == len(json.dumps(messages, ensure_ascii=False).encode())
    assert result['model_view']['estimated_tokens'] == estimate_tokens(projected)
    assert result['retained_source_ranges'] == [[0, 5]]
    assert result['omitted_source_ranges'] == []
    assert result['model_view']['identical_file_body_copies'] == 1
    assert result['model_view']['identical_file_body_utf8_bytes'] == len(secret.encode())
    assert result['fixed_skill_text_utf8_bytes'] == len(secret.encode())
    encoded = json.dumps(result, ensure_ascii=False)
    assert secret not in encoded
    assert '/workspace/' not in encoded
    assert 'call-1' not in encoded
    assert 'reasoning_content' not in encoded
    assert len(encoded) < 10000


def test_source_ranges_show_existing_budget_eviction_and_latest_pair_stays_intact():
    messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'request'},
                {'role': 'assistant', 'content': 'old'}, tool('command', {'stdout': 'x' * 20000}),
                {'role': 'assistant', 'content': 'recent'}, tool('command', {'stdout': 'latest'})]
    projected = projection(messages, budget=2000)
    result = observe_context(messages, projected, skill_contexts=[])
    assert result['retained_source_ranges'] == [[0, 1], [4, 5]]
    assert result['omitted_source_ranges'] == [[2, 3]]
    assert result['source_message_count'] == 6
    assert result['retained_source_message_count'] == 4
    assert result['segments']['retained_history']['message_count'] == 2
    assert projected[-3:-1] == messages[-2:]


def test_changed_file_not_counted_as_duplicate_and_legacy_results_are_counted():
    messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'request'},
                {'role': 'assistant', 'content': '{"action":"write_file","content":"abc"}'},
                tool('read_file', {'path': '/workspace/a', 'content': 'version1'}, role='user'),
                tool('read_file', {'path': '/workspace/a', 'content': 'version2'}, role='user'),
                tool('command', {'truncated': True, 'full_result_path': '/workspace/work/large.json'})]
    result = observe_context(messages, projection(messages), skill_contexts=[])
    assert result['model_view']['file_body_count'] == 2
    assert result['model_view']['identical_file_body_copies'] == 0
    assert result['model_view']['offloaded_top_level_result_count'] == 1
    assert result['model_view']['truncated_top_level_result_count'] == 1
    assert result['model_view']['tool_argument_utf8_bytes_by_action']['write_file'] > 0


def test_reference_shelf_and_active_instructions_remain_unchanged():
    state = {'reference_shelf': [{'path': '/workspace/spec.md', 'content': '布局要求', 'sha256': 'hash'}]}
    messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'request'}]
    projected = project_context(messages, checkpoint=json.dumps(state), skill_contexts=[],
                                loaded=set(), completed=set(), max_tokens=10000)
    result = observe_context(messages, projected, skill_contexts=[])
    assert result['reference_shelf_entry_count'] == 1
    assert result['reference_shelf_body_utf8_bytes'] == len('布局要求'.encode())
    assert '布局要求' in projected[-1]['content']


def test_observation_failure_is_metadata_only_and_does_not_raise():
    messages = [{'role': 'tool', 'content': 'do not log this', 'bad': object()}]
    result = safe_observe_context(messages, messages, skill_contexts=[])
    assert result['status'] == 'unavailable'
    assert 'do not log this' not in json.dumps(result)


@pytest.mark.parametrize('usage', [{}, None, {'prompt_tokens': True}, {'prompt_tokens': -1},
                                  {'prompt_tokens': float('nan')}, {'prompt_tokens': 10**1000}])
def test_absent_or_invalid_provider_usage_is_unknown_not_zero(usage):
    result = provider_usage(usage)
    assert result['available'] is False
    assert result['input_tokens'] is None and result['cache_read_tokens'] is None


def test_provider_counts_keep_real_zero_and_cache_semantics():
    result = provider_usage({'prompt_tokens': 200, 'completion_tokens': 0,
                             'prompt_tokens_details': {'cached_tokens': 120},
                             'prompt_cache_miss_tokens': 80, 'unknown': 'private value'})
    assert result['available'] is True
    assert result['input_tokens'] == 200 and result['output_tokens'] == 0
    assert result['cache_read_tokens'] == 120
    assert result['cache_write_tokens'] is None  # Misses are not cache writes.
    assert result['total_tokens'] is None  # Do not invent a provider total.
    assert 'private value' not in json.dumps(result)
    assert provider_usage({'prompt_cache_hit_tokens': 0})['cache_read_tokens'] == 0
    assert provider_usage({'input_tokens': 30, 'cache_creation_input_tokens': 9})['cache_write_tokens'] == 9
