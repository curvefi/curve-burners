"""Standalone tests for the stateless feetaker module.

Covers the FeeTaker extension layout (1inch/limit-order-protocol
FeeTaker.sol + AmountGetterWithFee.sol, deployed at
0xc0DFdB9E7a392c3dBBE7c6FBe8FBC1789C9FE05e on Ethereum and Polygon) against
vectors produced by @1inch/limit-order-sdk 5.4.4 (FeeTakerExtension /
LimitOrder), the field extraction and salt binding added to the oneinch
module for it, and the shape checks the fee-taking adapters share. The
Python encoders below are cross-checked against the SDK bytes and reused by
the adapter and fork tests. The OneInchFeeTakerAdapter built on top is
tested in test_oneinch_feetaker_adapter.py; settlement through the real
protocol in test_oneinch_feetaker_fork.py.
"""

import boa
import pytest
from eth_utils import keccak
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.burners.adapters.oneinch.test_module import (
    ALLOW_MULTIPLE_FILLS_FLAG,
    EXPIRATION_OFFSET,
    HAS_EXTENSION_FLAG,
    MASK160,
    ROUTER,
    ROUTER_NAME,
    ROUTER_VERSION,
    build_extension,
    domain_separator_reference,
    order_digest_reference,
)
from tests.burners.conftest import custom_err

POST_INTERACTION_CALL_FLAG = 1 << 251
PRE_INTERACTION_CALL_FLAG = 1 << 252

# ExtensionLib field indexes.
MAKER_ASSET_SUFFIX = 0
TAKER_ASSET_SUFFIX = 1
MAKING_AMOUNT_DATA = 2
TAKING_AMOUNT_DATA = 3
PREDICATE = 4
MAKER_PERMIT = 5
PRE_INTERACTION_DATA = 6
POST_INTERACTION_DATA = 7

# AmountGetterWithFee scales.
BASE_1E5 = 100_000
BASE_1E2 = 100
FEE_DATA_LEN = 6
WHITELIST_ENTRY_LEN = 10
POST_HEADER_LEN = 41

# The deployed FeeTaker (identical verified source on Ethereum and Polygon).
FEE_TAKER = "0xc0DFdB9E7a392c3dBBE7c6FBe8FBC1789C9FE05e"

# @1inch/limit-order-sdk 5.4.4: FeeTakerExtension.new(FEE_TAKER, Fees(
# ResolverFee(PROTOCOL, 100 bps, whitelistDiscount 50%), IntegratorFee(
# INTEGRATOR, PROTOCOL, 50 bps, share 40%)), [RESOLVER_A, RESOLVER_B],
# {customReceiver: RECEIVER}).build(), and a LimitOrder over it with
# MakerTraits.default().withExpiration(1_800_000_000).allowPartialFills()
# .allowMultipleFills().enablePostInteraction(), salt buildSalt(ext, SALT).
SDK_MAKER = "0x1111111111111111111111111111111111111111"
SDK_MAKER_ASSET = "0x4444444444444444444444444444444444444444"
SDK_TAKER_ASSET = "0x5555555555555555555555555555555555555555"
SDK_RECEIVER = "0x9999999999999999999999999999999999999999"
SDK_INTEGRATOR = "0x8888888888888888888888888888888888888888"
SDK_PROTOCOL = "0x7777777777777777777777777777777777777777"
SDK_RESOLVER_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SDK_RESOLVER_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
SDK_SALT = 0x0102030405060708091011
SDK_EXPIRATION = 1_800_000_000
SDK_MAKING = 250 * 10**18
SDK_TAKING = 100_000 * 10**18
# integrator fee 500 (0.5%), share 40%, resolver fee 1000 (1%), discount 50
SDK_FEES = (500, 40, 1000, 50)
SDK_EXTENSION = bytes.fromhex(
    "000000ca0000005e0000005e0000005e0000005e0000002f0000000000000000"
    "c0dfdb9e7a392c3dbbe7c6fbe8fbc1789c9fe05e01f42803e83202aaaaaaaaaaaaaaaaaaaa"
    "bbbbbbbbbbbbbbbbbbbb"
    "c0dfdb9e7a392c3dbbe7c6fbe8fbc1789c9fe05e01f42803e83202aaaaaaaaaaaaaaaaaaaa"
    "bbbbbbbbbbbbbbbbbbbb"
    "c0dfdb9e7a392c3dbbe7c6fbe8fbc1789c9fe05e01"
    "8888888888888888888888888888888888888888"
    "7777777777777777777777777777777777777777"
    "9999999999999999999999999999999999999999"
    "01f42803e83202aaaaaaaaaaaaaaaaaaaabbbbbbbbbbbbbbbbbbbb"
)
SDK_ORDER_SALT = 1780731860627700044962976925892681682176479356538083430623833579223674803
SDK_ORDER_TRAITS = 33471150795161712739625987854073848363835857118478200848988222348404189036544
SDK_ORDER_HASH = bytes.fromhex("1d9d3c4230273df37cd95c5c1b4bb0223747041050014557b0289e0171c9a146")
# FeeTakerExtension.getTakingAmount(taker, SDK_TAKING) for a whitelisted
# resolver (0.5% + 1% * 50%) and for an outsider (0.5% + 1%).
SDK_TAKING_WHITELISTED = 101_000 * 10**18
SDK_TAKING_OUTSIDER = 101_500 * 10**18

