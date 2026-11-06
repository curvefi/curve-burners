"""Standalone tests for the stateless delta module.

Covers the Velora Delta Order type, its EIP-712 struct hash under both
primary types (Order and FillableOrder), the Portikus domain, the digest
against an independent reference and against two real mainnet settlements
(the orderHash each one logged), the encoding round trip and bounds, the
partnerAndFee packing of FeeManagerLib and the floor netting the adapter
prices. The VeloraDeltaAdapter built on top (shape checks and typed reverts)
is tested in test_velora_delta_adapter.py; settlement through the real
protocol in test_velora_delta_fork.py.
"""

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak, to_checksum_address
from hypothesis import given, settings
from hypothesis import strategies as st

MAX_UINT256 = 2**256 - 1
MASK160 = 2**160 - 1
MAX_PERMIT = 256
MAX_METADATA = 256
MAX_PROTOCOL_DATA = 256

ORDER_FIELDS = (
    "(address owner,address beneficiary,address srcToken,address destToken,uint256 srcAmount,"
    "uint256 destAmount,uint256 expectedAmount,uint256 deadline,uint8 kind,uint256 nonce,"
    "uint256 partnerAndFee,bytes permit,bytes metadata,Bridge bridge)"
)
BRIDGE_TYPE = (
    "Bridge(bytes4 protocolSelector,uint256 destinationChainId,address outputToken,"
    "int8 scalingFactor,bytes protocolData)"
)
ORDER_TYPE = "Order" + ORDER_FIELDS + BRIDGE_TYPE
FILLABLE_ORDER_TYPE = "FillableOrder" + ORDER_FIELDS + BRIDGE_TYPE
DOMAIN_TYPE = "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
ORDER_TYPE_HASH = keccak(text=ORDER_TYPE)
FILLABLE_ORDER_TYPE_HASH = keccak(text=FILLABLE_ORDER_TYPE)
BRIDGE_TYPE_HASH = keccak(text=BRIDGE_TYPE)
DOMAIN_TYPE_HASH = keccak(text=DOMAIN_TYPE)
DOMAIN_NAME = "Portikus"
DOMAIN_VERSION = "2.0.0"
BRIDGE_ABI = "(bytes4,uint256,address,int8,bytes)"
ORDER_ABI = (
    "(address,address,address,address,uint256,uint256,uint256,uint256,uint8,uint256,uint256,"
    f"bytes,bytes,{BRIDGE_ABI})"
)
EMPTY_BRIDGE = (bytes(4), 0, "0x" + "00" * 20, 0, b"")
NATIVE_TOKEN = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

# The Delta contract (Portikus V2 adapter 0x0000000000bbf5c5fd284e657f01bd000933c96d,
# the same address on every supported chain): its DOMAIN_SEPARATOR on
# Ethereum was recorded by eth_call, and the two settlements below are real
# mainnet transactions whose OrderSettled event logged the orderHash the
# module must reproduce (SwapSettlementModule v2.3.0 swapSettle, block
# 26328579, a pre-signed order of a contract owner selling native ETH; and
# FillableSwapSettlementModule v2.3.0 swapSettleFillable, block 26328617,
# an EOA order filled at 100%).
SETTLEMENT = to_checksum_address("0x0000000000bbf5c5fd284e657f01bd000933c96d")
MAINNET_DOMAIN = bytes.fromhex("82ee5d4af512b33859c08c96ea6f73fe0a641013fbf1174cf13a1a2b6c9ead75")
SETTLED_ORDER = (
    to_checksum_address("0x281354062fa932086890a6bbf7dff7d0a67d220f"),
    to_checksum_address("0x281354062fa932086890a6bbf7dff7d0a67d220f"),
    NATIVE_TOKEN,
    to_checksum_address("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"),
    1_800_000_000_000_000_000,
    4_044_511_139,
    4_064_835_316,
    1_793_640_965,
    0,
    30535681830540056799497669840301612503360249199846626468829117349506008719703,
    0x3610486BD4975F5C3DC838A36E897BF97FAE15DD000000000000000000000102,
    b"",
    b"",
    EMPTY_BRIDGE,
)
SETTLED_ORDER_HASH = bytes.fromhex(
    "eea09d3b1c3882730c308f23d1e5691a7ef25592cdef63b2d93a9f8bf480f800"
)
FILLED_ORDER = (
    to_checksum_address("0x843b7c9cacf6a55ca3e6267b6e99dd7604013f2a"),
    to_checksum_address("0x843b7c9cacf6a55ca3e6267b6e99dd7604013f2a"),
    to_checksum_address("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"),
    to_checksum_address("0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"),
    5_000_000_000,
    6_493_506,
    6_493_506,
    1_795_999_534,
    0,
    52637427317502455851492853686304945851628131361225109438889870689005664063139,
    0x3610486BD4975F5C3DC838A36E897BF97FAE15DD000000000000000000000103,
    b"",
    b"",
    EMPTY_BRIDGE,
)
FILLED_ORDER_HASH = bytes.fromhex(
    "2fc9146c0b37f383cb9bde63d010ea163454d77ca0528ea80fe9c44918b7503d"
)
PARTNER = to_checksum_address("0x3610486bd4975f5c3dc838a36e897bf97fae15dd")

