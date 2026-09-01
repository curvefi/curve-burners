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

from ..conftest import ETH_ADDRESS, ZERO_ADDRESS, Epoch, WEEK

from .conftest import custom_err


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
ERC1271_INVALID = bytes.fromhex("ffffffff")

ERC165_INTERFACE = bytes.fromhex("01ffc9a7")
BURNER_INTERFACE = bytes.fromhex("a3b5e311")
CONDITIONAL_ORDER_INTERFACE = bytes.fromhex("b8296fc4")
SIGNATURE_VERIFIER_MUXER_INTERFACE = bytes.fromhex("62af8dc2")

# Lot tuple fields fixed by the integration ABI.
LOT_EPOCH = 0
LOT_INITIAL_AMOUNT = 1
LOT_START = 2
LOT_END = 3

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
    settlement: Any
    new_settlement: Any
    handler: Any
    registry: Any
    cow_adapter: Any
    new_cow_adapter: Any
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
    """Lot record extended with its epoch window: the contract stores no time
    bounds, so LOT_START/LOT_END index into epoch_bounds(lot.epoch) here."""
    lot = deployment.burner.lots(token)
    start, end = deployment.burner.epoch_bounds(lot[LOT_EPOCH])
    return (*lot, start, end)


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
    lot = _lot_with_bounds(deployment, token)
    _move_to_timestamp(lot[LOT_START])
    assert deployment.fee_collector.epoch() == Epoch.EXCHANGE
    return lot, staged


def _configure_and_enable_cow(
    deployment: AuctionDeployment,
    *,
    adapter: Any | None = None,
) -> int:
    adapter = adapter or deployment.cow_adapter
    with boa.env.prank(deployment.owner):
        deployment.burner.enable_adapter(adapter)
        deployment.burner.set_fallback_adapter(adapter)
        deployment.burner.configure_watchtower(
            deployment.composable_cow, deployment.handler
        )
    return deployment.burner.cow_generation()


