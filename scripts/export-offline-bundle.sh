#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
bundle_dir="${1:?Provide output directory outside live app}"
model_dir="${MODEL_DIR:-/opt/korsik/models}"
python3 - "$bundle_dir" "$model_dir" <<'PY'
from pathlib import Path
import sys
requested=Path(sys.argv[1]);root=requested.resolve();source=Path.cwd().resolve();models=Path(sys.argv[2]).resolve()
if root==source or source in root.parents:
    raise SystemExit('Bundle directory must be outside source checkout')
if root==models or models in root.parents:
    raise SystemExit('Bundle directory must be outside pinned models directory')
if requested.is_symlink() or requested.exists():
    raise SystemExit('Bundle directory must be fresh and must not already exist; existing output is never removed')
if not models.is_dir():
    raise SystemExit('Pinned models directory is missing')
try:
    root.mkdir(parents=True,mode=0o700,exist_ok=False)
except FileExistsError:
    raise SystemExit('Bundle directory was created concurrently; existing output is never removed')
PY
images=(korsik-app:local korsik-web:local korsik-tunnel:local korsik-models:cpu korsik-generator:local korsik-parser:local korsik-parser:structure postgres:17-alpine qdrant/qdrant:v1.20.0)
docker image inspect "${images[@]}" > "$bundle_dir/.images-before.json"
docker save "${images[@]}" | gzip -1 > "$bundle_dir/images.tar.gz"
tar -C "$model_dir" -cf - . | gzip -1 > "$bundle_dir/models.tar.gz"
python3 - "$bundle_dir" "${images[@]}" <<'PY'
from pathlib import Path
import hashlib,json,sys,subprocess,tarfile
root=Path(sys.argv[1]).resolve();source=Path.cwd().resolve()
if root==source or source in root.parents:raise SystemExit('Bundle directory must be outside source checkout')
excluded={'.git','.venv','node_modules','dist','__pycache__','.pytest_cache','.ruff_cache','data','models','secrets','.secrets','backups','private','private-backup'}
with tarfile.open(root/'source.tar.gz','w:gz',compresslevel=1) as archive:
    for p in sorted(source.rglob('*')):
        relative=p.relative_to(source)
        if p.is_symlink() or not p.is_file() or any(part in excluded for part in relative.parts):continue
        if p.name.startswith('.env') and p.name!='.env.example':continue
        if p.suffix in {'.db','.sqlite','.sqlite3','.dump','.log','.pyc','.tsbuildinfo','.pem','.key'} or p.name.endswith(('.db-wal','.db-shm','.sqlite-wal','.sqlite-shm')) or p.name == '.DS_Store' or p.name.startswith('.coverage'):continue
        archive.add(p,arcname=relative,recursive=False)
records=[]
for name in ('images.tar.gz','models.tar.gz','source.tar.gz'):
    p=root/name;h=hashlib.sha256()
    with p.open('rb') as f:
        while data:=f.read(8*1024*1024):h.update(data)
    records.append({'file':name,'bytes':p.stat().st_size,'sha256':h.hexdigest()})
images=json.loads(subprocess.check_output(['docker','image','inspect',*sys.argv[2:]]))
before=json.loads((root/'.images-before.json').read_text())
if [i['Id'] for i in before] != [i['Id'] for i in images]:
    raise SystemExit('Image tags changed during export; discard this bundle and repeat after freezing tags')
(root/'manifest.json').write_text(json.dumps({'format':2,'files':records,'images':[{'requested_tag':tag,'id':i['Id'],'tags':i['RepoTags'],'digests':i['RepoDigests']} for tag,i in zip(sys.argv[2:],images)], 'secrets_included':False,'model_licenses':'Full texts and pinned repository/revision declarations inside model directories and licenses/ sidecars; see license inventory','runtime_downloads_required':False},indent=2))
(root/'.images-before.json').unlink()
PY
cat > "$bundle_dir/INSTALL.txt" <<'TXT'
Korsik offline deployment. Requires Linux amd64 Docker Compose 2.24.4+, NVIDIA driver and Container Toolkit.
Verify all three archives against manifest.json before extracting.
docker load < images.tar.gz
mkdir -p app models
tar -xzf source.tar.gz -C app
tar -xzf models.tar.gz -C models
chown -R 10001:10001 models
cd app
cp .env.example .env
Set MODEL_DIR to the absolute extracted models directory and SECRET_DIR to a private operator directory.
python3 scripts/init-secrets.py --directory /private/korsik-secrets --owner 10001
For the PostgreSQL image only: chmod 644 /private/korsik-secrets/db_bootstrap (parent stays 700).
Install MAX token separately as /private/korsik-secrets/max_token, mode600 owner10001.
Set PUBLIC_URL and MAX_PUBLIC_URL to corporate trusted HTTPS origin, GENERATOR_GPU_ID to the available GPU index.
docker compose -f compose.yaml -f deploy/compose.onprem.yaml up -d --no-build --pull never
No public AI API is used. MAX requires separately permitted messenger network access.
Corporate reverse proxy must forward HTTPS443 to the loopback WEB_PORT; tunnel is disabled by the onprem override.
Without an available decoder/GPU use the explicit CORE override; this is LIMITED_MODE, not full LLM acceptance.
TXT
printf 'Offline image/model/source archives and SHA256 manifest prepared. Secrets are provisioned separately.\n'
