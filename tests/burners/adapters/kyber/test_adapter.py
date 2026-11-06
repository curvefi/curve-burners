"""KyberDsloAdapter: the standalone KyberSwap DSLO adapter behind the prefix
route.

Validation runs end-to-end through the auction's signature router: the order
is handed to takers with signature `adapter ++ abi_encode(order)`, the harness
strips the prefix and forwards the bare order, the adapter proves the
protocol's order digest and the order shape, and every economic decision
comes from the auction's shared check_order view. Shape failures revert with
the adapter's OrderNotValid(reason); the auction's typed check_order revert
bubbles unchanged for economic ones. Settlement runs through a protocol mock
that follows DSOrderMixin.fillOrderTo for the signature, predicate, getter,
fee and transfer paths.
"""

from typing import Any

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak

from tests.burners.adapters.kyber.test_module import (
    GET_MAKER_AMOUNT,
    GET_TAKER_AMOUNT,
    MAX_PREDICATE,
    ORDER_ABI,
    and_predicate,
    api_predicate,
    domain_separator_reference,
    linear_getters,
    order_digest_reference,
    pack_fee_config,
    timestamp_below,
    with_fields,
)
from tests.burners.conftest import custom_err

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WAD = 10**18
MAX_UINT256 = 2**256 - 1

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
AUCTION_LENGTH = 24 * 60 * 60
LOT_AMOUNT = 250 * WAD
SALT = 0xABCDEF

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
def taker():
    return boa.env.generate_address("taker")


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
def protocol_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/DsloProtocolMock.vy")


@pytest.fixture(scope="module")
def protocol(protocol_deployer):
    return protocol_deployer.deploy(bytes(32))


@pytest.fixture(scope="module")
def domain_separator(protocol):
    return domain_separator_reference(boa.env.evm.patch.chain_id, protocol.address)


@pytest.fixture(scope="module")
def adapter_deployer():
    return boa.load_partial("contracts/burners/adapters/kyber/KyberDsloAdapter.vy")


@pytest.fixture
def registry(role_source):
    return boa.load("contracts/burners/adapters/AdapterRegistry.vy", role_source.address)


@pytest.fixture
def adapter(adapter_deployer, protocol):
    return adapter_deployer.deploy(protocol)


@pytest.fixture
def harness(role_source, want, proceeds_receiver, registry, adapter, owner, protocol):
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
        # The protocol is the executor: a fill pulls the maker asset itself.
        registry.set_adapter(adapter, protocol)
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


@pytest.fixture
def make_order(harness, sell_token, want, proceeds_receiver, lot, protocol):
    def _make(**overrides) -> tuple:
        making = overrides.pop("makingAmount", LOT_AMOUNT)
        taking = overrides.pop("takingAmount", None)
        if taking is None:
            taking = harness.getAmountNeeded(sell_token.address, min(making, LOT_AMOUNT))
        getters = linear_getters(making, taking)
        order = (
            SALT,
            sell_token.address,
            want.address,
            harness.address,
            proceeds_receiver,
            ZERO_ADDRESS,
            making,
            taking,
            0,
            b"",
            b"",
            overrides.pop("getMakerAmount", getters[0]),
            overrides.pop("getTakerAmount", getters[1]),
            # timestampBelow is strict: lot_end + 1 lets the lot's last
            # second fill.
            overrides.pop("predicate", timestamp_below(harness.frame_end() + 1)),
            b"",
        )
        return with_fields(order, **overrides)

    return _make


def _prefix(adapter: Any) -> bytes:
    return bytes.fromhex(str(adapter.address)[2:])


def _bare(order: tuple) -> bytes:
    return encode([ORDER_ABI], [order])


def _signature(adapter: Any, order: tuple) -> bytes:
    """What a taker passes as params.signature.orderSignature."""
    return _prefix(adapter) + _bare(order)


def _validate(harness, adapter, order: tuple, signature: bytes | None = None, domain=None):
    signature = _signature(adapter, order) if signature is None else signature
    domain = (
        domain_separator_reference(boa.env.evm.patch.chain_id, adapter.protocol())
        if domain is None
        else domain
    )
    return harness.isValidSignature(order_digest_reference(order, domain), signature)


