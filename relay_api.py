from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import serial
import time
import threading
from collections import deque

from evdev import InputDevice, ecodes

import os
import base64
import subprocess
import signal
import getpass

# --------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------

DEVICE_RELAY = "/dev/ttyUSB0"
BAUDRATE = 9600
RELAY_ON_TIME = 300  # 5 Minuten

TOUCH_DEVICE_PATH = "/dev/input/touchscreen"  # stabil per udev

UNLOCK_TOUCHES = 10
UNLOCK_WINDOW = 10  # Sekunden

# --- Black overlay (mpv) ---
BLACK_PNG_PATH = "/tmp/relay_black.png"

# Wenn DISPLAY nicht gesetzt ist (typisch bei systemd), nehmen wir :0
MPV_DISPLAY_DEFAULT = ":0"

# RTSP
RTSP_DEFAULT_URL = "rtsp://192.168.10.36:8554/Eingang"
RTSP_DEFAULT_SECONDS = 300
RTSP_LOG_PATH = "/tmp/mpv_rtsp.log"

BLACK_PNG_B64 = """
iVBORw0KGgoAAAANSUhEUgAAB4AAAAQ4AQAAAADAqPzuAAAAIGNIUk0AAHomAACAhAAA+gAAAIDo
AAB1MAAA6mAAADqYAAAXcJy6UTwAAAACYktHRAAB3YoTpAAAAAd0SU1FB+oBBRMPE2zzC/4AAAET
SURBVHja7cEBDQAAAMKg909tDwcUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADApwH45QAB/OGP/gAA
ACV0RVh0ZGF0ZTpjcmVhdGUAMjAyNi0wMS0wNVQxOToxNToxOCswMDowMBGu0N8AAAAldEVYdGRh
dGU6bW9kaWZ5ADIwMjYtMDEtMDVUMTk6MTU6MTgrMDA6MDBg82hjAAAAKHRFWHRkYXRlOnRpbWVz
dGFtcAAyMDI2LTAxLTA1VDE5OjE1OjE4KzAwOjAwN+ZJvAAAAABJRU5ErkJggg==
""".strip()

# --------------------------------------------------------------------
# Globale Zustände
# --------------------------------------------------------------------

app = FastAPI(title="USB Relay Control + Touch Lock + RTSP", version="5.3")

relay_lock = threading.Lock()
relay_timer = None

touch_disabled = False   # OS bekommt kein Touch (grab), Script reagiert weiter (inkl. 10x Unlock)
touch_locked = False     # Hard-Lock: OS bekommt kein Touch (grab) UND Script ignoriert Touch komplett

touch_device = None  # InputDevice für /dev/input/touchscreen

unlock_counter = 0
unlock_start_time = None

touch_events = deque(maxlen=120)
touch_events_lock = threading.Lock()

# mpv Prozessverwaltung
mpv_lock = threading.Lock()
overlay_proc = None  # schwarzes Bild (mpv black.png)
stream_proc = None   # RTSP Stream (mpv rtsp://...)
stream_timer = None
stream_current_url = None
stream_end_ts = None


# --------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------

def log_touch(msg: str):
    line = f"{time.strftime('%H:%M:%S')} - {msg}"
    with touch_events_lock:
        touch_events.appendleft(line)
    print(msg)


# --------------------------------------------------------------------
# GUI / DISPLAY / XAUTHORITY
# --------------------------------------------------------------------

def find_xauthority() -> str:
    # 1) wenn env gesetzt und existiert -> nehmen
    xa = os.environ.get("XAUTHORITY", "")
    if xa and os.path.exists(xa):
        return xa

    candidates = []

    # wenn via sudo gestartet
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        candidates.append(f"/home/{sudo_user}/.Xauthority")

    # aktueller user
    candidates.append(f"/home/{getpass.getuser()}/.Xauthority")

    # häufig: conrad
    candidates.append("/home/conrad/.Xauthority")

    # gdm / run user (best effort)
    candidates.append("/run/user/1000/gdm/Xauthority")
    candidates.append("/run/user/1000/.Xauthority")

    for p in candidates:
        if os.path.exists(p):
            return p

    return ""


