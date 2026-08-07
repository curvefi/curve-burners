from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, localcontext
from typing import Any

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ..conftest import ETH_ADDRESS, ZERO_ADDRESS, Epoch, WEEK


WAD = 10**18
RAY = 10**27
START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
COW_ORDER_VALIDITY = 120
APP_DATA = bytes.fromhex("058315b749613051abcbf50cf2d605b4fa4a41554ec35d73fd058fc530da559f")
ZERO_BYTES32 = bytes(32)
MAX_UINT256 = 2**256 - 1

SELL_KIND = keccak(text="sell")
ERC20_BALANCE = keccak(text="erc20")
ERC1271_MAGIC_VALUE = bytes.fromhex("1626ba7e")

ERC165_INTERFACE = bytes.fromhex("01ffc9a7")
BURNER_INTERFACE = bytes.fromhex("a3b5e311")
CONDITIONAL_ORDER_INTERFACE = bytes.fromhex("b8296fc4")
SIGNATURE_VERIFIER_MUXER_INTERFACE = bytes.fromhex("62af8dc2")

# Lot tuple fields fixed by the integration ABI.
LOT_WEEK = 0
LOT_INITIAL_AMOUNT = 1
LOT_NATIVE_REMAINING = 2
LOT_START_TOTAL = 3
LOT_FLOOR_TOTAL = 4
LOT_START = 5
LOT_END = 6

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
# Reviewed upper bound for rounded-up RAY pow: active step 1439 reaches the floor.
DECAY_FACTOR_RAY = 992031276831159793484252056


@dataclass(frozen=True)
class AuctionDeployment:
    owner: str
    emergency_owner: str
    keeper: str
    buyer: str
    receiver: str
    watcher: str
    retired_relayer: str
    new_relayer: str
    target: Any
    sell_token: Any
    second_token: Any
    no_return_token: Any
    problem_token: Any
    weth: Any
    fee_collector: Any
    composable_cow: Any
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


def _address_bytes(address: Any) -> bytes:
    return bytes.fromhex(str(address)[2:])


def _static_input(token: Any, generation: int) -> bytes:
    return _address_bytes(token.address) + generation.to_bytes(32, "big")


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
    return amount - fee, fee


def _stage_problem_token(
    deployment: AuctionDeployment,
    token: Any,
    amount: int,
    *,
    receiver: str | None = None,
) -> tuple[int, int]:
    if deployment.fee_collector.epoch() != Epoch.COLLECT:
        _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    receiver = receiver or deployment.keeper
    token.mint(deployment.fee_collector, amount)
    fee = amount * deployment.fee_collector.fee(Epoch.COLLECT) // WAD
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect([token.address], receiver)
    return amount - fee, fee


def _activate_lot(deployment: AuctionDeployment, token: Any, amount: int) -> tuple[Any, int]:
    staged, _ = _stage(deployment, token, amount)
    lot = deployment.burner.lots(token)
    _move_to_timestamp(lot[LOT_START])
    assert deployment.fee_collector.epoch() == Epoch.EXCHANGE
    return lot, staged


def _configure_and_enable_cow(
    deployment: AuctionDeployment,
    *,
    relayer: str | None = None,
) -> int:
    relayer = relayer or deployment.retired_relayer
    with boa.env.prank(deployment.owner):
        deployment.burner.configure_cow(deployment.composable_cow, relayer)
        deployment.burner.enable_cow()
    return deployment.burner.cow_generation()


def _tradeable_order(
    deployment: AuctionDeployment,
    token: Any,
    generation: int | None = None,
    offchain_input: bytes = b"",
) -> Any:
    generation = deployment.burner.cow_generation() if generation is None else generation
    return deployment.burner.getTradeableOrder(
        deployment.burner.address,
        deployment.watcher,
        ZERO_BYTES32,
        _static_input(token, generation),
        offchain_input,
    )


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


def _verify_order(
    deployment: AuctionDeployment,
    token: Any,
    order: Any,
    *,
    generation: int | None = None,
    static_input: bytes | None = None,
    offchain_input: bytes = b"",
) -> None:
    generation = deployment.burner.cow_generation() if generation is None else generation
    static_input = static_input or _static_input(token, generation)
    domain_separator = deployment.composable_cow.domainSeparator()
    order_hash = _gpv2_order_digest(order, domain_separator)
    deployment.burner.verify(
        deployment.burner.address,
        deployment.watcher,
        order_hash,
        domain_separator,
        ZERO_BYTES32,
        static_input,
        offchain_input,
        order,
    )


