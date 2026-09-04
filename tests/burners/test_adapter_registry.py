from typing import Any

import boa
import pytest

from .conftest import custom_err


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Mirrors constants.MAX_ADAPTERS: the registry catalog bound.
MAX_ADAPTERS = 32

# AdapterConfig tuple fields.
CONFIG_EXECUTOR = 0
CONFIG_ACTIVE = 1


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
def executor():
    return boa.env.generate_address("executor")


@pytest.fixture(scope="module")
def role_source(owner, emergency_owner):
    return boa.load(
        "contracts/testing/dutch_auction/RoleSourceMock.vy", owner, emergency_owner
    )


@pytest.fixture(scope="module")
def adapter_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/AdapterMock.vy")


@pytest.fixture
def adapter(adapter_deployer):
    return adapter_deployer.deploy()


@pytest.fixture
def registry(role_source):
    return boa.load(
        "contracts/burners/auction/adapters/AdapterRegistry.vy", role_source.address
    )


def _register(registry: Any, owner: str, adapter: Any, executor: str) -> None:
    with boa.env.prank(owner):
        registry.set_adapter(adapter, executor)


# Constructor and roles


def test_constructor_derives_roles_from_source(registry, role_source, owner, emergency_owner):
    assert registry.role_source() == role_source.address
    assert registry.owner() == owner
    assert registry.emergency_owner() == emergency_owner


def test_constructor_rejects_source_with_zero_owner(emergency_owner):
    bad_source = boa.load(
        "contracts/testing/dutch_auction/RoleSourceMock.vy", ZERO_ADDRESS, emergency_owner
    )
    with boa.reverts():
        boa.load("contracts/burners/auction/adapters/AdapterRegistry.vy", bad_source.address)


def test_constructor_allows_zero_emergency_owner_sentinel(owner):
    source = boa.load(
        "contracts/testing/dutch_auction/RoleSourceMock.vy", owner, ZERO_ADDRESS
    )
    registry = boa.load(
        "contracts/burners/auction/adapters/AdapterRegistry.vy", source.address
    )
    assert registry.emergency_owner() == ZERO_ADDRESS


# Catalog writes


def test_unknown_adapter_returns_zeroed_config(registry, adapter):
    config = registry.get_adapter(adapter)
    assert config[CONFIG_EXECUTOR] == ZERO_ADDRESS
    assert config[CONFIG_ACTIVE] is False


def test_set_adapter_stores_config_inactive(registry, owner, adapter, executor):
    _register(registry, owner, adapter, executor)
    logs = registry.get_logs()
    stored = next(log for log in logs if type(log).__name__.endswith("AdapterSet"))
    assert stored.adapter == adapter.address
    assert stored.executor == executor

    config = registry.get_adapter(adapter)
    assert config[CONFIG_EXECUTOR] == executor
    # Activation is a separate owner step: a mistaken set cannot go live at once.
    assert config[CONFIG_ACTIVE] is False


def test_set_adapter_only_owner(registry, adapter, executor, attacker, emergency_owner):
    for account in (attacker, emergency_owner):
        with boa.env.prank(account), boa.reverts(custom_err("OnlyOwner()")):
            registry.set_adapter(adapter, executor)


def test_set_adapter_rejects_zero_adapter(registry, owner, executor):
    with boa.env.prank(owner), boa.reverts(custom_err("BadAdapter()")):
        registry.set_adapter(ZERO_ADDRESS, executor)


def test_set_adapter_with_zero_executor_removes_inactive_entry(registry, owner, adapter, executor):
    with boa.env.prank(owner):
        registry.set_adapter(adapter, executor)
        registry.activate_adapter(adapter)
    # An active adapter must be disabled before it can be removed.
    with boa.env.prank(owner), boa.reverts(custom_err("AlreadyActive()")):
        registry.set_adapter(adapter, ZERO_ADDRESS)
    with boa.env.prank(owner):
        registry.disable_adapter(adapter)
        registry.set_adapter(adapter, ZERO_ADDRESS)
    assert tuple(registry.get_adapter(adapter)) == (ZERO_ADDRESS, False)
    assert registry.get_adapters() == []
    with boa.env.prank(owner), boa.reverts(custom_err("UnknownAdapter()")):
        registry.activate_adapter(adapter)
    # Re-registering lists it again, once.
    with boa.env.prank(owner):
        registry.set_adapter(adapter, executor)
        registry.set_adapter(adapter, executor)
    assert registry.get_adapters() == [adapter.address]