def _fill(protocol, adapter, order: tuple, taker, making=0, taking=0, threshold=None):
    """fillOrderTo with the maker signature only (the mock skips the operator
    co-signature); the taker receives the maker asset. The default rate
    threshold is the order's own: the full taking amount for a making-amount
    fill, no minimum for a taking-amount one."""
    if threshold is None:
        threshold = order[7] if making else 0
    params = (order, (_signature(adapter, order), b""), 0, taker, b"")
    with boa.env.prank(taker):
        return protocol.fillOrderTo(params, (making, taking, threshold), ZERO_ADDRESS)


# Deployment pinning


def test_deploy_pins_protocol_and_domain(adapter, protocol, domain_separator):
    assert adapter.protocol() == protocol.address
    assert bytes(adapter.domain_separator()) == domain_separator
    assert bytes(protocol.DOMAIN_SEPARATOR()) == domain_separator
    assert adapter.ADAPTER_VERSION() == "KyberDsloAdapter"


def test_deploy_rejects_zero_protocol(adapter_deployer):
    with boa.reverts(custom_err("BadProtocol()")):
        adapter_deployer.deploy(ZERO_ADDRESS)


def test_deploy_rejects_foreign_domain(protocol_deployer, adapter_deployer):
    # A protocol whose separator is not the fixed name and version over its
    # own address and this chain (a proxy, a fork with another name).
    foreign = protocol_deployer.deploy(FOREIGN_DOMAIN)
    with boa.reverts(custom_err("BadDomain()")):
        adapter_deployer.deploy(foreign)


def test_deploy_rejects_non_protocol(adapter_deployer, want):
    with boa.reverts():
        adapter_deployer.deploy(want)


# Routing: the Kyber rail is a regular registry adapter behind the prefix


def test_prefixed_bare_order_is_valid_and_unprefixed_is_not(harness, adapter, make_order):
    order = make_order()
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE
    assert _validate(harness, adapter, order, _bare(order)) == ERC1271_INVALID


def test_direct_call_is_not_an_auction(adapter, make_order):
    # Called directly, msg.sender is not the maker: nothing is proven.
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "BadMaker")):
        adapter.isValidSignature(
            order_digest_reference(order, bytes(adapter.domain_separator())), _bare(order)
        )


# Transport encoding


def test_non_canonical_payload_rejected(harness, adapter, make_order):
    order = make_order()
    bare = _bare(order)
    # Trailing bytes decode but are not the order's one encoding.
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        _validate(harness, adapter, order, _signature(adapter, order) + b"\x00")
    # A loose head offset decodes to the same order but is not canonical.
    loose = (64).to_bytes(32, "big") + b"\x00" * 32 + bare[32:]
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        _validate(harness, adapter, order, _prefix(adapter) + loose)
    # Truncation fails ABI decoding before any check runs.
    with boa.reverts():
        _validate(harness, adapter, order, _signature(adapter, order)[:-1])
    with boa.reverts():
        _validate(harness, adapter, order, _prefix(adapter))


def test_field_above_bound_fails_to_decode(harness, adapter, make_order):
    with boa.reverts():
        _validate(harness, adapter, make_order(predicate=b"\x01" * (MAX_PREDICATE + 1)))


# Protocol constants and shape


def test_hash_mismatch_invalid(harness, adapter, make_order):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        harness.isValidSignature(keccak(b"other"), _signature(adapter, order))


def test_foreign_domain_separator_invalid(harness, adapter, make_order):
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        _validate(harness, adapter, make_order(), domain=FOREIGN_DOMAIN)


def test_other_maker_invalid(harness, adapter, make_order):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadMaker")):
        _validate(harness, adapter, make_order(maker=boa.env.generate_address("other")))


