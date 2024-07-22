import math

import boa
import boa_solidity
import pytest

from hypothesis import given, settings
from hypothesis import strategies as st

from unitary.conftest import ETH_ADDRESS, Epoch, WEEK


@pytest.fixture(scope="module", autouse=True)
def burner(admin, fee_collector):
    with boa.env.prank(admin):
        burner = boa.load("contracts/burners/DutchAuctionBurner.vy",
                          fee_collector, 10 * 10 ** 18, 10_000, [], 10 ** 18 // 2)
        fee_collector.set_burner(burner)
    return burner


@pytest.fixture(scope="module", autouse=True)
def multicall():
    deployer = boa_solidity.load_partial_solc("contracts/testing/Multicall3.sol", compiler_args={"solc_version": "0.8.12"})
    boa.env.deploy_code(bytecode=deployer.bytecode, override_address="0xcA11bde05977b3631167028862bE2a173976CA11")
    return deployer.at("0xcA11bde05977b3631167028862bE2a173976CA11")


@pytest.mark.xfail
def test_version(burner):
    assert burner.VERSION() == "DutchAuction"


@pytest.mark.xfail
def test_price(burner, fee_collector, coins):
    amounts = [10 * 10 ** coin.decimals() for coin in coins]
    for coin, amount in zip(coins, amounts):
        coin._mint_for_testing(fee_collector, amount)

    # Decreasing
    start, end = fee_collector.epoch_time_frame(Epoch.EXCHANGE)
    for coin in coins:
        price = 2 ** 256 - 1
        for ts in range(start, end, (end - start) // 100):
            new_price = burner.price(coin, ts)
            assert price > new_price
            price = new_price

    for coin in coins:
        with boa.reverts("Bad time"):
            burner.price(coin, start - 1)
        with boa.reverts("Bad time"):
            burner.price(coin, end)


@pytest.mark.xfail
@pytest.fixture(scope="class")
def mock_fee_collector(fee_collector, admin):
    return boa.loads("""
start: uint256
end: uint256
owner: public(address)
@external
def __init__(_owner: address):
    self.owner = _owner
@external
def set(start: uint256, end: uint256):
    self.start = start
    self.end = end
@external
@view
def epoch_time_frame(_epoch: uint256, _ts: uint256=block.timestamp) -> (uint256, uint256):
    return (self.start, self.end)
""",
                     admin,
                     override_address=fee_collector.address,
                     )


@given(
    lasted=st.integers(min_value=0, max_value=365 * 24 * 3600),
    remaining=st.integers(min_value=1, max_value=365 * 24 * 3600),
    base=st.integers(min_value=10 ** 18 + 10 ** 15, max_value=1_000_000 * 10 ** 18),
)
@settings(deadline=None)
@pytest.mark.xfail
def test_time_amplifier(burner, mock_fee_collector, admin, lasted, remaining, base):
    whole_period = lasted + remaining
    mock_fee_collector.set(0, whole_period)

    with boa.env.prank(admin):
        burner.set_time_amplifier_base(base, int(math.log(base / 10 ** 18) * 10 ** 18))

    assert burner.internal._time_amplifier(lasted) / 10 ** 18 ==\
           pytest.approx(((base / 10 ** 18) ** (remaining / whole_period) - 1) / ((base / 10 ** 18) - 1))


@pytest.mark.xfail
def test_exchange(burner, fee_collector, coins, target, set_epoch, arve, burle):
    amounts = [10 * 10 ** coin.decimals() for coin in coins]
    for coin, amount in zip(coins, amounts):
        coin._mint_for_testing(fee_collector, amount)

    set_epoch(Epoch.EXCHANGE)  # middle of the period
    current_week = boa.env.evm.vm.state.timestamp // WEEK
    prices0 = [burner.price(coin) for coin in coins]
    target_amounts0 = [(amount // 10) * price // 10 ** 18 for amount, price in zip(amounts, prices0)]
    target_total = sum(target_amounts0)

    transfers = [(coin, burle, amount // 10) for coin, amount in zip(coins, amounts)]

    # transfer target myself
    target._mint_for_testing(burner, target_total)
    with boa.env.prank(arve):
        burner.exchange(transfers, [])
    for coin, amount, target_amount in zip(coins, amounts, target_amounts0):
        assert coin.balanceOf(burle) == amount // 10, "Bad coin transfer"
        record = burner.records(coin)
        assert record[1][0] == amount // 10
        assert record[1][1] == target_amount
        assert record[2] == current_week

    # prices soar a bit after exchange
    prices1 = [burner.price(coin) for coin in coins]
    for p0, p1 in zip(prices0, prices1):
        assert p0 <= p1
    target_amounts1 = [(amount // 10) * price // 10 ** 18 for amount, price in zip(amounts, prices1)]
    target_total = sum(target_amounts1)

    # approve target
    target._mint_for_testing(arve, target_total)
    with boa.env.prank(arve):
        target.approve(burner, target_total)
        burner.exchange(transfers, [])
    for coin, amount, target_amounts in zip(coins, amounts, zip(target_amounts0, target_amounts1)):
        assert coin.balanceOf(burle) == 2 * amount // 10, "Bad coin transfer"
        record = burner.records(coin)
        assert record[1][0] == 2 * amount // 10
        assert record[1][1] == sum(target_amounts)
        assert record[2] == current_week

    boa.env.time_travel(seconds=WEEK)

    # prices cooldown over a week
    prices2 = [burner.price(coin) for coin in coins]
    for p1, p2 in zip(prices1, prices2):
        assert p1 <= p2
    target_amounts2 = [(amount // 10) * price // 10 ** 18 for amount, price in zip(amounts, prices2)]
    target_total = sum(target_amounts2)

    # approve target
    target._mint_for_testing(burner, target_total + 3)
    with boa.env.prank(arve):
        burner.exchange(transfers, [])
    for coin, amount, target_amounts in zip(coins, amounts, zip(target_amounts0, target_amounts1, target_amounts2)):
        assert coin.balanceOf(burle) == 3 * amount // 10, "Bad coin transfer"

        # Record update
        record = burner.records(coin)
        assert record[0][0] == amount // 10
        assert record[0][1] == sum(target_amounts[:-1]) // 2
        assert record[1][0] == amount // 10
        assert record[1][1] == target_amounts[-1]
        assert record[2] == current_week + 1

        assert target.balanceOf(burner) == 0, "Coins were not fully swept"


@pytest.mark.xfail
def test_burn_remained(fee_collector, burner, coins, set_epoch, arve, burle):
    """
    Fees are paid out, though coins remain in FeeCollector
    """
    set_epoch(Epoch.COLLECT)

    # Check amounts
    amounts = [10 * 10 ** coin.decimals() for coin in coins]
    for coin, amount in zip(coins, amounts):
        coin._mint_for_testing(fee_collector, amount)

    with boa.env.prank(fee_collector.address):
        burner.burn(coins, burle)

    payouts = [coin.balanceOf(burle) for coin in coins]
    for coin, amount, payout in zip(coins, amounts, payouts):
        assert coin.balanceOf(burner) == 0
        assert amount * fee_collector.max_fee(Epoch.COLLECT) // (2 * 10 ** 18) <= payout <= \
               amount * fee_collector.max_fee(Epoch.COLLECT) // 10 ** 18
        assert payout + coin.balanceOf(fee_collector) == amount

    # Check double spend
    with boa.env.prank(fee_collector.address):
        burner.burn(coins, burle)

    for coin, payout in zip(coins, payouts):
        assert coin.balanceOf(burle) == payout

    # Check new burn
    for coin, amount in zip(coins, amounts):
        coin._mint_for_testing(fee_collector, amount)

    with boa.env.prank(fee_collector.address):
        burner.burn(coins, burle)

    payouts_sum = [coin.balanceOf(burle) for coin in coins]
    for coin, amount, payout, payout_sum in zip(coins, amounts, payouts, payouts_sum):
        assert payout_sum >= 2 * payout  # might be greater since Dutch auction for fee

        assert coin.balanceOf(burner) == 0
        assert 2 * amount * fee_collector.max_fee(Epoch.COLLECT) // (2 * 10 ** 18) <= payout_sum <= \
               2 * amount * fee_collector.max_fee(Epoch.COLLECT) // 10 ** 18
        assert payout_sum + coin.balanceOf(fee_collector) == 2 * amount

    # only_revise
    coins[0]._mint_for_testing(fee_collector, amounts[0])  # increase balance
    with boa.env.prank(fee_collector.address):
        coins[1].transfer(coins[1], amounts[1])  # decrease balance

    burner.burn(coins, burle, True)
    with boa.env.prank(fee_collector.address):
        burner.burn(coins, burle)

    for coin, amount, payout_sum in zip(coins, amounts, payouts_sum):
        assert coin.balanceOf(burle) == payout_sum  # no new coins

    with boa.reverts("Only FeeCollector"):
        burner.burn([], arve)


@pytest.mark.xfail
def test_push_target(burner, target, fee_collector, arve):
    target._mint_for_testing(burner, 10 ** target.decimals())

    with boa.env.prank(arve):
        burner.push_target()
    assert target.balanceOf(burner) == 0
    assert target.balanceOf(arve) == 0
    assert target.balanceOf(fee_collector) == 10 ** target.decimals()


@pytest.mark.xfail
def test_erc165(burner):
    assert burner.supportsInterface(bytes.fromhex("01ffc9a7"))


@pytest.mark.xfail
def test_admin(burner, admin, emergency_admin, arve):
    # Both admins
    with boa.env.prank(admin):
        burner.recover([])
        burner.set_records([])
    with boa.env.prank(emergency_admin):
        burner.recover([])
        burner.set_records([])

    # Only ownership admin
    with boa.env.prank(admin):
        burner.set_records_smoothing(0)
        burner.set_price_parameters(0, 0)
        burner.set_time_amplifier_base(2 * 10 ** 18, int(math.log(2) * 10 ** 18))
    with boa.env.prank(emergency_admin):
        with boa.reverts("Only owner"):
            burner.set_records_smoothing(0)
        with boa.reverts("Only owner"):
            burner.set_price_parameters(0, 0)
        with boa.reverts("Only owner"):
            burner.set_time_amplifier_base(2 * 10 ** 18, int(math.log(2) * 10 ** 18))

    # Third wheel
    with boa.env.prank(arve):
        with boa.reverts("Only owner"):
            burner.recover([])
        with boa.reverts("Only owner"):
            burner.set_records([])
        with boa.reverts("Only owner"):
            burner.set_records_smoothing(0)
        with boa.reverts("Only owner"):
            burner.set_price_parameters(0, 0)
        with boa.reverts("Only owner"):
            burner.set_time_amplifier_base(2 * 10 ** 18, int(math.log(2) * 10 ** 18))


@pytest.mark.xfail
def test_recover_balance(burner, fee_collector, admin, emergency_admin, arve, coins):
    for coin in coins:
        coin._mint_for_testing(burner, 10 ** coin.decimals())
    boa.env.set_balance(burner.address, 10 ** 18)

    with boa.env.prank(admin):
        burner.recover(coins + [ETH_ADDRESS])

    for coin in coins:
        assert coin.balanceOf(burner) == 0
        assert coin.balanceOf(fee_collector) == 10 ** coin.decimals()
    assert boa.env.get_balance(burner.address) == 0
    assert boa.env.get_balance(fee_collector.address) == 10 ** 18
