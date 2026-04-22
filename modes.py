"""
crowcall modem modes
====================

Every number here was measured on real hardware by step5/step6, not taken
from documentation. Do not edit them by hand -- rerun the sweeps.

THE ACQUISITION FLOOR
---------------------
Of aicodix's 64 modes, 22 need exactly the same minimum signal: about
9.9 dB in-band SNR. Below it nothing decodes; at or above it they all decode
equally well. Within those 22, throughput spans 6.7x.

So there is a free tier, and the only sane policy is to take the fastest mode
in it that suits the job. Every mode below is on that floor.

Modes NOT on the floor, for reference -- these cost real signal quality:
    QAM16 1/2 short   91.7% at the floor  (our old default -- marginal!)
    QAM16 2/3 short   needs ~12 dB
    QAM64 1/2 short   needs ~13 dB
    QAM256 1/2 short  needs ~17 dB
    QAM4096 3/4+      failed even at 31 dB -- unusable

WHY THESE FOUR
--------------
All four are constant-envelope (PSK). Under the measured channel that gains
nothing -- QAM16 survived the harshest multipath setting completely intact,
and the real dividing line was constellation density at QAM64, not
amplitude-vs-phase. But the disorders tools model the CHANNEL and contain no
amplifier, so they cannot test what a real radio's ALC and non-linear PA do
to an amplitude-modulated signal. Constant envelope is insurance against the
thing we cannot measure, and here it costs nothing: the fastest mode on the
floor happens to be 8PSK anyway.

A practical bonus: every mode wants the same transmit level, so moving to
real hardware needs one ALC setting rather than four.

ROLES
-----
CONTROL   QPSK 1/2 short. Shortest burst that exists anywhere (1.09 s) and
          the most error correction available at that duration. Payload is
          irrelevant for control -- 108 B holds a loss map for an 864-chunk
          message, which is a 1.3 MB file at the file mode.

VOICE     QPSK 5/6 short. Same 1.09 s burst, more payload. With codec2-1400
          that is 27 frames = 1.08 s of speech per chunk, so a lost chunk is
          a 1.08 s hole -- smaller than the 1.32 s our old marginal default
          gave. Costs 1.12x realtime, which is fine for store-and-forward.

TEXT      QPSK 5/6 short under ~290 characters (one burst, 1.21 s), stepping
          up to 8PSK 3/4 short above that. A short message in a big burst is
          a postcard in a shipping container -- you wait for the container.

FILE      8PSK 3/4 normal. Fastest mode on the floor, full stop. 412 B/s
          against the old default's 194: 100 KB drops from 8m47s to 4m08s.
          Long frames amortise the fixed preamble and PTT gap over more
          payload, which is where the last 20% comes from.

NOT YET MEASURED
----------------
Fading was only ever tested at code rate 1/2. Rate 3/4 carries a quarter
less correction, and margin is exactly what failed for QAM64 at the harsh
setting. 8PSK should be fine on the density argument, but that is reasoning
rather than measurement.

Also note the multipath FACTOR parameter runs backwards: factor 1 is the
severe channel and 40 is nearly harmless. Any future sweep must go below 1.
"""

from __future__ import annotations

import struct

# fixed cost per burst, measured: PTT/settling gap between transmissions
PTT_GAP_S = 0.12

# Our protocol header, subtracted from every modem payload to give `usable`.
# Derived from the struct format rather than written out, because a
# hand-copied 20 here would silently mis-size every payload in the system if
# the header ever changed -- with no error, just wrong numbers everywhere.
# Version-3 header.  It carries the complete transfer identity, destination,
# repair exchange and an explicit body length/CRC.  Keep this definition here
# because modem capacities are derived from it.
HDR_FMT = ">2sBBBBQQQQHHHHBBI8s"
HDR_LEN = struct.calcsize(HDR_FMT)


class ModemMode:
    """One aicodix configuration, with its measured behaviour."""

    __slots__ = ("name", "modulation", "coderate", "framesize",
                 "payload", "signal_s", "floor_pct", "note")

    def __init__(self, name, modulation, coderate, framesize,
                 payload, signal_s, floor_pct=100.0, note=""):
        self.name = name
        self.modulation = modulation
        self.coderate = coderate
        self.framesize = framesize
        self.payload = payload              # bytes the modem carries
        self.signal_s = signal_s            # measured burst length
        self.floor_pct = floor_pct          # success at the 9.9 dB floor
        self.note = note

    @property
    def usable(self) -> int:
        """Bytes left for us after the protocol header."""
        return self.payload - HDR_LEN

    @property
    def air_s(self) -> float:
        return self.signal_s + PTT_GAP_S

    @property
    def bytes_per_s(self) -> float:
        return self.usable / self.air_s

    @property
    def on_floor(self) -> bool:
        return self.floor_pct >= 99.9

    @property
    def constant_envelope(self) -> bool:
        return self.modulation in ("BPSK", "QPSK", "8PSK")

    def args(self):
        """What aicodix encode wants."""
        return self.modulation, self.coderate, self.framesize

    def describe(self) -> dict:
        return {"name": self.name, "modulation": self.modulation,
                "coderate": self.coderate, "framesize": self.framesize,
                "payload": self.payload, "usable": self.usable,
                "signal_s": round(self.signal_s, 3),
                "air_s": round(self.air_s, 3),
                "bytes_per_s": round(self.bytes_per_s, 1),
                "on_floor": self.on_floor,
                "constant_envelope": self.constant_envelope}

    def __repr__(self):
        return (f"<{self.name}: {self.modulation} {self.coderate} "
                f"{self.framesize}, {self.usable}B in {self.air_s:.2f}s "
                f"= {self.bytes_per_s:.0f} B/s>")


