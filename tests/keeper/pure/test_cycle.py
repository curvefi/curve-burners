"""The keeper's cycle over the local model, against an in-memory chain."""

import time
from types import SimpleNamespace

import pytest

from keepers.auction_keeper.config import (
    FocusSettings,
    LookaheadSettings,
    NativeSettings,
    NetworkConfig,
    PricingSettings,
    TokenFilter,
    TtlSettings,
)
from keepers.auction_keeper.core import schedule
from keepers.auction_keeper.core.keeper import NetworkKeeper
from keepers.auction_keeper.core.model import LotRecord
from keepers.auction_keeper.core.state import KeeperState, OrderRecord
from keepers.auction_keeper.limits import Throttled
from keepers.auction_keeper.pricing import MarketPricer
from keepers.auction_keeper.rails import PublishResult
from keepers.auction_keeper.rails.native import NativeTaker
from tests.keeper.pure.conftest import (
    AUCTION,
    LENGTH,
    STEP,
    WAD,
    WINDOW_START,
    FakeChain,
    token_address,
)

TAKER = token_address(10**6)
KEEPER = token_address(10**6 + 1)


class Markets:
    """A price source: want per whole token by address (missing: no price)."""

    name = "markets"

    def __init__(self, rates=None):
        self.rates = rates or {}
        self.asked = []

    def quote(self, lot):
        self.asked.append(lot.token)
        rate = self.rates.get(lot.token)
        return int(lot.available * rate) if rate else None


class Router:
    """A router that pays ``rate`` want per token, with no price impact."""

    def __init__(self, rate=1.0, rates=None):
        self.rate, self.rates = rate, rates
        self.quoted = []

    def best_route(self, token, want, amount):
        self.quoted.append(token)
        rate = self.rate if self.rates is None else self.rates.get(token)
        return (rate, int(amount * rate)) if rate else None

    def chunk(self, route, available, max_impact):
        return route, available, int(available * route), 0.0

    def swap_calls(self, route, amount, min_dy, receiver):
        return []


def config(**overrides):
    fields = {
        "name": "fake",
        "rpc": "fake://",
        "auctions": (AUCTION,),
        "start_block": 0,
        "poll_interval": 60,
        "pricing": PricingSettings(sources=()),
        "focus": FocusSettings(audit=0),
        "native": NativeSettings(enabled=False),
    }
    fields.update(overrides)
    return NetworkConfig(**fields)


def keeper_of(chain, markets=None, router=None, state=None, **overrides):
    if router is not None:
        overrides.setdefault("auction_taker", TAKER)
        overrides.setdefault("native", NativeSettings(enabled=True))
        chain.sender = KEEPER
    cfg = config(**overrides)
    native = NativeTaker(cfg, chain, router, taker=TAKER) if router is not None else None
    return NetworkKeeper(
        cfg,
        state or KeeperState(None),
        client=chain,
        pricer=MarketPricer([markets or Markets()], clock=lambda: float(chain.now)),
        native=native,
        publishers={},
    )


def stage_many(chain, count, amount=1000 * WAD):
    tokens = [token_address(i) for i in range(count)]
    for token in tokens:
        chain.stage(token, amount)
    return tokens


def ask_per_token(curve, timestamp, amount=1000 * WAD):
    record = LotRecord("x", amount, WINDOW_START, WINDOW_START + LENGTH, amount)
    return record.ask(curve, timestamp) / amount


