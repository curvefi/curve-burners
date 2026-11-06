"""Standalone tests for the stateless jam module.

Covers the Bebop JAM v2 JamOrder type, its EIP-712 struct hash, digest and
domain against an independent reference and vectors recorded from the
deployed JamSettlement (DOMAIN_SEPARATOR / hashJamOrder on Ethereum and
Polygon), the struct hash against a real taker signature (a mainnet fill
whose Permit2 witness is the JamOrder hash), the calldata shape and the
partnerInfo packing of JamPartner. The BebopJamAdapter built on top (shape
checks and typed reverts) is tested in test_bebop_jam_adapter.py; settlement
through the real protocol in test_bebop_jam_fork.py.
"""

import boa
import pytest
from eth_abi import encode
from eth_keys import keys
from eth_utils import keccak, to_checksum_address
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.burners.conftest import custom_err

MAX_UINT256 = 2**256 - 1
MASK160 = 2**160 - 1
MAX_TOKENS = 8

ORDER_TYPE = (
    "JamOrder(address taker,address receiver,uint256 expiry,uint256 exclusivityDeadline,"
    "uint256 nonce,address executor,uint256 partnerInfo,address[] sellTokens,"
    "address[] buyTokens,uint256[] sellAmounts,uint256[] buyAmounts,bytes32 hooksHash)"
)
DOMAIN_TYPE = "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
ORDER_TYPE_HASH = keccak(text=ORDER_TYPE)
DOMAIN_TYPE_HASH = keccak(text=DOMAIN_TYPE)
DOMAIN_NAME = "JamSettlement"
DOMAIN_VERSION = "2"
# The signed order: twelve fields, hooksHash last.
ORDER_ABI = (
    "(address,address,uint256,uint256,uint256,address,uint256,"
    "address[],address[],uint256[],uint256[],bytes32)"
)
# The protocol's calldata shape: usingPermit2 in place of hooksHash.
ORDER_CALLDATA_ABI = (
    "(address,address,uint256,uint256,uint256,address,uint256,"
    "address[],address[],uint256[],uint256[],bool)"
)
EMPTY_HOOKS_HASH = bytes(32)
NATIVE_TOKEN = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
INF_EXPIRY = 9999999999

