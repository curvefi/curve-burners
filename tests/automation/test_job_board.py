import boa
import pytest

from tests.conftest import ETH_ADDRESS, ZERO_ADDRESS, WEEK
from tests.automation.conftest import AMOUNT, UNIT_AMOUNT

DUTY, SIMPLE, PER_UNIT, DUTCH = 0, 1, 2, 3


@pytest.fixture(autouse=True)
def fresh_week(job_board, duty_checker, checker, runner, duty_runner):
    # start each test on a clean weekly slate and neutral mock state
    boa.env.time_travel(seconds=WEEK)
    duty_checker.set_units(1)
    checker.set_units(0)
    runner.setup(0, False)
    duty_runner.setup(0, False)


def inputs(*ids, value=0, data=b""):
    return [(i, value, data) for i in ids]


def test_erc165_and_set_hooker(job_board, fee_collector, admin):
    assert job_board.supportsInterface(bytes.fromhex("01ffc9a7"))
    assert job_board.supportsInterface(bytes.fromhex("e569b44d"))
    with boa.env.prank(admin):
        fee_collector.set_hooker(job_board)  # validates HOOKER_INTERFACE_ID on-chain
    assert fee_collector.hooker() == job_board.address


def test_active_jobs_and_buffer(job_board, jobs):
    assert job_board.active_jobs() == list(range(len(jobs)))
    expected = AMOUNT * 1 + AMOUNT * 2 + UNIT_AMOUNT * 5 + AMOUNT * 10
    assert job_board.buffer_amount() == expected


def test_workable_hint(job_board, checker):
    ok, units, quote = job_board.workable(PER_UNIT, b"")
    assert (ok, units, quote) == (False, 0, 0)  # checker says no work

    checker.set_units(3)
    ok, units, quote = job_board.workable(PER_UNIT, b"")
    assert ok and units == 3
    assert quote == 3 * UNIT_AMOUNT

    assert job_board.workable(100, b"") == (False, 0, 0)  # nonexistent id


def test_work_pays_target(job_board, target, runner, arve):
    _, _, quote = job_board.workable(SIMPLE, b"")
    assert quote == AMOUNT

    before = target.balanceOf(arve)
    with boa.env.prank(arve):
        paid = job_board.work(inputs(SIMPLE))
    assert paid == AMOUNT
    assert target.balanceOf(arve) - before == AMOUNT
    assert runner.calls() == 1


def test_receiver_param(job_board, target, arve, accounts):
    receiver = accounts[5]
    before = target.balanceOf(receiver)
    with boa.env.prank(arve):
        job_board.work(inputs(SIMPLE), receiver)
    assert target.balanceOf(receiver) - before == AMOUNT


def test_cooldown_limit_and_weekly_reset(job_board, arve):
    with boa.env.prank(arve):
        assert job_board.work(inputs(SIMPLE)) == AMOUNT  # 1/2
        assert job_board.work(inputs(SIMPLE)) == AMOUNT  # 2/2
        assert job_board.work(inputs(SIMPLE)) == 0  # limit reached: executes, pays 0

    boa.env.time_travel(seconds=WEEK)
    with boa.env.prank(arve):
        assert job_board.work(inputs(SIMPLE)) == AMOUNT  # reset


def test_no_double_pay_in_batch(job_board, arve):
    with boa.env.prank(arve):
        paid = job_board.work(inputs(SIMPLE, SIMPLE, SIMPLE))
    assert paid == 2 * AMOUNT  # limit=2 caps the repeated job within one batch


def test_per_unit_payment(job_board, checker, runner, arve):
    checker.set_units(100)  # hint: plenty of work
    runner.setup(3, False)  # target reports 3 successful units

    with boa.env.prank(arve):
        paid = job_board.work(inputs(PER_UNIT))
    assert paid == 3 * UNIT_AMOUNT

    runner.setup(10, False)  # reports more than remaining limit (5 - 3 = 2)
    with boa.env.prank(arve):
        paid = job_board.work(inputs(PER_UNIT))
    assert paid == 2 * UNIT_AMOUNT  # capped by weekly limit


def test_checker_gates_execution(job_board, checker, runner, arve):
    checker.set_units(0)
    calls_before = runner.calls()
    with boa.env.prank(arve):
        assert job_board.work(inputs(PER_UNIT)) == 0
    assert runner.calls() == calls_before  # skipped, not executed


def test_failed_optional_job_skipped(job_board, runner, arve):
    runner.setup(0, True)  # target reverts
    with boa.env.prank(arve):
        assert job_board.work(inputs(SIMPLE)) == 0  # no revert, no pay


