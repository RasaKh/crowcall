#!/usr/bin/env python3
"""Fast deterministic regressions for the safety fixes in protocol v3."""
from __future__ import annotations

import pathlib
import tempfile

import modes as M
import protocol as P
import modem
from archive import Archive


def main():
    # Diagnostic audio is outside the modem frame and can be recovered without
    # changing the payload samples.  Auto-detection must cut at the first tone
    # window rather than at a later equally-correlated window.
    import numpy as np
    payload_audio = np.random.default_rng(4).normal(0, 0.08, 20000).astype("float32")
    prefixed = modem.prepend_debug_tone(payload_audio, modem.RATE, 800)
    stripped, detected = modem.strip_debug_tone(prefixed, modem.RATE)
    assert detected is not None and abs(detected - 800) < 5
    assert len(stripped) == len(payload_audio)

    # A padded control packet must not invent chunk zero.
    body = P.encode_rreq(P.station_id("ALPHA"), [3], 10, M.CONTROL.usable)
    pkt = P.pack(P.T_RREQ, "BRAVO", 1, body, total=10,
                 modem_mode=M.CONTROL, origin=P.station_id("ALPHA"),
                 destination=P.station_id("ALPHA"))
    target, missing = P.decode_rreq(P.unpack(pkt)["body"], 10)
    assert target == P.station_id("ALPHA") and missing == [3]

    # Malformed/unknown compression and CRC failures are rejected.
    stream, _ = P.wrap_payload(b"payload", P.KIND_FILE, "\N{LATIN SMALL LETTER E WITH ACUTE}" * 100)
    bad = bytearray(stream); bad[3] = 99
    assert not P.unwrap_payload(bytes(bad))["intact"]
    corrupted = bytearray(pkt); corrupted[P.HDR_LEN] ^= 1
    assert P.unpack(bytes(corrupted)) is None

    # Archive names are generated and cannot be controlled by callsign.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td) / "records"
        ar = Archive(str(root))
        path = pathlib.Path(ar.begin("rx", 7, "/../../", "1400", P.geom_for()))
        assert path.resolve().is_relative_to(root.resolve())
        assert ar.begin("rx", 7, "OTHER", "1400", P.geom_for()) != str(path)

    print("all regression checks passed")


if __name__ == "__main__":
    main()
