from collections.abc import Callable
from enum import IntFlag

import boa
import pytest

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ETH_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
WEEK = 7 * 24 * 3600


class Epoch(IntFlag):
    SLEEP = 1
    COLLECT = 2
    EXCHANGE = 4
    FORWARD = 8


# Thursday 2026-10-29 12:00 UTC: the middle of a SLEEP frame of the FeeCollector
# calendar (START_TIME 1600300800 + n * WEEK + 12 h).
PINNED_START = 1793361600


@pytest.fixture(scope="session", autouse=True)
def pinned_clock():
    """boa seeds the EVM clock from the wall clock, and the FeeCollector calendar
    makes several suites depend on the weekday they start in (staging in COLLECT
    lands in the current week, in SLEEP in the next one). Start every session at
    the same SLEEP instant instead; `time_travel` moves forward from there."""
    boa.env.evm.patch.timestamp = PINNED_START


@pytest.fixture(scope="module", autouse=True)
def fresh_storage_traces():
    """boa keeps `sstore_trace`/`sha3_trace` for the whole session and only resets
    them on `fork_rpc`. A mock deployed at an address an earlier module's mock
    used inherits that mock's nested-HashMap key paths, and boa's storage dump
    on a revert then walks them against the wrong layout (boa 0.2.8,
    `StorageVar.get`: 'IntegerT' object has no attribute 'key_type'). Start
    each module with empty traces."""
    boa.env.sstore_trace.clear()
    boa.env.sha3_trace.clear()


@pytest.fixture(scope="session")
def accounts():
    return [boa.env.generate_address() for _ in range(10)]


@pytest.fixture(scope="session")
def admin():
    return boa.env.generate_address()


@pytest.fixture(scope="session")
def emergency_admin():
    return boa.env.generate_address()


@pytest.fixture(scope="session")
def arve():
    return boa.env.generate_address()


@pytest.fixture(scope="module")
def burle():
    return boa.env.generate_address()


@pytest.fixture(scope="session")
def weth(admin):
    with boa.env.prank(admin):
        return boa.load("contracts/testing/WETH.vy")


@pytest.fixture(scope="session")
def erc20(admin):
    with boa.env.prank(admin):
        return boa.load_partial("contracts/testing/ERC20Mock.vy")


@pytest.fixture(scope="session")
def erc20_no_return(admin):
    with boa.env.prank(admin):
        return boa.load_partial("contracts/testing/ERC20MockNoReturn.vy")


@pytest.fixture(scope="session")
def target(erc20):
    return erc20.deploy("Curve Stablecoin", "crvUSD", 18)


@pytest.fixture(scope="session")
def coins(erc20, erc20_no_return, weth, target):
    return list(
        sorted(
            [
                erc20.deploy("Curve DAO", "CRV", 18),
                erc20.deploy("Bitcoin", "BTC", 8),
                erc20_no_return.deploy("Chinese Yuan", "CNY", 2),
                weth,
                target,
            ],
            key=lambda contract: int(contract.address, base=16),
        )
    )


@pytest.fixture(scope="session")
def fee_collector(admin, emergency_admin, target, weth):
    with boa.env.prank(admin):
        return boa.load("contracts/FeeCollector.vy", target, weth, admin, emergency_admin)


@pytest.fixture(scope="session")
def set_epoch(fee_collector) -> Callable[[Epoch], None]:
    boa.env.time_travel(
        seconds=100 * WEEK
    )  # move forward, so all time travels lead to positive values

    def inner(epoch: Epoch):
        ts = sum(fee_collector.epoch_time_frame(epoch)) // 2  # middle of the period for the fee
        diff = ts - boa.env.evm.vm.state.timestamp
        boa.env.time_travel(seconds=diff + WEEK * (diff // WEEK))

    return inner


@pytest.fixture(scope="module")
def burner(admin, fee_collector):
    with boa.env.prank(admin):
        burner = boa.load("contracts/burners/XYZBurner.vy", fee_collector)
        fee_collector.set_burner(burner)
        fee_collector.set_killed([(ZERO_ADDRESS, 0)])
    return burner


@pytest.fixture(scope="module")
def hooker(admin, fee_collector):
    with boa.env.prank(admin):
        hooker = boa.load("contracts/hooks/Hooker.vy", fee_collector, [], [], [])
        fee_collector.set_hooker(hooker)
    return hooker
