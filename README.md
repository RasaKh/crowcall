# crowcall

**Off-grid, store-and-forward voice, text, and file messenger for analog radios.**

`crowcall` turns a laptop or Raspberry Pi (and any phone browser on its local Wi-Fi) into a digital radio terminal over ordinary analog radios—VHF/UHF handhelds, CB, or HF/SSB—using **Codec2** speech compression and the **aicodix** OFDM/polar-coded audio modem. It is built for grid-down and zero-infrastructure environments where there is no internet, cellular network, or repeater infrastructure.

---

## Design Philosophy: Minimal Air Time & Effective Links

On radio, **every extra second on the air increases detectability, battery drain, and collision risk**. `crowcall` is designed around a deliberate balance: keep RF transmissions as short and infrequent as possible while still guaranteeing reliable delivery over noisy or fading channels.

* **Silero VAD Silence Stripping (`pysilero-vad`):** Removes dead air and pauses at 16 kHz before encoding, often cutting voice air time by 30–50% without clipping word edges (and safely falling back to untrimmed audio if no speech is detected).
* **Codec2 + Opportunistic Compression:** Voice is encoded at `700C`–`3200` bps (`100`–`400` B/s), and text/files are `zlib`-compressed only when compression actually shrinks the payload—never wasting a burst on already-compressed data.
* **Selective ARQ & Interleaving:** Bursts are sent in interleaved order so short fades scatter rather than wipe out contiguous speech, and repair (`T_RREQ` / `T_RACK` / `T_DONE`) retransmits only the exact missing chunks rather than the whole message.
* **Adaptive Link Health (`LinkHealthTracker`):** All four `aicodix` PSK profiles share the ~9.9 dB acquisition floor. On clean links, `crowcall` steps up to `Quick` (`bulk`, ~0.5x realtime) to minimize transmit duration; when dual-EMA telemetry detects fading or a sudden SNR drop, it steps down to `Fine` (`control`) + `700C` to shrink lost-chunk holes and recover cleanly.

---

## Capabilities & Operation Modes

* **Analog Radio Soundcard Mode (`--soundcard`):** Connects to any radio via 3.5 mm audio cable or acoustic coupling. Includes half-duplex RX self-mute during TX, live RX VU meter with clipping alerts, software RX AGC, TX `tanh` soft-limiting, optional pilot-tone/VOX calibration, and automatic USB audio hot-plug recovery.
* **Simplex Store-and-Forward Repeater (`--repeater`):** Turns a single radio + laptop/Pi into a single-frequency digipeater that relays complete messages across terrain obstacles while preserving original `msg_id`, `origin`, and a 2-bit hop limit (`MAX_HOPS = 3`).
* **Encrypted Channel Mode (`--key`):** Zero-overhead Encrypt-then-MAC (`BLAKE2s` counter-mode stream cipher + 32-bit truncated `HMAC-SHA256`) using a pre-shared passphrase.
* **Fire-and-Forget Delivery (`auto_retry`) & Reboot Resilience:** Unconfirmed messages retry with exponential backoff, wake immediately when any peer traffic or beacon is heard, and hydrate the outbox from `recordings/` on startup so repair requests survive power loss.
* **Sneakernet / Zero-Cable Mode:** Export any outbox message as a ready-to-play 48 kHz `.wav` (`/api/tx_wav/<mid>`) or upload recorded radio audio (`/api/rx_wav`) to decode offline.
* **Field Appliance Web UI (`--host 0.0.0.0`):** Accessible from smartphones over a local hotspot with hold-to-PTT or tap-to-latch voice recording, mobile HTTP mic capture fallback, 1-click `[Check-In: OK]` / `[! Need Help]` bursts, nearby-station roster, message history hydration, and **Dusk / Daylight / Night Red** themes.
* **Local Lab Simulator (`--modem` / raw UDP):** In-process auto-hub with real-time AWGN SNR, static loss, and time-window fade simulation for testing without RF hardware.

---

## Example Scenarios

