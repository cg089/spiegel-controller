import time
import threading
from collections import deque

class EventLog:
    def __init__(self, maxlen: int = 200):
        self._lock = threading.Lock()
        self._dq = deque(maxlen=maxlen)

    def add(self, msg: str):
        line = f"{time.strftime('%H:%M:%S')} - {msg}"
        with self._lock:
            self._dq.appendleft(line)
        print(msg)

    def tail(self, n: int = 100):
        with self._lock:
            return list(self._dq)[:n]
