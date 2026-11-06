"""Standalone tests for the stateless oneinch module.

Covers the LOP v4 Order type, its EIP-712 digest and domain against an
independent reference and vectors recorded from the deployed
AggregationRouterV6 (eip712Domain / hashOrder on Ethereum and Polygon), the
makerTraits layout, and the extension binding and field layout of
OrderLib/ExtensionLib. The OneInchAdapter built on top (shape checks and
typed reverts) is tested in test_oneinch_adapter.py; settlement through the
real protocol in test_oneinch_fork.py.
"""

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak, to_checksum_address
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.burners.conftest import custom_err

MAX_UINT256 = 2**256 - 1
MASK160 = 2**160 - 1

ORDER_TYPE = (
    "Order(uint256 salt,address maker,address receiver,address makerAsset,address takerAsset,"
    "uint256 makingAmount,uint256 takingAmount,uint256 makerTraits)"
)
DOMAIN_TYPE = "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
ORDER_TYPE_HASH = keccak(text=ORDER_TYPE)
DOMAIN_TYPE_HASH = keccak(text=DOMAIN_TYPE)
ORDER_ABI = "(uint256,address,address,address,address,uint256,uint256,uint256)"
ORDER_FIELD_TYPES = ["uint256", "address", "address", "address", "address"] + ["uint256"] * 3

# MakerTraitsLib
NO_PARTIAL_FILLS_FLAG = 1 << 255
ALLOW_MULTIPLE_FILLS_FLAG = 1 << 254
HAS_EXTENSION_FLAG = 1 << 249
USE_PERMIT2_FLAG = 1 << 248
UNWRAP_WETH_FLAG = 1 << 247
EXPIRATION_OFFSET = 80
EXPIRATION_MASK = 2**40 - 1

# Deployed AggregationRouterV6 (LOP v4, 0x111111125421cA6dc452d289314280a0f8842A65,
# the same verified source on Ethereum and Polygon): eip712Domain() reports
# fields 0x0f, "1inch Aggregation Router", "6", the chain id and itself; the
# separators and hashOrder(ROUTER_ORDER) below were recorded from forks
# (Ethereum block 26308877, Polygon block 96465088).
ROUTER = "0x111111125421cA6dc452d289314280a0f8842A65"
ROUTER_NAME = "1inch Aggregation Router"
ROUTER_VERSION = "6"
ROUTER_ORDER = (
    0x0102030405060708091011121314151617181920212223242526272829303132,
    "0x1111111111111111111111111111111111111111",
    "0x2222222222222222222222222222222222222222",
    "0x4444444444444444444444444444444444444444",
    "0x5555555555555555555555555555555555555555",
    250 * 10**18,
    100_000 * 10**18,
    ALLOW_MULTIPLE_FILLS_FLAG | (1_800_000_000 << EXPIRATION_OFFSET),
)
ROUTER_VECTORS = {
    1: (
        bytes.fromhex("d999e213f11c7bfa3e796c3409e316f25e02aa3e25e5c207a92e381c7d22b6de"),
        bytes.fromhex("89f89ef7c36d85843f116382704c088928c758054f0aa01c6dc50f877a90d04e"),
    ),
    137: (
        bytes.fromhex("3107ddeb100bad118b76ea251053c2dbf874460e2ba9c2fc6e129cd9b21ef29e"),
        bytes.fromhex("5b0e9a1945fa9a42527fea429ab9abdbf8374ef679afd8abfeb76ac9f3624efd"),
    ),
}