# Deployed JamSettlement (0xbeb0b0623f66bE8cE162EbDfA2ec543A522F4ea6, the same
# verified source and address on Ethereum and Polygon; the balance manager it
# created is 0xC5a350853E4e36b73EB0C24aaA4b8816C9A3579a on both): the
# separators and hashJamOrder(order, hooksHash) digests below were recorded
# by eth_call (Ethereum block 26328268, Polygon block 96620793).
SETTLEMENT = "0xbeb0b0623f66bE8cE162EbDfA2ec543A522F4ea6"
BALANCE_MANAGER = "0xC5a350853E4e36b73EB0C24aaA4b8816C9A3579a"
PROBE_ORDER = (
    "0x1111111111111111111111111111111111111111",
    "0x2222222222222222222222222222222222222222",
    1_800_000_000,
    1_700_000_000,
    0x0102030405060708091011121314151617181920212223242526272829303132,
    "0x3333333333333333333333333333333333333333",
    0,
    ["0x4444444444444444444444444444444444444444"],
    ["0x5555555555555555555555555555555555555555"],
    [250 * 10**18],
    [100_000 * 10**18],
    EMPTY_HOOKS_HASH,
)
EMPTY_ORDER = (
    "0x" + "00" * 20,
    "0x" + "00" * 20,
    0,
    0,
    0,
    "0x" + "00" * 20,
    0,
    [],
    [],
    [],
    [],
    EMPTY_HOOKS_HASH,
)
PARTNER = to_checksum_address("0x90a48d5cf7343b08da12e067680b4c6dbfe551be")
MULTI_PARTNER_INFO = (int(PARTNER, 16) << 32) | (60 << 16) | 5
MULTI_ORDER = (
    "0x1111111111111111111111111111111111111111",
    "0x2222222222222222222222222222222222222222",
    INF_EXPIRY,
    0,
    1,
    "0x0000000000000000000000000000000000000000",
    MULTI_PARTNER_INFO,
    ["0x4444444444444444444444444444444444444444", "0x6666666666666666666666666666666666666666"],
    ["0x5555555555555555555555555555555555555555"],
    [250 * 10**18, 7],
    [100_000 * 10**18],
    EMPTY_HOOKS_HASH,
)
HOOKS_HASH = keccak(b"hooks")
SETTLEMENT_VECTORS = {
    1: {
        "domain": bytes.fromhex("abbbaf11110207c0edf6d9f3bee82484d101da7b101cd14eeb3a61b738ec4d6e"),
        "probe": bytes.fromhex("eba6b6bd1af78ec26121fbead3d79771ffffc9e5f0a4a72a3babdd93ffefc3bc"),
        "empty": bytes.fromhex("dc90267e95a140f91453f8392e13c1cdd3eaf2af636b69dfdccbe0c3bec60046"),
        "multi": bytes.fromhex("1ee13412526cab505f547e722d97d0c5aa7715e87ed850b9d5f002d732b0ba9b"),
        "probe_hooks": bytes.fromhex(
            "33903c902406b7f6b7d0ac83c81e2b0159c75ce6ee05cf4d6abfd20772d8b142"
        ),
    },
    137: {
        "domain": bytes.fromhex("66ae1bca6b42c784edead92a3ccaef8f0151e34bdd04610c821010f93f3a2cf6"),
        "probe": bytes.fromhex("9c0914088fe427e80f4fa8bb44c767e335715c7185b8a452a0844c7a797cd43e"),
        "empty": bytes.fromhex("46bd9e21e4e14fd148ad294d9e3a614372dcbb46b06a57cf5bfc36b73bf8aa6a"),
        "multi": bytes.fromhex("42829311648254afd775dba906ad32509e84a2640c0abb1a0941706e6dd26a34"),
        "probe_hooks": bytes.fromhex(
            "b16e02466bf094dd1cc4736fdb2a4842653498e4ecdf481f2bcded3a71715562"
        ),
    },
}

# A real mainnet fill (tx 0xc532133e5ba2066fed43c26e78de0aec5bd3a022a7b502c271e75d9099d782c3,
# settle with usingPermit2): the taker's 65-byte signature is over the Permit2
# PermitBatchWitnessTransferFrom digest whose witness is JamOrderLib.hash of
# the order, so recovering the taker from it proves the struct hash.
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
FILLED_ORDER = (
    "0x8208eBe21d27BC8f50DAe468B344D9fe7b8648B0",
    "0x8208eBe21d27BC8f50DAe468B344D9fe7b8648B0",
    1783948921,
    1783948921,
    265728055035724575484349554235031966524,
    "0x6E1d61f5bf0c9D4E3511C33ea93C04488206e179",
    0,
    ["0xdAC17F958D2ee523a2206206994597C13D831ec7"],
    ["0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"],
    [3581951716],
    [3579286394],
    EMPTY_HOOKS_HASH,
)
FILLED_STRUCT_HASH = bytes.fromhex(
    "8358b9e831e02e909d4223b541dab8ae977fd4479ae2861dfd89aa86531dd872"
)
FILLED_SIGNATURE = bytes.fromhex(
    "cb4ae9e6bf6f43fd187712d9570f7d93dcd825d4fb9cee0937f408479e4d8102"
    "501d66f76aca036e74c0bb227d0fee745bb3346bde562d69024da5078ef0b0071c"
)

