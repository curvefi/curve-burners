from typing import Any

import boa
import pytest
from eth_abi import encode
from eth_hash.auto import keccak

from .conftest import custom_err


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ZERO_BYTES32 = bytes(32)
MAX_UINT256 = 2**256 - 1
WAD = 10**18
WEEK = 7 * 24 * 3600

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
# Reviewed bound shared with test_dutch_auction_v2: reaches the floor in-frame.
DECAY_FACTOR_RAY = 992031276831159793484252056
LOT_AMOUNT = 400 * WAD

ERC1271_MAGIC = bytes.fromhex("1626ba7e")
INVALID_SIGNATURE = bytes.fromhex("ffffffff")
ENVELOPE_MAGIC = keccak(b"CURVE_DUTCH_AUCTION_ENVELOPE_V1")[:4]
ENVELOPE_VERSION = 1
ENVELOPE_HEADER_LEN = 11

COW_ADAPTER_ID = keccak(b"CURVE_COW_GPV2")[:4]
MOCK_ADAPTER_ID = keccak(b"MOCK_VALIDATOR")[:4]
MODE_NONE = 0
MODE_COW_VAULT_RELAYER = 1

DOMAIN_SEPARATOR = keccak(b"test GPv2 settlement domain")
ORDER_TYPE_HASH = bytes.fromhex("d5a25ba2e97094ad7d83dc28a6572da797d6b3e7fc6663bd93efb789fc17e489")
SELL_KIND = keccak(b"sell")
BUY_KIND = keccak(b"buy")
ERC20_BALANCE = keccak(b"erc20")
EXTERNAL_BALANCE = keccak(b"external")

# Arbitrary protocol digest for the mock-validator path; the mock echoes it.
DIGEST = keccak(b"protocol order digest")

NORMALIZED_ORDER_LEN = 12 * 32
NORMALIZED_ORDER_TYPES = [
    "bytes32",  # recomputed_digest
    "bytes32",  # context_hash
    "uint256",  # auction_epoch
    "address",  # sell_token
    "address",  # buy_token
    "address",  # receiver
    "address",  # verifier
    "address",  # executor
    "uint256",  # sell_amount
    "uint256",  # min_buy_amount
    "uint256",  # valid_to
    "bool",  # partially_fillable
]
NO_DIGEST = 0
NO_CONTEXT = 1
NO_EPOCH = 2
NO_SELL_TOKEN = 3
NO_BUY_TOKEN = 4
NO_RECEIVER = 5
NO_VERIFIER = 6
NO_EXECUTOR = 7
NO_SELL_AMOUNT = 8
NO_MIN_BUY = 9
NO_VALID_TO = 10
NO_PARTIAL = 11

GPV2_ORDER_TYPES = [
    "address",  # sellToken
    "address",  # buyToken
    "address",  # receiver
    "uint256",  # sellAmount
    "uint256",  # buyAmount
    "uint32",  # validTo
    "bytes32",  # appData
    "uint256",  # feeAmount
    "bytes32",  # kind
    "bool",  # partiallyFillable
    "bytes32",  # sellTokenBalance
    "bytes32",  # buyTokenBalance
]
GPV2_SELL_TOKEN = 0
GPV2_BUY_TOKEN = 1
GPV2_RECEIVER = 2
GPV2_SELL_AMOUNT = 3
GPV2_BUY_AMOUNT = 4
GPV2_VALID_TO = 5
GPV2_APP_DATA = 6
GPV2_FEE_AMOUNT = 7
GPV2_KIND = 8
GPV2_PARTIALLY_FILLABLE = 9
GPV2_SELL_BALANCE = 10
GPV2_BUY_BALANCE = 11

# Lot struct tuple indices fixed by the core ABI.
LOT_EPOCH = 0
LOT_INITIAL_AMOUNT = 1
LOT_END = 6


def encode_envelope(adapter_id: bytes, adapter_version: int, payload: bytes) -> bytes:
    return (
        ENVELOPE_MAGIC
        + ENVELOPE_VERSION.to_bytes(1, "big")
        + adapter_id
        + adapter_version.to_bytes(2, "big")
        + payload
    )


def order_digest_reference(order: list, domain_separator: bytes = DOMAIN_SEPARATOR) -> bytes:
    struct_hash = keccak(encode(["bytes32", *GPV2_ORDER_TYPES], [ORDER_TYPE_HASH, *order]))
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def encode_normalized(order: list) -> bytes:
    return encode(NORMALIZED_ORDER_TYPES, order)


def patch_word(response: bytes, index: int, word: bytes) -> bytes:
    """Overwrite one 32-byte word of an encoded response with raw (dirty) bytes."""
    assert len(word) == 32
    return response[: index * 32] + word + response[(index + 1) * 32 :]


def event_name(log: Any) -> str:
    event_type = getattr(log, "event_type", None)
    return event_type.name if event_type is not None else type(log).__name__


def codehash_of(address: str) -> bytes:
    return keccak(boa.env.get_code(address))


@pytest.fixture(autouse=True)
def anchor():
    with boa.env.anchor():
        yield


@pytest.fixture(scope="module")
def owner():
    return boa.env.generate_address("owner")


@pytest.fixture(scope="module")
def emergency_owner():
    return boa.env.generate_address("emergency_owner")


