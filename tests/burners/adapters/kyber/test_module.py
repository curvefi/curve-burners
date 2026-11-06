"""Standalone tests for the stateless dslo module.

Covers the KyberSwap DSLO Order type, its EIP-712 digest and domain against
an independent reference and a vector decoded from a real mainnet fill of
DSLOProtocol, the feeConfig layout, the pinned linear amount getters, and the
predicate shapes the module reads an expiry from. The KyberDsloAdapter built
on top (shape checks and typed reverts) is tested in test_kyber_adapter.py;
settlement through the real protocol in test_kyber_fork.py.
"""

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak, to_checksum_address
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.burners.conftest import custom_err

MAX_UINT256 = 2**256 - 1
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

ORDER_TYPE = (
    "Order(uint256 salt,address makerAsset,address takerAsset,address maker,address receiver,"
    "address allowedSender,uint256 makingAmount,uint256 takingAmount,uint256 feeConfig,"
    "bytes makerAssetData,bytes takerAssetData,bytes getMakerAmount,bytes getTakerAmount,"
    "bytes predicate,bytes interaction)"
)
DOMAIN_TYPE = "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
ORDER_TYPE_HASH = keccak(text=ORDER_TYPE)
DOMAIN_TYPE_HASH = keccak(text=DOMAIN_TYPE)
DOMAIN_NAME = "Kyber DSLO Protocol"
DOMAIN_VERSION = "1"
ORDER_ABI = (
    "(uint256,address,address,address,address,address,uint256,uint256,uint256,"
    "bytes,bytes,bytes,bytes,bytes,bytes)"
)
STATIC_FIELD_TYPES = ["uint256", "address", "address", "address", "address", "address"] + [
    "uint256"
] * 3

# Selectors of the protocol's own helpers the order bytes call.
GET_MAKER_AMOUNT = keccak(text="getMakerAmount(uint256,uint256,uint256)")[:4]
GET_TAKER_AMOUNT = keccak(text="getTakerAmount(uint256,uint256,uint256)")[:4]
AND = keccak(text="and(address[],bytes[])")[:4]
OR = keccak(text="or(address[],bytes[])")[:4]
NONCE_EQUALS = keccak(text="nonceEquals(address,uint256)")[:4]
TIMESTAMP_BELOW = keccak(text="timestampBelow(uint256)")[:4]

# Field bounds, as the module pins them.
MAX_ASSET_DATA = 32
GETTER_LEN = 68
MAX_PREDICATES = 4
MAX_PREDICATE_CALL = 68
MAX_PREDICATE = 4 + 32 * (2 + 2 + 2 * MAX_PREDICATES + 4 * MAX_PREDICATES)
MAX_INTERACTION = 64

# Deployed DSLOProtocol (0xcab2FA2eeab7065B45CBcF6E3936dDE2506b4f6C, the same
# verified source on Ethereum and Arbitrum): EIP712('Kyber DSLO Protocol',
# '1'). The Ethereum separator below is the contract's DOMAIN_SEPARATOR(),
# and the order is the one filled by tx
# 0xaa42cc0209f1316f6f7f84d245128d6c8c1493eb5161a70e810fe090212fc523
# (Ethereum block 26320034, fillOrderTo calldata decoded), whose maker
# signature recovers to order.maker over the digest below.
PROTOCOL = to_checksum_address("0xcab2fa2eeab7065b45cbcf6e3936dde2506b4f6c")
PROTOCOL_DOMAIN_SEPARATOR = bytes.fromhex(
    "6be3ed8321cd445839be95cf41496dbcf9e0521c8d780de0e2f49307215eca61"
)
MAINNET_MAKER = to_checksum_address("0xf8bd4b73d00f485197c33368f4c528f9b695791f")
MAINNET_MAKING_AMOUNT = 35500471187580311584475
MAINNET_TAKING_AMOUNT = 134901791
MAINNET_FEE_CONFIG = 6277102173934605430740620579158774882848240723792603459329
MAINNET_EXPIRY = 0x6B0EC233
MAINNET_ORDER_DIGEST = bytes.fromhex(
    "e88cb1512e23b9b99ff411958d4a8edc3454762aa3b9a873c984de86f419327d"
)

