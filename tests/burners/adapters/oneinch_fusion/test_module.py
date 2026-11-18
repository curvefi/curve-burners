"""Standalone tests for the stateless fusion module.

Covers the Fusion settlement layout (1inch/limit-order-settlement
SimpleSettlement.sol, deployed as Settlement at
0x2Ad5004c60e16E54d5007C80CE329Adde5B51Ef5 on Ethereum and as
SimpleSettlement at the same address on Polygon) against vectors produced by
@1inch/fusion-sdk 2.4.16 (FusionOrder.new with AuctionDetails, Whitelist,
SurplusParams and Fees): the auction details prefixing the getter data, the
post-interaction whitelist with allowed times, the surplus block. The Python
encoders below are cross-checked against the SDK bytes and reused by the
adapter and fork tests. The OneInchFusionAdapter built on top is tested in
test_oneinch_fusion_adapter.py; settlement through the real protocol in
test_oneinch_fusion_fork.py.
"""

import boa
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.burners.adapters.oneinch.test_module import (
    ALLOW_MULTIPLE_FILLS_FLAG,
    EXPIRATION_OFFSET,
    HAS_EXTENSION_FLAG,
    ROUTER,
    ROUTER_NAME,
    ROUTER_VERSION,
    build_extension,
    domain_separator_reference,
    order_digest_reference,
)
from tests.burners.adapters.oneinch_feetaker.test_module import (
    POST_INTERACTION_CALL_FLAG,
    SDK_FEES,
    SDK_INTEGRATOR,
    SDK_MAKER,
    SDK_MAKER_ASSET,
    SDK_MAKING,
    SDK_PROTOCOL,
    SDK_RECEIVER,
    SDK_RESOLVER_A,
    SDK_RESOLVER_B,
    SDK_SALT,
    SDK_TAKER_ASSET,
    SDK_TAKING,
    address_bytes,
    address_half,
    as_tuple,
    bind_salt_reference,
    encode_fees,
    encode_whitelist,
    gross_up,
)
from tests.burners.conftest import custom_err

BASE_POINTS = 10_000_000
AUCTION_HEADER_LEN = 18
POINT_LEN = 5
POST_WHITELIST_PREFIX_LEN = 4
POST_WHITELIST_ENTRY_LEN = 12
SURPLUS_LEN = 33

# The deployed Fusion settlement (Ethereum: Settlement, Polygon:
# SimpleSettlement; identical layout).
SETTLEMENT = "0x2Ad5004c60e16E54d5007C80CE329Adde5B51Ef5"

# @1inch/fusion-sdk 2.4.16: FusionOrder.new(SETTLEMENT, {maker, assets,
# amounts, receiver: RECEIVER, salt: SDK_SALT}, {auction: AuctionDetails({
# startTime: START, duration: 180, initialRateBump: 100_000, points: [
# {coefficient: 60_000, delay: 60}, {coefficient: 20_000, delay: 60}],
# gasCost: {gasBumpEstimate: 1234, gasPriceEstimate: 1000}}), whitelist:
# Whitelist.new(START, [{RESOLVER_A, allowFrom: START}, {RESOLVER_B,
# allowFrom: START + 30}]), surplus: SurplusParams(TAKING + 1e18, 50%)},
# {fees: Fees(ResolverFee(PROTOCOL, 100 bps, discount 50%), IntegratorFee(
# INTEGRATOR, PROTOCOL, 50 bps, share 40%)), allowPartialFills,
# allowMultipleFills, orderExpirationDelay: 12}).
SDK_START = 1_799_000_000
SDK_DURATION = 180
SDK_INITIAL_RATE_BUMP = 100_000
SDK_POINTS = [(60_000, 60), (20_000, 60)]
SDK_GAS = (1234, 1000)
SDK_WHITELIST = [(SDK_RESOLVER_A, 0), (SDK_RESOLVER_B, 30)]
SDK_ESTIMATE = SDK_TAKING + 10**18
SDK_SURPLUS_FEE = 50
SDK_DEADLINE = SDK_START + SDK_DURATION + 12
SDK_AUCTION_DETAILS = bytes.fromhex("0004d2000003e86b3a8fc00000b40186a00200ea60003c004e20003c")
SDK_POST_WHITELIST = bytes.fromhex("6b3a8fc002aaaaaaaaaaaaaaaaaaaa0000bbbbbbbbbbbbbbbbbbbb001e")
SDK_EXTENSION = bytes.fromhex(
    "0000012b000000960000009600000096000000960000004b0000000000000000"
    "2ad5004c60e16e54d5007c80ce329adde5b51ef50004d2000003e86b3a8fc00000b40186a002"
    "00ea60003c004e20003c01f42803e83202aaaaaaaaaaaaaaaaaaaabbbbbbbbbbbbbbbbbbbb"
    "2ad5004c60e16e54d5007c80ce329adde5b51ef50004d2000003e86b3a8fc00000b40186a002"
    "00ea60003c004e20003c01f42803e83202aaaaaaaaaaaaaaaaaaaabbbbbbbbbbbbbbbbbbbb"
    "2ad5004c60e16e54d5007c80ce329adde5b51ef501"
    "8888888888888888888888888888888888888888"
    "7777777777777777777777777777777777777777"
    "9999999999999999999999999999999999999999"
    "01f42803e8326b3a8fc002aaaaaaaaaaaaaaaaaaaa0000bbbbbbbbbbbbbbbbbbbb001e"
    "00000000000000000000000000000000000000000000152d10a897fe9de40000"
    "32"
)
SDK_ORDER_SALT = 1780731860627700044962976663140104920600263653670078331058873933869405918
SDK_ORDER_TRAITS = 33471150795161712739625987854073848363835857117269507143130959182499556622336
SDK_ORDER_HASH = bytes.fromhex("cdcf264e23d5716dbbacdfb2cbdc2eab395015d6c394656a463d2e8240aa667e")
# FusionOrder.calcTakingAmount(RESOLVER_A, MAKING, time) with the SDK's
# AuctionCalculator (no gas bump: the calculator ignores block.basefee).
SDK_TAKING_AT = {
    SDK_START - 1: 102_010 * 10**18,
    SDK_START: 102_010 * 10**18,
    SDK_START + 30: 101_808 * 10**18,
    SDK_START + 60: 101_606 * 10**18,
    SDK_START + 90: 101_404 * 10**18,
    SDK_START + 180: 101_000 * 10**18,
    SDK_START + 181: 101_000 * 10**18,
}

