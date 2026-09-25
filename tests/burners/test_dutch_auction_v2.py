from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from scripts import dutch_auction_curve as curve
from tests.burners.conftest import custom_err
from tests.conftest import ETH_ADDRESS, WEEK, ZERO_ADDRESS, Epoch

WAD = 10**18
START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
APP_DATA = bytes.fromhex("058315b749613051abcbf50cf2d605b4fa4a41554ec35d73fd058fc530da559f")
DOMAIN_SEPARATOR = keccak(b"test GPv2 settlement domain")
MAX_UINT256 = 2**256 - 1

SELL_KIND = keccak(text="sell")
ERC20_BALANCE = keccak(text="erc20")
ERC1271_MAGIC_VALUE = bytes.fromhex("1626ba7e")
ERC1271_INVALID = bytes.fromhex("ffffffff")

ERC165_INTERFACE = bytes.fromhex("01ffc9a7")
BURNER_INTERFACE = bytes.fromhex("a3b5e311")

# IAdapterRegistry.AdapterConfig tuple fields.
CONFIG_EXECUTOR = 0
CONFIG_ACTIVE = 1

# _lot_with_bounds tuple: window(lots(token).staged_at) plus the snapshot.
LOT_START = 0
LOT_END = 1
LOT_INITIAL_AMOUNT = 2

# GPv2Order.Data tuple fields.
ORDER_SELL_TOKEN = 0
ORDER_BUY_TOKEN = 1
ORDER_RECEIVER = 2
ORDER_SELL_AMOUNT = 3
ORDER_BUY_AMOUNT = 4
ORDER_VALID_TO = 5
ORDER_APP_DATA = 6
ORDER_FEE_AMOUNT = 7
ORDER_KIND = 8
ORDER_PARTIALLY_FILLABLE = 9
ORDER_SELL_BALANCE = 10
ORDER_BUY_BALANCE = 11

EXCHANGE_DURATION = 24 * 60 * 60
# The curve decays over the EXCHANGE frame's last active second in whole
# steps: 1439 sixty-second steps for a one-day frame.
DECAY_STEPS = curve.decay_steps(EXCHANGE_DURATION, STEP_DURATION)


@dataclass(frozen=True)
class AuctionDeployment:
    owner: str
    emergency_owner: str
    keeper: str
    buyer: str
    taker_receiver: str
    sink: str
    relayer: str
    target: Any
    sell_token: Any
    second_token: Any
    no_return_token: Any
    problem_token: Any
    weth: Any
    fee_collector: Any
    settlement: Any
    registry: Any
    cow_adapter: Any
    burner: Any


def _timestamp() -> int:
    return boa.env.evm.vm.state.timestamp


def _move_to_epoch(fee_collector: Any, epoch: Epoch) -> int:
    start, end = fee_collector.epoch_time_frame(epoch)
    target = (start + end) // 2
    while target <= _timestamp():
        target += WEEK
    boa.env.time_travel(seconds=target - _timestamp())
    assert fee_collector.epoch() == epoch
    return target


def _move_to_timestamp(timestamp: int) -> None:
    assert timestamp >= _timestamp()
    boa.env.time_travel(seconds=timestamp - _timestamp())


def _lot_with_bounds(deployment: Any, token: Any) -> tuple:
    """(start, end, initial_amount): the lot's window plus its snapshot;
    all zeros for a never-staged token."""
    start, end = deployment.burner.window(token)
    return (start, end, deployment.burner.lots(token).initial_amount)


def _exchange_start(deployment: Any, timestamp: int | None = None) -> int:
    """Start of the EXCHANGE frame of the week containing `timestamp` (now by
    default): the window a lot staged at that time trades in."""
    start, _ = deployment.fee_collector.epoch_time_frame(
        Epoch.EXCHANGE, _timestamp() if timestamp is None else timestamp
    )
    return start


def _address_bytes(address: Any) -> bytes:
    return bytes.fromhex(str(address)[2:])


def _stage(
    deployment: AuctionDeployment,
    token: Any,
    amount: int,
    *,
    receiver: str | None = None,
) -> tuple[int, int]:
    if deployment.fee_collector.epoch() != Epoch.COLLECT:
        _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    receiver = receiver or deployment.keeper
    token._mint_for_testing(deployment.fee_collector, amount)
    fee = amount * deployment.fee_collector.fee(Epoch.COLLECT) // WAD
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect([token.address], receiver)
        if _cow_active(deployment):
            # The keeper's post-collect step: staging grants nothing, the
            # permissionless sync gives the relayer its allowance.
            deployment.burner.sync_executor_approvals(deployment.relayer, [token.address])
    return amount - fee, fee


def _cow_active(deployment: AuctionDeployment) -> bool:
    """The single CoW switch: the CowAdapter's activation flag in the registry."""
    return deployment.registry.get_adapter(deployment.cow_adapter)[CONFIG_ACTIVE]


def _activate_lot(deployment: AuctionDeployment, token: Any, amount: int) -> tuple[Any, int]:
    staged, _ = _stage(deployment, token, amount)
    lot = _lot_with_bounds(deployment, token)
    _move_to_timestamp(lot[LOT_START])
    assert deployment.fee_collector.epoch() == Epoch.EXCHANGE
    return lot, staged


def _configure_and_enable_cow(deployment: AuctionDeployment) -> None:
    """Switch the CoW rail on: the CowAdapter is a regular registry adapter, so
    the owner activates it in the registry (the fixture only registered it).
    The burner holds no adapter state of its own."""
    with boa.env.prank(deployment.owner):
        deployment.registry.activate_adapter(deployment.cow_adapter)
    assert _cow_active(deployment)
    assert deployment.registry.is_executor_active(deployment.relayer)


def _gpv2_order_digest(order: Any, domain_separator: bytes) -> bytes:
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


# Bit-for-bit mirror of auction_math.total_price (scripts/dutch_auction_curve).
def _reference_total(
    lot: Any, timestamp: int, start_total: int = START_TOTAL, floor_total: int = FLOOR_TOTAL
) -> int:
    log_start, log_drop = curve.curve_logs(start_total, floor_total)
    return curve.total_price(
        start_total,
        floor_total,
        log_start,
        log_drop,
        DECAY_STEPS,
        timestamp - lot[LOT_START],
        STEP_DURATION,
    )


def _quote_from_total(total: int, amount: int, initial_amount: int) -> int:
    return (total * amount + initial_amount - 1) // initial_amount


def _function_signatures(contract: Any) -> set[str]:
    signatures: set[str] = set()
    for entry in contract.abi:
        if entry.get("type") != "function":
            continue
        inputs = ",".join(item["type"] for item in entry["inputs"])
        signatures.add(f"{entry['name']}({inputs})")
    return signatures


def _event_name(log: Any) -> str:
    event_type = getattr(log, "event_type", None)
    return event_type.name if event_type is not None else type(log).__name__


def _encode_erc1271_signature(order: Any, adapter: Any) -> bytes:
    """The eip1271 signature a publisher posts: adapter prefix ++ bare order."""
    order_type = (
        "(address,address,address,uint256,uint256,uint32,bytes32,uint256,"
        "bytes32,bool,bytes32,bytes32)"
    )
    normalized_order = (str(order[0]), str(order[1]), str(order[2]), *order[3:])
    return _address_bytes(adapter.address) + encode([order_type], [normalized_order])


def _cow_order(deployment: AuctionDeployment, token: Any, **overrides: Any) -> tuple:
    """A CoW order for the token's live lot as a publisher would build it: sell
    everything available at the live quote, proceeds to the FeeCollector,
    valid until the lot window ends."""
    available = deployment.burner.available(token)
    lot = _lot_with_bounds(deployment, token)
    fields = {
        "sell_token": token.address,
        "buy_token": deployment.target.address,
        "receiver": deployment.fee_collector.address,
        "sell_amount": available,
        "buy_amount": deployment.burner.getAmountNeeded(token, available) if available else 0,
        "valid_to": lot[LOT_END],
        "app_data": APP_DATA,
        "fee_amount": 0,
        "kind": SELL_KIND,
        "partially_fillable": True,
        "sell_balance": ERC20_BALANCE,
        "buy_balance": ERC20_BALANCE,
    }
    fields.update(overrides)
    return tuple(fields.values())


def _take_calldata(token: Any, max_amount: int, receiver: Any) -> bytes:
    return keccak(text="take(address,uint256,address,bytes)")[:4] + encode(
        ["address", "uint256", "address", "bytes"],
        [str(token.address), max_amount, str(receiver), b""],
    )


@pytest.fixture(autouse=True)
def isolate_chain():
    with boa.env.anchor():
        yield


@pytest.fixture(scope="module")
def burner_deployer():
    return boa.load_partial("contracts/burners/DutchAuctionBurner.vy")


@pytest.fixture
def deployment(burner_deployer: Any) -> AuctionDeployment:
    owner = boa.env.generate_address("owner")
    emergency_owner = boa.env.generate_address("emergency_owner")
    keeper = boa.env.generate_address("keeper")
    buyer = boa.env.generate_address("buyer")
    taker_receiver = boa.env.generate_address("taker_receiver")
    sink = boa.env.generate_address("sink")
    relayer = boa.env.generate_address("relayer")

    erc20 = boa.load_partial("contracts/testing/ERC20Mock.vy")
    no_return_erc20 = boa.load_partial("contracts/testing/ERC20MockNoReturn.vy")
    target = erc20.deploy("Curve Stablecoin", "crvUSD", 18)
    sell_token = erc20.deploy("Sell Token", "SELL", 18)
    second_token = erc20.deploy("Second Token", "SECOND", 6)
    no_return_token = no_return_erc20.deploy("No Return", "NORET", 8)
    weth = boa.load("contracts/testing/WETH.vy")
    problem_token = boa.load(
        "contracts/testing/dutch_auction/ProblemERC20.vy", "Problem Token", "PROBLEM", 18
    )
    fee_collector = boa.load("contracts/FeeCollector.vy", target, weth, owner, emergency_owner)
    settlement = boa.load(
        "contracts/testing/dutch_auction/SettlementMock.vy", DOMAIN_SEPARATOR, relayer
    )
    registry = boa.load("contracts/burners/adapters/AdapterRegistry.vy", fee_collector.address)
    cow_adapter = boa.load("contracts/burners/adapters/cow/CowAdapter.vy", settlement, APP_DATA)

    burner = burner_deployer.deploy(
        fee_collector,
        START_TOTAL,
        FLOOR_TOTAL,
        STEP_DURATION,
        registry,
    )
    with boa.env.prank(owner):
        fee_collector.set_burner(burner)
        fee_collector.set_killed([(ZERO_ADDRESS, 0)])
        # CoW is a regular registry adapter: adapter = CowAdapter, executor =
        # vault relayer. Registered but inactive; tests opt in through the
        # registry's owner-only activation via _configure_and_enable_cow.
        registry.set_adapter(cow_adapter, relayer)

    return AuctionDeployment(
        owner,
        emergency_owner,
        keeper,
        buyer,
        taker_receiver,
        sink,
        relayer,
        target,
        sell_token,
        second_token,
        no_return_token,
        problem_token,
        weth,
        fee_collector,
        settlement,
        registry,
        cow_adapter,
        burner,
    )


