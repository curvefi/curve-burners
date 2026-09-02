"""Prefix-based ERC-1271 signature router and the shared check_order view.

Routing is sender-agnostic: a signature whose first 20 bytes name an enabled
verifier is forwarded with the prefix stripped; on the plain router anything
else is invalid (built-in rails such as CoW compose their own isValidSignature
on top of the same internals). Economic authority never leaves the auction:
verifiers call check_order, covered here on the core harness.
"""

from typing import Any

import boa
import pytest

from .conftest import custom_err


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
MAX_UINT256 = 2**256 - 1
WAD = 10**18
WEEK = 7 * 24 * 60 * 60

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
DECAY_FACTOR_RAY = 992031276831159793484252056
LOT_AMOUNT = 250 * WAD

ERC1271_MAGIC_VALUE = bytes.fromhex("1626ba7e")
ERC1271_INVALID = bytes.fromhex("ffffffff")

LOT_EPOCH = 0
LOT_INITIAL_AMOUNT = 1

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
def verifier_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/VerifierMock.vy")


@pytest.fixture
def registry(role_source):
    return boa.load(
        "contracts/burners/auction/adapters/AdapterRegistry.vy", role_source.address
    )


@pytest.fixture
def harness(role_source, want, proceeds_receiver, registry):
    return boa.load(
        "contracts/testing/dutch_auction/CoreHarness.vy",
        want.address,
        proceeds_receiver,
        registry.address,
        role_source.address,
        START_TOTAL,
        FLOOR_TOTAL,
        DECAY_FACTOR_RAY,
        STEP_DURATION,
    )


@pytest.fixture
def verifier(verifier_deployer, registry, owner, executor, harness):
    adapter = verifier_deployer.deploy()
    with boa.env.prank(owner):
        registry.set_adapter(adapter, executor)
        registry.activate_adapter(adapter)
        harness.enable_adapter(adapter)
    return adapter


@pytest.fixture
def lot(harness, sell_token):
    sell_token._mint_for_testing(harness.address, LOT_AMOUNT)
    harness.stage(sell_token.address)
    return harness.lots(sell_token.address)


def _prefix(adapter: Any) -> bytes:
    return bytes.fromhex(str(adapter.address)[2:])


# Routing


def test_prefixed_signature_routes_to_enabled_verifier(harness, verifier):
    assert (
        harness.isValidSignature(DIGEST, _prefix(verifier) + b"payload")
        == ERC1271_MAGIC_VALUE
    )


def test_prefix_only_signature_forwards_empty_payload(harness, verifier):
    assert harness.isValidSignature(DIGEST, _prefix(verifier)) == ERC1271_MAGIC_VALUE


def test_verifier_answer_is_passed_through_verbatim(harness, verifier):
    verifier.set_response(bytes.fromhex("deadbeef"))
    assert harness.isValidSignature(DIGEST, _prefix(verifier)) == bytes.fromhex(
        "deadbeef"
    )


def test_verifier_revert_bubbles_up(harness, verifier):
    verifier.set_revert(True)
    with boa.reverts():
        harness.isValidSignature(DIGEST, _prefix(verifier))


def test_state_writing_verifier_is_neutralized_by_staticcall(harness, verifier):
    verifier.set_write_state(True)
    with boa.reverts():
        harness.isValidSignature(DIGEST, _prefix(verifier))
    assert verifier.write_count() == 0


def test_unknown_prefix_is_invalid(harness, verifier_deployer):
    stranger = verifier_deployer.deploy()
    assert harness.isValidSignature(DIGEST, _prefix(stranger) + b"x") == ERC1271_INVALID


def test_short_and_empty_signatures_are_invalid(harness):
    assert harness.isValidSignature(DIGEST, b"") == ERC1271_INVALID
    assert harness.isValidSignature(DIGEST, b"\x00" * 19) == ERC1271_INVALID


def test_local_disable_kills_prefixed_route(harness, verifier, owner):
    with boa.env.prank(owner):
        harness.disable_adapter(verifier)
    assert harness.isValidSignature(DIGEST, _prefix(verifier)) == ERC1271_INVALID


def test_registry_disable_kills_prefixed_route_immediately(
    harness, verifier, registry, emergency_owner
):
    assert harness.isValidSignature(DIGEST, _prefix(verifier)) == ERC1271_MAGIC_VALUE
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(verifier)
    # No local action needed: routing rechecks the registry live.
    assert harness.enabled_adapters(verifier)
    assert harness.isValidSignature(DIGEST, _prefix(verifier)) == ERC1271_INVALID


# Unprefixed encodings


