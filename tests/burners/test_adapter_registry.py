from typing import Any

import boa
import pytest
from eth_hash.auto import keccak

from .conftest import custom_err


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ZERO_BYTES32 = bytes(32)
EMPTY_CODEHASH = keccak(b"")

MODE_NONE = 0
MODE_COW_VAULT_RELAYER = 1
MODE_PERMIT2_SIGNATURE_TRANSFER = 2
MODE_PERMIT2_ALLOWANCE_TRANSFER = 3

ADAPTER_ID = keccak(b"CURVE_COW_GPV2")[:4]
OTHER_ADAPTER_ID = keccak(b"OTHER_ADAPTER")[:4]


def event_name(log: Any) -> str:
    event_type = getattr(log, "event_type", None)
    return event_type.name if event_type is not None else type(log).__name__


def last_event(contract, name: str) -> Any:
    # boa keeps only the logs of the latest transaction, so this must run
    # before any further call (view calls included) touches the contract.
    return next(log for log in reversed(contract.get_logs()) if event_name(log) == name)


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
def attacker():
    return boa.env.generate_address("attacker")


@pytest.fixture(scope="module")
def verifier():
    return boa.env.generate_address("verifier")


@pytest.fixture(scope="module")
def executor():
    return boa.env.generate_address("executor")


@pytest.fixture(scope="module")
def validator(owner):
    # Any deployed contract with runtime code works as a validator stand-in;
    # the registry only pins and re-checks its codehash.
    with boa.env.prank(owner):
        return boa.load(
            "contracts/testing/dutch_auction/ComposableCowMock.vy", name="ValidatorMock"
        )


@pytest.fixture(scope="module")
def registry(owner, emergency_owner):
    with boa.env.prank(owner):
        return boa.load("contracts/AdapterRegistry.vy", owner, emergency_owner)


@pytest.fixture(scope="module")
def make_config(validator, verifier, executor):
    def _make_config(
        validator_address: str = None,
        validator_codehash: bytes = None,
        verifier_address: str = None,
        executor_address: str = None,
        authorization_mode: int = MODE_COW_VAULT_RELAYER,
        allow_partial_fills: bool = False,
        active: bool = False,
        version: int = 1,
    ) -> tuple:
        if validator_address is None:
            validator_address = validator.address
        if validator_codehash is None:
            validator_codehash = codehash_of(validator_address)
        return (
            validator_address,
            validator_codehash,
            verifier_address or verifier,
            executor_address or executor,
            authorization_mode,
            allow_partial_fills,
            active,
            version,
        )

    return _make_config


# Deployment


def test_constructor_sets_roles(registry, owner, emergency_owner):
    assert registry.owner() == owner
    assert registry.emergency_owner() == emergency_owner
    assert registry.future_owner() == ZERO_ADDRESS


def test_constructor_rejects_zero_owner(emergency_owner):
    with boa.reverts(custom_err("ZeroOwner()")):
        boa.load("contracts/AdapterRegistry.vy", ZERO_ADDRESS, emergency_owner)


def test_constructor_allows_zero_emergency_owner_sentinel(owner):
    registry = boa.load("contracts/AdapterRegistry.vy", owner, ZERO_ADDRESS)
    assert registry.emergency_owner() == ZERO_ADDRESS


# get_adapter


def test_unknown_adapter_returns_zeroed_config(registry):
    config = registry.get_adapter(ADAPTER_ID)
    assert config.validator == ZERO_ADDRESS
    assert bytes(config.validator_codehash) == ZERO_BYTES32
    assert config.verifier == ZERO_ADDRESS
    assert config.executor == ZERO_ADDRESS
    assert config.authorization_mode == MODE_NONE
    assert not config.allow_partial_fills
    assert not config.active
    assert config.version == 0


# set_adapter


def test_set_adapter_stores_config_inactive(
    registry, make_config, owner, validator, verifier, executor
):
    # active=True in the input must be ignored: activation is a separate step.
    with boa.env.prank(owner):
        registry.set_adapter(ADAPTER_ID, make_config(active=True, allow_partial_fills=True))
    event = last_event(registry, "AdapterSet")
    assert bytes(event.adapter_id) == ADAPTER_ID
    assert event.version == 1
    assert event.validator == validator.address

    config = registry.get_adapter(ADAPTER_ID)
    assert config.validator == validator.address
    assert bytes(config.validator_codehash) == codehash_of(validator.address)
    assert config.verifier == verifier
    assert config.executor == executor
    assert config.authorization_mode == MODE_COW_VAULT_RELAYER
    assert config.allow_partial_fills
    assert not config.active
    assert config.version == 1


