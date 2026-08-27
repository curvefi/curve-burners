from typing import Any

import boa
import pytest

from .conftest import custom_err


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
MAX_UINT256 = 2**256 - 1
WAD = 10**18

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
DECAY_FACTOR_RAY = 992031276831159793484252056
LOT_AMOUNT = 250 * WAD

LOT_EPOCH = 0
LOT_INITIAL_AMOUNT = 1

MAX_EXECUTORS = 8


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
def verifier_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/VerifierMock.vy")


@pytest.fixture
def registry(role_source):
    return boa.load(
        "contracts/burners/auction/adapters/AdapterRegistry.vy", role_source.address
    )


@pytest.fixture
def adapter_cow(verifier_deployer, registry, owner, relayer):
    """Adapter whose executor is the CoW vault relayer."""
    adapter = verifier_deployer.deploy()
    with boa.env.prank(owner):
        registry.set_adapter(adapter, relayer)
        registry.activate_adapter(adapter)
    return adapter


@pytest.fixture
def adapter_p2a(verifier_deployer, registry, owner, permit2):
    """First permit2-family adapter: shares the Permit2 executor."""
    adapter = verifier_deployer.deploy()
    with boa.env.prank(owner):
        registry.set_adapter(adapter, permit2)
        registry.activate_adapter(adapter)
    return adapter


@pytest.fixture
def adapter_p2b(verifier_deployer, registry, owner, permit2):
    """Second permit2-family adapter: shares the Permit2 executor."""
    adapter = verifier_deployer.deploy()
    with boa.env.prank(owner):
        registry.set_adapter(adapter, permit2)
        registry.activate_adapter(adapter)
    return adapter


@pytest.fixture
def harness(owner, role_source, want, proceeds_receiver, registry):
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
def enable(owner):
    def _enable(harness, adapter):
        with boa.env.prank(owner):
            harness.enable_adapter(adapter)

    return _enable


@pytest.fixture
def disable(owner):
    def _disable(harness, adapter):
        with boa.env.prank(owner):
            harness.disable_adapter(adapter)

    return _disable


@pytest.fixture
def stage():
    def _stage(harness, token, amount: int = LOT_AMOUNT) -> None:
        token._mint_for_testing(harness.address, amount)
        harness.stage(token.address)

    return _stage


# Staging drives executor approvals


def test_stage_grants_max_to_enabled_executor_only(
    harness, enable, stage, adapter_cow, token_a, relayer, permit2
):
    enable(harness, adapter_cow)
    stage(harness, token_a)
    assert token_a.allowance(harness, relayer) == MAX_UINT256
    assert token_a.allowance(harness, permit2) == 0


def test_stage_grants_every_referenced_executor(
    harness, enable, stage, adapter_cow, adapter_p2a, token_a, relayer, permit2
):
    enable(harness, adapter_cow)
    enable(harness, adapter_p2a)
    stage(harness, token_a)
    assert token_a.allowance(harness, relayer) == MAX_UINT256
    assert token_a.allowance(harness, permit2) == MAX_UINT256


def test_stage_without_enabled_adapters_grants_nothing(
    harness, stage, token_a, relayer, permit2
):
    stage(harness, token_a)
    assert harness.lots(token_a.address)[LOT_INITIAL_AMOUNT] == LOT_AMOUNT
    assert token_a.allowance(harness, relayer) == 0
    assert token_a.allowance(harness, permit2) == 0


def test_stage_rejects_target_token(harness, want):
    want._mint_for_testing(harness.address, LOT_AMOUNT)
    with boa.reverts(custom_err("TargetToken()")):
        harness.stage(want.address)


# Executor refcount lifecycle


def test_enable_requires_known_active_registry_entry(
    harness, registry, owner, verifier_deployer, relayer
):
    unknown = verifier_deployer.deploy()
    with boa.env.prank(owner), boa.reverts(custom_err("UnknownAdapter()")):
        harness.enable_adapter(unknown)

    inactive = verifier_deployer.deploy()
    with boa.env.prank(owner):
        registry.set_adapter(inactive, relayer)
        with boa.reverts(custom_err("InactiveAdapter()")):
            harness.enable_adapter(inactive)
        registry.activate_adapter(inactive)
        harness.enable_adapter(inactive)
    assert harness.enabled_adapters(inactive)