def test_constructor_and_fixed_interfaces(deployment: AuctionDeployment):
    burner = deployment.burner

    assert burner.VERSION() == "DutchAuction"
    assert burner.want() == deployment.target.address
    assert burner.fee_collector() == deployment.fee_collector.address
    assert burner.registry() == deployment.registry.address
    assert burner.supportsInterface(ERC165_INTERFACE)
    assert burner.supportsInterface(BURNER_INTERFACE)
    # The adapter dispatcher is always live, so ERC-1271 is claimed unconditionally.
    assert burner.supportsInterface(ERC1271_MAGIC_VALUE)

    # CoW is a registry adapter: registered with the relayer as executor, not
    # yet active — the burner holds no adapter state and only reads the registry.
    config = deployment.registry.get_adapter(deployment.cow_adapter)
    assert config[CONFIG_EXECUTOR] == deployment.relayer
    assert config[CONFIG_ACTIVE] is False
    assert not deployment.registry.is_executor_active(deployment.relayer)

    signatures = _function_signatures(burner)
    assert {
        "want()",
        "available(address)",
        "price(address)",
        "getAmountNeeded(address,uint256)",
        "take(address,uint256,address,bytes)",
        "take_with_limits(address,uint256,uint256,uint256,address,uint256,bytes)",
        "isValidSignature(bytes32,bytes)",
        "check_order(address,address,address,uint256,uint256,uint256)",
        "sync_executor_approvals(address,address[])",
        "registry()",
        "lots(address)",
        "window(address)",
        "window(address,uint256)",
        "auction_length()",
        "receiver()",
        "recover(address[])",
        "push_target()",
    } <= signatures


def test_yearn_auction_abi_is_exact(deployment: AuctionDeployment):
    """ABI-conformance for the Yearn-compatible surface (IYearnAuction.vyi).

    Vyper default arguments export every Yearn overload: the timestamped
    price/getAmountNeeded quotes, the single-argument getAmountNeeded, and
    the shortened take forms.
    """
    functions = [
        item
        for item in deployment.burner.abi
        if item["type"] == "function"
        and item["name"]
        in {
            "want",
            "available",
            "price",
            "getAmountNeeded",
            "take",
            "isActive",
            "auctionLength",
            "auctions",
        }
    ]
    signatures = {
        (
            item["name"],
            tuple((arg["name"], arg["type"]) for arg in item["inputs"]),
            tuple(arg["type"] for arg in item["outputs"]),
            item["stateMutability"],
        )
        for item in functions
    }
    assert len(functions) == len(signatures)
    assert signatures == {
        ("want", (), ("address",), "view"),
        ("isActive", (("_from", "address"),), ("bool",), "view"),
        ("auctionLength", (), ("uint256",), "view"),
        ("auctions", (("_from", "address"),), ("tuple",), "view"),
        ("available", (("_from", "address"),), ("uint256",), "view"),
        ("price", (("_from", "address"),), ("uint256",), "view"),
        ("price", (("_from", "address"), ("_ts", "uint256")), ("uint256",), "view"),
        ("getAmountNeeded", (("_from", "address"),), ("uint256",), "view"),
        (
            "getAmountNeeded",
            (("_from", "address"), ("amountToTake", "uint256")),
            ("uint256",),
            "view",
        ),
        (
            "getAmountNeeded",
            (("_from", "address"), ("amountToTake", "uint256"), ("_ts", "uint256")),
            ("uint256",),
            "view",
        ),
        ("take", (("_from", "address"),), ("uint256",), "nonpayable"),
        ("take", (("_from", "address"), ("maxAmount", "uint256")), ("uint256",), "nonpayable"),
        (
            "take",
            (("_from", "address"), ("maxAmount", "uint256"), ("takerReceiver", "address")),
            ("uint256",),
            "nonpayable",
        ),
        (
            "take",
            (
                ("_from", "address"),
                ("maxAmount", "uint256"),
                ("takerReceiver", "address"),
                ("data", "bytes"),
            ),
            ("uint256",),
            "nonpayable",
        ),
    }


@pytest.mark.parametrize(
    "start_total,floor_total,step_duration,error",
    [
        (0, FLOOR_TOTAL, STEP_DURATION, "BadStartTotal()"),
        (2**255, FLOOR_TOTAL, STEP_DURATION, "BadStartTotal()"),
        (START_TOTAL, 0, STEP_DURATION, "BadFloor()"),
        (START_TOTAL, START_TOTAL + 1, STEP_DURATION, "BadFloor()"),
        (START_TOTAL, FLOOR_TOTAL, 0, "ZeroStep()"),
        # A step longer than the EXCHANGE frame leaves no step to decay on.
        (START_TOTAL, FLOOR_TOTAL, EXCHANGE_DURATION, "StepExceedsAuction()"),
    ],
)
def test_constructor_rejects_invalid_curve_parameters(
    burner_deployer: Any,
    deployment: AuctionDeployment,
    start_total: int,
    floor_total: int,
    step_duration: int,
    error: str,
):
    with boa.reverts(custom_err(error)):
        burner_deployer.deploy(
            deployment.fee_collector,
            start_total,
            floor_total,
            step_duration,
            deployment.registry,
        )


def test_window_defaults_to_the_current_lot(deployment: AuctionDeployment):
    burner = deployment.burner
    assert tuple(burner.window(deployment.sell_token)) == (0, 0)
    lot, _ = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    staged_at = burner.lots(deployment.sell_token).staged_at
    assert tuple(burner.window(deployment.sell_token)) == (lot[LOT_START], lot[LOT_END])
    assert tuple(burner.window(deployment.sell_token, staged_at)) == (lot[LOT_START], lot[LOT_END])
    # A hypothetical staging a week later lands in the next window.
    assert tuple(burner.window(deployment.sell_token, staged_at + WEEK)) == (
        lot[LOT_START] + WEEK,
        lot[LOT_END] + WEEK,
    )


def test_constructor_prepares_curve_for_exchange_frame(deployment: AuctionDeployment):
    """The curve is fully determined by start, floor, step, and the EXCHANGE
    frame (the public parameters reproduce every quote off-chain), and the
    lot sits exactly at the floor from the frame's last active step."""
    burner = deployment.burner
    assert burner.auction_length() == EXCHANGE_DURATION

    lot, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    assert burner.getAmountNeeded(deployment.sell_token, staged, lot[LOT_START]) == START_TOTAL
    floor_from = lot[LOT_START] + DECAY_STEPS * STEP_DURATION
    assert (
        burner.getAmountNeeded(deployment.sell_token, staged, floor_from - STEP_DURATION)
        > FLOOR_TOTAL
    )
    assert burner.getAmountNeeded(deployment.sell_token, staged, floor_from) == FLOOR_TOTAL
    assert burner.getAmountNeeded(deployment.sell_token, staged, lot[LOT_END] - 1) == FLOOR_TOTAL
    assert burner.getAmountNeeded(deployment.sell_token, staged, lot[LOT_END]) == 0


def test_only_fee_collector_can_burn_and_fee_collector_rejects_target(
    deployment: AuctionDeployment,
):
    with boa.env.prank(deployment.keeper), boa.reverts(custom_err("OnlyFeeCollector()")):
        deployment.burner.burn([deployment.sell_token.address], deployment.keeper)

    # The target is killed for COLLECT in the FeeCollector, so its custody
    # transfer fails before the burner's own staging check (WantNotSellable,
    # covered at the core level) is reached.
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    deployment.target._mint_for_testing(deployment.fee_collector, WAD)
    with boa.env.prank(deployment.fee_collector.address), boa.reverts("Killed coin"):
        deployment.burner.burn([deployment.target.address], deployment.keeper)

    assert _lot_with_bounds(deployment, deployment.target)[LOT_INITIAL_AMOUNT] == 0


def test_collect_pays_fee_moves_custody_and_snapshots_lot(deployment: AuctionDeployment):
    amount = 1_000 * WAD
    staged, fee = _stage(deployment, deployment.sell_token, amount)
    lot_synced = next(
        log for log in deployment.fee_collector.get_logs() if _event_name(log) == "LotStaged"
    )
    lot = _lot_with_bounds(deployment, deployment.sell_token)

    assert deployment.sell_token.balanceOf(deployment.keeper) == fee
    assert deployment.sell_token.balanceOf(deployment.fee_collector) == 0
    assert deployment.sell_token.balanceOf(deployment.burner) == staged
    assert lot[LOT_INITIAL_AMOUNT] == staged
    assert lot[LOT_START] < lot[LOT_END]
    # Epochs are identified by their own window's start timestamp; staging in
    # COLLECT tags the lot with the SAME week's upcoming window — the frame
    # lookup anchors to the week containing the timestamp, so at staging time
    # the epoch is a strictly future timestamp.
    assert lot[LOT_START] == _exchange_start(deployment)
    assert _timestamp() < lot[LOT_START]

    assert lot_synced.address == deployment.burner.address
    assert lot_synced.token == deployment.sell_token.address
    assert lot_synced.start == lot[LOT_START]
    assert lot_synced.end == lot[LOT_END]
    assert lot_synced.initial_amount == lot[LOT_INITIAL_AMOUNT]
    assert lot_synced.start_total == START_TOTAL
    assert lot_synced.floor_total == FLOOR_TOTAL
    assert lot_synced.start == lot[LOT_START]
    assert lot_synced.end == lot[LOT_END]


def test_repeated_collect_updates_snapshot_without_charging_old_inventory(
    deployment: AuctionDeployment,
):
    first_staged, first_fee = _stage(deployment, deployment.sell_token, 1_000 * WAD)
    first_lot = _lot_with_bounds(deployment, deployment.sell_token)

    second_amount = 250 * WAD
    deployment.sell_token._mint_for_testing(deployment.fee_collector, second_amount)
    fee_rate = deployment.fee_collector.fee(Epoch.COLLECT)
    second_fee = second_amount * fee_rate // WAD
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect([deployment.sell_token.address], deployment.keeper)

    second_lot = _lot_with_bounds(deployment, deployment.sell_token)
    assert deployment.sell_token.balanceOf(deployment.keeper) == first_fee + second_fee
    assert second_lot[LOT_INITIAL_AMOUNT] == first_staged + second_amount - second_fee
    assert second_lot[LOT_START] == first_lot[LOT_START]


