"""
crowcall modem -- aicodix wrapper
=================================

Turns a 256-byte burst into ~1.09 s of audio and back.

Every constant here was measured, not assumed:

  Mode comes from modes.py, per message -- there is no single right one.
      Of aicodix's 64 modes, 22 share the same ~9.9 dB acquisition floor and
      differ only in throughput and burst length, so the job picks the mode:
      control wants the shortest burst, voice wants small chunks, files want
      throughput. NOTHING here defaults to a mode of its own choosing.
  1.09 s of signal      -- the encoder pads its output with 1.00 s of lead
      and 0.99 s of tail silence; 55% of the file is nothing. We trim it.
  0 ms guard is safe    -- with 80 ms of margin either side before decode
      starts failing, so the framer has room to be slightly wrong.
  ~9.5 dB in-band SNR   -- below that nothing decodes at all. There is no
      graceful degradation region, which is why the receiver needs a burst
      detector rather than a soft-decision front end.

`decode` only ever pulls the FIRST datagram out of a continuous stream, so
the receiver must slice bursts apart itself. BurstDetector does that on a
live audio stream using an adaptive noise floor.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import threading

import modes as MODES

RATE = 48000
CARRIER = 1500
DEBUG_TONE_S = 0.12
DEBUG_TONE_VOLUME = 0.22
# There is deliberately no module-level modulation constant. The old code had
# MODULATION = "QAM16" as a constructor default, which meant any path that
# failed to supply a mode silently fell back to the ONE mode we measured as
# unreliable (91.7% at the floor). A fallback should be the most conservative
# option available, never the fastest and never an accident.
FALLBACK = MODES.CONTROL

# measured signal span per burst, and the safe trim margin around it
SIGNAL_S = 1.09
TRIM_MARGIN_S = 0.040
MARGIN_PROVEN_S = 0.080


class ModemError(RuntimeError):
    pass


# --------------------------------------------------------------------------

def _read_wav(path):
    import numpy as np
    with open(path, "rb") as fh:
        d = fh.read()
    if d[:4] != b"RIFF" or d[8:12] != b"WAVE":
        raise ModemError("not a RIFF/WAVE file")
    pos, fmt, ch, rate, bits, audio = 12, None, None, None, None, None
    while pos + 8 <= len(d):
        cid, clen = d[pos:pos + 4], struct.unpack("<I", d[pos + 4:pos + 8])[0]
        body = d[pos + 8:pos + 8 + clen]
        if cid == b"fmt ":
            fmt, ch, rate = struct.unpack("<HHI", body[:8])
            bits = struct.unpack("<H", body[14:16])[0]
        elif cid == b"data":
            audio = body
        pos += 8 + clen + (clen & 1)
    if audio is None:
        raise ModemError("no data chunk")
    if fmt == 1 and bits == 16:
        x = np.frombuffer(audio, dtype="<i2").astype("float32") / 32768.0
    elif fmt == 3 and bits == 32:
        x = np.frombuffer(audio, dtype="<f4").astype("float32")
    else:
        raise ModemError(f"unhandled wav fmt={fmt} bits={bits}")
    if ch and ch > 1:
        x = x.reshape(-1, ch)[:, 0]
    return x, rate


def _write_wav(path, samples, rate=RATE):
    import numpy as np
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    f = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    riff = (b"WAVE" + b"fmt " + struct.pack("<I", len(f)) + f
            + b"data" + struct.pack("<I", len(pcm)) + pcm)
    with open(path, "wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", len(riff)) + riff)


def _span(x, rate, win_ms=5.0, frac=0.02):
    """First and last window above `frac` of peak RMS."""
    import numpy as np
    w = max(1, int(rate * win_ms / 1000))
    n = len(x) - (len(x) % w)
    if n <= 0:
        return 0, len(x)
    rms = np.sqrt((x[:n].reshape(-1, w).astype("float64") ** 2).mean(axis=1))
    if rms.max() <= 0:
        return 0, len(x)
    hot = np.nonzero(rms > rms.max() * frac)[0]
    if not len(hot):
        return 0, len(x)
    return int(hot[0] * w), int(min(len(x), (hot[-1] + 1) * w))


def trim(x, rate, margin_s=TRIM_MARGIN_S):
    """Cut the encoder's ~2 s of padding, keeping a small guard."""
    a, b = _span(x, rate)
    m = int(rate * margin_s)
    return x[max(0, a - m):min(len(x), b + m)]


