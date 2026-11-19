# pragma version 0.5.0a4
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
# Compiler: vyper@03e096e74b53993e652ed83dddecbee6f889fcc5
"""
@title JobBoard (Hooker v2)
@author Curve Finance
@license MIT
@notice dss-cron-style keeper job registry for Curve DAO.
        Implements the Hooker interface, so integration is a single
        `fee_collector.set_hooker(job_board)` — buffer mechanics, weekly
        approve and duty semantics are preserved.
        Searcher API: `active_jobs()` (static registry), `workable()`
        (hint with empty data / honest quote with payload), `work()`
        (permissionless, receiver + payout token chosen by keeper).
@dev Rewards are priced in target; keepers pick the payout token
     (target by default, or a rate-whitelisted token from this contract's
     pool — fund the native pool by plain transfer).
"""

from . import timing
from . import job_registry as reg
from . import payments as pay
from ..utils import recovery

initializes: reg
initializes: pay

exports: (
    reg.jobs,
    reg.duties_checklist,
    reg.buffer_amount,
    pay.rate,
)


interface FeeCollector:
    def target() -> address: view
    def owner() -> address: view
    def emergency_owner() -> address: view


error OnlyOwner:
    pass


error NotAllDuties:
    pass


error DutyFailed:
    pass


error ValueExceeded:
    pass


error LengthMismatch:
    pass


error TargetRateFixed:
    pass


event DutyAct:
    pass

event JobShot:
    job_id: indexed(uint8)
    units: uint256
    reward: uint256  # in target terms

event Act:
    receiver: indexed(address)
    payout_token: indexed(address)
    reward: uint256  # accrued, in target terms
    paid: uint256  # actually paid, in payout token

event OneTimeCall:
    target: indexed(address)


SUPPORTED_INTERFACES: constant(bytes4[2]) = [
    # ERC165: method_id("supportsInterface(bytes4)") == 0x01ffc9a7
    0x01ffc9a7,
    # Hooker:
    #   method_id("duty_act((uint8,uint256,bytes)[],address)") == 0x8c88eb86
    #   method_id("buffer_amount()") == 0x69e15fcb
    0xe569b44d,
]

MAX_CALLS: constant(uint256) = 8

fee_collector: public(immutable(FeeCollector))


@deploy
def __init__(_fee_collector: FeeCollector, _initial_jobs: DynArray[reg.Job, reg.MAX_JOBS]):
    """
    @param _fee_collector JobBoard is hooked to fee_collector with no update possibility
    @param _initial_jobs Jobs to set at initialization
    """
    self.fee_collector = _fee_collector
    reg.__init__()
    pay.__init__()
    reg._set_jobs(_initial_jobs)


@external
@payable
def __default__():
    # Native coin funding: plain transfer tops up the payout pool.
    # Note: nonreentrancy pragma locks this during work() — refunds sent
    # back by job targets mid-call revert; fund outside of work txs.
    pass


# ─────────────────────────── searcher API ───────────────────────────

@view
@external
def active_jobs() -> DynArray[uint256, reg.MAX_JOBS]:
    """
    @notice Static registry listing (Maker `activeJobs` semantics), NOT
            executability. Poll rarely; poll `workable` every block.
    """
    ids: DynArray[uint256, reg.MAX_JOBS] = []
    for i: uint256 in range(len(reg.jobs), bound=reg.MAX_JOBS):
        ids.append(i)
    return ids


@view
@internal
def _check(job: reg.Job, data: Bytes[reg.DATA_LEN]) -> uint256:
    """
    @notice Ask the job's checker how many units of work are available.
            data=b"" is HINT mode: "is there work worth preparing payload for".
    @return Units of work; 0 = not workable
    """
    if job.checker == empty(address):
        return 1

    ok: bool = False
    resp: Bytes[32] = b""
    ok, resp = raw_call(
        job.checker,
        concat(job.checker_foreplay, data),
        max_outsize=32,
        is_static_call=True,
        revert_on_failure=False,
    )
    if not ok or len(resp) < 32:
        return 0
    units: uint256 = extract32(resp, 0, output_type=uint256)
    if not job.per_unit:
        # gate-style checkers are 0/1: never scale reward by checker output
        units = min(units, 1)
    return units


