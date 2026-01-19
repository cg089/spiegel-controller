import subprocess
import signal
import time
import threading
import os


class RtspPlayer:
    def __init__(self, display_ctl, overlay, relay, log, log_path: str):
        self.display_ctl = display_ctl
        self.overlay = overlay
        self.relay = relay
        self.log = log
        self.log_path = log_path

        self._lock = threading.Lock()
        self._proc = None
        self._timer = None
        self._url = None
        self._end_ts = None
        self._mode = "normal"

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

    def start(self, url: str, seconds: int, mode: str = "normal", after_done=None):
        """
        Startet RTSP Stream in mpv.
        mode:
          - normal: keine Skalierung/Crop
          - crop:   Höhe auf 1920 skalieren und mittig auf 1080x1920 croppen (ohne Verzerrung)
          - stretch: auf 1080x1920 strecken (verzerrt)
        """
        if seconds <= 0:
            seconds = 300

        mode = (mode or "normal").lower().strip()
        if mode not in ("normal", "crop", "stretch"):
            mode = "normal"

        with self._lock:
            # vorherigen Stream beenden
            self._kill_group(self._proc, "RTSP")
            self._proc = None

            # Screen an + Overlay weg
            self.display_ctl.wake()
            self.overlay.hide()

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
            ]

            # Bildmodus
            if mode == "crop":
                cmd += ["--vf=scale=-2:1920,crop=1080:1920:(iw-1080)/2:(ih-1920)/2"]
            elif mode == "stretch":
                cmd += ["--vf=scale=1080:1920", "--no-keepaspect"]

            cmd.append(url)

            # Kopfzeile ins Log
            try:
                with open(self.log_path, "a", buffering=1) as f:
                    env = self.display_ctl.env()
                    f.write(f"\n--- {time.strftime('%F %T')} START ---\n")
                    f.write("CMD: " + " ".join(cmd) + "\n")
                    f.write(f"DISPLAY={env.get('DISPLAY')} XAUTHORITY={env.get('XAUTHORITY','')}\n")
            except Exception:
                pass

            try:
                logf = open(self.log_path, "a", buffering=1)
                self._proc = subprocess.Popen(
                    cmd,
                    env=self.display_ctl.env(),
                    stdout=logf,
                    stderr=logf,
                    start_new_session=True
                )
            except FileNotFoundError:
                self.log.add("RTSP: mpv nicht gefunden (installiere mpv).")
                self._proc = None
                self._url = None
                self._end_ts = None
                self._mode = "normal"
                return False
            except Exception as e:
                self.log.add(f"RTSP: Startfehler: {e} (siehe {self.log_path})")
                self._proc = None
                self._url = None
                self._end_ts = None
                self._mode = "normal"
                return False

            self._url = url
            self._end_ts = time.time() + seconds
            self._mode = mode

            self.log.add(f"RTSP: start {url} für {seconds}s mode={mode} (log {self.log_path})")

            # sofortige Beendigung erkennen
            time.sleep(0.3)
            rc = self._proc.poll()
            if rc is not None:
                self.log.add(f"RTSP: sofort beendet rc={rc} (siehe {self.log_path})")
                self._proc = None
                self._url = None
                self._end_ts = None
                self._mode = "normal"
                return False

            # Relay passend zur Dauer
            self.relay.activate_for(seconds)

            # Timer neu
            if self._timer and self._timer.is_alive():
                self._timer.cancel()

            def _finish():
                # Stream stoppen
                self.stop_only()

                # Idle nach Ablauf: Relais OFF + Schwarz
                self.relay.off()
                self.overlay.show()
                self.log.add("RTSP: fertig -> idle (relay off + black)")
                if after_done:
                    after_done()

            self._timer = threading.Timer(seconds, _finish)
            self._timer.start()

            return True

    def stop_only(self):
        """Stoppt nur den RTSP-Prozess und Timer – ohne Relais/Overlay/Idle-Logik."""
        with self._lock:
            if self._timer and self._timer.is_alive():
                self._timer.cancel()
            self._timer = None

            self._kill_group(self._proc, "RTSP")
            self._proc = None
            self._url = None
            self._end_ts = None
            self._mode = "normal"

            self.log.add("RTSP: stop_only (nur Stream beendet)")

    def stop(self):
        """Alias für stop_only (kompatibel)."""
        self.stop_only()

    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def info(self):
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return {"running": False, "url": None, "remaining": 0, "mode": "normal"}

            remaining = int(self._end_ts - time.time()) if self._end_ts else 0
            return {
                "running": True,
                "url": self._url,
                "remaining": max(0, remaining),
                "mode": self._mode,
            }
