#!/usr/bin/env sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this script with sudo on a dedicated Ubuntu/Debian executor." >&2
  exit 1
fi

if [ "$(uname -s)" != "Linux" ]; then
  echo "gVisor installation requires native Linux." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Install Docker Engine before gVisor." >&2
  exit 1
fi

apt-get update
apt-get install -y apt-transport-https ca-certificates curl gnupg

curl -fsSL https://gvisor.dev/archive.key \
  | gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg

arch="$(dpkg --print-architecture)"
echo "deb [arch=${arch} signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  > /etc/apt/sources.list.d/gvisor.list

apt-get update
apt-get install -y runsc

runsc install
systemctl reload docker

docker run --rm --runtime=runsc hello-world
docker info --format '{{json .Runtimes}}'

echo "gVisor runsc installed and Docker smoke test passed."
echo "No host-network runtime argument was configured."
