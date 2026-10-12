from copy import deepcopy


def extend_tools(tools: list[dict]) -> None:
    by_name = {item['function']['name']: item['function'] for item in tools}
    for name, single, field, description in (
        ('read_files', 'read_file', 'files',
         'Read 1-8 known text files in one call. Prefer this for related Skill references. '
         'Each file retains its completeness metadata. Call this tool alone in the turn.'),
        ('run_commands', 'command', 'commands',
         'Run 1-8 already-decided commands in order after a valid plan exists. '
         'Stops on the first failure and returns every executed result plus skipped indexes. '
         'Use only when later argv do not depend on unseen results. Skill checks remain required. '
         'Call this tool alone; never rerun successful commands blindly after a failure.'),
    ):
        item = deepcopy(by_name[single]['parameters'])
        item['required'] = [key for key in item.get('required', []) if key != 'reason']
        tools.append({'type': 'function', 'function': {
            'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': {
                field: {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': item},
                'reason': {'type': 'string', 'description': 'Short description of this batch.'},
            }, 'required': [field, 'reason'], 'additionalProperties': False},
        }})
    by_name['finish']['description'] = (
        'Finish after fulfilling the task and the applicable platform acceptance policy. '
        'Declare exact existing regular files inside /workspace, including Skill exports directories. '
        'The platform safely collects declared files into its delivery area; no manual output-directory copy is required.'
    )
    tools.append({'type':'function','function':{
        'name':'run_skill_workflow',
        'description': ('Run an available immutable workflow declared in the approved Skill stage catalog. '
            'After creating its required inputs, prefer this over deciding each known build/export script again. '
            'The framework loads complete stage references, runs all declared commands/checks in order, '
            'and stops on the first failure. Successful commands are not replayed on a retry. '
            'Set restart=true only after upstream inputs changed and the whole workflow really must rerun. '
            'After a failure, inspect diagnostics and perform recovery before explicitly setting retry_failed=true. '
            'Call alone; inspect failure diagnostics before retrying, especially exports that may already exist.'),
        'parameters': {'type':'object','properties': {
            'skill_index':{'type':'integer','minimum':1},
            'workflow_id':{'type':'string','description':'Exact ID from the Skill stage catalog, e.g. steps-3-5'},
            'restart':{'type':'boolean','description':'Explicitly restart after upstream changes; omitted defaults to false'},
            'retry_failed':{'type':'boolean','description':'Explicitly retry the failed command after diagnosing/correcting its cause; never blindly retry an export'},
            'reason':{'type':'string'}},'required':['skill_index','workflow_id','reason'],'additionalProperties':False}}})
    by_name['update_plan']['description'] = ('Create the initial execution outline with 2-8 steps and success criteria. '
        'Only revise it when the workflow or requirements change. Do not call this tool to tick steps or update statuses; '
        'new tasks do not track plan completion statuses.')
    plan = by_name['update_plan']['parameters']['properties']['steps']['items']
    for key in ('depends_on', 'input_refs', 'output_refs'):
        plan['properties'][key] = {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 32}
        if key in ('input_refs', 'output_refs'):
            plan['properties'][key]['items']['description'] = (
                'Exact absolute path under /workspace. Resolve Skill-relative examples '
                'against that Skill Root. Use a produced directory for a file batch, not a glob.'
            )
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
