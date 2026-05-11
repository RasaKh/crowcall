"""
crowcall audio pipeline
=======================

    browser (webm/opus)
      --ffmpeg-->  16 kHz mono s16le
      --silero-->  silence stripped
      --ffmpeg-->  8 kHz mono s16le
      --c2enc-->   codec2

    codec2 --c2dec--> 8 kHz PCM --+beeps--> wav for playback

The VAD stage is the single largest bandwidth win in the pipeline: dead air
costs exactly as much air time as speech, so removing it removes whole
chunks. Silero runs at 16 kHz, hence the detour up and back down.

It is optional and it never destroys a recording. If pysilero-vad is not
installed, or if it decides the whole clip is non-speech (a quiet mic, a
whisper, heavy background noise), the untrimmed audio is used instead. A VAD
that is wrong about your voice should cost you bandwidth, not your message.

Preflight on the target machine confirmed 700C at exactly 100 bytes/sec and
that c2enc round-trips all seven modes. pycodec2 accepts int 700 but rejects
"700C" -- different modes in libcodec2's history, only 700C survives -- so
the CLI is used because it is the verified path.

ffmpeg's libcodec2 is a fallback only, and note that its -mode option is NOT
optional: it silently defaults to 1300.
"""

from __future__ import annotations

import math
import os
import random
import shutil
import struct
import subprocess
import tempfile
import threading
from collections import deque
from typing import NamedTuple

try:
    from pysilero_vad import SileroVoiceActivityDetector
except Exception:                      # noqa: BLE001 - optional dependency
    SileroVoiceActivityDetector = None

SAMPLE_RATE = 8000                     # codec2 always works at 8 kHz
VAD_RATE = 16000                       # silero wants 16 kHz

MODES = ["700C", "1200", "1400", "1600", "2400", "3200"]
BYTES_PER_SEC = {"700C": 100, "1200": 150, "1400": 175,
                 "1600": 200, "2400": 300, "3200": 400}

EXTRA_DIRS = ("/usr/lib/codec2", "/usr/local/bin", "/opt/codec2/bin")


def _which(name):
    hit = shutil.which(name)
    if hit:
        return hit
    for d in EXTRA_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


FFMPEG = _which("ffmpeg")
C2ENC = _which("c2enc")
C2DEC = _which("c2dec")


class CodecError(RuntimeError):
    pass


def vad_available() -> bool:
    return SileroVoiceActivityDetector is not None


def available() -> dict:
    return {"ffmpeg": FFMPEG, "c2enc": C2ENC, "c2dec": C2DEC,
            "vad": vad_available(),
            "ok": bool(FFMPEG and ((C2ENC and C2DEC) or FFMPEG))}


def _run(cmd, timeout=60):
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise CodecError(
            f"{os.path.basename(cmd[0])} failed: "
            f"{p.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return p.stdout


def _tmp(suffix):
    fd, path = tempfile.mkstemp(prefix="crowcall-", suffix=suffix)
    os.close(fd)
    return path


def _cleanup(*paths):
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


# --------------------------------------------------------------------------
# resampling / capture
# --------------------------------------------------------------------------

def recording_to_pcm(blob: bytes, suffix=".webm", rate=SAMPLE_RATE) -> bytes:
    """Whatever MediaRecorder produced -> mono s16le at `rate`."""
    if not FFMPEG:
        raise CodecError("ffmpeg not found")
    src, dst = _tmp(suffix), _tmp(".raw")
    try:
        with open(src, "wb") as fh:
            fh.write(blob)
        # gentle levelling: laptop and phone mics differ wildly and codec2 is
        # happier with a consistent input level
        _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
              "-f", "s16le", "-ac", "1", "-ar", str(rate),
              "-af", "highpass=f=80,lowpass=f=3600,dynaudnorm=p=0.7", dst])
        with open(dst, "rb") as fh:
            return fh.read()
    finally:
        _cleanup(src, dst)


def resample_pcm(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    if from_rate == to_rate:
        return pcm
    src, dst = _tmp(".raw"), _tmp(".raw")
    try:
        with open(src, "wb") as fh:
            fh.write(pcm)
        _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
              "-f", "s16le", "-ar", str(from_rate), "-ac", "1", "-i", src,
              "-f", "s16le", "-ar", str(to_rate), "-ac", "1", dst])
        with open(dst, "rb") as fh:
            return fh.read()
    finally:
        _cleanup(src, dst)


