"""Registry-authoritative router consistency and refresh_adapter_router.

A registry version update may change an adapter's authorization mode. The
dispatcher must reject the adapter's signatures while the cached router
disagrees with the live config — a mode change can never settle through stale
allowance state — and the permissionless refresh_adapter_router realigns the
cache, the refcounts, and (via sync_router_approvals) the allowances.
"""

from typing import Any

import boa
import pytest
from eth_abi import encode
from eth_hash.auto import keccak

from .conftest import custom_err


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
MAX_UINT256 = 2**256 - 1
WAD = 10**18

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

ADAPTER_ID = keccak(b"REFRESH_ADAPTER")[:4]
MODE_NONE = 0
MODE_COW_VAULT_RELAYER = 1
MODE_PERMIT2_SIGNATURE_TRANSFER = 2
MODE_PERMIT2_ALLOWANCE_TRANSFER = 3

# Arbitrary protocol digest for the mock-validator path; the mock echoes it.
DIGEST = keccak(b"protocol order digest")

# Lot struct tuple indices fixed by the core ABI.
LOT_EPOCH = 0
LOT_INITIAL_AMOUNT = 1
LOT_END = 6


def encode_envelope(adapter_id: bytes, adapter_version: int, payload: bytes = b"") -> bytes:
    return (
        ENVELOPE_MAGIC
        + ENVELOPE_VERSION.to_bytes(1, "big")
        + adapter_id
        + adapter_version.to_bytes(2, "big")
        + payload
    )


def event_name(log: Any) -> str:
    event_type = getattr(log, "event_type", None)
    return event_type.name if event_type is not None else type(log).__name__


def refresh_events(contract) -> list:
    # boa keeps only the logs of the latest transaction, so read immediately.
    return [log for log in contract.get_logs() if event_name(log) == "AdapterRouterRefreshed"]


def codehash_of(address: str) -> bytes:
    return keccak(boa.env.get_code(address))


def is_valid(harness, signature: bytes, digest: bytes = DIGEST) -> bytes:
    return bytes(harness.isValidSignature(digest, signature))


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
    # Permissionless-path caller: never the owner or the emergency owner.
    return boa.env.generate_address("keeper")


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
def validator_mock():
    return boa.load("contracts/testing/dutch_auction/OrderValidatorMock.vy")


@pytest.fixture(scope="module")
def registry(owner, emergency_owner):
    with boa.env.prank(owner):
        return boa.load("contracts/AdapterRegistry.vy", owner, emergency_owner)


@pytest.fixture(scope="module")
def harness(owner, emergency_owner, want, proceeds_receiver, registry, permit2, cow_router):
    with boa.env.prank(owner):
        harness = boa.load(
            "contracts/testing/dutch_auction/CoreHarness.vy",
            want.address,
            proceeds_receiver,
            registry.address,
            permit2,
            START_TOTAL,
            FLOOR_TOTAL,
            DECAY_FACTOR_RAY,
            STEP_DURATION,
        )
        harness.set_emergency_owner(emergency_owner)
        harness.set_cow_router(cow_router)
    return harness


@pytest.fixture(scope="module")
def set_version(registry, owner, validator_mock, mock_verifier, mock_executor):
    """Register (and by default activate) an adapter version on the registry."""

    def _set_version(mode: int, version: int, *, activate: bool = True):
        config = (
            validator_mock.address,
            codehash_of(validator_mock.address),
            mock_verifier,
            mock_executor,
            mode,
            True,  # allow_partial_fills
            False,  # active is ignored by set_adapter
            version,
        )
        with boa.env.prank(owner):
            registry.set_adapter(ADAPTER_ID, config)
            if activate:
                registry.activate_adapter(ADAPTER_ID)

    return _set_version


@pytest.fixture(scope="module")
def enable_v1(harness, set_version, owner):
    """Register, activate, and enable version 1 of the test adapter."""

    def _enable_v1(mode: int):
        set_version(mode, 1)
        with boa.env.prank(owner):
            harness.enable_adapter(ADAPTER_ID)

    return _enable_v1


@pytest.fixture(scope="module")
def stage():
    def _stage(harness, token, amount: int = LOT_AMOUNT):
        token._mint_for_testing(harness.address, amount)
        harness.stage(token.address)
        lot = harness.lots(token.address)
        # Extend with the epoch window so LOT_END keeps indexing correctly.
        return (*lot, *harness.epoch_bounds(lot[0]))

    return _stage


