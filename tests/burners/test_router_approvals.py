from typing import Any

import boa
import pytest
from eth_hash.auto import keccak

from .conftest import custom_err


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
MAX_UINT256 = 2**256 - 1
WAD = 10**18

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
DECAY_FACTOR_RAY = 992031276831159793484252056
LOT_AMOUNT = 250 * WAD

MODE_NONE = 0
MODE_COW_VAULT_RELAYER = 1
MODE_PERMIT2_SIGNATURE_TRANSFER = 2
MODE_PERMIT2_ALLOWANCE_TRANSFER = 3

ID_COW = keccak(b"COW_MODE_ADAPTER")[:4]
ID_SIG = keccak(b"PERMIT2_SIG_ADAPTER")[:4]
ID_ALLOW = keccak(b"PERMIT2_ALLOW_ADAPTER")[:4]
ID_NONE = keccak(b"NO_ROUTER_ADAPTER")[:4]

LOT_EPOCH = 0
LOT_INITIAL_AMOUNT = 1

REGISTRY_MOCK_SOURCE = """
# pragma version 0.5.0a4

from contracts.burners.auction import adapter_types

config: adapter_types.AdapterConfig


@external
def set_config(_config: adapter_types.AdapterConfig):
    self.config = _config


@external
@view
def get_adapter(_adapter_id: bytes4) -> adapter_types.AdapterConfig:
    return self.config
"""


def event_name(log: Any) -> str:
    event_type = getattr(log, "event_type", None)
    return event_type.name if event_type is not None else type(log).__name__


def router_approvals(contract) -> list:
    # boa keeps only the logs of the latest transaction, so read immediately.
    return [log for log in contract.get_logs() if event_name(log) == "RouterApproval"]


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
def keeper():
    return boa.env.generate_address("keeper")


@pytest.fixture(scope="module")
def solver():
    return boa.env.generate_address("solver")


@pytest.fixture(scope="module")
def publisher():
    # Off-chain order publisher stand-in: must never hold an allowance.
    return boa.env.generate_address("publisher")


@pytest.fixture(scope="module")
def proceeds_receiver():
    return boa.env.generate_address("fee_collector")


@pytest.fixture(scope="module")
def cow_router():
    return boa.env.generate_address("cow_router")


@pytest.fixture(scope="module")
def permit2():
    return boa.env.generate_address("permit2")


@pytest.fixture(scope="module")
def shared_router():
    return boa.env.generate_address("shared_router")


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
def validator_mock():
    return boa.load("contracts/testing/dutch_auction/OrderValidatorMock.vy")


@pytest.fixture(scope="module")
def registry(owner, emergency_owner, validator_mock):
    verifier = boa.env.generate_address("verifier")
    executor = boa.env.generate_address("executor")
    with boa.env.prank(owner):
        role_source = boa.load(
            "contracts/testing/dutch_auction/RoleSourceMock.vy", owner, emergency_owner
        )
        registry = boa.load("contracts/AdapterRegistry.vy", role_source.address)
        for adapter_id, mode in (
            (ID_COW, MODE_COW_VAULT_RELAYER),
            (ID_SIG, MODE_PERMIT2_SIGNATURE_TRANSFER),
            (ID_ALLOW, MODE_PERMIT2_ALLOWANCE_TRANSFER),
            (ID_NONE, MODE_NONE),
        ):
            registry.set_adapter(
                adapter_id,
                (
                    validator_mock.address,
                    codehash_of(validator_mock.address),
                    verifier,
                    executor,
                    mode,
                    True,
                    False,
                    1,
                ),
            )
            registry.activate_adapter(adapter_id)
    return registry


def deploy_harness(owner, want, proceeds_receiver, registry, permit2_address, cow_router_address):
    with boa.env.prank(owner):
        harness = boa.load(
            "contracts/testing/dutch_auction/CoreHarness.vy",
            want.address,
            proceeds_receiver,
            registry.address,
            permit2_address,
            START_TOTAL,
            FLOOR_TOTAL,
            DECAY_FACTOR_RAY,
            STEP_DURATION,
        )
        if cow_router_address != ZERO_ADDRESS:
            harness.set_cow_router(cow_router_address)
    return harness


