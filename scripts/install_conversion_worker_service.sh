#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/ubuntu/VO-server}"
SERVICE_NAME="${SERVICE_NAME:-vo-conversion-worker}"
PYTHON_BIN="${PYTHON_BIN:-${APP_DIR}/.venv/bin/python}"

sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<EOF
[Unit]
Description=V-O Conversion Worker
After=network.target vo-server.service

[Service]
User=ubuntu
WorkingDirectory=${APP_DIR}
Environment=ENV=local
Environment=PATH=/home/ubuntu/.bun/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=${PYTHON_BIN} workers/converter/run_conversion_task_worker.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"
sudo systemctl status "${SERVICE_NAME}" --no-pager
