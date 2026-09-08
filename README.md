# crowcall

> **Protocol update:** the implementation now emits protocol version 3
> frames. They carry a complete source/origin/destination identity, explicit
> body length, and CRC32. Version-2 recordings remain readable as files, but
> v2 stations are not wire-compatible with this release. Use the audit and
> architecture handoff for the migration and physical-radio qualification
> plan.

Voice messages over radio. Record, and it goes out as codec2-compressed
chunks the receiver can ask to have re-sent if a fade eats some.

Two transports, same protocol above them:

* **raw bursts over UDP** — fast to iterate on, packet-level loss simulation
* **real aicodix modem over UDP** — UDP carries modulated *audio*, the real
  OFDM/polar-coded modem decides whether each burst survives

## Run it

```bash
pip install flask flask-socketio pysilero-vad     # vad is optional
sudo apt install codec2 ffmpeg

python3 hub.py                                     # terminal 1: the channel
python3 app.py --call ALPHA --port 5001            # terminal 2
python3 app.py --call BRAVO --port 5002            # terminal 3
```

Open `http://127.0.0.1:5001` and `http://127.0.0.1:5002`. Hold the record
button (or hold space), release, and it arrives in the other.

For the real modem in the loop:

```bash
cd ~/crowcall/vendor/modem && make CXX='g++ -march=native'
python3 app.py --call ALPHA --port 5001 --modem
```

To make the transmitting station audible during modem testing, add a short
diagnostic prefix to every burst. Choose a different frequency per station:

```bash
python3 app.py --call ALPHA --port 5001 --modem --debug-tone 800
python3 app.py --call BRAVO --port 5002 --modem --debug-tone 1200
```

The tone is transmitted and retained in captured burst WAVs, then stripped
before modem demodulation. It is off by default (`--debug-tone 0`). This
option applies to `--modem` and `--soundcard`; raw-burst UDP has no audio on
the wire, though `--render-modem --save-modem` includes the prefix in local
rendered recordings.

The channel knob then reads in **dB of SNR** instead of packet loss, with the
cliff marked at 9.5 dB. Drag it below that and nothing decodes at all — which
is the real behaviour, not a simulation of it.

```bash
python3 test_integration.py    # voice: two stations, forced loss, repair
python3 test_data.py           # text and files, including a corrupted one
python3 test_transport.py      # fragmentation + SNR curve, no aicodix needed
python3 test_smoke.py          # constructs what startup constructs
```

Run `test_smoke.py` after any refactor. The others all pass without ever
building a real `AicodixModem`, so a broken constructor compiles clean, passes
everything, and only shows up when you launch.

## Text and files

The same protocol carries text and arbitrary files over either transport.
Compose either in the left column; the cost is shown before you commit.

For non-voice payloads the usable capacity depends on the selected profile;
the version-3 header is 60 bytes:

**153 bytes / 1.21 s = 126 bytes/sec on the compact profile.**

| payload | bursts | air time |
|---|---|---|
| 200-char text | 1 | 1.2 s |
| 2 KB | 10 | 12 s |
| 10 KB text (compresses ~3x) | 15 | 18 s |
| 100 KB | 435 | **8 min 46 s** |
| 1 MB | 4445 | **1 h 29 min** |

Roughly 1980s packet-radio territory. Text is comfortable, small files are a
coffee break, media is out of the question. Every repair round adds more, so
a large transfer over a marginal link may simply never finish.

Three things differ from voice:

**Compression is on by default, and abandoned when it does not help.** Text
and code shrink 3-4x. Already-compressed input (jpeg, zip, mp3) grows
slightly under zlib, so the encoder compares and keeps the smaller -- on a
195 B/s link a wasted burst is 1.2 seconds.

**Padding is fatal.** A concealed voice chunk is a held sound; a padded file
is a corrupt file. So a data transfer carries a self-describing prefix --
exact lengths, CRC32, filename -- at the head of the reassembled stream.

