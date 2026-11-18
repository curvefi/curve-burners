# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title Token helpers
@author Curve Finance
@license MIT
@notice Ported from curve-std (curve_std/token.vy): allowance-guarded ERC-20
        approval helpers. No custom events — the token's own Approval logs are
        the record.
"""

from ethereum.ercs import IERC20


@internal
def max_approve(_token: IERC20, _spender: address):
    if staticcall _token.allowance(self, _spender) == 0:
        assert extcall _token.approve(_spender, max_value(uint256), default_return_value=True)


@internal
def clear_approve(_token: IERC20, _spender: address):
    if staticcall _token.allowance(self, _spender) != 0:
        assert extcall _token.approve(_spender, 0, default_return_value=True)