HARNESS_SOURCE = """
# pragma version 0.5.0b1

import contracts.burners.adapters.velora.delta as delta


@external
@pure
def domain_separator(_chain_id: uint256, _verifying_contract: address) -> bytes32:
    return delta._domain_separator(_chain_id, _verifying_contract)


@external
@pure
def bridge_hash(_bridge: delta.Bridge) -> bytes32:
    return delta._bridge_hash(_bridge)


@external
@pure
def struct_hash(_order: delta.Order, _fillable: bool) -> bytes32:
    if _fillable:
        return delta._struct_hash(_order, delta.FILLABLE_ORDER_TYPE_HASH)
    return delta._struct_hash(_order, delta.ORDER_TYPE_HASH)


@external
@pure
def order_digest(_order: delta.Order, _domain_separator: bytes32, _fillable: bool) -> bytes32:
    return delta._order_digest(_order, _domain_separator, _fillable)


@external
@pure
def encode(_order: delta.Order) -> Bytes[delta.ENCODED_ORDER_BOUND]:
    return abi_encode(_order)


@external
@pure
def decode(_encoded: Bytes[delta.ENCODED_ORDER_BOUND]) -> delta.Order:
    return abi_decode(_encoded, delta.Order)


@external
@pure
def parse_partner_and_fee(_packed: uint256) -> (address, uint256, bool, bool):
    return delta._parse_partner_and_fee(_packed)


@external
@pure
def net_floor(_order: delta.Order) -> uint256:
    return delta._net_floor(_order)


@external
@pure
def constants() -> (bytes32, bytes32, bytes32, bytes32, uint256, uint256, uint256, uint256):
    return (
        delta.ORDER_TYPE_HASH,
        delta.FILLABLE_ORDER_TYPE_HASH,
        delta.BRIDGE_TYPE_HASH,
        delta.EIP712_DOMAIN_TYPE_HASH,
        delta.ENCODED_ORDER_BOUND,
        delta.MAX_PERMIT,
        delta.MAX_METADATA,
        delta.MAX_PROTOCOL_DATA,
    )
"""


def domain_separator_reference(chain_id: int, contract: str) -> bytes:
    """EIP712._domainSeparatorV4 from eth_abi/keccak."""
    return keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                DOMAIN_TYPE_HASH,
                keccak(text=DOMAIN_NAME),
                keccak(text=DOMAIN_VERSION),
                chain_id,
                contract,
            ],
        )
    )


def bridge_hash_reference(bridge: tuple) -> bytes:
    selector, chain_id, output_token, scaling, data = bridge
    return keccak(
        encode(
            ["bytes32", "bytes4", "uint256", "address", "int8", "bytes32"],
            [BRIDGE_TYPE_HASH, selector, chain_id, output_token, scaling, keccak(data)],
        )
    )


def struct_hash_reference(order: tuple, fillable: bool = False) -> bytes:
    """Independent OrderHashLib.hash / FillableOrderHashLib.hash model."""
    *scalars, permit, metadata, bridge = order
    return keccak(
        encode(
            ["bytes32"]
            + ["address"] * 4
            + ["uint256"] * 4
            + ["uint8", "uint256", "uint256", "bytes32", "bytes32", "bytes32"],
            [
                FILLABLE_ORDER_TYPE_HASH if fillable else ORDER_TYPE_HASH,
                *scalars,
                keccak(permit),
                keccak(metadata),
                bridge_hash_reference(bridge),
            ],
        )
    )


def order_digest_reference(order: tuple, domain_separator: bytes, fillable: bool = False) -> bytes:
    return keccak(b"\x19\x01" + domain_separator + struct_hash_reference(order, fillable))


def pack_partner_and_fee(
    partner: str, fee_bps: int, takes_surplus: bool = False, cap_surplus: bool = False
) -> int:
    return (int(partner, 16) << 96) | (int(cap_surplus) << 9) | (int(takes_surplus) << 8) | fee_bps


