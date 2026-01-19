import json
import time
import threading
import paho.mqtt.client as mqtt


class MqttBridge:
    """
    Base topics:
      kiosk/<hostname>/availability   online/offline (LWT)
      kiosk/<hostname>/state          JSON state
      kiosk/<hostname>/cmd/...        commands
    Discovery:
      homeassistant/<component>/<device_id>/<object>/config
    """

    def __init__(self, config, log, *, state_provider, command_handler):
        self.cfg = config
        self.log = log
        self.state_provider = state_provider
        self.command_handler = command_handler

        self.device_id = getattr(config, "DEVICE_ID", "device")
        self.device_name = getattr(config, "HA_DEVICE_NAME", self.device_id)

        self.base = getattr(config, "MQTT_BASE_TOPIC", f"kiosk/{self.device_id}")
        self.discovery_prefix = getattr(config, "MQTT_DISCOVERY_PREFIX", "homeassistant")

        self.avail_topic = f"{self.base}/availability"
        self.state_topic = f"{self.base}/state"
        self.cmd_base = f"{self.base}/cmd"

        self.interval = int(getattr(config, "MQTT_PUBLISH_INTERVAL", 5))
        self.retain_discovery = bool(getattr(config, "MQTT_RETAIN_DISCOVERY", True))
        self.retain_state = bool(getattr(config, "MQTT_RETAIN_STATE", False))

        self._stop = threading.Event()
        self._client = None
        self._connected = False
        self._thread = None

    def start(self):
        if not getattr(self.cfg, "MQTT_ENABLED", True):
            self.log.add("MQTT: disabled")
            return

        host = getattr(self.cfg, "MQTT_HOST", "")
        if not host:
            self.log.add("MQTT: MQTT_HOST leer -> nicht gestartet")
            return

        port = int(getattr(self.cfg, "MQTT_PORT", 1883))

        self._client = mqtt.Client(client_id=f"{self.device_id}-spiegel", clean_session=True)

        user = getattr(self.cfg, "MQTT_USER", "")
        pwd = getattr(self.cfg, "MQTT_PASSWORD", "")
        if user:
            self._client.username_pw_set(user, pwd)

        # LWT
        self._client.will_set(self.avail_topic, payload="offline", qos=1, retain=True)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        self._client.connect_async(host, port, keepalive=30)

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

        self.log.add(f"MQTT: start {host}:{port} base={self.base}")

    def stop(self):
        self._stop.set()
        try:
            if self._client:
                self._client.publish(self.avail_topic, "offline", qos=1, retain=True)
                self._client.disconnect()
        except Exception:
            pass

    def publish_bytes(self, topic: str, payload: bytes, *, retain: bool = False, qos: int = 0):
        if not self._client or not self._connected:
            return
        self._client.publish(topic, payload=payload, qos=qos, retain=retain)

    def publish_state_now(self):
        if self._client and self._connected:
            self._publish_state()

    def _run(self):
        self._client.loop_start()

        next_pub = 0.0
        while not self._stop.is_set():
            if self._connected and time.time() >= next_pub:
                self._publish_state()
                next_pub = time.time() + max(1, self.interval)
            time.sleep(0.2)

        self._client.loop_stop()

    def _on_connect(self, client, userdata, flags, rc):
        self._connected = True
        self.log.add(f"MQTT: connected rc={rc}")

        client.publish(self.avail_topic, "online", qos=1, retain=True)
        client.subscribe(f"{self.cmd_base}/#", qos=1)

        self._publish_discovery()
        self._publish_state()

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        self.log.add(f"MQTT: disconnected rc={rc}")

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8", errors="ignore").strip()
        except Exception:
            payload = ""

        try:
            self.command_handler(msg.topic, payload)
        except Exception as e:
            self.log.add(f"MQTT: cmd error: {e}")

        self._publish_state()

    def _publish(self, topic: str, payload, *, retain: bool, qos: int = 1):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False)
        self._client.publish(topic, payload=payload, qos=qos, retain=retain)

    def _publish_state(self):
        st = self.state_provider() or {}
        self._publish(self.state_topic, st, retain=self.retain_state, qos=1)

    def _publish_discovery(self):
        dev = {
            "identifiers": [self.device_id],
            "name": self.device_name,
            "manufacturer": "ATRIVIO",
            "model": "Spiegel klein",
        }

        def dtopic(component: str, obj: str) -> str:
            return f"{self.discovery_prefix}/{component}/{self.device_id}/{obj}/config"

        common = {
            "availability_topic": self.avail_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": dev,
        }

        # Sensors aus JSON State
        sensors = [
            ("relay_state", "Relay", "{{ value_json.relay }}", None),
            ("overlay_black", "Overlay black", "{{ value_json.overlay_black }}", None),
            ("rtsp_running", "RTSP running", "{{ value_json.rtsp.running }}", None),
            ("rtsp_url", "RTSP url", "{{ value_json.rtsp.url }}", None),
            ("rtsp_mode", "RTSP mode", "{{ value_json.rtsp.mode }}", None),
            ("rtsp_remaining", "RTSP remaining", "{{ value_json.rtsp.remaining }}", "s"),
            ("touch_locked", "Touch locked", "{{ value_json.touch_locked }}", None),
            ("touch_disabled", "Touch disabled", "{{ value_json.touch_disabled }}", None),
            ("display_remaining_seconds", "Display remaining", "{{ value_json.display_remaining_seconds }}", "s"),
            ("uptime", "Uptime", "{{ value_json.system.uptime_seconds }}", "s"),
            ("cpu_temp", "CPU temp", "{{ value_json.system.cpu_temp_c }}", "°C"),
            ("load1", "Load 1m", "{{ value_json.system.load1 }}", None),
            ("mem_used_pct", "RAM used", "{{ value_json.system.mem_used_pct }}", "%"),
            ("disk_used_pct", "Disk used", "{{ value_json.system.disk_used_pct }}", "%"),
            ("cpu_usage_pct", "CPU usage", "{{ value_json.system.cpu_usage_pct }}", "%"),
            ("ipv4", "IPv4", "{{ value_json.system.ipv4 }}", None),
            ("ips_csv", "IPv4 list", "{{ value_json.system.ips_csv }}", None),
        ]

        for key, name, tpl, unit in sensors:
            payload = {
                **common,
                "name": name,
                "unique_id": f"{self.device_id}_{key}",
                "state_topic": self.state_topic,
                "value_template": tpl,
            }
            if unit:
                payload["unit_of_measurement"] = unit
            self._publish(dtopic("sensor", key), payload, retain=self.retain_discovery, qos=1)

        # ---------- MQTT SWITCHES als echte Toggle (State liefert ON/OFF) ----------
        def make_switch(obj_id: str, name: str, cmd_suffix: str, state_tpl_onoff: str):
            self._publish(
                dtopic("switch", obj_id),
                {
                    **common,
                    "name": name,
                    "unique_id": f"{self.device_id}_{obj_id}",
                    "command_topic": f"{self.cmd_base}/{cmd_suffix}",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "state_topic": self.state_topic,
                    "value_template": state_tpl_onoff,      # MUSS ON/OFF liefern
                    "state_on": "ON",
                    "state_off": "OFF",
                },
                retain=self.retain_discovery,
                qos=1,
            )

        # Overlay: ON wenn overlay_black True
        make_switch(
            "overlay_black_sw",
            "Overlay black",
            "overlay_black",
            "{{ 'ON' if value_json.overlay_black else 'OFF' }}",
        )

        # Touch: ON = unlock (also NICHT locked), OFF = lock
        make_switch(
            "touch_sw",
            "Touch",
            "touch",
            "{{ 'ON' if (not value_json.touch_locked) else 'OFF' }}",
        )

        # Screen: ON = relay an + overlay aus, OFF = relay aus + overlay an
        make_switch(
            "screen_sw",
            "Screen",
            "screen",
            "{{ 'ON' if value_json.screen_on else 'OFF' }}",
        )

        # ---------- Text: RTSP URL ----------
        self._publish(
            dtopic("text", "rtsp_url_text"),
            {
                **common,
                "name": "RTSP URL",
                "unique_id": f"{self.device_id}_rtsp_url_text",
                "command_topic": f"{self.cmd_base}/rtsp_url/set",
                "state_topic": self.state_topic,
                "value_template": "{{ value_json.rtsp.url }}",
                "mode": "text",
            },
            retain=self.retain_discovery,
            qos=1,
        )


        # Button: RTSP Start 5 Min (nutzt rtsp_cfg url+mode, seconds fix 300)
        self._publish(
            dtopic("button", "rtsp_start_5min_btn"),
            {
                **common,
                "name": "RTSP Start 5 Min",
                "unique_id": f"{self.device_id}_rtsp_start_5min_btn",
                "command_topic": f"{self.cmd_base}/rtsp_start_5min",
                "payload_press": "PRESS",
            },
            retain=self.retain_discovery,
            qos=1,
        )


        # ---------- Select: RTSP mode ----------
        self._publish(
            dtopic("select", "rtsp_mode_sel"),
            {
                **common,
                "name": "RTSP Mode",
                "unique_id": f"{self.device_id}_rtsp_mode_sel",
                "command_topic": f"{self.cmd_base}/rtsp_mode/set",
                "state_topic": self.state_topic,
                "value_template": "{{ value_json.rtsp.mode }}",
                "options": ["normal", "crop", "stretch"],
            },
            retain=self.retain_discovery,
            qos=1,
        )

        # ---------- Buttons ----------
        self._publish(
            dtopic("button", "rtsp_start_btn"),
            {
                **common,
                "name": "RTSP Start",
                "unique_id": f"{self.device_id}_rtsp_start_btn",
                "command_topic": f"{self.cmd_base}/rtsp_start",
                "payload_press": "PRESS",
            },
            retain=self.retain_discovery,
            qos=1,
        )
        self._publish(
            dtopic("button", "rtsp_stop_btn"),
            {
                **common,
                "name": "RTSP Stop",
                "unique_id": f"{self.device_id}_rtsp_stop_btn",
                "command_topic": f"{self.cmd_base}/rtsp_stop",
                "payload_press": "PRESS",
            },
            retain=self.retain_discovery,
            qos=1,
        )

        # Button: Screen 5 Min
        self._publish(
            dtopic("button", "screen_5min_btn"),
            {
                **common,
                "name": "Screen 5 Min",
                "unique_id": f"{self.device_id}_screen_5min_btn",
                "command_topic": f"{self.cmd_base}/screen_5min",
                "payload_press": "PRESS",
            },
            retain=self.retain_discovery,
            qos=1,
        )


        self._publish(
            dtopic("button", "system_reboot_btn"),
            {
                **common,
                "name": "System reboot",
                "unique_id": f"{self.device_id}_system_reboot_btn",
                "command_topic": f"{self.cmd_base}/system/reboot",
                "payload_press": "PRESS",
            },
            retain=self.retain_discovery,
            qos=1,
        )
        self._publish(
            dtopic("button", "system_shutdown_btn"),
            {
                **common,
                "name": "System shutdown",
                "unique_id": f"{self.device_id}_system_shutdown_btn",
                "command_topic": f"{self.cmd_base}/system/shutdown",
                "payload_press": "PRESS",
            },
            retain=self.retain_discovery,
            qos=1,
        )

        image_topic = f"{self.base}/screen/image"

        # MQTT Image entity (raw JPEG bytes on image_topic) :contentReference[oaicite:1]{index=1}
        self._publish(
            dtopic("image", "screen_image"),
            {
                **common,
                "name": "Screen snapshot",
                "unique_id": f"{self.device_id}_screen_image",
                "image_topic": image_topic,
                "content_type": "image/jpeg",
            },
            retain=self.retain_discovery,
            qos=1,
        )

        # Button: Screenshot aufnehmen
        self._publish(
            dtopic("button", "screenshot_btn"),
            {
                **common,
                "name": "Screenshot",
                "unique_id": f"{self.device_id}_screenshot_btn",
                "command_topic": f"{self.cmd_base}/screenshot",
                "payload_press": "PRESS",
            },
            retain=self.retain_discovery,
            qos=1,
        )


        self.log.add("MQTT: HA discovery published")
