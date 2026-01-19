import os
import base64
import subprocess
import signal
import time
import threading

class BlackOverlay:
    def __init__(self, png_path: str, png_b64: str, display_ctl, log):
        self.png_path = png_path
        self.png_b64 = png_b64
        self.display_ctl = display_ctl
        self.log = log

        self._lock = threading.Lock()
        self._proc = None

    def ensure_png(self):
        if os.path.exists(self.png_path) and os.path.getsize(self.png_path) > 0:
            return
        data = base64.b64decode(self.png_b64.encode("ascii"))
        with open(self.png_path, "wb") as f:
            f.write(data)
        try:
            os.chmod(self.png_path, 0o644)
        except Exception:
            pass

    def _kill_group(self, proc, name: str):
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
            self.log.add(f"{name}: beendet")
        except Exception as e:
            self.log.add(f"{name}: kill Fehler: {e}")

    def show(self):
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return
            self.ensure_png()
            cmd = ["mpv", "--no-terminal", "--fs", "--ontop", "--no-osc", "--vo=gpu", self.png_path]
            try:
                self._proc = subprocess.Popen(
                    cmd, env=self.display_ctl.env(),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
                self.log.add("Overlay: BLACK an")
            except Exception as e:
                self.log.add(f"Overlay: Startfehler: {e}")
                self._proc = None

    def hide(self):
        with self._lock:
            self._kill_group(self._proc, "Overlay")
            self._proc = None

    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None