def test_set_adapter_only_owner(registry, make_config, attacker, emergency_owner):
    for non_owner in (attacker, emergency_owner):
        with boa.env.prank(non_owner):
            with boa.reverts(custom_err("OnlyOwner()")):
                registry.set_adapter(ADAPTER_ID, make_config())


def test_adapter_cannot_self_register(registry, make_config, validator):
    # §5.3: the validator contract itself has no owner-independent write path.
    with boa.env.prank(validator.address):
        with boa.reverts(custom_err("OnlyOwner()")):
            registry.set_adapter(ADAPTER_ID, make_config())


def test_set_adapter_rejects_zero_adapter_id(registry, make_config, owner):
    with boa.env.prank(owner):
        with boa.reverts(custom_err("ZeroAdapterId()")):
            registry.set_adapter(bytes(4), make_config())


def test_set_adapter_rejects_zero_validator(registry, make_config, owner):
    with boa.env.prank(owner):
        with boa.reverts(custom_err("ZeroValidator()")):
            registry.set_adapter(
                ADAPTER_ID,
                make_config(validator_address=ZERO_ADDRESS, validator_codehash=EMPTY_CODEHASH),
            )


def test_set_adapter_rejects_wrong_codehash(registry, make_config, owner):
    with boa.env.prank(owner):
        with boa.reverts(custom_err("CodehashMismatch()")):
            registry.set_adapter(
                ADAPTER_ID, make_config(validator_codehash=keccak(b"not the code"))
            )


@pytest.mark.parametrize("claimed_codehash", [ZERO_BYTES32, EMPTY_CODEHASH])
def test_set_adapter_rejects_eoa_validator(registry, make_config, owner, claimed_codehash):
    # An EOA has no runtime code: whichever no-code hash form it reports, both
    # the match check and the emptiness checks keep it out.
    eoa_validator = boa.env.generate_address("eoa_validator")
    with boa.env.prank(owner):
        with boa.reverts():
            registry.set_adapter(
                ADAPTER_ID,
                make_config(validator_address=eoa_validator, validator_codehash=claimed_codehash),
            )


def test_set_adapter_rejects_zero_verifier(registry, make_config, owner):
    with boa.env.prank(owner):
        with boa.reverts(custom_err("ZeroVerifier()")):
            registry.set_adapter(ADAPTER_ID, make_config(verifier_address=ZERO_ADDRESS))


def test_set_adapter_rejects_zero_executor(registry, make_config, owner):
    with boa.env.prank(owner):
        with boa.reverts(custom_err("ZeroExecutor()")):
            registry.set_adapter(ADAPTER_ID, make_config(executor_address=ZERO_ADDRESS))


@pytest.mark.parametrize("mode", [MODE_PERMIT2_ALLOWANCE_TRANSFER + 1, 255])
def test_set_adapter_rejects_unknown_authorization_mode(registry, make_config, owner, mode):
    with boa.env.prank(owner):
        with boa.reverts(custom_err("BadMode()")):
            registry.set_adapter(ADAPTER_ID, make_config(authorization_mode=mode))


def test_set_adapter_accepts_every_closed_enum_mode(registry, make_config, owner):
    for offset, mode in enumerate(
        (
            MODE_NONE,
            MODE_COW_VAULT_RELAYER,
            MODE_PERMIT2_SIGNATURE_TRANSFER,
            MODE_PERMIT2_ALLOWANCE_TRANSFER,
        )
    ):
        with boa.env.prank(owner):
            registry.set_adapter(
                ADAPTER_ID, make_config(authorization_mode=mode, version=offset + 1)
            )
        assert registry.get_adapter(ADAPTER_ID).authorization_mode == mode


def test_set_adapter_version_must_strictly_grow(registry, make_config, owner):
    with boa.env.prank(owner):
        with boa.reverts(custom_err("VersionNotGrown()")):
            registry.set_adapter(ADAPTER_ID, make_config(version=0))

        registry.set_adapter(ADAPTER_ID, make_config(version=3))
        for version in (3, 2, 0):
            with boa.reverts(custom_err("VersionNotGrown()")):
                registry.set_adapter(ADAPTER_ID, make_config(version=version))

        registry.set_adapter(ADAPTER_ID, make_config(version=4))
    assert registry.get_adapter(ADAPTER_ID).version == 4


