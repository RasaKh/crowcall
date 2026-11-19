#!/usr/bin/env python3
"""End-to-end two-station tests: data (text/file) and voice transfers with ARQ."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

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


def run_data():
    print("crowcall data transfer\n" + "=" * 32)
    hub = subprocess.Popen([sys.executable, os.path.join(ROOT, "hub.py"), "--port", "45301",
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
            [sys.executable, os.path.join(ROOT, "hub.py"), "--port", "45322", "--quiet"],
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




#!/usr/bin/env python3
"""
crowcall integration test
=========================

Drives two real Station objects through a real hub over real loopback UDP,
with real codec2 audio, and forces chunk loss to prove the repair round trip
actually recovers a message.

The Flask/SocketIO layer is stubbed out -- this tests the protocol state
machine, which is the part with the interesting failure modes. Emitted
events are captured so the test can assert on the sequence.

    python3 test_integration.py
"""

import subprocess
import sys
import time
import types

# ---- stub the web layer so app.py imports without flask installed --------

_events = []


_responder = None   # stands in for a human clicking Resend / Decline


class _SIO:
    def emit(self, name, data=None, **kw):
        _events.append((name, data or {}))
        if name == "repair_request" and _responder:
            _responder(data or {})

    def run(self, *a, **kw):
        pass


def _install_stubs():
    flask = types.ModuleType("flask")

    class Flask:
        def __init__(self, *a, **kw):
            self.config = {}

        def route(self, *a, **kw):
            return lambda f: f

        def response_class(self, *a, **kw):
            return None

    flask.Flask = Flask
    flask.jsonify = lambda *a, **kw: (a[0] if a else kw)
    flask.request = types.SimpleNamespace(files={}, json={})
    flask.send_from_directory = lambda *a, **kw: None
    sys.modules["flask"] = flask

    fsio = types.ModuleType("flask_socketio")
    fsio.SocketIO = lambda *a, **kw: _SIO()
    sys.modules["flask_socketio"] = fsio


_install_stubs()

import codec          # noqa: E402
import protocol as P  # noqa: E402
import app as A       # noqa: E402


def names(*want):
    return [e for e in _events if e[0] in want]


def make_recording(seconds=12):
    """A webm/opus blob shaped like something MediaRecorder would produce."""
    out = "/tmp/crowcall-test-rec.webm"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=170:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=820:duration={seconds}",
        "-filter_complex", "[0][1]amix=inputs=2,tremolo=f=3.5:d=0.75",
        "-c:a", "libopus", "-f", "webm", out,
    ], check=True)
    with open(out, "rb") as fh:
        return fh.read()


def wait_for(pred, timeout, label):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.15)
    print(f"    TIMEOUT waiting for {label} after {timeout}s")
    return False


def run_voice():
    print("crowcall integration test\n" + "=" * 34)

    hub = subprocess.Popen([sys.executable, os.path.join(ROOT, "hub.py"), "--port", "45123",
                            "--quiet"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    failures = []

    try:
        alpha = A.Station("ALPHA", ("127.0.0.1", 45123), pacing=False)
        bravo = A.Station("BRAVO", ("127.0.0.1", 45123), pacing=False)
        time.sleep(0.7)
        print(f"  alpha udp :{alpha.tx.local[1]}   bravo udp :{bravo.tx.local[1]}")

        blob = make_recording(12)
        print(f"  recording {len(blob)} B webm/opus\n")

        # ---- 1. clean channel ------------------------------------------
        print("1. clean channel")
        _events.clear()
        msg_id, n, secs, _ = alpha.send_recording(blob, ".webm")
        print(f"   {secs:.1f}s speech -> {n} chunks "
              f"({P.air_seconds(n):.1f}s of air at radio speed)")
        ok = wait_for(lambda: any(e[0] == "rx_done" for e in _events), 25,
                      "rx_done")
        done = names("rx_done")
        if ok and done:
            d = done[-1][1]
            print(f"   bravo: {d['have']}/{d['of']} chunks, "
                  f"{d['loss_pct']}% lost, degraded={d['degraded']}")
            if d["lost"] != 0:
                failures.append("clean channel lost chunks")
            if d["msg_id"] not in bravo.audio:
                failures.append("no playable audio stored")
            else:
                print(f"   playable wav: {len(bravo.audio[d['msg_id']])} B")
        else:
            failures.append("clean channel never completed")

        # ---- 2. lossy channel, repair enabled --------------------------
        print("\n2. 35% static loss, alpha auto-answers repair")
        bravo.tx.impair.update(loss_pct=35.0)
        alpha.auto_repair = True
        _events.clear()
        msg_id, n, secs, _ = alpha.send_recording(blob, ".webm")
        ok = wait_for(lambda: any(e[0] == "rx_done" for e in _events), 40,
                      "rx_done after repair")
        asked = names("repair_asked")
        done = names("rx_done")
        print(f"   repair rounds requested: {len(asked)}")
        for _, d in asked:
            print(f"     round {d['round']}: asked for {d['count']}/{d['of']}")
        if ok and done:
            d = done[-1][1]
            print(f"   final: {d['have']}/{d['of']} chunks, "
                  f"{d['repaired']} recovered by repair, "
                  f"{d['lost']} still missing")
            if not asked:
                failures.append("lossy channel triggered no repair request")
            if d["repaired"] == 0:
                failures.append("repair recovered nothing")
            if d["lost"] > 0:
                print(f"   note: {d['lost']} chunks never arrived even after "
                      f"{len(asked)} rounds -- concealed on playback")
        else:
            failures.append("lossy channel never finalised")

        # ---- 3. repair explicitly declined -----------------------------
        print("\n3. 35% loss, alpha DECLINES repair (RNAK path)")
        alpha.auto_repair = False
        declines = []

        # play the part of the operator hitting Decline on the prompt
        global _responder

        def decline(info):
            declines.append(info.get("key"))
            alpha.answer_repair(info["key"], False)

        _responder = decline
        _events.clear()
        t0 = time.time()
        msg_id, n, secs, _ = alpha.send_recording(blob, ".webm")
        ok = wait_for(lambda: any(e[0] == "rx_done" for e in _events), 45,
                      "degraded finalisation")
        elapsed = time.time() - t0
        done = names("rx_done")
        if ok and done:
            d = done[-1][1]
            refused = [e for e in _events
                       if e[0] == "repair_result" and not e[1].get("agree")]
            print(f"   declines sent: {len(declines)}, "
                  f"RNAK seen by receiver: {len(refused)}")
            print(f"   final: {d['have']}/{d['of']}, {d['lost']} missing, "
                  f"degraded={d['degraded']}, settled in {elapsed:.1f}s")
            if not declines:
                failures.append("no repair prompt reached the operator")
            if not refused:
                failures.append("receiver never saw the RNAK")
            if elapsed > A.FINAL_TIMEOUT:
                failures.append("RNAK did not short-circuit the timeout")
            if not d["degraded"]:
                failures.append("declined repair did not mark message degraded")
            if d["msg_id"] not in bravo.audio:
                failures.append("degraded message produced no audio")
            else:
                print(f"   still produced playable audio: "
                      f"{len(bravo.audio[d['msg_id']])} B "
                      f"({d['lost_s']:.1f}s concealed)")
        else:
            failures.append("declined repair never finalised")

        # ---- 4. total blackout -----------------------------------------
        print("\n4. total blackout (100% loss) -- must not hang")
        bravo.tx.impair.update(loss_pct=100.0)
        _events.clear()
        alpha.send_recording(blob, ".webm")
        time.sleep(4)
        heard = names("rx_start", "rx_done")
        print(f"   bravo heard nothing: {'yes' if not heard else 'NO'}")
        if heard:
            failures.append("packets arrived during 100% loss")
        print("   no assembler created, nothing to time out, no hang")

        alpha.tx.close()
        bravo.tx.close()

    finally:
        hub.terminate()
        hub.wait()

    print("\n" + "=" * 34)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0



if __name__ == "__main__":
    rc1 = run_data()
    rc2 = run_voice()
    sys.exit(1 if (rc1 or rc2) else 0)
