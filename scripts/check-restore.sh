#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
backup_path="${1:?Provide backup directory}"
python3 - "$backup_path" <<'PY'
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1]);manifest=json.loads((root/'manifest.json').read_text())
for entry in manifest['files']:
    p=root/entry['file'];assert p.is_file() and p.stat().st_size==entry['bytes'] and hashlib.sha256(p.read_bytes()).hexdigest()==entry['sha256'],entry['file']
print('Archive SHA256 verified')
PY
restore_name="korsik_restore_check_$(date +%s)"
docker compose exec -T postgres createdb -U korsik_bootstrap "$restore_name"
trap 'docker compose exec -T postgres dropdb -U korsik_bootstrap "$restore_name" >/dev/null' EXIT
docker compose exec -T postgres pg_restore -U korsik_bootstrap -d "$restore_name" --no-owner --no-acl --exit-on-error < "$backup_path/database.dump"
docker compose exec -T postgres psql -U korsik_bootstrap -d "$restore_name" -v ON_ERROR_STOP=1 -c 'SELECT revision FROM schema_migrations; SELECT kind, count(*) FROM domain_entities GROUP BY kind ORDER BY kind;'
tar -tzf "$backup_path/blobs.tar.gz" > /dev/null
printf 'Restore into isolated temporary database passed; production unchanged.\n'