* **Low-Profile Tactical Check-In:** Send `[CHECK-IN] Status OK` or a short `700C` + Silero VAD voice note in 1–2 bursts (~1.2–2.4 s on air) for minimal RF signature and rapid confirmation via `T_DONE`.
* **Ridge / Hilltop Relay:** Place a battery-powered Pi + handheld radio on high ground with `python3 app.py --call RELAY1 --soundcard --repeater` to bridge two stations blocked by terrain on a single simplex channel.
* **Private Team Net:** Launch stations with `--key "team-passphrase"` (or set it in the UI) so unauthorized listeners cannot decode or inject bursts, with zero extra header bytes.
* **Zero-Cable Phone + Walkie-Talkie:** Download `tx.wav` from the web UI onto a phone, play it into a handheld radio's microphone, and decode incoming recordings later via `/api/rx_wav`.
* **Offline Lab Testing:** Run two stations locally with `--modem` to observe real `aicodix` decode behavior across the 9.5 dB SNR cliff and multi-second fades.

---

## Dependencies & Build

```bash
sudo apt install codec2 ffmpeg g++ make
pip install flask flask-socketio numpy sounddevice   # pysilero-vad is optional

git clone --recurse-submodules https://github.com/RasaKh/crowcall.git
cd crowcall
make
```

> `vendor/{code,dsp,modem,disorders}` are git submodules tracking the upstream `aicodix` repositories. Running `make` initializes submodules (if needed) and builds `encode`/`decode` and the channel disorder tools. `AicodixModem` also auto-compiles `vendor/modem` on first run if binaries are missing.

### Quick Start
```bash
# Real radio via soundcard (accessible from LAN/phone hotspot)
python3 app.py --call ALPHA --soundcard --host 0.0.0.0

# Local two-station simulation with real aicodix OFDM audio over UDP
python3 app.py --call ALPHA --port 5001 --modem --debug-tone 800
python3 app.py --call BRAVO --port 5002 --modem --debug-tone 1200
```

---

## Modem Profiles

All four profiles sit on the measured **~9.9 dB in-band SNR acquisition floor** using constant-envelope PSK:

| Profile | Aicodix Mode | Usable Payload | Burst Air Time | Throughput | Role |
|---|---|---|---|---|---|
| `control` | QPSK 1/2 short | 68 B | 1.21 s | 56 B/s | Control (`RREQ`/`RACK`/`DONE`/`BEACON`), Fine voice |
| `compact` | QPSK 5/6 short | 153 B | 1.21 s | 126 B/s | Balanced voice, short text |
| `bulk` | 8PSK 3/4 short | 708 B | 2.17 s | 326 B/s | Quick voice, medium text |
| `fast` | 8PSK 3/4 normal | 1476 B | 3.68 s | 401 B/s | Files |

---

## Under the Hood: Adaptive Link Control & Protocol Design

### Control-Theory Link Adaptation (`LinkHealthTracker`)
Because all four `aicodix` PSK profiles share the ~9.9 dB AWGN floor, mode switching doesn't trade raw sensitivity—it trades **burst duration & hole size** against **RF air time**. Shorter bursts (`control` / `compact`, 1.21 s) slip between multipath fades and shrink lost-speech holes, while longer bursts (`bulk` 2.17 s, `fast` 3.68 s) cut total transmit time roughly in half on clean links.

To adapt without mode-hopping, `LinkHealthTracker` in `protocol.py` implements a dual-EMA hysteresis controller:

* **Dual-EMA Slew-Rate Crash Detection:** Tracks both a **Slow EMA** ($\alpha = 0.15$, long-term baseline) and a **Fast EMA** ($\alpha = 0.50$, short-term transient) for delivery ratio and SNR, alongside SNR volatility (`snr_dev_ema`). A steep negative slew (`delivery_ema - fast_delivery_ema >= 0.28`), 3 consecutive burst drops, or `fast_snr_ema <= 10.3 dB` triggers `downshift_urgency`—bypassing dwell timers (`0 s`) to step down immediately before a transfer collapses.
* **Mode Inertia (Tenure Stickiness) & Hysteresis Deadbands:** As the current mode accumulates consecutive healthy bursts (`tenure_streak`), it earns `mode_inertia` (`0.0`–`1.0`), widening the hysteresis deadbands (up to `+6%` delivery and `+1.5 dB` SNR) and stretching upshift dwell time from `60 s` to `150 s` so a proven mode ignores minor fluctuations.
* **High-Headroom Switching Incentive:** When link headroom is overwhelmingly high (`snr_ema >= 19.0 dB`, jitter `< 1.2 dB`, `fast_delivery_ema >= 0.97`), `upshift_incentive` overrides inertia and halves upshift dwell to `30 s` so stations don't linger in slow modes on clear channels.
* **Bidirectional Feedback & Zero-Sync Switching:** Transmit-only stations still adapt because inbound `T_RREQ` loss counts and `T_DONE` receipts feed the tracker (with automatic safe fallback to `Fine + 700C` after two unconfirmed sends). Because every burst's BCH + v3 header is self-describing and `outbox` snapshots the mode when queued, sender and receiver never fall out of sync mid-message.