def net_floor_reference(order: tuple) -> int:
    partner = (order[10] >> 96) & MASK160
    fee_bps = min(order[10] & 0xFF, 200)
    if partner == 0:
        return order[5]
    return order[5] - order[5] * fee_bps // 10_000


@pytest.fixture(scope="module")
def harness():
    return boa.loads(HARNESS_SOURCE, name="DeltaHarness")


def _normalize(order: tuple) -> tuple:
    """The order as the harness returns it (bytes fields as bytes)."""
    order = tuple(order)
    bridge = tuple(order[13])
    return (
        *order[:11],
        bytes(order[11]),
        bytes(order[12]),
        (bytes(bridge[0]), bridge[1], bridge[2], bridge[3], bytes(bridge[4])),
    )


# Constants and hashing


def test_constants_match_protocol(harness):
    (
        order_type_hash,
        fillable_type_hash,
        bridge_type_hash,
        domain_type_hash,
        bound,
        max_permit,
        max_metadata,
        max_protocol_data,
    ) = harness.constants()
    assert bytes(order_type_hash) == ORDER_TYPE_HASH
    assert (
        bytes(order_type_hash).hex()
        == "c75d848e51cd0f81113e24c5a62c9b8566b0ff0d476245a7882709315eefbbf7"
    )
    assert bytes(fillable_type_hash) == FILLABLE_ORDER_TYPE_HASH
    assert (
        bytes(fillable_type_hash).hex()
        == "e9c0719dc3d5cb1a78318dfcf50c0b5d86013bc715805be49f4154bb98b994c8"
    )
    assert bytes(bridge_type_hash) == BRIDGE_TYPE_HASH
    assert (
        bytes(bridge_type_hash).hex()
        == "8d8bcbf96f8246b1dcca3f4e1750aecf6cd6971d370b908f10af1d459a2df6eb"
    )
    assert bytes(domain_type_hash) == DOMAIN_TYPE_HASH
    assert (max_permit, max_metadata, max_protocol_data) == (
        MAX_PERMIT,
        MAX_METADATA,
        MAX_PROTOCOL_DATA,
    )
    assert bound == 32 + 14 * 32 + (32 + MAX_PERMIT) + (32 + MAX_METADATA) + (
        5 * 32 + 32 + MAX_PROTOCOL_DATA
    )
    # The bound is exactly the encoder's output at the field bounds.
    full = (
        *SETTLED_ORDER[:11],
        b"\x01" * MAX_PERMIT,
        b"\x02" * MAX_METADATA,
        (b"\xaa\xbb\xcc\xdd", 137, PARTNER, -3, b"\x03" * MAX_PROTOCOL_DATA),
    )
    assert len(encode([ORDER_ABI], [full])) == bound


def test_mainnet_domain_separator(harness):
    assert bytes(harness.domain_separator(1, SETTLEMENT)) == MAINNET_DOMAIN
    assert domain_separator_reference(1, SETTLEMENT) == MAINNET_DOMAIN
    assert bytes(harness.domain_separator(137, SETTLEMENT)) != MAINNET_DOMAIN


@pytest.mark.parametrize(
    "order, fillable, logged",
    [(SETTLED_ORDER, False, SETTLED_ORDER_HASH), (FILLED_ORDER, True, FILLED_ORDER_HASH)],
)
def test_mainnet_settlements_reproduce_logged_order_hash(harness, order, fillable, logged):
    assert order_digest_reference(order, MAINNET_DOMAIN, fillable) == logged
    assert bytes(harness.order_digest(order, MAINNET_DOMAIN, fillable)) == logged
    # The other primary type is a different digest of the same fields.
    assert bytes(harness.order_digest(order, MAINNET_DOMAIN, not fillable)) != logged


def test_struct_hash_and_bridge_hash_match_reference(harness):
    bridge = (b"\x12\x34\x56\x78", 42161, PARTNER, -1, b"route")
    order = (*FILLED_ORDER[:11], b"\x01" * 96, b"meta", bridge)
    assert bytes(harness.bridge_hash(bridge)) == bridge_hash_reference(bridge)
    for fillable in (False, True):
        assert bytes(harness.struct_hash(order, fillable)) == struct_hash_reference(order, fillable)


# Encoding round trip


@pytest.mark.parametrize("order", [SETTLED_ORDER, FILLED_ORDER])
def test_encode_decode_round_trip(harness, order):
    encoded = bytes(harness.encode(order))
    assert encoded == encode([ORDER_ABI], [order])
    assert _normalize(harness.decode(encoded)) == order


