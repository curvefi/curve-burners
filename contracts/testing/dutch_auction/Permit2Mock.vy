# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title Permit2Mock
@author Curve Finance
@license MIT
@notice Minimal Permit2 stand-in: a fixed EIP-712 domain separator for
        UniswapXAdapter deployments in tests.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

DOMAIN_SEPARATOR: public(immutable(bytes32))


@deploy
def __init__(_domain_separator: bytes32):
    self.DOMAIN_SEPARATOR = _domain_separator
