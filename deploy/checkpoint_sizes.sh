#!/bin/bash
# Measure real durable-checkpoint sizes and per-job workspace volumes.
set -u
ROOT=/var/lib/docker/volumes/skillgo_skillgo-storage/_data/checkpoints
echo "=== checkpoint count: $(find "$ROOT" -name '*.zip' 2>/dev/null | wc -l) ==="
find "$ROOT" -name '*.zip' -printf '%s\n' 2>/dev/null | sort -n | awk '
{a[NR]=$1; s+=$1}
END {
  n=NR;
  if(!n) exit;
  print "min="a[1];
  print "p50="a[int(n*0.5)];
  print "p90="a[int(n*0.9)];
  print "p99="a[int(n*0.99)];
  print "max="a[n];
  print "total="s;
}'
echo "=== top 15 largest checkpoints ==="
find "$ROOT" -name '*.zip' -printf '%s\t%p\n' 2>/dev/null | sort -rn | head -15 | \
  awk '{printf "%.1f MB\t%s\n", $1/1048576, $2}'
echo "=== largest job workspace volumes (live/leftover) ==="
docker system df -v 2>/dev/null | grep -A2 'LOCAL VOLUME NAME' | head -3
for v in $(docker volume ls -q | grep '^skillgo-workspace-'); do
  mp=$(docker volume inspect "$v" --format '{{.Mountpoint}}' 2>/dev/null)
  [ -n "$mp" ] && du -sm "$mp" 2>/dev/null
done | sort -rn | head -10
