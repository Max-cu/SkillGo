"""Data-only Python dependency requests; package names are not a platform allowlist."""
import ast
import io
import re
import shlex
import sys
import zipfile
from pathlib import PurePosixPath
from packaging.requirements import Requirement, InvalidRequirement
from packaging.utils import canonicalize_name
from .skill_metadata import parse_skill_frontmatter

# Import/distribution spelling corrections, not an installation allowlist.
IMPORT_PACKAGES = {'cv2':'opencv-python-headless','skimage':'scikit-image','sklearn':'scikit-learn',
 'PIL':'Pillow','fitz':'PyMuPDF','pymupdf':'PyMuPDF','docx':'python-docx','pptx':'python-pptx',
 'yaml':'PyYAML','bs4':'beautifulsoup4','dateutil':'python-dateutil','barcode':'python-barcode'}


def normalize_requirements(values):
    if not isinstance(values, list) or len(values)>64:
        raise ValueError('Python dependencies must be an array of at most 64 package requirements')
    result=set()
    for value in values:
        if not isinstance(value,str) or not 1<=len(value)<=200 or any(c in value for c in '\r\n\\'):
            raise ValueError('Invalid Python dependency')
        try: r=Requirement(value)
        except InvalidRequirement as exc: raise ValueError('Invalid Python dependency: '+value[:80]) from exc
        if r.url or r.marker:
            raise ValueError('Use package names and versions only; URLs, paths and markers are not accepted')
        extras='['+','.join(sorted(r.extras))+']' if r.extras else ''
        result.add(canonicalize_name(r.name)+extras+str(r.specifier))
    return sorted(result)


def normalize_imports(values):
    if not isinstance(values,list) or len(values)>64 or any(not isinstance(v,str) or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*',v) or len(v)>120 for v in values):
        raise ValueError('Imports must be at most 64 Python module names')
    return sorted(set(values))


def analyze_python_dependencies(skill_md, manifest, package=None):
    requirements=[]; imports=set(); evidence=[]; requirements_file_requested=False
    front=parse_skill_frontmatter(skill_md)
    spec=manifest.get('spec',{})
    for source in (front,spec if isinstance(spec,dict) else {}):
        values=source.get('python_dependencies',[])
        requirements.extend(normalize_requirements(values))
        evidence.extend({'requirement':v,'source':'declaration'} for v in values)
    for number,line in enumerate(skill_md.splitlines(),1):
        if re.match(r'^\s*(?:from|import)\s+',line):
            try: tree=ast.parse(line.strip())
            except SyntaxError: continue
            for node in ast.walk(tree):
                if isinstance(node,ast.Import): imports.update(a.name.split('.')[0] for a in node.names)
                elif isinstance(node,ast.ImportFrom) and node.level==0 and node.module: imports.add(node.module.split('.')[0])
        # Markdown quotes/whole-line code spans are presentation, not shell
        # syntax. Recommended combined install commands often live here.
        install_line=re.sub(r'^\s*(?:>\s*)+', '', line).strip()
        if install_line.startswith('`') and install_line.endswith('`') and install_line.count('`')==2:
            install_line=install_line[1:-1]
        match=re.match(r'^\s*(?:\$\s*)?(?:python(?:3)?\s+-m\s+)?pip(?:3)?\s+install\s+(.+)$',install_line)
        if match:
            # Read declarative package arguments only; never run the command.
            args=shlex.split(match.group(1),comments=True)
            packages=[]
            index=0
            while index<len(args):
                value=args[index]
                if value in ('-U','--upgrade','--no-cache-dir'):
                    index+=1
                    continue
                if value in ('-i','--index-url','--extra-index-url'):
                    # Manual mirror hints never change the builder's trusted
                    # public-PyPI resolver. Extract package names only.
                    if index+1>=len(args):
                        raise ValueError('Missing index URL after '+value)
                    index+=2
                    continue
                if value.startswith(('--index-url=','--extra-index-url=')):
                    if not value.split('=',1)[1]:
                        raise ValueError('Missing index URL after '+value.split('=',1)[0])
                    index+=1
                    continue
                if value in ('-r','--requirement'):
                    index+=1
                    if index>=len(args):
                        raise ValueError('Missing requirements file after '+value)
                    reference=args[index]
                elif value.startswith('--requirement='):
                    reference=value.split('=',1)[1]
                elif value.startswith('-r'):
                    reference=value[2:]
                else:
                    packages.append(value)
                    index+=1
                    continue
                if reference not in ('requirements.txt','./requirements.txt'):
                    raise ValueError('Only requirements.txt beside SKILL.md is supported for pip -r examples')
                requirements_file_requested=True
                index+=1
            args=packages
            requirements.extend(normalize_requirements(args))
            evidence.extend({'requirement':v,'source':'install_example','line':number} for v in args)
    if package:
        requirements_file_found=False
        with zipfile.ZipFile(io.BytesIO(package)) as z:
            roots={str(PurePosixPath(i.filename).parent) for i in z.infolist() if PurePosixPath(i.filename).name=='SKILL.md'}
            for i in z.infolist():
                if PurePosixPath(i.filename).name=='requirements.txt' and str(PurePosixPath(i.filename).parent) in roots:
                    requirements_file_found=True
                    if i.file_size>32768: raise ValueError('requirements.txt exceeds 32 KiB')
                    values=[s.split('#',1)[0].strip() for s in z.read(i).decode('utf-8-sig').splitlines()]
                    values=[s for s in values if s]
                    requirements.extend(normalize_requirements(values))
                    evidence.extend({'requirement':v,'source':'requirements.txt'} for v in values)
        if requirements_file_requested and not requirements_file_found:
            raise ValueError('requirements.txt referenced by pip install is missing beside SKILL.md')
    imports-=sys.stdlib_module_names
    declared_names={canonicalize_name(Requirement(r).name) for r in requirements}
    for module in sorted(imports):
        name=IMPORT_PACKAGES.get(module,module)
        if canonicalize_name(name) not in declared_names:
            requirements.append(name)
            evidence.append({'requirement':name,'source':'import_inference','module':module})
    return {'python_requirements':normalize_requirements(requirements), 'python_imports':normalize_imports(sorted(imports)),
            'python_evidence':evidence, 'limitations':'Import names can differ from distribution names; unresolved or incompatible dependencies fail visibly and can be corrected.'}


def requirements_satisfied(requirements, inventory):
    installed={canonicalize_name(n):v for n,v in inventory.get('python_distributions',{}).items()}
    return all(not (r:=Requirement(value)).extras and canonicalize_name(r.name) in installed
               and r.specifier.contains(installed[canonicalize_name(r.name)],prereleases=True) for value in requirements)