**A hole means broken, not rough.** There is no concealing a gap in a file,
so missing chunks are left as zeros, the CRC is checked rather than assumed,
and the UI says "damaged" instead of handing over something that looks whole.
The byte ranges that never arrived are reported.

That prefix lives in chunk 0, which makes it the one chunk that matters more
than the others: without it the receiver cannot tell the length, the checksum
or the filename. So data transfers send chunk 0 first *and* again last -- one
extra burst on a transfer of hundreds. If it still never lands, the raw bytes
are archived as `stream.raw` and the message is reported as unrecognised
rather than silently mis-parsed.

Payload kind rides in the flags byte, and every frame is checksummed. The
version-3 header also carries source, origin, destination, exchange, and body
length fields.

## Saved messages

Every message is archived in all three of its forms, one directory per
message under `recordings/`:

```
recordings/20260730T142530Z-rx-ALPHA-a3f2/
  meta.json          mode, chunk map, loss, repair rounds, SNR, air time
  speech.wav         8 kHz mono - what a person hears
  speech.pcm         the same samples raw, for c2enc or a plot
  message.c2         the codec2 bytes as carried on air
  chunks/chunk-000.wav   modulated audio per burst
  chunks/failed-003.wav  bursts that did not decode, kept anyway
```

Each message row in the UI links to `wav`, `pcm`, `c2`, and a `zip` of
everything. To decode by hand you need the mode, because raw codec2 has no
header and nothing can infer the frame size - `meta.json` spells out the
command:

```bash
c2dec 1400 message.c2 out.raw
```

Received `message.c2` is longer than what the sender encoded: it is
`total_chunks x payload`, with the final chunk padded and any missing chunks
filled by repeating the last frame received.

The failed-burst recordings are the useful ones for tuning. They are the
audio exactly as it arrived at the moment the FEC gave up, so you can listen
to, or plot, what a dead link actually looks like.

Modem audio is ~100 kB per burst against 231 bytes of codec2, so it is a
separate switch (`--save-modem`, off by default). The archive prunes oldest
first past 2 GB.

```bash
python3 app.py --call ALPHA --port 5001 --modem --save-modem
python3 app.py --call ALPHA --port 5001 --no-save          # archive nothing
```

## Modes

aicodix has 64 modes. **22 of them need exactly the same minimum signal —
about 9.9 dB in-band SNR.** Below that nothing decodes; at or above it they
all decode equally well. Within those 22, throughput spans 6.7×.

So there is a free tier, and the only sane policy is to take the fastest
mode in it that suits the job. All four below are on that floor, and all four
are constant-envelope:

| role | aicodix | usable | air | throughput |
|---|---|---|---|---|
| control | QPSK 1/2 short | 68 B | 1.21 s | 56 B/s |
| voice, short text | QPSK 5/6 short | 153 B | 1.21 s | 126 B/s |
| long text | 8PSK 3/4 short | 708 B | 2.17 s | 326 B/s |
| files | 8PSK 3/4 normal | 1476 B | 3.68 s | **401 B/s** |

**Control** gets the shortest burst that exists anywhere plus the most error
correction available at that length. Payload is irrelevant here — 108 B holds
a loss map for an 864-chunk message, which is a 1.3 MB file.

**Voice** uses the same 1.09 s burst with more payload. With codec2-1400 that
is 27 frames = **1.08 s of speech per chunk**, so a lost chunk is a 1.08 s
hole. Costs 1.12× realtime, which is fine for store-and-forward.

**Files** get the fastest mode on the floor. 100 KB drops from 8m 47s to
**4m 14s**. Long frames amortise the fixed preamble and PTT gap over more
payload, which is where the last 20% comes from.

Text picks by length: a short message in a big burst is a postcard in a
shipping container.

### Voice is the one you choose

Everything else optimises for throughput and has a single right answer. Voice
trades **hole size against air time**, and which matters more depends on the
link and on you:

