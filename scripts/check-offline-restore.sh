#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
backup_path="${1:?Provide verified backup directory}"
check_root="${2:?Provide isolated check directory outside app}"
model_dir="${MODEL_DIR:-/opt/korsik/models}"
mode="${OFFLINE_CHECK_MODE:-FULL}"
case "$mode" in FULL) generation=true ;; CORE) generation=false ;; *) echo 'OFFLINE_CHECK_MODE must be FULL or CORE' >&2; exit 2 ;; esac
oom_hook="${OFFLINE_CHECK_OOM_HOOK:-}"
if [ -n "$oom_hook" ]; then
  test "$mode" = FULL
  case "$oom_hook" in /*) test -f "$oom_hook" ;; *) echo 'OOM proof hook must be an absolute local script path' >&2; exit 2 ;; esac
fi
python3 - "$backup_path" <<'VERIFY'
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1]);manifest=json.loads((root/'manifest.json').read_text())
for entry in manifest['files']:
 p=root/entry['file']
 if not p.is_file() or p.stat().st_size!=entry['bytes'] or hashlib.sha256(p.read_bytes()).hexdigest()!=entry['sha256']:
  raise SystemExit('Backup checksum verification failed')
print('Verified supplied backup SHA256 manifest')
VERIFY
mkdir -p "$check_root"
check_root="$(cd "$check_root" && pwd)"
project="korsik_offline_$(date +%s)"
app_image="$(docker image inspect -f '{{.Id}}' korsik-app:local)"
encoder_image="$(docker image inspect -f '{{.Id}}' korsik-models:cpu)"
generator_image="$(docker image inspect -f '{{.Id}}' korsik-generator:local)"
parser_image="$(docker image inspect -f '{{.Id}}' korsik-parser:structure)"
python3 - "$check_root" "$project" "$mode" "$app_image" "$encoder_image" "$generator_image" "$parser_image" <<'CONTEXT'
import json,sys
from datetime import datetime,timezone
from pathlib import Path
context={'project':sys.argv[2],'mode':sys.argv[3],'started_at':datetime.now(timezone.utc).isoformat(),
'images':dict(zip(('app','encoder_reranker','generator','parser'),sys.argv[4:])),
'prebuilt_images_only':True,'build_or_pull_during_check':False,'production_mutations':False,'external_MAX_sends':0}
(Path(sys.argv[1])/'run-context.json').write_text(json.dumps(context,indent=2))
CONTEXT
python3 scripts/init-secrets.py --directory "$check_root/secrets" --owner 10001
chmod 644 "$check_root/secrets/db_bootstrap"
# No MAX credential is installed, and transport/tunnel/web never start.
cat > "$check_root/.env" <<ENV
SECRET_DIR=$check_root/secrets
MODEL_DIR=$model_dir
GENERATOR_GPU_ID=${OFFLINE_CHECK_GPU_ID:-0}
PRODUCT_MODE=$mode
GENERATION_ENABLED=$generation
GENERATOR_MODEL_SUBDIR=${OFFLINE_CHECK_GENERATOR_SUBDIR:-generator-4b}
MAX_MODE=disabled
SECURE_COOKIES=false
PARSER_IMAGE=korsik-parser:structure
OFFLINE_CHECK_APP_IMAGE=$app_image
OFFLINE_CHECK_ENCODER_IMAGE=$encoder_image
OFFLINE_CHECK_GENERATOR_IMAGE=$generator_image
OFFLINE_CHECK_PARSER_IMAGE=$parser_image
ENV
cat > "$check_root/compose.offline.yaml" <<'YAML'
services:
  init-data:
    image: '${OFFLINE_CHECK_APP_IMAGE}'
  encoder:
    image: '${OFFLINE_CHECK_ENCODER_IMAGE}'
  reranker:
    image: '${OFFLINE_CHECK_ENCODER_IMAGE}'
  generator:
    image: '${OFFLINE_CHECK_GENERATOR_IMAGE}'
  parser:
    image: '${OFFLINE_CHECK_PARSER_IMAGE}'
  postgres:
    environment: {POSTGRES_DB: korsik_offline_check}
    healthcheck:
      test: [CMD-SHELL, 'pg_isready -U korsik_bootstrap -d korsik_offline_check']
  migrate:
    image: '${OFFLINE_CHECK_APP_IMAGE}'
    environment: {KORSIK_DATABASE_NAME: korsik_offline_check}
  api:
    image: '${OFFLINE_CHECK_APP_IMAGE}'
    environment: {KORSIK_DATABASE_NAME: korsik_offline_check}
  worker:
    image: '${OFFLINE_CHECK_APP_IMAGE}'
    environment: {KORSIK_DATABASE_NAME: korsik_offline_check}
    tmpfs: ['/tmp:size=256m,mode=1777', '/fault:rw,size=1m,uid=10001,gid=10001,mode=0700']
  answer-worker:
    image: '${OFFLINE_CHECK_APP_IMAGE}'
    environment: {KORSIK_DATABASE_NAME: korsik_offline_check}
YAML
dc=(docker compose -p "$project" --env-file "$check_root/.env" -f compose.yaml -f "$check_root/compose.offline.yaml")
cleanup() {
  result=$?
  if [ "$result" -ne 0 ]; then
    "${dc[@]}" ps --all --format '{{.Service}} {{.State}} {{.Health}}' > "$check_root/failure-state.log" 2>&1 || true
    "${dc[@]}" logs --no-color --tail 35 api migrate encoder reranker generator parser > "$check_root/failure-services.log" 2>&1 || true
  fi
  cleanup_result=0
  "${dc[@]}" down -v --remove-orphans > "$check_root/cleanup.log" 2>&1 || cleanup_result=$?
  python3 - "$check_root" "$result" "$cleanup_result" <<'CLEANUP'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]);path=root/'cleanup-result.json'
path.write_text(json.dumps({'check_exit_code':int(sys.argv[2]),'cleanup_exit_code':int(sys.argv[3]),'isolated_resources_removed':int(sys.argv[3])==0},indent=2))
CLEANUP
  if [ "$result" -eq 0 ] && [ "$cleanup_result" -ne 0 ]; then return "$cleanup_result"; fi
  return "$result"
}
trap cleanup EXIT
services=(postgres qdrant init-data encoder reranker parser)
health_services=(api encoder reranker parser)
if [ "$mode" = FULL ]; then services+=(generator); health_services+=(generator); fi
"${dc[@]}" up -d --no-build --pull never "${services[@]}"
for _ in {1..60}; do
  if "${dc[@]}" exec -T postgres pg_isready -U korsik_bootstrap -d korsik_offline_check > /dev/null 2>&1; then break; fi
  sleep 2
done
"${dc[@]}" exec -T postgres pg_restore -U korsik_bootstrap -d korsik_offline_check --no-owner --no-acl --exit-on-error < "$backup_path/database.dump"
"${dc[@]}" run --rm --no-deps -T --user 10001:10001 --entrypoint tar api --no-same-owner -xzf - -C /data < "$backup_path/blobs.tar.gz"
"${dc[@]}" up -d --no-build --pull never api
for _ in {1..90}; do
  healthy=0
  for service in "${health_services[@]}"; do
    container="$("${dc[@]}" ps -q "$service")"
    state="$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || true)"
    if [ "$state" = healthy ]; then healthy=$((healthy+1)); fi
  done
  if [ "$healthy" -eq "${#health_services[@]}" ]; then break; fi
  sleep 2
done
if [ "$healthy" -ne "${#health_services[@]}" ]; then
  "${dc[@]}" ps --all --format '{{.Service}} {{.State}} {{.Health}}'
  exit 1
fi
for network in backend models parsing; do
  test "$(docker network inspect -f '{{.Internal}}' "${project}_${network}")" = true
done
mkdir -p "$check_root/report"
chown 10001:10001 "$check_root/report"
chmod 755 "$check_root"
"${dc[@]}" run --rm --no-deps -T -v "$PWD/tests/offline_restore_probe.py:/probe.py:ro" -v "$check_root/report:/operator" worker python /probe.py
python3 - "$check_root/report/offline-restore-report.json" <<'PY'
import json,sys
report=json.load(open(sys.argv[1]));assert report['status']=='PASS', 'Offline restore checks failed; inspect report'
print('Independent offline cold restore, OCR, reindex, profile answer, analysis and dashboard passed')
PY
if [ "$mode" = FULL ]; then
  if [ -n "$oom_hook" ]; then
    # Test-only instrumentation owns its isolated generator override. The
    # outer EXIT trap still removes this project's containers and volumes.
    timeout 360s bash "$oom_hook" "$project" "$check_root"
    test -s "$check_root/report/cuda-oom-business-report.json"
  fi
  "${dc[@]}" stop generator
  "${dc[@]}" run --rm --no-deps -T -e KORSIK_MIXED_PROBE_DATABASE=korsik_offline_check -e KORSIK_MIXED_PROBE_REPORT=/operator/mixed-ai-failure-report.json -v "$PWD/tests/mixed_ai_failure_probe.py:/probe.py:ro" -v "$check_root/report:/operator" worker python /probe.py
fi
"${dc[@]}" run --rm --no-deps -T -e KORSIK_STORAGE_FAULT_ALLOWED=ISOLATED_TMPFS_ONLY -v "$PWD/tests/storage_fault_probe.py:/probe.py:ro" -v "$check_root/report:/operator" worker python /probe.py
"${dc[@]}" run --rm --no-deps -T -e KORSIK_SCHEDULER_PROBE_ALLOWED=ISOLATED_NO_MAX -v "$PWD/tests/scheduler_due_probe.py:/probe.py:ro" -v "$check_root/report:/operator" worker python /probe.py
if [ "${OFFLINE_CHECK_CORE_LATENCY:-false}" = true ]; then
  test "$mode" = CORE
  test -f "${OFFLINE_CHECK_ADMIN_PASSWORD_FILE:?Provide original admin password file for the restored account; mounted into benchmark helper only}"
  "${dc[@]}" up -d --no-build --pull never answer-worker
  "${dc[@]}" run --rm --no-deps -T -v "$PWD/tests/api_latency.py:/probe.py:ro" -v "$OFFLINE_CHECK_ADMIN_PASSWORD_FILE:/benchmark-admin-password:ro" -v "$check_root/report:/operator" worker python /probe.py --admin-password-file /benchmark-admin-password --mode CORE --output /operator/api-latency-core-restored.json
fi
python3 - "$check_root/report" "$project" "$mode" <<'RESULT'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]);checks=[]
names=('offline-restore-report.json','storage-fault-report.json','scheduler-due-report.json') + (('mixed-ai-failure-report.json',) if sys.argv[3]=='FULL' else ())
if (root/'cuda-oom-business-report.json').exists(): names+=('cuda-oom-business-report.json',)
for name in names:
 report=json.loads((root/name).read_text());checks.append({'report':name,'status':report['status']})
summary={'project':sys.argv[2],'mode':sys.argv[3],'isolated_database':'korsik_offline_check','external_MAX_sends':0,'proofs':checks,'status':'PASS' if all(v['status']=='PASS' for v in checks) else 'FAIL'}
(root/'proof-summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary),flush=True)
assert summary['status']=='PASS'
RESULT