HARNESS_SOURCE = """
# pragma version 0.5.0b1

import contracts.burners.adapters.oneinch.oneinch as oneinch
import contracts.burners.adapters.oneinch.feetaker as feetaker


@external
@pure
def field(
    _extension: Bytes[oneinch.MAX_EXTENSION], _offsets: uint256, _field: uint256
) -> Bytes[oneinch.MAX_EXTENSION]:
    return oneinch._field(_extension, _offsets, _field)


@external
@pure
def bind_salt(_salt: uint256, _extension: Bytes[oneinch.MAX_EXTENSION]) -> uint256:
    return oneinch._bind_salt(_salt, _extension)


@external
@pure
def amount_getter_data(
    _extension: Bytes[oneinch.MAX_EXTENSION], _getter: address
) -> Bytes[oneinch.MAX_EXTENSION]:
    return feetaker._amount_getter_data(_extension, oneinch._extension_offsets(_extension), _getter)


@external
@pure
def post_interaction_data(
    _extension: Bytes[oneinch.MAX_EXTENSION], _listener: address
) -> Bytes[oneinch.MAX_EXTENSION]:
    return feetaker._post_interaction_data(
        _extension, oneinch._extension_offsets(_extension), _listener
    )


@external
@pure
def parse_fees(_data: Bytes[oneinch.MAX_EXTENSION], _pos: uint256) -> (feetaker.Fees, uint256):
    return feetaker._parse_fees(_data, _pos)


@external
@pure
def parse_whitelist(
    _data: Bytes[oneinch.MAX_EXTENSION], _pos: uint256, _prefix_len: uint256, _entry_len: uint256
) -> (uint256, uint256, uint256):
    return feetaker._parse_whitelist(_data, _pos, _prefix_len, _entry_len)


@external
@pure
def check_fee_block(
    _getter: Bytes[oneinch.MAX_EXTENSION],
    _getter_pos: uint256,
    _post: Bytes[oneinch.MAX_EXTENSION],
    _post_pos: uint256,
    _post_whitelist_prefix_len: uint256,
    _post_whitelist_entry_len: uint256,
) -> (feetaker.Fees, uint256, uint256, uint256):
    return feetaker._check_fee_block(
        _getter, _getter_pos, _post, _post_pos, _post_whitelist_prefix_len, _post_whitelist_entry_len
    )


@external
@pure
def parse_post_header(_data: Bytes[oneinch.MAX_EXTENSION]) -> (feetaker.Receivers, uint256):
    return feetaker._parse_post_header(_data)


@external
@pure
def taking_receiver(_receivers: feetaker.Receivers, _maker: address) -> address:
    return feetaker._taking_receiver(_receivers, _maker)


@external
@pure
def constants() -> (uint256, uint256, uint256, uint256, uint256):
    return (
        oneinch.MAX_EXTENSION,
        feetaker.FEE_DATA_LEN,
        feetaker.WHITELIST_ENTRY_LEN,
        feetaker.POST_HEADER_LEN,
        feetaker.CUSTOM_RECEIVER_FLAG,
    )
"""