HARNESS_SOURCE = """
# pragma version 0.5.0b1

import contracts.burners.adapters.kyber.dslo as dslo


@external
@pure
def decode(_encoded: Bytes[dslo.ENCODED_ORDER_BOUND]) -> dslo.Order:
    return abi_decode(_encoded, dslo.Order)


@external
@pure
def encode(_order: dslo.Order) -> Bytes[dslo.ENCODED_ORDER_BOUND]:
    return abi_encode(_order)


@external
@pure
def domain_separator(_chain_id: uint256, _verifying_contract: address) -> bytes32:
    return dslo._domain_separator(_chain_id, _verifying_contract)


@external
@pure
def order_digest(_order: dslo.Order, _domain_separator: bytes32) -> bytes32:
    return dslo._order_digest(_order, _domain_separator)


@external
@pure
def receiver(_order: dslo.Order) -> address:
    return dslo._receiver(_order)


@external
@pure
def fee_config(_fee_config: uint256) -> (address, uint256, bool):
    return dslo._fee_config(_fee_config)


@external
@pure
def linear_getter(_selector: bytes4, _order: dslo.Order) -> Bytes[dslo.GETTER_LEN]:
    return dslo._linear_getter(_selector, _order)


@external
@pure
def check_getters(_order: dslo.Order):
    dslo._check_getters(_order)


@external
@pure
def timestamp_below(_time: uint256) -> Bytes[dslo.TIMESTAMP_BELOW_LEN]:
    return dslo._timestamp_below(_time)


@external
@pure
def nonce_equals(_maker: address, _nonce: uint256) -> Bytes[dslo.MAX_PREDICATE_CALL]:
    return dslo._nonce_equals(_maker, _nonce)


@external
@pure
def valid_to(_order: dslo.Order, _protocol: address) -> uint256:
    return dslo._valid_to(_order, _protocol)


@external
@pure
def constants() -> (bytes32, bytes32, uint256, uint256, uint256):
    return (
        dslo.ORDER_TYPE_HASH,
        dslo.EIP712_DOMAIN_TYPE_HASH,
        dslo.ENCODED_ORDER_BOUND,
        dslo.MAX_PREDICATE,
        dslo.FEE_UNITS,
    )
"""


def domain_separator_reference(chain_id: int, contract: str) -> bytes:
    """OpenZeppelin EIP712._buildDomainSeparator from eth_abi/keccak."""
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


def order_digest_reference(order: tuple, domain_separator: bytes) -> bytes:
    """Independent DSOrderMixin.hashOrder model built from eth_abi/keccak
    primitives: the static fields in place, every bytes field by its hash."""
    struct_hash = keccak(
        encode(
            ["bytes32", *STATIC_FIELD_TYPES, *["bytes32"] * 6],
            [ORDER_TYPE_HASH, *order[:9], *[keccak(field) for field in order[9:]]],
        )
    )
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def linear_getters(making_amount: int, taking_amount: int) -> tuple[bytes, bytes]:
    """AmountCalculator calls over the order amounts, as the KyberSwap API
    builds them; the protocol appends the swap amount at fill time."""
    args = encode(["uint256", "uint256"], [making_amount, taking_amount])
    return GET_MAKER_AMOUNT + args, GET_TAKER_AMOUNT + args


def timestamp_below(time: int) -> bytes:
    return TIMESTAMP_BELOW + encode(["uint256"], [time])


def nonce_equals(maker: str, nonce: int) -> bytes:
    return NONCE_EQUALS + encode(["address", "uint256"], [maker, nonce])


def and_predicate(targets: list[str], data: list[bytes], selector: bytes = AND) -> bytes:
    """PredicateHelper.and(address[] targets, bytes[] data) calldata."""
    return selector + encode(["address[]", "bytes[]"], [targets, data])


