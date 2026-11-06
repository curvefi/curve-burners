"""Standalone tests for the stateless hashflow module.

Covers the Hashflow RFQ-m Quote type, its EIP-712 struct hash, digest and
domain against an independent reference and the domain the deployed
HashflowRouter reports on Ethereum (eip712Domain), the market maker's EIP-191
digest against a reference and a key recovery, and the protocol's calldata
shape. The router exposes no hashing view, so there is no recorded digest
vector: the fork test proves the digest by having the real router accept the
auction's ERC-1271 answer. The HashflowAdapter built on top (shape checks and
typed reverts) is tested in test_hashflow_adapter.py; settlement through the
real protocol in test_hashflow_fork.py.
"""

import boa
import pytest
from eth_abi import encode
from eth_keys import keys
from eth_utils import keccak, to_checksum_address
from hypothesis import given, settings
from hypothesis import strategies as st

MAX_UINT256 = 2**256 - 1
MASK160 = 2**160 - 1

QUOTE_TYPE = (
    "Quote(bytes32 txid,address trader,address pool,address externalAccount,"
    "address baseToken,address quoteToken,uint256 baseTokenAmount,"
    "uint256 quoteTokenAmount,uint256 quoteExpiry)"
)
DOMAIN_TYPE = "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
QUOTE_TYPE_HASH = keccak(text=QUOTE_TYPE)
DOMAIN_TYPE_HASH = keccak(text=DOMAIN_TYPE)
DOMAIN_NAME = "Hashflow - Router"
DOMAIN_VERSION = "1.0"
DOMAIN_FIELDS = b"\x0f"
# The signed quote: nine static fields, txid first.
QUOTE_ABI = "(bytes32,address,address,address,address,address,uint256,uint256,uint256)"
# The protocol's RFQMQuote calldata: calldata order plus both signatures.
RFQM_CALLDATA_ABI = (
    "(address,address,address,address,address,uint256,uint256,uint256,bytes32,bytes,bytes)"
)
ENCODED_QUOTE_LEN = 9 * 32
EIP191_PREFIX = b"\x19Ethereum Signed Message:\n32"

# Deployed HashflowRouter on Ethereum (not a proxy, solc 0.8.18): its
# eip712Domain() answered (0x0f, "Hashflow - Router", "1.0", 1, router, 0, [])
# on a mainnet fork, which fixes the domain separator below.
ROUTER = "0x55084eE0fEf03f14a305cd24286359A35D735151"
FACTORY = "0xdE828fdc3F497F16416D1bB645261C7C6a62DAb5"
MAINNET_DOMAIN_SEPARATOR = bytes.fromhex(
    "8439e60b4107fcfbd5c424aa59d0e1712a37f295626b1a4eeda377218add2cdd"
)

PROBE_QUOTE = (
    bytes.fromhex("0102030405060708091011121314151617181920212223242526272829303132"),
    "0x1111111111111111111111111111111111111111",
    "0x2222222222222222222222222222222222222222",
    "0x3333333333333333333333333333333333333333",
    "0x4444444444444444444444444444444444444444",
    "0x5555555555555555555555555555555555555555",
    250 * 10**18,
    100_000 * 10**18,
    1_800_000_000,
)
EMPTY_QUOTE = (bytes(32), *["0x" + "00" * 20] * 5, 0, 0, 0)
MAKER_KEY = keys.PrivateKey(keccak(b"hashflow maker"))

