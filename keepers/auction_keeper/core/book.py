"""One auction's lots kept locally: the model's records, fed by the chain.

``AuctionBook`` holds what ``core.model`` needs for an auction: the curve
(read once, read again when an ``EconomicsSet`` log says it changed) and a
``LotRecord`` per token. The records come from three places, cheapest first:

* ``sync``: the auction's logs since the last scan, one ``eth_getLogs`` per
  range. ``LotStaged`` opens a record, ``Taken`` sets what is left of it.
* ``confirm``: one ``available(token)`` call for a lot the keeper is about
  to act on. It also sees what no log says: a kill switch, a target
  migration, tokens moved without a take.
* ``read``: the lot record, its window and ``available`` (three or four
  calls), for a token no log was seen for (the whitelist, a state file from
  before the model) and for the rotating audit.

Everything else, every lot's ask at any timestamp included, is arithmetic.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from eth_utils import to_checksum_address

from keepers.auction_keeper.chain.contracts import Auction, Erc20, LotView
from keepers.auction_keeper.chain.events import (
    AuctionEvent,
    LotStaged,
    SettingsChanged,
    Taken,
    auction_events,
)
from keepers.auction_keeper.core.model import Curve, LotRecord

log = logging.getLogger(__name__)


class AuctionBook:
    def __init__(self, auction: Auction):
        self.auction = auction
        self.curve: Curve | None = None
        self.want = ""
        self.receiver = ""
        self.lots: dict[str, LotRecord] = {}
        # Last block whose logs are in the records (None: never scanned).
        self.cursor: int | None = None
        # Block of the last EconomicsSet seen: lots staged up to it are stale.
        self.fence = 0
        # Chain timestamp of each token's last on-chain look (confirm or read).
        self.checked: dict[str, int] = {}

    # Settings

    def load_settings(self) -> None:
        """The curve, the want token and the receiver, from the chain."""
        auction = self.auction
        self.curve = Curve.build(
            auction.start_total(),
            auction.floor_total(),
            auction.step_duration(),
            auction.auction_length(),
        )
        self.want = auction.want()
        self.receiver = auction.receiver()

    # Events

    def apply(self, event: AuctionEvent) -> bool:
        """Fold one event in; True when the settings have to be read again."""
        if isinstance(event, SettingsChanged):
            if event.economics:
                # configured_at: nothing staged up to this block can fill.
                self.fence = event.block
                self.lots = {t: r for t, r in self.lots.items() if r.block > event.block}
            return True
        if isinstance(event, LotStaged):
            if event.block <= self.fence:
                return False
            self.lots[event.token] = LotRecord(
                token=event.token,
                initial_amount=event.initial_amount,
                start=event.start,
                end=event.end,
                available=event.initial_amount,
                block=event.block,
            )
            self.checked.pop(event.token, None)
        elif isinstance(event, Taken):
            record = self.lots.get(event.token)
            if record is not None and event.block >= record.block:
                self.lots[event.token] = record.taken(event.remaining_balance, event.block)
        return False

    def sync(self, latest: int, start_block: int | None, chunk: int) -> int:
        """Fold in the logs up to block ``latest``; the number of events seen.
        A first scan starts at ``start_block`` (the burner's deployment), or
        only looks forward from ``latest`` without one."""
        if self.cursor is None:
            from_block = start_block if start_block is not None else latest
        else:
            from_block = self.cursor + 1
        if from_block > latest:
            return 0
        seen = 0
        reload = self.curve is None
        try:
            # One range at a time, the cursor moving with it: a long first
            # scan that fails half way goes on from there, not from the start.
            for start in range(from_block, latest + 1, chunk):
                stop = min(start + chunk - 1, latest)
                events = auction_events(self.auction.client, self.auction.address, start, stop)
                for event in events:
                    reload = self.apply(event) or reload
                seen += len(events)
                self.cursor = stop
        finally:
            if reload:
                self.load_settings()
        return seen

    # Chain looks

    def confirm(self, token: str, now: int) -> LotRecord | None:
        """Set what is left of a lot inside its window from ``available(token)``."""
        record = self.lots.get(token)
        if record is None:
            return None
        if record.start <= now < record.end:
            record = record.taken(self.auction.available(token), record.block)
            self.lots[token] = record
            self.checked[token] = now
        return record

    def read(self, token: str, now: int) -> LotRecord | None:
        """The token's lot as the chain has it now (None: never staged)."""
        token = to_checksum_address(token)
        self.checked[token] = now
        lot = self.auction.lots(token)
        if lot.staged_at == 0 or lot.initial_amount == 0:
            self.lots.pop(token, None)
            return None
        start, end = self.auction.window(token)
        if start <= now < end:
            available = self.auction.available(token)
        elif now < start:
            # Not sellable yet, so available() answers 0: the balance stands in.
            balance = Erc20(self.auction.client, token).balance_of(self.auction.address)
            available = min(lot.initial_amount, balance)
        else:
            available = 0
        known = self.lots.get(token)
        block = known.block if known is not None and known.start == start else 0
        record = LotRecord(token, lot.initial_amount, start, end, available, block)
        self.lots[token] = record
        return record

    # Views

    def view(self, token: str, timestamp: int) -> LotView | None:
        """The lot as the keeper's rails take it, priced at ``timestamp``."""
        record = self.lots.get(token)
        if record is None or self.curve is None:
            return None
        active = record.active(timestamp)
        return LotView(
            auction=self.auction.address,
            token=token,
            want=self.want,
            receiver=self.receiver,
            timestamp=timestamp,
            start=record.start,
            end=record.end,
            initial_amount=record.initial_amount,
            available=record.available if active else 0,
            amount_needed=record.ask(self.curve, timestamp),
        )

    # Persistence

    def dump(self) -> dict[str, Any]:
        return {
            "cursor": self.cursor,
            "fence": self.fence,
            "lots": {token: asdict(record) for token, record in self.lots.items()},
        }

    def load(self, raw: dict[str, Any]) -> None:
        self.cursor = raw.get("cursor")
        self.fence = int(raw.get("fence", 0))
        self.lots = {token: LotRecord(**record) for token, record in raw.get("lots", {}).items()}


__all__ = ["AuctionBook"]