def api_predicate(protocol: str, maker: str, nonce: int, expiry: int) -> bytes:
    """The predicate the KyberSwap API signs into every order."""
    return and_predicate(
        [protocol, protocol], [nonce_equals(maker, nonce), timestamp_below(expiry)]
    )


def pack_fee_config(recipient: str, percent: int, taker_asset_fee: bool) -> int:
    return int(recipient, 16) | (percent << 160) | (int(taker_asset_fee) << 192)


def address_from_int(value: int) -> str:
    return to_checksum_address(value.to_bytes(20, "big"))


def mainnet_order() -> tuple:
    getters = linear_getters(MAINNET_MAKING_AMOUNT, MAINNET_TAKING_AMOUNT)
    return (
        161838989330076096099035857253140818836,
        to_checksum_address("0x6fb3e0a217407efff7ca062d46c26e5d60a14d69"),
        to_checksum_address("0xdac17f958d2ee523a2206206994597c13d831ec7"),
        MAINNET_MAKER,
        MAINNET_MAKER,
        ZERO_ADDRESS,
        MAINNET_MAKING_AMOUNT,
        MAINNET_TAKING_AMOUNT,
        MAINNET_FEE_CONFIG,
        b"",
        b"",
        *getters,
        api_predicate(PROTOCOL, MAINNET_MAKER, 0, MAINNET_EXPIRY),
        b"",
    )


@pytest.fixture(scope="module")
def harness():
    return boa.loads(HARNESS_SOURCE, name="DsloHarness")


@pytest.fixture(scope="module")
def protocol():
    return boa.env.generate_address("protocol")


@pytest.fixture(scope="module")
def maker():
    return boa.env.generate_address("maker")


@pytest.fixture
def order(protocol, maker):
    making, taking = 250 * 10**18, 100_000 * 10**18
    return (
        0xABCDEF,
        boa.env.generate_address("maker_asset"),
        boa.env.generate_address("taker_asset"),
        maker,
        boa.env.generate_address("receiver"),
        ZERO_ADDRESS,
        making,
        taking,
        0,
        b"",
        b"",
        *linear_getters(making, taking),
        timestamp_below(1_800_000_000),
        b"",
    )


def with_fields(order: tuple, **fields) -> tuple:
    names = (
        "salt makerAsset takerAsset maker receiver allowedSender makingAmount takingAmount "
        "feeConfig makerAssetData takerAssetData getMakerAmount getTakerAmount predicate "
        "interaction"
    ).split()
    return tuple(fields.get(name, value) for name, value in zip(names, order))


# Constants and encoding


def test_constants_match_protocol(harness):
    order_type_hash, domain_type_hash, encoded_bound, max_predicate, fee_units = harness.constants()
    assert bytes(order_type_hash) == ORDER_TYPE_HASH
    assert (
        ORDER_TYPE_HASH.hex() == "346e0660be2aef32794b25da30f6733303978cb6c10c395480eb5d2c56bcd956"
    )
    assert bytes(domain_type_hash) == DOMAIN_TYPE_HASH
    assert max_predicate == MAX_PREDICATE
    assert fee_units == 100_000
    # The bound is exactly the encoding of an order with every field at its
    # bound (a longer one fails to decode).
    widest = with_fields(
        mainnet_order(),
        makerAssetData=b"\x01" * MAX_ASSET_DATA,
        takerAssetData=b"\x01" * MAX_ASSET_DATA,
        predicate=b"\x01" * MAX_PREDICATE,
        interaction=b"\x01" * MAX_INTERACTION,
    )
    assert encoded_bound == len(encode([ORDER_ABI], [widest]))


def test_order_round_trips_through_abi(harness):
    order = mainnet_order()
    encoded = encode([ORDER_ABI], [order])
    assert tuple(harness.decode(encoded)) == order
    assert bytes(harness.encode(order)) == encoded


