"""OifEscrowAdapter: the standalone OIF escrow settlement adapter behind the
prefix route.

Validation runs end-to-end through the auction's signature router: the
escrow is handed `0x00 ++ adapter ++ abi_encode(order)`, strips its
signature-type byte and lets Permit2 forward the rest to the auction with
the batch witness digest; the harness strips the prefix and forwards the
bare order, the adapter proves the canonical digest and the same-chain
shape the rail needs (pinned output settler as oracle and settler, clean
identifiers, a context resolving to the signed amount), and every economic
decision comes from the auction's shared check_order view. Shape failures
revert with the adapter's OrderNotValid(reason); the auction's typed
check_order revert bubbles unchanged for economic ones. Permit2 is a
domain-only mock here; settlement through the real escrow, output settler
and Permit2 is the fork test's job.
"""

from typing import Any

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak

from tests.burners.conftest import custom_err
from tests.burners.test_oif_module import (
    ORDER_ABI,
    identifier,
    permit_digest_reference,
)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WAD = 10**18

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
AUCTION_LENGTH = 24 * 60 * 60
LOT_AMOUNT = 250 * WAD
NONCE = 0x5AFE_C0DE

PERMIT2_DOMAIN = keccak(b"PERMIT2_TEST_DOMAIN")
FOREIGN_DOMAIN = keccak(b"FOREIGN_DOMAIN")
ERC1271_MAGIC_VALUE = bytes.fromhex("1626ba7e")
ERC1271_INVALID = bytes.fromhex("ffffffff")
SIGNATURE_TYPE_PERMIT2 = b"\x00"
EXCLUSIVE_CONTEXT = b"\xe0" + b"\x11" * 36


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
def settler():
    return boa.env.generate_address("input_settler")


@pytest.fixture(scope="module")
def output_settler():
    return boa.env.generate_address("output_settler")


@pytest.fixture(scope="module")
def proceeds_receiver():
    return boa.env.generate_address("fee_collector")


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
def role_source(owner, emergency_owner):
    return boa.load("contracts/testing/dutch_auction/RoleSourceMock.vy", owner, emergency_owner)


@pytest.fixture(scope="module")
def permit2_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/Permit2Mock.vy")


@pytest.fixture(scope="module")
def permit2(permit2_deployer):
    return permit2_deployer.deploy(PERMIT2_DOMAIN)


@pytest.fixture(scope="module")
def adapter_deployer():
    return boa.load_partial("contracts/burners/adapters/oif/OifEscrowAdapter.vy")


@pytest.fixture
def registry(role_source):
    return boa.load("contracts/burners/adapters/AdapterRegistry.vy", role_source.address)


@pytest.fixture
def adapter(adapter_deployer, settler, output_settler, permit2):
    return adapter_deployer.deploy(settler, output_settler, permit2)


@pytest.fixture
def harness(role_source, want, proceeds_receiver, registry, adapter, owner, permit2):
    harness = boa.load(
        "contracts/testing/dutch_auction/CoreHarness.vy",
        want.address,
        proceeds_receiver,
        registry.address,
        role_source.address,
        START_TOTAL,
        FLOOR_TOTAL,
        STEP_DURATION,
        AUCTION_LENGTH,
    )
    with boa.env.prank(owner):
        # Permit2 pulls the sold tokens for the escrow: it is the executor.
        registry.set_adapter(adapter, permit2.address)
        registry.activate_adapter(adapter)
    # The core fences lots staged in the block that set its economics (the
    # deployment block included): stage from the next second on.
    boa.env.time_travel(seconds=1)
    return harness


@pytest.fixture
def lot(harness, sell_token):
    sell_token._mint_for_testing(harness.address, LOT_AMOUNT)
    harness.stage(sell_token.address)
    return harness.lots(sell_token.address)


@pytest.fixture
def make_order(harness, sell_token, want, proceeds_receiver, output_settler, lot):
    def _make(**overrides) -> tuple:
        amount = overrides.pop("sell_amount", LOT_AMOUNT)
        buy_amount = overrides.pop("buy_amount", None)
        if buy_amount is None:
            buy_amount = harness.getAmountNeeded(sell_token.address, min(amount, LOT_AMOUNT))
        deadline = overrides.pop("fill_deadline", harness.frame_end())
        output = {
            "oracle": identifier(output_settler),
            "settler": identifier(output_settler),
            "chain_id": boa.env.evm.chain.chain_id,
            "token": identifier(want.address),
            "amount": buy_amount,
            "recipient": identifier(proceeds_receiver),
            "callback_data": b"",
            "context": b"",
        }
        for key in list(overrides):
            if key in output:
                output[key] = overrides.pop(key)
        order = {
            "user": harness.address,
            "nonce": NONCE,
            "origin_chain_id": boa.env.evm.chain.chain_id,
            "expires": overrides.pop("expires", deadline),
            "fill_deadline": deadline,
            "input_oracle": output_settler,
            "inputs": [[int(sell_token.address, 16), amount]],
            "outputs": [tuple(output.values())],
        }
        order.update(overrides)
        return tuple(order.values())

    return _make