| | aicodix | a lost chunk costs | air time |
|---|---|---|---|
| **Fine** | QPSK 1/2 short | 0.60 s | 2.02x realtime |
| **Balanced** *(default)* | QPSK 5/6 short | 1.08 s | 1.12x realtime |
| **Quick** | 8PSK 3/4 short | 4.24 s | 0.51x realtime |

For 20 seconds of speech that is 34 chunks / 41 s, 19 chunks / 23 s, or
5 chunks / 11 s.

**All three are on the acquisition floor**, so this is not a robustness
choice — picking Fine does not make the link survive worse conditions. It
makes the damage smaller when conditions are already bad enough to lose
chunks. Pick Quick when the path is good and you want it over with; pick Fine
when you are already seeing losses and a swallowed sentence is worse than a
long transmission.

```bash
python3 app.py --call ALPHA --port 5001 --voice-mode fine
```

Switchable live in the UI. The mode travels in every chunk header, so the
receiver follows whatever the sender chose — the selector only affects what
*you* transmit.

Every message row shows the aicodix mode it was sent with, and hovering gives
the full picture: burst size, codec2 rate, frames per chunk, speech per chunk,
realtime ratio, and any frame padding waste.

### What we are not using, and why

| | |
|---|---|
| QAM16 1/2 short | the old default — only **91.7%** reliable at the floor |
| QAM16 2/3 short | needs ~12 dB |
| QAM64 1/2 short | needs ~13 dB, and dies completely under harsh multipath |
| QAM256 1/2 short | needs ~17 dB, same multipath failure |
| QAM4096 3/4 and up | failed even at 31 dB — unusable |

Constant envelope gains nothing against the *measured* channel: QAM16
survived the harshest multipath setting intact, and the real dividing line
was constellation density at QAM64. But the `disorders` tools model the
channel and contain no amplifier, so they cannot test what a real radio's ALC
and non-linear PA do to an amplitude-modulated signal. Here that insurance is
free — the fastest mode on the floor happens to be 8PSK anyway. Bonus: every
mode wants the same transmit level.

## The numbers

Burst size is no longer a constant; it comes from the mode. The receiver is
never told which mode was used — aicodix `decode` reads it out of its own
BCH-coded frame header — and chunk size is learned from the first body that
arrives, so mismatched stations still interoperate.

| | |
|---|---|
| acquisition floor | ~9.9 dB in-band SNR, sharp |
| awgn param → SNR | `SNR_dB = 0.9 - param` (constant to 0.44 dB over 31 dB) |
| encoder padding | ~1.0 s lead + ~1.0 s tail, trimmed away |
| trim margin | 80 ms proven safe either side |
| multipath `FACTOR` | **runs backwards** — 1 is severe, 40 is nearly harmless |

Codec2 mode is a runtime choice carried in the header:

| mode | frames/chunk | speech/chunk | vs realtime |
|---|---|---|---|
| 700C | 48 | 1.92 s | 0.63× |
| 1200 | 32 | 1.28 s | 0.95× |
| **1400** | 27 | **1.08 s** | 1.12× ← default |
| 1600 | 24 | 0.96 s | 1.26× |

1400 is the default: audibly better than 700C, and small chunks are what
matters for voice because a lost one is a hole you have to listen to.

## Why it works the way it does

**Silence is stripped before encoding.** Dead air costs exactly as much air
time as speech, so removing it removes whole chunks. It is also the one stage
that can be wrong about your voice, so it never fails the send: no VAD
installed, or nothing recognised as speech, and the untrimmed audio goes out
instead.

**No separate message-header packet.** Every chunk repeats callsign, message
id, total count and mode. A link that fails by going completely dark rather
than degrading gracefully is the wrong place for a single point of failure.

**Chunks transmit interleaved.** A fade punches scattered short holes instead
of one contiguous crater. Several short gaps stay intelligible; one long one
does not.

**Missing chunks are concealed by repeating the last frame received**, not by
silence — a held sound rather than a click.

