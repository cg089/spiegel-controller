from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

import os
import time
import html
import json
import socket
import shutil
import subprocess
import threading
from typing import Optional, Tuple

import config
from event_log import EventLog
from relay import RelayController
from display_ctl import DisplayController
from overlay_black import BlackOverlay
from rtsp_player import RtspPlayer
from touch_ctl import TouchController
from keyboard_wake import KeyboardWake
from mqtt_bridge import MqttBridge

# Base64 black png
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

app = FastAPI(title="Spiegel klein", version="7.1-mqtt-streaming")

hostname = socket.gethostname()
log = EventLog(maxlen=400)

display = DisplayController(display_default=config.DISPLAY, xauthority_env=config.XAUTHORITY, log=log)
relay = RelayController(config.DEVICE_RELAY, config.BAUDRATE, log)
overlay = BlackOverlay(config.BLACK_PNG_PATH, BLACK_PNG_B64, display, log)
rtsp = RtspPlayer(display, overlay, relay, log, config.RTSP_LOG_PATH)
touch = TouchController(config.TOUCH_DEVICE_PATH, config.UNLOCK_TOUCHES, config.UNLOCK_WINDOW, log)

# Touch-Event: Display wake + Relay 5min + Overlay off während aktiv
def on_touch_event():
    display.wake()
    relay.activate_for(config.RELAY_ON_TIME, on_start=overlay.hide, on_end=overlay.show)

touch.set_on_touch(on_touch_event)

# Tastendruck: Display wake + Relay 5min
kbd = KeyboardWake(
    log,
    on_keypress=lambda: (display.wake(), relay.activate_for(config.RELAY_ON_TIME, on_start=overlay.hide, on_end=overlay.show))
)

# RTSP config buffer (für HA Buttons "Start" ohne Payload)
rtsp_cfg = {"url": config.RTSP_DEFAULT_URL, "mode": "normal", "seconds": config.RTSP_DEFAULT_SECONDS}

# Lokaler RTSP-Server Stream (Browser kann RTSP nicht direkt -> Snapshot via ffmpeg)
LOCAL_CAMERA_RTSP_URL = "rtsp://127.0.0.1:8554/stream"


class RtspRequest(BaseModel):
    url: str = Field(default=config.RTSP_DEFAULT_URL)
    seconds: int = Field(default=config.RTSP_DEFAULT_SECONDS, ge=5, le=3600)
    mode: str = Field(default="normal")  # normal | crop | stretch


class RelayAction(BaseModel):
    state: str  # on/off


class StreamingAction(BaseModel):
    state: str  # on/off


# ---------- Systemdaten (ohne extra deps) ----------

_cpu_prev: Optional[Tuple[int, int]] = None  # (total, idle)

def cpu_usage_pct() -> float:
    """CPU usage % via /proc/stat delta."""
    global _cpu_prev
    try:
        with open("/proc/stat", "r") as f:
            line = f.readline().strip()
        parts = line.split()
        if parts[0] != "cpu":
            return 0.0
        nums = list(map(int, parts[1:]))
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
        total = sum(nums)

        if _cpu_prev is None:
            _cpu_prev = (total, idle)
            return 0.0

        prev_total, prev_idle = _cpu_prev
        _cpu_prev = (total, idle)

        dt = total - prev_total
        di = idle - prev_idle
        if dt <= 0:
            return 0.0
        usage = (dt - di) / dt * 100.0
        return round(max(0.0, min(100.0, usage)), 1)
    except Exception:
        return 0.0


def _read_cpu_temp_c():
    paths = [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/class/hwmon/hwmon0/temp1_input",
    ]
    for p in paths:
        try:
            if os.path.exists(p):
                v = open(p, "r").read().strip()
                if not v:
                    continue
                n = float(v)
                if n > 1000:
                    n = n / 1000.0
                return round(n, 1)
        except Exception:
            pass
    return None


def _read_uptime_seconds():
    try:
        with open("/proc/uptime", "r") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return None


def _read_load1():
    try:
        with open("/proc/loadavg", "r") as f:
            return float(f.read().split()[0])
    except Exception:
        return None


def _read_mem_used_pct():
    try:
        mem = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mem[parts[0].rstrip(":")] = int(parts[1])
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        if total <= 0:
            return None
        used = total - avail
        return int(round((used / total) * 100))
    except Exception:
        return None