HARNESS_SOURCE = """
# pragma version 0.5.0b1

import contracts.burners.adapters.oneinch.oneinch as oneinch


@external
@pure
def decode(_encoded: Bytes[oneinch.ENCODED_ORDER_LEN]) -> oneinch.Order:
    return abi_decode(_encoded, oneinch.Order)


@external
@pure
def domain_separator(
    _name: String[oneinch.MAX_DOMAIN_NAME],
    _version: String[oneinch.MAX_DOMAIN_VERSION],
    _chain_id: uint256,
    _verifying_contract: address,
) -> bytes32:
    return oneinch._domain_separator(_name, _version, _chain_id, _verifying_contract)


@external
@pure
def order_digest(_order: oneinch.Order, _domain_separator: bytes32) -> bytes32:
    return oneinch._order_digest(_order, _domain_separator)


@external
@pure
def to_words(_order: oneinch.Order) -> oneinch.OrderWords:
    return oneinch._to_words(_order)


@external
@pure
def words_digest(_words: oneinch.OrderWords, _domain_separator: bytes32) -> bytes32:
    return oneinch._words_digest(_words, _domain_separator)


@external
@pure
def expiration(_maker_traits: uint256) -> uint256:
    return oneinch._expiration(_maker_traits)


@external
@pure
def has_flag(_maker_traits: uint256, _flag: uint256) -> bool:
    return oneinch._has_flag(_maker_traits, _flag)


@external
@pure
def receiver(_order: oneinch.Order) -> address:
    return oneinch._receiver(_order)


@external
@pure
def check_extension_binding(_order: oneinch.Order, _extension: Bytes[oneinch.MAX_EXTENSION]):
    oneinch._check_extension_binding(_order, _extension)


@external
@pure
def extension_offsets(_extension: Bytes[oneinch.MAX_EXTENSION]) -> uint256:
    return oneinch._extension_offsets(_extension)


@external
@pure
def field_is_empty(_offsets: uint256, _field: uint256) -> bool:
    return oneinch._field_is_empty(_offsets, _field)


@external
@pure
def constants() -> (bytes32, bytes32, uint256, uint256):
    return (
        oneinch.ORDER_TYPE_HASH,
        oneinch.EIP712_DOMAIN_TYPE_HASH,
        oneinch.ENCODED_ORDER_LEN,
        oneinch.MAX_EXTENSION,
    )
"""


def domain_separator_reference(name: str, version: str, chain_id: int, contract: str) -> bytes:
    """OpenZeppelin EIP712._buildDomainSeparator from eth_abi/keccak."""
    return keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [DOMAIN_TYPE_HASH, keccak(text=name), keccak(text=version), chain_id, contract],
        )
    )


def order_digest_reference(order, domain_separator: bytes) -> bytes:
    """Independent OrderLib.hash model built from eth_abi/keccak primitives."""
    struct_hash = keccak(encode(["bytes32", *ORDER_FIELD_TYPES], [ORDER_TYPE_HASH, *order]))
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def build_extension(fields: list[bytes] | None = None, custom: bytes = b"") -> bytes:
    """ExtensionLib layout: a word of eight cumulative uint32 field ends, then
    the fields (makerAssetSuffix, takerAssetSuffix, makingAmountData,
    takingAmountData, predicate, makerPermit, preInteraction,
    postInteraction) and custom data."""
    fields = [b""] * 8 if fields is None else fields
    assert len(fields) == 8
    offsets, end = 0, 0
    for index, field in enumerate(fields):
        end += len(field)
        offsets |= end << (32 * index)
    return offsets.to_bytes(32, "big") + b"".join(fields) + custom


def bind_extension(order: tuple, extension: bytes) -> tuple:
    """Commit the salt's low 160 bits to the extension and set the flag."""
    salt = (order[0] & ~MASK160) | (int.from_bytes(keccak(extension), "big") & MASK160)
    return (salt, *order[1:7], order[7] | HAS_EXTENSION_FLAG)


def address_from_int(value: int) -> str:
    return to_checksum_address(value.to_bytes(20, "big"))


@pytest.fixture(scope="module")
def harness():
    return boa.loads(HARNESS_SOURCE, name="OneInchHarness", filename="OneInchHarness.vy")


# Constants and the router vectors


def test_constants_match_the_protocol(harness):
    order_type_hash, domain_type_hash, encoded_len, max_extension = harness.constants()
    assert bytes(order_type_hash) == ORDER_TYPE_HASH
    assert bytes(domain_type_hash) == DOMAIN_TYPE_HASH
    assert encoded_len == 8 * 32 == len(encode([ORDER_ABI], [ROUTER_ORDER]))
    assert max_extension % 32 == 0


@pytest.mark.parametrize("chain_id", sorted(ROUTER_VECTORS))
def test_router_vectors(harness, chain_id):
    domain, order_hash = ROUTER_VECTORS[chain_id]
    assert bytes(harness.domain_separator(ROUTER_NAME, ROUTER_VERSION, chain_id, ROUTER)) == domain
    assert bytes(harness.order_digest(ROUTER_ORDER, domain)) == order_hash
    # The reference model agrees with the deployed protocol too, so the fuzz
    # below is anchored to the real implementation.
    assert domain_separator_reference(ROUTER_NAME, ROUTER_VERSION, chain_id, ROUTER) == domain
    assert order_digest_reference(ROUTER_ORDER, domain) == order_hash