@pytest.fixture(scope="module")
def harness(owner, want, proceeds_receiver, registry, permit2, cow_router):
    return deploy_harness(owner, want, proceeds_receiver, registry, permit2, cow_router)


@pytest.fixture(scope="module")
def harness_shared(owner, want, proceeds_receiver, registry, shared_router):
    """Permit2 and the CoW router resolve to one shared canonical router."""
    return deploy_harness(owner, want, proceeds_receiver, registry, shared_router, shared_router)


@pytest.fixture(scope="module")
def enable():
    def _enable(harness, adapter_id: bytes):
        with boa.env.prank(harness.owner()):
            harness.enable_adapter(adapter_id)

    return _enable


@pytest.fixture(scope="module")
def disable():
    def _disable(harness, adapter_id: bytes):
        with boa.env.prank(harness.owner()):
            harness.disable_adapter(adapter_id)

    return _disable


@pytest.fixture(scope="module")
def stage():
    def _stage(harness, token, amount: int = LOT_AMOUNT) -> None:
        token._mint_for_testing(harness.address, amount)
        harness.stage(token.address)

    return _stage


# Staging drives canonical approvals


def test_stage_grants_max_to_cow_router_only(harness, enable, stage, token_a, cow_router,
                                             permit2, registry, validator_mock, publisher):
    enable(harness, ID_COW)
    assert token_a.allowance(harness.address, cow_router) == 0
    stage(harness, token_a)
    assert token_a.allowance(harness.address, cow_router) == MAX_UINT256
    # Permit2 has no enabled rail, so staging must not touch it.
    assert token_a.allowance(harness.address, permit2) == 0
    # §14.3: the approval surface is routers only — never the validator, the
    # registry, or an off-chain publisher.
    for never_approved in (validator_mock.address, registry.address, publisher):
        assert token_a.allowance(harness.address, never_approved) == 0


@pytest.mark.parametrize("adapter_id", [ID_SIG, ID_ALLOW], ids=["signature", "allowance"])
def test_stage_grants_max_to_permit2_for_each_mode(harness, enable, stage, token_a, cow_router,
                                                   permit2, adapter_id):
    enable(harness, adapter_id)
    stage(harness, token_a)
    assert token_a.allowance(harness.address, permit2) == MAX_UINT256
    assert token_a.allowance(harness.address, cow_router) == 0


def test_stage_grants_both_routers_when_both_referenced(harness, enable, stage, token_a,
                                                        token_b, cow_router, permit2):
    enable(harness, ID_COW)
    enable(harness, ID_SIG)
    for token in (token_a, token_b):
        stage(harness, token)
        assert token.allowance(harness.address, cow_router) == MAX_UINT256
        assert token.allowance(harness.address, permit2) == MAX_UINT256


def test_stage_without_enabled_rails_grants_nothing(harness, stage, token_a, cow_router,
                                                    permit2):
    stage(harness, token_a)
    assert router_approvals(harness) == []
    assert token_a.allowance(harness.address, cow_router) == 0
    assert token_a.allowance(harness.address, permit2) == 0


def test_mode_none_adapter_holds_no_router(harness, enable, stage, token_a, cow_router,
                                           permit2):
    enable(harness, ID_NONE)
    assert harness.adapter_router(ID_NONE) == ZERO_ADDRESS
    assert harness.router_refcount(cow_router) == 0
    assert harness.router_refcount(permit2) == 0
    stage(harness, token_a)
    assert router_approvals(harness) == []


def test_stage_emits_router_approval_events(harness, enable, stage, token_a, cow_router,
                                            permit2):
    enable(harness, ID_COW)
    enable(harness, ID_ALLOW)
    stage(harness, token_a)
    events = router_approvals(harness)
    assert {(log.router, log.amount) for log in events} == {
        (cow_router, MAX_UINT256),
        (permit2, MAX_UINT256),
    }


