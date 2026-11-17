#!/usr/bin/env python3
"""
crowcall station
================

One instance = one radio. Run two on different ports, point both at the
hub, and they can talk.

    python3 hub.py                            # terminal 1
    python3 app.py --call ALPHA --port 5001   # terminal 2
    python3 app.py --call BRAVO --port 5002   # terminal 3

Transmission is paced at the measured air time (1.09 s per 256 B burst plus
a 120 ms PTT gap) even over loopback, because the whole point is to feel
like radio -- and because a repair protocol you cannot watch happening is a
repair protocol you cannot debug.
"""

from __future__ import annotations

import argparse
import os
import random
import threading
import time
import re

from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO

import queue

import archive
import codec
import modem as MODEM
import modes as M
import protocol as P
from transport import HubTransport
from config import ConfigStore

# ---------------------------------------------------------------------------
# TUNABLES
# ---------------------------------------------------------------------------
OUTBOX_TTL = 20 * 60     # long enough to answer a late repair request
OUTBOX_MAX = 40
INBOX_TTL = 5 * 60       # finalised assemblers, kept briefly for the UI
PENDING_TTL = 10 * 60    # repair prompts an operator never answered
PAYLOAD_MAX = 60         # decoded audio / text / files held in memory
INBOX_MAX = 64            # concurrent inbound transfers retained in memory

QUIET_TIMEOUT = 3.0      # floor for the silence-means-finished test
REPAIR_WAIT = 15.0       # floor for waiting on a repair that may never come
FINAL_TIMEOUT = 28.0     # floor for giving up
MAX_ROUNDS = 3           # floor; data transfers get more

app = Flask(__name__, static_folder="static")
app.config["SECRET_KEY"] = os.urandom(16).hex()
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
sio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

DEMO_MODE = False
DEMO_MAX_AUDIO_BYTES = 512 * 1024
DEMO_MAX_FILE_BYTES = 16 * 1024
DEMO_MAX_TEXT_BYTES = 4096
SAMPLE_VOICE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "static", "sample_voice.wav"
)


def _bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ValueError("expected boolean")


