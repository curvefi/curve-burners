"""The local auction model against the contract's own numbers."""

import pytest

from keepers.auction_keeper.core.model import Curve, LotRecord, mul_div_up, wad_exp, wad_ln
from tests.keeper.pure.conftest import FLOOR_TOTAL, LENGTH, START_TOTAL, STEP, WAD

# getAmountNeeded(GHO, max, start + elapsed), getAmountNeeded(GHO, available // 7, ...)
# and price(GHO, ...) of burner 0xc014A92C76709D6c7f7c7841823b7164D6D2AbD4 on
# Ethereum, read on 2026-11-24: (elapsed, whole lot, a seventh of it, per 1e18).
START = 1_795_478_400
INITIAL = 274_289_231_570_384_508_256
CHAIN = [
    (0, 250000000000000000000000, 35714285714285714285454, 911446645457710125445),
    (35, 250000000000000000000000, 35714285714285714285454, 911446645457710125445),
    (36, 249185983609454742168822, 35597997658493534595287, 908478915423669851384),
    (37, 249185983609454742168822, 35597997658493534595287, 908478915423669851384),
    (3600, 180426430962054030692021, 25775204423150575812958, 657796261009085091729),
    (36000, 9583883054505047574502, 1369126150643578224919, 34940792241950472250),
    (43169, 5008160096678673199132, 715451442382667599871, 18258682880131751666),
    (43200, 4991853199057988543213, 713121885579712649026, 18199231411594970289),
    (70000, 441023080938818206449, 63003297276974029493, 1607876030764440137),
    (86327, 100654407566015586333, 14379201080859369477, 366964488506312255),
    (86363, 100326670215858149067, 14332381459408307010, 365769628072743475),
    (86364, 100000000000000000000, 14285714285714285715, 364578658183084051),
    (86399, 100000000000000000000, 14285714285714285715, 364578658183084051),
]


@pytest.fixture
def record():
    return LotRecord("0xGHO", INITIAL, START, START + LENGTH, INITIAL)


@pytest.mark.parametrize("elapsed, whole, seventh, price", CHAIN)
def test_quotes_match_the_chain_to_the_wei(curve, record, elapsed, whole, seventh, price):
    ts = START + elapsed
    assert curve.total_price(elapsed) == whole
    assert record.ask(curve, ts) == whole
    assert record.ask(curve, ts, INITIAL // 7) == seventh
    # price(): the same rounding for 1e18 raw units (not capped by availability).
    assert mul_div_up(curve.total_price(elapsed), WAD, INITIAL) == price


def test_wad_functions_at_known_points():
    assert wad_ln(WAD) == 0 and wad_ln(0) == 0
    assert wad_exp(0) == WAD
    assert wad_exp(-41_446_531_673_892_822_313) == 0
    # e and ln 2 to the last digits snekmate's own tests pin.
    assert wad_exp(WAD) == 2_718_281_828_459_045_235
    assert wad_ln(2 * WAD) == 693_147_180_559_945_309
    assert wad_ln(WAD // 2) == -693_147_180_559_945_310
    with pytest.raises(ValueError):
        wad_ln(-1)
    with pytest.raises(OverflowError):
        wad_exp(136 * WAD)


def test_curve_shape(curve):
    assert curve.decay_steps == (LENGTH - 1) // STEP == 2399
    assert curve.total_price(0) == curve.total_price(STEP - 1) == START_TOTAL
    assert curve.total_price(curve.decay_steps * STEP) == FLOOR_TOTAL
    prices = [curve.total_price(step * STEP) for step in range(curve.decay_steps + 1)]
    assert all(later <= earlier for earlier, later in zip(prices, prices[1:], strict=False))
    # Constant inside a step.
    assert curve.total_price(10 * STEP) == curve.total_price(11 * STEP - 1)
    flat = Curve.build(5 * WAD, 5 * WAD, 60, 3600)
    assert flat.total_price(1800) == 5 * WAD
    for bad in ((0, 1, 36, 86400), (1, 2, 36, 86400), (2, 1, 0, 86400), (2, 1, 86400, 86400)):
        with pytest.raises(ValueError):
            Curve.build(*bad)


def test_mul_div_up_rounds_for_the_receiver():
    assert mul_div_up(10, 3, 4) == 8 and mul_div_up(8, 3, 4) == 6
    assert mul_div_up(0, 3, 4) == 0 and mul_div_up(1, 1, 10**30) == 1
    with pytest.raises(ZeroDivisionError):
        mul_div_up(1, 1, 0)


def test_lot_record_follows_takes_and_the_window(curve, record):
    assert record.ask(curve, START - 1) == 0 and record.ask(curve, START + LENGTH) == 0
    part = record.taken(INITIAL // 4, block=7)
    assert part.available == INITIAL // 4 and part.block == 7
    ts = START + 3600
    assert part.ask(curve, ts) == mul_div_up(curve.total_price(3600), INITIAL // 4, INITIAL)
    # A donation cannot lift availability over the snapshot; an empty lot asks nothing.
    assert record.taken(2 * INITIAL).available == INITIAL
    assert not record.taken(0).active(ts) and record.taken(0).ask(curve, ts) == 0
    with pytest.raises(ValueError, match="AmountExceedsAvailable"):
        part.ask(curve, ts, INITIAL)


def test_reaches_is_the_first_second_of_the_step_that_gets_there(curve, record):
    for target in (200_000 * WAD, 5_000 * WAD, 337 * WAD, FLOOR_TOTAL):
        ts = record.reaches(curve, target)
        assert record.ask(curve, ts) <= target < record.ask(curve, ts - 1)
        assert (ts - START) % STEP == 0
    assert record.reaches(curve, START_TOTAL) == START
    assert record.reaches(curve, FLOOR_TOTAL - 1) is None
    # What is left of a lot costs its share: it gets to a target sooner.
    half = record.taken(INITIAL // 2)
    assert half.reaches(curve, 5_000 * WAD) < record.reaches(curve, 5_000 * WAD)
    assert record.taken(0).reaches(curve, WAD) is None
