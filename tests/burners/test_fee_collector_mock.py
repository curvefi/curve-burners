"""FeeCollectorMock: the configurable calendar the test-in-prod burner runs on.

The real DutchAuctionBurner is deployed against the mock, so these tests pin
what the burner reads: epoch frames, the COLLECT fee, transfer custody, kill
masks, and the collect -> burn -> stage path.
"""

import boa
import pytest

from tests.burners.conftest import custom_err

WAD = 10**18
ZERO = "0x" + "00" * 20

PERIOD = 3 * 3600
BOUNDS = [15 * 60, 45 * 60, 165 * 60]
SLEEP, COLLECT, EXCHANGE, FORWARD = 1, 2, 4, 8


def ts() -> int:
    return boa.env.evm.patch.timestamp


@pytest.fixture(autouse=True)
def anchor():
    with boa.env.anchor():
        yield


@pytest.fixture(scope="module")
def owner():
    return boa.env.generate_address("owner")


@pytest.fixture(scope="module")
def emergency_owner():
    return boa.env.generate_address("emergency_owner")


@pytest.fixture(scope="module")
def keeper():
    return boa.env.generate_address("keeper")


@pytest.fixture(scope="module")
def erc20():
    return boa.load_partial("contracts/testing/ERC20Mock.vy")


@pytest.fixture(scope="module")
def target(erc20):
    return erc20.deploy("Frax USD", "frxUSD", 18)


@pytest.fixture(scope="module")
def coin(erc20):
    return erc20.deploy("Curve DAO", "CRV", 18)


@pytest.fixture
def mock(owner, emergency_owner, target):
    start_time = ts() - 1
    return boa.load(
        "contracts/testing/dutch_auction/FeeCollectorMock.vy",
        target.address,
        owner,
        emergency_owner,
        start_time,
        PERIOD,
        BOUNDS,
    )


@pytest.fixture
def burner(mock, owner):
    registry = boa.load("contracts/burners/adapters/AdapterRegistry.vy", mock.address)
    burner = boa.load(
        "contracts/burners/DutchAuctionBurner.vy", mock.address, 100 * WAD, 5 * WAD, 60, registry
    )
    with boa.env.prank(owner):
        mock.set_burner(burner.address)
    return burner


def go_to(mock, epoch: int, offset: int = 60) -> None:
    start, _ = mock.epoch_time_frame(epoch)
    if start + offset <= ts():
        start += PERIOD
    boa.env.time_travel(seconds=start + offset - ts())
    assert mock.epoch() == epoch


def test_calendar_frames(mock):
    start_time = mock.start_time()
    assert mock.epoch(start_time) == SLEEP
    assert mock.epoch(start_time + BOUNDS[0]) == COLLECT
    assert mock.epoch(start_time + BOUNDS[1]) == EXCHANGE
    assert mock.epoch(start_time + BOUNDS[2]) == FORWARD
    assert mock.epoch(start_time + PERIOD) == SLEEP
    at = start_time + PERIOD + 7
    assert mock.epoch_time_frame(SLEEP, at) == (
        start_time + PERIOD,
        start_time + PERIOD + BOUNDS[0],
    )
    assert mock.epoch_time_frame(COLLECT, at) == (
        start_time + PERIOD + BOUNDS[0],
        start_time + PERIOD + BOUNDS[1],
    )
    assert mock.epoch_time_frame(EXCHANGE, at) == (
        start_time + PERIOD + BOUNDS[1],
        start_time + PERIOD + BOUNDS[2],
    )
    assert mock.epoch_time_frame(FORWARD, at) == (
        start_time + PERIOD + BOUNDS[2],
        start_time + 2 * PERIOD,
    )
    with boa.reverts(custom_err("BadEpoch()")):
        mock.epoch_time_frame(COLLECT | EXCHANGE)


def test_bad_calendar_rejected(owner, emergency_owner, target):
    deployer = boa.load_partial("contracts/testing/dutch_auction/FeeCollectorMock.vy")
    for bounds in ([0, 10, 20], [10, 10, 20], [10, 20, PERIOD]):
        with boa.reverts(custom_err("BadCalendar()")):
            deployer.deploy(target.address, owner, emergency_owner, ts(), PERIOD, bounds)
    with boa.reverts(custom_err("BadCalendar()")):
        deployer.deploy(target.address, owner, emergency_owner, ts() + 10, PERIOD, BOUNDS)