def test_500_lots_cost_a_handful_of_requests_and_a_fraction_of_a_second(chain, curve):
    chain.now = WINDOW_START + 20 * 3600
    tokens = stage_many(chain, 500)
    near = ask_per_token(curve, chain.now)
    # Three lots at the market, the others 2x to 500x above it, every tenth unpriced.
    rates = {t: near / (i - 1) for i, t in enumerate(tokens) if i >= 3 and i % 10}
    rates.update({t: near for t in tokens[:3]})
    # The routes pay a little less than the market says; the unpriced lots have none.
    router = Router(rates={token: rate * 0.9 for token, rate in rates.items()})
    keeper = keeper_of(
        chain, Markets(rates), router, pricing=PricingSettings(sources=(), budget=60)
    )

    started = time.perf_counter()
    outcomes = keeper.cycle()
    first = time.perf_counter() - started
    # Worked: the three at the market, the five nearest after them, two unpriced.
    assert len(outcomes) == 10
    assert sorted(o.action for o in outcomes) == ["unroutable"] * 2 + ["waiting"] * 8
    assert {o.token for o in outcomes[:3]} == set(tokens[:3])
    # One log scan and one block read for all 500; calls only for the settings
    # (6), the want's decimals, the adapter registry (2) and one `available`
    # per worked lot.
    assert chain.requests["eth_getLogs"] == 1 and chain.requests["eth_getBlockByNumber"] <= 2
    assert chain.requests["eth_call"] == 6 + 1 + 2 + 10

    chain.requests.clear()
    chain.mine()
    started = time.perf_counter()
    assert len(keeper.cycle()) >= 3
    second = time.perf_counter() - started
    # Nothing changed: the confirmed lots are reused, only the unpriced pair rotates.
    assert chain.requests["eth_getLogs"] == 1
    assert chain.requests["eth_call"] <= 2
    print(f"\n500-lot cycle: first {first * 1000:.0f} ms, next {second * 1000:.0f} ms")
    assert first < 5 and second < 1


def test_far_lots_cost_nothing_until_the_curve_brings_them_near(chain, curve):
    chain.now = WINDOW_START + 3600
    (token,) = stage_many(chain, 1)
    rate = ask_per_token(curve, WINDOW_START + 12 * 3600)  # the market: the ask at half time
    router = Router(rate=rate)
    keeper = keeper_of(
        chain, Markets({token: rate}), router, focus=FocusSettings(nearest=0, audit=0)
    )
    assert keeper.cycle() == [] and router.quoted == []
    # Settings, decimals and the adapter registry (no adapter: no allowance to sync);
    # nothing for the lot.
    assert chain.requests["eth_call"] == 6 + 1 + 2
    chain.requests.clear()
    chain.now = WINDOW_START + 11 * 3600 + 1800  # ~13% above the market now
    (outcome,) = keeper.cycle()
    assert outcome.action == "waiting" and router.quoted == [token]


def test_sold_lots_are_dropped_by_their_taken_log(chain, curve):
    chain.now = WINDOW_START + 20 * 3600
    tokens = stage_many(chain, 3)
    near = ask_per_token(curve, chain.now)
    router = Router(rate=near * 0.5)
    keeper = keeper_of(chain, Markets(dict.fromkeys(tokens, near)), router)
    assert len(keeper.cycle()) == 3
    chain.take(tokens[0], 0)  # a third party took the whole lot
    chain.take(tokens[1], 400 * WAD)  # and a part of another one
    chain.requests.clear()
    outcomes = keeper.cycle()
    assert {o.token for o in outcomes} == set(tokens[1:])
    assert chain.requests["eth_call"] == 0  # the logs said it all, and no quote was due
    assert keeper.books[AUCTION].lots[tokens[1]].available == 400 * WAD
    # What the estimate says about the remainder scales with it.
    assert keeper.estimate(keeper.books[AUCTION], keeper.books[AUCTION].lots[tokens[1]]).amount == (
        400 * WAD
    )


def test_a_take_no_log_reported_is_found_by_the_confirm_read(chain, curve):
    chain.now = WINDOW_START + 20 * 3600
    (token,) = stage_many(chain, 1)
    near = ask_per_token(curve, chain.now)
    keeper = keeper_of(
        chain, Markets({token: near}), Router(near * 0.5), focus=FocusSettings(confirm=30, audit=0)
    )
    assert len(keeper.cycle()) == 1
    chain.take(token, 0, log=False)
    assert len(keeper.cycle()) == 1  # within focus.confirm the record still stands
    chain.mine(30)
    (outcome,) = keeper.cycle()
    assert (outcome.action, outcome.detail) == ("skipped", "inactive lot")
    assert keeper.cycle() == []


