"""
crowcall wire protocol
======================

Burst size is NOT a constant. It comes from the modem mode chosen for each
message (see modes.py), because the right mode differs by payload:

    control  QPSK 1/2 short    108 B usable, 1.21 s   shortest burst there is
    voice    QPSK 5/6 short    193 B usable, 1.21 s   smallest chunks on the floor
    text     compact or bulk, by length
    file     8PSK 3/4 normal  1516 B usable, 3.68 s   fastest mode on the floor

All four sit on the measured 9.9 dB acquisition floor, so they are equally
robust and differ only in throughput and granularity.

HOW THE RECEIVER KNOWS WHAT TO DO
---------------------------------
It does not need to be told the modem mode: aicodix `decode` reads the mode
out of its own BCH-coded frame header, which is why it takes no mode
argument. By the time a burst reaches this layer it is already bytes.

Chunk payload size is then inferred from the first DAT body that arrives,
so a receiver configured differently from the sender still reassembles
correctly. The mode byte carries both choices anyway, for the archive:

    mode byte = (modem mode index << 4) | codec2 mode index

VOICE GEOMETRY
--------------
Frames per chunk come from the modem's usable bytes divided by the codec2
frame size. At QPSK 5/6 short with codec2-1400 that is 193 // 7 = 27 frames
= 1.08 s of speech per chunk, against 1.21 s of air (1.12x realtime).

Slower than speaking, and that is fine: this is store-and-forward, not
push-to-talk. What matters is that a lost chunk is a 1.08 s hole rather than
the 1.32 s the old marginal default gave.

Every header field is repeated in every chunk. There is deliberately no
separate "message header" packet: losing one would cost the whole message,
and on a link that fails by going completely dark rather than degrading,
single points of failure are the thing to avoid.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
import time
import zlib
from math import gcd

import modes as M

VERSION = 3                                  # addressed, checksummed frames
MAGIC = b"CC"

T_DAT = 1       # a chunk of voice, text or file
T_RREQ = 2      # "I am missing these chunks"
T_RACK = 3      # "sending them now"
T_RNAK = 4      # "no"
T_DONE = 5      # "I have the whole thing"
T_BEACON = 6    # "I exist, here is my callsign"

TYPE_NAMES = {T_DAT: "DAT", T_RREQ: "RREQ", T_RACK: "RACK",
              T_RNAK: "RNAK", T_DONE: "DONE", T_BEACON: "BEACON"}

# Payload kind rides in spare bits of the flags byte, so text and files cost
# exactly the same header overhead as voice.
#   bit 0    this burst is a repair
#   bits 1-3 payload kind
KIND_VOICE, KIND_TEXT, KIND_FILE = 0, 1, 2
KIND_NAMES = {KIND_VOICE: "voice", KIND_TEXT: "text", KIND_FILE: "file"}
KIND_ROLE = {KIND_VOICE: "voice", KIND_TEXT: "text", KIND_FILE: "file"}

F_REPAIR = 0x01
KIND_SHIFT, KIND_MASK = 1, 0x07
HOP_SHIFT, HOP_MASK, MAX_HOPS = 4, 0x03, 3

_CHANNEL_KEY: bytes | None = None


def set_channel_key(passphrase: str | None) -> None:
    """Configure an optional pre-shared channel key (None or '' for cleartext)."""
    global _CHANNEL_KEY
    if not passphrase:
        _CHANNEL_KEY = None
        return
    _CHANNEL_KEY = hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), b"crowcall-v3-psk", 10000, dklen=32
    )


def _xor_keystream(key: bytes, header_without_crc: bytes, payload: bytes) -> bytes:
    """Deterministic counter-mode keystream using BLAKE2s keyed with (key, header)."""
    if not payload:
        return b""
    out = bytearray(len(payload))
    nonce_base = hashlib.blake2s(header_without_crc, key=key, digest_size=24).digest()
    for blk_idx, offset in enumerate(range(0, len(payload), 32)):
        block = hashlib.blake2s(
            nonce_base + blk_idx.to_bytes(4, "big"), key=key, digest_size=32
        ).digest()
        chunk = payload[offset:offset + 32]
        for i, b in enumerate(chunk):
            out[offset + i] = b ^ block[i]
    return bytes(out)


def _frame_check(head_zero_crc: bytes, wire_body: bytes) -> int:
    """32-bit CRC32 when unkeyed, or 32-bit truncated HMAC-SHA256 when keyed."""
    if _CHANNEL_KEY is None:
        return zlib.crc32(head_zero_crc + wire_body) & 0xFFFFFFFF
    tag = hmac.digest(_CHANNEL_KEY, head_zero_crc + wire_body, "sha256")[:4]
    return int.from_bytes(tag, "big")

# defined in modes.py so `usable` and `pack` can never disagree
HDR_FMT = M.HDR_FMT
HDR_LEN = M.HDR_LEN                          # 52
MAX_BODY = 65535

DEFAULT_MODE = "1400"
MODES = ["700C", "1200", "1400", "1600", "2400", "3200"]
MODES_IDX = {m: i for i, m in enumerate(MODES)}
IDX_MODES = {i: m for i, m in enumerate(MODES)}

# implied by the byte rates preflight measured on real hardware
FRAME_BYTES = {"700C": 4, "1200": 6, "1400": 7, "1600": 8, "2400": 6, "3200": 8}
FRAME_MS = {"700C": 40, "1200": 40, "1400": 40, "1600": 40, "2400": 20, "3200": 20}
NOMINAL_BPS = {"700C": 700, "1200": 1200, "1400": 1400,
               "1600": 1600, "2400": 2400, "3200": 3200}

PTT_GAP_S = M.PTT_GAP_S

# codec.py keeps its own list of codec2 modes for validating encoder calls.
# The two must agree or chunk geometry and encoder arguments drift apart, so
# say so at import rather than producing quietly wrong frame counts.
try:
    import codec as _codec
    if set(_codec.MODES) != set(MODES):
        raise RuntimeError(
            f"codec2 mode tables disagree: codec.py has {sorted(_codec.MODES)}, "
            f"protocol.py has {sorted(MODES)}")
    for _m in MODES:
        if _codec.BYTES_PER_SEC[_m] != round(FRAME_BYTES[_m] / (FRAME_MS[_m] / 1000)):
            raise RuntimeError(
                f"codec2 {_m}: codec.py says {_codec.BYTES_PER_SEC[_m]} B/s, "
                f"protocol.py frame table implies "
                f"{round(FRAME_BYTES[_m] / (FRAME_MS[_m] / 1000))} B/s")
except ImportError:                              # codec is optional for tests
    pass


def pack_flags(repair: bool = False, kind: int = KIND_VOICE, hops: int = 0) -> int:
    return ((F_REPAIR if repair else 0)
            | ((kind & KIND_MASK) << KIND_SHIFT)
            | ((min(MAX_HOPS, max(0, int(hops))) & HOP_MASK) << HOP_SHIFT))


def is_repair(flags: int) -> bool:
    return bool(flags & F_REPAIR)


def kind_of(flags: int) -> int:
    return (flags >> KIND_SHIFT) & KIND_MASK


def hops_of(flags: int) -> int:
    return (flags >> HOP_SHIFT) & HOP_MASK


def pack_mode(c2mode: str, modem_mode) -> int:
    return ((M.INDEX_OF.get(getattr(modem_mode, "name", "compact"), 1) & 0x0F) << 4) \
        | (MODES_IDX.get(c2mode, 2) & 0x0F)


def unpack_mode(b: int):
    c2 = IDX_MODES.get(b & 0x0F)
    mm = M.BY_INDEX.get((b >> 4) & 0x0F)
    if c2 is None or mm is None:
        return None, None
    return c2, mm


# --------------------------------------------------------------------------

class Geometry:
    """How a codec2 mode divides into one modem burst."""

    __slots__ = ("mode", "modem", "frame_bytes", "frame_s", "frames",
                 "payload", "speech_s", "slack", "bps", "wire_bps", "bytes_s")

    def __init__(self, c2mode: str, modem_mode=None):
        if c2mode not in FRAME_BYTES:
            raise ValueError(f"unknown codec2 mode {c2mode!r}")
        self.mode = c2mode
        self.modem = modem_mode or M.COMPACT
        self.frame_bytes = FRAME_BYTES[c2mode]
        self.frame_s = FRAME_MS[c2mode] / 1000.0
        self.frames = max(1, self.modem.usable // self.frame_bytes)
        self.payload = self.frames * self.frame_bytes
        self.speech_s = self.frames * self.frame_s
        self.slack = self.modem.usable - self.payload
        self.bps = NOMINAL_BPS[c2mode]
        self.bytes_s = self.frame_bytes / self.frame_s
        self.wire_bps = int(round(self.bytes_s * 8))

    @property
    def air_s(self) -> float:
        return self.modem.air_s

    @property
    def realtime_ratio(self) -> float:
        return self.air_s / self.speech_s if self.speech_s else 0.0

    @property
    def padding_overhead(self) -> float:
        return (self.wire_bps - self.bps) / self.wire_bps if self.wire_bps else 0

    def describe(self) -> dict:
        return {"mode": self.mode, "modem": self.modem.name,
                "aicodix": f"{self.modem.modulation} {self.modem.coderate} "
                           f"{self.modem.framesize}",
                "bps": self.bps, "wire_bps": self.wire_bps,
                "bytes_s": round(self.bytes_s, 1),
                "frames": self.frames, "frame_bytes": self.frame_bytes,
                "payload": self.payload, "usable": self.modem.usable,
                "speech_s": round(self.speech_s, 3),
                "air_s": round(self.air_s, 3),
                "realtime": round(self.realtime_ratio, 2),
                "overhead_pct": round(100 * self.padding_overhead, 1)}

    def __repr__(self):
        return (f"<Geometry {self.mode} on {self.modem.name}: {self.frames} "
                f"frames, {self.speech_s:.2f}s, {self.realtime_ratio:.2f}x>")


_GEOM_CACHE: dict = {}


def geom_for(c2mode=None, modem_mode=None) -> Geometry:
    """Accepts a codec2 mode name or index, and a ModemMode or its name."""
    if isinstance(c2mode, bool) or c2mode is None:
        c2mode = DEFAULT_MODE
    elif isinstance(c2mode, int):
        c2mode = IDX_MODES.get(c2mode, DEFAULT_MODE)
    elif c2mode not in FRAME_BYTES:
        c2mode = DEFAULT_MODE
    if modem_mode is None:
        modem_mode = M.COMPACT
    elif isinstance(modem_mode, str):
        modem_mode = M.BY_NAME.get(modem_mode, M.COMPACT)
    elif isinstance(modem_mode, int):
        modem_mode = M.BY_INDEX.get(modem_mode, M.COMPACT)
    key = (c2mode, modem_mode.name)
    if key not in _GEOM_CACHE:
        _GEOM_CACHE[key] = Geometry(c2mode, modem_mode)
    return _GEOM_CACHE[key]


GEOM = {m: geom_for(m, M.COMPACT) for m in MODES}


def station_id(callsign: str) -> int:
    return zlib.crc32(callsign.strip().upper().encode()) & 0xFFFF


def _call8(callsign: str) -> bytes:
    return callsign.strip().upper()[:8].ljust(8).encode("ascii", "replace")


def pack(ptype: int, callsign: str, msg_id: int, body: bytes = b"",
         chunk_idx: int = 0, total: int = 0, mode: int = 0,
         flags: int = 0, modem_mode=None, pad: bool = True,
         destination: int = 0, origin: int | None = None,
         exchange_id: int = 0, source: int | None = None) -> bytes:
    """Build one burst, padded to the modem mode's payload so every packet on
    the medium is the size the modem expects."""
    mm = modem_mode or M.COMPACT
    if len(body) > mm.usable or len(body) > MAX_BODY:
        raise ValueError(f"body {len(body)} > {mm.usable} for {mm.name}")
    if not 0 <= ptype <= 255 or not 0 <= chunk_idx <= 0xFFFF or not 0 <= total <= 0xFFFF:
        raise ValueError("packet field out of range")
    if msg_id == 0 and ptype != T_BEACON:
        raise ValueError("message id must be non-zero")
    src = station_id(callsign) if source is None else int(source)
    org = src if origin is None else int(origin)
    if src == 0 or org == 0:
        raise ValueError("node identity must be non-zero")
    if not (0 <= src <= 0xFFFFFFFFFFFFFFFF and 0 <= org <= 0xFFFFFFFFFFFFFFFF):
        raise ValueError("identity out of range")
    if destination is None:
        destination = 0
    if not (0 <= int(destination) <= 0xFFFFFFFFFFFFFFFF and
            0 <= int(exchange_id) <= 0xFFFF):
        raise ValueError("identity or exchange out of range")
    # CRC occupies the last four bytes of the header.  Calculate it over the
    # header with a zero CRC plus the unpadded body.
    head0 = struct.pack(HDR_FMT, MAGIC, VERSION, ptype, flags & 0xFF, HDR_LEN,
                        src & 0xFFFFFFFFFFFFFFFF, org & 0xFFFFFFFFFFFFFFFF,
                        int(destination) & 0xFFFFFFFFFFFFFFFF,
                        msg_id & 0xFFFFFFFFFFFFFFFF, int(exchange_id) & 0xFFFF,
                        chunk_idx, total, len(body),
                        (mode >> 4) & 0xFF, mode & 0xFF, 0,
                        _call8(callsign))
    wire_body = (_xor_keystream(_CHANNEL_KEY, head0[:48] + head0[52:], body)
                 if _CHANNEL_KEY is not None else body)
    crc = _frame_check(head0, wire_body)
    head = head0[:48] + struct.pack(">I", crc) + head0[52:]
    out = head + wire_body
    return out.ljust(mm.payload, b"\x00") if pad else out


def unpack(data: bytes):
    """Parse a burst. Returns None for anything not ours -- on a shared
    medium you will hear things that are not for you."""
    if len(data) < HDR_LEN:
        return None
    magic, ver, ptype, flags, hlen, source, origin, destination, msg_id, exchange, idx, total, body_len, profile, codec_id, crc, call = \
        struct.unpack(HDR_FMT, data[:HDR_LEN])
    if magic != MAGIC or ver != VERSION or hlen != HDR_LEN or ptype not in TYPE_NAMES:
        return None
    if flags & 0xC0 or kind_of(flags) not in KIND_NAMES:
        return None
    if source == 0 or origin == 0 or msg_id == 0 and ptype != T_BEACON:
        return None
    if body_len > len(data) - HDR_LEN:
        return None
    if ptype == T_BEACON and body_len != 0:
        return None
    if ptype in (T_DAT, T_RREQ) and total == 0:
        return None
    if total and idx >= total:
        return None
    c2, mm = unpack_mode((profile << 4) | codec_id)
    if c2 is None or mm is None:
        return None
    if len(data) > mm.payload:
        return None
    if station_id(call.decode("ascii", "replace").strip()) != source:
        return None
    wire_body = data[HDR_LEN:HDR_LEN + body_len]
    head0 = data[:48] + b"\x00\x00\x00\x00" + data[52:HDR_LEN]
    check = _frame_check(head0, wire_body)
    if not hmac.compare_digest(check.to_bytes(4, "big"), crc.to_bytes(4, "big")):
        return None
    body = (_xor_keystream(_CHANNEL_KEY, data[:48] + data[52:HDR_LEN], wire_body)
            if _CHANNEL_KEY is not None else wire_body)
    if ptype == T_BEACON:
        if msg_id or total or idx:
            return None
    elif ptype == T_DAT and total == 0:
        return None
    return {"type": ptype, "type_name": TYPE_NAMES[ptype], "station": source,
            "callsign": call.decode("ascii", "replace").strip(), "source": source, "origin": origin,
            "destination": destination, "exchange": exchange,
            "msg_id": msg_id, "chunk_idx": idx, "total": total,
            "mode": (profile << 4) | codec_id, "mode_name": c2, "modem": mm,
            "modem_name": mm.name,
            "flags": flags, "repair": is_repair(flags),
            "kind": kind_of(flags),
            "hops": hops_of(flags),
            "kind_name": KIND_NAMES.get(kind_of(flags), "?"),
            "body": body}


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------

def chunkify(c2: bytes, c2mode=DEFAULT_MODE, modem_mode=None) -> list[bytes]:
    """Split codec2 bytes into whole-frame chunks. The final chunk is topped
    up with repeats of its own last frame rather than zeros: zeros decode to
    a click, a repeated frame decodes to a held sound."""
    g = geom_for(c2mode, modem_mode)
    size, fb = g.payload, g.frame_bytes
    chunks = [c2[i:i + size] for i in range(0, len(c2), size)] or [b""]
    tail = chunks[-1]
    if len(tail) < size:
        whole = max(fb, -(-len(tail) // fb) * fb)
        padded = tail.ljust(whole, b"\x00")
        last = padded[whole - fb:whole]
        need = max(0, (size - whole) // fb)
        chunks[-1] = (padded + last * need).ljust(size, b"\x00")
    return chunks


def chunkify_data(stream: bytes, modem_mode=None) -> list[bytes]:
    """Data has no frame alignment to respect, so every usable byte counts."""
    mm = modem_mode or M.FAST
    size = mm.usable
    if not stream:
        return [b"\x00" * size]
    return [stream[i:i + size].ljust(size, b"\x00")
            for i in range(0, len(stream), size)]


def interleave_order(n: int) -> list[int]:
    """Spread neighbours apart so a fade punches scattered holes rather than
    one contiguous crater. Uses a stride coprime with n."""
    if n <= 2:
        return list(range(n))
    stride = max(2, int(n ** 0.5))
    while gcd(stride, n) != 1:
        stride += 1
        if stride >= n:
            return list(range(n))
    return [(i * stride) % n for i in range(n)]


def data_order(n: int) -> list[int]:
    """Chunk 0 first and again last: it carries the stream prefix, without
    which the receiver cannot tell the length, checksum or filename."""
    if n <= 1:
        return [0]
    return [0] + [i for i in interleave_order(n) if i != 0] + [0]


def air_seconds(n_chunks: int, modem_mode=None) -> float:
    return n_chunks * (modem_mode or M.COMPACT).air_s


def _hms(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def plan(nbytes: int, kind: str = "file", c2mode=DEFAULT_MODE,
         voice_modem=None) -> dict:
    """What sending this costs, and which mode it will use."""
    if kind == "voice":
        mm = M.for_voice(voice_modem)
        g = geom_for(c2mode, mm)
        n = max(1, -(-nbytes // g.payload))
        speech = nbytes * g.frame_s / g.frame_bytes
        air = n * g.air_s
        return {"mode": mm.name, "codec2": c2mode, "chunks": n,
                "bursts": n, "speech_s": round(speech, 2),
                "air_s": round(air, 2), "air_human": _hms(air),
                "chunk_speech_s": round(g.speech_s, 3),
                "realtime": round(air / speech, 2) if speech else 0}
    p = M.plan(kind, nbytes)
    return {**p, "air_human": _hms(p["air_s"])}


def voice_options(c2mode=DEFAULT_MODE) -> list[dict]:
    """What each voice mode actually costs, for the operator to choose from.

    `hole_s` is the thing to look at: it is how much speech vanishes when one
    chunk fails to arrive and repair cannot recover it.
    """
    out = []
    for mm in M.voice_modes():
        g = geom_for(c2mode, mm)
        label, why = M.VOICE_LABELS.get(mm.name, (mm.name, ""))
        out.append({"modem": mm.name, "label": label, "why": why,
                    "aicodix": f"{mm.modulation} {mm.coderate} {mm.framesize}",
                    "frames": g.frames,
                    "hole_s": round(g.speech_s, 2),
                    "burst_s": round(mm.air_s, 2),
                    "realtime": round(g.realtime_ratio, 2),
                    "usable": mm.usable,
                    "default": mm.name == M.DEFAULT_VOICE})
    return out


# --------------------------------------------------------------------------
# non-voice payloads
# --------------------------------------------------------------------------

STREAM_MAGIC = b"CC"
STREAM_FMT = ">2sBBIIIH"
STREAM_LEN = struct.calcsize(STREAM_FMT)         # 18
COMP_NONE, COMP_ZLIB = 0, 1
COMP_NAMES = {COMP_NONE: "none", COMP_ZLIB: "zlib"}


def wrap_payload(data: bytes, kind: int = KIND_TEXT, name: str = "",
                 compress: bool = True) -> tuple[bytes, dict]:
    """Data cannot tolerate padding the way voice can, so the stream carries
    its own exact lengths, a checksum and a filename.

    Compression is attempted and kept only if it helped: already-compressed
    input grows slightly under zlib, and a wasted burst costs seconds."""
    raw_name = os.path.basename(name or "").encode("utf-8", "replace")
    nb = raw_name[:200]
    while True:
        try:
            nb.decode("utf-8")
            break
        except UnicodeDecodeError:
            nb = nb[:-1]
    comp, carried = COMP_NONE, data
    if compress and len(data) > 48:
        z = zlib.compress(data, 9)
        if len(z) < len(data):
            comp, carried = COMP_ZLIB, z
    head = struct.pack(STREAM_FMT, STREAM_MAGIC, kind & 0xFF, comp,
                       len(carried), len(data),
                       zlib.crc32(data) & 0xFFFFFFFF, len(nb))
    stream = head + nb + carried
    return stream, {"kind": kind, "kind_name": KIND_NAMES.get(kind, "?"),
                    "name": nb.decode("utf-8", "replace"),
                    "original_bytes": len(data), "carried_bytes": len(carried),
                    "compression": COMP_NAMES[comp],
                    "ratio": round(len(carried) / len(data), 3) if data else 1.0,
                    "stream_bytes": len(stream)}


def unwrap_payload(stream: bytes, max_original: int = 16 * 1024 * 1024) -> dict | None:
    """None means the prefix never arrived -- it lives in chunk 0."""
    if len(stream) < STREAM_LEN or stream[:2] != STREAM_MAGIC:
        return None
    _, kind, comp, carried_len, orig_len, crc, namelen = \
        struct.unpack(STREAM_FMT, stream[:STREAM_LEN])
    at = STREAM_LEN + namelen
    if namelen > 200 or orig_len > max_original or carried_len > max_original * 2 or len(stream) < at:
        return None
    name = stream[STREAM_LEN:at].decode("utf-8", "replace")
    body = stream[at:at + carried_len]
    truncated = len(body) < carried_len
    data, err = body, None
    if comp == COMP_ZLIB:
        try:
            d = zlib.decompressobj()
            data = d.decompress(body, max_original + 1)
            if len(data) > max_original or not d.eof or d.unconsumed_tail:
                raise zlib.error("compressed payload exceeds declared limit")
            data += d.flush()
            if len(data) > max_original:
                raise zlib.error("decompressed payload exceeds limit")
        except zlib.error as exc:
            data = b""
            err = f"decompression failed: {exc}"
    elif comp != COMP_NONE:
        data = b""
        err = f"unsupported compression {comp}"
    ok = (not truncated and err is None and len(data) == orig_len
          and (zlib.crc32(data) & 0xFFFFFFFF) == crc)
    return {"kind": kind, "kind_name": KIND_NAMES.get(kind, "?"),
            "name": name, "data": data, "intact": ok,
            "original_bytes": orig_len, "carried_bytes": carried_len,
            "compression": COMP_NAMES.get(comp, "?"),
            "truncated": truncated, "error": err,
            "recovered_bytes": len(data)}


# --------------------------------------------------------------------------
# repair requests
# --------------------------------------------------------------------------

RREQ_LIST, RREQ_BITMAP = 0, 1
RREQ_V3_MAGIC = b"RQ3"


def encode_rreq(target_station: int, missing: list[int], total: int,
                room: int) -> bytes:
    """Address a repair request while retaining a compact v2-compatible
    selection encoding.  The target is the origin station ID, not the
    requester, so unrelated listeners stay silent."""
    # 5 bytes are consumed by the RQ3 marker and target ID; reserve them
    # before asking the legacy selector to account for its encoding byte.
    selection = encode_missing(missing, total, max(1, room - 5))
    return RREQ_V3_MAGIC + int(target_station & 0xFFFF).to_bytes(2, "big") + selection


def decode_rreq(body: bytes, total: int) -> tuple[int | None, list[int]]:
    if body.startswith(RREQ_V3_MAGIC) and len(body) >= 5:
        target = int.from_bytes(body[3:5], "big")
        return target, decode_missing(body[5:], total)
    return None, decode_missing(body, total)


def encode_missing(missing: list[int], total: int, room: int) -> bytes:
    """Pack a set of missing chunk indices into one control burst.

    Two encodings, one leading byte to say which:
      list    2 bytes per index -- cheapest when little is missing
      bitmap  ceil(total/8) bytes -- fixed size, wins under heavy loss

    Control payload is 108 B, so a bitmap covers 856 chunks: a 1.3 MB file at
    the file mode. Beyond that the request is capped and the receiver simply
    asks again next round for what is still outstanding.
    """
    room = max(1, room - 1)
    as_list = len(missing) * 2
    as_bitmap = -(-total // 8)
    if as_bitmap <= room and as_bitmap < as_list:
        bm = bytearray(as_bitmap)
        for i in missing:
            if 0 <= i < total:
                bm[i >> 3] |= 1 << (i & 7)
        return bytes([RREQ_BITMAP]) + bytes(bm)
    return bytes([RREQ_LIST]) + b"".join(
        i.to_bytes(2, "big") for i in missing[:room // 2])


def decode_missing(body: bytes, total: int) -> list[int]:
    if not body:
        return []
    kind, rest = body[0], body[1:]
    if kind == RREQ_BITMAP:
        out = []
        for i in range(min(total, len(rest) * 8)):
            if rest[i >> 3] & (1 << (i & 7)):
                out.append(i)
        return out
    return [int.from_bytes(rest[i:i + 2], "big")
            for i in range(0, len(rest) - 1, 2)]


# --------------------------------------------------------------------------
# reassembly
# --------------------------------------------------------------------------

class Assembler:
    """Collects chunks for one inbound message.

    Chunk payload size is learned from the first body that arrives rather
    than assumed, so a receiver configured differently from the sender still
    reassembles correctly.
    """

    def __init__(self, msg_id: int, callsign: str, total: int, mode=None,
                 kind: int = KIND_VOICE, modem_mode=None):
        self.msg_id = msg_id
        self.callsign = callsign
        self.total = max(1, total)
        self.kind = kind
        self.geom = geom_for(mode, modem_mode)
        self.mode = self.geom.mode
        self.modem = self.geom.modem
        self.chunk_bytes = None                  # learned on first arrival
        self.chunks: dict[int, bytes] = {}
        self.repaired: set[int] = set()
        self.started = time.time()
        self.last_rx = time.time()
        self.repair_rounds = 0
        self.finalised = False
        self.declined = False
        # when the sender should have finished, from total x burst length
        self.expect_until = self.started + self.expected_duration()

    def add(self, idx: int, body: bytes, is_repair: bool = False) -> bool:
        if idx < 0 or idx >= self.total or idx in self.chunks:
            return False
        if not body:
            return False
        if self.kind != KIND_VOICE:
            expected = self.chunk_bytes or self.modem.usable
            if len(body) != expected:
                return False
            self.chunk_bytes = expected
        else:
            if self.chunk_bytes is None:
                self.chunk_bytes = self.geom.payload
        self.chunks[idx] = body[:self.chunk_bytes]
        self.last_rx = time.time()
        if is_repair:
            self.repaired.add(idx)
        return True

    def missing(self) -> list[int]:
        return [i for i in range(self.total) if i not in self.chunks]

    def complete(self) -> bool:
        return len(self.chunks) >= self.total

    def quiet_for(self) -> float:
        return time.time() - self.last_rx

    # -- patience, scaled to THIS message's burst length -------------------
    #
    # These were fixed constants (3 / 15 / 28 s) tuned when every burst was
    # 1.21 s. Once file transfers moved to a 3.68 s burst, the ordinary gap
    # between two consecutive bursts of a perfectly healthy transmission
    # exceeded the 3 s quiet timeout -- so the receiver decided the sender had
    # finished, fired a repair request, and did it again between every burst.
    # Three rounds of that and a 68-chunk file finalised with 6 chunks.
    #
    # Anything measured in "how long since I heard something" must therefore
    # be expressed in burst times, never in seconds.

    @property
    def quiet_timeout(self) -> float:
        """Silence before we conclude the sender has finished. 3x the burst
        interval, so an ordinary gap never looks like the end of a message."""
        return max(3.0, 3.0 * self.modem.air_s)

    @property
    def final_timeout(self) -> float:
        """Hard stop: play or write what arrived. Must clear a full repair
        exchange or a repair in flight gets cut off."""
        return self.quiet_timeout * 2.0 + 10.0

    @property
    def max_rounds(self) -> int:
        """A big file has more chunks to lose and each round can only fix so
        many, so long transfers get more attempts. Voice stays at 3 -- past
        that a message with holes is better than a message that never plays."""
        if self.kind == KIND_VOICE:
            return 3
        return min(8, 3 + self.total // 50)

    def expected_duration(self) -> float:
        """How long the sender needs to send everything, from the header's own
        chunk count. Data transfers with >1 chunk repeat chunk 0 as a trailer."""
        extra = 0 if (self.kind == KIND_VOICE or self.total <= 1) else 1
        return (self.total + extra) * self.modem.air_s

    def sender_should_be_done(self) -> bool:
        """The receiver never has to GUESS whether a transmission is still in
        progress. `total` is in every chunk header and the burst duration is
        known, so the finish time is arithmetic.

        Inferring it from silence was the bug: a gap between two healthy
        bursts is indistinguishable from the end of a message, so repair
        fired mid-transmission and the sender was interrupted servicing
        requests for chunks it had not sent yet.

        `expect_until` is pushed forward whenever we ask for a repair, since
        the sender then owes us that many more bursts.
        """
        return time.time() >= self.expect_until

    def note_repair_requested(self, n_chunks: int):
        """Give the sender time to actually send what we just asked for,
        plus a moment to decide -- an operator may have to click."""
        self.expect_until = time.time() + \
            n_chunks * self.modem.air_s + self.quiet_timeout

    @property
    def stall_timeout(self) -> float:
        """If the sender dies mid-transfer we cannot wait out the full
        expected duration -- that is 42 minutes for a 1 MB file. Prolonged
        silence overrides the arithmetic."""
        return self.quiet_timeout * 3.0

    def strip(self) -> list[str]:
        return ["lost" if i not in self.chunks
                else "repaired" if i in self.repaired
                else "ok" for i in range(self.total)]

    def assemble(self) -> bytes:
        """Voice. Missing chunks are concealed by repeating the last frame
        received -- a held sound rather than the click silence would give."""
        fb, frames = self.geom.frame_bytes, self.geom.frames
        out = bytearray()
        last = b"\x00" * fb
        for i in range(self.total):
            body = self.chunks.get(i)
            if body is not None and len(body) >= fb:
                out += body
                last = body[-fb:]
            else:
                out += last * frames
        return bytes(out)

    def assemble_data(self) -> bytes:
        """Text or file. Missing chunks become zeros: there is no concealing
        a hole in a file, and pretending otherwise would hand back something
        that looks whole and is not."""
        size = self.chunk_bytes or self.modem.usable
        out = bytearray()
        for i in range(self.total):
            out += self.chunks.get(i, b"\x00" * size)
        return bytes(out)

    def bad_ranges(self) -> list[tuple[int, int]]:
        size = self.chunk_bytes or self.modem.usable
        return [(i * size, (i + 1) * size) for i in self.missing()]

    def loss_fraction(self) -> float:
        return len(self.missing()) / self.total if self.total else 0.0

    def lost_seconds(self) -> float:
        return len(self.missing()) * self.geom.speech_s

    def lost_bytes(self) -> int:
        return len(self.missing()) * (self.chunk_bytes or self.modem.usable)


class LinkHealthTracker:
    """Dual-EMA link health controller with Mode Inertia (tenure stickiness),
    Hysteresis Deadband expansion, Dwell-Time scaling, Slew-Rate Downshift Urgency,
    and High-Headroom Upshift Incentive overrides."""

    TIERS = ["control", "compact", "bulk"]  # Fine -> Balanced -> Quick
    BASE_DOWNSHIFT_DWELL_S = 15.0
    BASE_UPSHIFT_DWELL_S = 60.0
    IDLE_FALLBACK_S = 300.0                 # 5 min silence resets to safe baseline

    def __init__(self, slow_alpha: float = 0.15, fast_alpha: float = 0.50):
        self.slow_alpha = slow_alpha
        self.fast_alpha = fast_alpha
        self.delivery_ema = 1.0             # slow EMA (long-term baseline)
        self.fast_delivery_ema = 1.0        # fast EMA (short-term transient)
        self.snr_ema: float | None = None
        self.fast_snr_ema: float | None = None
        self.snr_dev_ema = 0.0              # SNR mean absolute deviation (fading volatility)
        self.tone_margin_ema: float | None = None
        self.samples = 0
        self.dwell_samples = 0              # observations in current operating mode (dwell count)
        self.tenure_streak = 0              # consecutive healthy observations building mode_inertia
        self.consecutive_drops = 0          # consecutive failed bursts
        self.last_switch_at = 0.0
        self.last_obs_at = 0.0
        self.unconfirmed_streak = 0         # consecutive TX messages with no DONE/RREQ

    @staticmethod
    def _ema(cur: float | None, val: float, alpha: float) -> float:
        return float(val) if cur is None else (1.0 - alpha) * cur + alpha * float(val)

    @property
    def mode_inertia(self) -> float:
        """0.0 to 1.0 'tenure stickiness' score (mode inertia) earned by a mode
        proving stable over time. Reaches 1.0 after 20 clean bursts in the current mode."""
        if self.consecutive_drops >= 2:
            return 0.0
        return min(1.0, self.tenure_streak / 20.0)

    @property
    def tenure_stickiness(self) -> float:
        """Alias for mode_inertia for telemetry/readability."""
        return self.mode_inertia

    def observe_burst(self, ok: bool, snr_db: float | None = None,
                      tone_ms: float | None = None):
        self.samples += 1
        self.dwell_samples += 1
        self.last_obs_at = time.time()
        val = 1.0 if ok else 0.0
        self.delivery_ema = self._ema(self.delivery_ema, val, self.slow_alpha)
        self.fast_delivery_ema = self._ema(self.fast_delivery_ema, val, self.fast_alpha)
        if ok:
            self.consecutive_drops = 0
            self.tenure_streak += 1
        else:
            self.consecutive_drops += 1
            self.tenure_streak = (self.tenure_streak // 2
                                  if self.consecutive_drops == 1 else 0)
        if snr_db is not None:
            s = float(snr_db)
            dev = abs(s - (self.snr_ema if self.snr_ema is not None else s))
            self.snr_ema = self._ema(self.snr_ema, s, self.slow_alpha)
            self.fast_snr_ema = self._ema(self.fast_snr_ema, s, self.fast_alpha)
            self.snr_dev_ema = self._ema(self.snr_dev_ema, dev, self.slow_alpha)
        if tone_ms is not None and tone_ms > 0:
            self.tone_margin_ema = self._ema(self.tone_margin_ema, float(tone_ms), self.slow_alpha)

    def observe_message(self, total: int, lost_initial: int):
        """Called on RX finalise OR when an inbound RREQ tells us how many chunks
        a peer missed from our TX message."""
        if total > 0:
            self.samples += 1
            self.dwell_samples += 1
            self.last_obs_at = time.time()
            ratio = max(0.0, min(1.0, (total - lost_initial) / float(total)))
            self.delivery_ema = self._ema(self.delivery_ema, ratio, self.slow_alpha)
            self.fast_delivery_ema = self._ema(self.fast_delivery_ema, ratio, self.fast_alpha)
            if ratio >= 0.85:
                self.tenure_streak += max(1, total // 4)
            elif ratio < 0.65:
                self.tenure_streak = 0

    def note_tx_confirmed(self):
        """Peer confirmed our transmission (via T_DONE or T_RREQ)."""
        self.unconfirmed_streak = 0

    def note_tx_unconfirmed(self):
        """An outbound message timed out with zero response from any peer."""
        self.unconfirmed_streak += 1
        self.tenure_streak = 0
        self.delivery_ema = self._ema(self.delivery_ema, 0.35, self.slow_alpha)
        self.fast_delivery_ema = self._ema(self.fast_delivery_ema, 0.20, self.fast_alpha)

    def _switching_incentives(self) -> tuple[bool, bool, float]:
        negative_slew = max(0.0, self.delivery_ema - self.fast_delivery_ema)
        downshift_urgency = (
            self.consecutive_drops >= 3
            or self.fast_delivery_ema < 0.50
            or negative_slew >= 0.28
            or (self.fast_snr_ema is not None and self.fast_snr_ema <= 10.3)
        )
        upshift_incentive = (
            self.fast_delivery_ema >= 0.97
            and self.delivery_ema >= 0.93
            and self.snr_dev_ema < 1.2
            and (self.snr_ema is not None and self.snr_ema >= 19.0)
        )
        snr_headroom_score = (
            min(1.0, max(0.0, ((self.snr_ema or 12.0) - 14.0) / 8.0))
            if self.snr_ema is not None else 0.0
        )
        crash_score = min(1.0, max(negative_slew / 0.30, self.consecutive_drops / 3.0))
        switching_incentive_score = round(max(crash_score, snr_headroom_score), 2)
        return downshift_urgency, upshift_incentive, switching_incentive_score

    def _raw_target(self, cur_tier: int, now: float) -> tuple[int, str, str, str, str]:
        if self.unconfirmed_streak >= 2:
            return (0, "control", "700C", "compact",
                    "Safe fallback: last 2 transmissions went unconfirmed — using Fine + 700C.")
        if self.last_obs_at and (now - self.last_obs_at > self.IDLE_FALLBACK_S):
            return (1, "compact", "1400", "bulk",
                    "Channel idle >5m — reset to safe Balanced (compact + 1400) baseline.")

        downshift_urgency, upshift_incentive, _ = self._switching_incentives()
        deliv = (min(self.delivery_ema, self.fast_delivery_ema)
                 if downshift_urgency else self.delivery_ema)
        snr = (min(self.snr_ema or 99.0, self.fast_snr_ema or 99.0)
               if (self.snr_ema is not None and downshift_urgency) else self.snr_ema)
        fading = self.snr_dev_ema >= 2.5

        effective_inertia = 0.0 if (downshift_urgency or upshift_incentive) else self.mode_inertia
        hysteresis_deadband_deliv = 0.06 * effective_inertia
        hysteresis_deadband_snr = 1.5 * effective_inertia
        upshift_barrier_deliv = 0.03 * effective_inertia
        upshift_barrier_snr = 1.0 * effective_inertia

        up_to_2 = (deliv >= min(0.98, 0.94 + upshift_barrier_deliv)
                   and (snr is None or snr >= 15.0 + upshift_barrier_snr) and not fading)
        stay_in_2 = (deliv >= (0.88 - hysteresis_deadband_deliv)
                     and (snr is None or snr >= (13.5 - hysteresis_deadband_snr)) and not fading)

        up_to_1 = (deliv >= min(0.90, 0.78 + upshift_barrier_deliv)
                   and (snr is None or snr >= 12.0 + upshift_barrier_snr))
        stay_in_1 = (deliv >= (0.70 - hysteresis_deadband_deliv)
                     and (snr is None or snr >= (11.0 - hysteresis_deadband_snr)))

        if (cur_tier == 2 and stay_in_2) or (cur_tier < 2 and (up_to_2 or upshift_incentive)):
            return (2, "bulk", "1400", "fast",
                    "Link is clean & steady — Quick voice (0.5x air time) and Fast 3.68s data bursts.")
        if (cur_tier >= 1 and stay_in_1) or (cur_tier == 0 and up_to_1):
            c2 = "1200" if deliv < (0.80 - hysteresis_deadband_deliv * 0.5) else "1400"
            return (1, "compact", c2, "bulk",
                    "Moderate loss/fading — Balanced 1.2s bursts reduce hole size and dodge fades.")
        return (0, "control", "700C", "compact",
                "Heavy loss or slew-rate crash near 9.5 dB cliff — Fine voice + Codec2 700C minimizes holes and air time.")

    def recommend(self, cur_voice_modem: str, cur_c2_mode: str) -> dict:
        now = time.time()
        cur_tier = self.TIERS.index(cur_voice_modem) if cur_voice_modem in self.TIERS else 1
        target_tier, rec_voice, rec_c2, rec_data_cap, why = self._raw_target(cur_tier, now)
        downshift_urgency, upshift_incentive, switching_incentive_score = self._switching_incentives()
        inertia = self.mode_inertia

        if target_tier > cur_tier + 1:
            target_tier = cur_tier + 1
            rec_voice = self.TIERS[target_tier]
            rec_c2 = "1400"
            rec_data_cap = "bulk"

        dwell_elapsed_s = now - self.last_switch_at if self.last_switch_at else 9999.0
        if target_tier < cur_tier:
            required_dwell_s = (0.0 if downshift_urgency
                                else self.BASE_DOWNSHIFT_DWELL_S * (1.0 + 0.5 * inertia))
            remaining_dwell_s = max(0.0, required_dwell_s - dwell_elapsed_s)
            ready = (remaining_dwell_s == 0.0 and (downshift_urgency or self.samples >= 2))
        elif target_tier > cur_tier:
            required_dwell_s = (self.BASE_UPSHIFT_DWELL_S * 0.5 if upshift_incentive
                                else self.BASE_UPSHIFT_DWELL_S * (1.0 + 1.5 * inertia))
            required_dwell_samples = 4 if upshift_incentive else int(5 + 6 * inertia)
            remaining_dwell_s = max(0.0, required_dwell_s - dwell_elapsed_s)
            ready = (remaining_dwell_s == 0.0 and self.dwell_samples >= required_dwell_samples)
        else:
            remaining_dwell_s = 0.0
            ready = (rec_c2 != cur_c2_mode and dwell_elapsed_s >= self.BASE_DOWNSHIFT_DWELL_S)

        grade = ("clean" if target_tier == 2 else "marginal" if target_tier == 1 else "rough")
        vox_warn = None
        if self.tone_margin_ema is not None and self.tone_margin_ema < 25.0:
            vox_warn = f"Low VOX tone margin ({self.tone_margin_ema:.0f} ms surviving) — calibrate VOX or raise tone length."

        needs_change = (rec_voice != cur_voice_modem or rec_c2 != cur_c2_mode)
        return {
            "grade": grade,
            "samples": self.samples,
            "delivery_pct": round(self.delivery_ema * 100.0, 1),
            "fast_delivery_pct": round(self.fast_delivery_ema * 100.0, 1),
            "snr_ema_db": round(self.snr_ema, 1) if self.snr_ema is not None else None,
            "snr_jitter_db": round(self.snr_dev_ema, 1),
            "mode_inertia_pct": round(inertia * 100.0, 0),
            "tenure_stickiness_pct": round(inertia * 100.0, 0),
            "switching_incentive_score": switching_incentive_score,
            "downshift_urgency": downshift_urgency,
            "upshift_incentive": upshift_incentive,
            "tone_margin_ms": round(self.tone_margin_ema, 0) if self.tone_margin_ema is not None else None,
            "unconfirmed_streak": self.unconfirmed_streak,
            "recommended": {
                "voice_modem": rec_voice,
                "voice_label": M.VOICE_LABELS.get(rec_voice, (rec_voice, ""))[0],
                "mode": rec_c2,
                "data_cap": rec_data_cap,
            },
            "needs_change": needs_change,
            "ready_to_apply": bool(needs_change and ready),
            "remaining_dwell_s": round(remaining_dwell_s, 0),
            "cooldown_s": round(remaining_dwell_s, 0),
            "why": why,
            "vox_warning": vox_warn,
        }

    def mark_applied(self):
        self.last_switch_at = time.time()
        self.dwell_samples = 0
        self.tenure_streak = 0