def test_set_adapter_update_deactivates_previous_version(registry, make_config, owner):
    with boa.env.prank(owner):
        registry.set_adapter(ADAPTER_ID, make_config(version=1))
        registry.activate_adapter(ADAPTER_ID)
    assert registry.get_adapter(ADAPTER_ID).active

    # New semantics get a new version and must go through activation again.
    with boa.env.prank(owner):
        registry.set_adapter(ADAPTER_ID, make_config(version=2))
    config = registry.get_adapter(ADAPTER_ID)
    assert config.version == 2
    assert not config.active


def test_adapter_ids_are_independent(registry, make_config, owner):
    with boa.env.prank(owner):
        registry.set_adapter(ADAPTER_ID, make_config(version=5))
    assert registry.get_adapter(OTHER_ADAPTER_ID).validator == ZERO_ADDRESS
    # A fresh id starts from version 0 regardless of other entries.
    with boa.env.prank(owner):
        registry.set_adapter(OTHER_ADAPTER_ID, make_config(version=1))
    assert registry.get_adapter(OTHER_ADAPTER_ID).version == 1


# activate_adapter


def test_activate_adapter_happy_path(registry, make_config, owner):
    with boa.env.prank(owner):
        registry.set_adapter(ADAPTER_ID, make_config(version=1))
        registry.activate_adapter(ADAPTER_ID)
    event = last_event(registry, "AdapterActivated")
    assert bytes(event.adapter_id) == ADAPTER_ID
    assert event.version == 1

    assert registry.get_adapter(ADAPTER_ID).active


def test_activate_adapter_only_owner(registry, make_config, owner, attacker, emergency_owner):
    with boa.env.prank(owner):
        registry.set_adapter(ADAPTER_ID, make_config())
    for non_owner in (attacker, emergency_owner):
        with boa.env.prank(non_owner):
            with boa.reverts(custom_err("OnlyOwner()")):
                registry.activate_adapter(ADAPTER_ID)


def test_activate_unknown_adapter_reverts(registry, owner):
    with boa.env.prank(owner):
        with boa.reverts(custom_err("UnknownAdapter()")):
            registry.activate_adapter(ADAPTER_ID)


def test_activate_active_adapter_reverts(registry, make_config, owner):
    with boa.env.prank(owner):
        registry.set_adapter(ADAPTER_ID, make_config())
        registry.activate_adapter(ADAPTER_ID)
        with boa.reverts(custom_err("AlreadyActive()")):
            registry.activate_adapter(ADAPTER_ID)


def test_activate_rechecks_validator_codehash(registry, make_config, owner):
    # Pin at set, re-check at activation: code swapped at the same address
    # (e.g. a CREATE2 redeploy) must not go live under the old registration.
    fresh_validator = boa.load(
        "contracts/testing/dutch_auction/ComposableCowMock.vy", name="FreshValidatorMock"
    )
    pinned_codehash = codehash_of(fresh_validator.address)
    with boa.env.prank(owner):
        registry.set_adapter(
            ADAPTER_ID,
            make_config(
                validator_address=fresh_validator.address, validator_codehash=pinned_codehash
            ),
        )

    boa.env.set_code(fresh_validator.address, b"\xfe\x60\x00")
    with boa.env.prank(owner):
        with boa.reverts(custom_err("CodehashMismatch()")):
            registry.activate_adapter(ADAPTER_ID)
        # Re-registering under the stale pinned codehash fails the same way:
        # the changed code demands its actual new codehash.
        with boa.reverts(custom_err("CodehashMismatch()")):
            registry.set_adapter(
                ADAPTER_ID,
                make_config(
                    validator_address=fresh_validator.address,
                    validator_codehash=pinned_codehash,
                    version=2,
                ),
            )
        registry.set_adapter(
            ADAPTER_ID,
            make_config(
                validator_address=fresh_validator.address,
                validator_codehash=codehash_of(fresh_validator.address),
                version=2,
            ),
        )
        registry.activate_adapter(ADAPTER_ID)
    assert registry.get_adapter(ADAPTER_ID).active


# disable_adapter


@pytest.mark.parametrize("role", ["owner", "emergency_owner"])
def test_disable_adapter_by_each_role(registry, make_config, owner, emergency_owner, role):
    with boa.env.prank(owner):
        registry.set_adapter(ADAPTER_ID, make_config())
        registry.activate_adapter(ADAPTER_ID)

    disabler = owner if role == "owner" else emergency_owner
    with boa.env.prank(disabler):
        registry.disable_adapter(ADAPTER_ID)
    event = last_event(registry, "AdapterDisabled")
    assert bytes(event.adapter_id) == ADAPTER_ID
    assert event.version == 1

    assert not registry.get_adapter(ADAPTER_ID).active