# Reference encoders (the SDK layouts in Python)


def address_bytes(address: str) -> bytes:
    return bytes.fromhex(address[2:])


def address_half(address: str) -> bytes:
    """The low ten bytes the whitelists compare."""
    return address_bytes(address)[-10:]


def encode_fees(
    integrator_fee: int = 0,
    integrator_share: int = 0,
    resolver_fee: int = 0,
    whitelist_discount: int = 0,
) -> bytes:
    """AmountGetterWithFee._parseFeeData: 2 + 1 + 2 + 1 bytes."""
    return (
        integrator_fee.to_bytes(2, "big")
        + integrator_share.to_bytes(1, "big")
        + resolver_fee.to_bytes(2, "big")
        + whitelist_discount.to_bytes(1, "big")
    )


def encode_whitelist(addresses: list[str]) -> bytes:
    """_isWhitelistedGetterImpl: 1 byte size, (bytes10)[N]."""
    return len(addresses).to_bytes(1, "big") + b"".join(address_half(a) for a in addresses)


def fee_taker_getter_data(fees: bytes, whitelist: bytes, tail: bytes = b"") -> bytes:
    return fees + whitelist + tail


def fee_taker_post_data(
    integrator: str,
    protocol: str,
    custom_receiver: str | None,
    fees: bytes,
    whitelist: bytes,
    tail: bytes = b"",
) -> bytes:
    """FeeTaker._postInteraction extraData after the listener address."""
    flags = b"\x01" if custom_receiver else b"\x00"
    receiver = address_bytes(custom_receiver) if custom_receiver else b""
    return (
        flags
        + address_bytes(integrator)
        + address_bytes(protocol)
        + receiver
        + fees
        + whitelist
        + tail
    )


def fee_taker_fields(
    fee_taker: str,
    fees: bytes,
    whitelist: list[str],
    integrator: str,
    protocol: str,
    custom_receiver: str | None,
    getter_tail: bytes = b"",
    post_tail: bytes = b"",
) -> list[bytes]:
    """The eight extension fields of a FeeTakerExtension.build()."""
    getter = address_bytes(fee_taker) + fee_taker_getter_data(
        fees, encode_whitelist(whitelist), getter_tail
    )
    post = address_bytes(fee_taker) + fee_taker_post_data(
        integrator, protocol, custom_receiver, fees, encode_whitelist(whitelist), post_tail
    )
    return [b"", b"", getter, getter, b"", b"", b"", post]


def build_fee_taker_extension(
    fee_taker: str,
    fees: bytes,
    whitelist: list[str],
    integrator: str,
    protocol: str,
    custom_receiver: str | None,
) -> bytes:
    return build_extension(
        fee_taker_fields(fee_taker, fees, whitelist, integrator, protocol, custom_receiver)
    )


def bind_salt_reference(salt: int, extension: bytes) -> int:
    """LimitOrder.buildSalt: the base salt above the extension hash."""
    return ((salt << 160) & (2**256 - 1)) | (int.from_bytes(keccak(extension), "big") & MASK160)