def test_decode_rejects_fields_above_the_bounds(harness):
    for oversized in (
        (*FILLED_ORDER[:11], b"\x01" * (MAX_PERMIT + 1), b"", EMPTY_BRIDGE),
        (*FILLED_ORDER[:11], b"", b"\x01" * (MAX_METADATA + 1), EMPTY_BRIDGE),
        (
            *FILLED_ORDER[:11],
            b"",
            b"",
            (bytes(4), 0, "0x" + "00" * 20, 0, b"\x01" * (MAX_PROTOCOL_DATA + 1)),
        ),
    ):
        with boa.reverts():
            harness.decode(encode([ORDER_ABI], [oversized]))


# partnerAndFee


@pytest.mark.parametrize(
    "packed, expected",
    [
        (0, ("0x" + "00" * 20, 0, False, False)),
        (SETTLED_ORDER[10], (PARTNER, 2, True, False)),
        (FILLED_ORDER[10], (PARTNER, 3, True, False)),
        (pack_partner_and_fee(PARTNER, 200, False, True), (PARTNER, 200, False, True)),
        # The fee is clamped to 2% like FeeManagerLib does.
        (pack_partner_and_fee(PARTNER, 255), (PARTNER, 200, False, False)),
        # Bits between the flags and the partner are ignored.
        ((1 << 95) | pack_partner_and_fee(PARTNER, 7), (PARTNER, 7, False, False)),
    ],
)
def test_parse_partner_and_fee(harness, packed, expected):
    assert tuple(harness.parse_partner_and_fee(packed)) == expected


@pytest.mark.parametrize(
    "packed, dest_amount, expected",
    [
        (0, 10**18, 10**18),
        # A partner without a fee takes nothing off the top.
        (pack_partner_and_fee(PARTNER, 0, True, True), 10**18, 10**18),
        (pack_partner_and_fee(PARTNER, 2), 10**18, 10**18 - 2 * 10**14),
        (pack_partner_and_fee(PARTNER, 200), 10**18, 98 * 10**16),
        (pack_partner_and_fee(PARTNER, 255), 10**18, 98 * 10**16),
        # Fee bits without a partner address are ignored like the settlement does.
        (200, 10**18, 10**18),
        (pack_partner_and_fee(PARTNER, 200), 1, 1),
    ],
)
def test_net_floor(harness, packed, dest_amount, expected):
    order = (
        *FILLED_ORDER[:5],
        dest_amount,
        dest_amount,
        *FILLED_ORDER[7:10],
        packed,
        *FILLED_ORDER[11:],
    )
    assert harness.net_floor(order) == expected == net_floor_reference(order)


# Properties


address_st = st.integers(0, MASK160).map(lambda v: to_checksum_address(v.to_bytes(20, "big")))
uint_st = st.integers(0, MAX_UINT256)
bridge_st = st.tuples(
    st.binary(min_size=4, max_size=4),
    uint_st,
    address_st,
    st.integers(-128, 127),
    st.binary(max_size=MAX_PROTOCOL_DATA),
)
order_st = st.tuples(
    address_st,
    address_st,
    address_st,
    address_st,
    uint_st,
    uint_st,
    uint_st,
    uint_st,
    st.integers(0, 255),
    uint_st,
    uint_st,
    st.binary(max_size=MAX_PERMIT),
    st.binary(max_size=MAX_METADATA),
    bridge_st,
)


@given(order=order_st, domain=st.binary(min_size=32, max_size=32), fillable=st.booleans())
@settings(max_examples=60, deadline=None)
def test_digest_matches_reference(harness, order, domain, fillable):
    assert bytes(harness.order_digest(order, domain, fillable)) == order_digest_reference(
        order, domain, fillable
    )
    assert _normalize(harness.decode(bytes(harness.encode(order)))) == order


# Amounts above 2**248 overflow `amount * bps` in the settlement's checked
# arithmetic and in the module alike; token amounts never get there.
@given(packed=uint_st, dest_amount=st.integers(0, 2**248 // 200))
@settings(max_examples=60, deadline=None)
def test_net_floor_matches_reference(harness, packed, dest_amount):
    order = (
        *FILLED_ORDER[:5],
        dest_amount,
        dest_amount,
        *FILLED_ORDER[7:10],
        packed,
        *FILLED_ORDER[11:],
    )
    floor = harness.net_floor(order)
    assert floor == net_floor_reference(order)
    assert floor <= dest_amount
    assert floor >= dest_amount - dest_amount * 200 // 10_000
