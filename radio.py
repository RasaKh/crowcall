"""
crowcall radio transport -- real modem, UDP as the ether
========================================================

UDP carries MODULATED AUDIO, not protocol bursts. Each burst is encoded by
aicodix into ~1.1 s of audio, fragmented across datagrams, reassembled at
the far end, and run through the real decoder.

Two things this buys over shipping the 256-byte bursts directly:

  * The modem is genuinely in the loop -- OFDM, polar-coded payload,
    BCH-coded header, Schmidl-Cox sync, all actually running. A burst that
    fails does so because the FEC gave up, not because a random number said
    to drop it.

  * Impairments become physical. Instead of "discard 35% of packets", we add
    noise at a chosen SNR and let the decoder decide. Step-4 calibration
    pinned the conversion exactly:

        in-band SNR (dB) = 0.9 - awgn_param

    with the cliff at roughly 9.5 dB -- below it nothing decodes, above it
    everything does. So the UI knob reads in dB and produces the real
    failure mode.

Fragmentation rather than a lower sample rate: a 1.1 s burst at 48 kHz/16-bit
is ~112 KB and a UDP datagram caps at 65,507. Running the modem slower would
fit one datagram per burst, but that assumes aicodix accepts sub-48 kHz
rates, which is unverified. Fragmenting assumes nothing.

Trimming first, though: the encoder pads every burst with 1.00 s of lead and
0.99 s of tail silence -- 55% of the file is nothing -- so trimming roughly
halves the datagrams per burst before we even start.
"""

from __future__ import annotations

import heapq
import itertools
import queue
import random
import socket
import struct
import threading
import time

import modem as MODEM

# ---------------------------------------------------------------------------
# CHANNEL PARAMETERS
#
# BAND_LO/HI  The signal occupies 2400 Hz centred on 1500 Hz, so 300-2700 Hz.
#             That is not arbitrary: it is chosen to sit inside an SSB radio's
#             audio passband, which is the whole reason aicodix works over HF
#             gear at all. Noise outside this band is filtered away by the
#             decoder and does not count against you, which is why SNR here is
#             always in-band SNR.
#
# CLIFF_DB    9.5 dB in-band SNR. Measured, and sharp: at 1 dB steps the modem
#             went from 100% to 0% between 9.9 and 9.0 dB with no transition
#             region, and ALL 120 failures were sync loss rather than payload
#             loss. Every modulation died at the same point, which is why
#             modulation is fixed at the fastest option -- it buys throughput
#             and no robustness whatsoever.
#
#             Practical consequence: this is not a weak-signal mode. It needs a
#             comfortable armchair-copy signal. FT8 and WSPR work 20+ dB below
#             it. The link is either up or flatly down, so repair exists to
#             recover chunks lost inside a fade window, not to nibble at a
#             steady error rate.
#
# The awgn parameter of the disorders tools maps to this as:
#     in-band SNR (dB) = 0.9 - awgn_param
# constant to within 0.44 dB across a 31 dB sweep.
# ---------------------------------------------------------------------------
BAND_LO, BAND_HI = 300.0, 2700.0
CLIFF_DB = 9.5                  # measured: below this, nothing decodes

MAGIC = b"CRW3"
# magic, sender session, burst id, fragment total/index, exact PCM byte total,
# byte offset, sample rate, sample count, diagnostic-tone sample count. Source
# identity prevents two
# stations that happen to reuse a 16-bit stream number from mixing audio.
FRAG_FMT = ">4sQQHHIIHIH"
FRAG_LEN = struct.calcsize(FRAG_FMT)
# FRAG_PAYLOAD  Bytes of audio per datagram. A trimmed 1.17 s burst at
#               48 kHz/16-bit is ~112 KB, well past the 65,507-byte UDP
#               ceiling, so it is fragmented. 8 KB gives ~14 fragments per
#               burst and is safe because the loopback MTU is 64 KB. Lower it
#               towards 1400 if you ever route this over a real network.
#               NOTE: the hub must be able to receive whatever this is set to
#               -- it read 2048 once and silently truncated every fragment.
# STREAM_TTL    How long a half-arrived burst waits for its missing fragments
#               before being decoded anyway. Roughly 5 burst times. The FEC
#               will almost never survive a hole, but trying costs nothing and
#               the failure is reported with an honest reason.
FRAG_PAYLOAD = 8192             # loopback MTU is 64 KB, so this is safe
STREAM_TTL = 6.0
# Bound state created by unauthenticated UDP peers. Without this, a peer can
# keep one incomplete stream per forged session and grow the receiver forever.
MAX_STREAMS = 64


