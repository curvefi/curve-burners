from copy import deepcopy

import pytest
import boa


from ..conftest import Epoch, ZERO_ADDRESS


APP_DATA = "0x058315b749613051abcbf50cf2d605b4fa4a41554ec35d73fd058fc530da559f"


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
@external
@view
def domainSeparator() -> bytes32:
    return 0x8f05589c4b810bc2f706854508d66d447cd971f8354a4bb0b3471ceb0a466bc7
@external
@view
def isValidSafeSignature(safe: address, sender: address, _hash: bytes32, _domainSeparator: bytes32, typeHash: bytes32,
    encodeData: Bytes[15 * 32],
    payload: Bytes[(32 + 3 + 1 + 8) * 32],
) -> bytes4:
    return 0x5fd7e97d
""")


@pytest.fixture(scope="module", autouse=True)
def burner(admin, fee_collector, cow_swap):
    with boa.env.prank(admin):
        burner = boa.load("contracts/burners/CowSwapBurner.vy", fee_collector, cow_swap, cow_swap)
        fee_collector.set_burner(burner)
    return burner


def test_erc165(burner):
    assert burner.supportsInterface(bytes.fromhex("01ffc9a7"))

    # SignatureVerifierMuxer not supported
    with boa.reverts():
        burner.supportsInterface(bytes.fromhex("62af8dc2"))


def test_burn(burner, weth, arve):
    burner.burn([weth.address], arve)
    assert burner.created(weth)


def test_get_tradeable_order(burner, fee_collector, weth, target, arve, set_epoch, admin):
    next_ts = fee_collector.epoch_time_frame(Epoch.EXCHANGE, boa.env.vm.state.timestamp + 7 * 24 * 3600)[0]
    with boa.reverts(f"PollTryAtEpoch({next_ts},)"):  # Zero balance
        burner.getTradeableOrder(burner.address, arve, b"", bytes.fromhex(weth.address[2:]), b"")

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
    assert order[10].hex() == "5a28e9363bb942b639270062aa6bb295f434bcdfc42c97267bf003f272060dc9", "Wrong location"
    assert order[11].hex() == "5a28e9363bb942b639270062aa6bb295f434bcdfc42c97267bf003f272060dc9", "Wrong location"

    current_order = burner.get_current_order()
    for i in range(12):
        if i in [0, 3, 10]:
            continue
        assert order[i] == current_order[i]

    set_epoch(Epoch.FORWARD)
    with boa.reverts(f"PollTryAtEpoch({next_ts},)"):  # Outdated
        burner.getTradeableOrder(burner.address, arve, b"", bytes.fromhex(weth.address[2:]), b"")

    set_epoch(Epoch.EXCHANGE)
    next_ts = fee_collector.epoch_time_frame(Epoch.EXCHANGE, boa.env.vm.state.timestamp + 7 * 24 * 3600)[0]
    with boa.env.prank(admin):
        fee_collector.set_killed([(weth, Epoch.EXCHANGE)])
    with boa.reverts(f"PollTryAtEpoch({next_ts},)"):  # killed
        burner.getTradeableOrder(burner.address, arve, b"", bytes.fromhex(weth.address[2:]), b"")


def test_verify(burner, coins, arve, target, fee_collector, admin):
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
    with boa.reverts("OrderNotValid(NonZeroOffchainInput)"):
        burner.verify(*invalid_params)

    invalid_params[6] = bytes.fromhex("0100")
    with boa.reverts():  # Overflow error
        burner.verify(*invalid_params)

    # The verify method MUST revert with OrderNotValid(string) if the parameters in staticInput do not correspond to a
    # valid order.
    invalid_params = deepcopy(params)
    invalid_params[5] = bytes.fromhex(coins[1].address[2:])  # Wrong sellToken
    with boa.reverts("OrderNotValid(BadOrder)"):
        burner.verify(*invalid_params)

    invalid_params = deepcopy(params)
    invalid_params[7] = list(invalid_params[7])
    invalid_params[7][1] = coins[1].address  # Wrong buyToken
    with boa.reverts("OrderNotValid(BadOrder)"):
        burner.verify(*invalid_params)

    with boa.env.prank(admin):
        fee_collector.set_killed([(coin, Epoch.EXCHANGE)])
    with boa.reverts("OrderNotValid(NotAllowed)"):
        burner.verify(*params)