HARNESS_SOURCE = """
# pragma version 0.5.0b1

import contracts.burners.adapters.hashflow.hashflow as hashflow


@external
@pure
def domain_separator(_chain_id: uint256, _router: address) -> bytes32:
    return hashflow._domain_separator(_chain_id, _router)


@external
@pure
def struct_hash(_quote: hashflow.Quote) -> bytes32:
    return hashflow._struct_hash(_quote)


@external
@pure
def quote_digest(_quote: hashflow.Quote, _domain_separator: bytes32) -> bytes32:
    return hashflow._quote_digest(_quote, _domain_separator)


@external
@pure
def maker_digest(_quote: hashflow.Quote, _chain_id: uint256) -> bytes32:
    return hashflow._maker_digest(_quote, _chain_id)


@external
@pure
def signed(_quote: hashflow.RFQMQuote) -> hashflow.Quote:
    return hashflow._signed(_quote)


@external
@pure
def to_calldata(
    _quote: hashflow.Quote,
    _taker_signature: Bytes[hashflow.MAX_TAKER_SIGNATURE_LEN],
    _maker_signature: Bytes[hashflow.MAKER_SIGNATURE_LEN],
) -> hashflow.RFQMQuote:
    return hashflow._to_calldata(_quote, _taker_signature, _maker_signature)


@external
@pure
def encode(_quote: hashflow.Quote) -> Bytes[hashflow.ENCODED_QUOTE_LEN]:
    return abi_encode(_quote)


@external
@pure
def decode(_encoded: Bytes[hashflow.ENCODED_QUOTE_LEN]) -> hashflow.Quote:
    return abi_decode(_encoded, hashflow.Quote)


@external
@pure
def constants() -> (bytes32, bytes32, bytes1, uint256, uint256, uint256):
    return (
        hashflow.QUOTE_TYPE_HASH,
        hashflow.EIP712_DOMAIN_TYPE_HASH,
        hashflow.DOMAIN_FIELDS,
        hashflow.ENCODED_QUOTE_LEN,
        hashflow.MAX_TAKER_SIGNATURE_LEN,
        hashflow.MAKER_SIGNATURE_LEN,
    )
"""


def domain_separator_reference(chain_id: int, router: str) -> bytes:
    """OpenZeppelin EIP712._domainSeparatorV4 from eth_abi/keccak."""
    return keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                DOMAIN_TYPE_HASH,
                keccak(text=DOMAIN_NAME),
                keccak(text=DOMAIN_VERSION),
                chain_id,
                router,
            ],
        )
    )


def struct_hash_reference(quote: tuple) -> bytes:
    """Independent model of the router's abi.encode(QUOTE_TYPEHASH, fields...)."""
    return keccak(
        encode(
            ["bytes32", "bytes32", "address", "address", "address", "address", "address"]
            + ["uint256"] * 3,
            [QUOTE_TYPE_HASH, *quote],
        )
    )


def quote_digest_reference(quote: tuple, domain_separator: bytes) -> bytes:
    return keccak(b"\x19\x01" + domain_separator + struct_hash_reference(quote))


def maker_digest_reference(quote: tuple, chain_id: int) -> bytes:
    """HashflowPool._hashQuoteRFQM: EIP-191 over the packed fields in the
    pool's order (pool, externalAccount, trader, baseToken, quoteToken,
    amounts, expiry, txid, chainid)."""
    (
        txid,
        trader,
        pool,
        external_account,
        base_token,
        quote_token,
        base_amount,
        quote_amount,
        expiry,
    ) = quote
    packed = b"".join(
        bytes.fromhex(address[2:])
        for address in (pool, external_account, trader, base_token, quote_token)
    ) + b"".join(value.to_bytes(32, "big") for value in (base_amount, quote_amount, expiry))
    packed += txid + chain_id.to_bytes(32, "big")
    return keccak(EIP191_PREFIX + keccak(packed))


def sign_maker(private_key: keys.PrivateKey, quote: tuple, chain_id: int) -> bytes:
    """The market maker's 65-byte (r, s, v) signature over the pool digest."""
    signature = private_key.sign_msg_hash(maker_digest_reference(quote, chain_id))
    return (
        signature.r.to_bytes(32, "big")
        + signature.s.to_bytes(32, "big")
        + bytes([signature.v + 27])
    )


def to_calldata(quote: tuple, taker_signature: bytes, maker_signature: bytes) -> tuple:
    """The quote as tradeRFQM takes it."""
    (
        txid,
        trader,
        pool,
        external_account,
        base_token,
        quote_token,
        base_amount,
        quote_amount,
        expiry,
    ) = quote
    return (
        pool,
        external_account,
        trader,
        base_token,
        quote_token,
        base_amount,
        quote_amount,
        expiry,
        txid,
        taker_signature,
        maker_signature,
    )


def address_from_int(value: int) -> str:
    return to_checksum_address(value.to_bytes(20, "big"))


@pytest.fixture(scope="module")
def harness():
    return boa.loads(HARNESS_SOURCE, name="HashflowHarness", filename="HashflowHarness.vy")


# Constants and the deployed router's domain


def test_constants_match_the_protocol(harness):
    type_hash, domain_type_hash, fields, quote_len, taker_len, maker_len = harness.constants()
    assert bytes(type_hash) == QUOTE_TYPE_HASH
    assert bytes(domain_type_hash) == DOMAIN_TYPE_HASH
    assert bytes(fields) == DOMAIN_FIELDS
    assert quote_len == ENCODED_QUOTE_LEN == len(encode([QUOTE_ABI], [PROBE_QUOTE]))
    assert taker_len == 20 + ENCODED_QUOTE_LEN
    assert maker_len == 65