def test_unprefixed_signatures_are_invalid_on_the_plain_router(harness, verifier):
    # No fallback exists: bytes that select no enabled verifier — including
    # the zero-padded CoW shapes — answer the invalid magic without reverting.
    assert harness.isValidSignature(DIGEST, bytes(20) + b"\x01" * 364) == ERC1271_INVALID
    assert harness.isValidSignature(DIGEST, b"\x00" * 384) == ERC1271_INVALID
    # A prefixed signature still takes the stripped adapter path.
    assert harness.isValidSignature(DIGEST, _prefix(verifier) + b"x") == ERC1271_MAGIC_VALUE


# check_order — the shared economic order check


def _check(harness, sell_token, **overrides):
    quote = harness.quote(sell_token.address, LOT_AMOUNT)
    args = {
        "sell_token": sell_token.address,
        "buy_token": harness.want(),
        "receiver": harness.proceeds_receiver(),
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
    assert _check(harness, sell_token) == ""
    # Partial fills quote against the signed total, so smaller amounts pass.
    assert (
        _check(
            harness,
            sell_token,
            sell_amount=LOT_AMOUNT // 3,
            min_buy_amount=harness.quote(sell_token.address, LOT_AMOUNT // 3),
        )
        == ""
    )


def test_check_order_wrong_buy_token(harness, sell_token, lot):
    assert _check(harness, sell_token, buy_token=sell_token.address) == "BadToken"


def test_check_order_wrong_receiver(harness, sell_token, lot, solver):
    assert _check(harness, sell_token, receiver=solver) == "BadReceiver"


def test_check_order_unstaged_token_not_allowed(harness, sell_token, erc20_deployer):
    fresh = erc20_deployer.deploy("Fresh", "FRESH", 18)
    quote_free = harness.check_order(
        fresh.address, harness.want(), harness.proceeds_receiver(), 1, 1, harness.frame_end()
    )
    assert quote_free == "NotAllowed"


def test_check_order_want_as_sell_token_not_allowed(harness, want, lot):
    assert (
        harness.check_order(
            want.address,
            harness.want(),
            harness.proceeds_receiver(),
            1,
            1,
            harness.frame_end(),
        )
        == "NotAllowed"
    )


def test_check_order_unsellable_token_not_allowed(harness, sell_token, lot):
    harness.set_sellable(sell_token.address, False)
    assert _check(harness, sell_token) == "NotAllowed"


def test_check_order_expired_window_not_allowed(harness, sell_token, lot):
    boa.env.time_travel(seconds=harness.frame_end() - boa.env.evm.vm.state.timestamp)
    assert _check(harness, sell_token, valid_to=harness.frame_end()) == "NotAllowed"


def test_check_order_epoch_rollover_not_allowed(harness, sell_token, lot):
    harness.set_frame(harness.frame_start() + WEEK, harness.frame_end() + WEEK)
    boa.env.time_travel(seconds=WEEK)
    assert _check(harness, sell_token, valid_to=harness.frame_end()) == "NotAllowed"


def test_check_order_drained_lot_zero_balance(harness, sell_token, lot):
    with boa.env.prank(harness.address):
        sell_token.transfer(boa.env.generate_address("sink"), LOT_AMOUNT)
    assert _check(harness, sell_token) == "ZeroBalance"


def test_check_order_sell_amount_bounds(harness, sell_token, lot):
    assert _check(harness, sell_token, sell_amount=0) == "BadSellAmount"
    assert _check(harness, sell_token, sell_amount=LOT_AMOUNT + 1) == "BadSellAmount"


def test_check_order_valid_to_bounds(harness, sell_token, lot):
    now = boa.env.evm.vm.state.timestamp
    assert _check(harness, sell_token, valid_to=now - 1) == "BadValidTo"
    assert _check(harness, sell_token, valid_to=harness.frame_end() + 1) == "BadValidTo"
    assert _check(harness, sell_token, valid_to=now) == ""


def test_check_order_underpriced_min_buy(harness, sell_token, lot):
    quote = harness.quote(sell_token.address, LOT_AMOUNT)
    assert _check(harness, sell_token, min_buy_amount=quote - 1) == "BadBuyAmount"
    assert _check(harness, sell_token, min_buy_amount=quote + 1) == ""


def test_check_order_callable_by_external_verifier_contract(
    harness, sell_token, lot, verifier
):
    quote = harness.quote(sell_token.address, LOT_AMOUNT)
    assert (
        verifier.check_order_via_auction(
            harness.address,
            sell_token.address,
            harness.want(),
            harness.proceeds_receiver(),
            LOT_AMOUNT,
            quote,
            harness.frame_end(),
        )
        == ""
    )