class Station:
    def __init__(self, callsign, hub_addr, pacing=True, mode=P.DEFAULT_MODE,
                 use_modem=False, use_soundcard=False, vendor=None, use_vad=True,
                 save=True, save_modem=False, rec_root="recordings",
                 render_modem=False, voice_modem=None, config_path=None,
                 debug_tone=None, debug_tone_ms=120, vox_slack_ms=60,
                 repeater=False, auto_adapt=True, auto_retry=True,
                 channel_key=None):
        self.call = callsign.upper()[:8]
        self.pacing = pacing
        self.auto_repair = True
        self.auto_adapt = bool(auto_adapt)
        self.auto_retry = bool(auto_retry)
        self.repeater = bool(repeater)
        self.mode = mode if mode in P.MODES else P.DEFAULT_MODE
        self.use_vad = use_vad
        self.vendor = vendor
        self.voice_modem = M.for_voice(voice_modem).name
        self.debug_tone_hz = None if debug_tone in (None, 0, 0.0) else float(debug_tone)
        self.debug_tone_ms = max(40, min(500, int(debug_tone_ms)))
        self.vox_slack_ms = max(20, min(250, int(vox_slack_ms)))
        if self.debug_tone_hz is not None:
            MODEM.debug_tone(self.debug_tone_hz)
        if channel_key:
            P.set_channel_key(channel_key)
        self.archive = archive.Archive(rec_root, save=save,
                                      save_modem=save_modem)
        self.render_modem = render_modem
        self._render_q: queue.Queue = queue.Queue(maxsize=64)
        self._render_modem_obj = None
        self.render_error = None
        self.outbox: dict[int, dict] = {}
        self.inbox: dict[tuple, P.Assembler] = {}
        self.audio: dict[str, bytes] = {}
        self.texts: dict[str, str] = {}
        self.files: dict[str, tuple[str, bytes, bool]] = {}
        self.pending: dict[str, dict] = {}
        self._repeated: set[tuple[int, int]] = set()
        self.health = P.LinkHealthTracker()
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._data_tx_lock = threading.Lock()
        self._tx_lock = threading.RLock()
        self._tx_active = False

        self.last_activity = time.time()
        self.config_store = ConfigStore(config_path)

        if use_soundcard:
            import soundcard
            self.modem = MODEM.AicodixModem(vendor=vendor, callsign=self.call)
            self.tx = soundcard.SoundcardTransport(
                self.modem, on_recv=self.on_packet,
                on_drop=self.on_drop,
                on_tx_audio=self._archive_tx_audio,
                on_rx_audio=self._archive_rx_audio,
                debug_tone_hz=self.debug_tone_hz,
                debug_tone_s=self.debug_tone_ms / 1000.0)
            self.transport_kind = "soundcard"
            self._render_modem_obj = self.modem
        elif use_modem:
            import radio
            self.modem = MODEM.AicodixModem(vendor=vendor, callsign=self.call)
            self.tx = radio.ModemUdpTransport(
                hub_addr, self.modem, on_recv=self.on_packet,
                on_drop=self.on_drop,
                on_tx_audio=self._archive_tx_audio,
                on_rx_audio=self._archive_rx_audio,
                debug_tone_hz=self.debug_tone_hz)
            self.transport_kind = "modem"
            self._render_modem_obj = self.modem
        else:
            self.modem = None
            self.tx = HubTransport(hub_addr, on_recv=self.on_packet,
                                   on_drop=self.on_drop)
            self.transport_kind = "hub"

        self._hydrate_outbox()
        threading.Thread(target=self._monitor, daemon=True).start()
        threading.Thread(target=self._beacon, daemon=True).start()
        threading.Thread(target=self._render_worker, daemon=True).start()

    def _hydrate_outbox(self):
        """Reload recent TX messages from disk so repair requests survive restarts."""
        now = time.time()
        try:
            recs = self.archive.list()[:20]
        except Exception:
            return
        for rec in recs:
            meta = rec.get("meta") or {}
            if meta.get("direction") != "tx":
                continue
            if now - float(meta.get("started") or 0) >= OUTBOX_TTL:
                continue
            mid_hex = meta.get("msg_id")
            if not mid_hex:
                continue
            try:
                msg_id = int(mid_hex, 16)
            except ValueError:
                continue
            if msg_id in self.outbox:
                continue
            c2_mode = (meta.get("codec2") or {}).get("mode", self.mode)
            kind_name = meta.get("kind", "voice")
            rec_id = rec["id"]
            if kind_name == "voice":
                c2_path = self.archive.path_for(rec_id, "message.c2")
                if not c2_path:
                    continue
                with open(c2_path, "rb") as fh:
                    c2 = fh.read()
                mm = M.for_voice(self.voice_modem)
                chunks = P.chunkify(c2, c2_mode, mm)
                self.outbox[msg_id] = {
                    "chunks": chunks, "mode": c2_mode, "kind": P.KIND_VOICE,
                    "modem": mm, "at": float(meta.get("started", now)),
                    "speech_s": float(meta.get("speech_seconds", 0.0)),
                    "state": "SENT_UNCONFIRMED", "last_progress": now,
                }
            elif kind_name in ("text", "file"):
                kind = P.KIND_TEXT if kind_name == "text" else P.KIND_FILE
                fname = meta.get("file_name", "")
                art = "message.txt" if kind == P.KIND_TEXT else (f"file/{fname}" if fname else None)
                if not art:
                    continue
                p = self.archive.path_for(rec_id, art)
                if not p:
                    continue
                with open(p, "rb") as fh:
                    data = fh.read()
                stream, info = P.wrap_payload(data, kind, fname)
                mm = M.for_role(kind_name, len(stream))
                chunks = P.chunkify_data(stream, mm)
                self.outbox[msg_id] = {
                    "chunks": chunks, "mode": c2_mode, "kind": kind,
                    "modem": mm, "at": float(meta.get("started", now)),
                    "speech_s": 0.0, "name": info["name"],
                    "state": "SENT_UNCONFIRMED", "last_progress": now,
                }

    # -- csma/ca and sending wrappers -------------------------------------

    def _send(self, burst, mode=None, tone_duration_s=None):
        """Wrapper for all transmissions to track line activity for the beacon timer."""
        with self._tx_lock:
            self._tx_active = True
            try:
                self.last_activity = time.time()
                if tone_duration_s is not None and self.transport_kind == "soundcard":
                    self.tx.send(burst, mode, tone_duration_s=tone_duration_s)
                else:
                    self.tx.send(burst, mode)
            finally:
                self._tx_active = False

    def _is_channel_busy(self) -> bool:
        """Unified check for both physical energy (mic/UDP) and protocol state (mid-message)."""
        # 1. Physical Check: Is the transport hearing anything right now?
        if self._tx_active or getattr(self.tx, "is_busy", lambda: False)():
            return True
            
        # 2. Logical Check: Are we expecting more chunks for an ongoing message?
        with self.lock:
            for asm in self.inbox.values():
                if not asm.finalised and not asm.sender_should_be_done():
                    return True
        return False

    def _wait_for_clear_channel(self):
        """Blocks the thread until the channel is clear, applying CSMA/CA."""
        if self._is_channel_busy():
            wait_start = time.time()
            
            # Wait for physical energy to drop and logical timers to expire
            while self._is_channel_busy() and not self._stop.is_set():
                if time.time() - wait_start > 60.0:  # Timeout safety net
                    break
                self._stop.wait(0.1)

            # Random backoff to avoid immediate collisions
            if not self._stop.is_set():
                self._stop.wait(random.uniform(0.2, 0.8))

    def close(self):
        self._stop.set()
        try:
            self._render_q.put_nowait(None)
        except queue.Full:
            pass
        self.tx.close()

    # -- archiving the modulated audio ------------------------------------

    def chunk_audio_status(self) -> dict:
        built = MODEM.available()["encode"] is not None
        if not self.archive.save:
            return {"mode": "off", "why": "archive is off", "built": built}
        if not self.archive.save_modem:
            return {"mode": "off", "why": "burst audio switch is off",
                    "built": built}
        if self.transport_kind in ("modem", "soundcard"):
            return {"mode": "captured", "built": built,
                    "why": "captured from the modem, noise and all"}
        if not built:
            return {"mode": "off", "built": False,
                    "why": "aicodix encode not built -- "
                           "cd vendor/modem && make CXX='g++ -march=native'"}
        if self.render_error:
            return {"mode": "off", "built": built,
                    "why": f"render failed: {self.render_error}"}
        if not self.render_modem:
            return {"mode": "off", "built": built,
                    "why": "no modem in the loop; enable rendering or use --modem"}
        return {"mode": "rendered", "built": built,
                "why": "rendered locally -- the clean signal, not what crossed "
                       "the wire, and transmit side only"}

    def _render_worker(self):
        while not self._stop.is_set():
            item = self._render_q.get()
            if item is None:
                if self._stop.is_set():
                    break
                continue
            msg_id, idx, burst = item
            try:
                if self._render_modem_obj is None:
                    self._render_modem_obj = MODEM.AicodixModem(
                        vendor=self.vendor, callsign=self.call)
                samples, rate = self._render_modem_obj.modulate(burst)
                if self.debug_tone_hz is not None:
                    samples = MODEM.prepend_debug_tone(samples, rate, self.debug_tone_hz)
                self.archive.save_chunk_audio("tx", msg_id, samples, rate,
                                              chunk_idx=idx, ok=True)
            except Exception as exc:              # noqa: BLE001
                if self.render_error is None:
                    self.render_error = str(exc)[:160]
                    sio.emit("station_error", {"where": "render_modem",
                                               "error": self.render_error})

    def _queue_render(self, msg_id, idx, burst):
        if not (self.render_modem and self.archive.save
                and self.archive.save_modem
                and self.transport_kind not in ("modem", "soundcard")):
            return
        try:
            self._render_q.put_nowait((msg_id, idx, burst))
        except queue.Full:
            pass

    def _air_seconds(self, count, mm):
        """Estimated on-air time including an optional diagnostic prefix."""
        extra = ((self.debug_tone_ms / 1000.0) if self.debug_tone_hz and
                 self.transport_kind in ("modem", "soundcard") else 0.0)
        return count * (mm.air_s + extra)

    def _archive_tx_audio(self, burst, samples, rate, snr):
        m = P.unpack(burst) if burst else None
        if not m or m["type"] != P.T_DAT:
            return
        self.archive.save_chunk_audio("tx", m["msg_id"], samples, rate,
                                      chunk_idx=m["chunk_idx"], ok=True,
                                      callsign=self.call)

    def _archive_rx_audio(self, burst, samples, rate, snr):
        m = P.unpack(burst) if burst else None
        if m and m["type"] == P.T_DAT:
            self.archive.save_chunk_audio("rx", m["msg_id"], samples, rate,
                                          chunk_idx=m["chunk_idx"], ok=True,
                                          snr_db=snr, callsign=m["callsign"])
            return
        with self.lock:
            live = [a for a in self.inbox.values() if not a.finalised]
        if live:
            newest = max(live, key=lambda a: a.last_rx)
            self.archive.save_chunk_audio("rx", newest.msg_id, samples, rate,
                                          ok=False, snr_db=snr,
                                          callsign=newest.callsign)
        else:
            self.archive.stash_failed(samples, rate)

    # -- outbound ---------------------------------------------------------

    def send_recording(self, blob, suffix):
        enc = codec.encode_message(blob, suffix, self.mode,
                                   use_vad=self.use_vad)
        c2, secs, cstats = enc.c2, enc.seconds, enc.stats
        mm = M.for_voice(self.voice_modem)
        chunks = P.chunkify(c2, self.mode, mm)
        msg_id = random.getrandbits(16)
        g0 = P.geom_for(self.mode, mm)
        self.archive.begin("tx", msg_id, self.call, self.mode, g0,
                           chunks=len(chunks), transport=self.transport_kind,
                           chunk_audio=self.chunk_audio_status()["mode"],
                           air_seconds=round(self._air_seconds(len(chunks), mm), 2),
                           vad=cstats.get("vad"))
        self.archive.save_speech("tx", msg_id, enc.pcm, callsign=self.call)
        self.archive.save_codec2("tx", msg_id, c2, callsign=self.call)
        with self.lock:
            self.outbox[msg_id] = {"chunks": chunks, "mode": self.mode,
                                   "kind": P.KIND_VOICE, "modem": mm,
                                   "at": time.time(), "speech_s": secs,
                                   "state": "QUEUED", "last_progress": time.time()}
        order = P.interleave_order(len(chunks))
        g = g0
        sio.emit("tx_start", {
            "msg_id": f"{msg_id:04x}", "chunks": len(chunks),
            "speech_s": round(secs, 2),
            "air_s": round(self._air_seconds(len(chunks), mm), 2),
            "mode": self.mode, "modem": mm.name, "rate": g.describe(),
            "aicodix": f"{mm.modulation} {mm.coderate} {mm.framesize}",
            "order": order,
            "realtime": round(self._air_seconds(len(chunks), mm) / secs, 2) if secs else 0,
            "vad": cstats.get("vad"),
            "stripped_s": cstats.get("stripped_s", 0),
        })
        threading.Thread(target=self._transmit, args=(msg_id, order),
                         daemon=True).start()
        return msg_id, len(chunks), secs, cstats

    def send_data(self, data: bytes, kind=P.KIND_TEXT, name=""):
        role = P.KIND_ROLE.get(kind, "file")
        stream, info = P.wrap_payload(data, kind, name)
        mm = M.for_role(role, len(stream))
        if self.auto_adapt:
            adv = self.health.recommend(self.voice_modem, self.mode)
            cap_name = adv["recommended"]["data_cap"]
            if M.INDEX_OF.get(mm.name, 99) > M.INDEX_OF.get(cap_name, 99):
                mm = M.BY_NAME[cap_name]
        chunks = P.chunkify_data(stream, mm)
        msg_id = random.getrandbits(16)
        with self.lock:
            self.outbox[msg_id] = {"chunks": chunks, "mode": self.mode,
                                   "kind": kind, "at": time.time(),
                                   "speech_s": 0.0, "name": info["name"],
                                   "modem": mm, "state": "QUEUED",
                                   "last_progress": time.time()}
        order = P.data_order(len(chunks))
        est = P.plan(len(stream), role)
        if self.debug_tone_hz and self.transport_kind in ("modem", "soundcard"):
            est = dict(est)
            est["air_s"] = round(self._air_seconds(len(order), mm), 2)
            est["air_human"] = P._hms(est["air_s"])
        self.archive.begin("tx", msg_id, self.call, self.mode,
                           P.geom_for(self.mode, mm), chunks=len(chunks),
                           transport=self.transport_kind, payload=info,
                           kind=info["kind_name"],
                           air_seconds=round(self._air_seconds(len(order), mm), 2))
        self.archive.save_payload("tx", msg_id, data, kind, name,
                                  callsign=self.call)
        sio.emit("tx_start", {
            "msg_id": f"{msg_id:04x}", "chunks": len(chunks),
            "kind": info["kind_name"], "name": info["name"],
            "modem": mm.name,
            "aicodix": f"{mm.modulation} {mm.coderate} {mm.framesize}",
            "bytes": info["original_bytes"],
            "carried": info["carried_bytes"],
            "compression": info["compression"], "ratio": info["ratio"],
            "air_s": round(self._air_seconds(len(order), mm), 2),
            "air_human": P._hms(self._air_seconds(len(order), mm)),
            "order": order,
        })
        threading.Thread(target=self._transmit, args=(msg_id, order),
                         daemon=True).start()
        return msg_id, len(chunks), est, info

    def _transmit(self, msg_id, order, repair=False):
        """Serialize multi-chunk on-air passes while allowing control bursts between chunks."""
        try:
            with self._data_tx_lock:
                return self._transmit_unlocked(msg_id, order, repair)
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                rec = self.outbox.get(msg_id)
                if rec:
                    rec["state"] = "FAILED"
                    rec["error"] = str(exc)[:240]
            sio.emit("station_error", {"where": "transmit",
                                        "msg_id": f"{msg_id:04x}",
                                        "error": str(exc)[:240]})

    def _transmit_unlocked(self, msg_id, order, repair=False):
        with self.lock:
            rec = self.outbox.get(msg_id)
        if not rec:
            return
        with self.lock:
            rec["state"] = "REPAIRING" if repair else "TRANSMITTING"
            rec["last_progress"] = time.time()
            
        # CSMA/CA: Ensure channel is completely clear before firing
        self._wait_for_clear_channel()
            
        total = len(rec["chunks"])
        rec_kind = rec.get("kind", P.KIND_VOICE)
        mm = rec.get("modem") or M.COMPACT
        origin_id = rec.get("origin_id")
        hops = rec.get("hops", 0)
        aborted = False
        for n, idx in enumerate(order):
            if rec.get("aborted") or self._stop.is_set():
                aborted = True
                sio.emit("tx_aborted", {"msg_id": f"{msg_id:04x}", "sent": n, "of": len(order)})
                break
            burst = P.pack(P.T_DAT, self.call, msg_id, rec["chunks"][idx],
                           chunk_idx=idx, total=total,
                           mode=P.pack_mode(rec["mode"], mm),
                           flags=P.pack_flags(repair, rec_kind, hops=hops),
                           modem_mode=mm, origin=origin_id)
            self._send(burst, mm)
            with self.lock:
                rec["last_progress"] = time.time()
            self._queue_render(msg_id, idx, burst)
            sio.emit("tx_chunk", {"msg_id": f"{msg_id:04x}", "idx": idx,
                                  "n": n + 1, "of": len(order),
                                  "repair": repair, "burst_s": round(mm.air_s, 2)})
            if self.pacing and self.transport_kind != "soundcard":
                self._stop.wait(mm.air_s + ((self.debug_tone_ms / 1000.0) if self.debug_tone_hz else 0.0))
        if aborted:
            with self.lock:
                rec["state"] = "ABORTED"
                rec["next_retry_at"] = None
            return
        if not repair:
            record_name = self.archive.finish(
                "tx", msg_id, callsign=self.call, sent_chunks=len(order),
                interleave_order=order)
            sio.emit("tx_done", {
                "msg_id": f"{msg_id:04x}", "repair": repair, "record": record_name,
                "kind": P.KIND_NAMES.get(rec_kind, "voice"),
                "name": self.outbox.get(msg_id, {}).get("name"),
            })
            with self.lock:
                if rec.get("state") != "CONFIRMED":
                    rec["state"] = "SENT_UNCONFIRMED"
                    rec["tx_attempts"] = rec.get("tx_attempts", 0) + 1
                    if not rec.get("peer_heard"):
                        rec["next_retry_at"] = time.time() + max(25.0, len(order) * mm.air_s + 20.0)
        else:
            sio.emit("tx_done", {"msg_id": f"{msg_id:04x}", "repair": repair})
            with self.lock:
                if rec.get("state") != "CONFIRMED":
                    rec["state"] = "SENT_UNCONFIRMED"

    def _beacon(self):
        while not self._stop.is_set():
            if self._stop.wait(1.0):
                break
            # Only beacon if the channel has been dead silent for 60 seconds
            if time.time() - self.last_activity >= 60.0:
                # Add 1-5 second random jitter to avoid mass collisions
                if self._stop.wait(random.uniform(1.0, 5.0)):
                    break
                
                # Check ONE MORE TIME to ensure it wasn't interrupted during jitter
                if time.time() - self.last_activity >= 60.0 and not self._is_channel_busy():
                    try:
                        self._send(
                            P.pack(P.T_BEACON, self.call, 0, modem_mode=M.CONTROL), M.CONTROL)
                    except OSError:
                        pass

    # -- inbound ----------------------------------------------------------

    def on_drop(self, blob, reason):
        self.last_activity = time.time()
        self.health.observe_burst(False, snr_db=getattr(self.tx, "last_snr", None))
        m = P.unpack(blob)
        sio.emit("ether", {"dir": "drop", "reason": reason,
                           "what": m["type_name"] if m else "?",
                           "idx": m["chunk_idx"] if m else 0,
                           "call": m["callsign"] if m else "?"})

    def on_packet(self, blob):
        self.last_activity = time.time()
        m = P.unpack(blob)
        if not m or m["callsign"] == self.call:
            return
        self.health.observe_burst(
            True,
            snr_db=getattr(self.tx, "last_snr", None),
            tone_ms=getattr(self.tx, "last_rx_tone_ms", None),
        )
        # Wake-on-peer-contact: if we have unconfirmed messages waiting, schedule quick retry
        if self.auto_retry:
            now = time.time()
            with self.lock:
                for rec in self.outbox.values():
                    if (rec.get("state") == "SENT_UNCONFIRMED"
                            and not rec.get("peer_heard")
                            and now - rec.get("last_progress", 0) > 10.0):
                        cur_next = rec.get("next_retry_at")
                        if cur_next is None or cur_next > now + 2.0:
                            rec["next_retry_at"] = now + 2.0

        sio.emit("ether", {"dir": "rx", "what": m["type_name"],
                           "idx": m["chunk_idx"], "call": m["callsign"],
                           "snr": getattr(self.tx, "last_snr", None)})

        if m["type"] == P.T_BEACON:
            ex = m.get("exchange", 0)
            if 0 < ex < 0x8000:
                sent_ms = ex
                heard_ms = getattr(self.tx, "last_rx_tone_ms", 0) or 0
                cut_ms = max(0, min(sent_ms, sent_ms - heard_ms))
                peak = getattr(self.tx, "last_burst_peak", 0.5)
                clipped = getattr(self.tx, "last_burst_clipped", False)
                level_code = 2 if (clipped or peak >= 0.92) else (1 if peak < 0.20 else 0)
                sio.emit("vox_measured", {"from": m["callsign"], "cut_ms": cut_ms,
                                          "heard_ms": heard_ms, "level_code": level_code})
                reply = P.pack(
                    P.T_BEACON, self.call, 0, modem_mode=M.CONTROL,
                    destination=m["station"],
                    exchange_id=0x8000 | (level_code << 13) | (int(cut_ms) & 0x1FFF),
                )
                threading.Thread(target=self._send, args=(reply, M.CONTROL),
                                 daemon=True).start()
            elif ex >= 0x8000 and m.get("destination") in (0, P.station_id(self.call)):
                cut_ms = ex & 0x1FFF
                level_code = (ex >> 13) & 0x03
                rec_ms = max(60, min(500, cut_ms + self.vox_slack_ms))
                self.debug_tone_ms = rec_ms
                self.debug_tone_hz = self.debug_tone_hz or 800.0
                if hasattr(self.tx, "debug_tone_hz"):
                    self.tx.debug_tone_hz = self.debug_tone_hz
                if hasattr(self.tx, "debug_tone_s"):
                    self.tx.debug_tone_s = rec_ms / 1000.0
                if hasattr(self.tx, "tx_gain"):
                    if level_code == 1:
                        self.tx.tx_gain = min(1.8, round(self.tx.tx_gain * 1.30, 2))
                    elif level_code == 2:
                        self.tx.tx_gain = max(0.25, round(self.tx.tx_gain * 0.72, 2))
                self.config_store.save(self.settings_snapshot())
                sio.emit("vox_calibrated", {
                    "from": m["callsign"], "cut_ms": cut_ms,
                    "slack_ms": self.vox_slack_ms, "tone_ms": rec_ms,
                    "level_code": level_code,
                    "tx_gain": getattr(self.tx, "tx_gain", 1.0),
                })
            sio.emit("station", {"call": m["callsign"],
                                 "snr": getattr(self.tx, "last_snr", None)})
        elif m["type"] == P.T_DAT:
            self._on_dat(m)
        elif m["type"] == P.T_RREQ:
            self._on_rreq(m)
        elif m["type"] in (P.T_RACK, P.T_RNAK):
            self._on_answer(m)
        elif m["type"] == P.T_DONE:
            self._on_done(m)

    def _on_dat(self, m):
        origin_id = m.get("origin") or m["station"]
        key = (origin_id, m["msg_id"])
        with self.lock:
            # A valid-CRC packet from a hostile or failed sender must not
            # create unlimited retained assembler/archive state.
            if key not in self.inbox and sum(
                    not a.finalised for a in self.inbox.values()) >= INBOX_MAX:
                return
            asm = self.inbox.get(key)
            if asm is None:
                asm = P.Assembler(m["msg_id"], m["callsign"], m["total"],
                                  m["mode_name"], kind=m["kind"],
                                  modem_mode=m["modem"])
                asm.origin_id = origin_id
                asm.last_source_id = m["station"]
                asm.hops = m.get("hops", 0)
                self.inbox[key] = asm
                self.archive.begin(
                    "rx", m["msg_id"], m["callsign"], asm.mode, asm.geom,
                    chunks=m["total"], transport=self.transport_kind,
                    chunk_audio=("captured"
                                 if self.transport_kind in ("modem", "soundcard")
                                 and self.archive.save_modem else "none"))
                sio.emit("rx_start", {"msg_id": f"{m['msg_id']:04x}",
                                      "call": m["callsign"],
                                      "chunks": m["total"],
                                      "kind": m["kind_name"],
                                      "mode": asm.mode,
                                      "rate": asm.geom.describe(),
                                      "modem": asm.modem.name,
                                      "hops": asm.hops,
                                      "aicodix": f"{asm.modem.modulation} "
                                                 f"{asm.modem.coderate} "
                                                 f"{asm.modem.framesize}",
                                      "seconds": round(m["total"]
                                                       * asm.geom.speech_s, 1)})
            if asm.finalised:
                return
            asm.add(m["chunk_idx"], m["body"], is_repair=m["repair"])
        self._emit_progress(asm)
        if asm.complete():
            self._finalise(key, asm)

    def _on_rreq(self, m):
        # RREQs are addressed to the origin station.  A broadcast medium may
        # deliver the request to every listener, but unrelated stations must
        # stay silent instead of sending misleading RNAKs.
        target, requested = P.decode_rreq(m["body"], m["total"] or 0)
        if target is not None and target != P.station_id(self.call):
            return
        with self.lock:
            rec = self.outbox.get(m["msg_id"])
            if rec:
                rec["peer_heard"] = True
                rec["next_retry_at"] = None
        if not rec:
            self._send(P.pack(P.T_RNAK, self.call, m["msg_id"],
                                modem_mode=M.CONTROL,
                                destination=m.get("station", 0)), M.CONTROL)
            sio.emit("repair_answer", {"msg_id": f"{m['msg_id']:04x}",
                                       "agree": False, "count": 0,
                                       "reason": "message no longer held"})
            return
        want = requested if target is not None else P.decode_missing(
            m["body"], m["total"] or len(rec["chunks"]))
        want = sorted({i for i in want if 0 <= i < len(rec["chunks"])})
        if not want:
            return
        self.health.note_tx_confirmed()
        self.health.observe_message(len(rec["chunks"]), len(want))
        key = f"{m['msg_id']:04x}:{m['station']:04x}"
        info = {"msg_id": f"{m['msg_id']:04x}", "call": m["callsign"],
                "want": want, "of": len(rec["chunks"]),
                "air_s": round(P.air_seconds(len(want), rec.get("modem")), 1),
                "key": key, "requester": m["station"]}
        if self.auto_repair:
            self.answer_repair(key, True, _info=info)
        else:
            with self.lock:
                self.pending[key] = {"msg_id": m["msg_id"], "want": want,
                                     "at": time.time(), "requester": m["station"]}
            sio.emit("repair_request", info)

    def answer_repair(self, key, agree, _info=None):
        with self.lock:
            pend = self.pending.pop(key, None)
        requester = (_info or pend or {}).get("requester", 0)
        if _info is not None:
            msg_id = int(_info["msg_id"], 16)
            want = _info["want"]
        elif pend:
            msg_id, want = pend["msg_id"], pend["want"]
        else:
            return False
        if agree:
            self._send(
                P.pack(P.T_RACK, self.call, msg_id, modem_mode=M.CONTROL,
                       destination=requester), M.CONTROL)
            sio.emit("repair_answer", {"msg_id": f"{msg_id:04x}",
                                       "agree": True, "count": len(want)})
            threading.Thread(target=self._transmit,
                             args=(msg_id, want, True), daemon=True).start()
        else:
            self._send(
                P.pack(P.T_RNAK, self.call, msg_id, modem_mode=M.CONTROL,
                       destination=requester), M.CONTROL)
            sio.emit("repair_answer", {"msg_id": f"{msg_id:04x}",
                                       "agree": False, "count": len(want)})
        return True

    def _on_answer(self, m):
        agreed = m["type"] == P.T_RACK
        origin_id = m.get("origin") or m["station"]
        with self.lock:
            asm = self.inbox.get((origin_id, m["msg_id"])) or self.inbox.get((m["station"], m["msg_id"]))
            hit = asm is not None and not asm.finalised
            if hit and m.get("destination") not in (None, 0, P.station_id(self.call)):
                hit = False
            if hit and m.get("origin") not in (None, 0, P.station_id(asm.callsign)):
                hit = False
            if hit and not agreed:
                asm.declined = True
                # RNAK is a terminal response for this repair exchange.  Do
                # not retain the old data timestamp and wait out the entire
                # transfer deadline before finalizing degraded playback.
                asm.last_rx = time.time()
                asm.expect_until = time.time()
        if hit:
            sio.emit("repair_result", {"msg_id": f"{m['msg_id']:04x}",
                                       "call": m["callsign"],
                                       "agree": agreed})

    def _on_done(self, m):
        if m.get("destination") not in (None, 0, P.station_id(self.call)):
            return
        with self.lock:
            rec = self.outbox.get(m["msg_id"])
            if not rec:
                return
            rec["state"] = "CONFIRMED"
            rec["peer_heard"] = True
            rec["next_retry_at"] = None
            rec.setdefault("confirmed_by", set()).add(m["callsign"])
            who = sorted(rec["confirmed_by"])
        self.health.note_tx_confirmed()
        self.health.observe_message(m.get("total") or 1, 0)
        sio.emit("tx_confirmed", {
            "msg_id": f"{m['msg_id']:04x}",
            "call": m["callsign"],
            "confirmed_by": who,
        })

    # -- timers -----------------------------------------------------------

    def _monitor(self):
        while not self._stop.is_set():
            if self._stop.wait(0.5):
                break
            try:
                self._tick()
            except Exception as exc:              # noqa: BLE001
                sio.emit("station_error", {"where": "monitor",
                                           "error": repr(exc)})

    def _reap(self):
        now = time.time()
        with self.lock:
            for mid in [k for k, v in self.outbox.items()
                        if (v.get("state") not in ("TRANSMITTING", "REPAIRING")
                            and now - v.get("last_progress", v["at"]) > OUTBOX_TTL)]:
                del self.outbox[mid]
            while len(self.outbox) > OUTBOX_MAX:
                candidates = [k for k, v in self.outbox.items()
                              if v.get("state") not in ("TRANSMITTING", "REPAIRING")]
                if not candidates:
                    break
                del self.outbox[candidates[0]]
            for k in [k for k, a in self.inbox.items()
                      if a.finalised and now - a.last_rx > INBOX_TTL]:
                del self.inbox[k]
            for k in [k for k, v in self.pending.items()
                      if now - v.get("at", now) > PENDING_TTL]:
                del self.pending[k]
            for store in (self.audio, self.texts, self.files):
                while len(store) > PAYLOAD_MAX:
                    del store[next(iter(store))]
        self.archive.sweep_stale()

    def _tick(self):
        self._reap()
        now = time.time()
        with self.lock:
            items = list(self.inbox.items())
            out_items = list(self.outbox.items())
        for key, asm in items:
            if asm.finalised:
                continue
            quiet = asm.quiet_for()
            qt, ft, rounds = asm.quiet_timeout, asm.final_timeout, asm.max_rounds

            may_repair = asm.sender_should_be_done() or quiet > asm.stall_timeout

            if asm.complete():
                self._finalise(key, asm)
            elif not may_repair:
                continue
            elif quiet > qt and not asm.declined and asm.repair_rounds < rounds:
                self._request_repair(asm)
            elif quiet > ft or (asm.declined and quiet > qt):
                self._finalise(key, asm, degraded=True)
            elif asm.repair_rounds >= rounds and quiet > qt:
                self._finalise(key, asm, degraded=True)

        # Auto-retry unconfirmed outbound messages (Fire-and-Forget spool)
        for mid, rec in out_items:
            if (self.auto_retry and rec.get("state") == "SENT_UNCONFIRMED"
                    and not rec.get("peer_heard") and not rec.get("aborted")):
                nxt = rec.get("next_retry_at")
                attempts = rec.get("tx_attempts", 1)
                if nxt and now >= nxt:
                    if attempts < 5:
                        backoff = 30.0 * (2 ** max(0, attempts - 1))
                        with self.lock:
                            rec["next_retry_at"] = now + backoff
                            rec["state"] = "QUEUED"
                        order = (P.interleave_order(len(rec["chunks"]))
                                 if rec.get("kind", P.KIND_VOICE) == P.KIND_VOICE
                                 else P.data_order(len(rec["chunks"])))
                        sio.emit("tx_retry", {
                            "msg_id": f"{mid:04x}",
                            "attempt": attempts + 1,
                            "reason": "no confirmation heard",
                        })
                        threading.Thread(target=self._transmit, args=(mid, order, False),
                                         daemon=True).start()
                    elif not rec.get("unconfirmed_noted"):
                        rec["unconfirmed_noted"] = True
                        rec["next_retry_at"] = None
                        self.health.note_tx_unconfirmed()

        if self.auto_adapt:
            adv = self.health.recommend(self.voice_modem, self.mode)
            if adv["ready_to_apply"]:
                old_vm, old_c2 = self.voice_modem, self.mode
                self.voice_modem = adv["recommended"]["voice_modem"]
                self.mode = adv["recommended"]["mode"]
                self.health.mark_applied()
                self.config_store.save(self.settings_snapshot())
                sio.emit("mode_adapted", {
                    "from": f"{old_vm}/{old_c2}",
                    "to": f"{self.voice_modem}/{self.mode}",
                    "why": adv["why"],
                })

    def _request_repair(self, asm):
        missing = asm.missing()
        if not missing:
            return
        asm.repair_rounds += 1
        asm.last_rx = time.time()
        asm.note_repair_requested(len(missing))
        body = P.encode_rreq(P.station_id(asm.callsign), missing, asm.total,
                             M.CONTROL.usable)
        self._send(P.pack(P.T_RREQ, self.call, asm.msg_id, body,
                            total=asm.total, modem_mode=M.CONTROL,
                            origin=P.station_id(asm.callsign),
                            destination=P.station_id(asm.callsign)), M.CONTROL)
        sio.emit("repair_asked", {"msg_id": f"{asm.msg_id:04x}",
                                  "count": len(missing),
                                  "round": asm.repair_rounds,
                                  "of": asm.total})

    def _emit_progress(self, asm):
        sio.emit("rx_progress", {
            "msg_id": f"{asm.msg_id:04x}", "call": asm.callsign,
            "have": len(asm.chunks), "of": asm.total, "strip": asm.strip(),
        })

    def _finalise(self, key, asm, degraded=False):
        with self.lock:
            if asm.finalised:
                return
            asm.finalised = True
        self.health.observe_message(asm.total, len(asm.missing()) + len(asm.repaired))
        if asm.kind == P.KIND_VOICE:
            self._finalise_voice(asm, degraded)
        else:
            self._finalise_data(asm, degraded)
        if not degraded and asm.complete():
            origin_id = getattr(asm, "origin_id", None) or P.station_id(asm.callsign)
            done_pkt = P.pack(
                P.T_DONE, self.call, asm.msg_id,
                total=asm.total,
                mode=P.pack_mode(asm.mode, asm.modem),
                flags=P.pack_flags(repair=False, kind=asm.kind),
                modem_mode=M.CONTROL,
                origin=origin_id,
                destination=origin_id,
            )
            threading.Thread(target=self._send, args=(done_pkt, M.CONTROL),
                             daemon=True).start()
            m_hops = getattr(asm, "hops", 0)
            if self.repeater and asm.callsign != self.call and m_hops < P.MAX_HOPS:
                rep_key = (origin_id, asm.msg_id)
                with self.lock:
                    already = rep_key in self._repeated
                    if not already:
                        if len(self._repeated) >= 256:
                            self._repeated.pop()
                        self._repeated.add(rep_key)
                        chunks_list = [asm.chunks[i] for i in range(asm.total)]
                        self.outbox[asm.msg_id] = {
                            "chunks": chunks_list,
                            "mode": asm.mode,
                            "kind": asm.kind,
                            "modem": asm.modem,
                            "origin_id": origin_id,
                            "origin_call": asm.callsign,
                            "hops": min(P.MAX_HOPS, m_hops + 1),
                            "at": time.time(),
                            "speech_s": round(asm.total * asm.geom.speech_s, 2),
                            "state": "REPEATING",
                            "last_progress": time.time(),
                        }
                if not already:
                    order = (P.interleave_order(asm.total)
                             if asm.kind == P.KIND_VOICE else P.data_order(asm.total))
                    def _delayed_repeat():
                        if not self._stop.wait(random.uniform(2.5, 4.0)):
                            self._transmit(asm.msg_id, order, repair=False)
                    threading.Thread(target=_delayed_repeat, daemon=True).start()

    def _finalise_voice(self, asm, degraded):
        try:
            c2 = asm.assemble()
            wav, pcm = codec.decode_message_full(c2, asm.mode)
            self.archive.save_codec2("rx", asm.msg_id, c2, callsign=asm.callsign)
            self.archive.save_speech("rx", asm.msg_id, pcm, callsign=asm.callsign)
        except codec.CodecError as exc:
            sio.emit("rx_error", {"msg_id": f"{asm.msg_id:04x}",
                                  "error": str(exc)})
            self.archive.finish("rx", asm.msg_id, callsign=asm.callsign,
                                error=str(exc))
            return
        mid = f"{asm.msg_id:04x}"
        self.audio[mid] = wav
        rec = self._finish_record(asm, degraded)
        sio.emit("rx_done", {
            "msg_id": mid, "kind": "voice", "call": asm.callsign,
            "strip": asm.strip(), "have": len(asm.chunks), "of": asm.total,
            "lost": len(asm.missing()), "repaired": len(asm.repaired),
            "loss_pct": round(100 * asm.loss_fraction(), 1),
            "degraded": degraded and bool(asm.missing()),
            "mode": asm.mode, "rate": asm.geom.describe(),
            "modem": asm.modem.name,
            "aicodix": f"{asm.modem.modulation} {asm.modem.coderate} "
                       f"{asm.modem.framesize}",
            "lost_s": round(asm.lost_seconds(), 2),
            "seconds": round(asm.total * asm.geom.speech_s, 1),
            "hops": getattr(asm, "hops", 0),
            "record": rec,
        })

    def _finalise_data(self, asm, degraded):
        mid = f"{asm.msg_id:04x}"
        got = P.unwrap_payload(asm.assemble_data())
        base = {"msg_id": mid, "call": asm.callsign, "strip": asm.strip(),
                "have": len(asm.chunks), "of": asm.total,
                "lost": len(asm.missing()), "repaired": len(asm.repaired),
                "loss_pct": round(100 * asm.loss_fraction(), 1),
                "mode": asm.mode, "modem": asm.modem.name,
                "aicodix": f"{asm.modem.modulation} {asm.modem.coderate} "
                           f"{asm.modem.framesize}"}

        if got is None:
            self.archive.save_raw_stream("rx", asm.msg_id,
                                         asm.assemble_data(),
                                         callsign=asm.callsign)
            rec = self._finish_record(asm, True, payload_error="prefix lost")
            sio.emit("rx_done", {**base, "kind": "unknown", "degraded": True,
                                 "error": "chunk 1 never arrived, so the "
                                          "length, checksum and filename are "
                                          "unknown. Raw bytes saved.",
                                 "record": rec})
            return

        kind_name = got["kind_name"]
        intact = got["intact"] and not asm.missing()
        self.archive.save_payload("rx", asm.msg_id, got["data"],
                                  got["kind"], got["name"], callsign=asm.callsign)
        if not intact:
            self.archive.save_raw_stream("rx", asm.msg_id,
                                         asm.assemble_data(),
                                         callsign=asm.callsign)

        if got["kind"] == P.KIND_TEXT:
            text = got["data"].decode("utf-8", "replace")
            self.texts[mid] = text
            payload = {"text": text[:16000],
                       "truncated_display": len(text) > 16000}
        else:
            self.files[mid] = (got["name"] or f"unnamed-{mid}.bin",
                               got["data"], intact)
            payload = {"filename": self.files[mid][0]}

        rec = self._finish_record(asm, degraded,
                                 payload_intact=intact,
                                 payload_name=got["name"],
                                 payload_bytes=got["original_bytes"],
                                 compression=got["compression"])
        sio.emit("rx_done", {
            **base, **payload, "kind": kind_name, "intact": intact,
            "degraded": not intact,
            "bytes": got["original_bytes"],
            "recovered": got["recovered_bytes"],
            "compression": got["compression"],
            "bad_ranges": asm.bad_ranges()[:20],
            "lost_bytes": asm.lost_bytes(),
            "hops": getattr(asm, "hops", 0),
            "error": got["error"],
            "record": rec,
        })

    def _finish_record(self, asm, degraded, **extra):
        return self.archive.finish(
            "rx", asm.msg_id, callsign=asm.callsign,
            have=len(asm.chunks), total=asm.total,
            lost=len(asm.missing()), repaired=sorted(asm.repaired),
            missing=asm.missing(), strip=asm.strip(),
            repair_rounds=asm.repair_rounds, declined=asm.declined,
            kind=P.KIND_NAMES.get(asm.kind, "?"),
            degraded=bool(degraded and asm.missing()),
            loss_pct=round(100 * asm.loss_fraction(), 1), **extra)

    def settings_snapshot(self) -> dict:
        return {"call": self.call, "auto_repair": self.auto_repair,
                "auto_adapt": self.auto_adapt, "auto_retry": self.auto_retry,
                "pacing": self.pacing,
                "use_vad": self.use_vad, "save": self.archive.save,
                "save_modem": self.archive.save_modem,
                "render_modem": self.render_modem, "mode": self.mode,
                "voice_modem": self.voice_modem,
                "debug_tone_hz": self.debug_tone_hz or 0.0,
                "debug_tone_ms": self.debug_tone_ms,
                "vox_slack_ms": self.vox_slack_ms}


