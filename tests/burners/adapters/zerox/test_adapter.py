"""ZeroExSettlerAdapter: the standalone 0x Settler v2 gasless adapter behind
the prefix route, in both flavors (SettlerMetaTxn and SettlerIntent).

Validation runs end-to-end through the auction's signature router: the
order is handed to relayers or solvers with signature
`adapter ++ abi_encode(order)`, the harness strips the prefix and forwards
the bare order, the adapter proves the Permit2 witness digest under the
pinned Settler and flavor, and every economic decision comes from the
auction's shared check_order view. Shape failures revert with the adapter's
OrderNotValid(reason); the auction's typed check_order revert bubbles
unchanged for economic ones.
"""

from typing import Any

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak

from tests.burners.adapters.zerox.test_module import (
    ORDER_ABI,
    hash_actions_reference,
    permit_digest_reference,
)
from tests.burners.conftest import custom_err

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ETH_SENTINEL = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
WAD = 10**18

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
AUCTION_LENGTH = 24 * 60 * 60
LOT_AMOUNT = 250 * WAD
NONCE = 0xABCDEF
# 0x deployer/registry features (ERC-721 token ids) of the two flavors.
FEATURE_METATXN = 3
FEATURE_INTENT = 4
# A plausible signed action list: METATXN_TRANSFER_FROM and a BASIC swap.
ACTIONS = [bytes.fromhex("0dfeb419") + bytes(160), bytes.fromhex("38c9c147") + bytes(256)]
ACTIONS_HASH = hash_actions_reference(ACTIONS)

DOMAIN_SEPARATOR = keccak(b"PERMIT2_TEST_DOMAIN")
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
def permit2_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/Permit2Mock.vy")


@pytest.fixture(scope="module")
def settler_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/SettlerMock.vy")


@pytest.fixture(scope="module")
def permit2(permit2_deployer):
    return permit2_deployer.deploy(DOMAIN_SEPARATOR)


@pytest.fixture(scope="module")
def settler_metatxn(settler_deployer):
    return settler_deployer.deploy()


@pytest.fixture(scope="module")
def settler_intent(settler_deployer):
    return settler_deployer.deploy()


@pytest.fixture(scope="module")
def zerox_deployer(settler_deployer, settler_metatxn, settler_intent):
    """The 0x deployer/registry stand-in, listing both mock Settlers as the
    current instance of their feature (3 = SettlerMetaTxn, 4 = SettlerIntent)
    after two retired predecessors each, as on mainnet, where every feature
    has a history (prev answers after a removal)."""
    deployer = boa.load("contracts/testing/dutch_auction/ZeroExRegistryMock.vy")
    for feature, settler in ((FEATURE_METATXN, settler_metatxn), (FEATURE_INTENT, settler_intent)):
        deployer.add(feature, settler_deployer.deploy())
        deployer.add(feature, settler_deployer.deploy())
        deployer.add(feature, settler)
    return deployer


@pytest.fixture(scope="module")
def adapter_deployer():
    return boa.load_partial("contracts/burners/adapters/zerox/ZeroExSettlerAdapter.vy")


@pytest.fixture
def registry(role_source):
    return boa.load("contracts/burners/adapters/AdapterRegistry.vy", role_source.address)


@pytest.fixture
def adapter(adapter_deployer, settler_metatxn, zerox_deployer, permit2):
    """The SettlerMetaTxn-flavored adapter (the default of every test)."""
    return adapter_deployer.deploy(settler_metatxn, zerox_deployer, permit2, False)


@pytest.fixture
def intent_adapter(adapter_deployer, settler_intent, zerox_deployer, permit2):
    return adapter_deployer.deploy(settler_intent, zerox_deployer, permit2, True)


@pytest.fixture
def harness(
    role_source, want, proceeds_receiver, registry, adapter, intent_adapter, owner, permit2
):
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
        # Both flavors share the Permit2 executor (the registry refcounts it).
        registry.set_adapter(adapter, permit2)
        registry.activate_adapter(adapter)
        registry.set_adapter(intent_adapter, permit2)
        registry.activate_adapter(intent_adapter)
    # The core fences lots staged in the block that set its economics (the
    # deployment block included): stage from the next second on.
    boa.env.time_travel(seconds=1)
    return harness