def test_whitelisted_token_without_a_log_is_read_from_the_chain_and_audited(chain, curve):
    chain.now = WINDOW_START + 20 * 3600
    token = token_address(1)
    chain.stage(token, 1000 * WAD, log=False)
    near = ask_per_token(curve, chain.now)
    keeper = keeper_of(
        chain,
        Markets({token: near}),
        Router(near * 0.5),
        start_block=None,
        tokens=TokenFilter(whitelist=frozenset([token])),
        focus=FocusSettings(audit=1, confirm=10**6),
    )
    assert len(keeper.cycle()) == 1
    chain.take(token, 250 * WAD, log=False)
    keeper.cycle()  # the audit's turn: one lot a cycle is read back
    assert keeper.books[AUCTION].lots[token].available == 250 * WAD


def test_the_take_is_priced_for_the_next_block_across_a_step_boundary(chain, curve):
    start = WINDOW_START
    edge = start + 2000 * STEP  # a step boundary
    chain.now = edge - 5  # the latest block is the last one of the old step
    (token,) = stage_many(chain, 1)
    old, new = ask_per_token(curve, chain.now), ask_per_token(curve, edge)
    assert new < old
    # The route pays exactly what makes the take worth it from the new step on.
    rate = new / (1 - 0.005) * 1.000001
    assert old > rate * (1 - 0.005) >= new

    late = keeper_of(
        chain, Markets({token: rate}), Router(rate), lookahead=LookaheadSettings(blocks=0)
    )
    (outcome,) = late.cycle()
    assert outcome.action == "waiting" and chain.simulated == []

    ahead = keeper_of(chain, Markets({token: rate}), Router(rate))  # default: one block ahead
    assert ahead.ahead == 12
    (outcome,) = ahead.cycle()
    assert outcome.action == "simulated"
    record = ahead.books[AUCTION].lots[token]
    assert f"payment={record.ask(curve, edge + 7)}" in outcome.detail
    # The simulation ran with the next block's clock, not the latest one's.
    assert chain.simulated == [chain.now + 12]

    # Inside a step the next block costs the same: looking ahead changes nothing.
    chain.simulated.clear()
    chain.now = edge - 30
    for lookahead in (LookaheadSettings(blocks=0), LookaheadSettings()):
        keeper = keeper_of(chain, Markets({token: rate}), Router(rate), lookahead=lookahead)
        assert keeper.cycle()[0].action == "waiting"
    assert chain.simulated == []


def test_the_loop_wakes_when_the_next_block_crosses_into_a_new_step(chain, curve):
    start = WINDOW_START
    chain.now = start + 2000 * STEP + 3
    (token,) = stage_many(chain, 1)
    rate = ask_per_token(curve, chain.now)
    keeper = keeper_of(chain, Markets({token: rate}), Router(rate * 0.9))
    keeper.cycle()
    # Priced for now + 12; the next step starts 33 s from now, so the block
    # whose successor is in it comes 21 s from now.
    assert keeper.wait_seconds(0) == 21
    assert keeper.wait_seconds(20.5) == 1
    far = keeper_of(chain, Markets({token: rate / 100}), Router(rate / 100))
    far.cycle()
    assert far.wait_seconds(0) == 60  # nothing near: the poll interval


def test_lookahead_default_follows_the_chain_block_time(chain):
    assert keeper_of(chain).ahead == 12
    chain.chain_id = 42161
    assert keeper_of(chain).ahead == 1
    assert keeper_of(chain, lookahead=LookaheadSettings(blocks=3)).ahead == 3
    assert keeper_of(chain, lookahead=LookaheadSettings(seconds=7)).ahead == 7
    chain.chain_id = 999_999  # no block time known: the latest block is priced
    assert keeper_of(chain).ahead == 0