@pytest.fixture(scope="module")
def attacker():
    return boa.env.generate_address("attacker")


@pytest.fixture(scope="module")
def solver():
    return boa.env.generate_address("solver")


@pytest.fixture(scope="module")
def proceeds_receiver():
    return boa.env.generate_address("fee_collector")


@pytest.fixture(scope="module")
def vault_relayer():
    return boa.env.generate_address("vault_relayer")


@pytest.fixture(scope="module")
def mock_verifier():
    return boa.env.generate_address("mock_verifier")


@pytest.fixture(scope="module")
def mock_executor():
    return boa.env.generate_address("mock_executor")


@pytest.fixture(scope="module")
def erc20_deployer():
    return boa.load_partial("contracts/testing/ERC20Mock.vy")


@pytest.fixture(scope="module")
def want(erc20_deployer):
    return erc20_deployer.deploy("Curve Stablecoin", "crvUSD", 18)


@pytest.fixture(scope="module")
def sell_token(erc20_deployer):
    return erc20_deployer.deploy("Curve DAO", "CRV", 18)


@pytest.fixture(scope="module")
def registry(owner, emergency_owner):
    with boa.env.prank(owner):
        return boa.load("contracts/AdapterRegistry.vy", owner, emergency_owner)


@pytest.fixture(scope="module")
def settlement(vault_relayer):
    return boa.load(
        "contracts/testing/dutch_auction/SettlementMock.vy", DOMAIN_SEPARATOR, vault_relayer
    )


@pytest.fixture(scope="module")
def cow_validator(settlement):
    return boa.load("contracts/cow/OrderValidator.vy", settlement.address)


@pytest.fixture(scope="module")
def validator_mock():
    return boa.load("contracts/testing/dutch_auction/OrderValidatorMock.vy")


@pytest.fixture(scope="module")
def harness(owner, emergency_owner, want, proceeds_receiver, registry, vault_relayer):
    with boa.env.prank(owner):
        harness = boa.load(
            "contracts/testing/dutch_auction/CoreHarness.vy",
            want.address,
            proceeds_receiver,
            registry.address,
            ZERO_ADDRESS,  # permit2 unused in dispatcher tests
            START_TOTAL,
            FLOOR_TOTAL,
            DECAY_FACTOR_RAY,
            STEP_DURATION,
        )
        harness.set_emergency_owner(emergency_owner)
        harness.set_cow_router(vault_relayer)
    return harness


@pytest.fixture(scope="module")
def mock_adapter(registry, harness, owner, validator_mock, mock_verifier, mock_executor):
    """Enable the mock validator on both registry and auction (MODE_NONE, partials allowed)."""
    config = (
        validator_mock.address,
        codehash_of(validator_mock.address),
        mock_verifier,
        mock_executor,
        MODE_NONE,
        True,  # allow_partial_fills
        False,  # active is ignored by set_adapter
        1,
    )
    with boa.env.prank(owner):
        registry.set_adapter(MOCK_ADAPTER_ID, config)
        registry.activate_adapter(MOCK_ADAPTER_ID)
        harness.enable_adapter(MOCK_ADAPTER_ID)
    return MOCK_ADAPTER_ID


@pytest.fixture(scope="module")
def cow_adapter(registry, harness, owner, cow_validator, settlement, vault_relayer):
    config = (
        cow_validator.address,
        codehash_of(cow_validator.address),
        settlement.address,  # verifier: the EIP-712 verifying contract
        vault_relayer,  # executor: pulls the sell token
        MODE_COW_VAULT_RELAYER,
        True,  # canonical GPv2 orders are partially fillable
        False,
        1,
    )
    with boa.env.prank(owner):
        registry.set_adapter(COW_ADAPTER_ID, config)
        registry.activate_adapter(COW_ADAPTER_ID)
        harness.enable_adapter(COW_ADAPTER_ID)
    return COW_ADAPTER_ID


@pytest.fixture(scope="module")
def lot(harness, sell_token, mock_adapter, cow_adapter):
    """Stage the weekly lot after both adapters are enabled."""
    sell_token._mint_for_testing(harness.address, LOT_AMOUNT)
    harness.stage(sell_token.address)
    staged = harness.lots(sell_token.address)
    assert staged[LOT_INITIAL_AMOUNT] == LOT_AMOUNT
    # The contract stores no time bounds; extend the record so LOT_START and
    # LOT_END keep indexing the epoch window.
    return (*staged, *harness.epoch_bounds(staged[LOT_EPOCH]))


def make_context_hash(
    harness,
    adapter_id: bytes,
    adapter_version: int,
    lot,
    sell_token,
    want,
    proceeds_receiver,
) -> bytes:
    """Reference recomputation of the core's protocol-hashed replay commitment."""
    return keccak(
        encode(
            [
                "uint256",
                "address",
                "bytes4",
                "uint16",
                "uint256",
                "address",
                "address",
                "address",
                "uint256",
                "uint256",
            ],
            [
                boa.env.evm.patch.chain_id,
                harness.address,
                adapter_id,
                adapter_version,
                lot[LOT_EPOCH],
                sell_token.address,
                want.address,
                proceeds_receiver,
                lot[LOT_INITIAL_AMOUNT],
                lot[LOT_END],
            ],
        )
    )