def test_set_adapter_repoints_inactive_entries_only(registry, owner, adapter, executor):
    _register(registry, owner, adapter, executor)
    other_executor = boa.env.generate_address("other_executor")
    # An inactive entry can be repointed without growing the catalog.
    _register(registry, owner, adapter, other_executor)
    assert registry.get_adapter(adapter)[CONFIG_EXECUTOR] == other_executor
    assert registry.get_adapter(adapter)[CONFIG_ACTIVE] is False
    assert registry.get_adapters() == [adapter.address]

    # Once active, the executor reference is pinned until a disable.
    with boa.env.prank(owner):
        registry.activate_adapter(adapter)
        with boa.reverts(custom_err("AlreadyActive()")):
            registry.set_adapter(adapter, executor)
        registry.disable_adapter(adapter)
        registry.set_adapter(adapter, executor)
    assert registry.get_adapter(adapter)[CONFIG_EXECUTOR] == executor
    assert registry.is_executor_active(other_executor) is False


# Activation lifecycle


def _event(registry: Any, name: str) -> Any:
    return next(log for log in registry.get_logs() if type(log).__name__.endswith(name))


def test_activate_disable_reactivate_lifecycle(
    registry, owner, emergency_owner, adapter, executor
):
    """The flag and the executor reference move together through the cycle,
    each step announced by its event."""
    _register(registry, owner, adapter, executor)
    assert registry.get_adapter(adapter)[CONFIG_ACTIVE] is False
    assert registry.is_executor_active(executor) is False

    with boa.env.prank(owner):
        registry.activate_adapter(adapter)
    assert _event(registry, "AdapterActivated").adapter == adapter.address
    assert registry.get_adapter(adapter)[CONFIG_ACTIVE] is True
    assert registry.is_executor_active(executor) is True

    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter)
    assert _event(registry, "AdapterDisabled").adapter == adapter.address
    assert registry.get_adapter(adapter)[CONFIG_ACTIVE] is False
    assert registry.is_executor_active(executor) is False

    # Only the owner reactivates after an emergency disable.
    with boa.env.prank(owner):
        registry.activate_adapter(adapter)
    assert _event(registry, "AdapterActivated").adapter == adapter.address
    assert registry.get_adapter(adapter)[CONFIG_ACTIVE] is True
    assert registry.is_executor_active(executor) is True


def test_activate_adapter_only_owner(
    registry, owner, adapter, executor, attacker, emergency_owner
):
    _register(registry, owner, adapter, executor)
    for account in (attacker, emergency_owner):
        with boa.env.prank(account), boa.reverts(custom_err("OnlyOwner()")):
            registry.activate_adapter(adapter)


def test_activate_unknown_adapter_reverts(registry, owner, adapter):
    with boa.env.prank(owner), boa.reverts(custom_err("UnknownAdapter()")):
        registry.activate_adapter(adapter)


def test_activate_active_adapter_reverts(registry, owner, adapter, executor):
    _register(registry, owner, adapter, executor)
    with boa.env.prank(owner):
        registry.activate_adapter(adapter)
        with boa.reverts(custom_err("AlreadyActive()")):
            registry.activate_adapter(adapter)


@pytest.mark.parametrize("role", ["owner", "emergency_owner"])
def test_disable_adapter_by_each_role(
    registry, owner, emergency_owner, adapter, executor, role
):
    _register(registry, owner, adapter, executor)
    with boa.env.prank(owner):
        registry.activate_adapter(adapter)

    account = owner if role == "owner" else emergency_owner
    with boa.env.prank(account):
        registry.disable_adapter(adapter)
    assert registry.get_adapter(adapter)[CONFIG_ACTIVE] is False
    # The executor pin survives a disable: entries are set-once.
    assert registry.get_adapter(adapter)[CONFIG_EXECUTOR] == executor


def test_disable_adapter_rejects_outsider(registry, owner, adapter, executor, attacker):
    _register(registry, owner, adapter, executor)
    with boa.env.prank(owner):
        registry.activate_adapter(adapter)
    with boa.env.prank(attacker), boa.reverts(custom_err("OnlyOwnerOrEmergency()")):
        registry.disable_adapter(adapter)


def test_disable_inactive_adapter_reverts(registry, owner, adapter, executor):
    _register(registry, owner, adapter, executor)
    with boa.env.prank(owner), boa.reverts(custom_err("NotActive()")):
        registry.disable_adapter(adapter)


# Executor activity: the flag auctions read to drive executor allowances


def test_unknown_executor_is_inactive(registry, executor, adapter, owner):
    assert registry.is_executor_active(executor) is False
    assert registry.is_executor_active(boa.env.generate_address("nobody")) is False
    # Registration alone references nothing: only activation counts.
    _register(registry, owner, adapter, executor)
    assert registry.is_executor_active(executor) is False


