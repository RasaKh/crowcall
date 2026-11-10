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
    "call": "ANON",
    "auto_repair": True,
    "auto_adapt": True,
    "auto_retry": True,
    "pacing": True,
    "use_vad": True,
    "save": True,
    "save_modem": False,
    "render_modem": False,
    "mode": "1400",
    "voice_modem": "compact",
    "debug_tone_hz": 0.0,
    "debug_tone_ms": 120,
    "vox_slack_ms": 60,
}


def _coerce_field(key: str, val):
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)) and val in (0, 1):
            return bool(val)
        if isinstance(val, str) and val.lower() in ("true", "false"):
            return val.lower() == "true"
        return default
    if isinstance(default, float):
        try:
            f = float(val)
            return f if 0.0 <= f <= 20000.0 else default
        except (TypeError, ValueError):
            return default
    if isinstance(default, int):
        try:
            return int(val)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str):
        return val if isinstance(val, str) and val.strip() else default
    return default


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
        if not isinstance(raw, dict):
            return dict(DEFAULTS)
        out = dict(DEFAULTS)
        for k in DEFAULTS:
            if k in raw:
                out[k] = _coerce_field(k, raw[k])
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