def test_enable_requires_registry(owner, role_source, want, proceeds_receiver, adapter_cow):
    no_registry = boa.load(
        "contracts/testing/dutch_auction/CoreHarness.vy",
        want.address,
        proceeds_receiver,
        ZERO_ADDRESS,
        role_source.address,
        START_TOTAL,
        FLOOR_TOTAL,
        DECAY_FACTOR_RAY,
        STEP_DURATION,
    )
    with boa.env.prank(owner), boa.reverts(custom_err("NoRegistry()")):
        no_registry.enable_adapter(adapter_cow)


def test_enable_disable_authority_and_double_toggle(
    harness, owner, emergency_owner, keeper, adapter_cow
):
    with boa.env.prank(keeper), boa.reverts(custom_err("OnlyOwner()")):
        harness.enable_adapter(adapter_cow)
    with boa.env.prank(emergency_owner), boa.reverts(custom_err("OnlyOwner()")):
        harness.enable_adapter(adapter_cow)

    with boa.env.prank(owner):
        harness.enable_adapter(adapter_cow)
        with boa.reverts(custom_err("AlreadyEnabled()")):
            harness.enable_adapter(adapter_cow)

    with boa.env.prank(keeper), boa.reverts(custom_err("OnlyOwner()")):
        harness.disable_adapter(adapter_cow)
    # Emergency can disable but never enable.
    with boa.env.prank(emergency_owner):
        harness.disable_adapter(adapter_cow)
    with boa.env.prank(owner), boa.reverts(custom_err("NotEnabled()")):
        harness.disable_adapter(adapter_cow)


def test_shared_executor_refcount_and_enumeration(
    harness, enable, disable, adapter_p2a, adapter_p2b, permit2
):
    enable(harness, adapter_p2a)
    assert harness.executor_refcount(permit2) == 1
    assert harness.executors(0) == permit2
    enable(harness, adapter_p2b)
    # One shared Permit2: counted twice, listed once.
    assert harness.executor_refcount(permit2) == 2
    with boa.reverts():
        harness.executors(1)

    disable(harness, adapter_p2a)
    assert harness.executor_refcount(permit2) == 1
    assert harness.executors(0) == permit2
    disable(harness, adapter_p2b)
    assert harness.executor_refcount(permit2) == 0
    with boa.reverts():
        harness.executors(0)


def test_shared_executor_approved_once_and_kept_until_full_release(
    harness, enable, disable, stage, keeper, adapter_p2a, adapter_p2b, token_a, permit2
):
    enable(harness, adapter_p2a)
    enable(harness, adapter_p2b)
    stage(harness, token_a)
    assert token_a.allowance(harness, permit2) == MAX_UINT256

    disable(harness, adapter_p2a)
    # Still referenced: the permissionless sync must keep the grant.
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(permit2, [token_a.address])
    assert token_a.allowance(harness, permit2) == MAX_UINT256

    disable(harness, adapter_p2b)
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(permit2, [token_a.address])
    assert token_a.allowance(harness, permit2) == 0


def test_executor_list_capacity_bound(
    harness, registry, owner, verifier_deployer
):
    with boa.env.prank(owner):
        for i in range(MAX_EXECUTORS):
            adapter = verifier_deployer.deploy()
            registry.set_adapter(adapter, boa.env.generate_address(f"executor{i}"))
            registry.activate_adapter(adapter)
            harness.enable_adapter(adapter)
        overflow = verifier_deployer.deploy()
        registry.set_adapter(overflow, boa.env.generate_address("executor_overflow"))
        registry.activate_adapter(overflow)
        with boa.reverts(custom_err("TooManyExecutors()")):
            harness.enable_adapter(overflow)


# Permissionless sync