def test_disable_adapter_rejects_outsider(registry, make_config, owner, attacker):
    with boa.env.prank(owner):
        registry.set_adapter(ADAPTER_ID, make_config())
        registry.activate_adapter(ADAPTER_ID)
    with boa.env.prank(attacker):
        with boa.reverts(custom_err("OnlyOwner()")):
            registry.disable_adapter(ADAPTER_ID)


def test_disable_inactive_adapter_reverts(registry, make_config, owner):
    with boa.env.prank(owner):
        with boa.reverts(custom_err("NotActive()")):
            registry.disable_adapter(ADAPTER_ID)
        registry.set_adapter(ADAPTER_ID, make_config())
        with boa.reverts(custom_err("NotActive()")):
            registry.disable_adapter(ADAPTER_ID)


def test_owner_can_reactivate_after_emergency_disable(
    registry, make_config, owner, emergency_owner
):
    with boa.env.prank(owner):
        registry.set_adapter(ADAPTER_ID, make_config())
        registry.activate_adapter(ADAPTER_ID)
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(ADAPTER_ID)
    # Recovery from an emergency stop is a governance decision, not a re-set.
    with boa.env.prank(owner):
        registry.activate_adapter(ADAPTER_ID)
    assert registry.get_adapter(ADAPTER_ID).active


# Ownership


def test_commit_transfer_ownership_only_owner(registry, attacker):
    with boa.env.prank(attacker):
        with boa.reverts(custom_err("OnlyOwner()")):
            registry.commit_transfer_ownership(attacker)


def test_transfer_ownership_flow(registry, make_config, owner, attacker):
    new_owner = boa.env.generate_address("new_owner")
    with boa.env.prank(owner):
        registry.commit_transfer_ownership(new_owner)
    assert last_event(registry, "CommitOwnership").future_owner == new_owner
    assert registry.owner() == owner
    assert registry.future_owner() == new_owner

    with boa.env.prank(attacker):
        with boa.reverts(custom_err("OnlyFutureOwner()")):
            registry.accept_transfer_ownership()

    with boa.env.prank(new_owner):
        registry.accept_transfer_ownership()
    assert last_event(registry, "SetOwner").owner == new_owner
    assert registry.owner() == new_owner
    assert registry.future_owner() == ZERO_ADDRESS

    # The old owner lost every write path; the new owner gained them.
    with boa.env.prank(owner):
        with boa.reverts(custom_err("OnlyOwner()")):
            registry.set_adapter(ADAPTER_ID, make_config())
    with boa.env.prank(new_owner):
        registry.set_adapter(ADAPTER_ID, make_config())


def test_commit_can_be_overwritten_before_accept(registry, owner):
    stale_owner = boa.env.generate_address("stale_owner")
    with boa.env.prank(owner):
        registry.commit_transfer_ownership(stale_owner)
        registry.commit_transfer_ownership(ZERO_ADDRESS)
    with boa.env.prank(stale_owner):
        with boa.reverts(custom_err("OnlyFutureOwner()")):
            registry.accept_transfer_ownership()
    assert registry.owner() == owner


def test_set_emergency_owner(registry, make_config, owner, emergency_owner):
    new_emergency_owner = boa.env.generate_address("new_emergency_owner")
    with boa.env.prank(owner):
        registry.set_emergency_owner(new_emergency_owner)
    assert last_event(registry, "SetEmergencyOwner").emergency_owner == new_emergency_owner
    assert registry.emergency_owner() == new_emergency_owner

    with boa.env.prank(owner):
        registry.set_adapter(ADAPTER_ID, make_config())
        registry.activate_adapter(ADAPTER_ID)
    # The old emergency owner lost the disable right; the new one holds it.
    with boa.env.prank(emergency_owner):
        with boa.reverts(custom_err("OnlyOwner()")):
            registry.disable_adapter(ADAPTER_ID)
    with boa.env.prank(new_emergency_owner):
        registry.disable_adapter(ADAPTER_ID)
    assert not registry.get_adapter(ADAPTER_ID).active


def test_set_emergency_owner_only_owner_and_zero_sentinel(registry, owner, attacker):
    with boa.env.prank(attacker):
        with boa.reverts(custom_err("OnlyOwner()")):
            registry.set_emergency_owner(attacker)
    # empty(address) is the documented "no emergency owner" sentinel.
    with boa.env.prank(owner):
        registry.set_emergency_owner(ZERO_ADDRESS)
    assert registry.emergency_owner() == ZERO_ADDRESS
