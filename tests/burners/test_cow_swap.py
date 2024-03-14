from copy import deepcopy

import pytest
import boa


from ..conftest import Epoch, ZERO_ADDRESS


APP_DATA = "0x5af0ee7dec5167f618066986345e64763b2d91718dfb37f9969a1b0670a83e71"


@pytest.fixture(scope="module", autouse=True)
def preset(set_epoch):
    set_epoch(Epoch.EXCHANGE)


@pytest.fixture(scope="module")
def cow_swap(admin):
    with boa.env.prank(admin):
        return boa.loads("""
struct ConditionalOrderParams:
    handler: address
    salt: bytes32
    staticData: Bytes[STATIC_DATA_LEN]
STATIC_DATA_LEN: constant(uint256) = 20
@external
def create(params: ConditionalOrderParams, dispatch: bool):
    pass
""")


@pytest.fixture(scope="module", autouse=True)
def burner(admin, fee_collector, cow_swap):
    with boa.env.prank(admin):
        burner = boa.load("contracts/burners/CowSwapBurner.vy", fee_collector, cow_swap, cow_swap)
        fee_collector.set_burner(burner)
    return burner


def test_burn(burner, weth, arve):
    burner.burn([weth], arve)
    assert burner.created(weth)


def test_get_tradeable_order(burner, fee_collector, weth, target, arve):
    weth._mint_for_testing(burner, 10 ** weth.decimals())
    order = burner.getTradeableOrder(burner.address, arve, b"", bytes.fromhex(weth.address[2:]), b"")
    assert order[0] == weth.address, "Wrong sellToken"
    assert order[1] == target.address, "Wrong buyToken"
    assert order[2] == fee_collector.address, "Wrong receiver"
    assert order[3] == 10 ** weth.decimals(), "Wrong sellAmount"
    # buyAmount undefined
    assert order[5] == fee_collector.epoch_time_frame(Epoch.EXCHANGE)[1]
    assert order[6].hex() == APP_DATA[2:],  "Wrong appData"
    assert order[7] == 0,  "Positive feeAmount"
    assert order[8].hex() == "f3b277728b3fee749481eb3e0b3b48980dbbab78658fc419025cb16eee346775", "Wrong kind"
    assert order[9], "Not partiallyFillable"
    assert order[10].hex()[24:] == weth.address[2:].lower(), "Wrong location"
    assert order[11].hex()[24:] == target.address[2:].lower(), "Wrong location"

    current_order = burner.get_current_order()
    for i in range(12):
        if i in [0, 3, 10]:
            continue
        assert order[i] == current_order[i]


def test_verify(burner, coins, arve, target):
    coins = [coin for coin in coins if coin != target]
    coin = coins[0]
    coin._mint_for_testing(burner, 10 ** coin.decimals())
    params = [
        burner.address,  # _owner
        ZERO_ADDRESS,  # _sender
        bytes.fromhex(""),  # _hash
        bytes.fromhex(""),  # _domain_separator
        bytes.fromhex(""),  # _ctx
        bytes.fromhex(coin.address[2:]),  # _static_input
        bytes.fromhex(""),  # _offchain_input
        burner.get_current_order(coin),
    ]

    with boa.env.prank(arve):
        burner.verify(*params)

    # Order implementations MUST validate / verify offchainInput
    invalid_params = deepcopy(params)
    invalid_params[6] = bytes.fromhex("00")
    with boa.reverts():
        burner.verify(*invalid_params)

    invalid_params[6] = bytes.fromhex("0100")
    with boa.reverts():
        burner.verify(*invalid_params)

    # The verify method MUST revert with OrderNotValid(string) if the parameters in staticInput do not correspond to a
    # valid order.
    invalid_params = deepcopy(params)
    invalid_params[5] = bytes.fromhex(coins[1].address[2:])  # Wrong sellToken
    with boa.reverts("OrderNotValid()"):
        burner.verify(*invalid_params)

    invalid_params = deepcopy(params)
    invalid_params[7] = list(invalid_params[7])
    invalid_params[7][1] = coins[1].address  # Wrong buyToken
    with boa.reverts("OrderNotValid()"):
        burner.verify(*invalid_params)
