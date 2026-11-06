"""VeloraDeltaAdapter: the standalone Velora Delta settlement adapter behind
the prefix route.

Validation runs end-to-end through the auction's signature router: an agent
settles with signature `adapter ++ abi_encode(order)`, the harness strips the
prefix and forwards the bare order, the adapter proves the canonical digest
under either Delta primary type and the shape the rail needs, and every
economic decision comes from the auction's shared check_order view, priced
at the floor the beneficiary actually receives after the settlement's fees.
Shape failures revert with the adapter's OrderNotValid(reason); the
auction's typed check_order revert bubbles unchanged for economic ones.
Settlement is exercised against DeltaSettlementMock (swapSettle and
swapSettleFillable with the real fee arithmetic); the real protocol is the
fork test's job.
"""

from typing import Any

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak

from tests.burners.conftest import custom_err
from tests.burners.test_velora_delta_module import (
    EMPTY_BRIDGE,
    ORDER_ABI,
    PARTNER,
    domain_separator_reference,
    order_digest_reference,
    pack_partner_and_fee,
)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WAD = 10**18
MAX_UINT256 = 2**256 - 1

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
AUCTION_LENGTH = 24 * 60 * 60
LOT_AMOUNT = 250 * WAD
NONCE = 0x5AFE_C0DE
HUNDRED_PERCENT = 10_000

FOREIGN_DOMAIN = keccak(b"FOREIGN_DOMAIN")
ERC1271_MAGIC_VALUE = bytes.fromhex("1626ba7e")
ERC1271_INVALID = bytes.fromhex("ffffffff")


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
def agent():
    return boa.env.generate_address("agent")


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
def settlement_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/DeltaSettlementMock.vy")


@pytest.fixture(scope="module")
def settlement(settlement_deployer, agent):
    settlement = settlement_deployer.deploy(bytes(32))
    settlement.set_agent(agent, True)
    return settlement


@pytest.fixture(scope="module")
def executor():
    return boa.load("contracts/testing/dutch_auction/DeltaExecutorMock.vy")


@pytest.fixture(scope="module")
def adapter_deployer():
    return boa.load_partial("contracts/burners/adapters/velora/VeloraDeltaAdapter.vy")


@pytest.fixture
def registry(role_source):
    return boa.load("contracts/burners/adapters/AdapterRegistry.vy", role_source.address)


@pytest.fixture
def adapter(adapter_deployer, settlement):
    return adapter_deployer.deploy(settlement)


@pytest.fixture
def harness(role_source, want, proceeds_receiver, registry, adapter, owner, settlement):
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
        # The Delta contract pulls the sold tokens itself: it is the executor.
        registry.set_adapter(adapter, settlement.address)
        registry.activate_adapter(adapter)
    # The core fences lots staged in the block that set its economics (the
    # deployment block included): stage from the next second on.
    boa.env.time_travel(seconds=1)
    return harness


@pytest.fixture
def lot(harness, sell_token, settlement):
    sell_token._mint_for_testing(harness.address, LOT_AMOUNT)
    harness.stage(sell_token.address)
    harness.sync_executor_approvals(settlement.address, [sell_token.address])
    return harness.lots(sell_token.address)


@pytest.fixture
def domain(settlement):
    return domain_separator_reference(boa.env.evm.chain.chain_id, settlement.address)


@pytest.fixture
def make_order(harness, sell_token, want, proceeds_receiver, lot):
    def _make(**overrides) -> tuple:
        amount = overrides.pop("src_amount", LOT_AMOUNT)
        dest_amount = overrides.pop("dest_amount", None)
        if dest_amount is None:
            dest_amount = harness.getAmountNeeded(sell_token.address, min(amount, LOT_AMOUNT))
        expected_amount = overrides.pop("expected_amount", dest_amount)
        order = {
            "owner": harness.address,
            "beneficiary": proceeds_receiver,
            "src_token": sell_token.address,
            "dest_token": want.address,
            "src_amount": amount,
            "dest_amount": dest_amount,
            "expected_amount": expected_amount,
            "deadline": harness.frame_end(),
            "kind": 0,
            "nonce": NONCE,
            "partner_and_fee": 0,
            "permit": b"",
            "metadata": b"",
            "bridge": EMPTY_BRIDGE,
        }
        order.update(overrides)
        return tuple(order.values())

    return _make


