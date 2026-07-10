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
                 debug_tone_hz: float | None = None):
        if sd is None:
            raise RuntimeError("sounddevice is not installed. Run: pip install sounddevice")

        self.modem = modem
        self.on_recv = on_recv
        self.on_drop = on_drop
        self.on_tx_audio = on_tx_audio
        self.on_rx_audio = on_rx_audio
        self.rate = 48000
        self.debug_tone_hz = None if debug_tone_hz in (None, 0, 0.0) else float(debug_tone_hz)
        if self.debug_tone_hz is not None:
            MODEM.debug_tone(self.debug_tone_hz, self.rate)

        self.device = device

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
        """Physical carrier sense: is the mic hearing a signal above the noise floor right now?"""
        return self.detector._open

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any):
        """Callback executed per input block by sounddevice."""
        # Copy at the callback boundary; the PortAudio buffer is borrowed and
        # may be reused immediately after this function returns.
        bursts = self.detector.feed(np.asarray(indata, dtype="float32").copy())
        for b in bursts:
            try:
                self._rx_queue.put_nowait(np.asarray(b, dtype="float32").copy())
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
                continue

            noisy, snr = self.impair.apply_noise(burst_samples, self.rate)
            # A diagnostic prefix is intentionally outside the modem frame.
            # Soundcard framing has a pre-trigger pad, so locate and remove it
            # before handing samples to the decoder.  Passing None permits a
            # receiver to identify any station's configured tone frequency.
            # Auto-detect lets a receiver hear stations configured with a
            # different frequency (or with no local --debug-tone flag).  The
            # detector only strips when a stable sinusoid clears its
            # correlation threshold, so ordinary modem preambles pass through.
            decode_samples, _ = MODEM.strip_debug_tone(noisy, self.rate, None)
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

    def send(self, burst: bytes, mode=None):
        """Modulates and plays audio samples via system sound output."""
        with self._tx_lock:
            samples, rate = self.modem.modulate(burst, mode=mode)
            if self.debug_tone_hz is not None:
                samples = MODEM.prepend_debug_tone(samples, rate, self.debug_tone_hz)

            if self.on_tx_audio:
                try:
                    self.on_tx_audio(burst, samples, rate, None)
                except Exception:
                    pass

            sd.play(samples, samplerate=rate, device=self.device)
            sd.wait()

            self.tx_count += 1

    def stats(self) -> dict:
        return {
            "tx": self.tx_count,
            "rx": self.rx_count,
            "mode_fallbacks": getattr(self.modem, "fallbacks_used", 0),
            "dropped": self.drop_count,
            "port": "audio",
            "transport": self.name
        }

    def close(self):
        self._stop.set()
        if hasattr(self, "stream") and self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
