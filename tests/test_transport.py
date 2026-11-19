import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)
#!/usr/bin/env python3
"""
crowcall transport test -- fragmentation + audio-domain channel
==============================================================

Exercises ModemUdpTransport without the aicodix binaries, using a genuine
(if crude) BPSK modem as a test double: 2048 bits on a 1500 Hz carrier,
plus a CRC so a single bit error kills the whole burst.

That CRC is the point. It reproduces the sharp cliff the real modem has --
P(all 2048 bits survive) collapses fast as SNR drops -- so the transport is
tested against all-or-nothing behaviour rather than graceful degradation.

    python3 test_transport.py
"""

import subprocess
import sys
import time
import zlib

import numpy as np

import radio

RATE = 48000
SAMPLES_PER_BIT = 24
CARRIER = 1500.0


class BpskTestModem:
    """Real modulation, real demodulation, hard CRC. No shared state."""

    modulation, coderate = "BPSK-test", "1/1"

    def __init__(self):
        self.rate = RATE

    def modulate(self, burst: bytes, mode=None):
        burst = burst.ljust(256, b"\x00")[:256]
        framed = burst + zlib.crc32(burst).to_bytes(4, "big")
        bits = np.unpackbits(np.frombuffer(framed, dtype=np.uint8))
        sym = (bits.astype("float64") * 2 - 1).repeat(SAMPLES_PER_BIT)
        t = np.arange(len(sym)) / RATE
        x = 0.35 * sym * np.sin(2 * np.pi * CARRIER * t)
        return x.astype("float32"), RATE

    def demodulate(self, samples, rate):
        x = np.asarray(samples, dtype="float64")
        nbits = 260 * 8
        need = nbits * SAMPLES_PER_BIT
        if len(x) < need:
            return None
        x = x[:need]
        t = np.arange(need) / rate
        ref = np.sin(2 * np.pi * CARRIER * t)
        corr = (x * ref).reshape(nbits, SAMPLES_PER_BIT).sum(axis=1)
        bits = (corr > 0).astype(np.uint8)
        data = np.packbits(bits).tobytes()
        payload, crc = data[:256], data[256:260]
        if zlib.crc32(payload).to_bytes(4, "big") != crc:
            return None                      # the sharp cliff
        return payload


def main():
    hub = subprocess.Popen([sys.executable, "hub.py", "--port", "45188",
                            "--quiet"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    failures = []

    try:
        got, drops = [], []
        A = radio.ModemUdpTransport(("127.0.0.1", 45188), BpskTestModem())
        B = radio.ModemUdpTransport(("127.0.0.1", 45188), BpskTestModem(),
                                    on_recv=lambda b: got.append(b),
                                    on_drop=lambda b, r: drops.append(r))
        time.sleep(0.6)
        burst = bytes((i * 7 + 3) & 0xFF for i in range(256))
        print(f"burst {len(burst)} B -> "
              f"{len(A.modem.modulate(burst)[0]) / RATE:.2f}s audio -> "
              f"{-(-len(A.modem.modulate(burst)[0]) * 2 // radio.FRAG_PAYLOAD)} "
              f"fragments\n")

        def run(n=6, pause=0.10, settle=1.4):
            got.clear()
            drops.clear()
            for _ in range(n):
                A.send(burst)
                time.sleep(pause)
            time.sleep(settle)
            return sum(1 for g in got if g[:256] == burst), list(drops)

        print("1. clean channel, impairments off")
        B.impair.update(enabled=False)
        ok, d = run()
        print(f"   {ok}/6 decoded, {len(d)} dropped")
        if ok != 6:
            failures.append(f"clean channel only decoded {ok}/6")

        print("\n2. SNR sweep (in-band dB; measured cliff is 9.5)")
        B.impair.update(enabled=True)
        curve = {}
        for snr in (30, 20, 15, 12, 10, 8, 6, 3, 0):
            B.impair.update(snr_db=snr)
            ok, d = run(n=5, settle=1.2)
            curve[snr] = ok
            bar = "#" * (ok * 4)
            print(f"   {snr:3d} dB -> {ok}/5  {bar}"
                  + (f"   [{d[0]}]" if d else ""))
        top = [s for s, v in curve.items() if v == 5]
        bottom = [s for s, v in curve.items() if v == 0]
        if not top:
            failures.append("no SNR decoded reliably")
        if not bottom:
            failures.append("no SNR failed -- the channel model does nothing")
        if top and bottom and min(top) <= max(bottom):
            failures.append("SNR curve is not monotonic")
        else:
            print(f"   reliable at/above {min(top)} dB, dead at/below "
                  f"{max(bottom)} dB")

        print("\n3. fade blacks out a time window, not scattered bursts")
        B.impair.update(snr_db=30, fade_prob=1.0, fade_ms=1500)
        ok, d = run(n=5, pause=0.1, settle=1.0)
        print(f"   during fade: {ok}/5 decoded, {len(d)} dropped, "
              f"reasons {set(d) or '-'}")
        if ok != 0:
            failures.append("fade did not black out the channel")
        if d and set(d) != {"fade"}:
            failures.append(f"fade drops reported as {set(d)}")

        B.impair.update(fade_prob=0.0)
        time.sleep(1.7)
        ok, d = run(n=4, settle=1.0)
        print(f"   after it clears: {ok}/4 decoded")
        if ok != 4:
            failures.append(f"channel did not recover after fade ({ok}/4)")

        print("\n4. no fragments left pending, no leaks")
        time.sleep(radio.STREAM_TTL + 1.5)
        sa, sb = A.stats(), B.stats()
        print(f"   A tx={sa['tx']} pending={sa['pending']}")
        print(f"   B rx={sb['rx']} dropped={sb['dropped']} "
              f"frag_lost={sb['frag_lost']} pending={sb['pending']}")
        if sb["pending"] != 0:
            failures.append(f"{sb['pending']} streams left pending")
        if sb["frag_lost"]:
            print(f"   note: {sb['frag_lost']} fragments lost to socket "
                  f"buffer pressure over {sa['tx']} bursts")

        A.close()
        B.close()
    finally:
        hub.terminate()
        hub.wait()

    print("\n" + "=" * 40)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
