#!/usr/bin/env python3
"""Supervisor + landing wrapper for the live crowcall.khosrofar.me demo.

Starts and supervises:
  - hub.py on 127.0.0.1:45000
  - station ALPHA on 127.0.0.1:5061 (--modem --demo)
  - station BRAVO on 127.0.0.1:5062 (--modem --demo)

Serves the split-screen demo wrapper on 127.0.0.1:5058.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, send_from_directory

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor"
HUB_PORT = int(os.environ.get("CROWCALL_HUB_PORT", "45000"))
ALPHA_PORT = int(os.environ.get("CROWCALL_ALPHA_PORT", "5061"))
BRAVO_PORT = int(os.environ.get("CROWCALL_BRAVO_PORT", "5062"))
WEB_PORT = int(os.environ.get("CROWCALL_WEB_PORT", "5058"))

SPECS = [
    ("hub", [sys.executable, str(ROOT / "hub.py"), "--port", str(HUB_PORT)]),
    ("alpha", [
        sys.executable, str(ROOT / "app.py"),
        "--call", "ALPHA",
        "--port", str(ALPHA_PORT),
        "--hub", f"127.0.0.1:{HUB_PORT}",
        "--modem",
        "--vendor", str(VENDOR),
        "--no-save",
        "--demo",
    ]),
    ("bravo", [
        sys.executable, str(ROOT / "app.py"),
        "--call", "BRAVO",
        "--port", str(BRAVO_PORT),
        "--hub", f"127.0.0.1:{HUB_PORT}",
        "--modem",
        "--vendor", str(VENDOR),
        "--no-save",
        "--demo",
    ]),
]

procs: dict[str, subprocess.Popen] = {}
stop_event = threading.Event()
lock = threading.Lock()


def start_child(name: str, cmd: list[str]) -> None:
    p = subprocess.Popen(cmd, cwd=str(ROOT))
    procs[name] = p
    print(f"[demo_runner] started {name} (pid={p.pid})", flush=True)


def stop_children() -> None:
    stop_event.set()
    with lock:
        items = list(procs.items())
    for name, p in items:
        if p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass
    deadline = time.time() + 5.0
    for name, p in items:
        rem = max(0.0, deadline - time.time())
        try:
            p.wait(timeout=rem)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
            except OSError:
                pass


def supervisor_loop() -> None:
    with lock:
        # Launch hub first so UDP listener is bound before stations announce
        start_child(SPECS[0][0], SPECS[0][1])
    time.sleep(0.6)
    with lock:
        for name, cmd in SPECS[1:]:
            start_child(name, cmd)

    while not stop_event.wait(2.0):
        with lock:
            for name, cmd in SPECS:
                p = procs.get(name)
                if p is None or p.poll() is not None:
                    rc = p.returncode if p else None
                    print(f"[demo_runner] {name} exited (rc={rc}), restarting", flush=True)
                    start_child(name, cmd)


app = Flask(__name__, static_folder="static")


@app.route("/")
def demo_index():
    return send_from_directory("static", "demo.html")


@app.route("/health")
def health():
    with lock:
        status = {name: (p.poll() is None) for name, p in procs.items()}
    ok = all(status.values()) and len(status) == len(SPECS)
    return jsonify({"ok": ok, "children": status}), (200 if ok else 503)


def main() -> int:
    def _handle_sig(signum, _frame):
        print(f"[demo_runner] received signal {signum}, shutting down", flush=True)
        stop_children()
        os._exit(0)

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    t = threading.Thread(target=supervisor_loop, daemon=True)
    t.start()
    try:
        app.run(host="127.0.0.1", port=WEB_PORT, threaded=True, use_reloader=False)
    finally:
        stop_children()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
