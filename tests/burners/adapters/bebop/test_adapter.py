"""BebopJamAdapter: the standalone Bebop JAM v2 adapter behind the prefix
route.

Validation runs end-to-end through the auction's signature router: the order
is handed to solvers with signature `adapter ++ abi_encode(order)`, the
harness strips the prefix and forwards the bare order, the adapter proves the
settlement's order digest and the order shape, and every economic decision
comes from the auction's shared check_order view. Shape failures revert with
the adapter's OrderNotValid(reason); the auction's typed check_order revert
bubbles unchanged for economic ones. The settlement mock then fills orders the
way JamSettlement does: the signature checked on every fill, the nonce burnt,
the sell tokens pulled through the balance manager, the receiver paid at
least the signed buy amount after fees.
"""

from typing import Any

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak

from tests.burners.adapters.bebop.test_module import (
    EMPTY_HOOKS_HASH,
    INF_EXPIRY,
    NATIVE_TOKEN,
    ORDER_ABI,
    PARTNER,
    domain_separator_reference,
    order_digest_reference,
    pack_partner_info,
)
from tests.burners.conftest import custom_err

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WAD = 10**18

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
AUCTION_LENGTH = 24 * 60 * 60
LOT_AMOUNT = 250 * WAD
NONCE = 0xABCDEF

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
def protocol_fee_receiver():
    return boa.env.generate_address("bebop_treasury")


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
def settlement_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/JamSettlementMock.vy")


@pytest.fixture(scope="module")
def balance_manager_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/JamBalanceManagerMock.vy")


@pytest.fixture(scope="module")
def settlement(settlement_deployer, balance_manager_deployer, protocol_fee_receiver):
    settlement = settlement_deployer.deploy(bytes(32), protocol_fee_receiver)
    balance_manager = balance_manager_deployer.deploy(settlement.address)
    settlement.set_balance_manager(balance_manager.address)
    return settlement


@pytest.fixture(scope="module")
def balance_manager(settlement):
    return settlement.balanceManager()


@pytest.fixture(scope="module")
def domain_separator(settlement):
    return domain_separator_reference(boa.env.evm.patch.chain_id, settlement.address)


@pytest.fixture(scope="module")
def adapter_deployer():
    return boa.load_partial("contracts/burners/adapters/bebop/BebopJamAdapter.vy")


@pytest.fixture
def registry(role_source):
    return boa.load("contracts/burners/adapters/AdapterRegistry.vy", role_source.address)


@pytest.fixture
def adapter(adapter_deployer, settlement):
    return adapter_deployer.deploy(settlement)


@pytest.fixture
def harness(role_source, want, proceeds_receiver, registry, adapter, owner, balance_manager):
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
        registry.set_adapter(adapter, balance_manager)
        registry.activate_adapter(adapter)
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
    """Build a signed JamOrder tuple the way a publisher (or the JAM API's
    `toSign`) would.

    Overrides address the twelve fields (taker, receiver, expiry,
    exclusivity_deadline, nonce, executor, partner_info, sell_tokens,
    buy_tokens, sell_amounts, buy_amounts, hooks_hash); `sell_amount` and
    `buy_amount` set the single-entry arrays.
    """

    def _make(**overrides) -> tuple:
        sell_amount = overrides.pop("sell_amount", LOT_AMOUNT)
        buy_amount = overrides.pop("buy_amount", None)
        if buy_amount is None:
            buy_amount = harness.getAmountNeeded(sell_token.address, min(sell_amount, LOT_AMOUNT))
        fields = {
            "taker": harness.address,
            "receiver": proceeds_receiver,
            "expiry": harness.frame_end(),
            "exclusivity_deadline": 0,
            "nonce": NONCE,
            "executor": ZERO_ADDRESS,
            "partner_info": 0,
            "sell_tokens": [sell_token.address],
            "buy_tokens": [want.address],
            "sell_amounts": [sell_amount],
            "buy_amounts": [buy_amount],
            "hooks_hash": EMPTY_HOOKS_HASH,
        }
        unknown = set(overrides) - set(fields)
        assert not unknown, unknown
        fields.update(overrides)
        return tuple(fields.values())

    return _make


