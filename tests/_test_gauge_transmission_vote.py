import boa
import pytest

import scripts.gauge_transmission_vote as script


@pytest.fixture(scope="module")
def transmitter():
    return boa.loads(
        """
# @version 0.3.10

struct GasTopUp:
    amount: uint256
    token: address
    receiver: address

@external
@payable
def transmit(
    _min_amount: uint256,
    _gauges: DynArray[address, 128],
    _gas_top_ups: DynArray[GasTopUp, 128],
    _eth_refund: address,
) -> uint256:
    return 1

@external
@view
def get_gas_top_ups(_gauges: DynArray[address, 128]) -> DynArray[GasTopUp, 128]:
    return []
"""
    )


@pytest.fixture(scope="module")
def gauge_hook(transmitter):
    return boa.load("contracts/hooks/GaugeTransmissionHook.vy", transmitter, script.MIN_AMOUNT)


def test_build_transmission_hook(gauge_hook):
    hook = script.build_transmission_hook(gauge_hook)

    assert hook[0] == gauge_hook.address
    assert hook[1] == gauge_hook.transmit.prepare_calldata(
        ["0x0000000000000000000000000000000000000001"],
        "0x0000000000000000000000000000000000000001",
    )[:4]
    assert hook[2] == (
        script.MAX_PAYOUT,
        (0, 0, script.WEEKLY_LIMIT),
        script.WINDOW_START,
        script.WINDOW_END,
        True,
    )
    assert hook[3] is False
