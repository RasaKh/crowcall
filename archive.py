"""
crowcall archive
================

Saves every message in all three of its forms, because they are useful for
different things:

  speech.wav / speech.pcm   8 kHz mono s16le -- what a person hears. The wav
                            is playable anywhere; the .pcm is the same samples
                            raw, for feeding straight into c2enc or a plot.

  message.c2                the codec2 bytes actually carried over the air.
                            Decode by hand with:
                                c2dec 1400 message.c2 out.raw
                            The mode is in meta.json, and you need it -- raw
                            codec2 has no header, so nothing can infer the
                            frame size.

  chunks/*.wav              the modulated audio per burst, as it went out or
                            as it arrived. On the receive side these have the
                            channel noise in them, so a burst that failed to
                            decode is saved too and you can listen to what
                            was actually on the wire when it died.

Modem audio is by far the largest of the three -- roughly 100 KB per burst at
48 kHz against 231 bytes of codec2 -- so it is a separate switch from the
other two.

Layout, one directory per message:

    recordings/
      20260730T142530Z-rx-ALPHA-a3f2/
        meta.json
        speech.wav
        speech.pcm
        message.c2
        chunks/chunk-000.wav ...
        chunks/failed-000.wav ...
"""

from __future__ import annotations

import io
import json
import os
import shutil
import struct
import threading
import time
import zipfile
import re
import uuid
import zlib

ARTIFACTS = ("speech.wav", "speech.pcm", "message.c2")


def _wav(samples_i16: bytes, rate: int) -> bytes:
    f = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    riff = (b"WAVE" + b"fmt " + struct.pack("<I", len(f)) + f
            + b"data" + struct.pack("<I", len(samples_i16)) + samples_i16)
    return b"RIFF" + struct.pack("<I", len(riff)) + riff


