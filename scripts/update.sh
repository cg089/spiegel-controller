# scripts/update.sh
#!/usr/bin/env bash
set -euo pipefail

# immer aus Repo-Root arbeiten
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"

echo "[1/5] Pull latest changes"
git pull

echo "[2/5] Update Python deps"
if [[ -d "${REPO_DIR}/.venv" ]]; then
  VENV_PIP="${REPO_DIR}/.venv/bin/pip"
else
  echo "❌ .venv nicht gefunden im Repo. Bitte ./scripts/install.sh ausführen."
  exit 1
fi

"${VENV_PIP}" install -r "${REPO_DIR}/requirements.txt"

echo "[3/5] Restart services"
sudo systemctl restart rtsp-server.service || true
sudo systemctl restart relay-api.service

echo "[4/5] Status"
systemctl status rtsp-server.service --no-pager || true
systemctl status relay-api.service --no-pager || true

echo "[5/5] Done."