def _prefix(adapter: Any) -> bytes:
    return bytes.fromhex(str(adapter.address)[2:])


def _bare(order: tuple) -> bytes:
    return encode([ORDER_ABI], [order])


def _signature(adapter: Any, order: tuple) -> bytes:
    """What an agent passes as OrderWithSig.signature."""
    return _prefix(adapter) + _bare(order)


def _validate(
    harness, adapter, order: tuple, signature: bytes | None = None, domain=None, fillable=False
):
    signature = _signature(adapter, order) if signature is None else signature
    if domain is None:
        domain = domain_separator_reference(boa.env.evm.chain.chain_id, adapter.settlement())
    return harness.isValidSignature(order_digest_reference(order, domain, fillable), signature)


def _order_with_sig(order: tuple, signature: bytes) -> tuple:
    return (order, signature, (bytes(4), b""), b"")


def _executor_data(token: Any, amount: int) -> bytes:
    return encode(["address", "uint256"], [token.address, amount])


def _settle(settlement, agent, executor, want, order: tuple, signature: bytes, pay: int) -> int:
    want._mint_for_testing(executor.address, pay)
    with boa.env.prank(agent):
        return settlement.swapSettle(
            _order_with_sig(order, signature), _executor_data(want, pay), executor.address, b""
        )


def _settle_fillable(
    settlement, agent, executor, want, order: tuple, signature: bytes, percent: int, pay: int
) -> int:
    want._mint_for_testing(executor.address, pay)
    with boa.env.prank(agent):
        return settlement.swapSettleFillable(
            _order_with_sig(order, signature),
            _executor_data(want, pay),
            executor.address,
            percent,
            b"",
        )


# Deployment pinning


def test_deploy_pins_settlement_and_domain(adapter, settlement, domain):
    assert adapter.settlement() == settlement.address
    assert bytes(adapter.domain_separator()) == domain
    assert bytes(settlement.DOMAIN_SEPARATOR()) == domain
    assert adapter.ADAPTER_VERSION() == "VeloraDeltaAdapter"


def test_deploy_rejects_zero_settlement(adapter_deployer):
    with boa.reverts(custom_err("BadSettlement()")):
        adapter_deployer.deploy(ZERO_ADDRESS)


def test_deploy_rejects_foreign_domain(settlement_deployer, adapter_deployer):
    # A settlement whose domain is not Portikus/2.0.0 on this chain at its
    # own address (another version, a proxy, a fork) fails the cross-check.
    foreign = settlement_deployer.deploy(FOREIGN_DOMAIN)
    with boa.reverts(custom_err("BadDomain()")):
        adapter_deployer.deploy(foreign)


# Routing: the Delta rail is a regular registry adapter behind the prefix


def test_prefixed_bare_order_is_valid_under_both_primary_types(harness, adapter, make_order):
    order = make_order()
    assert _validate(harness, adapter, order, fillable=False) == ERC1271_MAGIC_VALUE
    assert _validate(harness, adapter, order, fillable=True) == ERC1271_MAGIC_VALUE
    # No fallback route: the unprefixed encoding selects no adapter.
    assert _validate(harness, adapter, order, _bare(order)) == ERC1271_INVALID


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
    # Trailing bytes decode but are not the order's encoding.
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        _validate(harness, adapter, order, signature + b"\x00")
    # Truncation fails ABI decoding before any check runs.
    with boa.reverts():
        _validate(harness, adapter, order, signature[:-1])
    with boa.reverts():
        _validate(harness, adapter, order, _prefix(adapter))


def test_loose_offsets_rejected(harness, adapter, make_order):
    order = make_order()
    bare = bytearray(_bare(order))
    # Point the permit at the (empty) metadata body: the same decoded order,
    # another byte string. The head follows the outer tuple offset word.
    permit_offset = 32 + 11 * 32
    bare[permit_offset : permit_offset + 32] = (14 * 32 + 32).to_bytes(32, "big")
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        _validate(harness, adapter, order, _prefix(adapter) + bytes(bare))