def test_mainnet_router_domain(harness):
    domain = bytes(harness.domain_separator(1, ROUTER))
    assert domain == domain_separator_reference(1, ROUTER) == MAINNET_DOMAIN_SEPARATOR


def test_digest_binds_chain_and_contract(harness):
    mainnet = bytes(harness.domain_separator(1, ROUTER))
    assert harness.domain_separator(137, ROUTER) != mainnet
    assert harness.domain_separator(1, FACTORY) != mainnet
    assert harness.quote_digest(PROBE_QUOTE, mainnet) != harness.quote_digest(
        PROBE_QUOTE, bytes(harness.domain_separator(137, ROUTER))
    )


# Encoding and the calldata shape


def test_encode_is_the_abi_tuple_encoding_and_round_trips(harness):
    for quote in (PROBE_QUOTE, EMPTY_QUOTE):
        encoded = encode([QUOTE_ABI], [quote])
        assert bytes(harness.encode(quote)) == encoded
        decoded = tuple(harness.decode(encoded))
        assert (bytes(decoded[0]), *decoded[1:]) == quote
    with boa.reverts():
        harness.decode(encode([QUOTE_ABI], [PROBE_QUOTE])[:-1])


def test_calldata_shape_is_the_protocol_abi(harness):
    taker_signature = b"\xaa" * 20 + encode([QUOTE_ABI], [PROBE_QUOTE])
    maker_signature = sign_maker(MAKER_KEY, PROBE_QUOTE, 1)
    calldata = tuple(harness.to_calldata(PROBE_QUOTE, taker_signature, maker_signature))
    expected = to_calldata(PROBE_QUOTE, taker_signature, maker_signature)
    assert (*calldata[:8], bytes(calldata[8]), bytes(calldata[9]), bytes(calldata[10])) == expected
    signed = tuple(harness.signed(expected))
    assert (bytes(signed[0]), *signed[1:]) == PROBE_QUOTE
    # tradeRFQM's selector, as the transaction census was taken.
    assert keccak(text=f"tradeRFQM({RFQM_CALLDATA_ABI})")[:4] == bytes.fromhex("3020db05")


# Digest fuzz against the references


@st.composite
def quotes(draw):
    return (
        draw(st.binary(min_size=32, max_size=32)),
        *(address_from_int(draw(st.integers(0, MASK160))) for _ in range(5)),
        *(draw(st.integers(0, MAX_UINT256)) for _ in range(3)),
    )


@given(quote=quotes(), domain=st.binary(min_size=32, max_size=32))
@settings(max_examples=40, deadline=None)
def test_digest_fuzz_matches_reference(harness, quote, domain):
    assert bytes(harness.struct_hash(quote)) == struct_hash_reference(quote)
    assert bytes(harness.quote_digest(quote, domain)) == quote_digest_reference(quote, domain)


@given(quote=quotes(), chain_id=st.integers(0, MAX_UINT256))
@settings(max_examples=40, deadline=None)
def test_maker_digest_fuzz_matches_reference(harness, quote, chain_id):
    assert bytes(harness.maker_digest(quote, chain_id)) == maker_digest_reference(quote, chain_id)


@given(chain_id=st.integers(0, MAX_UINT256), router_int=st.integers(0, MASK160))
@settings(max_examples=30, deadline=None)
def test_domain_fuzz_matches_reference(harness, chain_id, router_int):
    router = address_from_int(router_int)
    assert bytes(harness.domain_separator(chain_id, router)) == domain_separator_reference(
        chain_id, router
    )


def test_maker_signature_recovers_to_the_signer(harness):
    digest = bytes(harness.maker_digest(PROBE_QUOTE, 1))
    signature = sign_maker(MAKER_KEY, PROBE_QUOTE, 1)
    r, s, v = signature[:32], signature[32:64], signature[64]
    recovered = keys.Signature(
        vrs=(v - 27, int.from_bytes(r, "big"), int.from_bytes(s, "big"))
    ).recover_public_key_from_msg_hash(digest)
    assert recovered.to_checksum_address() == MAKER_KEY.public_key.to_checksum_address()
    # Another chain is another digest: a quote signed elsewhere never recovers here.
    assert bytes(harness.maker_digest(PROBE_QUOTE, 137)) != digest
