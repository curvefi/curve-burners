"""Executor: route takes through AuctionTaker and direct takes with limits."""

import boa
import pytest
from eth_abi import encode

from keepers.auction_keeper.chain.abi import MAX_UINT256, CallReverted
from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.execute import (
    Call,
    Executor,
    LotEnded,
    TakePlan,
    WaitTimeout,
    approve_call,
    contract_call,
    curve_router_exchange,
    raw_call,
)
from tests.keeper.conftest import LOT_AMOUNT, timestamp


@pytest.fixture(scope="module")
def solver():
    return boa.env.generate_address("solver")


@pytest.fixture
def taker():
    return boa.load("contracts/burners/auction/periphery/AuctionTaker.vy")


@pytest.fixture
def travel():
    def _sleep(seconds: float) -> None:
        boa.env.time_travel(seconds=int(seconds))

    return _sleep


@pytest.fixture
def executor(solver, taker, travel):
    return Executor(BoaClient(sender=solver), taker=taker.address, sleep=travel)


def mint_route(want, receiver: str, amount: int) -> list[Call]:
    """A route standing in for a swap: mints want as if a DEX paid out."""
    return [contract_call(want.address, "_mint_for_testing(address,uint256)", receiver, amount)]


def test_lot_snapshot(executor, harness, sell_token, lot):
    view = executor.lot(harness.address, sell_token.address)
    assert view.available == LOT_AMOUNT
    assert view.amount_needed == harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    assert (view.start, view.end) == tuple(harness.window(sell_token.address))
    assert executor.payment_for(
        harness.address, sell_token.address, LOT_AMOUNT // 2
    ) == harness.getAmountNeeded(sell_token.address, LOT_AMOUNT // 2)


def test_take_with_route_pays_from_proceeds(
    executor, harness, taker, want, sell_token, proceeds_receiver, solver, lot
):
    view = executor.lot(harness.address, sell_token.address)
    payment = view.amount_needed
    profit = 10**18
    plan = TakePlan(
        harness.address,
        sell_token.address,
        route=mint_route(want, taker.address, payment + profit),
        min_profit=profit,
    )
    result = executor.take_with_route(plan)
    assert result is not None and result.ok
    assert want.balanceOf(proceeds_receiver) == payment
    assert want.balanceOf(solver) == profit
    assert sell_token.balanceOf(solver) == LOT_AMOUNT
    assert harness.available(sell_token.address) == 0


def test_take_with_route_waits_for_payment_cap(executor, harness, taker, want, sell_token, lot):
    view = executor.lot(harness.address, sell_token.address)
    cap = view.amount_needed // 2
    plan = TakePlan(harness.address, sell_token.address, route=mint_route(want, taker.address, cap))
    started = timestamp()
    result = executor.take_with_route(plan, max_payment=cap, poll=600)
    assert result is not None
    assert timestamp() > started
    assert want.balanceOf(harness.receiver()) <= cap


def test_take_with_route_dry_run_simulates_only(executor, harness, taker, want, sell_token, lot):
    view = executor.lot(harness.address, sell_token.address)
    plan = TakePlan(
        harness.address,
        sell_token.address,
        route=mint_route(want, taker.address, view.amount_needed),
    )
    assert executor.take_with_route(plan, dry_run=True) is None
    assert harness.available(sell_token.address) == LOT_AMOUNT

    short = TakePlan(
        harness.address,
        sell_token.address,
        route=mint_route(want, taker.address, view.amount_needed - 1),
    )
    with pytest.raises(CallReverted, match="simulation reverted"):
        executor.take_with_route(short, dry_run=True)


def test_not_before_delays_execution(executor, harness, taker, want, sell_token, lot):
    view = executor.lot(harness.address, sell_token.address)
    plan = TakePlan(
        harness.address,
        sell_token.address,
        route=mint_route(want, taker.address, view.amount_needed),
    )
    target = timestamp() + 1000
    executor.take_with_route(plan, not_before=target, poll=100)
    assert timestamp() >= target


def test_wait_raises_when_window_ends(executor, harness, sell_token, lot):
    with pytest.raises(LotEnded):
        executor.wait_for(harness.address, sell_token.address, max_payment=0, poll=6 * 3600)


def test_wait_timeout(solver, taker, harness, sell_token, lot):
    executor = Executor(BoaClient(sender=solver), taker=taker.address, sleep=lambda s: None)
    with pytest.raises(WaitTimeout):
        executor.wait_for(harness.address, sell_token.address, max_payment=0, poll=0, timeout=0)


def test_take_direct_pays_from_own_want(
    executor, harness, want, sell_token, proceeds_receiver, solver, lot
):
    view = executor.lot(harness.address, sell_token.address)
    want._mint_for_testing(solver, view.amount_needed)
    result = executor.take_direct(
        harness.address, sell_token.address, max_payment=view.amount_needed
    )
    assert result is not None
    assert sell_token.balanceOf(solver) == LOT_AMOUNT
    assert want.balanceOf(proceeds_receiver) == view.amount_needed
    assert want.balanceOf(solver) == 0
    assert want.allowance(solver, harness.address) == 0


def test_take_direct_partial_with_receiver(executor, harness, want, sell_token, solver, lot):
    receiver = boa.env.generate_address("receiver")
    amount = LOT_AMOUNT // 4
    payment = harness.getAmountNeeded(sell_token.address, amount)
    want._mint_for_testing(solver, payment)
    executor.take_direct(
        harness.address,
        sell_token.address,
        max_amount=amount,
        max_payment=payment,
        receiver=receiver,
        deadline=timestamp() + 60,
    )
    assert sell_token.balanceOf(receiver) == amount
    assert harness.available(sell_token.address) == LOT_AMOUNT - amount


def test_executions_require_a_key(taker, harness, sell_token, lot):
    executor = Executor(BoaClient(), taker=taker.address)
    with pytest.raises(RuntimeError, match="keeper key"):
        executor.take_with_route(TakePlan(harness.address, sell_token.address))


def test_route_builders():
    token = "0x" + "11" * 20
    spender = "0x" + "22" * 20
    call = approve_call(token, spender, 5)
    assert call.data == contract_call(token, "approve(address,uint256)", spender, 5).data
    assert raw_call(token, "0x1234").data == b"\x12\x34"
    router = "0x" + "33" * 20
    swap = curve_router_exchange(
        router, [token, spender], [[0, 1, 1, 1, 2]], 10, 9, spender, pools=[router]
    )
    assert swap.target == router
    head, body = swap.data[:4], swap.data[4:]
    assert (
        head
        == contract_call(
            router,
            "exchange(address[11],uint256[5][5],uint256,uint256,address[5],address)",
            [token] * 11,
            [[0] * 5] * 5,
            0,
            0,
            [router] * 5,
            router,
        ).data[:4]
    )
    zero = "0x" + "00" * 20
    assert body == encode(
        ["address[11]", "uint256[5][5]", "uint256", "uint256", "address[5]", "address"],
        [
            [token, spender] + [zero] * 9,
            [[0, 1, 1, 1, 2]] + [[0] * 5] * 4,
            10,
            9,
            [router] + [zero] * 4,
            spender,
        ],
    )
    assert TakePlan("0xA", "0xB").max_amount == MAX_UINT256
