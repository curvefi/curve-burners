# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title SettlerMock
@author Curve Finance
@license MIT
@notice Minimal 0x Settler stand-in: a contract address for
        ZeroExSettlerAdapter deployments in tests (the adapter only requires
        code at the pinned Settler; it never calls it). Settlement through
        the real Settler and Permit2 is the fork test's job.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

MOCK_VERSION: public(constant(String[11])) = "SettlerMock"