def _prefix(adapter: Any) -> bytes:
    return bytes.fromhex(str(adapter.address)[2:])


def _bare(order: tuple) -> bytes:
    return encode([ORDER_ABI], [order])


def _signature(adapter: Any, order: tuple) -> bytes:
    """What solvers pass as the fill signature: the settlement forwards it verbatim."""
    return _prefix(adapter) + _bare(order)


def _calldata(order: tuple) -> tuple:
    """The order as the settlement ABI takes it: usingPermit2 in place of hooksHash."""
    return (*order[:11], False)


@pytest.fixture
def validate(harness, adapter, domain_separator):
    """Route an order through the harness: digest under the settlement's
    domain, signature `adapter ++ abi_encode(order)` by default."""

    def _validate(order: tuple, signature: bytes | None = None, domain: bytes = domain_separator):
        signature = _signature(adapter, order) if signature is None else signature
        return harness.isValidSignature(order_digest_reference(order, domain), signature)

    return _validate


# Deployment pinning


def test_deploy_pins_settlement_domain_and_balance_manager(
    adapter, settlement, domain_separator, balance_manager
):
    assert adapter.settlement() == settlement.address
    assert bytes(adapter.domain_separator()) == domain_separator
    assert bytes(settlement.DOMAIN_SEPARATOR()) == domain_separator
    assert adapter.balance_manager() == balance_manager
    assert adapter.ADAPTER_VERSION() == "BebopJamAdapter"


def test_deploy_rejects_zero_settlement(adapter_deployer):
    with boa.reverts(custom_err("BadSettlement()")):
        adapter_deployer.deploy(ZERO_ADDRESS)


def test_deploy_rejects_a_settlement_with_another_domain(
    settlement_deployer, balance_manager_deployer, adapter_deployer, protocol_fee_receiver
):
    # Another name, version or chain (a proxy, a fork, a future version):
    # DOMAIN_SEPARATOR differs from the one the adapter rebuilds.
    lying = settlement_deployer.deploy(FOREIGN_DOMAIN, protocol_fee_receiver)
    lying.set_balance_manager(balance_manager_deployer.deploy(lying.address).address)
    with boa.reverts(custom_err("BadDomain()")):
        adapter_deployer.deploy(lying)


def test_deploy_rejects_a_settlement_without_balance_manager(
    settlement_deployer, adapter_deployer, protocol_fee_receiver
):
    unwired = settlement_deployer.deploy(bytes(32), protocol_fee_receiver)
    with boa.reverts(custom_err("BadBalanceManager()")):
        adapter_deployer.deploy(unwired)


# Routing: the JAM rail is a regular registry adapter behind the prefix


def test_prefixed_bare_order_is_valid_and_unprefixed_is_not(harness, adapter, make_order, validate):
    order = make_order()
    assert validate(order) == ERC1271_MAGIC_VALUE
    assert validate(order, _bare(order)) == ERC1271_INVALID


def test_disabled_adapter_stops_routing(
    harness, adapter, make_order, registry, emergency_owner, validate
):
    order = make_order()
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter)
    assert validate(order) == ERC1271_INVALID


# Transport encoding


def test_malformed_payloads_fail_decoding(harness, adapter, make_order, validate):
    order = make_order()
    # Truncated, empty, or arrays above the bound: not one order at all.
    with boa.reverts():
        validate(order, _signature(adapter, order)[:-1])
    with boa.reverts():
        validate(order, _prefix(adapter))
    with boa.reverts():
        validate(order, _prefix(adapter) + _bare(order)[: 13 * 32])
    oversized = make_order(sell_tokens=[order[7][0]] * 9, sell_amounts=[1] * 9)
    with boa.reverts():
        validate(oversized)


def test_non_canonical_encodings_rejected(harness, adapter, make_order, validate):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        validate(order, _signature(adapter, order) + b"\x00")
    bare = _bare(order)
    # Move the first array one word further and pad the body so the
    # encoding is loose but still decodes to the same order.
    head_end = 32 + 12 * 32
    offsets = [int.from_bytes(bare[32 + 7 * 32 + i * 32 :][:32], "big") for i in range(4)]
    loose = bytearray(bare[:head_end]) + bytearray(32) + bytearray(bare[head_end:])
    for i, offset in enumerate(offsets):
        loose[32 + 7 * 32 + i * 32 : 32 + 8 * 32 + i * 32] = (offset + 32).to_bytes(32, "big")
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        validate(order, _prefix(adapter) + bytes(loose))