# --------------------------------------------------------------------------
# voice activity detection
# --------------------------------------------------------------------------

_VAD = None
_VAD_LOCK = threading.Lock()


def _get_vad():
    """One cached detector. Constructing it per message reloaded the ONNX
    model every time, which dominated encode latency for no benefit."""
    global _VAD
    with _VAD_LOCK:
        if _VAD is None:
            _VAD = SileroVoiceActivityDetector()
        return _VAD


def apply_vad(pcm_16k: bytes, threshold: float = 0.6,
              head_pad_chunks: int = 8, tail_pad_chunks: int = 8) -> bytes:
    """Strip non-speech, keeping padding either side so word edges survive.

    head_pad keeps a rolling history so the attack of a word is not clipped;
    tail_pad is hangover so trailing consonants are not cut. 8 chunks is
    ~256 ms at 16 kHz.
    """
    if not vad_available():
        return pcm_16k
    vad = _get_vad()
    chunk_size = vad.chunk_bytes()          # 1024 B = 32 ms at 16 kHz

    clean = bytearray()
    ring = deque(maxlen=head_pad_chunks)
    triggered = False
    tail = 0

    for i in range(0, len(pcm_16k), chunk_size):
        chunk = pcm_16k[i:i + chunk_size]
        if len(chunk) != chunk_size:
            # keep the final partial chunk instead of dropping up to 32 ms
            if triggered:
                clean.extend(chunk)
            break

        speech = vad(chunk) >= threshold

        if not triggered:
            if speech:
                triggered = True
                clean.extend(b"".join(ring))    # flush the lookback
                ring.clear()
                clean.extend(chunk)
                tail = tail_pad_chunks
            else:
                ring.append(chunk)
        else:
            clean.extend(chunk)
            if speech:
                tail = tail_pad_chunks
            else:
                tail -= 1
                if tail <= 0:
                    triggered = False
                    ring.clear()

    return bytes(clean)


# --------------------------------------------------------------------------
# codec2
# --------------------------------------------------------------------------

def pcm_to_codec2(pcm: bytes, mode: str = "1400") -> bytes:
    if mode not in BYTES_PER_SEC:
        raise CodecError(f"unknown mode {mode}")
    src, dst = _tmp(".raw"), _tmp(".c2")
    try:
        with open(src, "wb") as fh:
            fh.write(pcm)
        if C2ENC:
            _run([C2ENC, mode, src, dst])
        elif FFMPEG:
            # -mode is NOT optional: ffmpeg's libcodec2 encoder silently
            # defaults to 1300, giving 175 B/s under a different frame size
            # and breaking every alignment assumption downstream.
            _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                  "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
                  "-i", src, "-c:a", "libcodec2", "-mode", mode,
                  "-f", "codec2raw", dst])
        else:
            raise CodecError("no codec2 encoder available")
        with open(dst, "rb") as fh:
            return fh.read()
    finally:
        _cleanup(src, dst)


def codec2_to_pcm(c2: bytes, mode: str = "1400") -> bytes:
    src, dst = _tmp(".c2"), _tmp(".raw")
    try:
        with open(src, "wb") as fh:
            fh.write(c2)
        if C2DEC:
            _run([C2DEC, mode, src, dst])
        elif FFMPEG:
            # codec2raw marks -mode mandatory: raw codec2 has no header, so
            # nothing can infer the frame size
            _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                  "-f", "codec2raw", "-mode", mode, "-i", src,
                  "-f", "s16le", dst])
        else:
            raise CodecError("no codec2 decoder available")
        with open(dst, "rb") as fh:
            return fh.read()
    finally:
        _cleanup(src, dst)


def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    """Minimal RIFF wrapper -- no subprocess, and browsers play it."""
    f = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    riff = (b"WAVE" + b"fmt " + struct.pack("<I", len(f)) + f
            + b"data" + struct.pack("<I", len(pcm)) + pcm)
    return b"RIFF" + struct.pack("<I", len(riff)) + riff


# --------------------------------------------------------------------------
# playback flavour -- generated locally, never transmitted
# --------------------------------------------------------------------------

