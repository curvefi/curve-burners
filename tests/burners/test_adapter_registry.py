from typing import Any

import boa
import pytest

from .conftest import custom_err


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

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
def verifier_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/VerifierMock.vy")


@pytest.fixture
def verifier(verifier_deployer):
    return verifier_deployer.deploy()


@pytest.fixture
def registry(role_source):
    return boa.load(
        "contracts/burners/auction/adapters/AdapterRegistry.vy", role_source.address
    )


def _register(registry: Any, owner: str, verifier: Any, executor: str) -> None:
    with boa.env.prank(owner):
        registry.set_adapter(verifier, executor)


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


def test_unknown_verifier_returns_zeroed_config(registry, verifier):
    config = registry.get_adapter(verifier)
    assert config[CONFIG_EXECUTOR] == ZERO_ADDRESS
    assert config[CONFIG_ACTIVE] is False


def test_set_adapter_stores_config_inactive(registry, owner, verifier, executor):
    _register(registry, owner, verifier, executor)
    logs = registry.get_logs()
    stored = next(log for log in logs if type(log).__name__.endswith("AdapterSet"))
    assert stored.verifier == verifier.address
    assert stored.executor == executor

    config = registry.get_adapter(verifier)
    assert config[CONFIG_EXECUTOR] == executor
    # Activation is a separate owner step: a mistaken set cannot go live at once.
    assert config[CONFIG_ACTIVE] is False


def test_set_adapter_only_owner(registry, verifier, executor, attacker, emergency_owner):
    for account in (attacker, emergency_owner):
        with boa.env.prank(account), boa.reverts(custom_err("OnlyOwner()")):
            registry.set_adapter(verifier, executor)


def test_set_adapter_rejects_zero_verifier(registry, owner, executor):
    with boa.env.prank(owner), boa.reverts(custom_err("ZeroVerifier()")):
        registry.set_adapter(ZERO_ADDRESS, executor)


def test_set_adapter_rejects_eoa_verifier(registry, owner, executor):
    eoa = boa.env.generate_address("eoa_verifier")
    with boa.env.prank(owner), boa.reverts(custom_err("EmptyVerifier()")):
        registry.set_adapter(eoa, executor)


def test_set_adapter_rejects_zero_executor(registry, owner, verifier):
    with boa.env.prank(owner), boa.reverts(custom_err("ZeroExecutor()")):
        registry.set_adapter(verifier, ZERO_ADDRESS)


def test_set_adapter_is_immutable_once_set(registry, owner, verifier, executor):
    _register(registry, owner, verifier, executor)
    other_executor = boa.env.generate_address("other_executor")
    # Changed semantics always mean a new verifier deployment, never a rewrite.
    with boa.env.prank(owner), boa.reverts(custom_err("AlreadySet()")):
        registry.set_adapter(verifier, other_executor)
    with boa.env.prank(owner), boa.reverts(custom_err("AlreadySet()")):
        registry.set_adapter(verifier, executor)


def test_adapters_are_independent(registry, owner, verifier_deployer, executor):
    first = verifier_deployer.deploy()
    second = verifier_deployer.deploy()
    other_executor = boa.env.generate_address("other_executor")
    _register(registry, owner, first, executor)
    _register(registry, owner, second, other_executor)

    assert registry.get_adapter(first)[CONFIG_EXECUTOR] == executor
    assert registry.get_adapter(second)[CONFIG_EXECUTOR] == other_executor


# Activation lifecycle


def test_activate_adapter_happy_path(registry, owner, verifier, executor):
    _register(registry, owner, verifier, executor)
    with boa.env.prank(owner):
        registry.activate_adapter(verifier)
    assert registry.get_adapter(verifier)[CONFIG_ACTIVE] is True


def test_activate_adapter_only_owner(
    registry, owner, verifier, executor, attacker, emergency_owner
):
    _register(registry, owner, verifier, executor)
    for account in (attacker, emergency_owner):
        with boa.env.prank(account), boa.reverts(custom_err("OnlyOwner()")):
            registry.activate_adapter(verifier)


def test_activate_unknown_adapter_reverts(registry, owner, verifier):
    with boa.env.prank(owner), boa.reverts(custom_err("UnknownAdapter()")):
        registry.activate_adapter(verifier)


def test_activate_active_adapter_reverts(registry, owner, verifier, executor):
    _register(registry, owner, verifier, executor)
    with boa.env.prank(owner):
        registry.activate_adapter(verifier)
        with boa.reverts(custom_err("AlreadyActive()")):
            registry.activate_adapter(verifier)


@pytest.mark.parametrize("role", ["owner", "emergency_owner"])
def test_disable_adapter_by_each_role(
    registry, owner, emergency_owner, verifier, executor, role
):
    _register(registry, owner, verifier, executor)
    with boa.env.prank(owner):
        registry.activate_adapter(verifier)

    account = owner if role == "owner" else emergency_owner
    with boa.env.prank(account):
        registry.disable_adapter(verifier)
    assert registry.get_adapter(verifier)[CONFIG_ACTIVE] is False
    # The executor pin survives a disable: entries are set-once.
    assert registry.get_adapter(verifier)[CONFIG_EXECUTOR] == executor


def test_disable_adapter_rejects_outsider(registry, owner, verifier, executor, attacker):
    _register(registry, owner, verifier, executor)
    with boa.env.prank(owner):
        registry.activate_adapter(verifier)
    with boa.env.prank(attacker), boa.reverts(custom_err("OnlyOwnerOrEmergency()")):
        registry.disable_adapter(verifier)


def test_disable_inactive_adapter_reverts(registry, owner, verifier, executor):
    _register(registry, owner, verifier, executor)
    with boa.env.prank(owner), boa.reverts(custom_err("NotActive()")):
        registry.disable_adapter(verifier)


def test_owner_can_reactivate_after_emergency_disable(
    registry, owner, emergency_owner, verifier, executor
):
    _register(registry, owner, verifier, executor)
    with boa.env.prank(owner):
        registry.activate_adapter(verifier)
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(verifier)
    assert registry.get_adapter(verifier)[CONFIG_ACTIVE] is False
    with boa.env.prank(owner):
        registry.activate_adapter(verifier)
    assert registry.get_adapter(verifier)[CONFIG_ACTIVE] is True


# Live roles


def test_roles_follow_source_owner_change(registry, role_source, owner, attacker, verifier, executor):
    role_source.set_owner(attacker)
    with boa.env.prank(owner), boa.reverts(custom_err("OnlyOwner()")):
        registry.set_adapter(verifier, executor)
    with boa.env.prank(attacker):
        registry.set_adapter(verifier, executor)
    assert registry.get_adapter(verifier)[CONFIG_EXECUTOR] == executor


def test_roles_follow_source_emergency_owner_change(
    registry, role_source, owner, emergency_owner, attacker, verifier, executor
):
    _register(registry, owner, verifier, executor)
    with boa.env.prank(owner):
        registry.activate_adapter(verifier)
    role_source.set_emergency_owner(attacker)
    with boa.env.prank(emergency_owner), boa.reverts(custom_err("OnlyOwnerOrEmergency()")):
        registry.disable_adapter(verifier)
    with boa.env.prank(attacker):
        registry.disable_adapter(verifier)
    assert registry.get_adapter(verifier)[CONFIG_ACTIVE] is False
