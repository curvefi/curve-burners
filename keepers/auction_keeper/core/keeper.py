"""The keeper loop: rank every lot locally, work only on the ones that matter.

One ``NetworkKeeper`` serves one network. Each cycle, per auction, it

1. reads the latest block and the auction's logs since the last cycle (two
   requests) into the local book (``core.book``): staged lots, takes,
   changed settings;
2. ranks every live lot from the local model and the remembered market
   estimates (``core.focus.rank``): the ask the NEXT block will see
   (``lookahead``) against the highest estimate. No request per lot;
3. spends ``pricing.budget`` seconds refreshing the estimates that are due,
   the lots nearest their market first;
4. selects the lots to work on (``core.focus.select``): everything within
   ``focus.threshold`` of its market, the ``focus.nearest`` nearest of the
   rest and a few unpriced ones. Sold, ended and dust lots are never
   selected, and a far lot costs nothing until the curve brings it near;
5. confirms each selected lot with one ``available`` read (reused for
   ``focus.confirm`` seconds), plans its order validity (``core.schedule``)
   and (re)publishes through the auction's active adapters wherever the
   recorded order is missing, expired or built for a previous window,
   within ``max_open_orders``. The records of lots that are sold or over
   are dropped first, so no dead order holds a slot; long ``far`` orders
   get half the slots at most; a post that failed is tried again after a
   pause that doubles, not on every cycle; and
6. with the native rail enabled, quotes the selected lots that are due for
   a route quote and takes the ones whose next-block ask is under it, as
   many at once as the client has sending accounts.

What a cycle may ask for is bounded: the node gets two requests plus the
work on at most ``focus.max_lots`` lots, and every HTTP host has a request
budget of its own (``keepers.auction_keeper.limits``).

Between cycles the loop sleeps until the next order expiry or the moment the
next block crosses into a new price step of a lot it is working on, at most
``poll_interval``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from keepers.auction_keeper.chain.client import Block, ChainClient, PriorityFee, Web3Client
from keepers.auction_keeper.chain.contracts import (
    AdapterContract,
    AdapterRegistry,
    Auction,
    Erc20,
    LotView,
)
from keepers.auction_keeper.config import NetworkConfig
from keepers.auction_keeper.core import focus, schedule
from keepers.auction_keeper.core.approvals import ApprovalSync
from keepers.auction_keeper.core.book import AuctionBook
from keepers.auction_keeper.core.fees import GWEI, take_priority_fee
from keepers.auction_keeper.core.focus import Ranked
from keepers.auction_keeper.core.model import LotRecord
from keepers.auction_keeper.core.state import KeeperState, OrderRecord
from keepers.auction_keeper.limits import GATES, Throttled
from keepers.auction_keeper.pricing import MarketPricer, MarketQuote, build_sources
from keepers.auction_keeper.rails import AdapterPublisher, publisher_classes
from keepers.auction_keeper.rails.native import NativeResult, NativeTaker, TakeQuote, router_for
from keepers.auction_keeper.routing.curve_router import WRAPPED_NATIVE

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Outcome:
    auction: str
    token: str
    adapter: str | None
    action: str  # posted | kept | skipped | failed | taken | simulated | waiting | unroutable
    detail: str = ""


ACTIONS = ("posted", "kept", "skipped", "failed", "taken", "simulated", "waiting", "unroutable")
# Outcomes that are the steady state of a cycle: nothing to report.
QUIET = frozenset(("kept", "waiting", "skipped"))
NATIVE = "native"
# Longest pause, in seconds, before a post that keeps failing is tried again.
MAX_POST_BACKOFF = 600
# Chunks of one lot the native rail takes in a row within a cycle.
MAX_TAKES_PER_CYCLE = 16


def needs_publish(record: OrderRecord | None, lot: LotView) -> bool:
    """Whether the recorded order no longer covers the lot: none, expired, or
    built for a previous lot window."""
    if record is None:
        return True
    if record.lot_start != lot.start:
        return True  # restaged: a fresh window and curve
    return record.valid_to <= lot.timestamp


@dataclass(frozen=True)
class RouteNote:
    """The native rail's last route quote of a lot (``dy`` want for
    ``amount``; 0: nothing routed), and the chain time it was made at."""

    at: int
    start: int
    amount: int = 0
    dy: int = 0


@dataclass(frozen=True)
class Survey:
    """One auction as a cycle sees it, before any per-lot request."""

    book: AuctionBook
    now: int  # the latest block's timestamp
    ts: int  # the timestamp lots are priced for (now + lookahead)
    ranked: list[Ranked]
    chosen: list[Ranked]


class NetworkKeeper:
    # Seconds the auction's active adapters are reused before the registry is read again.
    adapters_ttl = 300.0
    # Seconds between writes of the books and prices to the state file when no log came in.
    save_interval = 30.0
    # Seconds between the summary lines of cycles in which nothing happened.
    heartbeat = 600.0

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
            config.rpc,
            config.private_keys if config.onchain else None,
            rpc_rate=config.rpc_rate,
            send_rpc=config.send_rpc,
            receipt_timeout=config.receipt_timeout,
        )
        if config.onchain and self.client.sender is None:
            raise RuntimeError(f"{config.name}: mode keeper but no key in ${config.keeper_key_env}")
        self.dry_run = dry_run
        # Transactions that can be in flight at once: one per sending account.
        self.lanes = len(getattr(self.client, "senders", ())) or 1
        if config.onchain:
            self._report_accounts()
        self.publisher_classes = publishers if publishers is not None else publisher_classes()
        # Optional hook (tests, custom orderbooks): (cls, client, adapter, config) -> publisher.
        self.publisher_factory = publisher_factory
        self.pricer = pricer or MarketPricer(build_sources(config, self.client))
        self.pricer.load(state.prices(config.name))
        self.native = native
        if self.native is None and config.native.enabled:
            assert config.auction_taker is not None  # enforced by the config parser
            self.native = NativeTaker(
                config,
                self.client,
                router_for(config, self.client),
                taker=config.auction_taker,
            )
        self.auctions = [Auction(self.client, address) for address in config.auctions]
        self.books: dict[str, AuctionBook] = {}
        for auction in self.auctions:
            book = self.books[auction.address] = AuctionBook(auction)
            saved = state.book(config.name, auction.address)
            if saved:
                book.load(saved)
        # Seconds past the latest block that lots are priced for (the next block).
        self.ahead = config.lookahead.ahead(self.client.chain_id)
        if config.lookahead.seconds is None and config.lookahead.blocks and not self.ahead:
            log.warning(
                "%s: no block time known for chain %s; set lookahead.seconds (pricing the latest block)",
                config.name,
                self.client.chain_id,
            )
        # Executor allowances, synced once per token (``approvals: startup``).
        self.approvals = ApprovalSync(
            self.client,
            send=config.onchain,
            dry_run=dry_run,
            workers=config.pricing.workers,
        )
        self._publishers: dict[str, AdapterPublisher] = {}
        self._adapters: dict[str, tuple[float, list[AdapterPublisher]]] = {}
        self._unknown_adapters: set[str] = set()
        self._want_units: dict[str, int] = {}
        # The native rail's last route quote per (auction, token).
        self._routes: dict[tuple[str, str], RouteNote] = {}
        # This cycle's schedule per (auction, token), read by the publishers' planner hook.
        self._plans: dict[tuple[str, str], schedule.Plan] = {}
        self._clock: tuple[float, Block] | None = None
        # (adapter, token) -> (chain time the next post may be tried at, the pause that led there).
        self._post_retry: dict[tuple[str, str], tuple[int, int]] = {}
        # What the last summary line said and when, and the request counts it had.
        self._reported: tuple[dict[str, int], float] = ({}, float("-inf"))
        self._rpc_seen = 0
        # The sending accounts' native balances and when they were read (gas refill).
        self._balances: dict[str, int] = {}
        self._balances_at = float("-inf")
        self._http_seen: dict[str, tuple[int, int]] = {}
        self._rotation = 0
        self._saved_at = 0.0
        self._dirty = False
        # Earliest recorded expiry seen in the last cycle, and the chain time
        # at which the next block first prices a worked lot one step lower
        # (the loop's wake-ups).
        self._next_expiry: int | None = None
        self._next_step: int | None = None
        self._cycle_timestamp: int | None = None

    def _report_accounts(self) -> None:
        """Name the sending accounts once, and the ones that cannot pay for gas."""
        balance = getattr(self.client, "balance", None)
        for address in getattr(self.client, "senders", ()):
            try:
                wei = balance(address) if balance else None
            except Exception as exc:  # noqa: BLE001 - only a report
                log.warning("%s: balance of %s unknown: %s", self.config.name, address, exc)
                continue
            if wei == 0:
                log.warning("%s: sending account %s has no gas money", self.config.name, address)
            elif wei is not None:
                log.info(
                    "%s: sending account %s, balance %.5f", self.config.name, address, wei / 1e18
                )

    # The chain clock

    def block(self, *, fresh: bool = False) -> Block:
        """The latest block; one read serves everything within a second."""
        if not fresh and self._clock is not None and time.monotonic() - self._clock[0] < 1.0:
            return self._clock[1]
        block = self.client.latest_block()
        self._clock = (time.monotonic(), block)
        return block

    # The book

    def allows(self, token: str) -> bool:
        return self.config.tokens.allows(token)

    def sync(self, auction: Auction, block: Block) -> AuctionBook:
        """Bring the auction's book up to ``block``: its logs, its settings on
        first use, and a chain read of every whitelisted token no log named."""
        book = self.books[auction.address]
        try:
            if book.sync(block.number, self.config.start_block, self.config.log_chunk):
                self._dirty = True
        except Exception as exc:  # noqa: BLE001 - the known lots are still served
            # The cursor stays at the last range read: the scan goes on from
            # there next cycle.
            log.warning(
                "%s: log scan stopped at block %s of %s (%s)",
                auction.address,
                book.cursor,
                block.number,
                exc,
            )
            self._dirty = True
        if book.curve is None:
            book.load_settings()
        for token in self.config.tokens.whitelist:
            if token not in book.lots and token not in book.checked and self.allows(token):
                self._read(book, token, block.timestamp)
        return book

    def _read(self, book: AuctionBook, token: str, now: int) -> None:
        try:
            known = book.lots.get(token)
            if book.read(token, now) != known:
                self._dirty = True
        except Exception as exc:  # noqa: BLE001 - one token must not stop the cycle
            log.warning("%s: lot read failed: %s", token, exc)
            book.checked[token] = now

    def audit(self, book: AuctionBook, now: int) -> None:
        """Read ``focus.audit`` lots back from the chain, the ones looked at
        longest ago first: what no log says (a kill switch, a lot staged
        before the scan, tokens moved without a take) is found this way."""
        count = self.config.focus.audit
        if not count:
            return
        # A lot whose window is over can only come back through a LotStaged
        # log; the whitelist is read regardless (it needs no log scan).
        tokens = [
            token
            for token in {*book.lots, *self.config.tokens.whitelist}
            if self.allows(token)
            and (token in self.config.tokens.whitelist or book.lots[token].end > now)
        ]
        tokens.sort(key=lambda token: (book.checked.get(token, -1), token))
        for token in tokens[:count]:
            if book.checked.get(token) != now:
                self._read(book, token, now)

    # Ranking

    def estimate(self, book: AuctionBook, record: LotRecord) -> MarketQuote | None:
        return self.pricer.cached(book.auction.address, record.token, record.available)

    def rank(self, book: AuctionBook, ts: int) -> list[Ranked]:
        """Every live, allowed, non-dust lot of the book, nearest its market
        first. Pure arithmetic over the book and the remembered estimates."""
        assert book.curve is not None
        dust = 0.0
        if self.config.dust:
            dust = self.config.dust * self._want_unit(book.want)
        return focus.rank(
            (record for record in book.lots.values() if self.allows(record.token)),
            book.curve,
            ts,
            lambda record: self.estimate(book, record),
            dust=dust,
        )

    def refresh_estimates(self, book: AuctionBook, ranked: list[Ranked], ts: int) -> int:
        """Ask the sources again about the lots whose estimate is due, most
        urgent first, for at most ``pricing.budget`` seconds; lots staged for
        a window that has not opened are priced ahead of it. Returns how many
        were asked about."""
        pricing = self.config.pricing
        if not self.pricer.sources or not pricing.budget:
            return 0
        assert book.curve is not None
        address = book.auction.address
        upcoming = [
            Ranked(record, 0, None, None)
            for record in book.lots.values()
            if record.start > ts and record.available > 0 and self.allows(record.token)
        ]
        queue = focus.stale(
            ranked + upcoming,
            book.curve,
            lambda record: self.pricer.age(address, record.token),
            self.config.focus,
            pricing,
        )
        if not queue:
            return 0
        deadline = time.monotonic() + pricing.budget

        def one(item: Ranked) -> bool:
            if time.monotonic() >= deadline:
                return False
            lot = book.view(item.token, max(ts, item.record.start))
            if lot is None or not lot.active:
                return False
            try:
                self.pricer.refresh(lot, fresh=pricing.cache)
            except Exception as exc:  # noqa: BLE001 - pricing must not block the cycle
                log.warning("%s: market estimate failed (%s)", item.token, exc)
                return False
            return True

        workers = min(pricing.workers, len(queue))
        if workers <= 1 or not getattr(self.client, "concurrent", False):
            asked = sum(one(item) for item in queue)
        else:
            with ThreadPoolExecutor(workers, thread_name_prefix="price") as pool:
                asked = sum(pool.map(one, queue))
        if asked:
            self._dirty = True
            log.debug("%s: %s of %s due estimates refreshed", address, asked, len(queue))
        return asked

    def survey(self, auction: Auction, *, refresh: bool = True) -> Survey:
        """Steps 1-4 of the cycle: the book brought up to date, every lot
        ranked, the lots to work on selected."""
        block = self.block(fresh=True)
        book = self.sync(auction, block)
        now = block.timestamp
        ts = now + self.ahead
        self.audit(book, now)
        ranked = self.rank(book, ts)
        if refresh and self.refresh_estimates(book, ranked, ts):
            ranked = self.rank(book, ts)
        chosen = focus.select(ranked, self.config.focus, rotation=self._rotation)
        return Survey(book, now, ts, ranked, chosen)

    # Adapters

    def publishers_for(self, auction: Auction) -> list[AdapterPublisher]:
        cached = self._adapters.get(auction.address)
        if cached is not None and time.monotonic() - cached[0] < self.adapters_ttl:
            return cached[1]
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
        self._adapters[auction.address] = (time.monotonic(), publishers)
        return publishers

    # Schedule

    def plan_for(self, book: AuctionBook, lot: LotView) -> schedule.Plan:
        """The lot's zone and order validity from its remembered estimate
        (kept for the cycle; no request)."""
        key = (lot.auction.lower(), lot.token.lower())
        record = book.lots[lot.token]
        curve = book.curve
        assert curve is not None
        planned = schedule.plan(
            lot,
            self.estimate(book, record),
            self.config.ttl,
            self.config.order_ttl,
            reaches=lambda target: record.reaches(curve, target),
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
        self._next_step = None
        for auction in self.auctions:
            try:
                outcomes.extend(self._cycle_auction(auction))
            except Exception as exc:  # noqa: BLE001 - keep serving the other auctions
                log.exception("auction %s: cycle failed", auction.address)
                outcomes.append(Outcome(auction.address, "", None, "failed", str(exc)))
        self._rotation += 1
        self.save()
        return outcomes

    def save(self, *, force: bool = False) -> None:
        """Write the books and the estimates to the state file: after a cycle
        that changed them, at most every ``save_interval`` seconds otherwise."""
        due = time.monotonic() - self._saved_at >= self.save_interval
        if self.dry_run or not (force or self._dirty or due):
            return
        for address, book in self.books.items():
            self.state.set_book(self.config.name, address, book.dump())
        self.state.set_prices(self.config.name, self.pricer.dump())
        self.state.save()
        self._saved_at = time.monotonic()
        self._dirty = False

    def _cycle_auction(self, auction: Auction) -> list[Outcome]:
        outcomes: list[Outcome] = []
        started = time.monotonic()
        seen = self.survey(auction)
        book, now, ts = seen.book, seen.now, seen.ts
        self._cycle_timestamp = now
        live: list[tuple[LotView, schedule.Plan]] = []
        for item in seen.chosen:
            token = item.token
            if (
                now - book.checked.get(token, -self.config.focus.confirm)
                >= self.config.focus.confirm
            ):
                try:
                    book.confirm(token, now)
                except Exception as exc:  # noqa: BLE001 - the model's record still stands
                    log.warning("%s: availability read failed: %s", token, exc)
            lot = book.view(token, ts)
            if lot is None or not lot.active:
                self._dirty = True
                outcomes.append(Outcome(auction.address, token, None, "skipped", "inactive lot"))
                continue
            planned = self.plan_for(book, lot)
            log.debug("%s: %s", token, planned)
            live.append((lot, planned))
            self._note_step(book, lot, planned)
        # Nearest the market first: a far lot waits for a budget slot, a near one never.
        live.sort(key=lambda item: (ZONE_RANK[item[1].zone], item[1].band_entry or 0))
        if self.config.approvals == "startup":
            self._sync_approvals(auction, seen)
        if live:
            self._publish_all(auction, book, live, now, outcomes)
        if self.native is not None and live:
            due = [lot for lot, planned in live if self._native_due(book, lot, planned, now)]
            quotes = self._quote_all(book, due)
            pairs = list(zip(due, quotes, strict=True))
            if self.lanes > 1 and len(pairs) > 1:
                # One take per sending account at once: a take waits for its
                # receipt, and the other lots' prices do not wait with it.
                with ThreadPoolExecutor(
                    min(self.lanes, len(pairs)), thread_name_prefix="take"
                ) as pool:
                    outcomes.extend(pool.map(lambda pair: self._take(book, *pair), pairs))
            else:
                outcomes.extend(self._take(book, lot, quoted) for lot, quoted in pairs)
            asked = {lot.token for lot in due}
            outcomes.extend(
                Outcome(auction.address, lot.token, NATIVE, "waiting", "last route quote stands")
                for lot, _ in live
                if lot.token not in asked
            )
        log.debug(
            "%s: %s live lots, %s worked, %.2fs",
            auction.address,
            len(seen.ranked),
            len(live),
            time.monotonic() - started,
        )
        return outcomes

    def _note_step(self, book: AuctionBook, lot: LotView, planned: schedule.Plan) -> None:
        """Remember when the next block first prices this lot a step lower,
        if the lot is somewhere a step matters (not far above everything)."""
        if planned.zone == "far" or book.curve is None:
            return
        step = book.curve.step_duration
        edge = lot.start + ((lot.timestamp - lot.start) // step + 1) * step
        wake = edge - self.ahead
        if self._next_step is None or wake < self._next_step:
            self._next_step = wake

    # Adapters' orders

    def _sync_approvals(self, auction: Auction, seen: Survey) -> None:
        """Grant every active adapter's executor the lots it may be asked to
        pull: the live ones (dust left out) and the ones staged for a window
        that has not opened. The first cycle covers them all in batches;
        later cycles only find the lots staged since."""
        amounts = {item.token: item.record.available for item in seen.ranked}
        for record in seen.book.lots.values():
            if record.start > seen.ts and record.available > 0 and self.allows(record.token):
                amounts.setdefault(record.token, record.available)
        if not amounts:
            return
        try:
            for spender in {publisher.spender for publisher in self.publishers_for(auction)}:
                if self.approvals.sync(auction, spender, amounts):
                    self._clock = None  # the sends took a while: read the block again
        except Exception as exc:  # noqa: BLE001 - orders are posted regardless
            log.warning("%s: allowance sync failed: %s", auction.address, exc)

    def _publish_all(
        self,
        auction: Auction,
        book: AuctionBook,
        live: list[tuple[LotView, schedule.Plan]],
        now: int,
        outcomes: list[Outcome],
    ) -> None:
        publishers = self.publishers_for(auction)
        if not publishers:
            log.debug("auction %s: no active adapters with a publisher", auction.address)
            return
        name, limit = self.config.name, self.config.max_open_orders
        lots = {token.lower(): record for token, record in book.lots.items()}
        for publisher in publishers:
            records = self.state.adapter_orders(name, auction.address, publisher.address)
            # An order whose lot is sold, over or restaged can no longer fill
            # (the auction's router rejects it): its record must not hold a slot.
            gone = [
                token
                for token, record in records.items()
                if (lot_record := lots.get(token)) is None
                or lot_record.start != record.lot_start
                or lot_record.available == 0
                or now >= lot_record.end
            ]
            if gone and not self.dry_run:
                self.state.drop_orders(name, auction.address, publisher.address, gone)
            open_orders = [
                record
                for token, record in records.items()
                if token not in gone and record.valid_to > now
            ]
            budget = limit - len(open_orders)
            # A far order stands for up to ttl.max and cannot be withdrawn:
            # half of the slots stay free for the lots that reach a price.
            far_room = max(1, limit // 2) - sum(1 for record in open_orders if record.zone == "far")
            for lot, planned in live:
                if not self.config.lookahead.orders and lot.timestamp != now:
                    # The order is priced for the latest block: an orderbook
                    # checks the signature against the chain as it is.
                    current = book.view(lot.token, now)
                    if current is None or not current.active:
                        continue  # the window opens with the next block
                    lot = current
                outcome = self._publish(auction, publisher, lot, planned, budget, far_room)
                if outcome.action == "posted":
                    budget -= 1
                    if planned.zone == "far":
                        far_room -= 1
                outcomes.append(outcome)

    def _publish(
        self,
        auction: Auction,
        publisher: AdapterPublisher,
        lot: LotView,
        planned: schedule.Plan,
        budget: int,
        far_room: int = 1,
    ) -> Outcome:
        name = self.config.name

        def skipped(why: str) -> Outcome:
            return Outcome(auction.address, lot.token, publisher.address, "skipped", why)

        record = self.state.order(name, auction.address, publisher.address, lot.token)
        # Inside the band an order may be replaced at every step of the curve.
        stepped = (
            self.config.ttl.every_step
            and record is not None
            and record.lot_start == lot.start
            and planned.zone == "near"
            and 0 < lot.amount_needed < record.buy_amount
        )
        if not stepped and not needs_publish(record, lot):
            assert record is not None
            self._note_expiry(record.valid_to)
            return Outcome(auction.address, lot.token, publisher.address, "kept", record.uid)
        if self.approvals.blocked(auction.address, publisher.spender, lot.token):
            return Outcome(
                auction.address, lot.token, publisher.address, "skipped", "allowance not synced"
            )
        if budget <= 0:
            return Outcome(
                auction.address,
                lot.token,
                publisher.address,
                "skipped",
                f"open-order budget ({self.config.max_open_orders}) spent; {planned}",
            )
        if planned.zone == "far" and far_room <= 0:
            return skipped(f"far-order slots spent; {planned}")
        retry_key = (publisher.address, lot.token)
        retry_at, pause = self._post_retry.get(retry_key, (0, 0))
        if lot.timestamp < retry_at:
            return skipped(f"post failed, next try in {retry_at - lot.timestamp}s")
        # A post that fails is tried again after a pause that doubles, not on
        # every cycle: the orderbook's answer rarely changes within a block.
        pause = min(max(2 * pause, self.config.ttl.min_ttl), MAX_POST_BACKOFF)
        try:
            result = publisher.publish(auction, lot, dry_run=self.dry_run)
        except Throttled as exc:
            return skipped(str(exc))
        except Exception as exc:  # noqa: BLE001 - one rail failing must not stop the rest
            # An answer of the rail's backend is one line; anything else is a bug to trace.
            log.warning(
                "%s via %s: publish failed: %s",
                lot.token,
                publisher.version,
                exc,
                exc_info=not isinstance(exc, RuntimeError),
            )
            self._post_retry[retry_key] = (lot.timestamp + pause, pause)
            return Outcome(auction.address, lot.token, publisher.address, "failed", str(exc))
        if result is None:
            self._post_retry[retry_key] = (lot.timestamp + pause, pause)
            return skipped("not published")
        self._post_retry.pop(retry_key, None)
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
                    zone=planned.zone,
                ),
            )
        return Outcome(
            auction.address, lot.token, publisher.address, "posted", f"{result.uid} {planned}"
        )

    def _note_expiry(self, valid_to: int) -> None:
        if self._next_expiry is None or valid_to < self._next_expiry:
            self._next_expiry = valid_to

    # The native rail

    def _native_due(
        self, book: AuctionBook, lot: LotView, planned: schedule.Plan, now: int
    ) -> bool:
        """Whether the lot gets a route quote this cycle: always when it has
        none for this window or its ask is within the band over the last one;
        otherwise every ``native.requote`` seconds."""
        note = self._routes.get((lot.auction.lower(), lot.token.lower()))
        if note is None or note.start != lot.start:
            return True
        if now - note.at >= self.config.native.requote:
            return True
        if not note.dy or book.curve is None:
            return False
        # What is left of the lot is compared pro rata with the quoted chunk.
        record = book.lots[lot.token]
        amount = min(note.amount, record.available)
        ask = record.ask(book.curve, lot.timestamp, amount)
        return ask <= note.dy * amount // note.amount * (1 + self.config.ttl.near_above)

    def _quote_all(
        self, book: AuctionBook, lots: list[LotView]
    ) -> list[TakeQuote | NativeResult | None]:
        """The native rail's quote for every lot, ``native.workers`` at a time:
        a quote is a handful of calls to a router, or seconds of a route
        solver, and the lots do not depend on each other. The chunk is priced
        by the local model. The takes that follow are sent one by one."""
        assert self.native is not None and book.curve is not None
        native, curve = self.native, book.curve

        def one(lot: LotView) -> TakeQuote | NativeResult | None:
            record = book.lots[lot.token]
            try:
                return native.try_quote(
                    book.auction, lot, lambda amount: record.ask(curve, lot.timestamp, amount)
                )
            except Exception as exc:  # noqa: BLE001 - one lot must not stop the others
                log.warning("%s: native quote failed: %s", lot.token, exc)
                return None

        workers = min(self.config.native.workers, len(lots))
        if workers <= 1 or not getattr(self.client, "concurrent", False):
            return [one(lot) for lot in lots]
        with ThreadPoolExecutor(workers, thread_name_prefix="quote") as pool:
            return list(pool.map(one, lots))

    def _take(
        self, book: AuctionBook, lot: LotView, quoted: TakeQuote | NativeResult | None
    ) -> Outcome:
        """The native rail's try at the lot with the quote made for it;
        offline mode only simulates. The chunk is priced again for the block
        after the latest one at this moment (the quote may have taken a
        while), and the take is simulated with that block's clock."""
        assert self.native is not None and book.curve is not None
        auction, curve, token = book.auction, book.curve, lot.token
        key = (auction.address.lower(), token.lower())
        dry_run = self.dry_run or not self.config.onchain
        if quoted is None:
            return Outcome(auction.address, token, NATIVE, "failed", "quote failed")
        result = taken = NativeResult("failed", "")
        for attempt in range(MAX_TAKES_PER_CYCLE):
            block = self.block()
            ts = block.timestamp + self.ahead
            if attempt:
                # A lot taken in chunks is taken back to back: the curve keeps
                # falling, so a chunk left for later pays the auction less.
                record = book.confirm(token, block.timestamp)
                fresh = book.view(token, ts)
                if record is None or fresh is None or not fresh.active:
                    break
                lot = fresh
                quoted = self.native.try_quote(
                    auction, lot, lambda amount, r=record, t=ts: r.ask(curve, t, amount)
                )
            record = book.lots[token]
            if isinstance(quoted, TakeQuote):
                if quoted.amount > record.available or not record.active(ts):
                    self._routes.pop(key, None)
                    return Outcome(auction.address, token, NATIVE, "skipped", "lot changed")
                quoted = replace(quoted, payment=record.ask(curve, ts, quoted.amount))
                self._routes[key] = RouteNote(block.timestamp, lot.start, quoted.amount, quoted.dy)
                self.pricer.note(replace(lot, available=quoted.amount), "route", quoted.dy)
            else:
                self._routes[key] = RouteNote(block.timestamp, lot.start)
            refill_to, short = self.refill_target()
            result = self.native.attempt(
                auction,
                lot,
                dry_run=dry_run,
                quoted=quoted,
                at=ts if self.ahead else None,
                priority_fee=lambda quote: self._priority_fee(book, quote),
                native_price=self._native_price(book),
                want_unit=self._want_unit(book.want),
                refill_to=refill_to,
                top_up=short,
            )
            if result.action != "taken":
                break
            taken = result
            self._balances_at = float("-inf")  # the take spent gas: read the balances again
            self._routes.pop(key, None)  # the pools moved: quote again
            self._dirty = True
        if result.near:
            self._note_step(book, lot, schedule.Plan(lot.end, "near"))
        if taken.action == "taken":
            result = taken
        return Outcome(auction.address, token, NATIVE, result.action, result.detail)

    def native_price(self, book: AuctionBook) -> float | None:
        """Want tokens per native token, for the takes' tip: the configured
        ``fees.native_price``, else the price sources' estimate of the wrapped
        native token (asked again every ``pricing.cache_far`` seconds)."""
        if self.config.fees.native_price is not None:
            return self.config.fees.native_price
        wrapped = next(iter(WRAPPED_NATIVE.get(self.client.chain_id, ())), None)
        if wrapped is None:
            return None
        if wrapped == book.want.lower():
            return 1.0
        if not self.pricer.sources:
            return None
        unit = 10**18
        lot = LotView(
            book.auction.address, wrapped, book.want, book.receiver, 0, 0, 0, unit, unit, 0
        )
        age = self.pricer.age(lot.auction, wrapped)
        if age is None or age >= self.config.pricing.cache_far:
            self.pricer.refresh(lot)
        quote = self.pricer.cached(lot.auction, wrapped, unit)
        if quote is None:
            return None
        return quote.want_amount / self._want_unit(book.want)

    # Seconds the sending accounts' balances are reused.
    balances_ttl = 300.0

    def refill_target(self) -> tuple[str | None, bool]:
        """The sending account a take buys gas for, and whether it is short
        of gas (under ``native.refill_below``): the poorest account, so the
        balances even out whoever sends. (None, False): no refill."""
        settings = self.config.native
        if not (settings.gas_refill or settings.refill_below):
            return None, False
        senders = getattr(self.client, "senders", None) or (
            [self.client.sender] if self.client.sender else []
        )
        if not senders:
            return None, False
        balance = getattr(self.client, "balance", None)
        if balance is None or (len(senders) == 1 and not settings.refill_below):
            return senders[0], False
        if time.monotonic() - self._balances_at >= self.balances_ttl:
            self._balances_at = time.monotonic()
            for address in senders:
                try:
                    self._balances[address] = balance(address)
                except Exception as exc:  # noqa: BLE001 - the last reading stands
                    log.warning("balance of %s unknown: %s", address, exc)
        if not self._balances:
            return (senders[0], False) if settings.gas_refill else (None, False)
        poorest = min(self._balances, key=lambda address: self._balances[address])
        short = bool(settings.refill_below) and (
            self._balances[poorest] < settings.refill_below * 10**18
        )
        return (poorest, short) if settings.gas_refill or short else (None, False)

    def _native_price(self, book: AuctionBook) -> float | None:
        try:
            return self.native_price(book)
        except Exception as exc:  # noqa: BLE001 - the tip falls back to the node's
            log.warning("native token price failed (%s)", exc)
            return None

    def _priority_fee(self, book: AuctionBook, quote: TakeQuote) -> PriorityFee:
        """The tip of a take of ``quote``: a share of its profit (``core.fees``)."""
        price = self._native_price(book)
        unit = self._want_unit(book.want)

        def tip(gas: int, base_fee: int, suggested: int) -> int:
            fee = take_priority_fee(
                self.config.fees,
                profit=quote.dy - quote.payment,
                want_unit=unit,
                native_price=price,
                gas=gas,
                base_fee=base_fee,
                suggested=suggested,
            )
            log.info(
                "%s: tip %.4f gwei (profit %s want wei, gas %s, base fee %.4f gwei)",
                book.auction.address,
                fee / GWEI,
                quote.dy - quote.payment,
                gas,
                base_fee / GWEI,
            )
            return fee

        return tip

    def _want_unit(self, want: str) -> int:
        unit = self._want_units.get(want)
        if unit is None:
            unit = self._want_units[want] = 10 ** Erc20(self.client, want).decimals()
        return unit

    # Loop

    def wait_seconds(self, elapsed: float) -> float:
        """Sleep until the next recorded expiry, the next price step of a
        worked lot or the poll interval, whichever is sooner (never below one
        second once something is due)."""
        remaining = self.config.poll_interval - elapsed
        if self._cycle_timestamp is not None:
            for due in (self._next_expiry, self._next_step):
                if due is not None:
                    remaining = min(remaining, max(1.0, due - self._cycle_timestamp - elapsed))
        return max(0.0, remaining)

    def run(
        self,
        stop: threading.Event | None = None,
        *,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        """Cycle until ``stop`` is set, sleeping ``wait_seconds`` in between.

        ``wait(seconds)`` blocks between cycles and returns True to stop; the
        default is ``stop.wait``.
        """
        stop = stop or threading.Event()
        wait = wait or stop.wait
        while not stop.is_set():
            started = time.monotonic()
            outcomes = self.cycle()
            self.report(outcomes, time.monotonic() - started)
            if wait(self.wait_seconds(time.monotonic() - started)):
                break
        self.save(force=True)

    def requests_note(self) -> str:
        """The requests made since the last call: the node's, and per HTTP
        host the ones sent and the ones its budget held back."""
        parts = []
        meter = getattr(self.client, "meter", None)
        if meter is not None:
            total = meter.total()
            parts.append(f"rpc {total - self._rpc_seen}")
            self._rpc_seen = total
        for host, counts in sorted(GATES.counts().items()):
            sent, held = (
                now - before
                for now, before in zip(counts, self._http_seen.get(host, (0, 0)), strict=True)
            )
            self._http_seen[host] = counts
            if sent or held:
                parts.append(f"{host} {sent}" + (f" (+{held} held back)" if held else ""))
        return ", ".join(parts)

    def report(self, outcomes: list[Outcome], elapsed: float) -> None:
        """One summary line per cycle in which something happened or changed;
        the cycles in between are told at debug level, with a line every
        ``heartbeat`` seconds to show the loop is alive."""
        summary = {
            action: count
            for action in ACTIONS
            if (count := sum(1 for o in outcomes if o.action == action))
        }
        before, at = self._reported
        now = time.monotonic()
        loud = not QUIET.issuperset(summary) or summary != before or now - at >= self.heartbeat
        if not loud and not log.isEnabledFor(logging.DEBUG):
            return
        requests = self.requests_note()
        log.log(
            logging.INFO if loud else logging.DEBUG,
            "%s: %s in %.1fs%s",
            self.config.name,
            summary,
            elapsed,
            f"; requests: {requests}" if requests else "",
        )
        if loud:
            self._reported = (summary, now)


ZONE_RANK = {zone: index for index, zone in enumerate(schedule.ZONES)}


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
