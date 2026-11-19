"""Platform-owned apt layers with source fallback and actionable build diagnostics."""
import io
import re
import time
from docker.errors import BuildError


def system_dockerfile(base, packages):
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', base) or not packages or any(
            not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', p) for p in packages):
        raise ValueError('Invalid platform system package specification')
    return (
        f'FROM {base}\nUSER 0:0\n'
        'RUN set -eu; installed=0; for mirror in mirrors.aliyun.com deb.debian.org; do '
        'if [ -f /etc/apt/sources.list.d/debian.sources ]; then '
        'sed -i -E "s#https?://(deb.debian.org|mirrors.aliyun.com|mirrors.tuna.tsinghua.edu.cn)#https://$mirror#g" '
        '/etc/apt/sources.list.d/debian.sources; fi; '
        'if apt-get -o Acquire::Retries=1 -o Acquire::https::Timeout=15 '
        '-o APT::Update::Error-Mode=any update && '
        'DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=1 '
        '-o Acquire::https::Timeout=15 install -y --no-install-recommends '
        + ' '.join(packages) + '; then installed=1; break; fi; done; '
        'test "$installed" = 1; rm -rf /var/lib/apt/lists/*\nUSER 10001:10001\n'
    )


def build_system_image(client, context, deadline):
    try:
        image, _ = client.images.build(fileobj=io.BytesIO(context), custom_context=True,
            rm=True, forcerm=True, pull=False, network_mode='bridge',
            timeout=max(1, int(deadline-time.monotonic())), labels={'skillgo.platform_office':'true'})
        return image
    except BuildError as exc:
        lines = []
        for entry in exc.build_log:
            text = entry.get('stream') or entry.get('error') or ''
            lines.extend(text.splitlines())
            lines = lines[-40:]
        raise RuntimeError('System package preparation failed: ' + '\n'.join(lines)[-3500:]) from exc