def test_collect_outside_collect_epoch_reverts_without_state_changes(
    deployment: AuctionDeployment,
):
    _move_to_epoch(deployment.fee_collector, Epoch.EXCHANGE)
    deployment.sell_token._mint_for_testing(deployment.fee_collector, WAD)

    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.fee_collector.collect([deployment.sell_token.address], deployment.keeper)

    assert deployment.sell_token.balanceOf(deployment.fee_collector) == WAD
    assert _lot_with_bounds(deployment, deployment.sell_token)[LOT_INITIAL_AMOUNT] == 0


def test_weekly_rollover_resnapshots_unsold_inventory_and_new_receipts(
    deployment: AuctionDeployment,
):
    first_staged, _ = _stage(deployment, deployment.sell_token, 1_000 * WAD)
    first_lot = _lot_with_bounds(deployment, deployment.sell_token)
    _move_to_timestamp(first_lot[LOT_START])

    amount_taken = first_staged // 4
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, amount_taken)
    deployment.target._mint_for_testing(deployment.buyer, payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        deployment.burner.take(deployment.sell_token, amount_taken, deployment.taker_receiver, b"")

    unsold = first_staged - amount_taken
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    new_staged, _ = _stage(deployment, deployment.sell_token, 200 * WAD)
    second_lot = _lot_with_bounds(deployment, deployment.sell_token)

    assert second_lot[LOT_START] == first_lot[LOT_START] + WEEK
    assert second_lot[LOT_INITIAL_AMOUNT] == unsold + new_staged


def test_unsynced_token_is_inactive_in_a_new_week(deployment: AuctionDeployment):
    first_lot, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    assert deployment.burner.available(deployment.sell_token) == staged

    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    _stage(deployment, deployment.second_token, 100 * 10**6)
    second_lot = _lot_with_bounds(deployment, deployment.second_token)
    _move_to_timestamp(second_lot[LOT_START])

    assert deployment.burner.available(deployment.sell_token) == 0
    assert deployment.burner.price(deployment.sell_token) == 0
    assert deployment.burner.getAmountNeeded(deployment.sell_token, 1) == 0


def test_price_boundaries_geometric_checkpoints_and_floor(deployment: AuctionDeployment):
    lot, staged = _activate_lot(deployment, deployment.sell_token, 10_000 * WAD)

    assert deployment.burner.getAmountNeeded(deployment.sell_token, staged) == START_TOTAL
    assert (
        deployment.burner.price(deployment.sell_token) == (START_TOTAL * WAD + staged - 1) // staged
    )

    previous = START_TOTAL
    for fraction, expected in [
        (Decimal("0.2"), 10_000 * WAD),
        (Decimal("0.4"), 1_000 * WAD),
        (Decimal("0.6"), 100 * WAD),
        (Decimal("0.8"), 10 * WAD),
    ]:
        timestamp = lot[LOT_START] + int(Decimal(lot[LOT_END] - lot[LOT_START]) * fraction)
        _move_to_timestamp(timestamp)
        quote = deployment.burner.getAmountNeeded(deployment.sell_token, staged)
        assert quote <= previous
        # Last-active-step calibration reaches each economic decade within one step.
        assert quote == pytest.approx(expected, rel=0.0081)
        previous = quote

    _move_to_timestamp(lot[LOT_END] - 1)
    assert deployment.burner.getAmountNeeded(deployment.sell_token, staged) == FLOOR_TOTAL
    _move_to_timestamp(lot[LOT_END])
    assert deployment.burner.available(deployment.sell_token) == 0
    assert deployment.burner.price(deployment.sell_token) == 0
    assert deployment.burner.getAmountNeeded(deployment.sell_token, staged) == 0
    with boa.env.prank(deployment.buyer), boa.reverts():
        deployment.burner.take(deployment.sell_token, staged, deployment.taker_receiver, b"")


@given(
    elapsed=st.integers(min_value=0, max_value=EXCHANGE_DURATION - 1),
    amount_seed=st.integers(min_value=0, max_value=10_000 * WAD),
)
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_price_matches_reference_integer_model(
    deployment: AuctionDeployment,
    elapsed: int,
    amount_seed: int,
):
    with boa.env.anchor():
        lot, staged = _activate_lot(deployment, deployment.sell_token, 10_000 * WAD)
        amount = 1 + amount_seed % staged
        timestamp = lot[LOT_START] + elapsed
        _move_to_timestamp(timestamp)
        actual = deployment.burner.getAmountNeeded(deployment.sell_token, amount)
        reference_total = _reference_total(lot, timestamp)
        expected = _quote_from_total(reference_total, amount, staged)
        assert actual == expected


def test_payment_is_proportional_rounded_up_and_unit_curve_survives_partial_fill(
    deployment: AuctionDeployment,
):
    lot, staged = _activate_lot(deployment, deployment.sell_token, 3 * WAD + 1)
    _move_to_timestamp(lot[LOT_START] + 12_345)

    total = deployment.burner.getAmountNeeded(deployment.sell_token, staged)
    amount = staged // 3
    quote = deployment.burner.getAmountNeeded(deployment.sell_token, amount)
    assert quote == (total * amount + staged - 1) // staged

    unit_price_before = deployment.burner.price(deployment.sell_token)
    deployment.target._mint_for_testing(deployment.buyer, quote)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, quote)
        deployment.burner.take(deployment.sell_token, amount, deployment.taker_receiver, b"")

    assert deployment.burner.price(deployment.sell_token) == unit_price_before
    assert _lot_with_bounds(deployment, deployment.sell_token)[LOT_INITIAL_AMOUNT] == staged


def test_large_balance_quote_does_not_overflow(deployment: AuctionDeployment):
    # The checked-product math supports lots up to start_total * amount fitting
    # uint256 — far beyond any real balance under the deployment assumption
    # that balances never approach 2**256.
    huge = 2**160
    lot, staged = _activate_lot(deployment, deployment.sell_token, huge)
    assert staged > 2**159
    assert deployment.burner.getAmountNeeded(deployment.sell_token, staged) == START_TOTAL
    assert deployment.burner.getAmountNeeded(deployment.sell_token, staged - 1) <= START_TOTAL
    assert deployment.burner.price(deployment.sell_token) > 0
    assert lot[LOT_INITIAL_AMOUNT] == staged


def test_donation_and_rebase_never_exceed_snapshot_or_reduce_unit_price(
    deployment: AuctionDeployment,
):
    lot, staged = _activate_lot(deployment, deployment.problem_token, 1_000 * WAD)
    unit_price = deployment.burner.price(deployment.problem_token)

    deployment.problem_token.mint(deployment.burner, 500 * WAD)
    assert deployment.burner.available(deployment.problem_token) == staged
    assert deployment.burner.price(deployment.problem_token) == unit_price

    amount = staged // 4
    payment = deployment.burner.getAmountNeeded(deployment.problem_token, amount)
    deployment.target._mint_for_testing(deployment.buyer, payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        deployment.burner.take(deployment.problem_token, amount, deployment.taker_receiver, b"")

    # The earlier donation backfills the partial fill: availability holds at
    # the snapshot cap while the balance covers it.
    remaining = staged - amount
    assert deployment.burner.available(deployment.problem_token) == staged

    deployment.problem_token.set_balance(deployment.burner, remaining // 2)
    assert deployment.burner.available(deployment.problem_token) == remaining // 2
    # A refill revives availability up to the snapshot, never above it, and
    # never moves the unit price pinned by initial_amount.
    deployment.problem_token.set_balance(deployment.burner, 10 * staged)
    assert deployment.burner.available(deployment.problem_token) == staged
    assert deployment.burner.price(deployment.problem_token) == unit_price
    assert (
        _lot_with_bounds(deployment, deployment.problem_token)[LOT_INITIAL_AMOUNT]
        == lot[LOT_INITIAL_AMOUNT]
    )


def test_yearn_views_mirror_the_lot(deployment: AuctionDeployment):
    """isActive/auctionLength/auctions follow Yearn's shapes on top of the lot:
    kicked is the window start, scaler is 1 (price() is already a 1e18 quote
    over raw amounts), initialAvailable the snapshot; a never-staged token
    reads as Yearn's unenabled auction."""
    burner = deployment.burner
    token = deployment.sell_token
    scaler = 1
    assert not burner.isActive(token)
    assert tuple(burner.auctions(token)) == (0, 0, 0)

    staged, _ = _stage(deployment, token, 100 * WAD)
    lot = _lot_with_bounds(deployment, token)
    # Staged in COLLECT: kicked names the upcoming window start, which still
    # lies in the future, and the auction is not active until it opens.
    assert lot[LOT_START] > _timestamp()
    assert not burner.isActive(token)
    assert tuple(burner.auctions(token)) == (lot[LOT_START], scaler, staged)

    _move_to_timestamp(lot[LOT_START])
    assert burner.auctionLength() == lot[LOT_END] - lot[LOT_START]
    assert burner.isActive(token)
    assert tuple(burner.auctions(token)) == (lot[LOT_START], scaler, staged)

    payment = burner.getAmountNeeded(token)
    deployment.target._mint_for_testing(deployment.buyer, payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(burner, payment)
        burner.take(token)
    # Drained: inactive like Yearn, while the record keeps the snapshot.
    assert not burner.isActive(token)
    assert tuple(burner.auctions(token)) == (lot[LOT_START], scaler, staged)

    # Yearn's auctions() tuple decodes as three static words.
    selector = keccak(text="auctions(address)")[:4]
    raw = boa.env.raw_call(burner.address, data=selector + encode(["address"], [token.address]))
    assert raw.output == encode(["uint64", "uint64", "uint128"], [lot[LOT_START], scaler, staged])


def test_timestamped_quote_overloads_match_time_travel(deployment: AuctionDeployment):
    """The `_ts` quote overloads evaluate the lot's window and curve at an
    arbitrary timestamp: zero outside the window, and inside it exactly what
    the plain views answer once the chain is there. Balance and FeeCollector
    state (kill masks, live epoch) are still read at the current block, so
    the projection is asked from inside the open window."""
    burner = deployment.burner
    token = deployment.sell_token
    lot, staged = _activate_lot(deployment, token, 100 * WAD)
    amount = staged // 3

    for outside in (lot[LOT_START] - 1, lot[LOT_END]):
        assert burner.price(token, outside) == 0
        assert burner.getAmountNeeded(token, amount, outside) == 0

    inside = lot[LOT_START] + 7 * STEP_DURATION + 5
    projected = (
        burner.price(token, inside),
        burner.getAmountNeeded(token, amount, inside),
    )
    assert projected[0] > 0
    assert projected[1] > 0
    _move_to_timestamp(inside)
    assert projected == (
        burner.price(token),
        burner.getAmountNeeded(token, amount),
    )


def test_direct_full_and_partial_take_pay_fee_collector_and_receiver(
    deployment: AuctionDeployment,
):
    _, staged = _activate_lot(deployment, deployment.sell_token, 1_000 * WAD)
    amount = staged // 3
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, amount)
    deployment.target._mint_for_testing(deployment.buyer, payment)

    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        amount_taken = deployment.burner.take(
            deployment.sell_token, amount, deployment.taker_receiver, b""
        )

    assert amount_taken == amount
    assert deployment.sell_token.balanceOf(deployment.taker_receiver) == amount
    assert deployment.target.balanceOf(deployment.fee_collector) == payment
    assert deployment.target.balanceOf(deployment.burner) == 0
    assert deployment.burner.available(deployment.sell_token) == staged - amount

    # Yearn's shortened overloads: getAmountNeeded(from) quotes everything
    # available and take(from) takes it all to the caller with no callback.
    second_payment = deployment.burner.getAmountNeeded(deployment.sell_token)
    assert second_payment == deployment.burner.getAmountNeeded(
        deployment.sell_token, staged - amount
    )
    deployment.target._mint_for_testing(deployment.buyer, second_payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, second_payment)
        second_taken = deployment.burner.take(deployment.sell_token)
    assert second_taken == staged - amount
    assert deployment.sell_token.balanceOf(deployment.buyer) == staged - amount
    assert deployment.burner.available(deployment.sell_token) == 0


def test_yearn_callback_pull_payment_happy_path(deployment: AuctionDeployment):
    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    amount = staged // 2
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, amount)
    taker = boa.load(
        "contracts/testing/dutch_auction/AuctionTakerMock.vy",
        deployment.burner,
        deployment.fee_collector,
        deployment.target,
    )
    deployment.target._mint_for_testing(taker, payment)
    taker.configure(4, False)  # PAY_PULL: approve the burner's allowance pull.

    callback_data = b"atomic unwind"
    amount_taken = taker.execute_take(deployment.sell_token, amount, taker.address, callback_data)

    assert amount_taken == amount
    assert taker.callback_count() == 1
    assert taker.callback_from() == deployment.sell_token.address
    assert taker.callback_sender() == taker.address
    assert taker.callback_amount_taken() == amount
    assert taker.callback_amount_needed() == payment
    assert taker.callback_data() == callback_data
    assert deployment.target.balanceOf(deployment.fee_collector) == payment
    assert deployment.target.balanceOf(deployment.burner) == 0
    assert deployment.sell_token.balanceOf(taker) == amount


@pytest.mark.parametrize("payment_mode", [1, 2, 3])
def test_callback_direct_transfers_never_count_as_payment(
    deployment: AuctionDeployment,
    payment_mode: int,
):
    """Paying by transfer to the collector or burner inside the callback must
    not settle the bill: only the caller's allowance does. Balance-delta
    crediting would let a callback route unrelated third-party inflows into
    its own payment."""
    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    amount = staged // 2
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, amount)
    taker = boa.load(
        "contracts/testing/dutch_auction/AuctionTakerMock.vy",
        deployment.burner,
        deployment.fee_collector,
        deployment.target,
    )
    deployment.target._mint_for_testing(taker, payment)
    taker.configure(payment_mode, False)

    with boa.reverts():
        taker.execute_take(deployment.sell_token, amount, taker.address, b"direct pay")

    assert deployment.sell_token.balanceOf(taker) == 0
    assert deployment.target.balanceOf(taker) == payment
    assert deployment.target.balanceOf(deployment.fee_collector) == 0
    assert deployment.burner.available(deployment.sell_token) == staged


