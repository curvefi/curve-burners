"""The publishing loop: poll every auction of a network, publish live lots.

One ``NetworkKeeper`` serves one network. Each cycle it

1. discovers candidate tokens (the whitelist plus every token with a
   ``LotStaged`` event since the last scan), filtered by the blacklist;
2. snapshots every candidate's lot and skips inactive ones;
3. estimates each live lot's market (``pricing``) and plans its order
   validity (``schedule``): short inside the band around the market, one
   long order far above it, the uniform TTL when no market is known;
4. resolves the auction's active registry adapters to publishers;
5. (re)publishes, lots nearest the market first, wherever the recorded
   order is missing, expired, or built for a previous lot window, while
   the adapter's open orders stay within ``max_open_orders``; and
6. with the native rail enabled, tries to take each lot through the
   AuctionTaker with a Curve swap (``keepers.auction_keeper.rails.native``), alongside
   whatever the adapters publish.

Between cycles the loop sleeps until the next recorded expiry (at most the
poll interval, or ``native.poll`` while a lot is near its route quote), so
an expiring order is replaced without a gap and without two orders for the
same lot being open at once.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from keepers.auction_keeper.chain.client import ChainClient, Web3Client
from keepers.auction_keeper.chain.contracts import (
    AdapterContract,
    AdapterRegistry,
    Auction,
    LotView,
)
from keepers.auction_keeper.config import NetworkConfig
from keepers.auction_keeper.core import schedule
from keepers.auction_keeper.core.state import KeeperState, OrderRecord
from keepers.auction_keeper.pricing import MarketPricer, build_sources
from keepers.auction_keeper.rails import AdapterPublisher, publisher_classes
from keepers.auction_keeper.rails.native import NativeTaker
from keepers.auction_keeper.routing.curve_router import curve_router_for

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Outcome:
    auction: str
    token: str
    adapter: str | None
    action: str  # posted | kept | skipped | failed | taken | simulated | waiting | unroutable
    detail: str = ""


ACTIONS = ("posted", "kept", "skipped", "failed", "taken", "simulated", "waiting", "unroutable")
NATIVE = "native"


def needs_publish(record: OrderRecord | None, lot: LotView) -> bool:
    """Whether the recorded order no longer covers the lot: none, expired, or
    built for a previous lot window."""
    if record is None:
        return True
    if record.lot_start != lot.start:
        return True  # restaged: a fresh window and curve
    return record.valid_to <= lot.timestamp


class NetworkKeeper:
    def __init__(
        self,
        config: NetworkConfig,
        state: KeeperState,
        *,
        client: ChainClient | None = None,
        dry_run: bool = False,
        publishers: dict[str, type[AdapterPublisher]] | None = None,
        publisher_factory: Callable[..., AdapterPublisher] | None = None,
        pricer: MarketPricer | None = None,
        native: NativeTaker | None = None,
    ):
        self.config = config
        self.state = state
        # Offline mode never loads a key: the loop cannot send by construction.
        self.client = client or Web3Client(
            config.rpc, config.private_key if config.onchain else None
        )
        if config.onchain and self.client.sender is None:
            raise RuntimeError(f"{config.name}: mode keeper but no key in ${config.keeper_key_env}")
        self.dry_run = dry_run
        self.publisher_classes = publishers if publishers is not None else publisher_classes()
        # Optional hook (tests, custom orderbooks): (cls, client, adapter, config) -> publisher.
        self.publisher_factory = publisher_factory
        self.pricer = pricer or MarketPricer(
            build_sources(config, self.client),
            cache_ttl=config.pricing.cache,
            cross_check=config.pricing.cross_check,
        )
        self.native = native
        if self.native is None and config.native.enabled:
            assert config.auction_taker is not None  # enforced by the config parser
            self.native = NativeTaker(
                config,
                self.client,
                curve_router_for(config, self.client),
                taker=config.auction_taker,
            )
        self.auctions = [Auction(self.client, address) for address in config.auctions]
        self._publishers: dict[str, AdapterPublisher] = {}
        # Set when a lot sits near its route quote: the loop polls at native.poll.
        self._fast_poll = False
        self._unknown_adapters: set[str] = set()
        # This cycle's schedule per (auction, token), read by the publishers' planner hook.
        self._plans: dict[tuple[str, str], schedule.Plan] = {}
        # Earliest recorded expiry seen in the last cycle (the loop's wake-up).
        self._next_expiry: int | None = None
        self._cycle_timestamp: int | None = None

    # Discovery

    def candidate_tokens(self, auction: Auction) -> set[str]:
        name = self.config.name
        latest = self.client.latest_block().number
        cursor = self.state.scan_cursor(name, auction.address)
        if cursor is None:
            # First run: scan from start_block, or only forward from here.
            from_block = self.config.start_block if self.config.start_block is not None else latest
        else:
            from_block = cursor + 1
        discovered: set[str] = set()
        if from_block <= latest:
            try:
                discovered = auction.staged_tokens(from_block, latest, chunk=self.config.log_chunk)
            except Exception as exc:  # noqa: BLE001 - discovery must not block publishing
                # The cursor stays put, so the range is retried next cycle; the
                # whitelist and already-known tokens are still served.
                log.warning(
                    "%s: LotStaged scan %s-%s failed (%s); serving known tokens only",
                    auction.address,
                    from_block,
                    latest,
                    exc,
                )
            else:
                self.state.record_scan(name, auction.address, latest, discovered)
        known = self.state.known_tokens(name, auction.address) | discovered
        candidates = known | set(self.config.tokens.whitelist)
        return {token for token in candidates if self.config.tokens.allows(token)}

    def publishers_for(self, auction: Auction) -> list[AdapterPublisher]:
        registry = AdapterRegistry(self.client, auction.registry())
        publishers = []
        for adapter in registry.active_adapters():
            publisher = self._publishers.get(adapter.adapter)
            if publisher is not None and publisher.executor != adapter.executor:
                # Re-pointed executor: rebuild from the fresh registry entry.
                publisher = None
            if publisher is None:
                version = AdapterContract(self.client, adapter.adapter).version()
                cls = self.publisher_classes.get(version or "")
                if cls is None:
                    if adapter.adapter not in self._unknown_adapters:
                        log.warning("no publisher for adapter %s (%s)", adapter.adapter, version)
                        self._unknown_adapters.add(adapter.adapter)
                    continue
                if not cls.enabled(self.config):
                    if adapter.adapter not in self._unknown_adapters:
                        log.info(
                            "rail %s disabled in config; skipping %s", version, adapter.adapter
                        )
                        self._unknown_adapters.add(adapter.adapter)
                    continue
                publisher = (
                    self.publisher_factory(cls, self.client, adapter, self.config)
                    if self.publisher_factory
                    else cls(self.client, adapter, self.config)
                )
                publisher.planner = self.planned_valid_to
                self._publishers[adapter.adapter] = publisher
            publishers.append(publisher)
        return publishers

    # Schedule

    def plan_for(self, auction: Auction, lot: LotView) -> schedule.Plan:
        """Market estimate and order validity for a live lot (cached for the cycle)."""
        key = (auction.address.lower(), lot.token.lower())
        cached = self._plans.get(key)
        if cached is not None:
            return cached
        try:
            market = self.pricer.market(lot)
        except Exception as exc:  # noqa: BLE001 - pricing must not block publishing
            log.warning("%s: market estimate failed (%s); uniform TTL", lot.token, exc)
            market = None
        try:
            planned = schedule.plan(auction, lot, market, self.config.ttl, self.config.order_ttl)
        except Exception as exc:  # noqa: BLE001 - a failed projection falls back to uniform
            log.warning("%s: schedule failed (%s); uniform TTL", lot.token, exc)
            planned = schedule.Plan(
                schedule.uniform_valid_to(lot, self.config.order_ttl),
                "blind",
                market.want_amount if market else None,
            )
        self._plans[key] = planned
        return planned

    def planned_valid_to(self, lot: LotView) -> int | None:
        planned = self._plans.get((lot.auction.lower(), lot.token.lower()))
        return planned.valid_to if planned is not None else None

    # One pass

    def cycle(self) -> list[Outcome]:
        outcomes: list[Outcome] = []
        self._plans.clear()
        self._next_expiry = None
        self._fast_poll = False
        for auction in self.auctions:
            try:
                outcomes.extend(self._cycle_auction(auction))
            except Exception as exc:  # noqa: BLE001 - keep serving the other auctions
                log.exception("auction %s: cycle failed", auction.address)
                outcomes.append(Outcome(auction.address, "", None, "failed", str(exc)))
        return outcomes

    def _cycle_auction(self, auction: Auction) -> list[Outcome]:
        outcomes: list[Outcome] = []
        tokens = sorted(self.candidate_tokens(auction))
        publishers = self.publishers_for(auction)
        if not publishers:
            log.info("auction %s: no active adapters with a publisher", auction.address)
        live: list[tuple[LotView, schedule.Plan]] = []
        for token in tokens:
            lot = auction.lot_view(token)
            self._cycle_timestamp = lot.timestamp
            if not lot.active:
                outcomes.append(Outcome(auction.address, token, None, "skipped", "inactive lot"))
                continue
            planned = self.plan_for(auction, lot)
            log.debug("%s: %s", lot.token, planned)
            live.append((lot, planned))
        # Nearest the market first: a far lot waits for a budget slot, a near one never.
        live.sort(key=lambda item: (ZONE_RANK[item[1].zone], item[1].band_entry or 0))
        for publisher in publishers:
            open_orders = self.state.open_orders(
                self.config.name,
                auction.address,
                publisher.address,
                self._cycle_timestamp or 0,
            )
            budget = self.config.max_open_orders - len(open_orders)
            for lot, planned in live:
                outcome = self._publish(auction, publisher, lot, planned, budget)
                if outcome.action == "posted":
                    budget -= 1
                outcomes.append(outcome)
        if self.native is not None:
            for lot, _ in live:
                outcomes.append(self._take(auction, lot))
        return outcomes

    def _take(self, auction: Auction, lot: LotView) -> Outcome:
        """The native rail's try at the lot; offline mode only simulates."""
        assert self.native is not None
        result = self.native.attempt(auction, lot, dry_run=self.dry_run or not self.config.onchain)
        if result.near:
            self._fast_poll = True
        return Outcome(auction.address, lot.token, NATIVE, result.action, result.detail)

    def _publish(
        self,
        auction: Auction,
        publisher: AdapterPublisher,
        lot: LotView,
        planned: schedule.Plan,
        budget: int,
    ) -> Outcome:
        name = self.config.name
        record = self.state.order(name, auction.address, publisher.address, lot.token)
        if not needs_publish(record, lot):
            assert record is not None
            self._note_expiry(record.valid_to)
            return Outcome(auction.address, lot.token, publisher.address, "kept", record.uid)
        if budget <= 0:
            return Outcome(
                auction.address,
                lot.token,
                publisher.address,
                "skipped",
                f"open-order budget ({self.config.max_open_orders}) spent; {planned}",
            )
        try:
            result = publisher.publish(auction, lot, dry_run=self.dry_run)
        except Exception as exc:  # noqa: BLE001 - one rail failing must not stop the rest
            log.exception("%s via %s: publish failed", lot.token, publisher.version)
            return Outcome(auction.address, lot.token, publisher.address, "failed", str(exc))
        if result is None:
            return Outcome(
                auction.address, lot.token, publisher.address, "skipped", "not published"
            )
        self._note_expiry(result.valid_to)
        if not self.dry_run:
            self.state.set_order(
                name,
                auction.address,
                publisher.address,
                lot.token,
                OrderRecord(
                    uid=result.uid,
                    valid_to=result.valid_to,
                    sell_amount=result.sell_amount,
                    buy_amount=result.buy_amount,
                    posted_at=lot.timestamp,
                    lot_start=lot.start,
                ),
            )
        return Outcome(
            auction.address, lot.token, publisher.address, "posted", f"{result.uid} {planned}"
        )

    def _note_expiry(self, valid_to: int) -> None:
        if self._next_expiry is None or valid_to < self._next_expiry:
            self._next_expiry = valid_to

    def wait_seconds(self, elapsed: float) -> float:
        """Sleep until the next recorded expiry or the poll interval, whichever
        is sooner (never below one second once something is due)."""
        remaining = self.config.poll_interval - elapsed
        if self._fast_poll:
            remaining = min(remaining, self.config.native.poll - elapsed)
        if self._next_expiry is not None and self._cycle_timestamp is not None:
            until_expiry = self._next_expiry - self._cycle_timestamp - elapsed
            remaining = min(remaining, max(1.0, until_expiry))
        return max(0.0, remaining)

    # Loop

    def run(
        self,
        stop: threading.Event | None = None,
        *,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        """Cycle until ``stop`` is set, sleeping until the next order expiry
        or ``poll_interval`` seconds, whichever comes first.

        ``wait(seconds)`` blocks between cycles and returns True to stop; the
        default is ``stop.wait``.
        """
        stop = stop or threading.Event()
        wait = wait or stop.wait
        while not stop.is_set():
            started = time.monotonic()
            outcomes = self.cycle()
            summary = {
                action: count
                for action in ACTIONS
                if (count := sum(1 for o in outcomes if o.action == action))
            }
            log.info("%s: %s", self.config.name, summary)
            if wait(self.wait_seconds(time.monotonic() - started)):
                break


ZONE_RANK = {"near": 0, "below": 1, "blind": 2, "far": 3}


def run_networks(
    keepers: list[NetworkKeeper], *, once: bool = False, stop: threading.Event | None = None
) -> None:
    """Run each keeper in its own thread (or one cycle each with ``once``)."""
    if once:
        for keeper in keepers:
            for outcome in keeper.cycle():
                log.info("%s %s", keeper.config.name, outcome)
        return
    stop = stop or threading.Event()
    threads = [
        threading.Thread(target=keeper.run, args=(stop,), name=keeper.config.name, daemon=True)
        for keeper in keepers
    ]
    for thread in threads:
        thread.start()
    try:
        while any(thread.is_alive() for thread in threads):
            for thread in threads:
                thread.join(timeout=1.0)
    except KeyboardInterrupt:
        stop.set()
        for thread in threads:
            thread.join(timeout=5.0)