@pytest.fixture(scope="module")
def arm_signature(
    harness, validator_mock, sell_token, want, proceeds_receiver, mock_verifier, mock_executor
):
    """Load the mock with a fully valid order bound to an adapter version.

    Returns the matching envelope signature: with the order armed, any
    remaining invalidity comes from the dispatcher's own checks — here the
    registry-authoritative router-consistency gate.
    """

    def _arm_signature(adapter_version: int) -> bytes:
        lot = harness.lots(sell_token.address)
        lot = (*lot, *harness.epoch_bounds(lot[0]))
        context_hash = keccak(
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
                    ADAPTER_ID,
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
        sell_amount = LOT_AMOUNT // 2
        validator_mock.set_order(
            [
                DIGEST,
                context_hash,
                lot[LOT_EPOCH],
                sell_token.address,
                want.address,
                proceeds_receiver,
                mock_verifier,
                mock_executor,
                sell_amount,
                harness.quote(sell_token.address, sell_amount),
                lot[LOT_END],
                True,
            ]
        )
        return encode_envelope(ADAPTER_ID, adapter_version)

    return _arm_signature


def assert_refreshed(harness, version: int, old_router: str, new_router: str):
    events = refresh_events(harness)
    assert len(events) == 1
    event = events[0]
    assert bytes(event.adapter_id) == ADAPTER_ID
    assert event.version == version
    assert event.old_router == old_router
    assert event.new_router == new_router


# Mode changes across registry versions: invalid until refreshed


def test_cow_to_permit2_requires_refresh(harness, registry, enable_v1, set_version, stage,
                                         arm_signature, sell_token, keeper, cow_router,
                                         permit2):
    enable_v1(MODE_COW_VAULT_RELAYER)
    stage(harness, sell_token)
    assert is_valid(harness, arm_signature(1)) == ERC1271_MAGIC

    set_version(MODE_PERMIT2_SIGNATURE_TRANSFER, 2)
    signature_v2 = arm_signature(2)
    # The registry is authoritative: the cached router disagrees with the live
    # mode, so the otherwise fully valid v2 order must read invalid.
    assert is_valid(harness, signature_v2) == INVALID_SIGNATURE
    assert harness.adapter_router(ADAPTER_ID) == cow_router
    assert harness.router_refcount(cow_router) == 1
    assert harness.router_refcount(permit2) == 0

    with boa.env.prank(keeper):
        harness.refresh_adapter_router(ADAPTER_ID)
    assert_refreshed(harness, 2, cow_router, permit2)
    assert harness.adapter_router(ADAPTER_ID) == permit2
    assert harness.router_refcount(cow_router) == 0
    assert harness.router_refcount(permit2) == 1
    assert is_valid(harness, signature_v2) == ERC1271_MAGIC


def test_none_to_cow_requires_refresh(harness, registry, enable_v1, set_version, stage,
                                      arm_signature, sell_token, keeper, cow_router):
    enable_v1(MODE_NONE)
    stage(harness, sell_token)
    assert is_valid(harness, arm_signature(1)) == ERC1271_MAGIC
    assert harness.adapter_router(ADAPTER_ID) == ZERO_ADDRESS

    set_version(MODE_COW_VAULT_RELAYER, 2)
    signature_v2 = arm_signature(2)
    assert is_valid(harness, signature_v2) == INVALID_SIGNATURE

    with boa.env.prank(keeper):
        harness.refresh_adapter_router(ADAPTER_ID)
    assert_refreshed(harness, 2, ZERO_ADDRESS, cow_router)
    assert harness.adapter_router(ADAPTER_ID) == cow_router
    assert harness.router_refcount(cow_router) == 1
    assert is_valid(harness, signature_v2) == ERC1271_MAGIC


def test_cow_to_none_requires_refresh(harness, registry, enable_v1, set_version, stage,
                                      arm_signature, sell_token, keeper, cow_router):
    enable_v1(MODE_COW_VAULT_RELAYER)
    stage(harness, sell_token)
    assert is_valid(harness, arm_signature(1)) == ERC1271_MAGIC

    set_version(MODE_NONE, 2)
    signature_v2 = arm_signature(2)
    assert is_valid(harness, signature_v2) == INVALID_SIGNATURE

    with boa.env.prank(keeper):
        harness.refresh_adapter_router(ADAPTER_ID)
    assert_refreshed(harness, 2, cow_router, ZERO_ADDRESS)
    assert harness.adapter_router(ADAPTER_ID) == ZERO_ADDRESS
    assert harness.router_refcount(cow_router) == 0
    assert is_valid(harness, signature_v2) == ERC1271_MAGIC


@pytest.mark.parametrize("mode", [MODE_NONE, MODE_COW_VAULT_RELAYER], ids=["none", "cow"])
def test_same_mode_upgrade_needs_no_refresh(harness, registry, enable_v1, set_version, stage,
                                            arm_signature, sell_token, keeper, cow_router,
                                            mode):
    enable_v1(mode)
    stage(harness, sell_token)
    expected_router = cow_router if mode == MODE_COW_VAULT_RELAYER else ZERO_ADDRESS
    expected_refcount = harness.router_refcount(cow_router)

    set_version(mode, 2)
    # The router did not move, so the consistency gate passes immediately.
    assert is_valid(harness, arm_signature(2)) == ERC1271_MAGIC

    # Refresh is a silent no-op: no event, no cache or refcount movement.
    with boa.env.prank(keeper):
        harness.refresh_adapter_router(ADAPTER_ID)
    assert refresh_events(harness) == []
    assert harness.adapter_router(ADAPTER_ID) == expected_router
    assert harness.router_refcount(cow_router) == expected_refcount


# refresh_adapter_router failure modes


def test_refresh_reverts_for_non_enabled_adapter(harness, keeper):
    with boa.env.prank(keeper):
        with boa.reverts(custom_err("NotEnabled()")):
            harness.refresh_adapter_router(keccak(b"NEVER_ENABLED")[:4])


def test_refresh_reverts_while_new_version_inactive(harness, registry, enable_v1, set_version,
                                                    keeper, owner):
    enable_v1(MODE_COW_VAULT_RELAYER)
    # A freshly set version is stored inactive until the separate activation
    # step; refresh must not realign against a config that cannot validate.
    set_version(MODE_PERMIT2_SIGNATURE_TRANSFER, 2, activate=False)
    with boa.env.prank(keeper):
        with boa.reverts(custom_err("InactiveAdapter()")):
            harness.refresh_adapter_router(ADAPTER_ID)
    # Activation unblocks the same call.
    with boa.env.prank(owner):
        registry.activate_adapter(ADAPTER_ID)
    with boa.env.prank(keeper):
        harness.refresh_adapter_router(ADAPTER_ID)
    assert harness.adapter_router(ADAPTER_ID) == harness.permit2()


def test_refresh_reverts_when_new_router_unresolvable(harness, registry, enable_v1, set_version,
                                                      keeper, owner, cow_router):
    # Strict resolution: a live MODE_COW config with the hook answering empty
    # must revert loudly instead of caching the empty router.
    enable_v1(MODE_COW_VAULT_RELAYER)
    with boa.env.prank(owner):
        harness.set_cow_router(ZERO_ADDRESS)
    with boa.env.prank(keeper):
        with boa.reverts(custom_err("CowRouterUnset()")):
            harness.refresh_adapter_router(ADAPTER_ID)
    assert harness.adapter_router(ADAPTER_ID) == cow_router


# Allowance flow around a refresh


def test_sync_moves_allowances_after_refresh(harness, registry, enable_v1, set_version, stage,
                                             sell_token, keeper, cow_router, permit2):
    enable_v1(MODE_COW_VAULT_RELAYER)
    stage(harness, sell_token)
    assert sell_token.allowance(harness.address, cow_router) == MAX_UINT256
    assert sell_token.allowance(harness.address, permit2) == 0

    set_version(MODE_PERMIT2_ALLOWANCE_TRANSFER, 2)
    with boa.env.prank(keeper):
        harness.refresh_adapter_router(ADAPTER_ID)
        # Old router is fully released: the derived sync target is zero.
        harness.sync_router_approvals(cow_router, [sell_token.address])
    assert sell_token.allowance(harness.address, cow_router) == 0
    with boa.env.prank(keeper):
        # New router is referenced: sync tops the already-staged token up.
        harness.sync_router_approvals(permit2, [sell_token.address])
    assert sell_token.allowance(harness.address, permit2) == MAX_UINT256


def test_staging_grants_new_router_after_refresh(harness, registry, enable_v1, set_version,
                                                 stage, erc20_deployer, keeper, cow_router,
                                                 permit2):
    enable_v1(MODE_COW_VAULT_RELAYER)
    set_version(MODE_PERMIT2_SIGNATURE_TRANSFER, 2)
    with boa.env.prank(keeper):
        harness.refresh_adapter_router(ADAPTER_ID)

    fresh_token = erc20_deployer.deploy("Fresh", "FRESH", 18)
    stage(harness, fresh_token)
    assert fresh_token.allowance(harness.address, permit2) == MAX_UINT256
    assert fresh_token.allowance(harness.address, cow_router) == 0


def test_mode_change_invalid_despite_stale_allowance(harness, registry, enable_v1, set_version,
                                                     stage, arm_signature, sell_token,
                                                     cow_router):
    # The security property: between the registry update and the refresh the
    # old router still holds a live max allowance, and exactly then every
    # adapter signature — old or new version — must read invalid.
    enable_v1(MODE_COW_VAULT_RELAYER)
    stage(harness, sell_token)
    signature_v1 = arm_signature(1)
    assert is_valid(harness, signature_v1) == ERC1271_MAGIC

    set_version(MODE_PERMIT2_SIGNATURE_TRANSFER, 2)
    assert sell_token.allowance(harness.address, cow_router) == MAX_UINT256
    assert is_valid(harness, arm_signature(2)) == INVALID_SIGNATURE
    # The stale v1 signature dies with the version bump as well: nothing can
    # settle through the stale allowance.
    assert is_valid(harness, signature_v1) == INVALID_SIGNATURE