def _ceil_decimal(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _reference_total(lot: Any, timestamp: int) -> int:
    steps = (timestamp - lot[LOT_START]) // STEP_DURATION
    with localcontext() as context:
        context.prec = 120
        total = Decimal(lot[LOT_START_TOTAL]) * (
            Decimal(DECAY_FACTOR_RAY) / Decimal(RAY)
        ) ** steps
    return max(lot[LOT_FLOOR_TOTAL], _ceil_decimal(total))


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


def _encode_erc1271_signature(order: Any, burner: Any, static_input: bytes) -> bytes:
    order_type = (
        "(address,address,address,uint256,uint256,uint32,bytes32,uint256,"
        "bytes32,bool,bytes32,bytes32)"
    )
    payload_type = "(bytes32[],(address,bytes32,bytes),bytes)"
    normalized_order = (
        str(order[0]),
        str(order[1]),
        str(order[2]),
        *order[3:],
    )
    payload = ([], (str(burner.address), ZERO_BYTES32, static_input), b"")
    return encode([order_type, payload_type], [normalized_order, payload])


def _take_calldata(token: Any, max_amount: int, receiver: Any) -> bytes:
    return keccak(text="take(address,uint256,address,bytes)")[:4] + encode(
        ["address", "uint256", "address", "bytes"],
        [str(token.address), max_amount, str(receiver), b""],
    )


@pytest.fixture(autouse=True)
def isolate_chain():
    with boa.env.anchor():
        yield


@pytest.fixture
def burner_deployer():
    return boa.load_partial("contracts/burners/DutchAuctionBurner.vy")


@pytest.fixture
def deployment(burner_deployer: Any) -> AuctionDeployment:
    owner = boa.env.generate_address("owner")
    emergency_owner = boa.env.generate_address("emergency_owner")
    keeper = boa.env.generate_address("keeper")
    buyer = boa.env.generate_address("buyer")
    receiver = boa.env.generate_address("receiver")
    watcher = boa.env.generate_address("watcher")
    retired_relayer = boa.env.generate_address("retired_relayer")
    new_relayer = boa.env.generate_address("new_relayer")

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
    fee_collector = boa.load(
        "contracts/FeeCollector.vy", target, weth, owner, emergency_owner
    )
    composable_cow = boa.load("contracts/testing/dutch_auction/ComposableCowMock.vy")

    burner = burner_deployer.deploy(
        fee_collector,
        START_TOTAL,
        FLOOR_TOTAL,
        DECAY_FACTOR_RAY,
        STEP_DURATION,
        COW_ORDER_VALIDITY,
        APP_DATA,
    )
    with boa.env.prank(owner):
        fee_collector.set_burner(burner)
        fee_collector.set_killed([(ZERO_ADDRESS, 0)])

    return AuctionDeployment(
        owner,
        emergency_owner,
        keeper,
        buyer,
        receiver,
        watcher,
        retired_relayer,
        new_relayer,
        target,
        sell_token,
        second_token,
        no_return_token,
        problem_token,
        weth,
        fee_collector,
        composable_cow,
        burner,
    )


def test_constructor_and_fixed_interfaces(deployment: AuctionDeployment):
    burner = deployment.burner

    assert burner.VERSION() == "DutchAuction"
    assert burner.want() == deployment.target.address
    assert burner.supportsInterface(ERC165_INTERFACE)
    assert burner.supportsInterface(BURNER_INTERFACE)
    assert not burner.supportsInterface(CONDITIONAL_ORDER_INTERFACE)
    assert not burner.supportsInterface(ERC1271_MAGIC_VALUE)

    assert not burner.cow_enabled()
    assert burner.composable_cow() == ZERO_ADDRESS
    assert burner.vault_relayer() == ZERO_ADDRESS
    assert burner.cow_generation() == 0

    signatures = _function_signatures(burner)
    assert {
        "want()",
        "available(address)",
        "price(address)",
        "getAmountNeeded(address,uint256)",
        "take(address,uint256,address,bytes)",
        "take_with_limits(address,uint256,uint256,uint256,address,uint256,uint256,bytes)",
    } <= signatures


@pytest.mark.parametrize(
    "start_total,floor_total,decay_factor,step_duration,validity",
    [
        (0, FLOOR_TOTAL, DECAY_FACTOR_RAY, STEP_DURATION, COW_ORDER_VALIDITY),
        (START_TOTAL, 0, DECAY_FACTOR_RAY, STEP_DURATION, COW_ORDER_VALIDITY),
        (START_TOTAL, START_TOTAL + 1, DECAY_FACTOR_RAY, STEP_DURATION, COW_ORDER_VALIDITY),
        (START_TOTAL, FLOOR_TOTAL, 0, STEP_DURATION, COW_ORDER_VALIDITY),
        (START_TOTAL, FLOOR_TOTAL, RAY // 2 - 1, STEP_DURATION, COW_ORDER_VALIDITY),
        (START_TOTAL, FLOOR_TOTAL, DECAY_FACTOR_RAY + 1, STEP_DURATION, COW_ORDER_VALIDITY),
        (START_TOTAL, FLOOR_TOTAL, RAY + 1, STEP_DURATION, COW_ORDER_VALIDITY),
        (START_TOTAL, FLOOR_TOTAL, DECAY_FACTOR_RAY, 0, COW_ORDER_VALIDITY),
        (START_TOTAL, FLOOR_TOTAL, DECAY_FACTOR_RAY, STEP_DURATION, 0),
    ],
)
def test_constructor_rejects_invalid_curve_parameters(
    burner_deployer: Any,
    deployment: AuctionDeployment,
    start_total: int,
    floor_total: int,
    decay_factor: int,
    step_duration: int,
    validity: int,
):
    with boa.reverts():
        burner_deployer.deploy(
            deployment.fee_collector,
            start_total,
            floor_total,
            decay_factor,
            step_duration,
            validity,
            APP_DATA,
        )


def test_constructor_rejects_more_than_100_000_active_price_steps(
    burner_deployer: Any,
    deployment: AuctionDeployment,
):
    long_frame_collector = boa.load(
        "contracts/testing/dutch_auction/FrameFeeCollectorMock.vy",
        deployment.target,
        100_002,
    )
    with boa.reverts("Too many price steps"):
        burner_deployer.deploy(
            long_frame_collector,
            START_TOTAL,
            FLOOR_TOTAL,
            RAY // 2,
            1,
            COW_ORDER_VALIDITY,
            APP_DATA,
        )


def test_only_fee_collector_can_burn_and_target_is_rejected(deployment: AuctionDeployment):
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.burner.burn([deployment.sell_token.address], deployment.keeper)

    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    deployment.target._mint_for_testing(deployment.fee_collector, WAD)
    with boa.env.prank(deployment.fee_collector.address), boa.reverts():
        deployment.burner.burn([deployment.target.address], deployment.keeper)

    assert deployment.burner.lots(deployment.target)[LOT_INITIAL_AMOUNT] == 0


def test_collect_pays_fee_moves_custody_and_snapshots_lot(deployment: AuctionDeployment):
    amount = 1_000 * WAD
    staged, fee = _stage(deployment, deployment.sell_token, amount)
    lot_synced = next(
        log
        for log in deployment.fee_collector.get_logs()
        if _event_name(log) == "LotSynced"
    )
    lot = deployment.burner.lots(deployment.sell_token)

    assert deployment.sell_token.balanceOf(deployment.keeper) == fee
    assert deployment.sell_token.balanceOf(deployment.fee_collector) == 0
    assert deployment.sell_token.balanceOf(deployment.burner) == staged
    assert lot[LOT_INITIAL_AMOUNT] == staged
    assert lot[LOT_NATIVE_REMAINING] == staged
    assert lot[LOT_START_TOTAL] == START_TOTAL
    assert lot[LOT_FLOOR_TOTAL] == FLOOR_TOTAL
    assert lot[LOT_START] < lot[LOT_END]
    assert lot[LOT_WEEK] == lot[LOT_START] // WEEK

    assert lot_synced.address == deployment.burner.address
    assert lot_synced.token == deployment.sell_token.address
    assert lot_synced.week == lot[LOT_WEEK]
    assert lot_synced.initial_amount == lot[LOT_INITIAL_AMOUNT]
    assert lot_synced.start_total == lot[LOT_START_TOTAL]
    assert lot_synced.floor_total == lot[LOT_FLOOR_TOTAL]
    assert lot_synced.start == lot[LOT_START]
    assert lot_synced.end == lot[LOT_END]


def test_repeated_collect_updates_snapshot_without_charging_old_inventory(
    deployment: AuctionDeployment,
):
    first_staged, first_fee = _stage(deployment, deployment.sell_token, 1_000 * WAD)
    first_lot = deployment.burner.lots(deployment.sell_token)

    second_amount = 250 * WAD
    deployment.sell_token._mint_for_testing(deployment.fee_collector, second_amount)
    fee_rate = deployment.fee_collector.fee(Epoch.COLLECT)
    second_fee = second_amount * fee_rate // WAD
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect([deployment.sell_token.address], deployment.keeper)

    second_lot = deployment.burner.lots(deployment.sell_token)
    assert deployment.sell_token.balanceOf(deployment.keeper) == first_fee + second_fee
    assert second_lot[LOT_INITIAL_AMOUNT] == first_staged + second_amount - second_fee
    assert second_lot[LOT_NATIVE_REMAINING] == second_lot[LOT_INITIAL_AMOUNT]
    assert second_lot[LOT_WEEK] == first_lot[LOT_WEEK]
    assert second_lot[LOT_START] == first_lot[LOT_START]


def test_collect_outside_collect_epoch_reverts_without_state_changes(
    deployment: AuctionDeployment,
):
    _move_to_epoch(deployment.fee_collector, Epoch.EXCHANGE)
    deployment.sell_token._mint_for_testing(deployment.fee_collector, WAD)

    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.fee_collector.collect([deployment.sell_token.address], deployment.keeper)

    assert deployment.sell_token.balanceOf(deployment.fee_collector) == WAD
    assert deployment.burner.lots(deployment.sell_token)[LOT_INITIAL_AMOUNT] == 0


def test_weekly_rollover_resnapshots_unsold_inventory_and_new_receipts(
    deployment: AuctionDeployment,
):
    first_staged, _ = _stage(deployment, deployment.sell_token, 1_000 * WAD)
    first_lot = deployment.burner.lots(deployment.sell_token)
    _move_to_timestamp(first_lot[LOT_START])

    amount_taken = first_staged // 4
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, amount_taken)
    deployment.target._mint_for_testing(deployment.buyer, payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        deployment.burner.take(deployment.sell_token, amount_taken, deployment.receiver, b"")

    unsold = first_staged - amount_taken
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    new_staged, _ = _stage(deployment, deployment.sell_token, 200 * WAD)
    second_lot = deployment.burner.lots(deployment.sell_token)

    assert second_lot[LOT_WEEK] == first_lot[LOT_WEEK] + 1
    assert second_lot[LOT_INITIAL_AMOUNT] == unsold + new_staged
    assert second_lot[LOT_NATIVE_REMAINING] == unsold + new_staged


def test_unsynced_token_is_inactive_in_a_new_week(deployment: AuctionDeployment):
    first_lot, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    assert deployment.burner.available(deployment.sell_token) == staged

    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    _stage(deployment, deployment.second_token, 100 * 10**6)
    second_lot = deployment.burner.lots(deployment.second_token)
    _move_to_timestamp(second_lot[LOT_START])

    assert deployment.burner.available(deployment.sell_token) == 0
    assert deployment.burner.price(deployment.sell_token) == 0
    assert deployment.burner.getAmountNeeded(deployment.sell_token, 1) == 0


def test_price_boundaries_geometric_checkpoints_and_floor(deployment: AuctionDeployment):
    lot, staged = _activate_lot(deployment, deployment.sell_token, 10_000 * WAD)

    assert deployment.burner.getAmountNeeded(deployment.sell_token, staged) == START_TOTAL
    assert deployment.burner.price(deployment.sell_token) == (
        START_TOTAL * WAD + staged - 1
    ) // staged

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
        deployment.burner.take(deployment.sell_token, staged, deployment.receiver, b"")


@given(
    elapsed=st.integers(min_value=0, max_value=EXCHANGE_DURATION - 1),
    amount_seed=st.integers(min_value=0, max_value=10_000 * WAD),
)
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_price_matches_high_precision_decimal_model(
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
        # RAY exponentiation rounds in the collector's favor. Its cumulative error must remain tiny.
        assert actual >= expected
        assert actual - expected <= max(2, expected // 10**20)


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
        deployment.burner.take(deployment.sell_token, amount, deployment.receiver, b"")

    assert deployment.burner.price(deployment.sell_token) == unit_price_before
    assert deployment.burner.lots(deployment.sell_token)[LOT_INITIAL_AMOUNT] == staged


def test_large_balance_quote_does_not_overflow(deployment: AuctionDeployment):
    huge = 2**192
    lot, staged = _activate_lot(deployment, deployment.sell_token, huge)
    assert staged > 2**191
    assert deployment.burner.getAmountNeeded(deployment.sell_token, staged) == START_TOTAL
    assert deployment.burner.getAmountNeeded(deployment.sell_token, staged - 1) <= START_TOTAL
    assert deployment.burner.price(deployment.sell_token) > 0
    assert lot[LOT_INITIAL_AMOUNT] == staged


def test_donation_and_rebase_do_not_expand_native_inventory_or_reduce_unit_price(
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
        deployment.burner.take(deployment.problem_token, amount, deployment.receiver, b"")

    remaining = staged - amount
    assert deployment.burner.lots(deployment.problem_token)[LOT_NATIVE_REMAINING] == remaining
    assert deployment.burner.available(deployment.problem_token) == remaining

    deployment.problem_token.set_balance(deployment.burner, remaining // 2)
    assert deployment.burner.available(deployment.problem_token) == remaining // 2
    deployment.problem_token.set_balance(deployment.burner, 10 * staged)
    assert deployment.burner.available(deployment.problem_token) == remaining
    assert deployment.burner.price(deployment.problem_token) == unit_price
    assert (
        deployment.burner.lots(deployment.problem_token)[LOT_INITIAL_AMOUNT]
        == lot[LOT_INITIAL_AMOUNT]
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
            deployment.sell_token, amount, deployment.receiver, b""
        )

    assert amount_taken == amount
    assert deployment.sell_token.balanceOf(deployment.receiver) == amount
    assert deployment.target.balanceOf(deployment.fee_collector) == payment
    assert deployment.target.balanceOf(deployment.burner) == 0
    assert deployment.burner.available(deployment.sell_token) == staged - amount

    second_payment = deployment.burner.getAmountNeeded(deployment.sell_token, staged - amount)
    deployment.target._mint_for_testing(deployment.buyer, second_payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, second_payment)
        second_taken = deployment.burner.take(
            deployment.sell_token, MAX_UINT256, deployment.receiver, b""
        )
    assert second_taken == staged - amount
    assert deployment.burner.available(deployment.sell_token) == 0


@pytest.mark.parametrize("payment_mode", [1, 2, 3, 4])
def test_yearn_callback_accepts_collector_burner_split_and_pull_payments(
    deployment: AuctionDeployment,
    payment_mode: int,
):
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

    callback_data = b"atomic unwind"
    amount_taken = taker.execute_take(
        deployment.sell_token, amount, taker.address, callback_data
    )

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


def test_yearn_callback_round_trips_full_8192_byte_data_bound(
    deployment: AuctionDeployment,
):
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
    taker.configure(1, False)

    callback_data = bytes(range(256)) * 32
    assert len(callback_data) == 8192
    amount_taken = taker.execute_take(
        deployment.sell_token, amount, taker.address, callback_data
    )

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
        deployment.burner.take(deployment.sell_token, 1, deployment.receiver, b"")

    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    with boa.env.prank(deployment.owner):
        deployment.fee_collector.set_killed(
            [(deployment.sell_token.address, Epoch.EXCHANGE)]
        )
    assert deployment.burner.available(deployment.sell_token) == 0
    with boa.env.prank(deployment.buyer), boa.reverts():
        deployment.burner.take(deployment.sell_token, staged, deployment.receiver, b"")


def test_active_exact_quote_rejects_amount_above_available_and_take_zero(
    deployment: AuctionDeployment,
):
    lot, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    with boa.reverts("Amount exceeds available"):
        deployment.burner.getAmountNeeded(deployment.sell_token, staged + 1)

    buyer_target_before = deployment.target.balanceOf(deployment.buyer)
    with boa.env.prank(deployment.buyer), boa.reverts("Nothing available"):
        deployment.burner.take(
            deployment.sell_token, 0, deployment.receiver, b""
        )

    current_lot = deployment.burner.lots(deployment.sell_token)
    assert current_lot[LOT_WEEK] == lot[LOT_WEEK]
    assert current_lot[LOT_NATIVE_REMAINING] == staged
    assert deployment.burner.available(deployment.sell_token) == staged
    assert deployment.sell_token.balanceOf(deployment.receiver) == 0
    assert deployment.target.balanceOf(deployment.buyer) == buyer_target_before
    assert deployment.target.balanceOf(deployment.fee_collector) == 0


def test_take_with_limits_enforces_deadline_week_amount_and_payment(
    deployment: AuctionDeployment,
):
    lot, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
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
        deployment.receiver,
        lot[LOT_WEEK],
        _timestamp() + 60,
        b"",
    )
    invalid_calls = [
        (*valid[:6], _timestamp() - 1, b""),
        (*valid[:5], lot[LOT_WEEK] + 1, valid[6], b""),
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


def test_expected_week_rejects_transaction_after_weekly_rollover(
    deployment: AuctionDeployment,
):
    first_lot, _ = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    _stage(deployment, deployment.sell_token, 1 * WAD)
    second_lot = deployment.burner.lots(deployment.sell_token)
    _move_to_timestamp(second_lot[LOT_START])

    amount = deployment.burner.available(deployment.sell_token)
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, amount)
    deployment.target._mint_for_testing(deployment.buyer, payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        with boa.reverts():
            deployment.burner.take_with_limits(
                deployment.sell_token,
                amount,
                1,
                payment,
                deployment.receiver,
                first_lot[LOT_WEEK],
                _timestamp() + 60,
                b"",
            )


def test_taken_event_captures_accounting_result(deployment: AuctionDeployment):
    lot, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    amount = staged // 2
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, amount)
    deployment.target._mint_for_testing(deployment.buyer, payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        deployment.burner.take(deployment.sell_token, amount, deployment.receiver, b"")

    taken = next(log for log in deployment.burner.get_logs() if _event_name(log) == "Taken")
    assert taken.address == deployment.burner.address
    assert taken.token == deployment.sell_token.address
    assert taken.week == lot[LOT_WEEK]
    assert taken.caller == deployment.buyer
    assert taken.receiver == deployment.receiver
    assert taken.amount_out == amount
    assert taken.payment == payment
    assert taken.remaining_balance == staged - amount


def test_cow_configuration_authority_and_initial_state(deployment: AuctionDeployment):
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.burner.configure_cow(
            deployment.composable_cow, deployment.retired_relayer
        )
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.burner.enable_cow()

    generation = _configure_and_enable_cow(deployment)
    assert generation == 1
    assert deployment.burner.cow_enabled()
    assert deployment.burner.composable_cow() == deployment.composable_cow.address
    assert deployment.burner.vault_relayer() == deployment.retired_relayer
    assert deployment.burner.supportsInterface(CONDITIONAL_ORDER_INTERFACE)
    assert deployment.burner.supportsInterface(ERC1271_MAGIC_VALUE)

    with boa.env.prank(deployment.owner), boa.reverts():
        deployment.burner.configure_cow(
            deployment.composable_cow, deployment.new_relayer
        )
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.burner.disable_cow()

    with boa.env.prank(deployment.emergency_owner):
        deployment.burner.disable_cow()
    assert not deployment.burner.cow_enabled()


def test_cow_lifecycle_events_and_owner_allowance_revoke(
    deployment: AuctionDeployment,
):
    with boa.env.prank(deployment.owner):
        deployment.burner.configure_cow(
            deployment.composable_cow, deployment.retired_relayer
        )
    configured = next(
        log
        for log in deployment.burner.get_logs()
        if _event_name(log) == "CowConfigured"
    )
    assert configured.address == deployment.burner.address
    assert configured.composable_cow == deployment.composable_cow.address
    assert configured.vault_relayer == deployment.retired_relayer
    assert configured.generation == 1

    with boa.env.prank(deployment.owner):
        deployment.burner.enable_cow()
    enabled = next(
        log
        for log in deployment.burner.get_logs()
        if _event_name(log) == "CowEnabled"
    )
    assert enabled.address == deployment.burner.address
    assert enabled.generation == 1

    staged, _ = _stage(deployment, deployment.sell_token, 100 * WAD)
    registered = next(
        log
        for log in deployment.fee_collector.get_logs()
        if _event_name(log) == "ConditionalOrderRegistered"
    )
    assert registered.address == deployment.burner.address
    assert registered.token == deployment.sell_token.address
    assert registered.generation == 1
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == staged
    )

    with boa.env.prank(deployment.owner):
        deployment.burner.disable_cow()
    disabled = next(
        log
        for log in deployment.burner.get_logs()
        if _event_name(log) == "CowDisabled"
    )
    assert disabled.address == deployment.burner.address
    assert disabled.generation == 1

    with boa.env.prank(deployment.owner):
        deployment.burner.configure_cow(
            deployment.composable_cow, deployment.new_relayer
        )
    reconfigured = next(
        log
        for log in deployment.burner.get_logs()
        if _event_name(log) == "CowConfigured"
    )
    assert reconfigured.composable_cow == deployment.composable_cow.address
    assert reconfigured.vault_relayer == deployment.new_relayer
    assert reconfigured.generation == 2

    with boa.env.prank(deployment.owner):
        deployment.burner.revoke_cow_allowances(
            [deployment.sell_token.address], deployment.retired_relayer
        )
    revoked = next(
        log
        for log in deployment.burner.get_logs()
        if _event_name(log) == "CowAllowanceRevoked"
    )
    assert revoked.address == deployment.burner.address
    assert revoked.token == deployment.sell_token.address
    assert revoked.relayer == deployment.retired_relayer
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == 0
    )


def test_first_collect_registers_generation_static_data_and_allowance_once(
    deployment: AuctionDeployment,
):
    generation = _configure_and_enable_cow(deployment)
    staged, _ = _stage(deployment, deployment.sell_token, 100 * WAD)

    assert deployment.composable_cow.create_count() == 1
    assert deployment.composable_cow.last_owner() == deployment.burner.address
    assert deployment.composable_cow.last_handler() == deployment.burner.address
    assert deployment.composable_cow.last_salt() == ZERO_BYTES32
    assert deployment.composable_cow.last_static_data() == _static_input(
        deployment.sell_token, generation
    )
    assert deployment.composable_cow.last_dispatch()
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == staged
    )

    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )
    assert deployment.composable_cow.create_count() == 1
    assert deployment.burner.lots(deployment.sell_token)[LOT_INITIAL_AMOUNT] == staged
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == staged
    )


def test_repeated_collect_refreshes_finite_budget_without_duplicate_create(
    deployment: AuctionDeployment,
):
    _configure_and_enable_cow(deployment)
    first_staged, _ = _stage(deployment, deployment.sell_token, 100 * WAD)
    assert deployment.composable_cow.create_count() == 1
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == first_staged
    )

    new_amount = 25 * WAD
    deployment.sell_token._mint_for_testing(deployment.fee_collector, new_amount)
    second_fee = new_amount * deployment.fee_collector.fee(Epoch.COLLECT) // WAD
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )

    refreshed_lot = deployment.burner.lots(deployment.sell_token)
    expected_budget = first_staged + new_amount - second_fee
    assert deployment.composable_cow.create_count() == 1
    assert refreshed_lot[LOT_INITIAL_AMOUNT] == expected_budget
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == expected_budget
    )