def test_order_above_bounds_fails_to_decode(harness):
    for field in ("makerAssetData", "takerAssetData", "interaction", "predicate"):
        bound = {
            "makerAssetData": MAX_ASSET_DATA,
            "takerAssetData": MAX_ASSET_DATA,
            "interaction": MAX_INTERACTION,
            "predicate": MAX_PREDICATE,
        }[field]
        too_long = with_fields(mainnet_order(), **{field: b"\x01" * (bound + 1)})
        with boa.reverts():
            harness.decode(encode([ORDER_ABI], [too_long]))
    # A getter is at most the linear call.
    too_long = with_fields(mainnet_order(), getMakerAmount=b"\x01" * (GETTER_LEN + 1))
    with boa.reverts():
        harness.decode(encode([ORDER_ABI], [too_long]))


# Digest and domain


def test_domain_separator_matches_reference_and_mainnet(harness):
    assert bytes(harness.domain_separator(1, PROTOCOL)) == PROTOCOL_DOMAIN_SEPARATOR
    assert domain_separator_reference(1, PROTOCOL) == PROTOCOL_DOMAIN_SEPARATOR
    other = harness.domain_separator(137, PROTOCOL)
    assert bytes(other) == domain_separator_reference(137, PROTOCOL)
    assert bytes(other) != PROTOCOL_DOMAIN_SEPARATOR


def test_mainnet_fill_digest(harness):
    order = mainnet_order()
    assert order_digest_reference(order, PROTOCOL_DOMAIN_SEPARATOR) == MAINNET_ORDER_DIGEST
    assert bytes(harness.order_digest(order, PROTOCOL_DOMAIN_SEPARATOR)) == MAINNET_ORDER_DIGEST


def test_every_field_changes_the_digest(harness, order):
    domain = domain_separator_reference(boa.env.evm.patch.chain_id, PROTOCOL)
    base = bytes(harness.order_digest(order, domain))
    assert base == order_digest_reference(order, domain)
    variants = {
        "salt": order[0] + 1,
        "makerAsset": boa.env.generate_address("other"),
        "takerAsset": boa.env.generate_address("other"),
        "maker": boa.env.generate_address("other"),
        "receiver": ZERO_ADDRESS,
        "allowedSender": boa.env.generate_address("other"),
        "makingAmount": order[6] + 1,
        "takingAmount": order[7] + 1,
        "feeConfig": 1,
        "makerAssetData": b"\x00",
        "takerAssetData": b"\x00",
        "getMakerAmount": b"",
        "getTakerAmount": b"",
        "predicate": timestamp_below(1),
        "interaction": b"\x00" * 20,
    }
    for field, value in variants.items():
        variant = with_fields(order, **{field: value})
        digest = bytes(harness.order_digest(variant, domain))
        assert digest != base, field
        assert digest == order_digest_reference(variant, domain), field


@settings(max_examples=50, deadline=None)
@given(
    salt=st.integers(min_value=0, max_value=MAX_UINT256),
    addresses=st.lists(st.integers(min_value=0, max_value=2**160 - 1), min_size=5, max_size=5),
    amounts=st.lists(st.integers(min_value=0, max_value=MAX_UINT256), min_size=3, max_size=3),
    blobs=st.lists(st.binary(min_size=0, max_size=32), min_size=6, max_size=6),
    domain=st.binary(min_size=32, max_size=32),
)
def test_digest_matches_reference_for_random_orders(
    harness, salt, addresses, amounts, blobs, domain
):
    order = (salt, *[address_from_int(a) for a in addresses], *amounts, *blobs)
    assert bytes(harness.order_digest(order, domain)) == order_digest_reference(order, domain)


# Order fields


def test_receiver_resolves_like_the_protocol(harness, order, maker):
    assert harness.receiver(order) == order[4]
    assert harness.receiver(with_fields(order, receiver=ZERO_ADDRESS)) == maker