@pytest.fixture
def lot(harness, sell_token):
    sell_token._mint_for_testing(harness.address, LOT_AMOUNT)
    harness.stage(sell_token.address)
    return harness.lots(sell_token.address)


def _now() -> int:
    return boa.env.evm.vm.state.timestamp


@pytest.fixture
def make_order(harness, sell_token, want, proceeds_receiver, lot):
    """Build an Order tuple the way a publisher would.

    Overrides address the flat field set: the permit (sell_token,
    sell_amount, nonce, deadline), the slippage (receiver, buy_token,
    buy_amount) and actions_hash.
    """

    def _make(**overrides) -> tuple:
        sell_amount = overrides.get("sell_amount", LOT_AMOUNT)
        buy_amount = overrides.pop("buy_amount", None)
        if buy_amount is None:
            buy_amount = harness.getAmountNeeded(sell_token.address, min(sell_amount, LOT_AMOUNT))
        fields = {
            "sell_token": sell_token.address,
            "sell_amount": sell_amount,
            "nonce": NONCE,
            "deadline": harness.frame_end(),
            "receiver": proceeds_receiver,
            "buy_token": want.address,
            "buy_amount": buy_amount,
            "actions_hash": ACTIONS_HASH,
        }
        unknown = set(overrides) - set(fields)
        assert not unknown, unknown
        fields.update(overrides)
        return (
            (fields["sell_token"], fields["sell_amount"], fields["nonce"], fields["deadline"]),
            (fields["receiver"], fields["buy_token"], fields["buy_amount"]),
            fields["actions_hash"],
        )

    return _make


def _prefix(adapter: Any) -> bytes:
    return bytes.fromhex(str(adapter.address)[2:])


def _bare(order: tuple) -> bytes:
    return encode([ORDER_ABI], [order])


def _signature(adapter: Any, order: tuple) -> bytes:
    """What the Settler hands Permit2 as sig: Permit2 forwards it verbatim."""
    return _prefix(adapter) + _bare(order)


def _digest(adapter: Any, order: tuple, domain=DOMAIN_SEPARATOR, spender=None) -> bytes:
    return permit_digest_reference(
        order, spender or adapter.settler(), domain, bool(adapter.intent())
    )


def _validate(harness, adapter, order: tuple, signature: bytes | None = None, **digest_kwargs):
    signature = _signature(adapter, order) if signature is None else signature
    return harness.isValidSignature(_digest(adapter, order, **digest_kwargs), signature)


def _to_tuple(order) -> tuple:
    permit, slippage, actions_hash = order
    return (tuple(permit), tuple(slippage), bytes(actions_hash))


# Deployment pinning


def test_deploy_pins_settler_flavor_registry_permit2_and_domain(
    adapter, intent_adapter, settler_metatxn, settler_intent, zerox_deployer, permit2
):
    assert adapter.settler() == settler_metatxn.address
    assert adapter.intent() is False
    assert adapter.feature() == FEATURE_METATXN
    assert intent_adapter.settler() == settler_intent.address
    assert intent_adapter.intent() is True
    assert intent_adapter.feature() == FEATURE_INTENT
    for deployed in (adapter, intent_adapter):
        assert deployed.deployer() == zerox_deployer.address
        assert deployed.permit2() == permit2.address
        assert bytes(deployed.domain_separator()) == DOMAIN_SEPARATOR
        assert deployed.ADAPTER_VERSION() == "ZeroExSettlerAdapter"


def test_deploy_rejects_settler_without_code(adapter_deployer, zerox_deployer, permit2):
    with boa.reverts(custom_err("BadSettler()")):
        adapter_deployer.deploy(ZERO_ADDRESS, zerox_deployer, permit2, False)
    with boa.reverts(custom_err("BadSettler()")):
        adapter_deployer.deploy(
            boa.env.generate_address("eoa_settler"), zerox_deployer, permit2, False
        )