ST: Station | None = None


# --------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/state")
def api_state():
    g = P.geom_for(ST.mode, M.for_voice(ST.voice_modem))
    return jsonify({
        "call": ST.call, "mode": ST.mode, "modes": P.MODES,
        "demo": DEMO_MODE,
        "auto_repair": ST.auto_repair, "pacing": ST.pacing,
        "auto_adapt": ST.auto_adapt, "auto_retry": ST.auto_retry,
        "repeater": ST.repeater,
        "channel_key_active": P._CHANNEL_KEY is not None,
        "use_vad": ST.use_vad,
        "transport_kind": ST.transport_kind,
        "debug_tone_hz": ST.debug_tone_hz or 0.0,
        "debug_tone_ms": ST.debug_tone_ms,
        "vox_slack_ms": ST.vox_slack_ms,
        "tx_gain": getattr(ST.tx, "tx_gain", 1.0),
        "health": ST.health.recommend(ST.voice_modem, ST.mode),
        "impair": ST.tx.impair.state(), "link": ST.tx.stats(),
        "archive": ST.archive.state(),
        "codec": codec.available(),
        "burst_bytes": g.modem.payload, "payload_bytes": g.payload,
        "chunk_speech_s": round(g.speech_s, 3),
        "modem_modes": [m.describe() for m in M.MODES],
        "voice_modem": ST.voice_modem,
        "voice_options": P.voice_options(ST.mode),
        "frames_per_chunk": g.frames,
        "realtime_ratio": round(g.realtime_ratio, 2),
        "geometry": {m: P.geom_for(m, M.for_voice(ST.voice_modem)).describe()
                     for m in P.MODES},
        "rate": P.geom_for(ST.mode, M.for_voice(ST.voice_modem)).describe(),
    })