# Protocol constants and shape


def test_hash_mismatch_invalid(harness, adapter, make_order):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        harness.isValidSignature(keccak(b"other"), _signature(adapter, order))


def test_foreign_domain_separator_invalid(harness, adapter, make_order):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        _validate(harness, adapter, order, domain=FOREIGN_DOMAIN)


def test_other_owner_invalid(harness, adapter, make_order, agent):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadOwner")):
        _validate(harness, adapter, make_order(owner=agent))


def test_buy_order_invalid(harness, adapter, make_order):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadOrderKind")):
        _validate(harness, adapter, make_order(kind=1))


@pytest.mark.parametrize("permit", [b"\x01", b"\x01" * 96, b"\x01" * 192, b"\x01" * 256])
def test_permit_invalid(harness, adapter, make_order, permit):
    # Any permit payload routes the pull through Permit2 (or skips it): never
    # the allowance the auction grants the Delta contract.
    with boa.reverts(custom_err("OrderNotValid(string)", "BadPermit")):
        _validate(harness, adapter, make_order(permit=permit))


@pytest.mark.parametrize(
    "bridge",
    [
        (bytes(4), 137, ZERO_ADDRESS, 0, b""),
        (b"\x12\x34\x56\x78", 0, ZERO_ADDRESS, 0, b""),
        (bytes(4), 0, PARTNER, 0, b""),
        (bytes(4), 0, ZERO_ADDRESS, 1, b""),
        (bytes(4), 0, ZERO_ADDRESS, 0, b"\x00"),
    ],
)
def test_bridge_invalid(harness, adapter, make_order, bridge):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadBridge")):
        _validate(harness, adapter, make_order(bridge=bridge))


def test_same_chain_bridge_id_still_invalid(harness, adapter, make_order):
    # isBridge would be false for this chain's id, but the shape stays strict.
    bridge = (bytes(4), boa.env.evm.chain.chain_id, ZERO_ADDRESS, 0, b"")
    with boa.reverts(custom_err("OrderNotValid(string)", "BadBridge")):
        _validate(harness, adapter, make_order(bridge=bridge))


def test_expected_below_dest_invalid(harness, adapter, make_order, sell_token, harness_quote=None):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadExpectedAmount")):
        _validate(harness, adapter, make_order(dest_amount=quote, expected_amount=quote - 1))
    # Above is fine: the surplus split only starts above expectedAmount.
    assert (
        _validate(harness, adapter, make_order(dest_amount=quote, expected_amount=quote * 2))
        == ERC1271_MAGIC_VALUE
    )


def test_metadata_and_nonce_pass_through(harness, adapter, make_order):
    order = make_order(metadata=b"\x01" * 256, nonce=0)
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE


# Economics: the auction's check_order reverts bubble through the adapter.
# Every branch of check_order is covered directly in test_erc1271_dispatcher.


def test_auction_economics_revert_bubbles_through_adapter(harness, adapter, make_order, sell_token):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    with boa.reverts(custom_err("BadBuyAmount()")):
        _validate(harness, adapter, make_order(dest_amount=quote - 1))
    # Overpriced orders are always fine for the receiver.
    assert _validate(harness, adapter, make_order(dest_amount=quote + 1)) == ERC1271_MAGIC_VALUE


def test_zero_beneficiary_means_the_owner_and_fails_as_receiver(harness, adapter, make_order):
    with boa.reverts(custom_err("BadReceiver()")):
        _validate(harness, adapter, make_order(beneficiary=ZERO_ADDRESS))


def test_deadline_past_the_window_invalid(harness, adapter, make_order):
    with boa.reverts(custom_err("BadValidTo()")):
        _validate(harness, adapter, make_order(deadline=harness.frame_end() + 1))