def test_digest_binds_the_domain(harness):
    mainnet, _ = ROUTER_VECTORS[1]
    polygon, _ = ROUTER_VECTORS[137]
    assert harness.order_digest(ROUTER_ORDER, mainnet) != harness.order_digest(
        ROUTER_ORDER, polygon
    )


def test_words_view_hashes_like_the_protocol_abi(harness):
    # The protocol ABI spells addresses as uint256 words (its Address type)
    # and hashes them raw; the words view of a clean order hashes the same.
    domain, order_hash = ROUTER_VECTORS[1]
    words = tuple(harness.to_words(ROUTER_ORDER))
    assert words == tuple(
        int(field, 16) if isinstance(field, str) else field for field in ROUTER_ORDER
    )
    assert bytes(harness.words_digest(words, domain)) == order_hash
    # A dirty upper bit changes the protocol's hash, so such an order can
    # never match a decoded (clean) order.
    dirty = (words[0], words[1] | (1 << 200), *words[2:])
    assert bytes(harness.words_digest(dirty, domain)) != order_hash
    # The selector the adapter's probe calls is the protocol's.
    assert keccak(text="hashOrder((" + ",".join(["uint256"] * 8) + "))")[:4] == bytes.fromhex(
        "802b2ef1"
    )


def test_decode_round_trips_the_canonical_encoding(harness):
    encoded = encode([ORDER_ABI], [ROUTER_ORDER])
    assert tuple(harness.decode(encoded)) == ROUTER_ORDER
    with boa.reverts():
        harness.decode(encoded[:-1])
    # A dirty address word is not an address: the decoder refuses it, and the
    # protocol would hash the raw word anyway.
    dirty = bytearray(encoded)
    dirty[32] = 0x01
    with boa.reverts():
        harness.decode(bytes(dirty))


# Digest fuzz against the reference


@given(
    salt=st.integers(min_value=0, max_value=MAX_UINT256),
    maker_int=st.integers(min_value=0, max_value=MASK160),
    receiver_int=st.integers(min_value=0, max_value=MASK160),
    maker_asset_int=st.integers(min_value=0, max_value=MASK160),
    taker_asset_int=st.integers(min_value=0, max_value=MASK160),
    making_amount=st.integers(min_value=0, max_value=MAX_UINT256),
    taking_amount=st.integers(min_value=0, max_value=MAX_UINT256),
    maker_traits=st.integers(min_value=0, max_value=MAX_UINT256),
    domain=st.binary(min_size=32, max_size=32),
)
@settings(max_examples=40, deadline=None)
def test_digest_fuzz_matches_reference(
    harness,
    salt,
    maker_int,
    receiver_int,
    maker_asset_int,
    taker_asset_int,
    making_amount,
    taking_amount,
    maker_traits,
    domain,
):
    order = (
        salt,
        address_from_int(maker_int),
        address_from_int(receiver_int),
        address_from_int(maker_asset_int),
        address_from_int(taker_asset_int),
        making_amount,
        taking_amount,
        maker_traits,
    )
    decoded = harness.decode(encode([ORDER_ABI], [order]))
    assert bytes(harness.order_digest(decoded, domain)) == order_digest_reference(order, domain)


# The domain strings are byte-bounded on chain; the protocol's are ASCII.
ASCII = st.characters(min_codepoint=0x20, max_codepoint=0x7E)


@given(
    name=st.text(alphabet=ASCII, max_size=64),
    version=st.text(alphabet=ASCII, max_size=32),
    chain_id=st.integers(min_value=0, max_value=MAX_UINT256),
    contract_int=st.integers(min_value=0, max_value=MASK160),
)
@settings(max_examples=30, deadline=None)
def test_domain_fuzz_matches_reference(harness, name, version, chain_id, contract_int):
    contract = address_from_int(contract_int)
    assert bytes(
        harness.domain_separator(name, version, chain_id, contract)
    ) == domain_separator_reference(name, version, chain_id, contract)


# Maker traits


