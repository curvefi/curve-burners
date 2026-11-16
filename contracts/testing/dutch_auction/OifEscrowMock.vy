# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title OifEscrowMock
@author Curve Finance
@license MIT
@notice Minimal OIF escrow input settler stand-in for the OifEscrowAdapter
        tests: only orderIdentifier, the view the adapter reads. The id
        commits to this contract, the chain and every order field like
        StandardOrderType.orderIdentifier does, but is not byte-identical to
        it. Opening, finalising and refunding against the real escrow are
        the fork tests' job.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

from contracts.burners.adapters.oif import oif


@external
@view
def orderIdentifier(_order: oif.StandardOrder) -> bytes32:
    return keccak256(abi_encode(chain.id, self, _order))
