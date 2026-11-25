# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title RouterMock
@author Curve Finance
@license MIT
@notice Curve router NG stand-in for keeper tests: `get_dy` and `exchange`
        over the route's token hops with a fixed rate per pair and a
        constant-product price impact, dy = amount * rate * depth / (depth +
        amount). Holds the out tokens it pays with.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

from ethereum.ercs import IERC20


struct Pair:
    rate: uint256  # out per in, 1e18 = 1:1
    depth: uint256  # in-token amount at which the impact halves the rate; 0 = none


MAX_HOPS: constant(uint256) = 5

pairs: public(HashMap[address, HashMap[address, Pair]])


@external
def set_pair(_token_in: address, _token_out: address, _rate: uint256, _depth: uint256):
    self.pairs[_token_in][_token_out] = Pair(rate=_rate, depth=_depth)


@internal
@view
def _hop(_token_in: address, _token_out: address, _amount: uint256) -> uint256:
    pair: Pair = self.pairs[_token_in][_token_out]
    assert pair.rate != 0, "no pair"
    dy: uint256 = _amount * pair.rate // 10**18
    if pair.depth != 0:
        dy = dy * pair.depth // (pair.depth + _amount)
    return dy


@internal
@view
def _quote(_route: address[11], _amount: uint256) -> (uint256, address):
    amount: uint256 = _amount
    token: address = _route[0]
    for i: uint256 in range(MAX_HOPS):
        if _route[2 * i + 1] == empty(address):
            break
        token_out: address = _route[2 * i + 2]
        amount = self._hop(token, token_out, amount)
        token = token_out
    return amount, token


@external
@view
def get_dy(
    _route: address[11],
    _swap_params: uint256[5][5],
    _amount: uint256,
    _pools: address[5],
) -> uint256:
    dy: uint256 = 0
    token_out: address = empty(address)
    dy, token_out = self._quote(_route, _amount)
    return dy


@external
def exchange(
    _route: address[11],
    _swap_params: uint256[5][5],
    _amount: uint256,
    _min_dy: uint256,
    _pools: address[5],
    _receiver: address,
) -> uint256:
    dy: uint256 = 0
    token_out: address = empty(address)
    dy, token_out = self._quote(_route, _amount)
    assert dy >= _min_dy, "slippage"
    assert extcall IERC20(_route[0]).transferFrom(msg.sender, self, _amount, default_return_value=True)
    assert extcall IERC20(token_out).transfer(_receiver, dy, default_return_value=True)
    return dy
