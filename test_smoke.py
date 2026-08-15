#!/usr/bin/env python3
"""
crowcall smoke test
===================

Imports every module and constructs what app.py constructs at startup.

This exists because a str.replace that silently did not match left
AicodixModem assigning from parameters that no longer existed. Everything
compiled, every other test passed -- because none of them build a real
AicodixModem -- and it only surfaced on launch as
`cannot start: name 'modulation' is not defined`.

Compiling is not constructing. Run this before trusting a refactor.

    python3 test_smoke.py
"""

import os
import sys
import tempfile
import types


def stub_web():
    flask = types.ModuleType("flask")

    class Flask:
        def __init__(self, *a, **kw):
            self.config = {}

        def route(self, *a, **kw):
            return lambda f: f

    flask.Flask = Flask
    flask.jsonify = lambda *a, **kw: (a[0] if a else kw)
    flask.request = types.SimpleNamespace(files={}, json={})
    flask.send_from_directory = lambda *a, **kw: None
    sys.modules["flask"] = flask
    fs = types.ModuleType("flask_socketio")
    fs.SocketIO = lambda *a, **kw: types.SimpleNamespace(
        emit=lambda *a, **k: None, run=lambda *a, **k: None)
    sys.modules["flask_socketio"] = fs


def fake_vendor():
    d = tempfile.mkdtemp(prefix="crowcall-smoke-")
    for sub in ("modem", "disorders"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    for b in ("encode", "decode"):
        p = os.path.join(d, "modem", b)
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(p, 0o755)
    return d


def main():
    stub_web()
    fails = []

    print("importing every module")
    import archive, codec, modes, modem, protocol, radio, transport  # noqa
    import app                                                        # noqa
    print("  ok")

    print("constructing AicodixModem (the one that broke)")
    m = modem.AicodixModem(vendor=fake_vendor(), callsign="SMOKE")
    for attr in ("modulation", "coderate", "framesize", "payload_bytes",
                 "default_mode", "fallbacks_used"):
        try:
            getattr(m, attr)
        except Exception as exc:                     # noqa: BLE001
            fails.append(f"AicodixModem.{attr}: {exc!r}")
    if m.modulation == "QAM16":
        fails.append("modem still defaults to the mode we measured as unreliable")
    print(f"  {m.default_mode.name}: {m.modulation} {m.coderate} {m.framesize}")

    print("constructing a Station on each transport")
    st = app.Station("SMOKE", ("127.0.0.1", 45999), pacing=False,
                     rec_root=tempfile.mkdtemp())
    for attr in ("chunk_audio_status", "archive", "tx", "mode"):
        if not hasattr(st, attr):
            fails.append(f"Station missing {attr}")
    st.tx.close()
    print("  hub transport ok")

    print("exercising every mode end to end through the protocol")
    for mm in modes.MODES:
        b = protocol.pack(protocol.T_DAT, "SMOKE", 1, b"\x01" * mm.usable,
                          total=1, mode=protocol.pack_mode("1400", mm),
                          modem_mode=mm)
        got = protocol.unpack(b)
        if not got or got["modem_name"] != mm.name or len(b) != mm.payload:
            fails.append(f"{mm.name}: pack/unpack mismatch")
    print(f"  {len(modes.MODES)} modes round-trip")

    print("cross-station repair must not hit the wrong message")
    # msg_id is 16 bits; two stations can hold the same id at once. The inbox
    # is keyed by (station, msg_id) but _on_answer used to match on msg_id
    # alone, so one station's RNAK marked another station's message declined.
    import tempfile as _tf
    st2 = app.Station("XCHK", ("127.0.0.1", 45997), pacing=False,
                      rec_root=_tf.mkdtemp())
    same_id = 0x4242
    a1 = protocol.Assembler(same_id, "ALFA", 4, "1400", modem_mode=modes.COMPACT)
    a2 = protocol.Assembler(same_id, "BETA", 4, "1400", modem_mode=modes.COMPACT)
    st2.inbox[(0x1111, same_id)] = a1
    st2.inbox[(0x2222, same_id)] = a2
    st2._on_answer({"type": protocol.T_RNAK, "station": 0x1111,
                    "msg_id": same_id, "callsign": "ALFA"})
    if a2.declined:
        fails.append("RNAK from one station declined another station's message")
    if not a1.declined:
        fails.append("RNAK did not reach the message it was for")
    print(f"  station 0x1111 declined={a1.declined}, "
          f"station 0x2222 untouched={not a2.declined}")

    print("eviction must actually reclaim memory")
    import time as _t
    for i in range(200):
        st2.outbox[i] = {"chunks": [b"x" * 1516] * 20, "at": _t.time(),
                         "mode": "1400", "kind": protocol.KIND_FILE,
                         "modem": modes.FAST, "name": ""}
        st2.audio[f"{i:04x}"] = b"w" * 1000
    st2._reap()
    if len(st2.outbox) > app.OUTBOX_MAX or len(st2.audio) > app.PAYLOAD_MAX:
        fails.append(f"eviction left outbox={len(st2.outbox)} "
                     f"audio={len(st2.audio)}")
    print(f"  outbox 200 -> {len(st2.outbox)}, audio 200 -> {len(st2.audio)}")
    st2.tx.close()

    print("\n" + ("FAILURES:" if fails else "all checks passed"))
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
