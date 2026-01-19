# Spiegel klein (Touch/Relay/RTSP Controller) – FastAPI + MQTT + Home Assistant

Dieses Projekt steuert einen kleinen PC mit Touchscreen („Spiegel klein“):

- USB-Relay (z. B. Display/Power/Backlight)
- schwarzes Overlay als „Screen off“-Zustand (mpv)
- RTSP-Stream Anzeige (mpv)
- Touch-Lock/Unlock (evdev grab/ungrab)
- Wake via Tastatur
- MQTT-Integration inkl. Home Assistant Auto-Discovery (ähnlich TouchKio)

---

## Features

### Web UI
- Statusanzeige (JSON pretty)
- Relais: dauerhaft an, 5-Min-Timer, aus
- RTSP: URL Eingabe, Modus (normal/crop/stretch), Sekunden, Start/Stop
- Touch: disable/enable/lock/unlock
- Overlay-Test: BLACK on/off
- System: Reboot/Shutdown (optional, via `.env` gated)
- Debug + RTSP Log als separate Seiten

### REST API (Auswahl)
- `GET  /status` – Gesamtstatus als JSON
- `POST /relay/on` – Relais dauerhaft an (Overlay aus)
- `POST /relay` – Relais 5 Min (`{"state":"on"}`) / aus (`{"state":"off"}`)
- `POST /relay/off` – sofort aus + Overlay an
- `POST /rtsp/start` – RTSP starten (`{"url":"rtsp://...","seconds":300,"mode":"crop"}`)
- `POST /rtsp/stop` – nur Stream stoppen (kein Idle)
- `POST /touch/lock` / `POST /touch/unlock`
- `POST /overlay/on` / `POST /overlay/off`
- `POST /system/reboot` / `POST /system/shutdown` (nur wenn `ALLOW_POWER_ACTIONS=1`)

### MQTT / Home Assistant
- Base Topic: `kiosk/<hostname>/...` (device_id, device_name und base = Hostname)
- Auto-Discovery via `homeassistant/...`
- Switches: `Screen`, `Touch`, `Overlay black`
- Buttons: `RTSP Start`, `RTSP Stop`, `RTSP Start 5 Min`, `Screen 5 Min`, `Screenshot`, `Reboot`, `Shutdown`
- Text/Select: RTSP URL + RTSP Mode
- Sensoren: Relay/Overlay/RTSP state, Display remaining, Uptime, CPU Temp/Usage, Load, RAM/Disk, IPv4 usw.
- Screenshot als Home Assistant **Image Entity** (JPEG via MQTT Image)

---

## Projektstruktur (Beispiel)

```text
.
├── api.py
├── config.py
├── mqtt_bridge.py
├── requirements.txt
├── example.env
├── .gitignore
├── systemd/
│   └── relay-api.service.template
└── scripts/
    └── install.sh


Voraussetzungen (System)

Debian/Ubuntu mit X11 (kein Wayland, oder XWayland sauber konfiguriert)

mpv (Overlay & RTSP)

ffmpeg (Screenshot via x11grab)

Python 3 + venv

Zugriff auf:

/dev/ttyUSB* (Relay)

/dev/input/... (Touch)

Systempakete installieren
sudo apt-get update
sudo apt-get install -y git python3-venv python3-pip mpv ffmpeg

Installation (neu aufgesetztes OS)
1) Repo klonen
git clone https://github.com/<DEINUSER>/<DEINREPO>.git
cd <DEINREPO>

2) Konfiguration anlegen

.env aus Vorlage kopieren:

cp example.env .env
nano .env


Wichtige Felder:

MQTT_HOST, MQTT_USER, MQTT_PASSWORD

DISPLAY (typisch :0)

XAUTHORITY (oft /home/<user>/.Xauthority, je nach Setup)

DEVICE_RELAY (typisch /dev/ttyUSB0)

TOUCH_DEVICE_PATH (udev-stabil, z. B. /dev/input/touchscreen)

3) Rechte / Gruppen (Touch & Serial)
sudo usermod -aG input,dialout,video $(id -un)


Danach neu einloggen (oder reboot), damit Gruppen greifen.

4) Install-Skript ausführen
chmod +x scripts/install.sh
./scripts/install.sh


Das Script:

erstellt venv im Repo (.venv/)

installiert Python Dependencies

erzeugt systemd Unit aus Template (mit den ermittelten Pfaden)

startet den Service

Service
Status
systemctl status relay-api.service --no-pager

Neustart
sudo systemctl restart relay-api.service

Logs
journalctl -u relay-api.service -f

Web UI

Home: http://<ip-des-geraets>:8000/

Debug: http://<ip-des-geraets>:8000/debug

RTSP Log: http://<ip-des-geraets>:8000/rtsp/log

MQTT / Home Assistant
Base Topic

Der Hostname wird automatisch verwendet:

State: kiosk/<hostname>/state

Availability: kiosk/<hostname>/availability

Commands: kiosk/<hostname>/cmd/...

Beispiel (Hostname: spiegel-schlafzimmer):

kiosk/spiegel-schlafzimmer/state

kiosk/spiegel-schlafzimmer/cmd/rtsp/start

RTSP per MQTT starten (direkt, JSON)

Home Assistant Aktion:

action: mqtt.publish
data:
  topic: "kiosk/spiegel-schlafzimmer/cmd/rtsp/start"
  payload: '{"url":"rtsp://192.168.10.36:8554/Eingang","seconds":300,"mode":"crop"}'

Screenshot per MQTT
action: mqtt.publish
data:
  topic: "kiosk/spiegel-schlafzimmer/cmd/screenshot"
  payload: "PRESS"


Der Screenshot wird als JPEG auf kiosk/<hostname>/screen/image publiziert und in Home Assistant als Image Entity angezeigt.

RTSP Modes

normal: 1:1 / Aspect beibehalten

crop: auf 1080×1920 füllen (ohne Verzerrung, seitlich crop)

stretch: auf 1080×1920 strecken (mit Verzerrung)

Sicherheit
Reboot/Shutdown

Diese Aktionen sind absichtlich per .env abgesichert:

ALLOW_POWER_ACTIONS=0 → Buttons/Endpoints existieren, führen aber nicht aus

ALLOW_POWER_ACTIONS=1 → Reboot/Shutdown wird ausgeführt

Troubleshooting
Touch lockt nicht / Touch geht trotzdem ans OS

Gruppen prüfen: groups

User muss in input sein → danach neu einloggen/reboot

Device-Pfad prüfen: ls -l /dev/input/touchscreen

RTSP startet nicht aus dem Service, aber per SSH schon

DISPLAY/XAUTHORITY in .env prüfen

mpv braucht Zugriff auf die X11 Session (korrekte Xauthority)

RTSP Log prüfen: http://<ip>:8000/rtsp/log oder Datei aus .env (RTSP_LOG_PATH)

MQTT Entities erscheinen nicht

MQTT Host/User/Pass prüfen

HA MQTT Integration aktiv?

Discovery Prefix korrekt (MQTT_DISCOVERY_PREFIX, default homeassistant)

Broker erreichbar (Firewall/VLAN)

Screenshot funktioniert nicht

ffmpeg installiert?

X11 läuft und DISPLAY stimmt?

Logs prüfen: journalctl -u relay-api.service -f (Suche nach Screenshot error)
