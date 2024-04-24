import boa
import boa_solidity
import pytest

from enum import IntFlag
from typing import Callable


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ETH_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
WEEK = 7 * 24 * 3600


class Epoch(IntFlag):
    SLEEP = 1
    COLLECT = 2
    EXCHANGE = 4
    FORWARD = 8


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
def target(admin):
    with boa.env.prank(admin):
        return boa.load("contracts/testing/ERC20Mock.vy", "Curve Stablecoin", "crvUSD", 18)


@pytest.fixture(scope="session")
def coins(admin, weth, target):
    with boa.env.prank(admin):
        return list(sorted([
            boa.load("contracts/testing/ERC20Mock.vy", "Curve DAO", "CRV", 18),
            boa.load("contracts/testing/ERC20Mock.vy", "Bitcoin", "BTC", 8),
            boa.load("contracts/testing/ERC20Mock.vy", "Chinese Yuan", "CNY", 2),
            weth,
            target,
        ], key=lambda contract: int(contract.address, base=16)))


@pytest.fixture(scope="session")
def fee_collector(admin, emergency_admin, target, weth):
    with boa.env.prank(admin):
        return boa.load("contracts/FeeCollector.vy", target, weth, admin, emergency_admin)


@pytest.fixture(scope="session")
def set_epoch(fee_collector) -> Callable[[Epoch], None]:
    def inner(epoch: Epoch):
        ts = sum(fee_collector.epoch_time_frame(epoch)) // 2  # middle fo the period for the fee
        diff = ts - boa.env.vm.state.timestamp
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
        hooker = boa.load("contracts/Hooker.vy", fee_collector)
        fee_collector.set_hooker(hooker)
    return hooker


@pytest.fixture(scope="module")
def multicall(admin, fee_collector):
    with boa.env.prank(admin):
        multicall = boa_solidity.load_partial_solc("contracts/testing/Multicall3.sol").deploy()
        fee_collector.set_multicall(multicall)
    return multicall