@view
@external
def workable(_job_id: uint256, _data: Bytes[reg.DATA_LEN],
             _payout_token: address = empty(address),
             _ts: uint256 = block.timestamp) -> (bool, uint256, uint256):
    """
    @notice Quote a job. With _data=b"" acts as a cheap hint; with a prepared
            payload the quote is exact and capped by available budget, so
            simulation matches execution.
    @param _job_id Job to quote
    @param _data Keeper payload (appended after DAO-fixed foreplay)
    @param _payout_token Payout token (empty = target)
    @param _ts Timestamp to evaluate at (current by default)
    @return (workable, units, payout in chosen token).
            `workable` means checker-approved, NOT profitable: quote can be 0
            when out of payment window or the chosen budget is empty.
    """
    if _job_id >= len(reg.jobs):
        return (False, 0, 0)
    job: reg.Job = reg.jobs[_job_id]

    units: uint256 = self._check(job, _data)
    if units == 0:
        return (False, 0, 0)

    cd: reg.Cooldown = job.payment.cooldown
    if convert(cd.week, uint256) < timing.week_number(_ts):
        cd.used = 0
    payable_units: uint256 = min(units, convert(cd.limit - min(cd.used, cd.limit), uint256))

    reward: uint256 = timing.reward_per_unit(
        job.payment.amount, job.payment.start, job.payment.end, job.payment.dutch, _ts,
    ) * payable_units
    quote: uint256 = pay._quote(
        reward, _payout_token,
        pay.ERC20(staticcall self.fee_collector.target()), self.fee_collector.address,
    )
    return (True, units, quote)


@internal
@payable
def _work(_inputs: DynArray[reg.JobInput, reg.MAX_JOBS], _receiver: address,
          _payout_token: address) -> uint256:
    week: uint256 = timing.week_number(block.timestamp)
    total_reward: uint256 = 0
    value_used: uint256 = 0

    for inp: reg.JobInput in _inputs:
        job: reg.Job = reg.jobs[convert(inp.job_id, uint256)]  # reverts on bad id

        value_used += inp.value
        assert value_used <= msg.value, ValueExceeded()  # keeper brings own native

        # checker gates execution for ALL jobs. Duty presence is enforced by
        # the duty_act mask, but a genuine "no work" result skips the call —
        # a duty target must never be able to brick FeeCollector.forward()
        units: uint256 = self._check(job, inp.data)
        if units == 0:
            log JobShot(job_id=inp.job_id, units=0, reward=0)
            continue

        ok: bool = False
        ret: Bytes[32] = b""
        ok, ret = raw_call(
            job.target,
            concat(job.foreplay, inp.data),
            value=inp.value,
            max_outsize=32,
            revert_on_failure=False,
        )
        if not ok:
            # checker said there IS work: a duty failure must surface loudly
            assert not job.duty, DutyFailed()
            log JobShot(job_id=inp.job_id, units=0, reward=0)
            continue

        if job.per_unit:
            units = 0
            if len(ret) >= 32:
                units = extract32(ret, 0, output_type=uint256)

        # weekly cooldown reset + cap; storage updated per iteration,
        # so repeating a job in one batch cannot double-pay
        cd: reg.Cooldown = job.payment.cooldown
        if convert(cd.week, uint256) < week:
            cd.used = 0
            cd.week = convert(week, uint64)
        payable_units: uint256 = min(units, convert(cd.limit - min(cd.used, cd.limit), uint256))
        cd.used += convert(payable_units, uint64)
        reg.jobs[convert(inp.job_id, uint256)].payment.cooldown = cd

        reward: uint256 = timing.reward_per_unit(
            job.payment.amount, job.payment.start, job.payment.end, job.payment.dutch, block.timestamp,
        ) * payable_units
        total_reward += reward
        log JobShot(job_id=inp.job_id, units=units, reward=reward)

    paid: uint256 = pay._payout(
        _receiver, total_reward, _payout_token,
        pay.ERC20(staticcall self.fee_collector.target()), self.fee_collector.address,
    )
    log Act(receiver=_receiver, payout_token=_payout_token, reward=total_reward, paid=paid)
    return paid