def test_finite_cow_budget_caps_uint256_max_lot_at_max_minus_one(
    deployment: AuctionDeployment,
):
    _configure_and_enable_cow(deployment)
    with boa.env.prank(deployment.owner):
        deployment.fee_collector.set_max_fee(Epoch.COLLECT, 0)
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    deployment.sell_token._mint_for_testing(
        deployment.fee_collector, MAX_UINT256
    )

    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )

    lot = deployment.burner.lots(deployment.sell_token)
    assert lot[LOT_INITIAL_AMOUNT] == MAX_UINT256
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == MAX_UINT256 - 1
    )
    _move_to_timestamp(lot[LOT_START])
    assert deployment.burner.available(deployment.sell_token) == MAX_UINT256 - 1


def test_tokens_staged_while_disabled_register_only_on_next_collect(
    deployment: AuctionDeployment,
):
    staged, _ = _stage(deployment, deployment.sell_token, 100 * WAD)
    assert deployment.composable_cow.create_count() == 0

    generation = _configure_and_enable_cow(deployment)
    assert deployment.composable_cow.create_count() == 0
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )

    assert deployment.composable_cow.create_count() == 1
    assert deployment.composable_cow.last_static_data() == _static_input(
        deployment.sell_token, generation
    )
    assert deployment.burner.lots(deployment.sell_token)[LOT_INITIAL_AMOUNT] == staged
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == staged
    )


