"""OKXIntentAdapter: the standalone OKX Intent Swap adapter behind the prefix
route.

Validation runs end-to-end through the auction's signature router: the order
is handed to solvers with signature `adapter ++ abi_encode(order)` (the
settlement prefixes the owner and strips it again), the harness strips the
adapter prefix and forwards the bare order, the adapter proves the
settlement's order digest and the order shape, and every economic decision
comes from the auction's shared check_order view. Shape failures revert with
the adapter's OrderNotValid(reason); the auction's typed check_order revert
bubbles unchanged for economic ones. The settlement mock then fills orders
the way the OKX Settlement does: an allowlisted solver, the signature checked
on every fill, the uid filled once, the sold tokens pulled through the
approval proxy, the receiver paid at least the signed toTokenAmount after
commissions and the solver fee.
"""

from typing import Any

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak

from tests.burners.conftest import custom_err
from tests.burners.test_okx_module import (
    BUY_ETH_ADDRESS,
    COMMISSION_DENOM,
    EIP1271_TRADE_FLAGS,
    EXACT_IN,
    FROM_TOKEN_COMMISSION,
    IS_TO_B,
    LABEL_OKX,
    ORDER_ABI,
    TO_TOKEN_COMMISSION,
    as_tuple,
    domain_separator_reference,
    order_digest_reference,
    order_uid_reference,
)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WAD = 10**18

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
AUCTION_LENGTH = 24 * 60 * 60
LOT_AMOUNT = 250 * WAD
APP_DATA = keccak(b"CURVE_DUTCH_AUCTION_OKX_APP_DATA")
BUY_KIND = keccak(text="buy")

FOREIGN_DOMAIN = keccak(b"FOREIGN_DOMAIN")

ERC1271_MAGIC_VALUE = bytes.fromhex("1626ba7e")
ERC1271_INVALID = bytes.fromhex("ffffffff")
NO_SOLVER_FEE = (0, ZERO_ADDRESS, 0)
NO_SURPLUS_FEE = (0, ZERO_ADDRESS, 0)
NO_INTERACTIONS = [[], [], []]


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
def settlement_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/OKXSettlementMock.vy")


@pytest.fixture(scope="module")
def token_approve_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/OKXTokenApproveMock.vy")


@pytest.fixture(scope="module")
def token_approve(token_approve_deployer):
    return token_approve_deployer.deploy(ZERO_ADDRESS)


@pytest.fixture(scope="module")
def settlement(settlement_deployer, token_approve):
    settlement = settlement_deployer.deploy(bytes(32), token_approve.address)
    # TokenApproveProxy.allowedApprove: the settlement may pull.
    token_approve.set_allowed(settlement.address, True)
    return settlement


@pytest.fixture(scope="module")
def domain_separator(settlement):
    return domain_separator_reference(boa.env.evm.patch.chain_id, settlement.address)


@pytest.fixture(scope="module")
def adapter_deployer():
    return boa.load_partial("contracts/burners/adapters/okx/OKXIntentAdapter.vy")


@pytest.fixture
def registry(role_source):
    return boa.load("contracts/burners/adapters/AdapterRegistry.vy", role_source.address)


@pytest.fixture
def adapter(adapter_deployer, settlement):
    return adapter_deployer.deploy(settlement)


