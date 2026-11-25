"""The ranking pass and the selection of the lots a cycle works on."""

import time

from keepers.auction_keeper.config import FocusSettings, PricingSettings
from keepers.auction_keeper.core import focus
from keepers.auction_keeper.core.model import LotRecord
from keepers.auction_keeper.pricing import MarketQuote
from tests.keeper.pure.conftest import LENGTH, WAD, WINDOW_START, token_address

NOW = WINDOW_START + 20 * 3600


def lots(count, amount=1000 * WAD):
    return [
        LotRecord(token_address(i), amount, WINDOW_START, WINDOW_START + LENGTH, amount)
        for i in range(count)
    ]


def market_by(values):
    """An estimate function: want for the whole lot by token (missing: none)."""

    def estimate(record):
        amount = values.get(record.token)
        return MarketQuote(amount, "test", 0.0, amount=record.available) if amount else None

    return estimate


def test_rank_orders_by_distance_to_the_highest_estimate(curve):
    records = lots(5)
    ask = records[0].ask(curve, NOW)
    values = {
        records[0].token: ask // 10,  # ask is 10x the market: far
        records[1].token: ask,  # at the market
        records[2].token: ask * 2,  # under the market already
        # records[3]: no estimate
        records[4].token: int(ask / 1.1),  # 10% above
    }
    ranked = focus.rank(records, curve, NOW, market_by(values))
    assert [item.token for item in ranked] == [records[i].token for i in (2, 1, 4, 0, 3)]
    assert ranked[0].ratio == 0.5 and ranked[1].ratio == 1.0 and ranked[-1].ratio is None
    assert all(item.ask == ask for item in ranked)  # one curve for the whole window


def test_rank_leaves_out_sold_ended_and_dust_lots(curve):
    live, sold, ended, upcoming, dust, crumbs = lots(6)
    sold = sold.taken(0)
    ended = LotRecord(ended.token, ended.initial_amount, NOW - LENGTH, NOW, ended.available)
    upcoming = LotRecord(upcoming.token, upcoming.initial_amount, NOW + 1, NOW + LENGTH, WAD)
    # What a fill left behind: the estimate scales down with the remainder.
    crumbs = crumbs.taken(crumbs.initial_amount // 10**6)
    whole = MarketQuote(500 * WAD, "test", 0.0, amount=live.initial_amount)

    def estimate(record):
        if record.token == dust.token:
            return MarketQuote(WAD // 1000, "test", 0.0, amount=record.available)
        return whole.scaled(record.available)

    ranked = focus.rank(
        [live, sold, ended, upcoming, dust, crumbs], curve, NOW, estimate, dust=0.01 * WAD
    )
    assert [item.token for item in ranked] == [live.token]
    # Without the dust floor both small lots are ranked again.
    assert len(focus.rank([live, dust, crumbs], curve, NOW, estimate)) == 3


def test_select_takes_everything_within_the_threshold_plus_the_nearest(curve):
    records = lots(30)
    ask = records[0].ask(curve, NOW)
    # Ten lots at one price right at the market, the rest ever farther away.
    values = {r.token: ask if i < 10 else ask // (i - 7) for i, r in enumerate(records)}
    ranked = focus.rank(records, curve, NOW, market_by(values))
    settings = FocusSettings(threshold=0.25, nearest=3, blind=0, max_lots=40)
    chosen = focus.select(ranked, settings)
    # All ten at the market (there may be many at one price), and the three nearest after them.
    assert len(chosen) == 13
    assert {item.token for item in chosen[:10]} == {r.token for r in records[:10]}
    assert [item.token for item in chosen[10:]] == [records[i].token for i in (10, 11, 12)]
    # The cap wins, nearest first; nearest: 0 is the pure threshold rule.
    assert len(focus.select(ranked, FocusSettings(nearest=3, blind=0, max_lots=4))) == 4
    assert len(focus.select(ranked, FocusSettings(nearest=0, blind=0))) == 10
    # threshold 0 and a large `nearest`: every lot, the behaviour before the model.
    everything = FocusSettings(threshold=0, nearest=10**6, blind=10**6, max_lots=10**6)
    assert len(focus.select(ranked, everything)) == 30


def test_select_rotates_through_the_unpriced_lots(curve):
    records = lots(7)
    ask = records[0].ask(curve, NOW)
    ranked = focus.rank(records, curve, NOW, market_by({records[0].token: ask}))
    settings = FocusSettings(nearest=0, blind=2, max_lots=3)
    seen = set()
    for rotation in range(3):
        chosen = focus.select(ranked, settings, rotation=rotation)
        assert chosen[0].token == records[0].token and len(chosen) == 3
        seen |= {item.token for item in chosen[1:]}
    assert len(seen) == 6  # all six unpriced lots got their turn in three cycles


def test_estimates_age_slowly_far_from_the_market(curve):
    records = lots(4)
    ask = records[0].ask(curve, NOW)
    values = {
        records[0].token: ask,
        records[1].token: ask // 2,
        records[2].token: ask // 1000,
    }
    ranked = {i.token: i for i in focus.rank(records, curve, NOW, market_by(values))}
    settings, pricing = FocusSettings(), PricingSettings(cache=60, cache_far=3600)
    near, mid, far, blind = (ranked[r.token] for r in records)
    assert focus.estimate_ttl(near, curve, settings, pricing) == 60
    # Halving takes ~2.1 h on this curve; the estimate is asked again well before.
    assert 600 < focus.estimate_ttl(mid, curve, settings, pricing) < 3600
    assert focus.estimate_ttl(far, curve, settings, pricing) == 3600
    assert focus.estimate_ttl(blind, curve, settings, pricing) == 3600

    ages = {near.token: 61.0, mid.token: 61.0, far.token: 4000.0, blind.token: None}
    queue = focus.stale(list(ranked.values()), curve, lambda r: ages[r.token], settings, pricing)
    # Near the market first, then the never-asked, then the far one; `mid` is not due.
    assert [item.token for item in queue] == [near.token, blind.token, far.token]


def test_ranking_500_lots_takes_milliseconds(curve):
    records = lots(500)
    ask = records[0].ask(curve, NOW)
    values = {r.token: ask * (i + 1) // 250 for i, r in enumerate(records) if i % 10}
    estimate = market_by(values)
    started = time.perf_counter()
    rounds = 20
    for _ in range(rounds):
        ranked = focus.rank(records, curve, NOW, estimate, dust=0.01 * WAD)
        chosen = focus.select(ranked, FocusSettings())
    per_pass = (time.perf_counter() - started) / rounds
    print(f"\nrank + select over 500 lots: {per_pass * 1000:.2f} ms")
    assert len(ranked) == 500 and chosen
    assert per_pass < 0.05