def test_deploy_rejects_missing_deployer(adapter_deployer, settler_metatxn, permit2):
    with boa.reverts(custom_err("BadDeployer()")):
        adapter_deployer.deploy(settler_metatxn, ZERO_ADDRESS, permit2, False)


def test_deploy_rejects_missing_permit2(adapter_deployer, settler_metatxn, zerox_deployer):
    with boa.reverts(custom_err("BadPermit2()")):
        adapter_deployer.deploy(settler_metatxn, zerox_deployer, ZERO_ADDRESS, False)


def test_deploy_rejects_missing_domain_separator(
    permit2_deployer, settler_metatxn, zerox_deployer, adapter_deployer
):
    with boa.reverts(custom_err("BadDomainSeparator()")):
        adapter_deployer.deploy(
            settler_metatxn, zerox_deployer, permit2_deployer.deploy(bytes(32)), False
        )


def test_deploy_rejects_settler_the_registry_does_not_list(
    adapter_deployer, settler_deployer, settler_metatxn, settler_intent, zerox_deployer, permit2
):
    # A contract 0x never deployed: the registry's own deployInfo revert
    # bubbles (an address with code is not enough).
    foreign = settler_deployer.deploy()
    with boa.reverts(custom_err("ERC721InvalidOwner(address)", foreign.address)):
        adapter_deployer.deploy(foreign, zerox_deployer, permit2, False)
    # A 0x deployment of the other flavor: the witness type would never match.
    with boa.reverts(custom_err("BadFlavor()")):
        adapter_deployer.deploy(settler_intent, zerox_deployer, permit2, False)
    with boa.reverts(custom_err("BadFlavor()")):
        adapter_deployer.deploy(settler_metatxn, zerox_deployer, permit2, True)
    # A 0x deployment 0x has since removed: deployInfo still knows it, the
    # feature no longer lists it.
    zerox_deployer.remove(settler_metatxn)
    with boa.reverts(custom_err("SettlerRetired()")):
        adapter_deployer.deploy(settler_metatxn, zerox_deployer, permit2, False)
    # Only its feature is affected.
    adapter_deployer.deploy(settler_intent, zerox_deployer, permit2, True)


# 0x's pause: the adapter follows the deployer/registry


def test_settler_accepted_while_current_or_previous(
    harness, adapter, adapter_deployer, make_order, settler_deployer, zerox_deployer, permit2
):
    order = make_order()
    assert zerox_deployer.ownerOf(FEATURE_METATXN) == adapter.settler()
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE
    # 0x deploys a newer SettlerMetaTxn: ours is now prev(3), which the 0x
    # API keeps serving during the dwell time, and still signs.
    zerox_deployer.add(FEATURE_METATXN, settler_deployer.deploy())
    assert zerox_deployer.ownerOf(FEATURE_METATXN) != adapter.settler()
    assert zerox_deployer.prev(FEATURE_METATXN) == adapter.settler()
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE
    # A previous Settler can still be pinned by a fresh deployment.
    adapter_deployer.deploy(adapter.settler(), zerox_deployer, permit2, False)


def test_settler_rejected_once_two_newer_are_deployed(
    harness, adapter, make_order, settler_deployer, zerox_deployer
):
    order = make_order()
    zerox_deployer.add(FEATURE_METATXN, settler_deployer.deploy())
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE
    zerox_deployer.add(FEATURE_METATXN, settler_deployer.deploy())
    with boa.reverts(custom_err("OrderNotValid(string)", "SettlerRetired")):
        _validate(harness, adapter, order)


