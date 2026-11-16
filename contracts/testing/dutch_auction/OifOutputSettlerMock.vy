# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title OifOutputSettlerMock
@author Curve Finance
@license MIT
@notice Minimal OIF output settler stand-in for the OifEscrowAdapter tests:
        fill follows OutputSettlerBase._fill for what the adapter relies on —
        the fill record written per order id and output, idempotently, and
        the signed amount transferred from the filler to the recipient in
        the same call — and getFillRecord reads it back. Not modelled:
        native outputs, callbacks, the context order types (the signed
        amount is always paid), attestations.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

from ethereum.ercs import IERC20

from contracts.burners.adapters.oif import oif


error FillDeadline:
    pass


error ZeroValue:
    pass


# OutputSettlerBase._fillRecords: keccak(solver, timestamp) once filled.
fill_records: HashMap[bytes32, HashMap[bytes32, bytes32]]


@internal
@pure
def _output_hash(_output: oif.MandateOutput) -> bytes32:
    return keccak256(abi_encode(_output))


@external
@view
def getFillRecord(_order_id: bytes32, _output: oif.MandateOutput) -> bytes32:
    return self.fill_records[_order_id][self._output_hash(_output)]


@external
def fill(
    _order_id: bytes32, _output: oif.MandateOutput, _fill_deadline: uint48, _filler_data: bytes32
) -> bytes32:
    """
    @notice OutputSettlerBase.fill for an ERC-20 output; `_filler_data` is
            the solver identifier.
    """
    assert convert(_fill_deadline, uint256) >= block.timestamp, FillDeadline()
    assert _filler_data != empty(bytes32), ZeroValue()
    output_hash: bytes32 = self._output_hash(_output)
    record: bytes32 = self.fill_records[_order_id][output_hash]
    if record != empty(bytes32):
        return record
    record = keccak256(concat(_filler_data, convert(convert(block.timestamp, uint32), bytes4)))
    self.fill_records[_order_id][output_hash] = record
    assert extcall IERC20(oif._from_identifier(convert(_output.token, uint256))).transferFrom(
        msg.sender,
        oif._from_identifier(convert(_output.recipient, uint256)),
        _output.amount,
        default_return_value=True,
    )
    return record