def _floats_to_i16(samples) -> bytes:
    import numpy as np
    a = np.asarray(samples, dtype="float32")
    return (np.clip(a, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def dir_size(path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


class Archive:
    """One directory per message. Thread-safe; the transport writes chunk
    audio from its receive thread while the station writes speech from
    another."""

    def __init__(self, root="recordings", save=True, save_modem=False,
                 max_mb=2048):
        self.root = os.path.abspath(root)
        self.save = save
        self.save_modem = save_modem
        self.max_bytes = max_mb * 1024 * 1024
        self._lock = threading.RLock()
        self._open: dict[str, dict] = {}     # key -> record
        self._aliases: dict[tuple[str, int, str], str] = {}
        # Bursts that fail to decode carry no header, so we cannot tell which
        # message they belong to. If one fails before any chunk of that
        # message has landed there is no record open yet either -- and that is
        # exactly the common case, since the first burst of a message is as
        # likely to die as any other. So stash them and flush into the next
        # record that opens.
        self._orphans: list[tuple[float, bytes, int]] = []
        self._orphan_max = 12
        os.makedirs(self.root, exist_ok=True)

    # -- lifecycle -------------------------------------------------------

    def _key(self, direction, msg_id):
        return f"{direction}:{msg_id:04x}"

    def _resolve_key(self, direction, msg_id, callsign=None):
        if callsign is not None:
            key = self._aliases.get((direction, int(msg_id), str(callsign)))
            if key in self._open:
                return key
        base = self._key(direction, msg_id)
        if base in self._open:
            return base
        candidates = [k for k, r in self._open.items()
                      if r["meta"].get("direction") == direction
                      and r["meta"].get("msg_id") == f"{int(msg_id):04x}"]
        return candidates[-1] if len(candidates) == 1 else None

    def begin(self, direction, msg_id, callsign, mode, geom, **extra):
        """Open a record. Safe to call twice; the second call is ignored."""
        if not self.save:
            return None
        with self._lock:
            alias = (direction, int(msg_id), str(callsign or ""))
            key = self._aliases.get(alias)
            if key in self._open:
                return self._open[key]["dir"]
            base = self._key(direction, msg_id)
            if base not in self._open:
                key = base
            else:
                key = f"{base}:{uuid.uuid4().hex[:10]}"
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            # Callsigns are metadata only.  Generated directory names prevent
            # traversal, separators, control bytes and collisions.
            call_tag = f"{zlib.crc32(str(callsign or '').encode()) & 0xffffffff:08x}"
            name = f"{stamp}-{direction}-{call_tag}-{int(msg_id):016x}-{uuid.uuid4().hex[:8]}"
            root_real = os.path.realpath(self.root)
            path = os.path.join(root_real, name)
            if not os.path.realpath(path).startswith(root_real + os.sep):
                raise RuntimeError("archive path escaped root")
            os.makedirs(os.path.join(path, "chunks"), exist_ok=True)
            rec = {
                "dir": path, "chunk_n": 0, "failed_n": 0,
                "meta": {
                    "msg_id": f"{msg_id:04x}", "direction": direction,
                    "callsign": callsign, "utc": stamp,
                    "codec2": {
                        "mode": mode,
                        "nominal_bps": geom.bps, "wire_bps": geom.wire_bps,
                        "bytes_per_sec": round(geom.bytes_s, 1),
                        "frames_per_chunk": geom.frames,
                        "frame_bytes": geom.frame_bytes,
                        "speech_per_chunk_s": round(geom.speech_s, 3),
                        "decode_with": f"c2dec {mode} message.c2 out.raw",
                    },
                    "started": time.time(),
                    **extra,
                },
            }
            self._open[key] = rec
            self._aliases[alias] = key
            orphans = []
            if direction == "rx" and self._orphans:
                orphans, self._orphans = self._orphans, []
        for _, pcm, srate in orphans:
            self._write_failed(rec, _wav(pcm, srate))
        return path

    def note(self, direction, msg_id, callsign=None, **fields):
        with self._lock:
            rec = self._open.get(self._resolve_key(direction, msg_id, callsign))
            if rec:
                rec["meta"].update(fields)

    def _write_failed(self, rec, wav_bytes):
        with self._lock:
            n = rec["failed_n"]
            rec["failed_n"] += 1
        path = os.path.join(rec["dir"], "chunks", f"failed-{n:03d}.wav")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(wav_bytes)

    def stash_failed(self, samples, rate):
        """Hold a failed burst until a record exists to file it under."""
        if not (self.save and self.save_modem):
            return
        with self._lock:
            self._orphans.append((time.time(), _floats_to_i16(samples), rate))
            if len(self._orphans) > self._orphan_max:
                self._orphans.pop(0)

    def _write(self, direction, msg_id, filename, data, callsign=None):
        with self._lock:
            rec = self._open.get(self._resolve_key(direction, msg_id, callsign))
            if not rec:
                return None
            path = os.path.join(rec["dir"], filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    # -- the three forms -------------------------------------------------

    def save_speech(self, direction, msg_id, pcm: bytes, rate=8000, callsign=None):
        """8 kHz mono s16le, saved both playable and raw."""
        if not self.save or not pcm:
            return
        self._write(direction, msg_id, "speech.pcm", pcm, callsign)
        self._write(direction, msg_id, "speech.wav", _wav(pcm, rate), callsign)
        self.note(direction, msg_id, callsign,
                  speech_seconds=round(len(pcm) / 2 / rate, 2))

    def save_codec2(self, direction, msg_id, c2: bytes, callsign=None):
        if not self.save or not c2:
            return
        self._write(direction, msg_id, "message.c2", c2, callsign)
        self.note(direction, msg_id, callsign, codec2_bytes=len(c2))

    def save_chunk_audio(self, direction, msg_id, samples, rate,
                         chunk_idx=None, ok=True, snr_db=None, callsign=None):
        """Modulated audio for one burst. `chunk_idx` is None for a burst that
        failed to decode -- we cannot know which chunk it was, but the audio
        is exactly the interesting part, so it is kept under failed-NNN."""
        if not (self.save and self.save_modem):
            return
        with self._lock:
            rec = self._open.get(self._resolve_key(direction, msg_id, callsign))
            if not rec:
                return
            if not (ok and chunk_idx is not None):
                self._write_failed(rec, _wav(_floats_to_i16(samples), rate))
                if snr_db is not None:
                    self.note(direction, msg_id,
                              last_snr_db=round(float(snr_db), 1))
                return
            name = f"chunks/chunk-{chunk_idx:03d}.wav"
            rec["chunk_n"] += 1
        self._write(direction, msg_id, name, _wav(_floats_to_i16(samples), rate), callsign)
        if snr_db is not None:
            self.note(direction, msg_id, last_snr_db=round(float(snr_db), 1))

    def save_payload(self, direction, msg_id, data: bytes, kind, name="", callsign=None):
        """Text lands as message.txt, a file under its own name so it is
        usable straight out of the directory."""
        if not self.save or data is None:
            return
        if kind == 1:                                  # KIND_TEXT
            self._write(direction, msg_id, "message.txt", data, callsign)
            self.note(direction, msg_id, callsign, text_bytes=len(data))
            return
        safe = os.path.basename(name or "").strip() or "payload.bin"
        safe = "".join(c for c in safe if c not in '/\\:*?"<>|') or "payload.bin"
        self._write(direction, msg_id, f"file/{safe}", data, callsign)
        self.note(direction, msg_id, callsign, file_name=safe, file_bytes=len(data))

    def save_raw_stream(self, direction, msg_id, stream: bytes):
        """The reassembled bytes exactly as they arrived, holes and all. Kept
        only when something went wrong, since that is when you want to pick
        through it."""
        if not self.save or not stream:
            return
        self._write(direction, msg_id, "stream.raw", stream)
        self.note(direction, msg_id, raw_stream_bytes=len(stream))

    # -- finish ----------------------------------------------------------

    def finish(self, direction, msg_id, callsign=None, **fields):
        if not self.save:
            return None
        with self._lock:
            key = self._resolve_key(direction, msg_id, callsign)
            rec = self._open.pop(key, None) if key else None
            if key:
                for a, k in list(self._aliases.items()):
                    if k == key:
                        self._aliases.pop(a, None)
        if not rec:
            return None
        rec["meta"].update(fields)
        rec["meta"]["finished"] = time.time()
        rec["meta"]["chunk_audio_saved"] = rec["chunk_n"]
        rec["meta"]["chunk_audio_failed"] = rec["failed_n"]
        rec["meta"]["bytes_on_disk"] = dir_size(rec["dir"])
        with open(os.path.join(rec["dir"], "meta.json"), "w") as fh:
            json.dump(rec["meta"], fh, indent=2, sort_keys=True, default=str)
        self.prune()
        return os.path.basename(rec["dir"])

    # -- browsing --------------------------------------------------------

    def list(self) -> list[dict]:
        out = []
        for name in sorted(os.listdir(self.root), reverse=True):
            path = os.path.join(self.root, name)
            if not os.path.isdir(path):
                continue
            meta = {}
            mp = os.path.join(path, "meta.json")
            if os.path.exists(mp):
                try:
                    with open(mp) as fh:
                        meta = json.load(fh)
                except (OSError, ValueError):
                    pass
            chunks = os.path.join(path, "chunks")
            have = {a for a in ARTIFACTS
                    if os.path.exists(os.path.join(path, a))}
            out.append({
                "id": name, "meta": meta, "have": sorted(have),
                "chunk_files": len(os.listdir(chunks))
                if os.path.isdir(chunks) else 0,
                "bytes": dir_size(path),
            })
        return out

    def path_for(self, rec_id, artifact=None):
        """Resolve a path, refusing anything that escapes the archive root."""
        base = os.path.realpath(os.path.join(self.root, rec_id))
        if not base.startswith(os.path.realpath(self.root) + os.sep):
            return None
        if artifact is None:
            return base if os.path.isdir(base) else None
        target = os.path.realpath(os.path.join(base, artifact))
        if not target.startswith(base + os.sep):
            return None
        return target if os.path.isfile(target) else None

    def zip_bytes(self, rec_id) -> bytes | None:
        base = self.path_for(rec_id)
        if not base:
            return None
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(base):
                for f in sorted(files):
                    full = os.path.join(root, f)
                    z.write(full, os.path.join(
                        rec_id, os.path.relpath(full, base)))
        return buf.getvalue()

    def delete(self, rec_id) -> bool:
        base = self.path_for(rec_id)
        if not base:
            return False
        shutil.rmtree(base, ignore_errors=True)
        return True

    # -- housekeeping ----------------------------------------------------

    def sweep_stale(self, max_age=3600):
        """Close records that were begun and never finished.

        `finish()` is the only thing that pops from _open, so a message that
        never finalises -- a sender that vanished, a decode that raised --
        held its entry and its metadata for the life of the process.
        """
        now = time.time()
        stale = []
        with self._lock:
            for key, rec in list(self._open.items()):
                if now - rec["meta"].get("started", now) > max_age:
                    stale.append((key, self._open.pop(key)))
        for key, rec in stale:
            with self._lock:
                for alias, alias_key in list(self._aliases.items()):
                    if alias_key == key:
                        self._aliases.pop(alias, None)
            rec["meta"]["abandoned"] = True
            rec["meta"]["finished"] = now
            try:
                with open(os.path.join(rec["dir"], "meta.json"), "w") as fh:
                    json.dump(rec["meta"], fh, indent=2, sort_keys=True,
                              default=str)
            except OSError:
                pass
        return len(stale)

    def total_bytes(self) -> int:
        return dir_size(self.root)

    def prune(self):
        """Drop the oldest records once the archive exceeds its cap. Modem
        audio is ~400x the size of the codec2 it carries, so an archive with
        it enabled fills up fast."""
        total = self.total_bytes()
        if total <= self.max_bytes:
            return 0
        removed = 0
        for rec in sorted(self.list(), key=lambda r: r["id"]):
            if total <= self.max_bytes:
                break
            if self.delete(rec["id"]):
                total -= rec["bytes"]
                removed += 1
        return removed

    def state(self) -> dict:
        return {"save": self.save, "save_modem": self.save_modem,
                "root": self.root, "bytes": self.total_bytes(),
                "max_bytes": self.max_bytes,
                "count": len([d for d in os.listdir(self.root)
                              if os.path.isdir(os.path.join(self.root, d))])}
