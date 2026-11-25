"""The auction, simulated locally: every lot's ask from events and the clock.

The curve of a ``DutchAuctionBurner`` is public arithmetic over a handful of
numbers, so the keeper does not have to ask the chain what a lot costs:

* the curve parameters (``start_total``, ``floor_total``, ``step_duration``,
  ``auction_length``) are read once per auction (``Curve``);
* a lot is its ``LotStaged`` event (``initial_amount``, window start and
  end) plus what is left of it, which only a ``Taken`` event or a balance
  read changes (``LotRecord``);
* the ask at any timestamp, the next block's included, is then
  ``ceil(total_price(ts - start) * available / initial_amount)``.

Everything here is integer arithmetic mirroring the contracts bit for bit:
``dutch_auction_math.vy`` (``curve_logs``, ``total_price``,
``proportional_payment``) and snekmate's ``wad_ln`` / ``wad_exp`` it calls.
No node, no HTTP, no imports beyond the standard library.

What the model cannot see is the policy outside the curve: a kill switch or
a target migration (``_sellable``), a resync of the economics, tokens moved
without a ``Taken`` event. The keeper therefore reads the chain for the few
lots it is about to act on, and every take is simulated on chain anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

_MASK = 2**256 - 1
_SIGN = 2**255


def _signed(value: int) -> int:
    """Wrap to int256 (two's complement), as the EVM's unchecked ops do."""
    value &= _MASK
    return value - 2**256 if value >= _SIGN else value


def _sdiv(a: int, b: int) -> int:
    """EVM SDIV: the quotient truncated toward zero."""
    quotient = abs(a) // abs(b)
    return -quotient if (a < 0) != (b < 0) else quotient


def wad_ln(x: int) -> int:
    """snekmate ``math._wad_ln``: ln of a WAD fixed-point number, WAD."""
    if x < 0:
        raise ValueError("wad_ln undefined")
    if x == 0:
        return 0
    k = x.bit_length() - 1 - 96
    x = ((x << (159 - k)) & _MASK) >> 159

    p = _signed(((x + 3_273_285_459_638_523_848_632_254_066_296) * x >> 96)
                + 24_828_157_081_833_163_892_658_089_445_524)  # fmt: skip
    p = _signed((_signed(p * x) >> 96) + 43_456_485_725_739_037_958_740_375_743_393)
    p = _signed((_signed(p * x) >> 96) - 11_111_509_109_440_967_052_023_855_526_967)
    p = _signed((_signed(p * x) >> 96) - 45_023_709_667_254_063_763_336_534_515_857)
    p = _signed((_signed(p * x) >> 96) - 14_706_773_417_378_608_786_704_636_184_526)
    p = _signed(_signed(p * x) - (795_164_235_651_350_426_258_249_787_498 << 96))

    q = _signed((_signed((x + 5_573_035_233_440_673_466_300_451_813_936) * x) >> 96)
                + 71_694_874_799_317_883_764_090_561_454_958)  # fmt: skip
    q = _signed((_signed(q * x) >> 96) + 283_447_036_172_924_575_727_196_451_306_956)
    q = _signed((_signed(q * x) >> 96) + 401_686_690_394_027_663_651_624_208_769_553)
    q = _signed((_signed(q * x) >> 96) + 204_048_457_590_392_012_362_485_061_816_622)
    q = _signed((_signed(q * x) >> 96) + 31_853_899_698_501_571_402_653_359_427_138)
    q = _signed((_signed(q * x) >> 96) + 909_429_971_244_387_300_277_376_558_375)

    r = _sdiv(p, q)
    return (
        _signed(
            r * 1_677_202_110_996_718_588_342_820_967_067_443_963_516_166
            + k
            * 16_597_577_552_685_614_221_487_285_958_193_947_469_193_820_559_219_878_177_908_093_499_208_371
            + 600_920_179_829_731_861_736_702_779_321_621_459_595_472_258_049_074_101_567_377_883_020_018_308
        )
        >> 174
    )


def wad_exp(x: int) -> int:
    """snekmate ``math._wad_exp``: e**x for a WAD fixed-point x, WAD."""
    if x <= -41_446_531_673_892_822_313:
        return 0
    if x >= 135_305_999_368_893_231_589:
        raise OverflowError("wad_exp overflow")
    x = _sdiv(_signed(x << 78), 5**18)
    k = _signed(_sdiv(_signed(x << 96), 54_916_777_467_707_473_351_141_471_128) + 2**95) >> 96
    x = _signed(x - k * 54_916_777_467_707_473_351_141_471_128)

    y = _signed((_signed((x + 1_346_386_616_545_796_478_920_950_773_328) * x) >> 96)
                + 57_155_421_227_552_351_082_224_309_758_442)  # fmt: skip
    p = _signed(
        _signed(
            _signed(
                (_signed(_signed(y + x - 94_201_549_194_550_492_254_356_042_504_812) * y) >> 96)
                + 28_719_021_644_029_726_153_956_944_680_412_240
            )
            * x
        )
        + (4_385_272_521_454_847_904_659_076_985_693_276 << 96)
    )

    q = _signed((_signed((x - 2_855_989_394_907_223_263_936_484_059_900) * x) >> 96)
                + 50_020_603_652_535_783_019_961_831_881_945)  # fmt: skip
    q = _signed((_signed(q * x) >> 96) - 533_845_033_583_426_703_283_633_433_725_380)
    q = _signed((_signed(q * x) >> 96) + 3_604_857_256_930_695_427_073_651_918_091_429)
    q = _signed((_signed(q * x) >> 96) - 14_423_608_567_350_463_180_887_372_962_807_573)
    q = _signed((_signed(q * x) >> 96) + 26_449_188_498_355_588_339_934_803_723_976_023)

    r = _sdiv(p, q)
    return (
        ((r & _MASK) * 3_822_833_074_963_236_453_042_738_258_902_158_003_155_416_615_667) & _MASK
    ) >> (195 - k)


def mul_div_up(a: int, b: int, denominator: int) -> int:
    """``dutch_auction_math.mul_div_up``: ceil(a * b / denominator)."""
    if denominator == 0:
        raise ZeroDivisionError("DivisionByZero")
    if a == 0 or b == 0:
        return 0
    return (a * b - 1) // denominator + 1


@dataclass(frozen=True)
class Curve:
    """The economics of one auction, with the logarithms the contract
    prepares in ``_set_economics`` (``decay_steps``, ``log_start``,
    ``log_drop``) recomputed the same way."""

    start_total: int
    floor_total: int
    step_duration: int
    auction_length: int
    decay_steps: int
    log_start: int
    log_drop: int

    @classmethod
    def build(
        cls, start_total: int, floor_total: int, step_duration: int, auction_length: int
    ) -> Curve:
        if not 0 < floor_total <= start_total < _SIGN:
            raise ValueError("bad curve totals")
        if step_duration <= 0 or auction_length <= 0:
            raise ValueError("bad curve timing")
        decay_steps = (auction_length - 1) // step_duration
        if decay_steps == 0:
            raise ValueError("the auction holds fewer than one step")
        log_start = wad_ln(start_total)
        return cls(
            start_total,
            floor_total,
            step_duration,
            auction_length,
            decay_steps,
            log_start,
            log_start - wad_ln(floor_total),
        )

    def total_price(self, elapsed: int) -> int:
        """``dutch_auction_math.total_price``: the full lot's price ``elapsed``
        seconds into its window. Constant within a step."""
        step = elapsed // self.step_duration
        if step == 0:
            return self.start_total
        if step >= self.decay_steps:
            return self.floor_total
        if self.start_total == self.floor_total:
            return self.start_total
        log_offset = self.log_drop * step // self.decay_steps
        return min(self.start_total, max(self.floor_total, wad_exp(self.log_start - log_offset)))

    def elapsed_at(self, total: int) -> int | None:
        """The first elapsed second at which the full lot costs at most
        ``total``; None when the curve never gets that low. A bisection over
        the steps: the price never rises from one step to the next."""
        if total >= self.start_total:
            return 0
        if total < self.floor_total:
            return None
        lo, hi = 0, self.decay_steps  # price(lo) > total >= price(hi)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.total_price(mid * self.step_duration) <= total:
                hi = mid
            else:
                lo = mid
        return hi * self.step_duration


@dataclass(frozen=True)
class LotRecord:
    """One staged lot as the events describe it. ``available`` is the
    contract's ``min(initial_amount, balanceOf)`` as last learnt: the staging
    snapshot, a ``Taken`` event's ``remaining_balance``, or a chain read."""

    token: str
    initial_amount: int
    start: int
    end: int
    available: int
    # Block of the event the record was last changed by (0: a chain read).
    block: int = 0

    def active(self, timestamp: int) -> bool:
        return self.start <= timestamp < self.end and self.available > 0

    def taken(self, remaining_balance: int, block: int = 0) -> LotRecord:
        return replace(self, available=min(self.initial_amount, remaining_balance), block=block)

    def ask(self, curve: Curve, timestamp: int, amount: int | None = None) -> int:
        """``getAmountNeeded(token, amount, timestamp)``: want owed for
        ``amount`` (everything available by default); 0 for an inactive lot."""
        if not self.active(timestamp):
            return 0
        if amount is None:
            amount = self.available
        if amount > self.available:
            raise ValueError("AmountExceedsAvailable")
        return mul_div_up(curve.total_price(timestamp - self.start), amount, self.initial_amount)

    def reaches(self, curve: Curve, target: int) -> int | None:
        """The first timestamp at which everything available costs at most
        ``target`` want; None when the window ends above it."""
        if self.available <= 0:
            return None
        # ceil(total * available / initial) <= target  <=>  total <= target * initial // available
        elapsed = curve.elapsed_at(target * self.initial_amount // self.available)
        if elapsed is None or self.start + elapsed >= self.end:
            return None
        return self.start + elapsed


__all__ = ["Curve", "LotRecord", "mul_div_up", "wad_exp", "wad_ln"]