@pytest.fixture(scope="module")
def mock_context_hash(harness, lot, sell_token, want, proceeds_receiver):
    return make_context_hash(
        harness, MOCK_ADAPTER_ID, 1, lot, sell_token, want, proceeds_receiver
    )


@pytest.fixture(scope="module")
def valid_order(
    harness, lot, sell_token, want, proceeds_receiver, mock_verifier, mock_executor,
    mock_context_hash,
):
    """Factory for a NormalizedOrder list that passes every core check."""

    def _valid_order(
        field_overrides: dict | None = None,
        *,
        sell_amount: int = LOT_AMOUNT // 2,
        min_buy_amount: int | None = None,
        valid_to: int | None = None,
        partially_fillable: bool = True,
    ) -> list:
        if min_buy_amount is None:
            min_buy_amount = harness.quote(sell_token.address, min(sell_amount, LOT_AMOUNT))
        order = [
            DIGEST,
            mock_context_hash,
            lot[LOT_EPOCH],
            sell_token.address,
            want.address,
            proceeds_receiver,
            mock_verifier,
            mock_executor,
            sell_amount,
            min_buy_amount,
            lot[LOT_END] if valid_to is None else valid_to,
            partially_fillable,
        ]
        for field, value in (field_overrides or {}).items():
            order[field] = value
        return order

    return _valid_order


@pytest.fixture(scope="module")
def mock_signature():
    return encode_envelope(MOCK_ADAPTER_ID, 1, b"")


def is_valid(harness, signature: bytes, digest: bytes = DIGEST) -> bytes:
    return bytes(harness.isValidSignature(digest, signature))


# Routing: embedded path


def test_no_magic_routes_to_embedded_path(harness, lot):
    assert bytes(harness.embedded_response()) == INVALID_SIGNATURE
    harness.set_embedded_response(bytes.fromhex("12345678"))
    for signature in (b"", b"\x00" * 100, keccak(b"legacy signature"), ENVELOPE_MAGIC[:3]):
        assert is_valid(harness, signature) == bytes.fromhex("12345678")


def test_magic_prefix_never_falls_back_to_embedded(harness, lot, valid_order, validator_mock,
                                                   mock_signature):
    # Embedded hook would answer magic; a malformed envelope must not reach it.
    harness.set_embedded_response(ERC1271_MAGIC)
    for truncated in range(4, ENVELOPE_HEADER_LEN):
        assert is_valid(harness, mock_signature[:truncated]) == INVALID_SIGNATURE
    # An intact envelope still takes the adapter path only.
    validator_mock.set_order(valid_order())
    assert is_valid(harness, mock_signature) == ERC1271_MAGIC


# Adapter path: envelope and registry gating


def test_happy_path_through_mock_validator(harness, lot, valid_order, validator_mock,
                                           mock_signature, attacker):
    validator_mock.set_order(valid_order())
    assert is_valid(harness, mock_signature) == ERC1271_MAGIC
    # ERC-1271 must accept arbitrary external callers and stay repeatable (view).
    with boa.env.prank(attacker):
        assert is_valid(harness, mock_signature) == ERC1271_MAGIC
    assert is_valid(harness, mock_signature) == ERC1271_MAGIC


def test_unknown_envelope_version_invalid(harness, lot, valid_order, validator_mock):
    validator_mock.set_order(valid_order())
    for version in (0, 2, 255):
        raw = ENVELOPE_MAGIC + version.to_bytes(1, "big") + MOCK_ADAPTER_ID + (1).to_bytes(2, "big")
        assert is_valid(harness, raw) == INVALID_SIGNATURE


def test_unknown_adapter_invalid(harness, lot, valid_order, validator_mock):
    validator_mock.set_order(valid_order())
    unknown = encode_envelope(keccak(b"UNKNOWN_ADAPTER")[:4], 1, b"")
    assert is_valid(harness, unknown) == INVALID_SIGNATURE


def test_disabled_on_auction_invalid(harness, lot, valid_order, validator_mock, mock_signature,
                                     emergency_owner):
    validator_mock.set_order(valid_order())
    assert is_valid(harness, mock_signature) == ERC1271_MAGIC
    with boa.env.prank(emergency_owner):
        harness.disable_adapter(MOCK_ADAPTER_ID)
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


def test_registry_disable_kills_signature_immediately(harness, lot, valid_order, validator_mock,
                                                      mock_signature, registry, emergency_owner):
    validator_mock.set_order(valid_order())
    assert is_valid(harness, mock_signature) == ERC1271_MAGIC
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(MOCK_ADAPTER_ID)
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


def test_adapter_version_mismatch_invalid(harness, lot, valid_order, validator_mock):
    validator_mock.set_order(valid_order())
    for version in (0, 2, 2**16 - 1):
        assert is_valid(harness, encode_envelope(MOCK_ADAPTER_ID, version, b"")) == (
            INVALID_SIGNATURE
        )


def test_validator_codehash_mismatch_invalid(harness, lot, valid_order, validator_mock,
                                             mock_signature):
    validator_mock.set_order(valid_order())
    assert is_valid(harness, mock_signature) == ERC1271_MAGIC
    # Code swapped at the same address (CREATE2-style redeploy) must not validate.
    boa.env.set_code(validator_mock.address, b"\xfe\x60\x00")
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


