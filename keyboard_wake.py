import glob
import threading
from evdev import InputDevice, ecodes

class KeyboardWake:
    """
    Lauscht auf Keyboard-Devices (by-id *-kbd) und ruft callback bei Keypress auf.
    """
    def __init__(self, log, on_keypress=None):
        self.log = log
        self.on_keypress = on_keypress
        self._thread = None
        self._devices = []

    def _discover(self):
        # Stabil: /dev/input/by-id/*-kbd
        paths = sorted(glob.glob("/dev/input/by-id/*-kbd"))
        devs = []
        for p in paths:
            try:
                devs.append(InputDevice(p))
            except Exception:
                pass
        return devs

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self._devices = self._discover()
        if not self._devices:
            self.log.add("KeyboardWake: kein *-kbd Device gefunden unter /dev/input/by-id/")
            return

        names = ", ".join([d.path for d in self._devices])
        self.log.add(f"KeyboardWake: lauscht auf {names}")

        # Einfachste Variante: loop auf jedem device in eigenem thread
        for dev in self._devices:
            threading.Thread(target=self._loop_dev, args=(dev,), daemon=True).start()

    def _loop_dev(self, dev: InputDevice):
        try:
            for ev in dev.read_loop():
                if ev.type == ecodes.EV_KEY and ev.value == 1:  # key down
                    self.log.add(f"KeyboardWake: Keypress auf {dev.path}")
                    if self.on_keypress:
                        self.on_keypress()
        except PermissionError:
            self.log.add(f"KeyboardWake: PermissionError {dev.path} (udev/gruppenrechte)")
        except Exception as e:
            self.log.add(f"KeyboardWake: Fehler {dev.path}: {e}")