def _tradeable_order(
    deployment: AuctionDeployment,
    token: Any,
    generation: int | None = None,
    offchain_input: bytes = b"",
) -> Any:
    generation = deployment.burner.cow_generation() if generation is None else generation
    return deployment.handler.getTradeableOrder(
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
    deployment.handler.verify(
        deployment.burner.address,
        deployment.watcher,
        order_hash,
        domain_separator,
        ZERO_BYTES32,
        static_input,
        offchain_input,
        order,
    )


def _ray_mul(a: int, b: int) -> int:
    return (a * b + RAY // 2) // RAY


def _ray_pow(base_ray: int, exponent: int) -> int:
    result = RAY
    factor = base_ray
    while exponent:
        if exponent & 1:
            result = _ray_mul(result, factor)
        exponent >>= 1
        if exponent:
            factor = _ray_mul(factor, factor)
    return result


# Bit-for-bit mirror of auction_math.total_price.
def _reference_total(
    lot: Any, timestamp: int, start_total: int = START_TOTAL, floor_total: int = FLOOR_TOTAL
) -> int:
    steps = (timestamp - lot[LOT_START]) // STEP_DURATION
    return max(floor_total, start_total * _ray_pow(DECAY_FACTOR_RAY, steps) // RAY)


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
    domain_separator = composable_cow.domainSeparator()
    settlement = boa.load(
        "contracts/testing/dutch_auction/SettlementMock.vy", domain_separator, retired_relayer
    )
    new_settlement = boa.load(
        "contracts/testing/dutch_auction/SettlementMock.vy", domain_separator, new_relayer
    )
    handler = boa.load("contracts/burners/cow/WatchtowerHandler.vy")
    registry = boa.load("contracts/burners/auction/adapters/AdapterRegistry.vy", fee_collector.address)
    cow_adapter = boa.load(
        "contracts/burners/cow/CowAdapter.vy", settlement, APP_DATA, COW_ORDER_VALIDITY
    )
    new_cow_adapter = boa.load(
        "contracts/burners/cow/CowAdapter.vy", new_settlement, APP_DATA, COW_ORDER_VALIDITY
    )

    burner = burner_deployer.deploy(
        fee_collector,
        START_TOTAL,
        FLOOR_TOTAL,
        DECAY_FACTOR_RAY,
        STEP_DURATION,
        registry,
    )
    with boa.env.prank(owner):
        fee_collector.set_burner(burner)
        fee_collector.set_killed([(ZERO_ADDRESS, 0)])
        registry.set_adapter(cow_adapter, cow_adapter.vault_relayer())
        registry.activate_adapter(cow_adapter)
        registry.set_adapter(new_cow_adapter, new_cow_adapter.vault_relayer())
        registry.activate_adapter(new_cow_adapter)

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
        settlement,
        new_settlement,
        handler,
        registry,
        cow_adapter,
        new_cow_adapter,
        burner,
    )


def test_constructor_and_fixed_interfaces(deployment: AuctionDeployment):
    burner = deployment.burner

    assert burner.VERSION() == "DutchAuction"
    assert burner.want() == deployment.target.address
    assert burner.target() == deployment.target.address
    assert burner.registry() == deployment.registry.address
    assert burner.supportsInterface(ERC165_INTERFACE)
    assert burner.supportsInterface(BURNER_INTERFACE)
    assert not burner.supportsInterface(CONDITIONAL_ORDER_INTERFACE)
    # The adapter dispatcher is always live, so ERC-1271 is claimed unconditionally.
    assert burner.supportsInterface(ERC1271_MAGIC_VALUE)

    assert not burner.cow_enabled()
    assert burner.composable_cow() == ZERO_ADDRESS
    assert burner.fallback_adapter() == ZERO_ADDRESS
    assert burner.cow_generation() == 0

    signatures = _function_signatures(burner)
    assert {
        "want()",
        "available(address)",
        "price(address)",
        "getAmountNeeded(address,uint256)",
        "take(address,uint256,address,bytes)",
        "take_with_limits(address,uint256,uint256,uint256,address,uint256,bytes)",
        "quote(address,uint256)",
        "isValidSignature(bytes32,bytes)",
        "check_order(address,address,address,uint256,uint256,uint256)",
        "sync_executor_approvals(address,address[])",
        "enable_adapter(address)",
        "disable_adapter(address)",
        "set_fallback_adapter(address)",
        "enabled_adapters(address)",
        "fallback_adapter()",
        "executor_refcount(address)",
        "registry()",
        "target()",
        "current_epoch()",
    } <= signatures
    # Retired finite-budget surface must be gone from the ABI.
    assert "revoke_cow_allowances(address[],address)" not in signatures
    # CoW execution left the burner for the fallback CowAdapter.
    assert "configure_cow(address,address,address)" not in signatures
    assert "enable_cow()" not in signatures
    assert "settlement()" not in signatures
    assert "vault_relayer()" not in signatures
    assert "retired_relayer(address)" not in signatures
    assert "MAX_COW_BUDGET()" not in signatures
    # The core is epoch-based; the weekly naming must be gone from the ABI.
    assert "current_week()" not in signatures
    assert "cancelled_week(address)" not in signatures
    # Cancellation retired: emergency is recover + FeeCollector.set_killed.
    assert "cancelled_epoch(address)" not in signatures


def test_yearn_auction_abi_is_exact(deployment: AuctionDeployment):
    """ABI-conformance for the Yearn-compatible surface (IDutchAuction.vyi).

    The quote views carry an optional evaluation timestamp, so Vyper exports
    two selectors per view: the Yearn-compatible base arity and the _ts
    variant (the three-arg getAmountNeeded matches Yearn's timestamp
    overload by ABI).
    """
    functions = [
        item
        for item in deployment.burner.abi
        if item["type"] == "function"
        and item["name"] in {"want", "available", "price", "getAmountNeeded", "take"}
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
        ("available", (("_from", "address"),), ("uint256",), "view"),
        ("available", (("_from", "address"), ("_ts", "uint256")), ("uint256",), "view"),
        ("price", (("_from", "address"),), ("uint256",), "view"),
        ("price", (("_from", "address"), ("_ts", "uint256")), ("uint256",), "view"),
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
    "start_total,floor_total,decay_factor,step_duration",
    [
        (0, FLOOR_TOTAL, DECAY_FACTOR_RAY, STEP_DURATION),
        (START_TOTAL, 0, DECAY_FACTOR_RAY, STEP_DURATION),
        (START_TOTAL, START_TOTAL + 1, DECAY_FACTOR_RAY, STEP_DURATION),
        (START_TOTAL, FLOOR_TOTAL, 0, STEP_DURATION),
        # Barely-decaying curve misses the floor within the frame.
        (START_TOTAL, FLOOR_TOTAL, RAY - 1, STEP_DURATION),
        (START_TOTAL, FLOOR_TOTAL, RAY + 1, STEP_DURATION),
        (START_TOTAL, FLOOR_TOTAL, DECAY_FACTOR_RAY, 0),
    ],
)
def test_constructor_rejects_invalid_curve_parameters(
    burner_deployer: Any,
    deployment: AuctionDeployment,
    start_total: int,
    floor_total: int,
    decay_factor: int,
    step_duration: int,
):
    with boa.reverts():
        burner_deployer.deploy(
            deployment.fee_collector,
            start_total,
            floor_total,
            decay_factor,
            step_duration,
            ZERO_ADDRESS,
        )


def test_only_fee_collector_can_burn_and_target_is_rejected(deployment: AuctionDeployment):
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.burner.burn([deployment.sell_token.address], deployment.keeper)

    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    deployment.target._mint_for_testing(deployment.fee_collector, WAD)
    with boa.env.prank(deployment.fee_collector.address), boa.reverts():
        deployment.burner.burn([deployment.target.address], deployment.keeper)

    assert _lot_with_bounds(deployment, deployment.target)[LOT_INITIAL_AMOUNT] == 0


def test_collect_pays_fee_moves_custody_and_snapshots_lot(deployment: AuctionDeployment):
    amount = 1_000 * WAD
    staged, fee = _stage(deployment, deployment.sell_token, amount)
    lot_synced = next(
        log
        for log in deployment.fee_collector.get_logs()
        if _event_name(log) == "LotStaged"
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
    assert lot[LOT_EPOCH] == lot[LOT_START]
    assert _timestamp() < lot[LOT_START]

    assert lot_synced.address == deployment.burner.address
    assert lot_synced.token == deployment.sell_token.address
    assert lot_synced.epoch == lot[LOT_EPOCH]
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
    assert second_lot[LOT_EPOCH] == first_lot[LOT_EPOCH]
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
        deployment.burner.take(deployment.sell_token, amount_taken, deployment.receiver, b"")

    unsold = first_staged - amount_taken
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    new_staged, _ = _stage(deployment, deployment.sell_token, 200 * WAD)
    second_lot = _lot_with_bounds(deployment, deployment.sell_token)

    assert second_lot[LOT_EPOCH] == first_lot[LOT_EPOCH] + WEEK
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
        deployment.burner.take(deployment.sell_token, amount, deployment.receiver, b"")

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
        deployment.burner.take(deployment.problem_token, amount, deployment.receiver, b"")

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
    taker.configure(4, False)

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
    with boa.reverts(custom_err("AmountExceedsAvailable()")):
        deployment.burner.getAmountNeeded(deployment.sell_token, staged + 1)

    buyer_target_before = deployment.target.balanceOf(deployment.buyer)
    with boa.env.prank(deployment.buyer), boa.reverts(custom_err("NothingAvailable()")):
        deployment.burner.take(
            deployment.sell_token, 0, deployment.receiver, b""
        )

    current_lot = _lot_with_bounds(deployment, deployment.sell_token)
    assert current_lot[LOT_EPOCH] == lot[LOT_EPOCH]
    assert deployment.burner.available(deployment.sell_token) == staged
    assert deployment.sell_token.balanceOf(deployment.receiver) == 0
    assert deployment.target.balanceOf(deployment.buyer) == buyer_target_before
    assert deployment.target.balanceOf(deployment.fee_collector) == 0


def test_quote_view_uses_signed_total_bound_not_live_availability(
    deployment: AuctionDeployment,
):
    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    amount = staged // 2
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, amount)
    deployment.target._mint_for_testing(deployment.buyer, payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, payment)
        deployment.burner.take(deployment.sell_token, amount, deployment.receiver, b"")

    # A persistent partially fillable order keeps quoting its signed total.
    assert deployment.burner.quote(deployment.sell_token, staged) > 0
    with boa.reverts(custom_err("AmountExceedsAvailable()")):
        deployment.burner.getAmountNeeded(deployment.sell_token, staged)
    with boa.reverts(custom_err("AmountExceedsLot()")):
        deployment.burner.quote(deployment.sell_token, staged + 1)


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
        deployment.receiver,
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
    """A deadline before the next epoch's window replaces the removed
    expected-epoch limit: exactly one epoch is active per timestamp, so the
    signed epoch's end caps inclusion and a restaged lot's fresh curve can
    never fill an old transaction."""
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
                deployment.receiver,
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
        deployment.burner.take(deployment.sell_token, amount, deployment.receiver, b"")

    taken = next(log for log in deployment.burner.get_logs() if _event_name(log) == "Taken")
    assert taken.address == deployment.burner.address
    assert taken.token == deployment.sell_token.address
    assert taken.epoch == lot[LOT_EPOCH]
    assert taken.caller == deployment.buyer
    assert taken.receiver == deployment.receiver
    assert taken.amount_out == amount
    assert taken.payment == payment
    assert taken.remaining_balance == staged - amount


def test_cow_configuration_authority_and_initial_state(deployment: AuctionDeployment):
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.burner.configure_watchtower(
            deployment.composable_cow, deployment.handler
        )
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.burner.enable_adapter(deployment.cow_adapter)
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.burner.set_fallback_adapter(deployment.cow_adapter)
    # The fallback route requires a locally enabled adapter first.
    with boa.env.prank(deployment.owner), boa.reverts(
        custom_err("FallbackNotEnabled()")
    ):
        deployment.burner.set_fallback_adapter(deployment.cow_adapter)

    generation = _configure_and_enable_cow(deployment)
    assert generation == 1
    assert deployment.burner.cow_enabled()
    assert deployment.burner.composable_cow() == deployment.composable_cow.address
    assert deployment.burner.fallback_adapter() == deployment.cow_adapter.address
    assert deployment.burner.executor_refcount(deployment.retired_relayer) == 1
    assert deployment.cow_adapter.settlement() == deployment.settlement.address
    assert deployment.cow_adapter.vault_relayer() == deployment.retired_relayer
    assert deployment.burner.cow_handler() == deployment.handler.address
    # The generator interface belongs to the external handler, never the burner.
    assert not deployment.burner.supportsInterface(CONDITIONAL_ORDER_INTERFACE)
    assert deployment.burner.supportsInterface(ERC1271_MAGIC_VALUE)

    with boa.env.prank(deployment.owner), boa.reverts(custom_err("AlreadyEnabled()")):
        deployment.burner.enable_adapter(deployment.cow_adapter)
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.burner.disable_adapter(deployment.cow_adapter)

    with boa.env.prank(deployment.emergency_owner):
        deployment.burner.disable_adapter(deployment.cow_adapter)
    assert not deployment.burner.cow_enabled()
    assert deployment.burner.executor_refcount(deployment.retired_relayer) == 0
    # ERC-1271 stays claimed for the signature router even with CoW disabled.
    assert not deployment.burner.supportsInterface(CONDITIONAL_ORDER_INTERFACE)
    assert deployment.burner.supportsInterface(ERC1271_MAGIC_VALUE)


def test_registry_disable_also_kills_cow_rail(deployment: AuctionDeployment):
    """The CoW rail mirrors the signature router's dual switches: an emergency
    registry disable must also stop registrations and watchtower publishing,
    not only settlement routing."""
    _configure_and_enable_cow(deployment)
    assert deployment.burner.cow_enabled()

    with boa.env.prank(deployment.emergency_owner):
        deployment.registry.disable_adapter(deployment.cow_adapter)
    assert not deployment.burner.cow_enabled()

    # Reactivation restores the rail without touching burner-local state.
    with boa.env.prank(deployment.owner):
        deployment.registry.activate_adapter(deployment.cow_adapter)
    assert deployment.burner.cow_enabled()


def test_cow_lifecycle_events_and_staging_approvals(deployment: AuctionDeployment):
    with boa.env.prank(deployment.owner):
        deployment.burner.enable_adapter(deployment.cow_adapter)
        enabled = next(
            log
            for log in deployment.burner.get_logs()
            if _event_name(log) == "AdapterEnabled"
        )
        deployment.burner.set_fallback_adapter(deployment.cow_adapter)
        routed = next(
            log
            for log in deployment.burner.get_logs()
            if _event_name(log) == "FallbackAdapterSet"
        )
        deployment.burner.configure_watchtower(
            deployment.composable_cow, deployment.handler
        )
        wired = next(
            log
            for log in deployment.burner.get_logs()
            if _event_name(log) == "WatchtowerConfigured"
        )
    assert enabled.address == deployment.burner.address
    assert enabled.verifier == deployment.cow_adapter.address
    assert enabled.executor == deployment.retired_relayer
    assert routed.verifier == deployment.cow_adapter.address
    assert wired.composable_cow == deployment.composable_cow.address
    assert wired.handler == deployment.handler.address
    assert wired.generation == 1
    assert deployment.burner.cow_enabled()

    _stage(deployment, deployment.sell_token, 100 * WAD)
    logs = deployment.fee_collector.get_logs()
    registered = next(log for log in logs if _event_name(log) == "ConditionalOrderRegistered")
    assert registered.address == deployment.burner.address
    assert registered.token == deployment.sell_token.address
    assert registered.generation == 1
    assert (
        deployment.sell_token.allowance(deployment.burner, deployment.retired_relayer)
        == MAX_UINT256
    )

    with boa.env.prank(deployment.owner):
        deployment.burner.disable_adapter(deployment.cow_adapter)
    disabled = next(
        log
        for log in deployment.burner.get_logs()
        if _event_name(log) == "AdapterDisabled"
    )
    assert disabled.verifier == deployment.cow_adapter.address
    assert disabled.executor == deployment.retired_relayer
    assert not deployment.burner.cow_enabled()

    # Rail migration: a new adapter (new settlement, new relayer) takes over.
    with boa.env.prank(deployment.owner):
        deployment.burner.enable_adapter(deployment.new_cow_adapter)
        deployment.burner.set_fallback_adapter(deployment.new_cow_adapter)
        deployment.burner.configure_watchtower(
            deployment.composable_cow, deployment.handler
        )
        rewired = next(
            log
            for log in deployment.burner.get_logs()
            if _event_name(log) == "WatchtowerConfigured"
        )
    assert rewired.generation == 2

    # Retired-executor cleanup is permissionless once its refcount is released.
    with boa.env.prank(deployment.keeper):
        deployment.burner.sync_executor_approvals(
            deployment.retired_relayer, [deployment.sell_token.address]
        )
    assert (
        deployment.sell_token.allowance(deployment.burner, deployment.retired_relayer) == 0
    )


def test_first_collect_registers_generation_static_data_and_infinite_allowance(
    deployment: AuctionDeployment,
):
    generation = _configure_and_enable_cow(deployment)
    staged, _ = _stage(deployment, deployment.sell_token, 100 * WAD)

    assert deployment.composable_cow.create_count() == 1
    assert deployment.composable_cow.last_owner() == deployment.burner.address
    assert deployment.composable_cow.last_handler() == deployment.handler.address
    assert deployment.composable_cow.last_salt() == ZERO_BYTES32
    assert deployment.composable_cow.last_static_data() == _static_input(
        deployment.sell_token, generation
    )
    assert deployment.composable_cow.last_dispatch()
    assert (
        deployment.sell_token.allowance(deployment.burner, deployment.retired_relayer)
        == MAX_UINT256
    )

    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )
    assert deployment.composable_cow.create_count() == 1
    assert _lot_with_bounds(deployment, deployment.sell_token)[LOT_INITIAL_AMOUNT] == staged
    assert (
        deployment.sell_token.allowance(deployment.burner, deployment.retired_relayer)
        == MAX_UINT256
    )


def test_tokens_staged_while_disabled_register_only_on_next_collect(
    deployment: AuctionDeployment,
):
    staged, _ = _stage(deployment, deployment.sell_token, 100 * WAD)
    assert deployment.composable_cow.create_count() == 0

    generation = _configure_and_enable_cow(deployment)
    assert deployment.composable_cow.create_count() == 0
    # Approvals follow staging, not enabling: nothing is approved retroactively.
    assert (
        deployment.sell_token.allowance(deployment.burner, deployment.retired_relayer) == 0
    )
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )

    assert deployment.composable_cow.create_count() == 1
    assert deployment.composable_cow.last_static_data() == _static_input(
        deployment.sell_token, generation
    )
    assert _lot_with_bounds(deployment, deployment.sell_token)[LOT_INITIAL_AMOUNT] == staged
    assert (
        deployment.sell_token.allowance(deployment.burner, deployment.retired_relayer)
        == MAX_UINT256
    )


