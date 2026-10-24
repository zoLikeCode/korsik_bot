#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
python3 - "${1:-./data/backups}" <<'PY'
from datetime import datetime, timezone
import hashlib, json, subprocess, sys
from pathlib import Path
target = Path(sys.argv[1]).resolve() / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
target.mkdir(parents=True, exist_ok=False)
# Purge workers lock 76810108 before deleting immutable files. This connection
# holds the same lock across SQL snapshot + tar; appends remain permissible.
lock = subprocess.Popen(["docker","compose","exec","-T","postgres","psql","-X","-Atq","-v","ON_ERROR_STOP=1","-U","korsik_bootstrap","-d","korsik"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
acquired = False
try:
    lock.stdin.write("SET lock_timeout='60s'; SELECT pg_advisory_lock(76810108); SELECT 'KORSIK_BACKUP_LOCKED';\n")
    lock.stdin.flush()
    while True:
        line = lock.stdout.readline()
        if not line: raise RuntimeError("Backup consistency lock failed")
        if line.strip() == "KORSIK_BACKUP_LOCKED":
            acquired = True
            break
    with (target/"database.dump").open("wb") as out:
        subprocess.run(["docker","compose","exec","-T","postgres","pg_dump","-U","korsik_bootstrap","-d","korsik","-Fc","--no-owner","--no-acl"],stdout=out,check=True)
    with (target/"blobs.tar.gz").open("wb") as out:
        subprocess.run(["docker","compose","exec","-T","api","tar","-C","/data","-czf","-","."],stdout=out,check=True)
finally:
    if lock.poll() is None:
        try:
            if acquired: lock.stdin.write("SELECT pg_advisory_unlock(76810108);\n")
            lock.stdin.write("\\q\n")
            lock.stdin.flush()
            lock.stdin.close()
            lock.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            lock.terminate()
            lock.wait(timeout=10)
with (target/"images.json").open("wb") as out:
    subprocess.run(["docker","compose","images","--format","json"],stdout=out,check=True)
records=[]
for p in sorted(target.iterdir()):
    h=hashlib.sha256()
    with p.open("rb") as source:
        while data:=source.read(8*1024*1024): h.update(data)
    records.append({"file":p.name,"bytes":p.stat().st_size,"sha256":h.hexdigest()})
(target/"manifest.json").write_text(json.dumps({"format":2,"database":"PostgreSQL custom dump","files":records,
    "consistency":"SQL snapshot then immutable blobs under purge-exclusion protocol 76810108",
    "vectors":"Rebuildable from SQL and immutable extraction; not authoritative",
    "secrets":"Operator provisions separately; not in this archive"},indent=2))
print("Backup created: "+str(target),flush=True)
PY
