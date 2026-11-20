# crowcall

**Off-grid, store-and-forward voice, text, and file messenger for analog radios.**

`crowcall` turns any laptop or Raspberry Pi (and any phone browser on its local Wi-Fi) into a digital radio terminal over ordinary analog radios—VHF/UHF walkie-talkies, CB, or HF/SSB—using **Codec2** speech compression and the **aicodix** OFDM/polar-coded audio modem. It is built for grid-down and zero-infrastructure environments where there is no internet, no cell service, and no repeater infrastructure.

---

## Why Store-and-Forward Radio?

Live digital voice drops out completely the moment a fade hits. `crowcall` instead slices voice, text, or small files into numbered, CRC/HMAC-checked bursts (~1.2–3.7 s each) and transmits them in interleaved order:

1. **Voice** is recorded in the browser (or phone mic), stripped of silence with optional Silero VAD, compressed with Codec2 (`700C`–`3200`), and sent as chunks. Missing chunks are concealed by repeating the last frame or recovered via selective ARQ repair (`T_RREQ` / `T_RACK` / `T_DONE`).
2. **Text & files** are compressed with `zlib` (when smaller than raw), prefixed with exact length, filename, and CRC32, and chunked for byte-exact reassembly.
3. **Zero-configuration receiver:** Every burst carries its modulation, Codec2 rate, hop count, and transfer metadata in a BCH-coded OFDM frame header + 60-byte Crowcall v3 header, so sender and receiver never need to match settings beforehand.

---

## Quick Start (Real Radio via Soundcard)

### Dependencies & Build
```bash
sudo apt install codec2 ffmpeg g++ make
pip install flask flask-socketio numpy sounddevice   # pysilero-vad is optional

git clone --recurse-submodules https://github.com/RasaKh/crowcall.git
cd crowcall
make
```

> `vendor/{code,dsp,modem,disorders}` are git submodules tracking the upstream `aicodix` repositories. Running `make` initializes submodules (if needed) and builds the `encode`/`decode` and channel disorder binaries. `AicodixModem` also auto-compiles `vendor/modem` on first run if binaries are missing.

### Run on a Radio (`--soundcard`)
Connect your laptop's audio interface (or 3.5 mm cable / acoustic coupling) to a radio with VOX enabled:

```bash
python3 app.py --call ALPHA --soundcard --debug-tone 800 --host 0.0.0.0
```

Open `http://127.0.0.1:5001` (or `http://<laptop-ip>:5001` from a smartphone on the same hotspot).

### Hardware & Off-Grid Features
* **VOX Wakeup Tone & Auto-Calibration:** Prepends a configurable pilot tone (`--debug-tone`, default 120 ms) to key VOX before the OFDM preamble. `strip_debug_tone_info()` measures the surviving tail so VOX-chopped audio never eats into the sync preamble. Click **Calibrate VOX & Level** in the UI to measure the far radio's exact VOX cutoff and auto-adjust `debug_tone_ms` and `tx_gain` over the air.
* **Open-Squelch & VOX Hang-Time Handling:** `BurstDetector` tracks peak-relative RMS to close bursts cleanly across open-squelch inter-burst gaps, while `SoundcardTransport` enforces half-duplex RX self-mute during TX and a 1200 ms VOX turnaround guard before replying.
* **Live RX VU Meter, Software AGC & Hot-Plug Watchdog:** Normalizes quiet bursts, soft-limits hot TX (`tanh`), alerts on ADC clipping, and automatically reopens `sounddevice.InputStream` if a USB audio dongle is bumped or replugged.
* **Simplex Store-and-Forward Repeater (`--repeater`):** Relays complete messages across terrain obstacles on a single frequency while preserving original `msg_id`, `origin`, and a 2-bit hop limit (`MAX_HOPS = 3`).
* **Fire-and-Forget Delivery (`auto_retry`):** Unconfirmed messages retry with exponential backoff and wake immediately when any beacon or traffic is heard from a peer.
* **Optional Channel Encryption (`--key`):** Zero-overhead Encrypt-then-MAC (`BLAKE2s` counter-mode stream cipher + 32-bit truncated `HMAC-SHA256`) using a pre-shared passphrase.
* **Sneakernet / Zero-Cable Mode:** Export any outbox message as a ready-to-play 48 kHz `.wav` (`/api/tx_wav/<mid>`) or upload a recorded radio `.wav` (`/api/rx_wav`) to decode offline.
* **Mobile & Field UX:** Hold-to-PTT or tap-to-latch voice recording, mobile HTTP mic capture fallback, editable callsign, Dusk / Daylight / Night Red themes, and persistent `recordings/` history hydration.

