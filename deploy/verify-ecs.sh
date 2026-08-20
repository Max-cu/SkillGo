#!/usr/bin/env bash
set -euo pipefail

cd "${SKILLGO_INSTALL_ROOT:-/opt/skillgo}"
deploy_env="${SKILLGO_DEPLOY_ENV:-deploy/ecs.env}"
if [ ! -f "$deploy_env" ]; then
  echo "Missing $deploy_env; copy deploy/ecs.env.example and adjust it first" >&2
  exit 1
fi
docker compose --env-file .env --env-file "$deploy_env" --profile sandbox ps

echo WORKER_LOG
docker logs --tail 100 skillgo-worker-1
echo API_LOG
docker logs --tail 80 skillgo-api-1

docker exec skillgo-api-1 python -c 'from app.database import SessionLocal; from app.models import User,Skill,SkillVersion,Conversation,WorkflowJob; from sqlalchemy import func,select; db=SessionLocal(); print("counts",*[db.scalar(select(func.count()).select_from(model)) for model in (User,Skill,SkillVersion,Conversation,WorkflowJob)])'

docker run --rm \
  --runtime=runsc \
  --network=none \
  --read-only \
  --cap-drop=ALL \
  --security-opt=no-new-privileges:true \
  --user 10001:10001 \
  --memory=256m \
  --pids-limit=64 \
  skillgo/sandbox-runtime:local \
  python3 -c 'import os,socket; s=socket.socket(); s.settimeout(1); print("uid",os.getuid()); print("network_reachable",s.connect_ex(("1.1.1.1",443))==0)'

echo VERIFY_ECS_OK