def test_orders_are_dense_at_each_source_price_and_sparse_between(chain, curve):
    chain.now = WINDOW_START + 6 * 3600
    (token,) = stage_many(chain, 1)
    ask = ask_per_token(curve, chain.now)

    class Other(Markets):
        name = "other"

    # Two sources 50x apart: the curve is at the higher one now.
    high, low = Markets({token: ask}), Other({token: ask / 50})
    keeper = NetworkKeeper(
        config(pricing=PricingSettings(sources=(), cache=10**6, cache_far=10**6)),
        KeeperState(None),
        client=chain,
        pricer=MarketPricer([high, low], clock=lambda: float(chain.now)),
        publishers={},
    )
    key = (AUCTION.lower(), token.lower())
    keeper.cycle()
    assert keeper._plans[key].zone == "near"
    # Well under the higher price and far above the lower one: one long order.
    chain.now = next(
        ts
        for ts in range(chain.now, WINDOW_START + LENGTH, STEP)
        if ask_per_token(curve, ts) < ask * 0.5
    )
    chain.mine()
    keeper.cycle()
    planned = keeper._plans[key]
    assert planned.zone == "far" and planned.valid_to > chain.now + 60
    # At the lower price: dense again.
    chain.now = planned.band_entry
    chain.mine()
    keeper.cycle()
    assert keeper._plans[key].zone == "near"


def test_estimates_are_refreshed_within_the_budget_nearest_first(chain, curve):
    chain.now = WINDOW_START + 20 * 3600
    tokens = stage_many(chain, 50)
    near = ask_per_token(curve, chain.now)
    markets = Markets({t: near / (i + 1) for i, t in enumerate(tokens)})
    keeper = keeper_of(chain, markets, pricing=PricingSettings(sources=(), budget=60, cache=60))
    keeper.cycle()
    assert sorted(markets.asked) == sorted(tokens)  # the first pass prices every lot once
    markets.asked.clear()
    chain.mine(61)
    keeper.cycle()
    # A minute on only the lot at the market is due; the far ones keep their estimate.
    assert markets.asked == [tokens[0]]
    # No budget, no lookups at all.
    idle = keeper_of(chain, Markets(), pricing=PricingSettings(sources=(), budget=0))
    idle.cycle()
    assert idle.pricer.sources[0].asked == []


def test_lots_are_priced_before_their_window_opens(chain, curve):
    chain.now = WINDOW_START - 3600  # staged in COLLECT, an hour before EXCHANGE
    tokens = stage_many(chain, 4)
    markets = Markets(dict.fromkeys(tokens, 1.0))
    keeper = keeper_of(chain, markets)
    assert keeper.cycle() == [] and sorted(markets.asked) == sorted(tokens)


def test_the_book_and_the_prices_survive_a_restart(chain, curve, tmp_path):
    chain.now = WINDOW_START + 20 * 3600
    tokens = stage_many(chain, 5)
    near = ask_per_token(curve, chain.now)
    path = tmp_path / "state.json"
    keeper = keeper_of(chain, Markets(dict.fromkeys(tokens, near)), state=KeeperState(path))
    keeper.cycle()
    chain.requests.clear()
    chain.logs.clear()  # a restart must not need the old logs again
    markets = Markets()
    restarted = keeper_of(chain, markets, state=KeeperState(path))
    seen = restarted.survey(restarted.auctions[0])
    assert len(seen.ranked) == 5 and all(item.ratio is not None for item in seen.ranked)
    assert markets.asked == [] and chain.requests["eth_getLogs"] == 0


def test_settings_change_refences_the_book(chain, curve):
    from keepers.auction_keeper.chain.client import Log
    from keepers.auction_keeper.chain.events import ECONOMICS_SET_TOPIC

    chain.now = WINDOW_START + 3600
    stage_many(chain, 3)
    keeper = keeper_of(chain)
    keeper.cycle()
    assert len(keeper.books[AUCTION].lots) == 3
    chain.mine()
    chain.logs.append(Log(AUCTION, (ECONOMICS_SET_TOPIC, b"\x00" * 32), b"", chain.number))
    chain.requests.clear()
    keeper.cycle()
    # Lots staged before the new economics can never fill; the curve is read again.
    assert keeper.books[AUCTION].lots == {} and chain.requests["eth_call"] == 6


class Publisher:
    version = "Fake"
    address = token_address(77)
    spender = token_address(78)

    def __init__(self):
        self.published = []

    def publish(self, auction, lot, *, dry_run=False):
        self.published.append(lot.amount_needed)
        return PublishResult("uid", lot.timestamp + 60, lot.available, lot.amount_needed, {})


