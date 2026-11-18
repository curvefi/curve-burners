"""Bit-exact Python mirror of the on-chain Dutch auction curve.

``wad_ln``/``wad_exp`` reproduce snekmate's ``_wad_ln``/``_wad_exp`` (the
dependency pinned in requirements.in) integer for integer on the domain the
curve uses (same range reduction, same rational approximations, same
rounding; snekmate answers ln(0) with 0 where this mirror raises), and
``total_price`` mirrors ``contracts/burners/auction/dutch_auction_math.vy``.
Used by the preflight to recompute what a deployed burner must publish and by
the tests as the exact reference for quotes; accuracy against a high-precision
reference is a separate property (see tests/burners/test_wad_math.py).
"""

WAD = 10**18
INT256_MAX = 2**255 - 1

_LN2_96 = 54916777467707473351141471128
_EXP_FLOOR = -41446531673892822313
_EXP_OVERFLOW = 135305999368893231589


class LnUndefined(ValueError):
    pass


class ExpOverflow(OverflowError):
    pass


def _sdiv(a: int, b: int) -> int:
    """EVM SDIV: division truncating toward zero."""
    quotient = abs(a) // abs(b)
    return -quotient if (a < 0) != (b < 0) else quotient


def wad_exp(x: int) -> int:
    """exp(x / 1e18) * 1e18 as the contract computes it."""
    if x <= _EXP_FLOOR:
        return 0
    if x >= _EXP_OVERFLOW:
        raise ExpOverflow(x)
    y = _sdiv(x << 78, 5**18)
    k = (_sdiv(y << 96, _LN2_96) + 2**95) >> 96
    y -= k * _LN2_96

    t = y + 1346386616545796478920950773328
    t = ((t * y) >> 96) + 57155421227552351082224309758442
    p = t + y - 94201549194550492254356042504812
    p = ((p * t) >> 96) + 28719021644029726153956944680412240
    p = p * y + (4385272521454847904659076985693276 << 96)

    q = y - 2855989394907223263936484059900
    q = ((q * y) >> 96) + 50020603652535783019961831881945
    q = ((q * y) >> 96) - 533845033583426703283633433725380
    q = ((q * y) >> 96) + 3604857256930695427073651918091429
    q = ((q * y) >> 96) - 14423608567350463180887372962807573
    q = ((q * y) >> 96) + 26449188498355588339934803723976023

    r = _sdiv(p, q)
    return (r * 3822833074963236453042738258902158003155416615667) >> (195 - k)


def wad_ln(x: int) -> int:
    """ln(x / 1e18) * 1e18 as the contract computes it."""
    if x <= 0:
        raise LnUndefined(x)
    log2 = x.bit_length() - 1
    m = (x << (255 - log2)) >> 159

    p = m + 3273285459638523848632254066296
    p = ((p * m) >> 96) + 24828157081833163892658089445524
    p = ((p * m) >> 96) + 43456485725739037958740375743393
    p = ((p * m) >> 96) - 11111509109440967052023855526967
    p = ((p * m) >> 96) - 45023709667254063763336534515857
    p = ((p * m) >> 96) - 14706773417378608786704636184526
    p = p * m - (795164235651350426258249787498 << 96)

    q = m + 5573035233440673466300451813936
    q = ((q * m) >> 96) + 71694874799317883764090561454958
    q = ((q * m) >> 96) + 283447036172924575727196451306956
    q = ((q * m) >> 96) + 401686690394027663651624208769553
    q = ((q * m) >> 96) + 204048457590392012362485061816622
    q = ((q * m) >> 96) + 31853899698501571402653359427138
    q = ((q * m) >> 96) + 909429971244387300277376558375

    r = _sdiv(p, q)
    r *= 1677202110996718588342820967067443963516166
    r += 16597577552685614221487285958193947469193820559219878177908093499208371 * (log2 - 96)
    r += 600920179829731861736702779321621459595472258049074101567377883020018308
    return r >> 174


def curve_logs(start_total: int, floor_total: int) -> tuple[int, int]:
    """(log_start, log_drop) exactly as the core stores them."""
    if start_total > INT256_MAX:
        raise ValueError("start_total above int256")
    if floor_total > start_total:
        raise ValueError("floor_total above start_total")
    log_start = wad_ln(start_total)
    return log_start, log_start - wad_ln(floor_total)


def decay_steps(auction_length: int, step_duration: int) -> int:
    """Steps over which the curve decays: the last active second of the
    window, in whole steps."""
    return (auction_length - 1) // step_duration


def total_price(
    start_total: int,
    floor_total: int,
    log_start: int,
    log_drop: int,
    steps: int,
    elapsed: int,
    step_duration: int,
) -> int:
    """The full-lot quote at ``elapsed`` seconds into the window."""
    step = elapsed // step_duration
    if step == 0:
        return start_total
    if step >= steps:
        return floor_total
    if start_total == floor_total:
        return start_total
    log_offset = log_drop * step // steps
    price = wad_exp(log_start - log_offset)
    return min(start_total, max(floor_total, price))


def proportional_payment(total: int, amount: int, initial_amount: int) -> int:
    """ceil(total * amount / initial_amount)."""
    if amount == 0 or total == 0:
        return 0
    return (total * amount - 1) // initial_amount + 1
