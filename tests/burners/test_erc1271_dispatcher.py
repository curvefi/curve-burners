"""Prefix-based ERC-1271 signature router and the shared check_order view.

Routing is sender-agnostic: a signature whose first 20 bytes name an active
registry adapter is forwarded with the prefix stripped; anything else is
invalid. The AdapterRegistry is the only switch — the auction keeps no adapter
state of its own — and is reread live on every call. Economic authority never
leaves the auction: adapters call check_order, which returns True for a
fillable order and reverts with a typed auction error otherwise, covered
here on the core harness.
"""

from typing import Any

import boa
import pytest

from .conftest import custom_err


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WAD = 10**18
WEEK = 7 * 24 * 60 * 60

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
# Floor reached by the last active second of a day: 1439 sixty-second steps.
AUCTION_LENGTH = 24 * 60 * 60
LOT_AMOUNT = 250 * WAD

ERC1271_MAGIC_VALUE = bytes.fromhex("1626ba7e")
ERC1271_INVALID = bytes.fromhex("ffffffff")

DIGEST = boa.util.abi.Address("0x" + "11" * 20).canonical_address + b"\x22" * 12


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
def solver():
    return boa.env.generate_address("solver")


@pytest.fixture(scope="module")
def proceeds_receiver():
    return boa.env.generate_address("fee_collector")


@pytest.fixture(scope="module")
def executor():
    return boa.env.generate_address("executor")


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
    return boa.load(
        "contracts/testing/dutch_auction/RoleSourceMock.vy", owner, emergency_owner
    )


@pytest.fixture(scope="module")
def adapter_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/AdapterMock.vy")


@pytest.fixture
def registry(role_source):
    return boa.load(
        "contracts/burners/auction/adapters/AdapterRegistry.vy", role_source.address
    )


def _deploy_harness(want, proceeds_receiver, registry_address, role_source):
    return boa.load(
        "contracts/testing/dutch_auction/CoreHarness.vy",
        want.address,
        proceeds_receiver,
        registry_address,
        role_source.address,
        START_TOTAL,
        FLOOR_TOTAL,
        STEP_DURATION,
        AUCTION_LENGTH,
    )


@pytest.fixture
def harness(role_source, want, proceeds_receiver, registry):
    return _deploy_harness(want, proceeds_receiver, registry.address, role_source)


@pytest.fixture
def adapter(adapter_deployer, registry, owner, executor):
    """Registered and active adapter: the only state routing depends on."""
    adapter = adapter_deployer.deploy()
    with boa.env.prank(owner):
        registry.set_adapter(adapter, executor)
        registry.activate_adapter(adapter)
    return adapter


@pytest.fixture
def lot(harness, sell_token):
    sell_token._mint_for_testing(harness.address, LOT_AMOUNT)
    harness.stage(sell_token.address)
    return harness.lots(sell_token.address)


def _prefix(adapter: Any) -> bytes:
    return bytes.fromhex(str(adapter.address)[2:])


# Routing


def test_prefixed_signature_routes_to_active_adapter(harness, adapter):
    assert (
        harness.isValidSignature(DIGEST, _prefix(adapter) + b"payload")
        == ERC1271_MAGIC_VALUE
    )


def test_prefix_only_signature_forwards_empty_payload(harness, adapter):
    assert harness.isValidSignature(DIGEST, _prefix(adapter)) == ERC1271_MAGIC_VALUE


def test_adapter_answer_is_passed_through_verbatim(harness, adapter):
    adapter.set_response(bytes.fromhex("deadbeef"))
    assert harness.isValidSignature(DIGEST, _prefix(adapter)) == bytes.fromhex(
        "deadbeef"
    )


def test_adapter_revert_bubbles_up(harness, adapter):
    adapter.set_revert(True)
    with boa.reverts():
        harness.isValidSignature(DIGEST, _prefix(adapter))


def test_state_writing_adapter_is_neutralized_by_staticcall(harness, adapter):
    adapter.set_write_state(True)
    with boa.reverts():
        harness.isValidSignature(DIGEST, _prefix(adapter))
    assert adapter.write_count() == 0