@pytest.fixture
def harness(role_source, want, proceeds_receiver, registry, adapter, owner, token_approve):
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
        registry.set_adapter(adapter, token_approve.address)
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
    """Build a signed Order tuple the way a publisher (or the quote API's
    `signData.message`) would.

    Overrides address the eleven fields (from_token, to_token, owner,
    receiver, from_amount, to_amount, valid_to, app_data, swap_mode,
    partially_fillable, commissions).
    """

    def _make(**overrides) -> tuple:
        from_amount = overrides.get("from_amount", LOT_AMOUNT)
        to_amount = overrides.get("to_amount")
        if to_amount is None:
            to_amount = harness.getAmountNeeded(sell_token.address, min(from_amount, LOT_AMOUNT))
        fields = {
            "from_token": sell_token.address,
            "to_token": want.address,
            "owner": harness.address,
            "receiver": proceeds_receiver,
            "from_amount": from_amount,
            "to_amount": to_amount,
            "valid_to": harness.frame_end(),
            "app_data": APP_DATA,
            "swap_mode": EXACT_IN,
            "partially_fillable": False,
            "commissions": [],
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
    """What a publisher posts: the settlement forwards it verbatim after
    stripping the owner it prefixed."""
    return _prefix(adapter) + _bare(order)


def _settle_signature(owner: Any, adapter: Any, order: tuple) -> bytes:
    """The trade signature a solver submits: owner ++ adapter ++ order."""
    return bytes.fromhex(str(owner.address)[2:]) + _signature(adapter, order)


def _trade(
    order: tuple,
    signature: bytes,
    from_index: int = 0,
    to_index: int = 1,
    solver_fee: tuple = NO_SOLVER_FEE,
    flags: int = EIP1271_TRADE_FLAGS,
) -> tuple:
    """The legacy TradeLib.Data for an order."""
    return (
        from_index,
        to_index,
        order[2],
        order[3],
        order[4],
        order[5],
        order[6],
        order[7],
        flags,
        order[4],
        order[10],
        signature,
        solver_fee,
    )


@pytest.fixture
def validate(harness, adapter, domain_separator):
    """Route an order through the harness: digest under the settlement's
    domain, signature `adapter ++ abi_encode(order)` by default."""

    def _validate(order: tuple, signature: bytes | None = None, domain: bytes = domain_separator):
        signature = _signature(adapter, order) if signature is None else signature
        return harness.isValidSignature(order_digest_reference(order, domain), signature)

    return _validate


# Deployment pinning


def test_deploy_pins_settlement_domain_and_approval_target(
    adapter, settlement, domain_separator, token_approve
):
    assert adapter.settlement() == settlement.address
    assert bytes(adapter.domain_separator()) == domain_separator
    assert bytes(settlement.domainSeparator()) == domain_separator
    assert adapter.token_approve_proxy() == token_approve.address
    assert adapter.token_approve() == token_approve.address
    assert adapter.ADAPTER_VERSION() == "OKXIntentAdapter"


def test_deploy_rejects_zero_settlement(adapter_deployer):
    with boa.reverts(custom_err("BadSettlement()")):
        adapter_deployer.deploy(ZERO_ADDRESS)


def test_deploy_rejects_a_settlement_with_another_domain(
    settlement_deployer, adapter_deployer, token_approve
):
    # Another name, version or chain (a proxy, a fork, a future version):
    # domainSeparator differs from the one the adapter rebuilds.
    lying = settlement_deployer.deploy(FOREIGN_DOMAIN, token_approve.address)
    with boa.reverts(custom_err("BadDomain()")):
        adapter_deployer.deploy(lying)


def test_deploy_rejects_a_settlement_without_approval_target(
    settlement_deployer, token_approve_deployer, adapter_deployer
):
    unwired = settlement_deployer.deploy(bytes(32), ZERO_ADDRESS)
    with boa.reverts(custom_err("BadTokenApprove()")):
        adapter_deployer.deploy(unwired)
    # A proxy that answers no approval target fails the same way.
    blank_proxy = token_approve_deployer.deploy(ZERO_ADDRESS)
    boa.env.set_code(blank_proxy.address, b"")
    unanswered = settlement_deployer.deploy(bytes(32), blank_proxy.address)
    with boa.reverts():
        adapter_deployer.deploy(unanswered)


def test_deploy_follows_the_proxy_to_a_separate_approval_target(
    settlement_deployer, token_approve_deployer, adapter_deployer
):
    # The real pair are two contracts: the executor is what the proxy names.
    target = token_approve_deployer.deploy(ZERO_ADDRESS)
    proxy = token_approve_deployer.deploy(target.address)
    settlement = settlement_deployer.deploy(bytes(32), proxy.address)
    adapter = adapter_deployer.deploy(settlement)
    assert adapter.token_approve_proxy() == proxy.address
    assert adapter.token_approve() == target.address


# Routing: the OKX rail is a regular registry adapter behind the prefix


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
    # Truncated, empty, or more commissions than the bound: not one order.
    with boa.reverts():
        validate(order, _signature(adapter, order)[:-1])
    with boa.reverts():
        validate(order, _prefix(adapter))
    with boa.reverts():
        validate(order, _prefix(adapter) + _bare(order)[: 11 * 32])
    referrer = boa.env.generate_address("referrer")
    oversized = make_order(commissions=[(1, referrer, TO_TOKEN_COMMISSION)] * 4)
    with boa.reverts():
        validate(oversized)


def test_non_canonical_encodings_rejected(harness, adapter, make_order, validate):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        validate(order, _signature(adapter, order) + b"\x00")
    bare = _bare(order)
    # Move the commissions array one word further and pad the body so the
    # encoding is loose but still decodes to the same order.
    head_end = 32 + 11 * 32
    offset_slot = 32 + 10 * 32
    offset = int.from_bytes(bare[offset_slot : offset_slot + 32], "big")
    loose = bytearray(bare[:head_end]) + bytearray(32) + bytearray(bare[head_end:])
    loose[offset_slot : offset_slot + 32] = (offset + 32).to_bytes(32, "big")
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


def test_foreign_owner_invalid(harness, adapter, make_order, validate):
    other_owner = boa.env.generate_address("other_owner")
    with boa.reverts(custom_err("OrderNotValid(string)", "BadOwner")):
        validate(make_order(owner=other_owner))


@pytest.mark.parametrize(
    "overrides",
    [{"swap_mode": BUY_KIND}, {"swap_mode": bytes(32)}, {"partially_fillable": True}],
)
def test_order_flag_violations_invalid(harness, adapter, make_order, validate, overrides):
    # The settlement only ever hashes exactIn fill-or-kill orders; any other
    # shape never matches a digest it computes, and the flags are what make
    # the signed amounts the whole fill.
    with boa.reverts(custom_err("OrderNotValid(string)", "BadOrderFlags")):
        validate(make_order(**overrides))


def test_commissions_follow_the_protocol_rules(harness, adapter, make_order, validate):
    referrer = boa.env.generate_address("referrer")
    # Fees are the solver's cost above the signed buy amount: any commission
    # set the settlement accepts passes, in either direction and fund flow.
    for commissions in [
        [(3_000_000, referrer, TO_TOKEN_COMMISSION | LABEL_OKX)],
        [(1_000_000, referrer, FROM_TOKEN_COMMISSION), (500_000, referrer, FROM_TOKEN_COMMISSION)],
        [(1_000_000, referrer, FROM_TOKEN_COMMISSION | IS_TO_B)],
        [(0, ZERO_ADDRESS, TO_TOKEN_COMMISSION)] * 3,
    ]:
        assert validate(make_order(commissions=commissions)) == ERC1271_MAGIC_VALUE
    with boa.reverts(custom_err("OrderNotValid(string)", "CommissionTooHigh")):
        validate(make_order(commissions=[(30_000_001, referrer, TO_TOKEN_COMMISSION)]))
    with boa.reverts(custom_err("OrderNotValid(string)", "BadCommissionDirection")):
        validate(make_order(commissions=[(1, referrer, 0)]))
    with boa.reverts(custom_err("OrderNotValid(string)", "ZeroFeeRecipient")):
        validate(make_order(commissions=[(1, ZERO_ADDRESS, TO_TOKEN_COMMISSION)]))


def test_app_data_is_not_the_adapters_concern(harness, adapter, make_order, validate):
    assert validate(make_order(app_data=bytes(32))) == ERC1271_MAGIC_VALUE
    assert validate(make_order(app_data=keccak(b"any"))) == ERC1271_MAGIC_VALUE


# Economics: the whole order priced by check_order


def test_auction_economics_revert_bubbles_through_adapter(
    harness, adapter, make_order, sell_token, validate
):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    with boa.reverts(custom_err("BadBuyAmount()")):
        validate(make_order(to_amount=quote - 1))
    assert validate(make_order(to_amount=quote + 1)) == ERC1271_MAGIC_VALUE


def test_valid_to_is_the_validity_bound(harness, adapter, make_order, validate):
    with boa.reverts(custom_err("BadValidTo()")):
        validate(make_order(valid_to=harness.frame_end() + 1))
    with boa.reverts(custom_err("BadValidTo()")):
        validate(make_order(valid_to=0))
    with boa.reverts(custom_err("BadValidTo()")):
        validate(make_order(valid_to=_now() - 1))
    assert validate(make_order(valid_to=_now())) == ERC1271_MAGIC_VALUE


def test_wrong_tokens_or_receiver_invalid(harness, adapter, make_order, sell_token, validate):
    with boa.reverts(custom_err("BadBuyToken()")):
        validate(make_order(to_token=sell_token.address))
    # The native-coin placeholder (an allowed buy token on the settlement) is
    # never the payment token, and never a lot either.
    with boa.reverts(custom_err("BadBuyToken()")):
        validate(make_order(to_token=BUY_ETH_ADDRESS))
    with boa.reverts(custom_err("LotInactive()")):
        validate(make_order(from_token=BUY_ETH_ADDRESS))
    with boa.reverts(custom_err("BadReceiver()")):
        validate(make_order(receiver=boa.env.generate_address("thief")))
    with boa.reverts(custom_err("BadReceiver()")):
        validate(make_order(receiver=harness.address))
    # An empty receiver resolves to the owner, the auction itself.
    with boa.reverts(custom_err("BadReceiver()")):
        validate(make_order(receiver=ZERO_ADDRESS))


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
        validate(make_order(from_amount=LOT_AMOUNT + 1, to_amount=order[5]))


# Publisher helpers: order + signature for the auction, payload description


def test_order_for_builds_the_publishable_pair(
    harness, adapter, sell_token, want, proceeds_receiver, lot, validate
):
    order, signature = adapter.order_for(harness.address, sell_token.address)
    order = as_tuple(order)
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    assert order == (
        sell_token.address,
        want.address,
        harness.address,
        proceeds_receiver,
        LOT_AMOUNT,
        quote,
        harness.frame_end(),
        bytes(32),
        EXACT_IN,
        False,
        [],
    )
    # The signature is the documented template and validates end-to-end.
    assert bytes(signature) == _signature(adapter, order)
    assert len(signature) == 20 + 416
    assert validate(order, bytes(signature)) == ERC1271_MAGIC_VALUE

    # A partial amount is priced proportionally and validates as well; the
    # appData is free and salts otherwise equal orders.
    partial, partial_signature = adapter.order_for(
        harness.address, sell_token.address, LOT_AMOUNT // 4, APP_DATA
    )
    partial = as_tuple(partial)
    assert partial[4] == LOT_AMOUNT // 4
    assert partial[5] == harness.getAmountNeeded(sell_token.address, LOT_AMOUNT // 4)
    assert partial[7] == APP_DATA
    assert validate(partial, bytes(partial_signature)) == ERC1271_MAGIC_VALUE


def test_order_for_stays_valid_as_the_curve_decays(harness, adapter, sell_token, lot, validate):
    # A published order is a standing ask: once the curve falls below it, it
    # is still accepted (overpriced for us is fine), so publishers re-post
    # lower rather than cancel.
    harness.set_frame(_now())
    order, signature = adapter.order_for(harness.address, sell_token.address)
    order = as_tuple(order)
    boa.env.time_travel(seconds=10 * STEP_DURATION)
    assert harness.getAmountNeeded(sell_token.address, LOT_AMOUNT) < order[5]
    assert validate(order, bytes(signature)) == ERC1271_MAGIC_VALUE


def test_order_for_rejects_empty_lots(harness, adapter, sell_token, erc20_deployer):
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, sell_token.address)
    fresh = erc20_deployer.deploy("Fresh", "FRESH", 18)
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, fresh.address)


