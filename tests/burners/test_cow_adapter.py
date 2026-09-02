"""CowAdapter: the standalone GPv2 settlement adapter behind the prefix route.

Validation runs end-to-end through the auction's signature router: the order
is published with signature `adapter ++ abi_encode(order)`, the harness strips
the prefix and forwards the bare order, the adapter proves the canonical digest
and protocol constants, and every economic decision comes from the auction's
shared check_order view — reverted verbatim in the CoW-canonical OrderNotValid
ABI.
"""

from typing import Any

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak

from .conftest import custom_err


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
MAX_UINT256 = 2**256 - 1
WAD = 10**18

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
DECAY_FACTOR_RAY = 992031276831159793484252056
LOT_AMOUNT = 250 * WAD

APP_DATA = keccak(b"CURVE_DUTCH_AUCTION_TEST_APP_DATA")
DOMAIN_SEPARATOR = keccak(b"GPV2_TEST_DOMAIN")
FOREIGN_DOMAIN = keccak(b"FOREIGN_DOMAIN")

SELL_KIND = keccak(text="sell")
BUY_KIND = keccak(text="buy")
ERC20_BALANCE = keccak(text="erc20")
EXTERNAL_BALANCE = keccak(text="external")

ERC1271_MAGIC_VALUE = bytes.fromhex("1626ba7e")
ERC1271_INVALID = bytes.fromhex("ffffffff")

ORDER_TYPE = (
    "(address,address,address,uint256,uint256,uint32,bytes32,uint256,"
    "bytes32,bool,bytes32,bytes32)"
)
PAYLOAD_TYPE = "(bytes32[],(address,bytes32,bytes),bytes)"


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
def relayer():
    return boa.env.generate_address("relayer")


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
    return boa.load(
        "contracts/testing/dutch_auction/RoleSourceMock.vy", owner, emergency_owner
    )


@pytest.fixture(scope="module")
def settlement_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/SettlementMock.vy")


@pytest.fixture(scope="module")
def settlement(settlement_deployer, relayer):
    return settlement_deployer.deploy(DOMAIN_SEPARATOR, relayer)


@pytest.fixture(scope="module")
def adapter_deployer():
    return boa.load_partial("contracts/burners/cow/CowAdapter.vy")


@pytest.fixture
def registry(role_source):
    return boa.load(
        "contracts/burners/auction/adapters/AdapterRegistry.vy", role_source.address
    )


@pytest.fixture
def adapter(adapter_deployer, settlement):
    return adapter_deployer.deploy(settlement, APP_DATA)