def test_disable_enable_without_reconfiguration_does_not_duplicate_orders(
    deployment: AuctionDeployment,
):
    generation = _configure_and_enable_cow(deployment)
    _stage(deployment, deployment.sell_token, 100 * WAD)

    with boa.env.prank(deployment.owner):
        deployment.burner.disable_cow()
        deployment.burner.enable_cow()
    assert deployment.burner.cow_generation() == generation

    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )
    assert deployment.composable_cow.create_count() == 1


def test_reconfiguration_increments_generation_and_rejects_stale_orders(
    deployment: AuctionDeployment,
):
    old_generation = _configure_and_enable_cow(deployment)
    _stage(deployment, deployment.sell_token, 100 * WAD)
    lot = deployment.burner.lots(deployment.sell_token)
    _move_to_timestamp(lot[LOT_START])
    old_order = _tradeable_order(deployment, deployment.sell_token, old_generation)

    with boa.env.prank(deployment.owner):
        deployment.burner.disable_cow()
        deployment.burner.configure_cow(
            deployment.composable_cow, deployment.new_relayer
        )
        deployment.burner.enable_cow()
    new_generation = deployment.burner.cow_generation()
    assert new_generation == old_generation + 1

    with boa.reverts():
        deployment.burner.getTradeableOrder(
            deployment.burner.address,
            deployment.watcher,
            ZERO_BYTES32,
            _static_input(deployment.sell_token, old_generation),
            b"",
        )
    with boa.reverts():
        _verify_order(
            deployment,
            deployment.sell_token,
            old_order,
            generation=old_generation,
        )

    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )
    refreshed_lot = deployment.burner.lots(deployment.sell_token)
    assert deployment.composable_cow.create_count() == 2
    assert deployment.composable_cow.last_static_data() == _static_input(
        deployment.sell_token, new_generation
    )
    assert (
        deployment.sell_token.allowance(deployment.burner, deployment.new_relayer)
        == refreshed_lot[LOT_INITIAL_AMOUNT]
    )