def test_order_for_rejects_a_window_past_uint32(
    role_source, want, proceeds_receiver, registry, adapter, sell_token
):
    # validTo is a uint32 on the settlement: a lot whose window ends past
    # 2**32 cannot be published.
    long_auction = boa.load(
        "contracts/testing/dutch_auction/CoreHarness.vy",
        want.address,
        proceeds_receiver,
        registry.address,
        role_source.address,
        START_TOTAL,
        FLOOR_TOTAL,
        STEP_DURATION,
        2**32,
    )
    boa.env.time_travel(seconds=1)
    sell_token._mint_for_testing(long_auction.address, LOT_AMOUNT)
    long_auction.stage(sell_token.address)
    assert long_auction.window(sell_token.address)[1] > 2**32 - 1
    with boa.reverts(custom_err("BadExpiration()")):
        adapter.order_for(long_auction.address, sell_token.address)


def test_describe_decodes_the_payload(harness, adapter, domain_separator, make_order):
    referrer = boa.env.generate_address("referrer")
    order = make_order(commissions=[(1000, referrer, TO_TOKEN_COMMISSION | LABEL_OKX)])
    described, digest, uid = adapter.describe(_bare(order))
    assert as_tuple(described) == order
    assert bytes(digest) == order_digest_reference(order, domain_separator)
    assert bytes(uid) == order_uid_reference(bytes(digest), order[2], order[6])
    with boa.reverts():
        adapter.describe(_bare(order)[:-1])


