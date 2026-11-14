"""
crowcall soundcard transport -- mic and speakers
================================================

Directly interfaces with the system's audio input and output devices.
Received audio is passed through the BurstDetector to slice bursts
out of the continuous microphone stream prior to demodulation.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable, Any
import time

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sd = None

import modem as MODEM
import radio


class SoundcardTransport:
    """Hardware audio transport sharing the interface of HubTransport and ModemUdpTransport."""

    def __init__(self, modem: MODEM.AicodixModem, on_recv: Callable[[bytes], None] | None = None,
                 on_drop: Callable[[bytes, str], None] | None = None,
                 on_tx_audio: Callable[..., None] | None = None,
                 on_rx_audio: Callable[..., None] | None = None,
                 device: int | str | tuple[Any, Any] | None = None,
                 debug_tone_hz: float | None = None,
                 debug_tone_s: float = MODEM.DEBUG_TONE_S,
                 vox_hang_ms: int = 1200):
        if sd is None:
            raise RuntimeError("sounddevice is not installed. Run: pip install sounddevice")

        self.modem = modem
        self.on_recv = on_recv
        self.on_drop = on_drop
        self.on_tx_audio = on_tx_audio
        self.on_rx_audio = on_rx_audio
        self.rate = 48000
        self.debug_tone_hz = None if debug_tone_hz in (None, 0, 0.0) else float(debug_tone_hz)
        self.debug_tone_s = float(debug_tone_s)
        if self.debug_tone_hz is not None:
            MODEM.debug_tone(self.debug_tone_hz, self.rate)

        self.device = device
        self.vox_hang_s = max(0.0, float(vox_hang_ms) / 1000.0)

        self.tx_count = 0
        self.rx_count = 0
        self.drop_count = 0
        self.name = "soundcard (mic/speaker)"
        self.local = ("soundcard", 0)

        self.impair = radio.Channel()
        self.impair.enabled = False

        self.detector = MODEM.BurstDetector(rate=self.rate)

        self._stop = threading.Event()
        self._tx_lock = threading.Lock()
        self._tx_active = False
        self._mute_until = 0.0
        self.last_rx_time = 0.0
        self.last_snr = None
        self.last_rx_tone_ms = None

        self.tx_gain = 1.0
        self.auto_level = True
        self.vu_rms = 0.0
        self.vu_peak = 0.0
        self.vu_db = -60.0
        self.clip_until = 0.0
        self.last_burst_peak = 0.0
        self.last_burst_clipped = False
        self.last_callback_at = time.time()
        self.stream_error: str | None = None

        self._rx_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=4)

        threading.Thread(target=self._rx_worker, daemon=True).start()

        self.stream = sd.InputStream(
            samplerate=self.rate,
            channels=1,
            device=self.device,
            callback=self._audio_callback
        )
        self.stream.start()

    def is_busy(self) -> bool:
        """Physical carrier sense: mic active OR walkie-talkie VOX hang time."""
        return bool(self.detector._open or (time.time() - self.last_rx_time < self.vox_hang_s))

    def _reopen_stream(self):
        try:
            if hasattr(self, "stream") and self.stream:
                try:
                    self.stream.stop()
                except Exception:
                    pass
                try:
                    self.stream.close()
                except Exception:
                    pass
            time.sleep(0.5)
            self.stream = sd.InputStream(
                samplerate=self.rate,
                channels=1,
                device=self.device,
                callback=self._audio_callback,
            )
            self.stream.start()
            self.detector = MODEM.BurstDetector(rate=self.rate)
            self.last_callback_at = time.time()
            self.stream_error = None
        except Exception as exc:  # noqa: BLE001
            self.stream_error = str(exc)[:120]

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any):
        """Callback executed per input block by sounddevice."""
        now = time.time()
        self.last_callback_at = now
        if self._tx_active or now < self._mute_until:
            return
        # Copy at the callback boundary; the PortAudio buffer is borrowed and
        # may be reused immediately after this function returns.
        block = np.asarray(indata, dtype="float32").reshape(-1).copy()
        if not len(block):
            return

        blk_rms = float(np.sqrt(np.mean(block.astype("float64") ** 2)))
        blk_peak = float(np.max(np.abs(block)))
        self.vu_rms = 0.70 * self.vu_rms + 0.30 * blk_rms
        self.vu_peak = max(blk_peak, self.vu_peak * 0.85)
        self.vu_db = round(max(-60.0, 20.0 * np.log10(max(self.vu_rms, 1e-3))), 1)
        if blk_peak >= 0.96:
            self.clip_until = now + 3.0

        bursts = self.detector.feed(block)
        for b in bursts:
            b_copy = np.asarray(b, dtype="float32").copy()
            raw_peak = float(np.max(np.abs(b_copy))) if len(b_copy) else 0.0
            self.last_burst_peak = round(raw_peak, 3)
            self.last_burst_clipped = (raw_peak >= 0.96)
            if self.auto_level and raw_peak > 0.002:
                if raw_peak < 0.35:
                    gain = min(25.0, 0.55 / raw_peak)
                    b_copy = (b_copy * gain).astype("float32")
                elif raw_peak > 0.85:
                    b_copy = (b_copy * (0.70 / raw_peak)).astype("float32")
            try:
                self._rx_queue.put_nowait(b_copy)
            except queue.Full:
                self.drop_count += 1
                if self.on_drop:
                    self.on_drop(b"", "audio receive queue full")

    def _rx_worker(self):
        """Worker thread processing detected audio bursts."""
        while not self._stop.is_set():
            try:
                burst_samples = self._rx_queue.get(timeout=0.5)
            except queue.Empty:
                if (time.time() - self.last_callback_at > 3.0
                        or not getattr(self.stream, "active", False)):
                    self._reopen_stream()
                continue

            self.last_rx_time = time.time()
            noisy, snr = self.impair.apply_noise(burst_samples, self.rate)
            decode_samples, _, heard_s = MODEM.strip_debug_tone_info(
                noisy, self.rate, None)
            self.last_rx_tone_ms = round(heard_s * 1000) if heard_s > 0 else 0
            if snr is None and len(decode_samples):
                sig_rms = float(np.sqrt(np.mean(decode_samples.astype("float64") ** 2)))
                floor_rms = float(self.detector.floor or 1e-5)
                if sig_rms > 0 and floor_rms > 0:
                    snr = round(20.0 * np.log10(max(sig_rms, 1e-9) / max(floor_rms, 1e-9)), 1)
            self.last_snr = snr
            decoded = self.modem.demodulate(decode_samples, self.rate)

            if self.on_rx_audio:
                try:
                    self.on_rx_audio(decoded, noisy, self.rate, snr)
                except Exception:
                    pass

            if decoded:
                self.rx_count += 1
                if self.on_recv:
                    self.on_recv(decoded)
            else:
                self.drop_count += 1
                if self.on_drop:
                    self.on_drop(b"", "decode failed")

    def send(self, burst: bytes, mode=None, tone_duration_s: float | None = None):
        """Modulates and plays audio samples via system sound output."""
        with self._tx_lock:
            self._tx_active = True
            try:
                samples, rate = self.modem.modulate(burst, mode=mode)
                if self.debug_tone_hz is not None:
                    dur = self.debug_tone_s if tone_duration_s is None else float(tone_duration_s)
                    samples = MODEM.prepend_debug_tone(
                        samples, rate, self.debug_tone_hz, duration_s=dur)
                if self.tx_gain != 1.0:
                    scaled = np.asarray(samples, dtype="float32") * float(self.tx_gain)
                    samples = (0.85 * np.tanh(scaled / 0.85)).astype("float32")

                if self.on_tx_audio:
                    try:
                        self.on_tx_audio(burst, samples, rate, None)
                    except Exception:
                        pass

                sd.play(samples, samplerate=rate, device=self.device)
                sd.wait()

                self.tx_count += 1
            finally:
                self._mute_until = time.time() + 0.15
                self._tx_active = False

    def stats(self) -> dict:
        return {
            "tx": self.tx_count,
            "rx": self.rx_count,
            "mode_fallbacks": getattr(self.modem, "fallbacks_used", 0),
            "dropped": self.drop_count,
            "port": "audio",
            "transport": self.name,
            "last_snr_db": self.last_snr,
            "last_rx_tone_ms": self.last_rx_tone_ms,
            "stream_error": self.stream_error,
            "vu": {
                "rms": round(self.vu_rms, 4),
                "peak": round(self.vu_peak, 3),
                "db": self.vu_db,
                "floor": round(float(self.detector.floor or 0.0), 5),
                "clipping": time.time() < self.clip_until,
                "gate_open": bool(self.detector._open),
                "last_burst_peak": self.last_burst_peak,
                "last_burst_clipped": self.last_burst_clipped,
                "tx_gain": self.tx_gain,
                "auto_level": self.auto_level,
            },
        }

    def close(self):
        self._stop.set()
        if hasattr(self, "stream") and self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