def test_sync_grants_as_retry_path(
    harness, enable, stage, keeper, adapter_cow, token_a, token_b, relayer
):
    enable(harness, adapter_cow)
    stage(harness, token_a)
    # token_b was never staged; sync tops it up while the executor is live.
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(relayer, [token_a.address, token_b.address])
    assert token_a.allowance(harness, relayer) == MAX_UINT256
    assert token_b.allowance(harness, relayer) == MAX_UINT256


def test_sync_revokes_after_release(
    harness, enable, disable, stage, keeper, adapter_cow, token_a, relayer
):
    enable(harness, adapter_cow)
    stage(harness, token_a)
    disable(harness, adapter_cow)
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(relayer, [token_a.address])
    assert token_a.allowance(harness, relayer) == 0


def test_sync_is_idempotent(harness, enable, stage, keeper, adapter_cow, token_a, relayer):
    enable(harness, adapter_cow)
    stage(harness, token_a)
    for _ in range(2):
        with boa.env.prank(keeper):
            harness.sync_executor_approvals(relayer, [token_a.address])
        assert token_a.allowance(harness, relayer) == MAX_UINT256


def test_sync_clears_residual_allowance_of_unreferenced_executor(
    harness, keeper, problem_token
):
    stranger = boa.env.generate_address("stranger_executor")
    problem_token.mint(harness.address, LOT_AMOUNT)
    # Residual allowance without any enabled adapter referencing the executor.
    with boa.env.prank(harness.address):
        problem_token.approve(stranger, 1234)
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(stranger, [problem_token.address])
    assert problem_token.allowance(harness, stranger) == 0


def test_sync_rejects_zero_executor(harness, keeper, token_a):
    with boa.env.prank(keeper), boa.reverts(custom_err("BadExecutor()")):
        harness.sync_executor_approvals(ZERO_ADDRESS, [token_a.address])


def test_sync_rejects_target_token(harness, enable, keeper, want, token_a, adapter_cow, relayer):
    enable(harness, adapter_cow)
    with boa.env.prank(keeper), boa.reverts(custom_err("TargetToken()")):
        harness.sync_executor_approvals(relayer, [token_a.address, want.address])


# Exotic-token approval semantics


def test_usdt_style_grant_from_zero_allowance(
    harness, enable, keeper, stage, problem_token, adapter_cow, relayer
):
    enable(harness, adapter_cow)
    problem_token.set_requires_approval_reset(True)
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(relayer, [problem_token.address])
    assert problem_token.allowance(harness, relayer) == MAX_UINT256


def test_usdt_style_grant_from_zero_allowance_on_stage(
    harness, enable, stage, problem_token, adapter_cow, relayer
):
    enable(harness, adapter_cow)
    problem_token.set_requires_approval_reset(True)
    problem_token.mint(harness.address, LOT_AMOUNT)
    harness.stage(problem_token.address)
    assert problem_token.allowance(harness, relayer) == MAX_UINT256


def test_nonzero_residual_allowance_left_untouched_while_referenced(
    harness, enable, keeper, problem_token, adapter_cow, relayer
):
    enable(harness, adapter_cow)
    with boa.env.prank(harness.address):
        problem_token.approve(relayer, 1234)
    # The guarded helper only writes from zero: a mid-range residual is
    # exotic-token drift, never topped up silently.
    with boa.env.prank(keeper):
        harness.sync_executor_approvals(relayer, [problem_token.address])
    assert problem_token.allowance(harness, relayer) == 1234


def test_approval_failure_blocks_staging_loudly(
    harness, enable, problem_token, token_a, stage, adapter_cow, relayer
):
    enable(harness, adapter_cow)
    problem_token.set_fails_nonzero_approval(True)
    problem_token.mint(harness.address, LOT_AMOUNT)
    with boa.reverts():
        harness.stage(problem_token.address)

    # Well-behaved tokens keep staging with the full grant.
    stage(harness, token_a)
    assert token_a.allowance(harness, relayer) == MAX_UINT256


def test_approve_failure_reverts_sync(
    harness, enable, keeper, problem_token, adapter_cow, relayer
):
    enable(harness, adapter_cow)
    problem_token.set_fails_nonzero_approval(True)
    with boa.env.prank(keeper), boa.reverts():
        harness.sync_executor_approvals(relayer, [problem_token.address])
