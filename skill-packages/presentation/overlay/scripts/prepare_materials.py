#!/usr/bin/env python3
"""Package immutable uploaded files without assuming Unix mtimes are >= 1980."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile
import shutil


def prepare(source, target):
    source=Path(source).resolve();target=Path(target).resolve()
    files=sorted(path for path in source.iterdir() if path.is_file() and not path.is_symlink()) if source.is_dir() else [source]
    if not files or any(not path.is_file() or path.is_symlink() for path in files):
        raise ValueError('Provide actual uploaded files; missing inputs cannot be replaced with invented material.')
    if target in files or (source.is_dir() and target.is_relative_to(source)):
        raise ValueError('Write the material archive outside the immutable input directory.')
    target.parent.mkdir(parents=True,exist_ok=True)
    if len(files)==1 and files[0].suffix.lower()=='.zip':
        shutil.copyfile(files[0],target)
    else:
        with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED,strict_timestamps=False) as archive:
            for path in files: archive.write(path,path.name)
    return {'ok':True,'archive':str(target),'files':[{'name':p.name,'bytes':p.stat().st_size,
            'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source');parser.add_argument('target')
    args=parser.parse_args()
    try: result=prepare(args.source,args.target)
    except (OSError,ValueError) as exc: result={'ok':False,'message':str(exc)}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['ok'] else 1


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