# Adapter path: validator return-data hardening


def test_validator_revert_is_invalid_not_revert(harness, lot, validator_mock, mock_signature):
    validator_mock.set_revert(True)
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


@pytest.mark.parametrize(
    "raw_length",
    [0, 1, 31, 32, NORMALIZED_ORDER_LEN - 32, NORMALIZED_ORDER_LEN - 1],
)
def test_short_return_data_invalid(harness, lot, validator_mock, mock_signature, raw_length):
    validator_mock.set_raw_response(b"\x11" * raw_length)
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


@pytest.mark.parametrize(
    "extra_length",
    [1, 32, 128],
)
def test_oversized_return_data_invalid(harness, lot, valid_order, validator_mock, mock_signature,
                                       extra_length):
    response = encode_normalized(valid_order()) + b"\x00" * extra_length
    validator_mock.set_raw_response(response)
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


@pytest.mark.parametrize(
    "index",
    [NO_SELL_TOKEN, NO_BUY_TOKEN, NO_RECEIVER, NO_VERIFIER, NO_EXECUTOR],
)
def test_dirty_address_word_invalid(harness, lot, valid_order, validator_mock, mock_signature,
                                    index):
    response = encode_normalized(valid_order())
    dirty = b"\xff" * 12 + response[index * 32 + 12 : (index + 1) * 32]
    validator_mock.set_raw_response(patch_word(response, index, dirty))
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


@pytest.mark.parametrize("word_value", [2, 255, MAX_UINT256])
def test_dirty_bool_word_invalid(harness, lot, valid_order, validator_mock, mock_signature,
                                 word_value):
    response = encode_normalized(valid_order())
    validator_mock.set_raw_response(
        patch_word(response, NO_PARTIAL, word_value.to_bytes(32, "big"))
    )
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


WRITER_VALIDATOR_SOURCE = """
# pragma version 0.5.0a4

from contracts.auction import adapter_types

calls: public(uint256)
response: adapter_types.NormalizedOrder


@external
def set_order(_order: adapter_types.NormalizedOrder):
    self.response = _order


@external
def validate(
    _auction: address, _digest: bytes32, _payload: Bytes[adapter_types.MAX_ADAPTER_PAYLOAD]
) -> adapter_types.NormalizedOrder:
    self.calls += 1
    return self.response
"""


def test_state_writing_validator_is_neutralized_by_staticcall(
    harness, lot, registry, owner, valid_order, validator_mock, sell_token, want,
    proceeds_receiver, mock_verifier, mock_executor
):
    # §13.5: adapters run only under staticcall. This validator would return a
    # fully valid order, but tries to write state first — the dispatcher's
    # static context must turn that into 0xffffffff, never a state change.
    writer = boa.loads(
        WRITER_VALIDATOR_SOURCE,
        name="StateWritingValidatorMock",
        filename="StateWritingValidatorMock.vy",
        no_vvm=True,
    )
    writer_id = keccak(b"STATE_WRITING_ADAPTER")[:4]
    with boa.env.prank(owner):
        registry.set_adapter(
            writer_id,
            (
                writer.address,
                codehash_of(writer.address),
                mock_verifier,
                mock_executor,
                MODE_NONE,
                True,
                False,
                1,
            ),
        )
        registry.activate_adapter(writer_id)
        harness.enable_adapter(writer_id)
    context = make_context_hash(
        harness, writer_id, 1, lot, sell_token, want, proceeds_receiver
    )
    writer.set_order(valid_order({NO_CONTEXT: context}))

    # Outside a static context the same call succeeds and does write.
    writer.validate(harness.address, DIGEST, b"")
    assert writer.calls() == 1

    assert is_valid(harness, encode_envelope(writer_id, 1, b"")) == INVALID_SIGNATURE
    assert writer.calls() == 1


# Adapter path: core economic checks


def test_digest_mismatch_invalid(harness, lot, valid_order, validator_mock, mock_signature):
    validator_mock.set_order(valid_order({NO_DIGEST: keccak(b"another digest")}))
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE
    # A correct returned digest fails against a different supplied hash.
    validator_mock.set_order(valid_order())
    assert is_valid(harness, mock_signature, keccak(b"another digest")) == INVALID_SIGNATURE
    # The mock echoing the supplied digest proves the dispatcher compares
    # against the hash argument, not a constant.
    validator_mock.set_echo_digest(True)
    assert is_valid(harness, mock_signature) == ERC1271_MAGIC


def test_verifier_mismatch_invalid(harness, lot, valid_order, validator_mock, mock_signature,
                                   mock_executor):
    validator_mock.set_order(valid_order({NO_VERIFIER: mock_executor}))
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


def test_executor_mismatch_invalid(harness, lot, valid_order, validator_mock, mock_signature,
                                   mock_verifier):
    validator_mock.set_order(valid_order({NO_EXECUTOR: mock_verifier}))
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


def test_unstaged_token_invalid(harness, lot, valid_order, validator_mock, mock_signature,
                                erc20_deployer):
    stranger = erc20_deployer.deploy("Stranger", "STR", 18)
    validator_mock.set_order(valid_order({NO_SELL_TOKEN: stranger.address}))
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


