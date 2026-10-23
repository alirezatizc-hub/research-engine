"""قفل تک‌نمونه‌ای: جلوگیری از اجرای هم‌زمان دو ربات روی یک حساب."""
from __future__ import annotations
import os

try:                                            # POSIX
    import fcntl
except ImportError:                             # pragma: no cover - Windows
    fcntl = None


class InstanceLock:
    def __init__(self, path):
        self.path = path
        self._fh = None

    @property
    def held(self):
        return self._fh is not None

    def acquire(self):
        if self._fh is not None:
            return True
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def release(self):
        if self._fh is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None
            try:
                os.remove(self.path)
            except OSError:
                pass
