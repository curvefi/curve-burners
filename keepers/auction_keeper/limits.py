"""Request budgets: what the keeper may ask of a host, and when it stops asking.

Every HTTP API the keeper talks to (an orderbook, a price feed, an
aggregator) is behind one ``HostGate`` per host, shared by the whole process:
the limits and the bans of those APIs are per IP, not per network or per
thread.

* A gate is a token bucket: ``per_minute`` requests a minute on average, a
  quarter of that at once. A request that finds the bucket empty is not sent
  and not queued: it raises ``Throttled`` and the caller goes on with what it
  has (an estimate without that source's level, an order posted a cycle
  later). The cycle never waits for a rate limit.
* ``reserve`` keeps part of the bucket for the requests that matter more:
  a price quote leaves half of it to the orders posted to the same host.
* A 403 or a 429 closes the gate for ``cooldown`` seconds, twice as long each
  time in a row (at most eight times): a host that has started refusing is
  left alone instead of being asked on every cycle.

``RpcMeter`` counts the node's requests by method (the keeper prints them
per cycle) and, with a rate set, spaces them out; a node request is never
dropped, it waits its turn.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# Requests per minute to a host nobody set a limit for, and the known ones.
DEFAULT_PER_MINUTE = 60.0
# The CoW orderbook answers a burst of quotes and posts with a CloudFront
# 403 for the IP that lasts tens of minutes (seen on 2026-11-24).
DEFAULT_HOSTS: dict[str, float] = {"api.cow.fi": 20.0}
DEFAULT_COOLDOWN = 600.0
MAX_COOLDOWN_FACTOR = 8
REFUSALS = (403, 429)


class Throttled(RuntimeError):
    """The request was not sent: the host's budget is spent or the host is
    being left alone after a refusal. Try again in ``wait`` seconds."""

    def __init__(self, host: str, wait: float, reason: str):
        self.host = host
        self.wait = wait
        super().__init__(f"{host}: {reason}, {wait:.0f}s to go")


class HostGate:
    def __init__(
        self,
        host: str,
        per_minute: float,
        *,
        cooldown: float = DEFAULT_COOLDOWN,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.host = host
        self.per_minute = per_minute  # 0: no limit
        self.capacity = max(1.0, per_minute / 4)
        self.cooldown = cooldown
        self.clock = clock
        self._tokens = self.capacity
        self._at = clock()
        self._closed_until = 0.0
        self._refusals = 0
        self._lock = threading.Lock()
        self.sent = 0
        self.held = 0

    def take(self, *, reserve: float = 0.0) -> None:
        """Account for one request about to be sent, or raise ``Throttled``.
        ``reserve``: the share of the bucket this request leaves untouched."""
        with self._lock:
            now = self.clock()
            if now < self._closed_until:
                self.held += 1
                raise Throttled(self.host, self._closed_until - now, "left alone after a refusal")
            if self.per_minute:
                rate = self.per_minute / 60
                self._tokens = min(self.capacity, self._tokens + (now - self._at) * rate)
                self._at = now
                floor = reserve * self.capacity
                if self._tokens - 1 < floor:
                    self.held += 1
                    raise Throttled(
                        self.host, (floor + 1 - self._tokens) / rate, "request budget spent"
                    )
                self._tokens -= 1
            self.sent += 1

    def answered(self, status: int) -> None:
        """Note the status of an answer: a refusal closes the gate."""
        with self._lock:
            if status not in REFUSALS:
                if status and status < 400:
                    self._refusals = 0
                return
            self._refusals += 1
            pause = self.cooldown * min(2 ** (self._refusals - 1), MAX_COOLDOWN_FACTOR)
            self._closed_until = self.clock() + pause
        log.warning("%s answered %s: no requests to it for %.0fs", self.host, status, pause)


class Gates:
    """The process's gates by host."""

    def __init__(self) -> None:
        self.default = DEFAULT_PER_MINUTE
        self.hosts = dict(DEFAULT_HOSTS)
        self.cooldown = DEFAULT_COOLDOWN
        self._gates: dict[str, HostGate] = {}
        self._lock = threading.Lock()

    def configure(self, *, default: float, hosts: Mapping[str, float], cooldown: float) -> None:
        with self._lock:
            self.default = default
            self.hosts = {**DEFAULT_HOSTS, **hosts}
            self.cooldown = cooldown
            self._gates.clear()

    def of(self, url: str) -> HostGate:
        host = urlsplit(url).netloc or url
        with self._lock:
            gate = self._gates.get(host)
            if gate is None:
                gate = self._gates[host] = HostGate(
                    host, self.hosts.get(host, self.default), cooldown=self.cooldown
                )
            return gate

    def counts(self) -> dict[str, tuple[int, int]]:
        """Per host: requests sent and requests held back, since the start."""
        with self._lock:
            return {host: (gate.sent, gate.held) for host, gate in self._gates.items()}


GATES = Gates()


class RpcMeter:
    """Counts a node's requests by method; with ``per_second`` set, a request
    waits until its turn so the node sees at most that many a second."""

    def __init__(
        self,
        per_second: float = 0.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.interval = 1 / per_second if per_second else 0.0
        self.clock = clock
        self.sleep = sleep
        self.counts: Counter[str] = Counter()
        self._next = 0.0
        self._lock = threading.Lock()

    def note(self, method: str) -> None:
        with self._lock:
            self.counts[method] += 1
            if not self.interval:
                return
            now = self.clock()
            turn = max(now, self._next)
            self._next = turn + self.interval
        if turn > now:
            self.sleep(turn - now)

    def total(self) -> int:
        with self._lock:
            return sum(self.counts.values())


__all__ = [
    "DEFAULT_COOLDOWN",
    "DEFAULT_HOSTS",
    "DEFAULT_PER_MINUTE",
    "GATES",
    "Gates",
    "HostGate",
    "RpcMeter",
    "Throttled",
]