@pytest.mark.parametrize("field", ["makerAssetData", "takerAssetData"])
def test_asset_data_invalid(harness, adapter, make_order, field):
    # Asset data is appended to the transferFrom calldata.
    with boa.reverts(custom_err("OrderNotValid(string)", "BadAssetData")):
        _validate(harness, adapter, make_order(**{field: b"\x00"}))


def test_non_linear_getters_invalid(harness, adapter, make_order):
    cheap = GET_TAKER_AMOUNT + encode(["uint256", "uint256"], [LOT_AMOUNT, 1])
    with boa.reverts(custom_err("OrderNotValid(string)", "BadAmountGetter")):
        _validate(harness, adapter, make_order(getTakerAmount=cheap))
    cheap = GET_MAKER_AMOUNT + encode(["uint256", "uint256"], [LOT_AMOUNT * 2, 1])
    with boa.reverts(custom_err("OrderNotValid(string)", "BadAmountGetter")):
        _validate(harness, adapter, make_order(getMakerAmount=cheap))


def test_empty_getters_valid(harness, adapter, make_order):
    # Exact-amount fills only, priced from the order amounts.
    order = make_order(getMakerAmount=b"", getTakerAmount=b"")
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE


def test_interaction_invalid(harness, adapter, make_order):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadInteraction")):
        _validate(harness, adapter, make_order(interaction=b"\x00" * 40))
    # Even one the protocol would ignore (shorter than an address).
    with boa.reverts(custom_err("OrderNotValid(string)", "BadInteraction")):
        _validate(harness, adapter, make_order(interaction=b"\x00"))


def test_predicate_shapes(harness, adapter, make_order, protocol):
    end = harness.frame_end()
    nonce_shape = api_predicate(protocol.address, harness.address, 0, end + 1)
    assert _validate(harness, adapter, make_order(predicate=nonce_shape)) == ERC1271_MAGIC_VALUE
    with boa.reverts(custom_err("OrderNotValid(string)", "BadPredicate")):
        _validate(harness, adapter, make_order(predicate=b""))
    with boa.reverts(custom_err("OrderNotValid(string)", "BadPredicate")):
        _validate(
            harness,
            adapter,
            make_order(predicate=and_predicate([adapter.address], [timestamp_below(end + 1)])),
        )


def test_pass_through_fields_valid(harness, adapter, make_order, taker):
    recipient = boa.env.generate_address("fee_recipient")
    for overrides in (
        {"salt": SALT + 1},
        {"allowedSender": taker},
        {"feeConfig": pack_fee_config(recipient, 300, True)},
        {"feeConfig": pack_fee_config(recipient, 10, False)},
    ):
        assert _validate(harness, adapter, make_order(**overrides)) == ERC1271_MAGIC_VALUE


# Economics: the auction's check_order reverts bubble through the adapter.
# Every branch of check_order is covered directly in test_erc1271_dispatcher.


def test_auction_economics_revert_bubbles_through_adapter(harness, adapter, make_order, sell_token):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    with boa.reverts(custom_err("BadBuyAmount()")):
        _validate(harness, adapter, make_order(takingAmount=quote - 1))
    assert _validate(harness, adapter, make_order(takingAmount=quote + 1)) == ERC1271_MAGIC_VALUE
    # An empty receiver resolves to the maker, the auction itself, which is an
    # accepted receiver (push_target forwards).
    assert _validate(harness, adapter, make_order(receiver=ZERO_ADDRESS)) == ERC1271_MAGIC_VALUE
    with boa.reverts(custom_err("BadReceiver()")):
        _validate(harness, adapter, make_order(receiver=boa.env.generate_address("other")))
    with boa.reverts(custom_err("BadBuyToken()")):
        _validate(harness, adapter, make_order(takerAsset=sell_token.address))
    with boa.reverts(custom_err("BadSellAmount()")):
        _validate(harness, adapter, make_order(makingAmount=LOT_AMOUNT + 1, takingAmount=quote))


