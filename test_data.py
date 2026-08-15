#!/usr/bin/env python3
"""
crowcall data-transfer test
===========================

Text and arbitrary files over the same protocol as voice. What differs:

  * the full 236 B payload is usable (no codec2 frame alignment)
  * padding is fatal, so the stream carries its own exact lengths
  * a missing chunk means a broken file, not a rough one -- so repair stops
    being optional and the CRC has to be checked rather than assumed

    python3 test_data.py
"""

import os
import subprocess
import sys
import time
import types
import zlib

_events = []
_responder = None


class _SIO:
    def emit(self, name, data=None, **kw):
        _events.append((name, data or {}))
        if name == "repair_request" and _responder:
            _responder(data or {})

    def run(self, *a, **kw):
        pass


def _stub():
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
    fsio = types.ModuleType("flask_socketio")
    fsio.SocketIO = lambda *a, **kw: _SIO()
    sys.modules["flask_socketio"] = fsio


_stub()

import app as A            # noqa: E402
import protocol as P       # noqa: E402


def wait(pred, timeout, label):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.15)
    print(f"    TIMEOUT: {label}")
    return False


def last(name):
    hits = [d for n, d in _events if n == name]
    return hits[-1] if hits else None


def main():
    print("crowcall data transfer\n" + "=" * 32)
    hub = subprocess.Popen([sys.executable, "hub.py", "--port", "45301",
                            "--quiet"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    failures = []
    root = "/tmp/crowcall-data-test"

    try:
        A_root = root
        alpha = A.Station("ALPHA", ("127.0.0.1", 45301), pacing=False,
                          rec_root=root)
        bravo = A.Station("BRAVO", ("127.0.0.1", 45301), pacing=False,
                          rec_root=root)
        time.sleep(0.6)

        # ---- 1. text ---------------------------------------------------
        print("\n1. text, clean channel")
        msg = ("Kabatas ucus bilgisi: ruzgar guney-bati, gorus 8 km. "
               "Bir sonraki istasyon kapali. " * 4)
        _events.clear()
        alpha.send_data(msg.encode(), P.KIND_TEXT)
        ok = wait(lambda: last("rx_done"), 25, "text rx_done")
        d = last("rx_done") or {}
        s = last("tx_start") or {}
        print(f"   {s.get('bytes')} B -> {s.get('carried')} B "
              f"{s.get('compression')} ({s.get('ratio')}) "
              f"-> {s.get('chunks')} chunks, {s.get('air_human')} on air")
        print(f"   received kind={d.get('kind')} intact={d.get('intact')} "
              f"{d.get('have')}/{d.get('of')} chunks")
        if not ok or d.get("kind") != "text":
            failures.append("text never arrived as text")
        elif d.get("text") != msg:
            failures.append("text did not match")
        elif not d.get("intact"):
            failures.append("clean text reported as damaged")
        else:
            print("   text matches exactly")

        # ---- 2. incompressible file ------------------------------------
        print("\n2. file, incompressible, clean channel")
        blob = os.urandom(6000)
        _events.clear()
        alpha.send_data(blob, P.KIND_FILE, "sample.bin")
        ok = wait(lambda: last("rx_done"), 45, "file rx_done")
        d = last("rx_done") or {}
        s = last("tx_start") or {}
        print(f"   {s.get('bytes')} B, compression={s.get('compression')} "
              f"-> {s.get('chunks')} chunks, {s.get('air_human')} on air")
        print(f"   received {d.get('filename')} intact={d.get('intact')} "
              f"{d.get('recovered')}/{d.get('bytes')} B")
        mid = d.get("msg_id")
        if not ok or mid not in bravo.files:
            failures.append("file never stored")
        else:
            name, data, intact = bravo.files[mid]
            print(f"   stored as {name!r}, bytes identical: {data == blob}")
            if data != blob:
                failures.append("file bytes differ")
            if s.get("compression") != "none":
                failures.append("wasted effort compressing random data")

        # ---- 3. file with loss, repair enabled -------------------------
        print("\n3. file with 25% loss, repair on")
        bravo.tx.impair.update(loss_pct=25.0)
        alpha.auto_repair = True
        blob2 = os.urandom(4000)
        _events.clear()
        alpha.send_data(blob2, P.KIND_FILE, "under-fire.bin")
        ok = wait(lambda: last("rx_done"), 90, "lossy file rx_done")
        d = last("rx_done") or {}
        rounds = [x for n, x in _events if n == "repair_asked"]
        print(f"   repair rounds: {len(rounds)}")
        print(f"   intact={d.get('intact')} lost_chunks={d.get('lost')} "
              f"lost_bytes={d.get('lost_bytes')}")
        mid = d.get("msg_id")
        if ok and mid in bravo.files and bravo.files[mid][1] == blob2:
            print("   recovered byte-for-byte after repair")
        elif ok:
            print(f"   NOT fully recovered -- CRC correctly reported "
                  f"intact={d.get('intact')}")
            if d.get("intact"):
                failures.append("damaged file wrongly reported intact")
        else:
            failures.append("lossy file never finalised")

        # ---- 4. losing chunk 0 destroys the prefix ---------------------
        print("\n4. the prefix chunk is the one that matters")
        stream, info = P.wrap_payload(os.urandom(3000), P.KIND_FILE, "x.bin")
        chunks = P.chunkify_data(stream)
        asm = P.Assembler(1, "X", len(chunks), kind=P.KIND_FILE)
        for i, c in enumerate(chunks):
            if i:
                asm.add(i, c)
        print(f"   {len(chunks)} chunks, dropped chunk 0 -> "
              f"unwrap returns {P.unwrap_payload(asm.assemble_data())}")
        if P.unwrap_payload(asm.assemble_data()) is not None:
            failures.append("prefix loss not detected")
        print(f"   transmit order sends it first AND last: "
              f"{P.data_order(len(chunks))[:3]}...{P.data_order(len(chunks))[-2:]}")

        # ---- 4b. REGRESSION: no repair mid-transmission ----------------
        #
        # This test failed to catch the original bug because it ran with
        # pacing=False -- bursts went out instantly, so the gaps that caused
        # the failure never existed. It now uses a REAL paced station.
        print("\n4b. repair must not fire while the sender is still sending")
        import modes as MM
        for mm in MM.MODES:
            asm = P.Assembler(1, "X", 68, "1400", kind=P.KIND_FILE,
                              modem_mode=mm)
            if asm.quiet_timeout <= mm.air_s * 1.5:
                failures.append(f"{mm.name}: quiet timeout too close to burst")
            if asm.final_timeout <= asm.quiet_timeout:
                failures.append(f"{mm.name}: final timeout preempts repair")
            print(f"    {mm.name:8s} burst {mm.air_s:5.2f}s, quiet "
                  f"{asm.quiet_timeout:5.2f}s, expects "
                  f"{asm.expected_duration():6.1f}s for 68 chunks")

        print("    now with PACING ON, which is what actually broke:")
        paced_hub = subprocess.Popen(
            [sys.executable, "hub.py", "--port", "45322", "--quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.6)
        try:
            pa = A.Station("PACEA", ("127.0.0.1", 45322), pacing=True,
                           rec_root=root)
            pb = A.Station("PACEB", ("127.0.0.1", 45322), pacing=True,
                           rec_root=root)
            time.sleep(0.5)
            pa.auto_repair = True
            _events.clear()
            # 4 chunks on the file mode = 4 x 3.68 s of real paced air time,
            # with 3.68 s gaps that used to look like end-of-message
            blob3 = os.urandom(5000)
            pa.send_data(blob3, P.KIND_FILE, "paced.bin")
            mm = MM.for_role("file", len(blob3))
            budget = (-(-len(blob3) // mm.usable) + 2) * mm.air_s + 25
            print(f"    {-(-len(blob3)//mm.usable)} chunks on {mm.name} "
                  f"({mm.air_s:.2f}s each), waiting up to {budget:.0f}s")
            ok = wait(lambda: last("rx_done"), budget, "paced file")
            d = last("rx_done") or {}
            asked = [x for n, x in _events if n == "repair_asked"]
            print(f"    repair requests during a clean paced transfer: "
                  f"{len(asked)}")
            if asked:
                failures.append(f"repair fired {len(asked)}x mid-transmission "
                                f"on a clean paced channel")
            if not ok:
                failures.append("paced file transfer never completed")
            elif not d.get("intact"):
                failures.append(f"paced transfer not intact: "
                                f"{d.get('lost')} chunks lost")
            else:
                print(f"    intact={d.get('intact')}, "
                      f"{d.get('of')} chunks, zero repair rounds")
            pa.tx.close()
            pb.tx.close()
        finally:
            paced_hub.terminate()
            paced_hub.wait()

        # ---- 5. what the numbers actually mean -------------------------
        print("\n5. air time, for reference")
        for n, label in [(280, "a paragraph"), (2048, "2 KB"),
                         (102400, "100 KB"), (1048576, "1 MB")]:
            e = P.plan(n, "file")
            print(f"   {label:13s} {e['mode']:8s} {e['bursts']:5d} bursts  {e['air_human']:>8s}")

        alpha.tx.close()
        bravo.tx.close()
    finally:
        hub.terminate()
        hub.wait()

    print("\n" + "=" * 32)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