# Digest and shape


def test_hash_mismatch_invalid(harness, adapter, make_order):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        harness.isValidSignature(keccak(b"other"), _signature(adapter, order))


def test_foreign_domain_separator_invalid(harness, adapter, make_order, validate):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        validate(order, domain=FOREIGN_DOMAIN)


def test_foreign_taker_invalid(harness, adapter, make_order, validate):
    other_taker = boa.env.generate_address("other_taker")
    with boa.reverts(custom_err("OrderNotValid(string)", "BadTaker")):
        validate(make_order(taker=other_taker))


def test_hooks_invalid(harness, adapter, make_order, validate):
    # Hooks are calls the settlement makes on the taker's behalf; none are
    # ever signed for the auction.
    with boa.reverts(custom_err("OrderNotValid(string)", "BadHooks")):
        validate(make_order(hooks_hash=keccak(b"hooks")))


def test_token_count_invalid(harness, adapter, make_order, validate, sell_token, want):
    other = boa.env.generate_address("other_token")
    cases = [
        {"sell_tokens": [sell_token.address, other], "sell_amounts": [LOT_AMOUNT, 1]},
        {"buy_tokens": [want.address, other], "buy_amounts": [1, 1]},
        {"sell_tokens": [], "sell_amounts": []},
        {"buy_tokens": [], "buy_amounts": []},
        # Mismatched lengths the protocol would reject too.
        {"sell_amounts": [LOT_AMOUNT, 1]},
        {"buy_tokens": [want.address, other]},
    ]
    for overrides in cases:
        with boa.reverts(custom_err("OrderNotValid(string)", "BadTokenCount")):
            validate(make_order(**overrides))


def test_zero_nonce_invalid(harness, adapter, make_order, validate):
    with boa.reverts(custom_err("OrderNotValid(string)", "ZeroNonce")):
        validate(make_order(nonce=0))


def test_partner_info_follows_the_protocol_rules(harness, adapter, make_order, validate):
    # Fees are the solver's cost above the signed buy amount: any
    # partnerInfo the settlement accepts passes.
    assert validate(make_order(partner_info=pack_partner_info(PARTNER, 60, 5))) == (
        ERC1271_MAGIC_VALUE
    )
    assert validate(make_order(partner_info=10)) == ERC1271_MAGIC_VALUE
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidFeePercentage")):
        validate(make_order(partner_info=pack_partner_info(PARTNER, 5000, 5000)))
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidPartnerAddress")):
        validate(make_order(partner_info=pack_partner_info(PARTNER, 0, 5)))


def test_executor_and_exclusivity_are_not_the_adapters_concern(
    harness, adapter, make_order, validate
):
    # An exclusive executor only restricts who fills; the price is the same.
    solver = boa.env.generate_address("solver")
    assert validate(make_order(executor=solver)) == ERC1271_MAGIC_VALUE
    assert validate(make_order(executor=solver, exclusivity_deadline=harness.frame_end())) == (
        ERC1271_MAGIC_VALUE
    )


# Economics: the whole order priced by check_order


def test_auction_economics_revert_bubbles_through_adapter(
    harness, adapter, make_order, sell_token, validate
):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    with boa.reverts(custom_err("BadBuyAmount()")):
        validate(make_order(buy_amount=quote - 1))
    assert validate(make_order(buy_amount=quote + 1)) == ERC1271_MAGIC_VALUE


def test_expiry_is_the_validity_bound(harness, adapter, make_order, validate):
    with boa.reverts(custom_err("BadValidTo()")):
        validate(make_order(expiry=harness.frame_end() + 1))
    # A limit order (infinite expiry) is not an option: the lot window
    # bounds every order.
    with boa.reverts(custom_err("BadValidTo()")):
        validate(make_order(expiry=INF_EXPIRY))
    with boa.reverts(custom_err("BadValidTo()")):
        validate(make_order(expiry=0))
    assert validate(make_order(expiry=_now())) == ERC1271_MAGIC_VALUE