def test_expiry_bounds_from_the_predicate(harness, adapter, make_order):
    end = harness.frame_end()
    # timestampBelow(end + 1) fills through the lot end; one more fails.
    assert _validate(harness, adapter, make_order(predicate=timestamp_below(end + 1)))
    with boa.reverts(custom_err("BadValidTo()")):
        _validate(harness, adapter, make_order(predicate=timestamp_below(end + 2)))
    now = boa.env.evm.vm.state.timestamp
    with boa.reverts(custom_err("BadValidTo()")):
        _validate(harness, adapter, make_order(predicate=timestamp_below(now)))
    assert _validate(harness, adapter, make_order(predicate=timestamp_below(now + 1)))


def test_partial_fill_order_valid(harness, adapter, make_order):
    assert _validate(harness, adapter, make_order(makingAmount=LOT_AMOUNT // 3)) == (
        ERC1271_MAGIC_VALUE
    )


# Publisher helpers


def test_order_for_builds_the_publishable_pair(
    harness, adapter, sell_token, want, proceeds_receiver, lot, domain_separator
):
    order, signature = adapter.order_for(harness.address, sell_token.address, SALT)
    order = tuple(order)
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    assert order == (
        SALT,
        sell_token.address,
        want.address,
        harness.address,
        proceeds_receiver,
        ZERO_ADDRESS,
        LOT_AMOUNT,
        quote,
        0,
        b"",
        b"",
        *linear_getters(LOT_AMOUNT, quote),
        timestamp_below(harness.frame_end() + 1),
        b"",
    )
    assert bytes(signature) == _signature(adapter, order)
    assert (
        harness.isValidSignature(order_digest_reference(order, domain_separator), signature)
        == ERC1271_MAGIC_VALUE
    )

    partial, partial_signature = adapter.order_for(
        harness.address, sell_token.address, SALT, LOT_AMOUNT // 4
    )
    assert partial[6] == LOT_AMOUNT // 4
    assert partial[7] == harness.getAmountNeeded(sell_token.address, LOT_AMOUNT // 4)
    assert (
        harness.isValidSignature(
            order_digest_reference(tuple(partial), domain_separator), partial_signature
        )
        == ERC1271_MAGIC_VALUE
    )


def test_order_for_stays_valid_as_the_curve_decays(
    harness, adapter, sell_token, lot, domain_separator
):
    now = boa.env.evm.vm.state.timestamp
    harness.set_frame(now)
    order, signature = adapter.order_for(harness.address, sell_token.address, SALT)
    boa.env.time_travel(seconds=10 * STEP_DURATION)
    assert harness.getAmountNeeded(sell_token.address, LOT_AMOUNT) < order[7]
    assert (
        harness.isValidSignature(order_digest_reference(tuple(order), domain_separator), signature)
        == ERC1271_MAGIC_VALUE
    )


def test_order_for_rejects_empty_lots(harness, adapter, sell_token, erc20_deployer):
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, sell_token.address, SALT)
    fresh = erc20_deployer.deploy("Fresh", "FRESH", 18)
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, fresh.address, SALT)


def test_describe(adapter, make_order, proceeds_receiver, harness, protocol):
    recipient = boa.env.generate_address("fee_recipient")
    end = harness.frame_end()
    order = make_order(
        feeConfig=pack_fee_config(recipient, 300, True),
        predicate=api_predicate(protocol.address, harness.address, 5, end + 1),
    )
    assert adapter.describe(order) == (proceeds_receiver, recipient, 300, True, end)
    assert adapter.describe(make_order(receiver=ZERO_ADDRESS))[0] == harness.address
    with boa.reverts(custom_err("OrderNotValid(string)", "BadPredicate")):
        adapter.describe(make_order(predicate=b""))


# Settlement through the protocol mock


