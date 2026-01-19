import os
import socket
from dotenv import load_dotenv

# .env aus Projektverzeichnis laden
_BASEDIR = os.path.dirname(__file__)
load_dotenv(os.path.join(_BASEDIR, ".env"))

HOSTNAME = socket.gethostname()

def _get_str(key: str, default: str = "") -> str:
    return os.getenv(key, default)

def _get_int(key: str, default: int) -> int:
    v = os.getenv(key, "")
    try:
        return int(v)
    except Exception:
        return default

def _get_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key, "")
    if v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")

def slug(s: str) -> str:
    s = (s or "").strip().lower()
    out = []
    for ch in s:
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        else:
            out.append("-")
    r = "".join(out)
    while "--" in r:
        r = r.replace("--", "-")
    return r.strip("-") or "device"

DEVICE_ID = slug(HOSTNAME)
HA_DEVICE_NAME = HOSTNAME

# ---------- Relay ----------
DEVICE_RELAY = _get_str("DEVICE_RELAY", "/dev/ttyUSB0")
BAUDRATE = _get_int("BAUDRATE", 9600)
RELAY_ON_TIME = _get_int("RELAY_ON_TIME", 300)

# ---------- Touch ----------
TOUCH_DEVICE_PATH = _get_str("TOUCH_DEVICE_PATH", "/dev/input/touchscreen")
UNLOCK_TOUCHES = _get_int("UNLOCK_TOUCHES", 10)
UNLOCK_WINDOW = _get_int("UNLOCK_WINDOW", 10)

# ---------- X11 / Display ----------
DISPLAY = _get_str("DISPLAY", ":0")
XAUTHORITY = _get_str("XAUTHORITY", "")

# ---------- Overlay ----------
BLACK_PNG_PATH = _get_str("BLACK_PNG_PATH", "/tmp/relay_black.png")

# ---------- RTSP ----------
RTSP_DEFAULT_URL = _get_str("RTSP_DEFAULT_URL", "rtsp://192.168.10.36:8554/Eingang")
RTSP_DEFAULT_SECONDS = _get_int("RTSP_DEFAULT_SECONDS", 300)
RTSP_LOG_PATH = _get_str("RTSP_LOG_PATH", "/tmp/mpv_rtsp.log")

# ---------- MQTT ----------
MQTT_ENABLED = _get_bool("MQTT_ENABLED", True)
MQTT_HOST = _get_str("MQTT_HOST", "")
MQTT_PORT = _get_int("MQTT_PORT", 1883)
MQTT_USER = _get_str("MQTT_USER", "")
MQTT_PASSWORD = _get_str("MQTT_PASSWORD", "")
MQTT_DISCOVERY_PREFIX = _get_str("MQTT_DISCOVERY_PREFIX", "homeassistant")
MQTT_PUBLISH_INTERVAL = _get_int("MQTT_PUBLISH_INTERVAL", 5)
MQTT_RETAIN_DISCOVERY = _get_bool("MQTT_RETAIN_DISCOVERY", True)
MQTT_RETAIN_STATE = _get_bool("MQTT_RETAIN_STATE", False)

# Base Topic: kiosk/<hostname>/
MQTT_BASE_TOPIC = f"kiosk/{DEVICE_ID}"

# ---------- Power actions ----------
ALLOW_POWER_ACTIONS = _get_bool("ALLOW_POWER_ACTIONS", False)
