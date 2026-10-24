#!/usr/bin/env bash
# Schema-only clone and synthetic fixtures. Never restore personnel data here.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
check_root="${1:?Provide a dedicated report directory outside the application}"
mkdir -p "$check_root"
check_root="$(cd "$check_root" && pwd)"
test_database=korsik_integration_check
dc=(docker compose -p "${COMPOSE_PROJECT_NAME:-korsik}")
existing="$("${dc[@]}" exec -T postgres psql -U korsik_bootstrap -d postgres -Atc "SELECT datname FROM pg_database WHERE datname='korsik_integration_check'")"
if [ -n "$existing" ]; then
  echo 'Refusing to overwrite an existing integration-check database' >&2
  exit 2
fi
install -d -m 0700 -o 10001 -g 10001 "$check_root/report"
app_image="$(docker image inspect -f '{{.Id}}' korsik-app:local)"
api_container="$("${dc[@]}" ps -q api)"
if [ -z "$api_container" ] || [ "$(docker inspect -f '{{.Image}}' "$api_container")" != "$app_image" ]; then
  echo 'Refusing to label an undeployed image as the current application' >&2
  exit 2
fi
cat > "$check_root/compose.integration.yaml" <<'YAML'
services:
  api:
    image: '${KORSIK_INTEGRATION_APP_IMAGE}'
YAML
export KORSIK_INTEGRATION_APP_IMAGE="$app_image"
cleanup() {
  original_status=$?
  trap - EXIT
  set +e
  "${dc[@]}" exec -T postgres dropdb -U korsik_bootstrap --force "$test_database"
  cleanup_status=$?
  python3 - "$check_root" "$original_status" "$cleanup_status" "$app_image" <<'REPORT'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]);check_code=int(sys.argv[2]);cleanup_code=int(sys.argv[3])
context={'execution_context':'one-off-api-container-current-deployed-image', 'app_image':sys.argv[4],
         'source_database_read':'schema-only pg_dump; no personnel data copied',
         'isolated_database':'korsik_integration_check', 'check_exit_code':check_code,
         'cleanup_exit_code':cleanup_code,'isolated_database_removed':cleanup_code==0,
         'production_business_mutations':False,'external_MAX_sends':0}
(root/'cleanup-report.json').write_text(json.dumps(context,indent=2))
report=root/'report'/'postgres-check-report.json'
if report.exists():
    result=json.loads(report.read_text());result['execution_context']=context
    report.write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(context),flush=True)
REPORT
  if [ "$original_status" -ne 0 ]; then exit "$original_status"; fi
  exit "$cleanup_status"
}
"${dc[@]}" exec -T postgres createdb -U korsik_bootstrap "$test_database"
trap cleanup EXIT
"${dc[@]}" exec -T postgres pg_dump -U korsik_bootstrap --schema-only korsik |
  "${dc[@]}" exec -T postgres psql -q -v ON_ERROR_STOP=1 -U korsik_bootstrap -d "$test_database"
docker compose -p "${COMPOSE_PROJECT_NAME:-korsik}" -f compose.yaml -f "$check_root/compose.integration.yaml" run --rm --no-deps -T \
  -e KORSIK_PG_REPORT=/operator/postgres-check-report.json \
  -v "$PWD/tests/postgres_checks.py:/integration.py:ro" \
  -v "$check_root/report:/operator" api python /integration.py