def test_eoa_payer_calls_take_with_separate_callback_receiver(
    deployment: AuctionDeployment,
):
    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    amount = staged // 2
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, amount)
    taker_receiver = boa.load(
        "contracts/testing/dutch_auction/AuctionTakerMock.vy",
        deployment.burner,
        deployment.fee_collector,
        deployment.target,
    )
    taker_receiver.configure(0, False)
    deployment.target._mint_for_testing(deployment.buyer, payment)

    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        amount_taken = deployment.burner.take(
            deployment.sell_token,
            amount,
            taker_receiver.address,
            b"receiver callback",
        )

    assert amount_taken == amount
    assert taker_receiver.callback_count() == 1
    assert taker_receiver.callback_from() == deployment.sell_token.address
    assert taker_receiver.callback_sender() == deployment.buyer
    assert taker_receiver.callback_amount_taken() == amount
    assert taker_receiver.callback_amount_needed() == payment
    assert taker_receiver.callback_data() == b"receiver callback"
    assert deployment.sell_token.balanceOf(taker_receiver) == amount
    assert deployment.target.balanceOf(deployment.buyer) == 0
    assert deployment.target.balanceOf(taker_receiver) == 0
    assert deployment.target.balanceOf(deployment.fee_collector) == payment


def test_callback_underpayment_reverts_all_effects(deployment: AuctionDeployment):
    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    amount = staged // 2
    taker = boa.load(
        "contracts/testing/dutch_auction/AuctionTakerMock.vy",
        deployment.burner,
        deployment.fee_collector,
        deployment.target,
    )
    taker.configure(0, False)

    with boa.reverts():
        taker.execute_take(deployment.sell_token, amount, taker.address, b"underpay")

    assert deployment.sell_token.balanceOf(taker) == 0
    assert deployment.target.balanceOf(deployment.fee_collector) == 0
    assert deployment.burner.available(deployment.sell_token) == staged
    assert taker.callback_count() == 0


def test_large_callback_data_round_trips(deployment: AuctionDeployment):
    """take() forwards `data` unbounded (Bytes[INF]); 8192 bytes is the
    taker mock's own callback bound, exercised in full here."""
    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    amount = staged // 2
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, amount)
    taker = boa.load(
        "contracts/testing/dutch_auction/AuctionTakerMock.vy",
        deployment.burner,
        deployment.fee_collector,
        deployment.target,
    )
    deployment.target._mint_for_testing(taker, payment)
    taker.configure(4, False)

    callback_data = bytes(range(256)) * 32
    assert len(callback_data) == 8192
    amount_taken = taker.execute_take(deployment.sell_token, amount, taker.address, callback_data)

    assert amount_taken == amount
    assert taker.callback_data() == callback_data
    assert deployment.sell_token.balanceOf(taker) == amount
    assert deployment.target.balanceOf(deployment.fee_collector) == payment


def test_callback_reentrancy_reverts_without_double_sell(deployment: AuctionDeployment):
    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    amount = staged // 2
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, amount)
    taker = boa.load(
        "contracts/testing/dutch_auction/AuctionTakerMock.vy",
        deployment.burner,
        deployment.fee_collector,
        deployment.target,
    )
    deployment.target._mint_for_testing(taker, payment)
    taker.configure(1, True)

    with boa.reverts():
        taker.execute_take(deployment.sell_token, amount, taker.address, b"reenter")

    assert deployment.sell_token.balanceOf(taker) == 0
    assert deployment.burner.available(deployment.sell_token) == staged
    assert deployment.target.balanceOf(deployment.fee_collector) == 0


def test_take_rejects_zero_outside_epoch_and_killed_token(deployment: AuctionDeployment):
    assert deployment.burner.available(deployment.sell_token) == 0
    with boa.env.prank(deployment.buyer), boa.reverts():
        deployment.burner.take(deployment.sell_token, 1, deployment.taker_receiver, b"")

    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    with boa.env.prank(deployment.owner):
        deployment.fee_collector.set_killed([(deployment.sell_token.address, Epoch.EXCHANGE)])
    assert deployment.burner.available(deployment.sell_token) == 0
    with boa.env.prank(deployment.buyer), boa.reverts():
        deployment.burner.take(deployment.sell_token, staged, deployment.taker_receiver, b"")


def test_active_exact_quote_rejects_amount_above_available_and_take_zero(
    deployment: AuctionDeployment,
):
    lot, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    with boa.reverts(custom_err("AmountExceedsAvailable()")):
        deployment.burner.getAmountNeeded(deployment.sell_token, staged + 1)

    buyer_target_before = deployment.target.balanceOf(deployment.buyer)
    with boa.env.prank(deployment.buyer), boa.reverts(custom_err("NothingAvailable()")):
        deployment.burner.take(deployment.sell_token, 0, deployment.taker_receiver, b"")

    current_lot = _lot_with_bounds(deployment, deployment.sell_token)
    assert current_lot[LOT_START] == lot[LOT_START]
    assert deployment.burner.available(deployment.sell_token) == staged
    assert deployment.sell_token.balanceOf(deployment.taker_receiver) == 0
    assert deployment.target.balanceOf(deployment.buyer) == buyer_target_before
    assert deployment.target.balanceOf(deployment.fee_collector) == 0