def test_fee_grows_linearly_to_max(mock, owner):
    assert mock.fee(COLLECT) == 0
    with boa.env.prank(owner):
        mock.set_max_fee(COLLECT, WAD // 100)
    start, end = mock.epoch_time_frame(COLLECT)
    assert mock.fee(COLLECT, start) == WAD // 100 * 1 // (end - start)
    assert mock.fee(COLLECT, end - 1) == WAD // 100
    assert mock.fee(COLLECT, end) == 0
    go_to(mock, COLLECT)
    assert mock.fee() == mock.fee(COLLECT, ts())
    with boa.reverts(custom_err("BadEpoch()")):
        with boa.env.prank(owner):
            mock.set_max_fee(COLLECT | EXCHANGE, 1)


def test_burner_auction_length_is_the_exchange_frame(mock, burner):
    assert burner.auction_length() == BOUNDS[2] - BOUNDS[1]
    assert burner.want() == mock.target()
    assert burner.receiver() == mock.address


def test_collect_stages_lot_and_pays_incentive(mock, burner, owner, keeper, coin):
    amount = 100 * WAD
    coin._mint_for_testing(mock.address, amount)
    with boa.env.prank(owner):
        mock.set_max_fee(COLLECT, WAD // 10)
    with boa.reverts(custom_err("WrongEpoch()")):
        mock.collect([coin.address], keeper)
    go_to(mock, COLLECT, offset=0)
    start, end = mock.epoch_time_frame(COLLECT)
    expected_fee = amount * (WAD // 10 * (ts() + 1 - start) // (end - start)) // WAD
    with boa.env.prank(keeper):
        mock.collect([coin.address], keeper)
    assert coin.balanceOf(keeper) == expected_fee
    assert coin.balanceOf(burner.address) == amount - expected_fee
    assert coin.balanceOf(mock.address) == 0
    lot = burner.lots(coin.address)
    assert lot.initial_amount == amount - expected_fee
    assert burner.window(coin.address) == tuple(mock.epoch_time_frame(EXCHANGE))

    assert burner.available(coin.address) == 0  # not yet EXCHANGE
    go_to(mock, EXCHANGE, offset=0)
    assert burner.available(coin.address) == amount - expected_fee
    assert burner.getAmountNeeded(coin.address) == 100 * WAD  # start_total at the window start


def test_collect_requires_sorted_coins(mock, burner, keeper, coin, target, erc20):
    other = erc20.deploy("Other", "OTH", 18)
    go_to(mock, COLLECT)
    low, high = sorted([coin.address, other.address], key=lambda a: int(a, 16))
    with boa.reverts(custom_err("CoinsNotSorted()")):
        mock.collect([high, low], keeper)
    with boa.reverts(custom_err("CoinsNotSorted()")):
        mock.collect([low, low], keeper)


def test_transfer_is_burner_only_and_epoch_gated(mock, burner, coin, keeper):
    coin._mint_for_testing(mock.address, WAD)
    with boa.reverts(custom_err("OnlyBurner()")):
        mock.transfer([(coin.address, keeper, WAD)])
    with boa.env.prank(burner.address), boa.reverts(custom_err("WrongEpoch()")):
        mock.transfer([(coin.address, keeper, WAD)])
    go_to(mock, EXCHANGE)
    with boa.env.prank(burner.address):
        mock.transfer([(coin.address, keeper, 2**256 - 1)])
    assert coin.balanceOf(keeper) == WAD


def test_kill_masks(mock, burner, owner, emergency_owner, keeper, coin):
    coin._mint_for_testing(mock.address, WAD)
    go_to(mock, COLLECT)
    with boa.env.prank(emergency_owner):
        mock.set_killed(coin.address, COLLECT)
    with boa.reverts(custom_err("KilledCoin()")):
        mock.collect([coin.address], keeper)
    with boa.env.prank(owner):
        mock.set_killed(coin.address, 0)
        mock.set_killed(ZERO, COLLECT)
    with boa.reverts(custom_err("KilledEpoch()")):
        mock.collect([coin.address], keeper)
    with boa.env.prank(owner):
        mock.set_killed(ZERO, 0)
        mock.collect([coin.address], keeper)

    go_to(mock, EXCHANGE)
    assert mock.can_exchange([coin.address])
    assert burner.available(coin.address) == WAD
    with boa.env.prank(emergency_owner):
        mock.set_killed(ZERO, EXCHANGE)
    assert not mock.can_exchange([coin.address])
    assert burner.available(coin.address) == 0
    go_to(mock, SLEEP)
    assert not mock.can_exchange([coin.address])


def test_roles_and_recovery(mock, owner, emergency_owner, keeper, coin, target):
    coin._mint_for_testing(mock.address, WAD)
    target._mint_for_testing(mock.address, 2 * WAD)
    boa.env.set_balance(mock.address, 3 * WAD)
    with boa.reverts(custom_err("OnlyOwner()")):
        mock.set_calendar(ts(), PERIOD, BOUNDS)
    with boa.reverts(custom_err("OnlyOwnerOrEmergency()")):
        mock.recover([coin.address], keeper)
    with boa.env.prank(emergency_owner):
        mock.recover(
            [coin.address, target.address, "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"], keeper
        )
    assert coin.balanceOf(keeper) == WAD
    assert target.balanceOf(keeper) == 2 * WAD
    assert boa.env.get_balance(keeper) == 3 * WAD
    with boa.env.prank(owner):
        mock.set_owner(keeper)
    assert mock.owner() == keeper