def test_disable_enable_without_reconfiguration_does_not_duplicate_orders(
    deployment: AuctionDeployment,
):
    generation = _configure_and_enable_cow(deployment)
    _stage(deployment, deployment.sell_token, 100 * WAD)

    with boa.env.prank(deployment.owner):
        deployment.burner.disable_adapter(deployment.cow_adapter)
        assert deployment.burner.executor_refcount(deployment.retired_relayer) == 0
        deployment.burner.enable_adapter(deployment.cow_adapter)
    assert deployment.burner.cow_generation() == generation
    assert deployment.burner.executor_refcount(deployment.retired_relayer) == 1

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
    lot = _lot_with_bounds(deployment, deployment.sell_token)
    _move_to_timestamp(lot[LOT_START])
    old_order = _tradeable_order(deployment, deployment.sell_token, old_generation)

    with boa.env.prank(deployment.owner):
        deployment.burner.disable_adapter(deployment.cow_adapter)
        deployment.burner.enable_adapter(deployment.new_cow_adapter)
        deployment.burner.set_fallback_adapter(deployment.new_cow_adapter)
        deployment.burner.configure_watchtower(
            deployment.composable_cow, deployment.handler
        )
    new_generation = deployment.burner.cow_generation()
    assert new_generation == old_generation + 1

    with boa.reverts():
        deployment.handler.getTradeableOrder(
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
    assert deployment.composable_cow.create_count() == 2
    assert deployment.composable_cow.last_static_data() == _static_input(
        deployment.sell_token, new_generation
    )
    assert (
        deployment.sell_token.allowance(deployment.burner, deployment.new_relayer)
        == MAX_UINT256
    )


def test_permissionless_sync_executor_approvals_follows_derived_state(
    deployment: AuctionDeployment,
):
    _configure_and_enable_cow(deployment)
    _stage(deployment, deployment.sell_token, 100 * WAD)
    relayer = deployment.retired_relayer
    assert deployment.sell_token.allowance(deployment.burner, relayer) == MAX_UINT256

    with boa.env.prank(deployment.keeper), boa.reverts(custom_err("BadExecutor()")):
        deployment.burner.sync_executor_approvals(
            ZERO_ADDRESS, [deployment.sell_token.address]
        )
    with boa.env.prank(deployment.keeper), boa.reverts(custom_err("WantNotSellable()")):
        deployment.burner.sync_executor_approvals(relayer, [deployment.target.address])

    # While the executor is referenced, sync is a top-up path and stays at max.
    with boa.env.prank(deployment.keeper):
        deployment.burner.sync_executor_approvals(relayer, [deployment.sell_token.address])
    assert deployment.sell_token.allowance(deployment.burner, relayer) == MAX_UINT256

    with boa.env.prank(deployment.emergency_owner):
        deployment.burner.disable_adapter(deployment.cow_adapter)
    # Approvals are not touched by disable; cleanup is the permissionless sync.
    assert deployment.sell_token.allowance(deployment.burner, relayer) == MAX_UINT256
    with boa.env.prank(deployment.keeper):
        deployment.burner.sync_executor_approvals(relayer, [deployment.sell_token.address])
    assert deployment.sell_token.allowance(deployment.burner, relayer) == 0

    # Re-enable: sync is the retry path that restores approvals without staging.
    with boa.env.prank(deployment.owner):
        deployment.burner.enable_adapter(deployment.cow_adapter)
    with boa.env.prank(deployment.keeper):
        deployment.burner.sync_executor_approvals(relayer, [deployment.sell_token.address])
    assert deployment.sell_token.allowance(deployment.burner, relayer) == MAX_UINT256


def test_emergency_disable_bundle_leaves_no_allowance_window(
    deployment: AuctionDeployment,
):
    """The approved emergency runbook: the multisig batches disable_adapter
    with sync_executor_approvals into one transaction, so the rail and its
    executor allowances die together (see scripts/emergency_cow_disable.py)."""
    from scripts.emergency_cow_disable import build_bundle

    _configure_and_enable_cow(deployment)
    _stage(deployment, deployment.sell_token, 100 * WAD)
    relayer = deployment.retired_relayer
    assert deployment.sell_token.allowance(deployment.burner, relayer) == MAX_UINT256

    # The script's calldata must match the live ABI of both bundled calls.
    bundle = build_bundle(
        str(deployment.burner.address),
        str(deployment.cow_adapter.address),
        relayer,
        [deployment.sell_token.address],
    )
    assert bundle == [
        (
            deployment.burner.address,
            "0x"
            + deployment.burner.disable_adapter.prepare_calldata(
                deployment.cow_adapter
            ).hex(),
        ),
        (
            deployment.burner.address,
            "0x"
            + deployment.burner.sync_executor_approvals.prepare_calldata(
                relayer, [deployment.sell_token.address]
            ).hex(),
        ),
    ]

    # Both calls execute back-to-back from the emergency multisig batch.
    with boa.env.prank(deployment.emergency_owner):
        deployment.burner.disable_adapter(deployment.cow_adapter)
        deployment.burner.sync_executor_approvals(
            relayer, [deployment.sell_token.address]
        )

    assert not deployment.burner.cow_enabled()
    assert deployment.sell_token.allowance(deployment.burner, relayer) == 0


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
        deployment.burner.disable_adapter(deployment.cow_adapter)

    with boa.reverts():
        _tradeable_order(deployment, deployment.sell_token, generation)
    with boa.reverts():
        _verify_order(deployment, deployment.sell_token, order, generation=generation)
    # The router has no live route left and answers the invalid magic.
    assert (
        deployment.burner.isValidSignature(order_hash, signature) == ERC1271_INVALID
    )
    assert not deployment.burner.supportsInterface(CONDITIONAL_ORDER_INTERFACE)
    assert deployment.burner.supportsInterface(ERC1271_MAGIC_VALUE)

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
            _timestamp(),
            b"",
        )


