#!/usr/bin/env bash
set -euo pipefail

docker exec -i skillgo-worker-1 python - <<'PY'
import socket
from urllib.parse import urlparse

from app.config import settings

parsed = urlparse(settings.model_base_url or "")
configured = bool(parsed.hostname and settings.model_name)
print("model_configured", configured)
if not configured:
    raise SystemExit(0)

port = parsed.port or (443 if parsed.scheme == "https" else 80)
try:
    addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
except OSError:
    print("model_dns", False)
    print("model_tcp", False)
    raise SystemExit(0)

print("model_dns", bool(addresses))
reachable = False
for family, sock_type, protocol, _, address in addresses[:4]:
    sock = socket.socket(family, sock_type, protocol)
    sock.settimeout(3)
    try:
        if sock.connect_ex(address) == 0:
            reachable = True
            break
    finally:
        sock.close()
print("model_tcp", reachable)
PY