# Settlement through the mock: an allowlisted solver, the signature checked
# on every fill, the uid filled once, tokens pulled through the approval
# proxy, the receiver paid at least the signed amount after fees.


def _transfer_from_calldata(sender: str, receiver: str, amount: int) -> bytes:
    return keccak(text="transferFrom(address,address,uint256)")[:4] + encode(
        ["address", "address", "uint256"], [sender, receiver, amount]
    )


@pytest.fixture
def solver(want, settlement, sell_token):
    """An allowlisted solver funded with the payment token and approved
    toward the settlement (its interactions pull the payment)."""
    solver = boa.env.generate_address("solver")
    settlement.set_solver(solver, True)
    want._mint_for_testing(solver, 1_000_000 * WAD)
    with boa.env.prank(solver):
        want.approve(settlement.address, 2**256 - 1)
    return solver


@pytest.fixture
def approved(harness, token_approve, sell_token, lot):
    harness.sync_executor_approvals(token_approve.address, [sell_token.address])
    assert sell_token.allowance(harness.address, token_approve.address) == 2**256 - 1


def _settle(settlement, solver, trades, tokens, prices, interactions=None):
    with boa.env.prank(solver):
        settlement.settle(
            1, tokens, prices, trades, interactions or NO_INTERACTIONS, NO_SURPLUS_FEE
        )