def _read_disk_used_pct(path="/"):
    try:
        du = shutil.disk_usage(path)
        if du.total <= 0:
            return None
        used = du.used / du.total * 100
        return int(round(used))
    except Exception:
        return None


def _read_ips():
    try:
        out = subprocess.check_output(["/usr/sbin/ip", "-4", "addr"], stderr=subprocess.DEVNULL).decode("utf-8", "ignore")
    except Exception:
        try:
            out = subprocess.check_output(["ip", "-4", "addr"], stderr=subprocess.DEVNULL).decode("utf-8", "ignore")
        except Exception:
            return []
    ips = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("inet "):
            ip = line.split()[1].split("/")[0]
            if ip != "127.0.0.1":
                ips.append(ip)
    return ips


def take_screenshot_jpeg() -> Optional[bytes]:
    """Screenshot vom Root-Window (X11)."""
    env = display.env()
    tmp = "/tmp/spiegel_screen.jpg"
    try:
        cmd = [
            "ffmpeg",
            "-loglevel", "error",
            "-y",
            "-f", "x11grab",
            "-i", f"{env.get('DISPLAY', ':0')}.0",
            "-frames:v", "1",
            "-q:v", "3",
            tmp
        ]
        subprocess.run(cmd, env={**os.environ, **env}, check=True, timeout=5)
        with open(tmp, "rb") as f:
            return f.read()
    except Exception as e:
        log.add(f"Screenshot error: {e}")
        return None


def camera_snapshot_jpeg(rtsp_url: str = LOCAL_CAMERA_RTSP_URL) -> Optional[bytes]:
    """
    Holt 1 Frame als JPEG aus einem RTSP-Stream via ffmpeg (pipe).
    Browser kann RTSP nicht direkt, daher Snapshot.
    """
    try:
        cmd = [
            "ffmpeg",
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-frames:v", "1",
            "-q:v", "5",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "pipe:1",
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=5)
        if r.returncode != 0 or not r.stdout:
            err = (r.stderr or b"").decode("utf-8", "ignore").strip()
            log.add(f"Camera snapshot failed rc={r.returncode} err='{err}'")
            return None
        return r.stdout
    except Exception as e:
        log.add(f"Camera snapshot error: {e}")
        return None


# ---------- RTSP Server (systemd) robust control ----------

_streaming_lock = threading.Lock()
_last_streaming_cmd_ts = 0.0

def _run(cmd: list[str], *, timeout: int = 8) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:
        return 999, "", str(e)

def rtsp_server_active() -> bool:
    rc, out, _ = _run(["systemctl", "is-active", "rtsp-server.service"], timeout=2)
    return rc == 0 and out == "active"

def _wait_rtsp_active(target: bool, *, timeout_s: float = 6.0, step_s: float = 0.4) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if rtsp_server_active() == target:
            return True
        time.sleep(step_s)
    return rtsp_server_active() == target

def set_streaming_enabled(enabled: bool, *, max_retries: int = 2) -> bool:
    """
    Start/stop rtsp-server.service via sudo, with retries and status verification.
    Requires sudoers rule for systemctl start/stop.
    """
    action = "start" if enabled else "stop"
    desired = "ON" if enabled else "OFF"

    for attempt in range(1, max_retries + 2):
        rc, out, err = _run(["sudo", "-n", "systemctl", action, "rtsp-server.service"], timeout=8)
        log.add(f"STREAMING {action.upper()} attempt {attempt}: rc={rc} out='{out}' err='{err}'")

        ok = _wait_rtsp_active(enabled, timeout_s=6.0, step_s=0.4)
        if ok:
            log.add(f"STREAMING state reached: {desired}")
            return True

    log.add(f"STREAMING FAILED to reach: {desired}")
    return False


def system_stats():
    ips = _read_ips()
    ipv4 = ips[0] if ips else ""
    return {
        "hostname": hostname,
        "uptime_seconds": _read_uptime_seconds(),
        "cpu_temp_c": _read_cpu_temp_c(),
        "cpu_usage_pct": cpu_usage_pct(),
        "load1": _read_load1(),
        "mem_used_pct": _read_mem_used_pct(),
        "disk_used_pct": _read_disk_used_pct("/"),
        "ips": ips,
        "ipv4": ipv4,
        "ips_csv": ", ".join(ips),
    }


