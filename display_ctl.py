import os
import subprocess
import getpass

class DisplayController:
    def __init__(self, display_default=":0", xauthority_env="", log=None):
        self.display_default = display_default
        self.xauthority_env = xauthority_env
        self.log = log

    def _find_xauthority(self) -> str:
        xa = os.environ.get("XAUTHORITY", "")
        if xa and os.path.exists(xa):
            return xa

        candidates = []
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            candidates.append(f"/home/{sudo_user}/.Xauthority")

        candidates.append(f"/home/{getpass.getuser()}/.Xauthority")
        candidates.append("/home/conrad/.Xauthority")
        candidates.append("/run/user/1000/gdm/Xauthority")
        candidates.append("/run/user/1000/.Xauthority")

        for p in candidates:
            if os.path.exists(p):
                return p
        return ""

    def env(self):
        env = os.environ.copy()
        env["DISPLAY"] = os.environ.get("DISPLAY", self.display_default)

        xa = os.environ.get("XAUTHORITY", "") or self.xauthority_env or self._find_xauthority()
        if xa:
            env["XAUTHORITY"] = xa

        return env

    def wake(self):
        """Best-effort Wake via xset (X11)."""
        try:
            subprocess.run(["xset", "dpms", "force", "on"], env=self.env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            subprocess.run(["xset", "s", "reset"], env=self.env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if self.log:
                self.log.add("Display: wake (xset dpms on + s reset)")
        except FileNotFoundError:
            if self.log:
                self.log.add("Display: xset fehlt (apt install x11-xserver-utils)")
        except Exception as e:
            if self.log:
                self.log.add(f"Display: wake Fehler: {e}")