def test_want_as_sell_token_invalid(harness, lot, valid_order, validator_mock, mock_signature,
                                    want):
    validator_mock.set_order(valid_order({NO_SELL_TOKEN: want.address}))
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


def test_unsellable_token_invalid(harness, lot, valid_order, validator_mock, mock_signature,
                                  sell_token):
    validator_mock.set_order(valid_order())
    harness.set_sellable(sell_token.address, False)
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


def test_cancelled_lot_invalid(harness, lot, valid_order, validator_mock, mock_signature,
                               sell_token):
    validator_mock.set_order(valid_order())
    harness.cancel(sell_token.address, lot[LOT_EPOCH])
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


def test_epoch_rollover_invalidates_signature(harness, lot, valid_order, validator_mock,
                                              mock_signature):
    validator_mock.set_order(valid_order())
    assert is_valid(harness, mock_signature) == ERC1271_MAGIC
    harness.set_frame(harness.frame_start() + WEEK, harness.frame_end() + WEEK)
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


def test_wrong_epoch_invalid(harness, lot, valid_order, validator_mock, mock_signature):
    for epoch in (0, lot[LOT_EPOCH] - 1, lot[LOT_EPOCH] + 1):
        validator_mock.set_order(valid_order({NO_EPOCH: epoch}))
        assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


def test_wrong_buy_token_invalid(harness, lot, valid_order, validator_mock, mock_signature,
                                 sell_token):
    validator_mock.set_order(valid_order({NO_BUY_TOKEN: sell_token.address}))
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


def test_wrong_receiver_invalid(harness, lot, valid_order, validator_mock, mock_signature,
                                attacker):
    validator_mock.set_order(valid_order({NO_RECEIVER: attacker}))
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


def test_zero_sell_amount_invalid(harness, lot, valid_order, validator_mock, mock_signature):
    validator_mock.set_order(valid_order(sell_amount=0, min_buy_amount=0))
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


def test_oversized_sell_amount_invalid(harness, lot, valid_order, validator_mock, mock_signature):
    validator_mock.set_order(
        valid_order(sell_amount=LOT_AMOUNT + 1, min_buy_amount=MAX_UINT256)
    )
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


def test_full_lot_sell_amount_valid(harness, lot, valid_order, validator_mock, mock_signature):
    validator_mock.set_order(valid_order(sell_amount=LOT_AMOUNT))
    assert is_valid(harness, mock_signature) == ERC1271_MAGIC


def test_valid_to_bounds(harness, lot, valid_order, validator_mock, mock_signature):
    now = boa.env.evm.patch.timestamp
    # Expired and beyond-lot-end deadlines are invalid; boundaries hold.
    validator_mock.set_order(valid_order(valid_to=now - 1))
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE
    validator_mock.set_order(valid_order(valid_to=lot[LOT_END] + 1))
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE
    validator_mock.set_order(valid_order(valid_to=now))
    assert is_valid(harness, mock_signature) == ERC1271_MAGIC
    validator_mock.set_order(valid_order(valid_to=lot[LOT_END]))
    assert is_valid(harness, mock_signature) == ERC1271_MAGIC


def test_partial_fill_config_gate(harness, lot, valid_order, validator_mock, mock_signature,
                                  registry, owner, mock_verifier, mock_executor, sell_token,
                                  want, proceeds_receiver):
    # Version 2 of the mock adapter forbids partial fills.
    config = (
        validator_mock.address,
        codehash_of(validator_mock.address),
        mock_verifier,
        mock_executor,
        MODE_NONE,
        False,  # allow_partial_fills
        False,
        2,
    )
    with boa.env.prank(owner):
        registry.set_adapter(MOCK_ADAPTER_ID, config)
        registry.activate_adapter(MOCK_ADAPTER_ID)
    signature = encode_envelope(MOCK_ADAPTER_ID, 2, b"")
    # The replay commitment binds the adapter version, so version 2 orders
    # carry a version-2 context hash.
    context_v2 = make_context_hash(
        harness, MOCK_ADAPTER_ID, 2, lot, sell_token, want, proceeds_receiver
    )

    validator_mock.set_order(valid_order({NO_CONTEXT: context_v2}, partially_fillable=True))
    assert is_valid(harness, signature) == INVALID_SIGNATURE
    validator_mock.set_order(valid_order({NO_CONTEXT: context_v2}, partially_fillable=False))
    assert is_valid(harness, signature) == ERC1271_MAGIC
    # The version-1 commitment does not transfer to the version-2 adapter.
    validator_mock.set_order(valid_order(partially_fillable=False))
    assert is_valid(harness, signature) == INVALID_SIGNATURE