def test_revoke_retired_allowance_authority(deployment: AuctionDeployment):
    _configure_and_enable_cow(deployment)
    staged, _ = _stage(deployment, deployment.sell_token, 100 * WAD)
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == staged
    )

    with boa.env.prank(deployment.owner):
        deployment.burner.disable_cow()
        deployment.burner.configure_cow(
            deployment.composable_cow, deployment.new_relayer
        )

    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.burner.revoke_cow_allowances(
            [deployment.sell_token.address], deployment.retired_relayer
        )
    with boa.env.prank(deployment.emergency_owner):
        deployment.burner.revoke_cow_allowances(
            [deployment.sell_token.address], deployment.retired_relayer
        )
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == 0
    )


def test_disabled_cow_handlers_revert_while_native_take_remains_live(
    deployment: AuctionDeployment,
):
    generation = _configure_and_enable_cow(deployment)
    lot, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    order = _tradeable_order(deployment, deployment.sell_token, generation)
    signature = _encode_erc1271_signature(
        order, deployment.burner, _static_input(deployment.sell_token, generation)
    )
    order_hash = _gpv2_order_digest(
        order, deployment.composable_cow.domainSeparator()
    )
    with boa.env.prank(deployment.emergency_owner):
        deployment.burner.disable_cow()

    with boa.reverts():
        _tradeable_order(deployment, deployment.sell_token, generation)
    with boa.reverts():
        _verify_order(deployment, deployment.sell_token, order, generation=generation)
    with boa.reverts():
        deployment.burner.isValidSignature(order_hash, signature)
    assert not deployment.burner.supportsInterface(CONDITIONAL_ORDER_INTERFACE)
    assert not deployment.burner.supportsInterface(ERC1271_MAGIC_VALUE)

    payment = deployment.burner.getAmountNeeded(deployment.sell_token, staged)
    deployment.target._mint_for_testing(deployment.buyer, payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        deployment.burner.take_with_limits(
            deployment.sell_token,
            staged,
            staged,
            payment,
            deployment.receiver,
            lot[LOT_WEEK],
            _timestamp(),
            b"",
        )


def test_tradeable_order_fields_quote_and_validity_buckets(deployment: AuctionDeployment):
    generation = _configure_and_enable_cow(deployment)
    _stage(deployment, deployment.sell_token, 100 * WAD)
    lot = deployment.burner.lots(deployment.sell_token)
    bucket_start = (
        (lot[LOT_START] + COW_ORDER_VALIDITY - 1) // COW_ORDER_VALIDITY
    ) * COW_ORDER_VALIDITY
    _move_to_timestamp(bucket_start)

    order = _tradeable_order(deployment, deployment.sell_token, generation)
    available = deployment.burner.available(deployment.sell_token)
    assert order[ORDER_SELL_TOKEN] == deployment.sell_token.address
    assert order[ORDER_BUY_TOKEN] == deployment.target.address
    assert order[ORDER_RECEIVER] == deployment.fee_collector.address
    assert order[ORDER_SELL_AMOUNT] == available
    assert order[ORDER_BUY_AMOUNT] == deployment.burner.getAmountNeeded(
        deployment.sell_token, available
    )
    assert order[ORDER_VALID_TO] == min(
        bucket_start + COW_ORDER_VALIDITY, lot[LOT_END]
    )
    assert order[ORDER_APP_DATA] == APP_DATA
    assert order[ORDER_FEE_AMOUNT] == 0
    assert order[ORDER_KIND] == SELL_KIND
    assert order[ORDER_PARTIALLY_FILLABLE]
    assert order[ORDER_SELL_BALANCE] == ERC20_BALANCE
    assert order[ORDER_BUY_BALANCE] == ERC20_BALANCE

    _move_to_timestamp(bucket_start + COW_ORDER_VALIDITY - 1)
    assert _tradeable_order(deployment, deployment.sell_token, generation) == order
    _move_to_timestamp(bucket_start + COW_ORDER_VALIDITY)
    next_order = _tradeable_order(deployment, deployment.sell_token, generation)
    assert next_order[ORDER_BUY_AMOUNT] <= order[ORDER_BUY_AMOUNT]
    assert next_order[ORDER_VALID_TO] > order[ORDER_VALID_TO]


def test_cow_pull_then_donation_limits_native_to_unspent_lot_budget(
    deployment: AuctionDeployment,
):
    generation = _configure_and_enable_cow(deployment)
    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    cow_amount = staged * 40 // 100
    native_budget = staged - cow_amount

    with boa.env.prank(deployment.retired_relayer):
        deployment.sell_token.transferFrom(
            deployment.burner, deployment.receiver, cow_amount
        )
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == native_budget
    )

    deployment.sell_token._mint_for_testing(deployment.burner, cow_amount)
    assert deployment.sell_token.balanceOf(deployment.burner) == staged
    assert deployment.burner.available(deployment.sell_token) == native_budget

    payment = deployment.burner.getAmountNeeded(
        deployment.sell_token, native_budget
    )
    deployment.target._mint_for_testing(deployment.buyer, payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        native_amount = deployment.burner.take(
            deployment.sell_token,
            MAX_UINT256,
            deployment.receiver,
            b"",
        )

    assert native_amount == native_budget
    assert cow_amount + native_amount == staged
    assert deployment.sell_token.balanceOf(deployment.receiver) == staged
    assert deployment.sell_token.balanceOf(deployment.burner) == cow_amount
    assert deployment.burner.available(deployment.sell_token) == 0
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == 0
    )
    with boa.reverts():
        _tradeable_order(deployment, deployment.sell_token, generation)


