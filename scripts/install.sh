#!/usr/bin/env bash
set -euo pipefail

# immer aus Repo-Root arbeiten
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(id -un)"
VENV_DIR="${REPO_DIR}/.venv"

SERVICE_API="relay-api.service"
SERVICE_RTSP="rtsp-server.service"

TEMPLATE_API="${REPO_DIR}/systemd/relay-api.service.template"
TEMPLATE_RTSP="${REPO_DIR}/systemd/rtsp-server.service.template"

OUT_API="${REPO_DIR}/systemd/${SERVICE_API}"
OUT_RTSP="${REPO_DIR}/systemd/${SERVICE_RTSP}"

SUDOERS_FILE="/etc/sudoers.d/spiegel-streaming"

echo "[1/12] Repo dir: ${REPO_DIR}"
echo "[2/12] Install system packages"

sudo apt-get update

# Basis
sudo apt-get install -y \
  git \
  python3-venv python3-pip \
  mpv \
  ffmpeg \
  v4l-utils

# v4l2loopback + GStreamer + RTSP Server bindings
sudo apt-get install -y \
  v4l2loopback-utils \
  gstreamer1.0-tools gstreamer1.0-vaapi \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-rtsp \
  python3-gi python3-gst-1.0 \
  gir1.2-gst-rtsp-server-1.0

echo "[3/12] Ensure user groups (input, dialout, video)"
NEED_RELOG=0

ensure_group() {
  local grp="$1"
  if getent group "${grp}" >/dev/null 2>&1; then
    if id -nG "${USER_NAME}" | tr ' ' '\n' | grep -qx "${grp}"; then
      echo "  - ${USER_NAME} already in group: ${grp}"
    else
      echo "  - adding ${USER_NAME} to group: ${grp}"
      sudo usermod -aG "${grp}" "${USER_NAME}"
      NEED_RELOG=1
    fi
  else
    echo "  - group not found (skip): ${grp}"
  fi
}

ensure_group "input"
ensure_group "dialout"
ensure_group "video"

echo "[4/12] Create/Update venv: ${VENV_DIR}"
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip

echo "[5/12] Install Python deps from requirements.txt"
if [[ ! -f "${REPO_DIR}/requirements.txt" ]]; then
  echo "❌ requirements.txt fehlt im Repo-Root."
  exit 1
fi
"${VENV_DIR}/bin/pip" install -r "${REPO_DIR}/requirements.txt"

echo "[6/12] Check .env"
if [[ ! -f "${REPO_DIR}/.env" ]]; then
  echo "⚠️  ${REPO_DIR}/.env fehlt."
  echo "    Kopiere example.env -> .env und trage die Werte ein:"
  echo "    cp ${REPO_DIR}/example.env ${REPO_DIR}/.env"
  exit 1
fi

echo "[7/12] Optional: enable v4l2loopback at boot (if configured)"
# Lies .env ein, um V4L2LOOPBACK_ENABLE zu erkennen
set +u
# shellcheck disable=SC1090
source "${REPO_DIR}/.env" >/dev/null 2>&1 || true
set -u

if [[ "${V4L2LOOPBACK_ENABLE:-0}" == "1" ]]; then
  echo "  - enabling v4l2loopback module load at boot"
  sudo mkdir -p /etc/modules-load.d
  echo "v4l2loopback" | sudo tee /etc/modules-load.d/v4l2loopback.conf >/dev/null

  if ! lsmod | grep -q "^v4l2loopback"; then
    sudo modprobe v4l2loopback || true
  fi
else
  echo "  - V4L2LOOPBACK_ENABLE not set to 1 -> skip"
fi

echo "[8/12] Install sudoers rule for rtsp-server.service control"
# systemctl ist je nach distro /bin/systemctl oder /usr/bin/systemctl
SYSTEMCTL_BIN="$(command -v systemctl || true)"
if [[ -z "${SYSTEMCTL_BIN}" ]]; then
  echo "❌ systemctl nicht gefunden."
  exit 1
fi

# minimal erlaubte Kommandos: start/stop/is-active nur für rtsp-server.service
TMP_SUDOERS="$(mktemp)"
cat > "${TMP_SUDOERS}" <<EOF
${USER_NAME} ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} start rtsp-server.service, ${SYSTEMCTL_BIN} stop rtsp-server.service, ${SYSTEMCTL_BIN} is-active rtsp-server.service, /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff
EOF

# Syntax prüfen bevor wir es aktivieren
if sudo visudo -cf "${TMP_SUDOERS}" >/dev/null; then
  sudo install -m 0440 "${TMP_SUDOERS}" "${SUDOERS_FILE}"
  echo "  - installed: ${SUDOERS_FILE}"
else
  echo "❌ visudo check failed, sudoers not installed."
  rm -f "${TMP_SUDOERS}"
  exit 1
fi
rm -f "${TMP_SUDOERS}"

echo "[9/12] Generate systemd units from templates"
for tpl in "${TEMPLATE_API}" "${TEMPLATE_RTSP}"; do
  if [[ ! -f "${tpl}" ]]; then
    echo "❌ Template fehlt: ${tpl}"
    exit 1
  fi
done

render_unit () {
  local in_tpl="$1"
  local out_file="$2"
  sed \
    -e "s|{{USER}}|${USER_NAME}|g" \
    -e "s|{{APP_DIR}}|${REPO_DIR}|g" \
    -e "s|{{VENV_DIR}}|${VENV_DIR}|g" \
    "${in_tpl}" > "${out_file}"
}

render_unit "${TEMPLATE_API}" "${OUT_API}"
render_unit "${TEMPLATE_RTSP}" "${OUT_RTSP}"

echo "[10/12] Install units to /etc/systemd/system/"
sudo cp "${OUT_API}" "/etc/systemd/system/${SERVICE_API}"
sudo cp "${OUT_RTSP}" "/etc/systemd/system/${SERVICE_RTSP}"

sudo systemctl daemon-reload

echo "[11/12] Enable + Restart services"
sudo systemctl enable "${SERVICE_API}" "${SERVICE_RTSP}"
sudo systemctl restart "${SERVICE_RTSP}"
sudo systemctl restart "${SERVICE_API}"

echo "[12/12] Show status"
systemctl status "${SERVICE_RTSP}" --no-pager || true
systemctl status "${SERVICE_API}" --no-pager || true

echo "✅ Installation abgeschlossen."

if [[ "${NEED_RELOG}" == "1" ]]; then
  echo ""
  echo "⚠️  WICHTIG: Gruppen wurden geändert."
  echo "   Bitte einmal neu einloggen oder rebooten, damit input/dialout/video greifen."
  echo "   Reboot: sudo reboot"
fi

echo ""
echo "Hinweis: Streaming-Schalter nutzt sudoers in ${SUDOERS_FILE} (start/stop/is-active rtsp-server.service)."
