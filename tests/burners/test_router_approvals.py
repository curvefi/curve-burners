"""Executor allowance sync driven by the registry's executor activity.

The auction holds no adapter state of its own: the AdapterRegistry is the
single switch. activate_adapter references the adapter's executor,
disable_adapter releases it, and the permissionless sync_executor_approvals
grants max allowance while is_executor_active(executor) and clears it
otherwise. Staging never touches allowances.
"""

import boa
import pytest

from .conftest import custom_err


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
MAX_UINT256 = 2**256 - 1
WAD = 10**18

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
# Floor reached by the last active second of a day: 1439 sixty-second steps.
AUCTION_LENGTH = 24 * 60 * 60
LOT_AMOUNT = 250 * WAD



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
def keeper():
    return boa.env.generate_address("keeper")


@pytest.fixture(scope="module")
def proceeds_receiver():
    return boa.env.generate_address("fee_collector")


@pytest.fixture(scope="module")
def relayer():
    return boa.env.generate_address("relayer")


@pytest.fixture(scope="module")
def permit2():
    return boa.env.generate_address("permit2")


@pytest.fixture(scope="module")
def erc20_deployer():
    return boa.load_partial("contracts/testing/ERC20Mock.vy")


@pytest.fixture(scope="module")
def want(erc20_deployer):
    return erc20_deployer.deploy("Curve Stablecoin", "crvUSD", 18)


@pytest.fixture(scope="module")
def token_a(erc20_deployer):
    return erc20_deployer.deploy("Curve DAO", "CRV", 18)


@pytest.fixture(scope="module")
def token_b(erc20_deployer):
    return erc20_deployer.deploy("Wrapped Bitcoin", "WBTC", 8)


@pytest.fixture(scope="module")
def problem_token():
    return boa.load(
        "contracts/testing/dutch_auction/ProblemERC20.vy", "Tether USD", "USDT", 6
    )


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


@pytest.fixture
def adapter_cow(adapter_deployer, registry, owner, relayer):
    """Active adapter whose executor is the CoW vault relayer."""
    adapter = adapter_deployer.deploy()
    with boa.env.prank(owner):
        registry.set_adapter(adapter, relayer)
        registry.activate_adapter(adapter)
    return adapter


@pytest.fixture
def adapter_p2a(adapter_deployer, registry, owner, permit2):
    """First permit2-family adapter: shares the Permit2 executor."""
    adapter = adapter_deployer.deploy()
    with boa.env.prank(owner):
        registry.set_adapter(adapter, permit2)
        registry.activate_adapter(adapter)
    return adapter


@pytest.fixture
def adapter_p2b(adapter_deployer, registry, owner, permit2):
    """Second permit2-family adapter: shares the Permit2 executor."""
    adapter = adapter_deployer.deploy()
    with boa.env.prank(owner):
        registry.set_adapter(adapter, permit2)
        registry.activate_adapter(adapter)
    return adapter


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
def disable(registry, owner):
    """Release an adapter's executor reference through the registry."""

    def _disable(adapter):
        with boa.env.prank(owner):
            registry.disable_adapter(adapter)

    return _disable


@pytest.fixture
def stage():
    def _stage(harness, token, amount: int = LOT_AMOUNT) -> None:
        token._mint_for_testing(harness.address, amount)
        harness.stage(token.address)

    return _stage


# Staging never grants; the permissionless sync does, to referenced executors only


def test_stage_grants_nothing_and_sync_grants_referenced_executors_only(
    harness,
    stage,
    keeper,
    owner,
    registry,
    adapter_deployer,
    adapter_cow,
    token_a,
    token_b,
    relayer,
    permit2,
):
    stage(harness, token_a)
    assert token_a.allowance(harness, relayer) == 0
    assert token_a.allowance(harness, permit2) == 0

    with boa.env.prank(keeper):
        harness.sync_executor_approvals(relayer, [token_a.address])
        harness.sync_executor_approvals(permit2, [token_a.address])
    assert token_a.allowance(harness, relayer) == MAX_UINT256
    # Unreferenced executor: the sync is a clearing pass, never a grant.
    assert token_a.allowance(harness, permit2) == 0

    # Every referenced executor is granted once its adapter goes live.
    permit2_adapter = adapter_deployer.deploy()
    with boa.env.prank(owner):
        registry.set_adapter(permit2_adapter, permit2)
        registry.activate_adapter(permit2_adapter)
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(permit2, [token_a.address])
    assert token_a.allowance(harness, permit2) == MAX_UINT256

    # The sync is the retry path for a never-staged token and is idempotent.
    for _ in range(2):
        with boa.env.prank(keeper):
            harness.sync_executor_approvals(relayer, [token_a.address, token_b.address])
        assert token_a.allowance(harness, relayer) == MAX_UINT256
        assert token_b.allowance(harness, relayer) == MAX_UINT256


def test_stage_rejects_target_token(harness, want):
    want._mint_for_testing(harness.address, LOT_AMOUNT)
    with boa.reverts(custom_err("WantNotSellable()")):
        harness.stage(want.address)


# The registry's executor activity is the only switch the sync follows


