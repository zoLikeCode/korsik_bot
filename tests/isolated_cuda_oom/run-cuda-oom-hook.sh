#!/usr/bin/env bash
# Optional check-offline-restore.sh hook. PREPARED ONLY: root schedules execution.
set -euo pipefail
umask 077
project="${1:?isolated project required}"
check_root="${2:?isolated check directory required}"
[[ "$project" =~ ^korsik_offline_[0-9]+$ ]] || { echo 'ISOLATED_PROJECT_REQUIRED' >&2; exit 2; }
test "${KORSIK_ALLOW_OOM_HOOK:-}" = YES_AFTER_HTTP100 || { echo 'EXPLICIT_POST_HTTP100_OPT_IN_REQUIRED' >&2; exit 2; }
test -f compose.yaml
test -f "$check_root/.env"
test -f "$check_root/compose.offline.yaml"
helper_dir="$(cd "$(dirname "$0")" && pwd)"
test -f "$helper_dir/cuda_oom_runtime.py"
test -f "$helper_dir/cuda_oom_business_probe.py"
dc=(docker compose -p "$project" --env-file "$check_root/.env" -f "$PWD/compose.yaml" -f "$check_root/compose.offline.yaml")
generator_id="$("${dc[@]}" ps -q generator)"
test -n "$generator_id"
test "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$generator_id")" = "$project"
test "$(docker network inspect -f '{{.Internal}}' "${project}_models")" = true
# Read only a UUID/index inventory, never container environments or credentials.
gpu0_uuid="$(nvidia-smi --query-gpu=index,uuid --format=csv,noheader,nounits | awk -F ', ' '$1 == "0" {print $2}')"
case "$gpu0_uuid" in GPU-*) ;; *) echo 'PHYSICAL_GPU0_UUID_UNAVAILABLE' >&2; exit 2 ;; esac
mkdir -p "$check_root/report"
chown 10001:10001 "$check_root/report"
report="$check_root/report/cuda-oom-business-report.json"
test ! -e "$report" || { echo 'NEW_EMPTY_OOM_REPORT_PATH_REQUIRED' >&2; exit 2; }
probe_name="${project}_oom_probe"
if docker inspect "$probe_name" > /dev/null 2>&1; then
  echo 'ISOLATED_PROBE_NAME_ALREADY_EXISTS' >&2
  exit 2
fi
override="$check_root/compose.cuda-oom.yaml"
cat > "$override" <<YAML
services:
  generator:
    command: [uvicorn, cuda_oom_runtime:app, --app-dir, /operator, --host, 0.0.0.0, --port, '8000', --workers, '1']
    environment:
      KORSIK_ALLOW_ISOLATED_ALLOCATOR_OOM: YES_GPU0_NO_MAX
      KORSIK_OOM_EXPECTED_GPU_UUID: '$gpu0_uuid'
      KORSIK_OOM_PROOF_FILE: /operator-proof/cuda-oom-observation.json
    volumes:
      - '$helper_dir/cuda_oom_runtime.py:/operator/cuda_oom_runtime.py:ro'
      - '$check_root/report:/operator-proof'
    deploy:
      resources:
        reservations:
          devices: !override [{driver: nvidia, device_ids: ['$gpu0_uuid'], capabilities: [gpu]}]
YAML
oomdc=("${dc[@]}" -f "$override")
finish() {
  result=$?
  if [ ! -s "$report" ]; then
    python3 - "$report" "$result" "$check_root/report/cuda-oom-observation.json" <<'PY'
from pathlib import Path
import json,sys
try: observation=json.loads(Path(sys.argv[3]).read_text())
except (OSError,ValueError): observation={}
count=observation.get('actual_oom_count',0)
result=int(sys.argv[2]); timed_out=result in {124,137,143}
Path(sys.argv[1]).write_text(json.dumps({'probe':'CONTROLLED_CUDA_ALLOCATOR_OOM_BUSINESS_PERSISTENCE','status':'FAIL' if count and not timed_out else 'INCONCLUSIVE','hook_exit_code':result,'actual_cuda_oom_proven':False,'actual_oom_observed':bool(count),'actual_oom_count':count,'actual_exception_class':observation.get('last_observed_exception'),'physical_vram_exhaustion':False,'external_MAX_sends':0,'reason':'No complete business/recovery result; inspect isolated operator logs without publishing document content'},indent=2))
PY
  fi
  # Parent restore EXIT trap owns all-project removal. Stop ONLY this isolated
  # instrumented generator even on timeout/failure, never production GPU1.
  "${oomdc[@]}" stop -t 5 generator > "$check_root/oom-generator-stop.log" 2>&1 || true
  if [ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$probe_name" 2>/dev/null || true)" = "$project" ]; then
    docker rm -f "$probe_name" > "$check_root/oom-helper-remove.log" 2>&1 || true
  fi
  return "$result"
}
trap finish EXIT
"${oomdc[@]}" up -d --no-build --pull never --no-deps --force-recreate generator
healthy=0
for _ in {1..60}; do
  container="$("${oomdc[@]}" ps -q generator)"
  state="$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || true)"
  if [ "$state" = healthy ]; then healthy=1; break; fi
  sleep 2
done
test "$healthy" = 1 || { echo 'INCONCLUSIVE_CAPPED_GENERATOR_STARTUP' >&2; exit 3; }
timeout 180s "${oomdc[@]}" run --rm --no-deps -T --name "$probe_name" \
  -e KORSIK_MIXED_PROBE_DATABASE=korsik_offline_check \
  -e KORSIK_OOM_PROBE_REPORT=/operator/cuda-oom-business-report.json \
  -v "$helper_dir/cuda_oom_business_probe.py:/probe.py:ro" \
  -v "$check_root/report:/operator" worker python /probe.py
python3 - "$report" <<'PY'
import json,sys
report=json.load(open(sys.argv[1]))
assert report['status']=='PASS' and report['actual_oom_count']>0
assert report['actual_exception_class']=='torch.cuda.OutOfMemoryError'
assert report['external_MAX_sends']==0 and report['physical_vram_exhaustion'] is False
print(json.dumps({'status':'PASS','probe':report['probe'],'actual_oom_count':report['actual_oom_count'],'business_checks':len(report['checks']),'physical_vram_exhaustion':False,'external_MAX_sends':0}))
PY
