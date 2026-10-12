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
    with tempfile.TemporaryDirectory(prefix='tcc-package-') as temporary:
        stage = Path(temporary)
        with zipfile.ZipFile(original) as archive:
            for entry in archive.infolist():
                path = PurePosixPath(entry.filename)
                if ('\\' in entry.filename or path.is_absolute() or '..' in path.parts
                        or not path.parts or path.parts[0] != 'tcc-ppt-skill-v4'
                        or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                    parser.error('Unsafe or unexpected vendor package entry.')
            archive.extractall(stage)
        skill = stage / 'tcc-ppt-skill-v4'
        shutil.copytree(ROOT / 'overlay', skill, dirs_exist_ok=True)
        return subprocess.call([sys.executable, '-X', 'utf8', str(skill / 'scripts/cleaners/pack_skill.py'),
                                '-o', str(args.output_zip.resolve())])


if __name__ == '__main__':
    raise SystemExit(main())
