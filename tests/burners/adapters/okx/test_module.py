"""Standalone tests for the stateless intent_swap module.

Covers the OKX Intent Swap Order type, its EIP-712 struct hash, digest and
domain against an independent reference, the domain separators recorded from
both production settlements on Ethereum, the struct hash against two real
owner signatures (mainnet fills whose EIP-712 ECDSA signatures recover to the
signed owner), the order uid packing, the receiver resolution, the commission
rules of FeeLib and the encoding bounds. The OKXIntentAdapter built on top
(shape checks and typed reverts) is tested in test_okx_adapter.py; settlement
through the real protocol in test_okx_fork.py.
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
MAX_COMMISSIONS = 3

ORDER_TYPE = (
    "Order(address fromTokenAddress,address toTokenAddress,address owner,address receiver,"
    "uint256 fromTokenAmount,uint256 toTokenAmount,uint32 validTo,bytes32 appData,"
    "bytes32 swapMode,bool partiallyFillable,CommissionInfo[] commissionInfos)"
    "CommissionInfo(uint256 feePercent,address referrerWalletAddress,uint256 flag)"
)
COMMISSION_TYPE = "CommissionInfo(uint256 feePercent,address referrerWalletAddress,uint256 flag)"
DOMAIN_TYPE = "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
ORDER_TYPE_HASH = keccak(text=ORDER_TYPE)
COMMISSION_TYPE_HASH = keccak(text=COMMISSION_TYPE)
DOMAIN_TYPE_HASH = keccak(text=DOMAIN_TYPE)
DOMAIN_NAME = "OKX Intent Swap"
DOMAIN_VERSION = "v1.0.0"
EXACT_IN = keccak(text="exactIn")
BUY_ETH_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
ZERO_ADDRESS = "0x" + "00" * 20
# The signed order: eleven fields, commissions last.
COMMISSION_ABI = "(uint256,address,uint256)"
ORDER_ABI = (
    "(address,address,address,address,uint256,uint256,uint32,bytes32,bytes32,bool,"
    f"{COMMISSION_ABI}[])"
)
COMMISSION_DENOM = 10**9
COMMISSION_MAX_TOTAL_PERCENT = 30_000_000
FROM_TOKEN_COMMISSION = 1 << 255
TO_TOKEN_COMMISSION = 1 << 254
IS_TO_B = 1 << 253
LABEL_OKX = 1 << 248
LABEL_PARENT = 1 << 247
EIP1271_TRADE_FLAGS = 2 << 5

# Production settlements on Ethereum (the same addresses on Arbitrum, Base,
# BSC and X Layer): the legacy schema (settle 0xf1c95699, where the volume
# is) and the threshold schema (settle 0x462c3ed1, version()
# "v2.0.0-threshold-uncapped-surplus"). Both read tokenApproveProxy
# 0x70cBb871E8f30Fc8Ce23609E9E0Ea87B6b222F58, whose tokenApprove() is the OKX
# DEX TokenApprove 0x40aA958dd87FC8305b97f2BA922CDdCa374bcD7f. The separators
# below were recorded by eth_call (domainSeparator()) on 2026-11-02.
LEGACY_SETTLEMENT = "0x25ED72C3f671b626810A6dB597DCFD50F215A423"
THRESHOLD_SETTLEMENT = "0xC75d2cE9d0A2C67b606a7B325c16E8a1B7C15a4A"
TOKEN_APPROVE_PROXY = "0x70cBb871E8f30Fc8Ce23609E9E0Ea87B6b222F58"
TOKEN_APPROVE = "0x40aA958dd87FC8305b97f2BA922CDdCa374bcD7f"
SETTLEMENT_DOMAINS = {
    LEGACY_SETTLEMENT: bytes.fromhex(
        "dba9454a6da847c638b91d3b4eb5968f7b40c3dab44c02ee32023691623fc77f"
    ),
    THRESHOLD_SETTLEMENT: bytes.fromhex(
        "6973a78f3ae083ae97b75f3f210bcaa3267610c9e5d5f2c363487696daa1dd26"
    ),
}

# Two real legacy-settlement fills (Ethereum, 2026-11-02): the owners' 65-byte
# EIP-712 signatures (trade flags 0) over the order digest, so recovering the
# owner from them proves the struct hash, the commission array hashing and
# the domain.
FILLED_ORDERS = [
    {
        "tx": "0xeb1498fe595e87c6d76feb578cf5c24122ed3a42d37efb30ae4fefdc12badaa5",
        "order": (
            to_checksum_address("0xf6b1117ec07684d3958cad8beb1b302bfd21103f"),
            to_checksum_address("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"),
            to_checksum_address("0xd616ba293ccc2ce09c297c28590578d77471ad60"),
            to_checksum_address("0xd616ba293ccc2ce09c297c28590578d77471ad60"),
            555240047519314297,
            226467252,
            1793640312,
            bytes.fromhex("b44dd4943b8f671e3e555b6e0fb8a882fd4c81d2bf2fbe27bf2bc76794d6f1ce"),
            EXACT_IN,
            False,
            # Flags as settled (to-token direction plus OKX's off-chain
            # label bits), taken from the calldata verbatim.
            [
                (
                    1375000,
                    to_checksum_address("0x7797c4a7f786f77dbce13d71d2c710310cb92aa3"),
                    29400335157912315244266070412362164103369332044010299463143527189509193072640,
                ),
                (
                    875000,
                    to_checksum_address("0x567ebbca7c00e3a8e2367c512910a68ca004f434"),
                    29061100521474865452986077292219523748330455135810180623184178798861010075648,
                ),
            ],
        ),
        "digest": bytes.fromhex("3b119630389b75802dee2f4e65f6338d67a7d12cabf88c9096978e916e5df3f2"),
        "signature": bytes.fromhex(
            "fbc07aeabdcdef9a202453748ee1c6322d197fbe178a9b5693ea7e7c533b7347"
            "4c79690d57fe55ffadd209b05654fd2038e8e6d1705195bb0fd35b193d867efd1c"
        ),
    },
    {
        "tx": "0x9e6336c4102a260f08cb8392432fc608fa53a68c11b2786a926606dcb20ab421",
        "order": (
            to_checksum_address("0xdac17f958d2ee523a2206206994597c13d831ec7"),
            BUY_ETH_ADDRESS,
            to_checksum_address("0x1515d766bfe47e6b469dc13462c03d98463e4e6c"),
            to_checksum_address("0x1515d766bfe47e6b469dc13462c03d98463e4e6c"),
            353310000,
            153898167808269054,
            1793640128,
            bytes.fromhex("2db602a44587acd9f0175bbe44a0bebd2607f9ae65ac049cdb7acd15ef2164a7"),
            EXACT_IN,
            False,
            [
                (
                    1000000,
                    to_checksum_address("0x7797c4a7f786f77dbce13d71d2c710310cb92aa3"),
                    29400335157912315244266070412362164103369332044010299463143527189509193072640,
                )
            ],
        ),
        "digest": bytes.fromhex("49b1b0a9515929de46611a44c2f5997f92f81624c41027627d2f7fe6937a4ef0"),
        "signature": bytes.fromhex(
            "5f755db0f019c93d20e68536b3623c30bc25cfe1c400e74140f14f03cfb399d6"
            "0225832a0e9e9b6aa5c1745560e636544d5680b59486a4c244129175d37e67371c"
        ),
    },
]

PROBE_ORDER = (
    "0x1111111111111111111111111111111111111111",
    "0x2222222222222222222222222222222222222222",
    "0x3333333333333333333333333333333333333333",
    "0x4444444444444444444444444444444444444444",
    250 * 10**18,
    100_000 * 10**18,
    1_800_000_000,
    keccak(b"app data"),
    EXACT_IN,
    False,
    [],
)
EMPTY_ORDER = (
    ZERO_ADDRESS,
    ZERO_ADDRESS,
    ZERO_ADDRESS,
    ZERO_ADDRESS,
    0,
    0,
    0,
    bytes(32),
    bytes(32),
    False,
    [],
)
REFERRER = to_checksum_address("0x90a48d5cf7343b08da12e067680b4c6dbfe551be")

HARNESS_SOURCE = """
# pragma version 0.5.0b1

