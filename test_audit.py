#!/usr/bin/env python3
"""Focused regression tests for the audit findings.

These are deliberately small and deterministic: malformed packets and forged
audio fragments must be rejected before they can consume receiver state.
"""
from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

import modes as M
import protocol as P
import radio


class ProtocolValidationTests(unittest.TestCase):
    def test_dat_chunk_index_must_be_below_total(self):
        raw = bytearray(P.pack(P.T_DAT, "ALPHA", 7, b"x", total=1,
                               modem_mode=M.CONTROL))
        # chunk_idx is at offset 36 in the v3 header.
        raw[36:38] = (1).to_bytes(2, "big")
        self.assertIsNone(P.unpack(bytes(raw)))

    def test_rreq_must_have_nonzero_total(self):
        raw = P.pack(P.T_RREQ, "ALPHA", 7, b"\x00",
                     modem_mode=M.CONTROL)
        self.assertIsNone(P.unpack(raw))

    def test_beacon_cannot_carry_a_body(self):
        raw = P.pack(P.T_BEACON, "ALPHA", 0, b"bad", modem_mode=M.CONTROL,
                     pad=False)
        self.assertIsNone(P.unpack(raw))


    def test_data_assembler_rejects_short_first_chunk(self):
        asm = P.Assembler(9, "ALPHA", 2, "1400", kind=P.KIND_FILE,
                          modem_mode=M.COMPACT)
        self.assertFalse(asm.add(0, b"x" * 10))
        self.assertIsNone(asm.chunk_bytes)

    def test_data_assembler_rejects_wrong_sized_chunk(self):
        asm = P.Assembler(10, "ALPHA", 2, "1400", kind=P.KIND_FILE,
                          modem_mode=M.COMPACT)
        self.assertFalse(asm.add(0, b"x" * (M.COMPACT.usable - 1)))
        self.assertIsNone(asm.chunk_bytes)


class RadioFragmentTests(unittest.TestCase):
    def setUp(self):
        class DummyModem:
            rate = 48000
        self.tx = radio.ModemUdpTransport(("127.0.0.1", 45996), DummyModem(),
                                           frag_payload=16)
        self.addCleanup(self.tx.close)

    def test_wrong_fragment_length_is_rejected(self):
        body = b"x" * 15
        head = struct.pack(radio.FRAG_FMT, radio.MAGIC, 1, 1, 1, 0,
                           len(body), 0, 48000, 8, 0)
        self.tx._on_fragment(head + body)
        self.assertEqual(self.tx.stats()["pending"], 0)

    def test_stream_state_is_bounded(self):
        # A receiver must not allocate one stream object for every forged
        # session it sees. Fill the bounded window and ensure the oldest
        # stream is evicted, not retained forever.
        for sid in range(radio.MAX_STREAMS + 8):
            # Two fragments are declared, but only the first is supplied, so
            # each stream remains incomplete and exercises the state cap.
            head = struct.pack(radio.FRAG_FMT, radio.MAGIC, 1, sid, 2, 0,
                               16, 0, 48000, 8, 0)
            self.tx._on_fragment(head + b"x" * 16)
        self.assertLessEqual(self.tx.stats()["pending"], radio.MAX_STREAMS)


if __name__ == "__main__":
    unittest.main()
