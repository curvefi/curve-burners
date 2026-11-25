"""Persisted keeper state: published orders, the lot books and the price cache.

A lost state file costs one extra publish per lot at most (orderbooks
deduplicate identical orders, and a repost at a fresh quote is always
valid), one scan of the auction's logs from ``start_block`` and one round of
price lookups.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderRecord:
    uid: str
    valid_to: int
    sell_amount: int
    buy_amount: int
    posted_at: int
    # Window start of the lot the order was built for: a restaged lot (new
    # window) invalidates the record even if the order has not expired.
    lot_start: int
    # The schedule zone the order was posted in ("": not recorded).
    zone: str = ""


_RECORD_FIELDS = {f.name for f in fields(OrderRecord)}


def _record(raw: dict[str, Any]) -> OrderRecord:
    """A record from the file, ignoring keys a newer or older layout wrote."""
    return OrderRecord(**{key: value for key, value in raw.items() if key in _RECORD_FIELDS})


class KeeperState:
    """Shared by every network keeper of one process (one thread each), so
    every mutation and the atomic rewrite of the file hold one lock."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else None
        self._orders: dict[str, dict[str, Any]] = {}
        # Per "network:auction": the AuctionBook dump (lot records, log cursor).
        self._books: dict[str, dict[str, Any]] = {}
        # Per network: the MarketPricer dump.
        self._prices: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        if self.path is not None and self.path.exists():
            self._load()

    # Orders

    @staticmethod
    def _key(network: str, auction: str, adapter: str, token: str) -> str:
        return f"{network}:{auction.lower()}:{adapter.lower()}:{token.lower()}"

    def order(self, network: str, auction: str, adapter: str, token: str) -> OrderRecord | None:
        with self._lock:
            raw = self._orders.get(self._key(network, auction, adapter, token))
            return _record(raw) if raw else None

    def set_order(
        self, network: str, auction: str, adapter: str, token: str, record: OrderRecord
    ) -> None:
        with self._lock:
            self._orders[self._key(network, auction, adapter, token)] = asdict(record)
            self.save()

    def clear_order(self, network: str, auction: str, adapter: str, token: str) -> None:
        with self._lock:
            if self._orders.pop(self._key(network, auction, adapter, token), None) is not None:
                self.save()

    def adapter_orders(self, network: str, auction: str, adapter: str) -> dict[str, OrderRecord]:
        """The recorded orders of one adapter by (lowercase) lot token."""
        prefix = f"{network}:{auction.lower()}:{adapter.lower()}:"
        with self._lock:
            return {
                key[len(prefix) :]: _record(raw)
                for key, raw in self._orders.items()
                if key.startswith(prefix)
            }

    def drop_orders(self, network: str, auction: str, adapter: str, tokens: list[str]) -> None:
        """Forget the adapter's records of ``tokens`` (one write of the file)."""
        with self._lock:
            dropped = [
                self._orders.pop(self._key(network, auction, adapter, token), None)
                for token in tokens
            ]
            if any(raw is not None for raw in dropped):
                self.save()

    def orders(self) -> dict[str, OrderRecord]:
        with self._lock:
            return {key: _record(raw) for key, raw in self._orders.items()}

    def open_orders(self, network: str, auction: str, adapter: str, now: int) -> list[OrderRecord]:
        """Recorded orders of one adapter still valid at ``now`` (the open-order budget)."""
        prefix = f"{network}:{auction.lower()}:{adapter.lower()}:"
        with self._lock:
            return [
                _record(raw)
                for key, raw in self._orders.items()
                if key.startswith(prefix) and raw["valid_to"] > now
            ]

    # The local model's books and the price cache: set in memory, written by
    # the keeper's one ``save`` per cycle.

    def book(self, network: str, auction: str) -> dict[str, Any] | None:
        with self._lock:
            return self._books.get(f"{network}:{auction.lower()}")

    def set_book(self, network: str, auction: str, book: dict[str, Any]) -> None:
        with self._lock:
            self._books[f"{network}:{auction.lower()}"] = book

    def prices(self, network: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._prices.get(network, {}))

    def set_prices(self, network: str, prices: dict[str, Any]) -> None:
        with self._lock:
            self._prices[network] = prices

    # Persistence

    def _load(self) -> None:
        assert self.path is not None
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("state file %s unreadable (%s); starting empty", self.path, exc)
            return
        self._orders = dict(raw.get("orders", {}))
        self._books = dict(raw.get("books", {}))
        self._prices = dict(raw.get("prices", {}))

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            payload = {
                "orders": self._orders,
                "books": self._books,
                "prices": self._prices,
            }
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
            os.replace(tmp, self.path)