def test_native_fill_then_donation_limits_cow_to_unspent_lot_budget(
    deployment: AuctionDeployment,
):
    generation = _configure_and_enable_cow(deployment)
    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    native_amount = staged * 40 // 100
    cow_budget = staged - native_amount
    payment = deployment.burner.getAmountNeeded(
        deployment.sell_token, native_amount
    )
    deployment.target._mint_for_testing(deployment.buyer, payment)

    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        deployment.burner.take(
            deployment.sell_token,
            native_amount,
            deployment.receiver,
            b"",
        )
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == cow_budget
    )

    deployment.sell_token._mint_for_testing(deployment.burner, native_amount)
    order = _tradeable_order(deployment, deployment.sell_token, generation)
    assert order[ORDER_SELL_AMOUNT] == cow_budget

    with boa.env.prank(deployment.retired_relayer):
        deployment.sell_token.transferFrom(
            deployment.burner, deployment.watcher, order[ORDER_SELL_AMOUNT]
        )

    cow_amount = deployment.sell_token.balanceOf(deployment.watcher)
    assert native_amount + cow_amount == staged
    assert deployment.sell_token.balanceOf(deployment.receiver) == native_amount
    assert deployment.sell_token.balanceOf(deployment.burner) == native_amount
    assert deployment.burner.available(deployment.sell_token) == 0
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == 0
    )
    with boa.reverts():
        _tradeable_order(deployment, deployment.sell_token, generation)


def test_tradeable_order_rejects_zero_unsynced_outside_killed_and_bad_inputs(
    deployment: AuctionDeployment,
):
    generation = _configure_and_enable_cow(deployment)
    with boa.reverts():
        _tradeable_order(deployment, deployment.sell_token, generation)

    _stage(deployment, deployment.sell_token, 100 * WAD)
    lot = deployment.burner.lots(deployment.sell_token)
    with boa.reverts():
        _tradeable_order(deployment, deployment.sell_token, generation)

    _move_to_timestamp(lot[LOT_START])
    with boa.reverts():
        deployment.burner.getTradeableOrder(
            deployment.burner.address,
            deployment.watcher,
            ZERO_BYTES32,
            _static_input(deployment.sell_token, generation)[:-1],
            b"",
        )
    with boa.reverts():
        _tradeable_order(deployment, deployment.sell_token, generation, b"unexpected")

    with boa.env.prank(deployment.owner):
        deployment.fee_collector.set_killed(
            [(deployment.sell_token.address, Epoch.EXCHANGE)]
        )
    with boa.reverts():
        _tradeable_order(deployment, deployment.sell_token, generation)


def test_verify_rejects_every_security_relevant_gpv2_field(deployment: AuctionDeployment):
    generation = _configure_and_enable_cow(deployment)
    lot, _ = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    bucket_start = (
        (_timestamp() + COW_ORDER_VALIDITY - 1) // COW_ORDER_VALIDITY
    ) * COW_ORDER_VALIDITY
    if bucket_start < lot[LOT_END]:
        _move_to_timestamp(bucket_start)
    order = list(_tradeable_order(deployment, deployment.sell_token, generation))
    _verify_order(deployment, deployment.sell_token, order)

    invalid_orders: list[list[Any]] = []
    mutations = {
        ORDER_SELL_TOKEN: deployment.second_token.address,
        ORDER_BUY_TOKEN: deployment.second_token.address,
        ORDER_RECEIVER: deployment.receiver,
        ORDER_SELL_AMOUNT: lot[LOT_INITIAL_AMOUNT] + 1,
        ORDER_BUY_AMOUNT: max(0, order[ORDER_BUY_AMOUNT] - 1),
        ORDER_VALID_TO: lot[LOT_END] + 1,
        ORDER_APP_DATA: bytes.fromhex("11" * 32),
        ORDER_FEE_AMOUNT: 1,
        ORDER_KIND: keccak(text="buy"),
        ORDER_PARTIALLY_FILLABLE: False,
        ORDER_SELL_BALANCE: keccak(text="external"),
        ORDER_BUY_BALANCE: keccak(text="internal"),
    }
    for index, invalid_value in mutations.items():
        invalid = deepcopy(order)
        invalid[index] = invalid_value
        invalid_orders.append(invalid)

    for invalid_order in invalid_orders:
        with boa.reverts():
            _verify_order(deployment, deployment.sell_token, invalid_order)

    with boa.reverts():
        _verify_order(
            deployment,
            deployment.sell_token,
            order,
            static_input=_static_input(deployment.second_token, generation),
        )
    with boa.reverts():
        _verify_order(
            deployment,
            deployment.sell_token,
            order,
            offchain_input=b"unexpected",
        )


def test_erc1271_valid_payload_magic_invalid_payload_and_stale_generation(
    deployment: AuctionDeployment,
):
    generation = _configure_and_enable_cow(deployment)
    lot, _ = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    order = _tradeable_order(deployment, deployment.sell_token, generation)
    signature = _encode_erc1271_signature(
        order, deployment.burner, _static_input(deployment.sell_token, generation)
    )
    order_hash = _gpv2_order_digest(
        order, deployment.composable_cow.domainSeparator()
    )

    assert (
        deployment.burner.isValidSignature(order_hash, signature)
        == ERC1271_MAGIC_VALUE
    )
    with boa.reverts():
        deployment.burner.isValidSignature(keccak(b"order"), b"malformed")
    with boa.reverts():
        deployment.burner.supportsInterface(SIGNATURE_VERIFIER_MUXER_INTERFACE)

    with boa.env.prank(deployment.owner):
        deployment.burner.disable_cow()
        deployment.burner.configure_cow(
            deployment.composable_cow, deployment.new_relayer
        )
        deployment.burner.enable_cow()
    assert deployment.burner.cow_generation() == generation + 1
    with boa.reverts():
        deployment.burner.isValidSignature(order_hash, signature)
    assert lot[LOT_WEEK] != 0


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
            deployment.receiver,
            lot[LOT_WEEK],
            _timestamp(),
            b"",
        )
    assert deployment.no_return_token.balanceOf(deployment.receiver) == staged


def test_false_return_balance_revert_and_blacklist_fail_atomically(
    deployment: AuctionDeployment,
):
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)

    deployment.problem_token.set_returns_false(True)
    deployment.problem_token.mint(deployment.fee_collector, 100 * WAD)
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.fee_collector.collect(
            [deployment.problem_token.address], deployment.keeper
        )
    assert deployment.burner.lots(deployment.problem_token)[LOT_INITIAL_AMOUNT] == 0

    deployment.problem_token.set_returns_false(False)
    deployment.problem_token.set_revert_balance_of(True)
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.fee_collector.collect(
            [deployment.problem_token.address], deployment.keeper
        )
    assert deployment.burner.lots(deployment.problem_token)[LOT_INITIAL_AMOUNT] == 0

    deployment.problem_token.set_revert_balance_of(False)
    deployment.problem_token.set_blacklisted(deployment.burner.address, True)
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.fee_collector.collect(
            [deployment.problem_token.address], deployment.keeper
        )
    assert deployment.burner.lots(deployment.problem_token)[LOT_INITIAL_AMOUNT] == 0


