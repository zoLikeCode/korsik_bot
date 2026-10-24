#!/usr/bin/env python3
"""Creates missing secrets only. Never emits their values."""
import os,secrets,argparse
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--directory',default='./secrets');p.add_argument('--owner',type=int);args=p.parse_args()
root=Path(args.directory);root.mkdir(parents=True,exist_ok=True);root.chmod(0o700)
for name in ('db_bootstrap','db_api','db_worker','db_scheduler','db_transport','session_key','admin_password','internal_key','max_webhook_secret'):
    path=root/name
    if not path.exists():path.write_text(secrets.token_urlsafe(36));path.chmod(0o600)
    if args.owner is not None:os.chown(path,args.owner,args.owner)
print('Secrets initialized; existing values preserved. Install max_token separately.')