def test_dutch_quote_grows_and_matches_execution(job_board, target, arve):
    start, end = 1000, 200000
    ts = boa.env.evm.vm.state.timestamp
    week_start = ts - (ts - 1600300800) % WEEK

    # jump into the NEXT week's window (always a positive travel)
    boa.env.time_travel(seconds=week_start + WEEK + start + (end - start) // 4 - ts)
    _, _, quote_early = job_board.workable(DUTCH, b"")

    boa.env.time_travel(seconds=(end - start) // 2)
    _, _, quote_late = job_board.workable(DUTCH, b"")
    assert 0 < quote_early < quote_late <= AMOUNT

    before = target.balanceOf(arve)
    with boa.env.prank(arve):
        paid = job_board.work(inputs(DUTCH))
    assert paid == quote_late  # simulation matches execution
    assert target.balanceOf(arve) - before == paid


def test_value_guard(job_board, arve):
    boa.env.set_balance(arve, 10 ** 18)
    with boa.env.prank(arve):
        with boa.reverts():
            job_board.work(inputs(SIMPLE, value=1))  # value not provided
        job_board.work(inputs(SIMPLE, value=1), value=1)  # keeper brings own


def test_payout_in_eth(job_board, admin, arve):
    rate = 2 * 10 ** 18  # 1 ETH = 2 target
    with boa.env.prank(admin):
        job_board.set_rate(ETH_ADDRESS, rate)
    boa.env.set_balance(job_board.address, 10 ** 18)  # fund native pool

    _, _, quote = job_board.workable(SIMPLE, b"", ETH_ADDRESS)
    assert quote == AMOUNT * 10 ** 18 // rate

    before = boa.env.get_balance(arve)
    with boa.env.prank(arve):
        paid = job_board.work(inputs(SIMPLE), arve, ETH_ADDRESS)
    assert paid == quote
    assert boa.env.get_balance(arve) - before == quote


def test_unlisted_token_quotes_zero(job_board, coins, target, arve):
    # coins includes target itself (always payable) — pick a truly unlisted one
    unlisted = next(c for c in coins if c.address != target.address).address
    ok, units, quote = job_board.workable(SIMPLE, b"", unlisted)
    assert ok and quote == 0
    with boa.env.prank(arve):
        assert job_board.work(inputs(SIMPLE), arve, unlisted) == 0


def test_quote_capped_by_allowance(job_board, fee_collector, target, arve):
    with boa.env.prank(fee_collector.address):
        target.approve(job_board, AMOUNT // 2)
    _, _, quote = job_board.workable(SIMPLE, b"")
    assert quote == AMOUNT // 2  # honest quote

    with boa.env.prank(arve):
        assert job_board.work(inputs(SIMPLE)) == AMOUNT // 2  # no revert

    with boa.env.prank(fee_collector.address):
        target.approve(job_board, 2 ** 255)  # restore for other tests


def test_duty_act_requires_all_duties(job_board, arve):
    with boa.env.prank(arve):
        with boa.reverts():
            job_board.duty_act(inputs(SIMPLE))  # duty job 0 missing
        job_board.duty_act(inputs(DUTY, SIMPLE))


def test_duty_no_work_skips(job_board, duty_checker, duty_runner, arve):
    duty_checker.set_units(0)
    calls_before = duty_runner.calls()
    with boa.env.prank(arve):
        job_board.duty_act(inputs(DUTY))  # must NOT revert: forward() safety
    assert duty_runner.calls() == calls_before


def test_duty_failure_reverts(job_board, duty_runner, arve):
    duty_runner.setup(0, True)  # checker says work exists, target fails
    with boa.env.prank(arve):
        with boa.reverts():
            job_board.duty_act(inputs(DUTY))


def test_only_owner(job_board, jobs, arve):
    with boa.env.prank(arve):
        with boa.reverts():
            job_board.set_jobs(jobs)
        with boa.reverts():
            job_board.set_rate(ETH_ADDRESS, 1)


def test_set_rate_rejects_target(job_board, admin, target):
    with boa.env.prank(admin):
        with boa.reverts():
            job_board.set_rate(target.address, 10 ** 18)


def test_recover(job_board, fee_collector, coins, admin):
    coin = coins[0]
    coin._mint_for_testing(job_board, 10 ** 18)
    before = coin.balanceOf(fee_collector)
    with boa.env.prank(admin):
        job_board.recover([coin.address])
    assert coin.balanceOf(fee_collector) - before == 10 ** 18


def test_forward_integration(job_board, fee_collector, target, duty_runner, admin, arve,
                             set_epoch, burner):
    """Full weekly flow: FeeCollector.forward -> duty_act through the real epoch."""
    from tests.conftest import Epoch

    with boa.env.prank(admin):
        fee_collector.set_hooker(job_board)

    target._mint_for_testing(fee_collector, 10 ** 20)
    set_epoch(Epoch.FORWARD)

    calls_before = duty_runner.calls()
    before = target.balanceOf(arve)
    with boa.env.prank(arve):
        fee_collector.forward([(DUTY, 0, b"")], arve)
    assert duty_runner.calls() == calls_before + 1  # duty executed through forward
    assert target.balanceOf(arve) > before  # forward fee + duty compensation