HARNESS_SOURCE = """
# pragma version 0.5.0b1

import contracts.burners.adapters.oneinch.oneinch as oneinch
import contracts.burners.adapters.oneinch.fusion as fusion


@external
@pure
def parse_auction_details(
    _data: Bytes[oneinch.MAX_EXTENSION], _pos: uint256
) -> (fusion.AuctionDetails, uint256):
    return fusion._parse_auction_details(_data, _pos)


@external
@pure
def parse_surplus(_data: Bytes[oneinch.MAX_EXTENSION], _pos: uint256) -> (fusion.Surplus, uint256):
    return fusion._parse_surplus(_data, _pos)


@external
@pure
def constants() -> (uint256, uint256, uint256, uint256, uint256, uint256):
    return (
        fusion.AUCTION_HEADER_LEN,
        fusion.POINT_LEN,
        fusion.POST_WHITELIST_PREFIX_LEN,
        fusion.POST_WHITELIST_ENTRY_LEN,
        fusion.SURPLUS_LEN,
        fusion.BASE_POINTS,
    )
"""


# Reference encoders (the SDK layouts in Python)


def encode_auction_details(
    start_time: int,
    duration: int,
    initial_rate_bump: int,
    points: list[tuple[int, int]] | None = None,
    gas_bump_estimate: int = 0,
    gas_price_estimate: int = 0,
) -> bytes:
    """AuctionDetails.encode: 3 + 4 + 4 + 3 + 3 + 1 bytes, (3 + 2)[N] points."""
    points = points or []
    return (
        gas_bump_estimate.to_bytes(3, "big")
        + gas_price_estimate.to_bytes(4, "big")
        + start_time.to_bytes(4, "big")
        + duration.to_bytes(3, "big")
        + initial_rate_bump.to_bytes(3, "big")
        + len(points).to_bytes(1, "big")
        + b"".join(bump.to_bytes(3, "big") + delay.to_bytes(2, "big") for bump, delay in points)
    )


def encode_post_whitelist(allowed_time: int, entries: list[tuple[str, int]]) -> bytes:
    """Whitelist.encode: 4 bytes allowed time, 1 byte size, (bytes10, bytes2)[N]."""
    return (
        allowed_time.to_bytes(4, "big")
        + len(entries).to_bytes(1, "big")
        + b"".join(address_half(a) + delay.to_bytes(2, "big") for a, delay in entries)
    )


def encode_surplus(estimated_taking_amount: int, protocol_fee: int) -> bytes:
    return estimated_taking_amount.to_bytes(32, "big") + protocol_fee.to_bytes(1, "big")


def fusion_getter_data(auction: bytes, fees: bytes, whitelist: bytes, tail: bytes = b"") -> bytes:
    return auction + fees + whitelist + tail