def test_mock_fills_a_published_order_in_parts(
    harness, adapter, protocol, sell_token, want, proceeds_receiver, lot, taker, owner
):
    order, _ = adapter.order_for(harness.address, sell_token.address, SALT)
    order = tuple(order)
    quote = order[7]
    with boa.env.prank(owner):
        harness.sync_executor_approvals(protocol, [sell_token.address])
    assert sell_token.allowance(harness, protocol) == MAX_UINT256
    want._mint_for_testing(taker, 2 * quote)
    with boa.env.prank(taker):
        want.approve(protocol, MAX_UINT256)

    # A making-amount fill of a quarter pays the proportional quote rounded up.
    quarter = LOT_AMOUNT // 4
    quarter_quote = -(-quote * quarter // LOT_AMOUNT)
    _fill(protocol, adapter, order, taker, making=quarter)
    assert sell_token.balanceOf(taker) == quarter
    assert want.balanceOf(proceeds_receiver) == quarter_quote
    assert harness.available(sell_token) == LOT_AMOUNT - quarter

    # A taking-amount fill delivers the floored maker amount for what is paid.
    paid = quote // 10
    _fill(protocol, adapter, order, taker, taking=paid, threshold=0)
    assert sell_token.balanceOf(taker) == quarter + paid * LOT_AMOUNT // quote
    assert want.balanceOf(proceeds_receiver) == quarter_quote + paid

    # Overpaying for the remainder is capped at the remainder, priced up.
    remaining = LOT_AMOUNT - sell_token.balanceOf(taker)
    remaining_quote = -(-quote * remaining // LOT_AMOUNT)
    _fill(protocol, adapter, order, taker, taking=quote, threshold=0)
    assert sell_token.balanceOf(taker) == LOT_AMOUNT
    assert want.balanceOf(proceeds_receiver) == quarter_quote + paid + remaining_quote
    assert want.balanceOf(proceeds_receiver) >= quote
    digest = order_digest_reference(order, bytes(adapter.domain_separator()))
    assert protocol.remainingRaw(digest) == 1


def test_mock_rejects_an_underpriced_order(
    harness, adapter, protocol, sell_token, want, lot, taker, owner, make_order
):
    with boa.env.prank(owner):
        harness.sync_executor_approvals(protocol, [sell_token.address])
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    want._mint_for_testing(taker, quote)
    with boa.env.prank(taker):
        want.approve(protocol, MAX_UINT256)
    with boa.reverts(custom_err("BadSignature()")):
        _fill(protocol, adapter, make_order(takingAmount=quote - 1), taker, making=LOT_AMOUNT)


def test_mock_fee_branches_keep_the_maker_whole(
    harness, adapter, protocol, sell_token, want, proceeds_receiver, lot, taker, owner, make_order
):
    with boa.env.prank(owner):
        harness.sync_executor_approvals(protocol, [sell_token.address])
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    fee_recipient = boa.env.generate_address("fee_recipient")
    want._mint_for_testing(taker, 3 * quote)
    with boa.env.prank(taker):
        want.approve(protocol, MAX_UINT256)
    half = LOT_AMOUNT // 2
    half_quote = -(-quote * half // LOT_AMOUNT)

    # Taker-asset fee: paid on top by the taker.
    order = make_order(feeConfig=pack_fee_config(fee_recipient, 300, True))
    making, taking, fee = _fill(protocol, adapter, order, taker, making=half)
    assert (making, fee) == (half, -(-half_quote * 300 // 100_000))
    assert taking == half_quote + fee
    assert want.balanceOf(proceeds_receiver) == half_quote
    assert want.balanceOf(fee_recipient) == fee
    assert sell_token.balanceOf(taker) == half

    # Maker-asset fee: carved out of the taker's receipt, the maker still
    # gives half and receives the full quote for it.
    # The lot is half sold now, so the order is priced from the earlier quote.
    order = make_order(
        salt=SALT + 1, takingAmount=quote, feeConfig=pack_fee_config(fee_recipient, 10, False)
    )
    making, taking, fee = _fill(protocol, adapter, order, taker, making=half)
    assert (making, taking, fee) == (half, half_quote, -(-half * 10 // 100_000))
    assert want.balanceOf(proceeds_receiver) == 2 * half_quote
    assert sell_token.balanceOf(fee_recipient) == fee
    assert sell_token.balanceOf(taker) == 2 * half - fee
    assert sell_token.balanceOf(harness) == 0


def test_mock_checks_the_signature_on_the_first_fill_only(
    harness, adapter, protocol, sell_token, want, lot, taker, owner, emergency_owner, registry
):
    order, _ = adapter.order_for(harness.address, sell_token.address, SALT)
    order = tuple(order)
    with boa.env.prank(owner):
        harness.sync_executor_approvals(protocol, [sell_token.address])
    want._mint_for_testing(taker, order[7])
    with boa.env.prank(taker):
        want.approve(protocol, MAX_UINT256)
    quarter = LOT_AMOUNT // 4
    _fill(protocol, adapter, order, taker, making=quarter)

    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter)
    fresh, _ = adapter.order_for(harness.address, sell_token.address, SALT + 1)
    with boa.reverts(custom_err("BadSignature()")):
        _fill(protocol, adapter, tuple(fresh), taker, making=quarter)
    # The remainder of the started order still fills at its signed price...
    _fill(protocol, adapter, order, taker, making=quarter)
    assert sell_token.balanceOf(taker) == 2 * quarter
    # ...until the allowance sync completes the emergency procedure.
    harness.sync_executor_approvals(protocol, [sell_token.address])
    assert sell_token.allowance(harness, protocol) == 0
    with boa.reverts():
        _fill(protocol, adapter, order, taker, making=quarter)


def test_mock_predicate_gates_the_fill(
    harness, adapter, protocol, sell_token, want, lot, taker, owner, make_order
):
    with boa.env.prank(owner):
        harness.sync_executor_approvals(protocol, [sell_token.address])
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    want._mint_for_testing(taker, quote)
    with boa.env.prank(taker):
        want.approve(protocol, MAX_UINT256)
    end = harness.frame_end()
    # The API shape with the auction's nonce gates on the protocol's nonce
    # manager; the auction never advances it, so 0 holds.
    order = make_order(predicate=api_predicate(protocol.address, harness.address, 1, end + 1))
    with boa.reverts(custom_err("PredicateFailed()")):
        _fill(protocol, adapter, order, taker, making=LOT_AMOUNT // 2)
    order = make_order(predicate=api_predicate(protocol.address, harness.address, 0, end + 1))
    _fill(protocol, adapter, order, taker, making=LOT_AMOUNT // 2)
    assert sell_token.balanceOf(taker) == LOT_AMOUNT // 2
    # Past the expiry the predicate fails even though the order was validated.
    boa.env.time_travel(seconds=end + 1 - boa.env.evm.vm.state.timestamp)
    with boa.reverts(custom_err("PredicateFailed()")):
        _fill(protocol, adapter, order, taker, making=LOT_AMOUNT // 4)


def test_mock_empty_getters_allow_exact_fills_only(
    harness, adapter, protocol, sell_token, want, lot, taker, owner, make_order, proceeds_receiver
):
    with boa.env.prank(owner):
        harness.sync_executor_approvals(protocol, [sell_token.address])
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    want._mint_for_testing(taker, quote)
    with boa.env.prank(taker):
        want.approve(protocol, MAX_UINT256)
    order = make_order(getMakerAmount=b"", getTakerAmount=b"")
    with boa.reverts(custom_err("WrongAmount()")):
        _fill(protocol, adapter, order, taker, making=LOT_AMOUNT // 2)
    _fill(protocol, adapter, order, taker, making=LOT_AMOUNT)
    assert sell_token.balanceOf(taker) == LOT_AMOUNT
    assert want.balanceOf(proceeds_receiver) == quote


# Read-only reentrancy: nothing about signed orders is readable mid-take


def test_erc1271_is_locked_during_a_take_callback(harness, sell_token, want, lot):
    from tests.burners.adapters.cow.test_adapter import LOCK_PROBE_TAKER

    probe = boa.loads(LOCK_PROBE_TAKER, name="LockProbeTaker")
    amount = LOT_AMOUNT // 2
    want._mint_for_testing(probe.address, harness.getAmountNeeded(sell_token.address, amount))
    probe.run(harness.address, sell_token.address, amount)
    assert not probe.check_order_call_succeeded()
    assert not probe.signature_call_succeeded()