HARNESS_SOURCE = """
# pragma version 0.5.0b1

import contracts.burners.adapters.bebop.jam as jam


@external
@pure
def domain_separator(_chain_id: uint256, _verifying_contract: address) -> bytes32:
    return jam._domain_separator(_chain_id, _verifying_contract)


@external
@pure
def struct_hash(_order: jam.JamOrder) -> bytes32:
    return jam._struct_hash(_order)


@external
@pure
def order_digest(_order: jam.JamOrder, _domain_separator: bytes32) -> bytes32:
    return jam._order_digest(_order, _domain_separator)


@external
@pure
def to_calldata(_order: jam.JamOrder) -> jam.JamOrderCalldata:
    return jam._to_calldata(_order)


@external
@pure
def encode(_order: jam.JamOrder) -> Bytes[jam.ENCODED_ORDER_BOUND]:
    return abi_encode(_order)


@external
@pure
def decode(_encoded: Bytes[jam.ENCODED_ORDER_BOUND]) -> jam.JamOrder:
    return abi_decode(_encoded, jam.JamOrder)


@external
@pure
def unpack_partner_info(_partner_info: uint256) -> (address, uint256, uint256):
    return jam._unpack_partner_info(_partner_info)


@external
@pure
def constants() -> (bytes32, bytes32, uint256, uint256, uint256, address, uint256):
    return (
        jam.ORDER_TYPE_HASH,
        jam.EIP712_DOMAIN_TYPE_HASH,
        jam.MAX_TOKENS,
        jam.ENCODED_ORDER_BOUND,
        jam.ENCODED_SINGLE_ORDER_LEN,
        jam.NATIVE_TOKEN,
        jam.INF_EXPIRY,
    )
"""


def domain_separator_reference(chain_id: int, contract: str) -> bytes:
    """JamValidation.DOMAIN_SEPARATOR from eth_abi/keccak."""
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


def _packed_hash(typ: str, values: list) -> bytes:
    """keccak256(abi.encodePacked(T[])): elements padded to words."""
    return keccak(encode([typ] * len(values), list(values)))


def struct_hash_reference(order: tuple) -> bytes:
    """Independent JamOrderLib.hash model built from eth_abi/keccak primitives."""
    (taker, receiver, expiry, deadline, nonce, executor, partner_info, *arrays, hooks_hash) = order
    sell_tokens, buy_tokens, sell_amounts, buy_amounts = arrays
    return keccak(
        encode(
            ["bytes32", "address", "address", "uint256", "uint256", "uint256", "address"]
            + ["uint256"]
            + ["bytes32"] * 5,
            [
                ORDER_TYPE_HASH,
                taker,
                receiver,
                expiry,
                deadline,
                nonce,
                executor,
                partner_info,
                _packed_hash("address", sell_tokens),
                _packed_hash("address", buy_tokens),
                _packed_hash("uint256", sell_amounts),
                _packed_hash("uint256", buy_amounts),
                hooks_hash,
            ],
        )
    )


def order_digest_reference(order: tuple, domain_separator: bytes) -> bytes:
    return keccak(b"\x19\x01" + domain_separator + struct_hash_reference(order))


def permit2_witness_digest(order: tuple, spender: str, chain_id: int = 1) -> bytes:
    """Permit2 PermitBatchWitnessTransferFrom digest with the order as witness
    (JamOrderLib.permit2OrderHash / PermitHash.hashWithWitness)."""
    witness_type = (
        "JamOrder witness)" + ORDER_TYPE + "TokenPermissions(address token,uint256 amount)"
    )
    type_hash = keccak(
        text="PermitBatchWitnessTransferFrom(TokenPermissions[] permitted,address spender,"
        "uint256 nonce,uint256 deadline," + witness_type
    )
    token_permissions_type_hash = keccak(text="TokenPermissions(address token,uint256 amount)")
    permitted = b"".join(
        keccak(encode(["bytes32", "address", "uint256"], [token_permissions_type_hash, t, a]))
        for t, a in zip(order[7], order[9])
    )
    struct_hash = keccak(
        encode(
            ["bytes32", "bytes32", "address", "uint256", "uint256", "bytes32"],
            [
                type_hash,
                keccak(permitted),
                spender,
                order[4],
                order[2],
                struct_hash_reference(order),
            ],
        )
    )
    domain = keccak(
        encode(
            ["bytes32", "bytes32", "uint256", "address"],
            [
                keccak(text="EIP712Domain(string name,uint256 chainId,address verifyingContract)"),
                keccak(text="Permit2"),
                chain_id,
                PERMIT2,
            ],
        )
    )
    return keccak(b"\x19\x01" + domain + struct_hash)


