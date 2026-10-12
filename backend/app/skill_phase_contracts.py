"""Conservative stage contracts derived from approved Skill instructions.

Only explicit numbered stage tables with literal script argv are executable.
User uploads, model prose and conditional examples never create a workflow.
Unrecognized Skills retain the ordinary agent tools.
"""
from __future__ import annotations

import json
import re
import shlex
from pathlib import PurePosixPath

_HEADER = re.compile(r'^#{1,6}\s+(?:Step|Phase|阶段)\s+(\d+)\s*[:：]\s*(.+)$', re.I)
_TICK = re.compile(r'`([^`\n]+)`')
_FULL = re.compile(r'全文|读全|都读|完整.*(?:读|加载)|\bin full\b|\bfully\b', re.I)
_LOAD = re.compile(r'加载|\bload\b|\bread\b', re.I)
MAX_STAGE_REFERENCES = 16
MAX_WORKFLOW_COMMANDS = 24


def _reference(value, root, reference_dir):
    if not re.fullmatch(r'[\w./-]+\.md', value, flags=re.UNICODE):
        return None
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or '..' in parsed.parts:
        return None
    if len(parsed.parts) == 1:
        parsed = reference_dir / parsed
    if not parsed.is_relative_to(reference_dir):
        return None
    return str(PurePosixPath(root) / parsed)


def _literal_command(value):
    try:
        argv = shlex.split(value)
    except ValueError:
        return None
    if (len(argv) < 2 or argv[0] not in {'python', 'python3'}
            or not re.fullmatch(r'scripts/[\w./-]+\.py', argv[1])
            or '..' in PurePosixPath(argv[1]).parts):
        return None
    # Placeholder/conditional/shell syntax must be decided by the agent.
    if any(re.search(r'[<>$*{}]|\.\.\.|…', arg) or arg in {';', '&&', '|', '||', '&'} for arg in argv):
        return None
    return ['python3', *argv[1:]]


def stage_contracts(context):
    root = context.get('root', '')
    guide = context.get('skill_md', '')
    if (not isinstance(guide, str) or not isinstance(root,str) or not PurePosixPath(root).is_absolute()
            or '..' in PurePosixPath(root).parts or not PurePosixPath(root).is_relative_to('/workspace')):
        return {'groups': {}, 'required': [], 'workflows': {}}
    sections = []
    fenced = False
    for line in guide.splitlines():
        if re.match(r'^\s*(?:```|~~~)',line):
            fenced = not fenced
            continue
        if fenced:
            continue
        header = _HEADER.match(line)
        if header:
            sections.append({'number': int(header[1]), 'title': header[2], 'lines': []})
        elif sections:
            # Same/higher-level unrelated headings end a stage description.
            if re.match(r'^#{1,3}\s+', line):
                sections[-1]['ended'] = True
            if not sections[-1].get('ended'):
                sections[-1]['lines'].append(line)
    groups, required, fixed = {}, [], []
    for section in sections:
        lines = section['lines']
        primary = None
        for line in lines:
            if _LOAD.search(line) and not re.search(r'不必|无需|按需|optional', line, re.I):
                for value in _TICK.findall(line):
                    if '/' in value and re.fullmatch(r'[\w./-]+\.md', value) and '..' not in PurePosixPath(value).parts:
                        primary = value
                        break
            if primary:
                break
        if (not primary or PurePosixPath(primary).is_absolute()
                or PurePosixPath(primary).parts[0] in {'assets','exports','work','output','tmp'}):
            fixed.append(None)
            continue
        directory = PurePosixPath(primary).parent
        refs = [str(PurePosixPath(root) / primary)]
        for line in lines:
            if _FULL.search(line) and not re.search(r'不必|无需|optional', line, re.I):
                for value in _TICK.findall(line):
                    path = _reference(value, root, directory)
                    if path and path not in refs:
                        refs.append(path)
        if len(refs) > MAX_STAGE_REFERENCES:
            fixed.append(None)
            continue
        groups[refs[0]] = refs
        required.extend(path for path in refs if path not in required)
        commands = []
        for line in lines:
            if not line.startswith('|'):
                continue
            cells = line.split('|')
            if len(cells) < 4 or not re.search(r'\b'+str(section['number'])+r'[a-z]\b', cells[1]):
                continue
            raw_commands = [value for value in _TICK.findall(line) if re.match(r'python(?:3)?\s', value)]
            if (not raw_commands and re.search(r'选择|决定|创作|编写|修改|\bchoose\b|\bdecide\b', cells[2], re.I)
                    and not re.search(r'无需|全自动|no decision', cells[2], re.I)):
                commands = []
                break
            values = [_literal_command(value) for value in raw_commands]
            if raw_commands and (any(value is None for value in values)
                    or re.search(r'\bif\b|\boptional\b|若|仅当|可选|按需|跳过', line, re.I)):
                commands = []
                break
            values = [value for value in values if value]
            if len(values) > 1:
                commands = []
                break
            if values:
                commands.append(values[0])
        # Dynamic creation/research stages never become fixed execution just
        # because their tables contain validators or examples.
        entry = re.search(r'([\w-]+\.py)', section['title'])
        if not entry or not commands or not any(PurePosixPath(c[1]).name == entry[1] for c in commands):
            fixed.append(None)
            continue
        fixed.append({**section, 'references': refs, 'commands': commands})
    # Resolve an explicit trailing "run `name.py`" only if its argv is already
    # declared unambiguously elsewhere in this approved guide (e.g. cleanup).
    known = {}
    for section in fixed:
        if section:
            for command in section['commands']:
                known.setdefault(PurePosixPath(command[1]).name, set()).add(tuple(command))
    for section in fixed:
        if not section:
            continue
        for line in section['lines']:
            if line.startswith('|'):
                continue
            for name in re.findall(r'(?:跑|运行|执行|\brun)\s*`([\w-]+\.py)`', line, re.I):
                candidates = known.get(name, set())
                if len(candidates) == 1:
                    command = list(next(iter(candidates)))
                    if command not in section['commands']:
                        section['commands'].append(command)
    workflows = {}
    if len({section['number'] for section in sections}) != len(sections):
        return {'groups':groups,'required':required,'workflows':{}}
    for position, section in enumerate(fixed):
        if section is None:
            continue
        chain = [section]
        for following in fixed[position+1:]:
            if following is None or following['number'] != chain[-1]['number']+1:
                break
            chain.append(following)
        for end in range(1,len(chain)+1):
            selected=chain[:end]
            commands = [command for stage in selected for command in stage['commands']]
            if len(commands) > MAX_WORKFLOW_COMMANDS:
                continue
            paths = list(dict.fromkeys(path for stage in selected for path in stage['references']))
            if len(paths) > MAX_STAGE_REFERENCES:
                continue
            name = f"steps-{selected[0]['number']}-{selected[-1]['number']}"
            workflows[name] = {'references': paths, 'commands': commands,
                               'stages': [stage['number'] for stage in selected]}
    return {'groups': groups, 'required': required, 'workflows': workflows}