def display_remaining_seconds():
    # 0 wenn dauerhaft an (force_on)
    if bool(getattr(relay, "force_on", False)):
        return 0

    if hasattr(relay, "remaining_seconds") and callable(getattr(relay, "remaining_seconds")):
        try:
            return int(relay.remaining_seconds())
        except Exception:
            return None

    if hasattr(relay, "_end_ts") and getattr(relay, "_end_ts") is not None:
        try:
            return max(0, int(getattr(relay, "_end_ts") - time.time()))
        except Exception:
            return None

    return None


# ---------- MQTT wiring ----------

def state_provider():
    env = display.env()
    relay_state = relay.status()
    overlay_black = bool(overlay.running())

    # Screen: ON = relay an UND overlay aus
    screen_on = (relay_state == "ON") and (not overlay_black)

    # Touch: ON = unlock (also nicht locked)
    touch_on = not bool(touch.touch_locked)

    return {
        "relay": relay_state,
        "overlay_black": overlay_black,
        "screen_on": screen_on,
        "touch_on": touch_on,
        "rtsp": rtsp.info(),
        "touch_disabled": bool(touch.touch_disabled),
        "touch_locked": bool(touch.touch_locked),
        "display_remaining_seconds": display_remaining_seconds(),
        "display": env.get("DISPLAY"),
        "xauthority": env.get("XAUTHORITY", ""),
        "system": system_stats(),
        "streaming_active": rtsp_server_active(),
    }


def _do_reboot():
    if not config.ALLOW_POWER_ACTIONS:
        log.add("SYSTEM: reboot blockiert (ALLOW_POWER_ACTIONS=0)")
        return

    log.add("SYSTEM: reboot angefordert")
    rc, out, err = _run(["sudo", "-n", "systemctl", "reboot"], timeout=8)
    log.add(f"SYSTEM: reboot rc={rc} out='{out}' err='{err}'")

def _do_shutdown():
    if not config.ALLOW_POWER_ACTIONS:
        log.add("SYSTEM: shutdown blockiert (ALLOW_POWER_ACTIONS=0)")
        return

    log.add("SYSTEM: shutdown angefordert")
    rc, out, err = _run(["sudo", "-n", "systemctl", "poweroff"], timeout=8)
    log.add(f"SYSTEM: shutdown rc={rc} out='{out}' err='{err}'")