**Repair is a request, not a demand.** The receiver asks for specific chunks;
the sender's operator can agree, decline, or say nothing. All three paths end
with the receiver playing what it has.

**Roger beeps are generated at playback**, never transmitted, so the flavour
costs no air time.

## Files

| | |
|---|---|
| `modes.py` | the four measured modem modes and which job each serves |
| `hub.py` | the shared medium: relays every datagram to everyone else |
| `archive.py` | saves all three forms of each message, plus metadata |
| `test_data.py` | text and file transfer, including a deliberately corrupted one |
| `protocol.py` | wire format, per-mode geometry, chunking, reassembly, concealment |
| `codec.py` | browser audio ↔ VAD ↔ 8 kHz PCM ↔ codec2, plus playback beeps |
| `transport.py` | raw-burst UDP transport with packet-level loss |
| `modem.py` | aicodix wrapper: burst ↔ audio, trimming, burst detector |
| `radio.py` | modulated audio over UDP with an audio-domain channel |
| `app.py` | one station: recording, transmit pacing, repair state machine |
| `static/index.html` | the UI |

## Tuning it

Every number below was measured on real hardware. Re-measure rather than guess
if you change modulation, frame size, or sample rate — `step4_calibrate.py`
does the sweeps.

### Air time — `protocol.py`

| | | |
|---|---|---|
| `AIR_TIME_S` | 1.09 | Trimmed signal span of one burst at QAM16 rate 1/2 short. The encoder emits 3.09 s of file, of which 1.00 s is leading silence and 0.99 s trailing. BPSK measured 1.64 s, so this changes with modulation. |
| `PTT_GAP_S` | 0.12 | Silence between bursts. PTT settling time on a radio; on UDP it is what lets an energy framer separate bursts. This sits *exactly* on the boundary where a 50 ms detector hold still works — raise it toward 200 ms before real hardware. |

Everything else derives from these two, so a wrong value here quietly corrupts
every estimate and every timer.

### Receiver patience — `protocol.Assembler`

**Every threshold scales with the burst length of the message it belongs to.**
They are properties of the `Assembler`, not constants, because burst length now
ranges from 1.21 s to 3.68 s depending on the mode:

| | | |
|---|---|---|
| `quiet_timeout` | `max(3 s, 3 x burst)` | Silence before the receiver concludes the sender finished. 3x the burst interval, so an ordinary gap between two bursts never looks like the end of a message. |
| `final_timeout` | `2 x quiet + 10 s` | Hard stop; play or write what arrived. Must clear a whole repair exchange or a repair in flight gets cut off. An explicit `RNAK` short-circuits it. |
| `max_rounds` | 3 for voice, `min(8, 3 + chunks/50)` for data | A big file has more chunks to lose and each round fixes only so many. Voice stays at 3: past that, a message with holes beats a message that never plays. |

### The receiver does not guess when a transmission ends

Scaled timeouts alone are still guessing from silence. The receiver has better
information: **`total` is in every chunk header and the burst duration is
known**, so the finish time is arithmetic.

```
expect_until = first_chunk_arrived + (total + trailer) x burst_air
```

No repair request is even considered before that time. Asking for repair
pushes the deadline out again, since the sender then owes that many more
bursts. The only override is `stall_timeout` (3x quiet), so a sender that dies
mid-transfer does not make the receiver wait out the full expected duration —
42 minutes for a 1 MB file.

**Why this matters.** With fixed timeouts (3 / 15 / 28 s), a file on the 3.68 s
burst had an *ordinary gap between two healthy bursts* that exceeded the 3 s
quiet timeout. The receiver fired a repair request between every single burst,
interrupting the sender to service requests for chunks it had not sent yet,
burned its three rounds in ~23 s, and finalised a 68-chunk file with 6 chunks.

Anything measured in "how long since I heard something" must be expressed in
burst times, never in seconds — and preferably not measured that way at all
when the finish time can be computed.

