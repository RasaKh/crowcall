#!/usr/bin/env python3
"""Fast deterministic unit, protocol, smoke, security, and handover tests."""
from __future__ import annotations

import json
import os
import pathlib
import struct
import sys
import tempfile
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _stub_web():
    if "flask" in sys.modules:
        return
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


def _fake_vendor():
    d = tempfile.mkdtemp(prefix="crowcall-smoke-")
    for sub in ("modem", "disorders"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    for b in ("encode", "decode"):
        p = os.path.join(d, "modem", b)
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(p, 0o755)
    return d


import modes as M
import modem as MODEM
import protocol as P
import radio
from archive import Archive
from config import ConfigStore, DEFAULTS


class ProtocolValidationTests(unittest.TestCase):
    def test_dat_chunk_index_must_be_below_total(self):
        raw = bytearray(P.pack(P.T_DAT, "ALPHA", 7, b"x", total=1,
                               modem_mode=M.CONTROL))
        raw[36:38] = (1).to_bytes(2, "big")
        self.assertIsNone(P.unpack(bytes(raw)))

    def test_rreq_must_have_nonzero_total(self):
        raw = P.pack(P.T_RREQ, "ALPHA", 7, b"\x00", modem_mode=M.CONTROL)
        self.assertIsNone(P.unpack(raw))

    def test_beacon_cannot_carry_a_body(self):
        raw = P.pack(P.T_BEACON, "ALPHA", 0, b"bad", modem_mode=M.CONTROL, pad=False)
        self.assertIsNone(P.unpack(raw))

    def test_data_assembler_rejects_short_first_chunk(self):
        asm = P.Assembler(9, "ALPHA", 2, "1400", kind=P.KIND_FILE, modem_mode=M.COMPACT)
        self.assertFalse(asm.add(0, b"x" * 10))
        self.assertIsNone(asm.chunk_bytes)

    def test_data_assembler_rejects_wrong_sized_chunk(self):
        asm = P.Assembler(10, "ALPHA", 2, "1400", kind=P.KIND_FILE, modem_mode=M.COMPACT)
        self.assertFalse(asm.add(0, b"x" * (M.COMPACT.usable - 1)))
        self.assertIsNone(asm.chunk_bytes)


class RadioFragmentTests(unittest.TestCase):
    def setUp(self):
        class DummyModem:
            rate = 48000
        self.tx = radio.ModemUdpTransport(("127.0.0.1", 45996), DummyModem(), frag_payload=16)
        self.addCleanup(self.tx.close)

    def test_wrong_fragment_length_is_rejected(self):
        body = b"x" * 15
        head = struct.pack(radio.FRAG_FMT, radio.MAGIC, 1, 1, 1, 0,
                           len(body), 0, 48000, 8, 0)
        self.tx._on_fragment(head + body)
        self.assertEqual(self.tx.stats()["pending"], 0)

    def test_stream_state_is_bounded(self):
        for sid in range(radio.MAX_STREAMS + 8):
            head = struct.pack(radio.FRAG_FMT, radio.MAGIC, 1, sid, 2, 0,
                               16, 0, 48000, 8, 0)
            self.tx._on_fragment(head + b"x" * 16)
        self.assertLessEqual(self.tx.stats()["pending"], radio.MAX_STREAMS)


class SmokeAndRegressionTests(unittest.TestCase):
    def test_smoke_station_and_modes(self):
        _stub_web()
        import app
        m = MODEM.AicodixModem(vendor=_fake_vendor(), callsign="SMOKE")
        for attr in ("modulation", "coderate", "framesize", "payload_bytes",
                     "default_mode", "fallbacks_used"):
            self.assertTrue(hasattr(m, attr))
        self.assertNotEqual(m.modulation, "QAM16")

        st = app.Station("SMOKE", ("127.0.0.1", 45999), pacing=False,
                         rec_root=tempfile.mkdtemp())
        self.addCleanup(st.close)
        for attr in ("chunk_audio_status", "archive", "tx", "mode"):
            self.assertTrue(hasattr(st, attr))

        for mm in M.MODES:
            b = P.pack(P.T_DAT, "SMOKE", 1, b"\x01" * mm.usable,
                       total=1, mode=P.pack_mode("1400", mm), modem_mode=mm)
            got = P.unpack(b)
            self.assertIsNotNone(got)
            self.assertEqual(got["modem_name"], mm.name)
            self.assertEqual(len(b), mm.payload)

    def test_cross_station_repair_isolation_and_eviction(self):
        _stub_web()
        import app
        st = app.Station("XCHK", ("127.0.0.1", 45997), pacing=False,
                         rec_root=tempfile.mkdtemp())
        self.addCleanup(st.close)
        same_id = 0x4242
        a1 = P.Assembler(same_id, "ALFA", 4, "1400", modem_mode=M.COMPACT)
        a2 = P.Assembler(same_id, "BETA", 4, "1400", modem_mode=M.COMPACT)
        st.inbox[(0x1111, same_id)] = a1
        st.inbox[(0x2222, same_id)] = a2
        st._on_answer({"type": P.T_RNAK, "station": 0x1111,
                        "msg_id": same_id, "callsign": "ALFA"})
        self.assertTrue(a1.declined)
        self.assertFalse(a2.declined)

        for i in range(200):
            st.outbox[i] = {"chunks": [b"x" * 1516] * 20, "at": time.time(),
                            "mode": "1400", "kind": P.KIND_FILE,
                            "modem": M.FAST, "name": ""}
            st.audio[f"{i:04x}"] = b"w" * 1000
        st._reap()
        self.assertLessEqual(len(st.outbox), app.OUTBOX_MAX)
        self.assertLessEqual(len(st.audio), app.PAYLOAD_MAX)

    def test_regressions_rreq_payload_and_archive_paths(self):
        body = P.encode_rreq(P.station_id("ALPHA"), [3], 10, M.CONTROL.usable)
        pkt = P.pack(P.T_RREQ, "BRAVO", 1, body, total=10,
                     modem_mode=M.CONTROL, origin=P.station_id("ALPHA"),
                     destination=P.station_id("ALPHA"))
        target, missing = P.decode_rreq(P.unpack(pkt)["body"], 10)
        self.assertEqual(target, P.station_id("ALPHA"))
        self.assertEqual(missing, [3])

        stream, _ = P.wrap_payload(b"payload", P.KIND_FILE, "\u00e9" * 100)
        bad = bytearray(stream)
        bad[3] = 99
        self.assertFalse(P.unwrap_payload(bytes(bad))["intact"])
        corrupted = bytearray(pkt)
        corrupted[P.HDR_LEN] ^= 1
        self.assertIsNone(P.unpack(bytes(corrupted)))

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "records"
            ar = Archive(str(root))
            p1 = pathlib.Path(ar.begin("rx", 7, "/../../", "1400", P.geom_for()))
            self.assertTrue(p1.resolve().is_relative_to(root.resolve()))
            p2 = ar.begin("rx", 7, "OTHER", "1400", P.geom_for())
            self.assertNotEqual(p2, str(p1))


class HandoverFeatureTests(unittest.TestCase):
    def test_vox_truncated_tone_and_exact_strip(self):
        import numpy as np
        rng = np.random.default_rng(4)
        payload_audio = rng.normal(0, 0.08, 20000).astype("float32")
        prefixed = MODEM.prepend_debug_tone(payload_audio, MODEM.RATE, 800)
        stripped, detected = MODEM.strip_debug_tone(prefixed, MODEM.RATE)
        self.assertIsNotNone(detected)
        self.assertLess(abs(detected - 800), 5)
        self.assertEqual(len(stripped), len(payload_audio))

        payload2 = np.random.default_rng(7).normal(0, 0.08, 24000).astype("float32")
        full_tone = MODEM.debug_tone(900.0, MODEM.RATE, duration_s=0.300)
        chopped = int(MODEM.RATE * 0.180)
        rx_audio = np.concatenate([full_tone[chopped:], payload2])
        stripped2, hz, heard_s = MODEM.strip_debug_tone_info(rx_audio, MODEM.RATE)
        self.assertIsNotNone(hz)
        self.assertLess(abs(hz - 900.0), 15.0)
        self.assertLess(abs(heard_s - 0.120), 0.025)
        self.assertLessEqual(abs(len(stripped2) - len(payload2)), int(MODEM.RATE * 0.025))

    def test_open_squelch_burst_detector(self):
        import numpy as np
        rng = np.random.default_rng(11)
        det = MODEM.BurstDetector(rate=MODEM.RATE, hold_ms=50.0, min_s=0.5)
        closed = rng.normal(0, 0.0001, int(MODEM.RATE * 0.4)).astype("float32")
        self.assertEqual(det.feed(closed), [])
        b1 = rng.normal(0, 0.20, int(MODEM.RATE * 0.8)).astype("float32")
        gap = rng.normal(0, 0.005, int(MODEM.RATE * 0.15)).astype("float32")
        b2 = rng.normal(0, 0.20, int(MODEM.RATE * 0.8)).astype("float32")
        bursts = det.feed(np.concatenate([b1, gap, b2, closed])) + det.flush()
        self.assertEqual(len(bursts), 2)

    def test_link_health_inertia_and_incentives(self):
        tr = P.LinkHealthTracker()
        for _ in range(20):
            tr.observe_burst(True, snr_db=15.5, tone_ms=80)
        self.assertGreaterEqual(tr.mode_inertia, 0.95)
        for _ in range(3):
            tr.observe_burst(False, snr_db=9.8)
        rec = tr.recommend("bulk", "1400")
        self.assertTrue(rec["downshift_urgency"])
        self.assertEqual(rec["remaining_dwell_s"], 0)

    def test_channel_key_encryption_and_auth(self):
        try:
            P.set_channel_key("post-apocalypse-secret")
            pkt = P.pack(P.T_DAT, "ALPHA", 42, b"secret-payload", chunk_idx=0, total=2,
                         mode=P.pack_mode("1400", M.COMPACT),
                         modem_mode=M.COMPACT, flags=P.pack_flags(False, P.KIND_TEXT, hops=2))
            self.assertNotIn(b"secret-payload", pkt)
            unpacked = P.unpack(pkt)
            self.assertIsNotNone(unpacked)
            self.assertEqual(unpacked["body"], b"secret-payload")
            self.assertEqual(unpacked["hops"], 2)
            P.set_channel_key("wrong-key")
            self.assertIsNone(P.unpack(pkt))
        finally:
            P.set_channel_key(None)

    def test_single_chunk_duration_and_config_coercion(self):
        asm = P.Assembler(1, "ALPHA", 1, "1400", kind=P.KIND_TEXT, modem_mode=M.COMPACT)
        self.assertAlmostEqual(asm.expected_duration(), M.COMPACT.air_s, places=6)

        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(path, "w") as fh:
                json.dump({"auto_repair": "notabool", "debug_tone_hz": -50, "mode": 123}, fh)
            loaded = ConfigStore(path).load()
            self.assertIs(loaded["auto_repair"], DEFAULTS["auto_repair"])
            self.assertEqual(loaded["debug_tone_hz"], DEFAULTS["debug_tone_hz"])
            self.assertEqual(loaded["mode"], DEFAULTS["mode"])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
