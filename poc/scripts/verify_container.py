from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class Check:
    name: str
    status: str
    actual: Any
    expected: str


def run_docker(*args: str) -> str:
    completed = subprocess.run(
        ["docker", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def load_active(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(
            f"{path} does not exist; start the runner with --suite hold first"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def find_container(active: dict[str, Any]) -> dict[str, Any]:
    ids = run_docker("ps", "-q").split()
    if not ids:
        raise RuntimeError("no running Docker containers found")
    inspected = json.loads(run_docker("inspect", *ids))
    sandbox_id = active["sandbox_id"]
    image = active["image"]

    def contains_sandbox_id(item: dict[str, Any]) -> bool:
        searchable = json.dumps(
            {
                "Name": item.get("Name"),
                "Labels": item.get("Config", {}).get("Labels"),
                "Env": item.get("Config", {}).get("Env"),
            },
            sort_keys=True,
        )
        return sandbox_id in searchable

    exact = [
        item
        for item in inspected
        if item.get("Config", {}).get("Image") == image
        and contains_sandbox_id(item)
    ]
    if len(exact) == 1:
        return exact[0]

    image_matches = [
        item for item in inspected if item.get("Config", {}).get("Image") == image
    ]
    if len(image_matches) == 1:
        return image_matches[0]

    summary = [
        {
            "name": item.get("Name"),
            "image": item.get("Config", {}).get("Image"),
            "labels": item.get("Config", {}).get("Labels"),
        }
        for item in inspected
    ]
    raise RuntimeError(
        "could not identify active Skill container; running containers:\n"
        + json.dumps(summary, indent=2)
    )


def check(
    checks: list[Check],
    name: str,
    condition: bool,
    actual: Any,
    expected: str,
) -> None:
    checks.append(
        Check(
            name=name,
            status="PASS" if condition else "FAIL",
            actual=actual,
            expected=expected,
        )
    )


def verify(container: dict[str, Any], expected_runtime: str) -> list[Check]:
    checks: list[Check] = []
    host = container["HostConfig"]
    config = container["Config"]

    runtime = host.get("Runtime") or "runc"
    check(
        checks,
        "runtime",
        runtime == expected_runtime,
        runtime,
        expected_runtime,
    )
    check(
        checks,
        "non_root_user",
        config.get("User") not in ("", "0", "root", "0:0", "root:root"),
        config.get("User"),
        "non-root UID/GID",
    )
    check(
        checks,
        "privileged_disabled",
        host.get("Privileged") is False,
        host.get("Privileged"),
        "false",
    )
    check(
        checks,
        "host_network_disabled",
        host.get("NetworkMode") != "host",
        host.get("NetworkMode"),
        "not host",
    )
    check(
        checks,
        "host_pid_disabled",
        host.get("PidMode") not in ("host",),
        host.get("PidMode"),
        "not host",
    )
    check(
        checks,
        "host_ipc_disabled",
        host.get("IpcMode") not in ("host",),
        host.get("IpcMode"),
        "not host",
    )
    check(
        checks,
        "readonly_rootfs",
        host.get("ReadonlyRootfs") is True,
        host.get("ReadonlyRootfs"),
        "true",
    )

    cap_drop = host.get("CapDrop") or []
    check(
        checks,
        "capabilities_dropped",
        "ALL" in {value.upper() for value in cap_drop},
        cap_drop,
        "contains ALL",
    )

    cap_add = host.get("CapAdd") or []
    check(
        checks,
        "no_capabilities_added",
        not cap_add,
        cap_add,
        "empty",
    )

    security_opts = host.get("SecurityOpt") or []
    check(
        checks,
        "no_new_privileges",
        any("no-new-privileges" in value for value in security_opts),
        security_opts,
        "contains no-new-privileges",
    )

    pids_limit = host.get("PidsLimit")
    check(
        checks,
        "pids_limit",
        isinstance(pids_limit, int) and 0 < pids_limit <= 128,
        pids_limit,
        "1..128",
    )

    memory = host.get("Memory")
    check(
        checks,
        "memory_limit",
        isinstance(memory, int) and 0 < memory <= 256 * 1024 * 1024,
        memory,
        "1..268435456 bytes",
    )

    nano_cpus = host.get("NanoCpus")
    check(
        checks,
        "cpu_limit",
        isinstance(nano_cpus, int) and 0 < nano_cpus <= 500_000_000,
        nano_cpus,
        "1..500000000 NanoCPUs",
    )

    binds = host.get("Binds") or []
    dangerous_bind = any(
        "docker.sock" in value
        or value.startswith("/:")
        or value.startswith("/etc:")
        or value.startswith("/proc:")
        for value in binds
    )
    check(
        checks,
        "no_dangerous_host_mount",
        not dangerous_bind,
        binds,
        "no Docker socket or broad host bind",
    )

    devices = host.get("Devices") or []
    check(
        checks,
        "no_host_devices",
        not devices,
        devices,
        "empty",
    )
    return checks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--active-file",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "results"
        / "active-sandbox.json",
    )
    parser.add_argument("--expected-runtime", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    active = load_active(args.active_file)
    container = find_container(active)
    checks = verify(container, args.expected_runtime)

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "sandbox_id": active["sandbox_id"],
        "container_id": container["Id"],
        "container_name": container["Name"],
        "image": container["Config"]["Image"],
        "checks": [asdict(item) for item in checks],
    }
    output_path = args.active_file.parent / "container-verification.json"
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    for item in checks:
        print(
            f"[{item.status}] {item.name}: actual={item.actual!r}; "
            f"expected={item.expected}"
        )
    failed = [item for item in checks if item.status == "FAIL"]
    print(f"result: {len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
