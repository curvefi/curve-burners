import boa
import pytest
from eth_abi import encode
from eth_hash.auto import keccak

from .conftest import custom_err


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WAD = 10**18
WEEK = 7 * 24 * 60 * 60

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
# Reviewed decay bound reused from the dutch auction test suite.
DECAY_FACTOR_RAY = 992031276831159793484252056

STAGED_AMOUNT = 100 * WAD

MINT_SELECTOR = keccak(b"_mint_for_testing(address,uint256)")[:4]
TRANSFER_FROM_SELECTOR = keccak(b"transferFrom(address,address,uint256)")[:4]


def _timestamp() -> int:
    return boa.env.evm.vm.state.timestamp


def _mint_call(token, receiver: str, amount: int) -> tuple:
    """Route step standing in for a swap: mints want as if a DEX paid out."""
    return (
        token.address,
        MINT_SELECTOR + encode(["address", "uint256"], [receiver, amount]),
    )


@pytest.fixture(autouse=True)
def anchor():
    with boa.env.anchor():
        yield


@pytest.fixture(scope="module")
def solver():
    return boa.env.generate_address("solver")


@pytest.fixture(scope="module")
def profit_receiver():
    return boa.env.generate_address("profit_receiver")


@pytest.fixture(scope="module")
def proceeds_receiver():
    return boa.env.generate_address("proceeds_receiver")


@pytest.fixture(scope="module")
def want():
    return boa.load("contracts/testing/ERC20Mock.vy", "Curve Stablecoin", "crvUSD", 18)


@pytest.fixture(scope="module")
def sell_token():
    return boa.load("contracts/testing/ERC20Mock.vy", "Curve DAO", "CRV", 18)


@pytest.fixture(scope="module")
def auction(want, proceeds_receiver):
    # Deploy shortly after a week boundary so per-test staging and short time
    # travels stay inside the harness auction frame.
    into_next_epoch = WEEK - _timestamp() % WEEK + 3600
    boa.env.time_travel(seconds=into_next_epoch)
    role_source = boa.load(
        "contracts/testing/dutch_auction/RoleSourceMock.vy",
        boa.env.generate_address("owner"),
        boa.env.generate_address("emergency_owner"),
    )
    return boa.load(
        "contracts/testing/dutch_auction/CoreHarness.vy",
        want.address,
        proceeds_receiver,
        ZERO_ADDRESS,
        role_source.address,
        START_TOTAL,
        FLOOR_TOTAL,
        DECAY_FACTOR_RAY,
        STEP_DURATION,
    )


@pytest.fixture(scope="module")
def taker():
    return boa.load("contracts/AuctionTaker.vy")


@pytest.fixture(scope="module")
def stage(auction):
    def _stage(token, amount: int = STAGED_AMOUNT) -> int:
        token._mint_for_testing(auction, amount)
        return auction.stage(token)

    return _stage


def test_route_fills_and_forwards_exact_payment(auction, taker, stage, sell_token, want,
                                                solver, profit_receiver, proceeds_receiver):
    stage(sell_token)
    amount = STAGED_AMOUNT // 4
    payment = auction.getAmountNeeded(sell_token.address, amount)

    # The "swap" mints exactly the quoted payment, so the profit is only the
    # unspent lot tokens the route never consumed.
    with boa.env.prank(solver):
        profit = taker.take_with_route(
            auction.address,
            sell_token.address,
            amount,
            0,
            profit_receiver,
            [_mint_call(want, taker.address, payment)],
        )

    assert profit == 0
    assert want.balanceOf(proceeds_receiver) == payment
    assert sell_token.balanceOf(profit_receiver) == amount
    assert want.balanceOf(taker.address) == 0
    assert sell_token.balanceOf(taker.address) == 0
    assert want.allowance(taker.address, auction.address) == 0