def test_partial_fill_keeps_unit_price_while_quote_is_bounded_by_availability(
    deployment: AuctionDeployment,
):
    """After a partial take the lot's initial_amount still pins the unit price
    (a persistent partially fillable order keeps its signed total), while
    getAmountNeeded is bounded by the live remainder."""
    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    amount = staged // 2
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, amount)
    unit_price = deployment.burner.price(deployment.sell_token)
    deployment.target._mint_for_testing(deployment.buyer, payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        deployment.burner.take(deployment.sell_token, amount, deployment.taker_receiver, b"")

    remaining = staged - amount
    assert deployment.burner.available(deployment.sell_token) == remaining
    assert deployment.burner.lots(deployment.sell_token).initial_amount == staged
    assert deployment.burner.price(deployment.sell_token) == unit_price
    assert deployment.burner.getAmountNeeded(deployment.sell_token, remaining) == (
        _quote_from_total(deployment.burner.start_total(), remaining, staged)
    )
    with boa.reverts(custom_err("AmountExceedsAvailable()")):
        deployment.burner.getAmountNeeded(deployment.sell_token, staged)


def test_price_keeps_wad_precision_for_non_18_decimals(deployment: AuctionDeployment):
    """price() is raw want per 1e18 raw sell units regardless of decimals, and
    auctions().scaler is 1 to match: Yearn's amount * scaler * price / 1e18
    reproduces getAmountNeeded up to rounding."""
    token = deployment.second_token
    assert token.decimals() == 6
    _, staged = _activate_lot(deployment, token, 100 * 10**6)

    price = deployment.burner.price(token)
    assert price == _quote_from_total(START_TOTAL, WAD, staged)
    assert deployment.burner.auctions(token)[1] == 1
    amount = 7 * 10**6
    needed = deployment.burner.getAmountNeeded(token, amount)
    assert abs(amount * 1 * price // WAD - needed) <= 1


def test_take_with_limits_enforces_deadline_amount_and_payment(
    deployment: AuctionDeployment,
):
    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    amount = staged // 2
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, amount)
    deployment.target._mint_for_testing(deployment.buyer, 10 * payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, 10 * payment)

    valid = (
        deployment.sell_token,
        amount,
        amount,
        payment,
        deployment.taker_receiver,
        _timestamp() + 60,
        b"",
    )
    invalid_calls = [
        (*valid[:5], _timestamp() - 1, b""),
        (valid[0], amount, amount + 1, *valid[3:]),
        (valid[0], amount, valid[2], payment - 1, *valid[4:]),
    ]
    for call in invalid_calls:
        with boa.env.prank(deployment.buyer), boa.reverts():
            deployment.burner.take_with_limits(*call)
        assert deployment.burner.available(deployment.sell_token) == staged

    with boa.env.prank(deployment.buyer):
        amount_taken, paid = deployment.burner.take_with_limits(*valid)
    assert amount_taken == amount
    assert paid == payment


def test_deadline_capped_at_lot_end_rejects_execution_after_rollover(
    deployment: AuctionDeployment,
):
    """A deadline within the lot window replaces any lot identifier: exactly
    one window is active per timestamp, so the signed lot's end caps
    inclusion and a restaged lot's fresh curve can never fill an old
    transaction."""
    first_lot, _ = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    deadline = first_lot[LOT_END] - 1

    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    _stage(deployment, deployment.sell_token, 1 * WAD)
    second_lot = _lot_with_bounds(deployment, deployment.sell_token)
    _move_to_timestamp(second_lot[LOT_START])

    amount = deployment.burner.available(deployment.sell_token)
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, amount)
    deployment.target._mint_for_testing(deployment.buyer, payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        with boa.reverts(custom_err("Deadline()")):
            deployment.burner.take_with_limits(
                deployment.sell_token,
                amount,
                1,
                payment,
                deployment.taker_receiver,
                deadline,
                b"",
            )


def test_taken_event_captures_accounting_result(deployment: AuctionDeployment):
    lot, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    amount = staged // 2
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, amount)
    deployment.target._mint_for_testing(deployment.buyer, payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        deployment.burner.take(deployment.sell_token, amount, deployment.taker_receiver, b"")

    taken = next(log for log in deployment.burner.get_logs() if _event_name(log) == "Taken")
    assert taken.address == deployment.burner.address
    assert taken.token == deployment.sell_token.address
    assert taken.caller == deployment.buyer
    assert taken.receiver == deployment.taker_receiver
    assert taken.amount_out == amount
    assert taken.payment == payment
    assert taken.remaining_balance == staged - amount


def test_cow_adapter_activation_authority_and_registry_switch(deployment: AuctionDeployment):
    """Adapter management lives entirely in the registry, which reads its roles
    from the same FeeCollector as the burner: activation is owner-only, disabling
    also belongs to the emergency owner, and the burner tracks nothing locally."""
    registry = deployment.registry
    adapter = deployment.cow_adapter
    for account in (deployment.keeper, deployment.emergency_owner):
        with boa.env.prank(account), boa.reverts(custom_err("OnlyOwner()")):
            registry.activate_adapter(adapter)
    _configure_and_enable_cow(deployment)
    with boa.env.prank(deployment.owner), boa.reverts(custom_err("AlreadyActive()")):
        registry.activate_adapter(adapter)
    with boa.env.prank(deployment.keeper), boa.reverts(custom_err("OnlyOwnerOrEmergency()")):
        registry.disable_adapter(adapter)

    with boa.env.prank(deployment.emergency_owner):
        registry.disable_adapter(adapter)
    assert not _cow_active(deployment)
    assert not registry.is_executor_active(deployment.relayer)
    # ERC-1271 stays claimed for the signature router even with CoW disabled.
    assert deployment.burner.supportsInterface(ERC1271_MAGIC_VALUE)


def test_sync_grants_after_activation_and_restage_keeps_allowance(
    deployment: AuctionDeployment,
):
    staged, _ = _stage(deployment, deployment.sell_token, 100 * WAD)
    assert deployment.sell_token.allowance(deployment.burner, deployment.relayer) == 0

    _configure_and_enable_cow(deployment)
    # Neither staging nor activation approves anything: only the sync does.
    assert deployment.sell_token.allowance(deployment.burner, deployment.relayer) == 0
    with boa.env.prank(deployment.keeper):
        deployment.burner.sync_executor_approvals(
            deployment.relayer, [deployment.sell_token.address]
        )
    assert deployment.sell_token.allowance(deployment.burner, deployment.relayer) == MAX_UINT256

    # A restage in the same frame keeps the snapshot and the allowance.
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect([deployment.sell_token.address], deployment.keeper)
    assert _lot_with_bounds(deployment, deployment.sell_token)[LOT_INITIAL_AMOUNT] == staged
    assert deployment.sell_token.allowance(deployment.burner, deployment.relayer) == MAX_UINT256


def test_permissionless_sync_executor_approvals_follows_derived_state(
    deployment: AuctionDeployment,
):
    _configure_and_enable_cow(deployment)
    _stage(deployment, deployment.sell_token, 100 * WAD)
    relayer = deployment.relayer
    assert deployment.sell_token.allowance(deployment.burner, relayer) == MAX_UINT256

    # The payment token is approved like any other: no signed order sells it.
    with boa.env.prank(deployment.keeper):
        deployment.burner.sync_executor_approvals(relayer, [deployment.target.address])
    assert deployment.target.allowance(deployment.burner, relayer) == MAX_UINT256

    # While the executor is referenced, sync is a top-up path and stays at max.
    with boa.env.prank(deployment.keeper):
        deployment.burner.sync_executor_approvals(relayer, [deployment.sell_token.address])
    assert deployment.sell_token.allowance(deployment.burner, relayer) == MAX_UINT256

    with boa.env.prank(deployment.emergency_owner):
        deployment.registry.disable_adapter(deployment.cow_adapter)
    # Approvals are not touched by a registry disable; cleanup is the
    # permissionless sync, which now reads an inactive executor.
    assert deployment.sell_token.allowance(deployment.burner, relayer) == MAX_UINT256
    with boa.env.prank(deployment.keeper):
        deployment.burner.sync_executor_approvals(relayer, [deployment.sell_token.address])
    assert deployment.sell_token.allowance(deployment.burner, relayer) == 0

    # Reactivate: sync is the retry path that restores approvals without staging.
    with boa.env.prank(deployment.owner):
        deployment.registry.activate_adapter(deployment.cow_adapter)
    with boa.env.prank(deployment.keeper):
        deployment.burner.sync_executor_approvals(relayer, [deployment.sell_token.address])
    assert deployment.sell_token.allowance(deployment.burner, relayer) == MAX_UINT256


def test_emergency_disable_bundle_leaves_no_allowance_window(
    deployment: AuctionDeployment,
):
    """The approved emergency runbook: the multisig batches the registry's
    disable_adapter with the burner's sync_executor_approvals into one
    transaction, so the rail and its executor allowances die together (see
    scripts/emergency_cow_disable.py)."""
    from scripts.emergency_cow_disable import build_bundle

    _configure_and_enable_cow(deployment)
    _stage(deployment, deployment.sell_token, 100 * WAD)
    relayer = deployment.relayer
    assert deployment.sell_token.allowance(deployment.burner, relayer) == MAX_UINT256

    # The script's calldata must match the live ABI of both bundled calls.
    bundle = build_bundle(
        str(deployment.registry.address),
        str(deployment.burner.address),
        str(deployment.cow_adapter.address),
        relayer,
        [deployment.sell_token.address],
    )
    assert bundle == [
        (
            deployment.registry.address,
            "0x"
            + deployment.registry.disable_adapter.prepare_calldata(deployment.cow_adapter).hex(),
        ),
        (
            deployment.burner.address,
            "0x"
            + deployment.burner.sync_executor_approvals.prepare_calldata(
                relayer, [deployment.sell_token.address]
            ).hex(),
        ),
    ]

    # Both calls execute back-to-back from the emergency multisig batch: the
    # registry reads its roles from the same FeeCollector, so one emergency
    # owner covers both legs.
    with boa.env.prank(deployment.emergency_owner):
        deployment.registry.disable_adapter(deployment.cow_adapter)
        deployment.burner.sync_executor_approvals(relayer, [deployment.sell_token.address])

    assert not _cow_active(deployment)
    assert not deployment.registry.is_executor_active(relayer)
    assert deployment.sell_token.allowance(deployment.burner, relayer) == 0


def test_disabled_cow_rail_rejects_signatures_while_native_take_remains_live(
    deployment: AuctionDeployment,
):
    """The registry flag is the only switch: an emergency registry disable stops
    settlement routing immediately, native take keeps working, and the owner's
    reactivation restores the rail the same way."""
    _configure_and_enable_cow(deployment)
    lot, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    order = _cow_order(deployment, deployment.sell_token)
    signature = _encode_erc1271_signature(order, deployment.cow_adapter)
    order_hash = _gpv2_order_digest(order, deployment.settlement.domainSeparator())
    assert deployment.burner.isValidSignature(order_hash, signature) == ERC1271_MAGIC_VALUE

    with boa.env.prank(deployment.emergency_owner):
        deployment.registry.disable_adapter(deployment.cow_adapter)
    # The router has no live route left and answers the invalid magic.
    assert deployment.burner.isValidSignature(order_hash, signature) == ERC1271_INVALID
    assert deployment.burner.supportsInterface(ERC1271_MAGIC_VALUE)

    # Native take stays live; a partial fill keeps the lot alive for the
    # reactivation check below.
    half = staged // 2
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, half)
    deployment.target._mint_for_testing(deployment.buyer, payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        deployment.burner.take_with_limits(
            deployment.sell_token,
            half,
            half,
            payment,
            deployment.taker_receiver,
            _timestamp(),
            b"",
        )

    with boa.env.prank(deployment.owner):
        deployment.registry.activate_adapter(deployment.cow_adapter)
    assert _cow_active(deployment)
    assert deployment.burner.isValidSignature(order_hash, signature) == ERC1271_MAGIC_VALUE


def test_cow_pull_then_donation_resells_in_favor_of_fee_collector(
    deployment: AuctionDeployment,
):
    """Documented balance-based trade-off: donations after the snapshot are
    resellable along the same curve, bounded by initial_amount."""
    _configure_and_enable_cow(deployment)
    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    cow_amount = staged * 40 // 100
    unit_price = deployment.burner.price(deployment.sell_token)

    with boa.env.prank(deployment.relayer):
        deployment.sell_token.transferFrom(deployment.burner, deployment.taker_receiver, cow_amount)
    # Live balance bounds availability after a relayer pull.
    assert deployment.burner.available(deployment.sell_token) == staged - cow_amount

    deployment.sell_token._mint_for_testing(deployment.burner, cow_amount)
    assert deployment.sell_token.balanceOf(deployment.burner) == staged
    assert deployment.burner.available(deployment.sell_token) == staged
    assert deployment.burner.price(deployment.sell_token) == unit_price

    payment = deployment.burner.getAmountNeeded(deployment.sell_token, staged)
    deployment.target._mint_for_testing(deployment.buyer, payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        native_amount = deployment.burner.take(
            deployment.sell_token,
            MAX_UINT256,
            deployment.taker_receiver,
            b"",
        )

    assert native_amount == staged
    assert deployment.sell_token.balanceOf(deployment.taker_receiver) == staged + cow_amount
    assert deployment.sell_token.balanceOf(deployment.burner) == 0
    assert deployment.target.balanceOf(deployment.fee_collector) == payment
    assert deployment.burner.available(deployment.sell_token) == 0


def test_native_fill_then_donation_revives_availability_up_to_snapshot(
    deployment: AuctionDeployment,
):
    _configure_and_enable_cow(deployment)
    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    native_amount = staged * 40 // 100
    remaining = staged - native_amount
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, native_amount)
    deployment.target._mint_for_testing(deployment.buyer, payment)

    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        deployment.burner.take(
            deployment.sell_token,
            native_amount,
            deployment.taker_receiver,
            b"",
        )
    assert deployment.burner.available(deployment.sell_token) == remaining

    # A donation refills the balance: availability revives up to the snapshot
    # cap, and the CoW order re-quotes the whole refilled amount at curve price.
    deployment.sell_token._mint_for_testing(deployment.burner, native_amount)
    assert deployment.burner.available(deployment.sell_token) == staged
    order = _cow_order(deployment, deployment.sell_token)
    assert order[ORDER_SELL_AMOUNT] == staged

    with boa.env.prank(deployment.relayer):
        deployment.sell_token.transferFrom(
            deployment.burner, deployment.sink, order[ORDER_SELL_AMOUNT]
        )

    assert deployment.sell_token.balanceOf(deployment.sink) == staged
    assert deployment.sell_token.balanceOf(deployment.taker_receiver) == native_amount
    assert deployment.sell_token.balanceOf(deployment.burner) == 0
    assert deployment.burner.available(deployment.sell_token) == 0


BAD_ORDER_FLAGS = custom_err("OrderNotValid(string)", "BadOrderFlags")
BAD_BALANCE_MODE = custom_err("OrderNotValid(string)", "BadBalanceMode")


@pytest.mark.parametrize(
    "field,mutate,expected",
    [
        pytest.param(
            ORDER_SELL_TOKEN,
            lambda d, lot, order: d.second_token.address,
            custom_err("LotInactive()"),
            id="sell_token",
        ),
        pytest.param(
            ORDER_BUY_TOKEN,
            lambda d, lot, order: d.second_token.address,
            custom_err("BadBuyToken()"),
            id="buy_token",
        ),
        pytest.param(
            ORDER_RECEIVER,
            lambda d, lot, order: d.taker_receiver,
            custom_err("BadReceiver()"),
            id="receiver",
        ),
        pytest.param(
            ORDER_SELL_AMOUNT,
            lambda d, lot, order: lot[LOT_INITIAL_AMOUNT] + 1,
            custom_err("BadSellAmount()"),
            id="sell_amount",
        ),
        pytest.param(
            ORDER_BUY_AMOUNT,
            lambda d, lot, order: order[ORDER_BUY_AMOUNT] - 1,
            custom_err("BadBuyAmount()"),
            id="buy_amount",
        ),
        pytest.param(
            ORDER_VALID_TO,
            lambda d, lot, order: lot[LOT_END] + 1,
            custom_err("BadValidTo()"),
            id="valid_to",
        ),
        pytest.param(
            ORDER_APP_DATA,
            lambda d, lot, order: bytes.fromhex("11" * 32),
            custom_err("OrderNotValid(string)", "BadAppData"),
            id="app_data",
        ),
        pytest.param(ORDER_FEE_AMOUNT, lambda d, lot, order: 1, BAD_ORDER_FLAGS, id="fee_amount"),
        pytest.param(
            ORDER_KIND, lambda d, lot, order: keccak(text="buy"), BAD_ORDER_FLAGS, id="kind"
        ),
        pytest.param(
            ORDER_PARTIALLY_FILLABLE,
            lambda d, lot, order: False,
            BAD_ORDER_FLAGS,
            id="partially_fillable",
        ),
        pytest.param(
            ORDER_SELL_BALANCE,
            lambda d, lot, order: keccak(text="external"),
            BAD_BALANCE_MODE,
            id="sell_balance",
        ),
        pytest.param(
            ORDER_BUY_BALANCE,
            lambda d, lot, order: keccak(text="internal"),
            BAD_BALANCE_MODE,
            id="buy_balance",
        ),
    ],
)
def test_erc1271_rejects_every_security_relevant_gpv2_field(
    deployment: AuctionDeployment, field: int, mutate: Any, expected: str
):
    """Every GPv2 field is either pinned by the adapter (OrderNotValid) or
    judged by the auction's check_order (typed auction error); the digest is
    rebuilt for each mutation so only the field check can fail."""
    _configure_and_enable_cow(deployment)
    lot, _ = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    order = list(_cow_order(deployment, deployment.sell_token))
    domain_separator = deployment.settlement.domainSeparator()

    def validate(candidate: list[Any]) -> bytes:
        return deployment.burner.isValidSignature(
            _gpv2_order_digest(candidate, domain_separator),
            _encode_erc1271_signature(candidate, deployment.cow_adapter),
        )

    assert validate(order) == ERC1271_MAGIC_VALUE
    invalid = deepcopy(order)
    invalid[field] = mutate(deployment, lot, order)
    with boa.reverts(expected):
        validate(invalid)


def test_erc1271_valid_payload_magic_and_invalid_payloads(
    deployment: AuctionDeployment,
):
    _configure_and_enable_cow(deployment)
    lot, _ = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    order = _cow_order(deployment, deployment.sell_token)
    signature = _encode_erc1271_signature(order, deployment.cow_adapter)
    order_hash = _gpv2_order_digest(order, deployment.settlement.domainSeparator())

    assert deployment.burner.isValidSignature(order_hash, signature) == ERC1271_MAGIC_VALUE
    # A partial-fill order (a persistent order after fills) validates too.
    partial = _cow_order(
        deployment,
        deployment.sell_token,
        sell_amount=order[ORDER_SELL_AMOUNT] // 2,
        buy_amount=deployment.burner.getAmountNeeded(
            deployment.sell_token, order[ORDER_SELL_AMOUNT] // 2
        ),
    )
    assert (
        deployment.burner.isValidSignature(
            _gpv2_order_digest(partial, deployment.settlement.domainSeparator()),
            _encode_erc1271_signature(partial, deployment.cow_adapter),
        )
        == ERC1271_MAGIC_VALUE
    )

    # Malformed payloads behind the adapter prefix revert loudly: the adapter
    # answers a short payload with OrderNotValid("NonCanonical"), and a payload
    # longer than one encoded order fails ABI decoding before any check.
    prefix = _address_bytes(deployment.cow_adapter.address)
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        deployment.burner.isValidSignature(keccak(b"order"), prefix + b"malformed")
    with boa.reverts():
        deployment.burner.isValidSignature(order_hash, signature + b"\x00")
    # Without the prefix nothing routes: the historical unprefixed encoding and
    # an unknown prefix both answer the invalid magic.
    assert deployment.burner.isValidSignature(order_hash, signature[20:]) == ERC1271_INVALID
    assert (
        deployment.burner.isValidSignature(order_hash, bytes(20) + signature[20:])
        == ERC1271_INVALID
    )
    assert lot[LOT_START] != 0


def test_no_return_token_can_be_staged_and_taken(deployment: AuctionDeployment):
    lot, staged = _activate_lot(deployment, deployment.no_return_token, 100 * 10**8)
    payment = deployment.burner.getAmountNeeded(deployment.no_return_token, staged)
    deployment.target._mint_for_testing(deployment.buyer, payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        deployment.burner.take_with_limits(
            deployment.no_return_token,
            staged,
            staged,
            payment,
            deployment.taker_receiver,
            _timestamp(),
            b"",
        )
    assert deployment.no_return_token.balanceOf(deployment.taker_receiver) == staged


def test_false_return_balance_revert_and_blacklist_fail_atomically(
    deployment: AuctionDeployment,
):
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)

    deployment.problem_token.set_returns_false(True)
    deployment.problem_token.mint(deployment.fee_collector, 100 * WAD)
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.fee_collector.collect([deployment.problem_token.address], deployment.keeper)
    assert _lot_with_bounds(deployment, deployment.problem_token)[LOT_INITIAL_AMOUNT] == 0

    deployment.problem_token.set_returns_false(False)
    deployment.problem_token.set_revert_balance_of(True)
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.fee_collector.collect([deployment.problem_token.address], deployment.keeper)
    assert _lot_with_bounds(deployment, deployment.problem_token)[LOT_INITIAL_AMOUNT] == 0

    deployment.problem_token.set_revert_balance_of(False)
    deployment.problem_token.set_blacklisted(deployment.burner.address, True)
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.fee_collector.collect([deployment.problem_token.address], deployment.keeper)
    assert _lot_with_bounds(deployment, deployment.problem_token)[LOT_INITIAL_AMOUNT] == 0


def test_fee_on_transfer_snapshot_uses_actual_custody(deployment: AuctionDeployment):
    deployment.problem_token.set_fee_bps(100)
    nominal = 1_000 * WAD
    _stage(deployment, deployment.problem_token, nominal)
    actual = deployment.problem_token.balanceOf(deployment.burner)
    lot = _lot_with_bounds(deployment, deployment.problem_token)

    assert actual < nominal
    assert lot[LOT_INITIAL_AMOUNT] == actual
    assert deployment.problem_token.balanceOf(deployment.fee_collector) == 0


def test_reentrant_sell_token_transfer_reverts_without_accounting_loss(
    deployment: AuctionDeployment,
):
    _, staged = _activate_lot(deployment, deployment.problem_token, 100 * WAD)
    amount = staged // 2
    payment = deployment.burner.getAmountNeeded(deployment.problem_token, amount)
    deployment.target._mint_for_testing(deployment.buyer, payment)
    deployment.problem_token.configure_hook(
        deployment.burner,
        deployment.burner,
        _take_calldata(deployment.problem_token, amount, deployment.taker_receiver),
        True,
    )

    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        with boa.reverts():
            deployment.burner.take(deployment.problem_token, amount, deployment.taker_receiver, b"")

    assert deployment.problem_token.balanceOf(deployment.taker_receiver) == 0
    assert deployment.burner.available(deployment.problem_token) == staged
    assert deployment.target.balanceOf(deployment.fee_collector) == 0


def test_recover_empties_lot_and_set_killed_fences_donation_revival(
    deployment: AuctionDeployment,
):
    _configure_and_enable_cow(deployment)
    lot, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    order = _cow_order(deployment, deployment.sell_token)
    signature = _encode_erc1271_signature(order, deployment.cow_adapter)
    order_hash = _gpv2_order_digest(order, deployment.settlement.domainSeparator())

    with boa.env.prank(deployment.owner):
        deployment.burner.recover([deployment.sell_token.address])
    recovered = next(log for log in deployment.burner.get_logs() if _event_name(log) == "Recovered")
    assert recovered.address == deployment.burner.address
    assert recovered.token == deployment.sell_token.address
    assert recovered.amount == staged

    # No cancellation state: the drained balance alone kills the lot.
    emptied_lot = _lot_with_bounds(deployment, deployment.sell_token)
    assert emptied_lot[LOT_START] == lot[LOT_START]
    assert emptied_lot[LOT_INITIAL_AMOUNT] == staged
    assert deployment.sell_token.balanceOf(deployment.burner) == 0
    assert deployment.sell_token.balanceOf(deployment.fee_collector) == staged
    assert deployment.burner.available(deployment.sell_token) == 0
    assert deployment.burner.price(deployment.sell_token) == 0
    assert deployment.burner.getAmountNeeded(deployment.sell_token, 1) == 0

    with boa.env.prank(deployment.buyer), boa.reverts():
        deployment.burner.take(deployment.sell_token, 1, deployment.taker_receiver, b"")
    with boa.reverts():
        deployment.burner.isValidSignature(order_hash, signature)

    # A donation revives the still-registered lot — it resells at curve price
    # in FeeCollector's favor. An emergency evacuation therefore batches
    # recover with FeeCollector.set_killed to also fence donations out.
    donation = 10 * WAD
    deployment.sell_token._mint_for_testing(deployment.burner, donation)
    assert deployment.burner.available(deployment.sell_token) == donation
    assert deployment.burner.price(deployment.sell_token) > 0

    with boa.env.prank(deployment.owner):
        deployment.fee_collector.set_killed(
            [(deployment.sell_token.address, Epoch.COLLECT | Epoch.EXCHANGE)]
        )
    assert deployment.burner.available(deployment.sell_token) == 0
    assert deployment.burner.price(deployment.sell_token) == 0
    with boa.reverts():
        deployment.burner.isValidSignature(order_hash, signature)

    # The kill also blocks the permissionless re-collect of evacuated funds.
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.fee_collector.collect([deployment.sell_token.address], deployment.keeper)

    # Lifting the kill lets a COLLECT restage everything from the curve top.
    with boa.env.prank(deployment.owner):
        deployment.fee_collector.set_killed([(deployment.sell_token.address, 0)])
    collector_balance = deployment.sell_token.balanceOf(deployment.fee_collector)
    collect_fee = collector_balance * deployment.fee_collector.fee(Epoch.COLLECT) // WAD
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect([deployment.sell_token.address], deployment.keeper)

    refreshed_lot = _lot_with_bounds(deployment, deployment.sell_token)
    expected_snapshot = donation + collector_balance - collect_fee
    assert refreshed_lot[LOT_START] == lot[LOT_START] + WEEK
    assert refreshed_lot[LOT_INITIAL_AMOUNT] == expected_snapshot
    assert deployment.sell_token.allowance(deployment.burner, deployment.relayer) == MAX_UINT256

    _move_to_timestamp(refreshed_lot[LOT_START])
    assert deployment.burner.available(deployment.sell_token) == expected_snapshot
    assert deployment.burner.price(deployment.sell_token) > 0
    refreshed_order = _cow_order(deployment, deployment.sell_token)
    assert refreshed_order[ORDER_SELL_AMOUNT] == expected_snapshot


def test_recover_during_collect_frame_recollect_restages_unless_killed(
    deployment: AuctionDeployment,
):
    _configure_and_enable_cow(deployment)
    staged, _ = _stage(deployment, deployment.sell_token, 100 * WAD)
    first_lot = _lot_with_bounds(deployment, deployment.sell_token)
    assert deployment.sell_token.allowance(deployment.burner, deployment.relayer) == MAX_UINT256

    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    recovery_window = _exchange_start(deployment)
    assert recovery_window == first_lot[LOT_START] + WEEK
    # An emergency evacuation batches recover with a COLLECT kill: recover
    # alone leaves the permissionless re-collect open in the same frame.
    with boa.env.prank(deployment.owner):
        deployment.burner.recover([deployment.sell_token.address])
        deployment.fee_collector.set_killed([(deployment.sell_token.address, Epoch.COLLECT)])

    assert deployment.sell_token.balanceOf(deployment.burner) == 0
    assert deployment.sell_token.balanceOf(deployment.fee_collector) == staged

    new_receipts = 25 * WAD
    deployment.sell_token._mint_for_testing(deployment.fee_collector, new_receipts)
    collector_before = deployment.sell_token.balanceOf(deployment.fee_collector)
    keeper_before = deployment.sell_token.balanceOf(deployment.keeper)
    lot_before = _lot_with_bounds(deployment, deployment.sell_token)

    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.fee_collector.collect([deployment.sell_token.address], deployment.keeper)

    assert _lot_with_bounds(deployment, deployment.sell_token) == lot_before
    assert deployment.sell_token.balanceOf(deployment.fee_collector) == collector_before
    assert deployment.sell_token.balanceOf(deployment.keeper) == keeper_before
    assert deployment.sell_token.balanceOf(deployment.burner) == 0

    # Once the kill is lifted, the very same frame's permissionless collect
    # restages the evacuated funds from the top of the curve.
    with boa.env.prank(deployment.owner):
        deployment.fee_collector.set_killed([(deployment.sell_token.address, 0)])
    fee = collector_before * deployment.fee_collector.fee(Epoch.COLLECT) // WAD
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect([deployment.sell_token.address], deployment.keeper)

    refreshed_lot = _lot_with_bounds(deployment, deployment.sell_token)
    expected_snapshot = collector_before - fee
    assert refreshed_lot[LOT_START] == recovery_window
    assert refreshed_lot[LOT_INITIAL_AMOUNT] == expected_snapshot
    assert deployment.sell_token.balanceOf(deployment.fee_collector) == 0
    assert deployment.sell_token.balanceOf(deployment.burner) == expected_snapshot
    assert deployment.sell_token.balanceOf(deployment.keeper) == keeper_before + fee
    assert deployment.sell_token.allowance(deployment.burner, deployment.relayer) == MAX_UINT256

    _move_to_timestamp(refreshed_lot[LOT_START])
    assert deployment.burner.available(deployment.sell_token) == expected_snapshot
    refreshed_order = _cow_order(deployment, deployment.sell_token)
    assert refreshed_order[ORDER_SELL_AMOUNT] == expected_snapshot


def test_recover_before_first_staging_leaves_no_state_and_collect_restages(
    deployment: AuctionDeployment,
):
    _configure_and_enable_cow(deployment)
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    recovery_window = _exchange_start(deployment)
    recovered_amount = 10 * WAD
    deployment.sell_token._mint_for_testing(deployment.burner, recovered_amount)

    with boa.env.prank(deployment.owner):
        deployment.burner.recover([deployment.sell_token.address])

    assert _lot_with_bounds(deployment, deployment.sell_token) == (0, 0, 0)
    assert deployment.sell_token.balanceOf(deployment.burner) == 0
    assert deployment.sell_token.balanceOf(deployment.fee_collector) == recovered_amount
    # Approvals only come from the keeper's sync: nothing was approved yet.
    assert deployment.sell_token.allowance(deployment.burner, deployment.relayer) == 0

    # Recovery leaves no lingering lot state: the same COLLECT frame's
    # permissionless collect restages the returned balance.
    keeper_before = deployment.sell_token.balanceOf(deployment.keeper)
    fee = recovered_amount * deployment.fee_collector.fee(Epoch.COLLECT) // WAD
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect([deployment.sell_token.address], deployment.keeper)
        deployment.burner.sync_executor_approvals(
            deployment.relayer, [deployment.sell_token.address]
        )

    refreshed_lot = _lot_with_bounds(deployment, deployment.sell_token)
    expected_snapshot = recovered_amount - fee
    assert refreshed_lot[LOT_START] == recovery_window
    assert refreshed_lot[LOT_INITIAL_AMOUNT] == expected_snapshot
    assert deployment.sell_token.balanceOf(deployment.keeper) == keeper_before + fee
    assert deployment.sell_token.allowance(deployment.burner, deployment.relayer) == MAX_UINT256
    _move_to_timestamp(refreshed_lot[LOT_START])
    assert _cow_order(deployment, deployment.sell_token)[ORDER_SELL_AMOUNT] == expected_snapshot


def test_push_target_and_recovery_only_return_assets_to_fee_collector(
    deployment: AuctionDeployment,
):
    deployment.target._mint_for_testing(deployment.burner, 10 * WAD)
    with boa.env.prank(deployment.keeper):
        assert deployment.burner.push_target() == 10 * WAD
    assert deployment.target.balanceOf(deployment.fee_collector) == 10 * WAD

    # recover() is owner-only: stopping fills is the emergency owner's job
    # (kill masks, registry disable); moving stuck funds is not.
    deployment.sell_token._mint_for_testing(deployment.burner, 5 * WAD)
    boa.env.set_balance(deployment.burner.address, WAD)
    with boa.env.prank(deployment.keeper), boa.reverts(custom_err("OnlyOwner()")):
        deployment.burner.recover([deployment.sell_token.address, ETH_ADDRESS])
    with boa.env.prank(deployment.emergency_owner), boa.reverts(custom_err("OnlyOwner()")):
        deployment.burner.recover([deployment.sell_token.address, ETH_ADDRESS])
    assert deployment.sell_token.balanceOf(deployment.burner) == 5 * WAD
    with boa.env.prank(deployment.owner):
        deployment.burner.recover([deployment.sell_token.address, ETH_ADDRESS])
    assert deployment.sell_token.balanceOf(deployment.fee_collector) == 5 * WAD
    assert boa.env.get_balance(deployment.fee_collector.address) == WAD
    assert deployment.sell_token.balanceOf(deployment.owner) == 0
    assert boa.env.get_balance(deployment.owner) == 0


def test_full_lifecycle_collect_cow_and_native_fills_then_forward(
    deployment: AuctionDeployment,
):
    """COLLECT -> CoW pull + native take -> FORWARD quality gate.

    The CoW leg models only what settles on the chain under test: the vault
    relayer spends the burner's allowance on the sell token, while the target
    payment a real solver routes through GPv2Settlement is simulated by
    minting the exact quote to the FeeCollector (the same boundary as the
    Gnosis fork test). The native leg pays through take() for real. FORWARD
    must then sweep burner-held target and deliver everything but the caller
    fee to the Hooker.
    """
    _configure_and_enable_cow(deployment)
    hooker = boa.load("contracts/hooks/Hooker.vy", deployment.fee_collector, [], [], [])
    with boa.env.prank(deployment.owner):
        deployment.fee_collector.set_hooker(hooker)

    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)

    cow_amount = staged * 40 // 100
    cow_payment = deployment.burner.getAmountNeeded(deployment.sell_token, cow_amount)
    with boa.env.prank(deployment.relayer):
        deployment.sell_token.transferFrom(deployment.burner, deployment.taker_receiver, cow_amount)
    deployment.target._mint_for_testing(deployment.fee_collector, cow_payment)

    native_amount = deployment.burner.available(deployment.sell_token)
    native_payment = deployment.burner.getAmountNeeded(deployment.sell_token, native_amount)
    deployment.target._mint_for_testing(deployment.buyer, native_payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, native_payment)
        assert (
            deployment.burner.take(deployment.sell_token, MAX_UINT256, deployment.buyer, b"")
            == native_amount
        )
    assert deployment.sell_token.balanceOf(deployment.burner) == 0

    # Target stranded on the burner must be swept by forward()'s push_target.
    stray_target = 3 * WAD
    deployment.target._mint_for_testing(deployment.burner, stray_target)
    total = cow_payment + native_payment + stray_target

    _move_to_epoch(deployment.fee_collector, Epoch.FORWARD)
    assert hooker.buffer_amount() == 0
    forward_fee = total * deployment.fee_collector.fee(Epoch.FORWARD) // WAD
    with boa.env.prank(deployment.keeper):
        assert deployment.fee_collector.forward([], deployment.keeper) == forward_fee

    assert deployment.target.balanceOf(deployment.keeper) == forward_fee
    assert deployment.target.balanceOf(hooker.address) == total - forward_fee
    assert deployment.target.balanceOf(deployment.fee_collector) == 0
    assert deployment.target.balanceOf(deployment.burner) == 0