def command_handler(topic: str, payload: str):
    base = f"{config.MQTT_BASE_TOPIC}/cmd/"
    if not topic.startswith(base):
        return
    cmd = topic[len(base):]
    p = (payload or "").strip()

    log.add(f"MQTT CMD: {cmd} payload={p}")

    if cmd == "relay_force_on":
        if p.upper() == "ON":
            relay.on_permanent(on_start=overlay.hide)
        else:
            relay.cancel_timer()
            relay.off()
            overlay.show()
        mqtt_bridge.publish_state_now()

    elif cmd == "overlay_black":
        if p.upper() == "ON":
            overlay.show()
        else:
            overlay.hide()
        mqtt_bridge.publish_state_now()

    elif cmd == "touch_lock":
        if p.upper() == "ON":
            touch.lock()
        else:
            touch.unlock()
        mqtt_bridge.publish_state_now()

    elif cmd == "rtsp_url/set":
        if p.startswith("rtsp://"):
            rtsp_cfg["url"] = p
            mqtt_bridge.publish_state_now()

    elif cmd == "rtsp_mode/set":
        if p in ("normal", "crop", "stretch"):
            rtsp_cfg["mode"] = p
            mqtt_bridge.publish_state_now()

    elif cmd == "rtsp_seconds/set":
        try:
            sec = int(p)
            if 5 <= sec <= 3600:
                rtsp_cfg["seconds"] = sec
                mqtt_bridge.publish_state_now()
        except Exception:
            pass

    elif cmd == "rtsp_start":
        rtsp.start(rtsp_cfg["url"], rtsp_cfg["seconds"], mode=rtsp_cfg["mode"])
        mqtt_bridge.publish_state_now()

    elif cmd == "rtsp_start_5min":
        if p.upper() == "PRESS":
            rtsp.start(rtsp_cfg["url"], 300, mode=rtsp_cfg["mode"])
            mqtt_bridge.publish_state_now()

    elif cmd == "rtsp_stop":
        rtsp.stop_only()
        mqtt_bridge.publish_state_now()

    elif cmd == "system/reboot":
        if p.upper() == "PRESS":
            _do_reboot()

    elif cmd == "system/shutdown":
        if p.upper() == "PRESS":
            _do_shutdown()

    elif cmd == "screen_5min":
        if p.upper() == "PRESS":
            display.wake()
            relay.activate_for(config.RELAY_ON_TIME, on_start=overlay.hide, on_end=overlay.show)
            mqtt_bridge.publish_state_now()

    elif cmd == "rtsp/start":
        try:
            j = json.loads(p) if p else {}
            url = str(j.get("url", rtsp_cfg["url"])).strip()
            sec = int(j.get("seconds", 300))
            mode = str(j.get("mode", rtsp_cfg["mode"])).strip()

            if not url.startswith("rtsp://"):
                log.add("MQTT rtsp/start: invalid url")
                return

            if mode not in ("normal", "crop", "stretch"):
                mode = "normal"

            sec = max(5, min(sec, 3600))

            rtsp_cfg["url"] = url
            rtsp_cfg["mode"] = mode
            rtsp_cfg["seconds"] = sec

            rtsp.start(url, sec, mode=mode)
            mqtt_bridge.publish_state_now()
        except Exception as e:
            log.add(f"MQTT rtsp/start: bad payload ({e})")

    elif cmd == "screen":
        # ON: relay an + overlay aus
        # OFF: relay aus + overlay an
        if p.upper() == "ON":
            display.wake()
            relay.on_permanent(on_start=overlay.hide)
        else:
            relay.cancel_timer()
            try:
                relay.off(force=True)
            except TypeError:
                relay.off()
            overlay.show()
        mqtt_bridge.publish_state_now()

    elif cmd == "touch":
        # ON: unlock, OFF: lock
        if p.upper() == "ON":
            touch.unlock()
        else:
            touch.lock()
        mqtt_bridge.publish_state_now()

    elif cmd == "screenshot":
        if p.upper() == "PRESS":
            jpeg = take_screenshot_jpeg()
            if jpeg:
                t = f"{config.MQTT_BASE_TOPIC}/screen/image"
                mqtt_bridge.publish_bytes(t, jpeg, retain=False, qos=0)
                log.add("MQTT: screenshot published")

    elif cmd == "streaming":
        # Debounce + Retry + Verify
        desired = (p.upper() == "ON")
        with _streaming_lock:
            global _last_streaming_cmd_ts
            now = time.time()
            if now - _last_streaming_cmd_ts < 1.5:
                log.add("STREAMING: debounced (too soon)")
                return
            _last_streaming_cmd_ts = now

        set_streaming_enabled(desired, max_retries=2)
        mqtt_bridge.publish_state_now()


mqtt_bridge = MqttBridge(config, log, state_provider=state_provider, command_handler=command_handler)


# ---------- Startup ----------

@app.on_event("startup")
def startup():
    overlay.ensure_png()

    # Idle: Relais aus + black
    relay.off()
    overlay.show()

    touch.start_monitor()
    kbd.start()
    mqtt_bridge.start()

    log.add(f"Startup: idle (relay off + black), hostname={hostname}, mqtt_base={config.MQTT_BASE_TOPIC}")


# -------------------- API --------------------

@app.get("/status")
def status():
    return {"ok": True, **state_provider()}


@app.get("/relay/status")
def relay_status():
    return {"relay": relay.status(), "relay_force_on": bool(getattr(relay, "force_on", False))}


@app.post("/relay/on")
def relay_on_permanent():
    log.add("API: relay/on (dauerhaft)")
    relay.on_permanent(on_start=overlay.hide)
    mqtt_bridge.publish_state_now()
    return {"ok": True, "relay": relay.status(), "overlay_black": overlay.running(), "relay_force_on": bool(getattr(relay, "force_on", False))}


@app.post("/relay/off")
def relay_off_now():
    log.add("API: relay/off")
    relay.cancel_timer()
    relay.off()
    overlay.show()
    mqtt_bridge.publish_state_now()
    return {"ok": True, "relay": relay.status(), "overlay_black": overlay.running(), "relay_force_on": bool(getattr(relay, "force_on", False))}


@app.post("/relay")
def relay_switch(action: RelayAction):
    s = action.state.lower().strip()
    if s == "on":
        relay.activate_for(config.RELAY_ON_TIME, on_start=overlay.hide, on_end=overlay.show)
    elif s == "off":
        relay.off()
        overlay.show()
    else:
        return {"error": "state must be 'on' or 'off'"}
    mqtt_bridge.publish_state_now()
    return {"relay": relay.status(), "relay_force_on": bool(getattr(relay, "force_on", False))}


