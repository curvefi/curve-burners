import boa


from ..conftest import ETH_ADDRESS, Epoch


def test_version(burner):
    assert burner.VERSION() == "XYZ"


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


def test_push_target(burner, target, fee_collector, arve):
    target._mint_for_testing(burner, 10 ** target.decimals())

    with boa.env.prank(arve):
        burner.push_target()
    assert target.balanceOf(burner) == 0
    assert target.balanceOf(arve) == 0
    assert target.balanceOf(fee_collector) == 10 ** target.decimals()


def test_erc165(burner):
    assert burner.supportsInterface(bytes.fromhex("01ffc9a7"))


def test_admin(burner, admin, emergency_admin, arve):
    with boa.env.prank(admin):
        burner.recover([])
    with boa.env.prank(emergency_admin):
        burner.recover([])

    with boa.env.prank(arve):
        with boa.reverts("Only owner"):
            burner.recover([])


def test_recover_balance(burner, fee_collector, admin, emergency_admin, arve, weth):
    weth._mint_for_testing(burner, 10 ** 18)
    boa.env.set_balance(burner.address, 10 ** 18)

    with boa.env.prank(admin):
        burner.recover([weth.address, ETH_ADDRESS])

    assert weth.balanceOf(burner) == 0
    assert boa.env.get_balance(burner.address) == 0
    assert weth.balanceOf(fee_collector) == 10 ** 18
    assert boa.env.get_balance(fee_collector.address) == 10 ** 18