def test_fee_config_unpacks_mainnet_vector(harness):
    recipient, percent, taker_asset_fee = harness.fee_config(MAINNET_FEE_CONFIG)
    assert recipient == to_checksum_address("0x1111115af63bfbcc151e5753e2ea1a29c79d2f01")
    assert percent == 300
    assert taker_asset_fee
    assert harness.fee_config(0) == (ZERO_ADDRESS, 0, False)
    recipient = boa.env.generate_address("fee_recipient")
    assert harness.fee_config(pack_fee_config(recipient, 10, False)) == (recipient, 10, False)
    # Any nonzero high bits mean a taker-asset fee, as in the protocol.
    assert harness.fee_config(pack_fee_config(recipient, 10, False) | (1 << 255))[2]


# Amount getters


def test_linear_getters_match_the_api(harness, order):
    maker_getter, taker_getter = linear_getters(order[6], order[7])
    assert bytes(harness.linear_getter(GET_MAKER_AMOUNT, order)) == maker_getter
    assert bytes(harness.linear_getter(GET_TAKER_AMOUNT, order)) == taker_getter
    mainnet = mainnet_order()
    assert bytes(harness.linear_getter(GET_MAKER_AMOUNT, mainnet)) == mainnet[11]
    assert bytes(harness.linear_getter(GET_TAKER_AMOUNT, mainnet)) == mainnet[12]


def test_check_getters_accepts_linear_or_empty(harness, order):
    harness.check_getters(order)
    harness.check_getters(with_fields(order, getMakerAmount=b""))
    harness.check_getters(with_fields(order, getTakerAmount=b""))
    harness.check_getters(with_fields(order, getMakerAmount=b"", getTakerAmount=b""))


@pytest.mark.parametrize("field", ["getMakerAmount", "getTakerAmount"])
def test_check_getters_rejects_everything_else(harness, order, field):
    making, taking = order[6], order[7]
    maker_getter, taker_getter = linear_getters(making, taking)
    own, other = (
        (maker_getter, taker_getter) if field == "getMakerAmount" else (taker_getter, maker_getter)
    )
    selector = GET_MAKER_AMOUNT if field == "getMakerAmount" else GET_TAKER_AMOUNT
    bad = [
        other,  # the sibling selector
        selector + encode(["uint256", "uint256"], [making, taking - 1]),  # cheaper ratio
        selector + encode(["uint256", "uint256"], [taking, making]),  # swapped
        own[:-1],  # truncated
        own[:4],  # selector only
        bytes.fromhex("12345678") + own[4:],  # another function
    ]
    for getter in bad:
        with boa.reverts(custom_err("OrderNotValid(string)", "BadAmountGetter")):
            harness.check_getters(with_fields(order, **{field: getter}))


# Predicates


def test_predicate_builders(harness, maker):
    assert bytes(harness.timestamp_below(MAINNET_EXPIRY)) == timestamp_below(MAINNET_EXPIRY)
    assert bytes(harness.nonce_equals(maker, 7)) == nonce_equals(maker, 7)
    assert TIMESTAMP_BELOW.hex() == "63592c2b"
    assert NONCE_EQUALS.hex() == "cf6fc6e3"
    assert AND.hex() == "961d5b1e"
    assert GET_MAKER_AMOUNT.hex() == "f4a215c3"
    assert GET_TAKER_AMOUNT.hex() == "296637bf"


def test_valid_to_from_bare_timestamp_below(harness, order, protocol):
    # timestampBelow(t) is strict: the last fillable second is t - 1.
    assert harness.valid_to(order, protocol) == 1_800_000_000 - 1


def test_valid_to_from_api_predicate(harness, order, protocol, maker):
    assert (
        harness.valid_to(
            with_fields(order, predicate=api_predicate(protocol, maker, 0, 123)), protocol
        )
        == 122
    )
    # Any nonce, any entry order, up to the bound of entries.
    shapes = [
        api_predicate(protocol, maker, 2**255, 123),
        and_predicate([protocol, protocol], [timestamp_below(123), nonce_equals(maker, 5)]),
        and_predicate([protocol], [timestamp_below(123)]),
        and_predicate(
            [protocol] * 4,
            [
                nonce_equals(maker, 0),
                nonce_equals(maker, 1),
                timestamp_below(123),
                nonce_equals(maker, 2),
            ],
        ),
    ]
    for predicate in shapes:
        assert harness.valid_to(with_fields(order, predicate=predicate), protocol) == 122
    mainnet = mainnet_order()
    assert harness.valid_to(mainnet, PROTOCOL) == MAINNET_EXPIRY - 1