def test_wrong_tokens_or_receiver_invalid(harness, adapter, make_order, sell_token, validate):
    with boa.reverts(custom_err("BadBuyToken()")):
        validate(make_order(buy_tokens=[sell_token.address]))
    # The native-coin placeholder is neither the payment token nor a lot.
    with boa.reverts(custom_err("BadBuyToken()")):
        validate(make_order(buy_tokens=[NATIVE_TOKEN]))
    with boa.reverts(custom_err("LotInactive()")):
        validate(make_order(sell_tokens=[NATIVE_TOKEN]))
    for receiver in (boa.env.generate_address("thief"), adapter.address, ZERO_ADDRESS):
        with boa.reverts(custom_err("BadReceiver()")):
            validate(make_order(receiver=receiver))
    # The auction itself is the one other receiver check_order accepts (the
    # proceeds are forwarded by push_target); the order is otherwise unchanged.
    assert validate(make_order(receiver=harness.address)) == ERC1271_MAGIC_VALUE


def test_sell_amount_is_bounded_by_the_snapshot(
    harness, adapter, make_order, sell_token, want, validate
):
    # A native take shrinks the remainder; an order over the whole snapshot
    # still validates (the settlement's transferFrom is what fails then),
    # one above the snapshot never does.
    order = make_order()
    taker = boa.env.generate_address("taker")
    taken = LOT_AMOUNT // 4
    want._mint_for_testing(taker, harness.getAmountNeeded(sell_token.address, taken))
    with boa.env.prank(taker):
        want.approve(harness.address, 2**256 - 1)
        harness.take(sell_token.address, taken)
    assert harness.available(sell_token.address) == LOT_AMOUNT - taken
    assert validate(order) == ERC1271_MAGIC_VALUE
    with boa.reverts(custom_err("BadSellAmount()")):
        validate(make_order(sell_amount=LOT_AMOUNT + 1, buy_amount=order[10][0]))


# Publisher helpers: order + signature for the auction, partnerInfo description