def _payment(want, solver, settlement, amount) -> list:
    """Mid-settlement interaction moving the solver's payment onto the
    settlement, where the payouts come from."""
    return [
        [],
        [(want.address, 0, _transfer_from_calldata(solver, settlement.address, amount))],
        [],
    ]


def test_settle_fills_a_published_order_through_the_approval_proxy(
    harness, adapter, settlement, sell_token, want, proceeds_receiver, solver, approved
):
    half = LOT_AMOUNT // 2
    order, signature = adapter.order_for(harness.address, sell_token.address, half)
    order = as_tuple(order)
    quote = order[5]
    trade = _trade(order, bytes.fromhex(harness.address[2:]) + bytes(signature))
    tokens = [sell_token.address, want.address]
    # Clearing prices at the signed limit: fromAmount * fromPrice ==
    # toAmount * toPrice, so the gross proceeds are exactly the quote.
    prices = [quote, half]
    _settle(settlement, solver, [trade], tokens, prices, _payment(want, solver, settlement, quote))
    assert sell_token.balanceOf(settlement.address) == half
    assert harness.available(sell_token.address) == half
    assert want.balanceOf(proceeds_receiver) == quote
    _, _, uid = adapter.describe(_bare(order))
    assert settlement.filledAmount(uid) == half
    # Fill-or-kill: the uid is filled once, so the same order (still
    # acceptable to the auction) never settles twice; a re-post with
    # another appData is a new uid.
    with boa.env.prank(solver), boa.reverts(custom_err("OrderFilled()")):
        _settle(
            settlement, solver, [trade], tokens, prices, _payment(want, solver, settlement, quote)
        )
    reposted = (*order[:7], APP_DATA, *order[8:])
    _settle(
        settlement,
        solver,
        [_trade(reposted, _settle_signature(harness, adapter, reposted))],
        tokens,
        prices,
        _payment(want, solver, settlement, quote),
    )
    assert sell_token.balanceOf(settlement.address) == LOT_AMOUNT
    assert harness.available(sell_token.address) == 0
    assert want.balanceOf(proceeds_receiver) == 2 * quote


