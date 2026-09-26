# pragma version 0.5.0a4
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
# Compiler: vyper@03e096e74b53993e652ed83dddecbee6f889fcc5
"""
@title Chainlink Automation Adapter for JobBoard
@author Curve Finance
@license MIT
@notice Backup liveness via a custom-logic upkeep. Covers only jobs workable
        with an empty payload (hint-mode jobs). Rewards route to `receiver`
        (e.g. an address recycling them into LINK refills).
"""

MAX_JOBS: constant(uint256) = 32
DATA_LEN: constant(uint256) = 8192

# ABI-identical to JobBoard's JobInput ((uint8,uint256,bytes)) — `bytes` is
# ABI-dynamic, so a smaller Vyper bound does not change the wire encoding.
# This adapter only ever passes data=b"".
struct HintInput:
    job_id: uint8
    value: uint256
    data: Bytes[32]

# abi_encode bound for DynArray[HintInput, 32] with empty data ~= 6244
PAYLOAD_LEN: constant(uint256) = 8192


interface JobBoard:
    def active_jobs() -> DynArray[uint256, MAX_JOBS]: view
    def workable(_job_id: uint256, _data: Bytes[DATA_LEN], _payout_token: address,
                 _ts: uint256) -> (bool, uint256, uint256): view
    def work(_inputs: DynArray[HintInput, MAX_JOBS], _receiver: address,
             _payout_token: address) -> uint256: payable


job_board: public(immutable(JobBoard))
receiver: public(immutable(address))
payout_token: public(immutable(address))


@deploy
def __init__(_job_board: JobBoard, _receiver: address, _payout_token: address):
    self.job_board = _job_board
    self.receiver = _receiver
    self.payout_token = _payout_token


@view
@external
def checkUpkeep(_check_data: Bytes[32]) -> (bool, Bytes[PAYLOAD_LEN]):
    """
    @notice Chainlink Automation check: collect workable hint-mode jobs
    @return (upkeepNeeded, performData)
    """
    job_ids: DynArray[uint256, MAX_JOBS] = staticcall self.job_board.active_jobs()

    inputs: DynArray[HintInput, MAX_JOBS] = []
    for job_id: uint256 in job_ids:
        ok: bool = False
        units: uint256 = 0
        quote: uint256 = 0
        ok, units, quote = staticcall self.job_board.workable(
            job_id, b"", self.payout_token, block.timestamp,
        )
        if ok and quote > 0:
            inputs.append(HintInput(job_id=convert(job_id, uint8), value=0, data=b""))

    if len(inputs) == 0:
        return (False, b"")
    return (True, abi_encode(inputs))


@external
def performUpkeep(_perform_data: Bytes[PAYLOAD_LEN]):
    """
    @notice Chainlink Automation perform: execute collected jobs
    @dev Permissionless by design — JobBoard.work is permissionless anyway,
         rewards always go to the configured receiver, no value is forwarded
    """
    inputs: DynArray[HintInput, MAX_JOBS] = abi_decode(_perform_data, DynArray[HintInput, MAX_JOBS])
    extcall self.job_board.work(inputs, self.receiver, self.payout_token)
