"""Task-local, content-addressed observations; never verification proofs."""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import time
from pathlib import PurePosixPath

from .sandbox_runtime import SandboxRuntimeError


class VisualInspectionCache:
    def __init__(self, max_entries=32):
        self.entries = OrderedDict()
        self.max_entries = max_entries

    async def inspect(self, sandbox, gateway, *, path, question):
        media = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}.get(PurePosixPath(path).suffix.lower())
        if not media:
            raise SandboxRuntimeError('IMAGE_FORMAT_UNSUPPORTED', 'Render a PNG/JPEG/WebP file first')
        data = sandbox.read_workspace_file(path)
        if len(data) > 10 * 1024 * 1024:
            raise SandboxRuntimeError('IMAGE_TOO_LARGE', 'Image exceeds 10 MiB')
        digest = hashlib.sha256(data).hexdigest()
        question_digest = hashlib.sha256(question.encode('utf-8')).hexdigest()
        # The gateway configuration is fixed for this task. Resolve before reuse.
        vision = gateway.for_capability('vision')
        key = (digest, media, question_digest, vision.model_name)
        if key in self.entries:
            self.entries.move_to_end(key)
            return {**deepcopy(self.entries[key]), 'path': path, 'cached': True, 'vision_duration_ms': 0}
        started = time.monotonic()
        visual = await vision.analyze_image(data=data, media_type=media, prompt=question, purpose='vision')
        payload = {'ok': True, 'path': path, 'sha256': digest, 'question_sha256': question_digest,
                   'observation': visual.output, 'model_name': visual.model_name, 'cached': False,
                   'image_bytes': len(data), 'vision_duration_ms': int((time.monotonic()-started)*1000)}
        self.entries[key] = deepcopy(payload)
        while len(self.entries) > self.max_entries:
            self.entries.popitem(last=False)
        return payload