@external
@payable
def work(_inputs: DynArray[reg.JobInput, reg.MAX_JOBS], _receiver: address = msg.sender,
         _payout_token: address = empty(address)) -> uint256:
    """
    @notice Entry point for keepers: execute jobs, receive reward in one token
    @dev Unspent msg.value of skipped jobs is NOT refunded (stays as pool
         funding) — simulate before sending value with a batch
    @param _inputs Job inputs assembled by the keeper
    @param _receiver Receiver of the payout (sender by default)
    @param _payout_token Payout token (empty = target)
    @return Amount paid in the payout token
    """
    return self._work(_inputs, _receiver, _payout_token)


# ────────────────────── Hooker compatibility ────────────────────────

@external
@payable
def duty_act(_hook_inputs: DynArray[reg.JobInput, reg.MAX_JOBS],
             _receiver: address = msg.sender) -> uint256:
    """
    @notice Entry point for FeeCollector's weekly forward. All duty jobs
            must be present. Pays out in target.
    @dev Deliberately permissionless (unlike Hooker's duty_counter gating):
         payout is bounded by the same weekly limits and the FeeCollector
         allowance as work(), so off-cycle calls grant nothing extra
    @param _hook_inputs Inputs assembled by keepers
    @param _receiver Receiver of compensation (sender by default)
    @return Compensation received (in target)
    """
    mask: uint256 = 0
    for inp: reg.JobInput in _hook_inputs:
        mask |= 1 << convert(inp.job_id, uint256)
    checklist: uint256 = reg.duties_checklist
    assert mask & checklist == checklist, NotAllDuties()

    log DutyAct()
    return self._work(_hook_inputs, _receiver, empty(address))


@pure
@external
def supportsInterface(_interface_id: bytes4) -> bool:
    """
    @dev Interface identification is specified in ERC-165
    """
    return _interface_id in SUPPORTED_INTERFACES


# ─────────────────────────── admin ──────────────────────────────────

@external
def set_jobs(_new_jobs: DynArray[reg.Job, reg.MAX_JOBS]):
    """
    @notice Replace the job list
    @dev Callable only by owner; execute at week boundaries (cooldown reset)
    """
    assert msg.sender == staticcall self.fee_collector.owner(), OnlyOwner()
    reg._set_jobs(_new_jobs)


@external
def set_rate(_token: address, _rate: uint256):
    """
    @notice Set conversion rate for a payout token (target base units per
            1e18 token base units — decimals baked in). 0 disallows the token.
            Conservative values; the dutch auction absorbs staleness.
    @dev Callable only by owner
    """
    assert msg.sender == staticcall self.fee_collector.owner(), OnlyOwner()
    assert _token != staticcall self.fee_collector.target(), TargetRateFixed()
    pay._set_rate(_token, _rate)


@external
@payable
def one_time_calls(_targets: DynArray[address, MAX_CALLS],
                   _data: DynArray[Bytes[reg.DATA_LEN], MAX_CALLS],
                   _values: DynArray[uint256, MAX_CALLS]):
    """
    @notice Coin approvals, any settings that need to be executed once
    @dev Callable only by owner; may spend existing contract balance as value
    """
    assert msg.sender == staticcall self.fee_collector.owner(), OnlyOwner()
    assert len(_targets) == len(_data), LengthMismatch()
    assert len(_targets) == len(_values), LengthMismatch()

    for i: uint256 in range(len(_targets), bound=MAX_CALLS):
        raw_call(_targets[i], _data[i], value=_values[i])
        log OneTimeCall(target=_targets[i])


@external
def recover(_coins: DynArray[address, reg.MAX_JOBS]):
    """
    @notice Recover ERC20 tokens or Ether from this contract to FeeCollector
    @dev Callable only by owner and emergency owner
    """
    assert msg.sender in [
        staticcall self.fee_collector.owner(),
        staticcall self.fee_collector.emergency_owner(),
    ], OnlyOwner()

    for coin: address in _coins:
        recovery._recover_coin(recovery.ERC20(coin), self.fee_collector.address)
