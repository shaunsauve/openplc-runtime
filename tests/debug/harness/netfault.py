"""The client link as something the harness can shape and break (F154).

The tunnel profile (N013) is one TCP port with a 300 ms ± 20 ms RTT between the client and the
runtime. On loopback there is no such link, so the harness splices one in: a TCP proxy it owns,
sitting between its Socket.IO client and the gateway. The TLS session is end to end — the harness
terminates it — so the proxy moves bytes and never sees a message, a frame boundary or a request.

Three faults, each of them the thing the name says and nothing else:

  delay   every byte forwarded is held for half the stated RTT (plus jitter) in each direction,
          so a round trip on the link is `rttMs`;
  drop    every connection open at that moment is severed at the proxy, which is what the client
          sees when the network goes away — no FIN dance, no close frame;
  restore the link as it was: no added delay, and new connections are served normally.

A severed connection is not re-established by the proxy: the client that lost it has to open a
new one, which is the reconnect an oracle's later step asks for. Nothing here reaches the
runtime's own transport, so a scenario that wants the core to notice a lost client uses
`inject: killClient`, which drops the session's transport with no detach (runner.py).

See also:
  tests/debug/harness/socketio_endpoint.py - the inject kinds that call this.
  tests/debug/wire/run_harness.py - puts the splice in front of the gateway.
"""

from __future__ import annotations

import random
import socket
import threading
import time
from typing import List, Optional, Tuple

CHUNK = 65536
ACCEPT_TIMEOUT_S = 0.2


class NetFault:
    """A TCP splice in front of the gateway, with an RTT, a cut and a reset."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self.target = (target_host, target_port)
        self.host = "127.0.0.1"
        self.rtt_ms = 0.0
        self.jitter_ms = 0.0
        self.drops = 0
        self.lock = threading.Lock()
        self.live: List[Tuple[socket.socket, socket.socket]] = []
        self.stop = threading.Event()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((self.host, 0))
        self.listener.listen(16)
        self.listener.settimeout(ACCEPT_TIMEOUT_S)
        self.port = int(self.listener.getsockname()[1])
        self.thread = threading.Thread(target=self._accept, name="netfault-accept", daemon=True)
        self.thread.start()

    # -- the shape of the link

    def shape(self) -> "dict[str, object]":
        """What the link is doing now, as an injection answers."""
        return {"rttMs": self.rtt_ms, "jitterMs": self.jitter_ms, "drops": self.drops}

    def delay(self, rtt_ms: float, jitter_ms: float = 0.0) -> None:
        """Hold every byte for half the RTT in each direction (P11's tunnel profile)."""
        if rtt_ms < 0 or jitter_ms < 0:
            raise ValueError(f"delayNetwork: rttMs={rtt_ms} jitterMs={jitter_ms}")
        with self.lock:
            self.rtt_ms = float(rtt_ms)
            self.jitter_ms = float(jitter_ms)

    def restore(self) -> None:
        with self.lock:
            self.rtt_ms = 0.0
            self.jitter_ms = 0.0

    def drop(self) -> int:
        """Sever every connection open now; how many were cut."""
        with self.lock:
            pairs = list(self.live)
            self.live.clear()
            self.drops += 1
        for pair in pairs:
            for sock in pair:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
        return len(pairs)

    # -- the splice

    def _half_delay_s(self) -> float:
        with self.lock:
            rtt, jitter = self.rtt_ms, self.jitter_ms
        if rtt <= 0 and jitter <= 0:
            return 0.0
        half = rtt / 2000.0
        if jitter:
            half += random.uniform(-jitter, jitter) / 2000.0
        return max(half, 0.0)

    def _accept(self) -> None:
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._serve, args=(client,), name="netfault-conn", daemon=True
            ).start()

    def _serve(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection(self.target, timeout=10.0)
        except OSError:
            client.close()
            return
        pair = (client, upstream)
        with self.lock:
            self.live.append(pair)
        pumps = [
            threading.Thread(target=self._pump, args=pair, name="netfault-up", daemon=True),
            threading.Thread(target=self._pump, args=(upstream, client), name="netfault-down",
                             daemon=True),
        ]
        for pump in pumps:
            pump.start()
        for pump in pumps:
            pump.join()
        with self.lock:
            if pair in self.live:
                self.live.remove(pair)
        for sock in pair:
            try:
                sock.close()
            except OSError:
                pass

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        try:
            while not self.stop.is_set():
                chunk = src.recv(CHUNK)
                if not chunk:
                    break
                delay = self._half_delay_s()
                if delay:
                    time.sleep(delay)
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            # One direction closing ends the pair: the peer sees EOF, as a dropped link would.
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def close(self) -> None:
        self.stop.set()
        self.drop()
        try:
            self.listener.close()
        except OSError:
            pass
        self.thread.join(timeout=1.0)