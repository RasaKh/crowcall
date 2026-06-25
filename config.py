"""Small, typed-ish configuration store used by the station.

The store deliberately accepts only known scalar settings and writes them by
rename, so a browser slider cannot leave a half-written JSON file after a
power loss.  Runtime objects still own the effective values; this module only
persists accepted changes and CLI defaults.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading


DEFAULTS = {
    "auto_repair": True,
    "pacing": True,
    "use_vad": True,
    "save": True,
    "save_modem": False,
    "render_modem": False,
    "mode": "1400",
    "voice_modem": "compact",
    "debug_tone_hz": 0.0,
}


class ConfigStore:
    def __init__(self, path: str | None = None):
        self.path = os.path.abspath(path) if path else None
        self._lock = threading.RLock()

    def load(self) -> dict:
        if not self.path:
            return dict(DEFAULTS)
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError, TypeError):
            return dict(DEFAULTS)
        out = dict(DEFAULTS)
        for k in DEFAULTS:
            if k in raw:
                out[k] = raw[k]
        return out

    def save(self, values: dict) -> None:
        if not self.path:
            return
        data = {k: values[k] for k in DEFAULTS if k in values}
        parent = os.path.dirname(self.path) or "."
        os.makedirs(parent, exist_ok=True)
        with self._lock:
            fd, tmp = tempfile.mkstemp(prefix=".crowcall-", suffix=".json",
                                       dir=parent, text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2, sort_keys=True)
                    fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
