# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title Permit2Mock
@author Curve Finance
@license MIT
@notice Minimal Permit2 stand-in: a fixed EIP-712 domain separator for
        Permit2-based adapter deployments in tests. Settlement through the
        real Permit2 is the fork tests' job.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

DOMAIN_SEPARATOR: public(immutable(bytes32))


@deploy
def __init__(_domain_separator: bytes32):
    self.DOMAIN_SEPARATOR = _domain_separator
