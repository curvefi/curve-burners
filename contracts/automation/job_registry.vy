# pragma version 0.5.0a4
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
# Compiler: vyper@03e096e74b53993e652ed83dddecbee6f889fcc5
"""
@title JobBoard registry module
@author Curve Finance
@license MIT
@notice Job storage. Jobs are declared inline (DAO-fixed `foreplay` prefix +
        keeper-supplied data suffix); anything more complex than
        "prefix + keeper bytes" should live in a dedicated wrapper contract
        that `target` points to (Maker dss-cron model with an inline shortcut).
"""

from . import timing


error EmptyTarget:
    pass


error BadWindow:
    pass


error DirtyCooldown:
    pass


MAX_JOBS: constant(uint256) = 32  # duty mask is a uint256 over job ids
FOREPLAY_LEN: constant(uint256) = 1024
DATA_LEN: constant(uint256) = 8192


struct Cooldown:
    week: uint64  # week number of the last reset
    used: uint64  # units paid out this week
    limit: uint64  # max paid units per week


struct Payment:
    amount: uint256  # max reward per unit, DENOMINATED IN TARGET
    start: uint256  # payout window inside week, [start, end)
    end: uint256
    dutch: bool  # linear 0 -> amount across window
    cooldown: Cooldown


struct Job:
    target: address
    foreplay: Bytes[FOREPLAY_LEN]  # method_id + const args; keeper data appended
    checker: address  # optional; empty => always workable, units=1
    checker_foreplay: Bytes[FOREPLAY_LEN]
    per_unit: bool  # units taken from work-call return value (uint256)
    duty: bool  # mandatory during weekly duty_act
    payment: Payment


# ABI-compatible with FeeCollector's HookInput: (uint8,uint256,bytes)
struct JobInput:
    job_id: uint8
    value: uint256
    data: Bytes[DATA_LEN]


event SetJobs:
    n_jobs: uint256
    buffer_amount: uint256


jobs: public(DynArray[Job, MAX_JOBS])
duties_checklist: public(uint256)  # mask of jobs with `duty` flag
buffer_amount: public(uint256)  # weekly target amount to keep approved


@deploy
def __init__():
    pass


@internal
def _set_jobs(new_jobs: DynArray[Job, MAX_JOBS]):
    """
    @notice Replace the whole job list. Cooldowns are reset.
    @dev Execute at week boundaries: resetting cooldowns mid-week lets
         pool-paid (non-target) limits be earned again within the same week.
    """
    week: uint64 = convert(timing.week_number(block.timestamp), uint64)

    jobs: DynArray[Job, MAX_JOBS] = []
    buffer: uint256 = 0
    mask: uint256 = 0
    for i: uint256 in range(len(new_jobs), bound=MAX_JOBS):
        job: Job = new_jobs[i]
        assert job.target != empty(address), EmptyTarget()
        assert job.payment.start < timing.WEEK, BadWindow()
        assert job.payment.end < timing.WEEK, BadWindow()
        assert job.payment.cooldown.used == 0, DirtyCooldown()

        job.payment.cooldown.week = week
        jobs.append(job)

        buffer += job.payment.amount * convert(job.payment.cooldown.limit, uint256)
        if job.duty:
            mask |= 1 << i

    self.jobs = jobs
    self.duties_checklist = mask
    self.buffer_amount = buffer
    log SetJobs(n_jobs=len(jobs), buffer_amount=buffer)