def _gui_env():
    env = os.environ.copy()
    env["DISPLAY"] = os.environ.get("DISPLAY", MPV_DISPLAY_DEFAULT)
    xa = find_xauthority()
    if xa:
        env["XAUTHORITY"] = xa
    return env


def screen_on():
    """
    Best effort: Bildschirm via X11 DPMS an.
    (Wenn xset fehlt oder kein X11-Kontext -> keine harte Fehlermeldung)
    """
    try:
        subprocess.run(
            ["xset", "dpms", "force", "on"],
            env=_gui_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["xset", "s", "reset"],
            env=_gui_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        log_touch("Display: screen_on() ausgeführt (xset).")
    except FileNotFoundError:
        log_touch("Display: xset nicht gefunden (apt install x11-xserver-utils).")
    except Exception as e:
        log_touch(f"Display: screen_on Fehler: {e}")


# --------------------------------------------------------------------
# Black overlay (mpv black.png)
# --------------------------------------------------------------------

def ensure_black_png_file() -> str:
    if os.path.exists(BLACK_PNG_PATH) and os.path.getsize(BLACK_PNG_PATH) > 0:
        return BLACK_PNG_PATH

    data = base64.b64decode(BLACK_PNG_B64.encode("ascii"))
    with open(BLACK_PNG_PATH, "wb") as f:
        f.write(data)

    try:
        os.chmod(BLACK_PNG_PATH, 0o644)
    except Exception:
        pass

    return BLACK_PNG_PATH


def _kill_proc_group(proc, name: str):
    if not proc or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        for _ in range(30):
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        log_touch(f"{name}: beendet.")
    except Exception as e:
        log_touch(f"{name}: Fehler beim Beenden: {e}")


def start_black_overlay():
    global overlay_proc
    with mpv_lock:
        if overlay_proc and overlay_proc.poll() is None:
            return

        png_path = ensure_black_png_file()
        cmd = [
            "mpv",
            "--no-terminal",
            "--fs",
            "--ontop",
            "--no-osc",
            "--vo=gpu",
            png_path
        ]

        try:
            overlay_proc = subprocess.Popen(
                cmd,
                env=_gui_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            log_touch("Black overlay: gestartet (Relay OFF).")
        except FileNotFoundError:
            log_touch("Black overlay: mpv nicht gefunden (installiere mpv).")
            overlay_proc = None
        except Exception as e:
            log_touch(f"Black overlay: Startfehler: {e}")
            overlay_proc = None


def stop_black_overlay():
    global overlay_proc
    with mpv_lock:
        _kill_proc_group(overlay_proc, "Black overlay")
        overlay_proc = None


# --------------------------------------------------------------------
# Relais-Steuerung
# --------------------------------------------------------------------

def send_command(data: bytes):
    with serial.Serial(DEVICE_RELAY, baudrate=BAUDRATE, timeout=1) as ser:
        ser.write(data)


def relay_on():
    # sobald Relais AN: schwarzes Overlay weg
    stop_black_overlay()
    send_command(b"\xA0\x01\x01\xA2")
    print("Relay ON")


def relay_off():
    send_command(b"\xA0\x01\x00\xA1")
    print("Relay OFF")
    # sobald Relais AUS: schwarzes Overlay an
    start_black_overlay()


def read_status():
    with serial.Serial(DEVICE_RELAY, baudrate=BAUDRATE, timeout=1) as ser:
        ser.reset_input_buffer()
        ser.write(b"\xFF")
        time.sleep(0.2)
        response = ser.read(64).decode(errors="ignore").strip()

    if "ON" in response:
        return "ON"
    if "OFF" in response:
        return "OFF"
    return f"UNKNOWN ({response})"


def activate_for(duration: int):
    """Schaltet das Relais für 'duration' Sekunden ein (Timer wird immer neu gesetzt)."""
    global relay_timer
    with relay_lock:
        if relay_timer and relay_timer.is_alive():
            relay_timer.cancel()
        relay_on()
        relay_timer = threading.Timer(duration, relay_off)
        relay_timer.start()
        print(f"Relay aktiviert für {duration} Sekunden")


# --------------------------------------------------------------------
# Touch-Lock (grab/ungrab)
# --------------------------------------------------------------------

def ensure_touch_device():
    global touch_device
    if touch_device is None:
        dev = InputDevice(TOUCH_DEVICE_PATH)
        touch_device = dev
        log_touch(f"Touch-Device geöffnet: {dev.path} ({dev.name})")


def apply_touch_state():
    """Sperrt/entsperrt Touch für das Betriebssystem (grab/ungrab)."""
    ensure_touch_device()
    global touch_disabled, touch_locked, touch_device

    try:
        if touch_disabled or touch_locked:
            touch_device.grab()
            log_touch("Touch via grab() für das System deaktiviert.")
        else:
            touch_device.ungrab()
            log_touch("Touch via ungrab() für das System wieder aktiviert.")
    except Exception as e:
        log_touch(f"Fehler bei grab/ungrab: {e}")


def disable_touch():
    global touch_disabled
    touch_disabled = True
    apply_touch_state()
    return touch_disabled


def enable_touch():
    global touch_disabled, unlock_counter, unlock_start_time
    touch_disabled = False
    unlock_counter = 0
    unlock_start_time = None
    apply_touch_state()
    return touch_disabled


def lock_touch():
    """Hard-Lock: Touch geht weder ans OS noch löst er im Script etwas aus."""
    global touch_locked, unlock_counter, unlock_start_time
    touch_locked = True
    unlock_counter = 0
    unlock_start_time = None
    apply_touch_state()
    return touch_locked


def unlock_touch():
    global touch_locked
    touch_locked = False
    apply_touch_state()
    return touch_locked


# --------------------------------------------------------------------
# RTSP Stream (mpv)
# --------------------------------------------------------------------

def rtsp_running() -> bool:
    global stream_proc
    return stream_proc is not None and stream_proc.poll() is None


def start_rtsp_stream(url: str, seconds: int):
    """
    Ablauf:
    - Display an
    - Overlay weg
    - Stream starten (mpv)
    - Relais an für seconds
    - Timer: nach seconds -> Stream beenden -> Relais aus -> Schwarz
    """
    global stream_proc, stream_timer, stream_current_url, stream_end_ts

    if seconds <= 0:
        seconds = RTSP_DEFAULT_SECONDS

    with mpv_lock:
        # laufenden Stream killen
        _kill_proc_group(stream_proc, "RTSP Stream")
        stream_proc = None

        # Overlay weg
        stop_black_overlay()

        # Bildschirm an
        screen_on()

        cmd = [
            "mpv",
            "--no-terminal",
            "--fs",
            "--ontop",
            "--no-osc",
            "--vo=gpu",
            "--rtsp-transport=tcp",
            "--profile=low-latency",
            "--cache=no",
            url
        ]

        # Logfile anhängen, damit du Fehler siehst
        try:
            with open(RTSP_LOG_PATH, "a", buffering=1) as logf:
                env = _gui_env()
                logf.write(f"\n--- {time.strftime('%F %T')} START ---\n")
                logf.write("CMD: " + " ".join(cmd) + "\n")
                logf.write(f"DISPLAY={env.get('DISPLAY')} XAUTHORITY={env.get('XAUTHORITY','')}\n")
        except Exception:
            pass

        try:
            logf = open(RTSP_LOG_PATH, "a", buffering=1)
            stream_proc = subprocess.Popen(
                cmd,
                env=_gui_env(),
                stdout=logf,
                stderr=logf,
                start_new_session=True,
            )
            stream_current_url = url
            stream_end_ts = time.time() + seconds
            log_touch(f"RTSP Stream: gestartet ({url}) für {seconds}s. Log: {RTSP_LOG_PATH}")
        except FileNotFoundError:
            log_touch("RTSP Stream: mpv nicht gefunden (installiere mpv).")
            stream_proc = None
            stream_current_url = None
            stream_end_ts = None
            return
        except Exception as e:
            log_touch(f"RTSP Stream: Startfehler: {e} (siehe {RTSP_LOG_PATH})")
            stream_proc = None
            stream_current_url = None
            stream_end_ts = None
            return

    # Relais für dieselbe Dauer aktiv
    activate_for(seconds)

    # Timer neu setzen
    with relay_lock:
        if stream_timer and stream_timer.is_alive():
            stream_timer.cancel()
        stream_timer = threading.Timer(seconds, stop_rtsp_stream_and_idle)
        stream_timer.start()


def stop_rtsp_stream_and_idle():
    """
    Beendet Stream -> Relais AUS -> Schwarz.
    """
    global stream_proc, stream_current_url, stream_end_ts

    with mpv_lock:
        _kill_proc_group(stream_proc, "RTSP Stream")
        stream_proc = None
        stream_current_url = None
        stream_end_ts = None

    relay_off()
    log_touch("RTSP Ablauf: fertig -> Relais OFF -> Schwarz -> warte auf neues Event.")


# --------------------------------------------------------------------
# Touch-Monitor
# --------------------------------------------------------------------

def monitor_touch():
    """Liest kontinuierlich Events von /dev/input/touchscreen."""
    global unlock_counter, unlock_start_time, touch_disabled, touch_locked

    try:
        ensure_touch_device()
        dev = touch_device
        log_touch(f"Starte Touch-Monitor auf {dev.path} ({dev.name})")

        for event in dev.read_loop():
            if event.type not in (ecodes.EV_KEY, ecodes.EV_ABS):
                continue

            if touch_locked:
                log_touch("Touch ignoriert (HARD-LOCK aktiv)")
                continue

            # Touch zählt -> Relais an
            log_touch("Touch erkannt → Relais 5 Minuten AN")
            activate_for(RELAY_ON_TIME)

            if touch_disabled:
                now = time.time()

                if unlock_start_time is None:
                    unlock_start_time = now
                    unlock_counter = 1
                else:
                    if now - unlock_start_time > UNLOCK_WINDOW:
                        unlock_start_time = now
                        unlock_counter = 1
                    else:
                        unlock_counter += 1

                log_touch(f"Unlock-Touch: {unlock_counter}/{UNLOCK_TOUCHES}")

                if unlock_counter >= UNLOCK_TOUCHES:
                    log_touch(">>> Entsperrmuster erkannt → Touch wird wieder aktiviert!")
                    enable_touch()

    except FileNotFoundError:
        log_touch(f"{TOUCH_DEVICE_PATH} nicht gefunden – Touch-Monitor beendet.")
    except PermissionError:
        log_touch(f"⚠️ Keine Berechtigung für {TOUCH_DEVICE_PATH} – User braucht Zugriff auf /dev/input.")
    except Exception as e:
        log_touch(f"Fehler im Touch-Monitor: {e}")


# --------------------------------------------------------------------
# FastAPI Modelle
# --------------------------------------------------------------------

class RelayAction(BaseModel):
    state: str  # "on" oder "off"


class RtspRequest(BaseModel):
    url: str = Field(default=RTSP_DEFAULT_URL)
    seconds: int = Field(default=RTSP_DEFAULT_SECONDS, ge=5, le=3600)


# --------------------------------------------------------------------
# API-Routen
# --------------------------------------------------------------------

@app.get("/status")
def api_status():
    running = rtsp_running()
    remaining = int(stream_end_ts - time.time()) if (stream_end_ts and running) else 0
    return {
        "relay": read_status(),
        "touch_disabled": touch_disabled,
        "touch_locked": touch_locked,
        "touch_device": TOUCH_DEVICE_PATH,
        "rtsp_running": running,
        "rtsp_url": stream_current_url,
        "rtsp_remaining": max(0, remaining),
        "display": _gui_env().get("DISPLAY"),
        "xauthority": _gui_env().get("XAUTHORITY", ""),
    }


@app.post("/switch")
def api_switch(action: RelayAction):
    s = action.state.lower()
    if s == "on":
        activate_for(RELAY_ON_TIME)
    elif s == "off":
        relay_off()
    else:
        return {"error": "Use 'on' or 'off'."}
    return {"relay": read_status()}


@app.post("/trigger5min")
def api_trigger():
    activate_for(RELAY_ON_TIME)
    return {"relay": read_status()}


@app.post("/touch/disable")
def api_touch_disable():
    disable_touch()
    return {"touch_disabled": touch_disabled, "touch_locked": touch_locked}


@app.post("/touch/enable")
def api_touch_enable():
    enable_touch()
    return {"touch_disabled": touch_disabled, "touch_locked": touch_locked}


@app.post("/touch/lock")
def api_touch_lock():
    lock_touch()
    return {"touch_disabled": touch_disabled, "touch_locked": touch_locked}


@app.post("/touch/unlock")
def api_touch_unlock():
    unlock_touch()
    return {"touch_disabled": touch_disabled, "touch_locked": touch_locked}


@app.get("/touch/status")
def api_touch_status():
    return {
        "touch_disabled": touch_disabled,
        "touch_locked": touch_locked,
        "touch_device": TOUCH_DEVICE_PATH,
    }


@app.post("/rtsp/start")
def api_rtsp_start(req: RtspRequest):
    start_rtsp_stream(req.url, req.seconds)
    return {"ok": True, "url": req.url, "seconds": req.seconds}


@app.post("/rtsp/stop")
def api_rtsp_stop():
    stop_rtsp_stream_and_idle()
    return {"ok": True}


@app.get("/rtsp/log", response_class=HTMLResponse)
def api_rtsp_log():
    try:
        if not os.path.exists(RTSP_LOG_PATH):
            return HTMLResponse("<pre>Kein RTSP-Log vorhanden.</pre>")
        with open(RTSP_LOG_PATH, "r", errors="ignore") as f:
            txt = f.read()[-20000:]
        return HTMLResponse("<pre>" + txt + "</pre>")
    except Exception as e:
        return HTMLResponse(f"<pre>Fehler beim Lesen: {e}</pre>")


# --------------------------------------------------------------------
# Web UI
# --------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def ui():
    status = read_status()
    color = "#4CAF50" if status == "ON" else "#E53935"

    if touch_locked:
        touch_text = "GESPERRT (Hard-Lock: kein Touch, nicht ans OS)"
    elif touch_disabled:
        touch_text = "System-gesperrt (Unlock per 10x Touch)"
    else:
        touch_text = "aktiv"

    touch_label = "Touch deaktivieren" if not touch_disabled else "Touch aktivieren"
    lock_label = "Touch entsperren" if touch_locked else "Touch sperren"

    html = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <title>USB Relay Steuerung</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                text-align: center;
                margin-top: 40px;
            }}
            .status {{
                color: {color};
                font-size: 2em;
                font-weight: bold;
                margin-bottom: 10px;
            }}
            .line {{
                margin-bottom: 12px;
                font-size: 1.05em;
            }}
            button {{
                padding: 12px 22px;
                margin: 8px;
                font-size: 1.05em;
                border-radius: 10px;
                border: none;
                color: white;
                cursor: pointer;
            }}
            .on {{ background: #4CAF50; }}
            .off {{ background: #E53935; }}
            .refresh {{ background: #1976D2; }}
            .trigger {{ background: #00897B; }}
            .touch {{ background: #8E24AA; }}
            .lock {{ background: #5E35B1; }}
            .rtsp {{ background: #6D4C41; }}
            a {{ color: #1976D2; }}
        </style>
    </head>
    <body>
        <h1>USB Relay Steuerung</h1>

        <div id="relayStatus" class="status">Relais: {status}</div>
        <div id="touchState" class="line">Touch: {touch_text}</div>
        <div id="rtspState" class="line">RTSP: unbekannt</div>

        <button class="on" onclick="send('on')">Einschalten</button>
        <button class="off" onclick="send('off')">Ausschalten</button>
        <button class="trigger" onclick="trigger5()">5 Minuten</button>

        <button class="rtsp" onclick="rtspStart()">RTSP Eingang (5 Min)</button>
        <button class="rtsp" onclick="rtspStop()">RTSP Stop</button>

        <button class="refresh" onclick="refreshStatus()">Aktualisieren</button>
        <button id="touchButton" class="touch" onclick="toggleTouch()">{touch_label}</button>
        <button id="lockButton" class="lock" onclick="toggleLock()">{lock_label}</button>

        <br><br>
        <a href="/debug" target="_blank">Debug anzeigen</a>
        &nbsp;|&nbsp;
        <a href="/rtsp/log" target="_blank">RTSP Log</a>

        <script>
            async function send(state) {{
                await fetch('/switch', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ state }})
                }});
                refreshStatus();
            }}

            async function trigger5() {{
                await fetch('/trigger5min', {{ method: 'POST' }});
                refreshStatus();
            }}

            async function toggleTouch() {{
                const st = await fetch('/touch/status').then(r => r.json());
                if (st.touch_disabled) {{
                    await fetch('/touch/enable', {{ method: 'POST' }});
                }} else {{
                    await fetch('/touch/disable', {{ method: 'POST' }});
                }}
                refreshStatus();
            }}

            async function toggleLock() {{
                const st = await fetch('/touch/status').then(r => r.json());
                if (st.touch_locked) {{
                    await fetch('/touch/unlock', {{ method: 'POST' }});
                }} else {{
                    await fetch('/touch/lock', {{ method: 'POST' }});
                }}
                refreshStatus();
            }}

            async function rtspStart() {{
                await fetch('/rtsp/start', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ url: '{RTSP_DEFAULT_URL}', seconds: {RTSP_DEFAULT_SECONDS} }})
                }});
                refreshStatus();
            }}

            async function rtspStop() {{
                await fetch('/rtsp/stop', {{ method: 'POST' }});
                refreshStatus();
            }}

            async function refreshStatus() {{
                const st = await fetch('/status').then(r => r.json());

                const relayEl = document.getElementById('relayStatus');
                const touchEl = document.getElementById('touchState');
                const rtspEl = document.getElementById('rtspState');
                const touchBtn = document.getElementById('touchButton');
                const lockBtn = document.getElementById('lockButton');

                relayEl.textContent = 'Relais: ' + st.relay;
                relayEl.style.color = (st.relay === 'ON') ? '#4CAF50' : '#E53935';

                if (st.touch_locked) {{
                    touchEl.textContent = 'Touch: GESPERRT (Hard-Lock)';
                    lockBtn.textContent = 'Touch entsperren';
                }} else {{
                    lockBtn.textContent = 'Touch sperren';
                    touchEl.textContent = 'Touch: ' + (st.touch_disabled ? 'System-gesperrt (Unlock per 10x Touch)' : 'aktiv');
                }}

                touchBtn.textContent = st.touch_disabled ? 'Touch aktivieren' : 'Touch deaktivieren';

                if (st.rtsp_running) {{
                    rtspEl.textContent = 'RTSP: läuft (' + (st.rtsp_remaining || 0) + 's)';
                }} else {{
                    rtspEl.textContent = 'RTSP: aus';
                }}
            }}

            window.addEventListener('load', refreshStatus);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)


# --------------------------------------------------------------------
# Debug-Seite
# --------------------------------------------------------------------

@app.get("/debug", response_class=HTMLResponse)
def debug_ui():
    with touch_events_lock:
        events = list(touch_events)

    items = "<br>".join(events) if events else "Keine Events aufgezeichnet."

    running = rtsp_running()
    remaining = int(stream_end_ts - time.time()) if (stream_end_ts and running) else 0

    html = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <title>Debug</title>
        <meta http-equiv="refresh" content="3">
    </head>
    <body style="font-family:monospace; background:#111; color:#eee; padding:20px;">
        <h2>Debug</h2>
        <p>Touch: {"HARD-LOCK" if touch_locked else ("DISABLED" if touch_disabled else "AKTIV")}</p>
        <p>Device: {TOUCH_DEVICE_PATH}</p>
        <p>Relais: {read_status()}</p>
        <p>RTSP: {"läuft" if running else "aus"} {f"(Rest {max(0,remaining)}s)" if running else ""}</p>
        <p>DISPLAY: {_gui_env().get("DISPLAY")}</p>
        <p>XAUTHORITY: {_gui_env().get("XAUTHORITY","")}</p>
        <h3>Letzte Events:</h3>
        <div style="white-space:pre-line;">{items}</div>
        <p><a href="/" style="color:#64B5F6;">Zurück</a></p>
    </body>
    </html>
    """
    return HTMLResponse(html)


# --------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    ensure_black_png_file()

    # Relais beim Start 5 Minuten an (Overlay weg)
    activate_for(RELAY_ON_TIME)

    # Touch-Monitor im Hintergrund
    t = threading.Thread(target=monitor_touch, daemon=True)
    t.start()
