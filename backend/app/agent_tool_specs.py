from copy import deepcopy


def extend_tools(tools: list[dict]) -> None:
    by_name = {item['function']['name']: item['function'] for item in tools}
    plan = by_name['update_plan']['parameters']['properties']['steps']['items']
    for key in ('depends_on', 'input_refs', 'output_refs'):
        plan['properties'][key] = {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 32}
        plan['required'].append(key)
    plan['properties']['skill_index'] = {'type': 'integer', 'minimum': 1}
    validation = by_name['record_validation']['parameters']
    validation['properties']['verification_id'] = {'type': 'string', 'description': 'ID returned by the latest successful run_verifier.'}
    validation['required'].append('verification_id')
    validation['properties']['checks']['maxItems'] = 200
    verifier = deepcopy(by_name['command'])
    verifier['name'] = 'run_verifier'
    verifier['description'] = ('Run the final read-only verifier. stdout must be exactly JSON with checks: '
        '[{requirement_id:"r1",passed:true,observed:"actual value"}]. Cover every success_criteria as r1, r2, etc. '
        'The platform checks exit code and unchanged artifacts and returns verification_id. '
        'Use write_file to prepare the verifier, then this tool to execute it.')
    tools.append({'type': 'function', 'function': verifier})
    fixed = deepcopy(by_name['read_skill'])
    fixed['name'] = 'run_fixed_skill'
    fixed['description'] = 'Execute the selected Skill immutable entrypoint and verifier. Load it first. Earlier output files are available as inputs. Never reimplement a fixed Skill in model-authored code.'
    tools.append({'type': 'function', 'function': fixed})
    for name, description, properties in [
        ('ask_user', 'Pause when a material requirement or input is missing. The sandbox is released; the answer starts a fresh attempt with original inputs and all answers. Ask before expensive work. Never fabricate a required business parameter.', {'question': {'type': 'string', 'maxLength': 2000}}),
        ('inspect_image', 'Inspect a generated PNG/JPEG/WebP using the configured vision model. Render document pages to images first. Returns observations, not automatic validation.', {'path': {'type': 'string'}, 'question': {'type': 'string', 'maxLength': 2000}}),
    ]:
        tools.append({'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}}})