def test_fees_never_lower_the_receiver_below_the_signed_amount(
    harness, adapter, settlement, sell_token, want, proceeds_receiver, solver, approved, make_order
):
    referrer = boa.env.generate_address("referrer")
    tokens = [sell_token.address, want.address]
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)

    # A to-token commission and a to-token solver fee come off the gross
    # proceeds: at the signed limit price the payout falls short, so the
    # solver must clear higher; the receiver then gets at least the quote.
    order = make_order(commissions=[(1_000_000, referrer, TO_TOKEN_COMMISSION)])
    signature = _settle_signature(harness, adapter, order)
    solver_fee = (500_000, solver, TO_TOKEN_COMMISSION)
    short = _trade(order, signature, solver_fee=solver_fee)
    with boa.reverts(custom_err("ToTokenAmountNotRespected()")):
        _settle(
            settlement,
            solver,
            [short],
            tokens,
            [quote, LOT_AMOUNT],
            _payment(want, solver, settlement, quote),
        )
    gross = quote * COMMISSION_DENOM // (COMMISSION_DENOM - 1_500_000) + 1
    _settle(
        settlement,
        solver,
        [short],
        tokens,
        [gross, LOT_AMOUNT],
        _payment(want, solver, settlement, gross),
    )
    assert want.balanceOf(referrer) == gross * 1_000_000 // COMMISSION_DENOM
    assert want.balanceOf(solver) >= gross * 500_000 // COMMISSION_DENOM
    assert want.balanceOf(proceeds_receiver) >= quote
    assert sell_token.balanceOf(harness.address) == 0


