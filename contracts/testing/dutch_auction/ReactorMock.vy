# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title ReactorMock
@author Curve Finance
@license MIT
@notice Minimal UniswapX reactor stand-in: a fixed Permit2 address for
        UniswapXAdapter deployments in tests. Settlement itself is exercised
        against the real reactor in the fork test.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

permit2: public(immutable(address))


@deploy
def __init__(_permit2: address):
    self.permit2 = _permit2