def test_sync_follows_registry_activation(
    harness, registry, owner, keeper, adapter_deployer, token_a, relayer
):
    # Registered but inactive: the executor is unreferenced, the sync clears.
    adapter = adapter_deployer.deploy()
    with boa.env.prank(owner):
        registry.set_adapter(adapter, relayer)
    assert not registry.is_executor_active(relayer)
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(relayer, [token_a.address])
    assert token_a.allowance(harness, relayer) == 0

    # Activation references the executor; nothing happens on the auction until
    # the sync is run.
    with boa.env.prank(owner):
        registry.activate_adapter(adapter)
    assert registry.is_executor_active(relayer)
    assert token_a.allowance(harness, relayer) == 0
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(relayer, [token_a.address])
    assert token_a.allowance(harness, relayer) == MAX_UINT256


def test_sync_without_registry_always_clears(
    role_source, want, proceeds_receiver, keeper, token_a, relayer
):
    # No registry means native settlement only: no executor is ever
    # referenced, so the sync degrades to a pure clearing pass.
    no_registry = _deploy_harness(want, proceeds_receiver, ZERO_ADDRESS, role_source)
    assert no_registry.registry() == ZERO_ADDRESS
    with boa.env.prank(no_registry.address):
        token_a.approve(relayer, 1234)
    with boa.env.prank(keeper):
        no_registry.sync_executor_approvals(relayer, [token_a.address])
    assert token_a.allowance(no_registry, relayer) == 0


def test_shared_executor_approved_once_and_kept_until_full_release(
    harness, disable, stage, keeper, adapter_p2a, adapter_p2b, token_a, permit2, registry
):
    assert registry.is_executor_active(permit2)
    stage(harness, token_a)
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(permit2, [token_a.address])
    assert token_a.allowance(harness, permit2) == MAX_UINT256

    disable(adapter_p2a)
    # Still referenced by the second adapter: the sync must keep the grant.
    assert registry.is_executor_active(permit2)
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(permit2, [token_a.address])
    assert token_a.allowance(harness, permit2) == MAX_UINT256

    disable(adapter_p2b)
    assert not registry.is_executor_active(permit2)
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(permit2, [token_a.address])
    assert token_a.allowance(harness, permit2) == 0


# Permissionless sync


def test_sync_revokes_after_emergency_release(
    harness, registry, emergency_owner, stage, keeper, adapter_cow, token_a, relayer
):
    stage(harness, token_a)
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(relayer, [token_a.address])
    assert token_a.allowance(harness, relayer) == MAX_UINT256
    # The registry disable itself touches no auction allowance; the sync does
    # the cleanup once the executor is no longer active.
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter_cow)
    assert token_a.allowance(harness, relayer) == MAX_UINT256
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(relayer, [token_a.address])
    assert token_a.allowance(harness, relayer) == 0


def test_sync_clears_residual_allowance_of_unreferenced_executor(
    harness, keeper, problem_token
):
    stranger = boa.env.generate_address("stranger_executor")
    problem_token.mint(harness.address, LOT_AMOUNT)
    # Residual allowance without any active adapter referencing the executor.
    with boa.env.prank(harness.address):
        problem_token.approve(stranger, 1234)
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(stranger, [problem_token.address])
    assert problem_token.allowance(harness, stranger) == 0


def test_sync_rejects_target_token(harness, keeper, want, token_a, adapter_cow, relayer):
    with boa.env.prank(keeper), boa.reverts(custom_err("WantNotSellable()")):
        harness.sync_executor_approvals(relayer, [token_a.address, want.address])


def test_sync_clears_want_allowance_of_released_executor(
    harness, disable, keeper, want, adapter_cow, relayer
):
    """A token promoted to want by a resync may carry a stale settlement
    allowance; only granting refuses want — clearing must stay possible."""
    disable(adapter_cow)
    with boa.env.prank(harness.address):
        want.approve(relayer, 1234)
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(relayer, [want.address])
    assert want.allowance(harness, relayer) == 0


# Exotic-token approval semantics


def test_usdt_style_grant_from_zero_allowance(
    harness, keeper, stage, problem_token, adapter_cow, relayer
):
    problem_token.set_requires_approval_reset(True)
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(relayer, [problem_token.address])
    assert problem_token.allowance(harness, relayer) == MAX_UINT256


def test_nonzero_residual_allowance_left_untouched_while_referenced(
    harness, keeper, problem_token, adapter_cow, relayer
):
    with boa.env.prank(harness.address):
        problem_token.approve(relayer, 1234)
    # The guarded helper only writes from zero: a mid-range residual is
    # exotic-token drift, never topped up silently.
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(relayer, [problem_token.address])
    assert problem_token.allowance(harness, relayer) == 1234


def test_approval_failure_never_blocks_staging(
    harness, keeper, problem_token, adapter_cow, relayer
):
    # Staging touches no allowances, so a token whose approve fails still
    # stages and trades natively; only the sync leg for it reverts.
    problem_token.set_fails_nonzero_approval(True)
    problem_token.mint(harness.address, LOT_AMOUNT)
    harness.stage(problem_token.address)
    assert harness.lots(problem_token.address).initial_amount == LOT_AMOUNT
    with boa.env.prank(keeper), boa.reverts():
        harness.sync_executor_approvals(relayer, [problem_token.address])
    assert problem_token.allowance(harness, relayer) == 0