---

## Modem Profiles & Link Health Adaptation

All four profiles sit on the measured **~9.9 dB in-band SNR acquisition floor** using constant-envelope PSK:

| Profile | Aicodix Mode | Usable Payload | Burst Air Time | Throughput | Role |
|---|---|---|---|---|---|
| `control` | QPSK 1/2 short | 68 B | 1.21 s | 56 B/s | Control (`RREQ`/`RACK`/`DONE`/`BEACON`), Fine voice |
| `compact` | QPSK 5/6 short | 153 B | 1.21 s | 126 B/s | Balanced voice, short text |
| `bulk` | 8PSK 3/4 short | 708 B | 2.17 s | 326 B/s | Quick voice, medium text |
| `fast` | 8PSK 3/4 normal | 1476 B | 3.68 s | 401 B/s | Files |

`LinkHealthTracker` monitors dual-EMA burst delivery, SNR, and jitter with mode inertia, hysteresis deadbands, and slew-rate crash detection to automatically step down to `Fine + 700C` on rough/fading links and step back up when headroom returns.

---

## Local Lab Simulation (UDP)

For testing two stations on one machine without radio hardware, `app.py` automatically starts an in-process UDP hub when `--soundcard` is not set (or you can run `python3 hub.py` separately):

```bash
# Terminal 1 (real aicodix OFDM audio over UDP)
python3 app.py --call ALPHA --port 5001 --modem --debug-tone 800

# Terminal 2
python3 app.py --call BRAVO --port 5002 --modem --debug-tone 1200
```

Omit `--modem` to ship raw protocol bursts over UDP for rapid protocol testing. The **Advanced & Channel Simulator** drawer lets you inject AWGN SNR, static packet loss, and Rayleigh-like time-window fades.

---

## Verification

```bash
python3 tests/test_unit.py        # protocol, smoke, security, VOX, link-health, PSK
python3 tests/test_transport.py   # real aicodix SNR cliff & fade curve (requires numpy)
python3 tests/test_e2e.py         # two-station voice, text, file, and ARQ repair
```

---

## Credits & Upstream Tools

`crowcall` stands on the shoulders of open-source radio and DSP engineering:

* **[aicodix](https://github.com/aicodix) (`modem`, `code`, `dsp`, `disorders`)** by **Ahmet Inan** (`inan@aicodix.de`) — provides the underlying OFDM audio modem, BCH frame headers, systematic polar-coded FEC, Schmidl-Cox synchronization, DSP building blocks, and channel impairment tools.
* **[Codec2](https://github.com/drowe67/codec2)** by **David Rowe (VK5DGR)** and contributors — provides the ultra-low-bitrate open-source speech codec (`700C`–`3200` bps) that makes voice messaging viable over narrowband analog radio channels.
* **[Silero VAD](https://github.com/snakers4/silero-vad)** (`pysilero-vad`) — optional voice activity detection used to strip dead air before encoding.
* **[FFmpeg](https://ffmpeg.org/)**, **[NumPy](https://numpy.org/)**, **[sounddevice](https://python-sounddevice.readthedocs.io/)** (PortAudio), and **[Flask](https://flask.palletsprojects.com/) / [Flask-SocketIO](https://flask-socketio.readthedocs.io/)** — audio transcoding, array DSP, soundcard I/O, and the offline-capable local web UI.