@app.post("/touch/disable")
def touch_disable():
    touch.disable()
    mqtt_bridge.publish_state_now()
    return {"touch_disabled": touch.touch_disabled, "touch_locked": touch.touch_locked}


@app.post("/touch/enable")
def touch_enable():
    touch.enable()
    mqtt_bridge.publish_state_now()
    return {"touch_disabled": touch.touch_disabled, "touch_locked": touch.touch_locked}


@app.post("/touch/lock")
def touch_lock():
    touch.lock()
    mqtt_bridge.publish_state_now()
    return {"touch_disabled": touch.touch_disabled, "touch_locked": touch.touch_locked}


@app.post("/touch/unlock")
def touch_unlock():
    touch.unlock()
    mqtt_bridge.publish_state_now()
    return {"touch_disabled": touch.touch_disabled, "touch_locked": touch.touch_locked}


@app.post("/rtsp/start")
def rtsp_start(req: RtspRequest):
    log.add(f"API: rtsp/start {req.url} {req.seconds}s mode={req.mode}")
    ok = rtsp.start(req.url, req.seconds, mode=req.mode)
    mqtt_bridge.publish_state_now()
    return {"ok": ok, "rtsp": rtsp.info(), "mode": req.mode}


@app.post("/rtsp/stop")
def rtsp_stop():
    log.add("API: rtsp/stop (nur Stream, kein Idle)")
    rtsp.stop_only()
    mqtt_bridge.publish_state_now()
    return {"ok": True, "rtsp": rtsp.info()}


@app.post("/overlay/on")
def overlay_on():
    log.add("API: overlay/on (Relais bleibt unverändert)")
    overlay.show()
    mqtt_bridge.publish_state_now()
    return {"ok": True, "overlay_black": overlay.running(), "relay": relay.status()}


@app.post("/overlay/off")
def overlay_off():
    log.add("API: overlay/off (Relais bleibt unverändert)")
    overlay.hide()
    mqtt_bridge.publish_state_now()
    return {"ok": True, "overlay_black": overlay.running(), "relay": relay.status()}


@app.get("/overlay/status")
def overlay_status():
    return {"overlay_black": overlay.running(), "relay": relay.status()}


@app.post("/system/reboot")
def api_reboot():
    log.add("API: system/reboot")
    _do_reboot()
    mqtt_bridge.publish_state_now()
    return {"ok": True, "allowed": bool(config.ALLOW_POWER_ACTIONS)}


@app.post("/system/shutdown")
def api_shutdown():
    log.add("API: system/shutdown")
    _do_shutdown()
    mqtt_bridge.publish_state_now()
    return {"ok": True, "allowed": bool(config.ALLOW_POWER_ACTIONS)}


@app.get("/streaming/status")
def streaming_status():
    return {"ok": True, "streaming_active": rtsp_server_active()}


@app.post("/streaming")
def streaming_set(action: StreamingAction):
    s = action.state.lower().strip()
    if s not in ("on", "off"):
        return {"ok": False, "error": "state must be 'on' or 'off'"}

    desired = (s == "on")

    with _streaming_lock:
        global _last_streaming_cmd_ts
        now = time.time()
        if now - _last_streaming_cmd_ts < 1.0:
            log.add("STREAMING(API): debounced (too soon)")
        _last_streaming_cmd_ts = now

    ok = set_streaming_enabled(desired, max_retries=2)
    mqtt_bridge.publish_state_now()
    return {"ok": ok, "streaming_active": rtsp_server_active()}


