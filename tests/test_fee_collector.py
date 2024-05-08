import boa
import pytest

from .conftest import Epoch, ETH_ADDRESS, ZERO_ADDRESS, WEEK


@pytest.fixture(scope="module", autouse=True)
def preset(burner, hooker, admin):
    # Increase hooker.buffer_amount
    with boa.env.prank(admin):
        hooker.set_hooks([(ZERO_ADDRESS, b"", (10 ** 9, 0, 0, 0, False), False)])


@pytest.fixture(scope="module")
def coins(coins, target):
    return [coin for coin in coins if coin != target]


def test_burn(fee_collector, arve, weth):
    # raw ETH
    boa.env.set_balance(arve, 10 ** 18)
    with boa.env.prank(arve):
        assert weth.balanceOf(fee_collector) == 0
        fee_collector.burn(ETH_ADDRESS, value=10 ** 18)
        assert boa.env.get_balance(arve) == 0
        assert boa.env.get_balance(fee_collector.address) == 0
        assert weth.balanceOf(fee_collector) == 10 ** 18

    # ERC20
    weth._mint_for_testing(arve, 10 ** 18)
    with boa.env.prank(arve):
        weth.approve(fee_collector, 10 ** 18)
        fee_collector.burn(weth)
        assert weth.balanceOf(arve) == 0
        assert weth.balanceOf(fee_collector) == 2 * 10 ** 18


def test_collect(fee_collector, set_epoch, coins, weth, arve, burle, burner):
    set_epoch(Epoch.COLLECT)
    with boa.env.prank(arve):
        fee_collector.collect([coin.address for coin in coins], burle)

    with boa.env.prank(arve):
        with boa.reverts("Coins not sorted"):
            fee_collector.collect([weth.address, weth.address])

    set_epoch(Epoch.EXCHANGE)
    with boa.env.prank(arve):
        with boa.reverts("Wrong epoch"):
            fee_collector.collect([weth.address])