@app.route("/api/send", methods=["POST"])
def api_send():
    f = request.files.get("audio")
    if not f:
        return jsonify({"error": "no audio in request"}), 400
    blob = f.read()
    if DEMO_MODE and len(blob) > DEMO_MAX_AUDIO_BYTES:
        return jsonify({"error": "recording too large for public demo (max 512 KB)"}), 413
    ext = os.path.splitext(f.filename or "")[1].lower()
    mt = (f.mimetype or "").lower()
    if ext in (".wav", ".mp4", ".m4a", ".aac", ".mp3", ".ogg", ".webm"):
        suffix = ext
    elif "wav" in mt:
        suffix = ".wav"
    elif "mp4" in mt or "m4a" in mt or "aac" in mt:
        suffix = ".m4a"
    elif "mpeg" in mt or "mp3" in mt:
        suffix = ".mp3"
    elif "webm" in mt:
        suffix = ".webm"
    else:
        suffix = ".ogg"
    try:
        msg_id, n, secs, cstats = ST.send_recording(blob, suffix)
    except codec.CodecError as exc:
        return jsonify({"error": str(exc)}), 500
    mm = M.for_voice(ST.voice_modem)
    air = ST._air_seconds(n, mm)
    return jsonify({"msg_id": f"{msg_id:04x}", "chunks": n,
                    "speech_s": round(secs, 2), "mode": ST.mode,
                    "air_s": round(air, 2),
                    "realtime": round(air / secs, 2)
                    if secs else 0,
                    **{k: v for k, v in cstats.items()
                       if k in ("vad", "stripped_s", "original_s",
                                "codec2_bytes")}})