def test_resync_target_follows_fee_collector_and_fences_old_lots(
    deployment: AuctionDeployment,
):
    """Target migration: divergence freezes every rail, resync re-pins the
    denomination from the FeeCollector during the next SLEEP phase — before
    that week's staging — so the fence only stales the previous week and a
    restage in the same week's COLLECT trades the same week (no week is
    lost), including the old target as regular sellable inventory."""
    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    assert deployment.burner.available(deployment.sell_token) == staged

    old_target = deployment.target
    new_target = boa.load("contracts/testing/ERC20Mock.vy", "New Target", "NEWT", 18)
    with boa.env.prank(deployment.owner):
        deployment.fee_collector.set_target(new_target)

    # Divergence freezes fills, validation, and staging before any resync.
    assert deployment.burner.available(deployment.sell_token) == 0
    with boa.env.prank(deployment.buyer), boa.reverts():
        deployment.burner.take(deployment.sell_token, MAX_UINT256, deployment.buyer, b"")
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    deployment.second_token._mint_for_testing(deployment.fee_collector, WAD)
    with boa.env.prank(deployment.keeper), boa.reverts(custom_err("TargetChanged()", nested=True)):
        deployment.fee_collector.collect([deployment.second_token.address], deployment.keeper)

    # Resyncs are SLEEP-only: a COLLECT execution must wait for the next week.
    with boa.env.prank(deployment.owner), boa.reverts(custom_err("NotSleepEpoch()")):
        deployment.burner.resync_target(new_target, START_TOTAL, FLOOR_TOTAL, STEP_DURATION)
    _move_to_epoch(deployment.fee_collector, Epoch.SLEEP)

    with boa.env.prank(deployment.keeper), boa.reverts(custom_err("OnlyOwner()")):
        deployment.burner.resync_target(new_target, START_TOTAL, FLOOR_TOTAL, STEP_DURATION)
    with boa.env.prank(deployment.owner), boa.reverts(custom_err("StepExceedsAuction()")):
        deployment.burner.resync_target(new_target, START_TOTAL, FLOOR_TOTAL, EXCHANGE_DURATION)
    # Delayed-execution guard: params tuned for the old denomination must not
    # bind to the new one.
    with boa.env.prank(deployment.owner), boa.reverts(custom_err("TargetChanged()")):
        deployment.burner.resync_target(old_target, START_TOTAL, FLOOR_TOTAL, STEP_DURATION)

    with boa.env.prank(deployment.owner):
        deployment.burner.resync_target(new_target, 2 * START_TOTAL, 2 * FLOOR_TOTAL, STEP_DURATION)
    resynced = next(
        log for log in deployment.burner.get_logs() if _event_name(log) == "EconomicsSet"
    )
    assert resynced.want == new_target.address
    assert resynced.start_total == 2 * START_TOTAL
    assert deployment.burner.want() == new_target.address
    assert deployment.burner.start_total() == 2 * START_TOTAL

    # The resync landed in SLEEP, before this week's staging and window: the
    # fence stops at the previous week, so only the stale lot stays dead.
    assert _lot_with_bounds(deployment, deployment.sell_token)[LOT_START] < _exchange_start(
        deployment
    )
    assert deployment.burner.available(deployment.sell_token) == 0
    with boa.env.prank(deployment.buyer), boa.reverts():
        deployment.burner.take(deployment.sell_token, MAX_UINT256, deployment.buyer, b"")

    # Same week's COLLECT: restage picks the new curve immediately; the old
    # target is now plain sellable inventory; fills pay in the new
    # denomination without waiting for the next week.
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    old_target._mint_for_testing(deployment.fee_collector, 10 * WAD)
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect([deployment.sell_token.address], deployment.keeper)
        deployment.fee_collector.collect([old_target.address], deployment.keeper)

    lot = _lot_with_bounds(deployment, deployment.sell_token)
    assert lot[LOT_START] == _exchange_start(deployment)
    assert deployment.burner.start_total() == 2 * START_TOTAL
    assert _lot_with_bounds(deployment, old_target)[LOT_START] == lot[LOT_START]
    _move_to_timestamp(lot[LOT_START])

    assert deployment.burner.available(deployment.sell_token) == staged
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, staged)
    new_target._mint_for_testing(deployment.buyer, payment)
    with boa.env.prank(deployment.buyer):
        new_target.approve(deployment.burner, payment)
        deployment.burner.take(deployment.sell_token, MAX_UINT256, deployment.buyer, b"")
    assert new_target.balanceOf(deployment.fee_collector) == payment
    assert deployment.burner.available(old_target) > 0