def test_valid_to_rejects_other_shapes(harness, order, protocol, maker):
    other = boa.env.generate_address("other")
    dirty_maker = NONCE_EQUALS + (1 << 200).to_bytes(32, "big") + encode(["uint256"], [0])
    assert int(dirty_maker[4:36].hex(), 16) & (2**160 - 1) == 0
    bad = {
        "empty": b"",
        "selector only": TIMESTAMP_BELOW,
        "short timestampBelow": timestamp_below(123)[:-1],
        "long timestampBelow": timestamp_below(123) + b"\x00",
        "nonceEquals alone": nonce_equals(maker, 0),
        "unknown selector": bytes.fromhex("12345678") + encode(["uint256"], [123]),
        "or": and_predicate([protocol], [timestamp_below(123)], selector=OR),
        "no timestampBelow": and_predicate([protocol], [nonce_equals(maker, 0)]),
        "two timestampBelow": and_predicate(
            [protocol, protocol], [timestamp_below(123), timestamp_below(456)]
        ),
        "foreign target": and_predicate([other], [timestamp_below(123)]),
        "foreign nonce target": and_predicate(
            [protocol, other], [timestamp_below(123), nonce_equals(maker, 0)]
        ),
        "other maker nonce": and_predicate(
            [protocol, protocol], [nonce_equals(other, 0), timestamp_below(123)]
        ),
        "dirty maker word": and_predicate(
            [protocol, protocol], [dirty_maker, timestamp_below(123)]
        ),
        "short nonceEquals": and_predicate(
            [protocol, protocol], [nonce_equals(maker, 0)[:-1], timestamp_below(123)]
        ),
        "nested and": and_predicate(
            [protocol], [and_predicate([protocol], [timestamp_below(123)])[:MAX_PREDICATE_CALL]]
        ),
        "empty entry": and_predicate([protocol, protocol], [b"", timestamp_below(123)]),
        "empty and": and_predicate([], []),
        "trailing bytes": and_predicate([protocol], [timestamp_below(123)]) + b"\x00",
        "length mismatch": AND
        + encode(["address[]", "bytes[]"], [[protocol, protocol], [timestamp_below(123)]]),
    }
    for name, predicate in bad.items():
        with boa.reverts(custom_err("OrderNotValid(string)", "BadPredicate")), boa.env.anchor():
            harness.valid_to(with_fields(order, predicate=predicate), protocol)


def test_valid_to_rejects_timestamp_below_zero(harness, order, protocol, maker):
    # timestampBelow(0) never fills; the bound underflows rather than wrap.
    with boa.reverts():
        harness.valid_to(with_fields(order, predicate=timestamp_below(0)), protocol)
    with boa.reverts():
        harness.valid_to(
            with_fields(order, predicate=api_predicate(protocol, maker, 0, 0)), protocol
        )


def test_valid_to_rejects_non_canonical_and_encoding(harness, order, protocol):
    # Loose offsets decode but are not the canonical encoding of the pair.
    canonical = and_predicate([protocol], [timestamp_below(123)])
    head = canonical[:4]
    body = bytearray(canonical[4:])
    # Shift the bytes[] array one word further and pad.
    offset = int.from_bytes(body[32:64], "big")
    body[32:64] = (offset + 32).to_bytes(32, "big")
    loose = head + bytes(body[:offset]) + b"\x00" * 32 + bytes(body[offset:])
    assert loose != canonical
    with boa.reverts(custom_err("OrderNotValid(string)", "BadPredicate")):
        harness.valid_to(with_fields(order, predicate=loose), protocol)
    assert harness.valid_to(with_fields(order, predicate=canonical), protocol) == 122