@pytest.fixture
def harness(role_source, want, proceeds_receiver, registry, adapter, owner, relayer):
    harness = boa.load(
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
    with boa.env.prank(owner):
        registry.set_adapter(adapter, relayer)
        registry.activate_adapter(adapter)
        harness.enable_adapter(adapter)
    return harness


@pytest.fixture
def lot(harness, sell_token):
    sell_token._mint_for_testing(harness.address, LOT_AMOUNT)
    harness.stage(sell_token.address)
    return harness.lots(sell_token.address)


def _order_digest(order: tuple, domain_separator: bytes) -> bytes:
    order_type_hash = keccak(
        text=(
            "Order(address sellToken,address buyToken,address receiver,uint256 sellAmount,"
            "uint256 buyAmount,uint32 validTo,bytes32 appData,uint256 feeAmount,string kind,"
            "bool partiallyFillable,string sellTokenBalance,string buyTokenBalance)"
        )
    )
    struct_hash = keccak(
        encode(
            [
                "bytes32",
                "address",
                "address",
                "address",
                "uint256",
                "uint256",
                "uint32",
                "bytes32",
                "uint256",
                "bytes32",
                "bool",
                "bytes32",
                "bytes32",
            ],
            [order_type_hash, *order],
        )
    )
    return keccak(b"\x19\x01" + bytes(domain_separator) + struct_hash)


@pytest.fixture
def make_order(harness, sell_token, want, proceeds_receiver, lot):
    def _make(**overrides) -> tuple:
        amount = overrides.pop("sell_amount", LOT_AMOUNT)
        buy_amount = overrides.pop("buy_amount", None)
        if buy_amount is None:
            buy_amount = harness.quote(sell_token.address, min(amount, LOT_AMOUNT))
        order = {
            "sell_token": sell_token.address,
            "buy_token": want.address,
            "receiver": proceeds_receiver,
            "sell_amount": amount,
            "buy_amount": buy_amount,
            "valid_to": harness.frame_end(),
            "app_data": APP_DATA,
            "fee_amount": 0,
            "kind": SELL_KIND,
            "partially_fillable": True,
            "sell_balance": ERC20_BALANCE,
            "buy_balance": ERC20_BALANCE,
        }
        order.update(overrides)
        return tuple(order.values())

    return _make


def _prefix(adapter: Any) -> bytes:
    return bytes.fromhex(str(adapter.address)[2:])


def _bare(order: tuple) -> bytes:
    return encode([ORDER_TYPE], [order])


def _signature(adapter: Any, order: tuple) -> bytes:
    """What a publisher posts to the CoW orderbook as the eip1271 signature."""
    return _prefix(adapter) + _bare(order)


def _validate(harness, adapter, order: tuple, signature: bytes | None = None, domain=DOMAIN_SEPARATOR):
    signature = _signature(adapter, order) if signature is None else signature
    return harness.isValidSignature(_order_digest(order, domain), signature)


# Deployment pinning


def test_deploy_pins_settlement_domain_and_relayer(adapter, settlement, relayer):
    assert adapter.settlement() == settlement.address
    assert bytes(adapter.domain_separator()) == DOMAIN_SEPARATOR
    assert adapter.vault_relayer() == relayer
    assert bytes(adapter.app_data()) == APP_DATA
    assert adapter.ADAPTER_VERSION() == "CowAdapter"


def test_deploy_rejects_zero_settlement(adapter_deployer):
    with boa.reverts(custom_err("BadSettlement()")):
        adapter_deployer.deploy(ZERO_ADDRESS, APP_DATA)


def test_deploy_rejects_missing_domain_separator(settlement_deployer, adapter_deployer, relayer):
    empty_domain = settlement_deployer.deploy(bytes(32), relayer)
    with boa.reverts(custom_err("BadDomainSeparator()")):
        adapter_deployer.deploy(empty_domain, APP_DATA)


def test_deploy_rejects_missing_vault_relayer(settlement_deployer, adapter_deployer):
    no_relayer = settlement_deployer.deploy(DOMAIN_SEPARATOR, ZERO_ADDRESS)
    with boa.reverts(custom_err("BadVaultRelayer()")):
        adapter_deployer.deploy(no_relayer, APP_DATA)


# Routing: the CoW rail is a regular registry adapter behind the prefix


def test_prefixed_bare_order_is_valid_and_unprefixed_is_not(harness, adapter, make_order):
    order = make_order()
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE
    # No fallback route: the historical unprefixed encoding selects no adapter.
    assert _validate(harness, adapter, order, _bare(order)) == ERC1271_INVALID


def test_registry_and_local_switches_kill_the_rail(
    harness, adapter, make_order, registry, owner, emergency_owner
):
    order = make_order()
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter)
    assert _validate(harness, adapter, order) == ERC1271_INVALID
    with boa.env.prank(owner):
        registry.activate_adapter(adapter)
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE

    with boa.env.prank(emergency_owner):
        harness.disable_adapter(adapter)
    assert _validate(harness, adapter, order) == ERC1271_INVALID
    with boa.env.prank(owner):
        harness.enable_adapter(adapter)
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE


def test_sync_approves_the_vault_relayer(harness, sell_token, relayer, lot):
    # The registry executor of the CoW adapter is the relayer: staging grants
    # nothing, the permissionless sync does — like for any enabled adapter.
    assert sell_token.allowance(harness, relayer) == 0
    harness.sync_executor_approvals(relayer, [sell_token.address])
    assert sell_token.allowance(harness, relayer) == MAX_UINT256


# Transport encoding