def fusion_post_data(
    integrator: str,
    protocol: str,
    custom_receiver: str | None,
    fees: bytes,
    post_whitelist: bytes,
    surplus: bytes,
    tail: bytes = b"",
) -> bytes:
    flags = b"\x01" if custom_receiver else b"\x00"
    receiver = address_bytes(custom_receiver) if custom_receiver else b""
    return (
        flags
        + address_bytes(integrator)
        + address_bytes(protocol)
        + receiver
        + fees
        + post_whitelist
        + surplus
        + tail
    )


def fusion_fields(
    settlement: str,
    auction: bytes,
    fees: bytes,
    whitelist: list[tuple[str, int]],
    allowed_time: int,
    integrator: str,
    protocol: str,
    custom_receiver: str | None,
    surplus: bytes,
    getter_tail: bytes = b"",
    post_tail: bytes = b"",
) -> list[bytes]:
    """The eight extension fields of a FusionExtension.build()."""
    getter = address_bytes(settlement) + fusion_getter_data(
        auction, fees, encode_whitelist([a for a, _ in whitelist]), getter_tail
    )
    post = address_bytes(settlement) + fusion_post_data(
        integrator,
        protocol,
        custom_receiver,
        fees,
        encode_post_whitelist(allowed_time, whitelist),
        surplus,
        post_tail,
    )
    return [b"", b"", getter, getter, b"", b"", b"", post]


def build_fusion_extension(*args, **kwargs) -> bytes:
    return build_extension(fusion_fields(*args, **kwargs))


def auction_bump_reference(
    now: int, start: int, duration: int, initial: int, points: list[tuple[int, int]]
) -> int:
    """SimpleSettlement._getAuctionBump (no gas bump)."""
    finish = start + duration
    if now <= start:
        return initial
    if now >= finish:
        return 0
    current_time, current_bump = start, initial
    for bump, delay in points:
        next_time = current_time + delay
        if now <= next_time:
            return ((now - current_time) * bump + (next_time - now) * current_bump) // (
                next_time - current_time
            )
        current_time, current_bump = next_time, bump
    return (finish - now) * current_bump // (finish - current_time)