def test_settler_rejected_after_removal(
    harness, adapter, intent_adapter, make_order, settler_deployer, zerox_deployer
):
    order = make_order()
    intent_order = make_order(actions_hash=bytes(32))
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE
    # 0x removes the current SettlerMetaTxn (its pause): the adapter stops
    # signing by itself, before any auction read; the other feature is
    # untouched.
    zerox_deployer.remove(adapter.settler())
    with boa.reverts(custom_err("OrderNotValid(string)", "SettlerRetired")):
        _validate(harness, adapter, order)
    assert _validate(harness, intent_adapter, intent_order) == ERC1271_MAGIC_VALUE
    # Removed as the previous instance: the same.
    with boa.env.anchor():
        zerox_deployer.add(FEATURE_METATXN, adapter.settler())
        zerox_deployer.add(FEATURE_METATXN, settler_deployer.deploy())
        assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE
        zerox_deployer.remove(adapter.settler())
        with boa.reverts(custom_err("OrderNotValid(string)", "SettlerRetired")):
            _validate(harness, adapter, order)
    # Every instance of the feature removed: the registry's ownerOf revert
    # bubbles, fail closed.
    zerox_deployer.remove_all(FEATURE_METATXN)
    with boa.reverts(custom_err("ERC721NonexistentToken(uint256)", FEATURE_METATXN)):
        _validate(harness, adapter, order)
    # Relisted, it signs again: the adapter holds no state of its own.
    zerox_deployer.add(FEATURE_METATXN, adapter.settler())
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE


# Routing: the 0x rail is a regular registry adapter behind the prefix


def test_prefixed_bare_order_is_valid_and_unprefixed_is_not(harness, adapter, make_order):
    order = make_order()
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE
    assert _validate(harness, adapter, order, _bare(order)) == ERC1271_INVALID


def test_intent_flavor_validates_its_own_witness(harness, intent_adapter, make_order):
    order = make_order(actions_hash=bytes(32))
    assert _validate(harness, intent_adapter, order) == ERC1271_MAGIC_VALUE


def test_disabled_adapter_stops_routing(
    harness, adapter, intent_adapter, make_order, registry, emergency_owner, permit2
):
    order = make_order()
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter)
    assert _validate(harness, adapter, order) == ERC1271_INVALID
    # The other flavor keeps routing and keeps the shared executor active.
    intent_order = make_order(actions_hash=bytes(32))
    assert _validate(harness, intent_adapter, intent_order) == ERC1271_MAGIC_VALUE
    assert registry.is_executor_active(permit2)


# Transport encoding


def test_malformed_payloads_fail(harness, adapter, make_order):
    order = make_order()
    # Shorter than the one static encoding: not an order at all.
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        _validate(harness, adapter, order, _signature(adapter, order)[:-1])
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        _validate(harness, adapter, order, _prefix(adapter))
    # Longer: fails ABI decoding of the adapter call (bounded payload).
    with boa.reverts():
        _validate(harness, adapter, order, _signature(adapter, order) + b"\x00")


# Digest and shape


def test_hash_mismatch_invalid(harness, adapter, make_order):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        harness.isValidSignature(keccak(b"other"), _signature(adapter, order))


def test_foreign_domain_separator_invalid(harness, adapter, make_order):
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        _validate(harness, adapter, make_order(), domain=FOREIGN_DOMAIN)


def test_foreign_spender_invalid(harness, adapter, make_order):
    # A digest with another Settler as the permit spender (another
    # deployment, or an unrelated Permit2 integrator) never matches.
    other_settler = boa.env.generate_address("other_settler")
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        _validate(harness, adapter, make_order(), spender=other_settler)


def test_payload_must_match_what_the_settler_passes_permit2(harness, adapter, make_order):
    # The Settler hashes the permit from its first action and its own
    # slippage and actions; a payload differing in any of them is not what
    # Permit2 asks about.
    order = make_order()
    digest = _digest(adapter, order)
    for tampered in (
        make_order(sell_amount=LOT_AMOUNT - 1),
        make_order(nonce=NONCE + 1),
        make_order(deadline=harness.frame_end() - 1),
        make_order(buy_amount=order[1][2] + 1),
        make_order(actions_hash=hash_actions_reference(ACTIONS[:1])),
    ):
        with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
            harness.isValidSignature(digest, _signature(adapter, tampered))


