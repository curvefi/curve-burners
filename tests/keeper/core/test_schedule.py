"""Order validity schedule against the harness curve."""

import pytest

from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.chain.contracts import Auction
from keepers.auction_keeper.config import TtlSettings
from keepers.auction_keeper.core.schedule import plan as schedule_plan
from keepers.auction_keeper.pricing import MarketQuote
from tests.keeper.conftest import STEP_DURATION, band_entry

ORDER_TTL = 300
SETTINGS = TtlSettings(near_above=0.2, near_below=0.1, min_ttl=60, max_ttl=10**6)


@pytest.fixture
def auction(keeper_account, harness):
    return Auction(BoaClient(sender=keeper_account), harness.address)


def market(amount):
    return MarketQuote(amount, "test", 0.0)


def plan(auction, view, quote, settings, order_ttl):
    """The schedule with the band entry answered by the local model."""
    return schedule_plan(
        view, quote, settings, order_ttl, reaches=lambda target: band_entry(auction, view, target)
    )


def test_far_zone_orders_until_the_curve_enters_the_band(auction, sell_token, lot):
    view = auction.lot_view(sell_token.address)
    quote = market(view.amount_needed // 50)
    planned = plan(auction, view, quote, SETTINGS, ORDER_TTL)
    assert planned.zone == "far" and planned.market == quote.want_amount
    assert planned.valid_to == planned.band_entry

    upper = int(quote.want_amount * 1.2)
    entry = planned.band_entry

    def needed(ts):
        return auction.get_amount_needed(sell_token.address, view.available, timestamp=ts)

    assert needed(entry) <= upper
    # The entry is the first second of its step: the second before is still above.
    assert needed(entry - 1) > upper and (entry - view.start) % STEP_DURATION == 0
    assert view.timestamp + ORDER_TTL < entry < view.end


def test_far_zone_validity_is_clamped(auction, sell_token, lot):
    view = auction.lot_view(sell_token.address)
    quote = market(view.amount_needed // 50)
    capped = plan(auction, view, quote, TtlSettings(min_ttl=60, max_ttl=600), ORDER_TTL)
    assert capped.zone == "far" and capped.valid_to == view.timestamp + 600
    # Just above the band: the entry is minutes away, the minimum still applies.
    close = plan(
        auction,
        view,
        market(int(view.amount_needed / 1.25)),
        TtlSettings(min_ttl=3600, max_ttl=7200),
        ORDER_TTL,
    )
    assert close.zone == "far" and close.valid_to == view.timestamp + 3600


def test_near_zone_uses_the_minimum_validity(auction, sell_token, lot):
    view = auction.lot_view(sell_token.address)
    for amount in (
        view.amount_needed,
        int(view.amount_needed / 1.15),
        int(view.amount_needed / 0.95),
    ):
        planned = plan(auction, view, market(amount), SETTINGS, ORDER_TTL)
        assert planned.zone == "near", amount
        assert planned.valid_to == view.timestamp + SETTINGS.min_ttl


def test_below_market_and_blind_use_the_uniform_ttl(auction, sell_token, lot):
    view = auction.lot_view(sell_token.address)
    below = plan(auction, view, market(view.amount_needed * 2), SETTINGS, ORDER_TTL)
    assert below.zone == "below" and below.valid_to == view.timestamp + ORDER_TTL
    blind = plan(auction, view, None, SETTINGS, ORDER_TTL)
    assert blind.zone == "blind" and blind.market is None
    assert blind.valid_to == view.timestamp + ORDER_TTL


def test_band_entry_is_the_window_end_when_never_reached(auction, sell_token, lot):
    view = auction.lot_view(sell_token.address)
    assert band_entry(auction, view, 0) == view.end
    planned = plan(auction, view, market(1), SETTINGS, ORDER_TTL)
    assert planned.zone == "far" and planned.band_entry == view.end
    assert planned.valid_to == view.end


def test_disagreeing_sources_each_keep_a_band_of_their_own(auction, sell_token, lot):
    view = auction.lot_view(sell_token.address)
    needed = view.amount_needed

    def levels(*amounts):
        return MarketQuote(
            amounts[0], "test", 0.0, levels=tuple((f"s{i}", a) for i, a in enumerate(amounts))
        )

    # The curve is at one of the answers, whichever: dense.
    for span in (levels(needed, needed // 100), levels(needed * 100, needed)):
        planned = plan(auction, view, span, SETTINGS, ORDER_TTL)
        assert planned.zone == "near", span
        assert planned.valid_to == view.timestamp + SETTINGS.min_ttl
    # Between two answers far apart: one order until the band of the lower one.
    between = plan(auction, view, levels(needed * 2, needed // 2), SETTINGS, ORDER_TTL)
    lower = plan(auction, view, market(needed // 2), SETTINGS, ORDER_TTL)
    assert between.zone == "far" and between.band_entry == lower.band_entry
    # Far above both: the wait ends at the band of the highest answer.
    far = plan(auction, view, levels(needed // 2, 1), SETTINGS, ORDER_TTL)
    alone = plan(auction, view, market(needed // 2), SETTINGS, ORDER_TTL)
    assert far.zone == "far" and far.band_entry == alone.band_entry