def test_expiration_is_the_40_bit_field_from_bit_80(harness):
    assert harness.expiration(0) == 0
    assert harness.expiration(1_800_000_000 << EXPIRATION_OFFSET) == 1_800_000_000
    # Neighbouring fields (allowed sender below, nonce above) do not leak in.
    traits = (2**80 - 1) | (EXPIRATION_MASK << EXPIRATION_OFFSET) | ((2**40 - 1) << 120)
    assert harness.expiration(traits) == EXPIRATION_MASK
    assert harness.expiration(MAX_UINT256 & ~(EXPIRATION_MASK << EXPIRATION_OFFSET)) == 0


def test_flags_are_read_individually(harness):
    traits = ALLOW_MULTIPLE_FILLS_FLAG | USE_PERMIT2_FLAG
    assert harness.has_flag(traits, ALLOW_MULTIPLE_FILLS_FLAG)
    assert harness.has_flag(traits, USE_PERMIT2_FLAG)
    assert not harness.has_flag(traits, UNWRAP_WETH_FLAG)
    assert not harness.has_flag(traits, HAS_EXTENSION_FLAG)
    assert not harness.has_flag(traits, NO_PARTIAL_FILLS_FLAG)


def test_receiver_defaults_to_the_maker(harness):
    assert harness.receiver(ROUTER_ORDER) == ROUTER_ORDER[2]
    no_receiver = (ROUTER_ORDER[0], ROUTER_ORDER[1], address_from_int(0), *ROUTER_ORDER[3:])
    assert harness.receiver(no_receiver) == ROUTER_ORDER[1]


# Extension binding (OrderLib.isValidExtension)


def test_extension_binding_follows_the_flag_and_salt(harness):
    harness.check_extension_binding(ROUTER_ORDER, b"")
    extension = build_extension([b""] * 4 + [b"\x01\x02", b"", b"", b""])
    bound = bind_extension(ROUTER_ORDER, extension)
    harness.check_extension_binding(bound, extension)
    with boa.reverts(custom_err("OrderNotValid(string)", "UnexpectedOrderExtension")):
        harness.check_extension_binding(ROUTER_ORDER, extension)
    with boa.reverts(custom_err("OrderNotValid(string)", "MissingOrderExtension")):
        harness.check_extension_binding(bound, b"")
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidExtensionHash")):
        harness.check_extension_binding(bound, extension + b"\x00")
    # Only the low 160 bits of the salt commit to the extension.
    high_salt = ((bound[0] | (1 << 200)), *bound[1:])
    harness.check_extension_binding(high_salt, extension)
    low_salt = ((bound[0] ^ 1), *bound[1:])
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidExtensionHash")):
        harness.check_extension_binding(low_salt, extension)


# Extension fields (ExtensionLib/OffsetsLib)


def test_offsets_are_the_first_word_or_zero(harness):
    assert harness.extension_offsets(b"") == 0
    assert harness.extension_offsets(b"\xff" * 31) == 0
    extension = build_extension([b"a", b"bc", b"", b"def", b"", b"", b"", b"g"], custom=b"zz")
    assert harness.extension_offsets(extension) == int.from_bytes(extension[:32], "big")


def test_field_emptiness_follows_cumulative_ends(harness):
    fields = [b"a", b"", b"bc", b"", b"def", b"", b"", b"g"]
    offsets = harness.extension_offsets(build_extension(fields))
    for index, field in enumerate(fields):
        assert harness.field_is_empty(offsets, index) == (field == b"")
    assert all(harness.field_is_empty(0, index) for index in range(8))
    # An end below its start is a bogus length, never an empty field.
    inverted = (5 << 32) | 7
    assert not harness.field_is_empty(inverted, 1)
    assert not harness.field_is_empty(inverted, 0)
    assert harness.field_is_empty(inverted, 2) is False
    aligned = (7 << 32) | 7
    assert harness.field_is_empty(aligned, 1)


@given(lengths=st.lists(st.integers(min_value=0, max_value=5), min_size=8, max_size=8))
@settings(max_examples=30, deadline=None)
def test_field_emptiness_fuzz(harness, lengths):
    fields = [bytes(length) for length in lengths]
    offsets = harness.extension_offsets(build_extension(fields))
    for index, length in enumerate(lengths):
        assert harness.field_is_empty(offsets, index) == (length == 0)