def test_order_for_builds_the_publishable_pair(
    harness, adapter, sell_token, want, proceeds_receiver, lot, validate
):
    order, signature = adapter.order_for(harness.address, sell_token.address, NONCE)
    order = tuple(order)
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    assert order[:7] == (
        harness.address,
        proceeds_receiver,
        harness.frame_end(),
        0,
        NONCE,
        ZERO_ADDRESS,
        0,
    )
    assert [list(field) for field in order[7:11]] == [
        [sell_token.address],
        [want.address],
        [LOT_AMOUNT],
        [quote],
    ]
    assert bytes(order[11]) == EMPTY_HOOKS_HASH
    # The signature is the documented template and validates end-to-end.
    expected = (*order[:11], EMPTY_HOOKS_HASH)
    assert bytes(signature) == _signature(adapter, expected)
    assert validate(expected, bytes(signature)) == ERC1271_MAGIC_VALUE

    # A partial amount is priced proportionally and validates as well.
    partial, partial_signature = adapter.order_for(
        harness.address, sell_token.address, NONCE + 1, LOT_AMOUNT // 4
    )
    partial = (*tuple(partial)[:11], EMPTY_HOOKS_HASH)
    assert list(partial[9]) == [LOT_AMOUNT // 4]
    assert list(partial[10]) == [harness.getAmountNeeded(sell_token.address, LOT_AMOUNT // 4)]
    assert validate(partial, bytes(partial_signature)) == ERC1271_MAGIC_VALUE


def test_order_for_stays_valid_as_the_curve_decays(harness, adapter, sell_token, lot, validate):
    # A published order is a standing ask: once the curve falls below it, it
    # is still accepted (overpriced for us is fine), so publishers re-post
    # lower with fresh nonces rather than cancel.
    harness.set_frame(_now())
    order, signature = adapter.order_for(harness.address, sell_token.address, NONCE)
    order = (*tuple(order)[:11], EMPTY_HOOKS_HASH)
    boa.env.time_travel(seconds=10 * STEP_DURATION)
    assert harness.getAmountNeeded(sell_token.address, LOT_AMOUNT) < order[10][0]
    assert validate(order, bytes(signature)) == ERC1271_MAGIC_VALUE


def test_order_for_rejects_empty_lots(harness, adapter, sell_token, erc20_deployer):
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, sell_token.address, NONCE)
    fresh = erc20_deployer.deploy("Fresh", "FRESH", 18)
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, fresh.address, NONCE)


def test_describe_unpacks_partner_info(adapter):
    assert tuple(adapter.describe(pack_partner_info(PARTNER, 60, 5))) == (PARTNER, 60, 5)
    assert tuple(adapter.describe(0)) == (ZERO_ADDRESS, 0, 0)
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidPartnerAddress")):
        adapter.describe(pack_partner_info(PARTNER, 0, 0))


# Settlement through the mock: the signature checked on every fill, the nonce
# burnt, tokens pulled through the balance manager, the receiver paid at
# least the signed buy amount.


def _transfer_from_calldata(sender: str, receiver: str, amount: int) -> bytes:
    return keccak(text="transferFrom(address,address,uint256)")[:4] + encode(
        ["address", "address", "uint256"], [sender, receiver, amount]
    )


@pytest.fixture
def solver(want, settlement, balance_manager, sell_token):
    """A solver funded with the payment token, approved toward the settlement
    (for settle's interactions) and the balance manager (for settleInternal)."""
    solver = boa.env.generate_address("solver")
    want._mint_for_testing(solver, 1_000_000 * WAD)
    with boa.env.prank(solver):
        want.approve(settlement.address, 2**256 - 1)
        want.approve(balance_manager, 2**256 - 1)
    return solver


@pytest.fixture
def approved(harness, balance_manager, sell_token, lot):
    harness.sync_executor_approvals(balance_manager, [sell_token.address])
    assert sell_token.allowance(harness.address, balance_manager) == 2**256 - 1


def test_settle_fills_a_published_order_through_the_balance_manager(
    harness, adapter, settlement, sell_token, want, proceeds_receiver, solver, approved
):
    half = LOT_AMOUNT // 2
    order, signature = adapter.order_for(harness.address, sell_token.address, NONCE, half)
    order = tuple(order)
    quote = order[10][0]
    surplus = 3 * WAD
    # The solver's interaction leaves the payment on the settlement; the
    # whole balance (surplus included) goes to the receiver.
    interactions = [
        (
            True,
            want.address,
            0,
            _transfer_from_calldata(solver, settlement.address, quote + surplus),
        )
    ]
    with boa.env.prank(solver):
        settlement.settle(_calldata(order), bytes(signature), interactions, b"", solver)
    assert sell_token.balanceOf(solver) == half
    assert harness.available(sell_token.address) == half
    assert want.balanceOf(proceeds_receiver) == quote + surplus
    assert not settlement.isNonceValid(harness.address, NONCE)
    # Fill-or-kill: the nonce is burnt right after the signature check, so
    # the same order (still acceptable to the auction) never settles twice;
    # the same ask under a fresh nonce does.
    with boa.env.prank(solver), boa.reverts(custom_err("InvalidNonce()")):
        settlement.settle(_calldata(order), bytes(signature), interactions, b"", solver)
    reposted = (*order[:4], NONCE + 1, *order[5:])
    with boa.env.prank(solver):
        settlement.settle(
            _calldata(reposted), _signature(adapter, reposted), interactions, b"", solver
        )
    assert sell_token.balanceOf(solver) == LOT_AMOUNT
    assert harness.available(sell_token.address) == 0


def test_settle_internal_pays_from_the_solver_balance(
    harness, adapter, settlement, sell_token, want, proceeds_receiver, solver, approved
):
    order, signature = adapter.order_for(
        harness.address, sell_token.address, NONCE, LOT_AMOUNT // 2
    )
    order = tuple(order)
    quote = order[10][0]
    with boa.env.prank(solver):
        settlement.settleInternal(_calldata(order), bytes(signature), [], b"")
    assert sell_token.balanceOf(solver) == LOT_AMOUNT // 2
    assert want.balanceOf(proceeds_receiver) == quote
    assert harness.available(sell_token.address) == LOT_AMOUNT // 2


def test_fees_never_lower_the_receiver_below_the_signed_amount(
    harness,
    adapter,
    settlement,
    sell_token,
    want,
    proceeds_receiver,
    protocol_fee_receiver,
    solver,
    approved,
    make_order,
):
    partner = boa.env.generate_address("partner")
    order = make_order(partner_info=pack_partner_info(partner, 100, 50))
    signature = _signature(adapter, order)
    quote = order[10][0]
    # A fill whose balance after fees falls short of the signed amount
    # reverts; one that covers it pays the fees out of the surplus.
    short = [
        (True, want.address, 0, _transfer_from_calldata(solver, settlement.address, quote + 1))
    ]
    with boa.env.prank(solver), boa.reverts():
        settlement.settle(_calldata(order), signature, short, b"", solver)
    gross = quote * 10_000 // (10_000 - 150) + 1
    covered = [(True, want.address, 0, _transfer_from_calldata(solver, settlement.address, gross))]
    with boa.env.prank(solver):
        settlement.settle(_calldata(order), signature, covered, b"", solver)
    assert want.balanceOf(partner) == gross * 100 // 10_000
    assert want.balanceOf(protocol_fee_receiver) == gross * 50 // 10_000
    assert want.balanceOf(proceeds_receiver) >= quote


def test_settlement_rejects_what_the_auction_rejects(
    harness,
    adapter,
    settlement,
    sell_token,
    want,
    solver,
    approved,
    make_order,
    registry,
    emergency_owner,
):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    cheaper = make_order(buy_amount=quote - 1)
    interactions = [
        (True, want.address, 0, _transfer_from_calldata(solver, settlement.address, quote))
    ]
    # The settlement lets the auction's typed revert bubble to the solver.
    with boa.env.prank(solver), boa.reverts(custom_err("BadBuyAmount()")):
        settlement.settle(
            _calldata(cheaper), _signature(adapter, cheaper), interactions, b"", solver
        )
    # A disabled adapter answers 0xffffffff through the router: the
    # settlement reports that as its own InvalidContractSignature.
    order = make_order()
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter)
    with boa.env.prank(solver), boa.reverts(custom_err("InvalidContractSignature()")):
        settlement.settle(_calldata(order), _signature(adapter, order), interactions, b"", solver)
    assert sell_token.balanceOf(harness.address) == LOT_AMOUNT