def test_shared_executor_stays_active_until_every_adapter_disabled(
    registry, owner, adapter_deployer, executor
):
    first = adapter_deployer.deploy()
    second = adapter_deployer.deploy()
    _register(registry, owner, first, executor)
    _register(registry, owner, second, executor)

    with boa.env.prank(owner):
        registry.activate_adapter(first)
        assert registry.is_executor_active(executor) is True
        registry.activate_adapter(second)
        assert registry.is_executor_active(executor) is True
        # One shared executor (Permit2-style): still referenced by the second
        # adapter after the first is disabled.
        registry.disable_adapter(first)
        assert registry.is_executor_active(executor) is True
        registry.disable_adapter(second)
        assert registry.is_executor_active(executor) is False


def test_executor_activity_is_per_executor(registry, owner, adapter_deployer, executor):
    first = adapter_deployer.deploy()
    second = adapter_deployer.deploy()
    other_executor = boa.env.generate_address("other_executor")
    _register(registry, owner, first, executor)
    _register(registry, owner, second, other_executor)
    # Entries are independent: each adapter keeps its own executor pin.
    assert registry.get_adapter(first)[CONFIG_EXECUTOR] == executor
    assert registry.get_adapter(second)[CONFIG_EXECUTOR] == other_executor
    with boa.env.prank(owner):
        registry.activate_adapter(first)
        registry.activate_adapter(second)
    assert registry.is_executor_active(executor) is True
    assert registry.is_executor_active(other_executor) is True
    with boa.env.prank(owner):
        registry.disable_adapter(first)
    assert registry.is_executor_active(executor) is False
    assert registry.is_executor_active(other_executor) is True


# Adapter list: every adapter with an executor set; removal (executor = 0)
# drops it, order is not guaranteed.


def test_get_adapters_is_empty_by_default(registry):
    assert registry.get_adapters() == []


def test_get_adapters_grows_on_set_not_on_activate(registry, owner, adapter, executor):
    _register(registry, owner, adapter, executor)
    assert registry.get_adapters() == [adapter.address]
    with boa.env.prank(owner):
        registry.activate_adapter(adapter)
    assert registry.get_adapters() == [adapter.address]


def test_get_adapters_preserves_registration_order(
    registry, owner, adapter_deployer, executor
):
    adapters = [adapter_deployer.deploy() for _ in range(3)]
    # Register out of deployment order to prove the list follows set order.
    ordered = [adapters[2], adapters[0], adapters[1]]
    for entry in ordered:
        _register(registry, owner, entry, executor)
    assert registry.get_adapters() == [entry.address for entry in ordered]


def test_disable_does_not_remove_from_get_adapters(
    registry, owner, emergency_owner, adapter, executor
):
    _register(registry, owner, adapter, executor)
    with boa.env.prank(owner):
        registry.activate_adapter(adapter)
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter)
    assert registry.get_adapters() == [adapter.address]
    assert registry.get_adapter(adapter)[CONFIG_ACTIVE] is False


def test_set_adapter_reverts_past_max_adapters(registry, owner, adapter_deployer, executor):
    for _ in range(MAX_ADAPTERS):
        _register(registry, owner, adapter_deployer.deploy(), executor)
    assert len(registry.get_adapters()) == MAX_ADAPTERS
    # The catalog bound is the DynArray's own: a plain revert, no typed error.
    with boa.env.prank(owner), boa.reverts():
        registry.set_adapter(adapter_deployer.deploy(), executor)
    assert len(registry.get_adapters()) == MAX_ADAPTERS


# Live roles


def test_roles_follow_source_owner_change(registry, role_source, owner, attacker, adapter, executor):
    role_source.set_owner(attacker)
    with boa.env.prank(owner), boa.reverts(custom_err("OnlyOwner()")):
        registry.set_adapter(adapter, executor)
    with boa.env.prank(attacker):
        registry.set_adapter(adapter, executor)
    assert registry.get_adapter(adapter)[CONFIG_EXECUTOR] == executor


def test_roles_follow_source_emergency_owner_change(
    registry, role_source, owner, emergency_owner, attacker, adapter, executor
):
    _register(registry, owner, adapter, executor)
    with boa.env.prank(owner):
        registry.activate_adapter(adapter)
    role_source.set_emergency_owner(attacker)
    with boa.env.prank(emergency_owner), boa.reverts(custom_err("OnlyOwnerOrEmergency()")):
        registry.disable_adapter(adapter)
    with boa.env.prank(attacker):
        registry.disable_adapter(adapter)
    assert registry.get_adapter(adapter)[CONFIG_ACTIVE] is False