# --------------------------------------------------------------------------
# The four. Index is carried on the wire, so ORDER MUST NOT CHANGE --
# appending is safe, reordering breaks compatibility with saved recordings.
# --------------------------------------------------------------------------

MODES = [
    ModemMode("control", "QPSK", "1/2", "short", 128, 1.09,
              note="shortest burst available, most correction at that length"),
    ModemMode("compact", "QPSK", "5/6", "short", 213, 1.09,
              note="same 1.09s burst, 85B more payload; voice and short text"),
    ModemMode("bulk", "8PSK", "3/4", "short", 768, 2.05,
              note="long text; 2115 chars of prose in one burst"),
    ModemMode("fast", "8PSK", "3/4", "normal", 1536, 3.56,
              note="fastest mode on the floor; files"),
]

BY_NAME = {m.name: m for m in MODES}
BY_INDEX = {i: m for i, m in enumerate(MODES)}
INDEX_OF = {m.name: i for i, m in enumerate(MODES)}

CONTROL = BY_NAME["control"]
COMPACT = BY_NAME["compact"]
BULK = BY_NAME["bulk"]
FAST = BY_NAME["fast"]

# Above this many bytes, a bigger burst finishes sooner despite its longer
# air time. Below it you are waiting on a mostly-empty container.
BULK_THRESHOLD = COMPACT.usable          # 193 B
FAST_THRESHOLD = BULK.usable             # 748 B


# --------------------------------------------------------------------------
# Voice is the one role the operator chooses, because it is the one with a
# real trade rather than a right answer.
#
# Everything else optimises for throughput and there is a single best answer.
# Voice trades HOLE SIZE against AIR TIME, and which matters more depends on
# the link and the operator, not on arithmetic:
#
#   a lost chunk is a hole you have to listen to, so small chunks are kinder
#   but each burst carries fixed overhead, so small chunks are slower overall
#
# All three are on the acquisition floor, so this is NOT a robustness choice.
# Picking "fine" does not make the link survive worse conditions -- it makes
# the damage smaller when conditions are already bad enough to lose chunks.
# --------------------------------------------------------------------------

VOICE_CHOICES = ["control", "compact", "bulk"]
DEFAULT_VOICE = "compact"

VOICE_LABELS = {
    "control": ("Fine",
                "smallest holes, slowest to send -- for a link already "
                "losing chunks"),
    "compact": ("Balanced",
                "the default: small holes, about as long as speaking"),
    "bulk":    ("Quick",
                "half the air time, but a lost chunk swallows a sentence"),
}


def voice_modes():
    return [BY_NAME[n] for n in VOICE_CHOICES]


def for_voice(name=None):
    if name in VOICE_CHOICES:
        return BY_NAME[name]
    return BY_NAME[DEFAULT_VOICE]


def for_role(kind: str, nbytes: int = 0, voice_mode=None):
    """Pick the mode for a payload. `kind` is voice / text / file / control.

    Voice honours `voice_mode` if given, because chunk size is a judgement
    call there rather than an optimisation -- see VOICE_CHOICES above.
    """
    if kind == "control":
        return CONTROL
    if kind == "voice":
        return for_voice(voice_mode)
    if nbytes <= BULK_THRESHOLD:
        return COMPACT
    if nbytes <= FAST_THRESHOLD:
        return BULK
    return FAST


def plan(kind: str, nbytes: int) -> dict:
    """Cost of sending nbytes, including which mode wins and why."""
    m = for_role(kind, nbytes)
    n = max(1, -(-nbytes // m.usable))
    # data transfers repeat chunk 0 as a trailer; it carries the stream prefix
    bursts = n + 1 if kind in ("text", "file") and n > 1 else n
    air = bursts * m.air_s
    return {"mode": m.name, "chunks": n, "bursts": bursts,
            "air_s": round(air, 2), "usable": m.usable,
            "bytes_per_s": round(m.bytes_per_s, 1)}


def summary() -> str:
    out = [f"{'role':9s} {'aicodix':22s} {'usable':>7s} {'air':>7s} {'B/s':>7s}"]
    for m in MODES:
        out.append(f"{m.name:9s} "
                   f"{m.modulation + ' ' + m.coderate + ' ' + m.framesize:22s} "
                   f"{m.usable:6d}B {m.air_s:6.2f}s {m.bytes_per_s:6.1f}")
    return "\n".join(out)