def gross_up(linear: int, integrator_fee: int, resolver_fee: int) -> int:
    """AmountGetterWithFee._getTakingAmount on a linear amount (rounded up)."""
    return -(-linear * (BASE_1E5 + integrator_fee + resolver_fee) // BASE_1E5)


def fee_split(taking: int, integrator_fee: int, integrator_share: int, resolver_fee: int) -> tuple:
    """FeeTaker._getFeeAmounts: (integrator amount, protocol amount)."""
    denominator = BASE_1E5 + integrator_fee + resolver_fee
    integrator_total = taking * integrator_fee // denominator
    integrator_amount = integrator_total * integrator_share // BASE_1E2
    protocol_amount = taking * resolver_fee // denominator + integrator_total - integrator_amount
    return integrator_amount, protocol_amount


def extension_offsets(extension: bytes) -> int:
    return int.from_bytes(extension[:32], "big") if len(extension) >= 32 else 0


def as_tuple(value) -> tuple:
    return tuple(value.values()) if hasattr(value, "values") else tuple(value)


SDK_GETTER_DATA = fee_taker_getter_data(
    encode_fees(*SDK_FEES), encode_whitelist([SDK_RESOLVER_A, SDK_RESOLVER_B])
)
SDK_POST_DATA = fee_taker_post_data(
    SDK_INTEGRATOR,
    SDK_PROTOCOL,
    SDK_RECEIVER,
    encode_fees(*SDK_FEES),
    encode_whitelist([SDK_RESOLVER_A, SDK_RESOLVER_B]),
)


@pytest.fixture(scope="module")
def harness():
    return boa.loads(HARNESS_SOURCE, name="FeeTakerHarness", filename="FeeTakerHarness.vy")


# SDK vectors


def test_encoders_reproduce_the_sdk_extension():
    assert (
        build_fee_taker_extension(
            FEE_TAKER,
            encode_fees(*SDK_FEES),
            [SDK_RESOLVER_A, SDK_RESOLVER_B],
            SDK_INTEGRATOR,
            SDK_PROTOCOL,
            SDK_RECEIVER,
        )
        == SDK_EXTENSION
    )
    assert SDK_EXTENSION[32 + 0x5E : 32 + 0x5E] == b""  # noqa: PLR0133 (no suffixes)
    assert bind_salt_reference(SDK_SALT, SDK_EXTENSION) == SDK_ORDER_SALT
    assert SDK_ORDER_TRAITS == (
        ALLOW_MULTIPLE_FILLS_FLAG
        | HAS_EXTENSION_FLAG
        | POST_INTERACTION_CALL_FLAG
        | (SDK_EXPIRATION << EXPIRATION_OFFSET)
    )
    # The SDK order hashes under the router's domain like a plain order.
    domain = domain_separator_reference(ROUTER_NAME, ROUTER_VERSION, 1, ROUTER)
    order = (
        SDK_ORDER_SALT,
        SDK_MAKER,
        FEE_TAKER,
        SDK_MAKER_ASSET,
        SDK_TAKER_ASSET,
        SDK_MAKING,
        SDK_TAKING,
        SDK_ORDER_TRAITS,
    )
    assert order_digest_reference(order, domain) == SDK_ORDER_HASH
    # The SDK's own fee calculator agrees with the gross-up formula.
    assert gross_up(SDK_TAKING, 500, 1000 * 50 // 100) == SDK_TAKING_WHITELISTED
    assert gross_up(SDK_TAKING, 500, 1000) == SDK_TAKING_OUTSIDER


def test_sdk_extension_parses_field_by_field(harness):
    assert bytes(harness.amount_getter_data(SDK_EXTENSION, FEE_TAKER)) == SDK_GETTER_DATA
    assert bytes(harness.post_interaction_data(SDK_EXTENSION, FEE_TAKER)) == SDK_POST_DATA
    fees, pos = harness.parse_fees(SDK_GETTER_DATA, 0)
    assert as_tuple(fees) == SDK_FEES
    assert pos == FEE_DATA_LEN
    assert harness.parse_whitelist(SDK_GETTER_DATA, pos, 0, WHITELIST_ENTRY_LEN) == (2, 7, 27)
    assert len(SDK_GETTER_DATA) == 27
    receivers, pos = harness.parse_post_header(SDK_POST_DATA)
    assert as_tuple(receivers) == (SDK_INTEGRATOR, SDK_PROTOCOL, SDK_RECEIVER)
    assert pos == POST_HEADER_LEN + 20
    fees, size, getter_end, post_end = harness.check_fee_block(
        SDK_GETTER_DATA, 0, SDK_POST_DATA, pos, 0, WHITELIST_ENTRY_LEN
    )
    assert (as_tuple(fees), size, getter_end, post_end) == (SDK_FEES, 2, 27, len(SDK_POST_DATA))
    assert harness.bind_salt(SDK_SALT, SDK_EXTENSION) == SDK_ORDER_SALT


def test_constants(harness):
    assert harness.constants() == (2048, FEE_DATA_LEN, WHITELIST_ENTRY_LEN, POST_HEADER_LEN, 1)


# Extension field extraction (oneinch._field)


def test_field_follows_the_offsets_word(harness):
    fields = [b"a", b"", b"bc", b"", b"def", b"", b"", b"g"]
    extension = build_extension(fields, custom=b"zz")
    offsets = extension_offsets(extension)
    for index, field in enumerate(fields):
        assert bytes(harness.field(extension, offsets, index)) == field
    # Sub-word extensions have every field empty.
    assert bytes(harness.field(b"\xff" * 31, 0, 2)) == b""


def test_field_rejects_inconsistent_offsets(harness):
    extension = build_extension([b"a", b"bc", b"", b"", b"", b"", b"", b""])
    # An end below its start: the protocol reads a bogus length.
    inverted = (1 << 32) | 3
    with boa.reverts(custom_err("OrderNotValid(string)", "BadExtensionOffsets")):
        harness.field(extension, inverted, 1)
    # An end past the extension: the protocol reverts OffsetOutOfBounds.
    with boa.reverts(custom_err("OrderNotValid(string)", "BadExtensionOffsets")):
        harness.field(extension, 1 | (99 << 32), 1)
    # A short field opening with less than an address is not a target.
    short = build_extension([b"", b"", b"\x01\x02", b"", b"", b"", b"", b""])
    with boa.reverts(custom_err("OrderNotValid(string)", "BadAmountGetter")):
        harness.amount_getter_data(short, FEE_TAKER)


# Targets


def test_amount_getters_must_both_target_the_getter_with_equal_data(harness):
    fields = fee_taker_fields(
        FEE_TAKER, encode_fees(1, 2, 3, 4), [SDK_RESOLVER_A], SDK_INTEGRATOR, SDK_PROTOCOL, None
    )
    other = "0x1234567890123456789012345678901234567890"
    for broken in (
        dict(making=b""),
        dict(taking=b""),
        dict(making=address_bytes(other) + fields[2][20:]),
        dict(taking=address_bytes(other) + fields[3][20:]),
        dict(taking=fields[3] + b"\x00"),
        dict(making=fields[2][:-1]),
    ):
        mutated = list(fields)
        if "making" in broken:
            mutated[MAKING_AMOUNT_DATA] = broken["making"]
        if "taking" in broken:
            mutated[TAKING_AMOUNT_DATA] = broken["taking"]
        with boa.reverts(custom_err("OrderNotValid(string)", "BadAmountGetter")):
            harness.amount_getter_data(build_extension(mutated), FEE_TAKER)
    assert bytes(harness.amount_getter_data(build_extension(fields), FEE_TAKER)) == fields[2][20:]
    # The getter address alone (no data) is a valid, empty extraData.
    bare = list(fields)
    bare[MAKING_AMOUNT_DATA] = bare[TAKING_AMOUNT_DATA] = address_bytes(FEE_TAKER)
    assert bytes(harness.amount_getter_data(build_extension(bare), FEE_TAKER)) == b""


def test_post_interaction_must_target_the_listener(harness):
    fields = fee_taker_fields(
        FEE_TAKER, encode_fees(), [], SDK_INTEGRATOR, SDK_PROTOCOL, SDK_RECEIVER
    )
    assert (
        bytes(harness.post_interaction_data(build_extension(fields), FEE_TAKER)) == fields[7][20:]
    )
    for post in (b"", fields[7][:19], address_bytes(SDK_PROTOCOL) + fields[7][20:]):
        mutated = list(fields)
        mutated[POST_INTERACTION_DATA] = post
        with boa.reverts(custom_err("OrderNotValid(string)", "BadPostInteraction")):
            harness.post_interaction_data(build_extension(mutated), FEE_TAKER)


# Fee data and whitelist


def test_fee_data_bounds_mirror_the_getter(harness):
    fees, pos = harness.parse_fees(b"\x00" * 3 + encode_fees(65535, 100, 65535, 100), 3)
    assert (as_tuple(fees), pos) == ((65535, 100, 65535, 100), 9)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadFeeData")):
        harness.parse_fees(encode_fees(1, 101, 1, 1), 0)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadFeeData")):
        harness.parse_fees(encode_fees(1, 1, 1, 101), 0)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadFeeData")):
        harness.parse_fees(encode_fees()[:-1], 0)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadFeeData")):
        harness.parse_fees(encode_fees(), 1)


def test_whitelist_parsing_is_bounded_by_its_size_byte(harness):
    whitelist = encode_whitelist([SDK_RESOLVER_A, SDK_RESOLVER_B, SDK_RECEIVER])
    assert harness.parse_whitelist(whitelist, 0, 0, 10) == (3, 1, 31)
    # A prefix (the Fusion allowed time) and wider entries.
    assert harness.parse_whitelist(b"\x00" * 4 + b"\x02" + b"\x00" * 24, 0, 4, 12) == (2, 5, 29)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadWhitelist")):
        harness.parse_whitelist(whitelist[:-1], 0, 0, 10)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadWhitelist")):
        harness.parse_whitelist(b"", 0, 0, 10)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadWhitelist")):
        harness.parse_whitelist(b"\x00" * 4, 0, 4, 12)
    assert harness.parse_whitelist(b"\x00", 0, 0, 10) == (0, 1, 1)


def test_fee_block_requires_equal_fees_and_takers_on_both_sides(harness):
    fees = encode_fees(500, 40, 1000, 50)
    getter = fee_taker_getter_data(fees, encode_whitelist([SDK_RESOLVER_A, SDK_RESOLVER_B]))
    post = fee_taker_post_data(
        SDK_INTEGRATOR, SDK_PROTOCOL, None, fees, encode_whitelist([SDK_RESOLVER_A, SDK_RESOLVER_B])
    )
    parsed, size, getter_end, post_end = harness.check_fee_block(getter, 0, post, 41, 0, 10)
    assert (as_tuple(parsed), size, getter_end, post_end) == ((500, 40, 1000, 50), 2, 27, 68)
    # The post whitelist may be wider per entry (Fusion) as long as the
    # halves line up.
    wide = b"".join(address_half(a) + b"\x00\x01" for a in (SDK_RESOLVER_A, SDK_RESOLVER_B))
    fusion_post = post[:47] + b"\x00\x00\x00\x00" + b"\x02" + wide
    assert harness.check_fee_block(getter, 0, fusion_post, 41, 4, 12)[3] == len(fusion_post)
    cases = {
        "BadFeeData": fee_taker_post_data(
            SDK_INTEGRATOR,
            SDK_PROTOCOL,
            None,
            encode_fees(500, 40, 1001, 50),
            encode_whitelist([SDK_RESOLVER_A, SDK_RESOLVER_B]),
        ),
        "BadWhitelist": fee_taker_post_data(
            SDK_INTEGRATOR, SDK_PROTOCOL, None, fees, encode_whitelist([SDK_RESOLVER_A])
        ),
        "BadWhitelist ": fee_taker_post_data(
            SDK_INTEGRATOR,
            SDK_PROTOCOL,
            None,
            fees,
            encode_whitelist([SDK_RESOLVER_B, SDK_RESOLVER_A]),
        ),
        "BadWhitelist  ": fee_taker_post_data(
            SDK_INTEGRATOR,
            SDK_PROTOCOL,
            None,
            fees,
            encode_whitelist([SDK_RESOLVER_A, SDK_RESOLVER_B])[:-1],
        ),
    }
    for reason, broken in cases.items():
        with boa.reverts(custom_err("OrderNotValid(string)", reason.strip())):
            harness.check_fee_block(getter, 0, broken, 41, 0, 10)


# Post-interaction header


def test_post_header_reads_flags_and_receivers(harness):
    fees = encode_fees()
    with_custom = fee_taker_post_data(SDK_INTEGRATOR, SDK_PROTOCOL, SDK_RECEIVER, fees, b"\x00")
    receivers, pos = harness.parse_post_header(with_custom)
    assert (as_tuple(receivers), pos) == ((SDK_INTEGRATOR, SDK_PROTOCOL, SDK_RECEIVER), 61)
    without = fee_taker_post_data(SDK_INTEGRATOR, SDK_PROTOCOL, None, fees, b"\x00")
    receivers, pos = harness.parse_post_header(without)
    assert (as_tuple(receivers), pos) == (
        (SDK_INTEGRATOR, SDK_PROTOCOL, "0x0000000000000000000000000000000000000000"),
        41,
    )
    # Only the lowest flag bit selects the custom receiver.
    assert harness.parse_post_header(b"\xfe" + without[1:])[1] == 41
    assert harness.parse_post_header(b"\xff" + with_custom[1:])[1] == 61
    for short in (without[:40], with_custom[:60]):
        with boa.reverts(custom_err("OrderNotValid(string)", "BadPostInteraction")):
            harness.parse_post_header(short)
    assert harness.taking_receiver(as_tuple(receivers), SDK_MAKER) == SDK_MAKER
    assert (
        harness.taking_receiver((SDK_INTEGRATOR, SDK_PROTOCOL, SDK_RECEIVER), SDK_MAKER)
        == SDK_RECEIVER
    )


# Round trip fuzz


@given(
    integrator_fee=st.integers(min_value=0, max_value=65535),
    integrator_share=st.integers(min_value=0, max_value=100),
    resolver_fee=st.integers(min_value=0, max_value=65535),
    whitelist_discount=st.integers(min_value=0, max_value=100),
    halves=st.lists(st.binary(min_size=10, max_size=10), max_size=12),
    custom=st.booleans(),
)
@settings(max_examples=40, deadline=None)
def test_fee_block_round_trips(
    harness, integrator_fee, integrator_share, resolver_fee, whitelist_discount, halves, custom
):
    fees = encode_fees(integrator_fee, integrator_share, resolver_fee, whitelist_discount)
    whitelist = len(halves).to_bytes(1, "big") + b"".join(halves)
    getter = fees + whitelist
    receiver = SDK_RECEIVER if custom else None
    post = fee_taker_post_data(SDK_INTEGRATOR, SDK_PROTOCOL, receiver, fees, whitelist)
    receivers, pos = harness.parse_post_header(post)
    parsed, size, getter_end, post_end = harness.check_fee_block(getter, 0, post, pos, 0, 10)
    assert as_tuple(parsed) == (integrator_fee, integrator_share, resolver_fee, whitelist_discount)
    assert (size, getter_end, post_end) == (len(halves), len(getter), len(post))
    assert as_tuple(receivers)[2] == (receiver or "0x0000000000000000000000000000000000000000")
