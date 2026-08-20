from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


ALLOWED_KEYS = {
    "SKILLGO_MODEL_BASE_URL",
    "SKILLGO_MODEL_API_KEY",
    "SKILLGO_MODEL_NAME",
    "SKILLGO_MODEL_JSON_MODE",
    "SKILLGO_MODEL_NATIVE_TOOLS",
    "SKILLGO_MODEL_TLS_VERIFY",
}


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: update-model-env.py ENV_FILE")

    env_path = Path(sys.argv[1]).resolve()
    updates = json.load(sys.stdin)
    if not isinstance(updates, dict) or set(updates) != ALLOWED_KEYS:
        raise SystemExit("invalid model configuration keys")
    if not all(isinstance(value, str) and value for value in updates.values()):
        raise SystemExit("model configuration values must be non-empty strings")

    existing = env_path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    written: set[str] = set()
    for line in existing:
        key = line.split("=", 1)[0]
        if key in updates:
            output.append(f"{key}={updates[key]}")
            written.add(key)
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in updates.items() if key not in written)

    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=env_path.parent,
        prefix=f".{env_path.name}.",
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(output) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, env_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


if __name__ == "__main__":
    main()
