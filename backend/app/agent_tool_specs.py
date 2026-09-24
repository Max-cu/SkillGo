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
        'The platform checks exit code and unchanged artifacts, automatically records successful validation, and returns verification_id and validation_recorded. Do not call record_validation after success. '
        'Reuse a suitable existing Skill verifier. For short checks, pass python3 -c and the code directly in argv (each argument must fit 4096 characters); use write_file only for a longer program.')
    tools.append({'type': 'function', 'function': verifier})
    fixed = deepcopy(by_name['read_skill'])
    fixed['name'] = 'run_fixed_skill'
    fixed['description'] = 'Execute the selected Skill immutable entrypoint and verifier. Load it first. Earlier output files are available as inputs. Never reimplement a fixed Skill in model-authored code.'
    tools.append({'type': 'function', 'function': fixed})
    for name, description, properties in [
        ('inspect_image', 'Inspect a generated PNG/JPEG/WebP using the configured vision model. Render document pages to images first. Returns observations, not automatic validation.', {'path': {'type': 'string'}, 'question': {'type': 'string', 'maxLength': 2000}}),
        ('inspect_document', (
            'Understand an uploaded PDF/image on demand. Use intent "structure" (MinerU layout OCR) when you need the actual text WITH positions - '
            'scanned/image-only PDFs, OCR, or in-place bilingual translation/annotation: it returns every block as {type,text,bbox:[x0,y0,x1,y1],page_idx} and a content_path JSON holding the complete list. '
            'Use intent "understand" (vision model) to ask what a rendered PNG/JPEG page looks like. "auto" picks understand for images and structure for PDFs. '
            'Digital PDFs with a real text layer usually do NOT need this: extract text directly with PyMuPDF. Optional pages:[start,end] (0-based) processes a page range. '
            'Results are cached per file/intent/pages.'
        ), {
            'path': {'type': 'string', 'description': 'Absolute path to a PDF/PNG/JPEG/WebP file under /workspace'},
            'intent': {'type': 'string', 'enum': ['structure', 'understand', 'auto'], 'description': 'structure=OCR text+bbox via MinerU; understand=vision description; auto=route by file type'},
            'pages': {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 2, 'maxItems': 2, 'description': 'Optional [start_page, end_page], 0-based inclusive'},
            'question': {'type': 'string', 'maxLength': 2000, 'description': 'Required question for intent understand; ignored for structure'},
        }),
    ]:
        required = ['path'] if name == 'inspect_document' else list(properties)
        tools.append({'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}})
