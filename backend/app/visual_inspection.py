"""Task-local, content-addressed observations; never verification proofs."""
import json
from collections import OrderedDict
from copy import deepcopy
import hashlib
import time
from pathlib import PurePosixPath

from .model_gateway import ModelGatewayError
from .sandbox_runtime import SandboxRuntimeError


IMAGE_MEDIA = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
DOCUMENT_MEDIA = {'.pdf': 'application/pdf', **IMAGE_MEDIA}
# Structured document parsing can upload a larger file than a single image check.
DOCUMENT_MAX_BYTES = 30 * 1024 * 1024
# Blocks echoed inline alongside the persisted JSON; the full list stays on disk.
INLINE_BLOCK_SAMPLE = 20


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

    async def inspect_document(
        self,
        sandbox,
        gateway,
        *,
        path: str,
        intent: str = 'auto',
        pages: tuple[int, int] | None = None,
        question: str = '',
    ) -> dict:
        """Understand a document/image on demand.

        intent:
          structure   -> MinerU layout OCR, returns text blocks WITH bbox;
                         the agent decides when real text extraction/placement
                         is needed (scanned PDFs, in-place translation).
          understand  -> vision model describing a rendered image.
          auto        -> images use understand; PDFs use structure.
        The heavy block list is persisted to /workspace/work; only a compact
        summary plus a small sample enters the model context.
        """

        suffix = PurePosixPath(path).suffix.lower()
        media = DOCUMENT_MEDIA.get(suffix)
        if not media:
            raise SandboxRuntimeError(
                'DOCUMENT_FORMAT_UNSUPPORTED',
                'inspect_document supports PDF, PNG, JPEG and WebP files',
            )
        resolved_intent = intent
        if intent == 'auto':
            resolved_intent = 'understand' if media in IMAGE_MEDIA.values() else 'structure'

        data = sandbox.read_workspace_file(path)
        if len(data) > DOCUMENT_MAX_BYTES:
            raise SandboxRuntimeError('DOCUMENT_TOO_LARGE', f'Document exceeds {DOCUMENT_MAX_BYTES // 1024 // 1024} MiB')
        digest = hashlib.sha256(data).hexdigest()
        page_key = tuple(pages) if pages else ()
        key = ('document', digest, media, resolved_intent, page_key)
        if key in self.entries:
            self.entries.move_to_end(key)
            return {**deepcopy(self.entries[key]), 'path': path, 'cached': True}

        started = time.monotonic()
        if resolved_intent == 'understand':
            if media not in IMAGE_MEDIA.values():
                raise SandboxRuntimeError(
                    'DOCUMENT_RENDER_FIRST',
                    'Vision understanding needs an image. Render the PDF page to PNG with PyMuPDF and inspect that image.',
                )
            vision = gateway.for_capability('vision')
            visual = await vision.analyze_image(
                data=data, media_type=media,
                prompt=question.strip() or '请准确描述该页面的版面、内容和需要注意的细节。',
                purpose='vision',
            )
            payload = {
                'ok': True, 'intent': 'understand', 'path': path, 'sha256': digest,
                'mode': 'vision', 'model_name': visual.model_name,
                'observation': visual.output,
                'duration_ms': int((time.monotonic() - started) * 1000),
            }
        else:
            ocr_gateway = gateway.for_capability('ocr')
            if ocr_gateway.connection.api_format != 'mineru':
                raise ModelGatewayError(
                    'DOCUMENT_STRUCTURE_BACKEND_UNAVAILABLE',
                    '结构化版面解析（带坐标）需要配置 MinerU 文件解析连接',
                )
            result = await ocr_gateway.parse_document_mineru(
                data=data, media_type=media,
                pages=pages if pages else None,
            )
            output = result.output
            blocks = output.get('blocks') or []
            report_path = f'/workspace/work/document_inspection/{digest[:16]}.json'
            persisted = {
                'source_path': path,
                'sha256': digest,
                'page_count': output.get('page_count'),
                'block_types': output.get('block_types'),
                'markdown': output.get('markdown', ''),
                'blocks': blocks,
            }
            await sandbox.write_text(report_path, json.dumps(persisted, ensure_ascii=False))
            payload = {
                'ok': True, 'intent': 'structure', 'path': path, 'sha256': digest,
                'mode': 'document_structure', 'model_name': result.model_name,
                'content_path': report_path,
                'page_count': output.get('page_count', 0),
                'block_count': output.get('block_count', 0),
                'block_types': output.get('block_types', {}),
                'blocks': blocks[:INLINE_BLOCK_SAMPLE],
                'hint': (
                    '完整文本块（含每个块的 bbox [x0,y0,x1,y1]、page_idx、type、text）在 content_path，'
                    '用 read_file 或 run_python 读取后再做翻译/原位标注；本次返回仅含前 '
                    f'{min(len(blocks), INLINE_BLOCK_SAMPLE)} 个样例。'
                ),
                'duration_ms': int((time.monotonic() - started) * 1000),
            }

        self.entries[key] = deepcopy(payload)
        while len(self.entries) > self.max_entries:
            self.entries.popitem(last=False)
        return payload