def address_from_int(value: int) -> str:
    return to_checksum_address(value.to_bytes(20, "big"))


def pack_partner_info(partner: str, partner_fee: int, protocol_fee: int) -> int:
    return (int(partner, 16) << 32) | (partner_fee << 16) | protocol_fee


@pytest.fixture(scope="module")
def harness():
    return boa.loads(HARNESS_SOURCE, name="JamHarness", filename="JamHarness.vy")


# Constants and the settlement vectors


def test_constants_match_the_protocol(harness):
    type_hash, domain_type_hash, max_tokens, bound, single_len, native, inf_expiry = (
        harness.constants()
    )
    assert bytes(type_hash) == ORDER_TYPE_HASH
    assert bytes(domain_type_hash) == DOMAIN_TYPE_HASH
    assert max_tokens == MAX_TOKENS
    assert bound == 32 + 12 * 32 + 4 * (32 + MAX_TOKENS * 32)
    assert single_len == len(encode([ORDER_ABI], [PROBE_ORDER])) == 672
    assert native == NATIVE_TOKEN
    assert inf_expiry == INF_EXPIRY


@pytest.mark.parametrize("chain_id", sorted(SETTLEMENT_VECTORS))
def test_settlement_vectors(harness, chain_id):
    vectors = SETTLEMENT_VECTORS[chain_id]
    domain = bytes(harness.domain_separator(chain_id, SETTLEMENT))
    assert domain == vectors["domain"] == domain_separator_reference(chain_id, SETTLEMENT)
    cases = {
        "probe": PROBE_ORDER,
        "empty": EMPTY_ORDER,
        "multi": MULTI_ORDER,
        "probe_hooks": (*PROBE_ORDER[:11], HOOKS_HASH),
    }
    for name, order in cases.items():
        assert bytes(harness.order_digest(order, domain)) == vectors[name], name
        # The reference model agrees with the deployed protocol too, so the
        # fuzz below is anchored to the real implementation.
        assert order_digest_reference(order, domain) == vectors[name], name


def test_digest_binds_the_domain(harness):
    mainnet = SETTLEMENT_VECTORS[1]["domain"]
    polygon = SETTLEMENT_VECTORS[137]["domain"]
    assert harness.order_digest(PROBE_ORDER, mainnet) != harness.order_digest(PROBE_ORDER, polygon)


def test_struct_hash_matches_a_real_taker_signature(harness):
    assert bytes(harness.struct_hash(FILLED_ORDER)) == FILLED_STRUCT_HASH
    assert struct_hash_reference(FILLED_ORDER) == FILLED_STRUCT_HASH
    digest = permit2_witness_digest(FILLED_ORDER, BALANCE_MANAGER)
    r, s, v = FILLED_SIGNATURE[:32], FILLED_SIGNATURE[32:64], FILLED_SIGNATURE[64]
    signature = keys.Signature(vrs=(v - 27, int.from_bytes(r, "big"), int.from_bytes(s, "big")))
    recovered = signature.recover_public_key_from_msg_hash(digest).to_checksum_address()
    assert recovered == FILLED_ORDER[0]


def test_hooks_hash_is_part_of_the_struct(harness):
    domain = SETTLEMENT_VECTORS[1]["domain"]
    with_hooks = (*PROBE_ORDER[:11], HOOKS_HASH)
    assert harness.struct_hash(PROBE_ORDER) != harness.struct_hash(with_hooks)
    assert bytes(harness.order_digest(with_hooks, domain)) == SETTLEMENT_VECTORS[1]["probe_hooks"]


# Encoding


def test_encode_is_the_abi_tuple_encoding_and_round_trips(harness):
    for order in (PROBE_ORDER, EMPTY_ORDER, MULTI_ORDER):
        encoded = encode([ORDER_ABI], [order])
        assert bytes(harness.encode(order)) == encoded
        decoded = harness.decode(encoded)
        assert tuple(decoded)[:7] == order[:7]
        assert [list(field) for field in tuple(decoded)[7:11]] == [list(f) for f in order[7:11]]
        assert bytes(tuple(decoded)[11]) == order[11]
    with boa.reverts():
        harness.decode(encode([ORDER_ABI], [PROBE_ORDER])[:-1])
    # Arrays above the bound do not decode.
    too_many = (*PROBE_ORDER[:7], ["0x" + "44" * 20] * 9, *PROBE_ORDER[8:])
    with boa.reverts():
        harness.decode(encode([ORDER_ABI], [too_many]))


