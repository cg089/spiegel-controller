import threading
import time
import serial

class RelayController:
    def __init__(self, device: str, baudrate: int, log):
        self.device = device
        self.baudrate = baudrate
        self.log = log

        self._lock = threading.Lock()
        self._timer = None

    def _send(self, data: bytes):
        with serial.Serial(self.device, baudrate=self.baudrate, timeout=1) as ser:
            ser.write(data)

    def on(self):
        self._send(b"\xA0\x01\x01\xA2")
        self.log.add("Relay: ON")

    def off(self):
        self._send(b"\xA0\x01\x00\xA1")
        self.log.add("Relay: OFF")

    def status(self) -> str:
        try:
            with serial.Serial(self.device, baudrate=self.baudrate, timeout=1) as ser:
                ser.reset_input_buffer()
                ser.write(b"\xFF")
                time.sleep(0.2)
                resp = ser.read(64).decode(errors="ignore").strip()
            if "ON" in resp:
                return "ON"
            if "OFF" in resp:
                return "OFF"
            return f"UNKNOWN ({resp})"
        except Exception as e:
            return f"ERROR ({e})"

    def activate_for(self, seconds: int, on_start=None, on_end=None):
        """Timer wird immer neu gesetzt."""
        with self._lock:
            if self._timer and self._timer.is_alive():
                self._timer.cancel()

            if on_start:
                on_start()

            self.on()

            def _end():
                try:
                    self.off()
                finally:
                    if on_end:
                        on_end()

            self._timer = threading.Timer(seconds, _end)
            self._timer.start()
            self.log.add(f"Relay: aktiviert für {seconds}s")

    def cancel_timer(self):
        with self._lock:
            if self._timer and self._timer.is_alive():
                self._timer.cancel()
            self._timer = None

    def on_permanent(self, on_start=None):
        """Schaltet Relais dauerhaft EIN (ohne Timer)."""
        with self._lock:
            # Timer sicher weg, sonst geht er später wieder aus
            if self._timer and self._timer.is_alive():
                self._timer.cancel()
            self._timer = None

        if on_start:
            on_start()

        self.on()
        self.log.add("Relay: dauerhaft EIN (kein Timer)")
