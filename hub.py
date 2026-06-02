#!/usr/bin/env python3
"""
crowcall hub -- the shared medium
=================================

A deliberately stupid reflector. Every station sends here; everything sent
gets repeated to every other station. No addressing, no routing, no
retries: whoever is listening hears it, whoever is not, does not. That is
the property we want, because it is how a radio channel behaves.

Impairments are NOT here -- each station applies its own on receive, so you
can have one station in good conditions and another in a fade.

    python3 hub.py            # port 45000
    python3 hub.py --port 45001 --quiet
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time

TYPE_NAMES = {1: "DAT", 2: "RREQ", 3: "RACK", 4: "RNAK", 5: "DONE", 6: "BEACON"}
PEER_TIMEOUT = 120.0

# Must exceed the largest datagram any transport sends. The modem transport
# ships fragments of modulated audio at 8 KB + header; reading 2048 here
# silently TRUNCATED every one of them, and since each stage tested fine on
# its own the corruption only appeared once the hub was in the loop.
RECV_MAX = 65536


def describe(blob: bytes) -> str:
    if blob[:4] == b"CRW3":                 # modulated-audio fragment
        try:
            _, session, sid, total, idx, total_bytes, offset, rate, _, _ = struct.unpack(">4sQQHHIIHIH", blob[:40])
            return (f"{'audio':8s} FRAG   burst{sid:04x} "
                    f"{idx + 1}/{total} @{rate} Hz  {len(blob)}B")
        except struct.error:
            return f"audio frag {len(blob)}B"
    if len(blob) < 20:
        return f"{len(blob)}B"
    try:
        magic, ver, ptype, flags, hlen, source, origin, dest, msg, exchange, idx, total, body_len, profile, codec_id, crc, call = \
            struct.unpack(">2sBBBBQQQQHHHHBBI8s", blob[:60])
    except struct.error:
        return f"{len(blob)}B"
    who = call.decode("ascii", "replace").strip() or f"{source:016x}"
    name = TYPE_NAMES.get(ptype, f"?{ptype}")
    if ptype == 1:
        return f"{who:8s} {name:6s} msg{msg:016x} chunk {idx + 1}/{total}"
    return f"{who:8s} {name:6s} msg{msg:04x}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=45000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, 8 << 20)
        except OSError:
            pass
    try:
        sock.bind((a.host, a.port))
    except OSError as exc:
        print(f"cannot bind {a.host}:{a.port} -- {exc}", file=sys.stderr)
        print("another hub may already be running", file=sys.stderr)
        return 1

    print(f"crowcall hub on {a.host}:{a.port}")
    print("stations appear as they transmit; ctrl-c to stop\n")

    peers: dict[tuple, float] = {}
    frames = 0

    try:
        while True:
            try:
                blob, src = sock.recvfrom(RECV_MAX)
            except KeyboardInterrupt:
                break

            now = time.time()
            new = src not in peers
            peers[src] = now
            for p in [p for p, t in peers.items() if now - t > PEER_TIMEOUT]:
                del peers[p]
                if not a.quiet:
                    print(f"  -- station on :{p[1]} went quiet")
            if new and not a.quiet:
                print(f"  ++ station on :{src[1]} joined "
                      f"({len(peers)} on channel)")

            if blob.startswith(b"\x00"):
                continue

            frames += 1
            sent = 0
            for p in peers:
                if p == src:
                    continue
                try:
                    sock.sendto(blob, p)
                    sent += 1
                except OSError:
                    pass
            if not a.quiet:
                print(f"  {describe(blob)}  -> {sent} listener(s)")
    except KeyboardInterrupt:
        pass

    print(f"\n{frames} bursts relayed")
    sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