def reference_payloads(body):
    """Recognize complete reference results without rewriting native history."""
    if not isinstance(body, dict) or body.get('tool_result') not in {'read_file', 'read_files', 'run_skill_workflow'}:
        return []
    payload = body.get('payload')
    if not isinstance(payload, dict):
        return []
    values = [payload]
    for item in payload.get('results', []) if isinstance(payload.get('results'), list) else []:
        if isinstance(item, dict) and item.get('tool') == 'read_file':
            values.append(item.get('payload'))
    values.extend(payload.get('phase_references', []) if isinstance(payload.get('phase_references'), list) else [])
    return [value for value in values if isinstance(value, dict)]


def available_reference_paths(messages):
    paths = set()
    for message in messages:
        if not isinstance(message.get('content'), str):
            continue
        if message.get('role') == 'tool':
            try:
                values = reference_payloads(json.loads(message['content']))
            except ValueError:
                continue
            for value in values:
                if (value.get('reference') is True and value.get('complete') is True
                        and isinstance(value.get('content'), str) and isinstance(value.get('path'), str)):
                    paths.add(value['path'])
        elif message['content'].startswith('Execution state and active Skill instructions'):
            try:
                shelf = json.loads(message['content'].split('\n',1)[1])['checkpoint'].get('reference_shelf',[])
            except (ValueError, KeyError, IndexError):
                continue
            for value in shelf:
                if isinstance(value, dict) and isinstance(value.get('content'), str) and isinstance(value.get('path'), str):
                    paths.add(value['path'])
    return paths


def expand_reference_actions(actions, contracts, available):
    result, seen = [], set()
    groups = {path:refs for contract in contracts for path,refs in contract['groups'].items()}
    for action in actions:
        path = action['path']
        if path not in seen:
            result.append(action);seen.add(path)
        for sibling in groups.get(path, []):
            if sibling != path and sibling not in available and sibling not in seen:
                result.append({'action':'read_file','path':sibling,'reason':'Load the complete declared stage requirements'})
                seen.add(sibling)
    return result