def _prefix(adapter: Any) -> bytes:
    return bytes.fromhex(str(adapter.address)[2:])


def _bare(order: tuple) -> bytes:
    return encode([ORDER_ABI], [order])


def _signature(adapter: Any, order: tuple) -> bytes:
    """What Permit2 forwards to the auction: the escrow signature without its
    signature-type byte."""
    return _prefix(adapter) + _bare(order)


def _escrow_signature(adapter: Any, order: tuple) -> bytes:
    """What a publisher hands to solvers for openFor / openForAndFinalise."""
    return SIGNATURE_TYPE_PERMIT2 + _signature(adapter, order)


def _validate(harness, adapter, order: tuple, signature: bytes | None = None, domain=None):
    signature = _signature(adapter, order) if signature is None else signature
    domain = PERMIT2_DOMAIN if domain is None else domain
    return harness.isValidSignature(
        permit_digest_reference(order, adapter.settler(), domain), signature
    )


def _normalized(order: Any) -> tuple:
    order = tuple(order)
    outputs = [
        (bytes(o[0]), bytes(o[1]), o[2], bytes(o[3]), o[4], bytes(o[5]), bytes(o[6]), bytes(o[7]))
        for o in order[7]
    ]
    return (*order[:6], [list(pair) for pair in order[6]], outputs)


# Deployment pinning


def test_deploy_pins_settlers_permit2_and_domain(adapter, settler, output_settler, permit2):
    assert adapter.settler() == settler
    assert adapter.output_settler() == output_settler
    assert adapter.permit2() == permit2.address
    assert bytes(adapter.domain_separator()) == PERMIT2_DOMAIN
    assert adapter.ADAPTER_VERSION() == "OifEscrowAdapter"


def test_deploy_rejects_zero_dependencies(adapter_deployer, settler, output_settler, permit2):
    with boa.reverts(custom_err("BadSettler()")):
        adapter_deployer.deploy(ZERO_ADDRESS, output_settler, permit2)
    with boa.reverts(custom_err("BadOutputSettler()")):
        adapter_deployer.deploy(settler, ZERO_ADDRESS, permit2)
    with boa.reverts(custom_err("BadPermit2()")):
        adapter_deployer.deploy(settler, output_settler, ZERO_ADDRESS)


def test_deploy_rejects_missing_domain_separator(
    permit2_deployer, adapter_deployer, settler, output_settler
):
    empty_domain = permit2_deployer.deploy(bytes(32))
    with boa.reverts(custom_err("BadDomainSeparator()")):
        adapter_deployer.deploy(settler, output_settler, empty_domain)


# Routing: the OIF rail is a regular registry adapter behind the prefix


def test_prefixed_bare_order_is_valid_and_unprefixed_is_not(harness, adapter, make_order):
    order = make_order()
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE
    # No fallback route: the unprefixed encoding selects no adapter.
    assert _validate(harness, adapter, order, _bare(order)) == ERC1271_INVALID
    # The escrow's signature-type byte never reaches the auction: with it
    # still in front, the prefix is not an adapter.
    assert _validate(harness, adapter, order, _escrow_signature(adapter, order)) == (
        ERC1271_INVALID
    )


def test_disabled_adapter_is_invalid_not_reverting(
    harness, adapter, make_order, registry, emergency_owner
):
    order = make_order()
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter)
    assert _validate(harness, adapter, order) == ERC1271_INVALID


# Transport encoding


def test_non_canonical_payload_rejected(harness, adapter, make_order):
    order = make_order()
    signature = _signature(adapter, order)
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        _validate(harness, adapter, order, signature + b"\x00")
    with boa.reverts():
        _validate(harness, adapter, order, signature[:-1])
    with boa.reverts():
        _validate(harness, adapter, order, _prefix(adapter))