def test_resync_only_in_sleep_fences_previous_lots(
    deployment: AuctionDeployment,
):
    """Resyncs are confined to SLEEP — before the week's staging — so a want
    change can never land under a staged lot: EXCHANGE and FORWARD attempts
    revert, and the SLEEP resync stales every earlier staging, killing the old
    week's lot and its published CoW order."""
    _configure_and_enable_cow(deployment)
    lot, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    order = _cow_order(deployment, deployment.sell_token)
    signature = _encode_erc1271_signature(order, deployment.cow_adapter)
    order_hash = _gpv2_order_digest(order, deployment.settlement.domainSeparator())
    assert deployment.burner.isValidSignature(order_hash, signature) == ERC1271_MAGIC_VALUE

    new_target = boa.load("contracts/testing/ERC20Mock.vy", "New Target", "NEWT", 18)
    with boa.env.prank(deployment.owner):
        deployment.fee_collector.set_target(new_target)
        # The window is open: any resync must wait for the next SLEEP.
        with boa.reverts(custom_err("NotSleepEpoch()")):
            deployment.burner.resync_target(new_target, START_TOTAL, FLOOR_TOTAL, STEP_DURATION)

    # FORWARD — the window closed, but still not SLEEP.
    _move_to_timestamp(lot[LOT_END])
    with boa.env.prank(deployment.owner), boa.reverts(custom_err("NotSleepEpoch()")):
        deployment.burner.resync_target(new_target, START_TOTAL, FLOOR_TOTAL, STEP_DURATION)

    _move_to_epoch(deployment.fee_collector, Epoch.SLEEP)
    with boa.env.prank(deployment.owner):
        deployment.burner.resync_target(new_target, START_TOTAL, FLOOR_TOTAL, STEP_DURATION)

    # The fence stops at the previous week: the lot staged then is dead while
    # the week now in SLEEP stays stageable.
    assert lot[LOT_START] < _exchange_start(deployment)
    assert deployment.burner.available(deployment.sell_token) == 0
    with boa.env.prank(deployment.buyer), boa.reverts(custom_err("NothingAvailable()")):
        deployment.burner.take(deployment.sell_token, MAX_UINT256, deployment.buyer, b"")
    # The published order names the old denomination, so the buy-token check
    # fails first; re-quoted in the new want, the fenced lot itself is inactive.
    with boa.reverts(custom_err("BadBuyToken()")):
        deployment.burner.isValidSignature(order_hash, signature)
    requoted = list(order)
    requoted[ORDER_BUY_TOKEN] = new_target.address
    with boa.reverts(custom_err("LotInactive()")):
        deployment.burner.isValidSignature(
            _gpv2_order_digest(requoted, deployment.settlement.domainSeparator()),
            _encode_erc1271_signature(requoted, deployment.cow_adapter),
        )


