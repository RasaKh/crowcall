"""
crowcall transport
==================

Two implementations behind one interface:

  HubTransport      -- loopback UDP through a dumb reflector (hub.py).
                       This is the simulated ether. Multicast was the
                       original plan but access points block it, and the
                       hub turned out better anyway: it gives one obvious
                       place for every station to hear everything, which is
                       exactly how a radio channel behaves.

  AicodixTransport   -- placeholder for the real thing. Same interface, so
                       swapping it in changes nothing above this file.

Impairments are applied on RECEIVE, per station. "My radio is in a bad
spot" is a property of the receiver, and doing it this way needs no
cross-process configuration.

The fade model matters more than plain packet loss. Step 4 showed this link
does not degrade gracefully -- above ~9.5 dB in-band SNR everything decodes,
below it nothing does. So the realistic failure is not "5% of packets are
lost at random", it is "the channel went completely dark for four seconds".
That is what `fade_prob` / `fade_ms` simulate, and it is the case the
repair protocol actually has to handle.
"""

from __future__ import annotations

import heapq
import itertools
import queue
import random
import socket
import threading
import time

# Must exceed the largest modem payload. Bursts are no longer a fixed 256:
# the file mode carries 1536 B. This cap was 512 and silently rejected every
# bulk burst the moment mode selection went in.
MAX_PACKET = 4096


class Impairments:
    def __init__(self):
        self.enabled = True
        self.loss_pct = 0.0        # independent per-packet loss
        self.fade_prob = 0.0       # chance per packet of starting a fade
        self.fade_ms = 3000        # how long the channel stays dark
        self.delay_ms = 0          # constant propagation delay
        self.jitter_ms = 0
        self._fade_until = 0.0
        self._lock = threading.Lock()

    def update(self, **kw):
        for k, v in kw.items():
            if hasattr(self, k) and not k.startswith("_"):
                setattr(self, k, type(getattr(self, k))(v)
                        if not isinstance(getattr(self, k), bool) else bool(v))

    def state(self) -> dict:
        return {"enabled": self.enabled, "loss_pct": self.loss_pct,
                "fade_prob": self.fade_prob, "fade_ms": self.fade_ms,
                "delay_ms": self.delay_ms, "jitter_ms": self.jitter_ms,
                "fading": self.in_fade()}

    def in_fade(self) -> bool:
        return time.time() < self._fade_until

    def judge(self) -> tuple[bool, float, str]:
        """Returns (deliver, delay_seconds, reason_if_dropped)."""
        if not self.enabled:
            return True, 0.0, ""
        with self._lock:
            if self.in_fade():
                return False, 0.0, "fade"
            if self.fade_prob > 0 and random.random() < self.fade_prob:
                self._fade_until = time.time() + self.fade_ms / 1000.0
                return False, 0.0, "fade"
        if self.loss_pct > 0 and random.random() * 100 < self.loss_pct:
            return False, 0.0, "noise"
        d = self.delay_ms / 1000.0
        if self.jitter_ms:
            d += random.uniform(0, self.jitter_ms / 1000.0)
        return True, d, ""


class HubTransport:
    """max_payload is the only thing callers need to know about the medium."""

    max_payload = 1536              # largest selected aicodix protocol burst
    name = "hub (simulated ether)"

    def __init__(self, hub_addr=("127.0.0.1", 45000), on_recv=None,
                 on_drop=None):
        self.hub_addr = hub_addr
        self.on_recv = on_recv
        self.on_drop = on_drop
        self.impair = Impairments()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.5)
        self.local = self.sock.getsockname()
        
        self.tx_count = 0
        self.rx_count = 0
        self.drop_count = 0
        self.last_rx_time = 0.0  # Added for CSMA physical checks
        
        self._delayed: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        threading.Thread(target=self._delay_worker, daemon=True).start()
        threading.Thread(target=self._keepalive, daemon=True).start()

    def is_busy(self) -> bool:
        """Physical carrier sense: did we get a UDP packet in the last 1.5 seconds?"""
        return time.time() - self.last_rx_time < 1.5

    def send(self, blob: bytes, mode=None):
        if len(blob) > MAX_PACKET:
            raise ValueError("burst too large")
        self.sock.sendto(blob, self.hub_addr)
        self.tx_count += 1

    def _deliver(self, blob):
        self.rx_count += 1
        if self.on_recv:
            try:
                self.on_recv(blob)
            except Exception:
                pass

    def _loop(self):
        try:
            self.sock.sendto(b"\x00HELLO", self.hub_addr)
        except OSError:
            pass
        while not self._stop.is_set():
            try:
                blob, _ = self.sock.recvfrom(MAX_PACKET)
            except socket.timeout:
                continue
            except OSError:
                break
            if blob.startswith(b"\x00"):
                continue
                
            self.last_rx_time = time.time()  # Track activity
            
            deliver, delay, reason = self.impair.judge()
            if not deliver:
                self.drop_count += 1
                if self.on_drop:
                    try:
                        self.on_drop(blob, reason)
                    except Exception:
                        pass
                continue
            if delay > 0:
                self._defer(delay, blob)
            else:
                self._deliver(blob)

    def _defer(self, delay, blob):
        self._delayed.put((time.time() + delay, next(self._seq), blob))

    def _keepalive(self):
        while not self._stop.wait(20.0):
            try:
                self.sock.sendto(b"\x00HELLO", self.hub_addr)
            except OSError:
                return

    def _delay_worker(self):
        while not self._stop.is_set():
            try:
                due, _, blob = self._delayed.get(timeout=0.25)
            except queue.Empty:
                continue
            wait = due - time.time()
            if wait > 0:
                time.sleep(min(wait, 5.0))
            self._deliver(blob)

    def stats(self) -> dict:
        return {"tx": self.tx_count, "rx": self.rx_count,
                "dropped": self.drop_count, "port": self.local[1],
                "transport": self.name}

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

# AicodixTransport used to live here as a stub that subclassed HubTransport.
# Removed: it was superseded by radio.ModemUdpTransport, and anything that
# instantiated it would have got a plain hub transport that merely CLAIMED to
# be a modem. See modem.BurstDetector for the piece a real soundcard path
# would still need.