def test_stage_rejects_target_token(harness, want):
    want._mint_for_testing(harness.address, LOT_AMOUNT)
    with boa.reverts(custom_err("TargetToken()")):
        harness.stage(want.address)


# Refcount lifecycle


def test_permit2_refcount_shared_by_both_permit2_modes(harness, enable, disable, permit2):
    enable(harness, ID_SIG)
    assert harness.router_refcount(permit2) == 1
    enable(harness, ID_ALLOW)
    assert harness.router_refcount(permit2) == 2

    disable(harness, ID_SIG)
    assert harness.router_refcount(permit2) == 1
    disable(harness, ID_ALLOW)
    assert harness.router_refcount(permit2) == 0


def test_adapter_router_pinned_at_enable_time(harness, enable, disable, cow_router):
    # The refcount released at disable belongs to the router resolved at
    # enable time, even if the hook answer drifted in between.
    enable(harness, ID_COW)
    assert harness.adapter_router(ID_COW) == cow_router

    drifted_router = boa.env.generate_address("drifted_router")
    with boa.env.prank(harness.owner()):
        harness.set_cow_router(drifted_router)
    disable(harness, ID_COW)
    assert harness.router_refcount(cow_router) == 0
    assert harness.router_refcount(drifted_router) == 0

    enable(harness, ID_COW)
    assert harness.adapter_router(ID_COW) == drifted_router
    assert harness.router_refcount(drifted_router) == 1


# Shared-router deployments


def test_shared_router_counted_once_per_stage(harness_shared, enable, stage, token_a,
                                              shared_router):
    enable(harness_shared, ID_COW)
    enable(harness_shared, ID_SIG)
    assert harness_shared.router_refcount(shared_router) == 2

    stage(harness_shared, token_a)
    events = router_approvals(harness_shared)
    # permit2 == cow router: one grant, not two.
    assert len(events) == 1
    assert events[0].router == shared_router
    assert token_a.allowance(harness_shared.address, shared_router) == MAX_UINT256


def test_shared_router_revoked_only_after_full_release(harness_shared, enable, disable, stage,
                                                       keeper, token_a, shared_router):
    enable(harness_shared, ID_COW)
    enable(harness_shared, ID_SIG)
    stage(harness_shared, token_a)

    disable(harness_shared, ID_COW)
    assert harness_shared.router_refcount(shared_router) == 1
    with boa.env.prank(keeper):
        harness_shared.sync_router_approvals(shared_router, [token_a.address])
    assert token_a.allowance(harness_shared.address, shared_router) == MAX_UINT256

    disable(harness_shared, ID_SIG)
    assert harness_shared.router_refcount(shared_router) == 0
    with boa.env.prank(keeper):
        harness_shared.sync_router_approvals(shared_router, [token_a.address])
    assert token_a.allowance(harness_shared.address, shared_router) == 0


# Permissionless sync_router_approvals


def test_sync_grants_as_retry_path(harness, enable, stage, keeper, token_a, token_b, permit2):
    # token_a was staged before permit2 gained a reference: sync tops it up.
    enable(harness, ID_COW)
    stage(harness, token_a)
    assert token_a.allowance(harness.address, permit2) == 0

    enable(harness, ID_SIG)
    with boa.env.prank(keeper):
        harness.sync_router_approvals(permit2, [token_a.address, token_b.address])
    events = router_approvals(harness)
    assert token_a.allowance(harness.address, permit2) == MAX_UINT256
    assert token_b.allowance(harness.address, permit2) == MAX_UINT256
    assert {log.token for log in events} == {token_a.address, token_b.address}
    assert all(log.amount == MAX_UINT256 for log in events)