def test_from_token_fees_never_pull_more_than_the_signed_amount(
    harness, adapter, settlement, sell_token, want, proceeds_receiver, solver, approved, make_order
):
    referrer = boa.env.generate_address("referrer")
    tokens = [sell_token.address, want.address]
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    # A from-token ToC commission is pulled from the owner straight to its
    # recipient and the rest to the settlement: the owner parts with
    # exactly the signed amount, the swapped net is smaller, so the solver
    # clears higher to still pay the floor.
    order = make_order(commissions=[(2_000_000, referrer, FROM_TOKEN_COMMISSION)])
    trade = _trade(order, _settle_signature(harness, adapter, order))
    with boa.reverts(custom_err("ToTokenAmountNotRespected()")):
        _settle(
            settlement,
            solver,
            [trade],
            tokens,
            [quote, LOT_AMOUNT],
            _payment(want, solver, settlement, quote),
        )
    fee = LOT_AMOUNT * 2_000_000 // COMMISSION_DENOM
    from_price = quote * LOT_AMOUNT // (LOT_AMOUNT - fee) + 1
    gross = (LOT_AMOUNT - fee) * from_price // LOT_AMOUNT + 1
    _settle(
        settlement,
        solver,
        [trade],
        tokens,
        [from_price, LOT_AMOUNT],
        _payment(want, solver, settlement, gross),
    )
    assert sell_token.balanceOf(harness.address) == 0
    assert sell_token.balanceOf(referrer) == fee
    assert sell_token.balanceOf(settlement.address) == LOT_AMOUNT - fee
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
    tokens = [sell_token.address, want.address]
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    cheaper = make_order(to_amount=quote - 1)
    # The settlement lets the auction's typed revert bubble to the solver.
    with boa.reverts(custom_err("BadBuyAmount()")):
        _settle(
            settlement,
            solver,
            [_trade(cheaper, _settle_signature(harness, adapter, cheaper))],
            tokens,
            [quote, LOT_AMOUNT],
            _payment(want, solver, settlement, quote),
        )
    # A disabled adapter answers 0xffffffff through the router: the
    # settlement reports that as its own invalid-signature error.
    order = make_order()
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter)
    with boa.reverts(custom_err("InvalidEip1271Signature()")):
        _settle(
            settlement,
            solver,
            [_trade(order, _settle_signature(harness, adapter, order))],
            tokens,
            [quote, LOT_AMOUNT],
            _payment(want, solver, settlement, quote),
        )
    assert sell_token.balanceOf(harness.address) == LOT_AMOUNT


def test_settlement_liveness_checks_are_the_protocols(
    harness, adapter, settlement, sell_token, want, solver, approved, make_order
):
    tokens = [sell_token.address, want.address]
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    order = make_order()
    trade = _trade(order, _settle_signature(harness, adapter, order))
    # Only allowlisted solvers settle.
    outsider = boa.env.generate_address("outsider")
    with boa.reverts(custom_err("NotASolver()")):
        _settle(settlement, outsider, [trade], tokens, [quote, LOT_AMOUNT])
    # The signed owner must be the prefixed owner.
    other = boa.env.generate_address("other")
    foreign = _trade(order, bytes.fromhex(other[2:]) + _signature(adapter, order))
    with boa.reverts():
        _settle(settlement, solver, [foreign], tokens, [quote, LOT_AMOUNT])
    # The partial-fill and buy-order bits are rejected before any hashing.
    with boa.reverts(custom_err("PartialFillNotSupported()")):
        _settle(
            settlement,
            solver,
            [_trade(order, trade[11], flags=EIP1271_TRADE_FLAGS | 2)],
            tokens,
            [quote, LOT_AMOUNT],
        )
    # validTo is inclusive on both sides: at the bound the auction validates
    # and the settlement fills; one second later neither does.
    expiring = make_order(valid_to=_now())
    assert (
        harness.isValidSignature(
            order_digest_reference(expiring, bytes(settlement.domainSeparator())),
            _signature(adapter, expiring),
        )
        == ERC1271_MAGIC_VALUE
    )
    boa.env.time_travel(seconds=1)
    with boa.reverts(custom_err("BadValidTo()")):
        _settle(
            settlement,
            solver,
            [_trade(expiring, _settle_signature(harness, adapter, expiring))],
            tokens,
            [quote, LOT_AMOUNT],
        )


def test_fills_without_a_proper_signature_are_refused(
    harness, adapter, settlement, sell_token, want, solver, approved, make_order
):
    tokens = [sell_token.address, want.address]
    cheaper = make_order(to_amount=1)
    # The owner prefix alone reaches the router as an empty signature,
    # which answers 0xffffffff.
    bare_owner = _trade(cheaper, bytes.fromhex(harness.address[2:]))
    with boa.reverts(custom_err("InvalidEip1271Signature()")):
        _settle(settlement, solver, [bare_owner], tokens, [1, LOT_AMOUNT])
    assert sell_token.balanceOf(harness.address) == LOT_AMOUNT