def test_tradeable_order_fields_quote_and_validity_buckets(deployment: AuctionDeployment):
    generation = _configure_and_enable_cow(deployment)
    _stage(deployment, deployment.sell_token, 100 * WAD)
    lot = _lot_with_bounds(deployment, deployment.sell_token)
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


def test_cow_pull_then_donation_resells_in_favor_of_fee_collector(
    deployment: AuctionDeployment,
):
    """Documented balance-based trade-off: donations after the snapshot are
    resellable along the same curve, bounded by initial_amount."""
    _configure_and_enable_cow(deployment)
    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    cow_amount = staged * 40 // 100
    unit_price = deployment.burner.price(deployment.sell_token)

    with boa.env.prank(deployment.retired_relayer):
        deployment.sell_token.transferFrom(
            deployment.burner, deployment.receiver, cow_amount
        )
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
            deployment.receiver,
            b"",
        )

    assert native_amount == staged
    assert deployment.sell_token.balanceOf(deployment.receiver) == staged + cow_amount
    assert deployment.sell_token.balanceOf(deployment.burner) == 0
    assert deployment.target.balanceOf(deployment.fee_collector) == payment
    assert deployment.burner.available(deployment.sell_token) == 0


def test_native_fill_then_donation_revives_availability_up_to_snapshot(
    deployment: AuctionDeployment,
):
    generation = _configure_and_enable_cow(deployment)
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
            deployment.receiver,
            b"",
        )
    assert deployment.burner.available(deployment.sell_token) == remaining

    # A donation refills the balance: availability revives up to the snapshot
    # cap, and the CoW order re-quotes the whole refilled amount at curve price.
    deployment.sell_token._mint_for_testing(deployment.burner, native_amount)
    assert deployment.burner.available(deployment.sell_token) == staged
    order = _tradeable_order(deployment, deployment.sell_token, generation)
    assert order[ORDER_SELL_AMOUNT] == staged

    with boa.env.prank(deployment.retired_relayer):
        deployment.sell_token.transferFrom(
            deployment.burner, deployment.watcher, order[ORDER_SELL_AMOUNT]
        )

    assert deployment.sell_token.balanceOf(deployment.watcher) == staged
    assert deployment.sell_token.balanceOf(deployment.receiver) == native_amount
    assert deployment.sell_token.balanceOf(deployment.burner) == 0
    assert deployment.burner.available(deployment.sell_token) == 0