def test_usdt_approval_reset_is_supported(deployment: AuctionDeployment):
    _configure_and_enable_cow(deployment)
    deployment.problem_token.set_requires_approval_reset(True)
    deployment.problem_token.set_allowance_for_testing(
        deployment.burner, deployment.retired_relayer, 1
    )

    staged, _ = _stage_problem_token(deployment, deployment.problem_token, 100 * WAD)
    assert (
        deployment.problem_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == staged
    )


def test_nonzero_budget_approval_failure_rolls_back_reset_registration_and_lot(
    deployment: AuctionDeployment,
):
    _configure_and_enable_cow(deployment)
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    amount = 100 * WAD
    deployment.problem_token.mint(deployment.fee_collector, amount)
    deployment.problem_token.set_requires_approval_reset(True)
    deployment.problem_token.set_allowance_for_testing(
        deployment.burner, deployment.retired_relayer, 1
    )
    deployment.problem_token.set_fails_nonzero_approval(True)

    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.fee_collector.collect(
            [deployment.problem_token.address], deployment.keeper
        )

    assert deployment.composable_cow.create_count() == 0
    assert not deployment.burner.created(deployment.problem_token)
    assert deployment.burner.lots(deployment.problem_token)[LOT_INITIAL_AMOUNT] == 0
    assert deployment.problem_token.balanceOf(deployment.fee_collector) == amount
    assert (
        deployment.problem_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == 1
    )

    deployment.problem_token.set_fails_nonzero_approval(False)
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect(
            [deployment.problem_token.address], deployment.keeper
        )
    assert deployment.burner.created(deployment.problem_token)
    lot = deployment.burner.lots(deployment.problem_token)
    assert (
        deployment.problem_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == lot[LOT_INITIAL_AMOUNT]
    )


def test_no_return_token_retired_relayer_allowance_can_be_revoked(
    deployment: AuctionDeployment,
):
    _configure_and_enable_cow(deployment)
    staged, _ = _stage(deployment, deployment.no_return_token, 100 * 10**8)
    assert (
        deployment.no_return_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == staged
    )

    with boa.env.prank(deployment.owner):
        deployment.burner.disable_cow()
        deployment.burner.configure_cow(
            deployment.composable_cow, deployment.new_relayer
        )
    with boa.env.prank(deployment.emergency_owner):
        deployment.burner.revoke_cow_allowances(
            [deployment.no_return_token.address], deployment.retired_relayer
        )
    assert (
        deployment.no_return_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == 0
    )


def test_fee_on_transfer_snapshot_uses_actual_custody(deployment: AuctionDeployment):
    deployment.problem_token.set_fee_bps(100)
    nominal = 1_000 * WAD
    _stage_problem_token(deployment, deployment.problem_token, nominal)
    actual = deployment.problem_token.balanceOf(deployment.burner)
    lot = deployment.burner.lots(deployment.problem_token)

    assert actual < nominal
    assert lot[LOT_INITIAL_AMOUNT] == actual
    assert lot[LOT_NATIVE_REMAINING] == actual
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
        _take_calldata(deployment.problem_token, amount, deployment.receiver),
        True,
    )

    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        with boa.reverts():
            deployment.burner.take(
                deployment.problem_token, amount, deployment.receiver, b""
            )

    assert deployment.problem_token.balanceOf(deployment.receiver) == 0
    assert deployment.burner.available(deployment.problem_token) == staged
    assert deployment.target.balanceOf(deployment.fee_collector) == 0


def test_recover_cancels_active_registered_lot_until_next_weekly_collect(
    deployment: AuctionDeployment,
):
    generation = _configure_and_enable_cow(deployment)
    lot, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    order = _tradeable_order(deployment, deployment.sell_token, generation)
    static_input = _static_input(deployment.sell_token, generation)
    signature = _encode_erc1271_signature(
        order, deployment.burner, static_input
    )
    order_hash = _gpv2_order_digest(
        order, deployment.composable_cow.domainSeparator()
    )

    with boa.env.prank(deployment.owner):
        deployment.burner.recover([deployment.sell_token.address])
    recovered = next(
        log
        for log in deployment.burner.get_logs()
        if _event_name(log) == "Recovered"
    )
    assert recovered.address == deployment.burner.address
    assert recovered.token == deployment.sell_token.address
    assert recovered.amount == staged

    cancelled_lot = deployment.burner.lots(deployment.sell_token)
    assert deployment.burner.cancelled_week(deployment.sell_token) == lot[LOT_WEEK]
    assert cancelled_lot[LOT_NATIVE_REMAINING] == 0
    assert deployment.sell_token.balanceOf(deployment.burner) == 0
    assert deployment.sell_token.balanceOf(deployment.fee_collector) == staged
    assert deployment.burner.available(deployment.sell_token) == 0
    assert deployment.burner.price(deployment.sell_token) == 0
    assert deployment.burner.getAmountNeeded(deployment.sell_token, 1) == 0

    with boa.env.prank(deployment.buyer), boa.reverts():
        deployment.burner.take(
            deployment.sell_token, 1, deployment.receiver, b""
        )
    with boa.reverts():
        _tradeable_order(deployment, deployment.sell_token, generation)
    with boa.reverts():
        _verify_order(
            deployment, deployment.sell_token, order, generation=generation
        )
    with boa.reverts():
        deployment.burner.isValidSignature(order_hash, signature)

    donation = 10 * WAD
    deployment.sell_token._mint_for_testing(deployment.burner, donation)
    assert deployment.burner.available(deployment.sell_token) == 0
    assert deployment.burner.price(deployment.sell_token) == 0
    assert deployment.burner.getAmountNeeded(deployment.sell_token, 1) == 0
    with boa.reverts():
        _tradeable_order(deployment, deployment.sell_token, generation)
    with boa.reverts():
        _verify_order(
            deployment, deployment.sell_token, order, generation=generation
        )
    with boa.reverts():
        deployment.burner.isValidSignature(order_hash, signature)

    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    collector_balance = deployment.sell_token.balanceOf(deployment.fee_collector)
    collect_fee = (
        collector_balance * deployment.fee_collector.fee(Epoch.COLLECT) // WAD
    )
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )

    refreshed_lot = deployment.burner.lots(deployment.sell_token)
    expected_snapshot = donation + collector_balance - collect_fee
    assert refreshed_lot[LOT_WEEK] == lot[LOT_WEEK] + 1
    assert refreshed_lot[LOT_INITIAL_AMOUNT] == expected_snapshot
    assert refreshed_lot[LOT_NATIVE_REMAINING] == expected_snapshot
    assert deployment.burner.cancelled_week(deployment.sell_token) == 0
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == expected_snapshot
    )

    _move_to_timestamp(refreshed_lot[LOT_START])
    assert deployment.burner.available(deployment.sell_token) == expected_snapshot
    assert deployment.burner.price(deployment.sell_token) > 0
    refreshed_order = _tradeable_order(
        deployment, deployment.sell_token, generation
    )
    assert refreshed_order[ORDER_SELL_AMOUNT] == expected_snapshot