@pytest.mark.parametrize("every_step, reposts", [(False, 0), (True, 1)])
def test_near_orders_follow_the_curve_step_by_step_when_asked_to(chain, every_step, reposts):
    keeper = keeper_of(chain, ttl=TtlSettings(every_step=every_step))
    publisher = Publisher()
    lot = SimpleNamespace(
        auction=AUCTION,
        token=token_address(1),
        start=5,
        timestamp=100,
        available=10,
        amount_needed=90,
    )
    # An order posted a step ago at a higher price, still valid.
    keeper.state.set_order(
        "fake", AUCTION, publisher.address, lot.token, OrderRecord("old", 150, 10, 95, 80, 5)
    )
    near = schedule.Plan(160, "near", 90)
    outcome = keeper._publish(SimpleNamespace(address=AUCTION), publisher, lot, near, budget=1)
    assert outcome.action == ("posted" if reposts else "kept")
    assert len(publisher.published) == reposts
    # Same price as the standing order, or a far lot: nothing to replace.
    keeper.state.set_order(
        "fake", AUCTION, publisher.address, lot.token, OrderRecord("old", 150, 10, 90, 80, 5)
    )
    assert (
        keeper._publish(SimpleNamespace(address=AUCTION), publisher, lot, near, budget=1).action
        == "kept"
    )


def test_orders_are_priced_for_the_latest_block_unless_told_otherwise(chain, curve):
    edge = WINDOW_START + 2000 * STEP
    chain.now = edge - 5
    (token,) = stage_many(chain, 1)
    rate = ask_per_token(curve, chain.now)
    for orders, at in ((False, chain.now), (True, chain.now + 12)):
        keeper = keeper_of(
            chain, Markets({token: rate}), lookahead=LookaheadSettings(orders=orders)
        )
        publisher = Publisher()
        keeper.publishers_for = lambda auction, p=publisher: [p]
        (outcome,) = keeper.cycle()
        assert outcome.action == "posted"
        assert publisher.published == [keeper.books[AUCTION].lots[token].ask(curve, at)]


def test_fake_chain_is_all_the_pure_tests_touch():
    assert FakeChain().latest_block().timestamp == WINDOW_START + 600


def order(valid_to, start=WINDOW_START, zone=""):
    return OrderRecord("uid", valid_to, 10, 10, 0, start, zone)


def publishing(chain, curve, count, **overrides):
    """A keeper with one fake adapter and ``count`` lots at their market."""
    tokens = stage_many(chain, count)
    rate = ask_per_token(curve, chain.now)
    keeper = keeper_of(chain, Markets(dict.fromkeys(tokens, rate)), **overrides)
    publisher = Publisher()
    keeper.publishers_for = lambda auction: [publisher]
    return keeper, publisher, tokens


def test_orders_of_sold_and_restaged_lots_do_not_hold_a_slot(chain, curve):
    chain.now = WINDOW_START + 6 * 3600
    keeper, publisher, tokens = publishing(chain, curve, 4, max_open_orders=2)
    late = chain.now + 7000
    # Two long orders stand, for lots that are gone: one sold, one of an earlier window.
    keeper.state.set_order("fake", AUCTION, publisher.address, tokens[0], order(late))
    keeper.state.set_order(
        "fake", AUCTION, publisher.address, tokens[1], order(late, WINDOW_START - LENGTH)
    )
    chain.take(tokens[0], 0)
    posted = [o.token for o in keeper.cycle() if o.action == "posted"]
    # Both slots were free: the dead records were dropped, not counted.
    assert len(posted) == 2 and tokens[0] not in posted
    records = keeper.state.adapter_orders("fake", AUCTION, publisher.address)
    assert tokens[0].lower() not in records
    assert all(record.lot_start == WINDOW_START for record in records.values())