def test_unknown_prefix_is_invalid(harness, adapter_deployer):
    stranger = adapter_deployer.deploy()
    assert harness.isValidSignature(DIGEST, _prefix(stranger) + b"x") == ERC1271_INVALID
    # Bytes that select no adapter — including zero-padded CoW order shapes —
    # answer the invalid magic without reverting.
    assert harness.isValidSignature(DIGEST, bytes(20) + b"\x01" * 364) == ERC1271_INVALID
    assert harness.isValidSignature(DIGEST, b"\x00" * 384) == ERC1271_INVALID


def test_short_and_empty_signatures_are_invalid(harness):
    assert harness.isValidSignature(DIGEST, b"") == ERC1271_INVALID
    assert harness.isValidSignature(DIGEST, b"\x00" * 19) == ERC1271_INVALID


def test_inactive_registry_entry_is_invalid(
    harness, registry, owner, adapter_deployer, executor
):
    # set_adapter alone never routes: activation is the separate owner step.
    pending = adapter_deployer.deploy()
    with boa.env.prank(owner):
        registry.set_adapter(pending, executor)
    assert harness.isValidSignature(DIGEST, _prefix(pending)) == ERC1271_INVALID
    with boa.env.prank(owner):
        registry.activate_adapter(pending)
    assert harness.isValidSignature(DIGEST, _prefix(pending)) == ERC1271_MAGIC_VALUE


def test_registry_disable_kills_prefixed_route_immediately(
    harness, adapter, registry, owner, emergency_owner
):
    assert harness.isValidSignature(DIGEST, _prefix(adapter)) == ERC1271_MAGIC_VALUE
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter)
    # No action on the auction needed: routing rechecks the registry live.
    assert harness.isValidSignature(DIGEST, _prefix(adapter)) == ERC1271_INVALID
    with boa.env.prank(owner):
        registry.activate_adapter(adapter)
    assert harness.isValidSignature(DIGEST, _prefix(adapter)) == ERC1271_MAGIC_VALUE


def test_no_registry_means_native_settlement_only(
    role_source, want, proceeds_receiver, adapter
):
    # A registry-less auction never routes, whatever the prefix names.
    no_registry = _deploy_harness(want, proceeds_receiver, ZERO_ADDRESS, role_source)
    assert no_registry.registry() == ZERO_ADDRESS
    assert no_registry.isValidSignature(DIGEST, _prefix(adapter)) == ERC1271_INVALID
    assert no_registry.isValidSignature(DIGEST, _prefix(adapter) + b"x") == ERC1271_INVALID
    assert no_registry.isValidSignature(DIGEST, b"") == ERC1271_INVALID


# Unbounded signatures: the router imposes no length cap of its own


WIDE_ADAPTER = """
# pragma version 0.5.0b1


@external
@view
def isValidSignature(_hash: bytes32, _signature: Bytes[8192]) -> bytes4:
    return convert(convert(len(_signature), uint32), bytes4)
"""


def test_oversized_signature_routes_intact(
    harness, registry, owner, executor, adapter, adapter_deployer
):
    payload = b"\x5a" * 5000
    # Unknown prefix: still a plain invalid answer, never a decoding failure.
    stranger = adapter_deployer.deploy()
    assert harness.isValidSignature(DIGEST, _prefix(stranger) + payload) == ERC1271_INVALID

    # A adapter accepting wide payloads sees every byte past the prefix.
    wide = boa.loads(WIDE_ADAPTER, name="WideAdapter")
    with boa.env.prank(owner):
        registry.set_adapter(wide, executor)
        registry.activate_adapter(wide)
    answer = harness.isValidSignature(DIGEST, _prefix(wide) + payload)
    assert int.from_bytes(bytes(answer), "big") == len(payload)

    # Any cap is the adapter's own (AdapterMock decodes Bytes[4096]) and
    # surfaces as its revert bubbling through the router.
    with boa.reverts():
        harness.isValidSignature(DIGEST, _prefix(adapter) + payload)
    assert harness.isValidSignature(DIGEST, _prefix(adapter) + b"\x5a" * 4096) == ERC1271_MAGIC_VALUE


# check_order — the shared economic order check