def test_flavor_mismatch_invalid(harness, adapter, intent_adapter, make_order):
    # The witness type is the pinned flavor's: a SettlerIntent digest fails
    # on the SettlerMetaTxn adapter and vice versa.
    order = make_order(actions_hash=bytes(32))
    intent_digest = permit_digest_reference(order, adapter.settler(), DOMAIN_SEPARATOR, True)
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        harness.isValidSignature(intent_digest, _signature(adapter, order))
    metatxn_digest = permit_digest_reference(
        order, intent_adapter.settler(), DOMAIN_SEPARATOR, False
    )
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        harness.isValidSignature(metatxn_digest, _signature(intent_adapter, order))


def test_intent_order_carries_no_actions_hash(harness, intent_adapter, make_order):
    # The Slippage witness ignores the field, so the digest matches; the
    # adapter still pins the one canonical payload.
    order = make_order(actions_hash=ACTIONS_HASH)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadActionsHash")):
        _validate(harness, intent_adapter, order)


def test_metatxn_order_accepts_any_signed_action_list(harness, adapter, make_order):
    # Only the Settler hash of the actions is carried: whatever the list
    # does, the floor is the signed slippage.
    assert _validate(harness, adapter, make_order(actions_hash=keccak(b""))) == (
        ERC1271_MAGIC_VALUE
    )
    assert _validate(harness, adapter, make_order(actions_hash=keccak(b"route"))) == (
        ERC1271_MAGIC_VALUE
    )


# Fill-or-kill: the permitted amount must be available now


def test_order_above_the_live_remainder_invalid(harness, adapter, make_order, sell_token, want):
    order = make_order()
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE
    # A native take shrinks the remainder below the permitted amount; the
    # snapshot-based check_order alone would still accept it.
    taker = boa.env.generate_address("taker")
    taken = LOT_AMOUNT // 4
    want._mint_for_testing(taker, harness.getAmountNeeded(sell_token.address, taken))
    with boa.env.prank(taker):
        want.approve(harness.address, 2**256 - 1)
        harness.take(sell_token.address, taken)
    with boa.reverts(custom_err("OrderNotValid(string)", "ExceedsAvailable")):
        _validate(harness, adapter, order)
    remainder = make_order(sell_amount=LOT_AMOUNT - taken)
    assert _validate(harness, adapter, remainder) == ERC1271_MAGIC_VALUE


# Economics: the signed slippage is what check_order prices


def test_auction_economics_revert_bubbles_through_adapter(harness, adapter, make_order, sell_token):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    with boa.reverts(custom_err("BadBuyAmount()")):
        _validate(harness, adapter, make_order(buy_amount=quote - 1))
    assert _validate(harness, adapter, make_order(buy_amount=quote + 1)) == ERC1271_MAGIC_VALUE


def test_deadline_past_the_window_invalid(harness, adapter, make_order):
    with boa.reverts(custom_err("BadValidTo()")):
        _validate(harness, adapter, make_order(deadline=harness.frame_end() + 1))
    with boa.reverts(custom_err("BadValidTo()")):
        _validate(harness, adapter, make_order(deadline=_now() - 1))


def test_wrong_buy_token_or_receiver_invalid(harness, adapter, make_order, sell_token):
    with boa.reverts(custom_err("BadBuyToken()")):
        _validate(harness, adapter, make_order(buy_token=sell_token.address))
    # The Settler's native-coin sentinel is never the auction's want.
    with boa.reverts(custom_err("BadBuyToken()")):
        _validate(harness, adapter, make_order(buy_token=ETH_SENTINEL))
    with boa.reverts(custom_err("BadReceiver()")):
        _validate(harness, adapter, make_order(receiver=boa.env.generate_address("thief")))


# Publisher helpers: order + signature for the burner