def test_partial_fill_order_valid(harness, adapter, make_order):
    assert _validate(harness, adapter, make_order(src_amount=LOT_AMOUNT // 3)) == (
        ERC1271_MAGIC_VALUE
    )


def test_partner_fee_is_netted_out_of_the_floor(harness, adapter, make_order, sell_token):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    # A 2% partner fee comes off the top: destAmount must cover it.
    with_fee = pack_partner_and_fee(PARTNER, 200)
    with boa.reverts(custom_err("BadBuyAmount()")):
        _validate(harness, adapter, make_order(dest_amount=quote, partner_and_fee=with_fee))
    grossed_up = -(-quote * HUNDRED_PERCENT // (HUNDRED_PERCENT - 200))
    assert grossed_up - grossed_up * 200 // HUNDRED_PERCENT >= quote
    assert (
        _validate(harness, adapter, make_order(dest_amount=grossed_up, partner_and_fee=with_fee))
        == ERC1271_MAGIC_VALUE
    )
    # Surplus flags alone never touch the floor.
    flags_only = pack_partner_and_fee(PARTNER, 0, True, True)
    assert (
        _validate(harness, adapter, make_order(dest_amount=quote, partner_and_fee=flags_only))
        == ERC1271_MAGIC_VALUE
    )


# Publisher helper: order + signature for the burner


def test_order_for_builds_the_publishable_pair(
    harness, adapter, sell_token, want, proceeds_receiver, lot
):
    order, signature = adapter.order_for(harness.address, sell_token.address, NONCE)
    order = tuple(order)
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    assert order[0] == harness.address
    assert order[1] == proceeds_receiver
    assert order[2] == sell_token.address
    assert order[3] == want.address
    assert order[4] == LOT_AMOUNT
    assert order[5] == quote
    assert order[6] == quote
    assert order[7] == harness.frame_end()
    assert order[8] == 0
    assert order[9] == NONCE
    assert order[10] == 0
    assert bytes(order[11]) == b""
    assert bytes(order[12]) == b""
    bridge = tuple(order[13])
    assert (bytes(bridge[0]), *bridge[1:4], bytes(bridge[4])) == EMPTY_BRIDGE
    # The signature is the documented template and validates end-to-end
    # under both primary types.
    normalized = (*order[:11], b"", b"", EMPTY_BRIDGE)
    assert bytes(signature) == _signature(adapter, normalized)
    for fillable in (False, True):
        assert _validate(harness, adapter, normalized, fillable=fillable) == ERC1271_MAGIC_VALUE

    # A partial amount is priced proportionally and validates as well.
    partial, partial_signature = adapter.order_for(
        harness.address, sell_token.address, NONCE + 1, LOT_AMOUNT // 4
    )
    assert partial[4] == LOT_AMOUNT // 4
    assert partial[5] == harness.getAmountNeeded(sell_token.address, LOT_AMOUNT // 4)
    partial_normalized = (*tuple(partial)[:11], b"", b"", EMPTY_BRIDGE)
    assert _validate(harness, adapter, partial_normalized) == ERC1271_MAGIC_VALUE


def test_order_for_stays_valid_as_the_curve_decays(harness, adapter, sell_token, lot):
    # A published order is a standing ask: once the curve falls below it, it
    # is still accepted (overpriced for us is fine), so publishers re-post
    # lower rather than cancel.
    now = boa.env.evm.vm.state.timestamp
    harness.set_frame(now)
    order, signature = adapter.order_for(harness.address, sell_token.address, NONCE)
    boa.env.time_travel(seconds=10 * STEP_DURATION)
    assert harness.getAmountNeeded(sell_token.address, LOT_AMOUNT) < order[5]
    normalized = (*tuple(order)[:11], b"", b"", EMPTY_BRIDGE)
    assert _validate(harness, adapter, normalized) == ERC1271_MAGIC_VALUE


def test_order_for_rejects_empty_lots(harness, adapter, sell_token, erc20_deployer):
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, sell_token.address, NONCE)
    fresh = erc20_deployer.deploy("Fresh", "FRESH", 18)
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, fresh.address, NONCE)


def test_describe_unpacks_partner_and_fee(adapter):
    assert tuple(adapter.describe(pack_partner_and_fee(PARTNER, 7, True, False))) == (
        PARTNER,
        7,
        True,
        False,
    )


# Settlement through the mock: the token flows the registry entry implies


def test_swap_settle_fills_the_whole_order(
    harness, adapter, settlement, agent, executor, sell_token, want, proceeds_receiver, lot
):
    order, signature = adapter.order_for(harness.address, sell_token.address, NONCE)
    order = (*tuple(order)[:11], b"", b"", EMPTY_BRIDGE)
    quote = order[5]
    # The agent overpays: the surplus above expectedAmount is split with
    # the protocol, the beneficiary still receives at least the floor.
    pay = quote + 1000
    paid = _settle(settlement, agent, executor, want, order, bytes(signature), pay)
    assert paid == pay - 1000 // 2
    assert want.balanceOf(proceeds_receiver) == paid >= quote
    assert sell_token.balanceOf(executor) == LOT_AMOUNT
    assert sell_token.balanceOf(harness) == 0
    assert settlement.fees(ZERO_ADDRESS, want.address) == 1000 // 2
    assert settlement.isNonceUsed(harness.address, NONCE)
    # Burnt nonce: the same order never settles twice.
    with boa.reverts(custom_err("InvalidNonce()")):
        _settle(settlement, agent, executor, want, order, bytes(signature), pay)


def test_swap_settle_rejects_an_underpaying_executor(
    harness, adapter, settlement, agent, executor, sell_token, want, lot
):
    order, signature = adapter.order_for(harness.address, sell_token.address, NONCE)
    order = (*tuple(order)[:11], b"", b"", EMPTY_BRIDGE)
    with boa.reverts(custom_err("InsufficientReturnAmount()")):
        _settle(settlement, agent, executor, want, order, bytes(signature), order[5] - 1)


def test_swap_settle_fillable_fills_pro_rata_and_burns_the_nonce_at_the_end(
    harness, adapter, settlement, agent, executor, sell_token, want, proceeds_receiver, lot
):
    order, signature = adapter.order_for(harness.address, sell_token.address, NONCE)
    order = (*tuple(order)[:11], b"", b"", EMPTY_BRIDGE)
    quote = order[5]
    quarter_out = quote * 2500 // HUNDRED_PERCENT
    paid = _settle_fillable(
        settlement, agent, executor, want, order, bytes(signature), 2500, quarter_out
    )
    assert paid == quarter_out
    assert sell_token.balanceOf(harness) == LOT_AMOUNT - LOT_AMOUNT // 4
    assert settlement.swapFilledAmount(order) == LOT_AMOUNT // 4
    assert not settlement.isNonceUsed(harness.address, NONCE)
    # The signature is re-checked on every slice against the live curve.
    rest_out = quote * 7500 // HUNDRED_PERCENT
    _settle_fillable(settlement, agent, executor, want, order, bytes(signature), 7500, rest_out)
    assert sell_token.balanceOf(harness) == 0
    assert want.balanceOf(proceeds_receiver) == quarter_out + rest_out
    assert settlement.isNonceUsed(harness.address, NONCE)
    with boa.reverts(custom_err("InvalidNonce()")):
        _settle_fillable(settlement, agent, executor, want, order, bytes(signature), 1, 1)


def test_settlement_bubbles_the_auction_revert_and_reports_a_disabled_adapter(
    harness,
    adapter,
    settlement,
    agent,
    executor,
    sell_token,
    want,
    registry,
    emergency_owner,
    make_order,
):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    underpriced = make_order(dest_amount=quote - 1)
    with boa.reverts(custom_err("BadBuyAmount()")):
        _settle(
            settlement, agent, executor, want, underpriced, _signature(adapter, underpriced), quote
        )
    order = make_order()
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter)
    with boa.reverts(custom_err("InvalidSignature()")):
        _settle(settlement, agent, executor, want, order, _signature(adapter, order), quote)


def test_only_registered_agents_settle(
    harness, adapter, settlement, executor, sell_token, want, make_order
):
    order = make_order()
    outsider = boa.env.generate_address("outsider")
    with boa.reverts(custom_err("UnauthorizedAgent()")):
        _settle(settlement, outsider, executor, want, order, _signature(adapter, order), order[5])