def test_want_surplus_is_profit_and_min_profit_gates(auction, taker, stage, sell_token, want,
                                                     solver, profit_receiver):
    stage(sell_token)
    amount = STAGED_AMOUNT // 4
    payment = auction.getAmountNeeded(sell_token.address, amount)
    surplus = 3 * WAD

    with boa.env.prank(solver):
        with boa.reverts(custom_err("ProfitShortfall()")):
            taker.take_with_route(
                auction.address,
                sell_token.address,
                amount,
                surplus + 1,
                profit_receiver,
                [_mint_call(want, taker.address, payment + surplus)],
            )
        profit = taker.take_with_route(
            auction.address,
            sell_token.address,
            amount,
            surplus,
            profit_receiver,
            [_mint_call(want, taker.address, payment + surplus)],
        )

    assert profit == surplus
    assert want.balanceOf(profit_receiver) == surplus
    assert sell_token.balanceOf(profit_receiver) == amount


def test_multi_step_route_runs_in_order(auction, taker, stage, sell_token, want, solver,
                                        profit_receiver):
    # Two mints summing to the payment prove every route step executes.
    stage(sell_token)
    amount = STAGED_AMOUNT // 10
    payment = auction.getAmountNeeded(sell_token.address, amount)

    with boa.env.prank(solver):
        taker.take_with_route(
            auction.address,
            sell_token.address,
            amount,
            0,
            profit_receiver,
            [
                _mint_call(want, taker.address, payment // 2),
                _mint_call(want, taker.address, payment - payment // 2),
            ],
        )
    assert sell_token.balanceOf(profit_receiver) == amount


def test_failing_route_step_reverts_whole_take(auction, taker, stage, sell_token, want,
                                               solver, profit_receiver, proceeds_receiver):
    stage(sell_token)
    amount = STAGED_AMOUNT // 4
    # transferFrom without allowance reverts inside the route; the raw_call
    # bubbles it up and the whole take unwinds.
    failing_call = (
        want.address,
        TRANSFER_FROM_SELECTOR
        + encode(
            ["address", "address", "uint256"],
            [proceeds_receiver, taker.address, WAD],
        ),
    )
    with boa.env.prank(solver):
        with boa.reverts():
            taker.take_with_route(
                auction.address, sell_token.address, amount, 0, profit_receiver,
                [failing_call],
            )
    assert sell_token.balanceOf(profit_receiver) == 0
    assert auction.available(sell_token.address) == STAGED_AMOUNT


def test_underfunded_route_reverts(auction, taker, stage, sell_token, want, solver,
                                   profit_receiver):
    stage(sell_token)
    amount = STAGED_AMOUNT // 4
    payment = auction.getAmountNeeded(sell_token.address, amount)

    # The route leaves less want than quoted: the auction's payment pull fails.
    with boa.env.prank(solver):
        with boa.reverts():
            taker.take_with_route(
                auction.address,
                sell_token.address,
                amount,
                0,
                profit_receiver,
                [_mint_call(want, taker.address, payment - 1)],
            )


def test_callback_rejects_direct_calls(taker, auction, solver):
    with boa.env.prank(solver):
        with boa.reverts(custom_err("OnlyTakenAuction()")):
            taker.auctionTakeCallback(ZERO_ADDRESS, solver, 0, 0, b"")
    # Even the auction itself cannot enter outside an active take.
    with boa.env.prank(auction.address):
        with boa.reverts(custom_err("OnlyTakenAuction()")):
            taker.auctionTakeCallback(ZERO_ADDRESS, solver, 0, 0, b"")


def test_input_validation(taker, solver, profit_receiver, auction, sell_token):
    with boa.env.prank(solver):
        with boa.reverts(custom_err("ZeroAuction()")):
            taker.take_with_route(ZERO_ADDRESS, sell_token.address, WAD, 0,
                                  profit_receiver, [])
        with boa.reverts(custom_err("ZeroReceiver()")):
            taker.take_with_route(auction.address, sell_token.address, WAD, 0,
                                  ZERO_ADDRESS, [])
