from copy import deepcopy

import pytest
import boa
from boa import BoaError

from ..conftest import Epoch, ZERO_ADDRESS


APP_DATA = "0x0000000000000000000000000000000000000000000000000000000000000000"


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


def test_version(burner):
    assert burner.VERSION() == "CowSwap"


def test_erc165(burner):
    assert burner.supportsInterface(bytes.fromhex("01ffc9a7"))

    # SignatureVerifierMuxer not supported
    with boa.reverts():
        burner.supportsInterface(bytes.fromhex("62af8dc2"))


def test_burn(burner, weth, arve):
    burner.burn([weth.address], arve)
    assert burner.created(weth)


def test_get_tradeable_order(burner, fee_collector, weth, target, arve, set_epoch, admin):
    def poll_try_at_epoch_error(ts, msg):
        return bytes(boa.eval(f'_abi_encode(convert({ts}, uint256), "{msg}",'
                              f'method_id=method_id("PollTryAtEpoch(uint256,string)"))'))

    next_ts = fee_collector.epoch_time_frame(Epoch.EXCHANGE, boa.env.evm.vm.state.timestamp + 7 * 24 * 3600)[0]
    with pytest.raises(BoaError) as error:
        burner.getTradeableOrder(burner.address, arve, b"", bytes.fromhex(weth.address[2:]), b"")
    assert error.value.args[0].last_frame.vm_error.args[0] == poll_try_at_epoch_error(next_ts, "ZeroBalance")

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
    with pytest.raises(BoaError) as error:  # Outdated
        burner.getTradeableOrder(burner.address, arve, b"", bytes.fromhex(weth.address[2:]), b"")
    assert error.value.args[0].last_frame.vm_error.args[0] == poll_try_at_epoch_error(next_ts, "NotAllowed")

    set_epoch(Epoch.EXCHANGE)
    next_ts = fee_collector.epoch_time_frame(Epoch.EXCHANGE, boa.env.evm.vm.state.timestamp + 7 * 24 * 3600)[0]
    with boa.env.prank(admin):
        fee_collector.set_killed([(weth.address, Epoch.EXCHANGE)])
    with pytest.raises(BoaError) as error:  # killed
        burner.getTradeableOrder(burner.address, arve, b"", bytes.fromhex(weth.address[2:]), b"")
    assert error.value.args[0].last_frame.vm_error.args[0] == poll_try_at_epoch_error(next_ts, "NotAllowed")


def test_verify(burner, coins, arve, target, fee_collector, admin):
    def order_not_valid_error(msg):
        return bytes(boa.eval(f'_abi_encode("{msg}", method_id=method_id("OrderNotValid(string)"))'))

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
    with pytest.raises(BoaError) as error:
        burner.verify(*invalid_params)
    assert error.value.args[0].last_frame.vm_error.args[0] == order_not_valid_error("NonZeroOffchainInput")

    invalid_params[6] = bytes.fromhex("0100")
    with boa.reverts():  # Overflow error
        burner.verify(*invalid_params)

    # The verify method MUST revert with OrderNotValid(string) if the parameters in staticInput do not correspond to a
    # valid order.
    invalid_params = deepcopy(params)
    invalid_params[5] = bytes.fromhex(coins[1].address[2:])  # Wrong sellToken
    with pytest.raises(BoaError) as error:
        burner.verify(*invalid_params)
    assert error.value.args[0].last_frame.vm_error.args[0] == order_not_valid_error("BadOrder")

    invalid_params = deepcopy(params)
    invalid_params[7] = list(invalid_params[7])
    invalid_params[7][1] = coins[1].address  # Wrong buyToken
    with pytest.raises(BoaError) as error:
        burner.verify(*invalid_params)
    assert error.value.args[0].last_frame.vm_error.args[0] == order_not_valid_error("BadOrder")

    with boa.env.prank(admin):
        fee_collector.set_killed([(coin.address, Epoch.EXCHANGE)])
    with pytest.raises(BoaError) as error:
        burner.verify(*params)
    assert error.value.args[0].last_frame.vm_error.args[0] == order_not_valid_error("NotAllowed")
