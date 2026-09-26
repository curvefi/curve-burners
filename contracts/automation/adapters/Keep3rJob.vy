# pragma version 0.5.0a4
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
# Compiler: vyper@03e096e74b53993e652ed83dddecbee6f889fcc5
"""
@title Keep3r Job Wrapper for JobBoard
@author Curve Finance
@license MIT
@notice Lets bonded Keep3r keepers work JobBoard jobs; the keeper receives
        the JobBoard payout directly (crvUSD / native / whitelisted token).
        Zero coupling: if Keep3r dies, nothing else breaks.
"""

MAX_JOBS: constant(uint256) = 32
DATA_LEN: constant(uint256) = 8192


struct JobInput:
    job_id: uint8
    value: uint256
    data: Bytes[DATA_LEN]


interface JobBoard:
    def work(_inputs: DynArray[JobInput, MAX_JOBS], _receiver: address,
             _payout_token: address) -> uint256: payable

interface IKeep3r:
    def isKeeper(_keeper: address) -> bool: nonpayable


error NotKeeper:
    pass


job_board: public(immutable(JobBoard))
keep3r: public(immutable(IKeep3r))


@deploy
def __init__(_job_board: JobBoard, _keep3r: IKeep3r):
    self.job_board = _job_board
    self.keep3r = _keep3r


@external
@payable
def work(_inputs: DynArray[JobInput, MAX_JOBS],
         _payout_token: address = empty(address)) -> uint256:
    """
    @notice Keep3r entry: validate the keeper, forward to JobBoard,
            pay the keeper directly
    @return Amount paid in the payout token
    """
    assert extcall self.keep3r.isKeeper(msg.sender), NotKeeper()
    return extcall self.job_board.work(_inputs, msg.sender, _payout_token, value=msg.value)