def test_calldata_shape_is_the_protocol_abi(harness):
    words = tuple(harness.to_calldata(PROBE_ORDER))
    assert words[:7] == PROBE_ORDER[:7]
    assert words[11] is False
    # The selector the adapter's probe calls is the protocol's.
    assert keccak(text=f"hashJamOrder({ORDER_CALLDATA_ABI},bytes32)")[:4] == bytes.fromhex(
        "f0a5f8f2"
    )


# Digest fuzz against the reference


@st.composite
def orders(draw):
    def addresses(n):
        return [address_from_int(draw(st.integers(0, MASK160))) for _ in range(n)]

    def amounts(n):
        return [draw(st.integers(0, MAX_UINT256)) for _ in range(n)]

    sells = draw(st.integers(0, MAX_TOKENS))
    buys = draw(st.integers(0, MAX_TOKENS))
    return (
        address_from_int(draw(st.integers(0, MASK160))),
        address_from_int(draw(st.integers(0, MASK160))),
        draw(st.integers(0, MAX_UINT256)),
        draw(st.integers(0, MAX_UINT256)),
        draw(st.integers(0, MAX_UINT256)),
        address_from_int(draw(st.integers(0, MASK160))),
        draw(st.integers(0, MAX_UINT256)),
        addresses(sells),
        addresses(buys),
        amounts(sells),
        amounts(buys),
        draw(st.binary(min_size=32, max_size=32)),
    )


@given(order=orders(), domain=st.binary(min_size=32, max_size=32))
@settings(max_examples=40, deadline=None)
def test_digest_fuzz_matches_reference(harness, order, domain):
    assert bytes(harness.struct_hash(order)) == struct_hash_reference(order)
    assert bytes(harness.order_digest(order, domain)) == order_digest_reference(order, domain)


@given(
    chain_id=st.integers(min_value=0, max_value=MAX_UINT256), contract_int=st.integers(0, MASK160)
)
@settings(max_examples=30, deadline=None)
def test_domain_fuzz_matches_reference(harness, chain_id, contract_int):
    contract = address_from_int(contract_int)
    assert bytes(harness.domain_separator(chain_id, contract)) == domain_separator_reference(
        chain_id, contract
    )


# Partner info (JamPartner.unpackPartnerInfo)


def test_partner_info_unpacks_like_the_protocol(harness):
    assert tuple(harness.unpack_partner_info(0)) == (address_from_int(0), 0, 0)
    assert tuple(harness.unpack_partner_info(MULTI_PARTNER_INFO)) == (PARTNER, 60, 5)
    # Protocol fee alone needs no partner.
    assert tuple(harness.unpack_partner_info(10)) == (address_from_int(0), 0, 10)
    # Bits above the address are dropped, as uint160 truncation does.
    high = MULTI_PARTNER_INFO | (1 << 200)
    assert tuple(harness.unpack_partner_info(high)) == (PARTNER, 60, 5)


def test_partner_info_rejects_what_the_protocol_rejects(harness):
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidFeePercentage")):
        harness.unpack_partner_info(pack_partner_info(PARTNER, 5000, 5000))
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidFeePercentage")):
        harness.unpack_partner_info(pack_partner_info(address_from_int(0), 0, 10000))
    assert tuple(harness.unpack_partner_info(pack_partner_info(PARTNER, 9998, 1))) == (
        PARTNER,
        9998,
        1,
    )
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidPartnerAddress")):
        harness.unpack_partner_info(pack_partner_info(PARTNER, 0, 10))
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidPartnerAddress")):
        harness.unpack_partner_info(pack_partner_info(PARTNER, 0, 0))
