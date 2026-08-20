from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def read_contract(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("contract must be a JSON object")
    message = payload.get("message")
    if not isinstance(message, str) or not message:
        raise ValueError("message must be a non-empty string")
    if len(message) > 1024:
        raise ValueError("message exceeds 1024 characters")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", required=True)
    args = parser.parse_args()

    contract = read_contract(Path(args.contract))
    message = contract["message"]
    result = {
        "message": message,
        "sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "network_required": False,
    }

    output_path = Path("/output/result.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"event": "result_written", "path": str(output_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