def test_partial_fill_order_valid(harness, adapter, make_order):
    order = make_order(sell_amount=LOT_AMOUNT // 3)
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE


def test_composable_cow_wrapper_is_not_accepted(harness, adapter, make_order):
    # Only the bare order is a valid payload; the ComposableCoW (order,
    # payload) wrapper has no publisher in this deployment.
    order = make_order()
    wrapper = encode(
        [ORDER_TYPE, PAYLOAD_TYPE], [order, ([], (ZERO_ADDRESS, bytes(32), b""), b"")]
    )
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        _validate(harness, adapter, order, _prefix(adapter) + wrapper)


def test_non_canonical_padded_order_rejected(harness, adapter, make_order):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        _validate(harness, adapter, order, _signature(adapter, order) + b"\x00")


def test_zero_filled_order_fails_checks_not_decode(harness, adapter, lot):
    zero_order = (ZERO_ADDRESS, ZERO_ADDRESS, ZERO_ADDRESS, 0, 0, 0, bytes(32), 0,
                  bytes(32), False, bytes(32), bytes(32))
    with boa.reverts(custom_err("OrderNotValid(string)", "BadAppData")):
        _validate(harness, adapter, zero_order)


# Protocol constants


def test_hash_mismatch_invalid(harness, adapter, make_order):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        harness.isValidSignature(keccak(b"other"), _signature(adapter, order))


def test_foreign_domain_separator_invalid(harness, adapter, make_order):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        _validate(harness, adapter, order, domain=FOREIGN_DOMAIN)


def test_wrong_app_data_invalid(harness, adapter, make_order):
    order = make_order(app_data=keccak(b"other app data"))
    with boa.reverts(custom_err("OrderNotValid(string)", "BadAppData")):
        _validate(harness, adapter, order)


@pytest.mark.parametrize(
    "overrides",
    [
        {"fee_amount": 1},
        {"kind": BUY_KIND},
        {"partially_fillable": False},
    ],
)
def test_order_flag_violations_invalid(harness, adapter, make_order, overrides):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadOrderFlags")):
        _validate(harness, adapter, make_order(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [{"sell_balance": EXTERNAL_BALANCE}, {"buy_balance": EXTERNAL_BALANCE}],
)
def test_balance_mode_violations_invalid(harness, adapter, make_order, overrides):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadBalanceMode")):
        _validate(harness, adapter, make_order(**overrides))


# Economic checks bubble from check_order


def test_wrong_buy_token_invalid(harness, adapter, make_order, sell_token):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadToken")):
        _validate(harness, adapter, make_order(buy_token=sell_token.address))


def test_wrong_receiver_invalid(harness, adapter, make_order):
    stranger = boa.env.generate_address("stranger")
    with boa.reverts(custom_err("OrderNotValid(string)", "BadReceiver")):
        _validate(harness, adapter, make_order(receiver=stranger))


def test_unstaged_token_not_allowed(harness, adapter, make_order, erc20_deployer):
    fresh = erc20_deployer.deploy("Fresh", "FRESH", 18)
    order = make_order(sell_token=fresh.address, buy_amount=1)
    with boa.reverts(custom_err("OrderNotValid(string)", "NotAllowed")):
        _validate(harness, adapter, order)


def test_unsellable_token_not_allowed(harness, adapter, make_order, sell_token):
    order = make_order()
    harness.set_sellable(sell_token.address, False)
    with boa.reverts(custom_err("OrderNotValid(string)", "NotAllowed")):
        _validate(harness, adapter, order)


def test_expired_window_not_allowed(harness, adapter, make_order):
    order = make_order()
    boa.env.time_travel(seconds=harness.frame_end() - boa.env.evm.vm.state.timestamp)
    with boa.reverts(custom_err("OrderNotValid(string)", "NotAllowed")):
        _validate(harness, adapter, order)


def test_drained_lot_zero_balance(harness, adapter, make_order, sell_token):
    order = make_order()
    with boa.env.prank(harness.address):
        sell_token.transfer(boa.env.generate_address("sink"), LOT_AMOUNT)
    with boa.reverts(custom_err("OrderNotValid(string)", "ZeroBalance")):
        _validate(harness, adapter, order)


@pytest.mark.parametrize("sell_amount", [0, LOT_AMOUNT + 1])
def test_sell_amount_bounds_invalid(harness, adapter, make_order, sell_amount):
    order = make_order(sell_amount=sell_amount, buy_amount=1)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadSellAmount")):
        _validate(harness, adapter, order)


def test_valid_to_bounds_invalid(harness, adapter, make_order):
    now = boa.env.evm.vm.state.timestamp
    with boa.reverts(custom_err("OrderNotValid(string)", "BadValidTo")):
        _validate(harness, adapter, make_order(valid_to=now - 1))
    with boa.reverts(custom_err("OrderNotValid(string)", "BadValidTo")):
        _validate(harness, adapter, make_order(valid_to=harness.frame_end() + 1))


def test_underpriced_order_invalid(harness, adapter, make_order, sell_token):
    quote = harness.quote(sell_token.address, LOT_AMOUNT)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadBuyAmount")):
        _validate(harness, adapter, make_order(buy_amount=quote - 1))
    # Overpriced orders are always fine for the receiver.
    assert _validate(harness, adapter, make_order(buy_amount=quote + 1)) == ERC1271_MAGIC_VALUE


def test_quote_is_live_against_lot_snapshot(harness, adapter, make_order, sell_token):
    # A donation above the snapshot never lowers the required unit price:
    # sell amounts stay bounded by initial_amount, quotes by the live curve.
    sell_token._mint_for_testing(harness.address, LOT_AMOUNT)
    order = make_order()
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE
    too_big = make_order(sell_amount=LOT_AMOUNT + 1, buy_amount=MAX_UINT256)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadSellAmount")):
        _validate(harness, adapter, too_big)


# Read-only reentrancy: nothing about signed orders is readable mid-take


LOCK_PROBE_TAKER = """
# pragma version 0.5.0b1

from ethereum.ercs import IERC20

interface Auction:
    def want() -> address: view
    def take(_from: address, maxAmount: uint256, takerReceiver: address, data: Bytes[32]) -> uint256: nonpayable

check_order_call_succeeded: public(bool)
signature_call_succeeded: public(bool)


@external
def run(_auction: address, _token: address, _amount: uint256):
    # Non-empty data opts into the taker callback.
    extcall Auction(_auction).take(_token, _amount, self, b"\\x01")


@external
def auctionTakeCallback(
    _from: address, _sender: address, _amount_taken: uint256, _amount_needed: uint256, _data: Bytes[32]
):
    want: address = staticcall Auction(msg.sender).want()
    # Both the economic check and the ERC-1271 entry sit behind the lock that
    # take() holds: a view only reads the lock, but the lock is set right now.
    self.check_order_call_succeeded = raw_call(
        msg.sender,
        abi_encode(
            _from, want, empty(address), convert(1, uint256), max_value(uint256), block.timestamp,
            method_id=method_id("check_order(address,address,address,uint256,uint256,uint256)"),
        ),
        max_outsize=0,
        revert_on_failure=False,
        is_static_call=True,
    )
    self.signature_call_succeeded = raw_call(
        msg.sender,
        abi_encode(empty(bytes32), b"", method_id=method_id("isValidSignature(bytes32,bytes)")),
        max_outsize=0,
        revert_on_failure=False,
        is_static_call=True,
    )
    assert extcall IERC20(want).approve(msg.sender, _amount_needed)
"""


def test_check_order_and_erc1271_are_locked_during_a_take_callback(
    harness, sell_token, want, lot
):
    taker = boa.loads(LOCK_PROBE_TAKER, name="LockProbeTaker")
    amount = LOT_AMOUNT // 2
    want._mint_for_testing(taker.address, harness.getAmountNeeded(sell_token.address, amount))

    taker.run(harness.address, sell_token.address, amount)

    # The adapter's staticcall back into check_order from isValidSignature
    # works because views only read the lock (routing tests above); inside a
    # native take callback the lock is set and both entries are rejected.
    assert not taker.check_order_call_succeeded()
    assert not taker.signature_call_succeeded()
    assert sell_token.balanceOf(taker) == amount


# Publisher helper: order + signature for the burner


def test_order_for_builds_the_publishable_pair(
    harness, adapter, sell_token, want, proceeds_receiver, lot, relayer
):
    order, signature = adapter.order_for(harness.address, sell_token.address)
    order = tuple(order)
    assert order[0] == sell_token.address
    assert order[1] == want.address
    assert order[2] == proceeds_receiver
    assert order[3] == LOT_AMOUNT
    assert order[4] == harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    assert order[5] == harness.frame_end()
    assert bytes(order[6]) == APP_DATA
    assert order[7] == 0
    assert bytes(order[8]) == SELL_KIND
    assert order[9]
    assert bytes(order[10]) == ERC20_BALANCE
    assert bytes(order[11]) == ERC20_BALANCE
    # The signature is the documented template and validates end-to-end.
    assert bytes(signature) == _signature(adapter, order)
    assert harness.isValidSignature(_order_digest(order, DOMAIN_SEPARATOR), signature) == ERC1271_MAGIC_VALUE

    # A partial amount is priced proportionally and validates as well.
    partial, partial_signature = adapter.order_for(
        harness.address, sell_token.address, LOT_AMOUNT // 4
    )
    assert partial[3] == LOT_AMOUNT // 4
    assert partial[4] == harness.getAmountNeeded(sell_token.address, LOT_AMOUNT // 4)
    assert harness.isValidSignature(
        _order_digest(tuple(partial), DOMAIN_SEPARATOR), partial_signature
    ) == ERC1271_MAGIC_VALUE


def test_order_for_stays_valid_as_the_curve_decays(harness, adapter, sell_token, lot):
    # A published order is a standing ask: once the curve falls below it, it
    # is still accepted (overpriced for us is fine), so publishers re-post
    # lower rather than cancel.
    # Re-open the frame at the current block so the curve starts from the top.
    now = boa.env.evm.vm.state.timestamp
    harness.set_frame(now, harness.frame_end())
    order, signature = adapter.order_for(harness.address, sell_token.address)
    boa.env.time_travel(seconds=10 * STEP_DURATION)
    assert harness.quote(sell_token.address, LOT_AMOUNT) < order[4]
    assert harness.isValidSignature(_order_digest(tuple(order), DOMAIN_SEPARATOR), signature) == ERC1271_MAGIC_VALUE


def test_order_for_rejects_empty_lots(harness, adapter, sell_token, erc20_deployer):
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, sell_token.address)
    fresh = erc20_deployer.deploy("Fresh", "FRESH", 18)
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, fresh.address)
