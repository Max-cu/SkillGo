from copy import deepcopy

from .config import settings


def parallel_subtasks_tool() -> dict:
    """Main-agent tool. Advertised only while parallel subtasks are enabled."""

    task = {
        "type": "object",
        "additionalProperties": False,
        "required": ["task_id", "output_paths"],
        "properties": {
            "task_id": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
                "description": "Stable id for this subtask. The framework does not derive a filename from it.",
            },
            "inputs": {
                "description": "Opaque input bundle for this subtask only. Put skill-specific fields here. The framework does not interpret them.",
            },
            "output_paths": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {"type": "string", "maxLength": 1024},
                "description": "Absolute /workspace files this subtask may write. No other writes are accepted.",
            },
            "readable_paths": {
                "type": "array",
                "maxItems": 16,
                "items": {"type": "string", "maxLength": 1024},
                "description": "Extra files this subtask may read. Shared readable_paths are already visible.",
            },
            "validation_argv": {
                "type": "array",
                "minItems": 1,
                "maxItems": 64,
                "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                "description": "Exact validation command for this subtask. Overrides the batch validation_argv.",
            },
        },
    }
    check = {
        "type": "object",
        "additionalProperties": False,
        "required": ["argv"],
        "properties": {
            "argv": {
                "type": "array",
                "minItems": 1,
                "maxItems": 64,
                "items": {"type": "string", "minLength": 1, "maxLength": 4096},
            },
            "cwd": {"type": "string", "maxLength": 1024},
            "timeout_seconds": {"type": "integer", "minimum": 1},
        },
    }
    return {"type": "function", "function": {
        "name": "run_parallel_subtasks",
        "description": (
            "Fan out independent subtasks inside this job. Each subtask runs in a fresh model loop that sees "
            "only its own inputs, the shared inputs and guidance, the files it may read, the output paths it may write, "
            "and its validation command. The subtask loop does not see this conversation. "
            "Declare any skill-specific contract inside inputs, guidance, and readable_paths; the platform does not interpret those fields. "
            "After every subtask finishes, aggregate_checks run once. They do not run inside each subtask. "
            "A failed subtask does not cancel the others, and each subtask keeps its own model retries. "
            "Retry by calling again with only the failed tasks. Call this tool alone in the turn."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["tasks", "reason"],
            "properties": {
                "tasks": {"type": "array", "minItems": 1, "maxItems": 40, "items": task},
                "guidance": {
                    "type": "string",
                    "maxLength": 80000,
                    "description": "Instructions copied into every subtask loop.",
                },
                "shared_inputs": {
                    "description": "Opaque JSON copied into every subtask loop. The framework does not interpret it.",
                },
                "readable_paths": {
                    "type": "array",
                    "maxItems": 32,
                    "items": {"type": "string", "maxLength": 1024},
                    "description": "Files every subtask may read. Paths must already exist.",
                },
                "validation_argv": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 64,
                    "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "description": "Default validation command. A task validation_argv overrides it. One of the two is required.",
                },
                "aggregate_checks": {
                    "type": "array",
                    "maxItems": 8,
                    "items": check,
                    "description": "Commands run once after all subtasks finish, in order, and only if something was written.",
                },
                "cwd": {"type": "string", "maxLength": 1024},
                "reason": {"type": "string"},
            },
        },
    }}


def sandbox_tools_for_request(tools: list[dict]) -> list[dict]:
    """Tool list actually sent to the model. Parallel subtasks stay hidden while the flag is off."""

    if not settings.parallel_subtasks_enabled:
        return tools
    if any(item.get("function", {}).get("name") == "run_parallel_subtasks" for item in tools):
        return tools
    return [*tools, parallel_subtasks_tool()]


def extend_tools(tools: list[dict]) -> None:
    by_name = {item['function']['name']: item['function'] for item in tools}
    structured=deepcopy(by_name['write_file'])
    structured['name']='write_json'
    structured['description']='Write a structured JSON object to a workspace file. Prefer this for schemas, page plans and configurations: pass data directly, without JSON-inside-a-string escaping. Uses the same plan, path, size and mutation guards as write_file.'
    structured['parameters']['properties'].pop('content')
    structured['parameters']['properties']['data']={'type':'object','additionalProperties':True}
    structured['parameters']['required']=['data' if key=='content' else key for key in structured['parameters']['required']]
    tools.append({'type':'function','function':structured})
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
    verifier['parameters']['properties']['artifacts'] = {
        'type':'array','minItems':1,'maxItems':50,'items':{'type':'string','maxLength':1024},
        'description':'Exact deliverable paths inside /workspace, including Skill exports. The platform collects these before verification and returns canonical delivery paths.'}
    verifier['description'] = ('Run the final read-only verifier. stdout must be exactly JSON with checks: '
        '[{requirement_id:"r1",passed:true,observed:"actual value"}]. Cover every success_criteria as r1, r2, etc. '
        'The platform checks exit code and unchanged artifacts, automatically records successful validation, and returns verification_id and validation_recorded. Do not call record_validation after success. '
        'Include artifacts with the original export paths; the platform collects them before running the verifier. Original sources and collected bytes must remain unchanged. Finish with those same declared paths; do not make manual delivery copies. '
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
