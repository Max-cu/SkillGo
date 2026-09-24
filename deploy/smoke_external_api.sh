#!/bin/bash
# External API routing/auth smoke through the public nginx (web:8080).
set -u
BASE="${SKILLGO_BASE_URL:-http://127.0.0.1:3399}"

echo "== 1) workflow job create, no key (expect 404: endpoint checked before key)"
curl -s -o /tmp/r1 -w 'HTTP %{http_code}\n' -X POST "$BASE/api/v1/workflow-endpoints/nope/jobs"
cat /tmp/r1; echo

echo "== 2) workflow job create, bad key (expect 404)"
curl -s -o /tmp/r2 -w 'HTTP %{http_code}\n' -X POST -H 'X-SkillGo-Key: skg_bad' \
  -F 'file=@/etc/hostname' "$BASE/api/v1/workflow-endpoints/nope/jobs"
cat /tmp/r2; echo

echo "== 3) sync invoke, bad key (expect 404)"
curl -s -o /tmp/r3 -w 'HTTP %{http_code}\n' -X POST -H 'X-SkillGo-Key: skg_bad' \
  -H 'Content-Type: application/json' -d '{"input":{}}' "$BASE/api/v1/invoke/nope"
cat /tmp/r3; echo

echo "== 4) sync invoke, bad JSON (expect 422)"
curl -s -o /tmp/r4 -w 'HTTP %{http_code}\n' -X POST -H 'X-SkillGo-Key: skg_bad' \
  -H 'Content-Type: application/json' -d 'not-json' "$BASE/api/v1/invoke/nope"
head -c 300 /tmp/r4; echo

echo "== 5) query unknown job (expect 404)"
curl -s -o /tmp/r5 -w 'HTTP %{http_code}\n' \
  -H 'X-SkillGo-Key: skg_bad' "$BASE/api/v1/workflow-endpoints/nope/jobs/x"
cat /tmp/r5; echo

echo "== 6) openapi reachable through nginx (expect 200)"
curl -s -o /tmp/r6 -w 'HTTP %{http_code}\n' "$BASE/api/openapi.json"
python3 -c "import json;d=json.load(open('/tmp/r6'));paths=[p for p in d['paths'] if 'workflow-endpoints' in p or p.endswith('/invoke/{endpoint_slug}')];print('\n'.join(sorted(paths)))"