@app.route("/api/sample", methods=["POST"])
def api_sample():
    if not DEMO_MODE:
        return jsonify({"error": "sample clip is only enabled in demo mode"}), 403
    if not os.path.isfile(SAMPLE_VOICE_PATH):
        return jsonify({"error": "sample clip missing"}), 404
    with open(SAMPLE_VOICE_PATH, "rb") as fh:
        blob = fh.read()
    try:
        msg_id, n, secs, cstats = ST.send_recording(blob, ".wav")
    except codec.CodecError as exc:
        return jsonify({"error": str(exc)}), 500
    mm = M.for_voice(ST.voice_modem)
    air = ST._air_seconds(n, mm)
    return jsonify({"msg_id": f"{msg_id:04x}", "chunks": n,
                    "speech_s": round(secs, 2), "mode": ST.mode,
                    "air_s": round(air, 2),
                    "realtime": round(air / secs, 2)
                    if secs else 0,
                    **{k: v for k, v in cstats.items()
                       if k in ("vad", "stripped_s", "original_s",
                                "codec2_bytes")}})


@app.route("/api/estimate", methods=["POST"])
def api_estimate():
    f = request.files.get("file")
    if f:
        data, name, kind = f.read(), f.filename or "", P.KIND_FILE
        if DEMO_MODE and len(data) > DEMO_MAX_FILE_BYTES:
            return jsonify({"error": f"demo file limit is {DEMO_MAX_FILE_BYTES // 1024} KB"}), 413
    else:
        d = request.json or {}
        data = (d.get("text") or "").encode("utf-8")
        name, kind = "", P.KIND_TEXT
        if DEMO_MODE and len(data) > DEMO_MAX_TEXT_BYTES:
            return jsonify({"error": f"demo text limit is {DEMO_MAX_TEXT_BYTES} bytes"}), 413
    if not data:
        return jsonify({"error": "nothing to estimate"}), 400
    _, info = P.wrap_payload(data, kind, name)
    role = "text" if kind == P.KIND_TEXT else "file"
    mm = M.for_role(role, info["stream_bytes"])
    chunks = -(-info["stream_bytes"] // mm.usable) or 1
    bursts = chunks + 1 if chunks > 1 else chunks
    air = ST._air_seconds(bursts, mm)
    return jsonify({**info, "chunks": chunks, "bursts": bursts,
                    "modem": mm.name, "burst_s": round(mm.air_s, 2),
                    "air_s": round(air, 1), "air_human": P._hms(air),
                    "bytes_per_s": round(mm.bytes_per_s, 1)})


@app.route("/api/text", methods=["POST"])
def api_text():
    text = ((request.json or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "no text"}), 400
    raw = text.encode("utf-8")
    if DEMO_MODE and len(raw) > DEMO_MAX_TEXT_BYTES:
        return jsonify({"error": f"demo text limit is {DEMO_MAX_TEXT_BYTES} bytes"}), 413
    msg_id, n, est, info = ST.send_data(raw, P.KIND_TEXT)
    return jsonify({"msg_id": f"{msg_id:04x}", "chunks": n, **info,
                    "air_s": est["air_s"], "air_human": est["air_human"]})


@app.route("/api/file", methods=["POST"])
def api_file():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    data = f.read()
    if not data:
        return jsonify({"error": "file is empty"}), 400
    if DEMO_MODE and len(data) > DEMO_MAX_FILE_BYTES:
        return jsonify({"error": f"demo file limit is {DEMO_MAX_FILE_BYTES // 1024} KB"}), 413
    msg_id, n, est, info = ST.send_data(data, P.KIND_FILE, f.filename or "")
    return jsonify({"msg_id": f"{msg_id:04x}", "chunks": n, **info,
                    "air_s": est["air_s"], "air_human": est["air_human"]})


@app.route("/api/payload/<mid>")
def api_payload(mid):
    if mid in ST.files:
        name, data, intact = ST.files[mid]
        safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", os.path.basename(name))[:120] or "payload.bin"
        return app.response_class(data, mimetype="application/octet-stream",
                                  headers={"Content-Disposition":
                                           f'attachment; filename="{safe_name}"'})
    if mid in ST.texts:
        return app.response_class(ST.texts[mid].encode("utf-8"),
                                  mimetype="text/plain; charset=utf-8")
    return jsonify({"error": "no such payload"}), 404


@app.route("/api/recordings")
def api_recordings():
    return jsonify({"archive": ST.archive.state(),
                    "records": ST.archive.list()})


@app.route("/api/recordings/<rec_id>.zip")
def api_recording_zip(rec_id):
    data = ST.archive.zip_bytes(rec_id)
    if data is None:
        return jsonify({"error": "no such recording"}), 404
    return app.response_class(data, mimetype="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{rec_id}.zip"'})


@app.route("/api/recordings/<rec_id>/file/<path:artifact>")
def api_recording_file(rec_id, artifact):
    path = ST.archive.path_for(rec_id, artifact)
    if not path:
        return jsonify({"error": "no such file"}), 404
    mime = ("audio/wav" if artifact.endswith(".wav")
            else "application/json" if artifact.endswith(".json")
            else "application/octet-stream")
    with open(path, "rb") as fh:
        data = fh.read()
    name = os.path.basename(artifact)
    return app.response_class(data, mimetype=mime, headers={
        "Content-Disposition": f'attachment; filename="{rec_id}-{name}"'})


@app.route("/api/recordings/<rec_id>", methods=["DELETE"])
def api_recording_delete(rec_id):
    if DEMO_MODE:
        return jsonify({"error": "disabled in demo mode"}), 403
    return jsonify({"ok": ST.archive.delete(rec_id)})


@app.route("/api/audio/<mid>")
def api_audio(mid):
    wav = ST.audio.get(mid)
    if not wav:
        return jsonify({"error": "no such message"}), 404
    return app.response_class(wav, mimetype="audio/wav")


@app.route("/api/repair/<key>", methods=["POST"])
def api_repair(key):
    agree = bool((request.json or {}).get("agree"))
    return jsonify({"ok": ST.answer_repair(key, agree)})


@app.route("/api/abort/<mid>", methods=["POST"])
def api_abort(mid):
    try:
        msg_id = int(mid, 16)
    except ValueError:
        return jsonify({"error": "bad msg_id"}), 400
    with ST.lock:
        rec = ST.outbox.get(msg_id)
        if not rec:
            return jsonify({"ok": False}), 404
        rec["aborted"] = True
        rec["next_retry_at"] = None
    return jsonify({"ok": True})


@app.route("/api/retry/<mid>", methods=["POST"])
def api_retry(mid):
    try:
        msg_id = int(mid, 16)
    except ValueError:
        return jsonify({"error": "bad msg_id"}), 400
    with ST.lock:
        asm = None
        for a in ST.inbox.values():
            if a.msg_id == msg_id:
                asm = a
                break
        if not asm or not asm.missing():
            return jsonify({"ok": False, "error": "nothing missing"}), 404
        asm.finalised = False
        asm.declined = False
        asm.repair_rounds = 0
        missing_n = len(asm.missing())
    threading.Thread(target=ST._request_repair, args=(asm,), daemon=True).start()
    return jsonify({"ok": True, "missing": missing_n})


@app.route("/api/vox_calibrate", methods=["POST"])
def api_vox_calibrate():
    probe_ms = 450
    if not ST.debug_tone_hz:
        ST.debug_tone_hz = 800.0
        if hasattr(ST.tx, "debug_tone_hz"):
            ST.tx.debug_tone_hz = 800.0
    pkt = P.pack(P.T_BEACON, ST.call, 0, modem_mode=M.CONTROL, exchange_id=probe_ms)
    threading.Thread(target=ST._send, args=(pkt, M.CONTROL),
                     kwargs={"tone_duration_s": probe_ms / 1000.0}, daemon=True).start()
    return jsonify({"ok": True, "probe_ms": probe_ms, "tone_hz": ST.debug_tone_hz})


@app.route("/api/audio_test", methods=["POST"])
def api_audio_test():
    pkt = P.pack(P.T_BEACON, ST.call, 0, modem_mode=M.CONTROL)
    threading.Thread(target=ST._send, args=(pkt, M.CONTROL), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/adapt_apply", methods=["POST"])
def api_adapt_apply():
    adv = ST.health.recommend(ST.voice_modem, ST.mode)
    old_vm, old_c2 = ST.voice_modem, ST.mode
    ST.voice_modem = adv["recommended"]["voice_modem"]
    ST.mode = adv["recommended"]["mode"]
    ST.health.mark_applied()
    ST.config_store.save(ST.settings_snapshot())
    sio.emit("mode_adapted", {
        "from": f"{old_vm}/{old_c2}",
        "to": f"{ST.voice_modem}/{ST.mode}",
        "why": adv["why"],
    })
    return jsonify({"ok": True, "voice_modem": ST.voice_modem, "mode": ST.mode})


@app.route("/api/devices")
def api_devices():
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        out = [{"index": i, "name": d["name"],
                "in": d["max_input_channels"],
                "out": d["max_output_channels"]}
               for i, d in enumerate(devs)]
        return jsonify({"ok": True, "devices": out,
                        "current": getattr(ST.tx, "device", None)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "devices": [], "error": str(exc)})


@app.route("/api/tx_wav/<mid>")
def api_tx_wav(mid):
    try:
        msg_id = int(mid, 16)
    except ValueError:
        return jsonify({"error": "bad msg_id"}), 400
    with ST.lock:
        rec = ST.outbox.get(msg_id)
    if not rec:
        return jsonify({"error": "message not in outbox"}), 404
    modem = ST.modem or MODEM.AicodixModem(vendor=ST.vendor, callsign=ST.call)
    total = len(rec["chunks"])
    rec_kind = rec.get("kind", P.KIND_VOICE)
    mm = rec.get("modem") or M.COMPACT
    order = (P.interleave_order(total)
             if rec_kind == P.KIND_VOICE else P.data_order(total))
    bursts = []
    for idx in order:
        burst = P.pack(P.T_DAT, ST.call, msg_id, rec["chunks"][idx],
                       chunk_idx=idx, total=total,
                       mode=P.pack_mode(rec["mode"], mm),
                       flags=P.pack_flags(False, rec_kind),
                       modem_mode=mm)
        samples, rate = modem.modulate(burst, mode=mm)
        if ST.debug_tone_hz:
            samples = MODEM.prepend_debug_tone(
                samples, rate, ST.debug_tone_hz,
                duration_s=ST.debug_tone_ms / 1000.0)
        bursts.append(samples)
    joined = MODEM.concat_bursts(bursts, rate=modem.rate, gap_s=M.PTT_GAP_S)
    pcm = (joined.clip(-1.0, 1.0) * 32767).astype("<i2").tobytes()
    wav = codec.pcm_to_wav(pcm, rate=modem.rate)
    return app.response_class(wav, mimetype="audio/wav", headers={
        "Content-Disposition": f'attachment; filename="crowcall-{mid}.wav"'})


@app.route("/api/rx_wav", methods=["POST"])
def api_rx_wav():
    f = request.files.get("audio")
    if not f:
        return jsonify({"error": "no audio"}), 400
    ext = os.path.splitext(f.filename or "")[1].lower() or ".wav"
    try:
        pcm = codec.recording_to_pcm(f.read(), suffix=ext, rate=MODEM.RATE)
    except codec.CodecError as exc:
        return jsonify({"error": str(exc)}), 400
    import numpy as np
    stream = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
    modem = ST.modem or MODEM.AicodixModem(vendor=ST.vendor, callsign=ST.call)
    decoded_n = 0
    for b in MODEM.split_bursts(stream, rate=MODEM.RATE):
        trimmed, _, _ = MODEM.strip_debug_tone_info(b, MODEM.RATE)
        pkt = modem.demodulate(trimmed, MODEM.RATE)
        if pkt:
            decoded_n += 1
            ST.on_packet(pkt)
    return jsonify({"ok": True, "decoded_bursts": decoded_n})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    d = request.json or {}
    try:
        if "call" in d:
            new_call = re.sub(r"[^A-Z0-9_-]", "", str(d["call"]).upper())[:8]
            if new_call:
                ST.call = new_call
                if ST.modem:
                    ST.modem.callsign = new_call
        if "auto_repair" in d:
            ST.auto_repair = _bool(d["auto_repair"])
        if "auto_adapt" in d:
            ST.auto_adapt = _bool(d["auto_adapt"])
        if "auto_retry" in d:
            ST.auto_retry = _bool(d["auto_retry"])
        if "repeater" in d:
            ST.repeater = _bool(d["repeater"])
        if "channel_key" in d:
            P.set_channel_key(str(d["channel_key"] or "").strip() or None)
        if "pacing" in d:
            ST.pacing = _bool(d["pacing"])
        if "impair" in d:
            if not isinstance(d["impair"], dict):
                raise ValueError("impair must be an object")
            ST.tx.impair.update(**d["impair"])
        if d.get("mode") in P.MODES:
            ST.mode = d["mode"]
        if "use_vad" in d:
            ST.use_vad = _bool(d["use_vad"])
        if d.get("voice_modem") in M.VOICE_CHOICES:
            ST.voice_modem = d["voice_modem"]
        if "debug_tone_hz" in d:
            hz = float(d["debug_tone_hz"] or 0.0)
            ST.debug_tone_hz = None if hz <= 0 else hz
            if hasattr(ST.tx, "debug_tone_hz"):
                ST.tx.debug_tone_hz = ST.debug_tone_hz
        if "debug_tone_ms" in d:
            ST.debug_tone_ms = max(40, min(500, int(d["debug_tone_ms"])))
            if hasattr(ST.tx, "debug_tone_s"):
                ST.tx.debug_tone_s = ST.debug_tone_ms / 1000.0
        if "vox_slack_ms" in d:
            ST.vox_slack_ms = max(20, min(250, int(d["vox_slack_ms"])))
        if "tx_gain" in d and hasattr(ST.tx, "tx_gain"):
            ST.tx.tx_gain = max(0.1, min(2.0, float(d["tx_gain"])))
        if "save" in d:
            ST.archive.save = _bool(d["save"])
        if "save_modem" in d:
            ST.archive.save_modem = _bool(d["save_modem"])
        if "render_modem" in d:
            ST.render_modem = _bool(d["render_modem"])
            ST.render_error = None
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    ST.config_store.save(ST.settings_snapshot())
    return jsonify({"call": ST.call, "auto_repair": ST.auto_repair,
                    "auto_adapt": ST.auto_adapt, "auto_retry": ST.auto_retry,
                    "repeater": ST.repeater,
                    "pacing": ST.pacing,
                    "mode": ST.mode, "use_vad": ST.use_vad,
                    "voice_modem": ST.voice_modem,
                    "debug_tone_hz": ST.debug_tone_hz or 0.0,
                    "debug_tone_ms": ST.debug_tone_ms,
                    "vox_slack_ms": ST.vox_slack_ms,
                    "archive": ST.archive.state(),
                    "render_modem": ST.render_modem,
                    "chunk_audio": ST.chunk_audio_status(),
                    "impair": ST.tx.impair.state()})


def main():
    global ST, DEMO_MODE
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default="crowcall.json")
    pre_args, _ = pre.parse_known_args()
    cfg = ConfigStore(pre_args.config).load()
    ap = argparse.ArgumentParser()
    ap.add_argument("--call", default=cfg.get("call", "ANON"))
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (use 0.0.0.0 for LAN/mobile access)")
    ap.add_argument("--hub", default="127.0.0.1:45000")
    ap.add_argument("--repeater", action="store_true",
                    help="simplex store-and-forward repeater mode")
    ap.add_argument("--key", default=None,
                    help="optional pre-shared channel passphrase")
    ap.add_argument("--no-auto-adapt", action="store_true",
                    help="disable automatic link-health mode adaptation")
    ap.add_argument("--no-auto-retry", action="store_true",
                    help="disable automatic retry of unconfirmed messages")
    ap.add_argument("--no-pacing", action="store_true",
                    help="transmit instantly instead of at radio speed")
    ap.add_argument("--modem", action="store_true",
                    help="carry modulated audio through the real aicodix "
                         "modem instead of raw bursts")
    ap.add_argument("--soundcard", action="store_true",
                    help="carry modulated audio through system soundcard "
                         "microphone and speakers using aicodix modem")
    ap.add_argument("--vendor", default=None,
                    help="path to the aicodix checkout (default ~/crowcall/vendor)")
    ap.add_argument("--mode", default=cfg.get("mode", P.DEFAULT_MODE), choices=P.MODES,
                    help="codec2 rate")
    ap.add_argument("--voice-mode", default=cfg.get("voice_modem", M.DEFAULT_VOICE),
                    choices=M.VOICE_CHOICES,
                    help="aicodix mode for voice: fine / balanced / quick "
                         "(smaller holes vs less air time)")
    ap.add_argument("--debug-tone", type=float,
                    default=cfg.get("debug_tone_hz", 0.0), metavar="HZ",
                    help="prepend an audible diagnostic tone to each modem "
                         "burst (use a different frequency per station; 0 disables)")
    ap.add_argument("--no-vad", action="store_true",
                    help="do not strip silence before encoding")
    ap.add_argument("--no-save", action="store_true",
                    help="do not archive messages to disk")
    ap.add_argument("--save-modem", action="store_true",
                    help="archive the modulated audio for each burst "
                         "(~100 KB each, so this grows fast)")
    ap.add_argument("--render-modem", action="store_true",
                    help="on the raw-burst transport there is no modulated "
                         "audio to capture; this renders it locally for the "
                         "archive instead (transmit side only). Implied by "
                         "--save-modem without --modem.")
    ap.add_argument("--recordings", default="recordings",
                    help="where to archive (default ./recordings)")
    ap.add_argument("--config", default=pre_args.config,
                    help="JSON file for accepted runtime settings")
    ap.add_argument("--demo", action="store_true",
                    help="public web demo mode: caps uploads, keeps settings in memory, "
                         "disables disk archiving, enables /api/sample")
    ap.set_defaults(no_pacing=not bool(cfg.get("pacing", True)),
                    no_vad=not bool(cfg.get("use_vad", True)),
                    no_save=not bool(cfg.get("save", True)),
                    save_modem=bool(cfg.get("save_modem", False)),
                    render_modem=bool(cfg.get("render_modem", False)),
                    no_auto_adapt=not bool(cfg.get("auto_adapt", True)),
                    no_auto_retry=not bool(cfg.get("auto_retry", True)))
    a = ap.parse_args()

    DEMO_MODE = bool(a.demo)
    if DEMO_MODE:
        app.config["MAX_CONTENT_LENGTH"] = DEMO_MAX_AUDIO_BYTES + 64 * 1024
        a.no_save = True
        a.save_modem = False
        a.render_modem = False

    host, _, hport = a.hub.partition(":")
    hub_port = int(hport or 45000)
    if not a.soundcard and not DEMO_MODE and host in ("127.0.0.1", "localhost", "0.0.0.0"):
        import hub
        hub.ensure_local_hub("127.0.0.1" if host == "0.0.0.0" else host, hub_port)
    try:
        ST = Station(a.call, (host, hub_port),
                     pacing=not a.no_pacing, mode=a.mode,
                     use_modem=a.modem, use_soundcard=a.soundcard,
                     vendor=a.vendor, use_vad=not a.no_vad,
                     voice_modem=a.voice_mode, save=not a.no_save,
                     save_modem=a.save_modem, rec_root=a.recordings,
                     render_modem=(a.render_modem
                                   or (a.save_modem and not (a.modem or a.soundcard))),
                     config_path=None if DEMO_MODE else a.config,
                     debug_tone=a.debug_tone,
                     debug_tone_ms=cfg.get("debug_tone_ms", 120),
                     vox_slack_ms=cfg.get("vox_slack_ms", 60),
                     repeater=a.repeater,
                     auto_adapt=not a.no_auto_adapt,
                     auto_retry=not a.no_auto_retry,
                     channel_key=a.key)
    except Exception as exc:                      # noqa: BLE001
        print(f"\ncannot start: {exc}")
        return 1

    avail = codec.available()
    if not avail["ffmpeg"]:
        print("WARNING: ffmpeg not found -- recording will fail")
    if not avail["c2enc"]:
        print("WARNING: c2enc not found -- falling back to ffmpeg libcodec2")
    if not avail["vad"] and not a.no_vad:
        print("note: pysilero-vad not installed -- silence will not be "
              "stripped (pip install pysilero-vad)")

    g = P.geom_for(ST.mode, M.for_voice(ST.voice_modem))
    print("\n" + M.summary())
    print(f"\ncrowcall station {ST.call}")
    print(f"  ui        http://{a.host}:{a.port}")
    if ST.transport_kind != "soundcard":
        print(f"  hub       {host}:{hport or 45000}   local udp :{ST.tx.local[1]}")
    print(f"  transport {ST.tx.stats().get('transport', ST.transport_kind)}")
    print(f"  debug tone {ST.debug_tone_hz:.0f} Hz" if ST.debug_tone_hz else
          "  debug tone off")
    print(f"  codec2    {ST.mode}: {g.frames} frames x {g.frame_bytes} B "
          f"= {g.payload} B per burst")
    for o in P.voice_options(ST.mode):
        mark = "->" if o["modem"] == ST.voice_modem else "  "
        print(f"  {mark} voice {o['label']:9s} {o['aicodix']:16s} "
              f"{o['hole_s']:.2f}s per chunk, {o['realtime']:.2f}x realtime")
    ast = ST.archive.state()
    if ast["save"]:
        print(f"  archive   {ast['root']}  "
              f"({ast['count']} records, {ast['bytes'] / 1e6:.1f} MB)")
        cas = ST.chunk_audio_status()
        print(f"  burst wav {cas['mode']}: {cas['why']}")
    else:
        print("  archive   off")
    print()

    sio.run(app, host=a.host, port=a.port,
            allow_unsafe_werkzeug=True, log_output=False)


if __name__ == "__main__":
    raise SystemExit(main())