class Channel:
    """Audio-domain impairments. SNR is real dB, in-band."""

    def __init__(self):
        # snr_db     In-band SNR the receiver sees. 20 dB is comfortable, 10 dB
        #            is marginal, below 9.5 nothing decodes at all.
        # fade_prob  Probability per burst that a fade STARTS. Not per-packet
        #            loss: a fade blacks out a time window, which is what
        #            actually happens on HF and is the case repair has to
        #            handle. Independent packet loss is the wrong model here.
        # fade_ms    How long the blackout lasts. 3 s is ~2.5 bursts, so a
        #            single fade typically eats two or three chunks.
        # delay_ms   Constant propagation delay, for testing timer behaviour.
        self.enabled = True
        self.snr_db = 20.0          # comfortable; the cliff is ~9.5
        self.fade_prob = 0.0        # chance per burst that a fade begins
        self.fade_ms = 3000
        self.delay_ms = 0
        self._fade_until = 0.0
        self._lock = threading.Lock()

    def update(self, **kw):
        for k, v in kw.items():
            if k.startswith("_") or not hasattr(self, k):
                continue
            cur = getattr(self, k)
            try:
                setattr(self, k, bool(v) if isinstance(cur, bool)
                        else type(cur)(v))
            except (TypeError, ValueError):
                pass

    def state(self):
        return {"enabled": self.enabled, "snr_db": self.snr_db,
                "fade_prob": self.fade_prob, "fade_ms": self.fade_ms,
                "delay_ms": self.delay_ms, "fading": self.in_fade(),
                "cliff_db": CLIFF_DB}

    def in_fade(self):
        return time.time() < self._fade_until

    def maybe_start_fade(self):
        """Called once per arriving burst. A fade blacks out a time window,
        which is what a real fade does -- it does not sprinkle losses."""
        if not self.enabled or self.fade_prob <= 0:
            return False
        with self._lock:
            if self.in_fade():
                return True
            if random.random() < self.fade_prob:
                self._fade_until = time.time() + self.fade_ms / 1000.0
                return True
        return False

    def apply_noise(self, samples, rate):
        """Add white noise scaled to hit the target in-band SNR.

        The signal fills 300-2700 Hz, so its in-band power is its total
        power. White noise spreads over 0..rate/2 and only the 2400 Hz slice
        competes with it, hence the bandwidth correction. This mirrors the
        disorders `awgn` tool -- calibration measured 4.9% of its noise
        landing in band, which is exactly 2400/24000 -- but in-process,
        because a subprocess per burst would be far too slow.
        """
        import numpy as np
        if not self.enabled:
            return samples, None
        snr = self.snr_db
        x = np.asarray(samples, dtype="float64")
        sig = float((x ** 2).mean())
        if sig <= 0:
            return samples, snr
        nyq = rate / 2.0
        band = max(1.0, min(BAND_HI, nyq) - BAND_LO)
        noise_pow = (sig / (10 ** (snr / 10.0))) * (nyq / band)
        noise = np.random.default_rng().standard_normal(len(x)) * noise_pow ** 0.5
        return (x + noise).astype("float32"), snr


class _Stream:
    __slots__ = ("total", "rate", "samples", "total_bytes", "tone_samples", "frags", "at")

    def __init__(self, total, rate, samples, total_bytes, tone_samples=0):
        self.total, self.rate, self.samples = total, rate, samples
        self.total_bytes, self.tone_samples = total_bytes, tone_samples
        self.frags = {}
        self.at = time.time()