def test_min_buy_amount_below_quote_invalid(harness, lot, valid_order, validator_mock,
                                            mock_signature, sell_token):
    quote = harness.quote(sell_token.address, LOT_AMOUNT // 2)
    validator_mock.set_order(valid_order(min_buy_amount=quote - 1))
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE
    validator_mock.set_order(valid_order(min_buy_amount=quote))
    assert is_valid(harness, mock_signature) == ERC1271_MAGIC


def test_context_hash_mismatch_invalid(harness, lot, valid_order, validator_mock, mock_signature,
                                       sell_token, want, proceeds_receiver):
    validator_mock.set_order(valid_order({NO_CONTEXT: keccak(b"wrong context")}))
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE
    # Context bound to another adapter identity fails too: the commitment
    # includes adapter_id/version, not just the lot.
    foreign = make_context_hash(
        harness, COW_ADAPTER_ID, 1, lot, sell_token, want, proceeds_receiver
    )
    validator_mock.set_order(valid_order({NO_CONTEXT: foreign}))
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


def test_wrong_chain_replay_invalid(harness, lot, valid_order, validator_mock, mock_signature):
    # The commitment binds chain.id: an order valid on this chain must read
    # as invalid when replayed under any other chain id.
    validator_mock.set_order(valid_order())
    assert is_valid(harness, mock_signature) == ERC1271_MAGIC
    original_chain_id = boa.env.evm.patch.chain_id
    try:
        boa.env.evm.patch.chain_id = original_chain_id + 1
        assert is_valid(harness, mock_signature) == INVALID_SIGNATURE
    finally:
        boa.env.evm.patch.chain_id = original_chain_id
    assert is_valid(harness, mock_signature) == ERC1271_MAGIC


def test_partial_fill_total_checked_against_initial_not_remaining(
    harness, lot, valid_order, validator_mock, mock_signature, sell_token, want, solver
):
    # A persistent partially fillable order signs the full weekly total.
    validator_mock.set_order(valid_order(sell_amount=LOT_AMOUNT))
    assert is_valid(harness, mock_signature) == ERC1271_MAGIC

    # Native partial fill: remaining < total, signature must stay valid.
    taken = LOT_AMOUNT // 4
    payment = harness.getAmountNeeded(sell_token.address, taken)
    want._mint_for_testing(solver, payment)
    with boa.env.prank(solver):
        want.approve(harness.address, payment)
        harness.take(sell_token.address, taken, solver, b"")
    assert harness.available(sell_token.address) == LOT_AMOUNT - taken
    assert is_valid(harness, mock_signature) == ERC1271_MAGIC

    # Once the lot is exhausted, available == 0 kills the signature.
    remaining = harness.available(sell_token.address)
    payment = harness.getAmountNeeded(sell_token.address, remaining)
    want._mint_for_testing(solver, payment)
    with boa.env.prank(solver):
        want.approve(harness.address, payment)
        harness.take(sell_token.address, remaining, solver, b"")
    assert harness.available(sell_token.address) == 0
    assert is_valid(harness, mock_signature) == INVALID_SIGNATURE


# CowOrderValidator: deployment and direct validate()


@pytest.fixture(scope="module")
def cow_context_hash(harness, lot, sell_token, want, proceeds_receiver):
    return make_context_hash(harness, COW_ADAPTER_ID, 1, lot, sell_token, want, proceeds_receiver)


@pytest.fixture(scope="module")
def make_gpv2_order(harness, lot, sell_token, want, proceeds_receiver, cow_context_hash):
    def _make_gpv2_order(
        field_overrides: dict | None = None,
        *,
        sell_amount: int = LOT_AMOUNT // 2,
        buy_amount: int | None = None,
        valid_to: int | None = None,
        app_data: bytes | None = None,
    ) -> list:
        if buy_amount is None:
            buy_amount = harness.quote(sell_token.address, min(sell_amount, LOT_AMOUNT))
        order = [
            sell_token.address,
            want.address,
            proceeds_receiver,
            sell_amount,
            buy_amount,
            lot[LOT_END] if valid_to is None else valid_to,
            cow_context_hash if app_data is None else app_data,
            0,
            SELL_KIND,
            True,
            ERC20_BALANCE,
            ERC20_BALANCE,
        ]
        for field, value in (field_overrides or {}).items():
            order[field] = value
        return order

    return _make_gpv2_order


def cow_payload(order: list, epoch: int) -> bytes:
    return encode([*GPV2_ORDER_TYPES, "uint256"], [*order, epoch])


def test_cow_validator_deployment_pins_settlement(cow_validator, settlement, vault_relayer):
    assert bytes(cow_validator.ADAPTER_ID()) == COW_ADAPTER_ID
    assert cow_validator.AUTHORIZATION_MODE() == MODE_COW_VAULT_RELAYER
    assert cow_validator.settlement() == settlement.address
    assert bytes(cow_validator.domain_separator()) == DOMAIN_SEPARATOR
    assert cow_validator.vault_relayer() == vault_relayer


def test_cow_validator_rejects_bad_settlement(vault_relayer):
    with boa.reverts(custom_err("BadSettlement()")):
        boa.load("contracts/cow/OrderValidator.vy", ZERO_ADDRESS)
    dead_settlement = boa.load(
        "contracts/testing/dutch_auction/SettlementMock.vy", ZERO_BYTES32, vault_relayer
    )
    with boa.reverts(custom_err("BadDomainSeparator()")):
        boa.load("contracts/cow/OrderValidator.vy", dead_settlement.address)
    relayerless = boa.load(
        "contracts/testing/dutch_auction/SettlementMock.vy", DOMAIN_SEPARATOR, ZERO_ADDRESS
    )
    with boa.reverts(custom_err("BadVaultRelayer()")):
        boa.load("contracts/cow/OrderValidator.vy", relayerless.address)


def test_cow_validate_normalizes_order(cow_validator, harness, lot, make_gpv2_order, settlement,
                                       vault_relayer, sell_token, want, proceeds_receiver,
                                       cow_context_hash):
    order = make_gpv2_order()
    digest = order_digest_reference(order)
    normalized = cow_validator.validate(
        harness.address, digest, cow_payload(order, lot[LOT_EPOCH])
    )
    assert bytes(normalized.recomputed_digest) == digest
    assert bytes(normalized.context_hash) == cow_context_hash
    assert normalized.auction_epoch == lot[LOT_EPOCH]
    assert normalized.sell_token == sell_token.address
    assert normalized.buy_token == want.address
    assert normalized.receiver == proceeds_receiver
    assert normalized.verifier == settlement.address
    assert normalized.executor == vault_relayer
    assert normalized.sell_amount == order[GPV2_SELL_AMOUNT]
    assert normalized.min_buy_amount == order[GPV2_BUY_AMOUNT]
    assert normalized.valid_to == order[GPV2_VALID_TO]
    assert normalized.partially_fillable


def test_cow_validate_rejects_bad_payload_length(cow_validator, harness, lot, make_gpv2_order):
    order = make_gpv2_order()
    digest = order_digest_reference(order)
    payload = cow_payload(order, lot[LOT_EPOCH])
    for broken in (b"", payload[:-32], payload + b"\x00" * 32):
        with boa.reverts(custom_err("BadPayloadLength()")):
            cow_validator.validate(harness.address, digest, broken)


def test_cow_validate_rejects_non_canonical_payload(cow_validator, harness, lot, make_gpv2_order):
    order = make_gpv2_order()
    digest = order_digest_reference(order)
    payload = cow_payload(order, lot[LOT_EPOCH])
    # Dirty high bits in the sellToken and validTo words must reject, never
    # alias a clean order. For this fully static tuple abi_decode itself
    # already bounds every word; the re-encode assert is defense-in-depth
    # (and load-bearing should the payload ever grow dynamic fields).
    for word in (GPV2_SELL_TOKEN, GPV2_VALID_TO):
        dirty = patch_word(payload, word, b"\xaa" + payload[word * 32 + 1 : (word + 1) * 32])
        with boa.reverts():
            cow_validator.validate(harness.address, digest, dirty)


@pytest.mark.parametrize(
    "index,value,error",
    [
        (GPV2_FEE_AMOUNT, 1, custom_err("BadOrderFlags()")),
        (GPV2_KIND, BUY_KIND, custom_err("BadOrderFlags()")),
        (GPV2_PARTIALLY_FILLABLE, False, custom_err("BadOrderFlags()")),
        (GPV2_SELL_BALANCE, EXTERNAL_BALANCE, custom_err("BadBalanceModes()")),
        (GPV2_BUY_BALANCE, EXTERNAL_BALANCE, custom_err("BadBalanceModes()")),
    ],
)
def test_cow_validate_rejects_bad_flags(cow_validator, harness, lot, make_gpv2_order, index,
                                        value, error):
    order = make_gpv2_order({index: value})
    digest = order_digest_reference(order)
    with boa.reverts(error):
        cow_validator.validate(harness.address, digest, cow_payload(order, lot[LOT_EPOCH]))


def test_cow_validate_rejects_digest_mismatch(cow_validator, harness, lot, make_gpv2_order):
    order = make_gpv2_order()
    payload = cow_payload(order, lot[LOT_EPOCH])
    with boa.reverts(custom_err("DigestMismatch()")):
        cow_validator.validate(harness.address, keccak(b"not the digest"), payload)
    # Same order under another domain separator is a different digest.
    with boa.reverts(custom_err("DigestMismatch()")):
        cow_validator.validate(
            harness.address, order_digest_reference(order, keccak(b"other domain")), payload
        )


# CowOrderValidator: full dispatcher happy path


def cow_signature(order: list, epoch: int) -> bytes:
    return encode_envelope(COW_ADAPTER_ID, 1, cow_payload(order, epoch))


def test_full_happy_path_through_cow_validator(harness, lot, cow_adapter, make_gpv2_order,
                                               solver):
    order = make_gpv2_order()
    digest = order_digest_reference(order)
    with boa.env.prank(solver):
        assert is_valid(harness, cow_signature(order, lot[LOT_EPOCH]), digest) == ERC1271_MAGIC


def test_full_lot_cow_order_valid(harness, lot, cow_adapter, make_gpv2_order):
    order = make_gpv2_order(sell_amount=LOT_AMOUNT)
    digest = order_digest_reference(order)
    assert is_valid(harness, cow_signature(order, lot[LOT_EPOCH]), digest) == ERC1271_MAGIC


@pytest.mark.parametrize(
    "index,delta",
    [
        (GPV2_BUY_AMOUNT, -1),  # underpriced against the curve
        (GPV2_VALID_TO, WEEK),  # deadline beyond lot end
    ],
)
def test_cow_order_economic_violations_invalid(harness, lot, cow_adapter, make_gpv2_order,
                                               index, delta):
    order = make_gpv2_order()
    order[index] += delta
    digest = order_digest_reference(order)
    assert is_valid(harness, cow_signature(order, lot[LOT_EPOCH]), digest) == INVALID_SIGNATURE


def test_cow_order_wrong_app_data_invalid(harness, lot, cow_adapter, make_gpv2_order):
    # appData is the context hash; a foreign commitment cannot validate even
    # though the GPv2 digest itself is consistent.
    order = make_gpv2_order(app_data=keccak(b"wrong context"))
    digest = order_digest_reference(order)
    assert is_valid(harness, cow_signature(order, lot[LOT_EPOCH]), digest) == INVALID_SIGNATURE


def test_cow_order_wrong_epoch_invalid(harness, lot, cow_adapter, make_gpv2_order):
    order = make_gpv2_order()
    digest = order_digest_reference(order)
    assert is_valid(harness, cow_signature(order, lot[LOT_EPOCH] + 1), digest) == (
        INVALID_SIGNATURE
    )


def test_cow_order_tampered_payload_digest_mismatch(harness, lot, cow_adapter, make_gpv2_order):
    # Payload tampering after signing changes the recomputed digest.
    order = make_gpv2_order()
    digest = order_digest_reference(order)
    tampered = list(order)
    tampered[GPV2_RECEIVER] = boa.env.generate_address("thief")
    assert is_valid(harness, cow_signature(tampered, lot[LOT_EPOCH]), digest) == (
        INVALID_SIGNATURE
    )


# Adapter set management


def test_enable_adapter_only_owner(harness, registry, attacker, emergency_owner):
    for non_owner in (attacker, emergency_owner):
        with boa.env.prank(non_owner):
            with boa.reverts(custom_err("OnlyOwner()")):
                harness.enable_adapter(keccak(b"SOME_ADAPTER")[:4])


def test_enable_adapter_requires_known_active_config(harness, registry, owner, validator_mock,
                                                     mock_verifier, mock_executor):
    fresh_id = keccak(b"FRESH_ADAPTER")[:4]
    with boa.env.prank(owner):
        with boa.reverts(custom_err("UnknownAdapter()")):
            harness.enable_adapter(fresh_id)
        registry.set_adapter(
            fresh_id,
            (
                validator_mock.address,
                codehash_of(validator_mock.address),
                mock_verifier,
                mock_executor,
                MODE_NONE,
                True,
                False,
                1,
            ),
        )
        # Registered but not activated: still not enableable.
        with boa.reverts(custom_err("InactiveAdapter()")):
            harness.enable_adapter(fresh_id)
        registry.activate_adapter(fresh_id)
        harness.enable_adapter(fresh_id)
        with boa.reverts(custom_err("AlreadyEnabled()")):
            harness.enable_adapter(fresh_id)
    assert harness.enabled_adapters(fresh_id)


def test_enable_adapter_requires_registry(owner, want, proceeds_receiver):
    with boa.env.prank(owner):
        registryless = boa.load(
            "contracts/testing/dutch_auction/CoreHarness.vy",
            want.address,
            proceeds_receiver,
            ZERO_ADDRESS,
            ZERO_ADDRESS,
            START_TOTAL,
            FLOOR_TOTAL,
            DECAY_FACTOR_RAY,
            STEP_DURATION,
        )
        with boa.reverts(custom_err("NoRegistry()")):
            registryless.enable_adapter(MOCK_ADAPTER_ID)


@pytest.mark.parametrize("role", ["owner", "emergency_owner"])
def test_disable_adapter_owner_or_emergency(harness, mock_adapter, owner, emergency_owner, role):
    disabler = owner if role == "owner" else emergency_owner
    with boa.env.prank(disabler):
        harness.disable_adapter(MOCK_ADAPTER_ID)
    assert not harness.enabled_adapters(MOCK_ADAPTER_ID)
    assert harness.adapter_router(MOCK_ADAPTER_ID) == ZERO_ADDRESS


def test_disable_adapter_rejects_outsider_and_not_enabled(harness, mock_adapter, attacker,
                                                          owner):
    with boa.env.prank(attacker):
        with boa.reverts(custom_err("OnlyOwner()")):
            harness.disable_adapter(MOCK_ADAPTER_ID)
    with boa.env.prank(owner):
        with boa.reverts(custom_err("NotEnabled()")):
            harness.disable_adapter(keccak(b"NEVER_ENABLED")[:4])


def test_adapter_refcount_lifecycle(harness, mock_adapter, cow_adapter, owner, vault_relayer):
    # MODE_NONE holds no router; MODE_COW_VAULT_RELAYER pins the relayer.
    assert harness.adapter_router(MOCK_ADAPTER_ID) == ZERO_ADDRESS
    assert harness.adapter_router(COW_ADAPTER_ID) == vault_relayer
    assert harness.router_refcount(vault_relayer) == 1

    with boa.env.prank(owner):
        harness.disable_adapter(COW_ADAPTER_ID)
    assert harness.router_refcount(vault_relayer) == 0
    assert harness.adapter_router(COW_ADAPTER_ID) == ZERO_ADDRESS

    # Re-enabling resolves the router again and retakes the reference.
    with boa.env.prank(owner):
        harness.enable_adapter(COW_ADAPTER_ID)
    assert harness.router_refcount(vault_relayer) == 1
    assert harness.adapter_router(COW_ADAPTER_ID) == vault_relayer