def _check(harness, sell_token, **overrides):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    args = {
        "sell_token": sell_token.address,
        "buy_token": harness.want(),
        "receiver": harness.receiver(),
        "sell_amount": LOT_AMOUNT,
        "min_buy_amount": quote,
        "valid_to": harness.frame_end(),
    }
    args.update(overrides)
    return harness.check_order(
        args["sell_token"],
        args["buy_token"],
        args["receiver"],
        args["sell_amount"],
        args["min_buy_amount"],
        args["valid_to"],
    )


def test_check_order_accepts_fillable_order(harness, sell_token, lot):
    assert _check(harness, sell_token) is True
    # Partial fills quote against the signed total, so smaller amounts pass.
    assert (
        _check(
            harness,
            sell_token,
            sell_amount=LOT_AMOUNT // 3,
            min_buy_amount=harness.getAmountNeeded(sell_token.address, LOT_AMOUNT // 3),
        )
        is True
    )


def test_check_order_wrong_buy_token(harness, sell_token, lot):
    with boa.reverts(custom_err("BadBuyToken()")):
        _check(harness, sell_token, buy_token=sell_token.address)


def test_check_order_wrong_receiver(harness, sell_token, lot, solver):
    with boa.reverts(custom_err("BadReceiver()")):
        _check(harness, sell_token, receiver=solver)


def test_check_order_unstaged_token_inactive(harness, sell_token, erc20_deployer):
    fresh = erc20_deployer.deploy("Fresh", "FRESH", 18)
    with boa.reverts(custom_err("LotInactive()")):
        harness.check_order(
            fresh.address, harness.want(), harness.receiver(), 1, 1, harness.frame_end()
        )


def test_check_order_want_as_sell_token_inactive(harness, want, lot):
    with boa.reverts(custom_err("LotInactive()")):
        harness.check_order(
            want.address,
            harness.want(),
            harness.receiver(),
            1,
            1,
            harness.frame_end(),
        )


def test_check_order_unsellable_token_inactive(harness, sell_token, lot):
    harness.set_sellable(sell_token.address, False)
    with boa.reverts(custom_err("LotInactive()")):
        _check(harness, sell_token)


def test_check_order_expired_window_inactive(harness, sell_token, lot):
    boa.env.time_travel(seconds=harness.frame_end() - boa.env.evm.vm.state.timestamp)
    with boa.reverts(custom_err("LotInactive()")):
        _check(harness, sell_token, valid_to=harness.frame_end())


def test_check_order_epoch_rollover_inactive(harness, sell_token, lot):
    harness.set_frame(harness.frame_start() + WEEK)
    boa.env.time_travel(seconds=WEEK)
    with boa.reverts(custom_err("LotInactive()")):
        _check(harness, sell_token, valid_to=harness.frame_end())


def test_check_order_drained_lot_nothing_available(harness, sell_token, lot):
    with boa.env.prank(harness.address):
        sell_token.transfer(boa.env.generate_address("sink"), LOT_AMOUNT)
    with boa.reverts(custom_err("NothingAvailable()")):
        _check(harness, sell_token)


def test_check_order_sell_amount_bounds(harness, sell_token, lot):
    with boa.reverts(custom_err("BadSellAmount()")):
        _check(harness, sell_token, sell_amount=0)
    with boa.reverts(custom_err("BadSellAmount()")):
        _check(harness, sell_token, sell_amount=LOT_AMOUNT + 1)


def test_check_order_valid_to_bounds(harness, sell_token, lot):
    now = boa.env.evm.vm.state.timestamp
    with boa.reverts(custom_err("BadValidTo()")):
        _check(harness, sell_token, valid_to=now - 1)
    with boa.reverts(custom_err("BadValidTo()")):
        _check(harness, sell_token, valid_to=harness.frame_end() + 1)
    assert _check(harness, sell_token, valid_to=now) is True


def test_check_order_underpriced_min_buy(harness, sell_token, lot):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    with boa.reverts(custom_err("BadBuyAmount()")):
        _check(harness, sell_token, min_buy_amount=quote - 1)
    assert _check(harness, sell_token, min_buy_amount=quote + 1) is True


def test_check_order_callable_by_external_adapter_contract(
    harness, sell_token, lot, adapter
):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    assert (
        adapter.check_order_via_auction(
            harness.address,
            sell_token.address,
            harness.want(),
            harness.receiver(),
            LOT_AMOUNT,
            quote,
            harness.frame_end(),
        )
        is True
    )