def bump_up(amount: int, bump: int) -> int:
    """SimpleSettlement._getTakingAmount on the FeeTaker amount (rounded up)."""
    return -(-amount * (BASE_POINTS + bump) // BASE_POINTS)


SDK_GETTER_DATA = fusion_getter_data(
    SDK_AUCTION_DETAILS, encode_fees(*SDK_FEES), encode_whitelist([SDK_RESOLVER_A, SDK_RESOLVER_B])
)
SDK_POST_DATA = fusion_post_data(
    SDK_INTEGRATOR,
    SDK_PROTOCOL,
    SDK_RECEIVER,
    encode_fees(*SDK_FEES),
    SDK_POST_WHITELIST,
    encode_surplus(SDK_ESTIMATE, SDK_SURPLUS_FEE),
)


@pytest.fixture(scope="module")
def harness():
    return boa.loads(HARNESS_SOURCE, name="FusionHarness", filename="FusionHarness.vy")


# SDK vectors


def test_encoders_reproduce_the_sdk_extension():
    assert (
        encode_auction_details(SDK_START, SDK_DURATION, SDK_INITIAL_RATE_BUMP, SDK_POINTS, *SDK_GAS)
        == SDK_AUCTION_DETAILS
    )
    assert encode_post_whitelist(SDK_START, SDK_WHITELIST) == SDK_POST_WHITELIST
    assert (
        build_fusion_extension(
            SETTLEMENT,
            SDK_AUCTION_DETAILS,
            encode_fees(*SDK_FEES),
            SDK_WHITELIST,
            SDK_START,
            SDK_INTEGRATOR,
            SDK_PROTOCOL,
            SDK_RECEIVER,
            encode_surplus(SDK_ESTIMATE, SDK_SURPLUS_FEE),
        )
        == SDK_EXTENSION
    )
    assert bind_salt_reference(SDK_SALT, SDK_EXTENSION) == SDK_ORDER_SALT
    assert SDK_ORDER_TRAITS == (
        ALLOW_MULTIPLE_FILLS_FLAG
        | HAS_EXTENSION_FLAG
        | POST_INTERACTION_CALL_FLAG
        | (SDK_DEADLINE << EXPIRATION_OFFSET)
    )
    domain = domain_separator_reference(ROUTER_NAME, ROUTER_VERSION, 1, ROUTER)
    order = (
        SDK_ORDER_SALT,
        SDK_MAKER,
        SETTLEMENT,
        SDK_MAKER_ASSET,
        SDK_TAKER_ASSET,
        SDK_MAKING,
        SDK_TAKING,
        SDK_ORDER_TRAITS,
    )
    assert order_digest_reference(order, domain) == SDK_ORDER_HASH


def test_bump_curve_matches_the_sdk_calculator():
    # Whitelisted resolver: 0.5% integrator + 1% * 50% resolver, then the bump.
    fee_amount = gross_up(SDK_TAKING, 500, 500)
    for now, expected in SDK_TAKING_AT.items():
        bump = auction_bump_reference(
            now, SDK_START, SDK_DURATION, SDK_INITIAL_RATE_BUMP, SDK_POINTS
        )
        assert bump_up(fee_amount, bump) == expected, now


def test_sdk_extension_parses_field_by_field(harness):
    details, pos = harness.parse_auction_details(SDK_GETTER_DATA, 0)
    assert as_tuple(details) == (1234, 1000, SDK_START, SDK_DURATION, SDK_INITIAL_RATE_BUMP, 2)
    assert pos == AUCTION_HEADER_LEN + 2 * POINT_LEN == len(SDK_AUCTION_DETAILS)
    surplus_pos = len(SDK_POST_DATA) - SURPLUS_LEN
    surplus, pos = harness.parse_surplus(SDK_POST_DATA, surplus_pos)
    assert (as_tuple(surplus), pos) == ((SDK_ESTIMATE, SDK_SURPLUS_FEE), len(SDK_POST_DATA))


def test_constants(harness):
    assert harness.constants() == (
        AUCTION_HEADER_LEN,
        POINT_LEN,
        POST_WHITELIST_PREFIX_LEN,
        POST_WHITELIST_ENTRY_LEN,
        SURPLUS_LEN,
        BASE_POINTS,
    )


# Auction details


def test_auction_details_are_bounded_by_their_point_count(harness):
    details = encode_auction_details(1, 2, 3, [(4, 5), (6, 7), (8, 9)], 10, 11)
    parsed, pos = harness.parse_auction_details(b"\xff" * 2 + details, 2)
    assert (as_tuple(parsed), pos) == ((10, 11, 1, 2, 3, 3), 2 + len(details))
    with boa.reverts(custom_err("OrderNotValid(string)", "BadAuctionDetails")):
        harness.parse_auction_details(details[:-1], 0)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadAuctionDetails")):
        harness.parse_auction_details(details[:17], 0)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadAuctionDetails")):
        harness.parse_auction_details(details, 20)
    # Maximal fields parse: 24-bit bumps, 32-bit times, 255 points.
    wide = encode_auction_details(
        2**32 - 1, 2**24 - 1, 2**24 - 1, [(2**24 - 1, 2**16 - 1)] * 255, 2**24 - 1, 2**32 - 1
    )
    parsed, pos = harness.parse_auction_details(wide, 0)
    assert as_tuple(parsed) == (2**24 - 1, 2**32 - 1, 2**32 - 1, 2**24 - 1, 2**24 - 1, 255)
    assert pos == len(wide)


# Surplus block


def test_surplus_bounds_mirror_the_settlement(harness):
    surplus, pos = harness.parse_surplus(encode_surplus(2**256 - 1, 100), 0)
    assert (as_tuple(surplus), pos) == ((2**256 - 1, 100), SURPLUS_LEN)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadSurplus")):
        harness.parse_surplus(encode_surplus(1, 101), 0)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadSurplus")):
        harness.parse_surplus(encode_surplus(1, 1)[:-1], 0)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadSurplus")):
        harness.parse_surplus(encode_surplus(1, 1), 1)


# Round trip fuzz


@given(
    start=st.integers(min_value=0, max_value=2**32 - 1),
    duration=st.integers(min_value=0, max_value=2**24 - 1),
    initial=st.integers(min_value=0, max_value=2**24 - 1),
    points=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=2**24 - 1),
            st.integers(min_value=0, max_value=2**16 - 1),
        ),
        max_size=8,
    ),
    estimate=st.integers(min_value=0, max_value=2**256 - 1),
    fee=st.integers(min_value=0, max_value=100),
)
@settings(max_examples=40, deadline=None)
def test_layouts_round_trip(harness, start, duration, initial, points, estimate, fee):
    details = encode_auction_details(start, duration, initial, points)
    parsed, pos = harness.parse_auction_details(details, 0)
    assert as_tuple(parsed) == (0, 0, start, duration, initial, len(points))
    assert pos == len(details)
    surplus, pos = harness.parse_surplus(details + encode_surplus(estimate, fee), len(details))
    assert (as_tuple(surplus), pos) == ((estimate, fee), len(details) + SURPLUS_LEN)
