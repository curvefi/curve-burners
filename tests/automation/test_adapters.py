import boa
import pytest

from boa.util.abi import abi_decode
from tests.conftest import ZERO_ADDRESS, WEEK

SIMPLE = 1  # always-workable job from the shared jobs fixture


@pytest.fixture(autouse=True)
def fresh_week(job_board, duty_checker, checker, runner):
    boa.env.time_travel(seconds=WEEK)
    duty_checker.set_units(1)
    checker.set_units(0)
    runner.setup(0, False)


@pytest.fixture(scope="module")
def refill(accounts):
    return accounts[7]


@pytest.fixture(scope="module")
def chainlink_adapter(admin, job_board, refill):
    with boa.env.prank(admin):
        return boa.load(
            "contracts/automation/adapters/ChainlinkAdapter.vy",
            job_board, refill, ZERO_ADDRESS,
        )


@pytest.fixture(scope="module")
def gelato_resolver(admin, job_board, refill):
    with boa.env.prank(admin):
        return boa.load(
            "contracts/automation/adapters/GelatoResolver.vy",
            job_board, refill, ZERO_ADDRESS,
        )


@pytest.fixture(scope="module")
def keep3r_job(admin, job_board, keep3r_mock):
    with boa.env.prank(admin):
        return boa.load(
            "contracts/automation/adapters/Keep3rJob.vy",
            job_board, keep3r_mock,
        )


def test_chainlink_roundtrip(chainlink_adapter, job_board, target, refill, arve):
    needed, perform_data = chainlink_adapter.checkUpkeep(b"")
    assert needed  # SIMPLE and DUTCH jobs are workable with empty payload

    before = target.balanceOf(refill)
    with boa.env.prank(arve):  # permissionless perform
        chainlink_adapter.performUpkeep(perform_data)
    assert target.balanceOf(refill) > before  # rewards route to the receiver


def test_chainlink_no_work(chainlink_adapter, job_board, fee_collector, target, arve):
    # exhaust budgets: with zero allowance every quote is 0 => no upkeep
    with boa.env.prank(fee_collector.address):
        target.approve(job_board, 0)
    needed, perform_data = chainlink_adapter.checkUpkeep(b"")
    assert not needed and perform_data == b""
    with boa.env.prank(fee_collector.address):
        target.approve(job_board, 2 ** 255)


def test_gelato_payload(gelato_resolver, job_board, refill):
    from eth_utils import function_signature_to_4byte_selector

    can_exec, payload = gelato_resolver.checker()
    assert can_exec
    assert payload[:4] == function_signature_to_4byte_selector(
        "work((uint8,uint256,bytes)[],address,address)")

    inputs, receiver, payout_token = abi_decode(
        "((uint8,uint256,bytes)[],address,address)", payload[4:])
    assert receiver == refill
    assert payout_token == ZERO_ADDRESS
    assert all(data == b"" and value == 0 for _, value, data in inputs)
    assert len(inputs) > 0


def test_keep3r_gate(keep3r_job, keep3r_mock, job_board, target, arve):
    with boa.env.prank(arve):
        with boa.reverts():
            keep3r_job.work([(SIMPLE, 0, b"")])

    keep3r_mock.set_keeper(arve, True)
    before = target.balanceOf(arve)
    with boa.env.prank(arve):
        keep3r_job.work([(SIMPLE, 0, b"")])
    assert target.balanceOf(arve) > before  # keeper paid directly
