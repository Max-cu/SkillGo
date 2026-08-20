from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
import traceback
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

import httpx
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.filesystem import WriteEntry
from opensandbox.models.sandboxes import NetworkPolicy


@dataclass
class CaseResult:
    name: str
    status: str
    duration_seconds: float
    details: str
    sandbox_id: str | None = None


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def python_command(source: str) -> str:
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    return (
        "python -I -c "
        f"\"import base64;exec(base64.b64decode('{encoded}'))\""
    )


class PocRunner:
    def __init__(self) -> None:
        self.domain = os.environ.get("OPEN_SANDBOX_DOMAIN", "opensandbox-server:8080")
        self.protocol = os.environ.get("OPEN_SANDBOX_PROTOCOL", "http")
        self.api_key = os.environ["OPEN_SANDBOX_API_KEY"]
        self.skill_image = os.environ.get(
            "POC_SKILL_IMAGE", "skillgo/poc-skill:local"
        )
        self.expected_runtime = os.environ.get("POC_EXPECTED_RUNTIME", "runc")
        self.results_dir = Path(os.environ.get("POC_RESULTS_DIR", "/results"))
        self.run_id = uuid.uuid4().hex
        self.results: list[CaseResult] = []
        self.connection = ConnectionConfig(
            domain=self.domain,
            protocol=self.protocol,
            api_key=self.api_key,
            # Docker Desktop cold starts include image inspection, egress
            # sidecar setup and execd injection. Keep the API timeout above
            # that control-plane work; per-command timeouts are tested
            # separately below.
            request_timeout=timedelta(seconds=90),
            use_server_proxy=True,
        )

    async def wait_for_server(self) -> None:
        url = f"{self.protocol}://{self.domain}/health"
        deadline = time.monotonic() + 60
        async with httpx.AsyncClient(timeout=3) as client:
            while time.monotonic() < deadline:
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
        raise RuntimeError(f"OpenSandbox health check timed out: {url}")

    async def create_sandbox(
        self,
        case_name: str,
        *,
        timeout_seconds: int = 60,
        memory: str = "256Mi",
        deny_network: bool = True,
    ) -> Sandbox:
        policy = (
            NetworkPolicy(default_action="deny", egress=[])
            if deny_network
            else None
        )
        return await Sandbox.create(
            self.skill_image,
            timeout=timedelta(seconds=timeout_seconds),
            ready_timeout=timedelta(seconds=90),
            resource={"cpu": "500m", "memory": memory},
            metadata={
                "skillgo-poc-run": self.run_id,
                "skillgo-poc-case": case_name,
            },
            network_policy=policy,
            entrypoint=["tail", "-f", "/dev/null"],
            connection_config=self.connection,
        )

    async def destroy(self, sandbox: Sandbox) -> None:
        try:
            await sandbox.kill()
        except Exception:
            pass
        try:
            await sandbox.close()
        except Exception:
            pass

    async def record(
        self,
        name: str,
        operation: Callable[[], Awaitable[tuple[str, str | None]]],
    ) -> None:
        started = time.monotonic()
        sandbox_id = None
        try:
            details, sandbox_id = await operation()
            status = "PASS"
        except AssertionError as exc:
            status = "FAIL"
            details = str(exc)
        except Exception as exc:
            status = "FAIL"
            details = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        result = CaseResult(
            name=name,
            status=status,
            duration_seconds=round(time.monotonic() - started, 3),
            details=details,
            sandbox_id=sandbox_id,
        )
        self.results.append(result)
        print(f"[{status}] {name}: {details}", flush=True)

    async def lifecycle_and_fixed_skill(self) -> tuple[str, str | None]:
        sandbox = await self.create_sandbox("fixed-skill")
        try:
            ping = await sandbox.commands.run("printf skillgo-poc")
            assert ping.exit_code == 0, f"command exit code: {ping.exit_code}"
            stdout = "".join(item.text for item in ping.logs.stdout)
            assert "skillgo-poc" in stdout, f"unexpected stdout: {stdout!r}"

            contract = json.dumps(
                {"message": "hello from isolated skill"}, ensure_ascii=False
            )
            await sandbox.files.write_files(
                [
                    WriteEntry(
                        path="/run/skill/input.json",
                        data=contract,
                        # The execd upload API expects an octal digit sequence,
                        # even though the SDK model currently types this as int.
                        mode=600,
                    )
                ]
            )
            execution = await sandbox.commands.run(
                "python -I /skill/src/main.py "
                "--contract /run/skill/input.json",
                opts=RunCommandOpts(
                    working_directory="/skill",
                    timeout=timedelta(seconds=10),
                ),
            )
            assert execution.exit_code == 0, (
                f"Skill failed: exit={execution.exit_code}, "
                f"stderr={[item.text for item in execution.logs.stderr]}"
            )
            payload = json.loads(
                await sandbox.files.read_file("/output/result.json")
            )
            assert payload["message"] == "hello from isolated skill"
            assert payload["uid"] == 10001, f"Skill ran as uid={payload['uid']}"
            assert payload["gid"] == 10001, f"Skill ran as gid={payload['gid']}"
            return "lifecycle, command, file API and fixed Skill succeeded", sandbox.id
        finally:
            await self.destroy(sandbox)

    async def network_default_deny(self) -> tuple[str, str | None]:
        sandbox = await self.create_sandbox("network-deny")
        code = """
import socket

targets = [
    ("1.1.1.1", 443),
    ("169.254.169.254", 80),
    ("10.0.0.1", 80),
]
reachable = []
for host, port in targets:
    try:
        with socket.create_connection((host, port), timeout=1):
            reachable.append(f"{host}:{port}")
    except OSError:
        pass
print({"reachable": reachable})
raise SystemExit(42 if reachable else 0)
"""
        try:
            execution = await sandbox.commands.run(
                python_command(code),
                opts=RunCommandOpts(timeout=timedelta(seconds=10)),
            )
            assert execution.exit_code == 0, (
                "default-deny was bypassed; "
                f"exit={execution.exit_code}, "
                f"stdout={[item.text for item in execution.logs.stdout]}"
            )
            return "public, metadata and private test targets were blocked", sandbox.id
        finally:
            await self.destroy(sandbox)

    async def command_timeout(self) -> tuple[str, str | None]:
        sandbox = await self.create_sandbox("command-timeout")
        started = time.monotonic()
        try:
            execution = await sandbox.commands.run(
                python_command("import time; time.sleep(30)"),
                opts=RunCommandOpts(timeout=timedelta(seconds=2)),
            )
            elapsed = time.monotonic() - started
            assert elapsed < 12, f"timeout returned too late: {elapsed:.2f}s"
            assert execution.exit_code not in (None, 0), (
                f"timed command unexpectedly succeeded: {execution.exit_code}"
            )
            return (
                f"command stopped in {elapsed:.2f}s with exit={execution.exit_code}",
                sandbox.id,
            )
        except Exception as exc:
            elapsed = time.monotonic() - started
            assert elapsed < 12, f"timeout raised too late: {elapsed:.2f}s"
            return f"command transport closed after timeout: {type(exc).__name__}", sandbox.id
        finally:
            await self.destroy(sandbox)

    async def pid_limit(self) -> tuple[str, str | None]:
        sandbox = await self.create_sandbox("pid-limit")
        code = """
import subprocess
import time

children = []
limited = False
try:
    for _ in range(256):
        children.append(subprocess.Popen(["sleep", "4"]))
except (OSError, BlockingIOError):
    limited = True
finally:
    for child in children:
        child.terminate()
    for child in children:
        try:
            child.wait(timeout=1)
        except Exception:
            child.kill()
print({"spawned": len(children), "limited": limited})
raise SystemExit(0 if limited else 43)
"""
        try:
            execution = await sandbox.commands.run(
                python_command(code),
                opts=RunCommandOpts(timeout=timedelta(seconds=15)),
            )
            assert execution.exit_code == 0, (
                f"PID limit did not stop process creation: {execution.exit_code}"
            )
            return (
                "process creation was rejected before 256 child processes",
                sandbox.id,
            )
        finally:
            await self.destroy(sandbox)

    async def memory_limit(self) -> tuple[str, str | None]:
        sandbox = await self.create_sandbox(
            "memory-limit", memory="128Mi", deny_network=True
        )
        code = """
data = bytearray(512 * 1024 * 1024)
print(len(data))
"""
        try:
            try:
                execution = await sandbox.commands.run(
                    python_command(code),
                    opts=RunCommandOpts(timeout=timedelta(seconds=15)),
                )
                assert execution.exit_code not in (None, 0), (
                    "512 MiB allocation succeeded inside a 128 MiB sandbox"
                )
                return (
                    f"oversized allocation failed with exit={execution.exit_code}",
                    sandbox.id,
                )
            except Exception as exc:
                return (
                    "oversized allocation terminated command transport: "
                    f"{type(exc).__name__}",
                    sandbox.id,
                )
        finally:
            await self.destroy(sandbox)

    async def sandbox_ttl(self) -> tuple[str, str | None]:
        sandbox = await self.create_sandbox(
            "sandbox-ttl", timeout_seconds=60, deny_network=True
        )
        sandbox_id = sandbox.id
        try:
            await asyncio.sleep(65)
            try:
                info = await sandbox.get_info()
                state = str(info.status.state).lower()
                assert "running" not in state, f"sandbox still running: {state}"
                return f"sandbox state after TTL: {state}", sandbox_id
            except Exception as exc:
                return (
                    "sandbox no longer queryable after TTL: "
                    f"{type(exc).__name__}",
                    sandbox_id,
                )
        finally:
            await self.destroy(sandbox)

    async def run_functional(self, selected_case: str | None = None) -> int:
        await self.wait_for_server()
        cases = (
            ("lifecycle_and_fixed_skill", self.lifecycle_and_fixed_skill),
            ("network_default_deny", self.network_default_deny),
            ("command_timeout", self.command_timeout),
            ("pid_limit", self.pid_limit),
            ("memory_limit", self.memory_limit),
            ("sandbox_ttl", self.sandbox_ttl),
        )
        for name, operation in cases:
            if selected_case is None or name == selected_case:
                await self.record(name, operation)
        self.write_results()
        return 1 if any(item.status == "FAIL" for item in self.results) else 0

    async def run_hold(self, hold_seconds: int) -> int:
        await self.wait_for_server()
        sandbox = await self.create_sandbox(
            "host-inspection",
            timeout_seconds=max(hold_seconds + 30, 120),
            deny_network=True,
        )
        active = {
            "created_at": utc_now(),
            "run_id": self.run_id,
            "sandbox_id": sandbox.id,
            "image": self.skill_image,
            "expected_runtime": self.expected_runtime,
            "hold_seconds": hold_seconds,
        }
        self.results_dir.mkdir(parents=True, exist_ok=True)
        (self.results_dir / "active-sandbox.json").write_text(
            json.dumps(active, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"[HOLD] sandbox={sandbox.id}; inspect it within {hold_seconds}s",
            flush=True,
        )
        try:
            await asyncio.sleep(hold_seconds)
        finally:
            await self.destroy(sandbox)
        return 0

    def write_results(self) -> None:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": utc_now(),
            "run_id": self.run_id,
            "expected_runtime": self.expected_runtime,
            "skill_image": self.skill_image,
            "cases": [asdict(item) for item in self.results],
        }
        (self.results_dir / "poc-results.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--suite",
        choices=("functional", "hold"),
        default="functional",
    )
    parser.add_argument("--hold-seconds", type=int, default=90)
    parser.add_argument(
        "--case",
        choices=(
            "lifecycle_and_fixed_skill",
            "network_default_deny",
            "command_timeout",
            "pid_limit",
            "memory_limit",
            "sandbox_ttl",
        ),
    )
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    runner = PocRunner()
    if args.suite == "hold":
        return await runner.run_hold(max(10, args.hold_seconds))
    return await runner.run_functional(args.case)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