@app.get("/camera/snapshot.jpg")
def camera_snapshot():
    jpg = camera_snapshot_jpeg(LOCAL_CAMERA_RTSP_URL)
    if not jpg:
        return Response(content=b"snapshot failed", media_type="text/plain", status_code=503)
    # no-store => Browser holt wirklich neu
    return Response(
        content=jpg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


# -------------------- Debug Seiten --------------------

@app.get("/debug", response_class=HTMLResponse)
def debug():
    lines = [html.escape(x) for x in log.tail(250)]
    txt = "\n".join(lines) if lines else "Keine Events."
    return HTMLResponse(f"""
    <html>
    <head>
      <meta charset="utf-8">
      <title>{html.escape(hostname)} - Debug</title>
      <meta http-equiv="refresh" content="2">
      <style>
        body {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background:#111; color:#eee; padding:20px; }}
        a {{ color:#64B5F6; }}
        pre {{ background:#0b0b0b; padding:12px; border-radius:10px; overflow:auto; white-space:pre-wrap; }}
      </style>
    </head>
    <body>
      <h2>Debug</h2>
      <p><a href="/">Home</a> | <a href="/rtsp/log">RTSP Log</a></p>
      <pre>{txt}</pre>
    </body>
    </html>
    """)


@app.get("/rtsp/log", response_class=HTMLResponse)
def rtsp_log():
    if not os.path.exists(config.RTSP_LOG_PATH):
        return HTMLResponse("<pre>Kein RTSP-Log vorhanden.</pre>")
    with open(config.RTSP_LOG_PATH, "r", errors="ignore") as f:
        txt = f.read()[-20000:]
    return HTMLResponse("<pre>" + html.escape(txt) + "</pre>")


# -------------------- Simple Web UI --------------------

@app.get("/", response_class=HTMLResponse)
def ui():
    default_url = html.escape(config.RTSP_DEFAULT_URL)
    default_seconds = int(config.RTSP_DEFAULT_SECONDS)
    title = html.escape(hostname)

    return HTMLResponse(f"""
    <html>
    <head>
      <meta charset="utf-8">
      <title>{title}</title>
      <style>
        body{{font-family:Arial;text-align:center;margin-top:30px;}}
        button{{padding:12px 22px;margin:8px;font-size:1.05em;border-radius:10px;border:0;color:#fff;cursor:pointer;}}
        .a{{background:#4CAF50}} .b{{background:#E53935}} .c{{background:#1976D2}} .d{{background:#6D4C41}} .e{{background:#8E24AA}} .f{{background:#5E35B1}} .g{{background:#455A64}}
        .card{{display:inline-block;text-align:left;min-width:680px;max-width:980px;background:#f6f7f9;border:1px solid #e3e6ea;border-radius:14px;padding:16px;margin:10px;}}
        .row{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;}}
        input, select{{padding:10px;border-radius:10px;border:1px solid #cfd6dd;font-size:1em;}}
        input[type="text"]{{width:560px;}}
        input[type="number"]{{width:120px;}}
        pre{{text-align:left;white-space:pre-wrap;word-break:break-word;background:#111;color:#eee;padding:14px;border-radius:12px;overflow:auto;}}
        a{{color:#1976D2;}}
        .label{{font-weight:bold;margin-right:6px;}}
        .warn{{color:#b71c1c;font-weight:bold;}}
        .switch{{display:flex;align-items:center;gap:10px;}}
        .switch input{{transform:scale(1.4);}}
        img.snap{{width:360px;max-width:100%;border-radius:12px;border:1px solid #cfd6dd;background:#000;}}
      </style>
    </head>
    <body>
      <h1>{title}</h1>

      <div class="card">
        <div class="label">Status</div>
        <pre id="s">Status lädt…</pre>
        <div class="row">
          <button class="c" onclick="refresh()">Refresh</button>
        </div>
      </div>

      <div class="card">
        <div class="label">Video Streaming (RTSP Server)</div>
        <div class="row switch">
          <input id="streamingSwitch" type="checkbox" onchange="toggleStreaming()">
          <span id="streamingLabel">lädt…</span>
          <span style="opacity:.7">(ON = rtsp-server.service läuft)</span>
        </div>
        <div class="row" style="margin-top:10px;">
          <div>
            <div class="label">Standbild</div>
            <img id="cam" class="snap" src="/camera/snapshot.jpg" alt="snapshot">
            <div class="row">
              <button class="c" onclick="refreshSnapshot()">Snapshot aktualisieren</button>
            </div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="label">Relais</div>
        <div class="row">
          <button class="a" onclick="post('/relay/on')">Relay ON (dauerhaft)</button>
          <button class="a" onclick="relay('on')">Relay 5 Min</button>
          <button class="b" onclick="relay('off')">Relay OFF</button>
        </div>
      </div>

      <div class="card">
        <div class="label">RTSP Player</div>
        <div class="row">
          <span class="label">URL</span>
          <input id="rtspUrl" type="text" value="{default_url}">
        </div>
        <div class="row">
          <span class="label">Modus</span>
          <select id="rtspMode">
            <option value="normal">normal (1:1)</option>
            <option value="crop">crop (Fill 1080x1920)</option>
            <option value="stretch">stretch (1080x1920)</option>
          </select>

          <span class="label">Sek.</span>
          <input id="rtspSeconds" type="number" min="5" max="3600" value="{default_seconds}">
        </div>
        <div class="row">
          <button class="d" onclick="rtspStartFromUi()">RTSP Start</button>
          <button class="d" onclick="rtspStop()">RTSP Stop</button>
        </div>
      </div>

      <div class="card">
        <div class="label">Touch</div>
        <div class="row">
          <button class="e" onclick="post('/touch/disable')">Touch disable</button>
          <button class="e" onclick="post('/touch/enable')">Touch enable</button>
          <button class="f" onclick="post('/touch/lock')">Touch lock</button>
          <button class="f" onclick="post('/touch/unlock')">Touch unlock</button>
        </div>
      </div>

      <div class="card">
        <div class="label">Overlay (Test)</div>
        <div class="row">
          <button class="b" onclick="post('/overlay/on')">Overlay BLACK ON</button>
          <button class="c" onclick="post('/overlay/off')">Overlay OFF</button>
        </div>
      </div>

      <div class="card">
        <div class="label">System</div>
        <div class="row">
          <span class="warn">Achtung:</span> Reboot/Shutdown nur wenn <code>ALLOW_POWER_ACTIONS=1</code>
        </div>
        <div class="row">
          <button class="g" onclick="post('/system/reboot')">Reboot</button>
          <button class="g" onclick="post('/system/shutdown')">Shutdown</button>
        </div>
      </div>

      <div style="margin-top:10px;">
        <a href="/debug" target="_blank">Debug</a> | <a href="/rtsp/log" target="_blank">RTSP Log</a>
      </div>

      <script>
        async function post(p, body=null){{
          const res = await fetch(p, {{
            method:'POST',
            headers: {{'Content-Type':'application/json'}},
            body: body ? JSON.stringify(body) : null
          }});
          if(!res.ok) {{
            const t = await res.text().catch(()=> '');
            alert('HTTP ' + res.status + ' ' + p + '\\n' + t);
          }}
          await refresh();
        }}

        async function relay(st){{ await post('/relay',{{state:st}}); }}

        async function rtspStartFromUi(){{
          const url = document.getElementById('rtspUrl').value.trim();
          const mode = document.getElementById('rtspMode').value;
          const seconds = parseInt(document.getElementById('rtspSeconds').value || '300', 10);

          if(!url.startsWith('rtsp://')) {{
            alert('URL muss mit rtsp:// beginnen');
            return;
          }}
          if(isNaN(seconds) || seconds < 5 || seconds > 3600) {{
            alert('Sekunden müssen zwischen 5 und 3600 liegen');
            return;
          }}

          await post('/rtsp/start', {{ url, seconds, mode }});
        }}

        async function rtspStop(){{ await post('/rtsp/stop'); }}

        function refreshSnapshot(){{
          const img = document.getElementById('cam');
          img.src = '/camera/snapshot.jpg?ts=' + Date.now();
        }}

        async function toggleStreaming(){{
          const sw = document.getElementById('streamingSwitch');
          const desired = sw.checked ? 'on' : 'off';
          // Optimistisch labeln; wird nach refresh() korrigiert
          document.getElementById('streamingLabel').textContent = sw.checked ? 'aktiv' : 'aus';
          await post('/streaming', {{ state: desired }});
        }}

        async function refresh(){{
          const el = document.getElementById('s');
          try {{
            const res = await fetch('/status', {{ cache: 'no-store' }});
            if(!res.ok) {{
              el.textContent = 'STATUS Fehler: HTTP ' + res.status;
              return;
            }}
            const j = await res.json();
            el.textContent = JSON.stringify(j, null, 2);

            // Streaming switch/state
            const active = !!(j.streaming_active);
            const sw = document.getElementById('streamingSwitch');
            const lbl = document.getElementById('streamingLabel');
            sw.checked = active;
            lbl.textContent = active ? 'aktiv' : 'aus';
          }} catch(e) {{
            el.textContent = 'STATUS Fetch Fehler: ' + e;
          }}
        }}

        window.onload = () => {{
          refresh();
          // Snapshot alle 10s aktualisieren (optional)
          setInterval(refreshSnapshot, 10000);
          // Status alle 5s (optional)
          setInterval(refresh, 5000);
        }};
      </script>
    </body>
    </html>
    """)