import contracts.burners.adapters.okx.intent_swap as intent_swap


@external
@pure
def domain_separator(_chain_id: uint256, _verifying_contract: address) -> bytes32:
    return intent_swap._domain_separator(_chain_id, _verifying_contract)


@external
@pure
def struct_hash(_order: intent_swap.Order) -> bytes32:
    return intent_swap._struct_hash(_order)


@external
@pure
def order_digest(_order: intent_swap.Order, _domain_separator: bytes32) -> bytes32:
    return intent_swap._order_digest(_order, _domain_separator)


@external
@pure
def order_uid(_digest: bytes32, _owner: address, _valid_to: uint32) -> Bytes[56]:
    return intent_swap._order_uid(_digest, _owner, _valid_to)


@external
@pure
def receiver(_order: intent_swap.Order) -> address:
    return intent_swap._receiver(_order)


@external
@pure
def check_commissions(
    _infos: DynArray[intent_swap.CommissionInfo, intent_swap.MAX_COMMISSIONS]
) -> uint256:
    return intent_swap._check_commissions(_infos)


@external
@pure
def encode(_order: intent_swap.Order) -> Bytes[intent_swap.ENCODED_ORDER_BOUND]:
    return abi_encode(_order)


@external
@pure
def decode(_encoded: Bytes[intent_swap.ENCODED_ORDER_BOUND]) -> intent_swap.Order:
    return abi_decode(_encoded, intent_swap.Order)


