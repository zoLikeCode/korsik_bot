#!/usr/bin/env bash
# Explicit operator installation only. Does not read or print .env contents.
set -euo pipefail
app_dir=/opt/korsik/app
env_file=/opt/korsik/app/.env
state_dir=/opt/korsik/runtime
render_only=
no_enable=false
while (($#)); do
  case "$1" in
    --app-dir) app_dir=$2; shift 2;;
    --env-file) env_file=$2; shift 2;;
    --state-dir) state_dir=$2; shift 2;;
    --render-only) render_only=$2; shift 2;;
    --no-enable) no_enable=true; shift;;
    *) echo 'Unsupported option; use --app-dir, --env-file, --state-dir, --render-only or --no-enable' >&2; exit 2;;
  esac
done
for value in "$app_dir" "$env_file" "$state_dir"; do
  [[ "$value" =~ ^/[A-Za-z0-9_./-]+$ && "$value" != *..* ]] || { echo 'Use absolute paths without spaces or parent traversal' >&2; exit 2; }
done
[[ -d "$app_dir" && -f "$app_dir/compose.yaml" && -f "$env_file" && -f "$app_dir/scripts/runtime-watchdog.py" ]] || { echo 'Existing app, Compose, environment and watchdog script are required' >&2; exit 2; }
if [[ -n "$render_only" ]]; then
  mkdir -p "$render_only"
  unit_dir=$render_only
  journal_dir=$render_only/journald
else
  [[ $(id -u) == 0 ]] || { echo 'Run this explicit installer as root' >&2; exit 2; }
  command -v docker >/dev/null
  command -v python3 >/dev/null
  command -v systemctl >/dev/null
  docker compose version >/dev/null
  [[ ! -L "$state_dir" ]] || { echo 'State directory must not be a symlink' >&2; exit 2; }
  install -d -m700 -o root -g root "$state_dir"
  install -m700 "$app_dir/scripts/runtime-watchdog.py" "$state_dir/runtime-watchdog.py"
  unit_dir=/etc/systemd/system
  journal_dir=/etc/systemd/journald@korsik-runtime.conf.d
  install -d -m755 "$journal_dir"
fi
python3 - "$unit_dir" "$journal_dir" "$app_dir" "$env_file" "$state_dir" <<'PY'
from pathlib import Path
import sys
units,journals,app,env,state=map(Path,sys.argv[1:])
shared='''User=root
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
StandardOutput=journal
StandardError=journal
LogNamespace=korsik-runtime
LogRateLimitIntervalSec=60s
LogRateLimitBurst=6
'''
boot=f'''[Unit]
Description=Korsik existing offline Compose stack autostart
Requires=docker.service
Wants=network-online.target
After=docker.service network-online.target
ConditionPathExists={app}/compose.yaml
ConditionPathExists={env}
ConditionPathExists=!{state}/maintenance
ConditionPathExists=!{state}/restore-in-progress

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory={app}
ExecStart=/usr/bin/docker compose --project-name korsik --env-file {env} -f {app}/compose.yaml up -d --no-build --pull never
TimeoutStartSec=600
{shared}
[Install]
WantedBy=multi-user.target
'''
watch=f'''[Unit]
Description=Korsik bounded per-service runtime recovery
Requires=docker.service
After=docker.service korsik-runtime.service
ConditionPathExists={app}/compose.yaml
ConditionPathExists={env}
ConditionPathExists=!{state}/maintenance
ConditionPathExists=!{state}/restore-in-progress

[Service]
Type=oneshot
WorkingDirectory={app}
ExecStart=/usr/bin/python3 {state}/runtime-watchdog.py --app-dir {app} --env-file {env} --state-dir {state}
TimeoutStartSec=180
Nice=10
ProtectSystem=strict
ReadWritePaths={state}
{shared}
'''
timer='''[Unit]
Description=Check Korsik runtime every 20 seconds

[Timer]
OnBootSec=40s
OnUnitActiveSec=20s
AccuracySec=1s
Unit=korsik-watchdog.service

[Install]
WantedBy=timers.target
'''
for name,text in [('korsik-runtime.service',boot),('korsik-watchdog.service',watch),('korsik-watchdog.timer',timer)]:
 (units/name).write_text(text);(units/name).chmod(0o644)
journals.mkdir(parents=True,exist_ok=True)
(journals/'limits.conf').write_text('[Journal]\nStorage=persistent\nSystemMaxUse=64M\nRuntimeMaxUse=32M\nMaxRetentionSec=7day\n')
PY
if [[ -n "$render_only" ]]; then
  echo 'Runtime units rendered only; no systemd or Docker mutation performed.'
  exit 0
fi
systemctl daemon-reload
if [[ "$no_enable" == false ]]; then
  systemctl enable --now korsik-runtime.service korsik-watchdog.timer
else
  echo 'Units installed without enabling or starting them.'
fi
