"""How disagreeing price sources are combined, and what it costs."""

from types import SimpleNamespace

from keepers.auction_keeper.config import TtlSettings
from keepers.auction_keeper.core.schedule import plan, zone_of
from keepers.auction_keeper.pricing import MarketPricer, MarketQuote
from tests.keeper.pure.conftest import AUCTION, token_address

TOKEN = token_address(1)
TTL = TtlSettings(near_above=0.2, near_below=0.1, min_ttl=60, max_ttl=7200)


class Source:
    def __init__(self, name, answer):
        self.name, self.answer, self.calls = name, answer, 0

    def quote(self, lot):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def lot(available=1000, timestamp=1_000_000, needed=0):
    return SimpleNamespace(
        auction=AUCTION,
        token=TOKEN,
        available=available,
        timestamp=timestamp,
        end=timestamp + 86_400,
        amount_needed=needed,
    )


def pricer_of(*answers, clock=None):
    sources = [Source(f"s{i}", answer) for i, answer in enumerate(answers)]
    return MarketPricer(sources, clock=clock or (lambda: 0.0)), sources


def test_every_answer_is_a_level_and_none_outvotes_another():
    # An LP token: one source knows its value, two answer far lower, two have no price.
    pricer, sources = pricer_of(None, 40, 1000, RuntimeError("down"), 35)
    quote = pricer.refresh(lot())
    assert quote.want_amount == 1000 and quote.source == "s2"
    assert quote.levels == (("s2", 1000), ("s1", 40), ("s4", 35))
    assert quote.prices == (1000, 40, 35)
    assert all(source.calls == 1 for source in sources)  # everyone was asked

    alone, _ = pricer_of(None, 1000, None)
    assert alone.refresh(lot()).levels == (("s1", 1000),)
    nobody, _ = pricer_of(None, None)
    assert nobody.refresh(lot()) is None
    # An estimate made without levels (a test, an old state file) is its own level.
    assert MarketQuote(7, "s", 0.0).prices == (7,)


def test_orders_are_dense_around_every_level_and_sparse_between():
    quote = MarketQuote(1000, "a", 0.0, levels=(("a", 1000), ("b", 400), ("c", 380)), amount=1000)
    assert zone_of(1300, quote, TTL) == "far"  # above 1000 * 1.2
    assert zone_of(1200, quote, TTL) == "near" and zone_of(900, quote, TTL) == "near"
    assert zone_of(899, quote, TTL) == "far"  # the gap between the bands of 1000 and 400
    assert zone_of(481, quote, TTL) == "far"
    assert zone_of(480, quote, TTL) == "near"  # 400 * 1.2
    assert zone_of(342, quote, TTL) == "near"  # 380 * 0.9: the bands of 400 and 380 merge
    assert zone_of(341, quote, TTL) == "below"
    assert zone_of(1, None, TTL) == "blind"


def test_the_gap_between_two_levels_costs_one_order():
    quote = MarketQuote(10**9, "liar", 0.0, levels=(("liar", 10**9), ("b", 40)), amount=1000)
    asked = []

    def reaches(target):
        asked.append(target)
        return view.timestamp + 5000

    # Far above everything: one long order until the band of the highest answer.
    view = lot(needed=10**12)
    far = plan(view, quote, TTL, 300, reaches=reaches)
    assert far.zone == "far" and far.band_entry == far.valid_to == view.timestamp + 5000
    assert asked == [12 * 10**8] and far.market == 10**9
    # Inside the band of the highest answer: dense, however lonely that answer is.
    dense = plan(lot(needed=10**9), quote, TTL, 300, reaches=reaches)
    assert dense.zone == "near" and dense.valid_to == view.timestamp + 60
    # Past it: one order until the band of the next answer, at most ttl.max.
    gap = plan(lot(needed=5000), quote, TTL, 300, reaches=reaches)
    assert gap.zone == "far" and gap.market == 40 and asked[-1] == 48
    assert gap.valid_to == view.timestamp + 5000
    capped = plan(lot(needed=5000), quote, TTL, 300, reaches=lambda target: view.timestamp + 10**6)
    assert capped.valid_to == view.timestamp + TTL.max_ttl
    again = plan(lot(needed=45), quote, TTL, 300)
    assert again.zone == "near" and again.market == 40
    under = plan(lot(needed=30), quote, TTL, 300)
    assert under.zone == "below" and under.valid_to == view.timestamp + 300


def test_estimates_are_kept_scaled_and_survive_a_restart():
    clock = [1000.0]
    pricer, sources = pricer_of(None, 40, 1000, clock=lambda: clock[0])
    assert pricer.cached(AUCTION, TOKEN, 1000) is None and pricer.age(AUCTION, TOKEN) is None
    pricer.refresh(lot())
    clock[0] += 3000
    assert pricer.age(AUCTION, TOKEN) == 3000
    # What is left after a partial fill is priced pro rata, with nobody asked.
    half = pricer.cached(AUCTION, TOKEN, 500)
    assert (half.want_amount, half.prices, half.amount) == (500, (500, 20), 500)
    assert sources[2].calls == 1

    restarted, fresh = pricer_of(None, 40, 1000, clock=lambda: clock[0])
    restarted.load(pricer.dump())
    assert restarted.cached(AUCTION, TOKEN, 1000) == pricer.cached(AUCTION, TOKEN, 1000)
    assert restarted.age(AUCTION, TOKEN) == 3000 and fresh[2].calls == 0

    # A token no source prices is remembered as such, and not asked about again.
    blind, nobody = pricer_of(None)
    blind.refresh(lot())
    again, _ = pricer_of(None)
    again.load(blind.dump())
    assert again.cached(AUCTION, TOKEN, 1000) is None and again.age(AUCTION, TOKEN) == 0.0


def test_an_old_state_file_is_asked_about_again():
    pricer, _ = pricer_of(40)
    old = {"want_amount": 1000, "source": "s", "at": 0.0, "low": 35, "firm": 40, "amount": 1000}
    pricer.load({f"{AUCTION.lower()}:{TOKEN.lower()}": {"asked": 5.0, "quote": old}})
    assert pricer.cached(AUCTION, TOKEN, 1000) is None and pricer.age(AUCTION, TOKEN) is None


def test_a_route_quote_is_one_more_level():
    pricer, _ = pricer_of(None, 40, 1000)
    pricer.refresh(lot())
    # The native rail routed the whole lot for 700: a level between the two.
    pricer.note(lot(), "route", 700)
    quote = pricer.cached(AUCTION, TOKEN, 1000)
    assert quote.levels == (("s2", 1000), ("route", 700), ("s1", 40))
    # A route that pays more than every source is the highest level.
    pricer.note(lot(), "route", 1500)
    quote = pricer.cached(AUCTION, TOKEN, 1000)
    assert (quote.want_amount, quote.source) == (1500, "route")
    assert quote.levels == (("route", 1500), ("s2", 1000), ("s1", 40))
    # The last route quote replaces the one before; an unpriced token gets its first estimate.
    pricer.note(lot(), "route", 10)
    assert pricer.cached(AUCTION, TOKEN, 1000).levels == (("s2", 1000), ("s1", 40), ("route", 10))
    blind, _ = pricer_of(None)
    blind.note(lot(available=10), "route", 7)
    assert blind.cached(AUCTION, TOKEN, 1000).want_amount == 700