def test_sync_revokes_after_release(harness, enable, disable, stage, keeper, token_a,
                                    cow_router):
    enable(harness, ID_COW)
    stage(harness, token_a)
    assert token_a.allowance(harness.address, cow_router) == MAX_UINT256

    disable(harness, ID_COW)
    with boa.env.prank(keeper):
        harness.sync_router_approvals(cow_router, [token_a.address])
    events = router_approvals(harness)
    assert token_a.allowance(harness.address, cow_router) == 0
    assert [(log.token, log.amount) for log in events] == [(token_a.address, 0)]


def test_sync_is_idempotent(harness, enable, stage, keeper, token_a, cow_router):
    enable(harness, ID_COW)
    stage(harness, token_a)
    with boa.env.prank(keeper):
        harness.sync_router_approvals(cow_router, [token_a.address])
    assert router_approvals(harness) == []
    assert token_a.allowance(harness.address, cow_router) == MAX_UINT256


def test_sync_clears_residual_allowance_of_unreferenced_router(harness, keeper, problem_token):
    # Retired-router cleanup: the derived target for refcount 0 is zero even if
    # some residual allowance survived (e.g. a router retired mid-week).
    retired_router = boa.env.generate_address("retired_router")
    problem_token.set_allowance_for_testing(harness.address, retired_router, 7 * WAD)
    with boa.env.prank(keeper):
        harness.sync_router_approvals(retired_router, [problem_token.address])
    assert problem_token.allowance(harness.address, retired_router) == 0


def test_sync_rejects_zero_router(harness, keeper, token_a):
    with boa.env.prank(keeper):
        with boa.reverts(custom_err("BadRouter()")):
            harness.sync_router_approvals(ZERO_ADDRESS, [token_a.address])


def test_sync_rejects_target_token(harness, enable, keeper, want, token_a, cow_router):
    enable(harness, ID_COW)
    with boa.env.prank(keeper):
        with boa.reverts(custom_err("TargetToken()")):
            harness.sync_router_approvals(cow_router, [want.address])
        # The whole batch reverts atomically: no partial grants.
        with boa.reverts(custom_err("TargetToken()")):
            harness.sync_router_approvals(cow_router, [token_a.address, want.address])
    assert token_a.allowance(harness.address, cow_router) == 0


# Non-standard ERC-20 behavior


def test_usdt_style_approve_zero_then_max_on_sync(harness, enable, keeper, problem_token,
                                                  cow_router):
    # A nonzero residual allowance plus reset-required approval: reaching max
    # proves the approve(0)-then-approve(max) pattern.
    problem_token.set_requires_approval_reset(True)
    problem_token.set_allowance_for_testing(harness.address, cow_router, 7 * WAD)
    enable(harness, ID_COW)
    with boa.env.prank(keeper):
        harness.sync_router_approvals(cow_router, [problem_token.address])
    assert problem_token.allowance(harness.address, cow_router) == MAX_UINT256


def test_usdt_style_approve_zero_then_max_on_stage(harness, enable, stage, problem_token,
                                                   cow_router):
    problem_token.set_requires_approval_reset(True)
    problem_token.set_allowance_for_testing(harness.address, cow_router, 7 * WAD)
    enable(harness, ID_COW)
    stage(harness, problem_token)
    assert problem_token.allowance(harness.address, cow_router) == MAX_UINT256


def test_approval_failure_does_not_block_staging(harness, enable, solver, want, problem_token,
                                                 keeper, cow_router):
    # Staging is best-effort (§4.3 isolation): a token whose approve fails is
    # staged anyway with the allowance grant silently skipped; the native rail
    # keeps working and sync stays the strict, loudly-reverting retry path.
    enable(harness, ID_COW)
    # approve(0) succeeds but approve(max) is rejected (e.g. uint96 tokens);
    # transfers keep working, so native take stays available.
    problem_token.set_fails_nonzero_approval(True)
    problem_token._mint_for_testing(harness.address, LOT_AMOUNT)
    harness.stage(problem_token.address)
    assert router_approvals(harness) == []
    lot = harness.lots(problem_token.address)
    assert lot[LOT_EPOCH] != 0
    assert lot[LOT_INITIAL_AMOUNT] == LOT_AMOUNT
    assert problem_token.allowance(harness.address, cow_router) == 0

    # Native take is unaffected by the failed router grant.
    taken = LOT_AMOUNT // 5
    payment = harness.getAmountNeeded(problem_token.address, taken)
    want._mint_for_testing(solver, payment)
    with boa.env.prank(solver):
        want.approve(harness.address, payment)
        harness.take(problem_token.address, taken, solver, b"")
    assert problem_token.balanceOf(solver) == taken

    # The permissionless retry path still surfaces the failure loudly.
    with boa.env.prank(keeper):
        with boa.reverts(custom_err("ApproveFailed()")):
            harness.sync_router_approvals(cow_router, [problem_token.address])