def test_far_orders_leave_half_the_slots_to_lots_at_a_price(chain, curve):
    chain.now = WINDOW_START + 6 * 3600
    tokens = stage_many(chain, 8)
    ask = ask_per_token(curve, chain.now)
    # Six lots 15% above their market (far, but in focus), two at it.
    rates = {t: ask / 1.15 for t in tokens[:6]} | {t: ask for t in tokens[6:]}
    keeper = keeper_of(chain, Markets(rates), max_open_orders=4)
    publisher = Publisher()
    keeper.publishers_for = lambda auction: [publisher]
    outcomes = keeper.cycle()
    posted = {o.token for o in outcomes if o.action == "posted"}
    assert set(tokens[6:]) <= posted and len(posted) == 4
    waiting = [o.detail for o in outcomes if o.action == "skipped"]
    assert len(waiting) == 4
    records = keeper.state.adapter_orders("fake", AUCTION, publisher.address)
    assert sorted(r.zone for r in records.values()) == ["far", "far", "near", "near"]
    # The next cycle: the far orders stand, so a new far one has no slot.
    chain.mine()
    again = [o for o in keeper.cycle() if o.action == "posted"]
    assert again == []


def test_a_failed_post_is_tried_again_after_a_pause_that_doubles(chain, curve, caplog):
    chain.now = WINDOW_START + 6 * 3600
    keeper, publisher, (token,) = publishing(chain, curve, 1)
    tries = []

    def refuse(auction, lot, *, dry_run=False):
        tries.append(lot.timestamp)
        raise RuntimeError("orderbook 400 InsufficientAllowance")

    publisher.publish = refuse
    with caplog.at_level("WARNING"):
        for _ in range(30):  # six minutes of blocks
            keeper.cycle()
            chain.mine()
    # At once, then after 60 s, then after 120 s more: three tries, not thirty.
    assert [at - tries[0] for at in tries] == [0, 60, 180]
    assert caplog.text.count("publish failed") == 3 and "Traceback" not in caplog.text

    # A request the host's budget held back is not a failure: no pause for it.
    def throttled(auction, lot, *, dry_run=False):
        tries.append(lot.timestamp)
        raise Throttled("api.cow.fi", 3, "request budget spent")

    publisher.publish = throttled
    keeper._post_retry.clear()
    before = len(tries)
    for _ in range(3):
        (outcome,) = keeper.cycle()
        assert outcome.action == "skipped" and "budget spent" in outcome.detail
        chain.mine()
    assert len(tries) == before + 3


def test_takes_of_different_lots_go_out_at_once_with_several_accounts(chain, curve):
    import threading

    chain.now = WINDOW_START + 20 * 3600
    tokens = stage_many(chain, 3)
    rate = ask_per_token(curve, chain.now)
    chain.senders = [KEEPER, token_address(10**6 + 2), token_address(10**6 + 3)]
    flying, peak, lock = [0], [0], threading.Lock()
    release = threading.Barrier(3, timeout=5)

    def send(to, data, **kwargs):
        with lock:
            flying[0] += 1
            peak[0] = max(peak[0], flying[0])
        release.wait()  # every take is in flight before any gets its receipt
        with lock:
            flying[0] -= 1
        return SimpleNamespace(tx_hash="0x01", ok=True)

    chain.send = send
    keeper = keeper_of(
        chain,
        Markets(dict.fromkeys(tokens, rate)),
        Router(rate=rate * 2),
        mode="keeper",
        keeper_key_env="KEY",
    )
    assert keeper.lanes == 3
    outcomes = [o for o in keeper.cycle() if o.adapter == "native"]
    assert [o.action for o in outcomes] == ["taken"] * 3 and peak[0] == 3


def test_a_quiet_cycle_is_not_a_log_line(chain, curve, caplog):
    chain.now = WINDOW_START + 6 * 3600
    keeper, publisher, tokens = publishing(chain, curve, 2)
    with caplog.at_level("INFO"):
        keeper.report(keeper.cycle(), 0.1)  # two orders posted: told
        for _ in range(4):  # the orders stand: nothing to tell
            chain.mine()
            keeper.report(keeper.cycle(), 0.1)
    lines = [r.getMessage() for r in caplog.records if r.name.endswith("core.keeper")]
    assert len(lines) == 2 and "'posted': 2" in lines[0] and "'kept': 2" in lines[1]
    keeper.heartbeat = 0.0  # ... except the line that says the loop is alive
    with caplog.at_level("INFO"):
        keeper.report(keeper.cycle(), 0.1)
    assert len([r for r in caplog.records if r.name.endswith("core.keeper")]) == 3