def generate_beep(frequency: float, duration_ms: int = 150,
                  rate: int = SAMPLE_RATE, volume: float = 0.3) -> bytes:
    n = int(rate * duration_ms / 1000.0)
    amp = 32767 * volume
    # 5 ms raised-cosine edges, so the beep does not click
    edge = max(1, int(rate * 0.005))
    out = bytearray()
    for i in range(n):
        env = 1.0
        if i < edge:
            env = 0.5 - 0.5 * math.cos(math.pi * i / edge)
        elif i > n - edge:
            env = 0.5 - 0.5 * math.cos(math.pi * (n - i) / edge)
        out += struct.pack("<h", int(amp * env
                                     * math.sin(2 * math.pi * frequency * i / rate)))
    return bytes(out)


def add_radio_texture(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    """Analogue wobble and flutter. Applied to the beeps only."""
    n = len(pcm) // 2
    if n == 0:
        return pcm
    s = list(struct.unpack(f"<{n}h", pcm))
    wobble_speed, wobble_depth = 3.5, 5.0
    fade, counter = 1.0, 0
    out = bytearray()
    for i, cur in enumerate(s):
        off = int(wobble_depth * math.sin(2 * math.pi * wobble_speed * i / rate))
        j = i + off
        v = s[j] if 0 <= j < n else cur
        counter += 1
        if counter > int(rate * 0.1):
            counter = 0
            fade = random.uniform(0.4, 0.9) if random.random() < 0.15 \
                else min(1.0, fade + 0.1)
        out += struct.pack("<h", max(-32768, min(32767, int(v * fade))))
    return bytes(out)


ROGER_IN = 1200
ROGER_OUT = 800


def _bookend(speech_pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    gap = b"\x00\x00" * int(rate * 0.1)
    start = add_radio_texture(generate_beep(ROGER_IN, 150, rate), rate)
    end = add_radio_texture(generate_beep(ROGER_OUT, 250, rate), rate)
    return start + gap + speech_pcm + gap + end


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------

class Encoded(NamedTuple):
    """Everything the archive and the transmitter both need, computed once."""
    c2: bytes
    seconds: float
    pcm: bytes          # 8 kHz mono s16le, post-VAD -- the archivable form
    stats: dict


def encode_message(blob: bytes, suffix=".webm", mode="1400",
                   use_vad=True) -> Encoded:
    """Recording -> codec2 bytes, speech seconds, and a stats dict.

    Never raises on a VAD miss: if the detector strips everything, the
    untrimmed audio is encoded instead and `stats['vad']` says why.
    """
    raw_rate = VAD_RATE if (use_vad and vad_available()) else SAMPLE_RATE
    pcm = recording_to_pcm(blob, suffix, rate=raw_rate)
    if not pcm:
        raise CodecError("recording decoded to nothing -- try holding longer")

    original_s = len(pcm) / 2 / raw_rate
    stats = {"original_s": round(original_s, 2), "vad": "off"}

    if use_vad and vad_available():
        trimmed = apply_vad(pcm)
        kept_s = len(trimmed) / 2 / VAD_RATE
        # A VAD that is wrong about your voice should cost bandwidth, not
        # your message. Anything under 200 ms kept means it did not find
        # speech, so fall back rather than fail.
        if kept_s >= 0.2:
            pcm = trimmed
            stats["vad"] = "applied"
            stats["kept_s"] = round(kept_s, 2)
            stats["stripped_s"] = round(original_s - kept_s, 2)
        else:
            stats["vad"] = "no speech found, kept original"
        pcm = resample_pcm(pcm, VAD_RATE, SAMPLE_RATE)

    secs = len(pcm) / 2 / SAMPLE_RATE
    c2 = pcm_to_codec2(pcm, mode)
    stats["speech_s"] = round(secs, 2)
    stats["codec2_bytes"] = len(c2)
    stats["pcm_bytes"] = len(pcm)
    stats["mode"] = mode
    return Encoded(c2, secs, pcm, stats)


def decode_message(c2: bytes, mode="1400", beeps=True) -> bytes:
    """codec2 -> playable wav. The beeps are local flavour: they are
    generated here, never transmitted, and so cost no air time."""
    return decode_message_full(c2, mode, beeps)[0]


def decode_message_full(c2: bytes, mode="1400",
                        beeps=True) -> tuple[bytes, bytes]:
    """Returns (playable wav, bare 8 kHz PCM). The archive wants the PCM
    without the roger beeps, since those were never on the air."""
    speech = codec2_to_pcm(c2, mode)
    wav = pcm_to_wav(_bookend(speech) if beeps else speech)
    return wav, speech
