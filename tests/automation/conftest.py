import boa
import pytest

from tests.conftest import ZERO_ADDRESS

AMOUNT = 10 ** 15  # reward per unit in target terms
UNIT_AMOUNT = 10 ** 13  # per-unit reward for the per_unit job
FULL_WEEK = (0, 0, False)  # (start, end, dutch): end <= start wraps to full week

CHECKER_SOURCE = """
#pragma version 0.3.10
units: public(uint256)

@external
def set_units(_units: uint256):
    self.units = _units

@external
@view
def check() -> uint256:
    return self.units
"""

RUNNER_SOURCE = """
#pragma version 0.3.10
calls: public(uint256)
ret: public(uint256)
fail: public(bool)

@external
def setup(_ret: uint256, _fail: bool):
    self.ret = _ret
    self.fail = _fail

@external
@payable
def run() -> uint256:
    assert not self.fail
    self.calls += 1
    return self.ret
"""


@pytest.fixture(scope="module")
def checker_deployer():
    return boa.loads_partial(CHECKER_SOURCE)


@pytest.fixture(scope="module")
def runner_deployer():
    return boa.loads_partial(RUNNER_SOURCE)


@pytest.fixture(scope="module")
def checker(admin, checker_deployer):
    with boa.env.prank(admin):
        return checker_deployer.deploy()


@pytest.fixture(scope="module")
def runner(admin, runner_deployer):
    with boa.env.prank(admin):
        return runner_deployer.deploy()


@pytest.fixture(scope="module")
def duty_runner(admin, runner_deployer):
    with boa.env.prank(admin):
        return runner_deployer.deploy()


@pytest.fixture(scope="module")
def duty_checker(admin, checker_deployer):
    with boa.env.prank(admin):
        instance = checker_deployer.deploy()
        instance.set_units(1)
    return instance


def make_job(target, foreplay, checker_addr=ZERO_ADDRESS, checker_foreplay=b"",
             per_unit=False, duty=False, amount=AMOUNT, window=FULL_WEEK, limit=2):
    start, end, dutch = window
    return (
        target, foreplay, checker_addr, checker_foreplay, per_unit, duty,
        (amount, start, end, dutch, (0, 0, limit)),  # Payment(amount, start, end, dutch, Cooldown)
    )


@pytest.fixture(scope="module")
def jobs(duty_runner, duty_checker, runner, checker):
    run_fp = runner.run.prepare_calldata()  # method_id only
    return [
        # 0: duty, gated by duty_checker
        make_job(duty_runner.address, duty_runner.run.prepare_calldata(),
                 duty_checker.address, duty_checker.check.prepare_calldata(),
                 duty=True, limit=1),
        # 1: optional, always workable (no checker)
        make_job(runner.address, run_fp),
        # 2: per_unit, counted by checker (hint) and by run() return (payment)
        make_job(runner.address, run_fp, checker.address, checker.check.prepare_calldata(),
                 per_unit=True, amount=UNIT_AMOUNT, limit=5),
        # 3: dutch over a mid-week window
        make_job(runner.address, run_fp, window=(1000, 200000, True), limit=10),
    ]


@pytest.fixture(scope="module")
def job_board(admin, fee_collector, target, jobs):
    with boa.env.prank(admin):
        board = boa.load("contracts/automation/JobBoard.vy", fee_collector, jobs)

    # Budget: mimic FeeCollector's weekly buffer approve generously for unit tests
    target._mint_for_testing(fee_collector, 10 ** 20)
    with boa.env.prank(fee_collector.address):
        target.approve(board, 2 ** 255)
    return board


@pytest.fixture(scope="module")
def keep3r_mock(admin):
    with boa.env.prank(admin):
        return boa.loads("""
#pragma version 0.3.10
keeper: public(HashMap[address, bool])

@external
def set_keeper(_keeper: address, _ok: bool):
    self.keeper[_keeper] = _ok

@external
def isKeeper(_keeper: address) -> bool:
    return self.keeper[_keeper]
""")
