"""Pack this skill directory into an uploadable zip."""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_zip", type=Path)
    args = parser.parse_args()
    script = ROOT / "scripts" / "cleaners" / "pack_skill.py"
    return subprocess.call(
        [sys.executable, "-X", "utf8", str(script), "-o", str(args.output_zip.resolve())]
    )


if __name__ == "__main__":
    raise SystemExit(main())