class ModemUdpTransport:
    """Same interface as HubTransport: send(bytes), on_recv(bytes)."""

    max_payload = 1536              # largest selected aicodix protocol burst

    def __init__(self, hub_addr, modem, on_recv=None, on_drop=None,
                 on_tx_audio=None, on_rx_audio=None,
                 frag_payload=FRAG_PAYLOAD, debug_tone_hz=None):
        self.hub_addr = hub_addr
        self.modem = modem
        self.on_recv = on_recv
        self.on_drop = on_drop
        # (burst_bytes_or_None, samples, rate, snr_or_None) -- lets the caller
        # archive what was actually on the wire, including bursts that died
        self.on_tx_audio = on_tx_audio
        self.on_rx_audio = on_rx_audio
        self.frag_payload = frag_payload
        self.debug_tone_hz = None if debug_tone_hz in (None, 0, 0.0) else float(debug_tone_hz)
        if self.debug_tone_hz is not None:
            MODEM.debug_tone(self.debug_tone_hz)
        self.impair = Channel()
        # name the mode SET, not one mode -- each message picks its own
        import modes as _M
        self.name = ("aicodix " + "/".join(m.name for m in _M.MODES)
                     + " over udp")

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, opt, 4 << 20)
            except OSError:
                pass
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.5)
        self.local = self.sock.getsockname()

        self.tx_count = self.rx_count = self.drop_count = 0
        self.frag_lost = 0
        self.last_snr = None
        self.last_rx_time = 0.0  # Added for CSMA physical checks
        
        self._streams: dict[int, _Stream] = {}
        self._next_stream = random.getrandbits(16)
        self.session_id = random.getrandbits(64) or 1
        self._lock = threading.Lock()
        self._delayed: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._delay_worker, daemon=True).start()
        threading.Thread(target=self._reaper, daemon=True).start()
        threading.Thread(target=self._keepalive, daemon=True).start()

    def is_busy(self) -> bool:
        """Physical carrier sense: did we get a UDP packet in the last 1.5 seconds?"""
        return time.time() - getattr(self, "last_rx_time", 0) < 1.5

    # -- transmit --------------------------------------------------------

    def send(self, burst: bytes, mode=None):
        """Modulate, trim, fragment, fire. No sleeps: the original version
        slept 5 ms per fragment, which added half a second of blocking delay
        to every burst. Raising SO_SNDBUF does the same job for free."""
        import numpy as np
        # The caller always knows the mode -- it built the burst for it. This
        # used to re-parse the burst to recover it, which meant a malformed
        # burst fell through to whatever default the modem carried. Passing it
        # explicitly removes that failure path entirely.
        samples, rate = self.modem.modulate(burst, mode=mode)   # trimmed
        tone_samples = int(round(rate * MODEM.DEBUG_TONE_S)) if self.debug_tone_hz is not None else 0
        if self.debug_tone_hz is not None:
            samples = MODEM.prepend_debug_tone(samples, rate, self.debug_tone_hz)
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
        if self.on_tx_audio:
            try:
                self.on_tx_audio(burst, samples, rate, None)
            except Exception:  # noqa: BLE001 - archiving must not break tx
                pass

        with self._lock:
            sid = self._next_stream = (self._next_stream + 1) & 0xFFFF

        n = max(1, -(-len(pcm) // self.frag_payload))
        if n > 0xFFFF or len(pcm) > 0xFFFFFFFF:
            raise ValueError("modem burst exceeds fragmentation limits")
        # Spread the fragments over a slice of the burst instead of firing
        # them all at once. A `fast` burst is 3.56 s of 48 kHz audio = ~340 KB
        # in 42 datagrams; dumping that into the hub instantly is exactly the
        # kind of thing that loses the opening of a transfer. The burst takes
        # seconds anyway, so 200 ms of spreading is free.
        spread = min(0.2, self.modem_air(mode) / 8.0) if n > 4 else 0.0
        per_frag = spread / n if n else 0.0
        for i in range(n):
            if per_frag:
                time.sleep(per_frag)
            chunk = pcm[i * self.frag_payload:(i + 1) * self.frag_payload]
            head = struct.pack(FRAG_FMT, MAGIC, self.session_id, sid, n, i,
                               len(pcm), i * self.frag_payload, rate,
                               len(samples), tone_samples & 0xFFFF)
            try:
                self.sock.sendto(head + chunk, self.hub_addr)
            except OSError:
                time.sleep(0.002)                       # buffer full, retry once
                try:
                    self.sock.sendto(head + chunk, self.hub_addr)
                except OSError:
                    pass
        self.tx_count += 1

    def modem_air(self, mode):
        base = getattr(mode, "air_s", 1.21) if mode is not None else 1.21
        return base + (MODEM.DEBUG_TONE_S if self.debug_tone_hz is not None else 0.0)

    # -- receive ---------------------------------------------------------

    def _on_fragment(self, blob):
        if len(blob) < FRAG_LEN or blob[:4] != MAGIC:
            return
            
        self.last_rx_time = time.time()  # Track activity for CSMA
        
        _, session, sid, total, idx, total_bytes, offset, rate, nsamp, tone_samples = struct.unpack(FRAG_FMT, blob[:FRAG_LEN])
        body = blob[FRAG_LEN:]
        if not (1 <= total <= 0xFFFF and idx < total and 1 <= nsamp <= 2 * 1024 * 1024
                and total_bytes <= 2 * 1024 * 1024
                and offset + len(body) <= total_bytes and len(body) <= self.frag_payload
                and total_bytes % 2 == 0 and offset == idx * self.frag_payload
                and len(body) == min(self.frag_payload, total_bytes - offset)):
            return
        if tone_samples > nsamp or tone_samples * 2 > total_bytes:
            return

        with self._lock:
            key = (session, sid)
            st = self._streams.get(key)
            if st is None:
                # Bound incomplete state before allocating it.
                if len(self._streams) >= MAX_STREAMS:
                    oldest = min(self._streams, key=lambda k: self._streams[k].at)
                    del self._streams[oldest]
                # decide the fade at the START of a burst, so the whole burst
                # shares one channel state instead of flickering mid-burst
                if self.impair.maybe_start_fade():
                    self._streams[key] = _Stream(total, rate, nsamp, total_bytes, tone_samples)
                    self._streams[key].frags = None      # marked as faded out
                    self.drop_count += 1
                    if self.on_drop:
                        self.on_drop(b"", "fade")
                    return
                st = self._streams[key] = _Stream(total, rate, nsamp, total_bytes, tone_samples)
            elif (st.total, st.rate, st.samples, st.total_bytes, st.tone_samples) != (total, rate, nsamp, total_bytes, tone_samples):
                return
            if st.frags is None:
                return                                   # faded, ignore the rest
            if idx in st.frags and st.frags[idx] != body:
                return
            st.frags[idx] = body
            st.at = time.time()
            if len(st.frags) < st.total:
                return
            del self._streams[key]

        self._decode_stream(st)

    def _decode_stream(self, st, partial=False):
        import numpy as np
        pcm = b"".join(st.frags.get(i, b"\x00" * self.frag_payload)
                       for i in range(st.total))[:st.total_bytes]
        missing = st.total - len(st.frags)
        if missing:
            self.frag_lost += missing
        x = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        if not len(x):
            return

        noisy, snr = self.impair.apply_noise(x, st.rate)
        self.last_snr = snr

        decode_samples = noisy[st.tone_samples:]
        burst = self.modem.demodulate(decode_samples, st.rate)
        if self.on_rx_audio:
            try:
                self.on_rx_audio(burst, noisy, st.rate, snr)
            except Exception:  # noqa: BLE001 - archiving must not break rx
                pass
        if burst is None:
            self.drop_count += 1
            if self.on_drop:
                if missing:
                    reason = f"{missing}/{st.total} fragments lost"
                elif snr is not None:
                    reason = (f"noise ({snr:.0f} dB, cliff {CLIFF_DB})"
                              if snr < CLIFF_DB + 3 else "decode failed")
                else:
                    reason = "decode failed"
                self.on_drop(b"", reason)
            return
        self.rx_count += 1
        if self.on_recv:
            self.on_recv(burst)

    def _loop(self):
        try:
            self.sock.sendto(b"\x00HELLO", self.hub_addr)
        except OSError:
            pass
        while not self._stop.is_set():
            try:
                blob, _ = self.sock.recvfrom(FRAG_PAYLOAD + 256)
            except socket.timeout:
                continue
            except OSError:
                break
            if blob.startswith(b"\x00"):
                continue
            d = self.impair.delay_ms / 1000.0
            if d > 0:
                self._defer(d, blob)
            else:
                self._safe(blob)

    def _safe(self, blob):
        try:
            self._on_fragment(blob)
        except Exception:  # noqa: BLE001 - one bad burst must not kill rx
            pass

    def _reaper(self):
        """Time out half-arrived bursts. The original only cleaned up when a
        new packet happened to arrive, so a stalled stream could sit forever."""
        while not self._stop.is_set():
            time.sleep(1.0)
            now = time.time()
            stale = []
            with self._lock:
                for sid, st in list(self._streams.items()):
                    if now - st.at > STREAM_TTL:
                        del self._streams[sid]
                        if st.frags:
                            stale.append(st)
                        elif st.frags is None:
                            pass
            for st in stale:
                # try anyway: the FEC may cover a small hole, and if it does
                # not, the drop is reported with an honest reason
                try:
                    self._decode_stream(st, partial=True)
                except Exception:  # noqa: BLE001
                    pass

    # -- misc ------------------------------------------------------------

    def _defer(self, delay, blob):
        """Deliver later without spawning a thread per packet.

        This was threading.Timer(...).start() per datagram. Harmless for one
        burst of raw protocol packets; ruinous once a `fast` burst became 42
        audio fragments, which is 42 threads created and torn down per burst
        the moment any delay is configured.
        """
        self._delayed.put((time.time() + delay, next(self._seq), blob))

    def _delay_worker(self):
        while not self._stop.is_set():
            try:
                due, _, blob = self._delayed.get(timeout=0.25)
            except queue.Empty:
                continue
            wait = due - time.time()
            if wait > 0:
                time.sleep(min(wait, 5.0))
            self._safe(blob)

    def _keepalive(self):
        while not self._stop.wait(20.0):
            try:
                self.sock.sendto(b"\x00HELLO", self.hub_addr)
            except OSError:
                return

    def stats(self):
        with self._lock:
            pending = len(self._streams)
        return {"tx": self.tx_count, "rx": self.rx_count,
                "mode_fallbacks": getattr(self.modem, "fallbacks_used", 0),
                "dropped": self.drop_count, "frag_lost": self.frag_lost,
                "pending": pending, "port": self.local[1],
                "transport": self.name, "last_snr_db": self.last_snr}

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
