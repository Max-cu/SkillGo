"""Apply the maintained overlay to the exact supplied vendor package."""
import argparse
import hashlib
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import zipfile

ORIGINAL_SHA256 = '46a33c26804316da41a39236d77dbb378e02f7b732f4861e908cbeab3105d86c'
ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('original_zip', type=Path)
    parser.add_argument('output_zip', type=Path)
    args = parser.parse_args()
    original = args.original_zip.resolve()
    if hashlib.sha256(original.read_bytes()).hexdigest() != ORIGINAL_SHA256:
        parser.error('Original package hash differs; review its contracts before applying this overlay.')
    with tempfile.TemporaryDirectory(prefix='presentation-package-') as temporary:
        stage = Path(temporary)
        with zipfile.ZipFile(original) as archive:
            roots = set()
            for entry in archive.infolist():
                path = PurePosixPath(entry.filename)
                if ('\\' in entry.filename or path.is_absolute() or '..' in path.parts
                        or not path.parts
                        or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                    parser.error('Unsafe or unexpected vendor package entry.')
                roots.add(path.parts[0])
            if len(roots) != 1:
                parser.error('Expected one reviewed Skill package root.')
            archive.extractall(stage)
        skill = stage / roots.pop()
        if not (skill / 'SKILL.md').is_file():
            parser.error('Reviewed package root must contain SKILL.md.')
        shutil.copytree(ROOT / 'overlay', skill, dirs_exist_ok=True)
        return subprocess.call([sys.executable, '-X', 'utf8', str(skill / 'scripts/cleaners/pack_skill.py'),
                                '-o', str(args.output_zip.resolve())])


if __name__ == '__main__':
    raise SystemExit(main())
