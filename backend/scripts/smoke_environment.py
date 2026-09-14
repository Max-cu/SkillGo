"""Run against a dedicated Docker host/controller with no user data mounted.
Set PYTHONPATH to backend; requires the platform base image already on that host.
The temporary extension image is deleted after successful synthetic verification.
"""
import argparse
import asyncio
import json
from uuid import uuid4
import docker
from app.environment_preparation import environment_spec
from app.environment_builder import build_environment
from app.sandbox_runtime import DockerSandbox
from app.environment_capabilities import preflight_environment


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-image', required=True)
    parser.add_argument('--capability', action='append', default=[])
    args = parser.parse_args()
    client = docker.from_env(timeout=30)
    image = None
    try:
        image, inventory = build_environment(client,
            environment_spec(args.capability or ['pdf.read', 'fonts.cjk', 'image.qr'], args.base_image),
            str(uuid4()), on_probing=lambda: print('Verifying isolated image', flush=True))
        async def task_probe():
            with DockerSandbox(client, job_id='environment-smoke-' + str(uuid4()), image_id=image) as sandbox:
                sandbox.put_files({'input/synthetic.txt': b'platform synthetic input'})
                actual = await preflight_environment(sandbox, [{'runtime_requirements': {'declared_capabilities': args.capability or ['pdf.read', 'fonts.cjk', 'image.qr']}}])
                assert actual['image_id'] == image
                result = await sandbox.command(['python3', '-I', '-c', "from pathlib import Path; assert Path('/workspace/input/synthetic.txt').read_text()=='platform synthetic input'"], cwd='/workspace')
                assert result.exit_code == 0, result.stderr
                print('Task sandbox uses prepared image and synthetic workspace successfully', flush=True)
        asyncio.run(task_probe())
        print(json.dumps({'image': image, 'capabilities': inventory['capabilities'],
                          'providers': inventory['providers']}, ensure_ascii=False), flush=True)
    finally:
        if image and image != args.base_image:
            client.images.remove(image)


if __name__ == '__main__':
    main()