### Additional Engineering Details
* **Split TX Locks (`_data_tx_lock` vs. `_tx_lock`):** Multi-chunk transfers hold `_data_tx_lock` while `_send()` acquires `_tx_lock` only per burst, allowing urgent `T_RREQ`, `T_RACK`, and `T_DONE` control bursts to slip in between data bursts during pacing gaps.
* **Arithmetic Finish-Time Prediction:** Rather than guessing end-of-transmission from silence, `Assembler.expected_duration()` computes `expect_until` from `total * modem.air_s` carried in every chunk header, preventing premature repair requests during slow `fast`-mode transfers.
* **Prefix-First & Prefix-Last Data Ordering:** `data_order()` transmits chunk `0` (which holds exact byte length, filename, and CRC32) both first and last on multi-chunk transfers so a fade at either end doesn't strand an intact payload.
* **Zero-Overhead Encrypt-then-MAC (`--key`):** Uses the 60-byte header (with CRC zeroed) as the per-burst nonce for a `BLAKE2s` counter-mode keystream and replaces CRC32 with a 32-bit truncated `HMAC-SHA256`—adding 0 bytes of wire overhead while binding `ptype`, `origin`, `destination`, `msg_id`, `chunk_idx`, and `hops`.

### Selective ARQ, Packet Requests & Voice/File Repair
* **Addressed Repair Requests (`T_RREQ`):** Once `expect_until` (or `stall_timeout`) passes and the channel is quiet for `quiet_timeout` (`max(3.0, 3.0 * modem.air_s)`), the receiver sends a single `control` burst (`T_RREQ`) addressed to the origin station (`RQ3` + target `station_id`). `encode_missing()` automatically picks the smaller of a **2-byte index list** (`RREQ_LIST`, cheapest for sparse loss) or a **bitmask** (`RREQ_BITMAP`, covering hundreds of chunks in one burst under heavy loss).
* **`T_RACK`, `T_RNAK` & `T_DONE` Handshake:** Unrelated stations stay silent when they overhear a `T_RREQ` for someone else. The origin replies with `T_RACK` and resends only the requested chunks, or sends `T_RNAK` (if declined or evicted from `outbox`) so the receiver finalizes immediately instead of hanging. When all chunks arrive, the receiver sends `T_DONE` so the sender marks the message `CONFIRMED`.
* **Opportunistic Scavenging & Late Retry:** `Station.inbox` keys transfers by `(origin, msg_id)`, so any overheard repair or repeater chunks automatically heal every listening station's partial `Assembler`. If a transfer finalizes degraded while out of range, clicking **Ask for missing** (`POST /api/retry/<mid>`) requests only the remaining chunks later without restarting from scratch.
* **Voice Concealment vs. File Integrity:** Missing voice chunks are concealed by repeating the last received Codec2 frame (a held sound rather than a click), whereas missing text/file chunks are zero-filled and checked against the `STREAM_FMT` CRC32 so a damaged file is never presented as intact.

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
* **[Silero VAD](https://github.com/snakers4/silero-vad)** (`pysilero-vad`) — optional neural voice activity detection used to strip dead air before encoding.
* **[FFmpeg](https://ffmpeg.org/)**, **[NumPy](https://numpy.org/)**, **[sounddevice](https://python-sounddevice.readthedocs.io/)** (PortAudio), and **[Flask](https://flask.palletsprojects.com/) / [Flask-SocketIO](https://flask-socketio.readthedocs.io/)** — audio transcoding, array DSP, soundcard I/O, and the offline-capable local web UI.
