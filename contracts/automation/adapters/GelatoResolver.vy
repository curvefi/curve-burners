# pragma version 0.5.0a4
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
# Compiler: vyper@03e096e74b53993e652ed83dddecbee6f889fcc5
"""
@title Gelato Resolver for JobBoard
@author Curve Finance
@license MIT
@notice Backup liveness via Gelato Automate. Covers only jobs workable with
        an empty payload (hint-mode jobs): jobs whose checkers require an
        off-chain payload naturally quote 0 and are skipped.
"""

MAX_JOBS: constant(uint256) = 32
DATA_LEN: constant(uint256) = 8192

# ABI-identical to JobBoard's JobInput ((uint8,uint256,bytes)) — `bytes` is
# ABI-dynamic, so a smaller Vyper bound does not change the wire encoding.
# The adapter only ever sends data=b"", so Bytes[32] suffices and keeps
# abi_encode bounds small.
struct HintInput:
    job_id: uint8
    value: uint256
    data: Bytes[32]

# abi_encode bound: 4 (method_id) + 2*32 (receiver, token heads)
#   + 32 (array offset) + 32 (len) + 32*32 (element offsets)
#   + 32*(3*32 + 2*32) (tuples with padded empty bytes) ~= 6308
PAYLOAD_LEN: constant(uint256) = 8192


interface JobBoard:
    def active_jobs() -> DynArray[uint256, MAX_JOBS]: view
    def workable(_job_id: uint256, _data: Bytes[DATA_LEN], _payout_token: address,
                 _ts: uint256) -> (bool, uint256, uint256): view


job_board: public(immutable(JobBoard))
receiver: public(immutable(address))  # rewards recycled here (e.g. refill funds)
payout_token: public(immutable(address))


@deploy
def __init__(_job_board: JobBoard, _receiver: address, _payout_token: address):
    self.job_board = _job_board
    self.receiver = _receiver
    self.payout_token = _payout_token


@view
@external
def checker() -> (bool, Bytes[PAYLOAD_LEN]):
    """
    @notice Gelato resolver entry: collect workable hint-mode jobs and
            build the `work()` payload
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

    payload: Bytes[PAYLOAD_LEN] = abi_encode(
        inputs, self.receiver, self.payout_token,
        method_id=method_id("work((uint8,uint256,bytes)[],address,address)"),
    )
    return (True, payload)
