import boa


from ..conftest import ETH_ADDRESS


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
        burner.recover([weth, ETH_ADDRESS])

    assert weth.balanceOf(burner) == 0
    assert boa.env.get_balance(burner.address) == 0
    assert weth.balanceOf(fee_collector) == 10 ** 18
    assert boa.env.get_balance(fee_collector.address) == 10 ** 18