def test_collect_after_collect_epoch_recovery_cannot_bypass_cancellation(
    deployment: AuctionDeployment,
):
    generation = _configure_and_enable_cow(deployment)
    staged, _ = _stage(deployment, deployment.sell_token, 100 * WAD)
    first_lot = deployment.burner.lots(deployment.sell_token)
    assert deployment.burner.created(deployment.sell_token)
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == staged
    )

    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    recovery_week = deployment.burner.current_week()
    assert recovery_week == first_lot[LOT_WEEK] + 1
    with boa.env.prank(deployment.owner):
        deployment.burner.recover([deployment.sell_token.address])

    cancelled_lot = deployment.burner.lots(deployment.sell_token)
    assert deployment.burner.cancelled_week(deployment.sell_token) == recovery_week
    assert cancelled_lot[LOT_NATIVE_REMAINING] == 0
    assert deployment.sell_token.balanceOf(deployment.burner) == 0
    assert deployment.sell_token.balanceOf(deployment.fee_collector) == staged

    new_receipts = 25 * WAD
    deployment.sell_token._mint_for_testing(
        deployment.fee_collector, new_receipts
    )
    collector_before = deployment.sell_token.balanceOf(deployment.fee_collector)
    keeper_before = deployment.sell_token.balanceOf(deployment.keeper)
    burner_before = deployment.sell_token.balanceOf(deployment.burner)
    allowance_before = deployment.sell_token.allowance(
        deployment.burner, deployment.retired_relayer
    )
    lot_before = deployment.burner.lots(deployment.sell_token)

    with boa.env.prank(deployment.keeper), boa.reverts("Lot cancelled"):
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )

    assert deployment.burner.cancelled_week(deployment.sell_token) == recovery_week
    assert deployment.burner.lots(deployment.sell_token) == lot_before
    assert deployment.burner.lots(deployment.sell_token)[LOT_NATIVE_REMAINING] == 0
    assert deployment.sell_token.balanceOf(deployment.fee_collector) == collector_before
    assert deployment.sell_token.balanceOf(deployment.keeper) == keeper_before
    assert deployment.sell_token.balanceOf(deployment.burner) == burner_before
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == allowance_before
    )
    assert deployment.composable_cow.create_count() == 1

    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    next_week = deployment.burner.current_week()
    assert next_week == recovery_week + 1
    fee = collector_before * deployment.fee_collector.fee(Epoch.COLLECT) // WAD
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )

    refreshed_lot = deployment.burner.lots(deployment.sell_token)
    expected_snapshot = collector_before - fee
    assert refreshed_lot[LOT_WEEK] == next_week
    assert refreshed_lot[LOT_INITIAL_AMOUNT] == expected_snapshot
    assert refreshed_lot[LOT_NATIVE_REMAINING] == expected_snapshot
    assert deployment.burner.cancelled_week(deployment.sell_token) == 0
    assert deployment.sell_token.balanceOf(deployment.fee_collector) == 0
    assert deployment.sell_token.balanceOf(deployment.burner) == expected_snapshot
    assert deployment.sell_token.balanceOf(deployment.keeper) == keeper_before + fee
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == expected_snapshot
    )
    assert deployment.composable_cow.create_count() == 1

    _move_to_timestamp(refreshed_lot[LOT_START])
    assert deployment.burner.available(deployment.sell_token) == expected_snapshot
    refreshed_order = _tradeable_order(
        deployment, deployment.sell_token, generation
    )
    assert refreshed_order[ORDER_SELL_AMOUNT] == expected_snapshot


def test_recover_before_first_staging_blocks_same_week_collect(
    deployment: AuctionDeployment,
):
    generation = _configure_and_enable_cow(deployment)
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    recovery_week = deployment.burner.current_week()
    recovered_amount = 10 * WAD
    deployment.sell_token._mint_for_testing(
        deployment.burner, recovered_amount
    )

    with boa.env.prank(deployment.owner):
        deployment.burner.recover([deployment.sell_token.address])

    assert deployment.burner.cancelled_week(deployment.sell_token) == recovery_week
    assert deployment.burner.lots(deployment.sell_token)[LOT_WEEK] == 0
    assert deployment.burner.lots(deployment.sell_token)[LOT_NATIVE_REMAINING] == 0
    assert not deployment.burner.created(deployment.sell_token)
    assert deployment.composable_cow.create_count() == 0
    assert deployment.sell_token.balanceOf(deployment.burner) == 0
    assert (
        deployment.sell_token.balanceOf(deployment.fee_collector)
        == recovered_amount
    )
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == 0
    )

    keeper_before = deployment.sell_token.balanceOf(deployment.keeper)
    with boa.env.prank(deployment.keeper), boa.reverts("Lot cancelled"):
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )
    assert deployment.burner.cancelled_week(deployment.sell_token) == recovery_week
    assert deployment.burner.lots(deployment.sell_token)[LOT_WEEK] == 0
    assert deployment.burner.lots(deployment.sell_token)[LOT_NATIVE_REMAINING] == 0
    assert deployment.sell_token.balanceOf(deployment.keeper) == keeper_before
    assert (
        deployment.sell_token.balanceOf(deployment.fee_collector)
        == recovered_amount
    )
    assert deployment.sell_token.balanceOf(deployment.burner) == 0
    assert deployment.composable_cow.create_count() == 0

    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    next_week = deployment.burner.current_week()
    fee = recovered_amount * deployment.fee_collector.fee(Epoch.COLLECT) // WAD
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )

    refreshed_lot = deployment.burner.lots(deployment.sell_token)
    expected_snapshot = recovered_amount - fee
    assert next_week == recovery_week + 1
    assert refreshed_lot[LOT_WEEK] == next_week
    assert refreshed_lot[LOT_INITIAL_AMOUNT] == expected_snapshot
    assert refreshed_lot[LOT_NATIVE_REMAINING] == expected_snapshot
    assert deployment.burner.cancelled_week(deployment.sell_token) == 0
    assert deployment.burner.created(deployment.sell_token)
    assert deployment.composable_cow.create_count() == 1
    assert (
        deployment.sell_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == expected_snapshot
    )
    _move_to_timestamp(refreshed_lot[LOT_START])
    assert _tradeable_order(
        deployment, deployment.sell_token, generation
    )[ORDER_SELL_AMOUNT] == expected_snapshot


def test_push_target_and_recovery_only_return_assets_to_fee_collector(
    deployment: AuctionDeployment,
):
    deployment.target._mint_for_testing(deployment.burner, 10 * WAD)
    with boa.env.prank(deployment.keeper):
        assert deployment.burner.push_target() == 10 * WAD
    assert deployment.target.balanceOf(deployment.fee_collector) == 10 * WAD

    deployment.sell_token._mint_for_testing(deployment.burner, 5 * WAD)
    boa.env.set_balance(deployment.burner.address, WAD)
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.burner.recover([deployment.sell_token.address, ETH_ADDRESS])
    with boa.env.prank(deployment.emergency_owner):
        deployment.burner.recover([deployment.sell_token.address, ETH_ADDRESS])
    assert deployment.sell_token.balanceOf(deployment.fee_collector) == 5 * WAD
    assert boa.env.get_balance(deployment.fee_collector.address) == WAD
    assert deployment.sell_token.balanceOf(deployment.emergency_owner) == 0
