import time
import threading
from evdev import InputDevice, ecodes

class TouchController:
    def __init__(self, device_path: str, unlock_touches: int, unlock_window: int, log):
        self.device_path = device_path
        self.unlock_touches = unlock_touches
        self.unlock_window = unlock_window
        self.log = log

        self.touch_disabled = False
        self.touch_locked = False

        self._dev = None
        self._thread = None

        self._unlock_counter = 0
        self._unlock_start = None

        self._on_touch = None  # callback

    def set_on_touch(self, cb):
        self._on_touch = cb

    def _ensure_dev(self):
        if self._dev is None:
            self._dev = InputDevice(self.device_path)
            self.log.add(f"Touch: geöffnet {self._dev.path} ({self._dev.name})")

    def _apply_state(self):
        self._ensure_dev()
        try:
            if self.touch_disabled or self.touch_locked:
                self._dev.grab()
                self.log.add("Touch: grab() -> OS bekommt nichts")
            else:
                self._dev.ungrab()
                self.log.add("Touch: ungrab() -> OS bekommt Touch")
        except Exception as e:
            self.log.add(f"Touch: grab/ungrab Fehler: {e}")

    def disable(self):
        # Soft-disable: OS blocken, Script reagiert weiter
        self.touch_locked = False      # wichtig: Hard-Lock aus
        self.touch_disabled = True
        self._unlock_counter = 0
        self._unlock_start = None
        self._apply_state()
        self.log.add("Touch: disable() -> OS geblockt, Script reagiert weiter")


    def enable(self):
        # vollständig frei: OS bekommt Touch wieder
        self.touch_locked = False      # wichtig: Hard-Lock aus
        self.touch_disabled = False
        self._unlock_counter = 0
        self._unlock_start = None
        self._apply_state()
        self.log.add("Touch: enable() -> OS bekommt Touch wieder")


    def lock(self):
        self.touch_locked = True
        self._unlock_counter = 0
        self._unlock_start = None
        self._apply_state()

    def unlock(self):
        self.touch_locked = False
        self._apply_state()

    def start_monitor(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self._ensure_dev()
            self.log.add("Touch: monitor startet")
            for ev in self._dev.read_loop():
                if ev.type not in (ecodes.EV_KEY, ecodes.EV_ABS):
                    continue

                if self.touch_locked:
                    self.log.add("Touch: ignoriert (HARD-LOCK)")
                    continue

                # Touch event -> callback
                if self._on_touch:
                    self._on_touch()

                # unlock pattern nur wenn touch_disabled aktiv
                if self.touch_disabled:
                    now = time.time()
                    if self._unlock_start is None or (now - self._unlock_start > self.unlock_window):
                        self._unlock_start = now
                        self._unlock_counter = 1
                    else:
                        self._unlock_counter += 1

                    self.log.add(f"Touch: unlock {self._unlock_counter}/{self.unlock_touches}")

                    if self._unlock_counter >= self.unlock_touches:
                        self.log.add("Touch: unlock pattern erkannt -> enable()")
                        self.enable()

        except FileNotFoundError:
            self.log.add(f"Touch: Device nicht gefunden: {self.device_path}")
        except PermissionError:
            self.log.add(f"Touch: PermissionError: {self.device_path} (udev/gruppenrechte)")
        except Exception as e:
            self.log.add(f"Touch: monitor Fehler: {e}")