def test_loose_offsets_rejected(harness, adapter, make_order):
    order = make_order()
    bare = bytearray(_bare(order))
    # Point the callbackData of the output at its (empty) context body: the
    # same decoded order, another byte string. Layout: outer offset word,
    # eight head words, inputs body (1 + 2 words), outputs body (length,
    # one offset, then the output head of eight words).
    output_head = 32 + 8 * 32 + 3 * 32 + 2 * 32
    callback_offset = output_head + 6 * 32
    bare[callback_offset : callback_offset + 32] = (8 * 32 + 32).to_bytes(32, "big")
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        _validate(harness, adapter, order, _prefix(adapter) + bytes(bare))


# Protocol constants and shape


def test_hash_mismatch_invalid(harness, adapter, make_order):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        harness.isValidSignature(keccak(b"other"), _signature(adapter, order))


def test_foreign_domain_separator_invalid(harness, adapter, make_order):
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        _validate(harness, adapter, make_order(), domain=FOREIGN_DOMAIN)


def test_other_spender_invalid(harness, adapter, make_order, output_settler):
    # The digest binds the escrow as Permit2 spender: another escrow's open
    # of the same order is another digest.
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        harness.isValidSignature(
            permit_digest_reference(order, output_settler, PERMIT2_DOMAIN),
            _signature(adapter, order),
        )


def test_other_user_invalid(harness, adapter, make_order, settler):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadUser")):
        _validate(harness, adapter, make_order(user=settler))


def test_other_origin_chain_invalid(harness, adapter, make_order):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadChain")):
        _validate(harness, adapter, make_order(origin_chain_id=137))


def test_empty_inputs_or_outputs_invalid(harness, adapter, make_order):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadInputs")):
        _validate(harness, adapter, make_order(inputs=[]))
    with boa.reverts(custom_err("OrderNotValid(string)", "BadOutputs")):
        _validate(harness, adapter, make_order(outputs=[]))


def test_other_input_oracle_invalid(harness, adapter, make_order, settler):
    # An oracle the adapter does not pin could attest a fill that never paid.
    with boa.reverts(custom_err("OrderNotValid(string)", "BadInputOracle")):
        _validate(harness, adapter, make_order(input_oracle=settler))


def test_other_output_chain_invalid(harness, adapter, make_order):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadOutputChain")):
        _validate(harness, adapter, make_order(chain_id=137))


def test_other_output_settler_or_oracle_invalid(harness, adapter, make_order, settler):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadOutputSettler")):
        _validate(harness, adapter, make_order(settler=identifier(settler)))
    with boa.reverts(custom_err("OrderNotValid(string)", "BadOutputSettler")):
        _validate(harness, adapter, make_order(oracle=identifier(settler)))


def test_dirty_identifiers_invalid(harness, adapter, make_order, sell_token, want):
    dirty = 1 << 160
    with boa.reverts(custom_err("OrderNotValid(string)", "BadIdentifier")):
        _validate(
            harness, adapter, make_order(inputs=[[int(sell_token.address, 16) | dirty, LOT_AMOUNT]])
        )
    with boa.reverts(custom_err("OrderNotValid(string)", "BadIdentifier")):
        _validate(
            harness, adapter, make_order(token=(int(want.address, 16) | dirty).to_bytes(32, "big"))
        )
    with boa.reverts(custom_err("OrderNotValid(string)", "BadIdentifier")):
        _validate(harness, adapter, make_order(recipient=dirty.to_bytes(32, "big")))


def test_callback_invalid(harness, adapter, make_order):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadCallback")):
        _validate(harness, adapter, make_order(callback_data=b"\x01"))


@pytest.mark.parametrize(
    "context",
    [b"\x01" + b"\x00" * 40, b"\x00\x00", b"\xe0" + b"\x11" * 35, b"\xff"],
)
def test_context_not_resolving_to_signed_amount_invalid(harness, adapter, make_order, context):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadContext")):
        _validate(harness, adapter, make_order(context=context))


@pytest.mark.parametrize("context", [b"", b"\x00", EXCLUSIVE_CONTEXT])
def test_signed_amount_contexts_valid(harness, adapter, make_order, context):
    assert _validate(harness, adapter, make_order(context=context)) == ERC1271_MAGIC_VALUE


def test_nonce_passes_through(harness, adapter, make_order):
    assert _validate(harness, adapter, make_order(nonce=0)) == ERC1271_MAGIC_VALUE


@pytest.mark.parametrize("offset", [1, 2**32 - 1, -1])
def test_expires_other_than_the_fill_deadline_invalid(harness, adapter, make_order, offset):
    # Regression: anyone can openFor a burner intent, and the escrow refunds
    # an unfilled one only after expires. An expires far past the fill
    # deadline (2**32 - 1 is year 2106) would park the lot in the escrow with
    # nobody able to fill or refund it.
    deadline = harness.frame_end()
    expires = min(deadline + offset, 2**32 - 1)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadExpires")):
        _validate(harness, adapter, make_order(fill_deadline=deadline, expires=expires))