def test_resync_same_target_retune_in_sleep_repins_curve(
    deployment: AuctionDeployment,
):
    """A same-target retune executed during SLEEP re-pins the curve behind the
    same fence as a target change (only earlier stagings, so no week is lost);
    the week staged right after trades the retuned curve from the window
    open. Outside SLEEP every resync reverts, so the price a taker sees can
    only decay — plain take() needs no payment bound."""
    _move_to_epoch(deployment.fee_collector, Epoch.SLEEP)
    with boa.env.prank(deployment.owner):
        deployment.burner.resync_target(
            deployment.target,
            2 * START_TOTAL,
            2 * FLOOR_TOTAL,
            STEP_DURATION,
        )

    assert deployment.burner.want() == deployment.target.address
    assert deployment.burner.start_total() == 2 * START_TOTAL

    # Staging moves to the same week's COLLECT — which is no longer SLEEP.
    staged, _ = _stage(deployment, deployment.sell_token, 100 * WAD)
    with boa.env.prank(deployment.owner), boa.reverts(custom_err("NotSleepEpoch()")):
        deployment.burner.resync_target(deployment.target, START_TOTAL, FLOOR_TOTAL, STEP_DURATION)

    lot = _lot_with_bounds(deployment, deployment.sell_token)
    _move_to_timestamp(lot[LOT_START])
    assert deployment.burner.available(deployment.sell_token) == staged
    # Full lot at the window open quotes the retuned start total exactly.
    assert deployment.burner.getAmountNeeded(deployment.sell_token, staged) == 2 * START_TOTAL
    # Mid-window retunes are rejected outright.
    with boa.env.prank(deployment.owner), boa.reverts(custom_err("NotSleepEpoch()")):
        deployment.burner.resync_target(deployment.target, START_TOTAL, FLOOR_TOTAL, STEP_DURATION)

    # A fill mid-window pays along the retuned curve.
    timestamp = boa.env.evm.vm.state.timestamp
    quote_live = deployment.burner.getAmountNeeded(deployment.sell_token, staged)
    assert quote_live == _quote_from_total(
        _reference_total(lot, timestamp, 2 * START_TOTAL, 2 * FLOOR_TOTAL),
        staged,
        staged,
    )
    deployment.target._mint_for_testing(deployment.buyer, quote_live)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, quote_live)
        deployment.burner.take(deployment.sell_token, MAX_UINT256, deployment.buyer, b"")
    assert deployment.target.balanceOf(deployment.fee_collector) == quote_live
