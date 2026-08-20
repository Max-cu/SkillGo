#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker Engine must be installed before this bootstrap" >&2
  exit 1
fi

if ! swapon --show=NAME --noheadings | grep -q .; then
  if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile
fi

if ! grep -q '^/swapfile ' /etc/fstab; then
  printf '/swapfile none swap sw 0 0\n' >> /etc/fstab
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl gnupg

if ! command -v runsc >/dev/null 2>&1; then
  curl -fsSL -o /tmp/gvisor-archive.key https://gvisor.dev/archive.key
  gpg --dearmor --yes --output /usr/share/keyrings/gvisor-archive-keyring.gpg /tmp/gvisor-archive.key
  architecture="$(dpkg --print-architecture)"
  printf 'deb [arch=%s signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main\n' "$architecture" \
    > /etc/apt/sources.list.d/gvisor.list
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y runsc
fi

runsc install
systemctl restart docker

docker run --rm --runtime=runsc --network=none hello-world >/tmp/skillgo-gvisor-smoke.log

echo "BOOTSTRAP_OK"
docker version --format 'docker={{.Server.Version}}'
runsc --version | head -n 1
docker info --format 'runtimes={{json .Runtimes}}'
free -h
