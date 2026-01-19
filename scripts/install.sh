#!/usr/bin/env bash
set -euo pipefail

# immer aus Repo-Root arbeiten, egal von wo aufgerufen
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(id -un)"

# Standard: venv im Repo (relativ!)
VENV_DIR="${REPO_DIR}/.venv"
SERVICE_NAME="relay-api.service"

echo "[1/8] Repo dir: ${REPO_DIR}"
echo "[2/8] System packages"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip git mpv

echo "[3/8] venv erstellen: ${VENV_DIR}"
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r "${REPO_DIR}/requirements.txt"

echo "[4/8] .env prüfen"
if [[ ! -f "${REPO_DIR}/.env" ]]; then
  echo "⚠️  ${REPO_DIR}/.env fehlt."
  echo "    Kopiere example.env -> .env und trage MQTT/XAUTHORITY ein:"
  echo "    cp ${REPO_DIR}/example.env ${REPO_DIR}/.env"
  exit 1
fi

echo "[5/8] systemd unit generieren"
TEMPLATE="${REPO_DIR}/systemd/relay-api.service.template"
OUT="${REPO_DIR}/systemd/relay-api.service"

sed \
  -e "s|{{USER}}|${USER_NAME}|g" \
  -e "s|{{APP_DIR}}|${REPO_DIR}|g" \
  -e "s|{{VENV_DIR}}|${VENV_DIR}|g" \
  "${TEMPLATE}" > "${OUT}"

echo "[6/8] unit installieren"
sudo cp "${OUT}" "/etc/systemd/system/${SERVICE_NAME}"
sudo systemctl daemon-reload

echo "[7/8] enable + restart"
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"

echo "[8/8] status"
systemctl status "${SERVICE_NAME}" --no-pager