# Economics: the auction's check_order reverts bubble through the adapter.
# Every branch of check_order is covered directly in test_erc1271_dispatcher.


def test_auction_economics_revert_bubbles_through_adapter(harness, adapter, make_order, sell_token):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    with boa.reverts(custom_err("BadBuyAmount()")):
        _validate(harness, adapter, make_order(buy_amount=quote - 1))
    # Overpriced orders are always fine for the receiver.
    assert _validate(harness, adapter, make_order(buy_amount=quote + 1)) == ERC1271_MAGIC_VALUE


def test_other_recipient_or_token_fails_the_auction_check(
    harness, adapter, make_order, settler, sell_token
):
    with boa.reverts(custom_err("BadReceiver()")):
        _validate(harness, adapter, make_order(recipient=identifier(settler)))
    with boa.reverts(custom_err("BadBuyToken()")):
        _validate(harness, adapter, make_order(token=identifier(sell_token.address)))


def test_fill_deadline_past_the_window_invalid(harness, adapter, make_order):
    with boa.reverts(custom_err("BadValidTo()")):
        _validate(harness, adapter, make_order(fill_deadline=harness.frame_end() + 1))


def test_partial_amount_order_valid(harness, adapter, make_order):
    assert _validate(harness, adapter, make_order(sell_amount=LOT_AMOUNT // 3)) == (
        ERC1271_MAGIC_VALUE
    )


# Publisher helper: order + escrow signature for the burner


def test_order_for_builds_the_publishable_pair(
    harness, adapter, sell_token, want, proceeds_receiver, output_settler, lot
):
    order, signature = adapter.order_for(harness.address, sell_token.address, NONCE)
    order = _normalized(order)
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    assert order[0] == harness.address
    assert order[1] == NONCE
    assert order[2] == boa.env.evm.chain.chain_id
    assert order[3] == order[4] == harness.frame_end()
    assert order[5] == output_settler
    assert order[6] == [[int(sell_token.address, 16), LOT_AMOUNT]]
    assert order[7] == [
        (
            identifier(output_settler),
            identifier(output_settler),
            boa.env.evm.chain.chain_id,
            identifier(want.address),
            quote,
            identifier(proceeds_receiver),
            b"",
            b"",
        )
    ]
    # The signature is the documented template and validates end-to-end
    # once the escrow strips its type byte.
    assert bytes(signature) == _escrow_signature(adapter, order)
    assert _validate(harness, adapter, order, bytes(signature)[1:]) == ERC1271_MAGIC_VALUE

    # A partial amount with a shorter fill deadline is priced proportionally.
    deadline = harness.frame_end() - 3600
    partial, partial_signature = adapter.order_for(
        harness.address, sell_token.address, NONCE + 1, LOT_AMOUNT // 4, deadline
    )
    partial = _normalized(partial)
    assert partial[3] == partial[4] == deadline
    assert partial[6] == [[int(sell_token.address, 16), LOT_AMOUNT // 4]]
    assert partial[7][0][4] == harness.getAmountNeeded(sell_token.address, LOT_AMOUNT // 4)
    assert _validate(harness, adapter, partial, bytes(partial_signature)[1:]) == (
        ERC1271_MAGIC_VALUE
    )


def test_order_for_stays_valid_as_the_curve_decays(harness, adapter, sell_token, lot):
    now = boa.env.evm.vm.state.timestamp
    harness.set_frame(now)
    order, signature = adapter.order_for(harness.address, sell_token.address, NONCE)
    boa.env.time_travel(seconds=10 * STEP_DURATION)
    order = _normalized(order)
    assert harness.getAmountNeeded(sell_token.address, LOT_AMOUNT) < order[7][0][4]
    assert _validate(harness, adapter, order, bytes(signature)[1:]) == ERC1271_MAGIC_VALUE


def test_order_for_rejects_empty_lots_and_bad_deadlines(
    harness, adapter, sell_token, erc20_deployer, lot
):
    with boa.reverts(custom_err("BadFillDeadline()")):
        adapter.order_for(harness.address, sell_token.address, NONCE, 0, harness.frame_end() + 1)
    with boa.reverts(custom_err("BadFillDeadline()")):
        adapter.order_for(
            harness.address, sell_token.address, NONCE, 0, boa.env.evm.vm.state.timestamp
        )
    fresh = erc20_deployer.deploy("Fresh", "FRESH", 18)
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, fresh.address, NONCE)