def test_transfer(fee_collector, burner, arve, burle, coins, set_epoch):
    amounts = [10 ** coin.decimals() for coin in coins]
    for coin, amount in zip(coins, amounts):
        coin._mint_for_testing(fee_collector, amount)

    set_epoch(Epoch.COLLECT)
    # Partial transfer
    with boa.env.prank(burner.address):
        fee_collector.transfer([(coin, arve, amount // 100) for coin, amount in zip(coins, amounts)])
    for coin, amount in zip(coins, amounts):
        assert coin.balanceOf(arve) == amount // 100

    set_epoch(Epoch.EXCHANGE)
    # Whole balance transfer
    with boa.env.prank(burner.address):
        fee_collector.transfer([(coin, burle, 2 ** 256 - 1) for coin, amount in zip(coins, amounts)])
    for coin, amount in zip(coins, amounts):
        assert coin.balanceOf(burle) == amount - amount // 100

    with boa.env.prank(arve):
        with boa.reverts("Only Burner"):
            fee_collector.transfer([])

    set_epoch(Epoch.FORWARD)
    with boa.env.prank(burner.address):
        with boa.reverts("Wrong Epoch"):
            fee_collector.transfer([])


def test_forward(fee_collector, set_epoch, target, arve, burle, hooker):
    target._mint_for_testing(fee_collector, 10 ** target.decimals())

    set_epoch(Epoch.FORWARD)
    with boa.env.prank(arve):
        fee_collector.forward([], burle)
    assert target.balanceOf(arve) == 0
    assert target.balanceOf(fee_collector) == hooker.buffer_amount()
    received_amount = 10 ** target.decimals() - hooker.buffer_amount()
    assert 0 < target.balanceOf(burle) <= received_amount * fee_collector.max_fee(Epoch.FORWARD) // 10 ** 18
    assert target.balanceOf(burle) + target.balanceOf(hooker) == received_amount
    assert target.allowance(fee_collector, hooker) == hooker.buffer_amount()

    with boa.env.prank(hooker.address):
        target.transferFrom(fee_collector, burle, hooker.buffer_amount() // 2)
    # Does not allow more within a week
    boa.env.time_travel(seconds=3600)
    with boa.env.prank(arve):
        fee_collector.forward([], burle)
    assert target.allowance(fee_collector, hooker) == hooker.buffer_amount() // 2

    # Allows more next week
    boa.env.time_travel(seconds=WEEK - 3600)
    with boa.env.prank(arve):
        fee_collector.forward([], burle)
    assert target.allowance(fee_collector, hooker) == hooker.buffer_amount()


def test_admin(fee_collector, admin, arve, burner, hooker, target):
    killed = [(arve, Epoch.COLLECT)]

    # Everything works for admin
    with boa.env.anchor():
        with boa.env.prank(admin):
            fee_collector.recover([], arve)
            fee_collector.set_max_fee(2, 5 * 10 ** (18 - 2))
            fee_collector.set_burner(burner)
            fee_collector.set_hooker(hooker)
            fee_collector.set_target(target)
            fee_collector.set_killed(killed)
            fee_collector.set_emergency_owner(arve)
            fee_collector.set_owner(arve)

    # Third party can not access
    with boa.env.prank(arve):
        with boa.reverts("Only owner"):
            fee_collector.recover([], arve)
        with boa.reverts("Only owner"):
            fee_collector.set_max_fee(2, 5 * 10 ** (18 - 2))
        with boa.reverts("Only owner"):
            fee_collector.set_burner(burner)
        with boa.reverts("Only owner"):
            fee_collector.set_hooker(hooker)
        with boa.reverts("Only owner"):
            fee_collector.set_hooker(target)
        with boa.reverts("Only owner"):
            fee_collector.set_killed(killed)
        with boa.reverts("Only owner"):
            fee_collector.set_emergency_owner(arve)
        with boa.reverts("Only owner"):
            fee_collector.set_owner(arve)


def test_emergency_admin(fee_collector, emergency_admin, arve):
    killed = [(arve, Epoch.COLLECT)]

    # Everything works for emergency owner
    with boa.env.anchor():
        with boa.env.prank(emergency_admin):
            fee_collector.recover([], arve)
            fee_collector.set_killed(killed)

    # Third part can not access
    with boa.env.prank(arve):
        with boa.reverts("Only owner"):
            fee_collector.recover([], arve)
        with boa.reverts("Only owner"):
            fee_collector.set_killed(killed)


@pytest.mark.parametrize("to_kill", [
    Epoch.COLLECT, Epoch.EXCHANGE, Epoch.FORWARD,
    Epoch.COLLECT | Epoch.EXCHANGE, Epoch.COLLECT | Epoch.FORWARD, Epoch.EXCHANGE | Epoch.FORWARD,
    Epoch.COLLECT | Epoch.EXCHANGE | Epoch.FORWARD])
def test_killed_all(fee_collector, set_epoch, weth, target, admin, arve, to_kill):
    killed = [(ZERO_ADDRESS, to_kill)]
    with boa.env.prank(admin):
        fee_collector.set_killed(killed)

    # Collect
    set_epoch(Epoch.COLLECT)
    if Epoch.COLLECT in to_kill:
        with boa.reverts("Killed epoch"):
            fee_collector.collect([weth])
    else:
        fee_collector.collect([weth])

    # Exchange
    set_epoch(Epoch.EXCHANGE)
    assert not fee_collector.exchange([weth]) == Epoch.EXCHANGE in to_kill

    # Forward
    set_epoch(Epoch.FORWARD)
    if Epoch.FORWARD in to_kill:
        with boa.reverts("Killed"):
            fee_collector.forward([])
    else:
        fee_collector.forward([])


def test_killed(fee_collector, set_epoch, coins, target, admin, burner):
    killed = [(coin.address, to_kill) for coin, to_kill in
              zip(coins[:3], [Epoch.COLLECT, Epoch.EXCHANGE, Epoch.COLLECT | Epoch.EXCHANGE])]
    with boa.env.prank(admin):
        fee_collector.set_killed(killed)

    # Collect
    set_epoch(Epoch.COLLECT)
    fee_collector.collect([coins[1]] + coins[3:])
    with boa.reverts("Killed coin"):
        fee_collector.collect([coins[0].address])
    with boa.reverts("Killed coin"):
        fee_collector.collect([coins[2].address])
    with boa.reverts("Killed coin"):
        fee_collector.collect([coin.address for coin in coins[1:]])

    with boa.env.prank(burner.address):
        with boa.reverts("Killed coin"):
            fee_collector.transfer([(coins[0], admin, 0)])
        with boa.reverts("Killed coin"):
            fee_collector.transfer([(coins[2], admin, 0)])
        with boa.reverts("Killed coin"):
            fee_collector.transfer([(coin, admin, 0) for coin in coins[1:]])

    # Exchange
    set_epoch(Epoch.EXCHANGE)
    assert fee_collector.exchange([coins[0]] + coins[3:])
    assert not fee_collector.exchange([coins[1]])
    assert not fee_collector.exchange([coins[2]])
    assert not fee_collector.exchange([coins[0], coins[2]] + coins[3:])

    with boa.env.prank(burner.address):
        with boa.reverts("Killed coin"):
            fee_collector.transfer([(coins[1], admin, 0)])
        with boa.reverts("Killed coin"):
            fee_collector.transfer([(coins[2], admin, 0)])
        with boa.reverts("Killed coin"):
            fee_collector.transfer([(coin, admin, 0) for coin in [coins[0], coins[2]] + coins[3:]])

    # Forward
    set_epoch(Epoch.FORWARD)
    fee_collector.forward([])
    with boa.env.prank(admin):
        fee_collector.set_killed([(target.address, Epoch.FORWARD)])
    with boa.reverts("Killed"):
        fee_collector.forward([])
    with boa.env.prank(admin):
        fee_collector.set_killed([(target.address, Epoch.EXCHANGE | Epoch.FORWARD)])
    with boa.reverts("Killed"):
        fee_collector.forward([])


def test_epoch(fee_collector):
    # Continuous
    week_start, prev = fee_collector.epoch_time_frame(Epoch.SLEEP)
    for epoch in [Epoch.COLLECT, Epoch.EXCHANGE, Epoch.FORWARD]:
        start, end = fee_collector.epoch_time_frame(epoch)
        assert start <= end
        assert prev == start
        prev = end
    assert prev - week_start == 7 * 24 * 60 * 60  # a week

    with boa.reverts("Bad Epoch"):
        fee_collector.epoch_time_frame(Epoch.SLEEP | Epoch.FORWARD)

    for epoch in [Epoch.SLEEP, Epoch.COLLECT, Epoch.EXCHANGE, Epoch.FORWARD]:
        start, end = fee_collector.epoch_time_frame(epoch)
        assert fee_collector.epoch(start) == epoch
        assert fee_collector.epoch((start + end) // 2) == epoch
        assert fee_collector.epoch(end - 1) == epoch


def test_fee(fee_collector, admin):
    for epoch, max_fee in zip([Epoch.COLLECT, Epoch.EXCHANGE, Epoch.FORWARD],
                              [2 * 10 ** 16, 3 * 10 ** 16, 4 * 10 ** 16]):
        with boa.env.prank(admin):
            fee_collector.set_max_fee(epoch, max_fee)
        start, end = fee_collector.epoch_time_frame(epoch)
        prev_fee = fee_collector.fee(epoch, start)
        assert prev_fee <= max_fee // 10
        for ts in range(start + 1, end, (end - start) // 12):
            fee = fee_collector.fee(epoch, ts)
            assert prev_fee <= fee
            prev_fee = fee
        assert max_fee * 9 // 10 <= prev_fee <= max_fee

    with boa.env.prank(admin):
        with boa.reverts("Bad Epoch"):
            fee_collector.set_max_fee(Epoch.COLLECT | Epoch.FORWARD, 10 ** 16)
        with boa.reverts("Bad max_fee"):
            fee_collector.set_max_fee(Epoch.COLLECT, 10 ** 18 + 1)


def test_recover(fee_collector, coins, admin, arve):
    amounts = []
    with boa.env.prank(admin):
        for coin in coins:
            coin._mint_for_testing(fee_collector, 10 ** coin.decimals())
            amounts.append(10 ** coin.decimals())
    amounts[0] = 2 ** 256 - 1
    amounts[-1] //= 2

    boa.env.set_balance(fee_collector.address, 10 ** 18)
    coins.append(ETH_ADDRESS)
    amounts.append(10 ** 18 // 3)

    with boa.env.prank(admin):
        fee_collector.recover([(coin.address if hasattr(coin, "address") else coin, amount)
                               for coin, amount in zip(coins, amounts)], arve)

    for coin in coins[:-2]:
        assert coin.balanceOf(fee_collector) == 0
        assert coin.balanceOf(arve) == 10 ** coin.decimals()
    assert coins[-2].balanceOf(fee_collector) == 10 ** coins[-2].decimals() - amounts[-2]
    assert coins[-2].balanceOf(arve) == amounts[-2]
    assert boa.env.get_balance(fee_collector.address) == 10 ** 18 - amounts[-1]
    assert boa.env.get_balance(arve) == amounts[-1]