def debug_tone(hz: float, rate: int = RATE, duration_s: float = DEBUG_TONE_S,
               volume: float = DEBUG_TONE_VOLUME):
    """Generate a short, edge-shaped diagnostic tone before a modem burst."""
    import numpy as np
    hz = float(hz)
    if not 20.0 <= hz <= rate / 2.0 - 20.0:
        raise ValueError("debug tone must be between 20 Hz and Nyquist")
    n = max(1, int(round(rate * duration_s)))
    t = np.arange(n, dtype="float32") / rate
    edge = max(1, min(n // 2, int(rate * 0.008)))
    env = np.ones(n, dtype="float32")
    ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(edge) / edge)
    env[:edge] = ramp
    env[-edge:] = ramp[::-1]
    return (np.sin(2 * np.pi * hz * t) * float(volume) * env).astype("float32")


def prepend_debug_tone(samples, rate: int, hz: float | None):
    import numpy as np
    if hz is None:
        return np.asarray(samples, dtype="float32")
    return np.concatenate([debug_tone(hz, rate),
                           np.asarray(samples, dtype="float32")])


def strip_debug_tone(samples, rate: int, hz: float | None = None,
                     duration_s: float = DEBUG_TONE_S):
    """Remove a diagnostic prefix from a soundcard candidate.

    Soundcard framing includes a small pre-trigger pad and the frequency may
    differ between stations, so locate the strongest early sinusoid rather
    than dropping a fixed number of samples.  With ``hz`` supplied the search
    is narrowly correlated; otherwise a short FFT chooses the dominant tone.
    Returns ``(samples_without_tone, detected_hz)``.
    """
    import numpy as np
    x = np.asarray(samples, dtype="float32").reshape(-1)
    if len(x) < int(rate * 0.06):
        return x, None
    search_n = min(len(x), int(rate * 0.30))
    head = x[:search_n].astype("float64")
    win = max(64, int(rate * 0.02))
    if hz is not None:
        freqs = [float(hz)]
    else:
        nfft = 1 << max(10, (search_n - 1).bit_length())
        spec = np.abs(np.fft.rfft(head * np.hanning(search_n), n=nfft))
        lo, hi = int(20 * nfft / rate), int(min(rate / 2 - 20, 6000) * nfft / rate)
        if hi <= lo:
            return x, None
        freqs = [float(np.argmax(spec[lo:hi]) + lo) * rate / nfft]
    best, best_score, best_hz = 0, 0.0, None
    for f in freqs:
        t = np.arange(win, dtype="float64") / rate
        ref = np.exp(-2j * np.pi * f * t)
        limit = max(1, min(search_n - win, int(rate * 0.22)))
        for start in range(0, limit, max(1, win // 2)):
            seg = head[start:start + win]
            score = abs(np.dot(seg, ref)) / (np.linalg.norm(seg) * np.linalg.norm(ref) + 1e-12)
            # Pick the first convincing window, not the numerically strongest
            # one.  A pure tone produces many equally strong overlapping
            # windows; choosing the maximum can move the cut arbitrarily deep
            # into the prefix because of floating-point noise.
            if score >= 0.35 and best_hz is None:
                best, best_score, best_hz = start, score, f
                break
            if score > best_score:
                best, best_score, best_hz = start, score, f
    # A modem preamble is not a stable single sinusoid; require a meaningful
    # correlation before treating the candidate as tone-prefixed.
    if best_score < 0.35:
        return x, None
    if hz is None:
        # A normal modem preamble (or an accidental carrier) can correlate
        # strongly at one frequency.  A diagnostic prefix is different: its
        # stable sinusoid ends after DEBUG_TONE_S.  Reject candidates that stay
        # just as stable after the proposed cut.
        after = best + int(rate * duration_s)
        if after + win <= len(head):
            seg = head[after:after + win]
            post = abs(np.dot(seg, np.exp(-2j * np.pi * best_hz * np.arange(win) / rate))) / (
                np.linalg.norm(seg) * np.sqrt(win) + 1e-12)
            if post >= 0.55:
                return x, None
    cut = min(len(x), best + int(rate * duration_s))
    return x[cut:], best_hz


# --------------------------------------------------------------------------

class AicodixModem:
    """Thin wrapper over the aicodix encode/decode binaries."""

    def __init__(self, vendor=None, callsign="CROWCALL", carrier=CARRIER,
                 default_mode=None):
        vendor = vendor or os.path.expanduser("~/crowcall/vendor")
        self.encode_bin = os.path.join(vendor, "modem", "encode")
        self.decode_bin = os.path.join(vendor, "modem", "decode")
        self.dis = os.path.join(vendor, "disorders")
        self.callsign = callsign.upper()[:8]
        self.carrier = carrier
        # Only used when a caller supplies no mode at all, which is a bug --
        # every burst is built by something that already knows its mode. The
        # fallback is therefore the most robust mode available, and it is
        # counted (see fallbacks_used) rather than applied silently.
        self.default_mode = default_mode or FALLBACK
        self.fallbacks_used = 0
        self.rate = RATE          # replaced by probe_rate()
        self._lock = threading.Lock()
        for b in (self.encode_bin, self.decode_bin):
            if not (os.path.isfile(b) and os.access(b, os.X_OK)):
                raise ModemError(
                    f"{b} missing -- build it with:\n"
                    f"  cd {vendor}/modem && make CXX='g++ -march=native'")

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _tmp(suffix):
        fd, p = tempfile.mkstemp(prefix="crowcall-modem-", suffix=suffix)
        os.close(fd)
        return p

    @staticmethod
    def _rm(*paths):
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    @staticmethod
    def _run(cmd, timeout=60):
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.returncode, p.stderr.decode("utf-8", "replace")

    # -- transmit --------------------------------------------------------

    @property
    def payload_bytes(self) -> int:
        return self.default_mode.payload

    @property
    def modulation(self) -> str:
        return self.default_mode.modulation

    @property
    def coderate(self) -> str:
        return self.default_mode.coderate

    @property
    def framesize(self) -> str:
        return self.default_mode.framesize

    def modulate(self, burst: bytes, margin_s=TRIM_MARGIN_S, mode=None):
        """256 B -> trimmed float32 samples at 48 kHz.

        NOTE the argument order: aicodix `encode` is OUTPUT-first, and so is
        every disorders tool. Only `decode` is INPUT-first. That inversion
        cost a whole measurement run once; it is written down here so it
        cannot cost another.
        """
        if mode is None:
            # Counted rather than silent: every burst is built by a caller
            # that knows its mode, so reaching here means a call site was
            # missed. The fallback is the most robust mode, not the fastest.
            self.fallbacks_used += 1
            mode = self.default_mode
        mod, cr, fs = mode.args()
        return self.modulate_at(burst, getattr(self, "rate", RATE), margin_s,
                                mod, cr, fs)

    # -- receive ---------------------------------------------------------

    def demodulate(self, samples, rate=RATE):
        """Samples -> 256 B, or None if this burst did not decode.

        There is no partial success: the polar-coded payload either comes
        out intact or nothing comes out at all.
        """
        wav, out = self._tmp(".wav"), self._tmp(".bin")
        try:
            _write_wav(wav, samples, rate)
            with self._lock:                      # decode is not reentrant
                rc, err = self._run([self.decode_bin, wav, out])
            if not os.path.exists(out):
                return None
            with open(out, "rb") as fh:
                data = fh.read()
            return data if data else None
        except (ModemError, subprocess.TimeoutExpired):
            return None
        finally:
            self._rm(wav, out)

    # -- rate probing ----------------------------------------------------

    #: candidate sample rates, lowest first. 2400 Hz of signal centred on
    #: 1500 Hz reaches 2700 Hz, so Nyquist needs > 5400 Hz -- 8 kHz is
    #: legitimate and is exactly what an SSB radio's audio port would give.
    #: Lower is better here purely because of the UDP datagram ceiling.
    RATE_CANDIDATES = (8000, 12000, 16000, 24000, 48000)

    UDP_LIMIT = 65507

    def probe_rate(self, limit=UDP_LIMIT, verbose=False):
        """Lowest sample rate that round-trips AND fits one datagram.

        Returns (rate, bytes_per_burst) or raises. Not assumed: the aicodix
        README only ever demonstrates 48 kHz, and 48 kHz does not fit.
        """
        # probe on the control mode: shortest burst, so the fastest probe,
        # and the one every station must support
        probe_mode = FALLBACK
        probe = bytes((i * 37 + 11) & 0xFF for i in range(probe_mode.payload))
        results = []
        for rate in self.RATE_CANDIDATES:
            try:
                samples, got_rate = self.modulate_at(
                    probe, rate, TRIM_MARGIN_S, *probe_mode.args())
            except ModemError as exc:
                results.append((rate, None, f"encode rejected it: {exc}"))
                continue
            nbytes = len(samples) * 2 + 16
            back = self.demodulate(samples, got_rate)
            ok = back is not None and back[:probe_mode.payload] == probe
            results.append((rate, nbytes, "round-trips" if ok else "decode failed"))
            if verbose:
                print(f"  {rate:6d} Hz -> {nbytes:7d} B/burst  "
                      f"{results[-1][2]}"
                      f"{'' if nbytes <= limit else '  (too big for UDP)'}")
            if ok and nbytes <= limit:
                self.rate = rate
                return rate, nbytes
        detail = "; ".join(f"{r}Hz {m}" for r, _, m in results)
        raise ModemError(
            "no sample rate both round-trips and fits a UDP datagram. "
            f"Tried: {detail}. Fall back to fragmenting 48 kHz bursts.")

    def modulate_at(self, burst: bytes, rate: int, margin_s=TRIM_MARGIN_S,
                    mod=None, cr=None, fs=None):
        """modulate() at an explicit sample rate and, optionally, an explicit
        aicodix mode -- the transport picks one per message now."""
        # burst size now varies by mode; the caller has already padded it
        dat, wav = self._tmp(".dat"), self._tmp(".wav")
        try:
            with open(dat, "wb") as fh:
                fh.write(burst)
            rc, err = self._run([
                self.encode_bin, wav, str(rate), "16", "1", str(self.carrier),
                self.callsign, mod or self.modulation, cr or self.coderate,
                fs or self.framesize, dat,
            ])
            if rc != 0 or not os.path.exists(wav):
                raise ModemError(err.strip()[:160] or f"rc={rc}")
            x, got = _read_wav(wav)
            return trim(x, got, margin_s), got
        finally:
            self._rm(dat, wav)

    # -- offline channel, for testing without a radio ---------------------

    def impair(self, samples, rate=RATE, awgn=None, multipath=None,
               cfo=None, sfo=None):
        """Push samples through the aicodix disorders tools.

        `awgn` is the tool's own parameter, which calibration showed maps to
        in-band SNR as:  SNR_dB = 0.9 - awgn.  So awgn=-20 is a clean ~21 dB
        channel and awgn=-9 sits right on the cliff.
        """
        stages = [("multipath", multipath), ("cfo", cfo), ("sfo", sfo),
                  ("awgn", awgn)]
        cur = self._tmp(".wav")
        made = [cur]
        try:
            _write_wav(cur, samples, rate)
            for tool, arg in stages:
                if arg is None:
                    continue
                exe = os.path.join(self.dis, tool)
                if not os.path.isfile(exe):
                    continue
                nxt = self._tmp(".wav")
                made.append(nxt)
                args = [exe, nxt, cur]            # OUTPUT first
                if tool == "multipath":
                    args.append(os.path.join(self.dis, "multipath.txt"))
                args.append(str(arg))
                rc, err = self._run(args)
                if rc != 0 or not os.path.exists(nxt):
                    raise ModemError(f"{tool} failed: {err.strip()[:150]}")
                cur = nxt
            x, r = _read_wav(cur)
            return x, r
        finally:
            self._rm(*made)


# --------------------------------------------------------------------------

class BurstDetector:
    """Slices a live audio stream into candidate bursts.

    Needed because aicodix `decode` extracts only the first datagram from a
    continuous stream -- proven by measurement, not guessed.

    Uses an adaptive noise floor rather than a fraction of peak, because on
    a live stream there is no peak to compare against yet. The floor tracks
    quiet passages and the trigger sits a fixed factor above it.
    """

    def __init__(self, rate=RATE, win_ms=5.0, trigger=6.0, release=3.0,
                 hold_ms=50.0, min_s=0.75, max_s=5.0, pad_ms=60.0,
                 warmup_ms=250.0):
        self.rate = rate
        self.win = max(1, int(rate * win_ms / 1000))
        self.trigger = trigger          # open at floor * trigger
        self.release = release          # close at floor * release (hysteresis)
        self.hold = max(1, int(hold_ms / win_ms))
        self.min_len = int(rate * min_s)
        self.max_len = int(rate * max_s)
        self.pad = int(rate * pad_ms / 1000)
        self.warmup = max(4, int(warmup_ms / win_ms))
        # The floor MUST be learned before anything can trigger. Seeding it
        # with a guess that lands below the real noise level makes the
        # detector fire on silence, and since the floor is frozen while a
        # burst is open it can then never close -- every burst runs to the
        # max-length cap and swallows the one after it.
        self.floor = None
        self._warm = []
        self._tail = None
        self._buf = []
        self._quiet = 0
        self._open = False
        self._pre = []                  # rolling pre-trigger padding
        self.truncated = 0              # bursts that hit the cap (diagnostic)

    def _rms(self, block):
        import numpy as np
        n = len(block) - (len(block) % self.win)
        if n <= 0:
            return []
        return np.sqrt((block[:n].reshape(-1, self.win)
                        .astype("float64") ** 2).mean(axis=1))

    def feed(self, block):
        """Feed a block of float samples. Returns a list of complete bursts."""
        import numpy as np
        # Own the samples before retaining any slices.  sounddevice reuses its
        # callback buffer after return; keeping a view here corrupts a burst
        # while it is waiting for framing/decoding.
        block = np.asarray(block, dtype="float32").reshape(-1).copy()
        if self._tail is not None and len(self._tail):
            block = np.concatenate([self._tail, block])
        n = len(block) - (len(block) % self.win)
        self._tail = block[n:].copy()
        if n <= 0:
            return []
        block = block[:n]
        rms = self._rms(block)
        out = []

        for i, r in enumerate(rms):
            seg = block[i * self.win:(i + 1) * self.win]

            if self.floor is None:
                # Learn the floor before anything may trigger. Median, not
                # mean, so a burst arriving mid-warmup cannot drag it up.
                self._warm.append(float(r))
                if len(self._warm) >= self.warmup:
                    s = sorted(self._warm)
                    self.floor = max(s[len(s) // 2], 1e-6)
                continue

            if not self._open:
                # track the floor only while idle, so a long burst cannot
                # drag the threshold up and swallow itself
                self.floor = 0.995 * self.floor + 0.005 * max(float(r), 1e-6)
                self._pre.append(seg)
                if len(self._pre) * self.win > self.pad:
                    self._pre.pop(0)
                if r > self.floor * self.trigger:
                    self._open = True
                    self._buf = list(self._pre)
                    self._pre = []
                    self._quiet = 0
            else:
                self._buf.append(seg)
                self._quiet = self._quiet + 1 if r < self.floor * self.release else 0
                length = len(self._buf) * self.win
                capped = length >= self.max_len
                if self._quiet >= self.hold or capped:
                    if capped:
                        self.truncated += 1
                    if length >= self.min_len:
                        out.append(np.concatenate(self._buf))
                    self._open = False
                    self._buf = []
                    self._quiet = 0
        return out

    def flush(self):
        """Emit any burst still in progress (end of a file or stream)."""
        import numpy as np
        if self._open and len(self._buf) * self.win >= self.min_len:
            burst = np.concatenate(self._buf)
            self._open, self._buf = False, []
            return [burst]
        self._open, self._buf = False, []
        return []


def split_bursts(stream, rate=RATE, **kw):
    """Convenience: slice a whole recorded stream in one call."""
    det = BurstDetector(rate=rate, **kw)
    return det.feed(stream) + det.flush()


def concat_bursts(bursts, rate=RATE, gap_s=0.12):
    """Join bursts with PTT-sized gaps, the way they go out on air."""
    import numpy as np
    gap = np.zeros(int(rate * gap_s), dtype="float32")
    parts = []
    for b in bursts:
        parts += [np.asarray(b, dtype="float32"), gap]
    return np.concatenate(parts) if parts else np.zeros(0, dtype="float32")


def available(vendor=None) -> dict:
    vendor = vendor or os.path.expanduser("~/crowcall/vendor")
    enc = os.path.join(vendor, "modem", "encode")
    dec = os.path.join(vendor, "modem", "decode")
    return {
        "vendor": vendor,
        "encode": enc if os.access(enc, os.X_OK) else None,
        "decode": dec if os.access(dec, os.X_OK) else None,
        "disorders": os.path.isdir(os.path.join(vendor, "disorders")),
        "sounddevice": bool(shutil.which("python3")) and _has_sounddevice(),
    }


def _has_sounddevice():
    try:
        import sounddevice  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False