def test_settlement_liveness_checks_are_the_protocols(
    harness, adapter, settlement, sell_token, want, solver, approved, make_order
):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    interactions = [
        (True, want.address, 0, _transfer_from_calldata(solver, settlement.address, quote))
    ]
    other_solver = boa.env.generate_address("other_solver")
    exclusive = make_order(executor=other_solver, exclusivity_deadline=harness.frame_end())
    with boa.env.prank(solver), boa.reverts(custom_err("InvalidExecutor()")):
        settlement.settle(
            _calldata(exclusive), _signature(adapter, exclusive), interactions, b"", solver
        )
    # The settlement's expiry is exclusive where the auction's bound is
    # inclusive: at the expiry second the auction still validates, the
    # settlement no longer fills.
    expiring = make_order(expiry=_now())
    assert (
        harness.isValidSignature(
            order_digest_reference(expiring, bytes(settlement.DOMAIN_SEPARATOR())),
            _signature(adapter, expiring),
        )
        == ERC1271_MAGIC_VALUE
    )
    with boa.env.prank(solver), boa.reverts(custom_err("OrderExpired()")):
        settlement.settle(
            _calldata(expiring), _signature(adapter, expiring), interactions, b"", solver
        )


def test_fills_without_a_signature_are_refused(
    harness, adapter, settlement, sell_token, want, solver, approved, make_order
):
    # The settlement skips the signature only when the taker is the caller;
    # the auction never calls the settlement, so every fill goes through the
    # router, which answers 0xffffffff to anything shorter than a prefix.
    cheaper = make_order(buy_amount=1)
    interactions = [(True, want.address, 0, _transfer_from_calldata(solver, settlement.address, 1))]
    with boa.env.prank(solver), boa.reverts(custom_err("InvalidContractSignature()")):
        settlement.settle(_calldata(cheaper), b"", interactions, b"", solver)
    assert sell_token.balanceOf(harness.address) == LOT_AMOUNT