def test_broken_token_does_not_affect_other_tokens(harness, enable, stage, problem_token,
                                                   token_a, cow_router):
    # §4.3: one token's approval failure must not degrade another rail/token.
    enable(harness, ID_COW)
    # returns_false breaks even the approve(0) reset: the grant is skipped
    # at the first best-effort step.
    problem_token.set_returns_false(True)
    problem_token._mint_for_testing(harness.address, LOT_AMOUNT)
    harness.stage(problem_token.address)
    assert router_approvals(harness) == []
    assert harness.lots(problem_token.address)[LOT_EPOCH] != 0
    assert problem_token.allowance(harness.address, cow_router) == 0

    stage(harness, token_a)
    assert harness.lots(token_a.address)[LOT_EPOCH] != 0
    assert token_a.allowance(harness.address, cow_router) == MAX_UINT256


def test_approve_max_failure_reverts_distinctly(harness, enable, keeper, problem_token,
                                                cow_router):
    # approve(0) passes but approve(max) is rejected (e.g. uint96 tokens).
    enable(harness, ID_COW)
    problem_token.set_fails_nonzero_approval(True)
    with boa.env.prank(keeper):
        with boa.reverts(custom_err("ApproveFailed()")):
            harness.sync_router_approvals(cow_router, [problem_token.address])
    assert problem_token.allowance(harness.address, cow_router) == 0


# _resolve_router failure modes


def test_enable_cow_mode_requires_cow_router(owner, want, proceeds_receiver, registry):
    routerless = deploy_harness(owner, want, proceeds_receiver, registry, ZERO_ADDRESS,
                                ZERO_ADDRESS)
    with boa.env.prank(owner):
        with boa.reverts(custom_err("CowRouterUnset()")):
            routerless.enable_adapter(ID_COW)


@pytest.mark.parametrize("adapter_id", [ID_SIG, ID_ALLOW], ids=["signature", "allowance"])
def test_enable_permit2_mode_requires_permit2(owner, want, proceeds_receiver, registry,
                                              cow_router, adapter_id):
    permit2less = deploy_harness(owner, want, proceeds_receiver, registry, ZERO_ADDRESS,
                                 cow_router)
    with boa.env.prank(owner):
        with boa.reverts(custom_err("Permit2Unset()")):
            permit2less.enable_adapter(adapter_id)


@pytest.mark.parametrize("bad_mode", [4, 7, 255])
def test_enable_rejects_unknown_mode_from_registry(owner, want, proceeds_receiver, permit2,
                                                   cow_router, validator_mock, bad_mode):
    # The real registry rejects unknown modes at set_adapter; a hostile or
    # buggy registry must still be stopped by the auction's closed enum.
    registry_mock = boa.loads(REGISTRY_MOCK_SOURCE, name="RegistryMock",
                              filename="RegistryMock.vy", no_vvm=True)
    registry_mock.set_config(
        (
            validator_mock.address,
            codehash_of(validator_mock.address),
            boa.env.generate_address("verifier"),
            boa.env.generate_address("executor"),
            bad_mode,
            True,
            True,  # active
            1,
        )
    )
    harness = deploy_harness(owner, want, proceeds_receiver, registry_mock, permit2, cow_router)
    with boa.env.prank(owner):
        with boa.reverts(custom_err("BadAuthorizationMode()")):
            harness.enable_adapter(ID_COW)
