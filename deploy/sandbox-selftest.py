import asyncio

from app.sandbox_runtime import DockerSandbox, docker_client


async def main() -> None:
    client = docker_client()
    with DockerSandbox(client, job_id="selftest") as sandbox:
        sandbox.put_files({"input/hello.txt": "隔离测试".encode("utf-8")})
        result = await sandbox.command(
            [
                "python3",
                "-c",
                "from pathlib import Path; p=Path('/workspace/output'); p.mkdir(); p.joinpath('result.txt').write_text(Path('/workspace/input/hello.txt').read_text()+'-成功',encoding='utf-8')",
            ],
            timeout_seconds=20,
        )
        assert result.exit_code == 0, result.stderr
        sandbox.container.reload()
        host_config = sandbox.container.attrs["HostConfig"]
        print("runtime", host_config.get("Runtime"))
        print("network_mode", host_config.get("NetworkMode"))
        print("memory", host_config.get("Memory"))
        print("pids_limit", host_config.get("PidsLimit"))
        print("cap_drop", host_config.get("CapDrop"))
        data = sandbox.download_file("/workspace/output/result.txt")
        assert data.decode("utf-8") == "隔离测试-成功"
        print("artifact_verified", True)
    assert not client.containers.list(all=True, filters={"name": "skillgo-job-selftest"})
    print("sandbox_cleaned", True)


asyncio.run(main())
