"""Content-free diagnostics for the existing context projection, never a reducer."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import logging
import math
from typing import Any

from .agent_context import estimate_tokens
from .skill_phase_contracts import reference_payloads

logger = logging.getLogger(__name__)
_ROLES = {'system', 'user', 'assistant', 'tool'}
_ACTIONS = {
    'read_file', 'read_files', 'read_skill', 'write_file', 'write_json', 'command',
    'run_python', 'run_commands', 'run_skill_workflow', 'list_files', 'update_plan',
    'finish', 'block', 'run_verifier', 'record_validation', 'complete_skill',
    'inspect_image', 'request_capability', 'request_python_dependencies',
}


def _encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


def _object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            pass
    return {}


def _action(value: Any) -> str:
    return value if isinstance(value, str) and value in _ACTIONS else 'other'


def _message_stats(messages: list[dict]) -> dict:
    roles = Counter()
    role_bytes = Counter()
    result_bytes = Counter()
    argument_bytes = Counter()
    seen_files = set()
    file_bodies = duplicate_bodies = duplicate_bytes = file_bytes = 0
    truncated = offloaded = 0
    largest = []
    for index, message in enumerate(messages):
        role = message.get('role') if message.get('role') in _ROLES else 'other'
        size = len(_encoded(message))
        roles[role] += 1
        role_bytes[role] += size
        largest.append({'message_index': index, 'role': role, 'utf8_bytes': size})
        if role == 'assistant':
            for call in message.get('tool_calls') or []:
                function = call.get('function') or {}
                args = function.get('arguments', {})
                name = _action(function.get('name'))
                argument_bytes[name] += len(args.encode('utf-8')) if isinstance(args, str) else len(_encoded(args))
            # Legacy JSON mode stores actions in assistant content, not tool_calls.
            if not message.get('tool_calls'):
                action = _object(message.get('content'))
                if action.get('action'):
                    argument_bytes[_action(action.get('action'))] += len(_encoded(action))
        if role not in {'tool', 'user'}:
            continue
        body = _object(message.get('content'))
        if 'tool_result' not in body:
            continue
        name = _action(body.get('tool_result'))
        result_bytes[name] += len(_encoded(body))
        payload = body.get('payload')
        if isinstance(payload, dict):
            truncated += int(payload.get('truncated') is True)
            offloaded += int(isinstance(payload.get('full_result_path'), str))
        for payload in reference_payloads(body):
            content, path = payload.get('content'), payload.get('path')
            if not isinstance(content, str) or not isinstance(path, str):
                continue
            raw = content.encode('utf-8')
            file_bodies += 1
            file_bytes += len(raw)
            key = (path, hashlib.sha256(raw).digest())
            if key in seen_files:
                duplicate_bodies += 1
                duplicate_bytes += len(raw)
            seen_files.add(key)
    raw = _encoded(messages)
    return {
        'message_count': len(messages), 'serialized_utf8_bytes': len(raw),
        'estimated_tokens': estimate_tokens(messages),
        'messages_by_role': dict(roles), 'message_utf8_bytes_by_role': dict(role_bytes),
        'tool_result_utf8_bytes_by_action': dict(result_bytes),
        'tool_argument_utf8_bytes_by_action': dict(argument_bytes),
        'file_body_count': file_bodies, 'file_body_utf8_bytes': file_bytes,
        'identical_file_body_copies': duplicate_bodies,
        'identical_file_body_utf8_bytes': duplicate_bytes,
        'truncated_top_level_result_count': truncated,
        'offloaded_top_level_result_count': offloaded,
        'largest_messages': sorted(largest, key=lambda item: item['utf8_bytes'], reverse=True)[:5],
    }


def _ranges(indices: list[int]) -> list[list[int]]:
    result = []
    for index in indices:
        if result and index == result[-1][1] + 1:
            result[-1][1] = index
        else:
            result.append([index, index])
    return result


def observe_context(source: list[dict], projected: list[dict], *, skill_contexts: list[dict]) -> dict:
    """Measure without mutating messages or persisting their content/paths/hashes."""
    positions = defaultdict(list)
    for index, message in enumerate(source):
        positions[id(message)].append(index)
    retained = []
    for message in projected:
        candidates = positions.get(id(message))
        if candidates:
            retained.append(candidates.pop(0))
    retained_set = set(retained)
    kept_ranges = _ranges(sorted(retained_set))
    omitted_ranges = _ranges([index for index in range(len(source)) if index not in retained_set])
    system = '\n'.join(m.get('content', '') for m in projected[:2]
                       if m.get('role') == 'system' and isinstance(m.get('content'), str))
    fixed_skills = {item['skill_md'] for item in skill_contexts
                    if isinstance(item.get('skill_md'), str) and item['skill_md'] in system}
    memory = _object(str(projected[-1].get('content', '')).partition('\n')[2]) if projected else {}
    shelf = memory.get('checkpoint', {}).get('reference_shelf', [])
    segments = {
        'fixed_prefix': projected[:min(2, len(source))],
        'retained_history': projected[min(2, len(source)):-1],
        'execution_state': projected[-1:] if projected else [],
    }
    return {
        'schema_version': 1, 'scope': 'agent_projection_before_gateway',
        'estimator': 'utf8_json_bytes_div_2',
        'source': _message_stats(source), 'model_view': _message_stats(projected),
        'segments': {name: {'message_count': len(items), 'serialized_utf8_bytes': len(_encoded(items)),
                            'estimated_tokens': estimate_tokens(items)} for name, items in segments.items()},
        'fixed_skill_text_utf8_bytes': sum(len(text.encode('utf-8')) for text in fixed_skills),
        'reference_shelf_entry_count': len(shelf),
        'reference_shelf_body_utf8_bytes': sum(len(entry['content'].encode('utf-8')) for entry in shelf
                                              if isinstance(entry.get('content'), str)),
        'retained_source_ranges': kept_ranges[:64], 'omitted_source_ranges': omitted_ranges[:64],
        'range_listing_truncated': len(kept_ranges) > 64 or len(omitted_ranges) > 64,
        'source_message_count': len(source), 'retained_source_message_count': len(retained_set),
    }


def safe_observe_context(*args, **kwargs) -> dict:
    try:
        return observe_context(*args, **kwargs)
    except Exception as exc:
        # Observation must not fail a task or expose a payload through exception text.
        logger.warning('Context observation unavailable (%s)', type(exc).__name__)
        return {'schema_version': 1, 'status': 'unavailable', 'error_type': type(exc).__name__}


def provider_usage(usage: Any) -> dict:
    """Known provider numeric fields only; missing values remain unknown, never 0."""
    usage = usage if isinstance(usage, dict) else {}
    fields = {
        'input_tokens': [('prompt_tokens',), ('input_tokens',)],
        'output_tokens': [('completion_tokens',), ('output_tokens',)],
        'total_tokens': [('total_tokens',)],
        'cache_read_tokens': [('prompt_cache_hit_tokens',), ('cache_read_input_tokens',),
                              ('prompt_tokens_details', 'cached_tokens'), ('input_tokens_details', 'cached_tokens')],
        'cache_write_tokens': [('cache_creation_input_tokens',)],
    }
    result = {'scope': 'gateway_result_usage', 'source': 'provider_reported', 'available': False}
    for name, candidates in fields.items():
        result[name] = None
        for path in candidates:
            value = usage
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and 0 <= value <= 2**63 - 1 and math.isfinite(value)):
                result[name] = value
                result[name + '_field'] = '.'.join(path)
                result['available'] = True
                break
    return result
