import boa
import pytest


@pytest.fixture(scope="module")
def bridge():
    return boa.loads("""
from vyper.interfaces import ERC20
@external
def relayTokens(_token: ERC20, _receiver: address, _value: uint256):
    _token.transferFrom(msg.sender, _receiver, _value)
""")


@pytest.fixture(scope="module")
def bridger():
    return boa.load("contracts/hooks/gnosis/GnosisBridger.vy")


@pytest.fixture(scope="module")
def target(bridge):
    source_code = boa.load_partial("contracts/testing/ERC20Mock.vy").compiler_data.source_code
    return boa.loads(source_code + f"""
@external
@view
def bridgeContract() -> address:
    return {bridge.address}
""", "Bridged crvUSD", "crvUSD", 18)


def test_bridger(target, bridger, bridge, arve, burle):
    target._mint_for_testing(arve, 10 ** 18)
    with boa.env.prank(arve):
        target.approve(bridger, 10 ** 18)
        assert target.balanceOf(burle) == 0
        assert target.balanceOf(arve) == 10 * 10 ** 17

        bridger.bridge(target, burle, 10 ** 17)
        assert target.balanceOf(burle) == 10 ** 17
        assert target.balanceOf(arve) == 9 * 10 ** 17

        bridger.bridge(target, burle, 10 ** 17, 10 ** 16)
        assert target.balanceOf(burle) == 2 * 10 ** 17
        assert target.balanceOf(arve) == 8 * 10 ** 17

        bridger.bridge(target, burle, 10 ** 17, 10 ** 17)
        assert target.balanceOf(burle) == 3 * 10 ** 17
        assert target.balanceOf(arve) == 7 * 10 ** 17

        bridger.bridge(target, burle, 10 ** 17, 10 ** 18)  # not enough
        assert target.balanceOf(burle) == 3 * 10 ** 17
        assert target.balanceOf(arve) == 7 * 10 ** 17

        bridger.bridge(target, burle, 2 ** 256 - 1, 10 ** 18)  # not enough
        assert target.balanceOf(burle) == 3 * 10 ** 17
        assert target.balanceOf(arve) == 7 * 10 ** 17

        bridger.bridge(target, burle, 2 ** 256 - 1, 10 ** 17)
        assert target.balanceOf(burle) == 10 ** 18
        assert target.balanceOf(arve) == 0
