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


def main():
    print("crowcall integration test\n" + "=" * 34)

    hub = subprocess.Popen([sys.executable, "hub.py", "--port", "45123",
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
    sys.exit(main())