def test_order_for_builds_the_publishable_pair(
    harness, adapter, sell_token, want, proceeds_receiver, lot
):
    order, signature = adapter.order_for(
        harness.address, sell_token.address, NONCE, 0, 0, ACTIONS_HASH
    )
    order = _to_tuple(order)
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    assert order == (
        (sell_token.address, LOT_AMOUNT, NONCE, harness.frame_end()),
        (proceeds_receiver, want.address, quote),
        ACTIONS_HASH,
    )
    # The signature is the documented template and validates end-to-end.
    assert bytes(signature) == _signature(adapter, order)
    assert harness.isValidSignature(_digest(adapter, order), signature) == ERC1271_MAGIC_VALUE

    # A partial amount with an explicit deadline is priced proportionally.
    deadline = _now() + 10 * STEP_DURATION
    partial, partial_signature = adapter.order_for(
        harness.address, sell_token.address, NONCE + 1, LOT_AMOUNT // 4, deadline, ACTIONS_HASH
    )
    partial = _to_tuple(partial)
    assert partial[0] == (sell_token.address, LOT_AMOUNT // 4, NONCE + 1, deadline)
    assert partial[1][2] == harness.getAmountNeeded(sell_token.address, LOT_AMOUNT // 4)
    assert (
        harness.isValidSignature(_digest(adapter, partial), partial_signature)
        == ERC1271_MAGIC_VALUE
    )


def test_order_for_intent_carries_no_actions(
    harness, intent_adapter, sell_token, want, proceeds_receiver, lot
):
    order, signature = intent_adapter.order_for(harness.address, sell_token.address, NONCE)
    order = _to_tuple(order)
    assert order[2] == bytes(32)
    assert (
        harness.isValidSignature(_digest(intent_adapter, order), signature) == ERC1271_MAGIC_VALUE
    )
    with boa.reverts(custom_err("BadActionsHash()")):
        intent_adapter.order_for(harness.address, sell_token.address, NONCE, 0, 0, ACTIONS_HASH)


def test_order_for_metatxn_requires_actions(harness, adapter, sell_token, lot):
    with boa.reverts(custom_err("BadActionsHash()")):
        adapter.order_for(harness.address, sell_token.address, NONCE)


def test_order_for_stays_valid_as_the_curve_decays(harness, adapter, sell_token, lot):
    # A published order is a standing ask: once the curve falls below it, it
    # is still accepted (overpriced for us is fine), so publishers re-post
    # lower with fresh nonces rather than cancel.
    harness.set_frame(_now())
    order, signature = adapter.order_for(
        harness.address, sell_token.address, NONCE, 0, 0, ACTIONS_HASH
    )
    order = _to_tuple(order)
    boa.env.time_travel(seconds=10 * STEP_DURATION)
    assert harness.getAmountNeeded(sell_token.address, LOT_AMOUNT) < order[1][2]
    assert harness.isValidSignature(_digest(adapter, order), signature) == ERC1271_MAGIC_VALUE


def test_order_for_rejects_empty_oversized_and_late(harness, adapter, sell_token, erc20_deployer):
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, sell_token.address, NONCE, 0, 0, ACTIONS_HASH)
    fresh = erc20_deployer.deploy("Fresh", "FRESH", 18)
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, fresh.address, NONCE, 0, 0, ACTIONS_HASH)
    sell_token._mint_for_testing(harness.address, LOT_AMOUNT)
    harness.stage(sell_token.address)
    with boa.reverts(custom_err("ExceedsAvailable()")):
        adapter.order_for(
            harness.address, sell_token.address, NONCE, LOT_AMOUNT + 1, 0, ACTIONS_HASH
        )
    with boa.reverts(custom_err("BadDeadline()")):
        adapter.order_for(
            harness.address, sell_token.address, NONCE, 0, harness.frame_end() + 1, ACTIONS_HASH
        )
    with boa.reverts(custom_err("BadDeadline()")):
        adapter.order_for(harness.address, sell_token.address, NONCE, 0, _now(), ACTIONS_HASH)


def test_describe_and_hash_actions(adapter, intent_adapter, make_order):
    order = make_order()
    described, digest = adapter.describe(_bare(order))
    assert _to_tuple(described) == order
    assert bytes(digest) == _digest(adapter, order)
    intent_order = make_order(actions_hash=bytes(32))
    _, intent_digest = intent_adapter.describe(_bare(intent_order))
    assert bytes(intent_digest) == _digest(intent_adapter, intent_order)
    assert bytes(adapter.hash_actions(ACTIONS)) == ACTIONS_HASH
