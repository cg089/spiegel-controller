#!/usr/bin/env bash
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"
git pull
"${REPO_DIR}/.venv/bin/pip" install -r requirements.txt
sudo systemctl restart relay-api.service