def test_tradeable_order_rejects_zero_unsynced_outside_killed_and_bad_inputs(
    deployment: AuctionDeployment,
):
    generation = _configure_and_enable_cow(deployment)
    with boa.reverts():
        _tradeable_order(deployment, deployment.sell_token, generation)

    _stage(deployment, deployment.sell_token, 100 * WAD)
    lot = _lot_with_bounds(deployment, deployment.sell_token)
    with boa.reverts():
        _tradeable_order(deployment, deployment.sell_token, generation)

    _move_to_timestamp(lot[LOT_START])
    with boa.reverts():
        deployment.handler.getTradeableOrder(
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
    # ComposableCoW's muxer probe must revert: only the reverting probe drops
    # the real ComposableCoW into its plain-ERC-1271 catch branch, while a
    # successful False answer is InvalidFallbackHandler().
    with boa.reverts(custom_err("NotSignatureVerifierMuxer()")):
        deployment.burner.supportsInterface(SIGNATURE_VERIFIER_MUXER_INTERFACE)

    # A signature with an unknown 20-byte prefix falls through to the CoW
    # fallback, whose canonical decode rejects it loudly.
    with boa.reverts():
        deployment.burner.isValidSignature(order_hash, bytes(20) + bytes(32))

    # A watchtower rewire bumps the generation without touching settlement.
    with boa.env.prank(deployment.owner):
        deployment.burner.configure_watchtower(
            deployment.composable_cow, deployment.handler
        )
    assert deployment.burner.cow_generation() == generation + 1
    # The wrapper is transport, never authority: a stale registration only
    # stops watchtower discovery, while settlement validation stays purely
    # economic — the still-active lot keeps validating the same order.
    assert (
        deployment.burner.isValidSignature(order_hash, signature)
        == ERC1271_MAGIC_VALUE
    )
    with boa.reverts():
        _tradeable_order(deployment, deployment.sell_token, generation)
    assert lot[LOT_EPOCH] != 0


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
    assert _lot_with_bounds(deployment, deployment.problem_token)[LOT_INITIAL_AMOUNT] == 0

    deployment.problem_token.set_returns_false(False)
    deployment.problem_token.set_revert_balance_of(True)
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.fee_collector.collect(
            [deployment.problem_token.address], deployment.keeper
        )
    assert _lot_with_bounds(deployment, deployment.problem_token)[LOT_INITIAL_AMOUNT] == 0

    deployment.problem_token.set_revert_balance_of(False)
    deployment.problem_token.set_blacklisted(deployment.burner.address, True)
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.fee_collector.collect(
            [deployment.problem_token.address], deployment.keeper
        )
    assert _lot_with_bounds(deployment, deployment.problem_token)[LOT_INITIAL_AMOUNT] == 0


def test_usdt_style_token_granted_from_zero_allowance(deployment: AuctionDeployment):
    # The allowance-guarded helper only approves from zero, which is exactly
    # the state USDT-style reset-required tokens accept a plain approve in.
    _configure_and_enable_cow(deployment)
    deployment.problem_token.set_requires_approval_reset(True)

    _stage_problem_token(deployment, deployment.problem_token, 100 * WAD)
    assert (
        deployment.problem_token.allowance(deployment.burner, deployment.retired_relayer)
        == MAX_UINT256
    )


def test_router_approval_failure_blocks_staging(
    deployment: AuctionDeployment,
):
    # Approvals are strict single calls: a token whose approve fails reverts
    # its staging, so the keeper excludes it from the collect batch instead of
    # silently staging a lot the CoW rail cannot pull.
    _configure_and_enable_cow(deployment)
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    deployment.problem_token.set_fails_nonzero_approval(True)

    deployment.problem_token._mint_for_testing(deployment.fee_collector, 100 * WAD)
    with boa.env.prank(deployment.keeper):
        with boa.reverts():
            deployment.fee_collector.collect(
                [deployment.problem_token.address], deployment.keeper
            )
    assert deployment.problem_token.allowance(
        deployment.burner, deployment.retired_relayer
    ) == 0

    # Once the token behaves, staging goes through with the full grant.
    deployment.problem_token.set_fails_nonzero_approval(False)
    _stage_problem_token(deployment, deployment.problem_token, 0)
    assert _lot_with_bounds(deployment, deployment.problem_token)[LOT_INITIAL_AMOUNT] > 0
    assert (
        deployment.problem_token.allowance(deployment.burner, deployment.retired_relayer)
        == MAX_UINT256
    )


def test_no_return_token_approval_can_be_synced_after_disable(
    deployment: AuctionDeployment,
):
    _configure_and_enable_cow(deployment)
    _stage(deployment, deployment.no_return_token, 100 * 10**8)
    assert (
        deployment.no_return_token.allowance(
            deployment.burner, deployment.retired_relayer
        )
        == MAX_UINT256
    )

    with boa.env.prank(deployment.owner):
        deployment.burner.disable_adapter(deployment.cow_adapter)
    with boa.env.prank(deployment.keeper):
        deployment.burner.sync_executor_approvals(
            deployment.retired_relayer, [deployment.no_return_token.address]
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


def test_recover_empties_lot_and_set_killed_fences_donation_revival(
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

    # No cancellation state: the drained balance alone kills the lot.
    emptied_lot = _lot_with_bounds(deployment, deployment.sell_token)
    assert emptied_lot[LOT_EPOCH] == lot[LOT_EPOCH]
    assert emptied_lot[LOT_INITIAL_AMOUNT] == staged
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
        _tradeable_order(deployment, deployment.sell_token, generation)
    with boa.reverts():
        deployment.burner.isValidSignature(order_hash, signature)

    # The kill also blocks the permissionless re-collect of evacuated funds.
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )

    # Lifting the kill lets a COLLECT restage everything from the curve top.
    with boa.env.prank(deployment.owner):
        deployment.fee_collector.set_killed([(deployment.sell_token.address, 0)])
    collector_balance = deployment.sell_token.balanceOf(deployment.fee_collector)
    collect_fee = (
        collector_balance * deployment.fee_collector.fee(Epoch.COLLECT) // WAD
    )
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )

    refreshed_lot = _lot_with_bounds(deployment, deployment.sell_token)
    expected_snapshot = donation + collector_balance - collect_fee
    assert refreshed_lot[LOT_EPOCH] == lot[LOT_EPOCH] + WEEK
    assert refreshed_lot[LOT_INITIAL_AMOUNT] == expected_snapshot
    assert (
        deployment.sell_token.allowance(deployment.burner, deployment.retired_relayer)
        == MAX_UINT256
    )

    _move_to_timestamp(refreshed_lot[LOT_START])
    assert deployment.burner.available(deployment.sell_token) == expected_snapshot
    assert deployment.burner.price(deployment.sell_token) > 0
    refreshed_order = _tradeable_order(
        deployment, deployment.sell_token, generation
    )
    assert refreshed_order[ORDER_SELL_AMOUNT] == expected_snapshot


def test_recover_during_collect_frame_recollect_restages_unless_killed(
    deployment: AuctionDeployment,
):
    generation = _configure_and_enable_cow(deployment)
    staged, _ = _stage(deployment, deployment.sell_token, 100 * WAD)
    first_lot = _lot_with_bounds(deployment, deployment.sell_token)
    assert deployment.burner.cow_registered(deployment.sell_token)
    assert (
        deployment.sell_token.allowance(deployment.burner, deployment.retired_relayer)
        == MAX_UINT256
    )

    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    recovery_epoch = deployment.burner.current_epoch()
    assert recovery_epoch == first_lot[LOT_EPOCH] + WEEK
    # An emergency evacuation batches recover with a COLLECT kill: recover
    # alone leaves the permissionless re-collect open in the same frame.
    with boa.env.prank(deployment.owner):
        deployment.burner.recover([deployment.sell_token.address])
        deployment.fee_collector.set_killed(
            [(deployment.sell_token.address, Epoch.COLLECT)]
        )

    assert deployment.sell_token.balanceOf(deployment.burner) == 0
    assert deployment.sell_token.balanceOf(deployment.fee_collector) == staged

    new_receipts = 25 * WAD
    deployment.sell_token._mint_for_testing(
        deployment.fee_collector, new_receipts
    )
    collector_before = deployment.sell_token.balanceOf(deployment.fee_collector)
    keeper_before = deployment.sell_token.balanceOf(deployment.keeper)
    lot_before = _lot_with_bounds(deployment, deployment.sell_token)

    with boa.env.prank(deployment.keeper), boa.reverts():
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )

    assert _lot_with_bounds(deployment, deployment.sell_token) == lot_before
    assert deployment.sell_token.balanceOf(deployment.fee_collector) == collector_before
    assert deployment.sell_token.balanceOf(deployment.keeper) == keeper_before
    assert deployment.sell_token.balanceOf(deployment.burner) == 0
    assert deployment.composable_cow.create_count() == 1

    # Once the kill is lifted, the very same frame's permissionless collect
    # restages the evacuated funds from the top of the curve.
    with boa.env.prank(deployment.owner):
        deployment.fee_collector.set_killed([(deployment.sell_token.address, 0)])
    fee = collector_before * deployment.fee_collector.fee(Epoch.COLLECT) // WAD
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )

    refreshed_lot = _lot_with_bounds(deployment, deployment.sell_token)
    expected_snapshot = collector_before - fee
    assert refreshed_lot[LOT_EPOCH] == recovery_epoch
    assert refreshed_lot[LOT_INITIAL_AMOUNT] == expected_snapshot
    assert deployment.sell_token.balanceOf(deployment.fee_collector) == 0
    assert deployment.sell_token.balanceOf(deployment.burner) == expected_snapshot
    assert deployment.sell_token.balanceOf(deployment.keeper) == keeper_before + fee
    assert (
        deployment.sell_token.allowance(deployment.burner, deployment.retired_relayer)
        == MAX_UINT256
    )
    assert deployment.composable_cow.create_count() == 1

    _move_to_timestamp(refreshed_lot[LOT_START])
    assert deployment.burner.available(deployment.sell_token) == expected_snapshot
    refreshed_order = _tradeable_order(
        deployment, deployment.sell_token, generation
    )
    assert refreshed_order[ORDER_SELL_AMOUNT] == expected_snapshot