`test_data.py` runs this **with pacing on**, because the first version of the
regression test ran with `pacing=False`: bursts went out instantly, the gaps
never existed, and it passed against the broken code. Sabotaging the fix now
makes it report 2 spurious repairs against 0.

### Channel — `radio.py`

| | | |
|---|---|---|
| `CLIFF_DB` | 9.5 | In-band SNR below which nothing decodes. Sharp: at 1 dB steps the modem went 100% → 0% between 9.9 and 9.0 dB with no transition region. |
| `BAND_LO/HI` | 300–2700 Hz | 2400 Hz centred on 1500 Hz. Chosen to fit an SSB radio's audio passband — the reason this works over HF gear at all. Noise outside it is filtered away, which is why SNR is always *in-band*. |
| `FRAG_PAYLOAD` | 8192 | Audio bytes per datagram. A 1.17 s burst is ~112 KB against a 65,507-byte UDP ceiling, so it fragments. Lower toward 1400 for a real network. **The hub must be able to receive whatever this is** — it read 2048 once and silently truncated every fragment. |
| `STREAM_TTL` | 6.0 s | How long a half-arrived burst waits before being decoded anyway. ~5 burst times. The FEC will almost never survive a hole, but trying is free and the failure is reported honestly. |
| `snr_db` | 20 | What the receiver sees. 20 comfortable, 10 marginal, under 9.5 dead. |
| `fade_prob` | 0 | Probability per burst that a fade *starts* — not per-packet loss. A fade blacks out a time window, which is what happens on HF and what repair exists for. Independent packet loss is the wrong model. |
| `fade_ms` | 3000 | Blackout length. 3 s is ~2.5 bursts, so one fade typically eats two or three chunks. |

The `awgn` parameter of the disorders tools converts as:

```
in-band SNR (dB) = 0.9 - awgn_param
```

constant to within 0.44 dB across a 31 dB sweep. So `awgn -20` is a clean
21 dB channel and `awgn -9` sits right on the cliff.

### Why modulation is not adjustable

All 120 decode failures in testing were **sync** loss, never payload loss, and
every modulation died at the same SNR — including QAM16, which carries four
bits per symbol and should be far more fragile than BPSK. The shared header and
preamble fail first and take everything with them.

So modulation buys throughput and no robustness at all, and the right choice is
always the fastest one. That deleted an entire adaptive-modulation subsystem.

The flip side: **this is not a weak-signal mode.** It needs comfortable
armchair-copy signal. FT8 and WSPR work 20+ dB below it. The link is either up
or flatly down, with no graceful middle — which is why repair exists to recover
chunks lost inside a fade window rather than to nibble at a steady error rate.

## Interface

Three columns: compose on the left, traffic in the middle, instruments on the
right.

Downloads under each message match what that message actually has — `wav`,
`pcm`, `c2` for voice; `txt` for text; the file itself for a file; `raw` for a
transfer whose prefix chunk never arrived. `zip` is always the whole record.

## Known limits

**Memory is bounded, disk is the durable copy.** A station evicts sent
messages after 20 minutes or 40 messages, finalised inbound ones after 5
minutes, and caps decoded audio/text/files at 60 in RAM. Everything is already
written to `recordings/`, so eviction costs nothing but a UI download link
going stale on very old messages. A repair request for an evicted message gets
an explicit `RNAK` rather than silence, so the far end fails fast.

**msg_id is 16 bits.** Two stations can hold the same id simultaneously, which
is why the inbox is keyed by `(station, msg_id)` and repair answers match on
both. With one station, a collision needs ~300 messages in flight to be likely.

## Loose ends

`modem.py` also carries a `BurstDetector` for slicing bursts out of a
continuous audio stream. It is not used by the UDP transport — one datagram
is one burst, so framing is free — but it is what a real soundcard path
would need, since aicodix `decode` only ever pulls the first datagram out of
a stream.

From the measurement work: the `cfo`/`sfo` tools reported zero effect up to
800 Hz and 500 ppm, which is surprising enough to be worth confirming they
are not silently no-oping.
