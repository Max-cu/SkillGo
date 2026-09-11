"""Executed with python -I inside the task sandbox, before any Agent operation.
Probe code is platform owned; do not accept executable probe definitions from Skills.
"""
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys

# Small functional checks, not only import discovery. No network or user files.
PROBES = {
    "pymupdf": "import pymupdf; d=pymupdf.open(); p=d.new_page(); p.insert_text((20,20),'probe'); assert 'probe' in p.get_text(); assert p.get_pixmap().width > 0; assert d.tobytes()",
    "pypdf": "from pypdf import PdfWriter,PdfReader; import io; w=PdfWriter(); w.add_blank_page(100,100); b=io.BytesIO(); w.write(b); b.seek(0); assert len(PdfReader(b).pages)==1",
    "pdfplumber": "import pdfplumber; assert callable(pdfplumber.open)",
    "reportlab": "from reportlab.pdfgen.canvas import Canvas; import io; b=io.BytesIO(); c=Canvas(b); c.drawString(10,10,'probe'); c.save(); assert b.getvalue().startswith(b'%PDF')",
    "python-docx": "from docx import Document; import io; d=Document(); d.add_paragraph('probe'); b=io.BytesIO(); d.save(b); b.seek(0); assert Document(b).paragraphs[0].text=='probe'",
    "openpyxl": "from openpyxl import Workbook; import io; w=Workbook(); w.active['A1']=1; w.save(io.BytesIO())",
    "python-pptx": "from pptx import Presentation; import io; Presentation().save(io.BytesIO())",
    "Pillow": "from PIL import Image; assert Image.new('RGB',(2,2)).size==(2,2)",
    "pandas": "import pandas as pd; assert pd.DataFrame({'x':[1,2]}).x.sum()==3",
}

def probe():
    providers = {}
    imports = {'pymupdf': ['pymupdf', 'fitz'], 'pypdf': ['pypdf'], 'pdfplumber': ['pdfplumber'],
               'reportlab': ['reportlab'], 'python-docx': ['docx'], 'openpyxl': ['openpyxl'],
               'python-pptx': ['pptx'], 'Pillow': ['PIL'], 'pandas': ['pandas']}
    for package, code in PROBES.items():
        try:
            version = importlib.metadata.version(package)
            result = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, timeout=6)
            providers[package] = {'available': result.returncode == 0, 'version': version,
                                  'probe': 'passed' if result.returncode == 0 else 'failed'}
        except importlib.metadata.PackageNotFoundError:
            providers[package] = {'available': False, 'probe': 'not_installed'}
        except (OSError, subprocess.TimeoutExpired):
            providers[package] = {'available': False, 'probe': 'timeout'}
        providers[package]['imports'] = imports[package]
    for name in ('pdftoppm', 'libreoffice', 'ffmpeg'):
        path = shutil.which(name)
        try:
            result = subprocess.run([path, '-v' if name == 'pdftoppm' else '--version' if name == 'libreoffice' else '-version'], capture_output=True, timeout=6) if path else None
            providers[name] = {'available': bool(result and result.returncode == 0), 'path': path, 'probe': 'version_command'}
        except (OSError, subprocess.TimeoutExpired):
            providers[name] = {'available': False, 'path': path, 'probe': 'failed'}
    fonts = []
    if shutil.which('fc-match'):
        for family in ('Noto Sans CJK SC', 'Noto Serif CJK SC'):
            try:
                result = subprocess.run(['fc-match', '-f', '%{family}|%{file}\n', family], capture_output=True, text=True, timeout=3)
                for line in result.stdout.splitlines():
                    if '|' in line:
                        actual, path = line.split('|', 1)
                        if family in actual:
                            fonts.append({'family': actual, 'path': path})
            except subprocess.TimeoutExpired:
                pass
    providers['fonts.cjk'] = {'available': bool(fonts), 'fonts': fonts, 'probe': 'fontconfig_match'}
    return {'schema_version': 1, 'python': platform.python_version(), 'architecture': platform.machine(), 'providers': providers}

if __name__ == '__main__':
    print(json.dumps(probe(), ensure_ascii=False))