def test_recover_before_first_staging_leaves_no_state_and_collect_restages(
    deployment: AuctionDeployment,
):
    generation = _configure_and_enable_cow(deployment)
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    recovery_epoch = deployment.burner.current_epoch()
    recovered_amount = 10 * WAD
    deployment.sell_token._mint_for_testing(
        deployment.burner, recovered_amount
    )

    with boa.env.prank(deployment.owner):
        deployment.burner.recover([deployment.sell_token.address])

    assert _lot_with_bounds(deployment, deployment.sell_token)[LOT_EPOCH] == 0
    assert not deployment.burner.cow_registered(deployment.sell_token)
    assert deployment.composable_cow.create_count() == 0
    assert deployment.sell_token.balanceOf(deployment.burner) == 0
    assert (
        deployment.sell_token.balanceOf(deployment.fee_collector)
        == recovered_amount
    )
    # Approvals only follow staging: nothing was approved before the recovery.
    assert (
        deployment.sell_token.allowance(deployment.burner, deployment.retired_relayer)
        == 0
    )

    # Recovery leaves no lingering lot state: the same COLLECT frame's
    # permissionless collect restages the returned balance.
    keeper_before = deployment.sell_token.balanceOf(deployment.keeper)
    fee = recovered_amount * deployment.fee_collector.fee(Epoch.COLLECT) // WAD
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )

    refreshed_lot = _lot_with_bounds(deployment, deployment.sell_token)
    expected_snapshot = recovered_amount - fee
    assert refreshed_lot[LOT_EPOCH] == recovery_epoch
    assert refreshed_lot[LOT_INITIAL_AMOUNT] == expected_snapshot
    assert deployment.sell_token.balanceOf(deployment.keeper) == keeper_before + fee
    assert deployment.burner.cow_registered(deployment.sell_token)
    assert deployment.composable_cow.create_count() == 1
    assert (
        deployment.sell_token.allowance(deployment.burner, deployment.retired_relayer)
        == MAX_UINT256
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
    hooker = boa.load(
        "contracts/hooks/Hooker.vy", deployment.fee_collector, [], [], []
    )
    with boa.env.prank(deployment.owner):
        deployment.fee_collector.set_hooker(hooker)

    _, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)

    cow_amount = staged * 40 // 100
    cow_payment = deployment.burner.getAmountNeeded(deployment.sell_token, cow_amount)
    with boa.env.prank(deployment.retired_relayer):
        deployment.sell_token.transferFrom(
            deployment.burner, deployment.receiver, cow_amount
        )
    deployment.target._mint_for_testing(deployment.fee_collector, cow_payment)

    native_amount = deployment.burner.available(deployment.sell_token)
    native_payment = deployment.burner.getAmountNeeded(
        deployment.sell_token, native_amount
    )
    deployment.target._mint_for_testing(deployment.buyer, native_payment)
    with boa.env.prank(deployment.buyer):
        deployment.target.approve(deployment.burner, native_payment)
        assert (
            deployment.burner.take(
                deployment.sell_token, MAX_UINT256, deployment.buyer, b""
            )
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
    that week's staging — so the fence stops at the previous epoch and a
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
        deployment.burner.take(
            deployment.sell_token, MAX_UINT256, deployment.buyer, b""
        )
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    deployment.second_token._mint_for_testing(deployment.fee_collector, WAD)
    with boa.env.prank(deployment.keeper), boa.reverts(
        custom_err("TargetChanged()", nested=True)
    ):
        deployment.fee_collector.collect(
            [deployment.second_token.address], deployment.keeper
        )

    # Resyncs are SLEEP-only: a COLLECT execution must wait for the next week.
    with boa.env.prank(deployment.owner), boa.reverts(
        custom_err("NotSleepEpoch()")
    ):
        deployment.burner.resync_target(
            new_target, START_TOTAL, FLOOR_TOTAL, DECAY_FACTOR_RAY, STEP_DURATION
        )
    _move_to_epoch(deployment.fee_collector, Epoch.SLEEP)

    with boa.env.prank(deployment.keeper), boa.reverts(custom_err("OnlyOwner()")):
        deployment.burner.resync_target(
            new_target, START_TOTAL, FLOOR_TOTAL, DECAY_FACTOR_RAY, STEP_DURATION
        )
    with boa.env.prank(deployment.owner), boa.reverts(
        custom_err("DecayMissesFloor()")
    ):
        deployment.burner.resync_target(
            new_target, START_TOTAL, FLOOR_TOTAL, RAY - 1, STEP_DURATION
        )
    # Delayed-execution guard: params tuned for the old denomination must not
    # bind to the new one.
    with boa.env.prank(deployment.owner), boa.reverts(
        custom_err("TargetChanged()")
    ):
        deployment.burner.resync_target(
            old_target, START_TOTAL, FLOOR_TOTAL, DECAY_FACTOR_RAY, STEP_DURATION
        )

    with boa.env.prank(deployment.owner):
        deployment.burner.resync_target(
            new_target, 2 * START_TOTAL, 2 * FLOOR_TOTAL, DECAY_FACTOR_RAY, STEP_DURATION
        )
    resynced = next(
        log
        for log in deployment.burner.get_logs()
        if _event_name(log) == "EconomicsResynced"
    )
    assert resynced.want == new_target.address
    assert resynced.start_total == 2 * START_TOTAL
    assert deployment.burner.want() == new_target.address
    assert deployment.burner.target() == new_target.address
    assert deployment.burner.start_total() == 2 * START_TOTAL

    # The resync landed in SLEEP, before this epoch's staging and window: the
    # fence stops at the previous epoch, so only the stale lot stays dead.
    fence = deployment.burner.reconfigured_epoch()
    assert fence == deployment.burner.current_epoch() - 1
    assert resynced.reconfigured_epoch == fence
    assert _lot_with_bounds(deployment, deployment.sell_token)[LOT_EPOCH] <= fence
    assert deployment.burner.available(deployment.sell_token) == 0
    with boa.env.prank(deployment.buyer), boa.reverts():
        deployment.burner.take(
            deployment.sell_token, MAX_UINT256, deployment.buyer, b""
        )

    # Same week's COLLECT: restage picks the new curve immediately; the old
    # target is now plain sellable inventory; fills pay in the new
    # denomination without waiting for the next week.
    _move_to_epoch(deployment.fee_collector, Epoch.COLLECT)
    old_target._mint_for_testing(deployment.fee_collector, 10 * WAD)
    with boa.env.prank(deployment.keeper):
        deployment.fee_collector.collect(
            [deployment.sell_token.address], deployment.keeper
        )
        deployment.fee_collector.collect([old_target.address], deployment.keeper)

    lot = _lot_with_bounds(deployment, deployment.sell_token)
    assert lot[LOT_EPOCH] > fence
    assert deployment.burner.start_total() == 2 * START_TOTAL
    assert _lot_with_bounds(deployment, old_target)[LOT_EPOCH] == lot[LOT_EPOCH]
    _move_to_timestamp(lot[LOT_START])

    assert deployment.burner.available(deployment.sell_token) == staged
    payment = deployment.burner.getAmountNeeded(deployment.sell_token, staged)
    new_target._mint_for_testing(deployment.buyer, payment)
    with boa.env.prank(deployment.buyer):
        new_target.approve(deployment.burner, payment)
        deployment.burner.take(
            deployment.sell_token, MAX_UINT256, deployment.buyer, b""
        )
    assert new_target.balanceOf(deployment.fee_collector) == payment
    assert deployment.burner.available(old_target) > 0


def test_resync_only_in_sleep_fences_previous_lots(
    deployment: AuctionDeployment,
):
    """Resyncs are confined to SLEEP — before the week's staging — so a want
    change can never land under a staged lot: EXCHANGE and FORWARD attempts
    revert, and the SLEEP resync fences every previous epoch, killing the old
    week's lot and its published CoW order."""
    generation = _configure_and_enable_cow(deployment)
    lot, staged = _activate_lot(deployment, deployment.sell_token, 100 * WAD)
    order = _tradeable_order(deployment, deployment.sell_token, generation)
    signature = _encode_erc1271_signature(
        order, deployment.burner, _static_input(deployment.sell_token, generation)
    )
    order_hash = _gpv2_order_digest(order, deployment.composable_cow.domainSeparator())
    assert (
        deployment.burner.isValidSignature(order_hash, signature)
        == ERC1271_MAGIC_VALUE
    )

    new_target = boa.load("contracts/testing/ERC20Mock.vy", "New Target", "NEWT", 18)
    with boa.env.prank(deployment.owner):
        deployment.fee_collector.set_target(new_target)
        # The window is open: any resync must wait for the next SLEEP.
        with boa.reverts(custom_err("NotSleepEpoch()")):
            deployment.burner.resync_target(
                new_target, START_TOTAL, FLOOR_TOTAL, DECAY_FACTOR_RAY, STEP_DURATION
            )

    # FORWARD — the window closed, but still not SLEEP.
    _move_to_timestamp(lot[LOT_END])
    with boa.env.prank(deployment.owner), boa.reverts(
        custom_err("NotSleepEpoch()")
    ):
        deployment.burner.resync_target(
            new_target, START_TOTAL, FLOOR_TOTAL, DECAY_FACTOR_RAY, STEP_DURATION
        )

    _move_to_epoch(deployment.fee_collector, Epoch.SLEEP)
    with boa.env.prank(deployment.owner):
        deployment.burner.resync_target(
            new_target, START_TOTAL, FLOOR_TOTAL, DECAY_FACTOR_RAY, STEP_DURATION
        )

    fence = deployment.burner.reconfigured_epoch()
    assert fence == deployment.burner.current_epoch() - 1
    assert lot[LOT_EPOCH] <= fence
    assert deployment.burner.available(deployment.sell_token) == 0
    with boa.env.prank(deployment.buyer), boa.reverts():
        deployment.burner.take(
            deployment.sell_token, MAX_UINT256, deployment.buyer, b""
        )
    with boa.reverts():
        deployment.burner.isValidSignature(order_hash, signature)


def test_resync_same_target_retune_in_sleep_repins_curve(
    deployment: AuctionDeployment,
):
    """A same-target retune executed during SLEEP re-pins the curve without a
    fence; the week staged right after trades the retuned curve from the
    window open. Outside SLEEP every resync reverts, so the price a taker
    sees can only decay — plain take() needs no payment bound."""
    _move_to_epoch(deployment.fee_collector, Epoch.SLEEP)
    with boa.env.prank(deployment.owner):
        deployment.burner.resync_target(
            deployment.target,
            2 * START_TOTAL,
            2 * FLOOR_TOTAL,
            DECAY_FACTOR_RAY,
            STEP_DURATION,
        )

    assert deployment.burner.reconfigured_epoch() == 0
    assert deployment.burner.want() == deployment.target.address
    assert deployment.burner.start_total() == 2 * START_TOTAL

    # Staging moves to the same week's COLLECT — which is no longer SLEEP.
    staged, _ = _stage(deployment, deployment.sell_token, 100 * WAD)
    with boa.env.prank(deployment.owner), boa.reverts(
        custom_err("NotSleepEpoch()")
    ):
        deployment.burner.resync_target(
            deployment.target, START_TOTAL, FLOOR_TOTAL, DECAY_FACTOR_RAY, STEP_DURATION
        )

    lot = _lot_with_bounds(deployment, deployment.sell_token)
    _move_to_timestamp(lot[LOT_START])
    assert deployment.burner.available(deployment.sell_token) == staged
    # Full lot at the window open quotes the retuned start total exactly.
    assert (
        deployment.burner.getAmountNeeded(deployment.sell_token, staged)
        == 2 * START_TOTAL
    )
    # Mid-window retunes are rejected outright.
    with boa.env.prank(deployment.owner), boa.reverts(
        custom_err("NotSleepEpoch()")
    ):
        deployment.burner.resync_target(
            deployment.target, START_TOTAL, FLOOR_TOTAL, DECAY_FACTOR_RAY, STEP_DURATION
        )

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
        deployment.burner.take(
            deployment.sell_token, MAX_UINT256, deployment.buyer, b""
        )
    assert deployment.target.balanceOf(deployment.fee_collector) == quote_live
