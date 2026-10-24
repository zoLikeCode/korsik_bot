#!/usr/bin/env bash
# Host operator: snapshot every six hours; no automatic business-data deletion.
set -euo pipefail
app_path="${1:-/opt/korsik/app}"
backup_path="${2:-/opt/korsik/backups}"
[[ "$app_path" =~ ^/[a-zA-Z0-9._/-]+$ && "$backup_path" =~ ^/[a-zA-Z0-9._/-]+$ ]] || { printf 'Use absolute paths without spaces\n' >&2; exit 1; }
test "$(id -u)" = 0
test -f "$app_path/compose.yaml"
test -f "$app_path/scripts/backup.sh"
mkdir -p "$backup_path"
chmod 700 "$backup_path"
cat > /etc/systemd/system/korsik-backup.service <<EOF
[Unit]
Description=Consistent Korsik database and immutable-file backup
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=$app_path
ExecStart=/bin/bash $app_path/scripts/backup.sh $backup_path
UMask=0077
Nice=10
IOSchedulingClass=idle
PrivateTmp=true
ProtectHome=true
NoNewPrivileges=true
TimeoutStartSec=30min
EOF
cat > /etc/systemd/system/korsik-backup.timer <<'EOF'
[Unit]
Description=Korsik backup every six hours

[Timer]
OnCalendar=*-*-* 00,06,12,18:15:00 UTC
RandomizedDelaySec=2min
Persistent=true
Unit=korsik-backup.service

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now korsik-backup.timer
systemctl list-timers korsik-backup.timer --no-pager
printf 'Host snapshots enabled. Off-host copy and retention approval are separate operator responsibilities.\n'