@external
@pure
def constants() -> (bytes32, bytes32, bytes32, bytes32, uint256, uint256, uint256, uint256):
    return (
        intent_swap.ORDER_TYPE_HASH,
        intent_swap.COMMISSION_INFO_TYPE_HASH,
        intent_swap.EIP712_DOMAIN_TYPE_HASH,
        intent_swap.EXACT_IN,
        intent_swap.ENCODED_ORDER_BOUND,
        intent_swap.ENCODED_PLAIN_ORDER_LEN,
        intent_swap.UID_LENGTH,
        intent_swap.EIP1271_TRADE_FLAGS,
    )
"""


def domain_separator_reference(chain_id: int, contract: str) -> bytes:
    """Signing._computeDomainSeparator from eth_abi/keccak."""
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


def struct_hash_reference(order: tuple) -> bytes:
    """Independent OrderLib.hash (struct part) model built from eth_abi/keccak."""
    *fields, commissions = order
    hashes = b"".join(
        keccak(encode(["bytes32", "uint256", "address", "uint256"], [COMMISSION_TYPE_HASH, *c]))
        for c in commissions
    )
    return keccak(
        encode(
            [
                "bytes32",
                "address",
                "address",
                "address",
                "address",
                "uint256",
                "uint256",
                "uint32",
                "bytes32",
                "bytes32",
                "bool",
                "bytes32",
            ],
            [ORDER_TYPE_HASH, *fields, keccak(hashes)],
        )
    )


def order_digest_reference(order: tuple, domain_separator: bytes) -> bytes:
    return keccak(b"\x19\x01" + domain_separator + struct_hash_reference(order))


def order_uid_reference(digest: bytes, owner: str, valid_to: int) -> bytes:
    """OrderLib.packOrderUidParams: digest ++ owner ++ validTo."""
    return digest + bytes.fromhex(owner[2:]) + valid_to.to_bytes(4, "big")


def address_from_int(value: int) -> str:
    return to_checksum_address(value.to_bytes(20, "big"))


def as_tuple(order) -> tuple:
    """A boa-returned Order as the reference tuple (bytes32 fields as bytes,
    commissions as tuples)."""
    order = tuple(order)
    return (
        *order[:7],
        bytes(order[7]),
        bytes(order[8]),
        order[9],
        [tuple(c) for c in order[10]],
    )


@pytest.fixture(scope="module")
def harness():
    return boa.loads(HARNESS_SOURCE, name="IntentSwapHarness", filename="IntentSwapHarness.vy")


# Constants and the settlement vectors


def test_constants_match_the_protocol(harness):
    order_th, commission_th, domain_th, exact_in, bound, plain_len, uid_len, flags = (
        harness.constants()
    )
    assert bytes(order_th) == ORDER_TYPE_HASH
    assert (
        ORDER_TYPE_HASH.hex() == "c70e289c8e7a87f3aeb5bfa5b860828276fa66d7f73bc34926b2e0383bfab574"
    )
    assert bytes(commission_th) == COMMISSION_TYPE_HASH
    assert COMMISSION_TYPE_HASH.hex() == (
        "383c49be8b10b90a55f1289bd1243f7250baff231facaad7e486d2391903bf5a"
    )
    assert bytes(domain_th) == DOMAIN_TYPE_HASH
    assert bytes(exact_in) == EXACT_IN
    assert EXACT_IN.hex() == "39124995febcd74fadbdf9bbad44826dab79bc9478b4965fd0d2381c0d649b2c"
    assert bound == 32 + 11 * 32 + 32 + MAX_COMMISSIONS * 96 == 704
    assert plain_len == len(encode([ORDER_ABI], [PROBE_ORDER])) == 416
    assert uid_len == 56
    assert flags == EIP1271_TRADE_FLAGS == 0x40


@pytest.mark.parametrize("settlement", sorted(SETTLEMENT_DOMAINS))
def test_settlement_domain_vectors(harness, settlement):
    expected = SETTLEMENT_DOMAINS[settlement]
    assert bytes(harness.domain_separator(1, settlement)) == expected
    assert domain_separator_reference(1, settlement) == expected


def test_digest_binds_the_domain(harness):
    legacy = SETTLEMENT_DOMAINS[LEGACY_SETTLEMENT]
    threshold = SETTLEMENT_DOMAINS[THRESHOLD_SETTLEMENT]
    assert harness.order_digest(PROBE_ORDER, legacy) != harness.order_digest(PROBE_ORDER, threshold)


@pytest.mark.parametrize("fill", FILLED_ORDERS, ids=[f["tx"][:10] for f in FILLED_ORDERS])
def test_digest_matches_real_owner_signatures(harness, fill):
    domain = SETTLEMENT_DOMAINS[LEGACY_SETTLEMENT]
    digest = bytes(harness.order_digest(fill["order"], domain))
    assert digest == fill["digest"] == order_digest_reference(fill["order"], domain)
    r, s, v = fill["signature"][:32], fill["signature"][32:64], fill["signature"][64]
    signature = keys.Signature(vrs=(v - 27, int.from_bytes(r, "big"), int.from_bytes(s, "big")))
    recovered = signature.recover_public_key_from_msg_hash(digest).to_checksum_address()
    assert recovered == fill["order"][2]


def test_commissions_are_part_of_the_struct(harness):
    with_fee = (*PROBE_ORDER[:10], [(1000, REFERRER, TO_TOKEN_COMMISSION)])
    assert harness.struct_hash(PROBE_ORDER) != harness.struct_hash(with_fee)
    assert bytes(harness.struct_hash(with_fee)) == struct_hash_reference(with_fee)


# Order uid and receiver


def test_order_uid_packs_like_the_protocol(harness):
    digest = keccak(b"digest")
    owner = address_from_int(0xABCDEF)
    uid = bytes(harness.order_uid(digest, owner, 1_800_000_000))
    assert len(uid) == 56
    assert uid == order_uid_reference(digest, owner, 1_800_000_000)
    assert uid[:32] == digest and uid[32:52] == bytes.fromhex(owner[2:])
    assert int.from_bytes(uid[52:], "big") == 1_800_000_000


def test_receiver_resolves_like_the_protocol(harness):
    assert harness.receiver(PROBE_ORDER) == PROBE_ORDER[3]
    to_owner = (*PROBE_ORDER[:3], ZERO_ADDRESS, *PROBE_ORDER[4:])
    assert harness.receiver(to_owner) == PROBE_ORDER[2]


# Encoding


def test_encode_is_the_abi_tuple_encoding_and_round_trips(harness):
    full = (*PROBE_ORDER[:10], [(1, REFERRER, FROM_TOKEN_COMMISSION)] * 3)
    for order in (PROBE_ORDER, EMPTY_ORDER, full, FILLED_ORDERS[0]["order"]):
        encoded = encode([ORDER_ABI], [order])
        assert bytes(harness.encode(order)) == encoded
        assert as_tuple(harness.decode(encoded)) == (*order[:10], [tuple(c) for c in order[10]])
    assert len(encode([ORDER_ABI], [full])) == 704
    with boa.reverts():
        harness.decode(encode([ORDER_ABI], [PROBE_ORDER])[:-1])
    # More commissions than the settlement accepts do not decode.
    too_many = (*PROBE_ORDER[:10], [(1, REFERRER, FROM_TOKEN_COMMISSION)] * 4)
    with boa.reverts():
        harness.decode(encode([ORDER_ABI], [too_many]))


# Digest fuzz against the reference


@st.composite
def orders(draw):
    count = draw(st.integers(0, MAX_COMMISSIONS))
    commissions = [
        (
            draw(st.integers(0, MAX_UINT256)),
            address_from_int(draw(st.integers(0, MASK160))),
            draw(st.integers(0, MAX_UINT256)),
        )
        for _ in range(count)
    ]
    return (
        address_from_int(draw(st.integers(0, MASK160))),
        address_from_int(draw(st.integers(0, MASK160))),
        address_from_int(draw(st.integers(0, MASK160))),
        address_from_int(draw(st.integers(0, MASK160))),
        draw(st.integers(0, MAX_UINT256)),
        draw(st.integers(0, MAX_UINT256)),
        draw(st.integers(0, 2**32 - 1)),
        draw(st.binary(min_size=32, max_size=32)),
        draw(st.binary(min_size=32, max_size=32)),
        draw(st.booleans()),
        commissions,
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


# Commission rules (FeeLib.validateAndComputeFromFees)


def test_commissions_follow_the_protocol_rules(harness):
    assert harness.check_commissions([]) == 0
    assert harness.check_commissions([(1000, REFERRER, TO_TOKEN_COMMISSION | LABEL_OKX)]) == 1000
    # Label bits and zero-fee entries are free; a zero fee needs no recipient.
    assert (
        harness.check_commissions(
            [
                (2_000_000, REFERRER, FROM_TOKEN_COMMISSION | IS_TO_B | LABEL_PARENT),
                (0, ZERO_ADDRESS, TO_TOKEN_COMMISSION),
                (1_000_000, REFERRER, FROM_TOKEN_COMMISSION | IS_TO_B),
            ]
        )
        == 3_000_000
    )
    # The cap is inclusive.
    assert (
        harness.check_commissions(
            [
                (COMMISSION_MAX_TOTAL_PERCENT - 1, REFERRER, TO_TOKEN_COMMISSION),
                (1, REFERRER, TO_TOKEN_COMMISSION),
            ]
        )
        == COMMISSION_MAX_TOTAL_PERCENT
    )


@pytest.mark.parametrize(
    ("infos", "reason"),
    [
        ([(1, REFERRER, 0)], "BadCommissionDirection"),
        ([(1, REFERRER, FROM_TOKEN_COMMISSION | TO_TOKEN_COMMISSION)], "BadCommissionDirection"),
        ([(0, ZERO_ADDRESS, LABEL_OKX)], "BadCommissionDirection"),
        ([(1, ZERO_ADDRESS, TO_TOKEN_COMMISSION)], "ZeroFeeRecipient"),
        ([(COMMISSION_MAX_TOTAL_PERCENT + 1, REFERRER, TO_TOKEN_COMMISSION)], "CommissionTooHigh"),
        ([(MAX_UINT256, REFERRER, TO_TOKEN_COMMISSION)], "CommissionTooHigh"),
        (
            [
                (COMMISSION_MAX_TOTAL_PERCENT, REFERRER, TO_TOKEN_COMMISSION),
                (1, REFERRER, TO_TOKEN_COMMISSION),
            ],
            "CommissionTooHigh",
        ),
        (
            [(1, REFERRER, FROM_TOKEN_COMMISSION), (1, REFERRER, TO_TOKEN_COMMISSION)],
            "MixedCommissionDirections",
        ),
        (
            [(1, REFERRER, TO_TOKEN_COMMISSION | IS_TO_B), (1, REFERRER, TO_TOKEN_COMMISSION)],
            "MixedCommissionFlows",
        ),
    ],
)
def test_commissions_reject_what_the_protocol_rejects(harness, infos, reason):
    with boa.reverts(custom_err("OrderNotValid(string)", reason)):
        harness.check_commissions(infos)
